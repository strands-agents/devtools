"""The sandbox wakes at its FIRST USE, never at agent build (docs/operations.md).

Production put the sandbox wake (an EC2 boot, 53–83 s) in front of every run's first model call:
``build_agent`` warmed the sandbox and the skills push then awaited it. These tests pin the new
contract end to end, with the hermetic fake ``bedrock-agentcore`` client from
``test_runtime_sandbox`` (it runs commands for real in a temp dir and records every wake/command):

* a run whose model answers without tools makes NO sandbox call — no wake (InvokeAgentRuntime), no
  command (InvokeAgentRuntimeCommand), no TTL schedule (Scheduler);
* the first shell call wakes exactly once, pushes the skills, then runs — and waits for all of it;
* concurrent first calls (same loop, or a subagent on another loop) share one wake;
* a failed wake is a tool error, and the next call retries;
* a cancel while the first call waits returns at once; the wake and the skills push finish in the
  background and the next call reuses them;
* the environment block still reaches the model.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient, FakeClientError

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.sandbox import runtime as runtime_module
from strandly.sandbox.runtime import RuntimeSandbox, SandboxUnavailableError


class RecordingTtl:
    """Stands in for :class:`~strandly.sandbox.ttl.SessionTtl`: records every Scheduler bump."""

    def __init__(self) -> None:
        self.bumps: list[str] = []

    def bump_soon(self, session_id: str, *, ephemeral: bool = False) -> None:
        self.bumps.append(session_id)


class WakeFailingClient(FakeAgentCoreClient):
    """A fake whose next ``wake_failures`` wakes raise (then wakes succeed)."""

    def __init__(self, *, workdir: str, wake_failures: list[Exception]) -> None:
        super().__init__(workdir=workdir)
        self.wake_failures = list(wake_failures)
        self.wake_attempts = 0

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.wake_attempts += 1
        if self.wake_failures:
            raise self.wake_failures.pop(0)
        return super().invoke_agent_runtime(**kwargs)


class RecordingModel(ScriptedModel):
    """A :class:`ScriptedModel` that keeps the messages of every call (what the model was shown)."""

    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.calls: list[str] = []
        self.system_prompts: list[str] = []

    async def stream(self, messages: Any = None, *args: Any, **kwargs: Any):  # type: ignore[override]
        self.calls.append(json.dumps(messages, default=str))
        self.system_prompts.append(json.dumps([args, kwargs], default=str))
        async for ev in super().stream(messages, *args, **kwargs):
            yield ev


@pytest.fixture(autouse=True)
def _no_retry_delays(monkeypatch):
    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)


@pytest.fixture
def skills_dirs(tmp_path, monkeypatch):
    """Point the skills push at temp dirs: the fake client runs commands on THIS machine."""
    from strandly.skills import loader

    primary = tmp_path / "skills"
    monkeypatch.setattr(loader, "_SANDBOX_SKILLS_DIR", str(primary))
    return primary


def _sandbox(tmp_path, client: FakeAgentCoreClient | None = None, ttl: Any = None) -> RuntimeSandbox:
    workdir = tmp_path / "workspace"
    workdir.mkdir(exist_ok=True)
    return RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_key="gh-strands-agents-sdk-python-issue-42",
        workdir=str(workdir),
        client=client or FakeAgentCoreClient(workdir=str(workdir)),
        ttl=ttl,
    )


async def _build(sandbox: RuntimeSandbox, model: Any, tmp_path, monkeypatch, **kwargs: Any) -> Any:
    """``build_agent`` exactly as production does it: the factory builds (here: is handed) the sandbox."""
    import strandly.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "build_sandbox", lambda config, ctx=None: sandbox)
    return await agent_mod.build_agent(
        Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=model, **kwargs
    )


def _shell_results(agent: Any) -> list[dict[str, Any]]:
    return [
        block["toolResult"]
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


# ---- a run that never uses a tool touches nothing ------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_free_run_never_wakes_the_sandbox(tmp_path, monkeypatch, skills_dirs):
    ttl = RecordingTtl()
    sandbox = _sandbox(tmp_path, ttl=ttl)
    model = RecordingModel(text_events("OK"))

    agent = await _build(sandbox, model, tmp_path, monkeypatch)
    await agent.invoke_async("reply with OK")

    client = sandbox._client
    assert client.wakes == []  # no InvokeAgentRuntime
    assert client.commands == []  # no InvokeAgentRuntimeCommand
    assert ttl.bumps == []  # no Scheduler upsert
    assert not sandbox.ready
    assert not skills_dirs.exists()  # nothing pushed
    # The environment context still reached the model — built without asking the sandbox.
    first_call = model.calls[0]
    assert "<environment>" in first_call
    assert f"Working directory: {sandbox.workdir}" in first_call
    assert "Platform: Linux" in first_call


@pytest.mark.asyncio
async def test_building_the_agent_and_a_subagent_does_no_sandbox_io(tmp_path, monkeypatch, skills_dirs):
    sandbox = _sandbox(tmp_path)
    model = RecordingModel(text_events("OK"))
    parent = await _build(sandbox, model, tmp_path, monkeypatch)
    from strandly.core.agent import build_agent

    # A subagent is handed the parent's sandbox.
    await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=ScriptedModel(),
        sandbox=sandbox,
        spawn_depth=1,
    )

    assert sandbox._client.wakes == [] and sandbox._client.commands == []
    # One push registered for the shared sandbox, not one per agent.
    assert len(sandbox._first_use_hooks) == 1
    # The skills are in the prompt anyway (read from the package), at their in-sandbox location.
    await parent.invoke_async("hi")
    assert f"{skills_dirs}/code-review/SKILL.md" in model.system_prompts[0]
    assert sandbox._client.wakes == []


# ---- the first use wakes, pushes, then runs ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_shell_call_wakes_once_pushes_skills_first_and_waits(
    tmp_path, monkeypatch, skills_dirs
):
    ttl = RecordingTtl()
    sandbox = _sandbox(tmp_path, ttl=ttl)
    pushed = skills_dirs / "bug-verify" / "assets" / "roles" / "reproducer.md"
    check = f"test -f {pushed} && echo PUSHED"
    model = ScriptedModel(
        tool_use_events("shell", "tu-1", json.dumps({"command": check})),
        tool_use_events("shell", "tu-2", json.dumps({"command": "echo second"})),
        text_events("done"),
    )
    agent = await _build(sandbox, model, tmp_path, monkeypatch)
    await agent.invoke_async("look around")

    client = sandbox._client
    assert len(client.wakes) == 1  # exactly one wake, for both calls
    assert sandbox.ready
    results = _shell_results(agent)
    assert [r["status"] for r in results] == ["success", "success"]
    # The first command saw the pushed skill files: the push ran BEFORE it, and it waited.
    assert pushed.is_file() and "PUSHED" in json.dumps(results[0])
    user_commands = [c for c in client.commands if "echo second" in c or check in c]
    push_commands = [c for c in client.commands if "tar -xzf" in c]
    assert push_commands and client.commands.index(push_commands[0]) < client.commands.index(
        user_commands[0]
    )
    assert ttl.bumps  # the wake (and every command) slides the TTL


@pytest.mark.asyncio
async def test_two_concurrent_first_calls_share_one_wake_and_one_hook_phase(tmp_path):
    client = FakeAgentCoreClient(workdir=str(tmp_path / "workspace"))
    client.wake_delay = 0.2  # long enough that both calls are waiting on the same wake
    sandbox = _sandbox(tmp_path, client)
    hook_runs: list[str] = []

    async def hook() -> None:
        hook_runs.append("ran")
        await sandbox.execute("echo from-hook")  # a hook may use the sandbox itself

    sandbox.add_first_use_hook(hook)
    a, b = await asyncio.gather(sandbox.execute("echo a"), sandbox.execute("echo b"))

    assert (a.stdout.strip(), b.stdout.strip()) == ("a", "b")
    assert len(client.wakes) == 1
    assert hook_runs == ["ran"]
    hook_index = next(i for i, c in enumerate(client.commands) if "from-hook" in c)
    assert all(
        hook_index < i for i, c in enumerate(client.commands) if "echo a" in c or "echo b" in c
    )


def test_a_subagent_on_another_loop_shares_the_parents_wake(tmp_path):
    """Subagents share the sandbox and may run on their own event loop."""
    client = FakeAgentCoreClient(workdir=str(tmp_path / "workspace"))
    client.wake_delay = 0.2
    sandbox = _sandbox(tmp_path, client)
    hook_runs: list[str] = []

    async def hook() -> None:
        hook_runs.append("ran")

    sandbox.add_first_use_hook(hook)
    outputs: dict[str, str] = {}

    def run(key: str) -> None:
        outputs[key] = asyncio.run(sandbox.execute(f"echo {key}")).stdout.strip()

    threads = [threading.Thread(target=run, args=(k,)) for k in ("parent", "child")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert outputs == {"parent": "parent", "child": "child"}
    assert len(client.wakes) == 1
    assert hook_runs == ["ran"]


@pytest.mark.asyncio
async def test_a_failing_hook_does_not_fail_the_command_and_a_late_hook_still_runs(tmp_path, caplog):
    sandbox = _sandbox(tmp_path)

    async def broken() -> None:
        raise RuntimeError("push exploded")

    sandbox.add_first_use_hook(broken)
    assert (await sandbox.execute("echo ok")).stdout.strip() == "ok"
    assert "push exploded" in caplog.text

    late: list[str] = []

    async def registered_after_first_use() -> None:
        late.append("ran")

    sandbox.add_first_use_hook(registered_after_first_use)
    await sandbox.execute("true")
    await sandbox.execute("true")
    assert late == ["ran"]  # once, before the next command
    assert len(sandbox._client.wakes) == 1


# ---- a failed wake -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_wake_is_a_tool_error_and_the_next_call_retries(
    tmp_path, monkeypatch, skills_dirs
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = WakeFailingClient(
        workdir=str(workdir), wake_failures=[FakeClientError("AccessDeniedException", "nope")]
    )
    sandbox = _sandbox(tmp_path, client)
    model = ScriptedModel(
        tool_use_events("shell", "tu-1", json.dumps({"command": "echo first"})),
        tool_use_events("shell", "tu-2", json.dumps({"command": "echo second"})),
        text_events("done"),
    )
    agent = await _build(sandbox, model, tmp_path, monkeypatch)
    await agent.invoke_async("go")

    first, second = _shell_results(agent)
    assert first["status"] == "error"
    assert "AccessDeniedException" in json.dumps(first)
    assert second["status"] == "success" and "second" in json.dumps(second)
    assert client.wake_attempts == 2 and len(client.wakes) == 1


@pytest.mark.asyncio
async def test_a_wake_that_runs_out_of_retries_opens_the_circuit_then_recovers(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    client = WakeFailingClient(
        workdir=str(workdir),
        wake_failures=[
            FakeClientError("ThrottlingException")
            for _ in range(runtime_module.SANDBOX_WAKE_MAX_ATTEMPTS)
        ],
    )
    sandbox = _sandbox(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="could not be started"):
        await sandbox.execute("echo a")
    attempts = client.wake_attempts
    # While the circuit is open the next call fails fast, without another round of wakes.
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        await sandbox.execute("echo b")
    assert client.wake_attempts == attempts
    # Once it closes, a later call wakes again — and this time succeeds.
    sandbox._circuit_open_until = float("-inf")
    assert (await sandbox.execute("echo c")).stdout.strip() == "c"
    assert len(client.wakes) == 1


# ---- a cancel during the first use -----------------------------------------------------------------


async def _cancelled_first_use(sandbox: RuntimeSandbox, command: str = "echo first") -> tuple[Any, float]:
    """Run ``command`` as a turn's first sandbox use, cancelling that turn 0.1 s into the wake."""
    signal = threading.Event()
    sandbox.bind_cancel_signal(signal)
    task = asyncio.ensure_future(sandbox.execute(command))
    await asyncio.sleep(0.1)
    signal.set()
    cancelled_at = time.monotonic()
    result = await task
    return result, time.monotonic() - cancelled_at


@pytest.mark.asyncio
async def test_a_cancel_during_a_slow_wake_returns_at_once_and_the_next_command_reuses_it(tmp_path):
    client = FakeAgentCoreClient(workdir=str(tmp_path / "workspace"))
    client.wake_delay = 1.5
    sandbox = _sandbox(tmp_path, client)
    hook_runs: list[str] = []

    async def hook() -> None:
        hook_runs.append("ran")

    sandbox.add_first_use_hook(hook)
    result, waited = await _cancelled_first_use(sandbox)

    assert waited < 1.0
    assert result.exit_code == 130
    assert runtime_module.CANCELLED_NOTE.strip() in result.stderr
    assert not any("echo first" in c for c in client.commands)  # never sent

    sandbox.bind_cancel_signal(threading.Event())  # the next turn
    assert (await sandbox.execute("echo next")).stdout.strip() == "next"
    assert len(client.wakes) == 1  # the cancelled call's wake, reused
    assert hook_runs == ["ran"]


@pytest.mark.asyncio
async def test_skills_are_pushed_when_the_first_sandbox_use_is_cancelled(tmp_path, skills_dirs):
    from strandly.skills.loader import builtin_skills

    client = FakeAgentCoreClient(workdir=str(tmp_path / "workspace"))
    client.wake_delay = 0.5
    sandbox = _sandbox(tmp_path, client)
    builtin_skills(sandbox)  # registers the skills push as a first-use hook

    result, _ = await _cancelled_first_use(sandbox)
    assert result.exit_code == 130

    # The next turn's first command sees the pushed skills: the push ignored the cancelled turn's
    # signal, ran after the wake, and the command waited for it.
    sandbox.bind_cancel_signal(threading.Event())
    pushed = skills_dirs / "bug-verify" / "assets" / "roles" / "reproducer.md"
    check = await sandbox.execute(f"test -f {pushed} && echo PUSHED")
    assert check.stdout.strip() == "PUSHED"
    assert sandbox._first_use_ran == len(sandbox._first_use_hooks)
    assert not any("failed" in notice for notice in sandbox._notices)
    assert len(client.wakes) == 1


@pytest.mark.asyncio
async def test_hooks_ignore_a_cancel_signal_set_before_they_run(tmp_path, skills_dirs):
    """The turn is cancelled while the wake is finishing: the hook's own commands still run."""
    sandbox = _sandbox(tmp_path)
    signal = threading.Event()
    hook_results: list[int] = []

    async def hook() -> None:
        signal.set()  # the turn is cancelled while the hook phase runs
        hook_results.append((await sandbox.execute("mkdir -p hooked")).exit_code)

    sandbox.add_first_use_hook(hook)
    sandbox.bind_cancel_signal(signal)
    result = await sandbox.execute("echo first")
    assert result.exit_code == 130  # the turn's own command stops waiting
    sandbox.bind_cancel_signal(None)
    assert (await sandbox.execute("test -d hooked && echo yes")).stdout.strip() == "yes"
    assert hook_results == [0]
    assert sandbox._first_use_ran == 1


@pytest.mark.asyncio
async def test_a_hook_that_ends_cancelled_is_not_counted_and_runs_again(tmp_path):
    sandbox = _sandbox(tmp_path)
    runs: list[str] = []

    async def hook() -> None:
        runs.append("ran")
        if len(runs) == 1:
            raise asyncio.CancelledError

    sandbox.add_first_use_hook(hook)
    assert (await sandbox.execute("echo a")).stdout.strip() == "a"  # the command still runs
    assert runs == ["ran"] and sandbox._first_use_ran == 0
    assert (await sandbox.execute("echo b")).stdout.strip() == "b"
    assert runs == ["ran", "ran"] and sandbox._first_use_ran == 1
    await sandbox.execute("true")
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_a_failed_hook_logs_the_end_of_its_error(tmp_path, caplog):
    sandbox = _sandbox(tmp_path)

    async def broken() -> None:
        raise OSError(
            f"Failed to create the parent directory for /mnt/x {sandbox._error_context()}: "
            + "noise " * 100
            + "mkdir: cannot create directory: Permission denied"
        )

    sandbox.add_first_use_hook(broken)
    await sandbox.execute("true")
    assert "Permission denied" in caplog.text
