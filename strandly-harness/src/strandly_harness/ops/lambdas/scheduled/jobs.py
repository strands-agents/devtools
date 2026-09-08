"""The scheduled-job registry — the single source of truth for what the agent does on a timer.

**Dependency-free on purpose.** This module imports only the stdlib so it can be loaded from two
very different places without dragging in the Strands SDK:

- the **Lambda invoker** (:mod:`strandly_harness.ops.lambdas.scheduled.invoker`), which looks a job up by name
  and runs its prompt; and
- the **CDK app** (``infra/stacks/scheduler_stack.py``), which runs in a separate venv that can't
  import the harness — it loads this file to create one EventBridge schedule per job. It reads only
  ``name`` + ``schedule`` (the parts CloudFormation needs); the prompt/skill never leave the harness.

To add or change a scheduled job, edit ``JOBS`` here (and redeploy the scheduler stack). The
``schedule`` is an EventBridge Scheduler expression — ``rate(...)`` or ``cron(...)`` — and
``session_id`` is deterministic per job so each job threads its own AgentCore Memory conversation
across runs (a daily review can see what it said yesterday).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    """One time-triggered self-invocation.

    Attributes:
        name: Stable identifier — names the EventBridge schedule, is the key the schedule passes to
            the invoker, and (with a date suffix) seeds the session id. Lowercase-kebab.
        schedule: An EventBridge Scheduler expression, e.g. ``rate(1 day)`` or
            ``cron(0 9 ? * MON *)``. Read by the CDK app to create the schedule.
        prompt: The instruction handed to the deployed agent. The agent does the actual work; this
            just frames the task.
        skill: Optional skill to activate before the prompt runs (prepended as a
            ``skill(action="activate", name=...)`` line). ``None`` to run with no skill.
        session_prefix: Session-id stem; the invoker appends the run date so each day/week is its
            own thread. Defaults to ``name``.
        enabled: Whether the schedule is created in the ENABLED state. ``False`` deploys it paused.
    """

    name: str
    schedule: str
    prompt: str
    skill: str | None = None
    session_prefix: str = ""
    enabled: bool = True

    def session_stem(self) -> str:
        return self.session_prefix or self.name


# The registry. Order doesn't matter; names must be unique.
JOBS: list[ScheduledJob] = [
    ScheduledJob(
        name="daily-activity-review",
        schedule="rate(1 day)",
        skill="code-review",
        prompt=(
            "This is your scheduled daily self-review. Review YOUR OWN activity over roughly the "
            "last 24 hours and flag anything that needs attention.\n\n"
            "Look at:\n"
            "- Your recent pull requests (open and recently merged) on the repos you work in — did "
            "  any get review pushback, fail CI, or stall waiting on you?\n"
            "- Issues you were mentioned on or were handling — any awaiting your follow-up?\n"
            "- Your own recent runs: any that failed or produced a questionable result worth a "
            "  second look?\n\n"
            "Produce a short, scannable summary: what you did, what went well, and a prioritized "
            "list of concrete follow-ups (with links). If everything is clean, say so plainly — "
            "do not manufacture work. This is a read-and-report pass: investigate and summarize, "
            "do not open PRs or post comments unless you find something genuinely broken that you "
            "already had authorization to fix."
        ),
    ),
]


def by_name(name: str) -> ScheduledJob | None:
    """Return the job with this ``name``, or ``None`` if there's no such job."""
    for job in JOBS:
        if job.name == name:
            return job
    return None
