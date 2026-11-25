"""Launch one run of a job on the deployed runtime (fire-and-forget).

Shared by the invoker (a schedule fired) and ``run_now``. ``queueStrategy: "reject"``: if the
job's session is still busy the runtime answers ``rejected`` instead of superseding it. A caller
that may not invoke the runtime itself (the agent) runs a job through :func:`remote_launcher`,
which asks the invoker to launch it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from strandly.core.config import AWS_REGION, RUNTIME_ARN, Config
from strandly.core.context import SLACK_CHANNEL_KEY, SLACK_THREAD_KEY
from strandly.ops.schedules.model import Job

logger = logging.getLogger(__name__)

DISPATCHED = "dispatched"
BUSY = "busy"
MAYBE = "maybe-dispatched"
ERROR = "error"
DISABLED = "disabled"
ENV_ENABLED = "STRANDLY_SCHEDULES_ENABLED"
ENV_LEGACY_ENABLED = "STRANDLY_SCHEDULED_ENABLED"


def schedules_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Both enable flags must permit dispatch; an explicit false/invalid value fails closed."""
    values = os.environ if env is None else env
    return all(
        values.get(key, "true").strip().lower() in {"true", "1", "yes", "on"}
        for key in (ENV_ENABLED, ENV_LEGACY_ENABLED)
    )


@dataclass(frozen=True)
class RunOutcome:
    status: str  # dispatched | busy | maybe-dispatched | error | disabled
    session_id: str
    task_id: str | None = None
    detail: str | None = None


HOME_NOTIFY_INSTRUCTION = (
    "\n\nWhen you are done, post your result to the maintainers' Slack home channel using "
    "notify_team: a title line saying what happened and whether action is needed, the details in "
    "its thread."
)


def is_slack_dm(channel: str | None) -> bool:
    """Whether ``channel`` is a Slack 1:1 DM (an IM channel id)."""
    return (channel or "").startswith("D")


def notify_context(job: Job, config: Config | None = None) -> dict[str, str]:
    """Payload keys for the job's notify target: a Slack target rides as the Slack ingress's
    channel/thread keys, which is what gives the run ``use_slack`` and its default thread.

    A scheduled run posts to Slack only in the enabled home channel (a job without a target goes
    to its root, a home-channel target to its thread). Any other stored Slack target, a DM
    included, gives the run no Slack channel: it posts nowhere.
    """
    target = job.notify
    home = config.slack_home_channel if config is not None else None
    if target is None:
        return {SLACK_CHANNEL_KEY: home} if home else {}
    if target.kind != "slack" or not target.channel:
        return {}
    if not home or target.channel != home:
        logger.warning("scheduled: job %r Slack target is not the home channel; the run posts nowhere", job.id)
        return {}
    keys = {SLACK_CHANNEL_KEY: target.channel}
    if target.thread_ts:
        keys[SLACK_THREAD_KEY] = target.thread_ts
    return keys


def launch(job: Job, config: Config, *, now: datetime, trigger_source: str) -> RunOutcome:
    """Send the job's prompt to its session. Never raises; failures come back as ``error``."""
    from strandly.ops import metrics, runtime_client

    session_id = job.session_id(now)
    if not schedules_enabled():
        return RunOutcome(DISABLED, session_id, detail="Scheduled jobs are globally disabled.")
    arn = runtime_client.resolve_runtime_arn(config.get(RUNTIME_ARN))
    region = runtime_client.resolve_region(config.get(AWS_REGION))
    if not arn or not region:
        detail = f"runtime_arn={arn!r} region={region!r} (set STRANDLY_RUNTIME_ARN / AWS_REGION)"
        logger.error("scheduled: cannot dispatch job %r: %s", job.id, detail)
        return RunOutcome(ERROR, session_id, detail="Runtime is not configured")
    notify = notify_context(job, config)
    extra = {"queueStrategy": "reject", "triggerSource": trigger_source, **notify}
    prompt = job.run_prompt()
    home = config.slack_home_channel
    if home and notify.get(SLACK_CHANNEL_KEY) == home and not notify.get(SLACK_THREAD_KEY):
        # A home-channel result is a notification: a title plus details in its thread.
        prompt = job.with_changes(notify=None).run_prompt() + HOME_NOTIFY_INSTRUCTION
    elif job.notify is not None and job.notify.kind == "slack" and not notify:
        prompt = job.with_changes(notify=None).run_prompt()
    try:
        resp: Any = runtime_client.launch_run(arn, region, session_id, prompt, extra)
    except Exception as e:  # noqa: BLE001 — a failed launch is reported, never raised
        if runtime_client.launch_maybe_accepted(e) or isinstance(e, UnicodeDecodeError):
            # Sent, answer lost: the run may be live, so it is neither retried nor an error.
            logger.warning("scheduled: job %r dispatch MAYBE accepted (%s: %s)", job.id, type(e).__name__, e)
            metrics.emit({metrics.DISPATCH_MAYBE: 1}, surface=metrics.SURFACE_SCHEDULED)
            return RunOutcome(MAYBE, session_id, detail="Acceptance uncertain; do not retry this launch")
        logger.exception("scheduled: job %r dispatch failed", job.id)
        return RunOutcome(ERROR, session_id, detail="Runtime launch failed; details logged internally")
    status = resp.get("status") if isinstance(resp, dict) else None
    task_id = resp.get("taskId") if isinstance(resp, dict) else None
    if status == "accepted":
        logger.info("scheduled: dispatched job %r (taskId=%s)", job.id, task_id)
        return RunOutcome(DISPATCHED, session_id, task_id=task_id)
    if status == "rejected":
        logger.info("scheduled: job %r skipped, its session is still running", job.id)
        return RunOutcome(BUSY, session_id, task_id=task_id)
    if status == "error":
        logger.warning("scheduled: job %r not accepted: %s", job.id, resp)
        return RunOutcome(ERROR, session_id, detail="Runtime did not accept the launch")
    logger.warning("scheduled: job %r acceptance is uncertain: %r", job.id, resp)
    metrics.emit({metrics.DISPATCH_MAYBE: 1}, surface=metrics.SURFACE_SCHEDULED)
    return RunOutcome(MAYBE, session_id, detail="Acceptance uncertain; do not retry this launch")


def remote_launcher(lambda_client: Any, function_arn: str) -> Any:
    """A ``ScheduleService`` launcher that asks the invoker Lambda to launch the job (its
    ``{"launch": {"job_id": ...}}`` event), for callers that may not invoke the runtime."""

    def launch_via_invoker(job: Job, config: Any, *, now: datetime, trigger_source: str) -> RunOutcome:
        del config, trigger_source  # the invoker reads the job and names the trigger itself
        from strandly.ops import runtime_client

        invoked = False
        try:
            resp = lambda_client.invoke(
                FunctionName=function_arn,
                Payload=json.dumps({"launch": {"job_id": job.id}}).encode(),
            )
            invoked = True
            body = json.loads(resp["Payload"].read() or b"{}")
        except Exception as e:  # noqa: BLE001 - reported as the run's outcome
            logger.exception("scheduled: remote launch of job %r failed", job.id)
            maybe = invoked or runtime_client.launch_maybe_accepted(e) or isinstance(e, TimeoutError)
            return RunOutcome(
                MAYBE if maybe else ERROR, job.session_id(now),
                detail="Acceptance uncertain; do not retry this launch" if maybe
                else "Invoker launch failed; details logged internally",
            )
        out = body.get("outcome") if isinstance(body, dict) else None
        if not isinstance(out, dict):
            logger.warning("scheduled: malformed invoker response: %r", body)
            return RunOutcome(MAYBE, job.session_id(now), detail="Acceptance uncertain; do not retry this launch")
        status = out.get("status")
        if status not in {DISPATCHED, MAYBE, BUSY, ERROR, "missing", "disabled", "pending-confirmation"}:
            logger.warning("scheduled: unrecognized invoker outcome: %r", out)
            return RunOutcome(MAYBE, job.session_id(now), detail="Acceptance uncertain; do not retry this launch")
        return RunOutcome(
            str(status),
            str(out.get("session_id") or job.session_id(now)),
            task_id=out.get("task_id"),
            detail=("Acceptance uncertain; do not retry this launch" if status == MAYBE
                    else "Details logged internally" if out.get("detail") else None),
        )

    return launch_via_invoker
