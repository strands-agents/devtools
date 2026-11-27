"""Goal loop: a tool-using critic over the SDK ``GoalLoop``.

After the actor finishes, a critic judges the work against the goal and, if it is not met, resumes
the actor with feedback, up to ``max_attempts``. The SDK's judge has no tools, so ``GoalLoop`` gets a
validator that builds a critic with the actor's tools and sandbox (so claims are checked, not
trusted), the actor's system prompt (head-truncated) and the transcript. Its verdict is BYPASS
(questions and research, whose answer is the deliverable), PASS or RETRY, and the ``GOALS.md`` of
every skill used this request becomes its acceptance criteria.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from strandly.core.constants import (
    CRITIC_SYSTEM_PROMPT_BUDGET,
    GOAL_DEFAULT,
    GOAL_MAX_ATTEMPTS,
)
from strandly.core.events import REASON_RESUME, RESUME_MARK_KEY

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.types.content import Message

logger = logging.getLogger(__name__)


# Whether an invocation boundary may be resumed or sent a new prompt; serving shares these. Two
# boundaries look finished and are not: a cancelled turn (the signal clears only when
# ``stream_async`` returns, after any resume inside the same call) and a turn paused on an
# interrupt (``event.resume`` feeds ``_interrupt_state.resume``, which raises on text).


def is_parked_on_an_interrupt(agent: Any) -> bool:
    """True when ``agent`` is holding an **activated** interrupt state — it is waiting for a human.

    A parked agent cannot accept a text prompt at all, from anybody. ``Agent.stream_async`` feeds
    whatever it is given to ``_interrupt_state.resume(...)``, which accepts only a list of
    ``interruptResponse`` blocks — so a plain string raises ``TypeError: prompt_type=<class 'str'>``
    and a ``Messages`` list (which ``serve/turn.py`` builds whenever history ends on an unanswered
    ``toolUse``, i.e. *always* at an interrupt boundary) raises
    ``TypeError: content_types=<['role', 'content']>``. Defensive ``getattr`` so an SDK rename
    degrades to ``False``.
    """
    return bool(getattr(getattr(agent, "_interrupt_state", None), "activated", False))


def has_a_pending_cancel(agent: Any) -> bool:
    """True when the agent's cancel signal is set and no invocation has consumed it yet.

    For a caller that has no ``AgentResult`` to read a ``stop_reason`` off — the goal loop's
    validator gets ``(response, agent)`` and nothing else. The SDK clears this only when
    ``stream_async`` returns (``agent.py``'s ``finally``), i.e. *after* every ``AfterInvocationEvent``
    boundary inside that call, so at a boundary it still says "this turn was killed". A per-run
    ``cancel_signal`` passed to ``stream_async`` is mirrored onto this same signal by a poller, so the
    per-run signal is read too. Defensive ``getattr`` so an SDK rename degrades to ``False``.
    """
    for name in ("_cancel_signal", "_external_cancel_signal"):
        is_set = getattr(getattr(agent, name, None), "is_set", None)
        if callable(is_set) and is_set():
            return True
    return False


def has_background_work_outstanding(agent: Any) -> bool:
    """True when a background task is still running or its result is not in the conversation yet.

    The SDK's background-tasks plugin keeps a task until its result is appended to history, so at
    an ``AfterInvocationEvent`` boundary a tracked task means the conversation will continue (or,
    if the run was cancelled, that the work was abandoned). Defensive ``getattr`` so an SDK rename
    degrades to ``False``.
    """
    snapshots = getattr(getattr(agent, "_background_tasks", None), "_task_snapshots", None)
    if not callable(snapshots):
        return False
    try:
        return bool(snapshots())
    except Exception:  # noqa: BLE001 — a predicate; an unreadable state is "nothing outstanding"
        return False


class CriticEvaluation(BaseModel):
    """Structured verdict the critic returns via structured output."""

    verdict: Literal["BYPASS", "PASS", "RETRY"] = Field(
        description=(
            "BYPASS if the task is not verifiable (a question, research, an opinion/proposal, a "
            "conversation) and the actor produced a substantive response. PASS if the actor "
            "completed a verifiable task correctly. RETRY if there are concrete, objective gaps."
        )
    )
    reason: str = Field(description="One-line reason for the verdict.")
    feedback: str | None = Field(
        default=None,
        description="Specific, actionable feedback for the actor. Required for RETRY; null otherwise.",
    )


CRITIC_SYSTEM_PROMPT = """\
You are a Critic evaluating an Actor agent's work against a stated goal. You are strict, impartial,
and concrete. You decide whether the actor satisfied the goal — nothing more.

You receive:
- The **goal**.
- The actor's **system prompt** — its operating contract and the `<available_skills>` listing.
  A skill the actor loaded shows in the transcript as a `skills` tool call; its result (the skill's
  instructions) is cut to a short preview there. The actor was expected to follow that procedure.
- The **transcript** of the actor's turn: its tool calls, results, and final response.
- Possibly an **"## Active skill goals"** section: explicit GOALS.md acceptance criteria for the
  skills the actor activated this request. Treat each as a first-class requirement to verify with tools.

You have the **actor's own tools** and share its sandbox. Use them.

## 1. BYPASS non-verifiable asks (check FIRST)
If the goal is conversational, a question, an opinion/recommendation, a research summary, or a
proposal — anything whose answer IS the deliverable, with no external state to check — and the
actor produced a substantive, on-topic response, return BYPASS immediately. Do not manufacture
gaps. (But if such a task produced an EMPTY/errored response, that's a RETRY — the reply that was
the deliverable never came.)

## 2. Verify claims with tools — do not trust the transcript
For a verifiable goal, enumerate every concrete ask, map each to the actor's claim, then CHECK it:
- File edited? `read` it. Tests pass? Re-run them and read the output. Command succeeded?
  Re-run or inspect. Artifact/PR/URL exists? Query it (`shell`/`use_github`).
- A confident or apologetic claim with no verifiable evidence is an unmet requirement, not a pass.
- If a skill was active, confirm the actor followed its procedure, not just the surface ask.

## Verdict
Return ONLY the CriticEvaluation structured output.
- RETRY only for concrete, objective failures (not style nits). If genuinely torn between PASS and
  RETRY, PASS — don't create busywork.
- For RETRY, `feedback` must name the specific unmet requirement and the concrete fix, actionable
  enough to correct in one more attempt.
- For BYPASS/PASS, `feedback` is null.
"""

_RETRY_TEMPLATE = (
    "A critic reviewed your work against the goal and found it not yet met.\n\n"
    "Feedback:\n{feedback}\n\n"
    "Address every point above and produce a corrected result that fully satisfies the goal. Do "
    "not restate or lightly edit the previous attempt — fix the specific problems called out.\n\n"
    "If this attempt already posted a GitHub comment/review that the correction supersedes, "
    "**edit that same comment** (`updateIssueComment` with its id, or the equivalent review-comment "
    "edit) rather than posting a second one — this attempt hasn't ended yet, so the first post was "
    "never a finished, standalone reply. This does NOT apply outside a retry: a fresh dispatch "
    "(a new mention, a follow-up question, a later turn) always gets a new reply — never silently "
    "edit a comment from a prior, already-finished turn."
)


def _truncate(text: str, max_chars: int) -> str:
    """Trim a long string with a visible marker so one section can't dominate the critic prompt."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 50] + f"\n\n… [truncated {len(text) - (max_chars - 50)} chars]"


def _system_prompt_text(agent: Agent) -> str:
    """The actor's system prompt as plain text (handles str or structured content-block form).

    Carries the actor's contract and the ``<available_skills>`` listing (names and descriptions
    only); a loaded skill's instructions are not in it.
    """
    sp = agent.system_prompt
    if isinstance(sp, str):
        return sp
    if isinstance(sp, list):
        return "\n\n".join(b["text"] for b in sp if isinstance(b, dict) and "text" in b)
    return ""


def skill_goals_budget(active: int) -> int:
    """Characters of each GOALS.md the critic sees when ``active`` skills with goals are engaged."""
    return max(1000, CRITIC_SYSTEM_PROMPT_BUDGET // max(1, active))


def _active_skill_goals_section(agent: Agent) -> str:
    """Render a critic-facing "## Active skill goals" section from the engaged skills' GOALS.md.

    The skills are those activated during this request (``skills.goals.engaged_skill_goals``).
    Returns an empty string when none of them ships goals (prompt unchanged then).
    """
    try:
        from strandly.skills.goals import engaged_skill_goals

        goals = engaged_skill_goals(agent)
    except Exception:  # noqa: BLE001 — goals are an enhancement; never break the critic over them
        return ""
    if not goals:
        return ""
    blocks = [
        "## Active skill goals (explicit acceptance criteria you MUST verify)",
        "Each active skill below shipped a GOALS.md naming exactly what to check. These are "
        "first-class requirements for this turn: a goal left unmet is grounds for RETRY (unless the "
        "whole task is non-verifiable per rule 1).",
    ]
    budget = skill_goals_budget(len(goals))
    for name, text in goals.items():
        if len(text) > budget:
            logger.warning(
                "goal loop: truncating %s GOALS.md (%d chars) to the %d-char budget of %d skills",
                name,
                len(text),
                budget,
                len(goals),
            )
        blocks.append(f"### Goals for active skill `{name}`\n{_truncate(text, budget)}")
    return "\n\n".join(blocks)


def _build_critic_prompt(goal: str, agent: Agent) -> str:
    """Assemble the critic's input: goal + actor's contract/skills + active skill goals + transcript."""
    from strands.vended_plugins.goal.judge import build_judge_prompt

    # build_judge_prompt renders the full transcript (tool calls + results, truncated) the same way
    # the SDK judge sees it — reuse it so the trajectory format stays consistent with the SDK.
    transcript = build_judge_prompt(goal, agent.messages)
    contract = _truncate(_system_prompt_text(agent), CRITIC_SYSTEM_PROMPT_BUDGET)
    parts = [
        transcript,
        "## Actor's system prompt (its contract and the available-skills listing)\n"
        f"{contract}",
    ]
    goals_section = _active_skill_goals_section(agent)
    if goals_section:
        parts.append(goals_section)
    return "\n\n".join(parts)


def _make_critic_validator(goal: str) -> Any:
    """Return a GoalLoop validator callable that runs a tool-wielding, skill-aware critic.

    Signature is GoalLoop's: ``(response, agent) -> ValidatorReturn``. We ignore ``response`` (the
    last assistant message) and evaluate the whole transcript, like the SDK judge does.
    """
    from strands.vended_plugins.goal import ValidationOutcome

    async def validate(_response: Message, agent: Agent, **_: Any) -> ValidationOutcome:
        from strands import Agent as _Agent

        # A BOUNDARY THAT IS NOT A FINISHED TURN HAS NOTHING TO GRADE. ``GoalLoop._after_invocation``
        # (the SDK's) has no guards of its own — no ``event.result`` check, no cancelled check, no
        # interrupt check — so it calls this validator at EVERY ``AfterInvocationEvent``, including
        # the ones where the invocation is being torn down, and then writes ``event.resume``.
        # Measured against a real ``Agent``:
        #
        #   * cancelled turn  -> 3 critic invocations and 3 resumed turns, each re-killed instantly
        #                        by the still-set cancel signal (the signal is cleared only when
        #                        ``stream_async`` returns). Reachable in production: a Stop or a
        #                        superseding message sets the run's cancel signal.
        #   * interrupt bdry  -> 1 critic invocation, then ``event.resume`` = retry text, which
        #                        ``_run_loop`` feeds to ``_interrupt_state.resume(...)``:
        #                        ``TypeError: prompt_type=<class 'str'>`` (see the predicates above),
        #                        taking the invocation and the human's pending prompt down with it.
        #
        # Our critic is a full tool-wielding ``Agent`` invocation, so each of those is a real model
        # call spent on a turn whose outcome is already decided. Declining is both the cheap and the
        # correct answer: ``passed=True`` finishes the goal run without setting ``resume``, which is
        # exactly what the existing critic-infra-failure branch below already does ("don't trap the
        # actor — accept and move on"). The predicates are the shared ones every resume writer uses
        # (defined above) so the writers cannot drift apart.
        #
        # NOT covered here, and filed rather than faked: the **unwinding** boundary
        # (``event.result is None`` — a mid-stream drop). It is visible only on the event, which a
        # validator is never given, so it needs a guard inside ``GoalLoop`` itself (upstream).
        if is_parked_on_an_interrupt(agent) or has_a_pending_cancel(agent):
            logger.info("goal loop: the turn did not finish (cancelled or paused to ask); not grading it")
            return ValidationOutcome(passed=True)
        # Background work still outstanding: its results are delivered as a continuation, a new
        # invocation the goal loop grades on its own. Grading now would judge a draft that is
        # still waiting on them.
        if has_background_work_outstanding(agent):
            logger.info("goal loop: background results are still due; grading the turn they continue")
            return ValidationOutcome(passed=True)

        try:
            # Critic: same model + the actor's tools + the actor's sandbox, fresh memory, no
            # plugins (a critic doesn't get its own critic), structured verdict.
            critic = _Agent(
                model=agent.model,
                system_prompt=CRITIC_SYSTEM_PROMPT,
                tools=list(agent.tool_registry.registry.values()),
                sandbox=agent.sandbox,
                callback_handler=None,
                structured_output_model=CriticEvaluation,
            )
            result = await critic.invoke_async(_build_critic_prompt(goal, agent))
        except Exception as e:  # noqa: BLE001 — critic infra failure: don't trap the actor, accept
            logger.warning("critic failed (%s); accepting the actor's work", e, exc_info=True)
            return ValidationOutcome(passed=True)

        verdict = result.structured_output
        if not isinstance(verdict, CriticEvaluation):
            logger.warning("critic produced no structured verdict; accepting")
            return ValidationOutcome(passed=True)

        if verdict.verdict in ("BYPASS", "PASS"):
            logger.info("critic %s — %s", verdict.verdict, verdict.reason)
            return ValidationOutcome(passed=True)
        logger.info("critic RETRY — %s", verdict.reason)
        return ValidationOutcome(passed=False, feedback=verdict.feedback or verdict.reason)

    return validate


def build_goal_loop(
    goal: str | None = None,
    max_attempts: int = GOAL_MAX_ATTEMPTS,
) -> Any:
    """Build the actor-critic ``GoalLoop`` plugin.

    Uses a validator callable (not the toolless NL judge) so the critic gets tools, the sandbox,
    and the actor's system prompt (its contract and the available-skills listing). ``goal``
    defaults to ``constants.GOAL_DEFAULT``. A critic retry stamps ``REASON_RESUME`` on the turn it
    opens.
    """
    from strands.hooks import AfterInvocationEvent
    from strands.vended_plugins.goal import GoalLoop

    retries = {"pending": False, "seq": 0}

    def resume_prompt(feedback: str | None) -> str:
        retries["pending"] = True
        return _RETRY_TEMPLATE.format(feedback=feedback or "(no detail)")

    def label_retry(event: AfterInvocationEvent) -> None:
        if not retries["pending"]:
            return
        retries["pending"] = False
        if event.resume is None:
            return
        retries["seq"] += 1
        event.invocation_state[RESUME_MARK_KEY] = {"seq": retries["seq"], "reason": REASON_RESUME}

    class _LabelledGoalLoop(GoalLoop):
        def init_agent(self, agent: Agent) -> None:
            # Registered before GoalLoop's own hook, so it runs right after it
            # (AfterInvocationEvent callbacks run in reverse registration order).
            agent.add_hook(label_retry, AfterInvocationEvent)
            super().init_agent(agent)

    return _LabelledGoalLoop(
        goal=_make_critic_validator(goal or GOAL_DEFAULT),
        max_attempts=max_attempts,
        resume_prompt_template=resume_prompt,
    )
