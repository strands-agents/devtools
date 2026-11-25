"""Tests for the actor-critic goal loop (``plugins/goal.py``).

The critic is what makes our loop more powerful than the SDK's toolless judge: it must be built
with the actor's tools, the actor's sandbox, and the actor's system prompt (which lists the
skills). These tests verify that wiring and the BYPASS/PASS/RETRY → pass/fail mapping by
faking the critic ``Agent`` — no model, no network.
"""

from __future__ import annotations

import threading

import pytest

from strandly.plugins.goal import (
    CriticEvaluation,
    _active_skill_goals_section,
    _build_critic_prompt,
    _make_critic_validator,
    build_goal_loop,
    has_a_pending_cancel,
    has_background_work_outstanding,
    is_parked_on_an_interrupt,
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
            registry = {"shell": object(), "read": object()}

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


async def _run(verdict: CriticEvaluation | None, system_prompt: str = "GLOBAL\n<available_skills/>"):
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
    # The actor's system prompt (and thus its skills listing) is in the critic's input.
    assert "<available_skills/>" in FakeCritic.last_prompt


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
    """Actor stand-in: ``agent.state`` in the AgentSkills shape, a request begun by SkillGoals."""

    def __init__(self, activated_now, goals, activated_before=()):
        from types import SimpleNamespace

        from strandly.skills.goals import SkillGoals

        self.system_prompt = "GLOBAL\n<available_skills/>"
        self.messages = [
            {"role": "user", "content": [{"text": "review the change"}]},
            {"role": "assistant", "content": [{"text": "done"}]},
        ]
        self.state = _State({"agent_skills": {"activated_skills": list(activated_before)}})
        SkillGoals(goals=goals)._on_invocation(
            SimpleNamespace(agent=self, invocation_context=object())
        )
        self.state = _State({"agent_skills": {"activated_skills": list(activated_now)}})


def test_active_skill_goals_section_includes_only_engaged():
    agent = _AgentWithGoals(
        activated_now=["triage", "code-review"],
        activated_before=["triage"],
        goals={"code-review": "VERDICT_REQUIRED", "triage": "GATE_REQUIRED"},
    )
    section = _active_skill_goals_section(agent)
    assert "## Active skill goals" in section
    assert "code-review" in section and "VERDICT_REQUIRED" in section
    assert "triage" not in section and "GATE_REQUIRED" not in section


def test_active_skill_goals_section_empty_when_none_engaged():
    agent = _AgentWithGoals(activated_now=[], goals={"code-review": "X"})
    assert _active_skill_goals_section(agent) == ""


def test_build_critic_prompt_appends_goals_section():
    agent = _AgentWithGoals(activated_now=["code-review"], goals={"code-review": "MUST_VERIFY_VERDICT"})
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



# ---- a boundary that is not a finished turn has nothing to grade ------------------------------


class _CountingCritic(FakeCritic):
    """Records whether the critic was ever CONSTRUCTED (i.e. a model call was about to happen)."""

    built = 0

    def __init__(self, **kwargs) -> None:
        _CountingCritic.built += 1
        super().__init__(**kwargs)


@pytest.fixture
def count_critic(monkeypatch):
    import strands

    _CountingCritic.built = 0
    FakeCritic.next_verdict = CriticEvaluation(verdict="RETRY", reason="nope", feedback="fix it")
    monkeypatch.setattr(strands, "Agent", _CountingCritic)
    return _CountingCritic


@pytest.mark.asyncio
async def test_the_critic_does_not_grade_a_turn_that_paused_to_ask_a_human(count_critic):
    """``GoalLoop`` is the FIRST of the three resume writers and has no guards of its own.

    ``GoalLoop._after_invocation`` (the SDK's) checks neither ``event.result``, nor cancellation, nor
    the interrupt state — it calls this validator at every boundary and then writes ``event.resume``.
    At an interrupt boundary that resume is fed to ``_interrupt_state.resume(...)``, which raises the
    same ``TypeError`` the two harness plugins guard against — measured against a real ``Agent``: one
    critic invocation burned, then the invocation and the human's pending prompt destroyed. Since the
    validator is ours, declining here is the cheap fix: no model call, and ``passed`` sets no resume.
    """
    validate = _make_critic_validator("the goal")
    agent = FakeHostAgent("GLOBAL")
    agent._interrupt_state = type("_S", (), {"activated": True})()

    outcome = await validate({"role": "assistant", "content": []}, agent)

    assert outcome.passed is True  # => GoalLoop finishes the run and sets NO resume
    assert outcome.feedback is None
    assert count_critic.built == 0, "a critic model call was spent on a turn that had not finished"


@pytest.mark.asyncio
async def test_the_critic_does_not_grade_a_cancelled_turn(count_critic):
    """A cancelled turn measured 3 critic invocations and 3 instantly re-killed resumed turns.

    The cancel signal is cleared only when ``stream_async`` returns — after every boundary inside
    that call — so each resume burns a model call and dies. Reachable in production: a Stop or a
    superseding message sets the run's cancel signal.
    """
    import threading

    validate = _make_critic_validator("the goal")
    agent = FakeHostAgent("GLOBAL")
    agent._cancel_signal = threading.Event()
    agent._cancel_signal.set()

    outcome = await validate({"role": "assistant", "content": []}, agent)

    assert outcome.passed is True
    assert count_critic.built == 0, "a critic model call was spent on a cancelled turn"


@pytest.mark.asyncio
async def test_the_critic_still_grades_an_ordinary_finished_turn(count_critic):
    """The guard must be narrow: a normal boundary is still graded, and RETRY still retries."""
    import threading

    validate = _make_critic_validator("the goal")
    agent = FakeHostAgent("GLOBAL")
    agent._interrupt_state = type("_S", (), {"activated": False})()
    agent._cancel_signal = threading.Event()  # armed=False

    outcome = await validate({"role": "assistant", "content": []}, agent)

    assert count_critic.built == 1
    assert outcome.passed is False
    assert outcome.feedback == "fix it"


@pytest.mark.asyncio
async def test_end_to_end_the_goal_loop_no_longer_destroys_an_interrupt(monkeypatch):
    """The whole thing against a real ``Agent``: our loop, the SDK's ``GoalLoop``, a real interrupt.

    Before the guard, driving a real ``Agent`` (``build_goal_loop()`` + a hook calling
    ``event.interrupt()``) constructed one critic and then raised
    ``TypeError: prompt_type=<class 'str'> | must resume from interrupt with list of
    interruptResponse's`` out of ``Agent._run_loop`` — the same ``TypeError`` the two plugins already
    guard against, arriving from the writer that runs FIRST, and taking the human's pending approval
    with it. After: no critic, no raise, the interrupt intact and answerable.
    """
    import strands
    from strands import Agent as RealAgent

    from harness.plugins._interrupt_fixtures import (
        NeedsApproval,
        ScriptedModel,
        _tool_use_events,
        ping,
        text_response_events,
    )

    built: list[dict] = []

    class SpyCritic:
        """A critic that would return RETRY — the verdict that makes GoalLoop write a resume."""

        def __init__(self, **kwargs) -> None:
            built.append(kwargs)

        async def invoke_async(self, prompt: str):
            return FakeResult(
                CriticEvaluation(verdict="RETRY", reason="not done", feedback="finish the review")
            )

    monkeypatch.setattr(strands, "Agent", SpyCritic)
    model = ScriptedModel([_tool_use_events("ping"), text_response_events("ok")])
    agent = RealAgent(
        model=model, tools=[ping], plugins=[build_goal_loop(), NeedsApproval()],
        callback_handler=None,
    )

    result = await agent.invoke_async("review the PR")  # pre-fix: raises TypeError

    assert result.stop_reason == "interrupt"
    assert result.interrupts, "the human's pending prompt was destroyed"
    assert built == [], "a critic model call was spent on an invocation that had not finished"


@pytest.mark.asyncio
async def test_a_critic_retry_names_its_turn_resume(monkeypatch):
    """The goal loop labels the turn its critic retry opens; the next boundary it does not open is
    left unnamed."""
    import strands
    from strands import Agent as RealAgent

    from harness.plugins._interrupt_fixtures import ScriptedModel, text_response_events
    from strandly.core.events import REASON_RESUME, REASON_START, translate

    verdicts = iter(["RETRY", "PASS"])

    class OneRetryCritic:
        def __init__(self, **kwargs) -> None:
            pass

        async def invoke_async(self, prompt: str):
            return FakeResult(CriticEvaluation(verdict=next(verdicts), reason="r", feedback="more"))

    monkeypatch.setattr(strands, "Agent", OneRetryCritic)
    model = ScriptedModel([text_response_events("draft"), text_response_events("final")])
    agent = RealAgent(model=model, plugins=[build_goal_loop()], callback_handler=None)

    reasons = [
        ev.data["reason"]
        async for ev in translate(agent.stream_async("do it", invocation_state={}))
        if ev.kind == "turn_start"
    ]

    assert reasons == [REASON_START, REASON_RESUME]


@pytest.mark.asyncio
async def test_the_critic_grades_only_after_background_results_are_folded_in(monkeypatch):
    """A turn that ends while a background task is still running is not finished: the SDK delivers
    the result and continues. The critic grades the turn that continuation produces, once."""
    import asyncio
    import json

    import strands
    from strands import Agent as RealAgent
    from strands import tool

    from scripted_model import ScriptedModel, text_events, tool_use_events

    graded: list[list[str]] = []

    class RecordingCritic:
        def __init__(self, **kwargs) -> None:
            pass

        async def invoke_async(self, prompt: str):
            graded.append([json.dumps(m["content"]) for m in host.messages])
            return FakeResult(CriticEvaluation(verdict="RETRY", reason="r", feedback="more"))

    @tool
    async def slow_check() -> str:
        """A slow check."""
        await asyncio.sleep(0.3)
        return "CHECK-RESULT"

    monkeypatch.setattr(strands, "Agent", RecordingCritic)
    model = ScriptedModel(
        tool_use_events("slow_check", "b1", json.dumps({"_background_execution": True})),
        text_events("dispatched the check"),
        text_events("the check says CHECK-RESULT"),
    )
    host = RealAgent(
        model=model,
        tools=[slow_check],
        plugins=[build_goal_loop(max_attempts=1)],
        background_tasks={"agentic": ["slow_check"]},
        callback_handler=None,
    )

    async for _ in host.stream_async("run the check", invocation_state={}):
        pass

    assert len(graded) == 1
    assert any("CHECK-RESULT" in block for block in graded[0])


class _Agent:
    def __init__(self, activated: bool = False) -> None:
        self._interrupt_state = type("_S", (), {"activated": activated})()


@pytest.mark.parametrize("activated", [True, False])
def test_is_parked_on_an_interrupt_reads_the_activated_flag(activated):
    assert is_parked_on_an_interrupt(_Agent(activated=activated)) is activated


def test_the_agent_predicates_degrade_rather_than_raise():
    """An SDK rename must not crash the end of a run (both are called in a ``finally``)."""

    class _Bare:
        pass

    for candidate in (_Bare(), None, object()):
        assert is_parked_on_an_interrupt(candidate) is False
        assert has_a_pending_cancel(candidate) is False
        assert has_background_work_outstanding(candidate) is False


@pytest.mark.parametrize(
    ("internal", "external", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_has_a_pending_cancel_reads_the_agent_and_the_per_run_signal(internal, external, expected):
    """``threading.Event``-shaped, like the SDK's ``_cancel_signal`` and the per-run
    ``_external_cancel_signal`` it mirrors on a poll."""

    class _Cancellable:
        def __init__(self) -> None:
            self._cancel_signal = threading.Event()
            self._external_cancel_signal = threading.Event()

    agent = _Cancellable()
    if internal:
        agent._cancel_signal.set()
    if external:
        agent._external_cancel_signal.set()
    assert has_a_pending_cancel(agent) is expected


class _BackgroundTasks:
    def __init__(self, tasks: list | Exception) -> None:
        self._tasks = tasks

    def _task_snapshots(self) -> list:
        if isinstance(self._tasks, Exception):
            raise self._tasks
        return list(self._tasks)


@pytest.mark.parametrize(
    ("tasks", "expected"),
    [
        ([{"task_id": "a", "status": "working"}], True),
        ([{"task_id": "a", "status": "completed"}], True),  # settled, not yet in the conversation
        ([], False),
        (RuntimeError("boom"), False),
    ],
)
def test_has_background_work_outstanding_reads_the_tracked_tasks(tasks, expected):
    agent = type("_A", (), {"_background_tasks": _BackgroundTasks(tasks)})()
    assert has_background_work_outstanding(agent) is expected


def test_an_over_budget_goals_md_is_truncated_with_a_warning(caplog):
    agent = _AgentWithGoals(activated_now=["code-review"], goals={"code-review": "x" * 9_000})
    with caplog.at_level("WARNING", logger="strandly.plugins.goal"):
        section = _active_skill_goals_section(agent)
    assert "[truncated" in section
    assert "truncating code-review GOALS.md (9000 chars)" in caplog.text
