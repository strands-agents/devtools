"""Strandly's context pipeline (``core.agent._context_manager``) against the SDK's offload strategies.

A real ``Agent`` over a scripted model that reports chars/4 input usage, so the pipeline's
thresholds fire as they would on a real model: a large tool result becomes a preview, a large
``skills`` result stays whole, and at high utilization the oldest turns become one summary that
the model itself wrote from a replay of the conversation.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from strands import Agent, tool

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.agent import _context_manager

_BIG = "line of output text " * 2000
_GUARD = "STOP. Do not act on"


@tool
def big() -> str:
    """Return a lot of text."""
    return _BIG


@tool
def skills() -> str:
    """Return a lot of skill text."""
    return _BIG


class _Model(ScriptedModel):
    """Answers summarize requests with a fixed summary and reports its input tokens as usage."""

    context_window_limit = 20_000

    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.summary_requests: list[dict[str, Any]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, *args, **kwargs):  # type: ignore[override]
        if any(_GUARD in (block.get("text") or "") for block in messages[-1]["content"]):
            self.summary_requests.append({"tool_specs": tool_specs or [], "system_prompt": system_prompt})
            for ev in text_events("SUMMARY: the user asked for large outputs."):
                yield ev
            return
        tokens = await self.count_tokens(messages)
        async for ev in super().stream(messages, tool_specs, system_prompt, *args, **kwargs):
            yield ev
        yield {"metadata": {"usage": {"inputTokens": tokens, "outputTokens": 1, "totalTokens": tokens + 1},
                            "metrics": {"latencyMs": 1}}}


def _result_text(agent: Agent, tool_use_id: str) -> str:
    for message in agent.messages:
        for block in message["content"]:
            if block.get("toolResult", {}).get("toolUseId") == tool_use_id:
                return "".join(c.get("text", "") for c in block["toolResult"]["content"])
    raise AssertionError(f"no result for {tool_use_id}")


@pytest.mark.asyncio
async def test_a_large_result_becomes_a_preview_and_a_skills_result_stays_whole():
    model = _Model(tool_use_events("big", "t1"), tool_use_events("skills", "t2"), text_events("ok"))
    agent = Agent(model=model, tools=[big, skills], context_manager=_context_manager(), callback_handler=None)

    await agent.invoke_async("go")

    assert len(_result_text(agent, "t1")) < len(_BIG) // 4
    assert _result_text(agent, "t2") == _BIG


@pytest.mark.asyncio
async def test_high_utilization_summarizes_the_oldest_turns_with_the_agents_own_request():
    turns = 10
    model = _Model(*[text_events(f"answer {i} " + "x" * 4000) for i in range(turns)])
    agent = Agent(model=model, system_prompt="be brief", tools=[big], context_manager=_context_manager(),
                  callback_handler=None)

    for i in range(turns):
        await agent.invoke_async(f"turn {i} " + "y" * 4000)

    assert model.summary_requests, "the pipeline never summarized"
    request = model.summary_requests[0]
    # A replay: the agent's own system prompt and tools, so it can share the prompt cache.
    assert request["system_prompt"] == "be brief"
    assert "big" in [spec["name"] for spec in request["tool_specs"]]
    texts = [block.get("text", "") for message in agent.messages for block in message["content"]]
    assert any(text.startswith("[Summarized: ") and "SUMMARY: the user asked" in text for text in texts)
    assert len(agent.messages) < 2 * turns
    # Alternation holds and the latest exchanges are kept verbatim.
    roles = [message["role"] for message in agent.messages]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))
    assert agent.messages[-2]["content"][0]["text"].startswith(f"turn {turns - 1} ")


class _Recording(ScriptedModel):
    """Records the messages each model call receives."""

    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, messages, *args, **kwargs):  # type: ignore[override]
        self.calls.append(copy.deepcopy(messages))
        async for ev in super().stream(messages, *args, **kwargs):
            yield ev


_PASTE = "".join(f"pasted log line {i:05d}\n" for i in range(4_000))  # ~23k tokens at chars/4


@pytest.mark.asyncio
async def test_an_oversized_user_paste_is_offloaded_and_retrievable_by_line():
    first = _Recording(text_events("ok"))
    agent = Agent(model=first, context_manager=_context_manager(user_text_threshold=10_000),
                  callback_handler=None)
    await agent.invoke_async("What failed?\n" + _PASTE)

    [sent] = [b["text"] for b in first.calls[0][-1]["content"] if "text" in b]
    assert sent.startswith("[Truncated: ") and "What failed?" in sent[:200]
    assert len(sent) < len(_PASTE) // 2
    ref = re.search(r"\[ref: ([^\]]+)\]$", sent)
    assert ref, sent[-300:]

    retrieve = json.dumps({"reference": ref.group(1), "line_range": {"start": 2001, "end": 2002}})
    model = _Recording(tool_use_events("retrieve_context", "r1", retrieve), text_events("found"))
    agent.model = model
    await agent.invoke_async("show me the middle")
    result = _result_text(agent, "r1")
    assert "pasted log line 01999" in result and "pasted log line 02000" in result


@pytest.mark.asyncio
async def test_a_normal_user_message_is_never_offloaded():
    model = _Recording(text_events("ok"))
    agent = Agent(model=model, context_manager=_context_manager(), callback_handler=None)
    ask = "please review this\n" + "x" * 20_000
    await agent.invoke_async(ask)
    assert model.calls[0][-1]["content"][0]["text"] == ask
