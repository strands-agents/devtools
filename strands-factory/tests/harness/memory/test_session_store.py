"""The session store: SnapshotSessionManager over a fail-closed Storage (offline)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from strands import Agent, tool
from strands.session import RepositorySessionManager, SnapshotSessionManager
from strands.storage import LocalFileStorage, S3Storage

import strandly.memory.session_store as session_store
from fake_agentcore import MEMORY_ID, FakeAgentCoreClient
from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.config import Config
from strandly.core.context import RuntimeContext
from strandly.core.session_store_reader import (
    actor_marker_key,
    snapshot_key,
    store_session_id,
)
from strandly.memory.session import (
    FailClosedSnapshotSessionManager,
    build_session_manager,
    session_id_for,
)
from strandly.memory.session_store import (
    FailClosedStorage,
    SessionStorageError,
    build_storage,
)
from strandly.memory.transcript import TranscriptRecorder, event_message

_text = text_events
_tool_call = tool_use_events


BIG = "\n".join(f"line {i}: " + "x" * 80 for i in range(150))  # ~12.9k chars ≈ 3.2k tokens


@tool
def big_report() -> str:
    """Return a large report."""
    return BIG


def _agent(storage: Any, session_id: str, model: Any, memory: Any = None) -> Agent:
    """An agent wired exactly like build_agent's depth-0 session (manager + agent-level storage)."""
    mgr = build_session_manager(
        Config(values={}), RuntimeContext(session_id=session_id), storage
    )
    return Agent(
        model=model,
        tools=[big_report],
        session_manager=mgr,
        storage=storage,
        context_manager="auto",
        plugins=[
            TranscriptRecorder(
                memory if memory is not None else FakeAgentCoreClient(),
                memory_id=MEMORY_ID,
                actor_id="team",
                session_id=session_id,
                storage=storage,
            )
        ],
        callback_handler=None,
    )


def _local_storage() -> FailClosedStorage:
    return build_storage(Config(values={}))


# --- build_storage / build_session_manager ------------------------------------------------------


def test_build_storage_is_local_files_without_a_bucket():
    storage = build_storage(Config(values={}))
    assert isinstance(storage, FailClosedStorage)
    assert isinstance(storage.inner, LocalFileStorage)
    assert storage.backend == "local"


def test_build_storage_is_s3_when_a_bucket_is_configured():
    session_store._s3_storage.cache_clear()
    storage = build_storage(
        Config(values={"STRANDLY_SESSION_BUCKET": "strandly-dev-sessions-1", "AWS_REGION": "us-west-2"})
    )
    assert isinstance(storage.inner, S3Storage)
    assert storage.backend == "s3://strandly-dev-sessions-1"
    # Root keys (dedicated bucket, no prefix) — the layout the bucket's lifecycle rules match.
    assert storage.inner._bucket == "strandly-dev-sessions-1"
    assert storage.inner._prefix == ""
    # A fresh wrapper per build, over one shared S3 client per bucket.
    again = build_storage(
        Config(values={"STRANDLY_SESSION_BUCKET": "strandly-dev-sessions-1", "AWS_REGION": "us-west-2"})
    )
    assert again is not storage and again.inner is storage.inner


def test_build_session_manager_is_a_snapshot_manager_saving_per_message():
    storage = _local_storage()
    mgr = build_session_manager(Config(values={}), RuntimeContext(session_id="gh/o/r/issue/1"), storage)
    assert isinstance(mgr, FailClosedSnapshotSessionManager)
    assert isinstance(mgr, SnapshotSessionManager)
    assert not isinstance(mgr, RepositorySessionManager)
    assert mgr.session_id == "gh-o-r-issue-1"  # sanitized, slash-free
    assert mgr._save_latest_on == "message"
    assert mgr._raw_storage is storage


def test_no_session_id_means_no_session_manager():
    assert session_id_for(RuntimeContext()) is None
    assert build_session_manager(Config(values={}), RuntimeContext(), _local_storage()) is None
    # session_key is honored when there is no explicit id.
    assert session_id_for(RuntimeContext(session_key="k/1")) == "k-1"


def test_store_session_id_never_collapses_the_key_prefix():
    assert store_session_id("..") == "session"
    assert store_session_id(".") == "session"
    assert store_session_id("a..b") == "a..b"
    assert snapshot_key("s-1") == "session/s-1/scopes/agent/default/snapshots/snapshot_latest.json"
    assert actor_marker_key("s/1") == "session/s-1/memory-actor.json"


# --- FailClosedStorage (error paths: tests/harness/memory/test_session_store_s3.py) -------------


def test_storage_is_durable_and_not_pre_namespaced():
    # The SDK must wrap it in its own namespaced view (so every key passes through the wrapper) and
    # treat it as durable (stash by external reference, not inlined into the snapshot).
    storage = _local_storage()
    assert not hasattr(storage, "namespace")
    assert not hasattr(storage, "for_sandbox")
    assert getattr(storage, "_ephemeral", None) is None
    assert getattr(storage, "_namespaced", None) is None


# --- the round trip ---------------------------------------------------------------------------


def _root() -> Path:
    return Path(session_store.LOCAL_SESSION_STORE_DIR)


def test_round_trip_restores_without_duplication_and_the_stash_survives():
    sid = "gh-o-r-pull-7"
    storage = _local_storage()
    agent = _agent(storage, sid, ScriptedModel(_tool_call("big_report", "tu-1"), _text("all done")))
    agent("summarize the report")
    assert agent.context_manager.stash_is_durable is True
    first_messages = agent.messages

    # The big tool result was cut to a preview in the managed history, its original stashed in
    # the store's context/ prefix (durable), and the snapshot written under session/.
    tool_result = next(
        b["toolResult"] for m in first_messages for b in m["content"] if "toolResult" in b
    )
    preview = tool_result["content"][0]["text"]
    assert preview.startswith("[Truncated:") and len(preview) < len(BIG)
    assert (_root() / snapshot_key(sid)).is_file()
    assert any((_root() / "context" / sid).rglob("tu-1_*"))

    # Rebuild from the same store (a cache eviction / cold start): history restored, not replayed.
    rebuilt = _agent(_local_storage(), sid, ScriptedModel())
    assert rebuilt.messages == first_messages
    assert len(rebuilt.messages) == 4  # user, toolUse, toolResult, answer — no duplication

    # retrieve_context resolves the stashed original after the restart.
    ref = "tu-1_0"
    original = asyncio.run(rebuilt.context_manager.stash.retrieve(ref))
    assert original == {"text": BIG}
    # ...and so does the model-facing tool, end to end.
    result = rebuilt.tool.retrieve_context(reference=ref, record_direct_tool_call=False)
    assert result["status"] == "success"
    assert "line 149" in json.dumps(result["content"])

    # A second turn appends; a third rebuild still sees exactly one copy of everything.
    rebuilt("and again")
    again = _agent(_local_storage(), sid, ScriptedModel())
    assert len(again.messages) == 6


def test_one_immutable_checkpoint_per_invocation():
    sid = "s-checkpoints"
    agent = _agent(_local_storage(), sid, ScriptedModel(_text("one"), _text("two")))
    agent("first")
    agent("second")
    history = _root() / f"session/{sid}/scopes/agent/default/snapshots/immutable_history"
    assert len(list(history.glob("snapshot_*.json"))) == 2


def test_the_transcript_log_is_verbatim_and_ordered():
    sid = "s-transcript"
    memory = FakeAgentCoreClient()
    agent = _agent(
        _local_storage(), sid, ScriptedModel(_tool_call("big_report", "tu-9"), _text("answer")), memory
    )
    agent("go")
    events = sorted(memory.session_events("team", sid), key=lambda e: e["metadata"]["seq"]["stringValue"])
    entries = [event_message(e) for e in events]
    roles = [e["role"] for e in entries if e is not None]
    assert roles == ["user", "assistant", "user", "assistant"]
    # Verbatim: the FULL tool result, not the context manager's preview.
    tool_result = entries[2]["content"][0]["toolResult"]  # type: ignore[index]
    assert tool_result["content"][0]["text"] == BIG
    assert entries[3]["content"][0]["text"] == "answer"  # type: ignore[index]
    # …while the snapshot the agent reloads holds the managed history.
    assert (_root() / actor_marker_key(sid)).is_file()


def test_a_corrupt_snapshot_stops_the_build_and_is_never_overwritten(caplog):
    sid = "s-corrupt"
    path = _root() / snapshot_key(sid)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    with pytest.raises(SessionStorageError, match="SESSION_STORAGE_FAILURE: session store restore"):
        _agent(_local_storage(), sid, ScriptedModel(_text("hello")))
    assert "SESSION_STORAGE_FAILURE op=restore" in caplog.text
    assert path.read_text() == "{not json"  # no empty agent ever existed to save over it


def test_a_restore_keeps_the_builds_system_prompt():
    # Snapshots persist the system prompt; the session manager keeps the one this build composed.
    sid = "s-prompt"
    first = _agent(_local_storage(), sid, ScriptedModel(_text("x")))
    first.system_prompt = "OLD PROMPT"
    first("hi")
    again = _agent(_local_storage(), sid, ScriptedModel())
    assert again.system_prompt is None
    assert len(again.messages) == 2


@pytest.mark.asyncio
async def test_build_agent_keeps_its_system_prompt_over_a_restored_one(fake_model, tmp_path):
    from strandly.core.agent import build_agent

    ctx = RuntimeContext(cwd=str(tmp_path), session_id="s-prompt-2")
    storage = build_storage(Config(values={}))
    # Seed a snapshot whose stored system prompt is stale.
    seeded = _agent(storage, "s-prompt-2", ScriptedModel(_text("x")))
    seeded.system_prompt = "STALE"
    seeded("hi")
    agent = await build_agent(Config(values={}), ctx, model=fake_model)
    assert "STALE" not in str(agent.system_prompt)
    assert len(agent.messages) == 2  # but the conversation itself was restored


@pytest.mark.parametrize("marker", ["BEDROCK_AGENTCORE_RUNTIME", "AGENT_OBSERVABILITY_ENABLED"])
def test_a_deployed_runtime_without_a_bucket_refuses_to_start_a_session(marker, monkeypatch, caplog):
    # Deployed = the existing AgentCore runtime markers (no new knob). Without a bucket the session
    # would live on the microVM's ephemeral disk and vanish at the next cold start.
    monkeypatch.setenv(marker, "true")
    with pytest.raises(SessionStorageError, match="SESSION_STORAGE_FAILURE: session store configure"):
        build_storage(Config(values={}))
    assert f"{marker} is set" in caplog.text
    # A bucket makes the same deployed process fine...
    session_store._s3_storage.cache_clear()
    deployed = build_storage(Config(values={"STRANDLY_SESSION_BUCKET": "b-1", "AWS_REGION": "us-west-2"}))
    assert isinstance(deployed.inner, S3Storage)


def test_local_development_keeps_the_disk_fallback(monkeypatch):
    for name in ("BEDROCK_AGENTCORE_RUNTIME", "AGENT_OBSERVABILITY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(build_storage(Config(values={})).inner, LocalFileStorage)
