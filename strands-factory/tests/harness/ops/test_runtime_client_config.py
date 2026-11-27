"""InvokeAgentRuntime client config: a launch is sent exactly once; polls keep retrying.

On Runtime *instances* a cold session's first invoke takes ~54-65 s to be accepted. botocore's
defaults (60 s read timeout + automatic retries) re-sent it and AgentCore ran the message twice.
These tests pin the fix at every call site that speaks InvokeAgentRuntime: the harness client
(``ops.runtime_client`` — mention poller, Slack ingress, scheduled invoker, ``strandly invoke`` /
``poll`` / ``chat --agentcore``, ``serve.deploy.invoke``), the dashboard read API's chat, and the
thin ``strandly-mcp`` package. Besides the static config they drive a real botocore client against
a local HTTP server and count the requests that actually go out.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from strandly.ops import runtime_client

_ROOT = Path(__file__).resolve().parents[3]
ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/strandly_test_agent-abc"


def _load_dashboard_handler():
    spec = importlib.util.spec_from_file_location(
        "_dash_handler_cfg", _ROOT / "dashboard" / "api" / "handler.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- static config ---------------------------------------------------------------------------------


def test_launch_config_one_attempt_long_read_timeout():
    cfg = runtime_client.client_config(launch=True)
    assert cfg.read_timeout == runtime_client.INVOKE_READ_TIMEOUT_S == 300
    assert cfg.connect_timeout == runtime_client.INVOKE_CONNECT_TIMEOUT_S
    # total_max_attempts, NOT max_attempts: botocore reads max_attempts=1 as ONE RETRY (2 sends).
    assert cfg.retries == {"total_max_attempts": 1, "mode": "standard"}


def test_control_config_keeps_retries_and_long_read_timeout():
    cfg = runtime_client.client_config(launch=False)
    assert cfg.read_timeout == 300  # a poll on a recycled instance pays the same cold start
    assert cfg.retries == {"total_max_attempts": 3, "mode": "standard"}


def test_read_timeout_clears_worst_observed_cold_start():
    worst_cold_start_s = 65
    assert runtime_client.INVOKE_READ_TIMEOUT_S >= 4 * worst_cold_start_s


@pytest.mark.parametrize(
    ("payload", "launch"),
    [
        ({"prompt": "hi", "sessionId": "s"}, True),
        ({"prompt": "hi", "sessionId": "s", "mode": "draft"}, True),
        ({"prompt": "hi", "sessionId": "s", "mode": "stream"}, True),
        ({"action": "poll", "taskId": "t", "sessionId": "s"}, False),
        ({"action": "cancel", "sessionId": "s"}, False),
    ],
)
def test_is_launch(payload, launch):
    assert runtime_client.is_launch(payload) is launch


def test_botocore_resolves_launch_config_to_a_single_attempt(monkeypatch):
    """The resolved client really has one total attempt (guards a botocore semantics change)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    launch = runtime_client._client("us-west-2", launch=True)
    control = runtime_client._client("us-west-2", launch=False)
    assert launch.meta.config.retries["total_max_attempts"] == 1
    assert launch.meta.config.read_timeout == 300
    assert control.meta.config.retries["total_max_attempts"] == 3


# --- behaviour against a local endpoint -------------------------------------------------------------


class _Server:
    """A local stand-in for the AgentCore data plane: counts requests, then misbehaves on demand."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.mode = "error"  # "error" → HTTP 500; "slow" → sleep past the read timeout; "ok"
        self.sleep_s = 1.5
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — stdlib API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                server.requests.append({"path": self.path, "body": body})
                if server.mode == "slow":
                    time.sleep(server.sleep_s)
                if server.mode == "ok":
                    out = json.dumps({"status": "accepted", "taskId": "t1"}).encode()
                    self.send_response(200)
                else:
                    out = json.dumps({"message": "boom"}).encode()
                    self.send_response(500)
                    self.send_header("x-amzn-errortype", "InternalServerException")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                try:
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):  # quiet
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def endpoint(monkeypatch) -> Iterator[_Server]:
    srv = _Server()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    # botocore's service-specific endpoint override (service id "Bedrock AgentCore").
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_AGENTCORE", srv.url)
    yield srv
    srv.close()


def _fast_timeouts(monkeypatch, read_s: float = 0.4) -> None:
    monkeypatch.setattr(runtime_client, "INVOKE_READ_TIMEOUT_S", read_s)
    monkeypatch.setattr(runtime_client, "INVOKE_CONNECT_TIMEOUT_S", 1)


def test_launch_run_read_timeout_is_not_resent(endpoint, monkeypatch):
    """The prod bug: a slow accept → client timeout → botocore re-sent the launch. Now: one send."""
    from botocore.exceptions import ReadTimeoutError

    _fast_timeouts(monkeypatch)
    endpoint.mode = "slow"
    with pytest.raises(ReadTimeoutError):
        runtime_client.launch_run(ARN, "us-west-2", "sess-1", "hello")
    time.sleep(0.2)
    assert len(endpoint.requests) == 1


def test_launch_run_5xx_is_not_resent(endpoint, monkeypatch):
    _fast_timeouts(monkeypatch)
    with pytest.raises(Exception):  # noqa: B017 — botocore ClientError
        runtime_client.launch_run(ARN, "us-west-2", "sess-1", "hello")
    assert len(endpoint.requests) == 1


def test_stream_run_is_not_resent(endpoint, monkeypatch):
    _fast_timeouts(monkeypatch)
    with pytest.raises(Exception):  # noqa: B017
        list(runtime_client.stream_run(ARN, "us-west-2", "sess-1", "hello"))
    assert len(endpoint.requests) == 1


def test_poll_run_still_retries(endpoint, monkeypatch):
    """Polls are idempotent: transient 5xx keep their standard retries (3 attempts)."""
    _fast_timeouts(monkeypatch)
    with pytest.raises(Exception):  # noqa: B017
        runtime_client.poll_run(ARN, "us-west-2", "sess-1", "task-1")
    assert len(endpoint.requests) == runtime_client.CONTROL_TOTAL_MAX_ATTEMPTS == 3


def test_deploy_invoke_classifies_by_payload(endpoint, monkeypatch):
    """``serve.deploy.invoke`` (``strandly`` CLI helper) routes through the same rule."""
    from strandly.serve import deploy

    _fast_timeouts(monkeypatch)
    with pytest.raises(Exception):  # noqa: B017
        deploy.invoke(ARN, "us-west-2", "sess-1", {"prompt": "x", "sessionId": "sess-1"})
    assert len(endpoint.requests) == 1


def test_launch_run_ok_path(endpoint, monkeypatch):
    _fast_timeouts(monkeypatch)
    endpoint.mode = "ok"
    assert runtime_client.launch_run(ARN, "us-west-2", "sess-1", "hello")["taskId"] == "t1"
    assert len(endpoint.requests) == 1


# --- dashboard read API chat -------------------------------------------------------------------------


def test_dashboard_chat_config_fits_the_lambda_budget():
    handler = _load_dashboard_handler()
    launch = handler.RuntimeInvoker.client_config(launch=True)
    control = handler.RuntimeInvoker.client_config(launch=False)
    assert launch.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert control.retries["total_max_attempts"] == 2
    lambda_timeout_s = 29  # infra/stacks/dashboard_stack.py ReadApi function
    assert launch.connect_timeout + launch.read_timeout < lambda_timeout_s
    assert 2 * (control.connect_timeout + control.read_timeout) <= lambda_timeout_s


def test_dashboard_chat_launch_timeout_reports_starting_without_resend(endpoint, monkeypatch):
    handler = _load_dashboard_handler()
    monkeypatch.setattr(handler, "CHAT_LAUNCH_READ_TIMEOUT_S", 0.4)
    monkeypatch.setattr(handler, "CHAT_CONNECT_TIMEOUT_S", 1)
    endpoint.mode = "slow"
    inv = handler.RuntimeInvoker(ARN, "us-west-2")
    out = inv.launch("sess-1", "hello")
    assert out == {"status": "starting", "sessionId": "sess-1"}
    time.sleep(0.2)
    assert len(endpoint.requests) == 1


def test_dashboard_chat_poll_retries_and_does_not_mask_errors(endpoint, monkeypatch):
    handler = _load_dashboard_handler()
    monkeypatch.setattr(handler, "CHAT_CONTROL_READ_TIMEOUT_S", 0.4)
    inv = handler.RuntimeInvoker(ARN, "us-west-2")
    with pytest.raises(Exception):  # noqa: B017
        inv.poll("sess-1", "task-1")
    assert len(endpoint.requests) == 2


@pytest.mark.parametrize("call", ["poll", "cancel"])
def test_dashboard_chat_control_timeout_reports_starting_after_both_attempts(endpoint, monkeypatch, call):
    """A cold session: both attempts read-time-out, and the caller is told to retry — not a crash."""
    handler = _load_dashboard_handler()
    monkeypatch.setattr(handler, "CHAT_CONTROL_READ_TIMEOUT_S", 0.4)
    monkeypatch.setattr(handler, "CHAT_CONNECT_TIMEOUT_S", 1)
    endpoint.mode = "slow"
    inv = handler.RuntimeInvoker(ARN, "us-west-2")
    with pytest.raises(handler.RuntimeStarting):
        inv.poll("sess-1", "task-1") if call == "poll" else inv.cancel("sess-1")
    assert len(endpoint.requests) == 2


# --- strandly-mcp (thin package) ---------------------------------------------------------------------


def test_mcp_package_mirrors_the_launch_rule(endpoint, monkeypatch):
    from strandly_mcp import agentcore

    assert agentcore.INVOKE_READ_TIMEOUT_S == runtime_client.INVOKE_READ_TIMEOUT_S
    assert agentcore.client_config(launch=True).retries == {"total_max_attempts": 1, "mode": "standard"}
    assert agentcore.client_config(launch=False).retries["total_max_attempts"] == 3
    monkeypatch.setattr(agentcore, "INVOKE_READ_TIMEOUT_S", 0.4)
    with pytest.raises(Exception):  # noqa: B017
        agentcore.launch_run(ARN, "us-west-2", "sess-1", "hello")
    assert len(endpoint.requests) == 1
    with pytest.raises(Exception):  # noqa: B017
        agentcore.poll_run(ARN, "us-west-2", "sess-1", "task-1")
    assert len(endpoint.requests) == 1 + 3


def test_launch_maybe_accepted_only_for_errors_after_the_request_was_sent():
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        IncompleteReadError,
        ReadTimeoutError,
        ResponseStreamingError,
    )

    from strandly.ops.runtime_client import launch_maybe_accepted

    url = "https://bedrock-agentcore.us-west-2.amazonaws.com"
    sent = [
        ReadTimeoutError(endpoint_url=url),
        ConnectionClosedError(endpoint_url=url),
        ResponseStreamingError(error="reset"),
        IncompleteReadError(actual_bytes=1, expected_bytes=2),
    ]
    not_sent = [
        ConnectTimeoutError(endpoint_url=url),
        EndpointConnectionError(endpoint_url=url),
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "InvokeAgentRuntime"),
        ClientError({"Error": {"Code": "ValidationException", "Message": "x"}}, "InvokeAgentRuntime"),
        RuntimeError("cannot dispatch: runtime_arn=None"),
    ]
    assert all(launch_maybe_accepted(e) for e in sent)
    assert not any(launch_maybe_accepted(e) for e in not_sent)


@pytest.mark.parametrize("mode", ["error", "slow"])
def test_sandbox_command_http_failure_has_one_total_attempt(endpoint, monkeypatch, mode):
    from strandly.sandbox import runtime as sandbox_runtime

    monkeypatch.setattr(sandbox_runtime, "SANDBOX_CLIENT_READ_TIMEOUT_SECONDS", 0.2)
    endpoint.mode = mode
    sandbox = sandbox_runtime.RuntimeSandbox(runtime_arn=ARN, session_key="http-retry-test", region="us-west-2")
    client = sandbox._ensure_client()
    assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
    with pytest.raises(Exception):  # noqa: B017 — HTTP 500 or socket read timeout
        client.invoke_agent_runtime_command(**sandbox._command_kwargs("printf side-effect", 1))
    assert len(endpoint.requests) == 1
