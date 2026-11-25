"""``SandboxEnvironment``: the harness's environment block, never waking a lazy sandbox."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from strands import Agent
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands_harness.plugins import EnvironmentContext
from strands_harness.plugins import environment as harness_environment

from scripted_model import ScriptedModel, text_events
from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.plugins import environment as environment_module
from strandly.plugins.environment import SandboxEnvironment

SOURCE = "strands:environment:injector"


class LazySandbox(NotASandboxLocalEnvironment):
    """A host sandbox with the lazy contract: ``ready`` flips on the first command."""

    def __init__(self) -> None:
        super().__init__()
        self.ready = False
        self.commands: list[str] = []

    def add_first_use_hook(self, hook: Any) -> None:
        pass

    async def execute_streaming(self, command: str, *args: Any, **kwargs: Any):  # type: ignore[override]
        self.ready = True
        self.commands.append(command)
        async for chunk in super().execute_streaming(command, *args, **kwargs):
            yield chunk


def _blocks(agent: Agent) -> list[str]:
    return [
        block["injected"]["text"]
        for message in agent.messages
        for block in message["content"]
        if "injected" in block and block["injected"]["source"] == SOURCE
    ]


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_an_unwoken_sandbox_gets_the_fixed_block_and_stays_asleep():
    sandbox = LazySandbox()
    agent = Agent(
        model=ScriptedModel(text_events("hi")),
        sandbox=sandbox,
        plugins=[SandboxEnvironment()],
        callback_handler=None,
    )
    agent("hello")

    [block] = _blocks(agent)
    assert "<environment>" in block and "starts on your first shell or file tool call" in block
    assert sandbox.commands == [] and sandbox.ready is False


def test_once_awake_each_user_turn_gets_the_harness_block():
    sandbox = LazySandbox()
    agent = Agent(
        model=ScriptedModel(text_events("one"), text_events("two")),
        sandbox=sandbox,
        plugins=[SandboxEnvironment()],
        callback_handler=None,
    )
    agent("first")
    sandbox.ready = True  # a tool call woke it
    agent("second")

    first, second = _blocks(agent)
    assert "starts on your first" in first
    assert "Platform: " in second and "starts on your first" not in second
    assert sandbox.commands  # probed, through the agent's sandbox


async def test_a_slow_probe_falls_back_to_the_fixed_block(monkeypatch):
    async def hang(_agent: Any, _memo: Any) -> str:
        await asyncio.sleep(10)
        return "never"

    monkeypatch.setattr(environment_module, "_render", hang)
    monkeypatch.setattr(environment_module, "PROBE_TIMEOUT_SECONDS", 0.01)
    agent = Agent(
        model=ScriptedModel(text_events("hi")),
        sandbox=NotASandboxLocalEnvironment(),
        plugins=[SandboxEnvironment()],
        callback_handler=None,
    )
    await agent.invoke_async("hello")

    [block] = _blocks(agent)
    assert "Sandbox: awake" in block


def test_the_harness_render_it_delegates_to_keeps_its_signature():
    assert list(inspect.signature(harness_environment._render).parameters) == ["agent", "memo"]
    assert issubclass(SandboxEnvironment, EnvironmentContext)


async def test_the_built_agent_has_one_environment_and_no_todos(fake_model, tmp_path):
    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    names = [type(p).__name__ for p in agent._plugin_registry._plugins.values()]
    assert names.count("SandboxEnvironment") == 1
    assert "EnvironmentContext" not in names and "Todos" not in names
    assert "todo_write" not in agent.tool_names
