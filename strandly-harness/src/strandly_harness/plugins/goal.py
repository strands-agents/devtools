"""Goal loop (actor-critic) — a tool-wielding, context-aware critic over the SDK ``GoalLoop``.

After the agent (the *actor*) finishes, a *critic* evaluates the work against the goal and, if it's
not met, resumes the actor with concrete feedback (bounded by ``max_attempts``).

We keep the SDK ``GoalLoop`` for the loop/resume/attempts/timeout machinery, but we do **not** use
its built-in natural-language judge — that judge is a *toolless* ``Agent`` (just a model +
structured output), so the usual "verify the agent's claims with tools" instruction is inert: it
has no tools to verify with. Instead we pass ``GoalLoop`` a **validator callable** (its supported
escape hatch) and build our own critic inside it, modeled on a proven actor-critic pattern from a
prior harness. The critic is more powerful than the stock judge in three ways:

1. **It has the actor's tools** (``bash``, ``file_editor``, ``use_github``, MCP, ...) and the same
   sandbox, so when the actor *claims* an outcome the critic can check it for real — read the file,
   re-run the command, query the API — rather than trusting the transcript.
2. **It sees the actor's full system prompt**, which includes the harness global prompt *and* the
   ``<active_skills>`` block injected by ``SystemPromptSkills``. So the critic knows which skills
   the actor activated and grades the work against those skills' actual procedures.
3. **A BYPASS/PASS/RETRY verdict.** BYPASS short-circuits non-verifiable asks (questions, research,
   opinions) — their answer *is* the deliverable — so the loop never manufactures busywork on
   "what do you think?".
4. **Explicit per-skill acceptance criteria.** Each active skill may ship a ``GOALS.md`` (sibling
   of its ``SKILL.md``) listing exactly what we want the critic to check when that skill is active.
   ``SystemPromptSkills`` loads these and stashes them in ``agent.state``; the critic pulls the
   *active* ones into an "## Active skill goals" section so we can tell it precisely what to verify
   per skill, beyond the skill's own procedure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from strandly_harness.core.constants import (
    CRITIC_SYSTEM_PROMPT_BUDGET,
    GOAL_DEFAULT,
    GOAL_MAX_ATTEMPTS,
)

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.types.content import Message

logger = logging.getLogger(__name__)


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
- The actor's **system prompt** — its operating contract AND any `<active_skills>` block listing
  the skills it activated, with those skills' full instructions. When a skill was active, the
  actor was expected to FOLLOW that skill's procedure; grade against it.
- The **transcript** of the actor's turn: its tool calls, results, and final response.
- Possibly an **"## Active skill goals"** section: explicit GOALS.md acceptance criteria for the
  skills the actor had active. Treat each as a first-class requirement to verify with tools.

You have the **actor's own tools** and share its sandbox. Use them.

## 1. BYPASS non-verifiable asks (check FIRST)
If the goal is conversational, a question, an opinion/recommendation, a research summary, or a
proposal — anything whose answer IS the deliverable, with no external state to check — and the
actor produced a substantive, on-topic response, return BYPASS immediately. Do not manufacture
gaps. (But if such a task produced an EMPTY/errored response, that's a RETRY — the reply that was
the deliverable never came.)

## 2. Verify claims with tools — do not trust the transcript
For a verifiable goal, enumerate every concrete ask, map each to the actor's claim, then CHECK it:
- File edited? `view`/read it. Tests pass? Re-run them and read the output. Command succeeded?
  Re-run or inspect. Artifact/PR/URL exists? Query it (`bash`/`use_github`).
- A confident or apologetic claim with no verifiable evidence is an unmet requirement, not a pass.
- If a skill was active, confirm the actor followed its procedure, not just the surface ask.

## 3. Reconcile the plan
If the transcript shows a `todo` list, treat unfinished in-scope items as the goal being unmet, and
name them.

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
    "not restate or lightly edit the previous attempt — fix the specific problems called out."
)


def _truncate(text: str, max_chars: int) -> str:
    """Trim a long string with a visible marker so one section can't dominate the critic prompt."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 50] + f"\n\n… [truncated {len(text) - (max_chars - 50)} chars]"


def _system_prompt_text(agent: Agent) -> str:
    """The actor's system prompt as plain text (handles str or structured content-block form).

    This is where the activated skills live — ``SystemPromptSkills`` injects an ``<active_skills>``
    block into the system prompt — so passing it to the critic is how the critic "sees the skills
    that were enabled".
    """
    sp = agent.system_prompt
    if isinstance(sp, str):
        return sp
    if isinstance(sp, list):
        return "\n\n".join(b["text"] for b in sp if isinstance(b, dict) and "text" in b)
    return ""


def _active_skill_goals_section(agent: Agent) -> str:
    """Render a critic-facing "## Active skill goals" section from the active skills' GOALS.md.

    Each skill MAY ship a ``GOALS.md`` (sibling of its ``SKILL.md``) holding explicit,
    critic-facing acceptance criteria — what we specifically want the critic to verify when that
    skill is active. ``SystemPromptSkills`` loads these and stashes them in ``agent.state``; here
    we pull the goals for every skill relevant this turn (those active now PLUS any engaged then
    deactivated mid-turn — see ``active_skill_goals``) and render them as additional must-check
    criteria. Returns an empty string when no relevant skill has goals (prompt unchanged then).
    """
    try:
        from strandly_harness.plugins.system_prompt_skills import active_skill_goals

        goals = active_skill_goals(agent)
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
    for name, text in goals.items():
        budget = max(1000, CRITIC_SYSTEM_PROMPT_BUDGET // max(1, len(goals)))
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
        "## Actor's system prompt (its contract + any active skills it was expected to follow)\n"
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
        except Exception as e:  # critic infra failure: don't trap the actor — accept and move on
            logger.warning("critic failed (%s); accepting the actor's work", e)
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
    and the actor's system prompt (including its active skills). ``goal`` defaults to
    ``constants.GOAL_DEFAULT``.
    """
    from strands.vended_plugins.goal import GoalLoop

    return GoalLoop(
        goal=_make_critic_validator(goal or GOAL_DEFAULT),
        max_attempts=max_attempts,
        resume_prompt_template=lambda fb: _RETRY_TEMPLATE.format(feedback=fb or "(no detail)"),
    )
