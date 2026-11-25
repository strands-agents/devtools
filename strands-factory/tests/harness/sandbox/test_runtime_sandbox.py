"""Tests for :class:`~strandly.sandbox.runtime.RuntimeSandbox`.

The fake ``bedrock-agentcore`` client below **really runs the command** with ``subprocess`` and
returns real ``contentDelta``/``contentStop`` events. That is deliberate: the interesting part of this
adapter is the shell string it composes (``/bin/bash -c '<cd && export && …>'``) and the fact that the
SDK's inherited file/code primitives — heredocs, ``base64`` pipelines, ``ls -1ap`` parsing — survive
that composition. A client that returned canned bytes would assert the shape of our own mock instead.

Everything stays hermetic: the host shell, ``tmp_path``, no AWS, no network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import threading
import time
from typing import Any

import pytest
from strands.sandbox.errors import SandboxPathNotFoundError
from strands.sandbox.types import ExecutionResult, StreamChunk

from strandly.core.constants import (
    SANDBOX_COMMAND_MAX_BYTES,
    SANDBOX_COMMAND_TIMEOUT_DEFAULT,
    SANDBOX_COMMAND_TIMEOUT_MAX,
    SANDBOX_MOUNT_PATH,
    SANDBOX_WRITE_CHUNK_CHARS,
)
from strandly.sandbox import runtime as runtime_module
from strandly.sandbox.runtime import (
    RuntimeSandbox,
    SandboxCommandTimeoutError,
    SandboxExecutionUncertainError,
    SandboxUnavailableError,
    _clamp_timeout,
    sandbox_session_id,
)
from strandly.sandbox.scripts import git_bootstrap_script

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/strandly_sandbox-abc"


class FakeClientError(Exception):
    """Stand-in for a botocore ``ClientError``: classified by ``response['Error']['Code']``."""

    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class _FakeBody:
    """The streaming body ``invoke_agent_runtime`` returns — read once, like the real one."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeAgentCoreClient:
    """Runs commands for real, and records every call so the adapter's behaviour is observable."""

    def __init__(self, *, workdir: str, fail_with: list[Exception] | None = None) -> None:
        self.workdir = workdir
        self.commands: list[str] = []
        self.wakes: list[str] = []
        self.stops: list[str] = []
        # Exceptions to raise, in order, before any command succeeds — for the retry/recovery paths.
        self.fail_with = list(fail_with or [])
        # Set to force the service's own timeout verdict without actually waiting.
        self.force_status: str | None = None
        # Set to drop the terminal contentStop event (the "stream ended without telling us" case).
        self.drop_content_stop = False
        # Extra raw events injected before the real ones (service exception members).
        self.inject_events: list[dict[str, Any]] = []
        self.concurrent_peak = 0
        self._in_flight = 0
        # Wake payloads, verbatim — the git credential travels here rather than in a command, so
        # several security assertions read them.
        self.wake_payloads: list[dict[str, Any]] = []
        # Extra keys the wake answers with (missingTools / workspaceReady), for the ops-warning paths.
        self.wake_status: dict[str, Any] = {}
        # Set False to imitate a sandbox image that ignores the credential (an older image).
        self.confirm_credential = True
        # Seconds the wake blocks for — how the cross-loop tests make contention deterministic.
        self.wake_delay = 0.0

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        if self.wake_delay:
            time.sleep(self.wake_delay)
        self.wakes.append(kwargs["runtimeSessionId"])
        payload = json.loads(kwargs["payload"].decode())
        self.wake_payloads.append(payload)
        # Answer the way sandbox_main.py does, through a streaming-body-shaped object: the adapter
        # reads this to learn the three things only the sandbox knows (credential written? workspace
        # writable? tools present?), so a fake that returned raw bytes would leave that path untested.
        body = {"status": "ready", **self.wake_status}
        if payload.get("gitCredential") and self.confirm_credential:
            body["gitCredentialStatus"] = "written"
        return {"response": _FakeBody(json.dumps(body).encode())}

    def stop_runtime_session(self, **kwargs: Any) -> dict[str, Any]:
        self.stops.append(kwargs["runtimeSessionId"])
        return {"runtimeSessionId": kwargs["runtimeSessionId"]}

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_with:
            raise self.fail_with.pop(0)
        command = kwargs["body"]["command"]
        self.commands.append(command)
        self._in_flight += 1
        self.concurrent_peak = max(self.concurrent_peak, self._in_flight)
        try:
            # The adapter sends "/bin/bash -c '<script>'" — run it exactly as sent.
            done = subprocess.run(command, shell=True, capture_output=True, text=True)  # noqa: S602
        finally:
            self._in_flight -= 1
        events: list[dict[str, Any]] = [*self.inject_events, {"chunk": {"contentStart": {}}}]
        if done.stdout:
            events.append({"chunk": {"contentDelta": {"stdout": done.stdout}}})
        if done.stderr:
            events.append({"chunk": {"contentDelta": {"stderr": done.stderr}}})
        if not self.drop_content_stop:
            events.append(
                {
                    "chunk": {
                        "contentStop": {
                            "exitCode": done.returncode,
                            "status": self.force_status or "COMPLETED",
                        }
                    }
                }
            )
        return {"stream": iter(events)}


@pytest.fixture(autouse=True)
def _no_retry_delays(monkeypatch):
    """Retry/backoff delays are real seconds in production; the behaviour, not the wait, is tested."""
    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)


@pytest.fixture
def sandbox(tmp_path) -> RuntimeSandbox:
    """A sandbox whose "session filesystem" is a temp dir standing in for the persistent mount."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FakeAgentCoreClient(workdir=str(workdir))
    return RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_key="gh-strands-agents-sdk-python-issue-42",
        workdir=str(workdir),
        client=client,
    )


def _client(sandbox: RuntimeSandbox) -> FakeAgentCoreClient:
    return sandbox._client  # type: ignore[return-value]


def _real_commands(sandbox: RuntimeSandbox) -> list[str]:
    """Commands excluding the one-off git bootstrap, which every first call pays for."""
    return [c for c in _client(sandbox).commands if "strandly-sandbox: git configured" not in c]


def _append_commands(sandbox: RuntimeSandbox) -> list[str]:
    """The base64 append steps of ``write_file``.

    Matched on the scratch-file suffix + append redirect rather than on ``printf '%s'``: the whole
    script is ``shlex.quote``d into ``/bin/bash -c '…'``, so the inner single quotes are re-escaped
    and a naive substring match on them would silently never match.
    """
    return [c for c in _client(sandbox).commands if ".b64" in c and ">>" in c]


# ---- session identity: the whole of cross-invocation persistence -------------------------------


def test_session_id_is_derived_and_stable():
    """The same harness session must always map to the same sandbox session.

    This IS the persistence mechanism: a stable id means invocation N+1 addresses the same session,
    and managed session storage restores its filesystem. A random or state-carried id would silently
    lose the workspace between turns.
    """
    key = "gh-strands-agents-sdk-python-issue-42"
    assert sandbox_session_id(key) == sandbox_session_id(key)
    assert sandbox_session_id(key) != sandbox_session_id("gh-other-issue-43")


def test_session_id_meets_the_service_floor_and_is_namespaced():
    """>= 33 chars (the API's opaque-ValidationException floor) and never the agent runtime's id."""
    from strandly.core.constants import RUNTIME_SESSION_ID_MIN_LEN, SANDBOX_SESSION_PREFIX
    from strandly.core.session_ids import runtime_session_id

    assert len(sandbox_session_id("s")) >= RUNTIME_SESSION_ID_MIN_LEN
    assert sandbox_session_id("s").startswith(SANDBOX_SESSION_PREFIX)
    # Two runtimes, two id spaces: the sandbox session must not collide with the agent's affinity id.
    assert sandbox_session_id("abc") != runtime_session_id("abc")


def test_a_sessionless_invoke_gets_a_fresh_unshared_session():
    """No session key ⇒ a unique id, so a one-shot run can't inherit another run's workspace."""
    assert sandbox_session_id(None) != sandbox_session_id(None)


# ---- the composed shell command ---------------------------------------------------------------


async def test_commands_run_in_the_workspace_by_default(sandbox):
    """Every command's default cwd is the persistent mount, so unqualified work lands there."""
    result = await sandbox.execute("pwd")
    assert result.stdout.strip() == sandbox.workdir
    assert result.exit_code == 0


async def test_cwd_and_env_are_applied_and_the_caller_wins(sandbox):
    """``cwd`` overrides the workspace, and an explicit env value beats our own pager pin."""
    result = await sandbox.execute("printf '%s|%s' \"$(pwd)\" \"$GIT_PAGER\"", cwd="/tmp", env={"GIT_PAGER": "mine"})
    where, pager = result.stdout.split("|")
    assert where == "/tmp"
    assert pager == "mine", "the caller's env must override the sandbox's pager pin"


async def test_pager_is_pinned_by_default(sandbox):
    """The image has no pager, so a bare `git log` would hang/fail without this."""
    result = await sandbox.execute('printf "%s" "$GIT_PAGER"')
    assert result.stdout == "cat"


async def test_output_streams_incrementally_before_the_result(sandbox):
    """stdout/stderr arrive as ``StreamChunk``s, and the ``ExecutionResult`` is last.

    Streaming is the reason this API was chosen over the Code Interpreter (which only returned result
    events after completion), so the ordering contract is worth pinning.
    """
    chunks: list[Any] = []
    async for item in sandbox.execute_streaming("echo out; echo err >&2"):
        chunks.append(item)

    assert isinstance(chunks[-1], ExecutionResult)
    stream = [c for c in chunks if isinstance(c, StreamChunk)]
    assert {c.stream_type for c in stream} == {"stdout", "stderr"}
    assert "out" in chunks[-1].stdout and "err" in chunks[-1].stderr


async def test_non_zero_exit_is_a_result_not_an_exception(sandbox):
    """A failing command is data for the model, not an error for the harness."""
    result = await sandbox.execute("exit 3")
    assert result.exit_code == 3


# ---- absolute paths: the catch-22 the Code Interpreter created -------------------------------


async def test_absolute_paths_work_for_every_file_primitive(sandbox, tmp_path):
    """read/write/remove/list all accept absolute paths — the regression that killed the old backend.

    The Code Interpreter's file API rejected every absolute path as "potential path traversal" while
    the harness's ``read``/``write``/``edit`` and the SDK file editor *require* one: a 100% failure rate on
    those tools in production. Being shell-backed, this backend has no such split, and this test is
    the guard that it stays that way.
    """
    target = f"{sandbox.workdir}/nested/dir/file.txt"
    await sandbox.write_file(target, b"hello")
    assert await sandbox.read_file(target) == b"hello"

    entries = await sandbox.list_files(f"{sandbox.workdir}/nested/dir")
    assert [e.name for e in entries] == ["file.txt"]
    assert entries[0].is_dir is False

    await sandbox.remove_file(target)
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file(target)


async def test_list_files_marks_directories(sandbox):
    """``is_dir`` must be right: the skills loader filters subdirectories on it.

    A backend that mislabelled directories as files silently emptied the whole skill set once — the
    inherited ``ls -1ap`` parsing is what gets this right here.
    """
    await sandbox.write_file(f"{sandbox.workdir}/tree/child/leaf.txt", b"x")
    entries = {e.name: e.is_dir for e in await sandbox.list_files(f"{sandbox.workdir}/tree")}
    assert entries == {"child": True}


async def test_list_files_missing_dir_raises_path_not_found(sandbox):
    """A missing directory is distinguishable from a transport failure (the SDK's own contract)."""
    with pytest.raises(SandboxPathNotFoundError):
        await sandbox.list_files(f"{sandbox.workdir}/nope")


async def test_binary_content_round_trips(sandbox):
    """Binary safety: skills ship assets and `write` takes arbitrary bytes."""
    blob = bytes(range(256)) * 4
    path = f"{sandbox.workdir}/blob.bin"
    await sandbox.write_file(path, blob)
    assert await sandbox.read_file(path) == blob


async def test_execute_code_runs_through_the_shell(sandbox):
    """``execute_code`` is the SDK's base64-heredoc pipeline; it must survive our bash wrapping."""
    result = await sandbox.execute_code("import sys; print('from python', 1 + 1)", "python3")
    assert result.exit_code == 0
    assert "from python 2" in result.stdout


async def test_execute_code_carries_shell_metacharacters_safely(sandbox):
    """Quotes/newlines/``$`` in source must not break out of the wrapper."""
    code = 'print("it\'s $HOME and `backticks`")\nprint("second line")'
    result = await sandbox.execute_code(code, "python3")
    assert "it's $HOME and `backticks`" in result.stdout
    assert "second line" in result.stdout


# ---- write_file chunking: the 64 KB command limit ---------------------------------------------


async def test_large_writes_are_chunked_under_the_command_limit(sandbox):
    """A file bigger than one command must still write — and no command may exceed 64 KB.

    The SDK's inherited ``write_file`` inlines the whole base64 payload in one command, capping writes
    at ~48 KB with an opaque ValidationException above it.
    """
    content = bytes(range(256)) * 800  # ~205 KB → ~273 KB of base64, several chunks
    path = f"{sandbox.workdir}/big.bin"
    await sandbox.write_file(path, content)

    assert await sandbox.read_file(path) == content
    appends = _append_commands(sandbox)
    assert len(appends) > 1, "a multi-chunk payload was not chunked"
    for command in _client(sandbox).commands:
        assert len(command.encode()) <= SANDBOX_COMMAND_MAX_BYTES


async def test_a_small_write_is_a_single_chunk(sandbox):
    """The common case stays cheap: one append, not one per arbitrary block."""
    await sandbox.write_file(f"{sandbox.workdir}/small.txt", b"x" * 100)
    assert len(_append_commands(sandbox)) == 1


async def test_writes_leave_no_scratch_files_behind(sandbox):
    """Scratch/staging files must be cleaned up: a stray ``.b64`` would look like a source file."""
    await sandbox.write_file(f"{sandbox.workdir}/pkg/mod.py", b"print(1)\n")
    listed = {e.name for e in await sandbox.list_files(f"{sandbox.workdir}/pkg")}
    assert listed == {"mod.py"}


async def test_write_chunk_size_leaves_room_for_the_wrapper():
    """The chunk size must be comfortably under the limit, since each chunk is wrapped + quoted."""
    assert SANDBOX_WRITE_CHUNK_CHARS < SANDBOX_COMMAND_MAX_BYTES


async def test_an_oversized_command_is_refused_with_advice(sandbox):
    """Better an actionable error than the service's opaque ValidationException."""
    with pytest.raises(ValueError, match="over the .* limit"):
        await sandbox.execute("echo " + "x" * SANDBOX_COMMAND_MAX_BYTES)


# ---- timeouts ----------------------------------------------------------------------------------


def test_timeouts_are_clamped_to_the_service_range():
    """``None`` → our default; out-of-range → clamped, never sent as-is."""
    assert _clamp_timeout(None) == SANDBOX_COMMAND_TIMEOUT_DEFAULT
    assert _clamp_timeout(0) == 1
    assert _clamp_timeout(10_000) == SANDBOX_COMMAND_TIMEOUT_MAX
    assert _clamp_timeout(42.7) == 42


async def test_the_timeout_is_forwarded_to_the_service(sandbox):
    """The service enforces the budget, so it has to receive it."""
    captured: dict[str, Any] = {}
    original = _client(sandbox).invoke_agent_runtime_command

    def spy(**kwargs: Any) -> Any:
        captured.update(kwargs["body"])
        return original(**kwargs)

    _client(sandbox).invoke_agent_runtime_command = spy  # type: ignore[method-assign]
    await sandbox.execute("true", timeout=120)
    assert captured["timeout"] == 120


async def test_a_service_timeout_keeps_the_output_and_fails_the_exit_code(sandbox):
    """``TIMED_OUT`` is surfaced as a result: partial output is the most useful thing to report.

    And the exit code must be non-zero even though the fake reports 0 — a timed-out command that
    looks like a success is the dangerous outcome (the model would trust an incomplete test run).
    """
    _client(sandbox).force_status = "TIMED_OUT"
    result = await sandbox.execute("echo partial", timeout=5)
    assert "partial" in result.stdout
    assert result.exit_code == 124
    assert "timed out after 5s" in result.stderr


async def test_a_stream_with_no_completion_event_is_reported_not_assumed_ok(sandbox):
    """No ``contentStop`` ⇒ unknown outcome. Never silently exit 0."""
    _client(sandbox).drop_content_stop = True
    with pytest.raises(SandboxExecutionUncertainError, match="without a completion event"):
        await sandbox.execute("echo hi")
    assert not sandbox.ready


async def test_a_stuck_stream_raises_a_message_carrying_timeout(monkeypatch, tmp_path):
    """The transport guard must raise ``SandboxTimeoutError`` **with text**.

    Both halves are lessons from production: the SDK ``shell`` tool re-raises ``SandboxTimeoutError``
    untouched but collapses anything else into ``RuntimeError(str(e))``, and ``asyncio.wait_for``'s
    bare ``TimeoutError`` stringifies to "" — which is how the literal payload ``Error: RuntimeError -``
    reached the model with nothing to act on.
    """
    import strandly.sandbox.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "SANDBOX_TRANSPORT_GRACE_SECONDS", 0.05)
    # Only the user's command hangs — the bootstrap/prepare commands run normally, otherwise
    # ``_ensure_ready`` blocks on the stuck stream before the guarded call even starts. The hang is
    # an Event (not a fixed sleep) released in ``finally``, so the abandoned pump thread exits at
    # once: a time.sleep(30) here used to cost ~60 s of suite time (call + thread join).
    release = threading.Event()

    class NeverFinishes(FakeAgentCoreClient):
        def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
            if "sleep 30" not in kwargs["body"]["command"]:
                return super().invoke_agent_runtime_command(**kwargs)

            def forever():
                release.wait(30)
                yield {}

            return {"stream": forever()}

    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_id="a" * 40,
        workdir=str(tmp_path),
        client=NeverFinishes(workdir=str(tmp_path)),
    )
    try:
        with pytest.raises(SandboxCommandTimeoutError) as excinfo:
            await sandbox.execute("sleep 30", timeout=1)
    finally:
        release.set()
    message = str(excinfo.value)
    assert message, "a timeout with an empty message is unactionable — the prod bug"
    assert RUNTIME_ARN in message
    assert "may still be running or may have completed" in message
    assert "before running it again" in message
    assert "retry" not in message.lower() and "larger timeout" not in message


async def test_a_cancelled_turn_stops_waiting_on_its_command(tmp_path):
    """A set cancel signal ends the wait with the partial output and a cancelled result."""
    release = threading.Event()

    class Slow(FakeAgentCoreClient):
        def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
            if "sleep 30" not in kwargs["body"]["command"]:
                return super().invoke_agent_runtime_command(**kwargs)

            def slow():
                yield {"chunk": {"contentStart": {}}}
                yield {"chunk": {"contentDelta": {"stdout": "step 1\n"}}}
                release.wait(30)
                yield {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}

            return {"stream": slow()}

    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_id="a" * 40,
        workdir=str(tmp_path),
        client=Slow(workdir=str(tmp_path)),
    )
    signal = threading.Event()
    sandbox.bind_cancel_signal(signal)
    result = None
    try:
        async for item in sandbox.execute_streaming("sleep 30", timeout=60):
            if isinstance(item, StreamChunk):
                signal.set()  # cancelled once the command is visibly running
                cancelled_at = time.monotonic()
            else:
                result = item
    finally:
        release.set()
    assert time.monotonic() - cancelled_at < 5
    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 130
    assert result.stdout == "step 1\n"
    assert result.stderr.endswith(runtime_module.CANCELLED_NOTE)

    # Unbound, the same sandbox runs commands normally again.
    sandbox.bind_cancel_signal(None)
    assert (await sandbox.execute("echo ok")).stdout.strip() == "ok"


# ---- session lifecycle -------------------------------------------------------------------------


async def test_the_session_is_woken_and_bootstrapped_once(sandbox):
    """``InvokeAgentRuntimeCommand`` needs a live session, so exactly one wake precedes the work."""
    await sandbox.execute("true")
    await sandbox.execute("true")
    assert len(_client(sandbox).wakes) == 1
    bootstraps = [c for c in _client(sandbox).commands if "credential.helper store" in c or "user.email" in c]
    assert len(bootstraps) == 1


async def test_concurrent_first_commands_wake_the_session_once(sandbox):
    """N concurrent tool calls (or subagents) must not each provision the session."""
    await asyncio.gather(*(sandbox.execute("true") for _ in range(5)))
    assert len(_client(sandbox).wakes) == 1


async def test_commands_are_not_serialized(sandbox):
    """No global lock: concurrent commands really do overlap.

    The Code Interpreter forced one ``asyncio.Lock`` around every invoke (a sync, session-stateful
    client), which is what made concurrent subagents fight over the sandbox. The command API
    is documented as non-blocking and concurrent-safe on one session, and this asserts we take the
    win rather than re-serializing out of habit.
    """
    await sandbox.execute("true")  # get the one-time wake/bootstrap out of the way
    _client(sandbox).concurrent_peak = 0
    await asyncio.gather(*(sandbox.execute("sleep 0.2") for _ in range(4)))
    assert _client(sandbox).concurrent_peak > 1, "commands were serialized"


async def test_warm_up_provisions_in_the_background(sandbox):
    """Warm-up overlaps provisioning with the agent's first model call; the first command reuses it."""
    sandbox.warm_up()
    await asyncio.sleep(0)
    await sandbox.execute("true")
    assert len(_client(sandbox).wakes) == 1


async def test_warm_up_does_not_deadlock_on_itself(sandbox):
    """Regression: the warm-up task must not await the warm-up task.

    ``_ensure_ready`` awaits ``_warmup_task``; if warm-up went through it the task would await itself
    and the sandbox would hang forever on its first command.
    """
    sandbox.warm_up()
    await asyncio.wait_for(sandbox.execute("true"), timeout=5)


async def test_close_does_not_stop_the_session(sandbox):
    """Persistence is the feature: ``close`` must not throw the workspace away.

    Stopping here would delete the clone/venv/build cache the next turn on the same issue expects,
    which is the entire reason for choosing a runtime over the Code Interpreter.
    """
    await sandbox.execute("true")
    await sandbox.close()
    assert _client(sandbox).stops == []


async def test_stop_session_is_explicit_and_best_effort(sandbox):
    """An explicit stop works, and a failure to clean up never raises into a turn."""
    await sandbox.execute("true")
    await sandbox.stop_session()
    assert _client(sandbox).stops == [sandbox.session_id]

    def boom(**kwargs: Any) -> Any:
        raise FakeClientError("InternalServerException")

    _client(sandbox).stop_runtime_session = boom  # type: ignore[method-assign]
    await sandbox.stop_session()  # must not raise


# ---- error classification + recovery ----------------------------------------------------------


async def test_an_expired_session_is_woken_and_the_command_retried(tmp_path):
    """The ordinary cost of persistence: idle sessions are reaped, and the next command revives one."""
    client = FakeAgentCoreClient(workdir=str(tmp_path))
    original = client.invoke_agent_runtime_command
    expired = {"pending": True}

    def gone_once(**kwargs: Any) -> Any:
        # Only the REAL command expires — not the one-off git bootstrap, which runs first and would
        # otherwise absorb the injected failure (and cannot recover: it is called with
        # allow_wake=False so a bootstrap can never recurse into another wake).
        if expired["pending"] and "echo revived" in kwargs["body"]["command"]:
            expired["pending"] = False
            raise FakeClientError("ResourceNotFoundException", "session not found")
        return original(**kwargs)

    client.invoke_agent_runtime_command = gone_once  # type: ignore[method-assign]
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_id="b" * 40, workdir=str(tmp_path), client=client
    )
    result = await sandbox.execute("echo revived")
    assert "revived" in result.stdout
    assert len(client.wakes) == 2, "the expired session was not re-provisioned"


async def test_a_retryable_conflict_is_retried_with_backoff(tmp_path, monkeypatch):
    """``RetryableConflictException`` (409, session provisioning/tearing down) is transient."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client = FakeAgentCoreClient(
        workdir=str(tmp_path),
        fail_with=[
            FakeClientError("RetryableConflictException", "Session operation in progress"),
            FakeClientError("ThrottlingException", "slow down"),
        ],
    )
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_id="c" * 40, workdir=str(tmp_path), client=client
    )
    result = await sandbox.execute("echo eventually")
    assert "eventually" in result.stdout


async def test_a_non_transient_error_is_raised_immediately(tmp_path):
    """An access-denied is a misconfiguration, not a blip: fail fast instead of burning retries."""
    client = FakeAgentCoreClient(
        workdir=str(tmp_path), fail_with=[FakeClientError("AccessDeniedException", "nope")] * 10
    )
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_id="d" * 40, workdir=str(tmp_path), client=client
    )
    with pytest.raises(FakeClientError, match="AccessDeniedException"):
        await sandbox.execute("true")


async def test_a_service_exception_on_the_stream_raises_with_context(sandbox):
    """A failure delivered as *data* must never read back as a command with no output."""
    _client(sandbox).inject_events = [{"validationException": {"message": "bad command"}}]
    with pytest.raises(SandboxExecutionUncertainError, match="ValidationException") as excinfo:
        await sandbox.execute("true")
    assert RUNTIME_ARN in str(excinfo.value)


async def test_an_empty_exception_member_still_raises(sandbox):
    """Membership, not truthiness: an exception member with an EMPTY payload must still fail.

    The Code Interpreter adapter had exactly this bug — a truthiness check skipped an empty exception
    event, the invoke returned no events, and the caller read it as a 0-byte file or a successful
    command. A failure must never look like a success.
    """
    _client(sandbox).inject_events = [{"internalServerException": {}}]
    with pytest.raises(RuntimeError) as excinfo:
        await sandbox.execute("true")
    assert "InternalServerException" in str(excinfo.value)
    assert RUNTIME_ARN in str(excinfo.value), "an error must name the session it came from"


async def test_a_missing_result_stream_names_the_response_shape(tmp_path):
    """Diagnosable, and without echoing values (a response can carry file contents)."""

    class NoStream(FakeAgentCoreClient):
        def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
            return {"statusCode": 200, "contentType": "application/json"}

    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_id="e" * 40,
        workdir=str(tmp_path),
        client=NoStream(workdir=str(tmp_path)),
    )
    with pytest.raises(SandboxExecutionUncertainError, match="no result stream.*contentType"):
        await sandbox.execute("true")
    assert not sandbox.ready, "an unreadable response to a sent command is not readiness"


async def test_a_mid_stream_failure_is_not_retried(tmp_path):
    """After output, even a "retryable" code is uncertain completion: no replay, output kept."""

    class DiesMidStream(FakeAgentCoreClient):
        def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
            self.commands.append(kwargs["body"]["command"])

            def events():
                yield {"chunk": {"contentDelta": {"stdout": "partial"}}}
                raise FakeClientError("RetryableConflictException", "mid-stream")

            return {"stream": events()}

    client = DiesMidStream(workdir=str(tmp_path))
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_id="f" * 40, workdir=str(tmp_path), client=client
    )
    sandbox._ready = True  # the user command, not the bootstrap, is what dies mid-stream
    with pytest.raises(SandboxExecutionUncertainError) as info:
        await sandbox.execute("echo partial")
    assert info.value.stdout == "partial"
    echoes = [c for c in client.commands if "echo partial" in c]
    assert len(echoes) == 1, "a mid-stream failure was retried and would duplicate output"


# ---- the sandbox's git credential --------------------------------------------------------------
#
# One rule, and it is the reason this section is long: AgentCore Runtime writes **every command it
# executes** to the sandbox's CloudWatch log group ("AgentCore Runtime sends the request ID and the
# input command to your agent's CloudWatch Logs log group"). So a credential in a command is a
# credential in plaintext logs, for every session, forever. It therefore travels in the wake *payload*
# — which the service does not log — and the sandbox entrypoint writes the file itself.


def test_the_bootstrap_script_cannot_carry_a_secret():
    """Enforced by the signature, not by inspecting the output: it takes a bool, never a token.

    A test that only checked "the token isn't in the string" would keep passing the day someone adds
    a token parameter back for something else. The type is the guarantee.
    """
    import inspect

    parameters = inspect.signature(git_bootstrap_script).parameters
    assert set(parameters) == {"workdir", "expect_credential", "author_name", "author_email"}
    assert parameters["expect_credential"].annotation == "bool"


def test_git_bootstrap_always_sets_an_identity():
    """Without a commit identity, ``git commit`` fails outright on a bare image."""
    script = git_bootstrap_script(workdir=SANDBOX_MOUNT_PATH, expect_credential=False)
    assert "user.name" in script and "user.email" in script
    assert "credential.helper" not in script, "no credential expected ⇒ nothing to configure"


def test_git_bootstrap_configures_the_credential_store_when_one_is_expected():
    """Credential store, not ``url.insteadOf``: a URL rewrite leaks the token into every error."""
    script = git_bootstrap_script(workdir=SANDBOX_MOUNT_PATH, expect_credential=True)
    assert "credential.helper store" in script
    assert "insteadOf" not in script
    # And it probes for the file the entrypoint wrote — the one failure the wake response can't see.
    assert ".git-credentials" in script


async def test_the_credential_travels_in_the_wake_payload_and_never_in_a_command(sandbox):
    """SECURITY: the whole point. The token must appear in the payload and in no command at all."""
    sandbox._github_token = "ghs_supersecret"
    await sandbox.execute("echo hi")
    client = _client(sandbox)
    assert client.wake_payloads, "the session was never woken"
    assert client.wake_payloads[0]["gitCredential"] == "ghs_supersecret"
    for command in client.commands:
        assert "ghs_supersecret" not in command, (
            "the credential reached a command body — the service logs those to CloudWatch"
        )


async def test_no_token_means_no_credential_key_at_all(sandbox):
    """An unconfigured sandbox must not send an empty/None credential for the sandbox to write."""
    await sandbox.execute("echo hi")
    assert "gitCredential" not in _client(sandbox).wake_payloads[0]


async def test_a_credential_the_sandbox_never_confirms_warns_and_does_not_fall_back(sandbox, caplog):
    """An old image ignores the payload. Warn — and do **not** retry through a command.

    Falling back to the previous shell-command write would "fix" pushes by putting the token in
    CloudWatch, i.e. by re-introducing the exact defect this change removes. Failing loudly is the
    correct trade.
    """
    sandbox._github_token = "ghs_supersecret"
    _client(sandbox).confirm_credential = False
    with caplog.at_level(logging.WARNING, logger="strandly.sandbox.runtime"):
        await sandbox.execute("echo hi")
    assert "did not confirm writing the git credential" in caplog.text
    assert "ghs_supersecret" not in caplog.text
    assert all("ghs_supersecret" not in command for command in _client(sandbox).commands)


async def test_the_wake_response_surfaces_a_broken_image(sandbox, caplog):
    """The two things no local test can check: what is on the image, and whether the mount is writable.

    Both are reported by the entrypoint's status and only visible if someone reads it — which is why
    ``_note_wake_response`` exists rather than the wake merely draining the body.
    """
    _client(sandbox).wake_status = {"missingTools": ["git", "rg"], "workspaceReady": False}
    with caplog.at_level(logging.WARNING, logger="strandly.sandbox.runtime"):
        await sandbox.execute("echo hi")
    assert "missing expected tools" in caplog.text and "git" in caplog.text
    assert "is not writable" in caplog.text


async def test_a_credential_written_to_a_different_home_is_reported(sandbox, caplog, tmp_path, monkeypatch):
    """If the entrypoint's ``$HOME`` and the command shell's differ, pushes fail and nothing says why.

    Real possibility, not a hypothetical: the credential is written by the *server* process and read by
    a *command* shell. The service documents them as the same container and environment, and the
    bootstrap probes for the file anyway — because "documented" and "true on the day" are different
    claims, and the failure it prevents (every push failing with an opaque 403 while git is otherwise
    perfectly configured) is expensive to diagnose from the other end.

    Here the sandbox confirms the write and the shell's HOME is empty, which is exactly that skew.
    """
    monkeypatch.setenv("HOME", str(tmp_path))  # the fake client inherits this for its shell
    sandbox._github_token = "ghs_supersecret"
    assert _client(sandbox).confirm_credential  # the sandbox says it wrote the file...
    with caplog.at_level(logging.WARNING, logger="strandly.sandbox.runtime"):
        await sandbox.execute("echo hi")
    assert "not visible to the command shell" in caplog.text


async def test_a_credential_the_shell_can_see_is_not_reported(sandbox, caplog, tmp_path, monkeypatch):
    """The other half: the probe must stay quiet when the credential is where git will look.

    Without this, the warning above could pass while firing unconditionally — the useless kind of
    alarm that gets muted and then misses the real event.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".git-credentials").write_text("https://x-access-token:ghs_supersecret@github.com\n")
    sandbox._github_token = "ghs_supersecret"
    with caplog.at_level(logging.WARNING, logger="strandly.sandbox.runtime"):
        await sandbox.execute("echo hi")
    assert "not visible to the command shell" not in caplog.text


async def test_the_bootstrap_command_is_never_logged(sandbox, caplog):
    """Belt and braces: no credential in our own logs either, at any level."""
    sandbox._github_token = "ghs_supersecret"
    with caplog.at_level(logging.DEBUG, logger="strandly.sandbox.runtime"):
        await sandbox.execute("true")
    assert "ghs_supersecret" not in caplog.text


async def test_non_platform_bootstrap_setup_failure_is_best_effort(tmp_path, caplog):
    """Fail-open only for a proven refusal: it costs the agent ``git push``, not the whole turn."""
    import logging

    client = FakeAgentCoreClient(workdir=str(tmp_path))
    original = client.invoke_agent_runtime_command

    def fail_bootstrap(**kwargs: Any) -> Any:
        if "user.email" in kwargs["body"]["command"]:
            raise FakeClientError("AccessDeniedException", "not allowed")
        return original(**kwargs)

    client.invoke_agent_runtime_command = fail_bootstrap  # type: ignore[method-assign]
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_id="g" * 40,
        workdir=str(tmp_path),
        github_token="ghs_x",
        client=client,
    )
    with caplog.at_level(logging.WARNING, logger="strandly.sandbox.runtime"):
        result = await sandbox.execute("echo fine")
    assert "fine" in result.stdout
    assert "git bootstrap failed" in caplog.text
    assert "ghs_x" not in caplog.text


# ---- vended tools -------------------------------------------------------------------------------


def test_the_sandbox_vends_no_duplicate_tools(sandbox):
    """strands registers every sandbox-vended tool on ANY Agent built with this
    sandbox (the goal critic is one built outside ``create_harness``), duplicating the agent's own
    exec and file tools. The sandbox vends nothing; the agent's tools route through it."""
    assert sandbox.get_tools() == []


def test_a_plain_agent_on_this_sandbox_gets_no_sandbox_tools(sandbox):
    """End to end through strands' own registration, which is where the duplicates came from."""
    from strands import Agent

    agent = Agent(sandbox=sandbox, callback_handler=None)
    names = set(agent.tool_registry.registry)
    assert not {"sandbox_bash", "sandbox_file_editor"} & names


def test_the_default_workdir_is_the_persistent_mount():
    """The default cwd must be the session-storage mount, or work would land on ephemeral disk."""
    assert RuntimeSandbox(runtime_arn=RUNTIME_ARN).workdir == SANDBOX_MOUNT_PATH


# ---- one sandbox, several event loops -----------------------------------------------------------
#
# `subagent` hands a child the PARENT's sandbox object, and a subagent can run on a different event
# loop (`strands._async.run_async` submits `asyncio.run` to a thread pool). So every synchronisation
# primitive on this object is shared across loops, which is the shape that took production down at
# v30–v32: an `asyncio.Lock` binds to the first loop that *waits* on it and hangs every other one
# forever. These tests exist because "it works today" was true of that code too — it worked only
# because of the order `build_agent` happened to call things in, and nothing pinned that order.


def _in_new_loop(coro_factory, results: dict, key: str) -> threading.Thread:
    """Run a coroutine on its own brand-new event loop, in its own thread."""

    def run() -> None:
        try:
            results[key] = asyncio.run(coro_factory())
        except BaseException as e:  # noqa: BLE001 — the failure IS the result here
            results[key] = e

    return threading.Thread(target=run, daemon=True)


def test_two_event_loops_can_share_one_sandbox(tmp_path):
    """The regression test for a permanent hang, and the reason readiness is a threading lock.

    Two first commands, two different loops, one shared sandbox — the exact configuration a concurrent
    ``subagent`` produces. With an ``asyncio.Lock`` guarding readiness the second loop waits on a lock
    owned by the first and never returns (observed hanging indefinitely); with a threading lock held
    inside a worker thread it waits out one wake and proceeds.

    The wake is slowed deliberately so the two genuinely overlap; a fast wake would let the first
    finish before the second starts and the test would pass while asserting nothing.
    """
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FakeAgentCoreClient(workdir=str(workdir))
    client.wake_delay = 0.25
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_key="shared", workdir=str(workdir), client=client
    )

    results: dict[str, Any] = {}
    threads = [
        _in_new_loop(lambda: sandbox.execute("echo first"), results, "first"),
        _in_new_loop(lambda: sandbox.execute("echo second"), results, "second"),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a sandbox shared between two event loops hung"

    for key in ("first", "second"):
        outcome = results[key]
        assert not isinstance(outcome, BaseException), f"{key} failed: {outcome!r}"
        assert key in outcome.stdout
    assert len(client.wakes) == 1, "the session must still be woken exactly once between them"


def test_a_warm_up_on_one_loop_does_not_block_a_command_on_another(tmp_path):
    """Warm-up creates an ``asyncio.Task``, which is as loop-bound as a lock.

    The task belongs to the loop that created it, so a command arriving on a *different* loop must not
    await it — it must fall through to the (loop-agnostic) readiness path instead. Otherwise the
    optimisation that makes the first command fast would be the thing that hangs the second agent.
    """
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FakeAgentCoreClient(workdir=str(workdir))
    client.wake_delay = 0.1
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, session_key="shared", workdir=str(workdir), client=client
    )

    async def warm_then_hold() -> str:
        sandbox.warm_up()  # schedules a task on THIS loop
        await asyncio.sleep(0.5)  # ...and keeps that loop alive while the other one works
        return "warmed"

    results: dict[str, Any] = {}
    warm = _in_new_loop(warm_then_hold, results, "warm")
    warm.start()
    time.sleep(0.05)  # let the warm-up be scheduled first — that is the interesting ordering
    command = _in_new_loop(lambda: sandbox.execute("echo foreign"), results, "command")
    command.start()

    for thread in (warm, command):
        thread.join(timeout=30)
        assert not thread.is_alive(), "a foreign loop awaited another loop's warm-up task"
    assert not isinstance(results["command"], BaseException), results["command"]
    assert "foreign" in results["command"].stdout


def test_close_from_a_foreign_loop_does_not_raise(tmp_path):
    """``close()`` awaits the warm-up task; from another loop that raises (or waits forever).

    Nothing in ``src/`` calls ``close()`` today, which is exactly why it is worth pinning — the next
    caller should not have to discover this.
    """
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_key="shared",
        workdir=str(workdir),
        client=FakeAgentCoreClient(workdir=str(workdir)),
    )

    async def warm() -> str:
        sandbox.warm_up()
        await asyncio.sleep(0.2)
        return "warmed"

    results: dict[str, Any] = {}
    thread = _in_new_loop(warm, results, "warm")
    thread.start()
    time.sleep(0.05)
    asyncio.run(sandbox.close())  # a different loop entirely
    thread.join(timeout=30)
    assert not isinstance(results["warm"], BaseException), results["warm"]


# ---- write_file failure reporting ---------------------------------------------------------------


async def test_a_failed_cleanup_cannot_mask_the_real_write_error(sandbox, caplog, monkeypatch):
    """An exception in a ``finally`` REPLACES the one being propagated. That is how item 5 came back.

    The write here fails for a real, nameable reason (an unwritable destination). If the scratch
    cleanup then raises, an unguarded ``finally`` would discard that ``OSError`` and propagate the
    cleanup's exception instead — and a cleanup exception with an empty ``str()`` is rendered by
    upstream ``bash.py``'s ``raise RuntimeError(str(e))`` as literally ``Error: RuntimeError - ``,
    the exact unactionable payload production produced nine times.

    So: the write's own error must survive, and the cleanup failure must be logged, not raised.
    """
    real_execute = sandbox.execute

    async def execute(command, **kwargs):
        if command.startswith("rm -f "):
            raise RuntimeError("")  # empty str() on purpose — the item-5 shape
        return await real_execute(command, **kwargs)

    monkeypatch.setattr(sandbox, "execute", execute)

    with caplog.at_level(logging.WARNING, logger="strandly.sandbox.runtime"):
        with pytest.raises(OSError) as excinfo:
            await sandbox.write_file("/proc/definitely/not/writable/file.txt", b"data")

    message = str(excinfo.value)
    assert message.strip(), "the surviving error must carry text"
    assert "file.txt" in message, "and name the file it failed to write"
    assert "could not remove sandbox write scratch files" in caplog.text


async def test_a_successful_write_still_cleans_up(sandbox, tmp_path):
    """The guard must not turn cleanup into a no-op: no scratch left beside a written file."""
    target = tmp_path / "workspace" / "written.txt"
    await sandbox.write_file(str(target), b"content")
    assert target.read_bytes() == b"content"
    leftovers = [p.name for p in target.parent.iterdir() if ".strandly-" in p.name]
    assert leftovers == [], f"scratch files left behind: {leftovers}"


def test_the_client_read_timeout_outlasts_the_longest_command_budget():
    """Command output streams, so a quiet command is a quiet socket: botocore's 60s default read
    timeout killed every silent command at ~60s ("Read timed out") whatever its ``timeout`` was."""
    from strandly.sandbox.runtime import sandbox_client_config

    config = sandbox_client_config()
    assert config.read_timeout > SANDBOX_COMMAND_TIMEOUT_MAX
    assert config.connect_timeout <= 60


def test_the_lazily_built_client_uses_the_sandbox_config(monkeypatch):
    """The real client (not a test double) must be built with the long read timeout."""
    import boto3

    seen: dict[str, Any] = {}

    class _Session:
        def __init__(self, **kwargs: Any) -> None:
            seen["session"] = kwargs

        def client(self, service: str, **kwargs: Any) -> object:
            seen["service"] = service
            seen["config"] = kwargs.get("config")
            return object()

    monkeypatch.setattr(boto3, "Session", _Session)
    sandbox = RuntimeSandbox(runtime_arn=RUNTIME_ARN, session_key="k", region="us-west-2")
    sandbox._ensure_client()
    assert seen["service"] == "bedrock-agentcore"
    assert seen["config"] is not None
    assert seen["config"].read_timeout > SANDBOX_COMMAND_TIMEOUT_MAX


def test_the_bootstrap_quotes_the_workdir_in_its_error_message(tmp_path):
    """Review: R1 minor. The workdir is a constant in production; a hostile one still must not
    be expanded inside the double-quoted ``echo``."""
    workdir = tmp_path / "ro $(echo INJECTED) `echo TICKS`"
    workdir.mkdir()
    workdir.chmod(0o500)
    try:
        done = subprocess.run(
            ["bash", "-c", git_bootstrap_script(workdir=str(workdir), expect_credential=False)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        workdir.chmod(0o700)
    assert done.returncode == 1
    assert f"workspace {workdir} is not writable" in done.stderr
    assert "INJECTED" in done.stderr and "$(echo INJECTED)" in done.stderr, "printed, not expanded"


def test_the_wake_has_its_own_client_with_a_long_read_timeout_and_one_send(monkeypatch):
    """An Instances wake blocks 53–83 s: botocore's 60 s default read timeout cut it off and its
    default retries re-sent it (a duplicate wake of a still-booting session)."""
    import boto3

    from strandly.sandbox.runtime import sandbox_wake_client_config

    built: list[tuple[str, Any]] = []

    class _Session:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def client(self, service: str, **kwargs: Any) -> object:
            built.append((service, kwargs.get("config")))
            return object()

    monkeypatch.setattr(boto3, "Session", _Session)
    sandbox = RuntimeSandbox(runtime_arn=RUNTIME_ARN, session_key="k", region="us-west-2")
    wake_client = sandbox._ensure_wake_client()
    assert sandbox._ensure_wake_client() is wake_client  # built once
    assert wake_client is not sandbox._ensure_client()  # commands keep their own client
    (service, config), (_, command_config) = built
    assert service == "bedrock-agentcore"
    assert config.read_timeout == 300 and config.connect_timeout == 10
    # `total_max_attempts` (NOT `max_attempts`, which botocore 1.43 counts as retries).
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert command_config.read_timeout > SANDBOX_COMMAND_TIMEOUT_MAX
    assert sandbox_wake_client_config().retries["total_max_attempts"] == 1


def test_the_wake_client_really_sends_once(monkeypatch):
    """Against botocore itself: a wake answered 503 is sent exactly once (the adapter retries)."""
    import boto3
    from botocore.exceptions import ClientError

    from strandly.sandbox.runtime import sandbox_wake_client_config

    client = boto3.Session(
        region_name="us-west-2", aws_access_key_id="x", aws_secret_access_key="y"
    ).client("bedrock-agentcore", config=sandbox_wake_client_config())
    sends: list[int] = []

    def _answer_503(request: Any, **_kwargs: Any) -> Any:
        from botocore.awsrequest import AWSResponse

        sends.append(1)

        class _Raw:
            def stream(self, **_k: Any):
                yield b'{"message":"busy"}'

        headers = {"x-amzn-errortype": "ServiceUnavailableException"}
        return AWSResponse(request.url, 503, headers, _Raw())

    client.meta.events.register("before-send.bedrock-agentcore.InvokeAgentRuntime", _answer_503)
    with pytest.raises(ClientError):
        client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, runtimeSessionId="s" * 40, payload=b"{}"
        )
    assert sends == [1]


# ---- a session that comes back fresh (out-of-band stop/resume) gets its setup back --------------


def _lose_home() -> None:
    """What a stop/resume does to the container: home (git config, the setup sentinel) is gone."""
    for name in (".gitconfig", ".git-credentials", ".strandly-sandbox-setup"):
        pathlib.Path(os.environ["HOME"], name).unlink(missing_ok=True)


async def test_a_session_resumed_out_of_band_has_its_git_setup_redone_before_the_command(sandbox):
    client = sandbox._client
    counter = os.path.join(sandbox.workdir, "runs")
    body = f"echo run >> {counter}; git config --global user.name"
    first = await sandbox.execute(body)
    assert first.stdout.strip() == sandbox.git_author_name
    wakes = len(client.wakes)

    _lose_home()
    again = await sandbox.execute(body)

    assert again.exit_code == 0
    assert again.stdout.strip() == sandbox.git_author_name
    assert sandbox._fresh_marker not in again.stderr
    assert len(client.wakes) == wakes + 1  # the wake re-delivers the git credential too
    assert open(counter).read() == "run\nrun\n"  # the refused attempt never ran the command
    notices, _ = sandbox.notices_since(0)
    assert any("restarted outside this session" in n for n in notices)
    assert (await sandbox.execute(body)).stdout.strip() == sandbox.git_author_name
    assert len(client.wakes) == wakes + 1


async def test_a_command_printing_the_guard_text_is_not_mistaken_for_a_fresh_session(sandbox):
    await sandbox.execute("true")
    wakes = len(sandbox._client.wakes)
    marker = sandbox._fresh_marker
    done = await sandbox.execute(f"echo {marker} >&2; echo out; exit {runtime_module._FRESH_SESSION_EXIT}")
    assert (done.exit_code, done.stdout) == (runtime_module._FRESH_SESSION_EXIT, "out\n")
    assert len(sandbox._client.wakes) == wakes


async def test_a_command_forging_the_guard_refusal_is_not_sent_again(sandbox):
    """P3-A: a body that prints the guard's nonce and exits 97 while the setup is still in place is
    uncertain (it ran), never re-sent and never reported as "did NOT run"."""
    from strandly.sandbox.runtime import SandboxExecutionUncertainError

    await sandbox.execute("true")
    wakes = len(sandbox._client.wakes)
    counter = os.path.join(sandbox.workdir, "runs")
    forged = (
        f"echo run >> {counter}; echo {sandbox._fresh_marker} >&2; "
        f"exit {runtime_module._FRESH_SESSION_EXIT}"
    )
    with pytest.raises(SandboxExecutionUncertainError, match="may have run") as caught:
        await sandbox.execute(forged)
    assert "did NOT run" not in str(caught.value)
    assert open(counter).read() == "run\n"  # sent exactly once
    assert len(sandbox._client.wakes) == wakes + 1  # the one redo that found the setup intact
    # A genuine loss afterwards is still recovered and re-sent.
    _lose_home()
    assert (await sandbox.execute(f"echo run >> {counter}")).exit_code == 0
    assert open(counter).read() == "run\nrun\n"


async def test_setup_that_cannot_be_recorded_stops_the_guard_instead_of_refusing_every_command(sandbox):
    await sandbox.execute("true")
    _lose_home()
    home = pathlib.Path(os.environ["HOME"])
    home.chmod(0o500)  # the sentinel cannot be written any more
    try:
        with pytest.raises(SandboxUnavailableError, match="did NOT run; run it again"):
            await sandbox.execute("echo once")
        assert (await sandbox.execute("echo once")).stdout == "once\n"
    finally:
        home.chmod(0o700)
