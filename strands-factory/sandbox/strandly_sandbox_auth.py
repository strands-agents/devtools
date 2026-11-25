"""Keyless provider client arguments for sandbox tests; no harness imports or token files.

Import from /opt/strandly (on the sandbox's PYTHONPATH). Install boto3 and the selected
provider SDK in the test environment. SDKs own token caching and refresh.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from contextlib import closing
from typing import Any

TOKEN_DURATION_SECONDS = 300
ANTHROPIC_AUDIENCE = "https://api.anthropic.com"
OPENAI_AUDIENCE = "https://api.openai.com/v1"
GOOGLE_TOKEN_URL = "https://sts.googleapis.com/v1/token"
GOOGLE_SUBJECT_TOKEN_TYPE = "urn:ietf:params:aws:token-type:aws4_request"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
OAUTH_API_BETA = "oauth-2025-04-20"
# Non-secret: LiteLLM requires a key value; the bearer replaces it per request.
LITELLM_KEY_PLACEHOLDER = "workload-identity"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}; configure sandbox provider federation before use")
    return value


def _reject_static_credentials(*names: str) -> None:
    for name in names:
        if os.environ.get(name):
            raise ValueError(
                f"Unset {name}; this helper only supports workload identity federation"
            )


def _region() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region or region == "aws-global":
        raise ValueError("Set AWS_REGION to a regional STS endpoint's region (not aws-global)")
    return region


def _identity_token_provider(audience: str) -> Callable[[], str]:
    region = _region()

    def get_token() -> str:
        import boto3
        import botocore.session
        from botocore.config import Config

        # A fresh session re-resolves ambient credentials, including rotating container credentials.
        # Explicitly override legacy/global STS and configured endpoint URLs for the exchange.
        session = botocore.session.get_session()
        session.set_config_variable("sts_regional_endpoints", "regional")
        aws = boto3.Session(botocore_session=session, region_name=region)
        with closing(aws.client("sts", config=Config(ignore_configured_endpoint_urls=True))) as sts:
            response = sts.get_web_identity_token(
                Audience=[audience],
                SigningAlgorithm="RS256",
                DurationSeconds=TOKEN_DURATION_SECONDS,
            )
        token = response.get("WebIdentityToken")
        if not isinstance(token, str) or not token:
            raise RuntimeError("AWS STS returned no web identity token")
        return token

    return get_token


def anthropic_client_args() -> dict[str, Any]:
    """Arguments for Anthropic/AsyncAnthropic or Strands AnthropicModel(client_args=...)."""
    _reject_static_credentials("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    config = {
        "federation_rule_id": _required("ANTHROPIC_FEDERATION_RULE_ID"),
        "organization_id": _required("ANTHROPIC_ORGANIZATION_ID"),
        "service_account_id": _required("ANTHROPIC_SERVICE_ACCOUNT_ID"),
        "workspace_id": _required("ANTHROPIC_WORKSPACE_ID"),
    }
    from anthropic import WorkloadIdentityCredentials

    return {
        "base_url": ANTHROPIC_AUDIENCE,
        "credentials": WorkloadIdentityCredentials(
            identity_token_provider=_identity_token_provider(ANTHROPIC_AUDIENCE),
            **config,
        ),
    }


def openai_client_args() -> dict[str, Any]:
    """Arguments for OpenAI/AsyncOpenAI or Strands OpenAI models' client_args."""
    _reject_static_credentials("OPENAI_API_KEY", "OPENAI_ADMIN_KEY")
    return {
        "base_url": OPENAI_AUDIENCE,
        "workload_identity": {
            "identity_provider_id": _required("OPENAI_IDENTITY_PROVIDER_ID"),
            "service_account_id": _required("OPENAI_SERVICE_ACCOUNT_ID"),
            "provider": {
                "token_type": "jwt",
                "get_token": _identity_token_provider(OPENAI_AUDIENCE),
            },
        },
    }


def _google_settings() -> tuple[str, str, str]:
    _reject_static_credentials("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
    audience = (
        f"//iam.googleapis.com/projects/{_required('GOOGLE_WIF_PROJECT_NUMBER')}"
        f"/locations/global/workloadIdentityPools/{_required('GOOGLE_WIF_POOL_ID')}"
        f"/providers/{_required('GOOGLE_WIF_PROVIDER_ID')}"
    )
    return audience, _required("GOOGLE_CLOUD_PROJECT"), _required("GOOGLE_CLOUD_LOCATION")


def google_credentials() -> Any:
    """google-auth credentials for Vertex AI, exchanged from the sandbox role on each refresh."""
    audience, _, _ = _google_settings()
    region = _region()
    from google.auth import aws

    class AmbientAwsCredentials(aws.AwsSecurityCredentialsSupplier):
        def get_aws_security_credentials(self, context: Any, request: Any) -> Any:
            import boto3

            credentials = boto3.Session(region_name=region).get_credentials()
            if credentials is None:
                raise RuntimeError("No ambient AWS credentials for the Google exchange")
            frozen = credentials.get_frozen_credentials()
            return aws.AwsSecurityCredentials(frozen.access_key, frozen.secret_key, frozen.token)

        def get_aws_region(self, context: Any, request: Any) -> str:
            return region

    return aws.Credentials(
        audience=audience,
        subject_token_type=GOOGLE_SUBJECT_TOKEN_TYPE,
        token_url=GOOGLE_TOKEN_URL,
        aws_security_credentials_supplier=AmbientAwsCredentials(),
        scopes=[GOOGLE_SCOPE],
    )


def gemini_client_args() -> dict[str, Any]:
    """Arguments for google.genai.Client or Strands GeminiModel(client_args=...), on Vertex AI."""
    _, project, location = _google_settings()
    return {
        "vertexai": True,
        "project": project,
        "location": location,
        "credentials": google_credentials(),
    }


def _no_keepalive() -> Any:
    # Strands runs each sync agent call on a new event loop; pooled connections die with it.
    import httpx

    return httpx.Limits(max_keepalive_connections=0)


def litellm_anthropic_args() -> dict[str, Any]:
    """Strands LiteLLMModel(client_args=...) for ``anthropic/`` models."""
    import anyio
    import httpx
    from anthropic.lib.credentials import TokenCache
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    cache = TokenCache(anthropic_client_args()["credentials"])

    class WorkloadBearer(httpx.Auth):
        def _apply(self, request: httpx.Request, token: str) -> httpx.Request:
            request.headers.pop("x-api-key", None)
            request.headers["authorization"] = f"Bearer {token}"
            beta = request.headers.get("anthropic-beta", "")
            flags = [flag.strip() for flag in beta.split(",") if flag.strip()]
            request.headers["anthropic-beta"] = ",".join(dict.fromkeys([*flags, OAUTH_API_BETA]))
            return request

        def sync_auth_flow(self, request: httpx.Request) -> Any:
            yield self._apply(request, cache.get_token())

        async def async_auth_flow(self, request: httpx.Request) -> Any:
            yield self._apply(request, await anyio.to_thread.run_sync(cache.get_token))

    handler = AsyncHTTPHandler()
    handler.client = httpx.AsyncClient(auth=WorkloadBearer(), limits=_no_keepalive())
    return {"api_key": LITELLM_KEY_PLACEHOLDER, "api_base": ANTHROPIC_AUDIENCE, "client": handler}


def litellm_openai_args() -> dict[str, Any]:
    """Strands LiteLLMModel(client_args=...) for ``openai/`` models."""
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    http_client = DefaultAsyncHttpxClient(limits=_no_keepalive())
    return {"client": AsyncOpenAI(**openai_client_args(), http_client=http_client)}


def litellm_vertex_args() -> dict[str, Any]:
    """Strands LiteLLMModel(client_args=...) for ``vertex_ai/`` models."""
    audience, project, location = _google_settings()
    return {
        # aws_region_name makes LiteLLM re-read ambient boto3 credentials on every refresh.
        "vertex_credentials": {
            "type": "external_account",
            "audience": audience,
            "subject_token_type": GOOGLE_SUBJECT_TOKEN_TYPE,
            "token_url": GOOGLE_TOKEN_URL,
            "credential_source": {"environment_id": "aws1"},
            "aws_region_name": _region(),
        },
        "vertex_project": project,
        "vertex_location": location,
    }


def main(argv: list[str] | None = None) -> int:
    """Check exchange or make one paid inference request, without printing credentials/content."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "smoke"))
    parser.add_argument("provider", choices=("anthropic", "openai", "google"))
    parser.add_argument(
        "--model", help="Required for smoke; choose a model enabled in your account"
    )
    args = parser.parse_args(argv)
    if args.action == "smoke" and not args.model:
        parser.error("smoke requires --model")
    try:
        if args.provider == "anthropic":
            from anthropic import Anthropic

            client_args = anthropic_client_args()
            if args.action == "check":
                # The credentials provider exchanges on invocation, without a model request.
                with client_args["credentials"] as credentials:
                    credentials()
            else:
                with Anthropic(**client_args) as client:
                    client.messages.create(
                        model=args.model,
                        max_tokens=32,
                        messages=[{"role": "user", "content": "Reply with OK."}],
                    )
        elif args.provider == "google":
            if args.action == "check":
                from google.auth.transport.requests import Request

                # Exchanges with Google STS only; no Vertex AI request.
                google_credentials().refresh(Request())
            else:
                from google import genai

                gemini = genai.Client(**gemini_client_args())
                gemini.models.generate_content(model=args.model, contents="Reply with OK.")
        else:
            from openai import OpenAI

            with OpenAI(**openai_client_args()) as client:
                if args.action == "check":
                    # Public SDK endpoint; exercises authentication without paid inference.
                    client.models.list()
                else:
                    client.responses.create(
                        model=args.model, input="Reply with OK.", max_output_tokens=128
                    )
    except Exception as exc:  # noqa: BLE001 — reported (type only) below, exit non-zero
        # HTTP/SDK exception messages can contain token-exchange bodies. Never echo them to logs.
        print(
            f"{args.provider} {args.action} failed ({type(exc).__name__}); see docs/provider-federation.md"
        )
        return 1
    print(f"{args.provider} {args.action} succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
