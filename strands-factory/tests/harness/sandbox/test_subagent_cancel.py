"""A cancelled run stops its subagent children's sandbox commands, even after the parent's turn ended.

A child shares its parent's ``RuntimeSandbox`` and runs in the background, so it can still be issuing
commands when the parent's turn has ended and unbound the sandbox's cancel signal. The child's
commands follow the run's own signal instead: a command whose wake is still pending is never sent,
and one that is running stops being waited on (exit 130). Driven end to end through ``run_turn``
with the hermetic fake ``bedrock-agentcore`` client from ``test_runtime_sandbox``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import pytest
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient

from model_seam import use_model
from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.config import Config
from strandly.core.constants import MODEL_TIERS
from strandly.core.context import RuntimeContext
from strandly.sandbox import runtime as runtime_module
from strandly.sandbox.runtime import RuntimeSandbox, command_cancel_signal
from strandly.serve.turn import run_turn


async def _wait_for(condition: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _ready_threads() -> list[threading.Thread]:
    """The sandbox's background wake + bootstrap threads (``RuntimeSandbox._in_background``)."""
    return [t for t in threading.enumerate() if t.name == "strandly-sandbox-ready" and t.is_alive()]


def _sandbox(tmp_path, client: FakeAgentCoreClient) -> RuntimeSandbox:
    return RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_id="a" * 40,
        workdir=str(tmp_path / "workspace"),
        client=client,
    )


class _RecordingSandbox:
    """Records every command's final result (the exit code the child's ``shell`` tool saw)."""

    def __init__(self, sandbox: RuntimeSandbox) -> None:
        self.results: dict[str, Any] = {}
        self.stdout: dict[str, str] = {}
        real = sandbox.execute_streaming

        async def recording(command: str, **kwargs: Any):
            async for item in real(command, **kwargs):
                if getattr(item, "stream_type", None) == "stdout":
                    self.stdout[command] = self.stdout.get(command, "") + item.data
                if not hasattr(item, "stream_type"):
                    self.results[command] = item
                yield item

        sandbox.execute_streaming = recording  # type: ignore[method-assign]


def _wire(monkeypatch, tmp_path, sandbox: RuntimeSandbox, child_command: str) -> None:
    """The parent delegates to one background child; the child runs ``child_command``."""
    import strandly.core.agent as agent_mod

    parent = ScriptedModel(
        tool_use_events("subagent", "s1", json.dumps({"task": "run it"})),
        text_events("waiting on the child"),
    )
    child = ScriptedModel(
        tool_use_events("shell", "c1", json.dumps({"command": child_command})),
        text_events("child done"),
    )
    monkeypatch.setattr(agent_mod, "build_sandbox", lambda config, ctx=None: sandbox)
    monkeypatch.setitem(MODEL_TIERS, "default", ("child", "auto"))
    use_model(monkeypatch, lambda model: child if model == "child" else parent)
    monkeypatch.chdir(tmp_path)
    from strandly.serve import cache

    monkeypatch.setattr(cache, "_CACHE", cache.AgentCache())


async def _run_and_cancel(
    tmp_path, when: Any, *, session_id: str | None = None
) -> tuple[dict[str, Any], float, str | None]:
    """Set a turn's cancel once ``when()`` holds, with or without a cached session agent."""
    run_signal = threading.Event()
    outcome: dict[str, Any] = {}
    cancelled_at: list[float] = []

    async def cancel() -> None:
        await _wait_for(when)
        run_signal.set()
        cancelled_at.append(time.monotonic())

    canceller = asyncio.create_task(cancel())
    ctx = RuntimeContext(cwd=str(tmp_path), session_id=session_id)
    stopped = None
    async for event in run_turn(Config(values={}), "go", ctx, cancel_signal=run_signal, outcome=outcome):
        if event.kind == "done":
            stopped = event.data.get("stop_reason")
    await canceller
    return outcome, time.monotonic() - cancelled_at[0], stopped


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "cached-stop"])
async def test_a_cancel_while_a_childs_first_command_wakes_the_sandbox_never_runs_it(
    tmp_path, monkeypatch, caplog, session_id
):
    caplog.set_level(logging.INFO)
    client = FakeAgentCoreClient(workdir=str(tmp_path / "workspace"))
    client.wake_delay = 1.0
    sandbox = _sandbox(tmp_path, client)
    recorder = _RecordingSandbox(sandbox)
    _wire(monkeypatch, tmp_path, sandbox, "echo child-ran")
    waking = {"started": False}
    real_wake = client.invoke_agent_runtime

    def wake(**kwargs: Any) -> dict[str, Any]:
        waking["started"] = True
        return real_wake(**kwargs)

    client.invoke_agent_runtime = wake  # type: ignore[method-assign]

    outcome, waited, stopped = await _run_and_cancel(
        tmp_path, lambda: waking["started"], session_id=session_id
    )
    assert sandbox._cancel_signal is None
    await _wait_for(lambda: "subagent finished: tool_use_id=s1" in caplog.text)
    await _wait_for(lambda: client.wakes)  # the wake itself completes in the background
    # ...and the git bootstrap after it, on the same daemon thread. Left running, it spawns its
    # ``git config --global`` once this test's temp ``$HOME`` is gone (into the next test's home,
    # or the process baseline between tests); wait for it so the test owns everything it started.
    await _wait_for(lambda: not _ready_threads())

    assert waited < 1.0
    assert outcome.get("abandoned") or outcome.get("skipped") or stopped == "cancelled", (
        outcome, stopped
    )
    assert recorder.results["echo child-ran"].exit_code == 130
    assert runtime_module.CANCELLED_NOTE.strip() in recorder.results["echo child-ran"].stderr
    assert not any("echo child-ran" in c for c in client.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "cached-stop"])
async def test_a_cancel_while_a_childs_command_runs_stops_waiting_on_it(
    tmp_path, monkeypatch, caplog, session_id
):
    caplog.set_level(logging.INFO)
    release = threading.Event()
    running = threading.Event()

    class Slow(FakeAgentCoreClient):
        def invoke_agent_runtime_command(self, **kwargs: Any) -> dict[str, Any]:
            if "sleep-forever" not in kwargs["body"]["command"]:
                return super().invoke_agent_runtime_command(**kwargs)

            def slow():
                yield {"chunk": {"contentStart": {}}}
                yield {"chunk": {"contentDelta": {"stdout": "step 1\n"}}}
                running.set()
                release.wait(30)
                yield {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}

            return {"stream": slow()}

    sandbox = _sandbox(tmp_path, Slow(workdir=str(tmp_path / "workspace")))
    recorder = _RecordingSandbox(sandbox)
    _wire(monkeypatch, tmp_path, sandbox, "echo sleep-forever")
    try:
        outcome, waited, stopped = await _run_and_cancel(
            tmp_path,
            lambda: running.is_set() and "step 1" in recorder.stdout.get("echo sleep-forever", ""),
            session_id=session_id,
        )
        assert sandbox._cancel_signal is None
        await _wait_for(lambda: "subagent finished: tool_use_id=s1" in caplog.text)
    finally:
        release.set()

    assert waited < 2.0
    # If the child settles before teardown, the SDK can deliver it and end a cancelled
    # continuation (or skip its model call) instead of leaving work outstanding.
    assert outcome.get("abandoned") or outcome.get("skipped") or stopped == "cancelled", (
        outcome, stopped
    )
    result = recorder.results["echo sleep-forever"]
    assert result.exit_code == 130
    assert "step 1" in result.stdout
    assert runtime_module.CANCELLED_NOTE.strip() in result.stderr


@pytest.mark.asyncio
async def test_a_contexts_cancel_signal_overrides_the_bound_one_and_is_rechecked_after_the_wake(
    tmp_path,
):
    """The signal is checked again once the wake returns, so a cancel landing as the wake finishes
    still keeps the command from being sent."""
    client = FakeAgentCoreClient(workdir=str(tmp_path / "workspace"))
    sandbox = _sandbox(tmp_path, client)
    sandbox.bind_cancel_signal(threading.Event())  # another turn's signal, never set
    run_signal = threading.Event()
    real_ready = sandbox._ready_sync

    def ready_then_cancel(*a: Any, **kw: Any) -> None:
        real_ready(*a, **kw)
        run_signal.set()

    sandbox._ready_sync = ready_then_cancel  # type: ignore[method-assign]
    with command_cancel_signal(run_signal):
        result = await sandbox.execute("echo late")

    assert result.exit_code == 130
    assert client.wakes and not any("echo late" in c for c in client.commands)
    sandbox._ready_sync = real_ready  # type: ignore[method-assign]
    assert (await sandbox.execute("echo next")).stdout.strip() == "next"
