"""A scripted fake model for multi-call agent tests (tool call → answer), importable by name.

Lives in its own uniquely-named module (not ``conftest``): ``from conftest import …`` resolves to
whichever ``conftest`` pytest loaded last, so a helper imported lazily from a test body could get
the wrong module depending on run order.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from strands.models import Model


def text_events(text: str, stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    """Raw model wire-format stream for a plain text response."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop_reason}},
    ]


def tool_use_events(name: str, tool_use_id: str, tool_input: str = "{}") -> list[dict[str, Any]]:
    """Raw model wire-format stream for one tool call (``stopReason: tool_use``)."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tool_use_id}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": tool_input}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


class ScriptedModel(Model):
    """Replays one canned response per model call (then a plain "done"), and estimates tokens at
    chars/4 so the SDK context manager's token thresholds fire like on a real model."""

    stateful = False
    context_window_limit = 200_000

    def __init__(self, *responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.config: dict[str, Any] = {}

    def get_config(self) -> dict[str, Any]:
        return self.config

    def update_config(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        events = self.responses.pop(0) if self.responses else text_events("done")
        for ev in events:
            yield ev

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def count_tokens(self, messages: Any = None, *args: Any, **kwargs: Any) -> int:
        return len(json.dumps(messages, default=str)) // 4
