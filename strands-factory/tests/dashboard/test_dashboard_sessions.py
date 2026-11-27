"""Dashboard session transcripts: AgentCore Memory events over real API shapes, the S3 session store.

The Memory side is the dashboard's real ``MemoryReader`` over ``fake_agentcore.FakeAgentCoreClient``
(every request and response validated against the installed botocore model); the events are written
by the runtime's real ``TranscriptRecorder``, so writer and reader are tested together. The store is
``fake_s3.FakeS3Client`` (botocore's real ``NoSuchKey`` / ``ClientError``).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fake_agentcore import ACCESS_DENIED, MEMORY_ID, FakeAgentCoreClient
from fake_s3 import BUCKET, FakeS3Client
from strandly.core.session_store_reader import actor_marker_key, snapshot_key
from strandly.memory.scopes import memory_session_id
from strandly.memory.transcript import TranscriptRecorder, event_message, transcript_event

_API_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "api"
sys.path.insert(0, str(_API_DIR))
handler = importlib.import_module("handler")

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


class _Added:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message


class _Store:
    """The recorder's view of the session store: writes straight into the fake bucket."""

    def __init__(self, fake: FakeS3Client) -> None:
        self.fake = fake

    async def write(self, key: str, data: bytes) -> None:
        self.fake.put(key, data)


def _record(memory: FakeAgentCoreClient, fake: FakeS3Client, sid: str, actor: str, messages: list[dict]) -> None:
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id=actor, session_id=sid, storage=_Store(fake))
    for message in messages:
        asyncio.run(recorder._on_message_added(_Added(message)))  # type: ignore[arg-type]


def _text(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"text": text}]}


def _reader(fake: FakeS3Client, memory: FakeAgentCoreClient | None) -> Any:
    mem = handler.MemoryReader(MEMORY_ID, "us-west-2", client=memory) if memory is not None else None
    return handler.SessionStoreReader(BUCKET, client=fake, fallback=mem)


def _snapshot(fake: FakeS3Client, sid: str, texts: list[str]) -> None:
    messages = [_text("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(texts)]
    fake.put(snapshot_key(sid), json.dumps({"data": {"messages": messages}}).encode())


# --- team and DM sessions ----------------------------------------------------------------------


def test_a_team_session_shows_every_message_verbatim_in_order():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "gh-o-r-issue-3"
    _record(memory, fake, sid, "team", [
        _text("user", "what is x?"),
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "lookup", "input": {"q": "x"}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "x=1"}]}}]},
        _text("assistant", "x is 1"),
    ])
    page, truncated = _reader(fake, memory).pagination_transcript(sid)
    assert truncated is False
    assert [(m["role"], m["text"], m["tool_use"]) for m in page] == [
        ("user", "what is x?", False), ("assistant", "", True), ("user", "", False), ("assistant", "x is 1", False),
    ]
    assert page[2]["blocks"][0]["kind"] == "result"
    assert len({m["id"] for m in page}) == 4


def test_a_slack_dm_session_is_found_under_its_person():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "slack-T01-D02-1712345678.123456"
    _record(memory, fake, sid, "slack-user-U0ABC", [_text("user", "remind me"), _text("assistant", "noted")])
    assert json.loads(fake.objects[actor_marker_key(sid)][0])["actor"] == "slack-user-U0ABC"
    assert [m["text"] for m in _reader(fake, memory).transcript(sid)] == ["remind me", "noted"]
    reads = [p for op, p in memory.calls if op == "ListEvents"]
    assert reads[0]["actorId"] == "slack-user-U0ABC" and reads[0]["sessionId"] == memory_session_id(sid)


def test_without_a_marker_the_reader_looks_under_team_and_never_finds_a_dm():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "slack-T01-D09-1712345678.5"
    _record(memory, fake, sid, "slack-user-U0ABC", [_text("user", "private")])
    del fake.objects[actor_marker_key(sid)]
    _snapshot(fake, sid, ["private (snapshot)"])
    assert [m["text"] for m in _reader(fake, memory).transcript(sid)] == ["private (snapshot)"]
    assert [p["actorId"] for op, p in memory.calls if op == "ListEvents"] == ["team"]


def test_a_session_without_events_shows_its_snapshot():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    _snapshot(fake, "portal-u-1", ["hello", "hi"])
    page, truncated = _reader(fake, memory).pagination_transcript("portal-u-1")
    assert [m["text"] for m in page] == ["hello", "hi"] and truncated is False


def test_a_session_with_neither_events_nor_snapshot_is_empty():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    reader = _reader(fake, memory)
    assert reader.pagination_transcript("gh-o-r-issue-99") == ([], False)
    assert reader.transcript("gh-o-r-issue-99") == []
    assert {p["actorId"] for op, p in memory.calls if op == "ListEvents"} == {"team"}


def test_a_failed_transcript_read_shows_the_snapshot_flagged(caplog):
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "gh-o-r-pull-5"
    _record(memory, fake, sid, "team", [_text("user", "q")])
    _snapshot(fake, sid, ["q", "a"])
    memory.fail("ListEvents", ACCESS_DENIED, when=lambda p: p["actorId"] == "team")
    with caplog.at_level(logging.WARNING):
        page, truncated = _reader(fake, memory).pagination_transcript(sid)
    assert [m["text"] for m in page] == ["q", "a"] and truncated is True
    assert "SESSION_STORAGE_FAILURE op=transcript-read" in caplog.text


# --- bounds --------------------------------------------------------------------------------------


def _bulk(memory: FakeAgentCoreClient, sid: str, n: int) -> None:
    for i in range(n):
        memory.create_event(
            memoryId=MEMORY_ID, actorId="team", sessionId=sid,
            eventTimestamp=T0 + timedelta(seconds=i),
            **transcript_event(_text("user", f"m{i}"), seq=f"{i:020d}-{i:06d}"),
        )


def test_an_over_cap_session_keeps_the_newest_messages_and_says_so():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    _bulk(memory, "gh-o-r-1", handler.TRANSCRIPT_MAX_MESSAGES + 30)
    page, truncated = _reader(fake, memory).pagination_transcript("gh-o-r-1")
    assert truncated is True
    assert len(page) == handler.TRANSCRIPT_MAX_MESSAGES
    assert page[-1]["text"] == f"m{handler.TRANSCRIPT_MAX_MESSAGES + 29}"
    assert page[0]["text"] == "m30"  # the oldest 30 dropped


def test_the_time_budget_keeps_what_was_read_and_flags_it(monkeypatch):
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    _bulk(memory, "gh-o-r-2", 250)
    monkeypatch.setattr(handler, "_TRANSCRIPT_READ_BUDGET_SECONDS", -1.0)
    page, truncated = _reader(fake, memory).pagination_transcript("gh-o-r-2")
    assert truncated is True
    assert len(page) == 100  # one page, then the budget was spent
    assert sum(op == "ListEvents" for op, _ in memory.calls if _["actorId"] == "team") == 1


def test_the_truncated_flag_reaches_the_paginated_route():
    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    _bulk(memory, "gh-o-r-3", handler.TRANSCRIPT_MAX_MESSAGES + 1)
    _, truncated = _reader(fake, memory).pagination_transcript("gh-o-r-3")
    _, untruncated = _reader(FakeS3Client(), FakeAgentCoreClient()).pagination_transcript("x")
    assert truncated is True and untruncated is False


# --- parity with the runtime ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "sid", ["gh-o-r-1", "slack-T-D-1.2", "portal-" + "x" * 150, "..", "", "a/b", "unicode-ü", "-x"]
)
def test_the_dashboard_memory_session_id_matches_the_runtime(sid):
    assert handler._memory_session_id(sid) == memory_session_id(sid)


def test_the_dashboard_reads_back_every_layout_the_recorder_writes():
    from strandly.core import constants

    samples = [
        _text("user", "short"),
        _text("assistant", "y" * (constants.TRANSCRIPT_ITEM_MAX_CHARS * 2 + 3)),
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "status": "success",
                                                     "content": [{"text": "r" * (constants.TRANSCRIPT_ITEM_MAX_CHARS + 1)}]}}]},
        {"role": "assistant", "content": [{"text": "a"}, {"toolUse": {"toolUseId": "t", "name": "n", "input": {}}}]},
        {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "hm", "signature": "s"}}},
                                          {"text": "the answer"}]},
        {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "hm"}}},
                                          {"text": "z" * (constants.TRANSCRIPT_ITEM_MAX_CHARS + 1)}]},
    ]
    for message in samples:
        event = {"payload": transcript_event(message, seq="1")["payload"]}
        assert handler._event_message(event) == event_message(event)
    for message in samples[-2:]:  # text + reasoning: the full message, not just its text
        assert handler._event_message({"payload": transcript_event(message, seq="1")["payload"]}) == message


# --- injected context and oversized events, through a real agent -------------------------------


def _live_agent(memory: FakeAgentCoreClient, fake: FakeS3Client, sid: str, model: Any, **kwargs: Any) -> Any:
    """A real ``Agent`` with Strandly's ``CallerContext`` injector and the real recorder."""
    from strands import Agent

    from fake_s3 import s3_storage
    from strandly.memory.session_store import FailClosedStorage
    from strandly.plugins.caller_context import CallerContext

    storage = FailClosedStorage(s3_storage(fake), backend=f"s3://{BUCKET}")
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id=sid, storage=storage)
    return Agent(model=model, plugins=[CallerContext(), recorder], callback_handler=None, **kwargs)


def _lookup_tool() -> Any:
    from strands import tool

    @tool
    def lookup(query: str) -> str:
        """Look something up."""
        return f"found {query}"

    return lookup


def test_an_injected_block_is_recorded_and_shown_as_a_context_chip_on_its_ask():
    from scripted_model import ScriptedModel, text_events, tool_use_events

    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "portal-alice-context1"
    model = ScriptedModel(tool_use_events("lookup", "t1", '{"query": "x"}'), text_events("the answer"))
    agent = _live_agent(memory, fake, sid, model, tools=[_lookup_tool()])
    agent("what is this PR about?", invocation_state={"callerContext": {"url": "https://github.com/o/r/pull/7"}})

    events = memory.session_events("team", memory_session_id(sid))
    kinds = [e["metadata"]["kind"]["stringValue"] for e in sorted(events, key=handler._event_sort_key)]
    assert kinds == ["text", "context", "message", "message", "text"]
    [ask] = [e for e in events if e["metadata"]["role"]["stringValue"] == "user" and e["metadata"]["kind"]["stringValue"] == "text"]
    [ctx] = [e for e in events if e["metadata"]["kind"]["stringValue"] == "context"]
    assert ctx["metadata"]["tid"] == ask["metadata"]["tid"] == {"stringValue": agent.messages[0]["tracking_id"]}
    assert ctx["_extractionMode"] == "SKIP"

    page, _ = _reader(fake, memory).pagination_transcript(sid)
    assert [m["role"] for m in page] == ["user", "assistant", "user", "assistant"]
    chips = [b for b in page[0]["blocks"] if b["kind"] == "context"]
    assert [c["source"] for c in chips] == ["caller_context:injector"]
    assert "https://github.com/o/r/pull/7" in chips[0]["text"]
    assert page[0]["text"] == "what is this PR about?"
    assert not any(b["kind"] == "context" for m in page[1:] for b in m["blocks"])


def test_a_context_event_without_its_message_in_the_read_is_dropped_and_duplicates_merge_once():
    items: list[tuple[str, dict[str, Any], Any]] = [("e1", {"role": "user", "content": [{"text": "hi"}]}, None)]
    block = {"injected": {"text": "ctx", "source": "s"}}
    handler._attach_context(items, None, {"role": "user", "content": [block]})
    handler._attach_context(items, 0, {"role": "user", "content": [block]})
    handler._attach_context(items, 0, {"role": "user", "content": [block]})
    assert items[0][1]["content"] == [{"text": "hi"}, block]


def test_an_oversized_message_is_stored_whole_and_read_back_in_full():
    from scripted_model import ScriptedModel, text_events
    from strandly.core.session_ids import transcript_payload_key

    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "portal-alice-bigpaste"
    paste = "".join(f"log line {i:06d} ü\n" for i in range(12_000))  # ~240 KB, non-ASCII too
    _live_agent(memory, fake, sid, ScriptedModel(text_events("seen")))(paste)

    events = sorted(memory.session_events("team", memory_session_id(sid)), key=handler._event_sort_key)
    ask = events[0]
    assert int(ask["metadata"]["offloaded"]["stringValue"]) > 102_400
    assert len(json.dumps(ask["payload"], separators=(",", ":"))) < 90_000
    stored = json.loads(fake.objects[transcript_payload_key(sid, ask["metadata"]["seq"]["stringValue"])][0])
    assert event_message({"payload": stored}) == {"role": "user", "content": [{"text": paste}]}
    # Memory keeps a preview: the head of the paste, marked as cut.
    assert event_message(ask)["content"][0]["text"].startswith("log line 000000")
    assert "more characters not recorded" in event_message(ask)["content"][0]["text"]

    page, _ = _reader(fake, memory).pagination_transcript(sid)
    assert [m["role"] for m in page] == ["user", "assistant"]
    assert page[0]["text"] == paste


def test_an_oversized_event_whose_payload_is_gone_shows_its_preview():
    from scripted_model import ScriptedModel, text_events
    from strandly.core.session_ids import transcript_payload_key

    memory, fake = FakeAgentCoreClient(), FakeS3Client()
    sid = "portal-alice-gone"
    _live_agent(memory, fake, sid, ScriptedModel(text_events("seen")))("x" * 200_000)
    [ask] = [e for e in memory.session_events("team", memory_session_id(sid)) if "offloaded" in e["metadata"]]
    del fake.objects[transcript_payload_key(sid, ask["metadata"]["seq"]["stringValue"])]

    page, _ = _reader(fake, memory).pagination_transcript(sid)
    assert page[0]["text"].startswith("x" * 2_000) and "more characters not recorded" in page[0]["text"]
