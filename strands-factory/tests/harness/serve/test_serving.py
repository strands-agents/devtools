from __future__ import annotations

import asyncio
import copy
import json
import threading
import time

import pytest

from model_seam import use_model
from strandly.core.config import Config
from strandly.core.context import RuntimeContext


@pytest.mark.asyncio
async def test_run_turn_end_to_end(tmp_path, monkeypatch, text_model):
    from strandly.serve.turn import run_turn

    use_model(monkeypatch, lambda _model: text_model("pong"))
    ctx = RuntimeContext(cwd=str(tmp_path))
    events = [e async for e in run_turn(Config(values={}), "hi", ctx)]
    kinds = [e.kind for e in events]
    assert "text" in kinds and kinds[-1] == "done"
    assert "pong" in "".join(e.text or "" for e in events if e.kind == "text")


@pytest.mark.asyncio
async def test_run_turn_injects_the_payloads_caller_context_per_turn(tmp_path, monkeypatch, text_model):
    """The whole chain, against a real agent build: an invoke payload carrying
    ``invocationContext`` produces a labeled caller-context block in the MODEL's input for that
    turn — and the next turn on the same (cached) agent carries the NEW payload's context, not the
    stale one. That per-turn freshness is the reason the value rides invocation state instead of
    the plugin constructor (the agent is cached per session while the user navigates)."""
    from strandly.serve.turn import run_turn

    seen: list[list] = []  # the messages of every model call, in order

    def recording_model(text: str):
        model = text_model(text)
        original_stream = model.stream

        async def stream(*args, **kwargs):
            messages = kwargs.get("messages", args[0] if args else [])
            seen.append(messages)
            async for ev in original_stream(*args, **kwargs):
                yield ev

        model.stream = stream
        return model

    use_model(monkeypatch, lambda _model: recording_model("ok"))

    def _payload(number: int) -> dict:
        return {
            "prompt": "what do you think about this one?",
            "sessionId": "s-caller-context",
            "invocationContext": {
                "surface": "maintainer-portal",
                "viewing": {"type": "issue", "repo": "o/r", "number": number},
            },
        }

    marker = "# Caller context (what the user is currently viewing)"

    def tail_of_the_ask_call(calls: list[list], ask: str) -> str:
        """The latest user message of the FIRST model call that carried ``ask``.

        The ask's own call is the earliest one containing it: every later call that also
        contains the ask text is a follow-up quoting it inside a larger prompt of its own —
        the goal loop's critic embeds the whole transcript (turn 1's context included, which
        would false-positive a leak assertion), and the trailing structured-output pass
        (work-product extraction) carries the SDK's formatting prompt as its tail. The
        injection contract is about the ask's own call, so match forward, not backward.
        """
        for messages in calls:
            tail = messages[-1]
            if tail.get("role") == "user" and ask in str(tail):
                return str(tail)
        raise AssertionError(f"no model call carried {ask!r} as its latest user message")

    ctx = RuntimeContext(cwd=str(tmp_path), session_id="s-caller-context", event=_payload(1))
    [e async for e in run_turn(Config(values={}), "what do you think about this one?", ctx)]

    assert seen, "the model was never called"
    first_tail = tail_of_the_ask_call(seen, "what do you think about this one?")
    assert marker in first_tail
    assert '"number": 1' in first_tail

    # Turn 2 on the SAME session (cache hit — build_agent not re-run): the NEW payload's context.
    seen.clear()
    ctx2 = RuntimeContext(cwd=str(tmp_path), session_id="s-caller-context", event=_payload(2))
    [e async for e in run_turn(Config(values={}), "and this?", ctx2)]

    assert seen
    second_tail = tail_of_the_ask_call(seen, "and this?")
    assert marker in second_tail
    assert '"number": 2' in second_tail
    assert '"number": 1' not in second_tail  # turn 1's view did not leak onto turn 2's ask


@pytest.mark.asyncio
async def test_run_turn_without_invocation_context_injects_nothing(tmp_path, monkeypatch, text_model):
    """Purely additive: an ordinary payload produces no caller-context block anywhere."""
    from strandly.serve.turn import run_turn

    seen: list[list] = []

    def recording_model(text: str):
        model = text_model(text)
        original_stream = model.stream

        async def stream(*args, **kwargs):
            messages = kwargs.get("messages", args[0] if args else [])
            seen.append(messages)
            async for ev in original_stream(*args, **kwargs):
                yield ev

        model.stream = stream
        return model

    use_model(monkeypatch, lambda _model: recording_model("ok"))
    ctx = RuntimeContext(cwd=str(tmp_path), event={"prompt": "hi", "sessionId": "s-no-context"})
    [e async for e in run_turn(Config(values={}), "hi", ctx)]

    assert seen
    assert "Caller context" not in str(seen)


def test_agentcore_build_app_importorskip(monkeypatch, text_model):
    pytest.importorskip("bedrock_agentcore")
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("hi"))
    app = build_app(Config(values={}))
    assert app is not None


def test_agentcore_build_app_enables_metrics_from_the_config_secret(monkeypatch, capsys, text_model):
    """The deployed runtime must actually emit EMF when the config secret names a namespace.

    Root cause of "runtime metrics were never emitted in prod": the runtime's config arrives as the
    Secrets Manager secret, which ``Config.load`` merges into ``Config.values`` and never into
    ``os.environ`` — while ``metrics.emit`` gates on ``os.environ`` alone (ops/ is Config-free). So
    ``metrics.enabled()`` was False in-runtime and every ``_emit_run_metrics`` call was a silent
    no-op, no matter what the secret said. ``build_app`` now bridges the two.
    """
    pytest.importorskip("bedrock_agentcore")
    from strandly.ops import metrics
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("hi"))
    # "" reads as unset to the gate (so the secret is the ONLY source here) but is still a key
    # monkeypatch restores at teardown — the namespace build_app sets can't leak into other tests.
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "")

    build_app(Config(values={metrics.NAMESPACE_ENV: "Strandly-prod"}))

    assert metrics.enabled() is True
    # ...and a real emit lands (this is what an alarm reads: the namespace-level rollup).
    assert metrics.emit({metrics.COMPLETED: 1}, surface=metrics.SURFACE_AGENTCORE) is True
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Strandly-prod"


def test_agentcore_build_app_leaves_metrics_off_when_unconfigured(monkeypatch, text_model):
    # No namespace anywhere → still a pure no-op (local runs and tests must not start emitting).
    pytest.importorskip("bedrock_agentcore")
    from strandly.ops import metrics
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("hi"))
    monkeypatch.delenv(metrics.NAMESPACE_ENV, raising=False)

    build_app(Config(values={}))

    assert metrics.enabled() is False


@pytest.mark.asyncio
async def test_agent_cache_reuses_per_session_and_isolates():
    from strandly.serve.cache import AgentCache

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


# --- agent cache key: session + the identity baked into the build --------------------------------
#
# A cached agent is not a blank slate: the long-term-memory personal namespace is resolved once, at
# build time, from the invoke that built it. Slack session ids are CHANNEL-scoped, so keying only on
# the session would serve a second identity on that session the first caller's personal memory store.


def test_agent_cache_key_is_the_bare_session_off_slack():
    """Every non-Slack surface keeps exactly today's key — no behavior change where there's no
    per-person state to confuse."""
    from strandly.serve.cache import agent_cache_key, session_key

    for ctx in (
        RuntimeContext(session_id="gh-org-repo-pr-7"),
        RuntimeContext(session_key="dash-1"),
        RuntimeContext(session_id="s1", event={"event_name": "issue_comment"}),
        # A Slack channel (not a DM) gets no personal namespace, so no suffix either.
        RuntimeContext(session_id="slack-T1-C123", event={"slackChannelId": "C123", "slackUserId": "U1"}),
    ):
        assert agent_cache_key(ctx) == session_key(ctx)
    # Sessionless stays sessionless (never cached at all).
    assert agent_cache_key(RuntimeContext()) is None


def test_agent_cache_key_separates_identities_on_one_session_key():
    """The leak this closes: same session id, different person → NOT the same cached agent.

    Reachable two ways — a future multi-person Slack surface, or a dashboard/CLI invoke that reuses a
    ``slack-*`` session id. Either way the second caller must not inherit the first one's personal
    memory store for the life of the process.
    """
    from strandly.serve.cache import agent_cache_key

    first = RuntimeContext(session_id="slack-T1-D123", event={"slackChannelId": "D123", "slackUserId": "U1", "triggerSource": "slack:U1"})
    same_person_again = RuntimeContext(
        session_id="slack-T1-D123", event={"slackChannelId": "D123", "slackUserId": "U1", "triggerSource": "slack:U1"}
    )
    other_person = RuntimeContext(
        session_id="slack-T1-D123", event={"slackChannelId": "D123", "slackUserId": "U2", "triggerSource": "slack:U2"}
    )
    no_identity = RuntimeContext(session_id="slack-T1-D123")  # e.g. a dashboard invoke

    # A DM is 1:1 with the person, so the key is stable across that person's turns: one agent, as before.
    assert agent_cache_key(first) == agent_cache_key(same_person_again)
    assert agent_cache_key(first) != agent_cache_key(other_person)
    assert agent_cache_key(first) != agent_cache_key(no_identity)
    # The session is still recoverable from the key (it is a suffix, not a hash) — keys stay debuggable.
    assert agent_cache_key(first).startswith("slack-T1-D123")


@pytest.mark.asyncio
async def test_cache_hands_a_second_identity_its_own_agent():
    """The key difference, exercised through the cache the serving path actually uses."""
    from strandly.serve.cache import AgentCache, agent_cache_key

    cache = AgentCache()

    async def build():
        return object()

    a = RuntimeContext(session_id="slack-T1-D9", event={"slackChannelId": "D9", "slackUserId": "U1", "triggerSource": "slack:U1"})
    b = RuntimeContext(session_id="slack-T1-D9", event={"slackChannelId": "D9", "slackUserId": "U2", "triggerSource": "slack:U2"})

    agent_a = await cache.get_or_build(agent_cache_key(a), build)
    agent_a_again = await cache.get_or_build(agent_cache_key(a), build)
    agent_b = await cache.get_or_build(agent_cache_key(b), build)

    assert agent_a is agent_a_again  # same person, same session → still one agent
    assert agent_b is not agent_a  # different person → never the first one's agent


@pytest.mark.asyncio
async def test_agentcore_invoke_no_github_context_accepted(monkeypatch, text_model):
    # The GitHub gate is gone: a bare {"prompt": ...} (no GitHub context) is ACCEPTED as a
    # fire-and-forget run and returns a taskId. GITHUB_CONTEXT is cleared so the test is hermetic
    # (it must not influence behavior either way now that the gate is removed).
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    pytest.importorskip("bedrock_agentcore")
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("hi"))
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
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("pong"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]

    gen = await invoke({"prompt": "ping", "mode": "stream"})
    events = [e async for e in gen]
    kinds = [e["kind"] for e in events]
    assert "text" in kinds and kinds[-1] == "done"
    assert "pong" in "".join(e.get("text") or "" for e in events if e["kind"] == "text")


def _msg(role, text, *, tool_use=False):
    content = [{"text": text}]
    if tool_use:
        content.append({"toolUse": {"name": "shell", "toolUseId": "t1", "input": {}}})
    return {"role": role, "content": content}


def _write_snapshot(session_id, messages):
    """Write a session's ``snapshot_latest.json`` into the (per-test) local session store."""
    import json
    from pathlib import Path

    import strandly.memory.session_store as session_store
    from strandly.core.session_store_reader import snapshot_key

    path = Path(session_store.LOCAL_SESSION_STORE_DIR) / snapshot_key(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"scope": "agent", "schema_version": "1.0", "data": {"messages": messages}})
    )


def test_poll_result_uses_sentinel_then_the_session_snapshot(monkeypatch):
    # poll_result merges the in-instance sentinel with the durable session (the latest snapshot).
    import strandly.serve.agentcore_app as ac
    from strandly.serve.agentcore_app import _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()

    # 1) Sentinel says completed with a captured result → that wins (no store read needed).
    store.start("t1")
    store.finish("t1", "captured final")
    assert poll_result(cfg, store, "t1", "s-1") == {
        "taskId": "t1", "status": "completed", "result": "captured final"
    }

    # 0) Sentinel unknown and nothing stored → unknown.
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "unknown"

    # 2) Sentinel unknown (recycled / poll landed elsewhere) → fall back to the tool_use-aware
    #    settle over the snapshot. A terminal text-only assistant answer is last → completed.
    _write_snapshot("s-1", [_msg("user", "do it"), _msg("assistant", "all done")])
    res = poll_result(cfg, store, "missing", "s-1")
    assert res["status"] == "completed" and res["result"] == "all done"

    # 2b) The latest assistant message is a narration with a pending toolUse → still running,
    #     NOT completed-with-narration.
    _write_snapshot(
        "s-1", [_msg("user", "do it"), _msg("assistant", "Let me start by cloning…", tool_use=True)]
    )
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "running"

    # 3) Only a user message (no reply yet) → running, not completed.
    _write_snapshot("s-1", [_msg("user", "do it")])
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "running"

    # 4) The store read raises → degrade to unknown (best-effort).
    def boom(config, sid):
        raise RuntimeError("s3 403")

    monkeypatch.setattr(ac, "read_conversation", boom)
    assert poll_result(cfg, store, "missing", "s-1")["status"] == "unknown"


def test_an_issued_only_poll_answers_only_for_a_task_started_on_that_session(monkeypatch):
    """A made-up or foreign task id reads nothing, not even the session's stored answer."""
    import strandly.serve.agentcore_app as ac
    from strandly.serve.agentcore_app import POLL_ISSUED_KEY, _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()
    store.start("t1", session="s-1")
    store.finish("t1", "mine")
    reads: list[str] = []
    monkeypatch.setattr(ac, "read_conversation", lambda config, sid: reads.append(sid) or [
        ("user", "q", False), ("assistant", "the stored answer", False)])

    assert poll_result(cfg, store, "t1", "s-1", issued_only=True)["result"] == "mine"
    for task_id, session_id in (("made-up", "s-1"), ("t1", "s-2"), ("t1", None)):
        res = poll_result(cfg, store, task_id, session_id, issued_only=True)
        assert res == {"taskId": task_id, "status": "unknown", POLL_ISSUED_KEY: False}
    assert reads == ["s-1"]
    # Without the flag the classic fallback still settles from the session (existing pollers).
    assert poll_result(cfg, store, "made-up", "s-1")["result"] == "the stored answer"


def test_poll_reads_only_the_snapshot_even_with_memory_configured(monkeypatch):
    import bedrock_agentcore.memory as memory_pkg

    from strandly.serve.agentcore_app import _TaskStore, poll_result

    def no_memory(**_):
        raise AssertionError("the poll must never read AgentCore Memory")

    monkeypatch.setattr(memory_pkg, "MemoryClient", no_memory)
    store = _TaskStore()
    with_memory = Config(values={"AGENTCORE_MEMORY_ID": "mem-1"})

    _write_snapshot("new-session", [_msg("user", "hi"), _msg("assistant", "from the snapshot")])
    res = poll_result(with_memory, store, "missing", "new-session")
    assert res["status"] == "completed" and res["result"] == "from the snapshot"
    # A session with no snapshot is unknown — there is no other source.
    assert poll_result(with_memory, store, "missing", "old-session")["status"] == "unknown"


def test_task_store_lifecycle():
    from strandly.serve.agentcore_app import _TaskStore

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
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("hi"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]

    assert (await invoke({"action": "poll"}))["status"] == "error"  # missing taskId
    res = await invoke({"action": "poll", "taskId": "missing"})
    assert res["status"] == "unknown" and res["taskId"] == "missing"


def test_github_target_extracts_pr_url_and_repo():
    from strandly.serve.agentcore_app import _github_target

    ctx = {
        "repository": "strands-agents/sdk-python",
        "event": {"pull_request": {"html_url": "https://github.com/strands-agents/sdk-python/pull/42"}},
    }
    url, repo = _github_target(ctx)
    assert url == "https://github.com/strands-agents/sdk-python/pull/42"
    assert repo == "strands-agents/sdk-python"


def test_github_target_falls_back_to_issue_and_event_repository():
    from strandly.serve.agentcore_app import _github_target

    ctx = {
        "event": {
            "issue": {"html_url": "https://github.com/o/r/issues/7"},
            "repository": {"full_name": "o/r"},
        }
    }
    url, repo = _github_target(ctx)
    assert url.endswith("/issues/7") and repo == "o/r"


def test_github_target_handles_missing_context():
    from strandly.serve.agentcore_app import _github_target

    assert _github_target(None) == (None, None)
    assert _github_target({"prompt": "hi"}) == (None, None)


def test_trace_id_falls_back_to_xray_header(monkeypatch):
    # No active OTel span (test env) → falls back to the X-Ray env var, else None.
    from strandly.serve import agentcore_app as agentcore

    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=x")
    assert agentcore._trace_id() == "1-5759e988-bd862e3fe1be46a994272793"
    monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
    assert agentcore._trace_id() is None


def test_xray_id_from_otel_format():
    # A 128-bit OTel trace id renders as an X-Ray id: 1-<8 hex>-<24 hex>.
    from strandly.serve import agentcore_app as agentcore

    otel = int("6a3f0fc5550b3c5d3b6ec25c6e8dcfd5", 16)
    assert agentcore._xray_id_from_otel(otel) == "1-6a3f0fc5-550b3c5d3b6ec25c6e8dcfd5"


def test_trace_id_prefers_live_otel_span(monkeypatch):
    # The fix: when a valid span context exists (runtime runs under opentelemetry-instrument),
    # _trace_id uses it — even though the X-Ray env var is empty in the fire-and-forget task.
    from strandly.serve import agentcore_app as agentcore

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
    from strandly.core.events import classify_all

    class _Metrics:
        accumulated_usage = {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15}

    class _Result:
        stop_reason = "end_turn"
        metrics = _Metrics()

    (ev,) = classify_all({"result": _Result()}, {})
    assert ev.kind == "done"
    assert ev.data["stop_reason"] == "end_turn"
    assert ev.data["usage"] == {"input": 12, "output": 3, "total": 15}


def test_done_event_carries_cache_token_usage():
    from strandly.core.events import classify_all

    class _Metrics:
        accumulated_usage = {
            "inputTokens": 12,
            "outputTokens": 3,
            "totalTokens": 1515,
            "cacheReadInputTokens": 1000,
            "cacheWriteInputTokens": 500,
        }

    class _Result:
        stop_reason = "end_turn"
        metrics = _Metrics()

    (ev,) = classify_all({"result": _Result()}, {})
    assert ev.data["usage"] == {
        "input": 12, "output": 3, "total": 1515, "cacheRead": 1000, "cacheWrite": 500,
    }


def test_done_event_without_metrics_has_no_usage():
    from strandly.core.events import classify_all

    (ev,) = classify_all({"result": {"stop_reason": "end_turn"}}, {})
    assert ev.kind == "done" and "usage" not in ev.data


def test_classify_maps_the_sdk_turn_boundary_to_turn_start():
    """``{"init_event_loop": True}`` opens every turn of a run — including a resumed one.

    That is the only marker on the live channel separating the answer to the original ask from the
    answer to a resumed turn (both arrive inside one ``stream_async`` call),
    so the dashboard renders them as two bubbles instead of one merged blob. The SDK merges the
    invocation state into this event's dict, which is why the check runs before every other branch:
    an invocation-state key must not be able to route the boundary somewhere else.
    """
    from strandly.core.events import classify_all

    (ev,) = classify_all({"init_event_loop": True}, {})
    assert ev.kind == "turn_start" and ev.text is None

    # With invocation state merged in (InitEventLoopEvent.prepare) it is still a boundary — even when
    # the state carries a key another branch would otherwise claim.
    (ev,) = classify_all({"init_event_loop": True, "data": "not a text delta"}, {})
    assert ev.kind == "turn_start"


def test_classify_drops_tool_stream_events():
    """A tool's intermediate yields never reach a surface: the tool result carries the output."""
    from strandly.core.events import classify_all

    def stream(data):
        return {
            "type": "tool_stream",
            "tool_stream_event": {"tool_use": {"toolUseId": "t1", "name": "x"}, "data": data},
        }

    assert classify_all(stream("the final text"), {}) == []
    assert classify_all(stream({"kind": "text", "text": "progress"}), {}) == []
    assert classify_all(stream({"event": {"contentBlockDelta": {}}, "agent": object()}), {}) == []
    assert classify_all({"type": "tool_stream"}, {}) == []
    assert classify_all({"type": "tool_stream", "tool_stream_event": "junk"}, {}) == []


def test_a_streamed_tool_use_is_filled_in_by_the_assistant_messages_complete_input():
    """The live ``tool_start`` fires on the FIRST input delta — before the arguments exist — so on
    its own every streamed tool call rendered empty (the dashboard bug: a session's live view showed
    "(empty)" for every tool use while a reload, reading durable Memory, showed the input fine, and
    tool results worked all along). The assistant message carries the complete parsed input, so
    ``classify_all`` re-surfaces it as a ``tool_input`` event for the same ``toolUseId`` — the
    watcher's cue to fill the already-rendered block in."""
    from strandly.core.events import classify_all

    seen: dict[str, str] = {}
    # 1) The start, as live Bedrock streams it: first delta, no arguments yet.
    (start,) = classify_all(
        {
            "type": "tool_use_stream",
            "current_tool_use": {"toolUseId": "t1", "name": "shell", "input": ""},
        },
        seen,
    )
    assert start.kind == "tool_start" and start.tool_use_id == "t1"
    assert not start.data["input"]  # the bug's shape: the start carries no arguments

    # 2) Later deltas (the input filling in) still do not re-emit the start.
    later_delta = {
        "type": "tool_use_stream",
        "current_tool_use": {"toolUseId": "t1", "name": "shell", "input": '{"comm'},
    }
    assert classify_all(later_delta, seen) == []

    # 3) The assistant message lands with the complete parsed input -> a tool_input follow-up.
    (filled,) = classify_all(
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t1", "name": "shell", "input": {"command": "ls -la"}}}
                ],
            }
        },
        seen,
    )
    assert filled.kind == "tool_input"
    assert filled.tool == "shell" and filled.tool_use_id == "t1"
    assert filled.data["input"] == {"command": "ls -la"}


def test_an_unstreamed_tool_use_still_gets_its_start_from_the_assistant_message():
    """The assistant-message safety net (a provider that does not stream tool-use deltas) emits a
    ``tool_start`` that already carries the complete input — so no ``tool_input`` follows it."""
    from strandly.core.events import classify_all

    (start,) = classify_all(
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t9", "name": "shell", "input": {"command": "pwd"}}}
                ],
            }
        },
        {},
    )
    assert start.kind == "tool_start"
    assert start.data["input"] == {"command": "pwd"}


# ---------------------------------------------------------------------------
# Retries: the SDK re-issues a failed model call inside the turn (core/retries.py); _run drops the
# retried call's text on the `retry` event, and any error that escapes the turn fails the run.
# ---------------------------------------------------------------------------


def _turn_events(text: str):
    from strandly.core.events import HarnessEvent

    return [
        HarnessEvent(kind="text", text=text),
        HarnessEvent(kind="done", data={"usage": {"inputTokens": 1, "outputTokens": 2}}),
    ]


async def _drain_background():
    for _ in range(100):
        await asyncio.sleep(0)


async def _wait_for(predicate, *, what: str, timeout: float = 5.0) -> None:
    """Spin the loop until ``predicate()`` holds — for work that crosses a REAL thread boundary.

    ``_drain_background``'s 100 × ``sleep(0)`` is enough for pure-coroutine background work, but it
    is not enough for ``asyncio.to_thread`` (which the terminal-reaction POST goes through): those
    100 spins can all complete in less wall-clock time than it takes a worker thread to run and
    schedule its result back onto the loop. Yielding the loop N times does not make a thread run.

    A test that needs two sequential thread hops would assert on a half-posted list. Waiting on the
    observable condition — with a real, tiny sleep and a bounded deadline — is deterministic in the
    passing case and fails with a useful message instead of hanging in the broken one.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.mark.asyncio
async def test_run_retry_event_drops_only_the_retried_calls_text(monkeypatch):
    # The SDK re-issues a model call that died mid-stream (core/retries.py) and the stream carries
    # a `retry` event: text from earlier calls of the turn stays, the retried call's partial text
    # goes, and the turn is never re-run.
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        calls.append(user_input)

        async def gen():
            yield HarnessEvent(kind="text", text="Checking.")
            yield HarnessEvent(kind="tool_start", tool="shell", tool_use_id="t1")
            yield HarnessEvent(kind="tool_result", tool="shell", tool_use_id="t1", data={"status": "success"})
            yield HarnessEvent(kind="text", text="partial narration that must not leak")
            yield HarnessEvent(kind="retry", data={"delaySeconds": 8})
            for ev in _turn_events("recovered final answer"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "do the task", "sessionId": "s-retry"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"], "since": 0})
    assert poll["status"] == "completed"
    assert "partial narration" not in poll["result"]
    assert poll["result"].startswith("Checking.") and poll["result"].endswith("recovered final answer")
    assert calls == ["do the task"]
    assert {"kind": "retry"}.items() <= next(e for e in poll["events"] if e["kind"] == "retry").items()


@pytest.mark.asyncio
async def test_run_transient_error_that_escapes_the_turn_fails_the_run(monkeypatch):
    # The SDK already retried the model call; an error that escapes the turn is final.
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        calls.append(user_input)

        async def gen():
            yield _turn_events("partial")[0]
            raise ConnectionResetError(104, "Connection reset by peer")

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-reset"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "failed" and "ConnectionResetError" in poll["error"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_non_transient_error_fails_immediately_no_retry(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        calls.append(user_input)

        async def gen():
            raise KeyError("usage")  # a real bug — must NOT be retried
            yield  # pragma: no cover — makes this an async generator

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-bug"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "failed" and "KeyError" in poll["error"]
    assert len(calls) == 1  # exactly one attempt


@pytest.mark.asyncio
async def test_run_error_event_non_transient_fails_run(monkeypatch):
    # The reported production bug: expired credentials killed the stream mid-run, translate
    # normalized it into an error event, the loop swallowed it, and the ledger said "completed".
    # A non-transient error event must land the run in "failed" — visibly, on the first attempt.
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        calls.append(user_input)

        async def gen():
            yield HarnessEvent(kind="text", text="I was working on it…")
            yield HarnessEvent(
                kind="error",
                text="The security token included in the request is expired",
                data={"error_type": "ExpiredTokenException"},
            )

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-cred"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "failed"
    assert "ExpiredTokenException" in poll["error"]
    assert len(calls) == 1  # permanent → no retry


@pytest.mark.asyncio
async def test_run_counts_tool_errors_and_stop_reason_into_ledger_and_metrics(monkeypatch):
    # A turn can "complete" while every tool call inside it failed (expired GitHub token, dead
    # sandbox). The run loop must count error-status tool_result events and thread the count plus
    # the terminal stop_reason into the ledger row and the ToolErrors EMF metric.
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent
    from strandly.ops import metrics as m

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)

    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            pass

        def finish(self, *a, **kw):
            finishes.append(kw)

        def fail(self, *a, **kw):
            pass

    emitted: list[dict] = []
    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    monkeypatch.setattr(
        ac.metrics, "emit", lambda doc, **kw: emitted.append(doc)
    )

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="tool_result",
                               data={"status": "error", "content": []})
            yield HarnessEvent(kind="tool_result",
                               data={"status": "success", "content": []})
            yield HarnessEvent(kind="tool_result",
                               data={"status": "error", "content": []})
            yield HarnessEvent(kind="text", text="I could not update the PR.")
            yield HarnessEvent(kind="done", data={"usage": {"total": 42}, "stop_reason": "end_turn"})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-tools"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed"

    (finish_kw,) = finishes
    assert finish_kw["tool_errors"] == 2
    assert finish_kw["stop_reason"] == "end_turn"
    # The model-preset name rides the terminal write — the dashboard's per-model token
    # breakdown attributes this run to the preset in effect (the constants default here).
    assert finish_kw["model"] == Config(values={}).model

    completed_docs = [d for d in emitted if m.COMPLETED in d]
    assert completed_docs and completed_docs[0][m.TOOL_ERRORS] == 2


@pytest.mark.asyncio
async def test_run_with_no_tool_errors_records_zero_not_absent(monkeypatch):
    # tool_errors=0 is written explicitly so a healthy post-upgrade run is distinguishable from a
    # pre-upgrade row that never carried the field.
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            pass

        def finish(self, *a, **kw):
            finishes.append(kw)

        def fail(self, *a, **kw):
            pass

    monkeypatch.setattr(ac, "RunLedger", FakeLedger)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            for ev in _turn_events("all done"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-clean"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed"
    (finish_kw,) = finishes
    assert finish_kw["tool_errors"] == 0


# ---------------------------------------------------------------------------
# Terminal lifecycle reactions (🚀 completed / 😕 failed) on the dispatched mention.
# The poller forwards the mention's reactions endpoint as ackReactionsUrl; _run
# posts the terminal signal there. Best-effort end to end — see ops.github.
# ---------------------------------------------------------------------------


def _reaction_recorder(monkeypatch):
    import strandly.serve.agentcore_app as ac

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        ac, "add_reaction", lambda url, token, content: (calls.append((url, token, content)), True)[1]
    )
    return calls


_ACK_URL = "https://api.github.com/repos/o/r/issues/comments/9/reactions"


@pytest.mark.asyncio
async def test_run_posts_completed_reaction(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.ops.github import REACTION_COMPLETED

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls = _reaction_recorder(monkeypatch)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            for ev in _turn_events("all done"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={"STRANDLY_GITHUB_TOKEN": "gh-tok"}))
    res = await app.handlers["main"](
        {"prompt": "task", "sessionId": "s-ok", "ackReactionsUrl": _ACK_URL}
    )
    assert res["status"] == "accepted"
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed"
    # 🚀 on the same item the poller 👀-acked, with the runtime's own GitHub token.
    await _wait_for(lambda: len(calls) == 1, what="the terminal reaction (a real thread hop)")
    assert calls == [(_ACK_URL, "gh-tok", REACTION_COMPLETED)]


@pytest.mark.asyncio
async def test_run_posts_failed_reaction_on_terminal_failure(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.ops.github import REACTION_FAILED

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls = _reaction_recorder(monkeypatch)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            raise ValueError("real bug — non-transient, fails the run immediately")
            yield  # pragma: no cover — makes this an async generator

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={"STRANDLY_GITHUB_TOKEN": "gh-tok"}))
    res = await app.handlers["main"](
        {"prompt": "task", "sessionId": "s-fail", "ackReactionsUrl": _ACK_URL}
    )
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "failed"
    # 😕 = "failed terminally, don't wait for a reply" — only on the *terminal* failure.
    await _wait_for(lambda: len(calls) == 1, what="the terminal reaction (a real thread hop)")
    assert calls == [(_ACK_URL, "gh-tok", REACTION_FAILED)]


@pytest.mark.asyncio
async def test_run_no_reaction_without_ack_url_or_token(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls = _reaction_recorder(monkeypatch)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            for ev in _turn_events("fine"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)

    # No ackReactionsUrl (a non-mention invoke, e.g. `strandly run`) → nothing to react on.
    app = ac.build_app(Config(values={"STRANDLY_GITHUB_TOKEN": "gh-tok"}))
    await app.handlers["main"]({"prompt": "task", "sessionId": "s-1"})
    # Non-string ackReactionsUrl (doctored payload) → ignored.
    await app.handlers["main"](
        {"prompt": "task", "sessionId": "s-2", "ackReactionsUrl": {"evil": True}}
    )
    await _drain_background()
    assert calls == []

    # URL present but the runtime has no GitHub token → silently skipped.
    app2 = ac.build_app(Config(values={}))
    await app2.handlers["main"](
        {"prompt": "task", "sessionId": "s-3", "ackReactionsUrl": _ACK_URL}
    )
    await _drain_background()
    assert calls == []


@pytest.mark.asyncio
async def test_run_reaction_failure_never_breaks_the_run(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)

    def explode(url, token, content):
        raise RuntimeError("reaction endpoint exploded")

    monkeypatch.setattr(ac, "add_reaction", explode)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            for ev in _turn_events("still the answer"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={"STRANDLY_GITHUB_TOKEN": "gh-tok"}))
    res = await app.handlers["main"](
        {"prompt": "task", "sessionId": "s-boom", "ackReactionsUrl": _ACK_URL}
    )
    await _drain_background()

    # The reaction is telemetry: its failure must not flip a completed run to failed.
    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed"
    assert poll["result"] == "still the answer"


def test_task_store_live_events_buffer_and_subscribers():
    # The live channel: emit assigns monotonic seqs, buffers for since-cursor catch-up, and fans
    # out to subscribers; finish/fail terminate every subscription with a None marker.
    from strandly.serve.agentcore_app import _TaskStore

    store = _TaskStore()
    store.emit("ghost", {"kind": "text"})  # unknown task: silently ignored, never raises
    store.start("t1")
    q = store.subscribe("t1")
    store.emit("t1", {"kind": "text", "text": "he"})
    store.emit("t1", {"kind": "text", "text": "llo"})

    events, next_seq = store.events_since("t1", 0)
    assert [e["seq"] for e in events] == [0, 1] and next_seq == 2
    tail, _ = store.events_since("t1", 1)
    assert [e["text"] for e in tail] == ["llo"]  # cursor skips what the client already has

    assert q.get_nowait()["text"] == "he"  # subscriber got the live fanout too
    assert q.get_nowait()["text"] == "llo"
    store.finish("t1", "hello")
    assert q.get_nowait() is None  # end-of-run marker closes the attachment

    # Unsubscribed queues stop receiving.
    store.start("t2")
    q2 = store.subscribe("t2")
    store.unsubscribe("t2", q2)
    store.emit("t2", {"kind": "text", "text": "x"})
    assert q2.empty()


def test_task_store_event_buffer_is_bounded():
    import strandly.serve.agentcore_app as ac
    from strandly.serve.agentcore_app import _TaskStore

    store = _TaskStore()
    store.start("t1")
    for i in range(ac._MAX_EVENTS_PER_TASK + 25):
        store.emit("t1", {"kind": "text", "text": str(i)})
    events, next_seq = store.events_since("t1", 0)
    assert len(events) == ac._MAX_EVENTS_PER_TASK  # oldest trimmed, not unbounded
    assert next_seq == ac._MAX_EVENTS_PER_TASK + 25  # seq keeps counting across the trim
    assert events[0]["seq"] == 25  # a poller that fell behind skips the trimmed window


def test_event_dict_clips_bulky_payloads():
    # Tool inputs and tool_result text blocks are clipped for the live channel; the durable copy
    # (Memory/CloudWatch) keeps the full data.
    from types import SimpleNamespace

    import strandly.serve.agentcore_app as ac

    big = "x" * (ac._EVENT_CLIP + 500)
    ev = SimpleNamespace(
        kind="tool_call", text=None, tool="shell",
        tool_use_id="tu1", data={"input": {"command": big}},
    )
    out = ac._event_dict(ev)
    assert len(out["data"]["input"]) == ac._EVENT_CLIP and out["data"]["input"].endswith("…")

    ev2 = SimpleNamespace(
        kind="tool_result", text=None, tool="shell", tool_use_id="tu1",
        data={"status": "success", "content": [{"text": big}, {"json": {"k": 1}}]},
    )
    out2 = ac._event_dict(ev2)
    assert len(out2["data"]["content"][0]["text"]) == ac._EVENT_CLIP
    assert out2["data"]["content"][1] == {"json": {"k": 1}}  # non-text blocks untouched


def test_poll_result_since_cursor_returns_live_events():
    from strandly.serve.agentcore_app import _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()
    store.start("t1")
    store.emit("t1", {"kind": "text", "text": "he"})
    store.emit("t1", {"kind": "text", "text": "llo"})

    res = poll_result(cfg, store, "t1", None, since=0)
    assert res["status"] == "running"
    assert [e["text"] for e in res["events"]] == ["he", "llo"] and res["nextSeq"] == 2

    res = poll_result(cfg, store, "t1", None, since=2)  # caught up: empty window, same cursor
    assert res["events"] == [] and res["nextSeq"] == 2

    classic = poll_result(cfg, store, "t1", None)  # no cursor -> classic status-only shape
    assert "events" not in classic and "nextSeq" not in classic


def test_a_tail_cursor_attaches_at_the_live_head_without_the_backlog():
    """``since = TAIL_CURSOR`` is "no backlog, just the cursor" — what a *reattach* needs.

    A client that joins a run already in flight (the dashboard sending a message into a session whose
    run started before the page loaded) has the earlier turns from the durable transcript already.
    Replaying them redrew a finished answer as a new bubble, and a full buffer
    (``_MAX_EVENTS_PER_TASK`` × ``_EVENT_CLIP``) is megabytes that a Lambda-mediated poll cannot even
    return. The cursor it hands back must be the live head, so the very next poll is incremental.
    """
    from strandly.serve.agentcore_app import TAIL_CURSOR, _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()
    store.start("t1")
    store.emit("t1", {"kind": "text", "text": "the answer you already have"})
    store.emit("t1", {"kind": "text", "text": " …and its tail"})

    attach = poll_result(cfg, store, "t1", None, since=TAIL_CURSOR)
    assert attach["events"] == [], "the tail attach replayed the buffer"
    assert attach["nextSeq"] == 2, "the tail attach did not hand back the live head"

    # ...and from that cursor on it is an ordinary incremental poll.
    store.emit("t1", {"kind": "text", "text": "what happened after I attached"})
    live = poll_result(cfg, store, "t1", None, since=attach["nextSeq"])
    assert [e["text"] for e in live["events"]] == ["what happened after I attached"]
    assert live["nextSeq"] == 3

    # An unknown/evicted task still answers with a usable cursor rather than raising.
    gone = poll_result(cfg, store, "nope", None, since=TAIL_CURSOR)
    assert gone["events"] == [] and gone["nextSeq"] == 0


@pytest.mark.asyncio
async def test_agentcore_stream_first_frame_is_init_and_events_are_replayable(
    monkeypatch, text_model
):
    # Streaming mode: frame 0 is {kind:"init", taskId} so a cut-off client can re-attach via the
    # since-cursor poll; every live frame carries a monotonic seq; the poll replays the same run.
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    pytest.importorskip("bedrock_agentcore")
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("pong"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]

    gen = await invoke({"prompt": "ping", "mode": "stream"})
    events = [e async for e in gen]
    assert events[0]["kind"] == "init"
    task_id = events[0]["data"]["taskId"]
    assert task_id
    seqs = [e["seq"] for e in events[1:]]
    assert seqs == list(range(len(seqs)))  # gapless, ordered

    # The since-cursor poll replays the exact same events (catch-up after a disconnect).
    res = await invoke({"action": "poll", "taskId": task_id, "since": 0})
    assert res["status"] == "completed" and "pong" in res["result"]
    assert [e["seq"] for e in res["events"]] == seqs
    assert res["nextSeq"] == len(seqs)

    # Garbage cursor degrades to the classic status-only poll, never an error.
    res2 = await invoke({"action": "poll", "taskId": task_id, "since": "garbage"})
    assert res2["status"] == "completed" and "events" not in res2


@pytest.mark.asyncio
async def test_agentcore_stream_client_disconnect_never_stops_the_run(monkeypatch, text_model):
    # THE issue-402 property: the run executes as a detached background task; the SSE generator
    # only watches it. Closing the generator (client cut the connection) must not stop the agent —
    # the run finishes and its result + full event log stay pollable.
    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    pytest.importorskip("bedrock_agentcore")
    from strandly.serve.agentcore_app import build_app

    use_model(monkeypatch, lambda _model: text_model("pong"))
    app = build_app(Config(values={}))
    invoke = app.handlers["main"]

    gen = await invoke({"prompt": "ping", "mode": "stream"})
    first = await gen.__anext__()
    assert first["kind"] == "init"
    task_id = first["data"]["taskId"]
    await gen.aclose()  # client disconnects right after the handshake

    res = {}
    for _ in range(500):
        res = await invoke({"action": "poll", "taskId": task_id, "since": 0})
        if res["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.01)
    assert res["status"] == "completed" and "pong" in res["result"]
    assert any(e["kind"] == "done" for e in res["events"])  # full run captured despite disconnect


# ---------------------------------------------------------------------------
# One run per session: a new message supersedes the running turn (per-run
# cancel signal), the next run sees the cancelled run's messages.
# ---------------------------------------------------------------------------


def _app(monkeypatch, run_turn_fake, values=None):
    import strandly.serve.agentcore_app as ac

    monkeypatch.setattr(ac, "run_turn", run_turn_fake)
    return ac.build_app(Config(values=values or {}))


def _gated_run_turn(calls: list[str], gate: asyncio.Event, signals: list | None = None):
    """A fake ``run_turn`` whose FIRST turn holds until ``gate`` is set or its cancel signal fires."""

    def fake_run_turn(config, user_input, ctx, *, cancel_signal=None, **_):
        async def gen():
            calls.append(user_input)
            if signals is not None:
                signals.append(cancel_signal)
            if len(calls) == 1:
                while not gate.is_set() and not (cancel_signal and cancel_signal.is_set()):
                    await asyncio.sleep(0)
            for ev in _turn_events(f"answer to {user_input}"):
                yield ev

        return gen()

    return fake_run_turn


async def _poll(invoke, task_id, session_id=None):
    return await invoke({"action": "poll", "taskId": task_id, "sessionId": session_id})


async def _wait_status(invoke, task_id, status, session_id=None):
    for _ in range(500):
        res = await _poll(invoke, task_id, session_id)
        if res["status"] == status:
            return res
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached {status!r}: {res}")


def _blocking_model():
    """A model whose first call streams until ``.release`` is set; every request's messages kept."""
    from scripted_model import ScriptedModel, text_events

    class BlockingModel(ScriptedModel):
        def __init__(self):
            super().__init__()
            self.requests: list[list] = []
            self.release = asyncio.Event()

        async def stream(self, messages, *args, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            if len(self.requests) == 1:
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {}}}
                while not self.release.is_set():
                    yield {"contentBlockDelta": {"delta": {"text": "."}}}
                    await asyncio.sleep(0.01)
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                return
            for ev in text_events(f"answer {len(self.requests)}"):
                yield ev

    return BlockingModel()


def _texts_of(messages) -> list[str]:
    return [
        block["text"]
        for m in messages
        for block in m.get("content", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]


def _actor_requests(model) -> list[list]:
    """The model requests made by the session's agent (the goal-loop critic shares the model)."""
    return [r for r in model.requests if not _texts_of(r[:1])[:1] or not _texts_of(r[:1])[0].startswith("Goal:")]


def _real_agent_app(monkeypatch, model):
    from strandly.serve.agentcore_app import build_app

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    use_model(monkeypatch, lambda _model: model)
    return build_app(Config(values={}))


async def _wait_requests(model, n):
    for _ in range(500):
        if len(model.requests) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} model request(s), saw {len(model.requests)}")


@pytest.mark.asyncio
async def test_a_second_message_supersedes_the_running_turn_and_sees_its_messages(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    model = _blocking_model()
    invoke = _real_agent_app(monkeypatch, model).handlers["main"]

    first = await invoke({"prompt": "first ask", "sessionId": "s-sup"})
    await _wait_requests(model, 1)
    second = await invoke({"prompt": "second ask", "sessionId": "s-sup"})
    assert first["status"] == second["status"] == "accepted"
    assert first["taskId"] != second["taskId"]

    await _wait_status(invoke, second["taskId"], "completed")
    assert (await _poll(invoke, first["taskId"]))["status"] == "cancelled"
    seen = _texts_of(_actor_requests(model)[-1])
    assert "first ask" in seen and "second ask" in seen
    assert seen.index("first ask") < seen.index("second ask")


@pytest.mark.asyncio
async def test_three_rapid_messages_the_middle_one_makes_no_model_call(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    from strandly.serve.turn import SUPERSEDED_MARKER

    model = _blocking_model()
    invoke = _real_agent_app(monkeypatch, model).handlers["main"]

    first = await invoke({"prompt": "ask one", "sessionId": "s-three"})
    await _wait_requests(model, 1)
    second = await invoke({"prompt": "ask two", "sessionId": "s-three"})
    third = await invoke({"prompt": "ask three", "sessionId": "s-three"})

    await _wait_status(invoke, third["taskId"], "completed")
    assert (await _poll(invoke, first["taskId"]))["status"] == "cancelled"
    assert (await _poll(invoke, second["taskId"]))["status"] == "cancelled"
    # One request for the first turn, one for the third — the superseded middle turn made none.
    assert len(_actor_requests(model)) == 2
    seen = _texts_of(_actor_requests(model)[-1])
    for text in ("ask one", "ask two", SUPERSEDED_MARKER, "ask three"):
        assert text in seen, (text, seen)


@pytest.mark.asyncio
async def test_a_cancel_after_the_run_finished_does_not_affect_the_next_run(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    model = _blocking_model()
    model.release.set()
    invoke = _real_agent_app(monkeypatch, model).handlers["main"]

    first = await invoke({"prompt": "one", "sessionId": "s-late-cancel"})
    await _wait_status(invoke, first["taskId"], "completed")
    stop = await invoke({"action": "cancel", "sessionId": "s-late-cancel"})
    assert stop == {"status": "idle", "sessionId": "s-late-cancel"}

    second = await invoke({"prompt": "two", "sessionId": "s-late-cancel"})
    await _wait_status(invoke, second["taskId"], "completed")
    assert len(_actor_requests(model)) == 2
    assert "two" in _texts_of(_actor_requests(model)[-1])


@pytest.mark.asyncio
async def test_stop_reaches_a_slack_dm_run(monkeypatch):
    """The DM's agent is cached under ``<session>#slack-user-…``; Stop still finds its run."""
    pytest.importorskip("bedrock_agentcore")
    from strandly.serve.cache import agent_cache_key, session_key

    payload = {
        "prompt": "hello",
        "sessionId": "slack-T1-D123",
        "slackChannelId": "D123",
        "slackUserId": "U1",
        "triggerSource": "slack:U1",
    }
    ctx = RuntimeContext(session_id=payload["sessionId"], event=payload)
    assert agent_cache_key(ctx) != session_key(ctx)

    model = _blocking_model()
    invoke = _real_agent_app(monkeypatch, model).handlers["main"]
    run = await invoke(payload)
    await _wait_requests(model, 1)

    stop = await invoke({"action": "cancel", "sessionId": "slack-T1-D123"})
    assert stop == {"status": "cancelled", "sessionId": "slack-T1-D123", "taskId": run["taskId"]}
    await _wait_status(invoke, run["taskId"], "cancelled")
    assert (await invoke({"action": "cancel", "sessionId": "slack-T1-D123"}))["status"] == "idle"


@pytest.mark.asyncio
async def test_cancel_action_requires_a_session_and_reports_an_idle_one(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    invoke = _app(monkeypatch, _gated_run_turn([], asyncio.Event())).handlers["main"]

    assert (await invoke({"action": "cancel"}))["status"] == "error"
    assert await invoke({"action": "cancel", "sessionId": "s-idle"}) == {
        "status": "idle",
        "sessionId": "s-idle",
    }


@pytest.mark.asyncio
async def test_reject_strategy_refuses_while_busy(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    calls: list[str] = []
    signals: list = []
    gate = asyncio.Event()
    invoke = _app(monkeypatch, _gated_run_turn(calls, gate, signals)).handlers["main"]

    first = await invoke({"prompt": "daily job", "sessionId": "s-cron"})
    await _drain_background()
    tick = await invoke({"prompt": "daily job", "sessionId": "s-cron", "queueStrategy": "reject"})
    assert tick == {"status": "rejected", "taskId": first["taskId"], "sessionId": "s-cron"}
    assert not signals[0].is_set()

    gate.set()
    await _wait_status(invoke, first["taskId"], "completed")
    assert calls == ["daily job"]
    # Idle again: the same request now starts a run.
    again = await invoke({"prompt": "daily job", "sessionId": "s-cron", "queueStrategy": "reject"})
    assert again["status"] == "accepted"
    await _wait_status(invoke, again["taskId"], "completed")


@pytest.mark.asyncio
async def test_a_blank_prompt_does_not_cancel_the_running_turn(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    calls: list[str] = []
    signals: list = []
    gate = asyncio.Event()
    invoke = _app(monkeypatch, _gated_run_turn(calls, gate, signals)).handlers["main"]

    first = await invoke({"prompt": "real work", "sessionId": "s-blank"})
    await _drain_background()
    refused = await invoke({"prompt": "   ", "sessionId": "s-blank"})
    assert refused.status_code == 400
    assert not signals[0].is_set()

    gate.set()
    await _wait_status(invoke, first["taskId"], "completed")
    assert calls == ["real work"]


@pytest.mark.asyncio
async def test_a_redispatch_of_the_running_trigger_is_a_no_op(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    calls: list[str] = []
    signals: list = []
    gate = asyncio.Event()
    invoke = _app(monkeypatch, _gated_run_turn(calls, gate, signals)).handlers["main"]
    mention = {"prompt": "@strandly review", "sessionId": "gh-o-r-pr-1", "triggerId": _ACK_URL}

    first = await invoke(dict(mention))
    await _drain_background()
    again = await invoke(dict(mention))
    assert again == {
        "status": "accepted",
        "taskId": first["taskId"],
        "sessionId": "gh-o-r-pr-1",
        "duplicate": True,
    }
    assert not signals[0].is_set()

    # An EDITED mention (same trigger, new text) supersedes the running turn.
    edited = await invoke({**mention, "prompt": "@strandly review, focus on tests"})
    assert edited["status"] == "accepted" and edited["taskId"] != first["taskId"]
    assert signals[0].is_set()
    await _wait_status(invoke, edited["taskId"], "completed")
    assert (await _poll(invoke, first["taskId"]))["status"] == "cancelled"
    assert calls == ["@strandly review", "@strandly review, focus on tests"]


@pytest.mark.asyncio
async def test_a_superseded_run_is_cancelled_in_the_ledger_without_reaction_or_work_product(
    monkeypatch,
):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.ops import metrics as m
    from strandly.ops.github import REACTION_COMPLETED

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            pass

        def finish(self, task_id, **kw):
            finishes.append({"task_id": task_id, **kw})

        def fail(self, *a, **kw):  # pragma: no cover - a failure here is the bug
            raise AssertionError("a superseded run is not a failure")

    persisted: list = []
    emitted: list[dict] = []
    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    monkeypatch.setattr(ac, "_persist_work_product", lambda *a, **kw: persisted.append(a))
    monkeypatch.setattr(ac.WorkProductStore, "from_config", staticmethod(lambda c: object()))
    monkeypatch.setattr(ac.metrics, "emit", lambda doc, **kw: emitted.append(doc))
    reactions = _reaction_recorder(monkeypatch)

    block = '```work-product\n{"kind": "review", "verdict": "approve"}\n```'

    def fake_run_turn(config, user_input, ctx, *, cancel_signal=None, **_):
        async def gen():
            from strandly.core.events import HarnessEvent

            yield HarnessEvent(kind="text", text=f"partial {user_input}\n{block}")
            while user_input == "one" and not cancel_signal.is_set():
                await asyncio.sleep(0)
            yield HarnessEvent(kind="done", data={})

        return gen()

    invoke = _app(monkeypatch, fake_run_turn, {"STRANDLY_GITHUB_TOKEN": "gh-tok"}).handlers["main"]
    first = await invoke({"prompt": "one", "sessionId": "s-l", "ackReactionsUrl": _ACK_URL})
    await _drain_background()
    second = await invoke({"prompt": "two", "sessionId": "s-l", "ackReactionsUrl": _ACK_URL})
    await _wait_status(invoke, second["taskId"], "completed")
    await _wait_for(lambda: len(reactions) == 1, what="the newer run's reaction")

    by_task = {f["task_id"]: f for f in finishes}
    assert by_task[first["taskId"]]["status"] == "cancelled"
    assert by_task[second["taskId"]]["status"] == "completed"
    assert "work-product" not in by_task[first["taskId"]]["result"]
    assert len(persisted) == 1  # only the completed run's artifact
    assert reactions == [(_ACK_URL, "gh-tok", REACTION_COMPLETED)]
    assert any(doc.get(m.CANCELLED) == 1 for doc in emitted), emitted


@pytest.mark.asyncio
async def test_a_stop_during_the_background_wait_records_the_run_cancelled(monkeypatch):
    """The model's last answer ended its turn (``end_turn``) and the SDK is waiting on a background
    subagent when Stop lands: the turn reports the work it abandoned, so the run is cancelled."""
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.ops import metrics as m

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            pass

        def finish(self, task_id, **kw):
            finishes.append({"task_id": task_id, **kw})

        def fail(self, *a, **kw):  # pragma: no cover - a failure here is the bug
            raise AssertionError("a stopped run is not a failure")

    emitted: list[dict] = []
    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    monkeypatch.setattr(ac.metrics, "emit", lambda doc, **kw: emitted.append(doc))
    waiting = asyncio.Event()

    def fake_run_turn(config, user_input, ctx, *, cancel_signal=None, outcome=None, **_):
        async def gen():
            from strandly.core.events import HarnessEvent

            yield HarnessEvent(kind="text", text="waiting on the subagent")
            yield HarnessEvent(kind="done", data={"stop_reason": "end_turn"})
            waiting.set()
            while not cancel_signal.is_set():
                await asyncio.sleep(0)
            outcome["abandoned"] = True

        return gen()

    invoke = _app(monkeypatch, fake_run_turn).handlers["main"]
    run = await invoke({"prompt": "review it", "sessionId": "s-bg-stop"})
    await asyncio.wait_for(waiting.wait(), 5)
    stop = await invoke({"action": "cancel", "sessionId": "s-bg-stop"})
    assert stop["status"] == "cancelled"
    await _wait_status(invoke, run["taskId"], "cancelled")

    await _wait_for(lambda: finishes, what="the ledger finish")
    assert finishes[0]["status"] == "cancelled"
    assert finishes[0]["stop_reason"] == "end_turn"
    assert any(doc.get(m.CANCELLED) == 1 for doc in emitted), emitted
    assert not any(doc.get(m.COMPLETED) for doc in emitted), emitted


@pytest.mark.asyncio
async def test_a_message_during_after_invocation_hooks_does_not_cancel_a_finished_answer(monkeypatch):
    """The answer is complete when a new message lands while after-invocation hooks (critic,
    snapshot save) still run: the run stays completed and keeps its work product."""
    pytest.importorskip("bedrock_agentcore")
    from strands.hooks import AfterInvocationEvent

    import strandly.serve.agentcore_app as ac
    import strandly.serve.turn as turn_mod
    from scripted_model import ScriptedModel, text_events

    block = '```work-product\n{"kind": "review", "verdict": "approve"}\n```'
    model = ScriptedModel(text_events(f"THE FULL ANSWER\n{block}"), text_events("second answer"))
    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            pass

        def finish(self, task_id, **kw):
            finishes.append({"task_id": task_id, **kw})

        def fail(self, *a, **kw):  # pragma: no cover - a failure here is the bug
            raise AssertionError("not a failure")

    persisted: list = []
    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    monkeypatch.setattr(ac, "_persist_work_product", lambda *a, **kw: persisted.append(kw))
    monkeypatch.setattr(ac.WorkProductStore, "from_config", staticmethod(lambda c: object()))

    in_hook = asyncio.Event()
    release_hook = asyncio.Event()
    build = turn_mod._build_session_agent

    async def build_with_slow_after_hook(*a, **kw):
        agent = await build(*a, **kw)

        async def slow_after(event: AfterInvocationEvent) -> None:
            if not in_hook.is_set():
                in_hook.set()
                await release_hook.wait()

        agent.hooks.add_callback(AfterInvocationEvent, slow_after)
        return agent

    monkeypatch.setattr(turn_mod, "_build_session_agent", build_with_slow_after_hook)
    invoke = _real_agent_app(monkeypatch, model).handlers["main"]

    first = await invoke({"prompt": "one", "sessionId": "s-race"})
    try:
        # Agent startup is not the race: hold the completed answer until the second
        # request has actually set its cancellation signal.
        await asyncio.wait_for(in_hook.wait(), 30)
        second = await invoke({"prompt": "two", "sessionId": "s-race"})
    finally:
        release_hook.set()

    done = await _wait_status(invoke, first["taskId"], "completed")
    assert done["result"] == "THE FULL ANSWER"
    await _wait_status(invoke, second["taskId"], "completed")
    by_task = {f["task_id"]: f for f in finishes}
    assert by_task[first["taskId"]]["status"] == "completed"
    assert [p["task_id"] for p in persisted] == [first["taskId"]]


@pytest.mark.asyncio
async def test_a_redispatch_rendered_at_a_later_time_is_still_a_no_op(monkeypatch):
    """Ingress prompts carry the dispatch time, so the runtime compares the trigger's fingerprint."""
    pytest.importorskip("bedrock_agentcore")
    import hashlib
    from datetime import datetime, timezone

    from strandly.ops.lambdas.mention_poller import handler as mentions

    calls: list[str] = []
    signals: list = []
    invoke = _app(monkeypatch, _gated_run_turn(calls, asyncio.Event(), signals)).handlers["main"]

    def payload(body: str, now: datetime) -> dict:
        mention = mentions.Mention(author="octocat", source="comment", timestamp="t", body=body)
        return {
            "prompt": mentions.build_prompt(mention, "o/r", True, 1, now),
            "sessionId": "gh-o-r-pr-1",
            "triggerId": _ACK_URL,
            "triggerHash": hashlib.sha256(body.encode()).hexdigest(),
        }

    t1 = datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 26, 10, 5, tzinfo=timezone.utc)
    first_payload, again_payload = payload("@strandly review", t1), payload("@strandly review", t2)
    assert first_payload["prompt"] != again_payload["prompt"]

    first = await invoke(first_payload)
    await _drain_background()
    again = await invoke(again_payload)
    assert again["duplicate"] is True and again["taskId"] == first["taskId"]
    assert not signals[0].is_set()

    edited = await invoke(payload("@strandly review the tests", t2))
    assert edited["taskId"] != first["taskId"]
    assert signals[0].is_set()
    await _wait_status(invoke, edited["taskId"], "completed")


@pytest.mark.asyncio
async def test_a_superseded_streamed_run_ends_with_a_cancelled_event(monkeypatch):
    """An attached stream learns the run was cancelled instead of taking its partial text as the answer."""
    pytest.importorskip("bedrock_agentcore")
    calls: list[str] = []
    invoke = _app(monkeypatch, _gated_run_turn(calls, asyncio.Event())).handlers["main"]

    stream = await invoke({"prompt": "one", "sessionId": "s-stream", "stream": True})
    seen: list[dict] = []

    async def watch():
        async for ev in stream:
            seen.append(ev)

    watcher = asyncio.create_task(watch())
    await _drain_background()
    second = await invoke({"prompt": "two", "sessionId": "s-stream"})
    await asyncio.wait_for(watcher, 5)
    await _wait_status(invoke, second["taskId"], "completed")

    assert seen[0]["kind"] == "init"
    assert seen[-1]["kind"] == "cancelled"
    polled = await invoke({"action": "poll", "taskId": seen[0]["data"]["taskId"], "since": 0})
    assert polled["status"] == "cancelled"
    assert polled["events"][-1]["kind"] == "cancelled"


@pytest.mark.asyncio
async def test_a_supersede_during_a_slow_sandbox_command_ends_it_and_the_next_run_sees_why(
    monkeypatch, tmp_path
):
    """The sandbox stops waiting on a superseded turn's command; the next run sees the cancelled
    tool result and the interrupted-turn marker."""
    pytest.importorskip("bedrock_agentcore")
    import threading
    import time

    import strandly.serve.turn as turn_mod
    from harness.sandbox.test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient
    from scripted_model import ScriptedModel, text_events, tool_use_events
    from strandly.sandbox.runtime import CANCELLED_NOTE, RuntimeSandbox
    from strandly.serve.turn import TURN_INTERRUPTED_MARKER

    release = threading.Event()
    command_started = threading.Event()

    class Slow(FakeAgentCoreClient):
        def invoke_agent_runtime_command(self, **kwargs):
            if "sleep 600" not in kwargs["body"]["command"]:
                return super().invoke_agent_runtime_command(**kwargs)

            def slow():
                command_started.set()
                yield {"chunk": {"contentStart": {}}}
                release.wait(30)
                yield {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}

            return {"stream": slow()}

    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN,
        session_id="a" * 40,
        workdir=str(tmp_path),
        client=Slow(workdir=str(tmp_path)),
    )
    build = turn_mod._build_session_agent

    async def build_on_the_runtime_sandbox(*a, **kw):
        agent = await build(*a, **kw)
        agent._sandbox = sandbox
        return agent

    monkeypatch.setattr(turn_mod, "_build_session_agent", build_on_the_runtime_sandbox)

    class Recording(ScriptedModel):
        def __init__(self, *responses):
            super().__init__(*responses)
            self.requests: list[list] = []

        async def stream(self, messages, *args, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            async for ev in super().stream(messages, *args, **kwargs):
                yield ev

    model = Recording(
        tool_use_events("shell", "t1", json.dumps({"command": "sleep 600", "timeout": 900})),
        text_events("second answer"),
    )
    invoke = _real_agent_app(monkeypatch, model).handlers["main"]
    try:
        first = await invoke({"prompt": "run the slow thing", "sessionId": "s-slow-tool"})
        for _ in range(500):
            if command_started.is_set():
                break
            await asyncio.sleep(0.01)
        started = time.monotonic()
        second = await invoke({"prompt": "never mind", "sessionId": "s-slow-tool"})
        await _wait_status(invoke, second["taskId"], "completed")
        assert time.monotonic() - started < 10
    finally:
        release.set()

    assert (await _poll(invoke, first["taskId"]))["status"] == "cancelled"
    seen = json.dumps(_actor_requests(model)[-1])
    assert "toolResult" in seen
    assert json.dumps(CANCELLED_NOTE.strip())[1:-1] in seen
    assert TURN_INTERRUPTED_MARKER in _texts_of(_actor_requests(model)[-1])
    assert "never mind" in _texts_of(_actor_requests(model)[-1])


@pytest.mark.asyncio
async def test_the_skip_hook_runs_before_every_other_before_model_hook(monkeypatch):
    """Registered first, so the context/injection hooks behind it see the call is cancelled."""
    from strands.hooks import BeforeModelCallEvent

    import strandly.serve.turn as turn_mod
    from scripted_model import ScriptedModel

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    use_model(monkeypatch, lambda _model: ScriptedModel())
    agent = await turn_mod._build_session_agent(
        Config(values={}), RuntimeContext(session_id="s-order"), model=None
    )
    event = BeforeModelCallEvent(agent=agent, invocation_context=None)
    callbacks = list(agent.hooks.get_callbacks_for(event))
    assert len(callbacks) > 1
    assert callbacks[0] is turn_mod._skip_model_call_when_cancelled


@pytest.mark.asyncio
async def test_sessionless_invokes_still_run_concurrently(monkeypatch):
    """No session → nothing to supersede; sessionless invokes run side by side."""
    pytest.importorskip("bedrock_agentcore")
    calls: list[str] = []
    signals: list = []
    gate = asyncio.Event()
    invoke = _app(monkeypatch, _gated_run_turn(calls, gate, signals)).handlers["main"]

    first = await invoke({"prompt": "a"})
    await _drain_background()
    second = await invoke({"prompt": "b"})

    assert first["status"] == second["status"] == "accepted"
    assert first["taskId"] != second["taskId"]
    assert not signals[0].is_set()
    gate.set()
    await _wait_status(invoke, first["taskId"], "completed")


class _StubAgent:
    def __init__(self, messages):
        self.messages = messages


def test_turn_input_is_the_plain_prompt_when_history_alternates_normally():
    from strandly.serve.turn import _turn_input

    agent = _StubAgent([{"role": "user", "content": [{"text": "hi"}]},
                        {"role": "assistant", "content": [{"text": "hello"}]}])
    assert _turn_input(agent, "next ask") == "next ask"
    assert _turn_input(_StubAgent([]), "first ask") == "first ask"


def test_turn_input_closes_an_interrupted_turn_so_roles_still_alternate():
    """A cancelled tool call leaves history ending on a synthetic tool-result USER message.

    Appending the next ask straight onto it would send Bedrock Converse two consecutive user
    messages (a ValidationException — the SDK normalizes blank text and tool names, never roles).
    """
    from strandly.serve.turn import TURN_INTERRUPTED_MARKER, _turn_input

    agent = _StubAgent(
        [
            {"role": "user", "content": [{"text": "review the PR"}]},
            {"role": "assistant", "content": [{"toolUse": {"name": "shell", "toolUseId": "t1"}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "status": "error"}}]},
        ]
    )

    result = _turn_input(agent, "stop, wrong repo")

    assert [m["role"] for m in result] == ["assistant", "user"]
    assert result[0]["content"][0]["text"] == TURN_INTERRUPTED_MARKER
    assert result[1]["content"][0]["text"] == "stop, wrong repo"


# ---------------------------------------------------------------------------
# The control plane on the cache: reach the live agent for a session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_input_also_closes_a_turn_left_on_an_unanswered_tool_use(monkeypatch, text_model):
    """The other shape an interrupted turn leaves: assistant(toolUse) with no toolResult.

    Reachable without any cancel at all — the session manager persists the assistant ``toolUse``
    message as soon as the model returns it, so an instance recycled *during* tool execution restores
    history in exactly this shape. ``Agent._convert_prompt_to_messages`` then injects a synthetic
    tool-result **user** message before our ask, which puts two consecutive user messages on the
    wire — the very ValidationException ``_turn_input`` exists to prevent. And because the shape is
    in DURABLE history, the session stays wedged: every later turn fails identically.
    """
    from strands import Agent

    from strandly.serve.turn import TURN_INTERRUPTED_MARKER, _turn_input

    model = text_model("ok")
    sent: list[list[dict]] = []
    original = model.stream

    async def recording_stream(*args, **kwargs):
        msgs = kwargs.get("messages") if kwargs.get("messages") is not None else (args[0] if args else [])
        sent.append(list(msgs or []))
        async for ev in original(*args, **kwargs):
            yield ev

    monkeypatch.setattr(model, "stream", recording_stream)
    agent = Agent(
        model=model,
        messages=[
            {"role": "user", "content": [{"text": "review the PR"}]},
            {"role": "assistant", "content": [{"toolUse": {"name": "shell", "toolUseId": "t1", "input": {}}}]},
        ],
    )

    async for _ in agent.stream_async(_turn_input(agent, "stop, wrong repo")):
        pass

    roles = [m["role"] for m in sent[-1]]
    assert [i for i in range(1, len(roles)) if roles[i] == roles[i - 1]] == [], roles
    assert any(
        b.get("text") == TURN_INTERRUPTED_MARKER
        for m in sent[-1]
        for b in m.get("content", [])
        if isinstance(b, dict)
    )


# ---------------------------------------------------------------------------------------------
# Naming the turn boundary: a named resume vs. an unnamed one
# ---------------------------------------------------------------------------------------------


def test_classify_carries_a_resume_mark_off_the_invocation_state():
    """The mark rides the invocation state the SDK merges into the boundary event.

    ``classify_all`` passes it through raw — naming the boundary needs the earlier turns, which only
    ``translate`` has.
    """
    from strandly.core.events import REASON_RESUME, RESUME_MARK_KEY, classify_all

    mark = {"seq": 2, "reason": REASON_RESUME}
    (ev,) = classify_all({"init_event_loop": True, RESUME_MARK_KEY: mark}, {})
    assert ev.kind == "turn_start" and ev.data == {RESUME_MARK_KEY: (2, REASON_RESUME)}

    # Invocation state is a shared namespace: nothing but a well-formed mark may name a boundary.
    # ``True`` is an ``int`` subclass, so it would read as sequence 1; a consumer switches its
    # rendering on ``reason``, so an unknown one is refused.
    junk_marks = [
        True,
        "1",
        None,
        1.5,
        2,
        {"seq": True, "reason": REASON_RESUME},
        {"seq": 1},
        {"reason": REASON_RESUME},
        {"seq": 1, "reason": "start"},  # a reason no writer may claim for itself
        {"seq": 1, "reason": "queued_messages"},
        {"seq": "1", "reason": REASON_RESUME},
    ]
    for junk in junk_marks:
        (ev,) = classify_all({"init_event_loop": True, RESUME_MARK_KEY: junk}, {})
        assert ev.data == {}, f"{junk!r} was read as a resume mark"


@pytest.mark.asyncio
async def test_translate_names_every_turn_boundary():
    """Only a FRESH mark names a turn. The invocation-state dict outlives the turn that set it, so a
    mark written at turn 2 is still there at turn 3 — a turn nobody named."""
    from strandly.core.events import (
        REASON_CONTINUE,
        REASON_RESUME,
        REASON_START,
        RESUME_MARK_KEY,
        translate,
    )

    def retry(seq: int) -> dict[str, object]:
        return {"init_event_loop": True, RESUME_MARK_KEY: {"seq": seq, "reason": REASON_RESUME}}

    async def stream():
        yield {"init_event_loop": True}  # the original ask
        yield {"data": "answer 1"}
        yield retry(1)  # a critic retry
        yield {"data": "answer 2"}
        yield retry(1)  # STALE: nobody named this turn
        yield {"data": "background results"}
        yield retry(2)  # a second, real retry
        yield {"data": "answer 3"}

    reasons = [ev.data["reason"] async for ev in translate(stream()) if ev.kind == "turn_start"]
    assert reasons == [REASON_START, REASON_RESUME, REASON_CONTINUE, REASON_RESUME]

    # The raw mark is an implementation detail of the hand-off; it must not leak to consumers.
    async def one_turn():
        yield retry(7)

    (ev,) = [e async for e in translate(one_turn()) if e.kind == "turn_start"]
    assert ev.data == {"reason": REASON_START}, ev.data


@pytest.mark.asyncio
async def test_translate_never_credits_a_resume_on_a_reused_invocation_state():
    """A caller may pass its own ``invocation_state`` dict and reuse it across calls, so a mark can
    be present from a PREVIOUS run. The first turn seeds the baseline from it rather than treating
    the next boundary as new work."""
    from strandly.core.events import (
        REASON_CONTINUE,
        REASON_RESUME,
        RESUME_MARK_KEY,
        translate,
    )

    stale = {"seq": 4, "reason": REASON_RESUME}

    async def stream():
        yield {"init_event_loop": True, RESUME_MARK_KEY: stale}  # left over from an earlier run
        yield {"init_event_loop": True, RESUME_MARK_KEY: stale}  # nobody re-stamped it

    reasons = [ev.data["reason"] async for ev in translate(stream()) if ev.kind == "turn_start"]
    assert reasons[1] == REASON_CONTINUE


def test_poll_result_publishes_the_final_turns_text_separately():
    """A live client renders one bubble per turn, so it needs the LAST turn's text — ``result`` is
    every turn's merged, and writing that into the open bubble repeats an answer already on screen
    (the reported duplicate)."""
    from strandly.serve.agentcore_app import _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()

    store.start("multi")
    store.finish("multi", "first answersecond answer", last_turn="second answer")
    res = poll_result(cfg, store, "multi", None)
    assert res["result"] == "first answersecond answer"
    assert res["lastTurnResult"] == "second answer"

    # A tools-only final turn really has no text, and "" is the honest answer (not "field missing":
    # the client must not fall back to the merged result there).
    store.start("silent")
    store.finish("silent", "the earlier answer", last_turn="")
    assert poll_result(cfg, store, "silent", None)["lastTurnResult"] == ""


def test_the_final_turns_text_is_published_even_when_it_equals_the_whole_result():
    """Skipping the equal case to save a copy of the answer on the wire cost the answer itself.

    ``last_turn == result`` does NOT mean "single-turn run". It is also every MULTI-turn run whose
    earlier turns emitted no text — a tools-only first turn, which is routine — and a client that
    renders per turn cannot tell "the final turn is the whole result" from "this runtime never tells
    me". It refuses the merged text on a multi-turn run (correctly: that would repeat an answer
    already on screen) and so renders nothing at all. Absence must mean exactly ONE thing: nobody
    could tell us.
    """
    from strandly.serve.agentcore_app import _TaskStore, poll_result

    cfg = Config(values={})
    store = _TaskStore()

    # The shape that lost the answer: two turns, the first tools-only, so both strings coincide.
    store.start("tools_first")
    store.finish("tools_first", "the final answer", last_turn="the final answer")
    assert poll_result(cfg, store, "tools_first", None)["lastTurnResult"] == "the final answer"

    # And the genuinely single-turn run, which is indistinguishable from it on the wire — which is
    # the whole point: the client is not asked to tell them apart.
    store.start("single")
    store.finish("single", "the only answer", last_turn="the only answer")
    assert poll_result(cfg, store, "single", None)["lastTurnResult"] == "the only answer"

    # Absence is reserved for "the per-turn split is not recoverable" — a status resolved from
    # durable Memory alone, where nothing ever called ``finish``.
    store.start("no_sentinel")
    store._tasks["no_sentinel"] = {"status": "completed", "result": "recovered from Memory"}
    assert "lastTurnResult" not in poll_result(cfg, store, "no_sentinel", None)


@pytest.mark.asyncio
async def test_a_multi_turn_run_reports_its_final_turn_as_well_as_the_merged_result(monkeypatch):
    """``_run``'s two accumulators, end to end through the app.

    A run with a resumed turn has TWO turns inside one ``stream_async`` call, and
    ``result`` is both of them, a blank line apart — that is right for the durable copy (the ledger, the
    GitHub report) and wrong for a live panel, which has one bubble open for the last turn. So the
    poll publishes the final turn separately. The case that made this a blocker: a panel that joined
    the run at the live head and polled once *after* it finished streamed nothing at all, so its only
    possible source for the answer was this field — without it the answer was invisible until a
    reload.
    """
    pytest.importorskip("bedrock_agentcore")
    from strandly.core.events import HarnessEvent

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="turn_start", data={"reason": "start"})
            yield HarnessEvent(kind="text", text="the answer to the original ask")
            # A resumed turn mid-run: a second turn on the SAME task id.
            yield HarnessEvent(kind="turn_start", data={"reason": "resume"})
            yield HarnessEvent(kind="text", text="and the answer to your follow-up")
            yield HarnessEvent(kind="done", data={})

        return gen()

    app = _app(monkeypatch, fake_run_turn)
    invoke = app.handlers["main"]
    started = await invoke({"prompt": "do A", "sessionId": "s-two-turns"})
    await _drain_background()

    poll = await invoke({"action": "poll", "taskId": started["taskId"], "sessionId": "s-two-turns"})
    assert poll["status"] == "completed"
    assert poll["result"] == "the answer to the original ask\n\nand the answer to your follow-up"
    assert poll["lastTurnResult"] == "and the answer to your follow-up"


@pytest.mark.asyncio
async def test_text_from_separate_model_calls_is_a_blank_line_apart(monkeypatch):
    """Two assistant messages of one turn (a tool call between them) do not run together."""
    pytest.importorskip("bedrock_agentcore")
    from strandly.core.events import HarnessEvent

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="turn_start", data={"reason": "start"})
            yield HarnessEvent(kind="text", text="Let me ")
            yield HarnessEvent(kind="text", text="triage.")
            yield HarnessEvent(kind="tool_start", tool="shell", tool_use_id="t1", data={})
            yield HarnessEvent(kind="tool_result", tool="shell", tool_use_id="t1", data={"status": "success"})
            yield HarnessEvent(kind="reasoning", text="thinking")
            yield HarnessEvent(kind="text", text="I listed the files.")
            yield HarnessEvent(kind="done", data={})

        return gen()

    app = _app(monkeypatch, fake_run_turn)
    invoke = app.handlers["main"]
    started = await invoke({"prompt": "triage", "sessionId": "s-two-messages"})
    await _drain_background()

    poll = await invoke({"action": "poll", "taskId": started["taskId"], "sessionId": "s-two-messages"})
    assert poll["result"] == "Let me triage.\n\nI listed the files."
    assert poll["lastTurnResult"] == "Let me triage.\n\nI listed the files."


def test_trigger_source_and_portal_mode_from_payload():
    import strandly.serve.agentcore_app as ac

    assert ac._trigger_source({"triggerSource": "portal:curate"}) == "portal:curate"
    assert ac._trigger_source({}) is None
    assert ac._trigger_source({"triggerSource": ""}) is None
    assert ac._trigger_source({"triggerSource": {"not": "a string"}}) is None
    # `mode` is the portal role — EXCEPT "stream", which is the transport selector (see _is_stream).
    assert ac._portal_mode({"mode": "post"}) == "post"
    assert ac._portal_mode({"mode": "curate"}) == "curate"
    assert ac._portal_mode({"mode": "stream"}) is None
    assert ac._portal_mode({}) is None


def test_requested_by_records_the_bound_person_or_the_portal_user():
    import strandly.serve.agentcore_app as ac

    sub = "11111111-1111-4111-8111-111111111111"
    portal = {"triggerSource": "portal:chat", "portalUser": sub}
    assert ac._requested_by({**portal, "personId": "octo", "personGithub": "octocat"}) == "person:octo"
    assert ac._requested_by(portal) == f"portal:{sub}"
    assert ac._requested_by({**portal, "triggerSource": "acp:prompt"}) == f"portal:{sub}"
    assert ac._requested_by({**portal, "triggerSource": "slack:U0BOB"}) is None
    assert ac._requested_by({"triggerSource": "portal:chat", "personId": "octo"}) is None
    assert ac._requested_by({"triggerSource": "portal:chat", "portalUser": "x" * 36}) is None
    direct = {"triggerSource": "portal:chat", "directInvoke": True, "personId": "octo"}
    assert ac._requested_by(direct) == "person:octo"
    assert ac._requested_by({**direct, "directInvoke": "true"}) is None


@pytest.mark.asyncio
async def test_run_threads_trigger_source_and_mode_into_the_ledger(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    starts: list[dict] = []
    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            starts.append(kw)

        def finish(self, *a, **kw):
            finishes.append(kw)

        def fail(self, *a, **kw):
            pass

    monkeypatch.setattr(ac, "RunLedger", FakeLedger)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            for ev in _turn_events("ok"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"](
        {"prompt": "post it", "sessionId": "gh-o-r-pr-42",
         "triggerSource": "portal:curate", "mode": "post"}
    )
    await _drain_background()
    await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})

    (start_kw,) = starts
    (finish_kw,) = finishes
    assert start_kw["trigger_source"] == "portal:curate" and start_kw["mode"] == "post"
    assert finish_kw["trigger_source"] == "portal:curate" and finish_kw["mode"] == "post"
    assert start_kw["requested_by"] is None and finish_kw["requested_by"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sessionId": "s-blank"},
        {"prompt": "", "sessionId": "s-blank"},
        {"prompt": "   \n\t ", "sessionId": "s-blank"},
        {"userInput": "  ", "mode": "stream", "sessionId": "s-blank"},
        {"prompt": "", "queueStrategy": "interrupt", "sessionId": "s-blank"},
        {"prompt": " "},  # sessionless
    ],
)
async def test_a_blank_prompt_is_refused_before_any_work(monkeypatch, payload):
    """A blank invoke is rejected before it starts a run."""
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)

    def no_run_turn(*_a, **_k):  # pragma: no cover — reaching it is the failure
        raise AssertionError("a blank prompt must not start a run")

    monkeypatch.setattr(ac, "run_turn", no_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"](payload)

    assert res.status_code == 400
    body = json.loads(res.body)
    assert body["status"] == "error" and "prompt" in body["error"]
    # Nothing was queued or started: the session is idle and the next real message starts a run.
    if payload.get("sessionId"):
        status = await app.handlers["main"]({"action": "cancel", "sessionId": payload["sessionId"]})
        assert status["status"] == "idle"


@pytest.mark.asyncio
async def test_actions_and_real_prompts_are_not_mistaken_for_blank_ones(monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[object] = []

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        calls.append(user_input)

        async def gen():
            for ev in _turn_events("answer"):
                yield ev

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    invoke = app.handlers["main"]
    # Control-plane actions carry no prompt and are answered as before.
    assert (await invoke({"action": "poll"}))["status"] == "error"  # needs a taskId — not a 400
    assert (await invoke({"action": "cancel", "sessionId": "s-ok"}))["status"] == "idle"
    # A real prompt — `prompt` or `userInput` — still runs.
    res = await invoke({"userInput": "  do it  ", "sessionId": "s-ok"})
    assert res["status"] == "accepted"
    await _drain_background()
    assert calls == ["  do it  "]


def test_blank_prompt_classifier():
    import strandly.serve.agentcore_app as ac

    assert ac._is_blank_prompt({})
    assert ac._is_blank_prompt({"prompt": " \n"})
    assert ac._is_blank_prompt({"prompt": []})
    assert not ac._is_blank_prompt({"prompt": "hi"})
    assert not ac._is_blank_prompt({"userInput": "hi", "prompt": ""})
    assert not ac._is_blank_prompt({"prompt": [{"text": "content blocks"}]})


@pytest.mark.asyncio
async def test_run_end_wakes_the_metrics_flusher_instead_of_awaiting_a_flush(monkeypatch):
    # An awaited run-end flush would queue behind a slow in-flight background flush (its lock).
    # The run only WAKES the flusher (non-blocking, no await).
    pytest.importorskip("bedrock_agentcore")
    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(ac.metrics, "request_flush", lambda: calls.append("request_flush"))

    def blocking_flush() -> int:  # an awaited flush would hang the run end here
        calls.append("flush")
        raise AssertionError("the run end must not flush synchronously")

    monkeypatch.setattr(ac.metrics, "flush", blocking_flush)

    def fake_run_turn(config, user_input, ctx, *, model=None, **_):
        async def gen():
            yield HarnessEvent(kind="text", text="ok")
            yield HarnessEvent(kind="done", data={"usage": {"total": 1}, "stop_reason": "end_turn"})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    app = ac.build_app(Config(values={}))
    res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-metrics-flush"})
    await _drain_background()

    poll = await app.handlers["main"]({"action": "poll", "taskId": res["taskId"]})
    assert poll["status"] == "completed"
    assert calls == ["request_flush"]
    # The session was released (finish() ran): a new prompt starts a fresh run, not a queue entry.
    res2 = await app.handlers["main"]({"prompt": "again", "sessionId": "s-metrics-flush"})
    await _drain_background()
    assert res2["taskId"] != res["taskId"]
    poll2 = await app.handlers["main"]({"action": "poll", "taskId": res2["taskId"]})
    assert poll2["status"] == "completed"


def test_sigterm_ledger_writes_are_bounded_per_row(monkeypatch):
    """P3-C: one hung write does not use up the budget of the rows after it."""
    import strandly.serve.agentcore_app as ac

    monkeypatch.setattr(ac, "_SIGTERM_LEDGER_BUDGET_SECONDS", 0.5)
    hang, written = threading.Event(), []
    monkeypatch.setattr(ac, "_IN_FLIGHT_RUNS", {
        "hung": lambda reason: hang.wait(10),
        "ok": lambda reason: written.append(reason),
    })
    started = time.monotonic()
    try:
        ac._fail_in_flight_runs("why")
    finally:
        hang.set()
    assert written == ["why"]
    assert time.monotonic() - started < 2
    assert ac._IN_FLIGHT_RUNS == {}


@pytest.mark.asyncio
async def test_sigterm_marks_an_in_flight_run_failed_and_still_shuts_down(monkeypatch):
    # A run killed at the runtime's 8 h cap must not stay `running` forever in the ledger.
    pytest.importorskip("bedrock_agentcore")
    import signal

    import strandly.serve.agentcore_app as ac
    from strandly.core.events import HarnessEvent

    monkeypatch.delenv("GITHUB_CONTEXT", raising=False)
    fails: list[tuple] = []
    finishes: list[dict] = []

    class FakeLedger:
        @staticmethod
        def from_config(config):
            return FakeLedger()

        def start(self, *a, **kw):
            pass

        def finish(self, *a, **kw):
            finishes.append(kw)

        def fail(self, *a, **kw):
            fails.append((a, kw))

    monkeypatch.setattr(ac, "RunLedger", FakeLedger)
    release = asyncio.Event()

    def fake_run_turn(config, user_input, ctx, **_):
        async def gen():
            await release.wait()
            yield HarnessEvent(kind="text", text="late")
            yield HarnessEvent(kind="done", data={"usage": {}, "stop_reason": "end_turn"})

        return gen()

    monkeypatch.setattr(ac, "run_turn", fake_run_turn)
    shutdowns: list[int] = []
    original = signal.signal(signal.SIGTERM, lambda signum, frame: shutdowns.append(signum))
    try:
        app = ac.build_app(Config(values={}))
        res = await app.handlers["main"]({"prompt": "task", "sessionId": "s-sigterm"})
        await _drain_background()
        signal.raise_signal(signal.SIGTERM)
        ((args, kw),) = fails
        assert args == (res["taskId"],)
        assert kw["error"] == ac.RUN_TERMINATED_REASON and "SIGTERM" in kw["error"]
        assert kw["session_id"] == "s-sigterm" and kw["prompt"] == "task"
        assert kw["only_if_running"] is True  # never overwrites a run that just finished
        assert shutdowns == [signal.SIGTERM]  # uvicorn's own handler still runs
        signal.raise_signal(signal.SIGTERM)
        assert len(fails) == 1  # once per run
        release.set()
        await _drain_background()
        assert len(finishes) == 1 and ac._IN_FLIGHT_RUNS == {}
    finally:
        release.set()
        signal.signal(signal.SIGTERM, original)
