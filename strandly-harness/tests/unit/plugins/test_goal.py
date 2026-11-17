"""Tests for the actor-critic goal loop (``plugins/goal.py``).

The critic is what makes our loop more powerful than the SDK's toolless judge: it must be built
with the actor's tools, the actor's sandbox, and the actor's system prompt (which carries the
activated skills). These tests verify that wiring and the BYPASS/PASS/RETRY → pass/fail mapping by
faking the critic ``Agent`` — no model, no network.
"""

from __future__ import annotations

import pytest

from strandly_harness.plugins.goal import (
    CriticEvaluation,
    _active_skill_goals_section,
    _build_critic_prompt,
    _make_critic_validator,
    build_goal_loop,
)


class FakeResult:
    def __init__(self, verdict: CriticEvaluation | None) -> None:
        self.structured_output = verdict


class FakeCritic:
    """Captures how the critic Agent was constructed and returns a canned verdict."""

    last: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeCritic.last = kwargs
        self._verdict = FakeCritic.next_verdict

    async def invoke_async(self, prompt: str):
        FakeCritic.last_prompt = prompt
        return FakeResult(self._verdict)


class FakeHostAgent:
    """Stand-in for the actor: model, tool registry, sandbox, system prompt, transcript."""

    def __init__(self, system_prompt: str) -> None:
        self.model = object()
        self.sandbox = object()

        class _Reg:
            registry = {"bash": object(), "file_editor": object()}

        self.tool_registry = _Reg()
        self.system_prompt = system_prompt
        self.messages = [
            {"role": "user", "content": [{"text": "do the thing"}]},
            {"role": "assistant", "content": [{"text": "done"}]},
        ]


@pytest.fixture
def patch_critic(monkeypatch):
    """Patch ``strands.Agent`` (imported inside the validator) with FakeCritic."""
    import strands

    monkeypatch.setattr(strands, "Agent", FakeCritic)
    return FakeCritic


async def _run(verdict: CriticEvaluation | None, system_prompt: str = "GLOBAL\n<active_skills/>"):
    FakeCritic.next_verdict = verdict
    validate = _make_critic_validator("the goal")
    agent = FakeHostAgent(system_prompt)
    outcome = await validate({"role": "assistant", "content": []}, agent)
    return outcome, agent


@pytest.mark.asyncio
async def test_critic_gets_actor_tools_sandbox_and_skills(patch_critic):
    _, agent = await _run(CriticEvaluation(verdict="PASS", reason="ok"))
    built = FakeCritic.last
    # Same model + sandbox as the actor.
    assert built["model"] is agent.model
    assert built["sandbox"] is agent.sandbox
    # The actor's tools (not an empty toolset like the SDK judge).
    assert set(t for t in agent.tool_registry.registry.values()) == set(built["tools"])
    # Structured verdict + the critic system prompt.
    assert built["structured_output_model"] is CriticEvaluation
    # The actor's system prompt (and thus its active skills) is in the critic's input.
    assert "<active_skills/>" in FakeCritic.last_prompt


@pytest.mark.asyncio
async def test_pass_and_bypass_map_to_passed(patch_critic):
    outcome, _ = await _run(CriticEvaluation(verdict="PASS", reason="verified"))
    assert outcome.passed is True
    outcome, _ = await _run(CriticEvaluation(verdict="BYPASS", reason="just a question"))
    assert outcome.passed is True


@pytest.mark.asyncio
async def test_retry_maps_to_failed_with_feedback(patch_critic):
    outcome, _ = await _run(
        CriticEvaluation(verdict="RETRY", reason="missing tests", feedback="add a test for X")
    )
    assert outcome.passed is False
    assert outcome.feedback == "add a test for X"


@pytest.mark.asyncio
async def test_no_structured_output_accepts(patch_critic):
    # A critic that returns no verdict must not trap the actor in a failing loop.
    outcome, _ = await _run(None)
    assert outcome.passed is True


@pytest.mark.asyncio
async def test_critic_infra_failure_accepts(monkeypatch):
    # If building/invoking the critic throws, accept the actor's work (don't block on infra).
    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("no creds")

    import strands

    monkeypatch.setattr(strands, "Agent", Boom)
    validate = _make_critic_validator("g")
    outcome = await validate({"role": "assistant", "content": []}, FakeHostAgent("sp"))
    assert outcome.passed is True


def test_build_goal_loop_uses_validator_not_string():
    # GoalLoop with a callable goal builds no NL judge — the whole point (the critic has tools).
    loop = build_goal_loop()
    assert loop._goal is None  # str goal would set _goal; we pass a validator
    assert loop._validator is not None


def test_build_goal_loop_respects_max_attempts():
    assert build_goal_loop(max_attempts=5)._max_attempts == 5


# ---- active skill goals feed the critic (GOALS.md) ------------------------------------------


class _State:
    def __init__(self, data):
        self._data = data

    def get(self, key):
        return self._data.get(key)


class _AgentWithGoals:
    """Actor stand-in carrying agent.state with active skills + their GOALS.md."""

    def __init__(self, active, goals):
        self.system_prompt = "GLOBAL\n<active_skills/>"
        self.messages = [
            {"role": "user", "content": [{"text": "review the change"}]},
            {"role": "assistant", "content": [{"text": "done"}]},
        ]
        self.state = _State({"system_prompt_skills": {"active": active, "goals": goals}})


def test_active_skill_goals_section_includes_only_active():
    agent = _AgentWithGoals(
        active=["code-review"],
        goals={"code-review": "VERDICT_REQUIRED", "triage": "GATE_REQUIRED"},
    )
    section = _active_skill_goals_section(agent)
    assert "## Active skill goals" in section
    assert "code-review" in section and "VERDICT_REQUIRED" in section
    assert "triage" not in section and "GATE_REQUIRED" not in section


def test_active_skill_goals_section_empty_when_none_active():
    agent = _AgentWithGoals(active=[], goals={"code-review": "X"})
    assert _active_skill_goals_section(agent) == ""


def test_build_critic_prompt_appends_goals_section():
    agent = _AgentWithGoals(active=["code-review"], goals={"code-review": "MUST_VERIFY_VERDICT"})
    prompt = _build_critic_prompt("the goal", agent)
    # Transcript + contract + the goals section are all present.
    assert "Actor's system prompt" in prompt
    assert "## Active skill goals" in prompt
    assert "MUST_VERIFY_VERDICT" in prompt


def test_active_skill_goals_section_safe_without_state():
    # An agent lacking .state must not crash the critic — section is just empty.
    class _Bare:
        pass

    assert _active_skill_goals_section(_Bare()) == ""

