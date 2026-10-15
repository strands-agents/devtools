"""Bedrock AgentCore Runtime adapter — two CLI-selected modes, no GitHub precondition.

The deployed runtime serves **two behaviors, selected by the invocation payload** — GitHub is never
a precondition, only an *optional* reporting sink a GitHub-triggered job may use:

1. **Streaming / chat** (``mode: "stream"`` or ``stream: true`` in the payload) — runs the turn
   synchronously and **streams normalized events back** (text / reasoning / tool events) as SSE.
   The answer returns inline, so there's nowhere to "report to" and no GitHub context is needed.
   This backs ``strandly chat --agentcore`` and any ``accept: text/event-stream`` client.

2. **Fire-and-forget** (the default) — for **long tasks** (a PR review, an implementation) that can
   run minutes to hours, where holding an HTTP socket open is the wrong model. It starts the work
   in a background task, immediately returns ``{status: "accepted", taskId}``, and the **durable**
   result channel is **AgentCore Memory**: the session manager persists the conversation under the
   payload's ``sessionId`` when ``AGENTCORE_MEMORY_ID`` is configured, and ``strandly poll`` reads
   it back with ``ListEvents`` (see :func:`strandly_harness.memory.session.read_conversation`). A GitHub
   context, if present, just lets the agent *also* report out of band via ``use_github`` — it is no
   longer required.

**Polling / completion detection.** A poll merges two signals: an in-instance status **sentinel**
(``_TaskStore`` — precise, but lost on instance recycle / a poll that lands elsewhere) and the
**durable Memory** session. When the sentinel is reachable it wins; otherwise we fall back to a
**settle heuristic** over Memory (an assistant reply exists after the last user message →
completed). AgentCore routes invokes with the same ``runtimeSessionId`` to the same instance
(session affinity), so a poll using the run's session id usually reaches the sentinel; Memory makes
it correct even when it doesn't.

While a background task runs, the app's ping status is ``HEALTHY_BUSY`` (via ``add_async_task`` /
``complete_async_task``), so AgentCore doesn't recycle the instance mid-run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.core.context import RuntimeContext
from strandly_harness.core.retries import CONTINUATION_PROMPT, backoff_seconds, is_transient_error
from strandly_harness.memory.session import (
    conversation_settled,
    extract_conversation,
    final_assistant_text,
    read_events,
)
from strandly_harness.ops import metrics
from strandly_harness.ops.ledger import RunLedger
from strandly_harness.serve.cache import session_key
from strandly_harness.serve.turn import run_turn

logger = logging.getLogger(__name__)

# Best-effort, in-memory status sentinel: task_id -> {status, result?, error?}. Per instance; not
# durable (Memory is the durable channel — see the module docstring). Bounded so a long-lived
# instance can't grow it without limit (oldest entries drop).
_MAX_TASKS = 256


class _TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def start(self, task_id: str) -> None:
        self._tasks[task_id] = {"status": "running"}
        self._order.append(task_id)
        while len(self._order) > _MAX_TASKS:
            self._tasks.pop(self._order.pop(0), None)

    def finish(self, task_id: str, result: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id] = {"status": "completed", "result": result}

    def fail(self, task_id: str, error: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id] = {"status": "failed", "error": error}

    def get(self, task_id: str) -> dict[str, Any]:
        # "unknown" (not "not found") because best-effort: the instance holding it may have been
        # recycled, or the poll landed on a different instance. The Memory fallback covers this.
        return self._tasks.get(task_id, {"status": "unknown"})


def _prompt_of(payload: dict[str, Any]) -> str:
    return payload.get("userInput") or payload.get("prompt") or ""


def _github_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the GitHub event context from the payload (or env), or ``None`` if there is none."""
    for key in ("githubContext", "github_context"):
        ctx = payload.get(key)
        if isinstance(ctx, dict):
            return ctx
    if "event_name" in payload and "event" in payload:
        return payload
    raw = os.environ.get("GITHUB_CONTEXT")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            logger.warning("GITHUB_CONTEXT is set but not valid JSON; ignoring")
    return None


def _github_target(ctx: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Best-effort (url, repo) the run is acting on, pulled from the GitHub event context.

    Returns the issue/PR html_url (what the run reports back to) and the ``owner/repo`` slug, or
    ``(None, None)`` when the context has neither. Purely for the ledger/dashboard — never raises.
    """
    if not isinstance(ctx, dict):
        return None, None
    event = ctx.get("event") if isinstance(ctx.get("event"), dict) else ctx
    url = None
    for key in ("pull_request", "issue", "discussion"):
        node = event.get(key) if isinstance(event, dict) else None
        if isinstance(node, dict) and node.get("html_url"):
            url = node["html_url"]
            break
    repo = ctx.get("repository")
    if not isinstance(repo, str):
        repo_node = event.get("repository") if isinstance(event, dict) else None
        if isinstance(repo_node, dict):
            repo = repo_node.get("full_name")
    return url, (repo if isinstance(repo, str) else None)


def _xray_id_from_otel(otel_trace_id: int) -> str:
    """Format a 128-bit OTel trace id as an X-Ray trace id: ``1-<8 hex>-<24 hex>``.

    X-Ray (and the GenAI Observability console) key traces by this form; the OTel/X-Ray exporter
    maps the first 32 bits of the OTel id to the X-Ray epoch segment, the rest to the unique segment.
    """
    hexid = format(otel_trace_id, "032x")
    return f"1-{hexid[:8]}-{hexid[8:]}"


def _trace_id() -> str | None:
    """The active trace id for deep-linking, as an X-Ray id.

    Prefers the **live OpenTelemetry span** — the runtime runs under ``opentelemetry-instrument``, so
    a valid span context exists during a turn, and (unlike the X-Ray env var) it's populated inside
    the fire-and-forget background task. Falls back to ``_X_AMZN_TRACE_ID`` (set on synchronous
    invokes) and returns ``None`` only when neither is available.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if getattr(ctx, "is_valid", False) and ctx.trace_id:
            return _xray_id_from_otel(ctx.trace_id)
    except Exception:  # noqa: BLE001 — tracing is best-effort; never break a run over a trace id
        pass

    raw = os.environ.get("_X_AMZN_TRACE_ID") or ""
    for part in raw.split(";"):
        if part.startswith("Root="):
            return part[len("Root=") :]
    return None


def _is_stream(payload: dict[str, Any]) -> bool:
    """True when the caller asked for the streaming/chat mode (vs fire-and-forget)."""
    return payload.get("mode") == "stream" or bool(payload.get("stream"))


def _event_dict(ev: Any) -> dict[str, Any]:
    """Serialize a HarnessEvent for the SSE stream (the SDK json-encodes + frames each yield)."""
    return {
        "kind": ev.kind,
        "text": ev.text,
        "tool": ev.tool,
        "toolUseId": ev.tool_use_id,
        "data": ev.data,
    }


def poll_result(
    config: Config, store: _TaskStore, task_id: str, session_id: str | None
) -> dict[str, Any]:
    """Resolve a fire-and-forget run's status by merging the sentinel with durable Memory.

    Precedence: a reachable in-instance sentinel (``completed``/``failed``/``running``) wins; on
    ``unknown`` (recycled / poll landed elsewhere) we fall back to the Memory settle heuristic. Pure
    apart from the Memory read, which is wrapped so a data-plane hiccup degrades to the sentinel.
    """
    sentinel = store.get(task_id)
    status = sentinel.get("status", "unknown")
    result = sentinel.get("result")
    error = sentinel.get("error")

    events: list[dict[str, Any]] = []
    if session_id:
        try:
            events = read_events(config, session_id)
        except Exception as e:  # noqa: BLE001 — Memory read is best-effort; fall back to sentinel
            logger.warning("Memory read failed during poll (session=%s): %s", session_id, e)
    messages = extract_conversation(events)

    if status == "completed":
        if result is None:
            result = final_assistant_text(messages)
    elif status == "unknown":
        # No sentinel on this instance — rely on the durable Memory session. Use the tool_use-aware
        # settle so an in-progress narration (assistant text + a pending toolUse) isn't mistaken for
        # the final answer; only a terminal text-only assistant message completes the poll.
        if conversation_settled(events):
            status = "completed"
            result = final_assistant_text(messages)
        elif messages:
            status = "running"

    resp: dict[str, Any] = {"taskId": task_id, "status": status}
    if result is not None:
        resp["result"] = result
    if error is not None:
        resp["error"] = error
    return resp


def _emit_run_metrics(outcome: str, *, started: float, usage: dict[str, int] | None) -> None:
    """Emit operational EMF for a finished run: the outcome (Completed|Failures), its duration, and
    (when known) the token throughput. Fail-open via :func:`metrics.emit`; a no-op when metrics are
    disabled. Token counts are throughput telemetry only — never presented as dollars (cost is
    AWS-native Cost Anomaly Detection, not a token-derived metric)."""
    doc: dict[str, Any] = {
        outcome: 1,
        metrics.DURATION_MS: (int((time.monotonic() - started) * 1000), metrics.MILLISECONDS),
    }
    if usage and isinstance(usage.get("total"), int):
        doc[metrics.TOKENS_TOTAL] = usage["total"]
    metrics.emit(doc, surface=metrics.SURFACE_AGENTCORE)


def build_app(config: Config, *, model: Any | None = None) -> Any:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()
    store = _TaskStore()
    # Optional durable mirror of the in-memory store (gated on STRANDLY_RUN_LEDGER_TABLE). None when
    # unconfigured, in which case the runtime behaves exactly as before. Fail-open throughout.
    ledger = RunLedger.from_config(config)
    # Strong references to in-flight background tasks. ``asyncio.create_task`` only keeps a *weak*
    # reference, so without this the GC can collect a running task mid-flight (CPython gotcha /
    # ruff RUF006) — fatal here, where the whole fire-and-forget model is background tasks.
    background: set[asyncio.Task[None]] = set()

    def _ctx(payload: dict[str, Any]) -> RuntimeContext:
        return RuntimeContext(
            session_id=payload.get("sessionId") or payload.get("sessionArn"),
            session_key=payload.get("sessionKey"),
            event=payload,
        )

    async def _run(payload: dict[str, Any], task_id: str, async_id: int) -> None:
        """Run one turn to completion in the background.

        The durable result is the AgentCore Memory session (the session manager persists it under
        the payload's ``sessionId``); we also record the final text in the in-instance sentinel for
        a fast same-instance poll, and (when configured) mirror the run into the durable ledger.
        """
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        session_id = payload.get("sessionId") or payload.get("sessionArn")
        target_url, repo = _github_target(_github_context(payload))
        trace_id = _trace_id()
        if ledger:
            ledger.start(
                task_id,
                session_id=session_id,
                github_target=target_url,
                repo=repo,
                prompt=_prompt_of(payload),
                trace_id=trace_id,
                started_at=started_at,
            )
        metrics.emit({metrics.INVOCATIONS: 1}, surface=metrics.SURFACE_AGENTCORE)
        try:
            # Capture the final assistant text as the poll result (the agent also reports via
            # GitHub — this is the convenience copy), plus token usage from the terminal event.
            #
            # Run-level retry (the model-layer gap): botocore retries cover request-time failures,
            # but a connection dropped MID-EventStream — where minutes-long streaming runs actually
            # die — surfaces here as an exception with the run half-done. Before this loop, that
            # exception permanently failed the run: the ingress backstop had already recorded the
            # dispatch and marked the notification read, so nothing ever retried it and the work
            # was lost until a human re-mentioned. Now: classify transient errors, back off, and
            # re-invoke with a continuation prompt. For a *session* invoke the per-session agent
            # cache preserves the full message history, so the agent RESUMES (no duplicate side
            # effects); a sessionless invoke gets a fresh agent, so we re-send the original prompt.
            # Non-transient errors (real bugs) re-raise immediately into the failure path below.
            final: list[str] = []
            usage: dict[str, int] | None = None
            resumable = session_key(_ctx(payload)) is not None
            user_input = _prompt_of(payload)
            attempt = 0
            while True:
                attempt += 1
                final = []  # partial text from an interrupted attempt is not the answer
                try:
                    async for ev in run_turn(config, user_input, _ctx(payload), model=model):
                        # Capture the trace id once the turn is underway: the model-call span is
                        # live by the first event, so the OTel context is populated (it isn't yet
                        # at `_run` start).
                        if trace_id is None:
                            trace_id = _trace_id()
                        if ev.kind == "text" and ev.text:
                            final.append(ev.text)
                        elif ev.kind == "done":
                            usage = ev.data.get("usage") or usage
                    break
                except Exception as e:
                    from strandly_harness.core.constants import RUN_RETRY_MAX_ATTEMPTS

                    if attempt >= RUN_RETRY_MAX_ATTEMPTS or not is_transient_error(e):
                        raise
                    delay = backoff_seconds(attempt)
                    logger.warning(
                        "transient model/stream error on attempt %d/%d (task_id=%s): %s: %s — "
                        "retrying in %.1fs with a %s prompt",
                        attempt,
                        RUN_RETRY_MAX_ATTEMPTS,
                        task_id,
                        type(e).__name__,
                        e,
                        delay,
                        "continuation" if resumable else "fresh (sessionless)",
                    )
                    await asyncio.sleep(delay)
                    user_input = CONTINUATION_PROMPT if resumable else _prompt_of(payload)
            text = "".join(final)
            store.finish(task_id, text)
            _emit_run_metrics(
                metrics.COMPLETED, started=started, usage=usage
            )
            if ledger:
                ledger.finish(
                    task_id,
                    result=text,
                    usage=usage,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    session_id=session_id,
                    github_target=target_url,
                    repo=repo,
                    # Terminal ledger writes replace the start() row — re-carry the prompt so
                    # finished runs keep it (the dashboard's session descriptions read it).
                    prompt=_prompt_of(payload),
                    trace_id=trace_id,
                    started_at=started_at,
                )
        except Exception as e:  # noqa: BLE001 — record failure for the poll, don't crash the worker
            logger.exception("background run failed (task_id=%s)", task_id)
            store.fail(task_id, f"{type(e).__name__}: {e}")
            _emit_run_metrics(metrics.FAILURES, started=started, usage=None)
            if ledger:
                ledger.fail(
                    task_id,
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    session_id=session_id,
                    github_target=target_url,
                    repo=repo,
                    prompt=_prompt_of(payload),
                    trace_id=trace_id,
                    started_at=started_at,
                )
        finally:
            app.complete_async_task(async_id)

    async def _stream(payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn's normalized events back to the caller (SSE)."""
        async for ev in run_turn(config, _prompt_of(payload), _ctx(payload), model=model):
            yield _event_dict(ev)

    @app.entrypoint
    async def invoke(payload: dict[str, Any]) -> Any:
        # Poll action: merge the in-instance sentinel with the durable Memory session.
        if payload.get("action") == "poll":
            task_id = payload.get("taskId")
            if not task_id:
                return {"status": "error", "error": "poll requires a 'taskId'"}
            return poll_result(config, store, str(task_id), payload.get("sessionId"))

        # Streaming / chat mode: return an async generator → the SDK streams it as text/event-stream.
        if _is_stream(payload):
            return _stream(payload)

        # Fire-and-forget: mark the app HEALTHY_BUSY for the life of the task, then return an ack.
        # The result is read back from AgentCore Memory by `strandly poll` (sentinel + settle).
        async_id = app.add_async_task("strandly_run")
        # The SDK's async task id is a signed int; a leading '-' would be parsed as a flag by
        # `strandly poll`. Expose an unsigned hex token to the caller, keyed back to the SDK id.
        task_id = format(async_id & 0xFFFFFFFFFFFFFFFF, "x")
        store.start(task_id)
        task = asyncio.create_task(_run(payload, task_id, async_id))
        background.add(task)
        task.add_done_callback(background.discard)
        return {"status": "accepted", "taskId": task_id}

    return app


def serve_agentcore(config: Config) -> None:
    build_app(config).run()
