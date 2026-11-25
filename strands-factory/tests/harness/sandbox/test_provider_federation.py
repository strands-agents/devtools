"""Offline contracts for the standalone helper and the *real* provider SDKs.

The SDKs perform their own OAuth exchanges, inference, caching, and refresh. Only
HTTP transport, AWS STS, and SDK-local clocks are replaced; no provider SDK is faked.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import json
import os
import socket
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import httpx
import pytest
from botocore.endpoint import Endpoint
from botocore.stub import Stubber

anthropic = pytest.importorskip("anthropic", minversion="0.118.0")
openai = pytest.importorskip("openai")

_ROOT = Path(__file__).resolve().parents[3]
_REAL_SESSION = boto3.Session
_CONFIG = {
    "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_test",
    "ANTHROPIC_ORGANIZATION_ID": "11111111-2222-4333-8444-555555555555",
    "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_test",
    "ANTHROPIC_WORKSPACE_ID": "wrkspc_test",
    "OPENAI_IDENTITY_PROVIDER_ID": "idp_test",
    "OPENAI_SERVICE_ACCOUNT_ID": "svc_test",
}
_AUDIENCES = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
}
_STATIC_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
)
_SOURCE_TOKENS = ("header.source-jwt-secret-1.signature", "header.source-jwt-secret-2.signature")
_ACCESS_TOKENS = ("provider-access-secret-1", "provider-access-secret-2")
_EXCEPTION_SECRET = "exception-body-secret-do-not-log"
_MESSAGES = [{"role": "user", "content": "Reply with OK."}]


def _blocked_network(*args, **kwargs):
    pytest.fail("Federation tests must never contact AWS, metadata, DNS, or providers")


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    # Ignore local profiles, container credentials, provider keys, and endpoint overrides.
    for name in tuple(os.environ):
        if name.startswith(("AWS_", "ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing-secret-key")
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_network)
    monkeypatch.setattr(socket.socket, "connect", _blocked_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_network)
    monkeypatch.setattr(Endpoint, "_send", _blocked_network)


@pytest.fixture
def auth():
    # Like sandbox_main.py, this root asset deliberately isn't a harness package module.
    spec = importlib.util.spec_from_file_location(
        "strandly_sandbox_auth_under_test", _ROOT / "sandbox" / "strandly_sandbox_auth.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configured(monkeypatch):
    for name, value in _CONFIG.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_REGION", "us-west-2")


@pytest.fixture
def sts(monkeypatch):
    """Fresh, closable clients; intentionally no context-manager client protocol."""
    state = SimpleNamespace(clients=[], sessions=[], response=None)

    def session_factory(**kwargs):
        client = Mock(spec=["get_web_identity_token", "close"])
        token_index = len(state.clients)
        client.get_web_identity_token.return_value = (
            state.response
            if state.response is not None
            else {"WebIdentityToken": _SOURCE_TOKENS[token_index % len(_SOURCE_TOKENS)]}
        )
        session = Mock(spec=["client"])
        session.client.return_value = client
        state.clients.append(client)
        state.sessions.append(session)
        return session

    state.factory = Mock(side_effect=session_factory)
    monkeypatch.setattr(boto3, "Session", state.factory)
    return state


@pytest.fixture
def wire(monkeypatch):
    """Route even SDK-internal OAuth clients through MockTransport, not a fake SDK.

    OpenAI constructs a separate synchronous httpx.Client for each exchange;
    Anthropic WorkloadIdentityCredentials also owns a separate synchronous client,
    including when AsyncAnthropic performs inference. Keep the actual httpx types
    intact so the SDKs' client validation and auth flows still execute normally.
    """
    state = SimpleNamespace(exchanges=[], requests=[], failure=None)
    sync_clients = []

    def handler(request):
        if request.url.path in ("/oauth/token", "/v1/oauth/token"):
            body = json.loads(request.content)
            state.exchanges.append((request, body))
            if state.failure == "exchange":
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": _EXCEPTION_SECRET + " " + " ".join(_SOURCE_TOKENS),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "access_token": _ACCESS_TOKENS[(len(state.exchanges) - 1) % 2],
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        state.requests.append(request)
        if state.failure == "inference":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": _EXCEPTION_SECRET + " " + " ".join(_ACCESS_TOKENS),
                    }
                },
            )
        if request.url.path == "/v1/messages" and json.loads(request.content).get("stream"):
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": "test-model",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "OK"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
                ),
            )
        if request.url.path == "/v1/messages":
            return httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            )
        if request.url.path == "/v1/chat/completions" and json.loads(request.content).get("stream"):
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(
                    "data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl_test",
                            "object": "chat.completion.chunk",
                            "created": 1234,
                            "model": "test-model",
                            **chunk,
                        }
                    )
                    + "\n\n"
                    for chunk in chunks
                )
                + "data: [DONE]\n\n",
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_test",
                    "object": "chat.completion",
                    "created": 1234,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        if request.url.path == "/v1/responses":
            body = json.loads(request.content)
            if body.get("stream"):
                event = {
                    "type": "response.completed",
                    "sequence_number": 0,
                    "response": {"id": "resp_test", "status": "completed", "usage": None},
                }
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
                )
            return httpx.Response(
                200,
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1234,
                    "model": "test-model",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_test",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "OK", "annotations": []}],
                        }
                    ],
                },
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        pytest.fail(f"Unexpected offline HTTP request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    sync_init = httpx.Client.__init__
    async_init = httpx.AsyncClient.__init__

    def init_sync(client, *args, **kwargs):
        kwargs.update(transport=transport, trust_env=False)
        sync_init(client, *args, **kwargs)
        sync_clients.append(client)

    def init_async(client, *args, **kwargs):
        kwargs.update(transport=transport, trust_env=False)
        async_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init_sync)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init_async)
    _route_httpx2(monkeypatch, handler, sync_clients)
    yield state
    for client in sync_clients:
        client.close()


def _route_httpx2(monkeypatch, handler, sync_clients):
    """Route httpx2 clients (anthropic>=1 sends over httpx2, not httpx) through the same handler."""
    try:
        import httpx2
    except ImportError:
        return

    def handler2(request):
        response = handler(request)
        return httpx2.Response(
            response.status_code, headers=response.headers.multi_items(), content=response.content
        )

    transport = httpx2.MockTransport(handler2)
    sync_init = httpx2.Client.__init__
    async_init = httpx2.AsyncClient.__init__

    def init_sync(client, *args, **kwargs):
        kwargs.update(transport=transport, trust_env=False)
        sync_init(client, *args, **kwargs)
        sync_clients.append(client)

    def init_async(client, *args, **kwargs):
        kwargs.update(transport=transport, trust_env=False)
        async_init(client, *args, **kwargs)

    monkeypatch.setattr(httpx2.Client, "__init__", init_sync)
    monkeypatch.setattr(httpx2.AsyncClient, "__init__", init_async)


def _client_args(auth, provider):
    return getattr(auth, f"{provider}_client_args")()


def _source_callback(args, provider):
    if provider == "anthropic":
        return args["credentials"]._identity_token_provider
    return args["workload_identity"]["provider"]["get_token"]


@pytest.mark.parametrize("name", _CONFIG)
@pytest.mark.parametrize("value", [None, "", " \t "])
def test_missing_configuration_rejected_before_sts(auth, configured, sts, monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"Missing {name}"):
        _client_args(auth, name.split("_", 1)[0].lower())
    sts.factory.assert_not_called()


@pytest.mark.parametrize("name", _STATIC_KEYS)
def test_static_provider_credentials_rejected_without_echoing_secret(
    auth, configured, sts, monkeypatch, name
):
    monkeypatch.setenv(name, _EXCEPTION_SECRET)
    with pytest.raises(ValueError, match=f"Unset {name}") as error:
        _client_args(auth, name.split("_", 1)[0].lower())
    assert _EXCEPTION_SECRET not in str(error.value)
    sts.factory.assert_not_called()


@pytest.mark.parametrize("provider", _AUDIENCES)
@pytest.mark.parametrize("region", [None, "", "aws-global"])
def test_missing_or_global_region_rejected(auth, configured, sts, monkeypatch, provider, region):
    if region is None:
        monkeypatch.delenv("AWS_REGION")
    else:
        monkeypatch.setenv("AWS_REGION", region)
    with pytest.raises(ValueError, match="regional STS"):
        _client_args(auth, provider)
    sts.factory.assert_not_called()


@pytest.mark.parametrize("provider", _AUDIENCES)
def test_client_arguments_are_lazy_scoped_and_ignore_provider_base_url_env(
    auth, configured, sts, wire, monkeypatch, provider
):
    prefix = provider.upper()
    monkeypatch.setenv(f"{prefix}_BASE_URL", "https://untrusted.invalid")
    for name in _STATIC_KEYS:
        monkeypatch.setenv(name, "")
    for name, value in _CONFIG.items():
        monkeypatch.setenv(name, f"  {value}\t")
    args = _client_args(auth, provider)
    assert args["base_url"] == _AUDIENCES[provider]
    assert "api_key" not in args
    sts.factory.assert_not_called()
    assert not wire.exchanges
    if provider == "anthropic":
        assert set(args) == {"base_url", "credentials"}
        assert isinstance(args["credentials"], anthropic.WorkloadIdentityCredentials)
    else:
        assert set(args) == {"base_url", "workload_identity"}
        workload = args["workload_identity"]
        assert workload["identity_provider_id"] == _CONFIG["OPENAI_IDENTITY_PROVIDER_ID"]
        assert workload["service_account_id"] == _CONFIG["OPENAI_SERVICE_ACCOUNT_ID"]
        assert workload["provider"]["token_type"] == "jwt"
    assert callable(_source_callback(args, provider))


@pytest.mark.parametrize("provider", _AUDIENCES)
@pytest.mark.parametrize("region_source", ["AWS_REGION", "AWS_DEFAULT_REGION"])
def test_every_callback_uses_new_ambient_session_and_regional_real_sts_client(
    auth, configured, wire, monkeypatch, provider, region_source
):
    """A real botocore client catches unsupported context managers and global STS fallback."""
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.setenv(region_source, "us-west-2")
    if region_source == "AWS_REGION":
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_STS_REGIONAL_ENDPOINTS", "legacy")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://untrusted-global.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://untrusted-sts.invalid")
    sessions = []
    clients = []
    stubbers = []
    calls = []

    def session_factory(**kwargs):
        calls.append(kwargs)
        assert set(kwargs) == {"region_name", "botocore_session"}
        assert kwargs["region_name"] == "us-west-2"
        session = _REAL_SESSION(**kwargs)
        create_client = session.client

        def client_factory(service_name, **client_kwargs):
            assert service_name == "sts"
            assert set(client_kwargs) == {"config"}
            assert client_kwargs["config"].ignore_configured_endpoint_urls is True
            client = create_client(service_name, **client_kwargs)
            assert client.meta.endpoint_url == "https://sts.us-west-2.amazonaws.com"
            close = Mock(wraps=client.close)
            monkeypatch.setattr(client, "close", close)
            stubber = Stubber(client)
            stubber.add_response(
                "get_web_identity_token",
                {"WebIdentityToken": _SOURCE_TOKENS[len(clients)]},
                {
                    "Audience": [_AUDIENCES[provider]],
                    "SigningAlgorithm": "RS256",
                    "DurationSeconds": 300,
                },
            )
            stubber.activate()
            clients.append(client)
            stubbers.append(stubber)
            return client

        monkeypatch.setattr(session, "client", client_factory)
        sessions.append(session)
        return session

    monkeypatch.setattr(boto3, "Session", session_factory)
    callback = _source_callback(_client_args(auth, provider), provider)
    assert not sessions  # No AWS lookup during model/client construction.
    try:
        for index, token in enumerate(_SOURCE_TOKENS):
            monkeypatch.setenv("AWS_ACCESS_KEY_ID", f"rotated-access-{index}")
            monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", f"rotated-secret-{index}")
            monkeypatch.setenv("AWS_SESSION_TOKEN", f"rotated-session-{index}")
            assert callback() == token
            credentials = sessions[index].get_credentials().get_frozen_credentials()
            assert credentials.access_key == f"rotated-access-{index}"
            assert credentials.secret_key == f"rotated-secret-{index}"
            assert credentials.token == f"rotated-session-{index}"
            clients[index].close.assert_called_once_with()
            stubbers[index].assert_no_pending_responses()
        assert len(sessions) == 2
        assert sessions[0] is not sessions[1]
        assert calls[0]["botocore_session"] is not calls[1]["botocore_session"]
    finally:
        for stubber in stubbers:
            stubber.deactivate()


@pytest.mark.parametrize("provider", _AUDIENCES)
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"WebIdentityToken": None},
        {"WebIdentityToken": ""},
        {"WebIdentityToken": 123},
        {"WebIdentityToken": b"secret-bytes"},
        {"WebIdentityToken": ["secret-list"]},
    ],
    ids=["missing", "null", "empty", "number", "bytes", "list"],
)
def test_invalid_sts_token_rejected_and_client_closed(
    auth, configured, sts, wire, provider, response
):
    sts.response = response
    callback = _source_callback(_client_args(auth, provider), provider)
    with pytest.raises(RuntimeError, match="AWS STS returned no web identity token") as error:
        callback()
    assert "secret" not in str(error.value)
    sts.clients[0].close.assert_called_once_with()
    assert not wire.exchanges


@pytest.mark.parametrize("provider", _AUDIENCES)
def test_sts_client_closed_when_issuance_fails(auth, configured, sts, wire, provider):
    create_session = sts.factory.side_effect

    def failing_session(**kwargs):
        session = create_session(**kwargs)
        sts.clients[-1].get_web_identity_token.side_effect = RuntimeError("STS unavailable")
        return session

    sts.factory.side_effect = failing_session
    callback = _source_callback(_client_args(auth, provider), provider)
    with pytest.raises(RuntimeError, match="STS unavailable"):
        callback()
    sts.clients[0].close.assert_called_once_with()
    assert not wire.exchanges


def _sdk_clock(monkeypatch, provider, client):
    """Advance only auth clocks, never global time or the asyncio scheduler."""
    clock = SimpleNamespace(now=1_700_000_000.0)
    if provider == "anthropic":
        from anthropic.lib.credentials import _workload

        monkeypatch.setattr(_workload, "time", SimpleNamespace(time=lambda: clock.now))
        # TokenCache captures time.time as a default argument: patch that instance's seam.
        monkeypatch.setattr(client._token_cache, "_time_source", lambda: clock.now)
    else:
        from openai.auth import _workload

        monkeypatch.setattr(_workload, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def _infer(client, provider, api):
    if provider == "anthropic":
        return client.messages.create(model="test-model", max_tokens=32, messages=_MESSAGES)
    if api == "chat":
        return client.chat.completions.create(model="test-model", messages=_MESSAGES)
    return client.responses.create(model="test-model", input="Reply with OK.")


def _assert_inference(result, provider, api):
    if provider == "anthropic":
        assert result.content[0].text == "OK"
    elif api == "chat":
        assert result.choices[0].message.content == "OK"
    else:
        assert result.output_text == "OK"


def _assert_exchanges(wire, sts, provider):
    assert len(wire.exchanges) == len(sts.clients) == 2
    for index, (request, body) in enumerate(wire.exchanges):
        assert request.method == "POST"
        if provider == "anthropic":
            assert str(request.url) == "https://api.anthropic.com/v1/oauth/token"
            assert body == {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": _SOURCE_TOKENS[index],
                **{
                    name.removeprefix("ANTHROPIC_").lower(): value
                    for name, value in _CONFIG.items()
                    if name.startswith("ANTHROPIC_")
                },
            }
        else:
            assert str(request.url) == "https://auth.openai.com/oauth/token"
            assert body == {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": _SOURCE_TOKENS[index],
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "identity_provider_id": _CONFIG["OPENAI_IDENTITY_PROVIDER_ID"],
                "service_account_id": _CONFIG["OPENAI_SERVICE_ACCOUNT_ID"],
            }
        sts.clients[index].get_web_identity_token.assert_called_once_with(
            Audience=[_AUDIENCES[provider]], SigningAlgorithm="RS256", DurationSeconds=300
        )
        sts.clients[index].close.assert_called_once_with()
    assert [request.headers["authorization"] for request in wire.requests] == [
        f"Bearer {_ACCESS_TOKENS[0]}",
        f"Bearer {_ACCESS_TOKENS[0]}",
        f"Bearer {_ACCESS_TOKENS[1]}",
        f"Bearer {_ACCESS_TOKENS[1]}",
    ]
    for request in wire.requests:
        assert request.url.host == f"api.{provider}.com"
        assert "x-api-key" not in request.headers
        assert not any(token in str(request.headers) for token in _SOURCE_TOKENS)


@pytest.mark.parametrize(
    "provider,api", [("anthropic", "messages"), ("openai", "chat"), ("openai", "responses")]
)
def test_real_sync_sdk_caches_access_token_and_expiry_mints_new_source_jwt(
    auth, configured, sts, wire, monkeypatch, provider, api
):
    sdk_class = anthropic.Anthropic if provider == "anthropic" else openai.OpenAI
    with sdk_class(**_client_args(auth, provider), max_retries=0) as client:
        clock = _sdk_clock(monkeypatch, provider, client)
        sts.factory.assert_not_called()
        for elapsed, expected_exchanges in [(0, 1), (1, 1), (3601, 2), (3602, 2)]:
            clock.now = 1_700_000_000.0 + elapsed
            _assert_inference(_infer(client, provider, api), provider, api)
            assert len(wire.exchanges) == len(sts.clients) == expected_exchanges
    _assert_exchanges(wire, sts, provider)


@pytest.mark.parametrize(
    "provider,api", [("anthropic", "messages"), ("openai", "chat"), ("openai", "responses")]
)
async def test_real_async_sdk_caches_access_token_and_expiry_mints_new_source_jwt(
    auth, configured, sts, wire, monkeypatch, provider, api
):
    sdk_class = anthropic.AsyncAnthropic if provider == "anthropic" else openai.AsyncOpenAI
    async with sdk_class(**_client_args(auth, provider), max_retries=0) as client:
        clock = _sdk_clock(monkeypatch, provider, client)
        sts.factory.assert_not_called()
        for elapsed, expected_exchanges in [(0, 1), (1, 1), (3601, 2), (3602, 2)]:
            clock.now = 1_700_000_000.0 + elapsed
            _assert_inference(await _infer(client, provider, api), provider, api)
            assert len(wire.exchanges) == len(sts.clients) == expected_exchanges
    _assert_exchanges(wire, sts, provider)


async def test_strands_anthropic_model_accepts_client_args_without_eager_sts(
    auth, configured, sts, wire
):
    from strands.models.anthropic import AnthropicModel

    model = AnthropicModel(
        model_id="test-model", max_tokens=1024, client_args=auth.anthropic_client_args()
    )
    try:
        assert isinstance(model.client, anthropic.AsyncAnthropic)
        sts.factory.assert_not_called()
        events = [
            event
            async for event in model.stream(
                [{"role": "user", "content": [{"text": "Reply with OK."}]}]
            )
        ]
        assert any(
            event.get("contentBlockDelta", {}).get("delta", {}).get("text") == "OK"
            for event in events
        )
        assert json.loads(wire.requests[0].content)["max_tokens"] == 1024
        assert wire.requests[0].headers["authorization"] == f"Bearer {_ACCESS_TOKENS[0]}"
        assert len(wire.exchanges) == 1
    finally:
        await model.client.close()


async def test_strands_openai_model_constructs_real_client_from_client_args(
    auth, configured, sts, wire
):
    from strands.models.openai import OpenAIModel

    model = OpenAIModel(model_id="test-model", client_args=auth.openai_client_args())
    sts.factory.assert_not_called()
    # Strands builds OpenAI clients lazily, per request, rather than keeping one on the model.
    async with model._get_client() as client:
        assert isinstance(client, openai.AsyncOpenAI)
        sts.factory.assert_not_called()
        result = await _infer(client, "openai", "chat")
        _assert_inference(result, "openai", "chat")
    assert len(wire.exchanges) == 1


async def test_strands_openai_responses_model_uses_client_args_during_stream(
    auth, configured, sts, wire
):
    from strands.models.openai_responses import OpenAIResponsesModel

    model = OpenAIResponsesModel(model_id="test-model", client_args=auth.openai_client_args())
    sts.factory.assert_not_called()
    events = [
        event
        async for event in model.stream([{"role": "user", "content": [{"text": "Reply with OK."}]}])
    ]
    assert events[0] == {"messageStart": {"role": "assistant"}}
    assert len(wire.exchanges) == 1
    assert wire.requests[0].url.path == "/v1/responses"
    assert wire.requests[0].headers["authorization"] == f"Bearer {_ACCESS_TOKENS[0]}"


@pytest.mark.parametrize("provider", _AUDIENCES)
@pytest.mark.parametrize("action", ["check", "smoke"])
def test_cli_success_never_prints_tokens_or_inference_content(
    auth, configured, sts, wire, capsys, provider, action
):
    argv = [action, provider] + (["--model", "test-model"] if action == "smoke" else [])
    assert auth.main(argv) == 0
    output = capsys.readouterr()
    assert output.out == f"{provider} {action} succeeded\n"
    assert output.err == ""
    assert len(wire.exchanges) == 1
    if action == "check":
        assert [request.url.path for request in wire.requests] == (
            [] if provider == "anthropic" else ["/v1/models"]
        )
    else:
        assert [request.url.path for request in wire.requests] == (
            ["/v1/messages"] if provider == "anthropic" else ["/v1/responses"]
        )


@pytest.mark.parametrize(
    "provider,action,failure",
    [
        (provider, action, failure)
        for provider in _AUDIENCES
        for action in ("check", "smoke")
        for failure in ("sts", "exchange", "inference")
        # Anthropic check is an exchange only; there is no inference failure path.
        if (provider, action, failure) != ("anthropic", "check", "inference")
    ],
)
def test_cli_failures_sanitize_source_access_tokens_and_exception_secrets(
    auth, configured, sts, wire, capsys, caplog, provider, action, failure
):
    if failure == "sts":
        sts.factory.side_effect = RuntimeError(
            _EXCEPTION_SECRET + " " + " ".join((*_SOURCE_TOKENS, *_ACCESS_TOKENS))
        )
    else:
        wire.failure = failure
    argv = [action, provider] + (["--model", "test-model"] if action == "smoke" else [])
    assert auth.main(argv) == 1
    output = capsys.readouterr()
    assert output.out.startswith(f"{provider} {action} failed (")
    assert "docs/provider-federation.md" in output.out
    assert output.err == ""
    for secret in (*_SOURCE_TOKENS, *_ACCESS_TOKENS, _EXCEPTION_SECRET):
        assert secret not in output.out + output.err + caplog.text


@pytest.mark.parametrize("provider", _AUDIENCES)
def test_cli_smoke_requires_model_before_any_auth(auth, configured, sts, wire, capsys, provider):
    with pytest.raises(SystemExit) as error:
        auth.main(["smoke", provider])
    assert error.value.code == 2
    assert "smoke requires --model" in capsys.readouterr().err
    sts.factory.assert_not_called()
    assert not wire.exchanges


_GOOGLE_CONFIG = {
    "GOOGLE_CLOUD_PROJECT": "strandly-sandbox",
    "GOOGLE_WIF_PROJECT_NUMBER": "123456789012",
    "GOOGLE_WIF_POOL_ID": "strandly-sandbox",
    "GOOGLE_WIF_PROVIDER_ID": "aws-sandbox",
    "GOOGLE_CLOUD_LOCATION": "us-central1",
}
_GOOGLE_AUDIENCE = (
    "//iam.googleapis.com/projects/123456789012/locations/global"
    "/workloadIdentityPools/strandly-sandbox/providers/aws-sandbox"
)
_GOOGLE_STATIC_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")


@pytest.fixture
def google_configured(monkeypatch):
    for name, value in _GOOGLE_CONFIG.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_REGION", "us-west-2")


@pytest.fixture
def google_wire(monkeypatch):
    """Google STS through google-auth's transport seam; Vertex AI through httpx."""
    pytest.importorskip("google.auth", minversion="2.29.0")
    import google.auth.transport.requests

    state = SimpleNamespace(exchanges=[], requests=[])

    class FakeRequest:
        def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
            assert url == "https://sts.googleapis.com/v1/token"
            (subject,) = urllib.parse.parse_qs(body.decode())["subject_token"]
            state.exchanges.append(json.loads(urllib.parse.unquote(subject)))
            token = _ACCESS_TOKENS[(len(state.exchanges) - 1) % 2]
            payload = {
                "access_token": token,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            return SimpleNamespace(status=200, headers={}, data=json.dumps(payload).encode())

    def handler(request):
        state.requests.append(request)
        assert request.url.host == "us-central1-aiplatform.googleapis.com"
        body = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1},
        }
        if request.url.path.endswith(":streamGenerateContent"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(body)}\n\n",
            )
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    sync_init = httpx.Client.__init__
    async_init = httpx.AsyncClient.__init__
    monkeypatch.setattr(google.auth.transport.requests, "Request", FakeRequest)
    # genai prefers aiohttp for async when installed (litellm brings it); keep the httpx seam.
    monkeypatch.setattr("google.genai._api_client.has_aiohttp", False)
    monkeypatch.setattr(
        httpx.Client,
        "__init__",
        lambda c, *a, **k: sync_init(c, *a, **{**k, "transport": transport}),
    )
    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        lambda c, *a, **k: async_init(c, *a, **{**k, "transport": transport}),
    )
    return state


def _signed_header(exchange, name):
    return next(h["value"] for h in exchange["headers"] if h["key"].lower() == name)


@pytest.mark.parametrize("name", _GOOGLE_CONFIG)
def test_google_missing_configuration_rejected(auth, google_configured, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=f"Missing {name}"):
        auth.gemini_client_args()


@pytest.mark.parametrize("name", _GOOGLE_STATIC_KEYS)
@pytest.mark.parametrize("helper", ["gemini_client_args", "litellm_vertex_args"])
def test_google_static_credentials_rejected_without_echo(
    auth, google_configured, monkeypatch, name, helper
):
    monkeypatch.setenv(name, _EXCEPTION_SECRET)
    with pytest.raises(ValueError, match=f"Unset {name}") as error:
        getattr(auth, helper)()
    assert _EXCEPTION_SECRET not in str(error.value)


def test_google_exchange_signs_get_caller_identity_with_rotating_ambient_credentials(
    auth, google_configured, google_wire, monkeypatch
):
    import google.auth.transport.requests

    credentials = auth.google_credentials()
    assert not google_wire.exchanges  # Nothing is exchanged at construction.
    for index in range(2):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", f"rotated-access-{index}")
        monkeypatch.setenv("AWS_SESSION_TOKEN", f"rotated-session-{index}")
        credentials.refresh(google.auth.transport.requests.Request())
        assert credentials.token == _ACCESS_TOKENS[index]
        exchange = google_wire.exchanges[index]
        assert exchange["url"] == (
            "https://sts.us-west-2.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
        )
        assert _signed_header(exchange, "x-goog-cloud-target-resource") == _GOOGLE_AUDIENCE
        assert f"Credential=rotated-access-{index}/" in _signed_header(exchange, "authorization")
        assert _signed_header(exchange, "x-amz-security-token") == f"rotated-session-{index}"


def test_gemini_client_args_drive_vertex_and_refresh_on_expiry(
    auth, google_configured, google_wire
):
    from google import genai

    args = auth.gemini_client_args()
    assert set(args) == {"vertexai", "project", "location", "credentials"}
    client = genai.Client(**args)
    for _ in range(2):
        response = client.models.generate_content(model="test-model", contents="Reply with OK.")
        assert response.text == "OK"
        args["credentials"].expiry = datetime.datetime(2000, 1, 1)
    assert len(google_wire.exchanges) == 2
    paths = {request.url.path for request in google_wire.requests}
    assert paths == {
        "/v1beta1/projects/strandly-sandbox/locations/us-central1/publishers/google/models/"
        "test-model:generateContent"
    }
    assert [r.headers["authorization"] for r in google_wire.requests] == [
        f"Bearer {token}" for token in _ACCESS_TOKENS
    ]


@pytest.fixture
def litellm(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return pytest.importorskip("litellm")


def _litellm_completions(litellm, model, client_args, calls=2):
    async def complete():
        return await litellm.acompletion(model=model, messages=_MESSAGES, **client_args)

    # Separate loops, as Strands' sync Agent calls have.
    return [asyncio.run(complete()) for _ in range(calls)]


def test_litellm_anthropic_args_replace_placeholder_with_refreshing_bearer(
    auth, configured, sts, wire, litellm
):
    args = auth.litellm_anthropic_args()
    assert args["api_key"] == "workload-identity"
    responses = _litellm_completions(litellm, "anthropic/test-model", args)
    assert [r.choices[0].message.content for r in responses] == ["OK", "OK"]
    assert len(wire.exchanges) == 1  # The SDK token cache serves the second call.
    for request in wire.requests:
        assert request.url.host == "api.anthropic.com"
        assert "x-api-key" not in request.headers
        assert request.headers["authorization"] == f"Bearer {_ACCESS_TOKENS[0]}"
        assert "oauth-2025-04-20" in request.headers["anthropic-beta"].split(",")


def test_litellm_openai_args_keep_sdk_workload_identity(auth, configured, sts, wire, litellm):
    args = auth.litellm_openai_args()
    assert set(args) == {"client"}
    responses = _litellm_completions(litellm, "openai/test-model", args)
    assert [r.choices[0].message.content for r in responses] == ["OK", "OK"]
    assert len(wire.exchanges) == 1
    assert [r.headers["authorization"] for r in wire.requests] == [
        f"Bearer {_ACCESS_TOKENS[0]}"
    ] * 2


def test_litellm_vertex_args_are_plain_config_without_secrets(auth, google_configured):
    args = auth.litellm_vertex_args()
    assert args == {
        "vertex_credentials": {
            "type": "external_account",
            "audience": _GOOGLE_AUDIENCE,
            "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
            "token_url": "https://sts.googleapis.com/v1/token",
            "credential_source": {"environment_id": "aws1"},
            "aws_region_name": "us-west-2",
        },
        "vertex_project": "strandly-sandbox",
        "vertex_location": "us-central1",
    }
    assert "testing-access-key" not in json.dumps(args)


def test_litellm_vertex_args_exchange_through_litellm(
    auth, google_configured, google_wire, litellm
):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    token, project = VertexBase().get_access_token(
        credentials=auth.litellm_vertex_args()["vertex_credentials"],
        project_id="strandly-sandbox",
    )
    assert (token, project) == (_ACCESS_TOKENS[0], "strandly-sandbox")
    exchange = google_wire.exchanges[0]
    assert _signed_header(exchange, "x-goog-cloud-target-resource") == _GOOGLE_AUDIENCE
    assert "Credential=testing-access-key/" in _signed_header(exchange, "authorization")


def _agent_replies(model, calls=2):
    from strands import Agent

    agent = Agent(model=model, callback_handler=None)
    return [str(agent("Reply with OK.")).strip() for _ in range(calls)]


def test_strands_gemini_model_on_vertex(auth, google_configured, google_wire):
    from strands.models.gemini import GeminiModel

    model = GeminiModel(model_id="test-model", client_args=auth.gemini_client_args())
    assert _agent_replies(model) == ["OK", "OK"]
    assert len(google_wire.exchanges) == 1


@pytest.mark.parametrize(
    ("model_id", "helper"),
    [
        ("anthropic/test-model", "litellm_anthropic_args"),
        ("openai/test-model", "litellm_openai_args"),
    ],
)
def test_strands_litellm_model_across_sync_agent_calls(
    auth, configured, sts, wire, litellm, model_id, helper
):
    from strands.models.litellm import LiteLLMModel

    model = LiteLLMModel(model_id=model_id, client_args=getattr(auth, helper)())
    assert _agent_replies(model) == ["OK", "OK"]
    assert len(wire.exchanges) == 1


@pytest.mark.parametrize("action", ["check", "smoke"])
def test_cli_google_never_prints_tokens(auth, google_configured, google_wire, capsys, action):
    assert auth.main([action, "google", "--model", "test-model"]) == 0
    output = capsys.readouterr()
    assert output.out == f"google {action} succeeded\n"
    assert not output.err
    assert len(google_wire.exchanges) == 1
    assert len(google_wire.requests) == (action == "smoke")
