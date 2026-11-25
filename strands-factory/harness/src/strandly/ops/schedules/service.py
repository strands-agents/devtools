"""The schedules service: the one write path for scheduled jobs.

Every change runs validate -> DynamoDB (job + revision, conditional) -> EventBridge Scheduler.
DynamoDB is the source of truth: a failed Scheduler call leaves the row ``unsynced`` and
:meth:`ScheduleService.reconcile` makes the jobs group match the table. The invoker calls
:meth:`ScheduleService.fire`; the dashboard API and the agent tool call the rest.

Every job belongs to the team: any actor a surface accepts may change any job, and the team holds
at most ``max_active`` active jobs across every surface. The caller's ``Actor`` is recorded as
``created_by``/``updated_by`` and on each revision for audit only. Other limits depend on the
caller's surface (``Actor.surface``): see :data:`LIMITS`.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from strandly.ops.schedules import dispatch, expressions
from strandly.ops.schedules.dispatch import RunOutcome
from strandly.ops.schedules.model import (
    KIND_ONCE,
    SESSION_DAILY,
    SESSION_MODES,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_PAUSED,
    SURFACE_AGENT,
    SURFACE_DASHBOARD,
    SURFACE_SYSTEM,
    SYNC_PENDING,
    SYNC_SYNCED,
    SYNC_UNSYNCED,
    Actor,
    Job,
    LastRun,
    NotifyTarget,
    Revision,
    iso,
    parse_iso,
    utcnow,
)
from strandly.ops.schedules.store import ConflictError, JobStore
from strandly.ops.schedules.timer import JobTimer, TimerSettings, error_code

logger = logging.getLogger(__name__)

# The ``trigger_id`` prefix of a job's own scheduled run. It marks a pending job as waiting but
# never confirms one: only a new authenticated requester mention does.
SCHEDULED_TRIGGER_PREFIX = "scheduled:"
ENV_TABLE = "STRANDLY_SCHEDULES_TABLE"
ENV_GROUP = "STRANDLY_SCHEDULES_GROUP"
ENV_TARGET_ARN = "STRANDLY_SCHEDULES_TARGET_ARN"
ENV_ROLE_ARN = "STRANDLY_SCHEDULES_ROLE_ARN"
# Active jobs the team may hold across every surface.
ENV_MAX_ACTIVE = "STRANDLY_SCHEDULES_MAX_ACTIVE"
DEFAULT_MAX_ACTIVE = 100
# A recurring job always expires: by default, and at the latest, this many days from the change.
RECURRING_TTL_DAYS = 90

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_SLACK_CHANNEL_RE = re.compile(r"^[CDG][A-Z0-9]{2,}$")
_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/[\w.-]+/[\w.-]+/(issues|pull|discussions)/\d+$"
)
_MAX_NAME = 200
# A once job whose time passed this long ago without a schedule is settled by reconcile.
_ONCE_GRACE = timedelta(minutes=15)
# A once job's schedule fire must be this close to the job's time (else it is a stale schedule).
_ONCE_SLACK = timedelta(minutes=5)
# Scheduler calls per sync, the backoff between them, and re-syncs after a concurrent edit.
_SYNC_ATTEMPTS = 3
_SYNC_BACKOFF_S = 0.2
_SYNC_ROUNDS = 3
# Attempts at a write that makes a job active when another activation lands between count and write.
_QUOTA_ROUNDS = 3
_NO_RETRY = frozenset({"ValidationException", "AccessDeniedException"})
# Reconcile deletes schedules with no job row only in a group named like this.
JOBS_GROUP_SUFFIX = "-jobs"
_EDITABLE = frozenset(
    {
        "name",
        "expression",
        "timezone",
        "prompt",
        "skill",
        "session_mode",
        "notify",
        "starts_at",
        "expires_at",
    }
)


class ValidationError(ValueError):
    """The job (or change) is not allowed; the message says why."""


class NotFoundError(LookupError):
    pass


class ForbiddenError(PermissionError):
    pass


#: The shortest interval of a recurring job, on every surface. A job saved under an older, shorter
#: minimum keeps firing and can be paused or deleted; an edit or resume that keeps it is refused.
MIN_INTERVAL_SECONDS = 3600
#: How far ahead a job's own scheduled run may set a once job, so a chain cannot loop fast.
SCHEDULED_ONCE_LEAD_SECONDS = 900


@dataclass(frozen=True)
class Limits:
    max_prompt_chars: int
    namespaces: tuple[str, ...]  # actor id namespaces this surface may act as
    run_now_gap_seconds: int | None = None  # minimum gap between two run_now of one job


# The agent surface's ``system`` namespace is the team actor of a scheduled run (``tools/schedule.py``).
LIMITS: dict[str, Limits] = {
    SURFACE_AGENT: Limits(8000, ("slack", "portal", "person", "github", "github-actions", "system"), 900),
    SURFACE_DASHBOARD: Limits(20000, ("portal", "person", "iam")),
    SURFACE_SYSTEM: Limits(20000, ("system",)),
}


@dataclass(frozen=True)
class JobSpec:
    """What a caller asks for when creating a job."""

    name: str
    expression: str
    prompt: str
    timezone: str = "UTC"
    skill: str | None = None
    session_mode: str = SESSION_DAILY
    notify: NotifyTarget | Mapping[str, Any] | None = None
    starts_at: str | None = None  # recurring: no fire before this (and a rate job's phase)
    expires_at: str | None = None
    enabled: bool = True
    id: str | None = None


@dataclass(frozen=True)
class Preview:
    """What :meth:`ScheduleService.preview` says about a spec: why it would be refused (empty if
    it would be accepted) and when it would fire."""

    errors: list[str]
    next_runs: list[datetime]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ReconcileReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    fired_late: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


def builtin_skill_names() -> frozenset[str]:
    """Skills shipped in the package (a directory with a ``SKILL.md``), read without the SDK."""
    from importlib.resources import files
    from pathlib import Path

    from strandly.extensions import selected_skill_packs, skills_source_dir
    root = skills_source_dir(Path(str(files("strandly").joinpath("skills"))), selected_skill_packs())
    return frozenset(
        d.name for d in root.iterdir() if d.is_dir() and d.joinpath("SKILL.md").is_file()
    )


def _check_kept_interval(job: Job, new: Job) -> None:
    """Refuse, with a reason, an edit or resume that keeps a job's interval below the minimum."""
    if new.kind == KIND_ONCE or new.expression != job.expression:
        return
    try:
        interval = expressions.parse(job.expression).min_interval_seconds() or 0
    except expressions.ExpressionError:
        return
    if interval < MIN_INTERVAL_SECONDS:
        raise ValidationError(
            f"job {job.id!r} runs {job.expression}, more often than the one-hour minimum. It keeps "
            "running as it is and can be paused or deleted; to change or resume it, set a schedule "
            "that runs at most hourly"
        )


def limits_for(actor: Actor) -> Limits:
    """The surface's limits; also checks the actor id is namespaced for that surface."""
    try:
        limits = LIMITS[actor.surface]
    except KeyError:
        raise ValidationError(f"unknown surface {actor.surface!r}") from None
    if actor.namespace not in limits.namespaces:
        raise ValidationError(
            f"actor {actor.actor!r} must be one of "
            f"{', '.join(n + ':<id>' for n in limits.namespaces)} on the {actor.surface} surface"
        )
    return limits


def notify_target(value: NotifyTarget | Mapping[str, Any] | None) -> NotifyTarget | None:
    """Parse and check a notify target (a Slack channel/thread or a GitHub issue/PR/discussion).

    A Slack DM is refused: a scheduled job never posts to one.
    """
    if value is None or value == {}:
        return None
    target = value if isinstance(value, NotifyTarget) else NotifyTarget.from_item(dict(value))
    if target is None:
        return None
    if target.kind == "slack":
        if not _SLACK_CHANNEL_RE.match(target.channel or ""):
            raise ValidationError(f"bad Slack channel id {target.channel!r}")
        if dispatch.is_slack_dm(target.channel):
            raise ValidationError(SLACK_DM_REFUSED)
        if target.thread_ts and not _SLACK_TS_RE.match(target.thread_ts):
            raise ValidationError(f"bad Slack thread ts {target.thread_ts!r}")
        return NotifyTarget("slack", channel=target.channel, thread_ts=target.thread_ts)
    if target.kind == "github":
        if not _GITHUB_URL_RE.match(target.url or ""):
            raise ValidationError(f"bad GitHub issue/PR/discussion URL {target.url!r}")
        return NotifyTarget("github", url=target.url)
    raise ValidationError(f"notify kind must be slack or github, not {target.kind!r}")


SLACK_DM_REFUSED = (
    "a scheduled job never posts to a Slack DM: clear notify and its runs post to the home "
    "channel when it is on, else nowhere (the result stays in the run's session)"
)
SLACK_NOTIFY_REFUSED = (
    "a job posts to Slack only in the home channel: ask Strandly from the home-channel thread it "
    "should post in, or clear notify (results then go to the home channel when it is on)"
)


def resolve_notify(
    value: NotifyTarget | Mapping[str, Any] | None,
    *,
    keep: Iterable[NotifyTarget | None] = (),
    slack_from: NotifyTarget | None = None,
) -> NotifyTarget | None:
    """A job's new notify target, refused when its caller may not set it.

    Never a Slack DM (:func:`notify_target`). Another Slack target is accepted unchanged (one of
    ``keep``, the job's current target) or from the home-channel conversation the caller speaks in
    (``slack_from``, which only the agent's ``schedule`` tool passes): that channel's root or the
    caller's own thread, never another thread. The dashboard API passes
    none: it may keep or clear a Slack target, never set one. Each run checks the target again
    (:func:`dispatch.notify_context`) and posts to Slack only in the enabled home channel.
    """
    target = notify_target(value)
    if target is None or target.kind != "slack":
        return target
    if any(kept == target for kept in keep):
        return target
    if (
        slack_from is not None
        and slack_from.kind == "slack"
        and slack_from.channel == target.channel
        and (not target.thread_ts or target.thread_ts == slack_from.thread_ts)
    ):
        return target
    raise ValidationError(SLACK_NOTIFY_REFUSED)


def _instant(field_name: str, text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return parse_iso(text)
    except ValueError as e:
        raise ValidationError(f"bad {field_name} {text!r}") from e


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "job"


@dataclass(frozen=True)
class ScheduleSettings:
    table: str
    group: str
    target_arn: str
    role_arn: str
    region: str | None = None
    max_active: int = DEFAULT_MAX_ACTIVE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ScheduleSettings | None:
        env = os.environ if env is None else env
        values = [env.get(k, "") for k in (ENV_TABLE, ENV_GROUP, ENV_TARGET_ARN, ENV_ROLE_ARN)]
        if not all(values):
            return None
        region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None
        table, group, target_arn, role_arn = values
        return cls(
            table, group, target_arn, role_arn, region=region,
            max_active=max_active(env.get(ENV_MAX_ACTIVE)),
        )


def max_active(raw: str | None) -> int:
    """The team's active-job cap from its setting: a positive integer, else the default (the cap
    never turns off)."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_MAX_ACTIVE
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "schedules: %s=%r is not a positive integer; using %d", ENV_MAX_ACTIVE, text, DEFAULT_MAX_ACTIVE
        )
        return DEFAULT_MAX_ACTIVE
    return value


class ScheduleService:
    """List, create, edit, pause/resume, delete, run and reconcile scheduled jobs."""

    def __init__(
        self,
        store: JobStore,
        timer: JobTimer,
        *,
        skills: Callable[[], Iterable[str]] | None = builtin_skill_names,
        clock: Callable[[], datetime] = utcnow,
        config: Any = None,
        launcher: Callable[..., RunOutcome] = dispatch.launch,
        sleep: Callable[[float], None] = time.sleep,
        max_active: int = DEFAULT_MAX_ACTIVE,
    ) -> None:
        self.store = store
        self.timer = timer
        self._skills = skills
        self._clock = clock
        self._config = config
        self._launcher = launcher
        self._sleep = sleep
        self._max_active = max_active
        self._late_left = 0
        self._via: dict[str, str] | None = None

    @classmethod
    def from_settings(cls, settings: ScheduleSettings, **kwargs: Any) -> ScheduleService:
        import boto3

        session = boto3.Session(region_name=settings.region)
        store = JobStore(session.client("dynamodb"), settings.table)
        timer = JobTimer(
            session.client("scheduler"),
            TimerSettings(settings.group, settings.target_arn, settings.role_arn),
            clock=kwargs.get("clock", utcnow),
        )
        kwargs.setdefault("max_active", settings.max_active)
        return cls(store, timer, **kwargs)

    def for_run_of(self, job_id: str, *, session_id: str, trigger: str) -> ScheduleService:
        """This service for writes made by job ``job_id``'s own scheduled run: every revision
        records ``via`` that run."""
        scoped = copy.copy(self)
        scoped._via = {"kind": "scheduled-run", "job_id": job_id, "session_id": session_id, "trigger": trigger}
        return scoped

    def _scheduled_run(self) -> bool:
        return self._via is not None and self._via.get("kind") == "scheduled-run"

    # --- reads ------------------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def list_jobs(self) -> list[Job]:
        return self.store.list_jobs()

    def get(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise NotFoundError(f"no job {job_id!r}")
        return job

    def revisions(self, job_id: str) -> list[Revision]:
        return self.store.revisions(job_id)

    def next_runs(self, job: Job, count: int = 5) -> list[datetime]:
        """When an active job fires next: a once job at its time, a rate job on its phase, both
        recurring kinds within ``[starts_at, expires_at)``."""
        if job.status != STATUS_ACTIVE:
            return []
        try:
            return job.next_runs(self._clock(), count)
        except expressions.ExpressionError:
            return []

    def validate(self, spec: JobSpec, actor: Actor, *, check_quota: bool = True) -> list[str]:
        """Why ``spec`` would be refused by :meth:`create` (empty if it would be accepted)."""
        return self.preview(spec, actor, check_quota=check_quota).errors

    def preview(
        self, spec: JobSpec, actor: Actor, *, count: int = 5, check_quota: bool = False,
        slack_from: NotifyTarget | None = None,
    ) -> Preview:
        """Validate ``spec`` as :meth:`create` would and preview its next ``count`` fires,
        without writing anything."""
        try:
            job = self._job_from_spec(spec, actor, self._clock(), slack_from=slack_from)
            self._check(job, actor, check_quota=check_quota, new_start=True)
        except ValidationError as e:
            return Preview([str(e)], [])
        return Preview([], self.next_runs(job, count))

    # --- validation -------------------------------------------------------------------------

    def _check(
        self, job: Job, actor: Actor, *, check_quota: bool, new_start: bool = False
    ) -> None:
        limits = limits_for(actor)
        now = self._clock()
        live = job.status in (STATUS_ACTIVE, STATUS_PAUSED)
        if not _ID_RE.match(job.id):
            raise ValidationError(f"bad job id {job.id!r} (lowercase letters, digits, '-', <=48)")
        if not job.name.strip() or len(job.name) > _MAX_NAME:
            raise ValidationError(f"name must be 1-{_MAX_NAME} characters")
        try:
            expr = expressions.parse(job.expression)
            expressions.zone(job.timezone)
        except expressions.ExpressionError as e:
            raise ValidationError(str(e)) from e
        if expr.kind != job.kind:
            raise ValidationError(f"kind {job.kind!r} does not match {job.expression!r}")
        starts = _instant("starts_at", job.starts_at)
        expires = _instant("expires_at", job.expires_at)
        if expr.kind == KIND_ONCE:
            if live and expr.at_time(job.timezone) <= now:
                raise ValidationError(f"{job.expression} in {job.timezone} is in the past")
            lead = timedelta(seconds=SCHEDULED_ONCE_LEAD_SECONDS)
            if live and self._scheduled_run() and expr.at_time(job.timezone) < now + lead:
                raise ValidationError(
                    f"a scheduled run may set a once job at least {SCHEDULED_ONCE_LEAD_SECONDS // 60} "
                    "minutes ahead"
                )
            if starts or expires:
                raise ValidationError("a once job cannot have starts_at or expires_at")
        else:
            interval = expr.min_interval_seconds() or 0
            if interval < MIN_INTERVAL_SECONDS:
                raise ValidationError(
                    f"{job.expression} runs more often than once an hour, the minimum interval"
                )
            if not expires:
                raise ValidationError(
                    f"recurring jobs need an expires_at (at most {RECURRING_TTL_DAYS} days away)"
                )
            if starts and expires and starts >= expires:
                raise ValidationError("starts_at must be before expires_at")
        if starts and new_start and starts <= now:
            raise ValidationError("starts_at is in the past")
        if expires:
            if expires <= now and live:
                raise ValidationError("expires_at is in the past")
            if expires > now + timedelta(days=RECURRING_TTL_DAYS, minutes=5):
                raise ValidationError(f"expires_at may be at most {RECURRING_TTL_DAYS} days away")
        if expr.kind != KIND_ONCE and live and not job.next_runs(now, 1):
            raise ValidationError(f"{job.expression} never fires before it expires")
        if not job.prompt.strip():
            raise ValidationError("prompt is empty")
        if len(job.prompt) > limits.max_prompt_chars:
            raise ValidationError(f"prompt is longer than {limits.max_prompt_chars} characters")
        if job.session_mode not in SESSION_MODES:
            raise ValidationError(f"session_mode must be one of {', '.join(SESSION_MODES)}")
        if job.skill and self._skills is not None:
            known = frozenset(self._skills())
            if job.skill not in known:
                raise ValidationError(f"unknown skill {job.skill!r}")
        # Exact under concurrency only through _write_counted: the write that follows this count
        # also advances the activation sequence read before it.
        if check_quota and job.status == STATUS_ACTIVE:
            jobs = self.store.list_jobs(consistent=True)
            active = sum(1 for j in jobs if j.id != job.id and j.status == STATUS_ACTIVE)
            if active >= self._max_active:
                raise ValidationError(
                    f"the team already has {active} active jobs (at most {self._max_active}); "
                    "pause or delete one first"
                )

    @staticmethod
    def _check_can_modify(job: Job, actor: Actor) -> None:
        """Every job belongs to the team: any actor its surface accepts may change any job."""
        limits_for(actor)

    @staticmethod
    def _require_trigger(trigger_id: str | None) -> None:
        if not trigger_id or not trigger_id.strip():
            raise ForbiddenError("A verified requester mention with triggerId is required")

    def _check_confirmation(self, job: Job, actor: Actor, trigger_id: str | None) -> None:
        if actor.namespace == "github":
            self._require_trigger(trigger_id)
        if not job.pending_trigger:
            return
        self._require_trigger(trigger_id)
        if (
            actor.namespace != "github"
            or not job.created_by.same_as(actor)
            or trigger_id == job.pending_trigger
            or str(trigger_id).startswith(SCHEDULED_TRIGGER_PREFIX)
        ):
            raise ForbiddenError("Job awaits confirmation in a NEW authenticated requester mention")

    @staticmethod
    def _expiry(kind: str, expires_at: str | None, now: datetime) -> str | None:
        """A recurring job's expiry: ``expires_at``, but never unset (default) and never later
        than :data:`RECURRING_TTL_DAYS` from ``now`` (clamped). A once job has none."""
        if kind == KIND_ONCE:
            return expires_at
        latest = now + timedelta(days=RECURRING_TTL_DAYS)
        if expires_at:
            try:
                if parse_iso(expires_at) <= latest:
                    return expires_at
            except ValueError:
                return expires_at  # _check reports the bad value
        return iso(latest)

    def _job_from_spec(
        self, spec: JobSpec, actor: Actor, now: datetime, *, slack_from: NotifyTarget | None = None,
    ) -> Job:
        try:
            kind = expressions.parse(spec.expression).kind
        except expressions.ExpressionError as e:
            raise ValidationError(str(e)) from e
        return Job(
            id=spec.id or f"{slug(spec.name)}-{secrets.token_hex(3)}",
            name=spec.name.strip(),
            kind=kind,
            expression=spec.expression.strip(),
            timezone=spec.timezone or "UTC",
            prompt=spec.prompt,
            skill=spec.skill or None,
            session_mode=spec.session_mode,
            notify=resolve_notify(spec.notify, slack_from=slack_from),
            status=STATUS_ACTIVE if spec.enabled else STATUS_PAUSED,
            starts_at=spec.starts_at or None,
            created_by=actor,
            updated_by=actor,
            created_at=iso(now),
            updated_at=iso(now),
            expires_at=self._expiry(kind, spec.expires_at or None, now),
            sync_state=SYNC_PENDING,
        )

    # --- writes -----------------------------------------------------------------------------

    def create(
        self, spec: JobSpec, actor: Actor, *,
        origin: Mapping[str, str] | None = None, trigger_id: str | None = None,
        slack_from: NotifyTarget | None = None,
    ) -> Job:
        """Create a job; ``slack_from`` is the Slack conversation the caller speaks from (see
        :func:`resolve_notify`)."""
        now = self._clock()
        job = self._job_from_spec(spec, actor, now, slack_from=slack_from)
        job = job.with_changes(origin=dict(origin) if origin else None)
        if actor.namespace == "github":
            self._require_trigger(trigger_id)
            job = job.with_changes(status=STATUS_PAUSED, pending_trigger=trigger_id)
        rev = self.store.latest_rev(job.id) + 1
        job = job.with_changes(version=rev)
        revision = self._revision(job, "create", actor, now)
        self._write_counted(
            job,
            lambda: self._check(job, actor, check_quota=True, new_start=True),
            lambda seen: self.store.create(job, revision, activation=seen),
        )
        return self._sync(job)

    def _write_counted(
        self, job: Job, check: Callable[[], None], write: Callable[[int | None], None]
    ) -> None:
        """``check`` (which counts active jobs) then ``write``. When ``job`` is active the write
        also advances the activation sequence read before the count, so a concurrent activation
        makes it fail; it is then re-counted and retried."""
        for attempt in range(_QUOTA_ROUNDS):
            seen = self.store.activation_seq() if job.status == STATUS_ACTIVE else None
            check()
            try:
                write(seen)
                return
            except ConflictError:
                if seen is None or attempt + 1 == _QUOTA_ROUNDS or self.store.activation_seq() == seen:
                    raise
                logger.info("schedules: another activation raced %r; re-checking the cap", job.id)

    def update(
        self,
        job_id: str,
        changes: Mapping[str, Any],
        actor: Actor,
        *,
        expected_version: int | None = None,
        trigger_id: str | None = None,
        slack_from: NotifyTarget | None = None,
    ) -> Job:
        """Change editable fields (``None`` clears ``skill`` / ``notify`` / ``starts_at`` /
        ``expires_at``; an agent's recurring job always keeps an expiry). A Slack ``notify``
        follows :func:`resolve_notify`."""
        job = self.get(job_id)
        self._check_can_modify(job, actor)
        return self._update(
            job, changes, actor, expected_version, "update", trigger_id=trigger_id, slack_from=slack_from
        )

    def _update(
        self,
        job: Job,
        changes: Mapping[str, Any],
        actor: Actor,
        expected_version: int | None,
        action: str,
        *,
        clear_window: bool = False,
        trigger_id: str | None = None,
        slack_from: NotifyTarget | None = None,
    ) -> Job:
        new, new_start, moved = self._prepare_update(job, changes, slack_from=slack_from)
        # Execution-affecting edits from GitHub must be confirmed by another mention.
        # Non-sensitive edits preserve any existing pending marker, never clear it.
        sensitive = set(changes) - {"name"}
        if sensitive and (actor.namespace == "github" or job.pending_trigger):
            self._require_trigger(trigger_id)
            if not job.created_by.same_as(actor):
                raise ForbiddenError("Only the requester may change a pending job")
            new = new.with_changes(status=STATUS_PAUSED, pending_trigger=trigger_id)
        return self._edit(
            job,
            new,
            action,
            actor,
            expected_version,
            check_quota=False,
            new_start=new_start,
            clear_window=clear_window or moved,
        )

    def _prepare_update(
        self, job: Job, changes: Mapping[str, Any], *, slack_from: NotifyTarget | None = None,
    ) -> tuple[Job, bool, bool]:
        unknown = set(changes) - _EDITABLE
        if unknown:
            raise ValidationError(f"cannot change {', '.join(sorted(unknown))}")
        values: dict[str, Any] = dict(changes)
        if job.notify is not None and job.notify.kind == "slack" and dispatch.is_slack_dm(job.notify.channel):
            # A stored DM target is never kept: any write clears it.
            values.setdefault("notify", None)
        if "notify" in values:
            values["notify"] = resolve_notify(values["notify"], keep=(job.notify,), slack_from=slack_from)
        if "expression" in values:
            try:
                values["kind"] = expressions.parse(str(values["expression"])).kind
            except expressions.ExpressionError as e:
                raise ValidationError(str(e)) from e
        for key in ("skill", "starts_at", "expires_at"):
            if key in values:
                values[key] = values[key] or None
        new = job.with_changes(**values)
        if new.kind == KIND_ONCE:
            new = new.with_changes(starts_at=None, expires_at=None)
        new_start = bool(values.get("starts_at")) and values["starts_at"] != job.starts_at
        # A new time (or zone) is a new series of fire windows.
        moved = (new.expression, new.timezone) != (job.expression, job.timezone)
        return new, new_start, moved

    def preview_update(
        self, job_id: str, changes: Mapping[str, Any], actor: Actor, *, count: int = 5
    ) -> Preview:
        """Validate an edit without changing its status, phase, or stored definition."""
        job = self.get(job_id)
        self._check_can_modify(job, actor)
        try:
            new, new_start, _ = self._prepare_update(job, changes)
            if new.status in (STATUS_ACTIVE, STATUS_PAUSED):
                new = new.with_changes(expires_at=self._expiry(new.kind, new.expires_at, self._clock()))
            self._check(new, actor, check_quota=False, new_start=new_start)
        except ValidationError as e:
            return Preview([str(e)], [])
        return Preview([], self.next_runs(new, count))

    @staticmethod
    def _check_version(job: Job, expected_version: int | None) -> None:
        if expected_version is not None and expected_version != job.version:
            raise ConflictError("the job changed since it was read; reload and retry")

    def pause(self, job_id: str, actor: Actor, *, expected_version: int | None = None) -> Job:
        """Stop the job firing. Pausing never validates the definition: a job outside the caller's
        surface limits, or a stale one, can always be stopped."""
        job = self.get(job_id)
        self._check_can_modify(job, actor)
        self._check_version(job, expected_version)
        if job.status == STATUS_PAUSED:
            return job
        if job.status != STATUS_ACTIVE:
            raise ValidationError(f"job {job_id!r} is {job.status}")
        new = job.with_changes(status=STATUS_PAUSED)
        return self._edit(job, new, "pause", actor, expected_version, check_quota=False, validate=False)

    def resume(
        self,
        job_id: str,
        actor: Actor,
        *,
        expires_at: str | None = None,
        expected_version: int | None = None,
        trigger_id: str | None = None,
    ) -> Job:
        """Re-enable a paused, expired or completed job (a once job needs a future time). A rate
        job resumes on its phase."""
        job = self.get(job_id)
        self._check_can_modify(job, actor)
        self._check_version(job, expected_version)
        if actor.namespace == "github" and expires_at and expires_at != job.expires_at:
            return self._update(
                job, {"expires_at": expires_at}, actor, expected_version, "update",
                trigger_id=trigger_id,
            )
        self._check_confirmation(job, actor, trigger_id)
        if job.status == STATUS_ACTIVE and not job.pending_trigger:
            return job
        now = self._clock()
        new = job.with_changes(status=STATUS_ACTIVE, pending_trigger=None)
        if expires_at:
            new = new.with_changes(expires_at=expires_at)
        elif job.is_expired(now):
            new = new.with_changes(expires_at=None)
        return self._edit(
            job,
            new,
            "resume",
            actor,
            expected_version,
            check_quota=True,
            clear_window=job.kind == KIND_ONCE,
        )

    def delete(
        self, job_id: str, actor: Actor, *, expected_version: int | None = None
    ) -> Revision:
        """Delete the job and its schedule; the revision history stays."""
        job = self.get(job_id)
        self._check_can_modify(job, actor)
        now = self._clock()
        rev = self._revision(job.with_changes(version=job.version + 1), "delete", actor, now)
        self.store.delete(
            job.id,
            rev,
            expected_version=job.version if expected_version is None else expected_version,
        )
        try:
            self.timer.delete(job.id)
        except Exception:  # noqa: BLE001 — reconcile removes the orphan
            logger.warning("schedules: deleting schedule %r failed", job.id, exc_info=True)
        return rev

    def revert(
        self, job_id: str, rev: int, actor: Actor, *, expected_version: int | None = None,
        trigger_id: str | None = None,
    ) -> Job:
        """Restore the definition saved in revision ``rev`` (re-creates a deleted job).

        ``expected_version`` is the job's version, or for a deleted job its latest revision (the
        delete). A deleted job may be restored by whoever could have changed it.
        """
        revisions = self.store.revisions(job_id)
        snapshot = next((r.snapshot for r in revisions if r.rev == rev), None)
        if snapshot is None:
            raise NotFoundError(f"job {job_id!r} has no revision {rev}")
        changes = {k: snapshot.get(k) for k in _EDITABLE if k != "starts_at"}
        job = self.store.get(job_id)
        snap_notify = NotifyTarget.from_item(snapshot.get("notify"))
        if snap_notify is not None and snap_notify.kind == "slack" and (
            job is None or job.notify != snap_notify or dispatch.is_slack_dm(snap_notify.channel)
        ):
            # A revision's Slack target comes back only as the job's current non-DM target;
            # otherwise the restored job reports to the home channel.
            changes["notify"] = None
        if job is not None:
            self._check_can_modify(job, actor)
            return self._update(
                job,
                changes,
                actor,
                expected_version,
                "revert",
                clear_window=job.kind == KIND_ONCE,
                trigger_id=trigger_id,
            )
        latest = revisions[0].rev if revisions else 0
        if expected_version is not None and expected_version != latest:
            raise ConflictError(f"job {job_id!r} is at revision {latest}, not {expected_version}")
        creator = next((r.actor for r in revisions if r.action == "create"), None)
        limits_for(actor)
        spec = JobSpec(
            id=job_id,
            name=str(snapshot.get("name") or job_id),
            expression=str(snapshot.get("expression", "")),
            prompt=str(snapshot.get("prompt", "")),
            timezone=str(snapshot.get("timezone") or "UTC"),
            skill=snapshot.get("skill"),
            session_mode=str(snapshot.get("session_mode") or SESSION_DAILY),
            notify=changes["notify"],
            starts_at=None,
            expires_at=snapshot.get("expires_at"),
            enabled=snapshot.get("status") != STATUS_PAUSED,
        )
        if any(r.snapshot.get("pending_trigger") for r in revisions):
            # A delete/restore cannot erase a pending security confirmation.
            raise ForbiddenError("A deleted pending job cannot be restored; create a new request")
        now = self._clock()
        restored = self._job_from_spec(spec, actor, now).with_changes(
            version=latest + 1, created_by=creator or actor, origin=snapshot.get("origin"),
        )
        original = next((r for r in revisions if r.action == "create"), None)
        if original is not None:
            restored = restored.with_changes(created_at=original.at)
        if actor.namespace == "github":
            self._require_trigger(trigger_id)
            restored = restored.with_changes(status=STATUS_PAUSED, pending_trigger=trigger_id)
        revision = self._revision(restored, "revert", actor, now)
        self._write_counted(
            restored,
            lambda: self._check(restored, actor, check_quota=True, new_start=True),
            lambda seen: self.store.create(restored, revision, activation=seen),
        )
        return self._sync(restored)

    def _edit(
        self,
        job: Job,
        new: Job,
        action: str,
        actor: Actor,
        expected_version: int | None,
        *,
        check_quota: bool,
        new_start: bool = False,
        clear_window: bool = False,
        validate: bool = True,
    ) -> Job:
        now = self._clock()
        if validate and new.status in (STATUS_ACTIVE, STATUS_PAUSED):
            new = new.with_changes(expires_at=self._expiry(new.kind, new.expires_at, now))
        # An edit that leaves the schedule as it is needs no Scheduler call.
        unchanged = (
            job.sync_state == SYNC_SYNCED
            and JobTimer.schedule_fields(new) == JobTimer.schedule_fields(job)
        )
        new = new.with_changes(
            version=job.version + 1,
            updated_by=actor,
            updated_at=iso(now),
            sync_state=SYNC_SYNCED if unchanged else SYNC_PENDING,
            sync_error=None,
            fire_window=None if clear_window else new.fire_window,
        )
        revision = self._revision(new, action, actor, now)

        def check() -> None:
            if validate:
                _check_kept_interval(job, new)
                self._check(new, actor, check_quota=check_quota, new_start=new_start)

        def write(seen: int | None) -> None:
            self.store.update(
                new,
                revision,
                expected_version=job.version if expected_version is None else expected_version,
                expected_status=job.status,
                clear_window=clear_window,
                activation=seen,
            )

        if validate and check_quota:
            self._write_counted(new, check, write)
        else:
            check()
            write(None)
        return new if unchanged else self._sync(new)

    def _revision(self, job: Job, action: str, actor: Actor, now: datetime) -> Revision:
        return Revision(job.id, job.version, action, actor, iso(now), job.definition(), self._via)

    def _push(self, job: Job) -> str | None:
        """Make the job's schedule match ``job``, retrying a few times; the last error, if any."""
        error: str | None = None
        for attempt in range(_SYNC_ATTEMPTS):
            try:
                if job.status in (STATUS_ACTIVE, STATUS_PAUSED):
                    self.timer.upsert(job)
                else:
                    self.timer.delete(job.id)
                return None
            except Exception as e:  # noqa: BLE001 — the row is the truth; reconcile retries
                error = f"{type(e).__name__}: {e}"
                if error_code(e) in _NO_RETRY or attempt + 1 == _SYNC_ATTEMPTS:
                    break
                self._sleep(_SYNC_BACKOFF_S * 2**attempt)
        logger.warning("schedules: syncing %r failed: %s", job.id, error)
        return error

    def _sync(self, job: Job) -> Job:
        """Make the job's schedule match the row; on failure mark the row ``unsynced``.

        If the row moved on meanwhile (a concurrent edit), the newer row is synced instead, so a
        slower writer never leaves its older definition on the schedule.
        """
        for _ in range(_SYNC_ROUNDS):
            error = self._push(job)
            state = SYNC_UNSYNCED if error else SYNC_SYNCED
            if self.store.set_sync(job.id, version=job.version, state=state, error=error):
                return job.with_changes(sync_state=state, sync_error=error)
            latest = self.store.get(job.id)
            if latest is None:
                self._drop_schedule(job.id)
                return job
            job = latest
        return job

    # --- runs -------------------------------------------------------------------------------

    def _config_or_load(self) -> Any:
        if self._config is None:
            from strandly.core.config import Config

            self._config = Config.load()
        return self._config

    def run_now(
        self, job_id: str, actor: Actor, *, trigger_id: str | None = None
    ) -> RunOutcome:
        """Launch the job immediately (any status), the same way a schedule fire does. The agent
        surface may run one job at most once per ``run_now_gap_seconds``."""
        if not dispatch.schedules_enabled():
            raise ForbiddenError("scheduled jobs are globally disabled")
        if self._scheduled_run():
            # Run-now chains would bypass the minimum interval; a once job is the paced equivalent.
            raise ForbiddenError("a scheduled run may not run a job now; create a once job instead")
        job = self.get(job_id)
        self._check_can_modify(job, actor)
        self._check_confirmation(job, actor, trigger_id)
        if job.pending_trigger:
            job = self._edit(
                job, job.with_changes(pending_trigger=None), "confirm", actor,
                job.version, check_quota=False,
            )
        now = self._clock()
        gap = limits_for(actor).run_now_gap_seconds
        if gap and not self.store.claim_manual_run(
            job.id, at=iso(now), not_since=iso(now - timedelta(seconds=gap))
        ):
            raise ValidationError(
                f"job {job_id!r} was run by hand less than {gap // 60} minutes ago"
            )
        outcome = self._launcher(
            job, self._config_or_load(), now=now,
            trigger_source=f"scheduled:{job.id}:manual",
        )
        try:
            self.store.record_run(
                job.id, LastRun(iso(now), outcome.status, outcome.session_id, trigger="manual")
            )
        except Exception:  # noqa: BLE001 — recording failure cannot undo a dispatched launch
            logger.exception("schedules: run_now %r returned %s but recording failed", job.id, outcome.status)
        return outcome

    def launch(self, job_id: str) -> RunOutcome:
        """Launch the job now as a manual run, with no guard and no ``last_run`` record: the
        invoker's half of a ``run_now`` made through :func:`dispatch.remote_launcher` (the caller
        guards and records)."""
        job = self.get(job_id)
        if job.pending_trigger:
            raise ForbiddenError("Job awaits confirmation in a new requester mention")
        return self._launcher(
            job, self._config_or_load(), now=self._clock(),
            trigger_source=f"scheduled:{job.id}:manual",
        )

    def fire(
        self,
        job_id: str,
        *,
        scheduled_time: str | None = None,
        late: bool = False,
    ) -> str:
        """Handle one schedule fire and return its outcome.

        The table wins: a missing, paused, completed or expired job does not run, nor does a once
        job fired by a schedule for another time. Each fire window (see ``Job.window_for``)
        dispatches at most once, and never after a later one. ``late`` is reconcile running a once
        job whose schedule never got created.
        """
        if not dispatch.schedules_enabled():
            return dispatch.DISABLED
        job = self.store.get(job_id)
        if job is None:
            self._drop_schedule(job_id)
            return "missing"
        now = self._clock()
        if job.pending_trigger:
            return "pending-confirmation"
        if job.status in (STATUS_COMPLETED, STATUS_EXPIRED):
            self._drop_schedule(job.id)
            return f"skipped-{job.status}"
        if job.status != STATUS_ACTIVE:
            return f"skipped-{job.status}"
        if job.is_expired(now):
            self.store.set_status(
                job.id, STATUS_EXPIRED, expected=(STATUS_ACTIVE,), version=job.version
            )
            self._drop_schedule(job.id)
            return "expired"
        when = now
        if scheduled_time:
            try:
                when = parse_iso(scheduled_time)
            except ValueError:
                logger.warning("schedules: bad scheduled_time %r; using now", scheduled_time)
        if job.kind == KIND_ONCE and scheduled_time and not late:
            at = job.parsed().at_time(job.timezone)
            if abs((when - at).total_seconds()) > _ONCE_SLACK.total_seconds():
                logger.warning(
                    "schedules: once job %r fired for %s but is set for %s; skipped",
                    job.id,
                    iso(when),
                    iso(at),
                )
                return "skipped-stale"
        claim = LastRun(iso(now), "dispatching", job.session_id(now))
        if not self.store.claim(job.id, window=job.window_for(when), last_run=claim):
            logger.info("schedules: job %r already fired in this window; skipped", job.id)
            return "duplicate"
        outcome = self._launcher(
            job,
            self._config_or_load(),
            now=now,
            trigger_source=f"scheduled:{job.id}" + (":late" if late else ""),
        )
        trigger = "late" if late else "schedule"
        self.store.record_run(
            job.id, LastRun(iso(now), outcome.status, outcome.session_id, trigger=trigger)
        )
        if job.kind == KIND_ONCE:
            self.store.set_status(
                job.id, STATUS_COMPLETED, expected=(STATUS_ACTIVE,), version=job.version
            )
        return outcome.status

    def _drop_schedule(self, job_id: str) -> None:
        try:
            self.timer.delete(job_id)
        except Exception:  # noqa: BLE001 — reconcile removes the orphan
            logger.warning("schedules: deleting schedule %r failed", job_id, exc_info=True)

    # --- reconcile --------------------------------------------------------------------------

    def reconcile(self, *, only_unsynced: bool = False, max_late: int = 1) -> ReconcileReport:
        """Make the jobs group match the table: create missing schedules, fix drift, delete
        orphans and the schedules of finished jobs, settle expired / past once jobs, and run a
        once job late (once) when its time passed before its schedule could be created.

        ``only_unsynced`` is the cheap pass (one table scan, no ListSchedules, no orphan
        deletion): it re-syncs only rows not marked ``synced``. At most ``max_late`` late runs
        are launched per pass (the invoker has time for one launch); the rest wait for the next.
        """
        report = ReconcileReport()
        self._late_left = max_late
        now = self._clock()
        live = None if only_unsynced else set(self.timer.names())
        jobs = self.store.list_jobs(consistent=True)
        for job in jobs:
            if only_unsynced and job.sync_state == SYNC_SYNCED:
                continue
            try:
                self._reconcile_job(job, now, live, report)
            except Exception as e:  # noqa: BLE001 — one bad job never stops the pass
                error = f"{type(e).__name__}: {e}"
                report.errors[job.id] = error
                logger.warning("schedules: reconcile of %r failed: %s", job.id, error)
                try:
                    self.store.set_sync(job.id, version=job.version, state=SYNC_UNSYNCED, error=error)
                except Exception:  # noqa: BLE001
                    logger.warning("schedules: marking %r unsynced failed", job.id, exc_info=True)
        if live is not None:
            self._delete_orphans(live - {j.id for j in jobs}, report)
        logger.info("schedules: reconcile %s", report.as_dict())
        return report

    def _reconcile_job(
        self, job: Job, now: datetime, live: set[str] | None, report: ReconcileReport
    ) -> None:
        job = self._settle(job, now, live, report)
        in_live = live is None or job.id in live
        if job.status not in (STATUS_ACTIVE, STATUS_PAUSED):
            if live is None or job.id in live:
                if self.timer.delete(job.id):
                    report.deleted.append(job.id)
        elif job.kind == KIND_ONCE and job.parsed().at_time(job.timezone) <= now:
            return  # its schedule fired (or is firing); nothing to create
        elif not in_live:
            self.timer.upsert(job)
            report.created.append(job.id)
        else:
            actual = self.timer.get(job.id)
            if actual is None or self.timer.drifted(job, actual):
                self.timer.upsert(job)
                (report.created if actual is None else report.updated).append(job.id)
        if job.sync_state != SYNC_SYNCED:
            self.store.set_sync(job.id, version=job.version, state=SYNC_SYNCED)

    def _settle(
        self, job: Job, now: datetime, live: set[str] | None, report: ReconcileReport
    ) -> Job:
        """Apply time-driven status changes the invoker may have missed.

        A once job whose time passed with no schedule: if its schedule was never created
        (the row is not ``synced``) and it never ran, it runs now, late; otherwise it is settled
        as completed (it ran) or expired (it was scheduled but never ran).
        """
        if job.status in (STATUS_ACTIVE, STATUS_PAUSED) and job.is_expired(now):
            if self.store.set_status(
                job.id, STATUS_EXPIRED, expected=(job.status,), version=job.version
            ):
                report.expired.append(job.id)
            return job.with_changes(status=STATUS_EXPIRED)
        if job.kind != KIND_ONCE or job.status != STATUS_ACTIVE:
            return job
        at = job.parsed().at_time(job.timezone)
        if at > now or (live is not None and job.id in live):
            return job
        if job.sync_state != SYNC_SYNCED and job.last_run is None:
            if self._late_left <= 0:
                report.fired_late[job.id] = "deferred"
                return job
            self._late_left -= 1
            outcome = self.fire(job.id, scheduled_time=iso(at), late=True)
            report.fired_late[job.id] = outcome
            return self.store.get(job.id) or job.with_changes(status=STATUS_COMPLETED)
        if live is not None and at < now - _ONCE_GRACE:
            status = STATUS_COMPLETED if job.last_run else STATUS_EXPIRED
            if self.store.set_status(job.id, status, expected=(STATUS_ACTIVE,), version=job.version):
                (report.completed if status == STATUS_COMPLETED else report.expired).append(job.id)
            return job.with_changes(status=status)
        return job

    def _delete_orphans(self, names: set[str], report: ReconcileReport) -> None:
        """Delete schedules with no job row, only in a ``-jobs`` group and only after a
        consistent read confirms the row is gone."""
        if not names:
            return
        if not self.timer.group.endswith(JOBS_GROUP_SUFFIX):
            logger.error(
                "schedules: group %r is not a jobs group; leaving %d orphan schedule(s)",
                self.timer.group,
                len(names),
            )
            report.errors["orphans"] = f"group {self.timer.group!r} is not a jobs group"
            return
        for name in sorted(names):
            try:
                if self.store.get(name) is None and self.timer.delete(name):
                    report.deleted.append(name)
            except Exception as e:  # noqa: BLE001
                report.errors[name] = f"{type(e).__name__}: {e}"


__all__ = [
    "ConflictError",
    "ForbiddenError",
    "JobSpec",
    "Limits",
    "LIMITS",
    "MIN_INTERVAL_SECONDS",
    "SCHEDULED_ONCE_LEAD_SECONDS",
    "NotFoundError",
    "Preview",
    "ReconcileReport",
    "ScheduleService",
    "ScheduleSettings",
    "ValidationError",
]
