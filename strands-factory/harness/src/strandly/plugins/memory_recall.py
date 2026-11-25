"""``MemoryRecall``: long-term memories relevant to the user's ask, as an injected block.

The ``MemoryManager`` is built with ``injection=False`` and this plugin recalls for it through the
SDK's ``ContextInjector`` with ``trigger="userTurn"``: one ``injected`` block on the ask, kept in
history and read by the model only while that ask is the latest user message. Every later request
replays the ask exactly as the model first saw it, which a signed thinking block in the following
assistant turn requires: the provider rejects it once the history before it changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from strands.memory import MemorySearchOptions
from strands.plugins import Plugin
from strands.vended_plugins.context_injector import ContextInjector

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.memory import MemoryEntry, MemoryManager
    from strands.vended_plugins.context_injector import InjectionContext

# The SDK's default number of memories injected per ask.
RECALL_MAX_ENTRIES = 5
_ATTR = {'"': "&quot;", "'": "&#39;"}


class MemoryRecall(Plugin):
    """Adds the memories that match the user's ask to that ask as an injected block."""

    name = "memory-recall"

    def __init__(self, memory: MemoryManager, *, max_entries: int = RECALL_MAX_ENTRIES) -> None:
        """Initialize the plugin.

        Args:
            memory: The agent's memory manager, built with ``injection=False``.
            max_entries: The most memories recalled per ask, across all stores.
        """
        self._memory = memory
        self._max_entries = max_entries
        # The injector writes at most one block per message under its name, so a retried model
        # call neither searches nor writes again. A search that raises is skipped with a warning.
        self._injector = ContextInjector(self._render, name=f"{self.name}:injector", trigger="userTurn")
        super().__init__()

    def init_agent(self, agent: Agent) -> None:
        """Register the composed context injector's middleware on this agent."""
        self._injector.init_agent(agent)

    async def _render(self, context: InjectionContext) -> str | None:
        """The ``<memory>`` block for the latest user message, or ``None`` when nothing matches."""
        ask = next((m for m in reversed(context.messages) if m["role"] == "user"), None)
        query = "\n".join(b["text"] for b in ask["content"] if "text" in b).strip() if ask else ""
        if not query:
            return None
        options = MemorySearchOptions(max_search_results=self._max_entries)
        entries = (await self._memory.search(query, options))[: self._max_entries]
        return render_memories(entries) if entries else None


def render_memories(entries: list[MemoryEntry]) -> str:
    """The SDK's ``<memory>`` format: one escaped ``<entry>`` per memory, naming its store."""
    lines = [
        f'<entry source="{escape(entry.store_name, _ATTR)}">{escape(entry.content)}</entry>'
        if entry.store_name
        else f"<entry>{escape(entry.content)}</entry>"
        for entry in entries
    ]
    return "<memory>\n" + "\n".join(lines) + "\n</memory>"
