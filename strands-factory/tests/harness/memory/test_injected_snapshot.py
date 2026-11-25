"""What injected context leaves in a saved session snapshot.

``ContextInjector`` writes ``injected`` blocks into the conversation, and the snapshot keeps them.
This drives a real ``Agent`` with Strandly's session manager over ``fake_s3`` and the plugins that
add context to a turn: the environment, the caller context and long-term memory recall, once each
on the ask and never on a tool-result message. The model reads only the latest user message's
injected text.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from strands import Agent

import strandly.memory.long_term as long_term
from fake_agentcore import MEMORY_ID, FakeAgentCoreClient
from fake_s3 import BUCKET, FakeS3Client, s3_storage
from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.session_store_reader import snapshot_key, snapshot_messages
from strandly.memory.long_term import build_memory_manager
from strandly.memory.session import build_session_manager
from strandly.memory.session_store import FailClosedStorage
from strandly.plugins.caller_context import CALLER_CONTEXT_STATE_KEY, CallerContext
from strandly.plugins.environment import SandboxEnvironment
from strandly.plugins.memory_recall import MemoryRecall

SID = "gh-o-r-issue-7"
CONFIG = Config(values={"AGENTCORE_MEMORY_ID": MEMORY_ID, "AWS_REGION": "us-west-2"})
RECALLED = "the build uses uv"


@pytest.fixture
def plane(monkeypatch) -> FakeAgentCoreClient:
    fake = FakeAgentCoreClient()
    monkeypatch.setattr(long_term, "_memory_client", lambda _p, _r: SimpleNamespace(gmdp_client=fake))
    now = datetime.now(timezone.utc)
    fake.records.append(
        {
            "memoryRecordId": f"mem-{0:040d}",
            "content": {"text": RECALLED},
            "memoryStrategyId": "facts-0123456789",
            "namespaces": ["/facts/team/"],
            "createdAt": now,
            "timestamp": now,
        }
    )
    return fake


class _Recording(ScriptedModel):
    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, messages, *args, **kwargs):  # type: ignore[override]
        self.calls.append(messages)
        async for ev in super().stream(messages, *args, **kwargs):
            yield ev


def _agent(fake: FakeS3Client, model: Any) -> Agent:
    storage = FailClosedStorage(s3_storage(fake), backend=f"s3://{BUCKET}")
    ctx = RuntimeContext(session_id=SID)
    manager = build_memory_manager(CONFIG, ctx)
    return Agent(
        model=model,
        session_manager=build_session_manager(CONFIG, ctx, storage),
        storage=storage,
        context_manager="auto",
        plugins=[SandboxEnvironment(), CallerContext(), manager, MemoryRecall(manager)],
        callback_handler=None,
    )


def _saved(fake: FakeS3Client) -> list[dict[str, Any]]:
    return snapshot_messages(fake.objects[snapshot_key(SID)][0])


def _injected(message: dict[str, Any]) -> list[str]:
    return [block["injected"]["source"] for block in message["content"] if "injected" in block]


def test_snapshot_keeps_the_ask_blocks_and_none_on_tool_results(plane):
    fake = FakeS3Client()
    model = _Recording(
        tool_use_events("no_such_tool", "t1", json.dumps({})),
        tool_use_events("no_such_tool", "t2", json.dumps({})),
        text_events("done"),
    )
    agent = _agent(fake, model)
    agent("what does the build use", invocation_state={CALLER_CONTEXT_STATE_KEY: {"view": "triage"}})

    saved = _saved(fake)
    assert [m["role"] for m in saved] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert saved[0]["content"][0] == {"text": "what does the build use"}
    assert _injected(saved[0]) == [
        "strands:environment:injector",
        "caller_context:injector",
        "memory-recall:injector",
    ]
    assert _injected(saved[2]) == [] and _injected(saved[4]) == []
    # Memory recall is stored on the ask it was recalled for, like the caller context.
    assert RECALLED in str(model.calls[0][-1])
    assert RECALLED in json.dumps(saved[0]) and RECALLED not in json.dumps(saved[1:])

    # Each call sends only the latest user message's injected text.
    last = json.dumps(model.calls[-1])
    assert "<environment>" not in last and "Caller context" not in last and RECALLED not in last


def test_a_restored_session_does_not_write_its_blocks_again(plane):
    fake = FakeS3Client()
    agent = _agent(fake, _Recording(text_events("first")))
    agent("turn one", invocation_state={CALLER_CONTEXT_STATE_KEY: {"view": "triage"}})

    model = _Recording(text_events("second"))
    restored = _agent(fake, model)
    restored("turn two")

    saved = _saved(fake)
    env = "strands:environment:injector"
    assert [_injected(m) for m in saved] == [[env, "caller_context:injector"], [], [env], []]
    assert "Caller context" not in json.dumps(model.calls[0])
