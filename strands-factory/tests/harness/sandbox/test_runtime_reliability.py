"""Reliability behaviour of :class:`~strandly.sandbox.runtime.RuntimeSandbox`.

Each test pins one production failure class (reports: sandbox runtime errors, last 30 days):
transport timeouts surfacing as ``AttributeError``, idle output streams, the workspace filling up
or failing writes, 120 s init-timeout wakes, ``502`` bursts from an unhealthy sandbox, corrupt large
``read``\\ s, and duplicate sandbox tools. The workspace is the session's EBS volume (Runtime
instances), so its usage and size come from ``df``.

Same fake as ``test_runtime_sandbox.py``: commands really run in the host shell, against a temp
dir standing in for the mount. No AWS, no network.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from strands.sandbox.errors import SandboxTimeoutError
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient, FakeClientError

from strandly.core.constants import (
    SANDBOX_CACHE_ENV,
    SANDBOX_CACHE_ROOT,
    SANDBOX_HEARTBEAT_MARKER,
    SANDBOX_MOUNT_PATH,
    SANDBOX_READ_CHUNK_BYTES,
    SANDBOX_STORAGE_ERROR_THRESHOLD,
    SANDBOX_WAKE_MAX_ATTEMPTS,
)
from strandly.sandbox import runtime as runtime_module
from strandly.sandbox import scripts as scripts_module
from strandly.sandbox.runtime import (
    RuntimeSandbox,
    SandboxCommandTimeoutError,
    SandboxExecutionUncertainError,
    SandboxUnavailableError,
    _is_init_timeout,
    _is_unhealthy,
)
from strandly.sandbox.scripts import (
    WorkspaceHealth,
    parse_workspace_health,
    strip_heartbeat,
    with_heartbeat,
    workspace_health_script,
)

INIT_TIMEOUT = (
    "Runtime initialization time exceeded. Please make sure that initialization completes in 120s."
)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """Deterministic retries and disk usage; bootstrap config writes only to a temp HOME."""
    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Tests that model full or unmeasurable volumes prepend their own df executable.
    # The default workspace must not inherit the developer machine's disk pressure.
    bin_dir = tmp_path / "healthy-df-bin"
    bin_dir.mkdir()
    df = bin_dir / "df"
    df.write_text(
        "#!/bin/sh\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
        "test-volume 41943040 1024 41942016 1%% /\\n'\n"
    )
    df.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _make(tmp_path, client: FakeAgentCoreClient | None = None, **kwargs: Any) -> RuntimeSandbox:
    workdir = tmp_path / "workspace"
    workdir.mkdir(exist_ok=True)
    return RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_key="gh-strands-agents-sdk-python-pr-5470",
        workdir=str(workdir),
        client=client or FakeAgentCoreClient(workdir=str(workdir)),
        **kwargs,
    )


@pytest.fixture
def healthy_volume(tmp_path, monkeypatch):
    """Keep healthy-volume tests independent of the host filesystem's utilization."""
    bin_dir = tmp_path / "healthy-volume-bin"
    bin_dir.mkdir()
    df = bin_dir / "df"
    df.write_text(
        "#!/bin/sh\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "test-volume 41943040 1048576 40894464 3%% /workspace\n'\n"
    )
    df.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.fixture
def sandbox(tmp_path) -> RuntimeSandbox:
    return _make(tmp_path)


def _client(sandbox: RuntimeSandbox) -> FakeAgentCoreClient:
    return sandbox._client  # type: ignore[return-value]


class FlakyWakeClient(FakeAgentCoreClient):
    """Fails the first N wakes with the given errors, then wakes normally."""

    def __init__(self, *, workdir: str, wake_errors: list[Exception]) -> None:
        super().__init__(workdir=workdir)
        self.wake_errors = list(wake_errors)
        self.wake_attempts: list[str] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.wake_attempts.append(kwargs["runtimeSessionId"])
        if self.wake_errors:
            raise self.wake_errors.pop(0)
        return super().invoke_agent_runtime(**kwargs)


# ---- 1. transport timeout: a real SandboxTimeoutError the SDK shell tool can read -------------


def test_the_timeout_error_carries_stdout_and_stderr():
    """The SDK ``shell`` tool reads ``e.stdout``; skipping the base __init__ made that AttributeError."""
    error = SandboxCommandTimeoutError(timeout=990, context="[ctx]", stdout="partial", stderr="e")
    assert isinstance(error, SandboxTimeoutError)
    assert (error.stdout, error.stderr) == ("partial", "e")
    assert "990s" in str(error) and "[ctx]" in str(error)
    bare = SandboxCommandTimeoutError(timeout=5, context="[ctx]")
    assert (bare.stdout, bare.stderr) == ("", "")


class StuckStreamClient(FakeAgentCoreClient):
    """Streams one chunk, then never completes, until the test releases it."""

    def __init__(self, *, workdir: str, release: threading.Event) -> None:
        super().__init__(workdir=workdir)
        self.release = release

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        command = kwargs["body"]["command"]
        if "STUCK" not in command:
            return super().invoke_agent_runtime_command(**kwargs)

        def events():
            yield {"chunk": {"contentDelta": {"stdout": "made progress\n"}}}
            self.release.wait(10)

        return {"stream": events()}


async def test_the_sdk_shell_tool_reports_a_transport_timeout_with_its_partial_output(
    tmp_path, monkeypatch
):
    """Drive the real SDK ``shell`` tool through a forced transport timeout (prod: 65 results)."""
    from strands.vended_tools.shell import make_shell

    monkeypatch.setattr(runtime_module, "SANDBOX_TRANSPORT_GRACE_SECONDS", 0.2)
    release = threading.Event()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = _make(tmp_path, StuckStreamClient(workdir=str(workdir), release=release))
    tool = make_shell(sandbox=sandbox)
    try:
        with pytest.raises(SandboxTimeoutError) as excinfo:
            await tool._tool_func(
                command="echo STUCK", tool_context=SimpleNamespace(cancel_signal=None), timeout=1
            )
    finally:
        release.set()
    message = str(excinfo.value)
    assert "AttributeError" not in message
    partial = json.loads(message.rsplit("\n", 1)[1])
    assert partial["output"] == "made progress\n"
    assert partial["exit_code"] == 124


# ---- 2. heartbeat: idle commands keep their stream alive; the marker never reaches the caller --


def _bash(script: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=timeout, check=False
    )


def test_the_heartbeat_writes_markers_to_stderr_while_a_command_is_silent():
    done = _bash(with_heartbeat("sleep 2.5; echo done", 1))
    assert done.stdout == "done\n"
    assert done.stderr.count(SANDBOX_HEARTBEAT_MARKER) >= 1
    assert strip_heartbeat(done.stderr) == ""


@pytest.mark.parametrize(
    ("body", "exit_code", "stdout"),
    [
        ("exit 7", 7, ""),
        ("false", 1, ""),
        ("cat <<'EOF'\nheredoc\nEOF", 0, "heredoc\n"),
        ("sleep 0.2 & wait; echo waited", 0, "waited\n"),  # a bare `wait` must not wait on it
        ("trap 'echo mine' EXIT; echo x", 0, "x\nmine\n"),  # a replaced trap still ends it
        ("exec echo replaced", 0, "replaced\n"),
        ("echo trailing # a comment", 0, "trailing\n"),
        ("set -e; false; echo never", 1, ""),
    ],
)
def test_the_heartbeat_preserves_command_semantics(body, exit_code, stdout):
    done = _bash(with_heartbeat(body, 30), timeout=5)
    assert done.returncode == exit_code
    assert done.stdout == stdout


def test_the_heartbeat_does_not_hold_the_stream_open_after_the_command():
    """Reading to EOF must return as soon as the command ends — no orphan keeps the pipe."""
    start = time.monotonic()
    _bash(with_heartbeat("echo quick", 30))
    assert time.monotonic() - start < 3


# ---- 2b. the wrapper leaves no process behind: a leftover (even a zombie) keeps a command slot --
#
# AgentCore counts a command as running until every process it started is gone, and a session
# container has 197 slots. The wrapper's text is run for real, in its own process group.

_SHELLS = ["/bin/sh", "bash"]  # the service runs `/bin/sh -c`; `_wrap` adds SANDBOX_SHELL (bash)


def _group(pgid: int) -> list[str]:
    """Command lines of every process (zombies included) still in process group ``pgid``."""
    rows = subprocess.run(
        ["ps", "-A", "-o", "pgid=,args="], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [row.split(None, 1)[1] for row in rows if row.split(None, 1)[0] == str(pgid)]


def _run_wrapped(
    shell: str, body: str, interval: int = 30, kill: int | None = None, keep: bool = False
) -> tuple[Any, ...]:
    """(exit code, stdout, stderr, what is left in its process group) for ``body`` under the wrapper.

    ``kill`` signals the wrapper shell alone once the body is running, as a timeout would. What is
    left is then killed, unless ``keep`` (which also returns the group id).
    """
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        process = subprocess.Popen(
            [shell, "-c", with_heartbeat(body, interval)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
        )
        if kill is not None:
            time.sleep(0.4)
            os.kill(process.pid, kill)
        code = process.wait(timeout=15)
        left = _group(process.pid)
        out.seek(0)
        err.seek(0)
        if keep:
            return code, out.read(), err.read(), left, process.pid
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return code, out.read(), err.read(), left


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    ("body", "exit_code", "stdout", "stderr"),
    [
        ("true", 0, "", ""),
        ("echo out; echo err >&2; exit 7", 7, "out\n", "err\n"),
        ("sleep 0.2 & wait; echo waited", 0, "waited\n", ""),  # a bare `wait` in the body
        ("exec echo replaced", 0, "replaced\n", ""),
        ("trap 'echo mine' EXIT; echo x", 0, "x\nmine\n", ""),
    ],
)
def test_nothing_the_wrapper_started_outlives_it(shell, body, exit_code, stdout, stderr):
    code, out, err, left = _run_wrapped(shell, body)
    assert (code, out, err) == (exit_code, stdout, stderr)
    assert left == []


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_heartbeat_that_has_fired_is_reaped_too(shell):
    code, out, err, left = _run_wrapped(shell, "sleep 1.3; echo done", interval=1)
    assert (code, out) == (0, "done\n")
    assert SANDBOX_HEARTBEAT_MARKER in err and strip_heartbeat(err) == ""
    assert left == []


@pytest.mark.parametrize("shell", _SHELLS)
def test_stopping_the_heartbeat_is_silent_however_early_the_command_ends(shell):
    """The heartbeat can be killed before its own trap is set; the shell must not report that."""
    for _ in range(25):
        assert _run_wrapped(shell, ":") == (0, "", "", [])


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_job_the_command_backgrounds_is_the_only_thing_left(shell):
    """The model's own orphan is not the wrapper's to kill (the image's init reaps it)."""
    start = time.monotonic()
    code, out, _, left = _run_wrapped(shell, "sleep 5 >/dev/null 2>&1 & echo started")
    assert (code, out) == (0, "started\n")
    assert time.monotonic() - start < 3  # neither waited for the job nor for the heartbeat
    assert left == ["sleep 5"]


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_the_heartbeat_ends_when_the_wrapper_is_killed(shell, sig):
    """A timeout or a cancel kills the wrapper, which then cannot stop anything itself."""
    code, _, err, left, pgid = _run_wrapped(shell, "exec sleep 5", kill=sig, keep=True)
    try:
        assert code == -sig and strip_heartbeat(err) == ""
        deadline = time.monotonic() + 3  # the heartbeat notices within a second
        while left != ["sleep 5"] and time.monotonic() < deadline:
            time.sleep(0.05)
            left = _group(pgid)
        assert left == ["sleep 5"]
    finally:
        os.killpg(pgid, signal.SIGKILL)


def _run_as_the_service_does(shell: str, body: str) -> subprocess.Popen[bytes]:
    """``body`` under the wrapper the way the service runs a command: a ``/bin/sh -c`` that leads
    the command's process group and is the wrapper's parent (``exit $?`` keeps it from exec-ing)."""
    wrapper = f"{shell} -c {shlex.quote(with_heartbeat(body, 30))}; exit $?"
    return subprocess.Popen(
        ["/bin/sh", "-c", wrapper], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc, as in the image")
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    "body",
    [
        "sleep 30",
        "sleep 30 & sleep 30",  # a job the body backgrounds goes with it
        "trap '' TERM; sleep 30",  # one that ignores TERM gets KILL
    ],
)
def test_a_command_the_service_abandons_leaves_nothing_behind(shell, body):
    """On a timeout the service kills only its ``/bin/sh``. The wrapper, the body and its children
    used to run on under PID 1, each such command holding a slot (197 of them: every later command
    exit -1). The heartbeat now ends the whole group, TERM then KILL."""
    leader = _run_as_the_service_does(shell, body)
    try:
        deadline = time.monotonic() + 5
        while len(_group(leader.pid)) < 4 and time.monotonic() < deadline:
            time.sleep(0.05)  # sh, wrapper, heartbeat, body
        os.kill(leader.pid, signal.SIGKILL)  # what the service's timeout does
        leader.wait(timeout=5)
        deadline = time.monotonic() + 6  # noticed within a second, KILL 2 seconds after TERM
        left = _group(leader.pid)
        while left and time.monotonic() < deadline:
            time.sleep(0.1)
            left = _group(leader.pid)
        assert left == []
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc, as in the image")
@pytest.mark.parametrize("shell", _SHELLS)
def test_a_command_that_completes_keeps_its_background_job(shell):
    """Only abandonment ends the group: a finished command's own background job is left alone."""
    leader = _run_as_the_service_does(shell, "sleep 6 >/dev/null 2>&1 & echo started")
    try:
        assert leader.wait(timeout=5) == 0
        time.sleep(2.5)  # longer than the heartbeat would take to notice and kill
        assert _group(leader.pid) == ["sleep 6"]
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(leader.pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_CHILD_SUBREAPER is Linux-only")
@pytest.mark.parametrize("shell", _SHELLS)
def test_no_orphan_is_left_for_pid_1_to_reap(shell):
    """As a subreaper this process inherits what PID 1 would: there must be nothing to inherit."""
    program = (
        "import ctypes, os, subprocess, sys, time\n"
        "ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER\n"
        "for _ in range(5):\n"
        "    subprocess.run([sys.argv[1], '-c', sys.argv[2]], check=True)\n"
        "time.sleep(1.5)  # an orphaned `sleep 1` would be a zombie of ours by now\n"
        "try:\n"
        "    print('orphan', os.waitpid(-1, os.WNOHANG))\n"
        "except ChildProcessError:\n"
        "    print('clean')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program, shell, with_heartbeat("echo hi >/dev/null", 30)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert done.stdout == "clean\n"


@pytest.mark.parametrize("shell", _SHELLS)
def test_stopping_the_heartbeat_never_waits_out_its_sleep(shell):
    """A TERM that lands in the heartbeat's freshly forked `sleep` must not cost a second."""
    slowest = 0.0
    for _ in range(60):
        start = time.monotonic()
        done = subprocess.run([shell, "-c", with_heartbeat(":", 30)], capture_output=True, timeout=10, check=False)
        slowest = max(slowest, time.monotonic() - start)
        assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert slowest < 0.95  # the race costs the whole second


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(("body", "exit_code"), [("true", 0), ("exit 3", 3), ("sleep 1.3; echo done", 0)])
def test_a_wrapper_started_with_term_ignored_still_returns(shell, body, exit_code):
    with tempfile.TemporaryFile("w+") as out:
        process = subprocess.Popen(
            [shell, "-c", with_heartbeat(body, 1)], stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.DEVNULL, start_new_session=True,
            preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN),  # noqa: PLW1509
        )
        try:
            assert process.wait(timeout=5) == exit_code
            assert _group(process.pid) == []
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


@pytest.mark.parametrize("shell", _SHELLS)
def test_the_body_does_not_see_the_heartbeat_as_its_last_job(shell):
    code, out, err, left = _run_wrapped(shell, "kill $! 2>/dev/null; sleep 1.3; echo done", interval=1)
    assert (code, out, left) == (0, "done\n", [])
    assert SANDBOX_HEARTBEAT_MARKER in err  # still beating


def test_wait_on_the_bodys_last_job_succeeds_before_it_starts_one():
    assert _run_wrapped("bash", "wait $!; echo rc=$?")[:3] == (0, "rc=0\n", "")


async def test_heartbeat_markers_are_stripped_from_command_output(sandbox):
    result = await sandbox.execute(
        f"printf 'a\\{ord(SANDBOX_HEARTBEAT_MARKER):03o}b' >&2; printf '\\{ord(SANDBOX_HEARTBEAT_MARKER):03o}' >&2"
    )
    assert result.stderr == "ab"


async def test_a_failed_cd_still_stops_the_command(sandbox):
    """The heartbeat's `;` must not break the `cd && …` chain."""
    result = await sandbox.execute("echo SHOULD-NOT-RUN", cwd="/definitely/not/here")
    assert result.exit_code != 0
    assert "SHOULD-NOT-RUN" not in result.stdout


# ---- 3. cache env: caches and build output on the workspace volume ------------------------------


async def test_caches_and_build_output_live_on_the_workspace_volume(sandbox):
    result = await sandbox.execute(
        'printf "%s|%s|%s|%s" "$UV_CACHE_DIR" "$npm_config_cache" "$CARGO_TARGET_DIR" "$PIP_CACHE_DIR"'
    )
    assert result.stdout.split("|") == [
        SANDBOX_CACHE_ENV["UV_CACHE_DIR"],
        SANDBOX_CACHE_ENV["npm_config_cache"],
        SANDBOX_CACHE_ENV["CARGO_TARGET_DIR"],
        SANDBOX_CACHE_ENV["PIP_CACHE_DIR"],
    ]
    # On the persistent volume (warm across invocations), all under the one root the prune empties.
    assert SANDBOX_CACHE_ROOT.startswith(SANDBOX_MOUNT_PATH + "/")
    assert all(
        value.startswith(SANDBOX_CACHE_ROOT)
        for key, value in SANDBOX_CACHE_ENV.items()
        if key != "GOFLAGS"  # the one non-path entry: -modcacherw
    )
    assert not any(value.startswith("/tmp") for value in SANDBOX_CACHE_ENV.values())


async def test_the_caller_can_still_override_a_scratch_path(sandbox):
    result = await sandbox.execute('printf "%s" "$UV_CACHE_DIR"', env={"UV_CACHE_DIR": "/x/uv"})
    assert result.stdout == "/x/uv"


# ---- 4. wake: init timeouts are retried on the same session, never rotated -----------------------


def test_init_timeout_classification():
    assert _is_init_timeout(FakeClientError("RuntimeClientError", INIT_TIMEOUT))
    assert not _is_init_timeout(FakeClientError("RuntimeClientError", "Received error (502)"))


async def test_an_init_timeout_wake_is_retried(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FlakyWakeClient(
        workdir=str(workdir),
        wake_errors=[FakeClientError("RuntimeClientError", INIT_TIMEOUT)] * 2,
    )
    sandbox = _make(tmp_path, client)
    result = await sandbox.execute("echo up")
    assert result.stdout == "up\n"
    assert len(client.wake_attempts) == 3
    assert set(client.wake_attempts) == {sandbox.session_id}, "same session, retried"


async def test_a_non_transient_wake_error_is_not_retried(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FlakyWakeClient(
        workdir=str(workdir), wake_errors=[FakeClientError("AccessDeniedException", "no")]
    )
    sandbox = _make(tmp_path, client)
    with pytest.raises(FakeClientError):
        await sandbox.execute("echo up")
    assert len(client.wake_attempts) == 1


async def test_a_session_that_never_finishes_init_is_not_swapped_for_a_fresh_one(tmp_path):
    """An init timeout is not evidence the workspace is bad: never abandon it for an empty one."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FlakyWakeClient(
        workdir=str(workdir),
        wake_errors=[FakeClientError("RuntimeClientError", INIT_TIMEOUT)] * SANDBOX_WAKE_MAX_ATTEMPTS,
    )
    sandbox = _make(tmp_path, client)
    base = sandbox.session_id
    with pytest.raises(SandboxUnavailableError, match="initialization time exceeded"):
        await sandbox.execute("echo up")
    assert sandbox.session_id == base
    assert set(client.wake_attempts) == {base}


def test_every_invocation_of_a_session_uses_the_same_sandbox_session(tmp_path):
    """There is no per-object rotation state for a later invocation to forget."""
    first = _make(tmp_path)
    second = _make(tmp_path)
    assert first.session_id == second.session_id
    assert not hasattr(first, "_rotate_session")


# ---- 5. unhealthy sandbox: retry, restart once, then fail fast ---------------------------------

BAD_GATEWAY = "Received error (502) from runtime. Please check your CloudWatch logs."


def test_unhealthy_classification():
    assert _is_unhealthy(FakeClientError("RuntimeClientError", BAD_GATEWAY))
    assert _is_unhealthy(FakeClientError("InternalServerException", "x"))
    assert not _is_unhealthy(FakeClientError("RuntimeClientError", "Received error (400) from runtime"))
    assert not _is_unhealthy(FakeClientError("RuntimeClientError", INIT_TIMEOUT))
    assert not _is_unhealthy(FakeClientError("ValidationException", "x"))


async def test_a_single_502_is_not_replayed_or_restarted(sandbox):
    await sandbox.execute("true")
    client = _client(sandbox)
    client.fail_with = [FakeClientError("RuntimeClientError", BAD_GATEWAY)]
    with pytest.raises(SandboxUnavailableError, match="NOT replayed"):
        await sandbox.execute("echo ok")
    assert not client.stops
    assert not sandbox.ready
    assert (await sandbox.execute("echo next-command")).stdout == "next-command\n"


async def test_a_dead_sandbox_fails_fast_after_one_harmless_probe(sandbox):
    await sandbox.execute("true")
    client = _client(sandbox)
    client.fail_with = [FakeClientError("RuntimeClientError", BAD_GATEWAY)] * 50
    for _ in range(4):
        with pytest.raises(SandboxUnavailableError, match="NOT replayed"):
            await sandbox.execute("echo ok")
    assert len(client.fail_with) == 48, "one original send and one health probe, never a replay"
    assert not client.stops


async def test_a_4xx_runtime_client_error_is_not_retried(sandbox):
    await sandbox.execute("true")
    _client(sandbox).fail_with = [FakeClientError("RuntimeClientError", "Received error (400)")]
    with pytest.raises(FakeClientError):
        await sandbox.execute("echo ok")
    assert _client(sandbox).stops == []


async def test_closed_stream_failure_can_recover_by_probe_without_replaying_or_stopping(sandbox):
    await sandbox.execute("true")
    _client(sandbox).drop_content_stop = True
    with pytest.raises(SandboxUnavailableError, match="without a completion event"):
        await sandbox.execute("echo x")
    _client(sandbox).drop_content_stop = False
    result = await sandbox.execute("echo fine")
    assert result.exit_code == 0
    assert not _client(sandbox).stops


async def test_a_throttle_on_the_result_stream_is_uncertain_and_not_replayed(sandbox):
    """The stream exists only because HTTP 200 accepted the command: not a proven rejection."""
    await sandbox.execute("true")
    client = _client(sandbox)
    sends: list[str] = []

    def throttled(**kwargs: Any) -> dict[str, Any]:
        sends.append(kwargs["body"]["command"])
        return {"stream": iter([{"throttlingException": {"message": "slow down"}}])}

    client.invoke_agent_runtime_command = throttled  # type: ignore[method-assign]
    with pytest.raises(SandboxExecutionUncertainError, match="ThrottlingException"):
        await sandbox.execute("echo ok")
    assert len(sends) == 1


# ---- 6. workspace: storage errors, health check, prune, restart, report ------------------------


def _populate(workdir) -> None:
    """A repo checkout with regenerable dirs, a cargo target, and source that must survive."""
    repo = workdir / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # the prune asks git what's tracked
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('keep me')\n")
    (repo / "src" / "target").mkdir()  # a SOURCE dir that happens to be named target
    (repo / "src" / "target" / "keep.py").write_text("x = 1\n")
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("x" * 10_000)
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / "rust" / "target" / "debug").mkdir(parents=True)
    (repo / "rust" / "target" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55")
    (repo / "rust" / "Cargo.toml").write_text("[package]\n")


def test_the_prune_keeps_source_and_git_and_removes_regenerable_dirs(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    _populate(workdir)
    done = _bash(workspace_health_script(str(workdir), force_prune=True), timeout=60)
    repo = workdir / "repo"
    assert not (repo / "node_modules").exists()
    assert not (repo / ".venv").exists()
    assert (repo / "rust" / "target").exists(), "CACHEDIR.TAG is PR-controlled: not a deletion signal"
    assert (repo / "src" / "main.py").exists()
    assert (repo / "src" / "target" / "keep.py").exists(), "a plain `target/` is never pruned"
    assert (repo / ".git" / "HEAD").exists()
    assert (repo / "rust" / "Cargo.toml").exists()
    health = parse_workspace_health(done.stdout)
    assert health.writable is True
    assert health.used_kb is not None


def test_the_health_script_reports_a_failed_write(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    workdir.chmod(0o500)
    try:
        done = _bash(workspace_health_script(str(workdir)), timeout=60)
    finally:
        workdir.chmod(0o700)
    health = parse_workspace_health(done.stdout)
    assert health.writable is False
    assert health.detail
    assert done.returncode == 0, "the health script never fails the bootstrap"


def test_health_parsing():
    health = parse_workspace_health(
        "noise\nstrandly-sandbox: workspace-capacity-kb=41943040\n"
        "strandly-sandbox: workspace-kb=900000\nstrandly-sandbox: pruned-kb=400000\n"
        "strandly-sandbox: workspace-write=failed: No space left on device\n"
    )
    assert (health.used_kb, health.pruned_kb, health.writable) == (900000, 400000, False)
    assert health.capacity_kb == 41943040
    assert health.full
    assert not WorkspaceHealth(writable=False, detail="Input/output error").full
    assert parse_workspace_health("strandly-sandbox: workspace-capacity-kb=0").capacity_kb is None
    assert parse_workspace_health("").writable is None


async def test_a_nearly_full_workspace_is_pruned_at_wake_and_the_model_is_told(tmp_path, monkeypatch):
    monkeypatch.setattr(scripts_module, "SANDBOX_WORKSPACE_PRUNE_FRACTION", 0)  # anything is "full"
    sandbox = _make(tmp_path)
    _populate(tmp_path / "workspace")
    result = await sandbox.execute("ls repo")
    assert "node_modules" not in result.stdout and "src" in result.stdout
    (notice,), _ = sandbox.notices_since(0)
    assert "repo/node_modules" in notice and "repo/.venv" in notice, "says exactly what was deleted"
    assert "[strandly-sandbox]" not in result.stderr, "notices go to SandboxNotices, not stderr"


#: The retired "~1 GiB session storage" wording — as a whole number, so a real size such as
#: "(~71 GiB)" (the host's df, on a CI runner) does not match it.
_OLD_1_GIB = re.compile(r"(?<![\d.])1 GiB")


def _fake_df_path(tmp_path, capacity_kb: int = 41_943_040, used_kb: int = 1_000_000) -> str:
    """A PATH whose ``df`` reports a fixed volume, so no assertion depends on the host's disk."""
    fake_bin = tmp_path / "fake-df-bin"
    fake_bin.mkdir(exist_ok=True)
    df = fake_bin / "df"
    df.write_text(
        "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        f"echo '/dev/nvme1n1 {capacity_kb} {used_kb} 1 3% /mnt/workspace'\n"
    )
    df.chmod(0o755)
    return f"{fake_bin}:{os.environ['PATH']}"


async def test_a_storage_failure_gets_actionable_advice(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _fake_df_path(tmp_path))  # the 40 GiB volume, not the host's disk
    result = await sandbox.execute("echo 'write error: No space left on device' >&2; exit 1")
    assert "df -h" in result.stderr and SANDBOX_CACHE_ROOT in result.stderr
    assert "(~40 GiB)" in result.stderr
    assert _OLD_1_GIB.search(result.stderr) is None and "MB cap" not in result.stderr
    assert sandbox._storage_strikes == 1


def test_the_old_1_gib_matcher_ignores_real_sizes():
    assert _OLD_1_GIB.search("(~71 GiB)") is None and _OLD_1_GIB.search("(~40 GiB)") is None
    assert _OLD_1_GIB.search("capped at ~1 GiB") and _OLD_1_GIB.search("(1 GiB)")


async def test_storage_advice_needs_a_failure_not_just_the_words(sandbox):
    """`grep "No space left" log` succeeding must not count as a storage failure."""
    result = await sandbox.execute("echo 'No space left on device' >&2; true")
    assert "df -h" not in result.stderr
    assert sandbox._storage_strikes == 0


async def test_storage_errors_in_command_output_alone_cannot_force_a_prune(healthy_volume, sandbox, tmp_path):
    """Review: R1 minor. Three failing commands that merely print a storage marker (a test's output)
    trigger a measure-and-probe, not a prune, while the workspace is healthy and small."""
    await sandbox.execute("true")
    _populate(tmp_path / "workspace")
    before = len(_client(sandbox).commands)
    for _ in range(SANDBOX_STORAGE_ERROR_THRESHOLD):
        await sandbox.execute("echo 'Input/output error' >&2; exit 1")
    probes = [c for c in _client(sandbox).commands[before:] if "workspace-write=" in c]
    assert len(probes) == 1, "the strikes ran one health check"
    assert (tmp_path / "workspace" / "repo" / "node_modules").exists()
    assert sandbox._storage_strikes == 0
    assert _client(sandbox).stops == [], "a writable workspace is not restarted"


async def test_repeated_storage_failures_prune_a_nearly_full_workspace(sandbox, tmp_path, monkeypatch):
    await sandbox.execute("true")
    _populate(tmp_path / "workspace")
    monkeypatch.setattr(scripts_module, "SANDBOX_WORKSPACE_PRUNE_FRACTION", 0)  # anything is "full"
    for _ in range(SANDBOX_STORAGE_ERROR_THRESHOLD):
        await sandbox.execute("echo 'No space left on device' >&2; exit 1")
    assert not (tmp_path / "workspace" / "repo" / "node_modules").exists()
    assert (tmp_path / "workspace" / "repo" / "src" / "main.py").exists()


class FullUnlessPrunedClient(FakeAgentCoreClient):
    """The write probe reports ENOSPC until a prune has run — while ``df`` reports a small
    workspace, so the usage check alone never prunes (R1 S3's case)."""

    def __init__(self, *, workdir: str, forever: bool = False) -> None:
        super().__init__(workdir=workdir)
        self.forced_prunes = 0
        self.forever = forever

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        command = kwargs["body"]["command"]
        forced = "if true; then" in command
        self.forced_prunes += forced
        response = super().invoke_agent_runtime_command(**kwargs)
        if "workspace-write=" not in command or (forced and not self.forever):
            return response
        events = []
        for event in response["stream"]:
            delta = event.get("chunk", {}).get("contentDelta", {})
            if delta.get("stdout"):
                delta["stdout"] = delta["stdout"].replace(
                    "workspace-write=ok", "workspace-write=failed: No space left on device"
                )
            events.append(event)
        return {"stream": iter(events)}


async def test_a_full_workspace_is_force_pruned_before_anything_else(healthy_volume, tmp_path):
    """Review: R1 S3."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FullUnlessPrunedClient(workdir=str(workdir))
    sandbox = _make(tmp_path, client)
    _populate(workdir)
    result = await sandbox.execute("echo ok")
    assert result.stdout == "ok\n"
    assert client.forced_prunes == 1
    assert not (workdir / "repo" / "node_modules").exists()
    assert client.stops == []
    assert not any("FULL" in n for n in sandbox.notices_since(0)[0])


def _health_with_fake_df(tmp_path, workdir, df_script: str, **kwargs):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "df").write_text(df_script)
    (fake_bin / "df").chmod(0o755)
    done = subprocess.run(
        ["bash", "-c", workspace_health_script(str(workdir), **kwargs)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert done.returncode == 0
    return parse_workspace_health(done.stdout)


def test_an_unmeasurable_workspace_is_not_pruned_on_a_guess(tmp_path):
    """``df`` failing gives no capacity, so nothing is pruned (a 0 threshold would prune on every
    wake and keep deleting node_modules); the write probe still runs."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    _populate(workdir)
    health = _health_with_fake_df(tmp_path, workdir, "#!/bin/sh\nexit 1\n")
    assert health.used_kb is None and health.capacity_kb is None
    assert health.pruned_kb is None and not health.pruned_paths
    assert (workdir / "repo" / "node_modules").exists()
    assert health.writable is True


@pytest.mark.parametrize(("used_kb", "pruned"), [(34_000_000, False), (36_000_000, True)])
def test_the_prune_threshold_is_85_percent_of_the_real_volume(tmp_path, used_kb, pruned):
    """40 GiB volume (41,943,040 KiB): 85% is 35,651,584 KiB."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    _populate(workdir)
    df = (
        "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        f"echo '/dev/nvme1n1 41943040 {used_kb} 1 99% /mnt/workspace'\n"
    )
    health = _health_with_fake_df(tmp_path, workdir, df, cache_root=str(tmp_path / "cache"))
    assert health.capacity_kb == 41_943_040 and health.used_kb == used_kb
    assert ("repo/node_modules" in health.pruned_paths) is pruned
    assert (workdir / "repo" / "node_modules").exists() is not pruned


def test_the_prune_empties_the_cache_root_without_following_a_symlink(tmp_path):
    """The caches live on the volume; a prune deletes the cache root itself (``rm -rf`` on the path:
    a symlink planted there is removed, its target untouched)."""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    cache = workdir / ".cache"
    (cache / "pip").mkdir(parents=True)
    (cache / "pip" / "wheel").write_text("x" * 1000)
    done = _bash(workspace_health_script(str(workdir), force_prune=True, cache_root=str(cache)))
    assert not cache.exists()
    assert parse_workspace_health(done.stdout).writable is True
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep\n")
    cache.symlink_to(precious, target_is_directory=True)
    _bash(workspace_health_script(str(workdir), force_prune=True, cache_root=str(cache)))
    assert not cache.exists() and not cache.is_symlink()
    assert (precious / "keep.txt").read_text() == "keep\n"


def _go_style_readonly_tree(root) -> None:
    """What ``go mod download`` leaves without -modcacherw: 0444 files in 0555 directories."""
    mod = root / "go-mod" / "github.com" / "x" / "y@v1.0.0"
    mod.mkdir(parents=True)
    (mod / "go.mod").write_text("module y\n")
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        path.chmod(0o444 if path.is_file() else 0o555)
    (root / "go-mod").chmod(0o555)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_the_prune_deletes_a_read_only_go_module_cache(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    cache = workdir / ".cache"
    cache.mkdir()
    _go_style_readonly_tree(cache)
    # A symlink inside the cache to a read-only tree elsewhere: chmod must not follow it.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("keep\n")
    outside.chmod(0o555)
    (cache / "link").symlink_to(outside, target_is_directory=True)
    try:
        done = _bash(workspace_health_script(str(workdir), force_prune=True, cache_root=str(cache)))
        assert parse_workspace_health(done.stdout).writable is True
        assert not cache.exists(), "the read-only Go module cache was deleted whole"
        assert stat.S_IMODE(outside.stat().st_mode) == 0o555, "chmod did not follow the symlink"
        assert (outside / "f").read_text() == "keep\n"
    finally:
        outside.chmod(0o755)


def test_go_keeps_its_module_cache_writable():
    assert "-modcacherw" in SANDBOX_CACHE_ENV["GOFLAGS"].split()
    assert SANDBOX_CACHE_ENV["GOMODCACHE"].startswith(SANDBOX_CACHE_ROOT + "/")


def test_the_cache_is_deleted_first_with_a_time_limit_and_never_walked(tmp_path):
    script = workspace_health_script("/mnt/workspace", force_prune=True)
    # (conftest points the module's cache root at a tmp dir, so a test never touches the real one)
    rm_at = script.index(f"rm -rf -- {scripts_module.SANDBOX_CACHE_ROOT}")
    walk_at = script.index("python3 - /mnt/workspace")
    assert rm_at < walk_at, "the cheap, always-safe space is reclaimed before the slow walk"
    assert re.search(r"__strandly_t \d+ rm -rf -- ", script), "the rm is time-limited"
    assert re.search(r"__strandly_t \d+ find ", script), "so is the chmod pass"
    # The walk skips the cache root even when it is still there (e.g. the rm timed out).
    workdir = tmp_path / "ws"
    (workdir / ".cache" / "pnpm-store" / "node_modules").mkdir(parents=True)
    (workdir / "app" / "node_modules").mkdir(parents=True)
    done = _bash(
        "set +e\n" + scripts_module._TIMEBOX_FUNCTION + "\n"
        + scripts_module.workspace_prune_script(str(workdir), skip=str(workdir / ".cache")),
        timeout=60,
    )
    pruned = parse_workspace_health(done.stdout).pruned_paths
    assert pruned == ["app/node_modules"]
    assert (workdir / ".cache" / "pnpm-store" / "node_modules").is_dir()


def test_an_unforced_healthy_workspace_keeps_its_caches(healthy_volume, tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    cache = workdir / ".cache"
    (cache / "uv").mkdir(parents=True)
    _bash(workspace_health_script(str(workdir), cache_root=str(cache)))
    assert (cache / "uv").is_dir(), "a workspace under the threshold is never pruned"


async def test_a_workspace_that_keeps_rejecting_writes_is_restarted_once_then_reported(tmp_path):
    sandbox = _make(tmp_path)
    workdir = tmp_path / "workspace"
    base = sandbox.session_id
    workdir.chmod(0o500)
    try:
        with pytest.raises(SandboxUnavailableError, match="rejects writes") as raised:
            await sandbox.execute("echo still-runs", cwd="/")
        # Identify the affected session without prescribing destructive recovery.
        message = str(raised.value)
        assert "delete-capacity-provider-session" not in message
        assert "owner-approved data-loss decision" in message
        assert f"session={base}" in message
        with pytest.raises(SandboxUnavailableError, match="Not retrying"):
            await sandbox.execute("echo again", cwd="/")
    finally:
        workdir.chmod(0o700)
    client = _client(sandbox)
    assert client.stops == [base], "an I/O fault gets exactly one restart"
    assert sandbox.session_id == base, "the workspace is never swapped for an empty one"


async def test_a_workspace_still_full_after_pruning_is_reported_not_restarted_or_abandoned(healthy_volume, tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FullUnlessPrunedClient(workdir=str(workdir), forever=True)
    sandbox = _make(tmp_path, client)
    result = await sandbox.execute("echo still-usable")
    assert result.stdout == "still-usable\n", "deleting works on a full disk: stay usable"
    assert client.stops == [], "a restart cannot free files"
    assert client.forced_prunes == 1
    assert sandbox.session_id == sandbox_session_id_for(sandbox)
    (notice,), _ = sandbox.notices_since(0)
    assert "FULL" in notice and "du -xsh" in notice and "rm -rf" in notice
    assert "/.[!.]*" in notice, "the du advice includes dot-directories (.cache, .venv)"
    assert "MB cap" not in notice


def sandbox_session_id_for(sandbox: RuntimeSandbox) -> str:
    return runtime_module.sandbox_session_id("gh-strands-agents-sdk-python-pr-5470")


# ---- 7. read_file: chunked, checksummed, retried ------------------------------------------------


async def test_a_large_file_round_trips_intact_in_slices(sandbox, tmp_path):
    payload = os.urandom(SANDBOX_READ_CHUNK_BYTES * 3 + 1234)  # ~150 KB: past where prod broke
    target = tmp_path / "workspace" / "agent.ts"
    target.write_bytes(payload)
    before = len(_client(sandbox).commands)
    assert await sandbox.read_file(str(target)) == payload
    reads = len(_client(sandbox).commands) - before
    assert reads >= 4, "header+first slice, then three more slices"


async def test_a_small_file_is_one_round_trip(sandbox, tmp_path):
    await sandbox.execute("true")  # wake + bootstrap first
    target = tmp_path / "workspace" / "small.txt"
    target.write_text("hello\n")
    before = len(_client(sandbox).commands)
    assert await sandbox.read_text(str(target)) == "hello\n"
    assert len(_client(sandbox).commands) - before == 1


async def test_an_empty_file_reads_as_empty(sandbox, tmp_path):
    target = tmp_path / "workspace" / "empty"
    target.write_bytes(b"")
    assert await sandbox.read_file(str(target)) == b""


async def test_a_missing_file_or_a_directory_is_file_not_found(sandbox, tmp_path):
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file(str(tmp_path / "workspace" / "nope.txt"))
    with pytest.raises(FileNotFoundError):
        await sandbox.read_file(str(tmp_path / "workspace"))


class CorruptingClient(FakeAgentCoreClient):
    """Corrupts the stdout of the first N slice commands (``dd … skip=``), like a lossy stream."""

    def __init__(self, *, workdir: str, corrupt: int) -> None:
        super().__init__(workdir=workdir)
        self.corrupt = corrupt

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        response = super().invoke_agent_runtime_command(**kwargs)
        if self.corrupt and "skip=" in kwargs["body"]["command"]:
            self.corrupt -= 1
            events = []
            for event in response["stream"]:
                delta = event.get("chunk", {}).get("contentDelta", {})
                if delta.get("stdout"):
                    head, _, body = delta["stdout"].partition("\n")
                    delta["stdout"] = f"{head}\n{body[: len(body) // 2]}"  # truncated chunk
                events.append(event)
            response = {"stream": iter(events)}
        return response


async def test_a_corrupt_slice_is_detected_and_retried(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = _make(tmp_path, CorruptingClient(workdir=str(workdir), corrupt=1))
    payload = os.urandom(SANDBOX_READ_CHUNK_BYTES * 2 + 10)
    (workdir / "big.bin").write_bytes(payload)
    assert await sandbox.read_file(str(workdir / "big.bin")) == payload


async def test_persistent_corruption_is_an_error_not_wrong_bytes(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = _make(tmp_path, CorruptingClient(workdir=str(workdir), corrupt=1000))
    (workdir / "big.bin").write_bytes(os.urandom(SANDBOX_READ_CHUNK_BYTES + 10))
    with pytest.raises(OSError, match="intact"):
        await sandbox.read_file(str(workdir / "big.bin"))


async def test_a_file_changing_mid_read_is_never_returned_spliced(sandbox, tmp_path):
    """The whole-file sha256 guards against a splice of old and new content."""
    target = tmp_path / "workspace" / "moving.bin"
    target.write_bytes(os.urandom(SANDBOX_READ_CHUNK_BYTES * 2))
    real_slice = sandbox._read_slice
    changed = {"done": False}

    async def change_then_slice(path: str, index: int, size: int) -> bytes:
        if not changed["done"]:  # after the header + first slice, before the second
            changed["done"] = True
            target.write_bytes(os.urandom(SANDBOX_READ_CHUNK_BYTES * 2))
        return await real_slice(path, index, size)

    sandbox._read_slice = change_then_slice  # type: ignore[method-assign]
    data = await sandbox.read_file(str(target))
    assert hashlib.sha256(data).digest() == hashlib.sha256(target.read_bytes()).digest()


async def test_the_harness_read_tool_reads_a_large_file(sandbox, tmp_path):
    """End to end through ``read_text`` (what the harness ``read`` tool calls)."""
    text = "".join(f"line {i}: {base64.b64encode(os.urandom(30)).decode()}\n" for i in range(4000))
    target = tmp_path / "workspace" / "agent.py"
    target.write_text(text)
    assert len(text) > 150_000
    assert await sandbox.read_text(str(target)) == text


class HeartbeatInterleavingClient(FakeAgentCoreClient):
    """Delivers stdout one line per chunk with a heartbeat byte on stderr between, as a real
    stream does when a command (here: the bootstrap's prune) runs past the heartbeat interval."""

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        events = []
        for event in super().invoke_agent_runtime_command(**kwargs)["stream"]:
            stdout = event.get("chunk", {}).get("contentDelta", {}).get("stdout")
            if not stdout:
                events.append(event)
                continue
            for line in stdout.splitlines(keepends=True):
                events.append({"chunk": {"contentDelta": {"stdout": line}}})
                events.append({"chunk": {"contentDelta": {"stderr": SANDBOX_HEARTBEAT_MARKER}}})
        return {"stream": iter(events)}


def test_heartbeat_bytes_do_not_hide_the_bootstrap_markers(tmp_path):
    """Review: R1 S6."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = _make(tmp_path, HeartbeatInterleavingClient(workdir=str(workdir)))
    health = sandbox._bootstrap(force_prune=True)
    assert health.writable is True
    assert health.used_kb is not None and health.pruned_kb is not None


async def test_a_file_over_the_read_cap_is_refused_with_directions_to_read_a_range(sandbox, tmp_path):
    """Review: R1 S5. A sparse file costs no disk, as a PR could make one."""
    from strandly.core.constants import SANDBOX_READ_MAX_BYTES
    from strandly.sandbox.runtime import SandboxFileTooLargeError

    target = tmp_path / "workspace" / "huge.log"
    with target.open("wb") as f:
        f.truncate(SANDBOX_READ_MAX_BYTES + 1)
    await sandbox.execute("true")
    before = len(_client(sandbox).commands)
    with pytest.raises(SandboxFileTooLargeError, match=r"sed -n '1,200p'") as caught:
        await sandbox.read_text(str(target))
    assert isinstance(caught.value, OSError)
    assert len(_client(sandbox).commands) - before == 1, "refused after the header, no slices sent"


async def test_a_file_at_the_read_cap_is_still_read(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "SANDBOX_READ_MAX_BYTES", SANDBOX_READ_CHUNK_BYTES * 2)
    payload = os.urandom(SANDBOX_READ_CHUNK_BYTES * 2)
    target = tmp_path / "workspace" / "edge.bin"
    target.write_bytes(payload)
    assert await sandbox.read_file(str(target)) == payload


class BitFlippingClient(FakeAgentCoreClient):
    """Flips one byte in the first N slice payloads (``dd … skip=``) and keeps their length, so only
    the per-slice sha256 can tell."""

    def __init__(self, *, workdir: str, corrupt: int) -> None:
        super().__init__(workdir=workdir)
        self.corrupt = corrupt
        self.headers = 0

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        command = kwargs["body"]["command"]
        if "wc -c <" in command:
            self.headers += 1
        response = super().invoke_agent_runtime_command(**kwargs)
        if not (self.corrupt and "skip=" in command):
            return response
        self.corrupt -= 1
        events = []
        for event in response["stream"]:
            delta = event.get("chunk", {}).get("contentDelta", {})
            if delta.get("stdout"):
                digest, _, body = delta["stdout"].partition("\n")
                raw = bytearray(base64.b64decode(body.strip()))
                raw[0] ^= 0xFF
                delta["stdout"] = f"{digest}\n{base64.b64encode(bytes(raw)).decode()}"
            events.append(event)
        return {"stream": iter(events)}


async def test_a_same_length_corrupt_slice_fails_its_own_checksum(tmp_path):
    """Only the per-slice sha256 catches this one."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    sandbox = _make(tmp_path, BitFlippingClient(workdir=str(workdir), corrupt=1000))
    (workdir / "big.bin").write_bytes(os.urandom(SANDBOX_READ_CHUNK_BYTES + 10))
    with pytest.raises(OSError, match="slice 1 failed its checksum"):
        await sandbox._read_slice(str(workdir / "big.bin"), 1, SANDBOX_READ_CHUNK_BYTES + 10)


async def test_a_corrupt_slice_is_retried_alone_not_by_rereading_the_file(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = BitFlippingClient(workdir=str(workdir), corrupt=1)
    sandbox = _make(tmp_path, client)
    payload = os.urandom(SANDBOX_READ_CHUNK_BYTES * 2 + 10)
    (workdir / "big.bin").write_bytes(payload)
    assert await sandbox.read_file(str(workdir / "big.bin")) == payload
    assert client.headers == 1, "the slice check caught it; the whole-file re-read never ran"



# ---- S2: wake failures open the circuit, are bounded, and a 5xx wake is retried -----------------


class DeadRuntimeClient(FakeAgentCoreClient):
    """A sandbox that answers every wake (and, once set, every command) with a 502."""

    def __init__(self, *, workdir: str) -> None:
        super().__init__(workdir=workdir)
        self.dead = False
        self.wake_attempts = 0

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.wake_attempts += 1
        if self.dead:
            raise FakeClientError("RuntimeClientError", BAD_GATEWAY)
        return super().invoke_agent_runtime(**kwargs)

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        if self.dead:
            raise FakeClientError("RuntimeClientError", BAD_GATEWAY)
        return super().invoke_agent_runtime_command(**kwargs)


async def test_a_dead_runtime_at_wake_opens_the_circuit_instead_of_rewaking_per_call(tmp_path):
    """Review: R1 S2."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = DeadRuntimeClient(workdir=str(workdir))
    client.dead = True
    sandbox = _make(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="could not be started"):
        await sandbox.execute("echo up")
    assert client.wake_attempts == SANDBOX_WAKE_MAX_ATTEMPTS, "a 502 on wake is retried"
    for _ in range(3):
        with pytest.raises(SandboxUnavailableError, match="Not retrying"):
            await sandbox.execute("echo up")
    assert client.wake_attempts == SANDBOX_WAKE_MAX_ATTEMPTS, "no further wakes while open"


async def test_dead_command_plane_does_not_trigger_stop_or_repeated_wakes(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = DeadRuntimeClient(workdir=str(workdir))
    sandbox = _make(tmp_path, client)
    await sandbox.execute("true")
    client.dead = True
    wakes = client.wake_attempts
    for _ in range(3):
        with pytest.raises(SandboxUnavailableError):
            await sandbox.execute("echo up")
    assert not client.stops
    assert client.wake_attempts == wakes


async def test_a_single_502_on_wake_is_retried(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FlakyWakeClient(
        workdir=str(workdir), wake_errors=[FakeClientError("RuntimeClientError", BAD_GATEWAY)]
    )
    sandbox = _make(tmp_path, client)
    assert (await sandbox.execute("echo up")).stdout == "up\n"
    assert len(client.wake_attempts) == 2


async def test_the_wake_budget_bounds_retries_in_wall_clock_time(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "SANDBOX_WAKE_BUDGET_SECONDS", 10.0)  # < one retry delay
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = FlakyWakeClient(
        workdir=str(workdir),
        wake_errors=[FakeClientError("RuntimeClientError", INIT_TIMEOUT)] * SANDBOX_WAKE_MAX_ATTEMPTS,
    )
    sandbox = _make(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="after 1 attempt"):
        await sandbox.execute("echo up")
    assert len(client.wake_attempts) == 1


async def test_concurrent_502_failures_do_not_replay_or_stop(tmp_path):
    import asyncio

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = DeadRuntimeClient(workdir=str(workdir))
    sandbox = _make(tmp_path, client)
    await sandbox.execute("true")
    client.dead = True
    results = await asyncio.gather(
        *(sandbox.execute(f"echo side-effect-{i}") for i in range(10)), return_exceptions=True
    )
    assert all(isinstance(result, SandboxUnavailableError) for result in results)
    assert not client.stops
    assert not sandbox.ready


# ---- storage advice on the EBS workspace -------------------------------------------------------


def test_storage_advice_describes_the_persistent_volume():
    advice = runtime_module.storage_error_advice("No space left on device", 41_943_040)
    assert "(~40 GiB)" in advice and "/.[!.]*" in advice
    # Rounded, not floored: a 40 GiB volume's filesystem reports a little under 40 GiB.
    assert "(~40 GiB)" in runtime_module.storage_error_advice("x", 41_500_000)  # 39.6 GiB
    assert "df -h /mnt/workspace" in advice and SANDBOX_CACHE_ROOT in advice
    assert _OLD_1_GIB.search(advice) is None and "session storage" not in advice
    assert "(~" not in runtime_module.storage_error_advice("No space left on device")


async def test_the_wake_response_tells_the_sandbox_its_volume_size(sandbox):
    sandbox._note_wake_response(b'{"workspaceCapacityBytes": 42949672960}')
    assert sandbox._capacity_kb == 41_943_040
    sandbox._note_wake_response(b'{"workspaceCapacityBytes": true}')
    assert sandbox._capacity_kb == 41_943_040, "only a real byte count is taken"
