"""Todo list: a tool + a re-surfacing hook in one plugin.

The AgentZ "tasks" pattern is two cooperating pieces sharing ``agent.state["todos"]``:
  1. a ``todo`` tool that writes/reads the list (storage);
  2. a hook that re-surfaces the list to the model as a ``<system-reminder>`` on later turns, so
     the plan stays in front of the model without it having to re-list.

Both live in one ``TodoPlugin`` because the SDK ``Plugin`` base auto-discovers ``@tool`` and
``@hook`` methods, and they must share the same state key.
"""

from __future__ import annotations

from typing import Any

from strands import tool
from strands.hooks import BeforeInvocationEvent
from strands.plugins import Plugin, hook

TODO_STATE_KEY = "todos"
VALID_TODO_STATUS = ("pending", "in_progress", "completed")
_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def render_todos(items: list[dict]) -> str:
    if not items:
        return "(no todos)"
    return "\n".join(f"{_MARK.get(i.get('status', 'pending'), '[ ]')} {i.get('content', '')}" for i in items)


class TodoPlugin(Plugin):
    """Owns the ``todo`` tool and re-surfaces the list as a ``<system-reminder>`` each turn."""

    name = "meta-harness-todo"

    @tool(context=True, name="todo")
    def todo(self, action: str, items: list[dict] | None = None, *, tool_context: Any = None) -> str:
        """Track a structured task list across the turn (persisted in agent state).

        Use this to plan multi-step work. The current list is automatically re-surfaced to you as
        a system reminder on later turns, so you do not need to re-list it to stay oriented.

        Args:
            action: "write" to replace the whole list, or "list" to read the current list.
            items: For "write": a list of {content, status} dicts where status is one of
                pending | in_progress | completed.
        """
        agent = getattr(tool_context, "agent", None)
        if agent is None:  # pragma: no cover - context always injected at runtime
            return "Error: todo requires agent context."
        state = agent.state
        if action == "list":
            return render_todos(state.get(TODO_STATE_KEY) or [])
        if action == "write":
            cleaned = []
            for it in items or []:
                status = it.get("status", "pending")
                if status not in VALID_TODO_STATUS:
                    return f"Error: invalid status {status!r} (use {list(VALID_TODO_STATUS)})"
                cleaned.append({"content": it.get("content", ""), "status": status})
            state.set(TODO_STATE_KEY, cleaned)
            return render_todos(cleaned)
        return f"Error: unknown action {action!r} (use 'write' or 'list')"

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def resurface(self, event: BeforeInvocationEvent) -> None:
        """Append the current todo list to the latest user message as a system reminder."""
        agent = event.agent
        items = agent.state.get(TODO_STATE_KEY) or []
        if not items:
            return
        messages = event.messages
        if not messages:
            return
        # Find the latest user message and append a reminder text block to it.
        for message in reversed(messages):
            if message.get("role") == "user":
                reminder = (
                    "<system-reminder>\nYour current todo list (keep it updated with the todo "
                    f"tool):\n{render_todos(items)}\n</system-reminder>"
                )
                message.setdefault("content", []).append({"text": reminder})
                return
