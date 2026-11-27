"""The scheduled-invoker Lambda: one handler for every scheduled job.

EventBridge Scheduler sends ``{"job_id", "scheduled_time"}`` from a job's schedule (the table wins:
a paused, deleted, completed or expired job does not run), ``{"reconcile": true}`` from the hourly
reconcile tick, or ``{"launch": {"job_id"}}`` from the agent's ``run_now``. Each fire window runs a job
at most once, so a redelivery never double-fires. The handler reports and never raises, so
EventBridge does not redeliver.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from strandly.ops.lambdas import configure_logging
from strandly.ops.schedules import dispatch
from strandly.ops.schedules.service import (
    ForbiddenError,
    NotFoundError,
    ScheduleService,
    ScheduleSettings,
)

logger = logging.getLogger(__name__)

# "false" pauses every job fire (an environment deployed with schedules disabled).
ENV_ENABLED = "STRANDLY_SCHEDULES_ENABLED"

_OK = (dispatch.DISPATCHED, dispatch.MAYBE)
_FAILED = (dispatch.ERROR, "unconfigured")


def _service() -> ScheduleService | None:
    settings = ScheduleSettings.from_env()
    return ScheduleService.from_settings(settings) if settings else None


def _summary(key: str, outcome: str) -> dict[str, Any]:
    if outcome in _OK:
        status = "ok"
    elif outcome in _FAILED:
        status = "error"
    else:
        status = "noop"
    return {
        "status": status,
        "dispatched": [key] if outcome in _OK else [],
        "results": {key: outcome},
    }


_cold = {"start": True}


def _cold_start_reconcile(service: ScheduleService) -> None:
    if not _cold["start"]:
        return
    _cold["start"] = False
    try:
        report = service.reconcile(only_unsynced=True, max_late=0)
        if report.as_dict():
            logger.info("scheduled: cold-start reconcile %s", report.as_dict())
    except Exception:  # noqa: BLE001 — the hourly tick retries
        logger.warning("scheduled: cold-start reconcile failed", exc_info=True)


def _launch(request: dict[str, Any], service: ScheduleService | None) -> dict[str, Any]:
    """A manual run asked for by the agent's ``run_now`` (which guards and records it)."""
    if service is None:
        return {"status": "error", "results": {"launch": "unconfigured"}}
    try:
        outcome = service.launch(str(request.get("job_id") or ""))
    except NotFoundError:
        return {"status": "noop", "outcome": {"status": "missing", "session_id": ""}}
    except ForbiddenError:
        return {"status": "noop", "outcome": {"status": "pending-confirmation", "session_id": ""}}
    return {"status": "ok", "outcome": asdict(outcome)}


def lambda_handler(event: Any, context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """AWS Lambda entrypoint; logs one summary line per invocation."""
    configure_logging()
    result = _handle(event if isinstance(event, dict) else {})
    logger.info(
        "scheduled: status=%s results=%s reconcile=%s",
        result.get("status"),
        result.get("results", {}),
        result.get("reconcile", {}),
    )
    return result


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    try:
        enabled = dispatch.schedules_enabled()
        if isinstance(event.get("launch"), dict) and not enabled:
            return {"status": "noop", "outcome": {"status": "disabled", "session_id": ""}}
        service = _service()
        if service is not None and enabled and not event.get("reconcile"):
            _cold_start_reconcile(service)
        if event.get("reconcile"):
            if service is None:
                return {"status": "error", "results": {"reconcile": "unconfigured"}}
            return {"status": "ok", "reconcile": service.reconcile(max_late=1 if enabled else 0).as_dict()}
        if isinstance(event.get("launch"), dict):
            return _launch(event["launch"], service)
        key = str(event.get("job_id") or "")
        if not key:
            logger.warning("scheduled: invoked with no job in event=%r; noop", event)
            return {"status": "noop", "dispatched": [], "results": {}}
        if not enabled:
            return _summary(key, "disabled")
        if service is None:
            return _summary(key, "unconfigured")
        outcome = service.fire(key, scheduled_time=event.get("scheduled_time"))
    except Exception:  # noqa: BLE001 — never raise, or EventBridge redelivers
        logger.exception("scheduled: handling %r failed", event)
        return {"status": "error", "results": {"error": "Scheduled operation failed; details logged internally"}}
    return _summary(key, outcome)
