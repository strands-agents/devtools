"""The Bedrock AgentCore Runtime app: streamed chat and fire-and-forget runs.

Every run is a background task. ``mode: "stream"`` streams its normalized events as SSE; the stream
is an attachment, so a dropped connection never stops the run. The default is fire-and-forget:
return ``{status: "accepted", taskId}`` at once, persist the conversation in the session store, and
answer polls from an in-instance sentinel or, after a recycle, from the latest snapshot. Each task
keeps a bounded, sequence-numbered event buffer, so a poll with ``since`` follows progress.

One run per session: a new message supersedes the running turn through its per-run cancel signal
(``queueStrategy: "reject"`` answers ``rejected`` instead, and a re-dispatch of the running trigger
is a duplicate no-op); ``{"action": "cancel"}`` stops it. The ping status is ``HEALTHY_BUSY`` while a
task runs, so AgentCore does not recycle the instance mid-run. GitHub is an optional reporting sink,
never a precondition. See docs/architecture.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from strandly.core.agent import check_model
from strandly.core.config import Config
from strandly.core.constants import (
    CHAT_EFFORT_KEY,
    CHAT_EFFORT_MAX_CHARS,
    CHAT_MODEL_KEY,
    CHAT_MODEL_MAX_CHARS,
)
from strandly.core.context import (
    PERSON_TRIGGER_PREFIXES,
    PORTAL_USER_KEY,
    TRIGGER_SOURCE_KEY,
    RuntimeContext,
    person_of,
)
from strandly.core.events import TurnStreamError
from strandly.memory.session import (
    conversation_settled,
    final_assistant_text,
    read_conversation,
)
from strandly.memory.session_store import SESSION_STORAGE_FAILURE
from strandly.ops import metrics
from strandly.ops.github import REACTION_COMPLETED, REACTION_FAILED, add_reaction
from strandly.ops.ledger import STATUS_CANCELLED, STATUS_COMPLETED, RunLedger
from strandly.ops.work_product import (
    STATUS_DRAFT,
    Finding,
    WorkProduct,
    WorkProductStore,
)
from strandly.otel_guard import cap_cloudwatch_log_batches
from strandly.serve import run_receipts
from strandly.serve.cache import session_key
from strandly.serve.instance_init import InstanceInit, ReseedRandomnessMiddleware
from strandly.serve.turn import run_turn
from strandly.serve.work_product_blocks import _MAX_YAML_CHARS as _MAX_YAML_CHARS
from strandly.serve.work_product_blocks import (
    _coerce_int,
    _split_work_product,
    _strip_work_product,
)

logger = logging.getLogger(__name__)

#: Ledger rows of runs in flight in this process: task id -> write that row as failed.
_IN_FLIGHT_RUNS: dict[str, Callable[[str], object]] = {}
#: Of the platform's termination grace (up to 15 s), what failing those rows may take.
_SIGTERM_LEDGER_BUDGET_SECONDS = 5.0
RUN_TERMINATED_REASON = (
    "RuntimeTerminated: the agent runtime was stopped (SIGTERM, e.g. its 8-hour session limit) "
    "before this run finished"
)


def _fail_in_flight_runs(reason: str = RUN_TERMINATED_REASON) -> None:
    """Mark every in-flight run failed, within :data:`_SIGTERM_LEDGER_BUDGET_SECONDS` in total.

    One thread per row, all sharing the deadline, so one hung write cannot starve the others. Each
    write is conditional on the row still being ``running`` (see ``fail_row``).
    """
    writes = [_IN_FLIGHT_RUNS.pop(task_id) for task_id in list(_IN_FLIGHT_RUNS)]
    if not writes:
        return

    def write_one(write: Callable[[str], object]) -> None:
        try:
            write(reason)
        except Exception:  # noqa: BLE001 — best effort, one row must not stop the others
            logger.exception("could not mark an in-flight run failed")

    # Not on the signal handler's own thread: the loop it interrupted may hold locks boto3 needs.
    workers = [
        threading.Thread(target=write_one, args=(write,), name="strandly-sigterm-ledger", daemon=True)
        for write in writes
    ]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + _SIGTERM_LEDGER_BUDGET_SECONDS
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


def _install_sigterm_handler() -> None:
    """Fail in-flight runs on SIGTERM, then hand the signal to whoever had it (uvicorn's shutdown)."""
    if threading.current_thread() is not threading.main_thread():
        return
    previous = signal.getsignal(signal.SIGTERM)
    if getattr(previous, "_strandly_fails_runs", False):
        return

    def on_sigterm(signum: int, frame: Any) -> None:
        _fail_in_flight_runs()
        if callable(previous):
            previous(signum, frame)
        elif previous != signal.SIG_IGN:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.raise_signal(signal.SIGTERM)

    on_sigterm._strandly_fails_runs = True  # type: ignore[attr-defined]
    signal.signal(signal.SIGTERM, on_sigterm)

# Best-effort, in-memory status sentinel: task_id -> {status, result?, error?}, plus a bounded,
# sequence-numbered live-event buffer per task (incremental polls) and live subscriber queues (the
# SSE attach). Per instance; not durable (the session store is the durable channel — see the module
# docstring). Bounded so a long-lived instance can't grow it without limit (oldest entries drop).
_MAX_TASKS = 256
# Per-task live-event buffer bound. Text deltas dominate (a long answer streams as thousands of
# small chunks); with tool payloads clipped to _EVENT_CLIP the buffer stays modest per task, and a
# poller on a ~1–2s cadence never falls behind. Oldest events drop first — the buffer is a live
# view, not the archive (the session store/CloudWatch are).
_MAX_EVENTS_PER_TASK = 3000
# The "attach at the live head" cursor: a poll with a negative ``since`` gets no backlog, only the
# next sequence number (see :meth:`_TaskStore.events_since`).
TAIL_CURSOR = -1
# Poll payload flag and response key for a poll bound to the tasks this instance issued on that
# session (see :func:`poll_result`); mirrored in dashboard/api/handler.py.
POLL_ISSUED_ONLY_KEY = "issuedTaskOnly"
POLL_ISSUED_KEY = "issued"
# Clip bulky event payloads (a tool call's input, a tool result's text) on the live channel: it
# exists for *watching* a run; the full data is durable elsewhere.
_EVENT_CLIP = 2000


class _TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._receipt_keys: dict[str, str] = {}
        self._receipt_settled: set[str] = set()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._next_seq: dict[str, int] = {}
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}
        self._sessions: dict[str, str | None] = {}

    def start(self, task_id: str, receipt_key: str | None = None, session: str | None = None) -> None:
        if receipt_key is not None:
            self._receipt_keys[task_id] = receipt_key
        self._tasks[task_id] = {"status": "running"}
        self._sessions[task_id] = session
        self._events[task_id] = []
        self._next_seq[task_id] = 0
        self._subs.setdefault(task_id, [])
        self._order.append(task_id)
        while len(self._order) > _MAX_TASKS:
            dropped = self._order.pop(0)
            self._tasks.pop(dropped, None)
            self._sessions.pop(dropped, None)
            self._receipt_keys.pop(dropped, None)
            self._receipt_settled.discard(dropped)
            self._events.pop(dropped, None)
            self._next_seq.pop(dropped, None)
            for q in self._subs.pop(dropped, []):
                q.put_nowait(None)  # end any live attachment to the evicted task

    def settle_receipt(self, task_id: str) -> None:
        self._receipt_settled.add(task_id)

    def receipt(self, key: str, since: int | None = None) -> dict[str, Any]:
        for task_id, receipt_key in self._receipt_keys.items():
            if receipt_key == key:
                result = {**self.get(task_id), "taskId": task_id}
                if task_id not in self._receipt_settled:
                    result["status"] = "running"
                if since is not None:
                    events, next_seq = self.events_since(task_id, since)
                    # Public prose only. Tool inputs/results, reasoning and errors are private.
                    result["events"] = [
                        {"kind": "text", "text": e.get("text", ""), "seq": e["seq"]}
                        for e in events if e.get("kind") == "text"
                    ]
                    result["nextSeq"] = next_seq
                    # Floor is from ALL events, before the public-text filter. Private events
                    # legitimately consume sequence numbers; adjacency cannot prove text loss.
                    buf = self._events.get(task_id, [])
                    result["firstSeq"] = buf[0]["seq"] if buf else next_seq
                return result
        return {"status": "unknown"}

    def emit(self, task_id: str, event: dict[str, Any]) -> None:
        """Record a live event (assigning its sequence number) and fan it out to subscribers.

        No-op for unknown/evicted tasks. ``put_nowait`` on unbounded queues never blocks, so a
        slow/stalled SSE consumer can't back-pressure the run.
        """
        if task_id not in self._events:
            return
        ev = dict(event)
        ev["seq"] = self._next_seq[task_id]
        self._next_seq[task_id] += 1
        buf = self._events[task_id]
        buf.append(ev)
        if len(buf) > _MAX_EVENTS_PER_TASK:
            del buf[: len(buf) - _MAX_EVENTS_PER_TASK]
        for q in list(self._subs.get(task_id, [])):
            q.put_nowait(ev)

    def events_since(self, task_id: str, since: int) -> tuple[list[dict[str, Any]], int]:
        """Buffered events with ``seq >= since``, plus the next cursor.

        A **negative** ``since`` (:data:`TAIL_CURSOR`) means *attach at the live head*: no backlog,
        just the cursor to continue from. That is what a client needs when it attaches to a run that
        has been going for a while — a dashboard panel sending a message into a session whose run
        started before the page was loaded. Replaying the backlog there redraws turns the client
        already has from the durable transcript (the reported symptom: an answer appearing twice),
        and a full buffer (:data:`_MAX_EVENTS_PER_TASK` × :data:`_EVENT_CLIP`) is megabytes that a
        Lambda-mediated poll cannot carry anyway.

        Events older than the buffer bound are gone — a poller that falls very far behind skips
        them silently (the durable transcript lives in the session store, not here).
        """
        next_seq = self._next_seq.get(task_id, 0)
        if since < 0:
            return [], next_seq
        buf = self._events.get(task_id, [])
        return [ev for ev in buf if ev["seq"] >= since], next_seq

    def subscribe(self, task_id: str) -> asyncio.Queue[dict[str, Any] | None]:
        """A live queue of this task's events from now on; ``None`` marks the end of the run."""
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subs.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        subs = self._subs.get(task_id)
        if subs and q in subs:
            subs.remove(q)

    def _close_subs(self, task_id: str) -> None:
        for q in self._subs.get(task_id, []):
            q.put_nowait(None)

    def finish(self, task_id: str, result: str, last_turn: str | None = None) -> None:
        """Mark the run completed. ``result`` is every turn's text; ``last_turn`` is the final turn's.

        Both, because they answer different questions. ``result`` is the run's durable convenience
        copy (the ledger row, the GitHub report). ``last_turn`` is what a *live watcher* needs: a
        client that joined mid-run — or whose cursor missed the tail of the stream — has one bubble
        open for the final turn, and writing the merged text into it repeats an answer the transcript
        above is already showing. There is no way to derive one from the other client-side, which is
        why the runtime says it (the same reason the tail cursor is a protocol sentinel).
        """
        if task_id in self._tasks:
            done: dict[str, Any] = {"status": "completed", "result": result}
            if last_turn is not None:
                done["lastTurn"] = last_turn
            self._tasks[task_id] = done
        self._close_subs(task_id)

    def cancel(self, task_id: str, result: str, last_turn: str | None = None) -> None:
        """Mark the run cancelled (superseded by a newer message, or stopped).

        A terminal ``cancelled`` live event goes out first, so an attached stream can tell a
        cancelled run's partial text from an answer.
        """
        self.emit(
            task_id,
            {"kind": "cancelled", "text": None, "tool": None, "toolUseId": None, "data": {}},
        )
        if task_id in self._tasks:
            done: dict[str, Any] = {"status": "cancelled", "result": result}
            if last_turn is not None:
                done["lastTurn"] = last_turn
            self._tasks[task_id] = done
        self._close_subs(task_id)

    def fail(self, task_id: str, error: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id] = {"status": "failed", "error": error}
        self._close_subs(task_id)

    def issued_for(self, task_id: str, session: str | None) -> bool:
        """Whether this instance started ``task_id`` on ``session`` (and still remembers it)."""
        return bool(session) and task_id in self._tasks and self._sessions.get(task_id) == session

    def get(self, task_id: str) -> dict[str, Any]:
        # "unknown" (not "not found") because best-effort: the instance holding it may have been
        # recycled, or the poll landed on a different instance. The session-store fallback covers this.
        return self._tasks.get(task_id, {"status": "unknown"})


def _prompt_of(payload: dict[str, Any]) -> str:
    return payload.get("userInput") or payload.get("prompt") or ""


#: HTTP status of a refused invoke whose prompt is missing or blank (a client error, not a run).
BLANK_PROMPT_STATUS = 400
BLANK_PROMPT_ERROR = (
    "the invoke has no prompt: send a non-blank 'prompt' (or 'userInput'), or an 'action' "
    "('poll', 'cancel')"
)


def _is_blank_prompt(payload: dict[str, Any]) -> bool:
    """True when a run-starting invoke carries nothing for the model to answer.

    Rejected up front rather than failing at the model call after a ledger row and an agent
    build. Every ingress sends its text as ``prompt``/``userInput``; a non-string prompt (content
    blocks) is left to the agent unless it is empty.
    """
    prompt = _prompt_of(payload)
    if isinstance(prompt, str):
        return not prompt.strip()
    return not prompt


def _blank_prompt_response() -> Any:
    """The refusal for :func:`_is_blank_prompt`: a 400 with the usual ``{status, error}`` body."""
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"status": "error", "error": BLANK_PROMPT_ERROR}, status_code=BLANK_PROMPT_STATUS
    )


def _model_choice_of(payload: dict[str, Any]) -> tuple[str | None, str | None] | None:
    """The payload's ``(model, effort)``, each ``None`` when omitted or empty; ``None`` for neither.

    Checks type and size only; the harness model resolution decides what a choice means. The
    dashboard API applies the same rules (``_chat_model`` in dashboard/api/handler.py).

    Raises:
        ValueError: If ``model`` is not a string of at most ``CHAT_MODEL_MAX_CHARS`` characters, or
            ``effort`` not one of at most ``CHAT_EFFORT_MAX_CHARS``, or either has a control character.
    """
    choice: list[str | None] = []
    for key, limit in ((CHAT_MODEL_KEY, CHAT_MODEL_MAX_CHARS), (CHAT_EFFORT_KEY, CHAT_EFFORT_MAX_CHARS)):
        value = payload.get(key)
        if value is None or value == "":
            choice.append(None)
        elif not isinstance(value, str) or len(value) > limit or not value.isprintable():
            raise ValueError(f"'{key}' must be a string of at most {limit} printable characters")
        else:
            choice.append(value)
    model, effort = choice
    return None if model is None and effort is None else (model, effort)


def _invalid_model_response(error: str) -> dict[str, Any]:
    """The refusal of a ``model`` / ``effort``: ``{status: "error", error}``.

    A 200 body, like the runtime's other request errors: AgentCore replaces a 4xx response's body
    with a generic message, so the dashboard API turns this body into its 400.
    """
    return {"status": "error", "error": error}


@dataclass
class _Run:
    """A session's run in flight: what a newer message cancels, and how a re-dispatch is known."""

    task_id: str
    cancel: threading.Event
    trigger_id: str | None
    trigger_hash: str | None
    prompt_hash: str
    result_id: str | None = None


def _prompt_hash(prompt: Any) -> str:
    raw = prompt if isinstance(prompt, str) else json.dumps(prompt, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _str_field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _is_redispatch(payload: dict[str, Any], run: _Run) -> bool:
    """Same trigger with unchanged content as the running ``run``.

    Ingresses send ``triggerHash`` (a fingerprint of the triggering text) because their rendered
    prompts carry a timestamp; without one the whole prompt is compared.
    """
    trigger_id = _str_field(payload, "triggerId")
    if trigger_id is None or trigger_id != run.trigger_id:
        return False
    trigger_hash = _str_field(payload, "triggerHash")
    if trigger_hash is not None and run.trigger_hash is not None:
        return trigger_hash == run.trigger_hash
    return _prompt_hash(_prompt_of(payload)) == run.prompt_hash


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
        logger.debug("OTel trace id unavailable; falling back to the X-Ray env var", exc_info=True)

    raw = os.environ.get("_X_AMZN_TRACE_ID") or ""
    for part in raw.split(";"):
        if part.startswith("Root="):
            return part[len("Root=") :]
    return None


def _is_stream(payload: dict[str, Any]) -> bool:
    """True when the caller asked for the streaming/chat mode (vs fire-and-forget)."""
    return payload.get("mode") == "stream" or bool(payload.get("stream"))


def _trigger_source(payload: dict[str, Any]) -> str | None:
    """HOW this run started, for the ledger's ``trigger_source``.

    The ingresses already stamp the payload with ``triggerSource``:
    ``mention:@user`` / ``slack:<id>`` / ``scheduled:<job>`` and, for the portal, ``portal:curate``
    / ``portal:chat``. Passed through verbatim; only a non-empty string is written.
    """
    value = payload.get("triggerSource")
    return value if isinstance(value, str) and value else None


def _requested_by(payload: dict[str, Any]) -> str | None:
    """Who asked for a portal, CLI or ACP run: its bound ``person:<id>``, else ``portal:<sub>``."""
    person = person_of(payload)
    if person is not None:
        return f"person:{person.person_id}"
    source, user = payload.get(TRIGGER_SOURCE_KEY), payload.get(PORTAL_USER_KEY)
    if (isinstance(source, str) and source.startswith(PERSON_TRIGGER_PREFIXES)
            and isinstance(user, str) and re.fullmatch(r"[0-9a-f-]{36}", user)):
        return f"portal:{user}"
    return None


def _portal_mode(payload: dict[str, Any]) -> str | None:
    """The portal role of this turn for the ledger's ``mode`` (``draft`` / ``curate`` / ``post``).

    Reads the same ``mode`` payload key, but ``mode: "stream"`` is the *transport* selector (see
    :func:`_is_stream`), never a portal role — so it is excluded. A portal ``post``/``curate`` turn
    rides the ordinary fire-and-forget path (the frontend reuses the ``/api/chat`` launch), so this
    does not collide with streaming.
    """
    value = payload.get("mode")
    return value if isinstance(value, str) and value and value != "stream" else None


def _extract_work_product(
    text: str, *, repo: str | None = None, target_url: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Pull the run's work-product block out of its final message.

    Returns ``(text with the counted blocks stripped, parsed dict | None)``. The block is
    metadata — it must never render as prose — so it is stripped whether or not it parses.
    ``repo``/``target_url`` are the run's GitHub target, when it has one. Blocks that were present
    but not used are logged at WARNING; when no block yields an artifact, ``WorkProductParseFailed``
    is emitted once and ``None`` returned: fail-open, this never raises, so the run is never
    broken by its own metadata.
    """
    stripped, data, how, reasons = _split_work_product(text, repo=repo, target_url=target_url)
    for reason in reasons:
        logger.warning("work-product block dropped: %s", reason)
    if data is not None and how != "json":
        logger.info("work-product block was not strict JSON; recovered via %s", how)
    if data is None and reasons:
        logger.warning("work-product block(s) present but none usable; persisting no artifact")
        metrics.emit({metrics.WORK_PRODUCT_PARSE_FAILED: 1}, surface=metrics.SURFACE_AGENTCORE)
    return stripped, data


def _break_text(parts: list[str]) -> None:
    """End the text collected so far with a blank line, before the next assistant message's text."""
    if not parts:
        return
    tail = parts[-1]
    newlines = len(tail) - len(tail.rstrip("\n"))
    if newlines < 2:
        parts.append("\n" * (2 - newlines))


def _text(value: Any) -> str:
    """A block's text field as a string; a missing or null value is empty, never ``"None"``."""
    return "" if value is None else str(value)


def _texts(value: Any) -> list[str]:
    """A block's list-of-strings field. ``null`` → ``[]``; a lone scalar is one item, not its chars."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    return [str(x) for x in items if x is not None]


def _work_product_from_block(
    data: dict[str, Any], *, session_id: str, run_task_id: str
) -> WorkProduct | None:
    """Build a draft :class:`WorkProduct` from a parsed block, or ``None`` if it is unusable.

    The skill supplies the content; the runtime fills ``session_id`` / ``run_task_id`` /
    ``status=draft`` and the timestamps (dataclass defaults, refreshed on ``put``). ``kind`` and
    ``verdict`` are required: a block missing either is logged and skipped rather than
    persisted as a half-record.
    """
    kind = data.get("kind")
    verdict = data.get("verdict")
    if not kind or not verdict:
        logger.warning(
            "work-product block missing required field(s) (kind=%r verdict=%r); skipping",
            kind, verdict,
        )
        return None
    raw_findings = data.get("findings")
    findings = [
        Finding.from_item(f)
        for f in (raw_findings if isinstance(raw_findings, list) else [])
        if isinstance(f, dict)
    ]
    ledger = data.get("evidence_ledger")
    return WorkProduct(
        session_id=session_id,
        kind=str(kind),
        status=STATUS_DRAFT,
        verdict=str(verdict),
        title=_text(data.get("title")),
        target_url=_text(data.get("target_url")),
        repo=_text(data.get("repo")),
        number=_coerce_int(data.get("number")),
        tldr=_text(data.get("tldr")),
        findings=findings,
        evidence_ledger=ledger if isinstance(ledger, dict) else {},
        recommended_labels=_texts(data.get("recommended_labels")),
        draft_comment=_text(data.get("draft_comment")),
        priority=_text(data.get("priority")),
        repro_status=_text(data.get("repro_status")),
        suspected_area=_text(data.get("suspected_area")),
        duplicates=_texts(data.get("duplicates")),
        run_task_id=run_task_id,
    )


def _persist_work_product(
    work_products: WorkProductStore,
    data: dict[str, Any],
    *,
    session_id: Any,
    task_id: str,
) -> None:
    """Build and store the run's work product. Never raises (fail-open).

    Runs AFTER ``store.finish()``, so an exception here would land in ``_run``'s failure handler and
    flip an already-completed run to failed (no COMPLETED metric, no ledger finish, no ✅) — which is
    exactly what a ``"confidence": "high"`` finding did in prod on 2026-09-25. The work product is
    metadata; losing it is a warning, never a failed run.
    """
    if not session_id:
        logger.warning(
            "work-product block present but the run has no session id (task_id=%s); "
            "skipping — the by_session join key would be empty",
            task_id,
        )
        return
    try:
        wp = _work_product_from_block(data, session_id=str(session_id), run_task_id=task_id)
        if wp is None:
            metrics.emit({metrics.WORK_PRODUCT_PARSE_FAILED: 1}, surface=metrics.SURFACE_AGENTCORE)
            return
        logger.info(
            "work-product extracted (task_id=%s session=%s kind=%s verdict=%s findings=%d)",
            task_id, session_id, wp.kind, wp.verdict, len(wp.findings),
        )
        work_products.put(wp)  # fail-open inside
    except Exception:  # noqa: BLE001 — fail-open; logged with the traceback below
        logger.warning(
            "work-product persistence failed (task_id=%s session=%s); the run still completes",
            task_id, session_id, exc_info=True,
        )
        metrics.emit({metrics.WORK_PRODUCT_PARSE_FAILED: 1}, surface=metrics.SURFACE_AGENTCORE)


def _clip(value: Any, limit: int = _EVENT_CLIP) -> Any:
    """Clip a string to ``limit`` chars (non-strings pass through untouched)."""
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _event_dict(ev: Any) -> dict[str, Any]:
    """Serialize a HarnessEvent for the live channel (the SSE stream and the poll event buffer).

    Bulky payloads are clipped to :data:`_EVENT_CLIP`: a tool call's ``input`` (stringified when it
    isn't already a string — the live channel is display-oriented) and a tool result's text blocks.
    The full data is durable elsewhere (the session store, CloudWatch); clipping here keeps the
    in-memory buffer and a catch-up poll response bounded.
    """
    data = dict(ev.data)
    if "input" in data and data["input"] is not None:
        inp = data["input"]
        if not isinstance(inp, str):
            try:
                inp = json.dumps(inp)
            except (TypeError, ValueError):
                inp = str(inp)
        data["input"] = _clip(inp)
    content = data.get("content")
    if isinstance(content, list):
        data["content"] = [
            {**b, "text": _clip(b["text"])}
            if isinstance(b, dict) and isinstance(b.get("text"), str)
            else b
            for b in content
        ]
    return {
        "kind": ev.kind,
        "text": ev.text,
        "tool": ev.tool,
        "toolUseId": ev.tool_use_id,
        "data": data,
    }


def poll_result(
    config: Config,
    store: _TaskStore,
    task_id: str,
    session_id: str | None,
    since: int | None = None,
    *,
    issued_only: bool = False,
) -> dict[str, Any]:
    """Resolve a fire-and-forget run's status by merging the sentinel with the durable session.

    Precedence: a reachable in-instance sentinel (``completed``/``cancelled``/``failed``/``running``)
    wins; on ``unknown`` (recycled / poll landed elsewhere) we fall back to the settle heuristic over
    the session store's latest snapshot. Pure apart from that read, which is wrapped so a storage
    hiccup degrades to the sentinel.

    ``issued_only`` answers only for a task this instance started on ``session_id``: any other
    task id gets ``{POLL_ISSUED_KEY: False}`` without reading the session, so a made-up task id
    cannot fetch the session's last answer through the snapshot fallback.

    ``since`` (a live-event cursor) additionally returns the run's buffered events at/after that
    sequence number plus ``nextSeq`` — the incremental "streaming over polling" channel the
    dashboard chat uses (see the module docstring). ``None`` keeps the classic status-only shape
    (and payload size) for existing pollers; :data:`TAIL_CURSOR` asks for the head without the
    backlog. AgentCore's session affinity routes a poll with the
    run's session id to the instance holding the buffer; on a miss the events are simply empty and
    the durable status signals still apply.
    """
    if issued_only and not store.issued_for(task_id, session_id):
        return {"taskId": task_id, "status": "unknown", POLL_ISSUED_KEY: False}
    sentinel = store.get(task_id)
    status = sentinel.get("status", "unknown")
    result = sentinel.get("result")
    error = sentinel.get("error")

    messages: list[tuple[str, str, bool]] = []
    if session_id:
        try:
            messages = read_conversation(config, session_id)
        except Exception as e:  # noqa: BLE001 — the poll degrades to the sentinel, but loudly
            logger.error(
                "%s op=poll-read session=%s: %s", SESSION_STORAGE_FAILURE, session_id, e
            )

    if status == "completed":
        if result is None:
            result = final_assistant_text(messages)
    elif status == "unknown":
        # No sentinel on this instance — rely on the durable session. Use the tool_use-aware settle
        # so an in-progress narration (assistant text + a pending toolUse) isn't mistaken for the
        # final answer; only a terminal text-only assistant message completes the poll.
        if conversation_settled(messages):
            status = "completed"
            result = final_assistant_text(messages)
        elif messages:
            status = "running"

    resp: dict[str, Any] = {"taskId": task_id, "status": status}
    if result is not None:
        resp["result"] = result
    # The FINAL turn's text, whenever the sentinel has it. A live client renders per turn, so this is
    # the only text it can safely write into the bubble it has open — see :meth:`_TaskStore.finish`.
    #
    # Published even when it EQUALS ``result``, which costs one extra copy of the answer on the wire
    # and buys the client an unambiguous signal. Omitting the equal case looks free (a single-turn
    # run is the common case) but the two strings also coincide on a MULTI-turn run whose earlier
    # turns emitted no text — a tools-only first turn, which is routine — and then a client that
    # renders per turn cannot tell "the final turn is the whole result" from "this runtime never
    # tells me", so it falls back to a rule that refuses ``result`` on a multi-turn run and renders
    # NOTHING. Absence now means exactly one thing: nobody could tell us (a runtime older than this
    # field, or a status resolved from the durable session alone, where the per-turn split is not
    # recoverable). A client that gets no ``lastTurnResult`` falls back to its own streamed text.
    last_turn = sentinel.get("lastTurn")
    if isinstance(last_turn, str):
        resp["lastTurnResult"] = last_turn
    if error is not None:
        resp["error"] = error
    if since is not None:
        live, next_seq = store.events_since(task_id, since)
        resp["events"] = live
        resp["nextSeq"] = next_seq
    return resp


def _emit_run_metrics(
    outcome: str, *, started: float, usage: dict[str, int] | None, tool_errors: int = 0
) -> None:
    """Emit operational EMF for a finished run: the outcome (Completed|Failures|Cancelled), its
    duration, (when known) the token throughput, and the run's tool-error count. Fail-open via
    :func:`metrics.emit`; a no-op when metrics are disabled. Token counts are throughput telemetry
    only — never presented as dollars (cost is AWS-native Cost Anomaly Detection, not a
    token-derived metric). ``ToolErrors`` is always emitted for a finished run (zero included) so
    the series is continuous and a spike — e.g. every ``use_github`` call failing on an expired
    token while the turn still "completes" — is alarmable instead of invisible."""
    doc: dict[str, Any] = {
        outcome: 1,
        metrics.DURATION_MS: (int((time.monotonic() - started) * 1000), metrics.MILLISECONDS),
        metrics.TOOL_ERRORS: tool_errors,
    }
    if usage and isinstance(usage.get("total"), int):
        doc[metrics.TOKENS_TOTAL] = usage["total"]
    metrics.emit(doc, surface=metrics.SURFACE_AGENTCORE)


def build_app(source: Config | Callable[[], Config], *, model: Any | None = None) -> Any:
    """Build the AgentCore app.

    ``source`` is either a loaded :class:`Config` (tests, local serving — everything is wired
    immediately, exactly as before) or a **loader** such as ``Config.load``. The deployed entrypoint
    (``main.py``) passes the loader: AgentCore Runtime V2 snapshots the process once startup finishes
    and clones that snapshot into every session, so anything resolved at import — the config secret,
    AWS clients and their credentials, the ``random`` seed — would be shared by every instance and
    frozen at snapshot time. With a loader that per-instance state is resolved on the first request
    instead (:func:`strandly.serve.instance_init.InstanceInit`), after the restore.
    """
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    # Per-instance state, bound by ``_wire`` — immediately for a Config, on the first request for a
    # loader (see ``InstanceInit``). The closures below read these names at call time.
    config: Config
    ledger: RunLedger | None = None
    work_products: WorkProductStore | None = None

    def _wire(loaded: Config) -> None:
        nonlocal config, ledger, work_products
        # Bridge the config secret's metric namespace into the process env BEFORE anything can
        # emit. ``metrics.emit`` gates on ``os.environ`` (ops/ is a strands-free zone with no
        # Config), and the runtime — unlike every Lambda, whose env the CDK writes directly —
        # receives its config only as the secret ``Config``. Without this, a namespace in the secret
        # was loaded and then ignored, and every run's Completed/Failures/DurationMs/ToolErrors emit
        # was a silent no-op (so the runtime success-rate alarms had no data). An explicit env var
        # still wins — see ``metrics.configure``. Backend stack puts the key in the secret.
        metrics.configure(loaded.metrics_namespace)
        # Optional durable mirror of the in-memory store (gated on STRANDLY_RUN_LEDGER_TABLE). None
        # when unconfigured, in which case the runtime behaves exactly as before. Fail-open.
        ledger = RunLedger.from_config(loaded)
        # Optional store for the structured review/triage artifacts a skill emits, gated on
        # STRANDLY_WORK_PRODUCT_TABLE. None when unconfigured — the block is still
        # stripped from the prose, just not persisted. Fail-open on writes, like the ledger.
        work_products = WorkProductStore.from_config(loaded)
        config = loaded

    instance = InstanceInit(source, _wire)

    # Keep OTLP log exports under CloudWatch's 1 MiB request limit (no-op without ADOT).
    cap_cloudwatch_log_batches()
    app = BedrockAgentCoreApp()
    store = _TaskStore()
    # Strong references to in-flight background tasks. ``asyncio.create_task`` only keeps a *weak*
    # reference, so without this the GC can collect a running task mid-flight (CPython gotcha /
    # ruff RUF006) — fatal here, where the whole fire-and-forget model is background tasks.
    background: set[asyncio.Task[None]] = set()
    # The run in flight per session (bare session key). Registered synchronously before the run's
    # task is scheduled and removed when it ends, so every invoke sees the run it would supersede.
    active: dict[str, _Run] = {}

    def _model_choice(payload: dict[str, Any]) -> tuple[str, str] | None:
        """The run's caller-chosen ``(model, effort)``; ``None`` is the deployment default.

        An effort alone applies to ``config.model``; a model alone runs at ``"auto"``.

        Raises:
            ValueError: If the payload's model or effort fails :func:`_model_choice_of`.
        """
        choice = _model_choice_of(payload)
        if choice is None:
            return None
        model, effort = choice
        return model or config.model, effort or "auto"

    def _ctx(payload: dict[str, Any]) -> RuntimeContext:
        try:
            choice = _model_choice(payload)
        except ValueError:
            # A run-starting invoke was refused for this up front; anything else has no model.
            choice = None
        return RuntimeContext(
            session_id=payload.get("sessionId") or payload.get("sessionArn"),
            session_key=payload.get("sessionKey"),
            event=payload,
            model=choice[0] if choice else None,
            effort=choice[1] if choice else None,
        )

    def _run_model(ctx: RuntimeContext) -> tuple[str, str]:
        """The model string and effort a run records in the ledger."""
        if ctx.model is not None:
            return ctx.model, ctx.effort or "auto"
        return config.model, config.effort

    def _deregister(key: str | None, run: _Run) -> None:
        if key is not None and active.get(key) is run:
            del active[key]

    async def _terminal_reaction(payload: dict[str, Any], content: str) -> None:
        """Post the terminal lifecycle reaction (🚀 completed / 😕 failed) on the mention.

        The mention poller forwards the mention's reactions endpoint as ``ackReactionsUrl`` (the
        same item it 👀-acked). Best-effort: no URL, no token, or a failed POST degrade to nothing.
        The blocking ``urllib`` POST runs in a thread so it can't stall the event loop.
        """
        token = config.github_token
        url = payload.get("ackReactionsUrl")
        if not token or not isinstance(url, str) or not url:
            return
        try:
            await asyncio.to_thread(add_reaction, url, token, content)
        except Exception as e:  # noqa: BLE001 — telemetry only; never break a finished run
            logger.warning("terminal reaction %r failed for %s: %s", content, url, e)

    async def _run(
        payload: dict[str, Any],
        run: _Run,
        async_id: int,
    ) -> None:
        """Run one turn to completion in the background.

        The durable result is the stored session (the snapshot session manager persists it under
        the payload's ``sessionId``); we also record the final text in the in-instance sentinel for
        a fast same-instance poll, and (when configured) mirror the run into the durable ledger.
        Every normalized event is also published to the task's live-event buffer (``store.emit``)
        so attached SSE streams and ``since``-cursor polls can watch the run in real time.

        ``run.cancel`` is this run's own cancel signal: once set (a newer message superseded the
        run, or a Stop), the turn stops and the run ends ``cancelled`` — no work product, no
        terminal reaction.
        """
        task_id = run.task_id
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        session_id = payload.get("sessionId") or payload.get("sessionArn")
        key = session_key(_ctx(payload))
        initial_input = _prompt_of(payload)
        target_url, repo = _github_target(_github_context(payload))
        # HOW the run started + its portal role — written to the ledger only when
        # truthy, so an event/mention/CLI invoke that sets neither produces an identical row.
        trigger_source = _trigger_source(payload)
        mode = _portal_mode(payload)
        requested_by = _requested_by(payload)
        run_model, run_effort = _run_model(_ctx(payload))
        trace_id = _trace_id()
        if ledger:
            ledger.start(
                task_id,
                session_id=session_id,
                github_target=target_url,
                repo=repo,
                prompt=initial_input,
                trace_id=trace_id,
                started_at=started_at,
                model=run_model,
                effort=run_effort,
                trigger_source=trigger_source,
                mode=mode,
                requested_by=requested_by,
            )
            started_ledger = ledger

            def fail_row(reason: str) -> None:
                started_ledger.fail(
                    task_id,
                    error=reason,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    session_id=session_id,
                    github_target=target_url,
                    repo=repo,
                    prompt=initial_input,
                    trace_id=trace_id,
                    started_at=started_at,
                    model=run_model,
                    effort=run_effort,
                    trigger_source=trigger_source,
                    mode=mode,
                    requested_by=requested_by,
                    only_if_running=True,
                )

            _IN_FLIGHT_RUNS[task_id] = fail_row
        metrics.emit({metrics.INVOCATIONS: 1}, surface=metrics.SURFACE_AGENTCORE)
        # Per run, across its turns. Initialized OUTSIDE the try so the failure path below can
        # always record it.
        tool_errors = 0
        receipt_watch: asyncio.Task[None] | None = None
        try:
            if payload.get("resultId"):
                await run_receipts.write(config, payload, {"status": "running"})
                if await run_receipts.cancel_requested(config, payload):
                    run.cancel.set()
                receipt_watch = asyncio.create_task(run_receipts.watch_cancel(config, payload, run.cancel))
            # Capture the final assistant text as the poll result (the agent also reports via
            # GitHub — this is the convenience copy), plus token usage from the terminal event.
            # A model call that fails on a transient error is re-issued by the SDK inside the turn
            # (core/retries.py), which arrives here as a `retry` event; any error that escapes the
            # turn fails the run.
            final: list[str] = []
            # Just the turn in flight. A run can hold several turns (a background-results
            # continuation, a critic retry) and every turn boundary resets this, so what lands in the sentinel is the
            # LAST turn's text — the only piece a live watcher can write into the bubble it has open.
            turn_final: list[str] = []
            # Set by any non-text event: the next text starts another assistant message.
            text_break = False
            # Where the model call in flight began in `final` / `turn_final`: a retried call's text
            # is regenerated, so a `retry` drops what it streamed.
            call_final = call_turn_final = 0
            usage: dict[str, int] | None = None
            stop_reason: str | None = None
            # The turn's stop reason as the SDK reported it (None until its done event), and what
            # the turn reports back (``skipped``: cancelled before its first model call).
            ended: str | None = None
            outcome: dict[str, Any] = {}
            try:
                async for ev in run_turn(
                    config,
                    initial_input,
                    _ctx(payload),
                    model=model,
                    cancel_signal=run.cancel,
                    outcome=outcome,
                ):
                    # Capture the trace id once the turn is underway: the model-call span is
                    # live by the first event, so the OTel context is populated (it isn't yet
                    # at `_run` start).
                    if trace_id is None:
                        trace_id = _trace_id()
                    # Publish to the live channel (SSE attachments + since-cursor polls) —
                    # except `error`, which is re-raised below and lands on the channel as the
                    # terminal `error` (failure path); emitting it here too would double-report
                    # the same exception.
                    if ev.kind != "error":
                        store.emit(task_id, _event_dict(ev))
                    if ev.kind not in ("text", "reasoning"):
                        text_break = True
                    if ev.kind == "turn_start":
                        # A new turn (the original ask, a background-results continuation, or a
                        # critic retry — ``ev.data["reason"]`` says which). A retry SUPERSEDES the draft
                        # it re-attempts, so resetting here is right for every reason.
                        turn_final = []
                    elif ev.kind == "retry":
                        del final[call_final:]
                        del turn_final[call_turn_final:]
                        logger.warning(
                            "model call retried after a transient error (task_id=%s, delay=%ss)",
                            task_id,
                            ev.data.get("delaySeconds"),
                        )
                    elif ev.kind == "text" and ev.text:
                        if text_break:
                            _break_text(final)
                            _break_text(turn_final)
                            text_break = False
                        final.append(ev.text)
                        turn_final.append(ev.text)
                    elif ev.kind == "tool_result" and ev.data.get("status") == "error":
                        # A failed tool call (expired GitHub token, sandbox error, …). The
                        # turn usually still ends in a polite "I couldn't…" — i.e. status
                        # "completed" — so record the signal for the ledger/dashboard.
                        tool_errors += 1
                    elif ev.kind == "done":
                        usage = ev.data.get("usage") or usage
                        stop_reason = ev.data.get("stop_reason") or stop_reason
                        ended = ev.data.get("stop_reason")
                    elif ev.kind == "error":
                        # `translate` swallows stream exceptions into this event so
                        # interactive surfaces can render them inline — but HERE it must be
                        # the exception it was, or a run whose stream died (expired creds, a
                        # dropped connection) is recorded "completed" with partial text (the
                        # dashboard false-success bug).
                        raise TurnStreamError(ev.data.get("error_type"), ev.text)
                    if ev.kind not in ("text", "reasoning", "retry"):
                        call_final, call_turn_final = len(final), len(turn_final)
            except Exception as e:
                if not run.cancel.is_set():
                    raise
                logger.info("cancelled run %s ended with %s: %s", task_id, type(e).__name__, e)
            # Decided by how the turn ended, not by the signal alone: a turn the SDK finished on its
            # own stays completed even if a message landed while its after-invocation hooks ran,
            # but one the cancel cut off while it waited for background work did not finish.
            cancelled = (
                bool(outcome.get("skipped"))
                or bool(outcome.get("abandoned"))
                or ended == "cancelled"
                or (run.cancel.is_set() and ended is None)
            )
            # Off the registry now: the turn is over, so a message arriving while this run records
            # its result starts a new run instead of "cancelling" one that already finished.
            _deregister(key, run)
            # Pull the skill's structured work-product block out of the final message BEFORE it is
            # recorded anywhere as prose: the cleaned text is what lands in the
            # sentinel, the ledger's result_summary, and any surface that renders the run — the block
            # is metadata, persisted separately as a WorkProduct. Fail-open: a malformed block just
            # isn't persisted (the strip still happens, so it never leaks into prose either way).
            # A cancelled run's text is partial, so its block is only stripped, never persisted.
            wp_data: dict[str, Any] | None = None
            if cancelled:
                text = _strip_work_product("".join(final))
            else:
                text, wp_data = _extract_work_product(
                    "".join(final), repo=repo, target_url=target_url
                )
            last_turn = _strip_work_product("".join(turn_final))
            if cancelled:
                store.cancel(task_id, text, last_turn=last_turn)
            else:
                store.finish(task_id, text, last_turn=last_turn)
            if work_products is not None and wp_data is not None:
                _persist_work_product(
                    work_products, wp_data, session_id=session_id, task_id=task_id
                )
            _emit_run_metrics(
                metrics.CANCELLED if cancelled else metrics.COMPLETED,
                started=started,
                usage=usage,
                tool_errors=tool_errors,
            )
            _IN_FLIGHT_RUNS.pop(task_id, None)
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
                    prompt=initial_input,
                    trace_id=trace_id,
                    started_at=started_at,
                    tool_errors=tool_errors,
                    stop_reason=stop_reason,
                    # Per-model token attribution survives a model change between turns or deploys.
                    model=run_model,
                    effort=run_effort,
                    trigger_source=trigger_source,
                    mode=mode,
                    requested_by=requested_by,
                    status=STATUS_CANCELLED if cancelled else STATUS_COMPLETED,
                )
            # A superseded trigger gets no terminal reaction: the run that replaced it reacts.
            if not cancelled:
                await _terminal_reaction(payload, REACTION_COMPLETED)
        except Exception as e:  # noqa: BLE001 — record failure for the poll, don't crash the worker
            logger.exception("background run failed (task_id=%s)", task_id)
            # Terminal error onto the live channel (attached streams render it inline; the
            # since-cursor poll sees it before the status flips), then fail the sentinel —
            # store.fail also ends every live subscription.
            store.emit(
                task_id,
                {
                    "kind": "error",
                    "text": f"{type(e).__name__}: {e}",
                    "tool": None,
                    "toolUseId": None,
                    "data": {"error_type": type(e).__name__},
                },
            )
            store.fail(task_id, f"{type(e).__name__}: {e}")
            _emit_run_metrics(
                metrics.FAILURES, started=started, usage=None, tool_errors=tool_errors
            )
            _IN_FLIGHT_RUNS.pop(task_id, None)
            if ledger:
                ledger.fail(
                    task_id,
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    session_id=session_id,
                    github_target=target_url,
                    repo=repo,
                    prompt=initial_input,
                    trace_id=trace_id,
                    started_at=started_at,
                    tool_errors=tool_errors,
                    model=run_model,
                    effort=run_effort,
                    trigger_source=trigger_source,
                    mode=mode,
                    requested_by=requested_by,
                )
            await _terminal_reaction(payload, REACTION_FAILED)
        finally:
            if receipt_watch is not None:
                receipt_watch.cancel()
                try:
                    await receipt_watch
                except asyncio.CancelledError:
                    pass
            if payload.get("resultId"):
                store.settle_receipt(task_id)
                try:
                    await run_receipts.persist_terminal(config, payload, store.get(task_id))
                except Exception:  # noqa: BLE001 — a storage failure must not strand runtime cleanup
                    logger.exception("durable run receipt failed (task_id=%s)", task_id)
            # Land this run's metrics now rather than at the emitter's next timed flush (direct
            # PutMetricData on the runtime): WAKE the background flusher and move on — an awaited
            # flush would queue behind a slow in-flight one. ``request_flush`` never raises.
            metrics.request_flush()
            _IN_FLIGHT_RUNS.pop(task_id, None)
            _deregister(key, run)
            app.complete_async_task(async_id)

    async def _attach(
        task_id: str,
        queue: asyncio.Queue[dict[str, Any] | None],
        init_data: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a detached background run's live events to the caller (SSE).

        The run itself is `_start_run`'s background task — this generator only *watches* it, so a
        client disconnect (the SDK closing the generator) never stops the agent. The first frame is
        ``{kind: "init", taskId}``: with the taskId in hand a cut-off client re-attaches via
        ``{action: "poll", since: <last seq + 1>}`` and misses nothing (every event is also in the
        task's buffer). ``None`` on the queue marks the end of the run (store.finish/fail).
        """
        try:
            yield {
                "kind": "init",
                "text": None,
                "tool": None,
                "toolUseId": None,
                "data": {"taskId": task_id, **(init_data or {})},
            }
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            store.unsubscribe(task_id, queue)

    def _start_run(payload: dict[str, Any]) -> str:
        """Start one turn as a detached background task; returns its (unsigned hex) task id.

        Shared by both modes — the only difference is whether anyone is watching: fire-and-forget
        returns just the ack, streaming attaches an SSE subscriber to the same machinery. Marks the
        app HEALTHY_BUSY for the life of the task (add_async_task/complete_async_task) so AgentCore
        doesn't recycle the instance mid-run — including a streamed run whose client disconnected.

        Synchronous on purpose: the run is registered as the session's active run before any other
        invoke can interleave, so the next message always sees (and supersedes) it.
        """
        _install_sigterm_handler()  # here: uvicorn installs its own handlers once serving starts
        async_id = app.add_async_task("strandly_run")
        # The SDK's async task id is a signed int; a leading '-' would be parsed as a flag by
        # `strandly poll`. Expose an unsigned hex token to the caller, keyed back to the SDK id.
        task_id = format(async_id & 0xFFFFFFFFFFFFFFFF, "x")
        run = _Run(
            task_id=task_id,
            cancel=threading.Event(),
            trigger_id=_str_field(payload, "triggerId"),
            trigger_hash=_str_field(payload, "triggerHash"),
            prompt_hash=_prompt_hash(_prompt_of(payload)),
            result_id=_str_field(payload, "resultId"),
        )
        key = session_key(_ctx(payload))
        store.start(task_id, run_receipts.key(payload) if payload.get("resultId") else None, key)
        previous = active.get(key) if key is not None else None
        if key is not None:
            active[key] = run
        try:
            task = asyncio.create_task(_run(payload, run, async_id))
        except Exception:
            _deregister(key, run)
            if key is not None and previous is not None:
                active[key] = previous
            app.complete_async_task(async_id)
            raise
        background.add(task)
        task.add_done_callback(background.discard)
        return task_id

    def _cancel_session(payload: dict[str, Any]) -> dict[str, Any]:
        """``{"action": "cancel"}`` — stop the session's run in flight, starting nothing."""
        key = session_key(_ctx(payload))
        if not key:
            return {"status": "error", "error": "cancel requires a 'sessionId'"}
        run = active.get(key)
        if run is None or (payload.get("resultId") and run.result_id != payload["resultId"]):
            return {"status": "idle", "sessionId": key}
        run.cancel.set()
        logger.info("stop requested on %s (task=%s)", key, run.task_id)
        return {"status": "cancelled", "sessionId": key, "taskId": run.task_id}

    @app.entrypoint
    async def invoke(payload: dict[str, Any]) -> Any:
        await instance.ensure_ready()  # per-instance state, post-snapshot (no-op once wired)
        if payload.get("action") == "poll" and payload.get("capabilities") == "runReceipts":
            return {"runReceipts": 1}
        if "resultId" in payload:
            try:
                run_receipts.key(payload)
            except ValueError as exc:
                return {"status": "error", "error": str(exc)}
            if payload.get("action") == "cancel":
                # Cancellation must not depend on availability of the result object. Persist
                # intent and signal first; a missing/unreadable receipt cannot block Stop.
                await run_receipts.request_cancel(config, payload)
                _cancel_session(payload)
                try:
                    receipt = await run_receipts.read(config, payload)
                except Exception:  # noqa: BLE001 — intent is durable; report pending, never terminal
                    logger.warning("cancel requested but result receipt unavailable")
                    return {"status": "cancel_requested"}
                if receipt["status"] in {"completed", "failed", "cancelled"}:
                    return receipt
                return {"status": "cancel_requested"}
            if payload.get("action") == "poll":
                receipt = await run_receipts.read(config, payload)
                since = payload.get("since")
                if not isinstance(since, int) or since < 0:
                    since = 0
                exact = store.receipt(run_receipts.key(payload), since)
                if receipt["status"] in {"unknown", "running"} and exact["status"] in {"completed", "failed", "cancelled"}:
                    await run_receipts.persist_terminal(config, payload, exact)
                    receipt = await run_receipts.read(config, payload)
                if "since" not in payload:
                    return receipt
                return {
                    **receipt,
                    "events": exact.get("events", []),
                    "nextSeq": exact.get("nextSeq", 0),
                    # None explicitly means the live buffer is unavailable on this instance.
                    "firstSeq": exact.get("firstSeq"),
                    "taskId": exact.get("taskId"),
                }
            if payload.get("action") not in {None, ""}:
                return {"status": "error", "error": "unsupported receipt action"}
            receipt = await run_receipts.read(config, payload)
            exact = store.receipt(run_receipts.key(payload))
            if receipt["status"] != "unknown":
                return receipt
            if exact["status"] != "unknown":
                return {"status": exact["status"]}
        # Poll action: merge the in-instance sentinel with the durable stored session. An integer
        # `since` cursor additionally returns the run's buffered live events from that sequence on.
        if payload.get("action") == "poll":
            task_id = payload.get("taskId")
            if not task_id:
                return {"status": "error", "error": "poll requires a 'taskId'"}
            since_raw = payload.get("since")
            try:
                since = int(since_raw) if since_raw is not None else None
            except (TypeError, ValueError):
                since = None
            return poll_result(
                config, store, str(task_id), payload.get("sessionId"), since=since,
                issued_only=payload.get(POLL_ISSUED_ONLY_KEY) is True,
            )

        if payload.get("action") == "cancel":
            return _cancel_session(payload)

        # Everything below starts a run. Refuse one with nothing to answer NOW — before it can
        # supersede the session's running turn, write a ledger row or build an agent. Logged at
        # WARNING with no payload (it may carry ingress secrets).
        if _is_blank_prompt(payload):
            logger.warning(
                "refused an invoke with a blank prompt (session=%s, keys=%s)",
                session_key(_ctx(payload)) or "-",
                sorted(str(k)[:40] for k in payload)[:20],
            )
            return _blank_prompt_response()
        # The runtime is the authoritative gate on a caller's model (a direct invoke skips the
        # dashboard API): the harness model resolution refuses a choice before any work starts.
        # Off the event loop: building a model client may import its provider package.
        try:
            choice = _model_choice(payload)
            if choice is not None:
                await asyncio.to_thread(check_model, *choice)
        except (ValueError, TypeError, ImportError) as exc:
            logger.warning(
                "refused an invoke whose model choice the harness cannot build | error=<%s>", exc
            )
            return _invalid_model_response(str(exc))

        key = session_key(_ctx(payload))
        current = active.get(key) if key is not None else None
        if current is not None:
            body: dict[str, Any] = {"taskId": current.task_id, "sessionId": key}
            if payload.get("queueStrategy") == "reject":
                return {"status": "rejected", **body}
            if _is_redispatch(payload, current):
                logger.info("re-dispatch of running trigger on %s is a no-op", key)
                body = {"status": "accepted", **body, "duplicate": True}
                if _is_stream(payload):
                    return _attach(current.task_id, store.subscribe(current.task_id), body)
                return body

        # Streaming / chat mode: start the run DETACHED (same background machinery as
        # fire-and-forget — ledger, metrics, retries, session durability) and return an async
        # generator attached to its live events → the SDK streams it as text/event-stream. A
        # dropped connection ends the attachment, never the run.
        # Fire-and-forget: an ack; the result is read back by `strandly poll` (sentinel + settle)
        # or watched live via `since`. The new run starts before the old one is cancelled, so a
        # failure to start it leaves the old run going rather than losing the message.
        stream = _is_stream(payload)
        task_id = _start_run(payload)
        if current is not None:
            logger.info("new message on %s supersedes run %s", key, current.task_id)
            current.cancel.set()
        if stream:
            # Subscribing in the same event-loop tick as create_task is race-free: the background
            # task cannot run before this coroutine's next await, so no early event is missed.
            return _attach(task_id, store.subscribe(task_id))
        return {"status": "accepted", "taskId": task_id}

    # LAST middleware added = outermost (Starlette inserts at the front), so the reseed runs before
    # OpenTelemetry's server span is opened and before the first-request config load. Keep it the
    # final add_middleware call in this function (tests/unit/serve/test_snapshot_safety.py).
    app.add_middleware(ReseedRandomnessMiddleware)
    return app


def serve_agentcore(config: Config) -> None:
    build_app(config).run()
