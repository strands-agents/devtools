"""Sandbox notices reach the model, in the production call order. Review: R1 B1.

In production the first sandbox calls after a wake are the harness's own: ``warm_up`` (wake +
bootstrap, where a prune or restart is recorded), then the skills push, then the
environment probe. None of their output reaches the model. These tests run that order with a real
``Agent``, the real skills plugin, the real environment probe and the SDK ``shell`` tool against
the fake AgentCore client (commands run in a host shell), and assert the notice is in the first
tool result the model sees.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from strands import Agent
from strands.vended_tools.shell import shell
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient, FakeClientError

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.plugins.environment import SandboxEnvironment
from strandly.sandbox import runtime as runtime_module
from strandly.sandbox import scripts as scripts_module
from strandly.sandbox.notices import SandboxNotices
from strandly.sandbox.runtime import RuntimeSandbox
from strandly.skills import loader as loader_module
from strandly.skills.loader import build_skills_plugin

BAD_GATEWAY = "Received error (502) from runtime. Please check your CloudWatch logs."


@pytest.fixture(autouse=True)
def _local_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(loader_module, "_SANDBOX_SKILLS_DIR", str(tmp_path / "workspace" / ".skills"))


def _sandbox(tmp_path: Path, client: FakeAgentCoreClient | None = None) -> RuntimeSandbox:
    workdir = tmp_path / "workspace"
    workdir.mkdir(exist_ok=True)
    return RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_key=f"notices-{uuid.uuid4().hex}",
        workdir=str(workdir),
        client=client or FakeAgentCoreClient(workdir=str(workdir)),
    )


def _agent(sandbox: RuntimeSandbox, skills: Any, *commands: str) -> Agent:
    turns = [
        tool_use_events("shell", f"t{i}", json.dumps({"command": c})) for i, c in enumerate(commands)
    ]
    return Agent(
        model=ScriptedModel(*turns, text_events("done")),
        sandbox=sandbox,
        tools=[shell],
        plugins=[skills, SandboxEnvironment(), SandboxNotices(sandbox)],
        callback_handler=None,
    )


def _tool_results(agent: Agent) -> list[str]:
    return [
        json.dumps(block["toolResult"]["content"])
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


async def test_a_wake_time_prune_is_in_the_first_shell_result_after_the_skills_push(
    tmp_path, monkeypatch
):
    """Through ``build_agent``: wake, skills push, environment probe, then the model."""
    monkeypatch.setattr(scripts_module, "SANDBOX_WORKSPACE_PRUNE_FRACTION", 0)  # anything is "full"
    repo = tmp_path / "workspace" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    sandbox = _sandbox(tmp_path)
    model = ScriptedModel(
        tool_use_events("shell", "t1", json.dumps({"command": "ls repo"})), text_events("done")
    )
    agent = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=model,
        sandbox=sandbox,
        mcp_clients=[],
    )
    await agent.invoke_async("go")
    client: FakeAgentCoreClient = sandbox._client  # type: ignore[assignment]
    assert any("skills-" in c for c in client.commands), "the skills push ran through the sandbox"
    first = _tool_results(agent)[0]
    assert "repo/node_modules" in first and "were deleted" in first
    assert not (repo / "node_modules").exists()


async def test_a_mid_run_platform_fault_is_reported_without_replay_or_restart(tmp_path):
    sandbox = _sandbox(tmp_path)
    skills = build_skills_plugin(sandbox)
    client: FakeAgentCoreClient = sandbox._client  # type: ignore[assignment]
    agent = _agent(sandbox, skills, "echo one", "echo two")
    real = client.invoke_agent_runtime_command
    calls = {"n": 0}

    def burst_on_the_second_shell_call(**kwargs: Any) -> dict[str, Any]:
        if "echo two" in kwargs["body"]["command"]:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise FakeClientError("RuntimeClientError", BAD_GATEWAY)
        return real(**kwargs)

    client.invoke_agent_runtime_command = burst_on_the_second_shell_call  # type: ignore[method-assign]
    await agent.invoke_async("go")
    first, second = _tool_results(agent)
    assert "restarted" not in first
    assert "NOT replayed" in second and "RuntimeClientError" in second
    assert calls["n"] == 1
    assert not client.stops
    assert not sandbox.ready
    results = [
        block["toolResult"]
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    assert results[1]["status"] == "error"


async def test_every_agent_sharing_the_sandbox_gets_each_notice_once(tmp_path):
    sandbox = _sandbox(tmp_path)
    sandbox._add_notice("[strandly-sandbox] something happened")
    parent, child = SandboxNotices(sandbox), SandboxNotices(sandbox)

    class _Event:
        def __init__(self) -> None:
            self.result: dict[str, Any] = {"toolUseId": "x", "status": "success", "content": []}

    for plugin in (parent, child):
        event = _Event()
        await plugin._deliver(event)  # type: ignore[arg-type]
        assert event.result["content"] == [{"text": "[strandly-sandbox] something happened"}]
        again = _Event()
        await plugin._deliver(again)  # type: ignore[arg-type]
        assert again.result["content"] == [], "delivered once per agent"


async def test_a_failed_skills_push_is_retried_then_reported_on_the_first_shell_result(tmp_path):
    sandbox = _sandbox(tmp_path)
    (tmp_path / "workspace" / ".skills").write_text("a file where the directory should be")
    agent = _agent(sandbox, build_skills_plugin(sandbox), "echo one")
    await agent.invoke_async("go")

    client: FakeAgentCoreClient = sandbox._client  # type: ignore[assignment]
    assert sum("tar -xzf" in c for c in client.commands) == 2, "pushed, then retried once"
    first = _tool_results(agent)[0]
    assert "skill files" in first and "may be missing." in first and "one" in first


async def test_uncertain_shell_partial_output_reaches_only_its_tool_result(tmp_path):
    sandbox = _sandbox(tmp_path)
    client = sandbox._client
    original = client.invoke_agent_runtime_command

    def fail_with_partial_output(**kwargs):
        if "SIDE_EFFECT" in kwargs["body"]["command"]:
            return {"stream": iter([
                {"chunk": {"contentDelta": {"stdout": "partial-out", "stderr": "partial-err"}}},
                {"chunk": {"contentStop": {"exitCode": -1, "status": "COMPLETED"}}},
            ])}
        return original(**kwargs)

    client.invoke_agent_runtime_command = fail_with_partial_output
    agent = _agent(sandbox, build_skills_plugin(sandbox), "echo one", "SIDE_EFFECT")
    await agent.invoke_async("go")
    first, second = _tool_results(agent)
    assert "partial-out" not in first
    assert "partial-out" in second and "partial-err" in second
    assert "unknown" in second and "NOT replayed" in second


async def test_uncertain_output_preview_is_bounded_and_disables_tool_retry(tmp_path):
    from strandly.sandbox.runtime import SandboxExecutionUncertainError

    error = SandboxExecutionUncertainError("diagnostic", stdout="x" * 20000, stderr="partial")
    wrapped = RuntimeError("SDK wrapper")
    wrapped.__cause__ = error

    class Event:
        exception = wrapped
        retry = True
        result = {"toolUseId": "t1", "status": "error", "content": [{"text": "diagnostic"}]}

    event = Event()
    await SandboxNotices(_sandbox(tmp_path))._deliver(event)  # type: ignore[arg-type]
    preview = json.loads(event.result["content"][-1]["text"])
    assert preview["output"] == "x" * 16000
    assert preview["truncated"] is True and preview["exit_code"] is None
    assert len(error.stdout) == 20000
    assert event.retry is False
