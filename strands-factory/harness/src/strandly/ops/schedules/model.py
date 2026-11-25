"""The scheduled-job record: fields, DynamoDB item shape, prompt and session id."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from strandly.ops.schedules import expressions

KIND_ONCE = expressions.ONCE
KIND_RECURRING = expressions.RECURRING

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"
STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED, STATUS_EXPIRED)

SESSION_DAILY = "daily"
SESSION_SINGLE = "single"
SESSION_PER_RUN = "per-run"
SESSION_MODES = (SESSION_DAILY, SESSION_SINGLE, SESSION_PER_RUN)

SURFACE_AGENT = "agent"
SURFACE_DASHBOARD = "dashboard"
SURFACE_SYSTEM = "system"
SURFACES = (SURFACE_AGENT, SURFACE_DASHBOARD, SURFACE_SYSTEM)

# Actor ids are namespaced by where the identity comes from: ``slack:U123``, ``portal:alice``,
# ``person:example-maintainer`` (a people-directory row, advisory), ``github:octocat``,
# ``github-actions:<repository id>``, ``system:seed``, ``iam:direct-invoke``.
ACTOR_NAMESPACES = ("slack", "portal", "person", "github", "github-actions", "system", "iam")
# Every job belongs to the team. ``created_by``/``updated_by`` and the revisions record who asked,
# for audit only; nothing authorizes on them. A scheduled run acts as this team actor.
TEAM_ACTOR_ID = "system:team"
TEAM_ISSUER = "strandly"

SYNC_SYNCED = "synced"
SYNC_PENDING = "pending"
SYNC_UNSYNCED = "unsynced"

JOB_SK = "job"
REV_PREFIX = "rev#"

# Fields a revision snapshots (the job's definition, not its runtime state).
DEFINITION_FIELDS = (
    "name",
    "kind",
    "expression",
    "timezone",
    "prompt",
    "skill",
    "session_mode",
    "notify",
    "status",
    "starts_at",
    "expires_at",
    "origin",
    "pending_trigger",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def rev_sk(n: int) -> str:
    return f"{REV_PREFIX}{n:06d}"


@dataclass(frozen=True)
class Actor:
    """Who made a change (a namespaced id, see :data:`ACTOR_NAMESPACES`) and through which
    surface (agent | dashboard | system)."""

    actor: str
    surface: str
    issuer: str = ""

    @property
    def namespace(self) -> str:
        ns, sep, rest = self.actor.partition(":")
        return ns if sep and rest else ""

    def same_identity(self, other: Actor) -> bool:
        """Exact issuer + namespaced subject; never compare mutable aliases or email."""
        return self.actor == other.actor and self.issuer == other.issuer

    def same_as(self, other: Actor) -> bool:
        return self.same_identity(other) and self.surface == other.surface

    def to_item(self) -> dict[str, str]:
        item = {"actor": self.actor, "surface": self.surface}
        if self.issuer:
            item["issuer"] = self.issuer
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any] | None) -> Actor:
        item = item or {}
        return cls(
            str(item.get("actor", "")), str(item.get("surface", "")), str(item.get("issuer", ""))
        )


@dataclass(frozen=True)
class NotifyTarget:
    """Where a run posts its result: a Slack channel (optionally a thread) or a GitHub thread."""

    kind: str  # "slack" | "github"
    channel: str | None = None
    thread_ts: str | None = None
    url: str | None = None

    def instruction(self) -> str:
        if self.kind == "slack":
            where = f"Slack channel {self.channel}"
            if self.thread_ts:
                where += f" (thread {self.thread_ts})"
            return f"When you are done, post your result to {where} using use_slack."
        return f"When you are done, post your result as a comment on {self.url} using use_github."

    def to_item(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}

    @classmethod
    def from_item(cls, item: dict[str, Any] | None) -> NotifyTarget | None:
        if not item:
            return None
        return cls(
            kind=str(item.get("kind", "")),
            channel=item.get("channel"),
            thread_ts=item.get("thread_ts"),
            url=item.get("url"),
        )


@dataclass(frozen=True)
class LastRun:
    time: str
    status: str
    session_id: str
    trigger: str = "schedule"

    def to_item(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any] | None) -> LastRun | None:
        if not item:
            return None
        return cls(
            time=str(item.get("time", "")),
            status=str(item.get("status", "")),
            session_id=str(item.get("session_id", "")),
            trigger=str(item.get("trigger", "schedule")),
        )


@dataclass(frozen=True)
class Job:
    """One scheduled job (the ``sk = "job"`` item of the schedules table)."""

    id: str
    name: str
    kind: str
    expression: str
    timezone: str
    prompt: str
    created_by: Actor
    updated_by: Actor
    created_at: str
    updated_at: str
    skill: str | None = None
    session_mode: str = SESSION_DAILY
    notify: NotifyTarget | None = None
    status: str = STATUS_ACTIVE
    starts_at: str | None = None
    expires_at: str | None = None
    version: int = 1
    last_run: LastRun | None = None
    fire_window: str | None = None
    manual_run_at: str | None = None
    sync_state: str = SYNC_PENDING
    sync_error: str | None = None
    origin: dict[str, str] | None = None
    pending_trigger: str | None = None

    @property
    def enabled(self) -> bool:
        return self.status == STATUS_ACTIVE

    def parsed(self) -> expressions.Expression:
        return expressions.parse(self.expression)

    def is_expired(self, now: datetime) -> bool:
        return self.kind == KIND_RECURRING and bool(self.expires_at) and (
            parse_iso(str(self.expires_at)) <= now
        )

    def session_id(self, now: datetime) -> str:
        """The run's session: one per UTC day, one for ever, or one per run."""
        now = now.astimezone(timezone.utc)
        if self.session_mode == SESSION_SINGLE:
            return f"sched-{self.id}"
        if self.session_mode == SESSION_PER_RUN:
            return f"sched-{self.id}-{now:%Y%m%d-%H%M%S}"
        return f"sched-{self.id}-{now:%Y%m%d}"

    def run_prompt(self) -> str:
        """The prompt sent to the runtime: skill activation, the job prompt, the run's own job
        id, the notify line."""
        parts = []
        if self.skill:
            parts.append(f'skills(skill_name="{self.skill}")')
        parts.append(self.prompt)
        parts.append(self.own_job_line())
        if self.notify:
            parts.append(self.notify.instruction())
        return "\n\n".join(parts)

    def own_job_line(self) -> str:
        """Tells the run which job it belongs to, so it can stop that job itself."""
        return (
            f"This run belongs to scheduled job {self.id}. When the job's goal is done or it "
            'should stop, pause it: schedule(action="pause").'
        )

    def rate_anchor(self) -> datetime | None:
        """The phase of a rate job: ``starts_at``, else ``created_at``."""
        for text in (self.starts_at, self.created_at):
            if text:
                try:
                    return parse_iso(text)
                except ValueError:
                    continue
        return None

    def next_runs(self, after: datetime, count: int = 5) -> list[datetime]:
        """The job's next fire times as its schedule would run them (raises ``ExpressionError``)."""
        expr = self.parsed()
        if self.kind == KIND_ONCE:
            return expr.next_runs(self.timezone, after, count)
        return expr.next_runs(
            self.timezone,
            after,
            count,
            anchor=self.rate_anchor() if expr.rate_seconds else None,
            not_before=parse_iso(self.starts_at) if self.starts_at else None,
            until=parse_iso(self.expires_at) if self.expires_at else None,
        )

    def window_for(self, when: datetime) -> str:
        """The dedupe key of a fire at ``when``: a fire runs only if its window is later than the
        last one taken (raises ``ExpressionError`` for an unknown timezone).

        Once jobs have one window per ``at()``. Rate jobs round ``when`` to their nearest slot
        (``anchor + k * period``, in UTC): the slot's UTC date when they fire at most daily, else
        the slot itself. Cron jobs that fire at most daily use the local date; faster ones the
        UTC minute.
        """
        if self.kind == KIND_ONCE:
            return f"once:{self.expression}"
        expr = self.parsed()
        when = when.astimezone(timezone.utc)
        if expr.rate_seconds is not None:
            step = timedelta(seconds=expr.rate_seconds)
            anchor = self.rate_anchor() or datetime(1970, 1, 1, tzinfo=timezone.utc)
            slot = anchor + step * round((when - anchor) / step)
            return slot.date().isoformat() if expr.rate_seconds >= 86400 else iso(slot)
        if (expr.min_interval_seconds() or 86400) >= 86400:
            return when.astimezone(expressions.zone(self.timezone)).date().isoformat()
        return iso(when.replace(second=0, microsecond=0))

    def definition(self) -> dict[str, Any]:
        item = self.to_item()
        return {k: item[k] for k in DEFINITION_FIELDS if k in item}

    def to_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.id,
            "sk": JOB_SK,
            "name": self.name,
            "kind": self.kind,
            "expression": self.expression,
            "timezone": self.timezone,
            "prompt": self.prompt,
            "session_mode": self.session_mode,
            "status": self.status,
            "enabled": self.enabled,
            "created_by": self.created_by.to_item(),
            "updated_by": self.updated_by.to_item(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "sync_state": self.sync_state,
        }
        optional: dict[str, Any] = {
            "skill": self.skill,
            "notify": self.notify.to_item() if self.notify else None,
            "starts_at": self.starts_at,
            "expires_at": self.expires_at,
            "last_run": self.last_run.to_item() if self.last_run else None,
            "fire_window": self.fire_window,
            "manual_run_at": self.manual_run_at,
            "sync_error": self.sync_error,
            "origin": self.origin,
            "pending_trigger": self.pending_trigger,
        }
        item.update({k: v for k, v in optional.items() if v})
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Job:
        return cls(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            kind=str(item.get("kind", KIND_RECURRING)),
            expression=str(item["expression"]),
            timezone=str(item.get("timezone") or "UTC"),
            prompt=str(item.get("prompt", "")),
            skill=item.get("skill") or None,
            session_mode=str(item.get("session_mode") or SESSION_DAILY),
            notify=NotifyTarget.from_item(item.get("notify")),
            status=str(item.get("status") or STATUS_ACTIVE),
            created_by=Actor.from_item(item.get("created_by")),
            updated_by=Actor.from_item(item.get("updated_by")),
            created_at=str(item.get("created_at", "")),
            updated_at=str(item.get("updated_at", "")),
            starts_at=item.get("starts_at") or None,
            expires_at=item.get("expires_at") or None,
            version=int(item.get("version", 1)),
            last_run=LastRun.from_item(item.get("last_run")),
            fire_window=item.get("fire_window") or None,
            manual_run_at=item.get("manual_run_at") or None,
            sync_state=str(item.get("sync_state") or SYNC_PENDING),
            sync_error=item.get("sync_error") or None,
            origin=dict(item["origin"]) if isinstance(item.get("origin"), dict) else None,
            pending_trigger=item.get("pending_trigger") or None,
        )

    def with_changes(self, **changes: Any) -> Job:
        return replace(self, **changes)


@dataclass(frozen=True)
class Revision:
    """One ``rev#n`` item: the job definition after an edit, who made it, and ``via``: the
    scheduled run it came from, when a job's own run made it (``job_id``, ``session_id``,
    ``trigger``)."""

    job_id: str
    rev: int
    action: str
    actor: Actor
    at: str
    snapshot: dict[str, Any]
    via: dict[str, str] | None = None

    def to_item(self) -> dict[str, Any]:
        item = {
            "id": self.job_id,
            "sk": rev_sk(self.rev),
            "rev": self.rev,
            "action": self.action,
            "actor": self.actor.to_item(),
            "at": self.at,
            "snapshot": self.snapshot,
        }
        if self.via:
            item["via"] = dict(self.via)
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Revision:
        via = item.get("via")
        return cls(
            job_id=str(item["id"]),
            rev=int(item["rev"]),
            action=str(item.get("action", "")),
            actor=Actor.from_item(item.get("actor")),
            at=str(item.get("at", "")),
            snapshot=dict(item.get("snapshot") or {}),
            via={str(k): str(v) for k, v in via.items()} if isinstance(via, dict) and via else None,
        )
