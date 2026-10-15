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

EventKind = Literal["text", "reasoning", "tool_start", "tool_result", "done", "error"]


@dataclass
class HarnessEvent:
    kind: EventKind
    text: str | None = None
    tool: str | None = None
    tool_use_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


def classify_all(raw: Mapping[str, Any], seen_tools: dict[str, str]) -> list[HarnessEvent]:
    """Map a single Strands event dict to zero or more ``HarnessEvent``s.

    ``seen_tools`` maps toolUseId -> tool name, so a streamed tool-use start is emitted once and
    the later result can be labeled with the tool name.

    Returns a list because a single ``message`` event can carry **multiple** ``toolResult``
    blocks when the model fans out parallel tool calls in one turn — each becomes its own
    ``tool_result`` HarnessEvent.

    Live-Bedrock shapes (verified by the P0 smoke test, issue #318):

    - Tool-use START streams as ``{"type": "tool_use_stream", "current_tool_use": {...}}``.
    - Tool RESULTS arrive as a *complete user message*::

          {"message": {"role": "user", "content": [{"toolResult": {"toolUseId": ..,
            "status": ..., "content": [...]}}]}}

      The live SDK never emits a top-level ``tool_result`` key — the old fabricated shape
      ``{"type": "tool_result", "tool_result": {...}}`` was a test fiction, so before this fix
      ``translate`` dropped *every* tool result against a real model (tool_start fired, the
      matching ``← tool [status]`` never did). The legacy shape is still handled for
      backward-compatibility / non-streaming providers.
    - Assistant text deltas surface as ``{"data": "<chunk>", ...}``.
    - The terminal event is ``{"result": AgentResult}``.
    """
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
                    if tool_id and tool_id not in seen_tools:
                        seen_tools[tool_id] = name or ""
                        events.append(
                            HarnessEvent(
                                kind="tool_start",
                                tool=name,
                                tool_use_id=tool_id,
                                data={"input": tu.get("input")},
                            )
                        )
        return events

    # Legacy / non-streaming tool result shape (kept for back-compat; live SDK never emits it).
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
        data: dict[str, Any] = {"stop_reason": stop_reason}
        usage = _extract_usage(result)
        if usage:
            data["usage"] = usage
        return [HarnessEvent(kind="done", data=data)]

    return []


def _extract_usage(result: Any) -> dict[str, int] | None:
    """Pull accumulated token usage off an ``AgentResult`` (best-effort, shape-tolerant).

    The terminal event's ``result`` is an ``AgentResult`` whose ``.metrics.accumulated_usage`` is a
    Strands ``Usage`` mapping (``inputTokens``/``outputTokens``/``totalTokens``). We read it
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
    ):
        val = usage.get(src_key)
        if isinstance(val, bool):  # bool is an int subclass — exclude it
            continue
        if isinstance(val, int):
            out[dst_key] = val
    return out or None


def classify(raw: Mapping[str, Any], seen_tools: dict[str, str]) -> HarnessEvent | None:
    """Back-compat single-event wrapper around :func:`classify_all`.

    Returns the first HarnessEvent a raw event maps to (or ``None``). Prefer ``classify_all``
    for new code: a single ``message`` event may carry multiple ``toolResult`` blocks.
    """
    events = classify_all(raw, seen_tools)
    return events[0] if events else None


async def translate(
    stream: AsyncIterator[Mapping[str, Any]],
) -> AsyncIterator[HarnessEvent]:
    """Translate a Strands event stream into a normalized HarnessEvent stream."""
    seen_tools: dict[str, str] = {}
    saw_done = False
    try:
        async for raw in stream:
            for ev in classify_all(raw, seen_tools):
                if ev.kind == "done":
                    saw_done = True
                yield ev
    except Exception as exc:  # surface model/loop errors as a normalized error event
        yield HarnessEvent(kind="error", text=str(exc), data={"error_type": type(exc).__name__})
        return
    if not saw_done:
        yield HarnessEvent(kind="done")
