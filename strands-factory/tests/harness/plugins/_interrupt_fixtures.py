"""A scripted model, an approval-asking plugin and a trivial tool for interrupt-boundary tests."""

from __future__ import annotations

from typing import Any

from strands import tool
from strands.hooks import BeforeToolCallEvent
from strands.models import Model
from strands.plugins import Plugin, hook


def text_response_events(text: str, stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    """Raw model wire-format for a plain text response (same shape as the shared conftest helper)."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop_reason}},
    ]


def _tool_use_events(name: str, tool_use_id: str = "t1") -> list[dict[str, Any]]:
    """Raw model wire-format for a single tool call (what makes the next model call mid-loop)."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tool_use_id}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


class ScriptedModel(Model):
    """Replays one canned turn per ``stream`` call and records the messages it was sent.

    Recording the per-call messages is the point: it is the only way to assert that the injected
    block reached a *specific* model call (mid-loop) and that it never entered ``agent.messages``.
    """

    stateful = False
    context_window_limit = 200_000

    def __init__(self, turns: list[list[dict[str, Any]]]):
        self._turns = turns
        self.config: dict[str, Any] = {}
        self.seen: list[list[dict[str, Any]]] = []

    def get_config(self) -> dict[str, Any]:
        return self.config

    def update_config(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    async def count_tokens(self, *args: Any, **kwargs: Any) -> int:
        return 0

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def stream(self, *args: Any, **kwargs: Any):
        self.seen.append(args[0] if args else kwargs.get("messages"))
        events = self._turns[min(len(self.seen) - 1, len(self._turns) - 1)]
        for event in events:
            yield event


class NeedsApproval(Plugin):
    """Asks a human before ``ping`` — the SDK's documented ``event.interrupt()`` API."""

    name = "needs-approval"

    @hook  # type: ignore[call-overload]
    async def gate(self, event: BeforeToolCallEvent) -> None:
        """Pause the agent for approval of ``ping``."""
        if event.tool_use["name"] == "ping":
            event.interrupt("approve_ping", reason="may I?")


@tool
def ping() -> str:
    """A trivial tool so the agent takes a tool-loop step."""
    return "pong"
