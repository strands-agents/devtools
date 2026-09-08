from __future__ import annotations

import asyncio

import pytest

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext


@pytest.mark.asyncio
async def test_run_turn_end_to_end(tmp_path, monkeypatch, text_model):
    import strandly_harness.core.agent as agent_mod
    from strandly_harness.serve.turn import run_turn

    monkeypatch.setattr(agent_mod, "build_model", lambda c, tier="default": text_model("pong"))
    ctx = RuntimeContext(cwd=str(tmp_path))
    events = [e async for e in run_turn(Config(values={}), "hi", ctx)]
    kinds = [e.kind for e in events]
    assert "text" in kinds and kinds[-1] == "done"
    assert "pong" in "".join(e.text or "" for e in events if e.kind == "text")


@pytest.mark.asyncio
async def test_mcp_run_collect(tmp_path, monkeypatch, text_model):
    import strandly_harness.core.agent as agent_mod
    from strandly_harness.serve.mcp_server import run_collect

    monkeypatch.setattr(agent_mod, "build_model", lambda c, tier="default": text_model("the answer"))
    out = await run_collect(Config(values={}), "question", "s1")
    assert "the answer" in out


def test_agentcore_build_app_importorskip(monkeypatch, text_model):
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.core.agent as agent_mod
    from strandly_harness.serve.agentcore_app import build_app

    monkeypatch.setattr(agent_mod, "build_model", lambda c, tier="default": text_model("hi"))
    app = build_app(Config(values={}))
    assert app is not None


@pytest.mark.asyncio
async def test_agent_cache_reuses_per_session_and_isolates():
    from strandly_harness.serve.cache import AgentCache

    cache = AgentCache()
    builds = {"n": 0}

    async def build():
        builds["n"] += 1
        return object()

    a1 = await cache.get_or_build("s1", build)
    a2 = await cache.get_or_build("s1", build)
    b1 = await cache.get_or_build("s2", build)
    assert a1 is a2  # same session → same cached agent (built once)
    assert b1 is not a1  # different session → different agent
    assert builds["n"] == 2  # s1 built once, s2 built once


@pytest.mark.asyncio
async def test_agentcore_invoke_no_github_context_accepted(monkeypatch, text_model):
    # The GitHub gate is gone: a bare {"prompt": ...} (no GitHub context) is ACCEPTED as a
    # fire-and-forget run and returns a taskId. GITHUB_CONTEXT is cleared so the test is hermetic
    # (it must not influence behavior either way now that the gate is removed).
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.core.agent as agent_mod
    from strandly_harness.serve.agentcore_app import build_app

    monkeypatch.setattr(agent_mod, "build_model", lambda c, tier="default": text_model("hi"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]  # the @app.entrypoint function

    res = await invoke({"prompt": "hi", "sessionId": "s-1"})
    assert res["status"] == "accepted" and res["taskId"]
    # Let the fire-and-forget background task run to completion (and not be GC'd mid-flight).
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_agentcore_stream_mode_streams_events(monkeypatch, text_model):
    # mode:"stream" returns an async generator of event dicts (SSE), no GitHub context needed.
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.core.agent as agent_mod
    from strandly_harness.serve.agentcore_app import build_app

    monkeypatch.setattr(agent_mod, "build_model", lambda c, tier="default": text_model("pong"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]

    gen = await invoke({"prompt": "ping", "mode": "stream"})
    events = [e async for e in gen]
    kinds = [e["kind"] for e in events]
    assert "text" in kinds and kinds[-1] == "done"
    assert "pong" in "".join(e.get("text") or "" for e in events if e["kind"] == "text")


def _mem_event(ts, role, text, *, tool_use=False):
    """A raw ListEvents event wrapping a SessionMessage (optionally carrying a toolUse block)."""
    import json

    content = [{"text": text}]
    if tool_use:
        content.append({"toolUse": {"name": "bash", "toolUseId": "t1", "input": {}}})
    wrapped = json.dumps({"message": {"role": role, "content": content}})
    return {
        "eventTimestamp": ts,
        "payload": [{"conversational": {"role": role.upper(), "content": {"text": wrapped}}}],
    }


def test_poll_result_uses_sentinel_then_memory(monkeypatch):
    # poll_result merges the in-instance sentinel with the durable Memory session (raw events).
    import strandly_harness.serve.agentcore_app as ac
    from strandly_harness.serve.agentcore_app import _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()

    # 1) Sentinel says completed with a captured result → that wins (no Memory needed).
    store.start("t1")
    store.finish("t1", "captured final")
    assert poll_result(cfg, store, "t1", "s-1") == {
        "taskId": "t1", "status": "completed", "result": "captured final"
    }

    # 2) Sentinel unknown (recycled / poll landed elsewhere) → fall back to the tool_use-aware
    #    Memory settle. A terminal text-only assistant answer is the last event → completed.
    convo = [_mem_event(1, "user", "do it"), _mem_event(2, "assistant", "all done")]
    monkeypatch.setattr(ac, "read_events", lambda config, sid: convo)
    res = poll_result(cfg, store, "missing", "s-1")
    assert res["status"] == "completed" and res["result"] == "all done"

    # 2b) Unknown sentinel + the latest assistant message is a narration with a pending toolUse
    #     (the bug this fixes) → still running, NOT completed-with-narration.
    narrating = [
        _mem_event(1, "user", "do it"),
        _mem_event(2, "assistant", "Let me start by cloning…", tool_use=True),
    ]
    monkeypatch.setattr(ac, "read_events", lambda config, sid: narrating)
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "running"

    # 3) Unknown sentinel + only a user message (no reply yet) → running, not completed.
    monkeypatch.setattr(ac, "read_events", lambda config, sid: [_mem_event(1, "user", "do it")])
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "running"

    # 4) Unknown sentinel + Memory read raises → degrade to unknown (best-effort).
    def boom(config, sid):
        raise RuntimeError("memory 403")

    monkeypatch.setattr(ac, "read_events", boom)
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "unknown"


def test_task_store_lifecycle():
    from strandly_harness.serve.agentcore_app import _TaskStore

    store = _TaskStore()
    assert store.get("nope")["status"] == "unknown"  # best-effort: unknown, not error
    store.start("t1")
    assert store.get("t1")["status"] == "running"
    store.finish("t1", "the result")
    assert store.get("t1") == {"status": "completed", "result": "the result"}
    store.start("t2")
    store.fail("t2", "boom")
    assert store.get("t2")["status"] == "failed" and store.get("t2")["error"] == "boom"


@pytest.mark.asyncio
async def test_agentcore_poll_action(monkeypatch, text_model):
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.core.agent as agent_mod
    from strandly_harness.serve.agentcore_app import build_app

    monkeypatch.setattr(agent_mod, "build_model", lambda c, tier="default": text_model("hi"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]

    assert (await invoke({"action": "poll"}))["status"] == "error"  # missing taskId
    res = await invoke({"action": "poll", "taskId": "missing"})
    assert res["status"] == "unknown" and res["taskId"] == "missing"


def test_github_target_extracts_pr_url_and_repo():
    from strandly_harness.serve.agentcore_app import _github_target

    ctx = {
        "repository": "strands-agents/sdk-python",
        "event": {"pull_request": {"html_url": "https://github.com/strands-agents/sdk-python/pull/42"}},
    }
    url, repo = _github_target(ctx)
    assert url == "https://github.com/strands-agents/sdk-python/pull/42"
    assert repo == "strands-agents/sdk-python"


def test_github_target_falls_back_to_issue_and_event_repository():
    from strandly_harness.serve.agentcore_app import _github_target

    ctx = {
        "event": {
            "issue": {"html_url": "https://github.com/o/r/issues/7"},
            "repository": {"full_name": "o/r"},
        }
    }
    url, repo = _github_target(ctx)
    assert url.endswith("/issues/7") and repo == "o/r"


def test_github_target_handles_missing_context():
    from strandly_harness.serve.agentcore_app import _github_target

    assert _github_target(None) == (None, None)
    assert _github_target({"prompt": "hi"}) == (None, None)


def test_trace_id_falls_back_to_xray_header(monkeypatch):
    # No active OTel span (test env) → falls back to the X-Ray env var, else None.
    from strandly_harness.serve import agentcore_app as agentcore

    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=x")
    assert agentcore._trace_id() == "1-5759e988-bd862e3fe1be46a994272793"
    monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
    assert agentcore._trace_id() is None


def test_xray_id_from_otel_format():
    # A 128-bit OTel trace id renders as an X-Ray id: 1-<8 hex>-<24 hex>.
    from strandly_harness.serve import agentcore_app as agentcore

    otel = int("6a3f0fc5550b3c5d3b6ec25c6e8dcfd5", 16)
    assert agentcore._xray_id_from_otel(otel) == "1-6a3f0fc5-550b3c5d3b6ec25c6e8dcfd5"


def test_trace_id_prefers_live_otel_span(monkeypatch):
    # The fix: when a valid span context exists (runtime runs under opentelemetry-instrument),
    # _trace_id uses it — even though the X-Ray env var is empty in the fire-and-forget task.
    from strandly_harness.serve import agentcore_app as agentcore

    monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)

    class _Ctx:
        is_valid = True
        trace_id = int("6a3f0fc5550b3c5d3b6ec25c6e8dcfd5", 16)

    class _Span:
        def get_span_context(self):
            return _Ctx()

    import opentelemetry.trace as ot

    monkeypatch.setattr(ot, "get_current_span", lambda: _Span())
    assert agentcore._trace_id() == "1-6a3f0fc5-550b3c5d3b6ec25c6e8dcfd5"


def test_done_event_carries_token_usage():
    """The terminal event surfaces accumulated token usage for the run-ledger."""
    from strandly_harness.core.events import classify_all

    class _Metrics:
        accumulated_usage = {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15}

    class _Result:
        stop_reason = "end_turn"
        metrics = _Metrics()

    (ev,) = classify_all({"result": _Result()}, {})
    assert ev.kind == "done"
    assert ev.data["stop_reason"] == "end_turn"
    assert ev.data["usage"] == {"input": 12, "output": 3, "total": 15}


def test_done_event_without_metrics_has_no_usage():
    from strandly_harness.core.events import classify_all

    (ev,) = classify_all({"result": {"stop_reason": "end_turn"}}, {})
    assert ev.kind == "done" and "usage" not in ev.data


# ---------------------------------------------------------------------------
# Run-level retry (the model-layer gap): _run must ride out transient
# mid-stream failures with a continuation prompt, and fail fast on real bugs.
# ---------------------------------------------------------------------------


def _turn_events(text: str):
    from strandly_harness.core.events import HarnessEvent

    return [
        HarnessEvent(kind="text", text=text),
        HarnessEvent(kind="done", data={"usage": {"inputTokens": 1, "outputTokens": 2}}),
    ]


async def _drain_background():
    for _ in range(100):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_run_retries_transient_midstream_error_with_continuation(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, hitl=False):
        calls.append(user_input)

        async def gen():
            if len(calls) == 1:
                # First attempt dies mid-stream, AFTER yielding partial text — the classic
                # long-run failure botocore retries can't help with.
                yield _turn_events("partial narration that must not leak into the result")[0]
                raise ConnectionResetError(104, "Connection reset by peer")
            for ev in _turn_events("recovered final answer"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    monkeypatch.setattr(ac, "backoff_seconds", lambda attempt: 0.0)  # no real sleeps in tests

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "do the task", "sessionId": "s-retry"})
    assert res["status"] == "accepted"
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed"
    assert poll["result"] == "recovered final answer"  # partial text from attempt 1 discarded
    assert len(calls) == 2
    assert calls[0] == "do the task"
    # Session invoke → the retry resumes with the continuation prompt, not the original.
    from strandly_harness.core.retries import CONTINUATION_PROMPT

    assert calls[1] == CONTINUATION_PROMPT


@pytest.mark.asyncio
async def test_run_sessionless_retry_resends_original_prompt(monkeypatch):
    # Without a session there's no cached agent (no history), so a continuation prompt would land
    # on a fresh agent with no context — the retry must re-send the ORIGINAL prompt instead.
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, hitl=False):
        calls.append(user_input)

        async def gen():
            if len(calls) == 1:
                raise TimeoutError("Read timed out. (read timeout=300)")
            for ev in _turn_events("ok"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    monkeypatch.setattr(ac, "backoff_seconds", lambda attempt: 0.0)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "one-shot ask"})  # no sessionId
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed" and poll["result"] == "ok"
    assert calls == ["one-shot ask", "one-shot ask"]


@pytest.mark.asyncio
async def test_run_non_transient_error_fails_immediately_no_retry(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, hitl=False):
        calls.append(user_input)

        async def gen():
            raise KeyError("usage")  # a real bug — must NOT be retried
            yield  # pragma: no cover — makes this an async generator

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    monkeypatch.setattr(ac, "backoff_seconds", lambda attempt: pytest.fail("must not back off"))

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-bug"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "failed" and "KeyError" in poll["error"]
    assert len(calls) == 1  # exactly one attempt


@pytest.mark.asyncio
async def test_run_transient_errors_exhaust_attempts_then_fail(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly_harness.serve.agentcore_app as ac
    from strandly_harness.core.constants import RUN_RETRY_MAX_ATTEMPTS

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, hitl=False):
        calls.append(user_input)

        async def gen():
            raise ConnectionResetError(104, "Connection reset by peer")
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    monkeypatch.setattr(ac, "backoff_seconds", lambda attempt: 0.0)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-exhaust"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "failed"  # bounded: eventually fails loudly, not an infinite loop
    assert len(calls) == RUN_RETRY_MAX_ATTEMPTS
