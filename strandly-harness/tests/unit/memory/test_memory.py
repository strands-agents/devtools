"""Tests for long-term memory wiring (gated on the KB id + data source id)."""

from __future__ import annotations

import logging

import pytest

from strandly_harness.core.config import Config
from strandly_harness.memory.knowledge_base import build_memory_manager
from strandly_harness.memory.session import _resilient_session_manager


def test_long_term_memory_off_without_kb():
    assert build_memory_manager(Config(values={})) is None


def test_resilient_session_manager_swallows_failures():
    # A session-backend failure (e.g. an AgentCore Memory data-plane 403) must NOT crash the turn.
    class Boom:
        def append_message(self, *a, **k):
            raise RuntimeError("CreateEvent 403 Forbidden")

        def sync_agent(self, *a, **k):
            raise RuntimeError("403")

        def initialize(self, *a, **k):
            return "restored"

    mgr = _resilient_session_manager(Boom())
    # The raising hook methods are now no-ops that return None instead of propagating.
    assert mgr.append_message({"role": "user"}, None) is None
    assert mgr.sync_agent(None) is None
    # A method that doesn't raise still works (and isn't swallowed to None spuriously).
    assert mgr.initialize(None) == "restored"


def test_long_term_memory_needs_both_ids():
    # KB id alone isn't enough — the data source id is required to write.
    assert build_memory_manager(Config(values={"STRANDLY_KB_ID": "KB123"})) is None
    assert build_memory_manager(Config(values={"STRANDLY_KB_DATA_SOURCE_ID": "DS456"})) is None


def test_long_term_memory_on_with_both_ids(monkeypatch):
    captured = {}

    class FakeStore:
        def __init__(self, **kw):
            captured["store"] = kw

    class FakeManager:
        def __init__(self, **kw):
            captured["manager"] = kw

    import strands.memory as sm
    import strands.vended_memory_stores.bedrock_knowledge_base as kb

    monkeypatch.setattr(sm, "MemoryManager", FakeManager)
    monkeypatch.setattr(kb, "BedrockKnowledgeBaseStore", FakeStore)

    # AWS_REGION so the boto3 client construction inside build_memory_manager has a region
    # (offline — no network call); the store itself is faked above.
    cfg = Config(
        values={
            "STRANDLY_KB_ID": "KB123",
            "STRANDLY_KB_DATA_SOURCE_ID": "DS456",
            "AWS_REGION": "us-west-2",
        }
    )
    mgr = build_memory_manager(cfg)

    assert mgr is not None
    # The store points at the configured KB + a writable CUSTOM data source.
    store_cfg = captured["store"]["config"]
    assert store_cfg["knowledge_base_id"] == "KB123"
    assert store_cfg["data_source_id"] == "DS456"
    assert store_cfg["data_source_type"] == "CUSTOM"
    assert captured["store"]["writable"] is True
    # The manager enables the add tool (write path), not just search.
    assert captured["manager"]["add_tool_config"] is True


@pytest.mark.asyncio
async def test_agent_wires_memory_manager_when_kb_configured(fake_model, tmp_path, monkeypatch):
    # End to end through build_agent: with a KB configured, the agent gets a MemoryManager (which
    # owns the search_memory/add_memory tools + recall injection). We assert it's attached rather
    # than re-test the SDK's tool registration. Inject a fake runtime client so the real store
    # makes no network/boto call.
    from strandly_harness.core.agent import build_agent
    from strandly_harness.core.context import RuntimeContext

    real_build = __import__(
        "strandly_harness.memory.knowledge_base", fromlist=["build_memory_manager"]
    ).build_memory_manager
    cfg = Config(
        values={
            "STRANDLY_KB_ID": "KB123",
            "STRANDLY_KB_DATA_SOURCE_ID": "DS456",
            "AWS_REGION": "us-west-2",  # region for offline boto3 client construction
        }
    )

    # Patch the store so it makes no boto call during build: inject a fake runtime client (so
    # __init__ builds none) and pin knowledge_base_type (so the MemoryManager's init-time
    # initialize() skips its GetKnowledgeBase detection call rather than reaching AWS).
    import strands.vended_memory_stores.bedrock_knowledge_base as kb

    orig_store = kb.BedrockKnowledgeBaseStore

    def store_with_fake_client(**store_config):
        store_config["config"] = {
            **store_config["config"],
            "runtime_client": object(),
            "knowledge_base_type": "VECTOR",
        }
        return orig_store(**store_config)

    monkeypatch.setattr(kb, "BedrockKnowledgeBaseStore", store_with_fake_client)

    mgr = real_build(cfg)
    assert mgr is not None

    agent = await build_agent(cfg, RuntimeContext(cwd=str(tmp_path)), model=fake_model)
    names = set(agent.tool_names)
    assert "search_memory" in names
    assert "add_memory" in names


def test_runtime_session_id_pads_short_and_keeps_long():
    from strandly_harness.memory.session import runtime_session_id

    short = runtime_session_id("s-1")
    assert len(short) >= 33 and "/" not in short
    # Deterministic: same input → same affinity key (so a later poll lands on the same instance).
    assert runtime_session_id("s-1") == short
    # Slashes are sanitized (Memory/Runtime ids are slash-free).
    assert "/" not in runtime_session_id("gh/owner/repo/issue/1")
    # An already-long id is returned unchanged (still slash-free).
    long_id = "a" * 40
    assert runtime_session_id(long_id) == long_id


def test_unwrap_session_message_double_wrapped_and_plain():
    import json

    from strandly_harness.memory.session import _unwrap_session_message

    wrapped = json.dumps(
        {"message": {"role": "user", "content": [{"text": "hello"}, {"text": "world"}]}}
    )
    assert _unwrap_session_message(wrapped) == "hello\nworld"
    # Not JSON → returned as-is.
    assert _unwrap_session_message("just text") == "just text"
    # JSON but not a SessionMessage → as-is.
    assert _unwrap_session_message('{"foo": 1}') == '{"foo": 1}'


def test_extract_conversation_sorts_and_unwraps():
    import json

    from strandly_harness.memory.session import extract_conversation

    def ev(ts, role, text):
        wrapped = json.dumps({"message": {"role": role, "content": [{"text": text}]}})
        return {
            "eventTimestamp": ts,
            "payload": [{"conversational": {"role": role.upper(), "content": {"text": wrapped}}}],
        }

    # Out of order on purpose → sorted by timestamp.
    events = [ev(2, "assistant", "done"), ev(1, "user", "do it")]
    convo = extract_conversation(events)
    assert convo == [("user", "do it"), ("assistant", "done")]


def _ev(ts, role, text, *, tool_use=False):
    """Build a raw ListEvents event wrapping a SessionMessage (optionally with a toolUse block)."""
    import json

    content = [{"text": text}]
    if tool_use:
        content.append({"toolUse": {"name": "bash", "toolUseId": "t1", "input": {}}})
    wrapped = json.dumps({"message": {"role": role, "content": content}})
    return {
        "eventTimestamp": ts,
        "payload": [{"conversational": {"role": role.upper(), "content": {"text": wrapped}}}],
    }


def test_final_assistant_text():
    from strandly_harness.memory.session import final_assistant_text

    assert final_assistant_text([("user", "x"), ("assistant", "A"), ("assistant", "B")]) == "B"
    assert final_assistant_text([("user", "x")]) is None
    # Empty assistant text is skipped (a pure tool-use turn carries no answer text).
    assert final_assistant_text([("assistant", "real"), ("assistant", "   ")]) == "real"


def test_parse_session_message_detects_tool_use():
    import json

    from strandly_harness.memory.session import _parse_session_message

    narration = json.dumps(
        {"message": {"role": "assistant", "content": [{"text": "cloning"}, {"toolUse": {"x": 1}}]}}
    )
    assert _parse_session_message(narration) == ("cloning", True)
    answer = json.dumps({"message": {"role": "assistant", "content": [{"text": "final"}]}})
    assert _parse_session_message(answer) == ("final", False)
    # Non-SessionMessage input → (raw, False), so hand-written events still parse.
    assert _parse_session_message("plain") == ("plain", False)


def test_conversation_settled_is_tool_use_aware():
    from strandly_harness.memory.session import conversation_settled

    # No events → not settled.
    assert conversation_settled([]) is False
    # Only the user ask so far → not settled.
    assert conversation_settled([_ev(1, "user", "review PR")]) is False
    # The regression this fixes: a narration ("Let me clone…") emitted *before* a tool call carries
    # a toolUse block. The old "any assistant after the last user" rule called this settled and
    # returned the narration as the final result. The tool_use-aware rule keeps it running.
    assert (
        conversation_settled(
            [_ev(1, "user", "review PR"), _ev(2, "assistant", "Let me clone…", tool_use=True)]
        )
        is False
    )
    # A tool result (non-assistant message) as the latest event → still running.
    assert (
        conversation_settled(
            [
                _ev(1, "user", "review PR"),
                _ev(2, "assistant", "cloning", tool_use=True),
                _ev(3, "user", "<tool result>"),
            ]
        )
        is False
    )
    # Only when a terminal, text-only assistant answer is the last message → settled.
    assert (
        conversation_settled(
            [
                _ev(1, "user", "review PR"),
                _ev(2, "assistant", "cloning", tool_use=True),
                _ev(3, "user", "<tool result>"),
                _ev(4, "assistant", "Here is my review: LGTM."),
            ]
        )
        is True
    )


def test_read_events_requests_full_tail_not_just_default_100(monkeypatch):
    # MED-HIGH regression: list_events is oldest-first and truncates to max_results (default 100),
    # so the default would drop the FINAL assistant message of a >100-event run. read_events must
    # request MEMORY_MAX_EVENTS so the read reaches the tail.
    import strandly_harness.memory.session as mem
    from strandly_harness.core.constants import MEMORY_MAX_EVENTS

    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def list_events(self, **kw):
            captured.update(kw)
            return []

    import bedrock_agentcore.memory as bam

    monkeypatch.setattr(bam, "MemoryClient", FakeClient)
    cfg = Config(values={"AGENTCORE_MEMORY_ID": "mem-123", "AWS_REGION": "us-west-2"})
    assert cfg.use_agentcore_session is True
    mem.read_events(cfg, "gh/owner/repo/issue/1")
    assert captured["max_results"] == MEMORY_MAX_EVENTS
    assert captured["include_payload"] is True
    # The Memory id is read under the sanitized (slash-free) session id.
    assert "/" not in captured["session_id"]


def test_read_events_empty_without_memory_configured():
    import strandly_harness.memory.session as mem

    assert mem.read_events(Config(values={}), "s-1") == []


# --- add_memory write instrumentation (structured log + EMF metric) -----------------------------
#
# Long-term KB writes (the ``add_memory`` tool) were the one ingestion path with no log and no
# metric, so memory poisoning would go unobserved. `_instrument_add_memory` wraps the real SDK
# add_memory tool. We exercise it through a minimal offline writable store (no AWS/network) and
# assert via caplog + by monkeypatching the metrics `emit` seam.


class _FakeKBStore:
    """Minimal writable ``MemoryStore`` so the real ``add_memory`` tool runs fully offline."""

    name = "strandly-memory"
    description = "fake KB"
    max_search_results = None
    writable = True
    extraction = None

    def __init__(self, *, fail: bool = False):
        self._fail = fail
        self.added: list[str] = []

    async def search(self, query, options=None):
        return []

    async def add(self, content, metadata=None):
        if self._fail:
            raise RuntimeError("KB ingestion failed: AccessDenied")
        self.added.append(content)


def _build_instrumented_manager(store, *, ctx=None, config=None):
    """A real MemoryManager over ``store`` with its add_memory tool instrumented."""
    from strands.memory import MemoryManager

    from strandly_harness.memory.knowledge_base import _instrument_add_memory

    manager = MemoryManager(stores=[store], add_tool_config=True)
    return _instrument_add_memory(manager, config or Config(values={}), ctx)


def _add_tool(manager):
    return next(t for t in manager.tools if t.tool_name == "add_memory")


def _patch_emit_seam(monkeypatch):
    """Monkeypatch the metrics ``emit`` seam; return the list it records ``(doc, surface)`` into."""
    from strandly_harness.ops import metrics

    emitted: list[tuple[dict, str | None]] = []

    def fake_emit(doc, *, surface=None):
        emitted.append((doc, surface))
        return True

    monkeypatch.setattr(metrics, "emit", fake_emit)
    return emitted


@pytest.mark.asyncio
async def test_add_memory_success_logs_and_emits_metric(caplog, monkeypatch):
    from strandly_harness.core.context import RuntimeContext
    from strandly_harness.ops import metrics

    emitted = _patch_emit_seam(monkeypatch)
    store = _FakeKBStore()
    manager = _build_instrumented_manager(store, ctx=RuntimeContext(session_id="gh-issue-1"))

    with caplog.at_level(logging.INFO, logger="strandly_harness.memory.knowledge_base"):
        result = await _add_tool(manager)._tool_func(entries=["prefer ruff over flake8"])

    # The underlying write still happened and its result is unchanged.
    assert result == {"stored": 1}
    assert store.added == ["prefer ruff over flake8"]

    # Exactly one success metric, on the memory surface, once per add_memory call.
    memory_writes = [d for d, s in emitted if metrics.MEMORY_WRITE in d and s == metrics.SURFACE_MEMORY]
    assert len(memory_writes) == 1
    assert memory_writes[0][metrics.MEMORY_WRITE] == 1
    assert not any(metrics.MEMORY_WRITE_FAILED in d for d, _ in emitted)

    # Structured INFO log: records that a write happened with count + session, fail-soft.
    rec = [r for r in caplog.records if "add_memory write ok" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelno == logging.INFO
    msg = rec[0].getMessage()
    assert "entries=1" in msg and "session_id=gh-issue-1" in msg


@pytest.mark.asyncio
async def test_add_memory_logs_preview_not_full_secret_content(caplog, monkeypatch):
    # Adversarial: a long entry must NOT be logged in full — only a short capped preview + a length.
    _patch_emit_seam(monkeypatch)
    store = _FakeKBStore()
    manager = _build_instrumented_manager(store)

    secret_tail = "SECRET_TOKEN_zzzzzzzzzzzzzzzzzzzz"
    entry = "fact: " + ("x" * 200) + secret_tail
    with caplog.at_level(logging.INFO, logger="strandly_harness.memory.knowledge_base"):
        await _add_tool(manager)._tool_func(entries=[entry])

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert secret_tail not in logged  # tail beyond the preview window is never logged
    assert entry not in logged  # the full entry is never logged
    assert f"total_len={len(entry)}" in logged  # but the length is recorded


@pytest.mark.asyncio
async def test_add_memory_failure_emits_failure_metric_and_logs_warning(caplog, monkeypatch):
    # A failing write is logged at WARNING + metered as MemoryWriteFailed; the underlying error is
    # re-raised UNCHANGED (we deliberately do not mask a failed write — that would make the agent
    # believe a fact was stored when it wasn't). Fail-soft refers to the instrumentation itself
    # (covered by the next test), not to swallowing genuine write failures.
    from strandly_harness.ops import metrics

    emitted = _patch_emit_seam(monkeypatch)
    store = _FakeKBStore(fail=True)
    manager = _build_instrumented_manager(store)

    from strands.types.exceptions import AggregateMemoryError

    with caplog.at_level(logging.WARNING, logger="strandly_harness.memory.knowledge_base"):
        with pytest.raises(AggregateMemoryError):  # SDK wraps per-store failures
            await _add_tool(manager)._tool_func(entries=["poisoned fact"])

    failures = [d for d, s in emitted if metrics.MEMORY_WRITE_FAILED in d and s == metrics.SURFACE_MEMORY]
    assert len(failures) == 1
    assert not any(metrics.MEMORY_WRITE in d for d, _ in emitted)
    warns = [r for r in caplog.records if "add_memory write FAILED" in r.getMessage()]
    assert len(warns) == 1 and warns[0].levelno >= logging.WARNING


@pytest.mark.asyncio
async def test_add_memory_instrumentation_is_fail_soft(monkeypatch):
    # The instrumentation must NEVER break (or alter) the write: even if the metrics emit seam
    # blows up, a successful write still returns its normal result and does not raise.
    from strandly_harness.ops import metrics

    def boom_emit(doc, *, surface=None):
        raise RuntimeError("metrics backend exploded")

    monkeypatch.setattr(metrics, "emit", boom_emit)
    store = _FakeKBStore()
    manager = _build_instrumented_manager(store)

    result = await _add_tool(manager)._tool_func(entries=["still stored"])
    assert result == {"stored": 1}
    assert store.added == ["still stored"]


@pytest.mark.asyncio
async def test_add_memory_metrics_disabled_is_noop(capsys, monkeypatch):
    # With no namespace configured, emit is a pure no-op: the write succeeds and NOT a single EMF
    # line hits stdout (logging still happens — that's stderr/log handlers, not the EMF channel).
    from strandly_harness.ops import metrics

    monkeypatch.delenv(metrics.NAMESPACE_ENV, raising=False)
    store = _FakeKBStore()
    manager = _build_instrumented_manager(store)

    result = await _add_tool(manager)._tool_func(entries=["a fact"])
    assert result == {"stored": 1}
    assert capsys.readouterr().out == ""  # no EMF line emitted when metrics are disabled
