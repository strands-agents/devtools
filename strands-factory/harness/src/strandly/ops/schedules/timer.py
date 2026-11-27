"""EventBridge Scheduler side of a job: one schedule per job in the jobs group.

The schedule is named after the job id and fires the invoker Lambda with
``{"job_id": <id>, "scheduled_time": <aws.scheduler.scheduled-time>}``. Once jobs use
``at(...)`` with ``ActionAfterCompletion: DELETE``; recurring jobs carry ``EndDate`` when they
expire. A ``rate(...)`` schedule always carries ``StartDate``: the next ``anchor + k * period`` (the
anchor is ``starts_at``, else ``created_at``), so every update keeps the job's phase (a rate
schedule without one fires right away and is phased from the update). A cron schedule carries
``StartDate`` while ``starts_at`` is still ahead.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from strandly.ops.schedules.expressions import next_aligned
from strandly.ops.schedules.model import KIND_ONCE, STATUS_ACTIVE, Job, parse_iso, utcnow

SCHEDULED_TIME = "<aws.scheduler.scheduled-time>"
# The reconcile tick's schedule (infra ``JOBS_RECONCILE_SCHEDULE``) shares the jobs group. It is not
# a valid job id; the timer never lists, updates or deletes it.
RECONCILE_SCHEDULE = "_reconcile"


@dataclass(frozen=True)
class TimerSettings:
    group: str
    target_arn: str
    role_arn: str


def error_code(error: BaseException) -> str:
    return str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))


def target_input(job_id: str) -> str:
    return json.dumps({"job_id": job_id, "scheduled_time": SCHEDULED_TIME}, sort_keys=True)


class JobTimer:
    """Creates, updates, deletes and lists the jobs group's schedules."""

    def __init__(
        self, client: Any, settings: TimerSettings, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    @property
    def group(self) -> str:
        return self._settings.group

    @staticmethod
    def schedule_fields(job: Job) -> tuple[Any, ...]:
        """What the job's schedule is built from; an edit that keeps these needs no Scheduler call."""
        return (
            job.kind,
            job.expression,
            job.timezone,
            job.status == STATUS_ACTIVE,
            job.starts_at,
            job.expires_at,
            job.id,
        )

    def start_date(self, job: Job, now: datetime) -> datetime | None:
        """The schedule's ``StartDate`` (see the module docstring)."""
        if job.kind == KIND_ONCE:
            return None
        rate = job.parsed().rate_seconds
        anchor = job.rate_anchor() if rate else None
        if rate and anchor is not None:
            return next_aligned(anchor, now, timedelta(seconds=rate))
        if job.starts_at:
            start = parse_iso(job.starts_at)
            return start if start > now else None
        return None

    def request(self, job: Job, now: datetime | None = None) -> dict[str, Any]:
        """The full Create/UpdateSchedule request for ``job``."""
        now = now or self._clock()
        req: dict[str, Any] = {
            "Name": job.id,
            "GroupName": self._settings.group,
            "ScheduleExpression": job.expression,
            "ScheduleExpressionTimezone": job.timezone,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED" if job.status == STATUS_ACTIVE else "DISABLED",
            "Description": job.name[:512],
            "Target": {
                "Arn": self._settings.target_arn,
                "RoleArn": self._settings.role_arn,
                "Input": target_input(job.id),
            },
            "ActionAfterCompletion": "DELETE" if job.kind == KIND_ONCE else "NONE",
        }
        if job.kind == KIND_ONCE:
            return req
        end = parse_iso(job.expires_at) if job.expires_at else None
        start = self.start_date(job, now)
        if end is not None:
            req["EndDate"] = end
        if start is not None and end is not None and start >= end:
            # No fire left before the end date (Scheduler wants StartDate < EndDate).
            req["State"] = "DISABLED"
        elif start is not None:
            req["StartDate"] = start
        return req

    def upsert(self, job: Job) -> None:
        if job.id == RECONCILE_SCHEDULE:
            raise ValueError(f"{RECONCILE_SCHEDULE!r} is the reconcile tick, not a job")
        req = self.request(job)
        try:
            self._client.update_schedule(**req)
        except Exception as e:
            if error_code(e) != "ResourceNotFoundException":
                raise
            self._client.create_schedule(**req)

    def delete(self, job_id: str) -> bool:
        """Delete the job's schedule; ``False`` if there was none (or it is the reconcile tick)."""
        if job_id == RECONCILE_SCHEDULE:
            return False
        try:
            self._client.delete_schedule(Name=job_id, GroupName=self._settings.group)
        except Exception as e:
            if error_code(e) == "ResourceNotFoundException":
                return False
            raise
        return True

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            return dict(self._client.get_schedule(Name=job_id, GroupName=self._settings.group))
        except Exception as e:
            if error_code(e) == "ResourceNotFoundException":
                return None
            raise

    def names(self) -> list[str]:
        """The job schedules in the group (every schedule but the reconcile tick)."""
        out: list[str] = []
        kwargs: dict[str, Any] = {"GroupName": self._settings.group}
        while True:
            resp = self._client.list_schedules(**kwargs)
            out.extend(
                str(s["Name"]) for s in resp.get("Schedules", []) if s["Name"] != RECONCILE_SCHEDULE
            )
            if not resp.get("NextToken"):
                return out
            kwargs["NextToken"] = resp["NextToken"]

    def drifted(self, job: Job, actual: dict[str, Any]) -> bool:
        """True when the live schedule differs from what ``job`` wants."""
        now = self._clock()
        want = self.request(job, now)
        for key in (
            "ScheduleExpression",
            "ScheduleExpressionTimezone",
            "State",
            "ActionAfterCompletion",
        ):
            if (actual.get(key) or None) != (want.get(key) or None):
                return True
        target = actual.get("Target") or {}
        for key in ("Arn", "RoleArn"):
            if target.get(key) != want["Target"][key]:
                return True
        try:
            if json.loads(target.get("Input") or "{}") != json.loads(want["Target"]["Input"]):
                return True
        except ValueError:
            return True
        if _differ(want.get("EndDate"), actual.get("EndDate")):
            return True
        return self._start_drifted(job, want.get("StartDate"), actual.get("StartDate"), now)

    def _start_drifted(
        self, job: Job, want: datetime | None, have: datetime | None, now: datetime
    ) -> bool:
        rate = job.parsed().rate_seconds if job.kind != KIND_ONCE else None
        if rate and want is not None:
            # Any start date on the job's phase is as good as the next one.
            if have is None:
                return True
            offset = (have - want).total_seconds() % rate
            return min(offset, rate - offset) > 1
        if want is None:
            return have is not None and have > now
        return _differ(want, have)


def _differ(want: datetime | None, have: datetime | None) -> bool:
    if (want is None) != (have is None):
        return True
    return bool(want and have and abs((want - have).total_seconds()) > 1)
