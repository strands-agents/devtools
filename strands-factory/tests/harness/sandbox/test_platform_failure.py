"""Protocol platform failures are not process exit codes or permission to replay work."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from strands.sandbox.types import ExecutionResult
from strands.vended_tools.shell import make_shell
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient

from strandly.sandbox import runtime as runtime_module
from strandly.sandbox.runtime import (
    RuntimeSandbox,
    SandboxExecutionUncertainError,
    SandboxPlatformError,
)


class PlatformClient(FakeAgentCoreClient):
    def __init__(self, workdir: str) -> None:
        super().__init__(workdir=workdir)
        self.healthy = False
        self.sent: list[str] = []
        self.stop_count = 0
        self.stop_fails = False
        self.reset_fixes = True
        self.exit_code = -1
        self.status = "COMPLETED"
        self.output = ""

    def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
        command = kwargs["body"]["command"]
        self.sent.append(command)
        if self.healthy:
            return super().invoke_agent_runtime_command(**kwargs)
        events = []
        if self.output:
            events.append({"chunk": {"contentDelta": {"stdout": self.output}}})
        events.append(
            {"chunk": {"contentStop": {"exitCode": self.exit_code, "status": self.status}}}
        )
        return {"stream": iter(events)}

    def stop_runtime_session(self, **kwargs: Any) -> dict[str, Any]:
        self.stop_count += 1
        time.sleep(0.01)
        if self.stop_fails:
            raise RuntimeError("a-secret-sdk-message")
        self.healthy = self.reset_fixes
        return {**super().stop_runtime_session(**kwargs), "statusCode": 200}


@pytest.fixture
def platform(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = PlatformClient(str(workdir))
    sandbox = RuntimeSandbox(runtime_arn=RUNTIME_ARN, session_key="platform-test", client=client, workdir=str(workdir))
    sandbox._ready = True
    return sandbox, client


@pytest.mark.parametrize("output", ["", "possibly-sensitive-output\n"])
async def test_protocol_minus_one_is_typed_and_never_replayed(platform, output):
    sandbox, client = platform
    client.output = output
    with pytest.raises(SandboxPlatformError) as info:
        await sandbox.execute("SIDE_EFFECT secret-token")
    message = str(info.value)
    assert "exitCode=-1" in message
    assert "empty stderr" in message
    assert "command-slot exhaustion" in message and "197" in message
    assert "Stopping and resuming the session clears it" in message
    assert "NOT replayed" in message
    assert sandbox.session_id in message
    assert sandbox.runtime_arn in message
    assert "secret-token" not in message
    assert "possibly-sensitive-output" not in message
    assert info.value.stdout == output
    assert len(client.sent) == 1
    assert not sandbox.ready
    assert client.stop_count == 0


async def test_sdk_shell_raises_a_tool_error_instead_of_success_json(platform):
    sandbox, _ = platform
    tool = make_shell(sandbox=sandbox)
    tool_use = {"toolUseId": "t1", "name": "shell", "input": {"command": "SIDE_EFFECT"}}
    events = [event async for event in tool.stream(tool_use, {"agent": object()})]
    result = events[-1].tool_result
    assert result["status"] == "error"
    assert "command platform failure" in result["content"][0]["text"]


async def test_file_read_keeps_platform_diagnostic_instead_of_unknown_oserror(platform):
    sandbox, _ = platform
    with pytest.raises(SandboxPlatformError, match="exitCode=-1"):
        await sandbox.read_file("/etc/os-release")


async def test_unhealthy_calls_share_one_safe_probe_then_fail_fast(platform):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    results = await asyncio.gather(
        *(sandbox.execute(f"must-not-run-{i}") for i in range(16)), return_exceptions=True
    )
    assert all(isinstance(result, SandboxPlatformError) for result in results)
    assert client.sent[1:] == [runtime_module._HEALTH_PROBE]
    assert not client.wakes
    assert client.stop_count == 0
    assert not sandbox.ready


async def test_transient_platform_failure_recovers_by_probe_not_replay(platform):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    client.healthy = True
    result = await sandbox.execute("printf next-command")
    assert result.stdout == "next-command"
    assert sandbox.ready
    assert sum("SIDE_EFFECT" in command for command in client.sent) == 1
    assert client.stop_count == 0
    # The automatic recovery budget is finite, even if failures recur.
    client.healthy = False
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("second-failure")
    sent = len(client.sent)
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("not-sent")
    assert len(client.sent) == sent


class _ConnectionClosed(Exception):
    """Stands in for botocore's ``ConnectionClosedError`` (a transport error with no error code)."""


async def test_a_later_failure_gets_its_own_probe_once_the_interval_has_passed(
    platform, monkeypatch, caplog
):
    """Production, 2026-11-15: one failure, recovered by its probe, used the adapter's only probe; a
    later ConnectionClosedError then fenced the main agent and four subagents for good, each call
    refused in ~10 ms without being sent, though the session itself was fine."""
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("first-failure")
    client.healthy = True
    assert (await sandbox.execute("printf ok")).stdout == "ok"  # recovered by the first probe

    sent_before = len(client.sent)
    original = client.invoke_agent_runtime_command
    def closed_once(**kwargs: Any) -> dict[str, Any]:
        client.invoke_agent_runtime_command = original  # type: ignore[method-assign]
        client.sent.append(kwargs["body"]["command"])
        raise _ConnectionClosed()
    client.invoke_agent_runtime_command = closed_once  # type: ignore[method-assign]
    with caplog.at_level("WARNING", logger=runtime_module.logger.name):
        with pytest.raises(SandboxExecutionUncertainError, match="_ConnectionClosed"):
            await sandbox.execute("transport-blip")
        # Moments after the previous probe: fail fast, nothing sent.
        with pytest.raises(SandboxExecutionUncertainError):
            await sandbox.execute("not-sent")
        assert client.sent[sent_before:] == [client.sent[sent_before]]
        # Once the interval has passed the next caller probes again, and the session recovers.
        monkeypatch.setattr(runtime_module, "SANDBOX_PLATFORM_PROBE_INTERVAL_SECONDS", 0.0, raising=False)
        assert (await sandbox.execute("printf again")).stdout == "again"
    assert client.sent.count(runtime_module._HEALTH_PROBE) == 2
    assert not any("not-sent" in command for command in client.sent)
    assert sum("transport-blip" in command for command in client.sent) == 1  # never replayed
    assert client.stop_count == 0 and not client.wakes
    assert "platform failure: command completion is unknown (_ConnectionClosed)" in caplog.text
    assert "health probe after a platform failure passed: stops=COMPLETED/0" in caplog.text


async def test_probes_stay_rate_limited_while_the_session_stays_unhealthy(platform, monkeypatch):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    for i in range(5):  # within one interval: the one probe, then nothing is sent
        with pytest.raises(SandboxPlatformError):
            await sandbox.execute(f"must-not-run-{i}")
    assert client.sent[1:] == [runtime_module._HEALTH_PROBE]
    sandbox._platform_last_probe -= runtime_module.SANDBOX_PLATFORM_PROBE_INTERVAL_SECONDS
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("must-not-run-late")
    assert client.sent[1:] == [runtime_module._HEALTH_PROBE] * 2
    assert not any("must-not-run" in command for command in client.sent)


async def test_reset_requires_explicit_ephemeral_loss_approval(platform):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    with pytest.raises(ValueError, match="approval"):
        await sandbox.recover_platform_session()
    assert client.stop_count == 0
    assert not sandbox.ready


async def test_approved_concurrent_cross_loop_reset_is_once_and_keeps_workspace(platform):
    sandbox, client = platform
    kept = Path(sandbox.workdir) / "work.txt"
    kept.write_text("durable work")
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")

    def recover():
        asyncio.run(sandbox.recover_platform_session(allow_ephemeral_loss=True))

    await asyncio.wait_for(asyncio.gather(*(asyncio.to_thread(recover) for _ in range(12))), 10)
    assert client.stop_count == 1
    assert len(client.wakes) == 1
    assert kept.read_text() == "durable work"
    assert sandbox.ready
    assert sum("SIDE_EFFECT" in command for command in client.sent) == 1
    notices, _ = sandbox.notices_since(0)
    assert any("outside" in note and "NOT replayed" in note for note in notices)
    result = await sandbox.execute("cat work.txt")
    assert result.stdout == "durable work"


@pytest.mark.parametrize("stop_fails", [True, False])
async def test_failed_approved_recovery_is_bounded_and_sanitized(platform, stop_fails):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    client.stop_fails = stop_fails
    client.reset_fixes = False
    for _ in range(3):
        with pytest.raises(SandboxPlatformError) as info:
            await sandbox.recover_platform_session(allow_ephemeral_loss=True)
        assert "a-secret-sdk-message" not in str(info.value)
    assert client.stop_count == 1
    assert not sandbox.ready
    if stop_fails:
        assert not client.wakes


async def test_late_failure_from_old_generation_does_not_poison_recovered_session(platform):
    sandbox, _ = platform
    generation = sandbox._restart_generation
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    error = sandbox._record_platform_failure(generation)
    assert isinstance(error, SandboxPlatformError)
    assert sandbox.ready
    assert sandbox._platform_error is None


@pytest.mark.parametrize("code", [1, 2, 127, 137])
async def test_process_failures_do_not_mark_platform_unhealthy(platform, code):
    sandbox, client = platform
    client.exit_code = code
    result = await sandbox.execute("process-failure")
    assert result.exit_code == code
    assert sandbox.ready
    assert sandbox._platform_error is None
    assert client.stop_count == 0


@pytest.mark.parametrize("code", [0, -1, 124])
async def test_timeout_completion_is_not_platform_failure(platform, code):
    sandbox, client = platform
    client.status = "TIMED_OUT"
    client.exit_code = code
    result = await sandbox.execute("timeout", timeout=1)
    assert result.exit_code == 124
    assert "timed out" in result.stderr.lower()
    assert sandbox.ready
    assert client.stop_count == 0


async def test_missing_user_cwd_is_a_process_failure_not_a_restart(platform):
    sandbox, client = platform
    client.healthy = True
    result = await sandbox.execute("printf must-not-run", cwd="/no-such-strandly-cwd")
    assert result.exit_code > 0
    assert result.stdout == ""
    assert "No such file" in result.stderr
    assert sandbox.ready
    assert client.stop_count == 0
    assert len(client.sent) == 1


async def test_cancelled_turn_does_not_probe_or_reset(platform):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    cancel = threading.Event()
    cancel.set()
    sandbox.bind_cancel_signal(cancel)
    result = await sandbox.execute("not-sent")
    assert result.exit_code == 130
    assert len(client.sent) == 1
    assert client.stop_count == 0


async def test_cancelled_reset_waiter_does_not_cancel_recovery_or_skill_setup(platform, monkeypatch):
    sandbox, client = platform
    started, release = threading.Event(), threading.Event()
    original = client.stop_runtime_session

    def stop(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(client, "stop_runtime_session", stop)
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    ran = []

    async def skill_setup():
        ran.append(1)
        await sandbox.execute("printf skill-setup")

    sandbox.add_first_use_hook(skill_setup)
    task = asyncio.create_task(sandbox.recover_platform_session(allow_ephemeral_loss=True))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    result = await sandbox.execute("printf resumed")
    assert result.stdout == "resumed"
    assert ran == [1]
    assert client.stop_count == 1


async def test_bootstrap_platform_failure_is_not_readiness(platform):
    sandbox, client = platform
    sandbox._ready = False
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    assert not sandbox.ready
    client.healthy = True
    result = await sandbox.execute("printf after-bootstrap")
    assert isinstance(result, ExecutionResult)
    assert result.stdout == "after-bootstrap"
    assert len(client.wakes) == 2
    assert sandbox.ready


@pytest.mark.parametrize(
    ("answer", "healthy", "warned"),
    [
        ("strandly-sandbox-health zombies=0", True, False),
        ("strandly-sandbox-health zombies=      12\n", True, True),
        ("strandly-sandbox-health", True, False),
        ("strandly-sandbox-health zombies=", True, False),
        ("strandly-sandbox-health zombies=wc: not found", True, False),
        ("zombies=0", False, False),
    ],
)
def test_health_probe_reports_unreaped_processes(platform, monkeypatch, caplog, answer, healthy, warned):
    sandbox, _ = platform
    events = [
        {"contentDelta": {"stdout": answer}},
        {"contentStop": {"exitCode": 0, "status": "COMPLETED"}},
    ]
    monkeypatch.setattr(sandbox, "_iter_events", lambda command, timeout: iter(events))
    with caplog.at_level("WARNING", logger=runtime_module.logger.name):
        assert sandbox._command_plane_healthy_sync() is healthy
    assert ("12 unreaped (zombie) processes" in caplog.text) is warned
    assert ("unreaped (zombie) processes" in caplog.text) is warned


def test_image_runs_an_init_as_pid_1_to_reap_orphans():
    lines = (Path(__file__).resolve().parents[3] / "sandbox" / "Dockerfile.sandbox").read_text().splitlines()
    assert lines[-2:] == ['ENTRYPOINT ["/usr/bin/tini", "--"]', 'CMD ["python", "/app/sandbox_main.py"]']
    assert any(line.rstrip(" \\").endswith(" tini") for line in lines)  # from the distro package


def test_image_home_work_alias_only_created_in_fresh_image():
    root = Path(__file__).resolve().parents[3]
    dockerfile = (root / "sandbox" / "Dockerfile.sandbox").read_text()
    assert "&& ln -s /mnt/workspace /home/sandbox/work" in dockerfile
    assert "WORKDIR /mnt/workspace" in dockerfile
    assert "ln -sf" not in dockerfile
    assert "mv /home/sandbox" not in dockerfile
    sandbox = RuntimeSandbox(runtime_arn=RUNTIME_ARN, session_key="default-cwd", client=object())
    assert sandbox.workdir == "/mnt/workspace"
    assert "cd /mnt/workspace" in sandbox._wrap("pwd", cwd=None, env=None)


async def test_health_probe_deadline_does_not_leave_readiness_lock_stuck(platform, monkeypatch):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    release = threading.Event()
    original = client.invoke_agent_runtime_command

    def stuck(**kwargs):
        if kwargs["body"]["command"] == runtime_module._HEALTH_PROBE:
            assert release.wait(20)
        return original(**kwargs)

    monkeypatch.setattr(client, "invoke_agent_runtime_command", stuck)
    monkeypatch.setattr(runtime_module, "SANDBOX_TRANSPORT_GRACE_SECONDS", 0)
    try:
        with pytest.raises(SandboxPlatformError):
            await asyncio.wait_for(sandbox.execute("not-sent"), 15)
        with pytest.raises(SandboxPlatformError):
            await asyncio.wait_for(sandbox.execute("also-not-sent"), 1)
        assert not sandbox.ready
    finally:
        release.set()


async def test_cancelled_inflight_command_does_not_turn_late_minus_one_into_recovery(platform, monkeypatch):
    sandbox, client = platform
    started, release = threading.Event(), threading.Event()
    cancel = threading.Event()
    sandbox.bind_cancel_signal(cancel)
    original = client.invoke_agent_runtime_command

    def blocked(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(client, "invoke_agent_runtime_command", blocked)
    task = asyncio.create_task(sandbox.execute("in-flight"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        cancel.set()
        result = await asyncio.wait_for(task, 2)
        assert result.exit_code == 130
    finally:
        release.set()
    await asyncio.sleep(0.05)
    assert sandbox._platform_error is None
    assert sandbox.ready
    assert client.stop_count == 0


@pytest.mark.parametrize(
    "stop",
    [{}, {"status": "COMPLETED"}, {"exitCode": 0}, {"exitCode": 0, "status": "UNKNOWN"},
     {"exitCode": True, "status": "COMPLETED"}, {"exitCode": -9, "status": "COMPLETED"}, None],
)
async def test_invalid_completion_is_unknown_not_success_or_platform_exit(platform, monkeypatch, stop):
    from strandly.sandbox.runtime import SandboxExecutionUncertainError

    sandbox, client = platform
    monkeypatch.setattr(client, "invoke_agent_runtime_command", lambda **kw: {
        "stream": iter([{"chunk": {"contentStop": stop}}])
    })
    with pytest.raises(SandboxExecutionUncertainError) as info:
        await sandbox.execute("SIDE_EFFECT")
    assert not isinstance(info.value, SandboxPlatformError)
    assert "invalid terminal event" in str(info.value)
    assert not sandbox.ready
    assert client.stop_count == 0


@pytest.mark.parametrize("failure", ["platform", "502", "closed"])
@pytest.mark.parametrize("prefix", [[], [{"chunk": {"contentStart": {}}}],
    [{"chunk": {"contentDelta": {"stdout": "partial-out", "stderr": "partial-err"}}}]])
async def test_side_effect_before_uncertain_failure_is_never_repeated(platform, monkeypatch, failure, prefix):
    from test_runtime_sandbox import FakeClientError

    from strandly.sandbox.runtime import SandboxExecutionUncertainError

    sandbox, client = platform
    side_effects = []

    def invoke(**kwargs):
        side_effects.append(kwargs["body"]["command"])

        def stream():
            yield from prefix
            if failure == "502":
                raise FakeClientError("RuntimeClientError", "Received error (502)")
            if failure == "platform":
                yield {"chunk": {"contentStop": {"exitCode": -1, "status": "COMPLETED"}}}

        return {"stream": stream()}

    monkeypatch.setattr(client, "invoke_agent_runtime_command", invoke)
    with pytest.raises(SandboxExecutionUncertainError) as info:
        await sandbox.execute("SIDE_EFFECT")
    assert len(side_effects) == 1
    assert info.value.stdout == ("partial-out" if prefix and "contentDelta" in prefix[0]["chunk"] else "")
    assert info.value.stderr == ("partial-err" if info.value.stdout else "")
    assert client.stop_count == 0
    assert not sandbox.ready


@pytest.mark.parametrize("status", [202, 500, None])
async def test_unknown_stop_outcome_does_not_wake_or_claim_recovery(platform, monkeypatch, status):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    monkeypatch.setattr(client, "stop_runtime_session", lambda **kw: {"statusCode": status})
    with pytest.raises(SandboxPlatformError):
        await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    assert not client.wakes
    assert not sandbox.ready


async def test_ready_wake_and_zero_exit_without_probe_marker_are_not_health(platform, monkeypatch):
    sandbox, client = platform
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    monkeypatch.setattr(client, "invoke_agent_runtime_command", lambda **kw: {
        "stream": iter([{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}])
    })
    with pytest.raises(SandboxPlatformError):
        await sandbox.recover_platform_session(allow_ephemeral_loss=True)
    assert len(client.wakes) == 1
    assert not sandbox.ready


async def test_new_sends_are_fenced_while_reset_is_in_progress(platform, monkeypatch):
    from strandly.sandbox.runtime import SandboxExecutionUncertainError

    sandbox, client = platform
    started, release = threading.Event(), threading.Event()
    original = client.stop_runtime_session

    def stop(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(client, "stop_runtime_session", stop)
    with pytest.raises(SandboxPlatformError):
        await sandbox.execute("SIDE_EFFECT")
    reset = asyncio.create_task(sandbox.recover_platform_session(allow_ephemeral_loss=True))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        # A caller that already passed readiness before the fault cannot send during recovery.
        with pytest.raises(SandboxExecutionUncertainError):
            list(sandbox._retrying("must-not-send", 1, allow_wake=True))
        assert len(client.sent) == 1
    finally:
        release.set()
    await reset
    assert sandbox.ready
    assert client.stop_count == 1
