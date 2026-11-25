"""The transcript recorder: one AgentCore Memory event per message (``memory/transcript.py``).

Whole turns run through a real ``strands.Agent`` over the in-memory Memory data plane
(``tests/fake_agentcore.py``, every request validated against the real botocore model) and the
in-memory S3 bucket for the snapshot and the actor marker. Exact request shapes are pinned with
``botocore.stub.Stubber`` on a real ``bedrock-agentcore`` client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import pytest
from botocore.stub import ANY, Stubber
from strands import Agent, tool

from fake_agentcore import (
    ACCESS_DENIED,
    INTERNAL,
    MEMORY_ID,
    THROTTLED,
    FakeAgentCoreClient,
    real_agentcore_client,
)
from fake_s3 import ACCESS_DENIED as S3_DENIED
from fake_s3 import FakeS3Client, s3_storage
from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core import constants
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.session_store_reader import actor_marker_key, snapshot_key
from strandly.memory import transcript as transcript_mod
from strandly.memory.scopes import memory_actor, memory_session_id
from strandly.memory.session import build_session_manager
from strandly.memory.session_store import FailClosedStorage
from strandly.memory.transcript import (
    TranscriptRecorder,
    build_transcript_recorder,
    event_message,
    transcript_event,
)
from strandly.ops import metrics

TOKEN = "SESSION_STORAGE_FAILURE"
BUCKET = "strandly-test-sessions"


@pytest.fixture
def emitted(monkeypatch, capsys) -> Any:
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "StrandlyTest")

    def read() -> list[dict[str, Any]]:
        docs = []
        for line in capsys.readouterr().out.splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and "_aws" in doc:
                docs.append(doc)
        return docs

    return read


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(transcript_mod, "_BACKOFF_SECONDS", (0.0, 0.0, 0.0))


@tool
def lookup(query: str) -> str:
    """Look something up."""
    return f"found {query}"


def _store(fake_s3: FakeS3Client) -> FailClosedStorage:
    return FailClosedStorage(s3_storage(fake_s3), backend=f"s3://{BUCKET}")


def _agent(
    memory: Any,
    storage: Any,
    ctx: RuntimeContext,
    model: Any,
    *,
    tools: list[Any] | None = None,
    plugins: list[Any] | None = None,
) -> Agent:
    sid = ctx.session_id or ""
    recorder = TranscriptRecorder(
        memory,
        memory_id=MEMORY_ID,
        actor_id=memory_actor(ctx),
        session_id=sid,
        storage=storage,
    )
    return Agent(
        model=model,
        session_manager=build_session_manager(Config(values={}), ctx, storage),
        storage=storage,
        tools=tools or [],
        plugins=[recorder, *(plugins or [])],
        callback_handler=None,
    )


def _messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda e: (e["eventTimestamp"], e["metadata"]["seq"]["stringValue"]))
    return [m for m in (event_message(e) for e in ordered) if m is not None]


# --- whole turns -------------------------------------------------------------------------------


def test_a_turn_with_a_tool_call_records_every_message_verbatim_and_in_order():
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    ctx = RuntimeContext(session_id="gh-o-r-issue-3")
    agent = _agent(
        memory,
        _store(s3),
        ctx,
        ScriptedModel(tool_use_events("lookup", "t1", '{"query": "x"}'), text_events("the answer")),
        tools=[lookup],
    )
    agent("what is x?")

    events = memory.session_events("team", "gh-o-r-issue-3")
    assert len(events) == 4  # user prompt, tool use, tool result, final answer
    # Role and content verbatim; a tool message also keeps every other field it was added with.
    assert [(m["role"], m["content"]) for m in _messages(events)] == [
        (m["role"], m["content"]) for m in agent.messages
    ]
    assert _messages(events)[1].get("metadata") == agent.messages[1].get("metadata")
    kinds = [(e["metadata"]["kind"]["stringValue"], e.get("_extractionMode")) for e in events]
    assert kinds == [("text", None), ("message", "SKIP"), ("message", "SKIP"), ("text", None)]
    # Plain conversation text is a conversational payload — what extraction learns from.
    assert events[0]["payload"] == [{"conversational": {"content": {"text": "what is x?"}, "role": "USER"}}]
    assert events[3]["payload"][0]["conversational"]["role"] == "ASSISTANT"
    # The actor marker sits next to the snapshot.
    marker = json.loads(s3.objects[actor_marker_key("gh-o-r-issue-3")][0])
    assert marker == {"actor": "team", "sessionId": "gh-o-r-issue-3"}
    assert snapshot_key("gh-o-r-issue-3") in s3.objects


def test_the_users_turn_is_recorded_apart_from_the_environment_the_model_sees():
    from strandly.plugins.environment import SandboxEnvironment

    seen: list[str] = []

    class Recording(ScriptedModel):
        async def stream(self, messages: Any = None, *args: Any, **kwargs: Any):  # type: ignore[override]
            seen.append(json.dumps(messages))
            async for ev in super().stream(messages, *args, **kwargs):
                yield ev

    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    ctx = RuntimeContext(session_id="gh-o-r-issue-4")
    agent = _agent(memory, _store(s3), ctx, Recording(text_events("hi")), plugins=[SandboxEnvironment()])
    agent("what is x?")

    assert "<environment>" in seen[0]
    # The user's words are the conversation (extraction's input); the block is a context event.
    events = sorted(memory.session_events("team", "gh-o-r-issue-4"), key=lambda e: e["metadata"]["seq"]["stringValue"])
    assert events[0]["payload"] == [{"conversational": {"content": {"text": "what is x?"}, "role": "USER"}}]
    [context] = [e for e in events if e["metadata"]["kind"]["stringValue"] == "context"]
    [block] = event_message(context)["content"]
    assert block["injected"]["source"] == "strands:environment:injector"


def test_a_slack_dm_is_recorded_under_the_person_and_never_under_team():
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    sid = "slack-T01-D02-1712345678.123456"
    ctx = RuntimeContext(session_id=sid, event={"slackChannelId": "D02", "slackUserId": "U0ABC", "triggerSource": "slack:U0ABC"})
    _agent(memory, _store(s3), ctx, ScriptedModel(text_events("hi")))("hello")

    msid = memory_session_id(sid)
    assert "." not in msid
    assert len(memory.session_events("slack-user-U0ABC", msid)) == 2
    assert all(actor != "team" for (_m, actor, _s) in memory.events)
    assert json.loads(s3.objects[actor_marker_key(sid)][0])["actor"] == "slack-user-U0ABC"


def test_events_written_by_two_processes_read_back_in_order():
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    ctx = RuntimeContext(session_id="portal-u-1")
    _agent(memory, _store(s3), ctx, ScriptedModel(text_events("one")))("first")
    again = _agent(memory, _store(s3), ctx, ScriptedModel(text_events("two")))
    again("second")
    texts = [m["content"][0]["text"] for m in _messages(memory.session_events("team", "portal-u-1"))]
    assert texts == ["first", "one", "second", "two"]
    seqs = [e["metadata"]["seq"]["stringValue"] for e in memory.session_events("team", "portal-u-1")]
    assert seqs == sorted(seqs)


# --- failure semantics -------------------------------------------------------------------------


def test_a_failed_event_write_is_logged_and_counted_and_the_turn_goes_on(caplog, emitted):
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    memory.fail("CreateEvent", ACCESS_DENIED)
    ctx = RuntimeContext(session_id="gh-o-r-pull-9")
    agent = _agent(memory, _store(s3), ctx, ScriptedModel(text_events("still answered")))
    with caplog.at_level(logging.ERROR):
        result = agent("question")
    assert "still answered" in str(result)
    assert f"{TOKEN} op=transcript" in caplog.text
    assert "code=AccessDeniedException" in caplog.text
    failed = [d for d in emitted() if metrics.TRANSCRIPT_WRITE_FAILED in d]
    assert len(failed) == 2  # both messages, each once
    # A non-retryable error is not retried.
    assert sum(op == "CreateEvent" for op, _ in memory.calls) == 2
    # The snapshot is untouched by the transcript failure.
    assert snapshot_key("gh-o-r-pull-9") in s3.objects


def test_a_throttled_write_is_retried_and_lands():
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    memory.fail("CreateEvent", THROTTLED, times=2)
    _agent(memory, _store(s3), RuntimeContext(session_id="s-retry"), ScriptedModel(text_events("ok")))("q")
    assert len(memory.session_events("team", "s-retry")) == 2
    assert sum(op == "CreateEvent" for op, _ in memory.calls) == 4  # 2 throttled + 2 landed


def test_retries_are_bounded(caplog, emitted):
    memory = FakeAgentCoreClient()
    memory.fail("CreateEvent", INTERNAL)
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id="s")
    agent = Agent(model=ScriptedModel(text_events("a")), plugins=[recorder], callback_handler=None)
    agent("q")
    attempts = sum(op == "CreateEvent" for op, _ in memory.calls)
    assert attempts == 2 * constants.TRANSCRIPT_WRITE_ATTEMPTS
    assert len([d for d in emitted() if metrics.TRANSCRIPT_WRITE_FAILED in d]) == 2


def test_the_retry_budget_stops_a_slow_failure(monkeypatch):
    memory = FakeAgentCoreClient()
    memory.fail("CreateEvent", THROTTLED)
    monkeypatch.setattr(transcript_mod, "_BACKOFF_SECONDS", (10.0, 10.0))
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id="s")
    asyncio.run(recorder._send({"memoryId": MEMORY_ID}, "seq"))
    assert len(memory.calls) == 1  # the next pause would pass the budget: give up now


def test_a_failed_actor_marker_does_not_fail_the_turn_and_is_retried(caplog):
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    s3.fail_when("put_object", S3_DENIED, lambda key: key.endswith(constants.SESSION_STORE_ACTOR_MARKER))
    store = _store(s3)
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id="s-m", storage=store)
    agent = Agent(model=ScriptedModel(text_events("a")), plugins=[recorder], callback_handler=None)
    agent("q")
    assert len(memory.session_events("team", "s-m")) == 2  # events still written
    marker_puts = [
        kw for op, kw in s3.calls if op == "put_object" and kw["Key"].endswith("memory-actor.json")
    ]
    assert len(marker_puts) == 2  # tried again on the next message


# --- event layout ------------------------------------------------------------------------------


def test_the_exact_create_event_request_over_a_real_client():
    client = real_agentcore_client()
    recorder = TranscriptRecorder(client, memory_id=MEMORY_ID, actor_id="team", session_id="mcp-a.b")
    message = {"role": "assistant", "content": [{"text": "hello"}, {"text": "world"}]}
    with Stubber(client) as stub:
        stub.add_response(
            "create_event",
            {
                "event": {
                    "memoryId": MEMORY_ID,
                    "actorId": "team",
                    "sessionId": memory_session_id("mcp-a.b"),
                    "eventId": "1#ab",
                    "eventTimestamp": datetime(2025, 1, 1),
                    "payload": [],
                }
            },
            {
                "memoryId": MEMORY_ID,
                "actorId": "team",
                "sessionId": memory_session_id("mcp-a.b"),
                "eventTimestamp": ANY,
                "payload": [{"conversational": {"content": {"text": "hello\nworld"}, "role": "ASSISTANT"}}],
                "metadata": {
                    "seq": {"stringValue": ANY},
                    "kind": {"stringValue": "text"},
                    "role": {"stringValue": "assistant"},
                },
            },
        )

        class _Event:
            pass

        ev = _Event()
        ev.message = message  # type: ignore[attr-defined]
        asyncio.run(recorder._on_message_added(ev))  # type: ignore[arg-type]
        stub.assert_no_pending_responses()


def test_long_text_is_split_across_items_and_reassembled():
    text = "a" * (constants.TRANSCRIPT_ITEM_MAX_CHARS * 2 + 5)
    fields = transcript_event({"role": "user", "content": [{"text": text}]}, seq="1")
    assert [len(i["conversational"]["content"]["text"]) for i in fields["payload"]] == [
        constants.TRANSCRIPT_ITEM_MAX_CHARS,
        constants.TRANSCRIPT_ITEM_MAX_CHARS,
        5,
    ]
    assert event_message({"payload": fields["payload"]}) == {"role": "user", "content": [{"text": text}]}


def test_text_past_the_event_limit_is_cut_with_an_explicit_marker():
    limit = constants.TRANSCRIPT_ITEM_MAX_CHARS * constants.TRANSCRIPT_EVENT_MAX_ITEMS
    text = "b" * (limit + 12_345)
    fields = transcript_event({"role": "assistant", "content": [{"text": text}]}, seq="1")
    assert len(fields["payload"]) == constants.TRANSCRIPT_EVENT_MAX_ITEMS
    back = event_message({"payload": fields["payload"]})["content"][0]["text"]  # type: ignore[index]
    assert len(back) <= limit
    dropped = len(text) - back.index("\n[transcript:")
    assert back.endswith(f"[transcript: {dropped} more characters not recorded]")


def test_a_large_tool_result_is_split_into_json_parts_and_reassembled():
    big = "r" * (constants.TRANSCRIPT_ITEM_MAX_CHARS + 10)
    message = {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "status": "success",
                                                          "content": [{"text": big}]}}]}
    fields = transcript_event(message, seq="1")
    assert fields["extractionMode"] == "SKIP"
    assert len(fields["payload"]) == 2 and all("json" in i for i in fields["payload"])
    assert event_message({"payload": fields["payload"]}) == message


def test_an_image_is_recorded_verbatim_and_never_extracted():
    message = {"role": "user", "content": [{"text": "see"}, {"image": {"format": "png",
                                                                      "source": {"bytes": b"\x89PNG"}}}]}
    fields = transcript_event(message, seq="1")
    assert fields["extractionMode"] == "SKIP"
    back = event_message({"payload": fields["payload"]})
    assert back is not None and back["content"][0] == {"text": "see"}
    assert "image" in back["content"][1]


def test_an_oversized_json_message_is_clipped_with_a_marker():
    limit = constants.TRANSCRIPT_ITEM_MAX_CHARS * constants.TRANSCRIPT_EVENT_MAX_ITEMS
    message = {"role": "user", "content": [{"toolResult": {"toolUseId": "t", "status": "success",
                                                          "content": [{"text": "z" * (limit + 1)}]}}]}
    back = event_message({"payload": transcript_event(message, seq="1")["payload"]})
    text = back["content"][0]["toolResult"]["content"][0]["text"]  # type: ignore[index]
    assert len(text) < constants.TRANSCRIPT_STRING_CLIP_CHARS + 100
    assert "more characters not recorded]" in text


def _reasoned_reply(text: str) -> list[dict[str, Any]]:
    """A model stream for an adaptive-thinking reply: a reasoning block, then the text."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "think it over"}}}},
        {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig-1"}}}},
        {"contentBlockStop": {}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]


def test_a_reply_with_reasoning_is_extracted_and_reads_back_verbatim():
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    ctx = RuntimeContext(session_id="gh-o-r-issue-4")
    agent = _agent(memory, _store(s3), ctx, ScriptedModel(_reasoned_reply("we deploy on Fridays")))
    agent("when do we deploy?")

    events = memory.session_events("team", "gh-o-r-issue-4")
    reply, message = events[1], agent.messages[1]
    assert [next(iter(b)) for b in message["content"]] == ["reasoningContent", "text"]
    # Extraction is on and sees the reply text; the full message rides along as a blob item.
    assert reply.get("_extractionMode") is None
    assert reply["metadata"]["kind"]["stringValue"] == "text"
    assert reply["payload"][0] == {
        "conversational": {"content": {"text": "we deploy on Fridays"}, "role": "ASSISTANT"}
    }
    assert [next(iter(i)) for i in reply["payload"]] == ["conversational", "blob"]
    assert _messages(events)[1] == message


def test_text_and_reasoning_layout():
    message = {
        "role": "assistant",
        "content": [
            {"text": "first"},
            {"reasoningContent": {"reasoningText": {"text": "hm", "signature": "s"}}},
            {"text": "second"},
        ],
    }
    fields = transcript_event(message, seq="1")
    assert "extractionMode" not in fields
    assert fields["payload"] == [
        {"conversational": {"content": {"text": "first\nsecond"}, "role": "ASSISTANT"}},
        {"blob": {"message": message}},
    ]
    assert event_message({"payload": fields["payload"]}) == message


def test_a_long_reply_with_reasoning_splits_both_parts_and_reads_back():
    text = "t" * (constants.TRANSCRIPT_ITEM_MAX_CHARS + 7)
    message = {
        "role": "assistant",
        "content": [{"reasoningContent": {"reasoningText": {"text": "hm"}}}, {"text": text}],
    }
    fields = transcript_event(message, seq="1")
    kinds = [next(iter(i)) for i in fields["payload"]]
    assert kinds == ["conversational", "conversational", "blob", "blob"]
    assert "".join(i["conversational"]["content"]["text"] for i in fields["payload"][:2]) == text
    assert event_message({"payload": fields["payload"]}) == message


@pytest.mark.parametrize(
    "content",
    [
        [{"reasoningContent": {"reasoningText": {"text": "hm"}}}],
        [{"reasoningContent": {"reasoningText": {"text": "hm"}}}, {"text": ""}],
        [
            {"reasoningContent": {"reasoningText": {"text": "hm"}}},
            {"text": "calling"},
            {"toolUse": {"toolUseId": "t", "name": "n", "input": {}}},
        ],
    ],
)
def test_reasoning_without_text_or_with_tools_stays_skipped(content):
    message = {"role": "assistant", "content": content}
    fields = transcript_event(message, seq="1")
    assert fields["extractionMode"] == "SKIP"
    assert fields["metadata"]["kind"]["stringValue"] == "message"
    assert fields["payload"] == [{"json": {"content": {"message": message}}}]


def test_plain_text_has_no_blob():
    fields = transcript_event({"role": "user", "content": [{"text": "a"}, {"text": "b"}]}, seq="1")
    assert fields["payload"] == [{"conversational": {"content": {"text": "a\nb"}, "role": "USER"}}]
    assert "extractionMode" not in fields


def test_every_layout_passes_the_real_request_shape():
    memory = FakeAgentCoreClient()  # validates each request against the botocore model
    recorder = TranscriptRecorder(memory, memory_id=MEMORY_ID, actor_id="team", session_id="s")
    samples = [
        {"role": "user", "content": [{"text": "x" * (constants.TRANSCRIPT_ITEM_MAX_CHARS * 3)}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "n", "input": {}}}]},
        {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "hm"}}}]},
        {"role": "assistant", "content": [{"reasoningContent": {"redactedContent": b"\x00\x01"}},
                                          {"text": "ok"}]},
        {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "hm"}}},
                                          {"text": "y" * (constants.TRANSCRIPT_ITEM_MAX_CHARS * 2)}]},
        {"role": "assistant", "content": []},
        {"role": "user", "content": [{"text": ""}]},
    ]

    class _Event:
        def __init__(self, message: dict[str, Any]) -> None:
            self.message = message

    for message in samples:
        asyncio.run(recorder._on_message_added(_Event(message)))  # type: ignore[arg-type]
    assert len(memory.session_events("team", "s")) == len(samples)


def test_no_memory_means_no_recorder(monkeypatch):
    # No real boto3 client: CI has no AWS region, and this test is about the Memory id gate only.
    monkeypatch.setattr(transcript_mod, "_client", lambda _profile, _region: FakeAgentCoreClient())
    ctx = RuntimeContext(session_id="s")
    assert build_transcript_recorder(Config(values={}), ctx, "s", None) is None
    built = build_transcript_recorder(Config(values={"AGENTCORE_MEMORY_ID": MEMORY_ID}), ctx, "s", None)
    assert built is not None and built.actor_id == "team"


@pytest.mark.asyncio
async def test_build_agent_records_a_top_level_session_and_never_a_subagent(text_model, tmp_path, monkeypatch):
    from strandly.core.agent import build_agent

    memory = FakeAgentCoreClient()
    monkeypatch.setattr(transcript_mod, "_client", lambda _profile, _region: memory)
    cfg = Config(values={"AGENTCORE_MEMORY_ID": MEMORY_ID, "AWS_REGION": "us-west-2"})
    ctx = RuntimeContext(session_id="gh-o-r-issue-77", cwd=str(tmp_path))
    agent = await build_agent(cfg, ctx, model=text_model("built answer"))
    await agent.invoke_async("hello")
    messages = _messages(memory.session_events("team", "gh-o-r-issue-77"))
    texts = [m["content"][0]["text"] for m in messages if "text" in m["content"][0]]
    assert texts == ["hello", "built answer"]

    sub = await build_agent(cfg, RuntimeContext(cwd=str(tmp_path)), model=text_model("x"), spawn_depth=1)
    await sub.invoke_async("sub task")
    assert all(s == "gh-o-r-issue-77" for (_m, _a, s) in memory.events)


# --- events too large for CreateEvent ----------------------------------------------------------


@tool
def dump() -> str:
    """Print a lot."""
    return "".join(f"{i}\n" for i in range(1, 25_001))  # ~130 KB, like `seq 1 20000` and more


def test_an_oversized_tool_result_is_stored_whole_and_recorded_as_a_preview(emitted):
    from strandly.core.session_ids import transcript_payload_key

    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    ctx = RuntimeContext(session_id="portal-a-big")
    agent = _agent(memory, _store(s3), ctx, ScriptedModel(tool_use_events("dump", "d1"), text_events("ok")),
                   tools=[dump])
    agent("dump it")

    assert not [d for d in emitted() if metrics.TRANSCRIPT_WRITE_FAILED in d]
    events = memory.session_events("team", "portal-a-big")
    [big] = [e for e in events if "offloaded" in e["metadata"]]
    assert big["metadata"]["kind"]["stringValue"] == "message" and big["_extractionMode"] == "SKIP"
    full = json.loads(s3.objects[transcript_payload_key("portal-a-big", big["metadata"]["seq"]["stringValue"])][0])
    result = event_message({"payload": full})["content"][0]["toolResult"]  # type: ignore[index]
    assert result["content"][0]["text"] == dump._tool_func()
    preview = event_message(big)["content"][0]["toolResult"]["content"][0]["text"]  # type: ignore[index]
    assert preview.startswith("1\n2\n3\n") and "more characters not recorded" in preview
    assert len(_messages(events)) == 4


def test_when_the_payload_cannot_be_stored_the_preview_is_still_recorded(caplog, emitted):
    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    s3.fail_when("put_object", S3_DENIED, lambda key: "/transcript/" in key)
    ctx = RuntimeContext(session_id="portal-a-big2")
    agent = _agent(memory, _store(s3), ctx, ScriptedModel(tool_use_events("dump", "d1"), text_events("ok")),
                   tools=[dump])
    agent("dump it")

    events = memory.session_events("team", "portal-a-big2")
    assert len(events) == 4 and not any("offloaded" in e["metadata"] for e in events)
    assert not [d for d in emitted() if metrics.TRANSCRIPT_WRITE_FAILED in d]


def test_a_preview_of_very_many_blocks_keeps_only_the_role():
    from strandly.memory.transcript import payload_bytes, preview_event

    message = {"role": "user", "content": [{"text": "y" * 500} for _ in range(1_000)], "tracking_id": "t-1"}
    fields = preview_event(message, seq="1", kind=None, stored_bytes=600_000)
    assert payload_bytes(fields["payload"]) < 2_000
    assert fields["metadata"]["offloaded"] == {"stringValue": "600000"}
    assert fields["metadata"]["tid"] == {"stringValue": "t-1"}
    assert fields["extractionMode"] == "SKIP"


def test_every_message_event_names_its_tracking_id():
    fields = transcript_event({"role": "user", "content": [{"text": "hi"}], "tracking_id": "abc"}, seq="1")
    assert fields["metadata"]["tid"] == {"stringValue": "abc"}
    assert "tid" not in transcript_event({"role": "user", "content": [{"text": "hi"}]}, seq="1")["metadata"]


def test_injected_blocks_are_recorded_once_per_message_and_source():
    from strands.vended_plugins.context_injector import ContextInjector

    memory, s3 = FakeAgentCoreClient(), FakeS3Client()
    ctx = RuntimeContext(session_id="portal-a-ctx")
    model = ScriptedModel(tool_use_events("lookup", "t1", '{"query": "x"}'), text_events("a1"), text_events("a2"))
    agent = _agent(memory, _store(s3), ctx, model, tools=[lookup],
                   plugins=[ContextInjector(lambda _c: "<thread>ctx</thread>", name="gh:injector")])
    agent("first")
    agent("second")

    events = sorted(memory.session_events("team", "portal-a-ctx"), key=lambda e: e["metadata"]["seq"]["stringValue"])
    contexts = [e for e in events if e["metadata"]["kind"]["stringValue"] == "context"]
    # One per user ask (userTurn), none for the tool-result message or the second model call.
    assert len(contexts) == 2
    asks = [e for e in events if e["metadata"]["kind"]["stringValue"] == "text" and e["metadata"]["role"]["stringValue"] == "user"]
    assert [c["metadata"]["tid"] for c in contexts] == [a["metadata"]["tid"] for a in asks]
    assert event_message(contexts[0]) == {
        "role": "user",
        "content": [{"injected": {"text": "<thread>ctx</thread>", "source": "gh:injector"}}],
        "tracking_id": agent.messages[0]["tracking_id"],
    }
