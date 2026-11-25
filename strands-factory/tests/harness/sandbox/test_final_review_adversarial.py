"""Adversarial acceptance tests from the independent final review, plus regressions per blocker.

B1: every post-send outcome that may have executed is typed, never replayed, keeps its output.
B2: the dispatch boundary is exclusive with a stop/reset, not check-then-send.
B3: a failed approved reset stays failed, and lost setup is redone before user work.
Re-review N1: an error delivered on the result stream is never a proven rejection.
"""

from __future__ import annotations

import asyncio
import binascii
import json
import struct
import threading
import time
from types import SimpleNamespace
from typing import Any

import botocore.session
import pytest
from botocore.eventstream import EventStream
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from botocore.parsers import EventStreamJSONParser
from strands import Agent
from strands.hooks import AfterToolCallEvent
from strands.vended_tools.shell import shell
from test_platform_failure import PlatformClient
from test_runtime_sandbox import RUNTIME_ARN, FakeClientError

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.sandbox import runtime as runtime_module
from strandly.sandbox.notices import SandboxNotices
from strandly.sandbox.runtime import (
    RuntimeSandbox,
    SandboxCommandTimeoutError,
    SandboxExecutionUncertainError,
    SandboxPlatformError,
    SandboxUnavailableError,
)
from strandly.sandbox.scripts import WorkspaceHealth

DONE = {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}


@pytest.fixture
def subject(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = PlatformClient(str(workdir))
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_key="review-only", workdir=str(workdir), client=client
    )
    sandbox._ready = True
    return sandbox, client


def _client_error(code: str, status: int, message: str = "detail") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "InvokeAgentRuntimeCommand",
    )


# ---- reviewer's adversarial tests ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fault",
    [
        "read_timeout",
        "response_streaming",
        "http_metadata_502",
        "missing_stream",
        "unhashable_status",
        "non_dict_event",
    ],
)
async def test_post_send_unknowns_are_typed_and_fail_closed(subject, monkeypatch, fault):
    sandbox, client = subject
    sends = []

    def invoke(**kwargs):
        sends.append(kwargs)
        if fault == "read_timeout":
            raise ReadTimeoutError(endpoint_url="https://example.invalid")
        if fault == "http_metadata_502":
            raise _client_error("RuntimeClientError", 502, "Bad gateway")
        if fault == "missing_stream":
            return {"statusCode": 200}

        def stream():
            yield {"chunk": {"contentDelta": {"stdout": "partial-out", "stderr": "partial-err"}}}
            if fault == "response_streaming":
                raise ResponseStreamingError(error="connection reset")
            if fault == "non_dict_event":
                yield None
            else:
                yield {"chunk": {"contentStop": {"exitCode": 0, "status": []}}}

        return {"stream": stream()}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    with pytest.raises(Exception) as info:  # noqa: B017 — the type is asserted below
        await sandbox.execute("SIDE_EFFECT")
    actual = info.value
    assert len(sends) == 1
    assert isinstance(actual, SandboxExecutionUncertainError), (type(actual).__name__, sandbox.ready)
    assert not sandbox.ready


async def test_post_output_transport_fault_retains_output_and_disables_hook_retry(
    subject, monkeypatch
):
    sandbox, client = subject

    def invoke(**kwargs):
        def stream():
            yield {"chunk": {"contentDelta": {"stdout": "partial-out", "stderr": "partial-err"}}}
            raise ResponseStreamingError(error="connection reset")

        return {"stream": stream()}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    with pytest.raises(Exception) as info:  # noqa: B017 — the hook must recognise whatever it is
        await sandbox.execute("SIDE_EFFECT")
    actual = info.value
    wrapped = RuntimeError("SDK wrapper")
    wrapped.__cause__ = actual
    event = SimpleNamespace(
        exception=wrapped,
        retry=True,
        result={"toolUseId": "t", "status": "error", "content": [{"text": "transport failed"}]},
    )
    await SandboxNotices(sandbox)._deliver(event)
    assert event.retry is False, (type(actual).__name__, event.result)
    assert "partial-out" in str(event.result)


async def test_failed_reset_cannot_bypass_setup_via_unused_automatic_probe(subject, monkeypatch):
    sandbox, client = subject
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("first-fault")
    setup = []

    async def hook():
        setup.append(1)

    sandbox.add_first_use_hook(hook)
    sandbox._first_use_ran = 1  # already installed in the old container

    def failed_wake():
        raise RuntimeError("wake failed after successful stop")

    monkeypatch.setattr(sandbox, "_wake", failed_wake)
    with pytest.raises(SandboxPlatformError):
        await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    assert client.stop_count == 1
    assert not sandbox.ready
    # An unconsumed diagnostic probe must not erase a failed reset and skip lost setup.
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("printf unsafe-post-reset-dispatch")


def test_dispatch_boundary_cannot_send_after_reset_has_started(subject, monkeypatch):
    sandbox, client = subject
    passed_check, release_send = threading.Event(), threading.Event()
    stop_started, release_stop = threading.Event(), threading.Event()
    sends = []
    errors = []

    def on_send(sent):
        if sent:
            passed_check.set()
            assert release_send.wait(5)

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])
        return {"stream": iter([DONE])}

    def stop(**kwargs):
        stop_started.set()
        assert release_stop.wait(5)
        return {"statusCode": 500}  # fail closed after the race, no bootstrap subprocess

    def command():
        try:
            list(sandbox._retrying("LATE_SIDE_EFFECT", 1, allow_wake=True, on_send=on_send))
        except Exception as error:  # noqa: BLE001 — recorded for the assertions
            errors.append(error)

    def recover():
        try:
            sandbox._recover_platform_session_sync()
        except SandboxPlatformError:
            pass

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    monkeypatch.setattr(client, "stop_runtime_session", stop)
    sender = threading.Thread(target=command)
    reset = threading.Thread(target=recover)
    sender.start()
    try:
        assert passed_check.wait(5)
        sandbox._record_platform_failure(sandbox._restart_generation)
        reset.start()
        assert stop_started.wait(5)
        release_send.set()
        sender.join(5)
        assert not sender.is_alive()
        assert sends == [], f"dispatched while StopRuntimeSession was in progress: {sends}"
    finally:
        release_send.set()
        release_stop.set()
        sender.join(5)
        if reset.ident is not None:
            reset.join(5)


async def test_missing_bootstrap_response_cannot_mark_ready(subject, monkeypatch):
    sandbox, client = subject
    sandbox._ready = False
    sends = []

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])
        if len(sends) == 1:
            return {"statusCode": 200}  # bootstrap completed? unknown
        return {"stream": iter([DONE])}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    with pytest.raises(SandboxExecutionUncertainError):
        await sandbox.execute("USER_COMMAND_MUST_NOT_RUN")
    assert len(sends) == 1 and not sandbox.ready


async def test_real_agent_hook_cannot_replay_transport_uncertain_command(subject, monkeypatch):
    sandbox, client = subject
    sends = []

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])
        if len(sends) == 1:

            def stream():
                yield {"chunk": {"contentDelta": {"stdout": "side-effect-happened"}}}
                raise ResponseStreamingError(error="reset after execution")

            return {"stream": stream()}
        return {"stream": iter([DONE])}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    agent = Agent(
        model=ScriptedModel(
            tool_use_events("shell", "t1", '{"command":"SIDE_EFFECT"}'), text_events("done")
        ),
        sandbox=sandbox,
        tools=[shell],
        plugins=[SandboxNotices(sandbox)],
        callback_handler=None,
    )

    def retry_once(event):
        if len(sends) == 1:
            event.retry = True

    agent.add_hook(retry_once, AfterToolCallEvent, order=0)
    await agent.invoke_async("go")
    assert len(sends) == 1, f"uncertain SIDE_EFFECT was dispatched {len(sends)} times"


# ---- B1 regressions -----------------------------------------------------------------------------


def _raises(error: Exception):
    def invoke(**kwargs: Any) -> dict[str, Any]:
        raise error

    return invoke


def _streams(*events: Any, then: Exception | None = None):
    def invoke(**kwargs: Any) -> dict[str, Any]:
        def stream():
            yield from events
            if then is not None:
                raise then

        return {"stream": stream()}

    return invoke


PARTIAL = {"chunk": {"contentDelta": {"stdout": "partial-out"}}}


@pytest.mark.parametrize(
    ("invoke", "partial"),
    [
        (_raises(ConnectionClosedError(endpoint_url="https://example.invalid")), ""),
        (_raises(ValueError("unrecognised failure inside the command API")), ""),
        (_raises(_client_error("SomethingNewException", 503)), ""),
        (_raises(FakeClientError("SomethingNewException", "no status at all")), ""),
        (_raises(FakeClientError("RuntimeClientError", "runtime failed, status not stated")), ""),
        (_raises(_client_error("RuntimeClientError", 424, "runtime failed")), ""),
        (_streams(then=FakeClientError("SomethingNewException")), ""),
        # The API call returned, so the command was accepted: no code is a proven refusal any more.
        (_streams(then=FakeClientError("ThrottlingException")), ""),
        (_streams(then=FakeClientError("ResourceNotFoundException")), ""),
        (_streams(then=_client_error("ValidationException", 400)), ""),
        (_streams(PARTIAL, then=FakeClientError("ThrottlingException")), "partial-out"),
        (_streams(PARTIAL, then=_client_error("ValidationException", 400)), "partial-out"),
        (_streams(PARTIAL, {"chunk": {"contentDelta": {"stdout": 7}}}, DONE), "partial-out"),
        (_streams(PARTIAL, {"chunk": {"contentDelta": "not-a-delta"}}, DONE), "partial-out"),
    ],
    ids=[
        "connection-closed",
        "unrecognised-exception",
        "unknown-code-5xx-metadata",
        "unknown-code-no-status",
        "runtime-client-error-no-status",
        "runtime-client-error-424-only",
        "unknown-code-on-stream",
        "retryable-code-on-stream-before-output",
        "session-gone-code-on-stream-before-output",
        "rejection-code-on-stream-before-output",
        "retryable-code-after-output",
        "rejection-code-after-output",
        "non-string-output",
        "non-dict-delta",
    ],
)
async def test_unrecognised_post_send_outcomes_are_uncertain_and_keep_output(
    subject, monkeypatch, invoke, partial
):
    sandbox, client = subject
    sends = []

    def counted(**kwargs):
        sends.append(kwargs["body"]["command"])
        return invoke(**kwargs)

    monkeypatch.setattr(client, "invoke_agent_runtime_command", counted)
    with pytest.raises(SandboxExecutionUncertainError) as info:
        await sandbox.execute("SIDE_EFFECT")
    assert not isinstance(info.value, SandboxPlatformError)
    assert "NOT replayed" in str(info.value)
    assert "unrecognised failure" not in str(info.value), "SDK text stays out of diagnostics"
    assert info.value.stdout == partial
    assert len(sends) == 1
    assert not sandbox.ready
    assert client.stop_count == 0


@pytest.mark.parametrize(
    "error",
    [
        FakeClientError("AccessDeniedException", "nope"),
        FakeClientError("ValidationException", "bad"),
        FakeClientError("RuntimeClientError", "Received error (400) from runtime"),
        _client_error("SomethingNewException", 403),
        EndpointConnectionError(endpoint_url="https://example.invalid"),
    ],
    ids=["access-denied", "validation", "runtime-4xx", "unknown-code-4xx-metadata", "never-connected"],
)
async def test_proven_rejections_stay_ordinary_errors_and_keep_the_sandbox_ready(
    subject, monkeypatch, error
):
    sandbox, client = subject
    monkeypatch.setattr(client, "invoke_agent_runtime_command", _raises(error))
    with pytest.raises(type(error)) as info:
        await sandbox.execute("not-executed")
    assert info.value is error
    assert sandbox.ready
    assert sandbox._platform_error is None


async def test_uncertain_bootstrap_transport_failure_cannot_mark_ready(subject, monkeypatch):
    sandbox, client = subject
    sandbox._ready = False
    sends = []

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])
        if len(sends) == 1:
            raise ReadTimeoutError(endpoint_url="https://example.invalid")
        return {"stream": iter([DONE])}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    with pytest.raises(SandboxExecutionUncertainError):
        await sandbox.execute("USER_COMMAND_MUST_NOT_RUN")
    assert len(sends) == 1 and not sandbox.ready


async def test_abandoned_transport_timeout_disables_hook_retry(subject):
    sandbox, _ = subject
    error = SandboxCommandTimeoutError(timeout=5, context="[ctx]", stdout="partial-out")
    event = SimpleNamespace(
        exception=error,
        retry=True,
        result={"toolUseId": "t", "status": "error", "content": [{"text": str(error)}]},
    )
    await SandboxNotices(sandbox)._deliver(event)
    assert event.retry is False


# ---- B2 regressions -----------------------------------------------------------------------------


def _run(target) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def body():
        try:
            target()
        except Exception as error:  # noqa: BLE001 — recorded for the assertions
            errors.append(error)

    thread = threading.Thread(target=body)
    thread.start()
    return thread, errors


def _wait_for(condition, seconds: float = 5.0) -> bool:
    waited = threading.Event()
    for _ in range(int(seconds / 0.01)):
        if condition():
            return True
        waited.wait(0.01)
    return False


def test_reset_is_refused_while_an_admitted_send_is_still_inside_the_command_api(
    subject, monkeypatch
):
    sandbox, client = subject
    monkeypatch.setattr(runtime_module, "_DISPATCH_QUIESCE_SECONDS", 0.2, raising=False)
    entered, release = threading.Event(), threading.Event()

    def invoke(**kwargs):
        entered.set()
        assert release.wait(5)
        return {"stream": iter([DONE])}

    original = client.invoke_agent_runtime_command
    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    sender, errors = _run(lambda: list(sandbox._retrying("ADMITTED", 1, allow_wake=True)))
    try:
        assert entered.wait(5)
        sandbox._record_platform_failure(sandbox._restart_generation)
        with pytest.raises(SandboxUnavailableError, match="not attempted") as info:
            sandbox._recover_platform_session_sync()
        assert not isinstance(info.value, SandboxExecutionUncertainError)
        assert client.stop_count == 0, "stopped underneath a send that had not left the client"
        assert not sandbox._platform_reset_attempted, "an unattempted reset keeps its one budget"
    finally:
        release.set()
        sender.join(5)
    assert errors == []
    monkeypatch.setattr(client, "invoke_agent_runtime_command", original)
    # Once the send has returned from the API the same approved reset goes through.
    sandbox._recover_platform_session_sync()
    assert client.stop_count == 1 and sandbox.ready


def test_reset_waits_for_an_admitted_send_to_leave_before_stopping(subject, monkeypatch):
    sandbox, client = subject
    order: list[str] = []
    entered, release = threading.Event(), threading.Event()

    def invoke(**kwargs):
        entered.set()
        assert release.wait(5)
        order.append("send-returned")
        return {"stream": iter([DONE])}

    def stop(**kwargs):
        order.append("stop")
        return {"statusCode": 500}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    monkeypatch.setattr(client, "stop_runtime_session", stop)
    sender, _ = _run(lambda: list(sandbox._retrying("ADMITTED", 1, allow_wake=True)))
    assert entered.wait(5)
    sandbox._record_platform_failure(sandbox._restart_generation)
    reset, _ = _run(sandbox._recover_platform_session_sync)
    try:
        assert not _wait_for(lambda: bool(order), 0.3), (
            f"stopped underneath a send that had not left the client: {order}"
        )
    finally:
        release.set()
        sender.join(5)
        reset.join(5)
    assert order == ["send-returned", "stop"]


async def test_queued_command_cannot_cross_a_completed_reset(subject, monkeypatch):
    sandbox, client = subject
    resets: list[threading.Thread] = []

    def reset_while_queued():  # called after the command is readied, before it is sent
        if resets:
            return

        def reset():
            sandbox._record_platform_failure(sandbox._restart_generation)
            sandbox._recover_platform_session_sync()

        resets.append(threading.Thread(target=reset))
        resets[0].start()
        resets[0].join(10)

    monkeypatch.setattr(sandbox, "_bump_ttl", reset_while_queued)
    with pytest.raises(SandboxUnavailableError, match="NOT sent") as info:
        await sandbox.execute("QUEUED_BEFORE_RESET")
    assert not isinstance(info.value, SandboxExecutionUncertainError)
    assert sandbox.ready and client.stop_count == 1
    assert not any("QUEUED_BEFORE_RESET" in command for command in client.sent)
    # A command issued after the reset is readied in the new generation and runs.
    assert (await sandbox.execute("printf after-reset")).stdout == "after-reset"


@pytest.mark.parametrize("path", ["stop_session", "_stop_session_sync"])
def test_best_effort_stops_hold_the_same_dispatch_fence(subject, monkeypatch, path):
    sandbox, client = subject
    order: list[str] = []
    stopping, release = threading.Event(), threading.Event()

    def stop(**kwargs):
        stopping.set()
        assert release.wait(5)
        order.append("stop-returned")
        return {"statusCode": 200}

    def invoke(**kwargs):
        order.append("send")
        return {"stream": iter([DONE])}

    monkeypatch.setattr(client, "stop_runtime_session", stop)
    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    if path == "stop_session":
        stopper, _ = _run(lambda: asyncio.run(sandbox.stop_session()))
    else:
        stopper, _ = _run(sandbox._stop_session_sync)
    assert stopping.wait(5)
    sender, errors = _run(lambda: list(sandbox._retrying("DURING_STOP", 1, allow_wake=True)))
    try:
        assert not _wait_for(lambda: bool(order), 0.3), (
            f"dispatched while StopRuntimeSession was in progress: {order}"
        )
    finally:
        release.set()
        stopper.join(5)
        sender.join(5)
    assert order == ["stop-returned", "send"] and errors == []


def test_session_gone_wake_does_not_run_after_a_platform_fault(subject, monkeypatch):
    sandbox, client = subject
    entered, release = threading.Event(), threading.Event()

    def invoke(**kwargs):
        entered.set()
        assert release.wait(5)
        raise FakeClientError("ResourceNotFoundException", "session not found")

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    sender, errors = _run(lambda: list(sandbox._retrying("IN_FLIGHT", 1, allow_wake=True)))
    assert entered.wait(5)
    sandbox._record_platform_failure(sandbox._restart_generation)
    release.set()
    sender.join(5)
    assert len(errors) == 1 and isinstance(errors[0], SandboxPlatformError)
    assert not client.wakes


# ---- B3 regressions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stage",
    ["stop-raises", "stop-unknown", "wake", "health", "bootstrap", "bootstrap-refused"],
)
async def test_next_command_after_a_failed_reset_fails_closed_at_every_stage(
    subject, monkeypatch, stage
):
    sandbox, client = subject
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("first-fault")
    setup = []

    async def hook():
        setup.append(1)

    sandbox.add_first_use_hook(hook)
    sandbox._first_use_ran = 1  # installed in the old container
    client.healthy = True  # the automatic probe would pass if it were allowed to run
    if stage == "stop-raises":
        client.stop_fails = True
    elif stage == "stop-unknown":
        monkeypatch.setattr(client, "stop_runtime_session", lambda **kw: {"statusCode": 202})
    elif stage == "wake":
        monkeypatch.setattr(sandbox, "_wake", _raises(RuntimeError("wake failed")))
    elif stage == "health":
        monkeypatch.setattr(sandbox, "_command_plane_healthy_sync", lambda: False)
    elif stage == "bootstrap":
        monkeypatch.setattr(sandbox, "_bootstrap", lambda **kw: WorkspaceHealth(writable=False))
    else:
        original = client.invoke_agent_runtime_command

        def refuse_bootstrap(**kwargs):
            if "user.email" in kwargs["body"]["command"]:
                raise FakeClientError("AccessDeniedException", "refused, so setup did not run")
            return original(**kwargs)

        monkeypatch.setattr(client, "invoke_agent_runtime_command", refuse_bootstrap)
    with pytest.raises(SandboxPlatformError):
        await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    assert not sandbox.ready
    assert sandbox._first_use_ran == 0, "setup in a possibly-stopped container is not kept"
    sent = len(client.sent)
    for _ in range(2):
        with pytest.raises(SandboxPlatformError):
            await sandbox.execute("printf unsafe-post-reset-dispatch")
    assert len(client.sent) == sent, "neither a probe nor user work is sent after a failed reset"
    assert setup == [] and not sandbox.ready
    with pytest.raises(SandboxPlatformError):
        await sandbox.recover_platform_session(allow_ephemeral_loss=True)


async def test_reset_invalidates_setup_before_the_stop_is_sent(subject, monkeypatch):
    sandbox, client = subject
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("first-fault")
    sandbox.add_first_use_hook(lambda: asyncio.sleep(0))
    sandbox._first_use_ran = 1
    generation = sandbox._restart_generation
    seen: dict[str, Any] = {}
    original = client.stop_runtime_session

    def stop(**kwargs):
        seen.update(
            ready=sandbox.ready,
            hooks=sandbox._first_use_ran,
            incomplete=getattr(sandbox, "_platform_reset_incomplete", None),
            needs_bootstrap=sandbox._platform_needs_bootstrap,
            generation=sandbox._restart_generation,
            fenced=getattr(sandbox, "_dispatch_fences", None),
        )
        return original(**kwargs)

    monkeypatch.setattr(client, "stop_runtime_session", stop)
    await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    assert seen == {
        "ready": False,
        "hooks": 0,
        "incomplete": True,
        "needs_bootstrap": True,
        "generation": generation + 1,
        "fenced": 1,
    }
    assert sandbox.ready and not sandbox._platform_reset_incomplete
    assert sandbox._dispatch_fences == 0 and sandbox._dispatching == 0


async def test_successful_reset_reruns_first_use_setup_before_the_next_command(subject):
    sandbox, client = subject
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("first-fault")

    async def hook():
        await sandbox.execute("printf first-use-setup")

    sandbox.add_first_use_hook(hook)
    sandbox._first_use_ran = 1  # installed in the old container, which the reset discards
    await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    result = await sandbox.execute("printf user-command")
    assert result.stdout == "user-command"
    setup = next(i for i, command in enumerate(client.sent) if "first-use-setup" in command)
    user = next(i for i, command in enumerate(client.sent) if "user-command" in command)
    assert setup < user
    # A later fault in the recovered session may still use the one automatic probe.
    client.healthy = False
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("second-fault")
    client.healthy = True
    assert (await sandbox.execute("printf after-probe")).stdout == "after-probe"


async def test_approved_stop_uses_the_bounded_one_attempt_control_client(subject):
    sandbox, client = subject
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("first-fault")

    class Control(PlatformClient):
        def stop_runtime_session(self, **kwargs: Any) -> dict[str, Any]:
            client.healthy = True
            return super().stop_runtime_session(**kwargs)

    control = Control(sandbox.workdir)
    sandbox._wake_client = control  # the wake client: one attempt, bounded read timeout
    await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    assert control.stop_count == 1 and client.stop_count == 0
    assert sandbox.ready


# ---- re-review N1: errors on the result stream --------------------------------------------------


def _frame(headers: dict[str, str], payload: dict[str, Any] | None = None) -> bytes:
    """One event-stream message, as the service would put it on the wire."""
    head = b""
    for name, value in headers.items():
        head += bytes([len(name)]) + name.encode() + b"\x07"
        head += struct.pack(">H", len(value)) + value.encode()
    body = b"" if payload is None else json.dumps(payload).encode()
    prelude = struct.pack(">II", 16 + len(head) + len(body), len(head))
    message = prelude + struct.pack(">I", binascii.crc32(prelude)) + head + body
    return message + struct.pack(">I", binascii.crc32(message))


def _wire(*frames: bytes):
    """A result stream parsed by botocore itself from raw frames (no fake client in the parse)."""
    operation = (
        botocore.session.get_session()
        .get_service_model("bedrock-agentcore")
        .operation_model("InvokeAgentRuntimeCommand")
    )
    raw = SimpleNamespace(stream=lambda: iter([b"".join(frames)]), close=lambda: None)
    return lambda: EventStream(
        raw, operation.output_shape.members["stream"], EventStreamJSONParser(), operation.name
    )


def _error_frame(code: str) -> bytes:
    return _frame({":message-type": "error", ":error-code": code, ":error-message": "detail"})


def _typed_frame(message_type: str, name: str) -> bytes:
    headers = {":message-type": message_type, f":{message_type}-type": name}
    return _frame({**headers, ":content-type": "application/json"}, {"message": "detail"})


@pytest.mark.parametrize(
    "stream",
    [
        lambda: iter([{"throttlingException": {"message": "detail"}}]),
        lambda: iter([{"resourceNotFoundException": {"message": "detail"}}]),
        lambda: iter([{"validationException": {"message": "detail"}}]),
        _wire(_error_frame("ThrottlingException")),
        _wire(_error_frame("RetryableConflictException")),
        _wire(_error_frame("ResourceNotFoundException")),
        _wire(_error_frame("ValidationException")),
        _wire(_typed_frame("event", "throttlingException")),
        _wire(_typed_frame("event", "resourceNotFoundException")),
        _wire(_typed_frame("exception", "throttlingException")),
        _wire(_typed_frame("exception", "resourceNotFoundException")),
        _wire(_typed_frame("exception", "ThrottlingException")),
    ],
    ids=[
        "member-throttle",
        "member-session-gone",
        "member-validation",
        "error-frame-throttle",
        "error-frame-conflict",
        "error-frame-session-gone",
        "error-frame-validation",
        "event-frame-throttle",
        "event-frame-session-gone",
        "exception-frame-throttle",
        "exception-frame-session-gone",
        "exception-frame-pascal-case",
    ],
)
async def test_an_error_on_the_result_stream_is_uncertain_and_sent_once(
    subject, monkeypatch, stream
):
    """HTTP 200 accepted the command before any frame arrived, whatever code the frame carries."""
    sandbox, client = subject
    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)
    sends = []

    def invoke(**kwargs: Any) -> dict[str, Any]:
        sends.append(kwargs["body"]["command"])
        return {"stream": stream()}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    with pytest.raises(SandboxExecutionUncertainError) as info:
        await sandbox.execute("SIDE_EFFECT")
    assert "NOT replayed" in str(info.value)
    assert len(sends) == 1
    assert not client.wakes, "no wake in order to resend"
    assert client.stop_count == 0
    assert not sandbox.ready


@pytest.mark.parametrize(
    ("code", "status"), [("ThrottlingException", 429), ("RetryableConflictException", 409)]
)
async def test_the_same_code_refused_at_the_http_level_is_still_retried(
    subject, monkeypatch, code, status
):
    sandbox, client = subject
    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)
    sends = []

    def invoke(**kwargs: Any) -> dict[str, Any]:
        sends.append(kwargs["body"]["command"])
        if len(sends) == 1:
            raise _client_error(code, status)
        return {"stream": iter([DONE])}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    result = await sandbox.execute("not-executed-the-first-time")
    assert result.exit_code == 0
    assert len(sends) == 2
    assert sandbox.ready and sandbox._platform_error is None


# ---- re-review N3 / N4: the I/O-fault restart's best-effort stop -----------------------------------


def _io_fault_restart(sandbox, client, monkeypatch, calls):
    """Patch a stop that blocks until released, a slow wake and a healthy bootstrap; return gates."""
    stop_started, release_stop = threading.Event(), threading.Event()

    def invoke(**kwargs):
        calls.append("cmd")
        return {"stream": iter([DONE])}

    def stop(**kwargs):
        calls.append("stop")
        stop_started.set()
        assert release_stop.wait(5)
        return {"statusCode": 200}

    def wake():
        calls.append("wake")
        time.sleep(0.3)

    def bootstrap(force_prune=False):
        calls.append("bootstrap")
        return WorkspaceHealth(writable=True)

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    monkeypatch.setattr(client, "stop_runtime_session", stop)
    monkeypatch.setattr(sandbox, "_wake", wake)
    monkeypatch.setattr(sandbox, "_bootstrap", bootstrap)

    def restart():
        with sandbox._ready_lock:
            sandbox._settle_workspace(WorkspaceHealth(writable=False, pruned_kb=0, detail="Input/output error"))

    return threading.Thread(target=restart), stop_started, release_stop


def test_a_command_readied_before_the_io_fault_restart_is_not_sent_during_it(subject, monkeypatch):
    sandbox, client = subject
    calls, errors = [], []
    restarter, stop_started, release_stop = _io_fault_restart(sandbox, client, monkeypatch, calls)
    generation = sandbox._restart_generation

    def command():
        try:
            list(sandbox._retrying("WORK", 1, allow_wake=True, seen_generation=generation))
        except Exception as error:  # noqa: BLE001 — recorded for the assertions
            errors.append(error)

    sender = threading.Thread(target=command)
    restarter.start()
    try:
        assert stop_started.wait(5)
        sender.start()
        time.sleep(0.2)  # the sender now waits on the stop's fence
        release_stop.set()
        restarter.join(5)
        sender.join(5)
    finally:
        release_stop.set()
    assert calls == ["stop", "wake", "bootstrap"]
    assert len(errors) == 1 and "NOT sent" in str(errors[0])


def test_a_command_cancelled_while_waiting_on_the_fence_is_never_sent(subject, monkeypatch):
    sandbox, client = subject
    calls, errors = [], []
    restarter, stop_started, release_stop = _io_fault_restart(sandbox, client, monkeypatch, calls)
    cancel = threading.Event()

    def command():
        try:
            list(sandbox._retrying("WORK", 1, allow_wake=True, cancel=cancel))
        except BaseException as error:  # noqa: BLE001 — recorded for the assertions
            errors.append(error)

    sender = threading.Thread(target=command)
    restarter.start()
    try:
        assert stop_started.wait(5)
        sender.start()
        time.sleep(0.2)
        cancel.set()
        release_stop.set()
        restarter.join(5)
        sender.join(5)
        time.sleep(0.4)
    finally:
        release_stop.set()
    assert "cmd" not in calls
    assert len(errors) == 1 and "abandoned before it was sent" in str(errors[0])


async def test_the_cap_not_the_default_thread_pool_limits_concurrent_commands(subject, monkeypatch):
    """P3-B: pumps run on the adapter's own pool, so a small default executor (production's is
    ``min(32, cpus + 4)``) neither queues commands under the cap nor hides the cap's refusal."""
    import concurrent.futures

    monkeypatch.setattr(runtime_module, "SANDBOX_MAX_CONCURRENT_COMMANDS", 3)
    sandbox, client = subject
    loop = asyncio.get_running_loop()
    small = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(small)
    release, sends = threading.Event(), []

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])

        def stream():
            assert release.wait(5)
            yield DONE

        return {"stream": stream()}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    held = [asyncio.create_task(sandbox.execute("HOLD")) for _ in range(3)]
    try:
        for _ in range(100):
            if len(sends) == 3:
                break
            await asyncio.sleep(0.02)
        assert len(sends) == 3  # all under the cap are sent, though the default pool has 2 threads
        with pytest.raises(SandboxUnavailableError, match="already has 3 commands running"):
            await asyncio.wait_for(sandbox.execute("OVER"), 2)
    finally:
        release.set()
    assert [r.exit_code for r in await asyncio.gather(*held)] == [0, 0, 0]


async def test_commands_over_the_per_session_cap_are_refused_and_not_sent(subject, monkeypatch):
    assert runtime_module.SANDBOX_MAX_CONCURRENT_COMMANDS == 64
    monkeypatch.setattr(runtime_module, "SANDBOX_MAX_CONCURRENT_COMMANDS", 3)
    sandbox, client = subject
    release, sends = threading.Event(), []

    def invoke(**kwargs):
        sends.append(kwargs["body"]["command"])

        def stream():
            assert release.wait(5)
            yield DONE

        return {"stream": stream()}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    held = [asyncio.create_task(sandbox.execute("HOLD")) for _ in range(3)]
    try:
        for _ in range(100):
            if len(sends) == 3:
                break
            await asyncio.sleep(0.02)
        with pytest.raises(SandboxUnavailableError, match="already has 3 commands running.*NOT sent"):
            await sandbox.execute("OVER")
        assert len(sends) == 3
    finally:
        release.set()
    assert [r.exit_code for r in await asyncio.gather(*held)] == [0, 0, 0]
    assert (await sandbox.execute("AFTER")).exit_code == 0
    assert len(sends) == 4
