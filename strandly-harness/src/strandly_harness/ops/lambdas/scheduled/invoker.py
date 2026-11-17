"""The scheduled-invoker Lambda — one handler for every scheduled job.

EventBridge Scheduler fires this Lambda with a payload naming which job to run (``{"job": "<name>"}``
— set per-schedule by the CDK). The handler looks the job up in :mod:`jobs`, builds its prompt
(optionally prepending a skill activation), and dispatches the deployed AgentCore runtime
fire-and-forget via the same ``serving.runtime_client.launch_run`` the mention poller uses. The
agent does the work and persists its result to AgentCore Memory; this is a pure trigger, so it does
not wait for or poll the result.

Session id: ``sched-<job stem>-<UTC date>`` — deterministic within a day so a same-day retry threads
the same AgentCore Memory conversation, but distinct across days so each run starts fresh (a daily
review can still see yesterday's via Memory recall, but isn't appended into one ever-growing thread).

Design mirrors ``ingress/mentions.py``: boto3 / the Strands-SDK-pulling serving imports are lazy, so
importing this module (or the dependency-free :mod:`jobs`) never drags in the SDK; the dispatch seam
is the one network call tests monkeypatch. The handler is **fail-soft per job**: one job's dispatch
error is logged and reported, never raised, so a bad job can't wedge the schedule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from strandly_harness.core.config import AWS_REGION, RUNTIME_ARN, Config
from strandly_harness.ops.lambdas.scheduled.jobs import ScheduledJob, by_name

logger = logging.getLogger(__name__)


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def build_session_id(job: ScheduledJob, *, date: str | None = None) -> str:
    """Deterministic session id for a job's run on a given UTC date (``sched-<stem>-<YYYYMMDD>``).

    This threads a job's same-day runs into one AgentCore Memory conversation; it is **not** a dedup
    mechanism. Unlike the mention poller (which has a DynamoDB dedup backstop), a double-fire from
    EventBridge's at-least-once delivery would dispatch the job twice into the same session.
    Idempotency therefore relies on the job's prompt being safe to re-run (the daily review is a
    read-and-report pass, so a duplicate is harmless), not on dispatch-level deduplication.
    """
    return f"sched-{job.session_stem()}-{date or _utc_date()}"


def build_prompt(job: ScheduledJob) -> str:
    """The full prompt for a job: a skill activation line (if any) prepended to its prompt."""
    if job.skill:
        return f'skill(action="activate", name="{job.skill}")\n\n{job.prompt}'
    return job.prompt


def run_job(job: ScheduledJob, config: Config, *, date: str | None = None) -> dict[str, Any]:
    """Dispatch one job fire-and-forget. Returns the runtime's response dict (or an error dict).

    Lazy imports of the serving layer (which pulls the Strands SDK) keep this module — and the
    dependency-free job registry the CDK loads — import-light.
    """
    from strandly_harness.ops import runtime_client

    arn = runtime_client.resolve_runtime_arn(config.get(RUNTIME_ARN))
    region = runtime_client.resolve_region(config.get(AWS_REGION))
    if not arn or not region:
        raise RuntimeError(
            f"cannot dispatch job {job.name!r}: runtime_arn={arn!r} region={region!r} "
            "(set STRANDLY_RUNTIME_ARN / AWS_REGION)"
        )
    session_id = build_session_id(job, date=date)
    # Fire-and-forget: empty github context. The deployed runtime persists the result to Memory.
    return runtime_client.launch_run(arn, region, session_id, build_prompt(job), {})


def dispatch_jobs(job_names: list[str], config: Config | None = None) -> dict[str, Any]:
    """Run each named job, collecting per-job outcomes. Fail-soft: one bad job never sinks the rest.

    Returns ``{"status", "dispatched": [...], "results": {name: outcome}}`` where each outcome is
    ``"dispatched"``, ``"unknown-job"``, or ``"error: <msg>"``.
    """
    config = config or Config.load()
    results: dict[str, str] = {}
    dispatched: list[str] = []
    for name in job_names:
        job = by_name(name)
        if job is None:
            logger.warning("scheduled: no such job %r; skipping", name)
            results[name] = "unknown-job"
            continue
        try:
            resp = run_job(job, config)
            accepted = isinstance(resp, dict) and resp.get("status") == "accepted"
            if accepted:
                dispatched.append(name)
                results[name] = "dispatched"
                logger.info(
                    "scheduled: dispatched job %r (taskId=%s)", name, resp.get("taskId")
                )
            else:
                # An HTTP-200 rejection from the runtime (e.g. {"status":"error"}).
                results[name] = f"error: runtime rejected ({resp})"
                logger.warning("scheduled: job %r not accepted: %s", name, resp)
        except Exception as e:  # noqa: BLE001 — fail-soft: a bad job can't wedge the schedule
            results[name] = f"error: {type(e).__name__}: {e}"
            logger.exception("scheduled: job %r dispatch failed", name)
    status = "ok" if dispatched else ("noop" if not job_names else "error")
    return {"status": status, "dispatched": dispatched, "results": results}


def _jobs_from_event(event: Any) -> list[str]:
    """Pull the job name(s) to run from the EventBridge payload.

    The CDK sets each schedule's input to ``{"job": "<name>"}``; we also accept ``{"jobs": [...]}``
    for a schedule that fans several jobs into one tick. An empty/standalone invoke runs nothing
    (returns a noop) rather than guessing — schedules always name their job.
    """
    if isinstance(event, dict):
        if isinstance(event.get("jobs"), list):
            return [str(j) for j in event["jobs"]]
        if event.get("job"):
            return [str(event["job"])]
    return []


def lambda_handler(event: Any, context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """AWS Lambda entrypoint: dispatch the job(s) named in the EventBridge payload."""
    job_names = _jobs_from_event(event)
    if not job_names:
        logger.warning("scheduled: invoked with no job name in event=%r; noop", event)
        return {"status": "noop", "dispatched": [], "results": {}}
    return dispatch_jobs(job_names)
