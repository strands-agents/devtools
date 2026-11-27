"""Normalized event stream.

Every serving surface consumes one ``HarnessEvent`` stream. ``translate`` maps the Strands
``stream_async`` events (dict-shaped ``TypedEvent``s) into that normalized form, deduping tool
ids so a tool's streamed input is attached to its eventual result. Pure: an async iterator of
Strands events in, an async iterator of HarnessEvents out — testable with canned dicts, no model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal[
    "turn_start",
    "text",
    "reasoning",
    "tool_start",
    "tool_input",
    "tool_result",
    "done",
    "error",
    "retry",
]

# The invocation-state key a resume writer stamps on the turn it opens, so the turn boundary says WHY
# the agent resumed. The SDK merges the invocation state into the next turn's ``init_event_loop``
# event (``InitEventLoopEvent.prepare``), so the mark arrives attached to exactly the turn it opened.
# The value is ``{"seq": <monotone counter>, "reason": <one of NAMED_RESUME_REASONS>}``:
#
# * **reason** — the writer names its own boundary: the goal loop stamps :data:`REASON_RESUME` on its
#   critic retries. A turn nobody names (e.g. the SDK's background-task delivery) is
#   :data:`REASON_CONTINUE`.
# * **seq** — a counter, not a flag, because the state dict PERSISTS across the turns of one run: a
#   flag set at turn 2 is still set at turn 3. A writer counts its own resumes; :func:`translate`
#   credits a mark only when its ``seq`` is higher than any it has already credited *for that reason*.
RESUME_MARK_KEY = "strandly_resume_mark"

# Why a turn_start happened, as ``HarnessEvent.data["reason"]``. Resolved by :func:`translate` (which
# is stateful per stream) rather than by :func:`classify_all` (which sees one event at a time), so
# every consumer — including one that JOINS a run mid-flight and never saw its earlier turns — gets
# the boundary already named instead of re-deriving it from a counter whose baseline it missed.
REASON_START = "start"  # the run's first turn: the original ask
REASON_RESUME = "resume"  # the goal loop's critic retry: another pass at the SAME ask
REASON_CONTINUE = "continue"  # a continuation nobody named (e.g. background-task results)

# The reasons a writer may stamp on itself. Invocation state is a shared namespace, so an unvalidated
# ``reason`` would let an unrelated key steer a consumer's rendering; a mark naming anything else is
# ignored (the boundary falls back to the neutral :data:`REASON_CONTINUE`).
NAMED_RESUME_REASONS = frozenset({REASON_RESUME})


@dataclass
class HarnessEvent:
    kind: EventKind
    text: str | None = None
    tool: str | None = None
    tool_use_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class TurnStreamError(RuntimeError):
    """A turn's underlying stream exception, re-materialized from an ``error`` HarnessEvent.

    :func:`translate` catches any exception the agent stream raises and normalizes it into
    ``HarnessEvent(kind="error")`` so *interactive* surfaces (SSE chat, the REPL, MCP) can render
    the failure inline without killing the stream framing. But a *non-interactive* consumer — the
    deployed fire-and-forget run — must treat that event as the exception it was: swallowing it
    records a half-dead run as ``completed`` (the dashboard false-success bug). Such consumers
    re-raise the event as this exception.

    The message leads with the ORIGINAL exception type name (``"<ErrorType>: <message>"``), so the
    failure keeps the signature of the exception the stream died with.
    """

    def __init__(self, error_type: str | None, message: str | None):
        self.error_type = error_type or "UnknownError"
        super().__init__(f"{self.error_type}: {message or ''}")


def _resume_mark(value: Any) -> tuple[int, str] | None:
    """Validate a resume mark off the merged invocation state, or ``None`` if it is not one.

    Invocation state is a shared namespace: every rejection here is a key that could otherwise name a
    boundary it did not open. ``bool`` is excluded explicitly — it is an ``int`` subclass, so ``True``
    would read as sequence 1 — and ``reason`` must be one a writer is allowed to claim
    (:data:`NAMED_RESUME_REASONS`).
    """
    if not isinstance(value, Mapping):
        return None
    seq = value.get("seq")
    reason = value.get("reason")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    if reason not in NAMED_RESUME_REASONS:
        return None
    return seq, reason


def classify_all(raw: Mapping[str, Any], seen_tools: dict[str, str]) -> list[HarnessEvent]:
    """Map a single Strands event dict to zero or more ``HarnessEvent``s.

    ``seen_tools`` maps toolUseId -> tool name, so a streamed tool-use start is emitted once and
    the later result can be labeled with the tool name.

    Returns a list because a single ``message`` event can carry **multiple** ``toolResult``
    blocks when the model fans out parallel tool calls in one turn — each becomes its own
    ``tool_result`` HarnessEvent.

    Live-Bedrock shapes:

    - Tool-use START streams as ``{"type": "tool_use_stream", "current_tool_use": {...}}``.
      Emitted on the FIRST delta so a watcher sees the call immediately — but at that point the
      input is empty or a partial JSON prefix (the SDK accumulates it delta by delta), so the
      ``tool_start``'s ``data["input"]`` is NOT the call's arguments. The complete input only
      exists once the assistant message lands (below), which emits a ``tool_input`` event for the
      same ``toolUseId`` — a UI fills the streamed tool block in with it.
    - A tool's intermediate yields stream as ``{"type": "tool_stream", "tool_stream_event":
      {...}}`` and are dropped (the tool result carries the output).
    - Tool RESULTS arrive as a *complete user message*::

          {"message": {"role": "user", "content": [{"toolResult": {"toolUseId": ..,
            "status": ..., "content": [...]}}]}}

      The live SDK never emits a top-level ``tool_result`` key; the
      ``{"type": "tool_result", "tool_result": {...}}`` shape is still accepted for
      non-streaming providers.
    - Assistant text deltas surface as ``{"data": "<chunk>", ...}``.
    - The terminal event is ``{"result": AgentResult}``.
    - Each *turn* inside one ``stream_async`` call opens with ``{"init_event_loop": True}``
      (``Agent._run_loop`` yields it once per loop iteration, so a hook-requested ``resume``
      starts a second one). Mapped to ``turn_start``, which is the only signal a live watcher
      gets that the answer it is streaming has moved on; without it the dashboard renders two
      turns as one merged bubble. A resume comes from the goal loop (a critic retry) or a
      background task's result, so the boundary is *named* — see :data:`RESUME_MARK_KEY` and
      :func:`translate`, which resolves ``data["reason"]``.
    """
    # Turn boundary FIRST: the SDK merges ``invocation_state`` into this event's dict
    # (``InitEventLoopEvent.prepare``), so an unlucky invocation-state key could otherwise be
    # picked up by a later branch. The marker key is the SDK's own and is never user data.
    if raw.get("init_event_loop"):
        # The merged invocation state may carry a resume mark (see :data:`RESUME_MARK_KEY`). Passed
        # through RAW here — deciding whether it is *fresh* needs the previous turns' marks, which
        # only :func:`translate` has. Validated on the way through, because invocation state is a
        # shared namespace: an unrelated key must not be able to name a boundary.
        mark = _resume_mark(raw.get(RESUME_MARK_KEY))
        return [HarnessEvent(kind="turn_start", data={RESUME_MARK_KEY: mark} if mark else {})]

    # The SDK re-issues a model call that failed on a retryable error (core/retries.py), after its
    # delay: the text that call streamed is being regenerated.
    if "event_loop_throttled_delay" in raw:
        return [HarnessEvent(kind="retry", data={"delaySeconds": raw["event_loop_throttled_delay"]})]

    etype = raw.get("type")

    # Tool use start (may stream repeatedly as input fills in; emit once per id).
    if etype == "tool_use_stream" or "current_tool_use" in raw:
        cur = raw.get("current_tool_use") or {}
        tool_id = cur.get("toolUseId")
        name = cur.get("name")
        if tool_id and tool_id not in seen_tools:
            seen_tools[tool_id] = name or ""
            return [
                HarnessEvent(
                    kind="tool_start",
                    tool=name,
                    tool_use_id=tool_id,
                    data={"input": cur.get("input")},
                )
            ]
        return []

    # A tool's intermediate yields (the SDK's ToolStreamEvent) are not surfaced: a tool's output is
    # its result, and the SDK re-yields a streaming tool's final value here before wrapping it.
    if etype == "tool_stream" or "tool_stream_event" in raw:
        return []

    # Complete message event (live Bedrock shape). A user message carries toolResult blocks;
    # an assistant message may carry toolUse blocks (a safety net for providers that don't
    # stream tool-use deltas — deduped via seen_tools so we never double-emit a tool_start).
    msg = raw.get("message")
    if isinstance(msg, Mapping):
        role = msg.get("role")
        content = msg.get("content") or []
        events: list[HarnessEvent] = []
        if role == "user":
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                tr = block.get("toolResult")
                if isinstance(tr, Mapping):
                    tool_id = tr.get("toolUseId")
                    events.append(
                        HarnessEvent(
                            kind="tool_result",
                            tool=seen_tools.get(tool_id or ""),
                            tool_use_id=tool_id,
                            data={"status": tr.get("status"), "content": tr.get("content")},
                        )
                    )
        elif role == "assistant":
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                tu = block.get("toolUse")
                if isinstance(tu, Mapping):
                    tool_id = tu.get("toolUseId")
                    name = tu.get("name")
                    if not tool_id:
                        continue
                    if tool_id not in seen_tools:
                        seen_tools[tool_id] = name or ""
                        events.append(
                            HarnessEvent(
                                kind="tool_start",
                                tool=name,
                                tool_use_id=tool_id,
                                data={"input": tu.get("input")},
                            )
                        )
                    else:
                        # The streamed tool_start for this id carried an empty/partial input (it
                        # fires on the first delta — see the docstring); THIS block has the
                        # complete parsed arguments. Emit them as a follow-up so a live watcher
                        # can fill the already-rendered tool call in.
                        events.append(
                            HarnessEvent(
                                kind="tool_input",
                                tool=name or seen_tools[tool_id] or None,
                                tool_use_id=tool_id,
                                data={"input": tu.get("input")},
                            )
                        )
        return events

    # The non-streaming tool result shape; the live SDK never emits it.
    if etype == "tool_result" or "tool_result" in raw:
        tr = raw.get("tool_result") or {}
        tool_id = tr.get("toolUseId")
        return [
            HarnessEvent(
                kind="tool_result",
                tool=seen_tools.get(tool_id or ""),
                tool_use_id=tool_id,
                data={"status": tr.get("status"), "content": tr.get("content")},
            )
        ]

    # Reasoning text delta.
    if raw.get("reasoning") and raw.get("reasoningText") is not None:
        return [HarnessEvent(kind="reasoning", text=raw.get("reasoningText"))]

    # Assistant text delta. TextStreamEvent puts the chunk under "data".
    if "data" in raw and isinstance(raw["data"], str):
        return [HarnessEvent(kind="text", text=raw["data"])]

    # Terminal result. The raw object is a (non-serializable) AgentResult; extract just the
    # serializable bits so downstream SSE/JSON encoding is safe.
    if "result" in raw:
        result = raw["result"]
        stop_reason = getattr(result, "stop_reason", None)
        if stop_reason is None and isinstance(result, Mapping):
            stop_reason = result.get("stop_reason")
        done_data: dict[str, Any] = {"stop_reason": stop_reason}
        usage = _extract_usage(result)
        if usage:
            done_data["usage"] = usage
        return [HarnessEvent(kind="done", data=done_data)]

    return []


def _extract_usage(result: Any) -> dict[str, int] | None:
    """Pull accumulated token usage off an ``AgentResult`` (best-effort, shape-tolerant).

    The terminal event's ``result`` is an ``AgentResult`` whose ``.metrics.accumulated_usage`` is a
    Strands ``Usage`` mapping (``inputTokens``/``outputTokens``/``totalTokens``, plus
    ``cacheReadInputTokens``/``cacheWriteInputTokens`` when the model reports them). We read it
    defensively so a shape change (or a plain-dict result in tests) never raises into the stream;
    on anything unexpected we return ``None`` and the ``done`` event simply carries no usage. This
    is what feeds the run-ledger's token counts.
    """
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None)
    if usage is None and isinstance(result, Mapping):
        m = result.get("metrics")
        if isinstance(m, Mapping):
            usage = m.get("accumulated_usage")
        elif isinstance(result.get("usage"), Mapping):
            usage = result.get("usage")
    if not isinstance(usage, Mapping):
        return None
    out: dict[str, int] = {}
    for src_key, dst_key in (
        ("inputTokens", "input"),
        ("outputTokens", "output"),
        ("totalTokens", "total"),
        ("cacheReadInputTokens", "cacheRead"),
        ("cacheWriteInputTokens", "cacheWrite"),
    ):
        val = usage.get(src_key)
        if isinstance(val, bool):  # bool is an int subclass — exclude it
            continue
        if isinstance(val, int):
            out[dst_key] = val
    return out or None


async def translate(
    stream: AsyncIterator[Mapping[str, Any]],
) -> AsyncIterator[HarnessEvent]:
    """Translate a Strands event stream into a normalized HarnessEvent stream.

    Also **names every turn boundary** (``turn_start``'s ``data["reason"]``, one of
    :data:`REASON_START` / :data:`REASON_RESUME` / :data:`REASON_CONTINUE`). That needs the per-stream state this function already keeps: the first
    turn is the original ask, and a later one carries the reason its writer stamped on it — but only
    if that mark is *fresh* (a ``seq`` higher than any credited for that reason). A stale mark (the
    state dict outlives the turn that set it) means nobody claimed this boundary: a neutral
    continuation.
    """
    seen_tools: dict[str, str] = {}
    saw_done = False
    turns = 0
    credited: dict[str, int] = {}
    try:
        async for raw in stream:
            for ev in classify_all(raw, seen_tools):
                if ev.kind == "done":
                    saw_done = True
                elif ev.kind == "turn_start":
                    turns += 1
                    mark = ev.data.pop(RESUME_MARK_KEY, None)
                    if turns == 1:
                        # A first turn is never a delivery. Seed the baseline from whatever the state
                        # already holds so a REUSED invocation-state dict (a caller passing its own)
                        # can't make turn 2 look like a delivery on a stale mark.
                        ev.data["reason"] = REASON_START
                        if mark is not None:
                            credited[mark[1]] = mark[0]
                    elif mark is not None and mark[0] > credited.get(mark[1], 0):
                        ev.data["reason"] = mark[1]
                        credited[mark[1]] = mark[0]
                    else:
                        ev.data["reason"] = REASON_CONTINUE
                yield ev
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a normalized error event
        yield HarnessEvent(kind="error", text=str(exc), data={"error_type": type(exc).__name__})
        return
    if not saw_done:
        yield HarnessEvent(kind="done")
