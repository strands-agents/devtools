"""GOALS.md for the critic: loaded from the package, scoped to the skills a request activated."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from strands import Agent

from scripted_model import ScriptedModel, text_events, tool_use_events
from strandly.core.constants import GOALS_MAX_ENGAGED_SKILLS
from strandly.plugins.goal import skill_goals_budget
from strandly.skills.goals import (
    SkillGoals,
    builtin_goals,
    engaged_skill_goals,
    engaged_skills,
)
from strandly.skills.loader import build_skills_plugin, builtin_skills_dir


def test_builtin_goals_has_every_shipped_goals_md():
    shipped = {
        p.parent.name
        for p in builtin_skills_dir().glob("*/GOALS.md")
        if (p.parent / "SKILL.md").is_file() and p.read_text().strip()
    }
    assert set(builtin_goals()) == shipped
    assert "code-review" in shipped


def test_every_goals_md_fits_the_critic_budget_with_the_most_skills_engaged():
    budget = skill_goals_budget(GOALS_MAX_ENGAGED_SKILLS)
    over = {name: len(text) for name, text in builtin_goals().items() if len(text) > budget}
    assert not over, (
        f"GOALS.md over the {budget}-char critic budget at {GOALS_MAX_ENGAGED_SKILLS} engaged "
        f"skills; the critic would not see the tail: {over}"
    )


def _activate(name: str, tool_id: str) -> list:
    return tool_use_events("skills", tool_id, json.dumps({"skill_name": name}))


def _agent(model: ScriptedModel) -> Agent:
    from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment

    sandbox = NotASandboxLocalEnvironment()
    return Agent(
        model=model,
        plugins=[build_skills_plugin(sandbox), SkillGoals()],
        sandbox=sandbox,
        callback_handler=None,
    )


@pytest.mark.asyncio
async def test_each_request_engages_only_what_it_activated_including_a_reactivation():
    model = ScriptedModel(
        _activate("code-review", "t1"),
        text_events("reviewed"),
        _activate("code-review", "t2"),  # the same skill again, in a later request
        _activate("no-such-skill", "t3"),
        text_events("reviewed again"),
        text_events("just chatting"),
    )
    agent = _agent(model)

    await agent.invoke_async("review PR 1")
    assert engaged_skills(agent) == ["code-review"]
    assert engaged_skill_goals(agent) == {"code-review": builtin_goals()["code-review"]}

    await agent.invoke_async("review PR 2")
    assert engaged_skills(agent) == ["code-review"]  # the harness list did not change; still engaged

    await agent.invoke_async("thanks")
    assert engaged_skills(agent) == []
    assert engaged_skill_goals(agent) == {}


def test_a_resume_within_the_same_request_keeps_what_earlier_attempts_engaged():
    state = {"agent_skills": {"activated_skills": []}}

    class _Actor:
        pass

    agent = _Actor()
    agent.state = SimpleNamespace(get=state.get)  # type: ignore[attr-defined]
    plugin = SkillGoals(goals={"triage": "GATE"})
    request = object()

    plugin._on_invocation(SimpleNamespace(agent=agent, invocation_context=request))
    state["agent_skills"] = {"activated_skills": ["triage"]}
    # A goal-loop retry: a new invocation of the same request.
    plugin._on_invocation(SimpleNamespace(agent=agent, invocation_context=request))
    assert engaged_skill_goals(agent) == {"triage": "GATE"}

    # A new request starts from what is activated now.
    plugin._on_invocation(SimpleNamespace(agent=agent, invocation_context=object()))
    assert engaged_skill_goals(agent) == {}


def test_no_request_means_no_goals():
    assert engaged_skill_goals(SimpleNamespace()) == {}
