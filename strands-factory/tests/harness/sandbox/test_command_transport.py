"""Command transport: keep pooled connections healthy, resend only what provably never left.

The soak failures (``specs/soak-053e188.md``): ``ConnectionClosedError`` about 10 ms into a command,
nothing recorded by the service, each time a subagent's burst started. A request written onto a
pooled connection that the path dropped while it sat idle fails exactly that way, and botocore
cannot tell it from a command the service received, so it must stay "completion unknown". The fix
keeps such connections out of use; only a failure to connect at all is sent again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from typing import Any

import pytest
from botocore.awsrequest import AWSHTTPConnectionPool
from botocore.exceptions import ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError
from idle_stub_server import IdleDroppingServer
from strands import Agent
from strands.vended_tools.shell import shell
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.sandbox import runtime as rt
from strandly.sandbox.notices import SandboxNotices
from strandly.sandbox.runtime import (
    RuntimeSandbox,
    SandboxExecutionUncertainError,
    SandboxPlatformError,
)

DONE = {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}
OUT = {"chunk": {"contentDelta": {"stdout": "out\n"}}}
URL = "https://bedrock-agentcore.us-west-2.amazonaws.com"


@pytest.fixture
def subject(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "_sleep", lambda _s: None)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FakeAgentCoreClient(workdir=str(workdir))
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_key="transport", client=client, workdir=str(workdir)
    )
    sandbox._ready = True
    return sandbox, client


def _scripted(monkeypatch, client, *steps):
    """Each send takes the next step: an exception to raise, or a list of stream events."""
    sends: list[str] = []
    queue = list(steps)

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])
        step = queue.pop(0) if queue else [DONE]
        if isinstance(step, BaseException):
            raise step
        return {"stream": iter(step), "ResponseMetadata": {"RequestId": "req-123"}}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    return sends


def _stream_then(error: BaseException, *events):
    def gen():
        yield from events
        raise error

    return gen()


# --- resend only when nothing was sent ---------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [EndpointConnectionError(endpoint_url=URL), ConnectTimeoutError(endpoint_url=URL)],
    ids=["endpoint-connection", "connect-timeout"],
)
async def test_a_command_that_never_connected_is_sent_once_more_and_succeeds(
    subject, monkeypatch, caplog, error
):
    sandbox, client = subject
    sends = _scripted(monkeypatch, client, error, [OUT, DONE])
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        result = await sandbox.execute("SIDE_EFFECT")
    assert result.exit_code == 0 and result.stdout == "out\n"
    assert sum("SIDE_EFFECT" in c for c in sends) == 2
    assert sandbox.ready and sandbox._platform_error is None
    assert f"sandbox command was not sent ({type(error).__name__}" in caplog.text


async def test_a_command_that_never_connects_is_resent_only_once(subject, monkeypatch):
    sandbox, client = subject
    error = EndpointConnectionError(endpoint_url=URL)
    sends = _scripted(monkeypatch, client, error, error, error)
    with pytest.raises(EndpointConnectionError):
        await sandbox.execute("SIDE_EFFECT")
    assert sum("SIDE_EFFECT" in c for c in sends) == 2
    assert sandbox.ready  # still a proven refusal, not an unhealthy sandbox


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda: ConnectionClosedError(endpoint_url=URL), id="closed-before-response"),
        pytest.param(
            lambda: [_stream_then(ConnectionClosedError(endpoint_url=URL))],
            id="closed-after-response-before-output",
        ),
        pytest.param(
            lambda: [_stream_then(ConnectionClosedError(endpoint_url=URL), OUT)],
            id="closed-after-output",
        ),
        pytest.param(
            lambda: [_stream_then(EndpointConnectionError(endpoint_url=URL))],
            id="connect-error-after-response",
        ),
    ],
)
async def test_a_closed_connection_is_never_resent(subject, monkeypatch, failure):
    """The stale-connection signature is indistinguishable from "received, then closed"."""
    sandbox, client = subject
    step = failure()
    if isinstance(step, list):
        stream = step[0]
        step = None
        sends: list[str] = []

        def invoke(**kwargs):
            sends.append(kwargs["body"]["command"])
            return {"stream": stream}

        monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    else:
        sends = _scripted(monkeypatch, client, step)
    with pytest.raises(SandboxExecutionUncertainError, match="NOT replayed"):
        await sandbox.execute("SIDE_EFFECT")
    assert sum("SIDE_EFFECT" in c for c in sends) == 1
    assert not sandbox.ready


# --- traceability ------------------------------------------------------------------------------


async def test_the_platform_failure_warning_names_the_send(subject, monkeypatch, caplog):
    sandbox, client = subject
    _scripted(monkeypatch, client, ConnectionClosedError(endpoint_url=URL))

    async def call() -> None:
        rt.tag_command_caller("tooluse_spawn", 0)
        rt.tag_command_caller("tooluse_child", 1)
        with pytest.raises(SandboxExecutionUncertainError):
            await sandbox.execute("SIDE_EFFECT")

    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        await asyncio.create_task(call())  # its own copy of the context
    line = next(r.getMessage() for r in caplog.records if "platform failure" in r.getMessage())
    assert "caller=tooluse_spawn>tooluse_child (subagent depth=1)" in line
    assert "error=ConnectionClosedError" in line
    assert "pre_send=no" in line and "response=none" in line
    assert "elapsed=" in line and "ms" in line
    assert "request_id=none" in line
    assert sandbox.session_id in line


async def test_a_failure_after_the_response_logs_its_request_id(subject, monkeypatch, caplog):
    sandbox, client = subject
    _scripted(
        monkeypatch, client, [OUT, {"chunk": {"contentStop": {"exitCode": -1, "status": "COMPLETED"}}}]
    )
    with caplog.at_level(logging.WARNING, logger=rt.__name__), pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    line = next(r.getMessage() for r in caplog.records if "platform failure" in r.getMessage())
    assert "response=started" in line and "request_id=req-123" in line


async def test_each_agent_tags_its_tool_calls_commands(tmp_path, monkeypatch, caplog):
    """Through a real ``Agent``: the shell call's id, and the spawning call's for a subagent."""
    monkeypatch.setattr(rt, "_sleep", lambda _s: None)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FakeAgentCoreClient(workdir=str(workdir))
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_key="tagging", client=client, workdir=str(workdir)
    )
    sandbox._ready = True

    def closed(**_kwargs: Any) -> dict[str, Any]:
        raise ConnectionClosedError(endpoint_url=URL)

    monkeypatch.setattr(client, "invoke_agent_runtime_command", closed)
    agent = Agent(
        model=ScriptedModel(
            tool_use_events("shell", "tooluse_child_1", json.dumps({"command": "x"})),
            text_events("done"),
        ),
        sandbox=sandbox,
        tools=[shell],
        plugins=[SandboxNotices(sandbox, depth=1)],
        callback_handler=None,
    )

    async def as_subagent() -> None:
        rt.tag_command_caller("tooluse_spawn_9", 0)  # the parent's ``subagent`` call
        await agent.invoke_async("go")

    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        await asyncio.create_task(as_subagent())  # its own copy of the context
    assert "caller=tooluse_spawn_9>tooluse_child_1 (subagent depth=1)" in caplog.text


# --- connection health -------------------------------------------------------------------------


def test_the_command_client_keeps_one_connection_per_admitted_command_and_keepalive():
    config = rt.sandbox_client_config()
    assert config.tcp_keepalive is True
    assert config.max_pool_connections >= rt.SANDBOX_MAX_CONCURRENT_COMMANDS
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}  # still one HTTP attempt
    assert rt.sandbox_wake_client_config().tcp_keepalive is True


def test_the_installed_botocore_takes_the_idle_bound_and_keepalive_timing(monkeypatch):
    """Pins the botocore internals ``keep_connections_healthy`` edits; fails loudly on an upgrade."""
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    client = boto3.Session(region_name="us-west-2").client(
        "bedrock-agentcore", config=rt.sandbox_client_config()
    )
    assert rt.keep_connections_healthy(client) is client
    session = client._endpoint.http_session
    classes = session._manager.pool_classes_by_scheme
    assert all(getattr(cls, "_strandly_idle_bounded", False) for cls in classes.values())
    options = session._manager.connection_pool_kw["socket_options"]
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in options
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    assert (socket.IPPROTO_TCP, idle, rt.SANDBOX_CLIENT_KEEPALIVE_IDLE_SECONDS) in options
    rt.keep_connections_healthy(client)  # idempotent
    assert classes == session._manager.pool_classes_by_scheme
    assert len(options) == len(set(options))
    pool = session._manager.connection_from_url("https://example.invalid")
    assert pool.pool.maxsize == rt.sandbox_client_config().max_pool_connections


def test_a_connection_idle_past_the_bound_is_reconnected_not_reused(monkeypatch):
    monkeypatch.setattr(rt, "SANDBOX_CLIENT_MAX_IDLE_SECONDS", 0.2)
    server = IdleDroppingServer(idle_limit=60)
    try:
        host, port = server.url.rsplit("/", 1)[1].split(":")
        pool = rt._idle_bounded_pool(AWSHTTPConnectionPool)(host, int(port), maxsize=2)
        body = json.dumps({"command": "x"}).encode()

        def post() -> None:
            pool.urlopen("POST", "/", body=body, headers={"Content-Length": str(len(body))}).read()

        post()
        post()
        assert server.connections == 1  # reused while fresh
        time.sleep(0.3)
        post()
        assert server.connections == 2  # idle past the bound: a new connection
    finally:
        server.close()


def test_idle_dropped_connections_no_longer_fail_commands_on_the_wire(monkeypatch, caplog):
    """Real botocore: a burst after a quiet spell reuses connections the path forgot.

    Without the idle bound the burst's requests are reset unread (the soak's ConnectionClosedError,
    never seen by the service); with it they go out on new connections and every command succeeds.
    """
    server = IdleDroppingServer(idle_limit=0.6, delay=0.15)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_AGENTCORE", server.url)
    monkeypatch.setattr(rt, "SANDBOX_CLIENT_MAX_IDLE_SECONDS", 0.3)

    def scenario(sandbox: RuntimeSandbox) -> list[BaseException]:
        errors: list[BaseException] = []

        async def run(label: str) -> None:
            try:
                await sandbox.execute(f"echo {label}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        async def main() -> None:
            for burst in range(3):
                await asyncio.gather(*(run(f"burst{burst}-{i}") for i in range(4)))
                end = time.monotonic() + 1.0
                while time.monotonic() < end:  # one caller: only the top connection is used
                    await run("main")

        asyncio.run(main())
        return errors

    try:
        sandbox = RuntimeSandbox(runtime_arn=RUNTIME_ARN, session_key="wire", region="us-west-2")
        sandbox._ready = True
        with caplog.at_level(logging.WARNING, logger=rt.__name__):
            errors = scenario(sandbox)
        assert errors == []
        assert server.dropped == 0
        assert "platform failure" not in caplog.text

        # The same scenario on an unbounded pool fails the way the soak did, and nothing ran.
        monkeypatch.setattr(rt, "keep_connections_healthy", lambda client: client)
        before = len(server.commands)
        stale = RuntimeSandbox(runtime_arn=RUNTIME_ARN, session_key="wire-2", region="us-west-2")
        stale._ready = True
        errors = scenario(stale)
        assert server.dropped >= 1
        assert any(isinstance(e, SandboxExecutionUncertainError) for e in errors)
        assert "(ConnectionClosedError)" in str(errors[0])
        assert len(server.commands) > before
    finally:
        server.close()


def test_the_pool_hook_records_the_connection_a_send_used():
    trace = rt._SendTrace()
    rt._SEND_TRACE.current = trace
    try:
        server = IdleDroppingServer(idle_limit=60)
        host, port = server.url.rsplit("/", 1)[1].split(":")
        pool = rt._idle_bounded_pool(AWSHTTPConnectionPool)(host, int(port), maxsize=1)
        pool.urlopen("POST", "/", body=b'{"command": "x"}', headers={"Content-Length": "16"}).read()
        assert trace.connection == "new"
        pool.urlopen("POST", "/", body=b'{"command": "x"}', headers={"Content-Length": "16"}).read()
        assert trace.connection.startswith("reused after ")
        server.close()
    finally:
        rt._SEND_TRACE.current = None


def test_threads_have_their_own_send_trace():
    seen: list[object] = []
    rt._SEND_TRACE.current = rt._SendTrace()
    t = threading.Thread(target=lambda: seen.append(getattr(rt._SEND_TRACE, "current", None)))
    t.start()
    t.join()
    rt._SEND_TRACE.current = None
    assert seen == [None]
