"""``memory.stash``: JSON tool output in the context stash reads back with its real lines."""

from __future__ import annotations

import json

import pytest
from strands import Agent, tool
from strands.storage import InMemoryStorage

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.agent import _context_manager
from strandly.memory.stash import ReadableStash, readable_block, readable_text

_SEQ = "".join(f"{i}\n" for i in range(1, 20_001))


def test_readable_text_unwraps_multi_line_fields_first():
    raw = json.dumps({"output": "a\nb\n", "error": "", "exit_code": 0})
    assert readable_text(raw) == "output:\na\nb\nerror: \nexit_code: 0"


@pytest.mark.parametrize(
    "text",
    ["plain text\nwith lines", '{"output": "one line", "exit_code": 0}', "[1, 2]", "{not json", ""],
)
def test_readable_text_leaves_everything_else(text):
    assert readable_text(text) is None


def test_readable_block_only_touches_text_blocks():
    shell = json.dumps({"text": json.dumps({"output": "x\ny"})}).encode()
    assert json.loads(readable_block(shell)) == {"text": "output:\nx\ny"}
    for other in (b'{"json": {"output": "x\\ny"}}', b"not json", b'{"text": "hi"}'):
        assert readable_block(other) == other


@pytest.mark.asyncio
async def test_only_stash_keys_are_rendered_and_writes_are_verbatim():
    inner = InMemoryStorage()
    store = ReadableStash(inner)
    shell = json.dumps({"text": json.dumps({"output": "x\ny"})}).encode()
    await store.write("context/s/scopes/agent/default/t1_0", shell)
    await store.write("session/s/snapshot.json", shell)
    assert await inner.read("context/s/scopes/agent/default/t1_0") == shell
    assert json.loads(await store.read("context/s/scopes/agent/default/t1_0")) == {"text": "output:\nx\ny"}
    assert await store.read("session/s/snapshot.json") == shell
    assert await store.read("context/missing") is None
    assert await store.list("context/") == ["context/s/scopes/agent/default/t1_0"]
    await store.delete("session/s/snapshot.json")
    assert await inner.read("session/s/snapshot.json") is None


def test_ephemeral_follows_the_wrapped_storage():
    agent = Agent(model=ScriptedModel(), storage=ReadableStash(InMemoryStorage()),
                  context_manager=_context_manager(), callback_handler=None)
    assert agent.context_manager.stash_is_durable is False


@tool
def shell_like() -> dict:
    """Return shell-shaped output."""
    return {"output": _SEQ, "error": "", "exit_code": 0}


def _result_text(agent: Agent, tool_use_id: str) -> str:
    for message in agent.messages:
        for block in message["content"]:
            if block.get("toolResult", {}).get("toolUseId") == tool_use_id:
                return "".join(c.get("text", "") for c in block["toolResult"]["content"])
    raise AssertionError(f"no result for {tool_use_id}")


@pytest.mark.asyncio
async def test_an_offloaded_shell_result_slices_by_line():
    query = json.dumps({"reference": "s1", "line_range": {"start": 15001, "end": 15002}})
    grep = json.dumps({"reference": "s1", "pattern": "^19999$", "context_lines": 0})
    model = ScriptedModel(
        tool_use_events("shell_like", "s1"),
        tool_use_events("retrieve_context", "r1", query),
        tool_use_events("retrieve_context", "r2", grep),
        text_events("done"),
    )
    agent = Agent(model=model, tools=[shell_like], storage=ReadableStash(InMemoryStorage()),
                  context_manager=_context_manager(), callback_handler=None)
    await agent.invoke_async("count")

    assert "[ref: s1]" in _result_text(agent, "s1")
    sliced = _result_text(agent, "r1")
    # Line 1 is "output:", so output line N is line N + 1.
    assert "15000" in sliced and "15001" in sliced and "14999" not in sliced
    assert "19999" in _result_text(agent, "r2")
