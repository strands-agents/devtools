"""The schedules service over the real JobStore / JobTimer, with in-memory AWS clients."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from fake_schedules import FakeDynamoDB, FakeScheduler
from strandly.core.config import Config
from strandly.ops.schedules import dispatch
from strandly.ops.schedules import service as service_module
from strandly.ops.schedules.dispatch import RunOutcome
from strandly.ops.schedules.model import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_PAUSED,
    SYNC_SYNCED,
    SYNC_UNSYNCED,
    Actor,
    LastRun,
    NotifyTarget,
    Revision,
    iso,
)
from strandly.ops.schedules.service import (
    ConflictError,
    ForbiddenError,
    JobSpec,
    NotFoundError,
    ScheduleService,
    ScheduleSettings,
    ValidationError,
    builtin_skill_names,
)
from strandly.ops.schedules.store import JobStore
from strandly.ops.schedules.timer import RECONCILE_SCHEDULE, JobTimer, TimerSettings

UTC = timezone.utc
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
GROUP = "strandly-test-jobs"
DASH = Actor("portal:alice", "dashboard")
AGENT = Actor("slack:U0BOB", "agent")
TEAM = Actor("system:team", "agent", "strandly")


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Env:
    def __init__(self) -> None:
        self.ddb = FakeDynamoDB()
        self.clock = Clock()
        self.scheduler = FakeScheduler(clock=self.clock)
        self.launches: list[dict] = []
        self.outcome = "dispatched"
        self.sleeps: list[float] = []
        self.service = ScheduleService(
            JobStore(self.ddb, "strandly-test-schedules"),
            JobTimer(
                self.scheduler,
                TimerSettings(GROUP, "arn:fn:invoker", "arn:role:jobs"),
                clock=self.clock,
            ),
            skills=lambda: {"parity-sync", "test-simplify"},
            clock=self.clock,
            config=object(),
            launcher=self._launch,
            sleep=self.sleeps.append,
        )

    def _launch(self, job, config, *, now, trigger_source):
        self.launches.append(
            {"id": job.id, "prompt": job.run_prompt(), "trigger": trigger_source,
             "session": job.session_id(now), "job": job}
        )
        return RunOutcome(self.outcome, job.session_id(now), task_id="t1")

    def schedule(self, job_id: str) -> dict | None:
        return self.scheduler.schedules.get((GROUP, job_id))

    def row(self, job_id: str) -> dict:
        return self.ddb.items[(job_id, "job")]

    def revs(self, job_id: str) -> list[dict]:
        return [v for (i, sk), v in sorted(self.ddb.items.items()) if i == job_id and sk != "job"]


@pytest.fixture
def env() -> Env:
    return Env()


def spec(**kw) -> JobSpec:
    base = dict(id="daily", name="Daily", expression="rate(1 day)", prompt="do the thing")
    base.update(kw)
    return JobSpec(**base)


# --- create ---------------------------------------------------------------------------------


def test_create_recurring_writes_row_revision_and_schedule(env):
    job = env.service.create(spec(skill="parity-sync", timezone="Europe/Istanbul"), DASH)
    assert job.sync_state == SYNC_SYNCED and job.version == 1
    row = env.row("daily")
    assert row["kind"] == "recurring" and row["status"] == STATUS_ACTIVE and row["enabled"] is True
    assert row["created_by"] == {"actor": "portal:alice", "surface": "dashboard"}
    assert row["sync_state"] == SYNC_SYNCED and row["expires_at"] == iso(NOW + timedelta(days=90))
    (rev,) = env.revs("daily")
    assert rev["sk"] == "rev#000001" and rev["action"] == "create"
    assert rev["snapshot"]["prompt"] == "do the thing" and rev["snapshot"]["skill"] == "parity-sync"
    sched = env.schedule("daily")
    assert sched["ScheduleExpression"] == "rate(1 day)"
    assert sched["ScheduleExpressionTimezone"] == "Europe/Istanbul"
    assert sched["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert sched["State"] == "ENABLED" and sched["ActionAfterCompletion"] == "NONE"
    assert sched["Target"]["Arn"] == "arn:fn:invoker" and sched["Target"]["RoleArn"] == "arn:role:jobs"
    assert json.loads(sched["Target"]["Input"]) == {
        "job_id": "daily",
        "scheduled_time": "<aws.scheduler.scheduled-time>",
    }


def test_create_once_uses_at_with_delete_after_completion(env):
    env.service.create(spec(id="remind", expression="at(2030-01-02T09:00:00)"), DASH)
    sched = env.schedule("remind")
    assert sched["ScheduleExpression"] == "at(2030-01-02T09:00:00)"
    assert sched["ActionAfterCompletion"] == "DELETE" and "EndDate" not in sched
    assert env.row("remind")["kind"] == "once"


def test_once_time_in_the_past_is_rejected_in_its_timezone(env):
    with pytest.raises(ValidationError, match="past"):
        env.service.create(spec(expression="at(2029-12-31T09:00:00)"), DASH)
    # 14:00 in Istanbul is 11:00 UTC, already past; 14:00 in New York is still ahead.
    with pytest.raises(ValidationError, match="past"):
        env.service.create(spec(expression="at(2030-01-01T14:00:00)", timezone="Europe/Istanbul"), DASH)
    env.service.create(spec(expression="at(2030-01-01T14:00:00)", timezone="America/New_York"), DASH)


def test_created_id_is_derived_from_the_name_when_not_given(env):
    job = env.service.create(spec(id=None, name="Weekly test sweep!"), DASH)
    assert job.id.startswith("weekly-test-sweep-") and env.schedule(job.id)


def test_create_paused(env):
    job = env.service.create(spec(enabled=False), DASH)
    assert job.status == STATUS_PAUSED and env.schedule("daily")["State"] == "DISABLED"


def test_duplicate_id_conflicts(env):
    env.service.create(spec(), DASH)
    with pytest.raises(ConflictError):
        env.service.create(spec(), DASH)


@pytest.mark.parametrize(
    ("actor", "expression", "ok"),
    [
        (AGENT, "rate(30 minutes)", False),
        (AGENT, "cron(0/30 * * * ? *)", False),
        (AGENT, "rate(1 hour)", True),
        (TEAM, "rate(59 minutes)", False),
        (TEAM, "rate(1 hour)", True),
        (DASH, "rate(15 minutes)", False),
        (DASH, "rate(1 hour)", True),
        (Actor("system:seed", "system"), "rate(15 minutes)", False),
        (Actor("system:seed", "system"), "cron(0 * * * ? *)", True),
    ],
)
def test_the_one_hour_minimum_interval_holds_on_every_surface(env, actor, expression, ok):
    if ok:
        env.service.create(spec(expression=expression), actor)
    else:
        with pytest.raises(ValidationError, match="more often than once an hour"):
            env.service.create(spec(expression=expression), actor)


def _legacy_fast_job(env, monkeypatch, job_id: str, expression: str = "rate(15 minutes)"):
    """A job saved when the minimum was shorter."""
    with monkeypatch.context() as m:
        m.setattr(service_module, "MIN_INTERVAL_SECONDS", 900)
        return env.service.create(spec(id=job_id, expression=expression), DASH)


def test_a_sub_hour_job_keeps_firing_but_edits_that_keep_its_interval_are_refused(env, monkeypatch):
    job = _legacy_fast_job(env, monkeypatch, "fast")
    assert env.schedule("fast")["ScheduleExpression"] == "rate(15 minutes)"
    env.clock.now = NOW + timedelta(minutes=15)
    assert env.service.fire("fast") == "dispatched"
    assert len(env.launches) == 1
    for actor in (DASH, AGENT, TEAM):
        with pytest.raises(ValidationError, match="more often than the one-hour minimum.*paused or deleted"):
            env.service.update("fast", {"name": "renamed"}, actor)
    assert env.service.get("fast").version == job.version
    moved = env.service.update("fast", {"expression": "rate(1 hour)"}, DASH)
    assert moved.expression == "rate(1 hour)"
    assert env.schedule("fast")["ScheduleExpression"] == "rate(1 hour)"
    with pytest.raises(ValidationError, match="more often than once an hour"):
        env.service.update("fast", {"expression": "rate(30 minutes)"}, DASH)


def test_a_sub_hour_job_can_be_paused_and_deleted_but_not_resumed(env, monkeypatch):
    _legacy_fast_job(env, monkeypatch, "fast")
    assert env.service.pause("fast", DASH).status == STATUS_PAUSED
    with pytest.raises(ValidationError, match="more often than the one-hour minimum"):
        env.service.resume("fast", DASH)
    env.service.delete("fast", DASH)
    assert env.service.list_jobs() == []


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"expression": "rate(1 week)"}, "rate"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"skill": "nope"}, "unknown skill"),
        ({"prompt": "   "}, "empty"),
        ({"prompt": "x" * 20001}, "longer"),
        ({"session_mode": "weekly"}, "session_mode"),
        ({"id": "Bad_Id"}, "bad job id"),
        ({"name": ""}, "name"),
        ({"notify": {"kind": "slack", "channel": "#general"}}, "Slack channel"),
        ({"notify": {"kind": "github", "url": "https://example.com/x"}}, "GitHub"),
        ({"notify": {"kind": "email"}}, "notify kind"),
        ({"expression": "cron(0 9 1 1 ? 2020)"}, "never fires"),
        ({"expires_at": "2029-01-01T00:00:00Z"}, "past"),
        ({"starts_at": "2029-01-01T00:00:00Z"}, "starts_at is in the past"),
        ({"expression": "at(2030-02-01T00:00:00)", "starts_at": "2030-01-05T00:00:00Z"}, "once"),
        ({"expression": "at(2030-02-01T00:00:00)", "expires_at": "2030-03-01T00:00:00Z"}, "once"),
    ],
)
def test_validation_errors(env, changes, match):
    with pytest.raises(ValidationError, match=match):
        env.service.create(spec(**changes), DASH)
    assert not env.ddb.items and not env.scheduler.schedules


def test_starts_at_sets_the_schedules_start_date(env):
    env.service.create(spec(starts_at="2030-01-03T08:00:00Z"), DASH)
    assert env.schedule("daily")["StartDate"] == datetime(2030, 1, 3, 8, tzinfo=UTC)
    env.service.update("daily", {"starts_at": None}, DASH)
    # A rate job without starts_at is phased from its creation.
    assert env.schedule("daily")["StartDate"] == NOW + timedelta(days=1)
    assert "starts_at" not in env.row("daily")
    with pytest.raises(ValidationError, match="starts_at is in the past"):
        env.service.update("daily", {"starts_at": "2029-06-01T00:00:00Z"}, DASH)


def test_agent_prompt_limit_is_tighter(env):
    with pytest.raises(ValidationError, match="longer than 8000"):
        env.service.create(spec(prompt="x" * 8001), AGENT)


@pytest.mark.parametrize("actor", [AGENT, DASH])
def test_recurring_jobs_expire_within_90_days_on_every_surface(env, actor):
    job = env.service.create(spec(), actor)
    assert job.expires_at == iso(NOW + timedelta(days=90))
    assert env.schedule("daily")["EndDate"] == NOW + timedelta(days=90)
    later = env.service.create(spec(id="later", expires_at=iso(NOW + timedelta(days=120))), actor)
    assert later.expires_at == iso(NOW + timedelta(days=90))
    sooner = env.service.create(spec(id="sooner", expires_at=iso(NOW + timedelta(days=7))), actor)
    assert sooner.expires_at == iso(NOW + timedelta(days=7))
    once = env.service.create(spec(id="once", expression="at(2030-01-05T00:00:00)"), actor)
    assert once.expires_at is None


def test_the_team_holds_at_most_100_active_jobs_across_surfaces(env):
    for i in range(50):
        env.service.create(spec(id=f"a{i}"), AGENT)
    for i in range(49):
        env.service.create(spec(id=f"d{i}"), DASH)
    _run_of(env, "a0").create(spec(id="s0"), TEAM)
    for actor in (AGENT, DASH, TEAM, Actor("iam:direct-invoke", "dashboard")):
        with pytest.raises(ValidationError, match="at most 100"):
            env.service.create(spec(id="over"), actor)
    # A paused job frees a slot, and resuming one needs a free slot again.
    env.service.pause("a0", DASH)
    env.service.create(spec(id="over"), AGENT)
    with pytest.raises(ValidationError, match="at most 100"):
        env.service.resume("a0", DASH)
    # Paused creates do not count.
    assert env.service.create(spec(id="idle", enabled=False), DASH).status == STATUS_PAUSED


def test_the_team_cap_comes_from_the_settings(env, monkeypatch):
    base = {
        "STRANDLY_SCHEDULES_TABLE": "t", "STRANDLY_SCHEDULES_GROUP": "g",
        "STRANDLY_SCHEDULES_TARGET_ARN": "arn:fn", "STRANDLY_SCHEDULES_ROLE_ARN": "arn:role",
    }
    assert ScheduleSettings.from_env(base).max_active == 100
    assert ScheduleSettings.from_env({**base, "STRANDLY_SCHEDULES_MAX_ACTIVE": "3"}).max_active == 3
    for bad in ("0", "-1", "many"):
        assert ScheduleSettings.from_env({**base, "STRANDLY_SCHEDULES_MAX_ACTIVE": bad}).max_active == 100
    env.service._max_active = 2
    env.service.create(spec(id="one"), AGENT)
    env.service.create(spec(id="two"), DASH)
    with pytest.raises(ValidationError, match="the team already has 2 active jobs"):
        env.service.create(spec(id="three"), AGENT)


def _race_after_count(env, monkeypatch, other):
    """Run ``other()`` once, right after this service's first count of active jobs."""
    store, real = env.service.store, env.service.store.list_jobs
    raced: list[bool] = []

    def list_jobs(*, consistent=False):
        jobs = real(consistent=consistent)
        if consistent and not raced:
            raced.append(True)
            other()
        return jobs

    monkeypatch.setattr(store, "list_jobs", list_jobs)


def test_concurrent_activations_cannot_overshoot_the_team_cap(env, monkeypatch):
    env.service._max_active = 2
    env.service.create(spec(id="one"), AGENT)
    rival = ScheduleService(
        JobStore(env.ddb, "strandly-test-schedules"), env.service.timer,
        clock=env.clock, sleep=lambda _s: None, max_active=2,
    )
    _race_after_count(env, monkeypatch, lambda: rival.create(spec(id="two"), DASH))
    with pytest.raises(ValidationError, match="the team already has 2 active jobs"):
        env.service.create(spec(id="three"), TEAM)
    assert sorted(j.id for j in env.service.list_jobs()) == ["one", "two"]
    # Resume and restore count the same way.
    env.service.pause("two", DASH)
    env.service.create(spec(id="three", enabled=False), TEAM)
    _race_after_count(env, monkeypatch, lambda: rival.resume("two", DASH))
    with pytest.raises(ValidationError, match="the team already has 2 active jobs"):
        env.service.resume("three", TEAM)
    assert env.service.get("three").status == STATUS_PAUSED


def test_an_activation_raced_below_the_cap_is_retried(env, monkeypatch):
    env.service._max_active = 3
    rival = ScheduleService(
        JobStore(env.ddb, "strandly-test-schedules"), env.service.timer,
        clock=env.clock, sleep=lambda _s: None, max_active=3,
    )
    _race_after_count(env, monkeypatch, lambda: rival.create(spec(id="two"), DASH))
    assert env.service.create(spec(id="one"), TEAM).status == STATUS_ACTIVE
    assert sorted(j.id for j in env.service.list_jobs()) == ["one", "two"]


# --- update / pause / resume / delete / revert ----------------------------------------------


def test_update_bumps_version_and_records_a_revision(env):
    env.service.create(spec(skill="parity-sync"), DASH)
    job = env.service.update("daily", {"prompt": "new", "skill": None, "expression": "cron(0 9 * * ? *)"}, DASH)
    assert job.version == 2 and job.skill is None and job.updated_by == DASH
    row = env.row("daily")
    assert row["prompt"] == "new" and "skill" not in row and row["version"] == 2
    revs = env.revs("daily")
    assert [r["action"] for r in revs] == ["create", "update"]
    assert revs[1]["snapshot"]["expression"] == "cron(0 9 * * ? *)"
    assert env.schedule("daily")["ScheduleExpression"] == "cron(0 9 * * ? *)"


def test_update_rejects_stale_version_and_unknown_fields(env):
    env.service.create(spec(), DASH)
    env.service.update("daily", {"prompt": "v2"}, DASH)
    with pytest.raises(ConflictError):
        env.service.update("daily", {"prompt": "v3"}, DASH, expected_version=1)
    with pytest.raises(ValidationError, match="cannot change"):
        env.service.update("daily", {"status": "active"}, DASH)
    with pytest.raises(NotFoundError):
        env.service.update("nope", {"prompt": "x"}, DASH)


def test_update_keeps_runtime_state(env):
    env.service.create(spec(), DASH)
    env.service.fire("daily")
    env.service.update("daily", {"prompt": "v2"}, DASH)
    row = env.row("daily")
    assert row["last_run"]["status"] == "dispatched" and row["fire_window"] == "2030-01-01"


def test_every_job_belongs_to_the_team(env):
    env.service.create(spec(id="bobs"), AGENT)
    env.service.create(spec(id="alices"), DASH)
    carol = Actor("slack:U0CAROL", "agent", "slack")
    person = Actor("person:example-maintainer", "agent", "strandly:people")
    assert env.service.update("alices", {"prompt": "x"}, AGENT).updated_by == AGENT
    assert env.service.pause("bobs", carol).updated_by == carol
    assert env.service.resume("bobs", person).updated_by == person
    env.service.update("bobs", {"prompt": "y"}, DASH)
    env.service.delete("alices", carol)
    # created_by stays the original requester, for audit; each change records its author.
    job = env.service.get("bobs")
    assert job.created_by == AGENT and job.updated_by == DASH
    assert [r.actor for r in env.service.revisions("bobs")] == [DASH, person, carol, AGENT]


def test_pause_and_resume(env):
    env.service.create(spec(), DASH)
    job = env.service.pause("daily", DASH)
    assert job.status == STATUS_PAUSED and env.schedule("daily")["State"] == "DISABLED"
    assert env.row("daily")["enabled"] is False
    job = env.service.resume("daily", DASH)
    assert job.status == STATUS_ACTIVE and env.schedule("daily")["State"] == "ENABLED"
    assert [r["action"] for r in env.revs("daily")] == ["create", "pause", "resume"]


def _run_of(env, job_id: str) -> ScheduleService:
    return env.service.for_run_of(job_id, session_id=f"sched-{job_id}-20300101", trigger=f"scheduled:{job_id}")


def test_a_jobs_own_run_records_via_on_every_revision(env):
    env.service.create(spec(id="parent"), DASH)
    run = _run_of(env, "parent")
    run.create(spec(id="child"), AGENT)
    run.update("child", {"prompt": "x"}, AGENT)
    run.delete("child", AGENT)
    revs = env.service.revisions("child")
    assert [r.action for r in revs] == ["delete", "update", "create"]
    assert {r.via["job_id"] for r in revs if r.via} == {"parent"} and all(r.via for r in revs)
    assert all(r["via"]["kind"] == "scheduled-run" for r in env.revs("child"))
    assert "via" not in env.revs("parent")[0]  # the unscoped service records none
    assert env.service._via is None  # scoping never leaks into the shared service


def test_a_scheduled_run_acts_as_the_team_on_any_job(env):
    env.service.create(spec(id="parent"), Actor("person:example-maintainer", "agent", "strandly:people"))
    env.service.create(spec(id="peer"), DASH)
    run = _run_of(env, "parent")
    child = run.create(spec(id="child", expression="rate(2 hours)"), TEAM)
    assert child.status == STATUS_ACTIVE and child.created_by == TEAM
    assert child.expires_at == iso(NOW + timedelta(days=90))
    assert run.update("peer", {"prompt": "follow up"}, TEAM).updated_by == TEAM
    run.pause("peer", TEAM)
    env.clock.now = NOW + timedelta(days=30)
    resumed = run.resume("peer", TEAM, expires_at=iso(env.clock.now + timedelta(days=200)))
    assert resumed.status == STATUS_ACTIVE and resumed.expires_at == iso(env.clock.now + timedelta(days=90))
    deleted = run.delete("child", TEAM)
    restored = run.revert("child", 1, TEAM, expected_version=deleted.rev)
    assert restored.status == STATUS_ACTIVE and restored.created_by == TEAM
    later = run.create(spec(id="later", expression="at(2030-02-01T00:00:00)"), TEAM)
    assert later.status == STATUS_ACTIVE
    assert all(r.via and r.via["job_id"] == "parent" for r in env.service.revisions("child"))


def test_a_scheduled_run_paces_once_jobs_and_never_runs_now(env):
    env.service.create(spec(id="parent"), DASH)
    run = _run_of(env, "parent")
    with pytest.raises(ValidationError, match="at least 15 minutes ahead"):
        run.create(spec(id="soon", expression="at(2030-01-01T12:14:00)"), TEAM)
    assert run.create(spec(id="later", expression="at(2030-01-01T12:15:00)"), TEAM).status == STATUS_ACTIVE
    # A person may still ask for a job in five minutes.
    assert env.service.create(spec(id="soon", expression="at(2030-01-01T12:05:00)"), AGENT)
    with pytest.raises(ForbiddenError, match="may not run a job now"):
        run.run_now("parent", TEAM)
    assert not env.launches


def test_a_scheduled_run_never_confirms_or_changes_a_pending_github_job(env):
    gh = Actor("github:12345", "agent", "https://github.com")
    env.service.create(spec(id="gh", expression="rate(2 hours)"), gh, trigger_id="mention-1")
    run = _run_of(env, "gh")
    trigger = "scheduled:gh@sched-gh-20300101"
    with pytest.raises(ForbiddenError):
        run.resume("gh", TEAM, trigger_id=trigger)
    with pytest.raises(ForbiddenError):
        run.update("gh", {"prompt": "now active"}, TEAM, trigger_id=trigger)
    job = env.service.get("gh")
    assert job.status == STATUS_PAUSED and job.pending_trigger == "mention-1"
    assert env.service.resume("gh", gh, trigger_id="mention-2").status == STATUS_ACTIVE


def test_revision_via_round_trips_and_is_optional():
    rev = Revision("j", 2, "pause", AGENT, iso(NOW), {}, {"kind": "scheduled-run", "job_id": "j"})
    assert Revision.from_item(rev.to_item()) == rev
    plain = Revision("j", 1, "create", AGENT, iso(NOW), {})
    assert "via" not in plain.to_item() and Revision.from_item(plain.to_item()).via is None


def test_pause_never_validates_the_definition(env, monkeypatch):
    # A 15-minute job is outside today's limits, but anyone may stop it.
    _legacy_fast_job(env, monkeypatch, "fast")
    _legacy_fast_job(env, monkeypatch, "other")
    assert env.service.pause("fast", AGENT).status == STATUS_PAUSED
    assert env.schedule("fast")["State"] == "DISABLED"
    job = _run_of(env, "other").pause("other", TEAM)
    assert job.status == STATUS_PAUSED and env.revs("other")[-1]["via"]["job_id"] == "other"
    with pytest.raises(ValidationError, match="more often than the one-hour minimum"):
        env.service.resume("fast", AGENT)


def test_a_scheduled_trigger_never_confirms_a_pending_job(env):
    gh = Actor("github:12345", "agent", "https://github.com")
    env.service.create(spec(id="gh", expression="rate(2 hours)"), gh, trigger_id="mention-1")
    for trigger in ("scheduled:gh@sched-gh-20300101", "scheduled:other:manual@x"):
        with pytest.raises(ForbiddenError):
            env.service.resume("gh", gh, trigger_id=trigger)
        with pytest.raises(ForbiddenError):
            env.service.run_now("gh", gh, trigger_id=trigger)
    assert env.service.get("gh").pending_trigger == "mention-1"
    assert env.service.resume("gh", gh, trigger_id="mention-2").status == STATUS_ACTIVE


def test_resume_renews_an_expired_agent_job(env):
    env.service.create(spec(), AGENT)
    env.clock.now = NOW + timedelta(days=91)
    assert env.service.fire("daily") == "expired"
    assert env.row("daily")["status"] == STATUS_EXPIRED and env.schedule("daily") is None
    job = env.service.resume("daily", AGENT)
    assert job.status == STATUS_ACTIVE and job.expires_at == iso(env.clock.now + timedelta(days=90))
    assert env.schedule("daily")["State"] == "ENABLED"


def test_resume_of_a_completed_once_job_needs_a_new_time(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.clock.now = NOW + timedelta(hours=1)
    env.service.fire("once")
    with pytest.raises(ValidationError, match="past"):
        env.service.resume("once", DASH)


def test_delete_keeps_history_and_revert_restores(env):
    env.service.create(spec(skill="parity-sync"), DASH)
    env.service.update("daily", {"prompt": "v2"}, DASH)
    rev = env.service.delete("daily", DASH)
    assert rev.action == "delete" and rev.rev == 3
    assert ("daily", "job") not in env.ddb.items and env.schedule("daily") is None
    assert [r.action for r in env.service.revisions("daily")] == ["delete", "update", "create"]
    job = env.service.revert("daily", 1, DASH)
    assert job.prompt == "do the thing" and job.version == 4 and env.schedule("daily")
    job = env.service.revert("daily", 2, DASH)
    assert job.prompt == "v2" and job.version == 5


def test_delete_with_stale_version_conflicts(env):
    env.service.create(spec(), DASH)
    with pytest.raises(ConflictError):
        env.service.delete("daily", DASH, expected_version=7)
    assert env.schedule("daily")


# --- Scheduler failures and reconcile -------------------------------------------------------


def test_scheduler_failure_marks_the_row_unsynced_and_reconcile_fixes_it(env):
    env.scheduler.fail = {"update_schedule", "create_schedule"}
    job = env.service.create(spec(), DASH)
    assert job.sync_state == SYNC_UNSYNCED and "InternalServerException" in (job.sync_error or "")
    assert env.row("daily")["sync_state"] == SYNC_UNSYNCED and env.schedule("daily") is None
    env.scheduler.fail = set()
    report = env.service.reconcile()
    assert report.created == ["daily"] and env.schedule("daily")
    row = env.row("daily")
    assert row["sync_state"] == SYNC_SYNCED and "sync_error" not in row


def test_reconcile_deletes_orphans_and_fixes_drift(env):
    env.service.create(spec(), DASH)
    env.service.create(spec(id="other"), DASH)
    env.scheduler.schedules[(GROUP, "stray")] = {"Name": "stray", "GroupName": GROUP}
    env.scheduler.schedules[(GROUP, "other")]["ScheduleExpression"] = "rate(5 minutes)"
    env.scheduler.schedules[(GROUP, "daily")]["State"] = "DISABLED"
    report = env.service.reconcile()
    assert report.deleted == ["stray"]
    assert sorted(report.updated) == ["daily", "other"]
    assert env.schedule("other")["ScheduleExpression"] == "rate(1 day)"
    assert env.schedule("daily")["State"] == "ENABLED"
    assert env.service.reconcile().as_dict() == {}


def test_reconcile_and_fire_never_touch_the_reconcile_tick(env):
    tick = {"Name": RECONCILE_SCHEDULE, "GroupName": GROUP}
    env.scheduler.schedules[(GROUP, RECONCILE_SCHEDULE)] = dict(tick)
    env.service.create(spec(), DASH)
    assert env.service.reconcile().deleted == []
    assert env.service.fire(RECONCILE_SCHEDULE) == "missing"
    assert env.scheduler.schedules[(GROUP, RECONCILE_SCHEDULE)] == tick
    assert ("delete", RECONCILE_SCHEDULE) not in env.scheduler.calls
    assert env.service.timer.names() == ["daily"]
    with pytest.raises(ValidationError):
        env.service.create(spec(id=RECONCILE_SCHEDULE), DASH)


def test_reconcile_settles_expired_and_finished_once_jobs(env):
    env.service.create(spec(id="agent-job"), AGENT)
    env.service.create(spec(id="ran", expression="at(2030-01-01T13:00:00)"), DASH)
    env.service.create(spec(id="missed", expression="at(2030-01-01T13:00:00)"), DASH)
    env.clock.now = NOW + timedelta(hours=1)
    env.service.store.record_run(
        "ran", LastRun("2030-01-01T13:00:00Z", "dispatched", "sched-ran-20300101")
    )
    for once in ("ran", "missed"):  # Scheduler deleted them after the fire
        del env.scheduler.schedules[(GROUP, once)]
    env.clock.now = NOW + timedelta(days=91)
    report = env.service.reconcile()
    assert report.completed == ["ran"] and sorted(report.expired) == ["agent-job", "missed"]
    assert report.deleted == ["agent-job"]
    assert env.row("ran")["status"] == STATUS_COMPLETED
    assert env.row("missed")["status"] == STATUS_EXPIRED


# --- fire / run_now -------------------------------------------------------------------------


def test_fire_dispatches_records_and_dedupes_per_day(env):
    env.service.create(spec(skill="parity-sync"), DASH)
    assert env.service.fire("daily", scheduled_time="2030-01-01T12:00:00Z") == "dispatched"
    (launch,) = env.launches
    assert launch["session"] == "sched-daily-20300101" and launch["trigger"] == "scheduled:daily"
    assert launch["prompt"] == (
        'skills(skill_name="parity-sync")\n\ndo the thing\n\n'
        'This run belongs to scheduled job daily. When the job\'s goal is done or it should stop, '
        'pause it: schedule(action="pause").'
    )
    row = env.row("daily")
    assert row["last_run"] == {
        "time": "2030-01-01T12:00:00Z",
        "status": "dispatched",
        "session_id": "sched-daily-20300101",
        "trigger": "schedule",
    }
    # A redelivery, or a fire later the same day, does not run it again.
    assert env.service.fire("daily", scheduled_time="2030-01-01T12:00:00Z") == "duplicate"
    env.clock.now = NOW + timedelta(hours=5)
    assert env.service.fire("daily") == "duplicate"
    env.clock.now = NOW + timedelta(days=1)
    assert env.service.fire("daily", scheduled_time="2030-01-02T12:00:00Z") == "dispatched"
    assert len(env.launches) == 2


def test_fire_window_for_sub_daily_jobs(env):
    env.service.create(spec(expression="rate(1 hour)"), DASH)
    assert env.service.fire("daily", scheduled_time="2030-01-01T12:00:00Z") == "dispatched"
    assert env.service.fire("daily", scheduled_time="2030-01-01T12:00:00Z") == "duplicate"
    assert env.service.fire("daily", scheduled_time="2030-01-01T13:00:00Z") == "dispatched"


def test_fire_uses_the_jobs_local_date_for_daily_windows(env):
    env.service.create(spec(expression="cron(30 0 * * ? *)", timezone="Asia/Tokyo"), DASH)
    # 00:30 in Tokyo on two consecutive days are 15:30 UTC on consecutive UTC days.
    assert env.service.fire("daily", scheduled_time="2030-01-01T15:30:00Z") == "dispatched"
    assert env.service.fire("daily", scheduled_time="2030-01-02T15:30:00Z") == "dispatched"


def test_fire_skips_paused_missing_and_completed(env):
    env.service.create(spec(), DASH)
    env.service.pause("daily", DASH)
    assert env.service.fire("daily") == "skipped-paused"
    assert env.service.fire("ghost") == "missing"
    env.scheduler.schedules[(GROUP, "ghost")] = {"Name": "ghost", "GroupName": GROUP}
    assert env.service.fire("ghost") == "missing" and env.schedule("ghost") is None
    assert env.launches == []


def test_fire_global_toggle_is_not_the_services_concern(env):
    # The service fires whatever is active; the Lambda's env toggle is tested with the invoker.
    env.service.create(spec(), DASH)
    assert env.service.fire("daily") == "dispatched"


def test_once_job_completes_after_its_fire(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.clock.now = NOW + timedelta(hours=1)
    assert env.service.fire("once", scheduled_time="2030-01-01T13:00:00Z") == "dispatched"
    row = env.row("once")
    assert row["status"] == STATUS_COMPLETED and row["enabled"] is False
    assert row["last_run"]["status"] == "dispatched"
    assert env.service.fire("once") == "skipped-completed"
    assert len(env.launches) == 1
    assert [r.action for r in env.service.revisions("once")] == ["create"]


def test_once_job_completes_even_when_dispatch_fails(env):
    env.outcome = "error"
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    assert env.service.fire("once") == "error"
    assert env.row("once")["status"] == STATUS_COMPLETED
    assert env.row("once")["last_run"]["status"] == "error"


def test_expired_recurring_job_is_marked_and_unscheduled_on_fire(env):
    env.service.create(spec(expires_at=iso(NOW + timedelta(days=2))), DASH)
    env.clock.now = NOW + timedelta(days=3)
    assert env.service.fire("daily") == "expired"
    assert env.row("daily")["status"] == STATUS_EXPIRED and env.schedule("daily") is None
    assert env.launches == []


def test_run_now_launches_without_taking_the_window(env):
    env.service.create(spec(), DASH)
    env.service.pause("daily", DASH)
    outcome = env.service.run_now("daily", DASH)
    assert outcome.status == "dispatched"
    assert env.launches[0]["trigger"] == "scheduled:daily:manual"
    row = env.row("daily")
    assert row["last_run"]["trigger"] == "manual" and "fire_window" not in row
    assert env.service.run_now("daily", AGENT).status == "dispatched"


# --- prompt, session, notify ----------------------------------------------------------------


def test_session_modes(env):
    job = env.service.create(spec(), DASH)
    assert job.session_id(NOW) == "sched-daily-20300101"
    assert job.with_changes(session_mode="single").session_id(NOW) == "sched-daily"
    assert job.with_changes(session_mode="per-run").session_id(NOW) == "sched-daily-20300101-120000"


def test_notify_target_is_added_to_the_prompt_only_when_set(env):
    plain = env.service.create(spec(id="plain"), DASH)
    assert plain.run_prompt() == f"do the thing\n\n{plain.own_job_line()}"
    assert "scheduled job plain." in plain.own_job_line()
    slack = env.service.create(
        spec(id="slack", notify={"kind": "slack", "channel": "C0123ABC", "thread_ts": "1700000000.000100"}),
        AGENT,
        slack_from=NotifyTarget("slack", channel="C0123ABC", thread_ts="1700000000.000100"),
    )
    assert slack.notify == NotifyTarget("slack", channel="C0123ABC", thread_ts="1700000000.000100")
    assert slack.run_prompt().endswith(
        "post your result to Slack channel C0123ABC (thread 1700000000.000100) using use_slack."
    )
    gh = env.service.create(
        spec(id="gh", notify={"kind": "github", "url": "https://github.com/o/r/issues/7"}), DASH
    )
    assert gh.run_prompt().endswith("comment on https://github.com/o/r/issues/7 using use_github.")
    assert env.row("gh")["notify"] == {"kind": "github", "url": "https://github.com/o/r/issues/7"}


def test_next_runs_preview(env):
    job = env.service.create(spec(expression="cron(0 9 ? * MON *)"), DASH)
    runs = env.service.next_runs(job, 2)
    assert runs == [datetime(2030, 1, 7, 9, tzinfo=UTC), datetime(2030, 1, 14, 9, tzinfo=UTC)]


def test_list_and_get(env):
    env.service.create(spec(id="b"), DASH)
    env.service.create(spec(id="a"), DASH)
    assert [j.id for j in env.service.list_jobs()] == ["a", "b"]
    assert env.service.get("a").name == "Daily"
    with pytest.raises(NotFoundError):
        env.service.get("zzz")


def test_builtin_skill_names_are_read_without_the_sdk():
    names = builtin_skill_names()
    assert {"code-review", "test-simplify", "dependabot-review"} <= names
    assert "_shared" not in names


def test_settings_from_env():
    assert ScheduleSettings.from_env({}) is None
    s = ScheduleSettings.from_env(
        {
            "STRANDLY_SCHEDULES_TABLE": "t",
            "STRANDLY_SCHEDULES_GROUP": "g",
            "STRANDLY_SCHEDULES_TARGET_ARN": "arn:fn",
            "STRANDLY_SCHEDULES_ROLE_ARN": "arn:role",
            "AWS_REGION": "us-west-2",
        }
    )
    assert s == ScheduleSettings("t", "g", "arn:fn", "arn:role", region="us-west-2")


# --- review fixes: Scheduler calls, phase, windows, expiry, sync, reconcile -----------------


def test_edit_that_keeps_the_schedule_makes_no_scheduler_call(env):
    env.service.create(spec(), DASH)
    calls = list(env.scheduler.calls)
    job = env.service.update("daily", {"prompt": "new", "name": "Renamed", "skill": "parity-sync"}, DASH)
    assert env.scheduler.calls == calls
    assert job.sync_state == SYNC_SYNCED and env.row("daily")["sync_state"] == SYNC_SYNCED
    env.service.update("daily", {"timezone": "Europe/Istanbul"}, DASH)
    assert env.scheduler.calls[-1] == ("update", "daily")


def test_unsynced_row_is_synced_even_by_an_edit_that_keeps_the_schedule(env):
    env.scheduler.fail.add("create_schedule")
    env.service.create(spec(), DASH)
    env.scheduler.fail.clear()
    job = env.service.update("daily", {"prompt": "new"}, DASH)
    assert job.sync_state == SYNC_SYNCED and env.schedule("daily") is not None


def test_rate_job_keeps_its_phase_across_edits_and_resume(env):
    env.service.create(spec(expression="rate(1 day)", starts_at="2030-01-01T19:28:00Z"), DASH)
    assert env.schedule("daily")["StartDate"] == datetime(2030, 1, 1, 19, 28, tzinfo=UTC)
    env.clock.now = datetime(2030, 1, 5, 21, 0, tzinfo=UTC)
    env.service.update("daily", {"expression": "rate(2 days)"}, DASH)
    # 2030-01-01 19:28 + 2k days, first after now: the 7th.
    assert env.schedule("daily")["StartDate"] == datetime(2030, 1, 7, 19, 28, tzinfo=UTC)
    env.service.pause("daily", DASH)
    env.clock.now = datetime(2030, 1, 10, 8, 0, tzinfo=UTC)
    env.service.resume("daily", DASH)
    assert env.schedule("daily")["State"] == "ENABLED"
    assert env.schedule("daily")["StartDate"] == datetime(2030, 1, 11, 19, 28, tzinfo=UTC)


def test_rate_job_without_starts_at_is_phased_from_creation(env):
    env.service.create(spec(expression="rate(6 hours)"), DASH)
    assert env.schedule("daily")["StartDate"] == NOW + timedelta(hours=6)
    env.clock.now = NOW + timedelta(hours=13)
    env.service.update("daily", {"timezone": "Asia/Tokyo"}, DASH)
    assert env.schedule("daily")["StartDate"] == NOW + timedelta(hours=18)


def test_reconcile_accepts_a_live_start_date_on_the_jobs_phase(env):
    env.service.create(spec(expression="rate(1 day)"), DASH)
    env.clock.now = NOW + timedelta(days=3, hours=1)
    report = env.service.reconcile()
    assert report.updated == [] and report.created == []
    env.scheduler.schedules[(GROUP, "daily")]["StartDate"] = NOW + timedelta(days=4, hours=2)
    assert env.service.reconcile().updated == ["daily"]
    assert env.schedule("daily")["StartDate"] == NOW + timedelta(days=4)


def test_resume_of_a_completed_once_job_runs_at_its_new_time(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.clock.now = datetime(2030, 1, 1, 13, 0, 5, tzinfo=UTC)
    assert env.service.fire("once", scheduled_time="2030-01-01T13:00:00Z") == "dispatched"
    assert env.row("once")["status"] == STATUS_COMPLETED
    env.service.update("once", {"expression": "at(2030-01-01T18:00:00)"}, DASH)
    env.service.resume("once", DASH)
    assert "fire_window" not in env.row("once")
    env.clock.now = datetime(2030, 1, 1, 18, 0, 5, tzinfo=UTC)
    assert env.service.fire("once", scheduled_time="2030-01-01T18:00:00Z") == "dispatched"
    assert len(env.launches) == 2 and env.row("once")["status"] == STATUS_COMPLETED


def test_revert_of_a_once_job_clears_its_window(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.service.update("once", {"expression": "at(2030-01-02T13:00:00)"}, DASH)
    env.clock.now = datetime(2030, 1, 2, 13, 0, 5, tzinfo=UTC)
    env.service.fire("once", scheduled_time="2030-01-02T13:00:00Z")
    env.service.update("once", {"expression": "at(2030-01-03T13:00:00)"}, DASH)
    env.service.resume("once", DASH)
    env.ddb.items[("once", "job")]["fire_window"] = "once:at(2030-01-03T13:00:00)"
    env.service.revert("once", 3, DASH)
    assert "fire_window" not in env.row("once")


def test_stale_once_schedule_does_not_run_the_moved_job(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T15:00:00)"), DASH)
    env.scheduler.fail.add("update_schedule")
    env.service.update("once", {"expression": "at(2030-01-01T16:00:00)"}, DASH)
    env.clock.now = datetime(2030, 1, 1, 15, 0, 3, tzinfo=UTC)
    assert env.service.fire("once", scheduled_time="2030-01-01T15:00:00Z") == "skipped-stale"
    assert env.launches == [] and env.row("once")["status"] == STATUS_ACTIVE


def test_expression_change_starts_a_new_window_series(env):
    env.service.create(spec(expression="cron(0 13 * * ? *)"), DASH)
    env.clock.now = datetime(2030, 1, 1, 13, 0, tzinfo=UTC)
    assert env.service.fire("daily", scheduled_time="2030-01-01T13:00:00Z") == "dispatched"
    env.service.update("daily", {"expression": "cron(0 18 * * ? *)"}, DASH)
    env.clock.now = datetime(2030, 1, 1, 18, 0, tzinfo=UTC)
    assert env.service.fire("daily", scheduled_time="2030-01-01T18:00:00Z") == "dispatched"


def test_late_redelivery_of_an_older_window_does_not_run(env):
    env.service.create(spec(expression="rate(1 hour)"), DASH)
    assert env.service.fire("daily", scheduled_time="2030-01-01T14:00:00Z") == "dispatched"
    assert env.service.fire("daily", scheduled_time="2030-01-01T13:00:00Z") == "duplicate"
    assert env.service.fire("daily", scheduled_time="2030-01-01T14:00:20Z") == "duplicate"
    assert env.service.fire("daily", scheduled_time="2030-01-01T15:00:00Z") == "dispatched"
    assert len(env.launches) == 2


def test_rate_windows_are_utc_slots_on_the_jobs_phase(env):
    job = env.service.create(
        spec(expression="rate(2 hours)", starts_at="2030-01-01T12:30:00Z", timezone="America/New_York"),
        DASH,
    )
    assert job.window_for(datetime(2030, 1, 1, 14, 29, 40, tzinfo=UTC)) == "2030-01-01T14:30:00Z"
    assert job.window_for(datetime(2030, 1, 1, 14, 30, 40, tzinfo=UTC)) == "2030-01-01T14:30:00Z"
    daily = job.with_changes(expression="rate(1 day)", starts_at="2030-01-01T02:00:00Z")
    # A daily rate job's window is the UTC date of its slot, whatever its timezone (02:00 UTC is
    # still the previous day in New York).
    assert daily.window_for(datetime(2030, 1, 2, 1, 59, 40, tzinfo=UTC)) == "2030-01-02"
    assert daily.window_for(datetime(2030, 1, 2, 2, 0, 20, tzinfo=UTC)) == "2030-01-02"


def test_unknown_timezone_is_loud(env):
    job = env.service.create(spec(expression="cron(0 9 * * ? *)"), DASH)
    with pytest.raises(ValueError, match="unknown timezone"):
        job.with_changes(timezone="Mars/Olympus").window_for(NOW)
    with pytest.raises(ValidationError, match="unknown timezone"):
        env.service.update("daily", {"timezone": "Mars/Olympus"}, DASH)


def test_terminal_status_is_version_guarded(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.service.update("once", {"prompt": "edited"}, DASH)
    store = env.service.store
    assert not store.set_status("once", STATUS_COMPLETED, expected=(STATUS_ACTIVE,), version=1)
    assert env.row("once")["status"] == STATUS_ACTIVE
    assert store.set_status("once", STATUS_COMPLETED, expected=(STATUS_ACTIVE,), version=2)


@pytest.mark.parametrize("actor", [AGENT, DASH])
def test_an_edit_preview_clamps_the_expiry_as_the_edit_does(env, actor):
    env.service.create(spec(), actor)
    for expires in (None, iso(NOW + timedelta(days=365))):
        preview = env.service.preview_update("daily", {"expires_at": expires}, actor)
        assert preview.ok and preview.next_runs
    assert env.service.get("daily").expires_at == iso(NOW + timedelta(days=90))


@pytest.mark.parametrize("actor", [AGENT, DASH])
def test_an_expiry_can_be_moved_but_never_removed(env, actor):
    job = env.service.create(spec(), actor)
    assert job.expires_at == iso(NOW + timedelta(days=90))
    env.clock.now = NOW + timedelta(days=10)
    cleared = env.service.update("daily", {"expires_at": None}, actor)
    assert cleared.expires_at == iso(env.clock.now + timedelta(days=90))
    assert env.schedule("daily")["EndDate"] == env.clock.now + timedelta(days=90)
    far = env.service.update("daily", {"expires_at": iso(env.clock.now + timedelta(days=365))}, actor)
    assert far.expires_at == iso(env.clock.now + timedelta(days=90))
    near = env.service.update("daily", {"expires_at": iso(env.clock.now + timedelta(days=3))}, actor)
    assert near.expires_at == iso(env.clock.now + timedelta(days=3))
    once = env.service.create(spec(id="once", expression="at(2030-02-01T00:00:00)"), actor)
    assert once.expires_at is None
    recurring = env.service.update("once", {"expression": "rate(1 day)"}, actor)
    assert recurring.expires_at == iso(env.clock.now + timedelta(days=90))


def test_a_legacy_job_without_an_expiry_gets_one_on_its_next_change(env):
    env.service.create(spec(), DASH)
    env.ddb.items[("daily", "job")].pop("expires_at")
    legacy = env.service.get("daily")
    assert legacy.expires_at is None
    assert env.service.fire("daily") == "dispatched"
    renamed = env.service.update("daily", {"name": "Renamed"}, AGENT)
    assert renamed.expires_at == iso(NOW + timedelta(days=90))


def test_starts_at_must_be_before_expires_at(env):
    with pytest.raises(ValidationError, match="before expires_at"):
        env.service.create(
            spec(starts_at="2030-02-01T00:00:00Z", expires_at="2030-01-15T00:00:00Z"), DASH
        )


def test_scheduler_calls_are_retried_a_few_times(env):
    env.scheduler.fail_times["create_schedule"] = 2
    job = env.service.create(spec(), DASH)
    assert job.sync_state == SYNC_SYNCED and len(env.sleeps) == 2
    env.scheduler.fail_times["update_schedule"] = 5
    job = env.service.update("daily", {"timezone": "Asia/Tokyo"}, DASH)
    assert job.sync_state == SYNC_UNSYNCED and len(env.sleeps) == 4


def test_a_slower_sync_never_leaves_an_older_definition_on_the_schedule(env):
    v1 = env.service.create(spec(), DASH)
    env.service.update("daily", {"expression": "cron(0 9 * * ? *)"}, DASH)
    out = env.service._sync(v1)  # a writer that read v1 syncs after the v2 edit
    assert env.schedule("daily")["ScheduleExpression"] == "cron(0 9 * * ? *)"
    assert out.version == 2 and env.row("daily")["sync_state"] == SYNC_SYNCED


def test_unsynced_once_job_whose_time_passed_runs_late_once(env):
    env.scheduler.fail.add("create_schedule")
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.scheduler.fail.clear()
    env.clock.now = datetime(2030, 1, 1, 14, 30, tzinfo=UTC)
    assert env.service.reconcile(max_late=0).fired_late == {"once": "deferred"}
    assert env.launches == [] and env.schedule("once") is None
    report = env.service.reconcile()
    assert report.fired_late == {"once": "dispatched"}
    assert env.launches[0]["trigger"] == "scheduled:once:late"
    row = env.row("once")
    assert row["status"] == STATUS_COMPLETED and row["last_run"]["trigger"] == "late"
    assert env.service.reconcile().fired_late == {} and len(env.launches) == 1


def test_synced_once_job_that_never_ran_is_settled_as_expired(env):
    env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    del env.scheduler.schedules[(GROUP, "once")]  # fired, but the invoker never got it
    env.clock.now = datetime(2030, 1, 1, 13, 5, tzinfo=UTC)
    report = env.service.reconcile()
    assert report.expired == [] and report.created == [] and env.launches == []
    env.clock.now = datetime(2030, 1, 1, 13, 30, tzinfo=UTC)
    assert env.service.reconcile().expired == ["once"]


def test_cheap_reconcile_only_resyncs_unsynced_rows(env):
    env.service.create(spec(id="ok"), DASH)
    env.scheduler.fail.add("create_schedule")
    env.service.create(spec(id="bad"), DASH)
    env.scheduler.fail.clear()
    env.scheduler.schedules[(GROUP, "orphan")] = {"Name": "orphan"}
    calls = len(env.scheduler.calls)
    report = env.service.reconcile(only_unsynced=True, max_late=0)
    assert report.created == ["bad"] and report.deleted == []
    assert env.scheduler.calls[calls:] == [("update", "bad"), ("create", "bad")]
    assert env.row("bad")["sync_state"] == SYNC_SYNCED and (GROUP, "orphan") in env.scheduler.schedules


def test_reconcile_reads_consistently_and_rechecks_orphans(env, monkeypatch):
    env.service.create(spec(), DASH)
    env.service.reconcile()
    assert "scan[consistent]" in env.ddb.calls
    # A row the scan missed (written after it) is re-read before its schedule is deleted.
    monkeypatch.setattr(env.service.store, "list_jobs", lambda consistent=False: [])
    assert env.service.reconcile().deleted == [] and env.schedule("daily") is not None


def test_reconcile_deletes_orphans_only_in_a_jobs_group(env):
    other = ScheduleService(
        env.service.store,
        JobTimer(env.scheduler, TimerSettings("default", "arn:fn:invoker", "arn:role:jobs"), clock=env.clock),
        skills=None,
        clock=env.clock,
    )
    env.scheduler.schedules[("default", "legacy")] = {"Name": "legacy"}
    report = other.reconcile()
    assert report.deleted == [] and "orphans" in report.errors
    assert ("default", "legacy") in env.scheduler.schedules


def test_next_runs_follow_the_schedule(env):
    rate = env.service.create(
        spec(expression="rate(1 day)", starts_at="2030-01-01T19:28:00Z",
             expires_at="2030-01-04T00:00:00Z"),
        DASH,
    )
    env.clock.now = datetime(2030, 1, 1, 21, 0, tzinfo=UTC)
    assert env.service.next_runs(rate, 5) == [
        datetime(2030, 1, 2, 19, 28, tzinfo=UTC),
        datetime(2030, 1, 3, 19, 28, tzinfo=UTC),
    ]
    cron = env.service.create(
        spec(id="cron", expression="cron(0 9 * * ? *)", starts_at="2030-01-03T00:00:00Z"), DASH
    )
    assert env.service.next_runs(cron, 1) == [datetime(2030, 1, 3, 9, tzinfo=UTC)]
    once = env.service.create(spec(id="once", expression="at(2030-01-05T08:00:00)"), DASH)
    assert env.service.next_runs(once) == [datetime(2030, 1, 5, 8, tzinfo=UTC)]
    env.service.pause("once", DASH)
    assert env.service.next_runs(env.service.get("once")) == []


def test_preview_and_validate_a_spec_without_writing(env):
    preview = env.service.preview(spec(expression="cron(0 9 ? * MON *)"), DASH, count=2)
    assert preview.ok and preview.next_runs == [
        datetime(2030, 1, 7, 9, tzinfo=UTC),
        datetime(2030, 1, 14, 9, tzinfo=UTC),
    ]
    rate = env.service.preview(spec(expression="rate(1 day)"), DASH, count=1)
    assert rate.next_runs == [NOW + timedelta(days=1)]
    bad = env.service.preview(spec(expression="rate(5 minutes)"), DASH)
    assert not bad.ok and "more often" in bad.errors[0] and bad.next_runs == []
    assert env.service.validate(spec(expression="rate(30 minutes)"), AGENT)
    assert env.service.validate(spec(), DASH) == []
    assert not env.ddb.items and not env.scheduler.schedules


def test_agent_run_now_is_rate_limited_per_job(env):
    env.service.create(spec(), AGENT)
    env.service.run_now("daily", AGENT)
    env.clock.now = NOW + timedelta(minutes=10)
    with pytest.raises(ValidationError, match="less than 15 minutes"):
        env.service.run_now("daily", AGENT)
    env.service.run_now("daily", Actor("portal:alice", "dashboard"))  # the dashboard is not limited
    env.clock.now = NOW + timedelta(minutes=16)
    env.service.run_now("daily", AGENT)
    assert len(env.launches) == 3


@pytest.mark.parametrize(
    "actor",
    [Actor("bob", "agent"), Actor("portal:alice", "system"), Actor("slack:U1", "dashboard")],
)
def test_actor_ids_are_namespaced_per_surface(env, actor):
    with pytest.raises(ValidationError, match="must be one of"):
        env.service.create(spec(), actor)


def test_revert_of_a_deleted_job_checks_version(env):
    env.service.create(spec(), AGENT)
    env.service.delete("daily", AGENT)
    with pytest.raises(ConflictError):
        env.service.revert("daily", 1, AGENT, expected_version=1)
    carol = Actor("slack:U0CAROL", "agent", "slack")
    job = env.service.revert("daily", 1, carol, expected_version=2)
    assert job.version == 3 and job.status == STATUS_ACTIVE
    assert job.created_by == AGENT and job.updated_by == carol


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_noop_status_actions_still_check_expected_version(env, action):
    job = env.service.create(spec(), DASH)
    if action == "pause":
        job = env.service.pause(job.id, DASH)
    with pytest.raises(ConflictError):
        getattr(env.service, action)(job.id, DASH, expected_version=job.version + 1)
    assert env.service.get(job.id).version == job.version


def test_deleted_revert_preserves_creator_and_records_revert(env):
    job = env.service.create(spec(), AGENT)
    original_at = job.created_at
    deleted = env.service.delete(job.id, DASH)
    env.clock.now += timedelta(minutes=5)
    restored = env.service.revert(job.id, 1, DASH, expected_version=deleted.rev)
    assert restored.created_by == AGENT and restored.created_at == original_at
    assert restored.updated_by == DASH and restored.version == deleted.rev + 1
    assert env.service.revisions(job.id)[0].action == "revert"


def test_deleted_revert_rejects_concurrent_create_delete_cycle(env, monkeypatch):
    job = env.service.create(spec(), DASH)
    deleted = env.service.delete(job.id, DASH)
    original_get = env.service.store.get
    raced = False

    def get_after_cycle(job_id):
        nonlocal raced
        if not raced:
            raced = True
            other = env.service.create(spec(prompt="concurrent"), DASH)
            env.service.delete(other.id, DASH)
        return original_get(job_id)

    monkeypatch.setattr(env.service.store, "get", get_after_cycle)
    with pytest.raises(ConflictError):
        env.service.revert(job.id, 1, DASH, expected_version=deleted.rev)
    assert original_get(job.id) is None
    assert env.service.revisions(job.id)[0].rev == deleted.rev + 2


def test_manual_run_ledger_failure_does_not_hide_acceptance_or_retry(env, monkeypatch):
    env.service.create(spec(), DASH)
    env.outcome = "maybe-dispatched"

    def fail(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(env.service.store, "record_run", fail)
    outcome = env.service.run_now("daily", DASH)
    assert outcome.status == "maybe-dispatched" and len(env.launches) == 1


def test_deleted_revert_preserves_the_latest_incarnation_owner(env):
    env.service.create(spec(), AGENT)
    env.service.delete("daily", DASH)
    recreated = env.service.create(spec(), DASH)
    deleted = env.service.delete("daily", DASH)
    restored = env.service.revert("daily", 1, DASH, expected_version=deleted.rev)
    assert restored.created_by == recreated.created_by
    assert restored.created_at == recreated.created_at


def test_a_change_from_another_interface_keeps_the_creator_for_audit(env):
    creator = Actor("portal:immutable-sub", "dashboard", "https://issuer/pool")
    caller = Actor(creator.actor, "agent", creator.issuer)
    job = env.service.create(spec(), creator)
    updated = env.service.update(job.id, {"prompt": "From chat"}, caller, expected_version=1)
    assert updated.created_by == creator and updated.updated_by == caller
    deleted = env.service.delete(job.id, caller, expected_version=updated.version)
    restored = env.service.revert(job.id, 1, caller, expected_version=deleted.rev)
    assert restored.created_by == creator and restored.updated_by == caller


def test_deleted_restore_without_creator_history_records_the_restorer(env, monkeypatch):
    job = env.service.create(spec(), DASH)
    deleted = env.service.delete(job.id, DASH)
    revisions = env.service.store.revisions(job.id)
    monkeypatch.setattr(env.service.store, "revisions", lambda _id: [r for r in revisions if r.action != "create"])
    restored = env.service.revert(job.id, deleted.rev, AGENT, expected_version=deleted.rev)
    assert restored.created_by == AGENT and restored.status == STATUS_ACTIVE


@pytest.mark.parametrize("flag", ["STRANDLY_SCHEDULES_ENABLED", "STRANDLY_SCHEDULED_ENABLED"])
def test_global_kill_switch_blocks_direct_manual_and_scheduled_dispatch(env, monkeypatch, flag):
    job = env.service.create(spec(), DASH)
    monkeypatch.setenv(flag, "false")
    with pytest.raises(ForbiddenError, match="globally disabled"):
        env.service.run_now(job.id, DASH)
    assert env.service.fire(job.id) == "disabled"
    current = env.service.get(job.id)
    assert not env.launches and current.last_run is None and current.fire_window is None


def test_disabled_reconcile_does_not_dispatch_late_once_jobs(env, monkeypatch):
    job = env.service.create(spec(id="once", expression="at(2030-01-01T13:00:00)"), DASH)
    env.clock.now += timedelta(hours=2)
    monkeypatch.setenv("STRANDLY_SCHEDULES_ENABLED", "false")
    env.service.reconcile()
    assert not env.launches
    current = env.service.get(job.id)
    assert current.status == STATUS_ACTIVE and current.last_run is None and current.fire_window is None


# --- Slack targets ------------------------------------------------------------------------------

HOME_THREAD = NotifyTarget("slack", channel="C0HOME", thread_ts="1700000000.000100")
DM_A = NotifyTarget("slack", channel="D0AAA", thread_ts="1700000000.000100")
HOME_CONFIG = Config.load({
    "STRANDLY_SLACK_BOT_TOKEN": "xoxb-1",
    "STRANDLY_SLACK_HOME_CHANNEL": "C0HOME",
    "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true",
})


def _launched_context(env) -> dict:
    return dispatch.notify_context(env.launches[-1]["job"], HOME_CONFIG)


@pytest.mark.parametrize("target", [
    {"kind": "slack", "channel": "D0AAA"},
    {"kind": "slack", "channel": "D0AAA", "thread_ts": "1700000000.000100"},
    {"kind": "slack", "channel": "C0HOME"},
    {"kind": "slack", "channel": "C0ANY"},
])
@pytest.mark.parametrize("actor", [AGENT, DASH])
def test_a_slack_target_is_refused_without_a_requester_in_that_home_channel(env, target, actor):
    with pytest.raises(ValidationError):
        env.service.create(spec(notify=target), actor)
    assert env.service.list_jobs() == []


@pytest.mark.parametrize("dm", [DM_A, NotifyTarget("slack", channel="D0AAA")])
def test_a_dm_is_refused_even_when_the_requester_speaks_from_it(env, dm):
    with pytest.raises(ValidationError, match="never posts to a Slack DM"):
        env.service.create(spec(notify=dm), AGENT, slack_from=dm)
    job = env.service.create(spec(), AGENT)
    with pytest.raises(ValidationError, match="never posts to a Slack DM"):
        env.service.update(job.id, {"notify": dm.to_item()}, AGENT, slack_from=dm)
    assert env.service.get(job.id).notify is None


def test_a_home_channel_thread_is_set_from_that_thread_and_fires_there(env):
    job = env.service.create(spec(notify=HOME_THREAD), AGENT, slack_from=HOME_THREAD)
    assert job.notify == HOME_THREAD
    env.service.run_now(job.id, AGENT)
    assert _launched_context(env) == {"slackChannelId": "C0HOME", "slackThreadTs": "1700000000.000100"}
    # Kept unchanged by anyone, or cleared to the home channel's root.
    kept = env.service.update(job.id, {"prompt": "new", "notify": HOME_THREAD.to_item()}, DASH)
    assert kept.notify == HOME_THREAD
    with pytest.raises(ValidationError):
        env.service.update(job.id, {"notify": {"kind": "slack", "channel": "C0HOME"}}, DASH)
    assert env.service.update(job.id, {"notify": None}, DASH).notify is None


def test_a_home_thread_caller_sets_only_its_own_thread_or_the_root(env):
    other = NotifyTarget("slack", channel="C0HOME", thread_ts="1700000000.000999")
    with pytest.raises(ValidationError):
        env.service.create(spec(notify=other), AGENT, slack_from=HOME_THREAD)
    root = env.service.create(spec(id="root", notify={"kind": "slack", "channel": "C0HOME"}), AGENT, slack_from=HOME_THREAD)
    assert root.notify == NotifyTarget("slack", channel="C0HOME")
    with pytest.raises(ValidationError):
        env.service.update(root.id, {"notify": other.to_item()}, AGENT, slack_from=HOME_THREAD)
    own = env.service.update(root.id, {"notify": HOME_THREAD.to_item()}, AGENT, slack_from=HOME_THREAD)
    assert own.notify == HOME_THREAD


def _legacy_dm_job(env, job_id: str = "daily"):
    """A row written before this rule: a DM target (``set_from`` recorded) on a job made in it."""
    job = env.service.create(
        spec(id=job_id), AGENT, origin={"surface": "slack", "channel": "D0AAA", "thread": "1700000000.000100"}
    )
    env.row(job.id)["notify"] = {**DM_A.to_item(), "set_from": "D0AAA"}
    for rev in env.revs(job.id):
        rev["snapshot"]["notify"] = {**DM_A.to_item(), "set_from": "D0AAA"}
    return env.service.get(job.id)


def test_a_legacy_dm_target_never_fires_into_the_dm(env):
    job = _legacy_dm_job(env)
    assert job.notify == DM_A  # read, but never honored
    env.service.run_now(job.id, AGENT)
    assert _launched_context(env) == {}
    env.service.launch(job.id)
    assert _launched_context(env) == {}


def test_any_write_clears_a_legacy_dm_target(env):
    job = _legacy_dm_job(env)
    renamed = env.service.update(job.id, {"name": "renamed"}, DASH)
    assert renamed.notify is None
    env.service.run_now(job.id, AGENT)
    assert _launched_context(env) == {"slackChannelId": "C0HOME"}


def test_revert_never_restores_a_dm_target(env):
    job = _legacy_dm_job(env)
    env.service.update(job.id, {"prompt": "v2"}, DASH)
    restored = env.service.revert(job.id, 1, DASH)
    assert restored.notify is None and restored.prompt == job.prompt
    deleted = env.service.get(job.id)
    env.service.delete(job.id, DASH, expected_version=deleted.version)
    latest = env.service.store.revisions(job.id)[0].rev
    again = env.service.revert(job.id, 1, DASH, expected_version=latest)
    assert again.notify is None


def test_revert_keeps_a_home_thread_only_as_the_jobs_current_target(env):
    job = env.service.create(spec(notify=HOME_THREAD), AGENT, slack_from=HOME_THREAD)
    env.service.update(job.id, {"prompt": "v2"}, DASH)
    assert env.service.revert(job.id, 1, DASH).notify == HOME_THREAD
    env.service.update(job.id, {"notify": None}, DASH)
    assert env.service.revert(job.id, 1, DASH).notify is None
