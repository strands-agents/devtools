"""Critic-facing ``GOALS.md`` for the built-in skills, and which skills a request engaged.

A skill directory may ship a ``GOALS.md`` beside its ``SKILL.md``: acceptance criteria for the
goal-loop critic, never put in the actor's prompt. :func:`builtin_goals` reads them from the
package once per process. :class:`SkillGoals` records the skills activated during a request — the
names that appear in the ``AgentSkills`` activation state after the request's first invocation
began, plus any re-activated by a ``skills`` call (the harness's list keeps one entry per name) —
and :func:`engaged_skill_goals` returns their goals. A goal-loop retry resumes the same request, so it
keeps the skills engaged by the attempts before it.
"""

from __future__ import annotations

import functools
import logging
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from strands.hooks import AfterToolCallEvent, BeforeInvocationEvent
from strands.plugins import Plugin, hook

from strandly.core.constants import SKILLS_TOOL_NAME
from strandly.skills.loader import builtin_skills_dir

if TYPE_CHECKING:
    from strands import Agent

logger = logging.getLogger(__name__)

# The harness's AgentSkills plugin keeps its activation list under this ``agent.state`` key.
_AGENT_SKILLS_STATE_KEY = "agent_skills"


def builtin_goals(root: Path | None = None) -> dict[str, str]:
    """Normalize the selected root before caching, including environment-selected packs."""
    return _goals_for_root((root or builtin_skills_dir()).resolve())


@functools.cache
def _goals_for_root(root: Path) -> dict[str, str]:
    goals: dict[str, str] = {}
    for goals_file in sorted(root.glob("*/GOALS.md")):
        if not (goals_file.parent / "SKILL.md").is_file():
            continue
        text = goals_file.read_text(encoding="utf-8").strip()
        if text:
            goals[goals_file.parent.name] = text
    return goals


def activated_skills(agent: Any) -> list[str]:
    """The skill names the harness's ``AgentSkills`` has recorded as activated on ``agent``."""
    try:
        data = agent.state.get(_AGENT_SKILLS_STATE_KEY)
    except Exception:  # noqa: BLE001 — no state, no activations
        return []
    if not isinstance(data, dict):
        return []
    names = data.get("activated_skills") or []
    return [n for n in names if isinstance(n, str)]


@dataclass
class _Request:
    context: Any
    started_with: list[str]
    reactivated: list[str] = field(default_factory=list)


class SkillGoals(Plugin):
    """Tracks the skills each request engages, for :func:`engaged_skill_goals`."""

    name = "strandly_skill_goals"

    def __init__(self, goals: dict[str, str] | None = None) -> None:
        self.goals = builtin_goals() if goals is None else goals
        super().__init__()

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def _on_invocation(self, event: BeforeInvocationEvent) -> None:
        current = _lookup(_REQUESTS, event.agent)
        context = event.invocation_context
        if current is not None and context is not None and current.context is context:
            return  # a resume within the same request (goal-loop retry, background delivery)
        _REQUESTS[event.agent] = _Request(context=context, started_with=activated_skills(event.agent))
        _PLUGINS[event.agent] = self

    @hook  # type: ignore[call-overload]  # SDK hook() overloads don't model bound (self, event) methods
    def _on_tool(self, event: AfterToolCallEvent) -> None:
        if event.tool_use.get("name") != SKILLS_TOOL_NAME:
            return
        tool_input = event.tool_use.get("input")
        name = tool_input.get("skill_name") if isinstance(tool_input, dict) else None
        request = _lookup(_REQUESTS, event.agent)
        if request is None or not isinstance(name, str) or name not in activated_skills(event.agent):
            return
        if name not in request.reactivated:
            request.reactivated.append(name)


_REQUESTS: weakref.WeakKeyDictionary[Any, _Request] = weakref.WeakKeyDictionary()
_PLUGINS: weakref.WeakKeyDictionary[Any, SkillGoals] = weakref.WeakKeyDictionary()


def _lookup(table: weakref.WeakKeyDictionary[Any, Any], agent: Any) -> Any:
    try:
        return table.get(agent)
    except TypeError:  # not weak-referenceable: never registered
        return None


def engaged_skills(agent: Agent) -> list[str]:
    """Skills activated during ``agent``'s current request, in activation order."""
    request = _lookup(_REQUESTS, agent)
    if request is None:
        return []
    names = [n for n in activated_skills(agent) if n not in request.started_with]
    names.extend(n for n in request.reactivated if n not in names)
    return names


def engaged_skill_goals(agent: Agent) -> dict[str, str]:
    """``{skill_name: GOALS.md text}`` for the skills engaged in ``agent``'s current request."""
    plugin = _lookup(_PLUGINS, agent)
    goals = plugin.goals if plugin is not None else builtin_goals()
    return {name: goals[name] for name in engaged_skills(agent) if name in goals}
