"""Per-tool call and error metrics: one low-cardinality CloudWatch series per tool name.

Every finished tool call emits ``ToolCalls`` and ``ToolCallErrors`` under a ``[tool_name]``
dimension. The SDK's ``strands.tool.*`` metrics key on the per-call id, far too many series to sum
per tool within the Metrics Insights scan limit.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import AfterToolCallEvent
from strands.plugins import Plugin, hook

from strandly.ops import metrics

logger = logging.getLogger(__name__)


def _is_cancelled(event: AfterToolCallEvent) -> bool:
    """True if the call was cancelled (interrupt / ``BeforeToolCallEvent.cancel_tool``).

    A cancelled tool never ran, so it is neither a success nor a failure — counting it either way
    would skew the per-tool error rate this metric exists to make accurate, so we skip it entirely.
    The cancel path sets ``cancel_message`` on the event (and ``result["status"]`` to ``"error"``,
    which is exactly why a plain status check would misread it); ``result["cancelled"]`` covers a
    mid-execution cancel that some paths flag on the result instead.
    """
    if getattr(event, "cancel_message", None) is not None:
        return True
    result = getattr(event, "result", None)
    return isinstance(result, dict) and result.get("cancelled") is True


def _is_error(event: AfterToolCallEvent) -> bool:
    """True if the tool call ended in an error — a raised exception or an error tool result."""
    if getattr(event, "exception", None) is not None:
        return True
    result = getattr(event, "result", None)
    return isinstance(result, dict) and result.get("status") == "error"


class ToolMetrics(Plugin):
    """Emits a per-``tool_name`` call/error rollup after each tool invocation."""

    name = "tool_metrics"

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    async def _on_tool_call(self, event: AfterToolCallEvent) -> None:
        try:
            tool_use: Any = getattr(event, "tool_use", None) or {}
            tool_name = tool_use.get("name") if isinstance(tool_use, dict) else None
            if not tool_name or _is_cancelled(event):
                return
            metrics.emit_tool_call(tool_name, error=_is_error(event))
        except Exception:  # noqa: BLE001 — telemetry must never break a tool call
            logger.debug("tool metrics emit failed; continuing", exc_info=True)
