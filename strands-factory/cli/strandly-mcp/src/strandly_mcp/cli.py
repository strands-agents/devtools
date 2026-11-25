"""``strandly-mcp`` — ``login`` | ``logout`` | ``serve [--agentcore]``.

The whole CLI a teammate needs: sign in once (browser PKCE against the deployed dashboard's
Cognito pool), then register ``strandly-mcp serve`` in an MCP client. No AWS credentials, no agent
stack. ``--agentcore`` is the admin fallback (direct SigV4; needs ``strandly-mcp[aws]`` + AWS
credentials that can invoke the runtime).
"""

from __future__ import annotations

import argparse
import logging
import sys

from strandly_mcp import __version__, _build_info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strandly-mcp",
        description="Use the deployed Strands Factory agent from any MCP client — login once, then serve.",
    )
    parser.add_argument("--version", action="version", version=f"strandly-mcp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="browser sign-in (Cognito PKCE); cached in ~/.strandly/auth.json")
    p_login.add_argument(
        "--portal-url",
        help="the deployed Maintainer Portal URL — discovers --api-url/--cognito-domain/--client-id "
        "from its public config.json"
        + (f" (default when nothing else is configured: {_build_info.PORTAL_URL})" if _build_info.PORTAL_URL else ""),
    )
    p_login.add_argument("--api-url", help="the dashboard HTTP API endpoint (else $STRANDLY_API_URL / cached)")
    p_login.add_argument(
        "--cognito-domain",
        help="the Cognito Hosted-UI domain, https://…amazoncognito.com (else $STRANDLY_COGNITO_DOMAIN / cached)",
    )
    p_login.add_argument(
        "--client-id", help="the Cognito public app-client id (else $STRANDLY_COGNITO_CLIENT_ID / cached)"
    )

    sub.add_parser("logout", help="drop (and best-effort revoke) the cached login session")

    p_serve = sub.add_parser("serve", help="run the stdio MCP server that proxies to the deployed agent")
    p_serve.add_argument(
        "--agentcore",
        action="store_true",
        help="speak InvokeAgentRuntime directly with ambient AWS credentials instead of the "
        "dashboard API (admin fallback; needs strandly-mcp[aws])",
    )
    p_serve.add_argument("--runtime-arn", help="--agentcore: the deployed runtime ARN (else $STRANDLY_RUNTIME_ARN / ~/.strandly/runtime.json)")
    p_serve.add_argument("--region", help="--agentcore: AWS region (else $AWS_REGION / ~/.strandly/runtime.json)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # An INFO "Processing request of type …" per call would land on stderr for every tool call.
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

    if args.command == "login":
        from strandly_mcp.auth import login_command

        return login_command(
            api_url=args.api_url,
            cognito_domain=args.cognito_domain,
            client_id=args.client_id,
            portal_url=args.portal_url,
        )

    if args.command == "logout":
        from strandly_mcp.auth import logout_command

        return logout_command()

    if args.agentcore:
        from strandly_mcp import agentcore

        runtime_arn = agentcore.resolve_runtime_arn(args.runtime_arn)
        region = agentcore.resolve_region(args.region)
        if not runtime_arn:
            print(
                "serve --agentcore: no runtime ARN — pass --runtime-arn or set STRANDLY_RUNTIME_ARN",
                file=sys.stderr,
            )
            return 1
        if not region:
            print("serve --agentcore: no region (pass --region or set AWS_REGION)", file=sys.stderr)
            return 1
        try:
            import boto3  # noqa: F401 — fail before the first tool call, with the extra's name
        except ImportError:
            print(f"serve --agentcore: {agentcore.AwsExtraMissingError()}", file=sys.stderr)
            return 1
        from strandly_mcp.server import serve_mcp_proxy

        serve_mcp_proxy(runtime_arn, region)
        return 0

    from strandly_mcp.auth import LOGIN_HINT, load_auth

    auth = load_auth()
    if auth is None:
        print(
            f"serve: not logged in — {LOGIN_HINT} first (or use --agentcore for direct SigV4)",
            file=sys.stderr,
        )
        return 1
    from strandly_mcp.server import serve_mcp_dashboard_proxy

    serve_mcp_dashboard_proxy(auth)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
