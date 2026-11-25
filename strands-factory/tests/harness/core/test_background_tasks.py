"""Background tools are the SDK's; Strandly wires the policy.

The end-to-end tests drive a **real** agent over a scripted model (no network) because every claim
here is about the SDK's behavior at our seam: which specs carry ``_background_execution``, that a
detached ``shell`` really runs and its result really lands as a synthetic
``strands_manage_background_task`` result, and that the pass this opens reaches
:func:`~strandly.core.events.translate` as a neutral ``continue`` — not as a critic retry.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import FakeModel, text_response_events
from strandly.core.agent import build_agent
from strandly.core.config import Config
from strandly.core.constants import (
    BACKGROUND_MAX_CONCURRENCY,
    BACKGROUND_TASK_TIMEOUT_SECONDS,
    BACKGROUND_TOOL_NAMES,
)
from strandly.core.context import RuntimeContext
from strandly.core.events import REASON_CONTINUE, REASON_START, translate

MANAGE_TOOL = "strands_manage_background_task"
# The SDK's own state key for its background-task snapshots.
SDK_TASKS_STATE_KEY = "strands.background_tasks"
FLAG = "_background_execution"


def _tool_use_events(name: str, inp: dict[str, Any], tool_use_id: str) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(inp)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


class _ScriptedModel(FakeModel):
    """Plays one scripted response per model call and records what each call was given."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # type: ignore[override]
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "specs": {t["name"]: t for t in tool_specs or []},
            }
        )
        for event in self._turns.pop(0) if self._turns else text_response_events("done"):
            yield event


def _flagged(specs: dict[str, Any]) -> set[str]:
    return {
        name
        for name, spec in specs.items()
        if FLAG in spec["inputSchema"]["json"].get("properties", {})
    }


# ---------------------------------------------------------------- wiring through build_agent


@pytest.mark.asyncio
async def test_depth_0_scopes_the_sdk_policy_to_shell_and_always_detaches_subagent(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)  # the depth-0 file session writes under ./sessions
    model = _ScriptedModel([text_response_events("hi")])
    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=model)

    assert agent._background_tasks is not None
    config = dict(agent._background_tasks._config)
    assert [t.tool_name for t in config.pop("always")] == ["subagent"]
    assert config == {
        "agentic": sorted(BACKGROUND_TOOL_NAMES),
        "max_concurrency": BACKGROUND_MAX_CONCURRENCY,
        "timeout": BACKGROUND_TASK_TIMEOUT_SECONDS,
    }
    assert MANAGE_TOOL in agent.tool_names

    # The flag reaches the model on exactly the policy's tools — not `use_github`, `think`, `read`…
    await agent.invoke_async("go")
    assert _flagged(model.calls[0]["specs"]) == set(BACKGROUND_TOOL_NAMES)

    prompt = str(agent.system_prompt)
    assert f"`{FLAG}: true` on `shell`)" in prompt
    assert "`_background: true`" not in prompt
    assert "<background_tasks>" not in prompt


@pytest.mark.asyncio
async def test_a_subagent_gets_no_background_tasks_and_no_prompt_block(tmp_path):
    model = _ScriptedModel([text_response_events("hi")])
    agent = await build_agent(
        Config(values={}),
        RuntimeContext(cwd=str(tmp_path)),
        model=model,
        spawn_depth=1,
        system_prompt="role",
    )
    assert agent._background_tasks is None
    assert MANAGE_TOOL not in agent.tool_names
    await agent.invoke_async("go")
    assert _flagged(model.calls[0]["specs"]) == set()
    assert "Background work" not in str(agent.system_prompt)


@pytest.mark.asyncio
async def test_a_detached_shell_runs_and_its_result_is_delivered_as_a_continuation(
    tmp_path, monkeypatch
):
    """The whole path, end to end: dispatch → ack → turn ends → SDK waits and delivers."""
    monkeypatch.chdir(tmp_path)
    model = _ScriptedModel(
        [
            _tool_use_events("shell", {"command": "echo bg-ran", FLAG: True}, "t1"),
            text_response_events("kicked it off; done for now"),
            text_response_events("saw the result"),
        ]
    )
    agent = await build_agent(Config(values={}), RuntimeContext(cwd=str(tmp_path)), model=model)

    reasons = [
        ev.data["reason"]
        async for ev in translate(agent.stream_async("go", invocation_state={}))
        if ev.kind == "turn_start"
    ]

    assert reasons == [REASON_START, REASON_CONTINUE]
    # Durable history holds the whole story: the dispatch, the ack (no output in it), then the
    # SDK's synthetic get pair carrying the real output.
    history = agent.messages
    dispatch = next(b["toolUse"] for m in history for b in m["content"] if "toolUse" in b)
    assert dispatch["name"] == "shell"
    ack = next(
        b["toolResult"]
        for m in history
        for b in m["content"]
        if b.get("toolResult", {}).get("toolUseId") == "t1"
    )
    assert "Background task dispatched" in ack["content"][0]["text"] and "bg-ran" not in json.dumps(
        ack
    )
    gets = [
        b["toolUse"]
        for m in history
        for b in m["content"]
        if b.get("toolUse", {}).get("name") == MANAGE_TOOL
    ]
    assert len(gets) == 1 and gets[0]["input"]["mode"] == "get"
    result = next(
        b["toolResult"]
        for m in history
        for b in m["content"]
        if b.get("toolResult", {}).get("toolUseId") == gets[0]["toolUseId"]
    )
    assert "bg-ran" in json.dumps(result["content"])
    assert agent.state.get(SDK_TASKS_STATE_KEY) is None  # delivered tasks are dropped from state
