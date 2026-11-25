"""The portal's /api/schedules routes over the real ScheduleService, with in-memory AWS clients."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from fake_schedules import FakeDynamoDB, FakeScheduler
from strandly.core.config import Config
from strandly.ops import people, runtime_client
from strandly.ops.lambdas.schedules_api import handler
from strandly.ops.schedules import dispatch
from strandly.ops.schedules import service as service_module
from strandly.ops.schedules.dispatch import RunOutcome
from strandly.ops.schedules.model import Actor
from strandly.ops.schedules.service import JobSpec, ScheduleService
from strandly.ops.schedules.store import JobStore
from strandly.ops.schedules.timer import JobTimer, TimerSettings

UTC = timezone.utc
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
SUB = "11111111-2222-3333-4444-555555555555"
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test"
CLAIMS = {"sub": SUB, "iss": ISSUER, "token_use": "access", "email": "Alice@Example.com"}
ALICE = Actor(f"portal:{SUB}", "dashboard", issuer=ISSUER)
# The deployed invoker's config, with the home channel on: what a launched run's Slack keys are.
HOME_CONFIG = Config.load({
    "STRANDLY_SLACK_BOT_TOKEN": "xoxb-1",
    "STRANDLY_SLACK_HOME_CHANNEL": "C0HOME",
    "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true",
})


@pytest.fixture(autouse=True)
def cognito_issuer(monkeypatch):
    monkeypatch.setenv(handler.ENV_COGNITO_ISSUER, ISSUER)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Env:
    def __init__(self) -> None:
        self.clock = Clock()
        self.scheduler = FakeScheduler(clock=self.clock)
        self.launches: list[dict[str, Any]] = []
        self.service = ScheduleService(
            JobStore(FakeDynamoDB(), "strandly-test-schedules"),
            JobTimer(
                self.scheduler,
                TimerSettings("strandly-test-jobs", "arn:fn:invoker", "arn:role:jobs"),
                clock=self.clock,
            ),
            skills=lambda: {"parity-sync", "test-simplify"},
            clock=self.clock,
            config=object(),
            launcher=self._launch,
            sleep=lambda _s: None,
        )
        self.runs: handler.RunsReader | None = None
        self.people: people.PeopleDirectory | None = None

    def _launch(self, job, config, *, now, trigger_source):
        self.launches.append(
            {"id": job.id, "trigger": trigger_source, "slack": dispatch.notify_context(job, HOME_CONFIG)}
        )
        return RunOutcome("dispatched", job.session_id(now), task_id="t1")

    def call(self, method: str, path: str, body: Any = None, query: dict | None = None,
             claims: dict | None = None, *, direct: bool = False,
             person: str | None = None) -> tuple[int, dict[str, Any]]:
        """An API Gateway event with ``claims``; ``direct``: the team site's direct-invoke event.

        ``person`` is the ``X-Strandly-Person`` header value."""
        event = direct_event(method, path, body, query) if direct else {
            "rawPath": path,
            "requestContext": {
                "apiId": "api123",
                "http": {"method": method, "path": path},
                "authorizer": {"jwt": {"claims": CLAIMS if claims is None else claims}},
            },
            "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
        }
        if person is not None:
            event["headers"] = {**(event.get("headers") or {}), "X-Strandly-Person": person}
        return handler.route(
            event,
            service=self.service,
            actor_for=lambda e: handler.caller_actor(e, self.people),
            runs=self.runs,
            skills=lambda: {"parity-sync", "test-simplify"},
        )

    def create(self, **fields: Any) -> dict[str, Any]:
        body = {"name": "Daily digest", "expression": "cron(0 14 * * ? *)", "prompt": "summarize", **fields}
        status, resp = self.call("POST", "/api/schedules", body)
        assert status == 201, resp
        return resp["job"]


def direct_event(method: str, path: str, body: Any = None, query: dict | None = None) -> dict[str, Any]:
    """What a direct Lambda invoker sends (the team site's ``invokeHttpApi``): no API Gateway or
    authorizer context."""
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "requestContext": {"http": {"method": method, "path": path}},
    }
    if query:
        event["queryStringParameters"] = query
    if body is not None:
        event.update(body=json.dumps(body), isBase64Encoded=False)
    return event


@pytest.fixture
def env() -> Env:
    return Env()


# --- identity ----------------------------------------------------------------------------------


def test_actor_uses_immutable_subject_and_issuer_not_email_or_username():
    for extra in ({}, {"email": "new@example.com", "username": "new-owner"}):
        event = {"requestContext": {"authorizer": {"jwt": {"claims": {**CLAIMS, **extra}}}}}
        assert handler.caller_actor(event) == ALICE


@pytest.mark.parametrize("claims", [
    {}, {"email": "alice@example.com"}, {"username": SUB},
    {**CLAIMS, "sub": ""}, {**CLAIMS, "sub": 123},
    {**CLAIMS, "iss": "https://another-pool.example"},
    {**CLAIMS, "token_use": "id"},
])
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/schedules"), ("POST", "/api/schedules"),
    ("POST", "/api/schedules/preview"), ("GET", "/api/schedules/job"),
    ("GET", "/api/schedules/job/runs"), ("PUT", "/api/schedules/job"),
    ("DELETE", "/api/schedules/job"), ("POST", "/api/schedules/job/pause"),
    ("POST", "/api/schedules/job/resume"), ("POST", "/api/schedules/job/revert"),
    ("POST", "/api/schedules/job/run"),
])
def test_every_route_requires_verified_access_subject(env, claims, method, path):
    status, body = env.call(method, path, claims=claims)
    assert (status, body["code"]) == (401, "unauthenticated")
    assert not env.launches


def test_missing_issuer_configuration_fails_closed(env, monkeypatch):
    monkeypatch.delenv(handler.ENV_COGNITO_ISSUER)
    assert env.call("GET", "/api/schedules")[0] == 401


def test_writes_without_claims_are_401(env):
    status, resp = env.call("POST", "/api/schedules", {"name": "x"}, claims={})
    assert (status, resp["code"]) == (401, "unauthenticated")


def test_api_gateway_event_without_authorizer_is_401(env):
    """Only the missing ``apiId`` marks a direct invoke; API Gateway always sets it."""
    event = {"rawPath": "/api/schedules",
             "requestContext": {"apiId": "api123", "http": {"method": "GET", "path": "/api/schedules"}}}
    status, body = handler.route(event, service=env.service, actor_for=handler.caller_actor)
    assert (status, body["code"]) == (401, "unauthenticated")


# --- direct invoke (trusted IAM principals, like the read API) --------------------------------


def test_direct_invoke_is_the_shared_iam_actor_not_a_portal_user():
    actor = handler.caller_actor(direct_event("GET", "/api/schedules"))
    assert actor == handler.DIRECT_INVOKE_ACTOR == Actor("iam:direct-invoke", "dashboard")
    assert not actor.same_identity(ALICE)
    assert handler.caller_actor({}) == handler.DIRECT_INVOKE_ACTOR


def test_direct_invoke_needs_no_issuer_configuration(env, monkeypatch):
    monkeypatch.delenv(handler.ENV_COGNITO_ISSUER)
    assert env.call("GET", "/api/schedules", direct=True)[0] == 200


def test_direct_invoke_lists_creates_and_changes_jobs(env):
    portal_job = env.create()
    status, resp = env.call("GET", "/api/schedules", direct=True)
    assert status == 200 and [j["id"] for j in resp["jobs"]] == [portal_job["id"]]
    assert resp["limits"] == {"min_interval_minutes": 60, "max_prompt_chars": 20000}

    body = {"name": "Site job", "expression": "rate(1 hour)", "prompt": "check"}
    status, resp = env.call("POST", "/api/schedules", body, direct=True)
    assert status == 201, resp
    job = resp["job"]
    assert job["created_by"] == handler.DIRECT_INVOKE_ACTOR.to_item()

    # Dashboard callers may change any job, whichever dashboard identity created it.
    path = f"/api/schedules/{portal_job['id']}"
    status, resp = env.call("POST", f"{path}/pause", {"expected_version": portal_job["version"]},
                            direct=True)
    assert status == 200 and resp["job"]["updated_by"] == handler.DIRECT_INVOKE_ACTOR.to_item()
    status, resp = env.call("POST", f"/api/schedules/{job['id']}/pause",
                            {"expected_version": job["version"]})
    assert status == 200 and resp["job"]["updated_by"] == ALICE.to_item()

    status, resp = env.call("POST", f"/api/schedules/{job['id']}/run", direct=True)
    assert status == 200 and resp["run"]["status"] == "dispatched"
    assert env.call("GET", f"/api/schedules/{job['id']}", direct=True)[0] == 200


def test_direct_invoke_may_synthesize_verified_claims(env):
    """Same trust as the read API: a direct invoker that sends authorizer claims is that caller."""
    event = direct_event("POST", "/api/schedules",
                         {"name": "x", "expression": "rate(1 hour)", "prompt": "p"})
    event["requestContext"]["authorizer"] = {"jwt": {"claims": CLAIMS}}
    assert handler.caller_actor(event) == ALICE
    status, resp = handler.route(event, service=env.service, actor_for=handler.caller_actor)
    assert status == 201 and resp["job"]["created_by"] == ALICE.to_item()


@pytest.mark.parametrize("authorizer", [
    {}, {"jwt": {}}, {"jwt": {"claims": {}}}, {"jwt": {"claims": {**CLAIMS, "iss": "https://x"}}},
    {"jwt": {"claims": {**CLAIMS, "token_use": "id"}}}, {"lambda": {"actor": "portal:someone"}},
    None,
])
def test_direct_invoke_with_unverifiable_authorizer_context_is_401(env, authorizer):
    """Authorizer context, once present, must verify: it never falls back to the shared actor."""
    event = direct_event("POST", "/api/schedules", {"name": "x"})
    event["requestContext"]["authorizer"] = authorizer
    status, body = handler.route(event, service=env.service, actor_for=handler.caller_actor)
    assert (status, body["code"]) == (401, "unauthenticated")


def test_direct_invoke_body_cannot_choose_the_actor(env):
    body = {"name": "x", "expression": "rate(1 hour)", "prompt": "p",
            "actor": "portal:" + SUB, "created_by": ALICE.to_item()}
    status, _ = env.call("POST", "/api/schedules", body, direct=True)
    assert status == 400
    assert env.call("GET", "/api/schedules", direct=True)[1]["jobs"] == []


# --- list / get --------------------------------------------------------------------------------


def test_list_carries_next_runs_skills_and_limits(env):
    env.create()
    status, resp = env.call("GET", "/api/schedules")
    assert status == 200
    (job,) = resp["jobs"]
    assert job["created_by"] == ALICE.to_item()
    assert job["next_runs"][:2] == ["2030-01-01T14:00:00Z", "2030-01-02T14:00:00Z"]
    assert len(job["next_runs"]) == 5
    assert job["sync_state"] == "synced" and job["last_run"] is None and "sk" not in job
    assert resp["skills"] == ["parity-sync", "test-simplify"]
    assert resp["session_modes"] == ["daily", "single", "per-run"]
    assert resp["limits"] == {"min_interval_minutes": 60, "max_prompt_chars": 20000}
    assert resp["now"] == "2030-01-01T12:00:00Z"


def test_unconfigured_and_unknown_paths(env):
    assert handler.route({"rawPath": "/api/schedules"}, service=None, actor_for=lambda _: ALICE)[0] == 503
    assert env.call("GET", "/api/sessions")[0] == 404
    assert env.call("GET", "/api/schedules/a/b/c")[0] == 404
    assert env.call("POST", "/api/schedules/x/explode")[0] == 404
    assert env.call("PATCH", "/api/schedules")[0] == 405


def test_get_returns_the_job_with_its_revisions_and_a_deleted_job_its_history(env):
    job = env.create()
    status, resp = env.call("GET", f"/api/schedules/{job['id']}")
    assert status == 200 and resp["job"]["id"] == job["id"]
    assert [r["action"] for r in resp["revisions"]] == ["create"]
    env.call("DELETE", f"/api/schedules/{job['id']}", query={"expected_version": 1})
    status, resp = env.call("GET", f"/api/schedules/{job['id']}")
    assert status == 200 and resp["job"] is None
    assert [r["action"] for r in resp["revisions"]] == ["delete", "create"]
    assert env.call("GET", "/api/schedules/nope")[0] == 404


def test_history_shows_changes_a_jobs_own_scheduled_run_made(env):
    job = env.create()
    run = env.service.for_run_of(job["id"], session_id=f"sched-{job['id']}-20300101", trigger=f"scheduled:{job['id']}")
    run.pause(job["id"], Actor("slack:U0BOB", "agent", "slack"))
    status, resp = env.call("GET", f"/api/schedules/{job['id']}")
    assert status == 200 and resp["job"]["status"] == "paused"
    paused, created = resp["revisions"]
    assert paused["action"] == "pause" and paused["via"] == {
        "kind": "scheduled-run", "job_id": job["id"],
        "session_id": f"sched-{job['id']}-20300101", "trigger": f"scheduled:{job['id']}",
    }
    assert "via" not in created


# --- writes ------------------------------------------------------------------------------------


def test_create_validates_with_the_dashboard_limits(env):
    status, resp = env.call(
        "POST", "/api/schedules", {"name": "fast", "expression": "rate(5 minutes)", "prompt": "p"}
    )
    assert (status, resp["code"]) == (400, "invalid")
    assert "more often than once an hour" in resp["error"]
    status, resp = env.call(
        "POST", "/api/schedules", {"name": "fast", "expression": "rate(15 minutes)", "prompt": "p"}
    )
    assert (status, resp["code"]) == (400, "invalid")
    assert env.call("POST", "/api/schedules", {"name": "n", "expression": "rate(1 hour)", "prompt": "p"})[0] == 201


def test_a_sub_hour_job_can_be_paused_and_deleted_but_not_edited_as_it_is(env, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(service_module, "MIN_INTERVAL_SECONDS", 900)
        env.service.create(
            JobSpec(id="fast", name="fast", expression="rate(15 minutes)", prompt="p"),
            Actor("portal:alice", "dashboard"),
        )
    status, resp = env.call("PUT", "/api/schedules/fast", {"expected_version": 1, "prompt": "new"})
    assert (status, resp["code"]) == (400, "invalid")
    assert "more often than the one-hour minimum" in resp["error"]
    status, resp = env.call("POST", "/api/schedules/fast/pause", {"expected_version": 1})
    assert status == 200 and resp["job"]["status"] == "paused"
    status, resp = env.call("DELETE", "/api/schedules/fast", query={"expected_version": "2"})
    assert status == 200 and resp["deleted"] == "fast"


def test_create_rejects_unknown_fields_and_bad_types(env):
    status, resp = env.call("POST", "/api/schedules", {"name": "x", "status": "active"})
    assert (status, resp["code"]) == (400, "bad_request") and "status" in resp["error"]
    assert env.call("POST", "/api/schedules", {"name": 3})[0] == 400
    assert env.call("POST", "/api/schedules", {"name": "x", "enabled": "yes"})[0] == 400
    assert env.call("POST", "/api/schedules", {"name": "x", "notify": "C123"})[0] == 400
    assert env.call("POST", "/api/schedules", ["not", "an", "object"])[0] == 400


def test_create_a_once_job_with_a_skill_and_notify_target(env):
    job = env.create(
        name="Remind",
        expression="at(2030-01-02T09:00:00)",
        timezone="Europe/Istanbul",
        skill="parity-sync",
        session_mode="per-run",
        notify={"kind": "github", "url": "https://github.com/o/r/issues/1"},
    )
    assert job["kind"] == "once" and job["next_runs"] == ["2030-01-02T06:00:00Z"]
    assert job["notify"] == {"kind": "github", "url": "https://github.com/o/r/issues/1"}
    assert env.scheduler.schedules[("strandly-test-jobs", job["id"])]["ActionAfterCompletion"] == "DELETE"


def test_update_needs_the_version_it_edits(env):
    job = env.create()
    path = f"/api/schedules/{job['id']}"
    assert env.call("PUT", path, {"prompt": "new"})[1]["code"] == "bad_request"
    status, resp = env.call("PUT", path, {"expected_version": job["version"], "prompt": "new"})
    assert status == 200 and resp["job"]["prompt"] == "new"
    assert resp["job"]["updated_by"]["actor"] == ALICE.actor
    status, resp = env.call("PUT", path, {"expected_version": job["version"], "prompt": "stale"})
    assert (status, resp["code"]) == (409, "conflict")
    assert env.call("PUT", path, {"expected_version": 9, "enabled": False})[0] == 400
    assert env.call("PUT", path, {"expected_version": 2, "prompt": 5})[1]["code"] == "bad_request"
    assert env.call("PUT", path, {"expected_version": 2, "prompt": None})[1]["code"] == "bad_request"
    assert env.call("PUT", path, {"expected_version": 2, "notify": "C1"})[1]["code"] == "bad_request"
    status, resp = env.call(
        "PUT", path, {"expected_version": 2, "skill": "parity-sync", "notify": {"kind": "github", "url": "https://github.com/o/r/issues/7"}}
    )
    assert status == 200 and resp["job"]["skill"] == "parity-sync"
    status, resp = env.call("PUT", path, {"expected_version": 3, "skill": None, "notify": None})
    assert status == 200 and resp["job"]["skill"] is None and resp["job"]["notify"] is None


def test_pause_resume_run_now_and_delete(env):
    job = env.create()
    path = f"/api/schedules/{job['id']}"
    status, resp = env.call("POST", f"{path}/pause", {"expected_version": job["version"]})
    assert status == 200 and resp["job"]["status"] == "paused" and resp["job"]["next_runs"] == []
    status, resp = env.call("POST", f"{path}/resume", {"expected_version": resp["job"]["version"]})
    assert resp["job"]["status"] == "active"
    status, resp = env.call("POST", f"{path}/run")
    assert status == 200 and resp["run"] == {
        "status": "dispatched", "session_id": f"sched-{job['id']}-20300101", "task_id": "t1"
    }
    assert env.launches == [
        {"id": job["id"], "trigger": f"scheduled:{job['id']}:manual", "slack": {"slackChannelId": "C0HOME"}}
    ]
    assert env.call("GET", path)[1]["job"]["last_run"]["trigger"] == "manual"
    assert env.call("GET", f"{path}/run")[0] == 405
    status, resp = env.call("DELETE", path, query={"expected_version": "1"})
    assert status == 409
    status, resp = env.call("DELETE", path, query={"expected_version": 3})
    assert status == 200 and resp["deleted"] == job["id"]
    assert ("strandly-test-jobs", job["id"]) not in env.scheduler.schedules


def test_revert_restores_a_revision_and_recreates_a_deleted_job(env):
    job = env.create(prompt="v1")
    path = f"/api/schedules/{job['id']}"
    edited = env.call("PUT", path, {"expected_version": job["version"], "prompt": "v2"})[1]["job"]
    status, resp = env.call("POST", f"{path}/revert", {"rev": 1, "expected_version": edited["version"]})
    assert status == 200 and resp["job"]["prompt"] == "v1"
    assert env.call("POST", f"{path}/revert", {})[1]["code"] == "bad_request"
    assert env.call("POST", f"{path}/revert", {"rev": 99, "expected_version": 3})[0] == 404
    env.call("DELETE", path, query={"expected_version": 3})
    status, resp = env.call("POST", f"{path}/revert", {"rev": 1, "expected_version": 4})
    assert status == 200 and resp["job"]["prompt"] == "v1"


# --- preview -----------------------------------------------------------------------------------


def test_preview_returns_errors_or_the_next_five_runs(env):
    status, resp = env.call(
        "POST", "/api/schedules/preview",
        {"name": "x", "expression": "cron(0 9 ? * MON *)", "timezone": "America/New_York", "prompt": "p"},
    )
    assert status == 200 and resp["ok"] and resp["kind"] == "recurring" and resp["errors"] == []
    assert resp["next_runs"][0] == "2030-01-07T14:00:00Z" and len(resp["next_runs"]) == 5
    status, resp = env.call(
        "POST", "/api/schedules/preview", {"name": "x", "expression": "at(2029-01-01T00:00:00)", "prompt": "p"}
    )
    assert not resp["ok"] and "in the past" in resp["errors"][0] and resp["next_runs"] == []
    assert env.call("GET", "/api/schedules")[1]["jobs"] == []  # nothing written


def test_preview_of_an_edit_keeps_the_rate_phase(env):
    job = env.create(expression="rate(2 hours)")  # phase: created at 12:00 -> 14:00, 16:00, ...
    env.clock.now = NOW + timedelta(minutes=30)
    status, resp = env.call(
        "POST", "/api/schedules/preview",
        {"id": job["id"], "name": "x", "expression": "rate(3 hours)", "prompt": "p"},
    )
    assert resp["ok"] and resp["next_runs"][:2] == ["2030-01-01T15:00:00Z", "2030-01-01T18:00:00Z"]


# --- past runs ---------------------------------------------------------------------------------


class FakeLedger:
    """The run-ledger table's ``recent`` GSI: newest-first pages honouring the begins_with filter."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = sorted(rows, key=lambda r: r["started_at"], reverse=True)
        self.queries: list[dict[str, Any]] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        self.queries.append(kw)
        since = kw["KeyConditionExpression"].get_expression()["values"][1].get_expression()["values"][1]
        prefix = kw["FilterExpression"].get_expression()["values"][1]
        start = kw.get("ExclusiveStartKey", {}).get("i", 0)
        window = [r for r in self.rows if r["started_at"] >= since]
        page = window[start: start + kw["Limit"]]
        resp: dict[str, Any] = {
            "Items": [r for r in page if r.get("session_id", "").startswith(prefix)],
            "ScannedCount": len(page),
        }
        if start + kw["Limit"] < len(window):
            resp["LastEvaluatedKey"] = {"i": start + kw["Limit"]}
        return resp


def _row(i: int, session: str, **extra: Any) -> dict[str, Any]:
    return {"task_id": f"t{i}", "session_id": session, "status": "completed",
            "started_at": (NOW - timedelta(hours=i)).isoformat(), **extra}


def test_runs_are_the_ledger_rows_of_the_jobs_own_sessions(env):
    from decimal import Decimal

    job = env.create()
    jid = job["id"]
    ledger = FakeLedger([
        _row(1, f"sched-{jid}-20300101", duration_ms=Decimal(1200), trigger_source=f"scheduled:{jid}"),
        _row(2, f"sched-{jid}-x-20300101"),  # another job whose id starts with this one's
        _row(3, "portal-alice-1"),
        _row(4, f"sched-{jid}-20291231-120000", error="boom" * 200, status="failed"),
        _row(24 * 40, f"sched-{jid}-20291120"),  # outside the default 30-day window
    ])
    env.runs = handler.RunsReader(ledger)
    status, resp = env.call("GET", f"/api/schedules/{jid}/runs")
    assert status == 200 and resp["days"] == 30 and resp["complete"] is True
    assert [r["task_id"] for r in resp["runs"]] == ["t1", "t4"]
    assert resp["runs"][0]["duration_ms"] == 1200 and resp["runs"][0]["trigger_source"] == f"scheduled:{jid}"
    assert len(resp["runs"][1]["error"]) == 500
    (q,) = ledger.queries
    assert q["IndexName"] == "recent" and q["ScanIndexForward"] is False
    resp = env.call("GET", f"/api/schedules/{jid}/runs", query={"days": "999"})[1]
    assert resp["days"] == 90 and [r["task_id"] for r in resp["runs"]] == ["t1", "t4", "t960"]


def test_runs_stop_at_the_limit_or_the_scan_cap(env, monkeypatch):
    rows = [_row(i, f"sched-j-{i:08d}" if i % 10 == 0 else "other") for i in range(1, 600)]
    reader = handler.RunsReader(FakeLedger(rows))
    out = reader.for_job("j", since=(NOW - timedelta(days=90)).isoformat(), limit=3)
    assert [r["task_id"] for r in out["runs"]] == ["t10", "t20", "t30"] and out["complete"] is False
    monkeypatch.setattr(handler, "_RUNS_PAGE", 50)
    monkeypatch.setattr(handler, "RUNS_MAX_SCANNED", 100)
    out = reader.for_job("j", since=(NOW - timedelta(days=90)).isoformat(), limit=50)
    assert out["scanned"] == 100 and len(out["runs"]) == 10 and out["complete"] is False


def test_runs_need_the_ledger(env):
    job = env.create()
    assert env.call("GET", f"/api/schedules/{job['id']}/runs")[0] == 503
    env.runs = handler.RunsReader(FakeLedger([]))
    assert env.call("GET", f"/api/schedules/{job['id']}/runs", query={"limit": "x"})[0] == 400


# --- Lambda wiring -----------------------------------------------------------------------------


def test_lambda_handler_serializes_and_hides_internal_errors(env, monkeypatch):
    monkeypatch.setattr(handler, "_wired", {"service": env.service, "runs": None})
    event = {"rawPath": "/api/schedules", "requestContext": {"http": {"method": "GET"},
             "authorizer": {"jwt": {"claims": CLAIMS}}}}
    resp = handler.lambda_handler(event)
    assert resp["statusCode"] == 200 and resp["headers"]["content-type"] == "application/json"
    assert json.loads(resp["body"])["jobs"] == []

    def boom(*_a, **_k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(env.service, "list_jobs", boom)
    resp = handler.lambda_handler(event)
    assert resp["statusCode"] == 500 and "secret" not in resp["body"]


def test_run_now_launches_with_timeouts_inside_the_api_budget(monkeypatch):
    seen: list[tuple[int, int]] = []

    def fake_launch(job, config, *, now, trigger_source):
        seen.append((runtime_client.INVOKE_CONNECT_TIMEOUT_S, runtime_client.INVOKE_READ_TIMEOUT_S))
        raise RuntimeError("boom")

    monkeypatch.setattr(handler.dispatch, "launch", fake_launch)
    before = (runtime_client.INVOKE_CONNECT_TIMEOUT_S, runtime_client.INVOKE_READ_TIMEOUT_S)
    with pytest.raises(RuntimeError):
        handler.launch_within_budget(object(), object(), now=NOW, trigger_source="t")  # type: ignore[arg-type]
    assert seen == [(3, 20)]
    assert (runtime_client.INVOKE_CONNECT_TIMEOUT_S, runtime_client.INVOKE_READ_TIMEOUT_S) == before
    assert handler.RUN_NOW_CONNECT_TIMEOUT_S + handler.RUN_NOW_READ_TIMEOUT_S < 29


@pytest.mark.parametrize("action", ["pause", "resume", "revert", "delete"])
def test_all_definition_actions_require_version(env, action):
    job = env.create()
    path = f"/api/schedules/{job['id']}"
    method = "DELETE" if action == "delete" else "POST"
    if action != "delete":
        path += f"/{action}"
    assert env.call(method, path)[0] == 400
    assert env.service.get(job["id"]).version == 1


def test_action_fields_cannot_spoof_owner(env):
    job = env.create()
    for action in ("pause", "resume", "run", "revert"):
        status, _ = env.call("POST", f"/api/schedules/{job['id']}/{action}",
                             {"expected_version": 1, "actor": "portal:another"})
        assert status == 400
    assert not env.launches


def test_conflict_and_dispatch_errors_are_safe(env, monkeypatch):
    job = env.create()
    from strandly.ops.schedules.store import ConflictError

    def fail(*args, **kwargs):
        raise ConflictError("secret transaction payload")

    monkeypatch.setattr(env.service, "update", fail)
    status, body = env.call("PUT", f"/api/schedules/{job['id']}", {"expected_version": 1, "prompt": "x"})
    assert status == 409 and "secret" not in json.dumps(body)
    for state in ("error", "maybe-dispatched"):
        monkeypatch.setattr(env.service, "_launcher", lambda *a, status=state, **k: RunOutcome(status, "sess", detail="secret ARN"))
        status, body = env.call("POST", f"/api/schedules/{job['id']}/run")
        assert status == 200 and body["run"]["status"] == state
        assert "secret" not in json.dumps(body)
    assert "Check the session" in body["run"]["detail"]


def test_truncated_last_page_is_not_complete():
    ledger = FakeLedger([_row(i, "sched-j") for i in range(1, 10)])
    out = handler.RunsReader(ledger).for_job("j", since="2029", limit=2)
    assert len(out["runs"]) == 2 and out["complete"] is False


def test_scan_budget_not_rounded_up_to_whole_pages(monkeypatch):
    monkeypatch.setattr(handler, "RUNS_MAX_SCANNED", 75)
    monkeypatch.setattr(handler, "_RUNS_PAGE", 50)
    ledger = FakeLedger([_row(i, "other") for i in range(1, 200)])
    out = handler.RunsReader(ledger).for_job("j", since="2029", limit=2)
    assert [q["Limit"] for q in ledger.queries] == [50, 25]
    assert out["scanned"] == 75 and not out["complete"]


def test_completed_once_preview_and_update_keep_status_until_explicit_resume(env):
    job = env.create(expression="at(2030-01-01T13:00:00)")
    env.clock.now = NOW + timedelta(hours=1)
    env.service.fire(job["id"])
    path = f"/api/schedules/{job['id']}"
    body = {"id": job["id"], "name": job["name"], "expression": job["expression"], "prompt": "edited"}
    status, result = env.call("POST", "/api/schedules/preview", body)
    assert status == 200 and result["ok"] and result["next_runs"] == []
    status, result = env.call("PUT", path, {"expected_version": 1, "prompt": "edited"})
    assert status == 200 and result["job"]["status"] == "completed"
    body["expression"] = "at(2030-01-02T13:00:00)"
    assert env.call("POST", "/api/schedules/preview", body)[1]["ok"]
    status, result = env.call("PUT", path, {"expected_version": 2, "expression": body["expression"]})
    assert status == 200 and result["job"]["status"] == "completed" and result["job"]["next_runs"] == []
    status, result = env.call("POST", f"{path}/resume", {"expected_version": 3})
    assert status == 200 and result["job"]["next_runs"] == ["2030-01-02T13:00:00Z"]


def test_edit_preview_unknown_job_is_not_a_create(env):
    status, result = env.call("POST", "/api/schedules/preview", {"id": "missing", "name": "x",
                            "expression": "rate(1 hour)", "prompt": "x"})
    assert status == 404 and result["code"] == "not_found"


@pytest.mark.parametrize("disabled_key", ["STRANDLY_SCHEDULES_ENABLED", "STRANDLY_SCHEDULED_ENABLED", "both"])
def test_global_kill_switch_blocks_manual_route_without_any_launch(env, monkeypatch, disabled_key):
    job = env.create()
    for key in ("STRANDLY_SCHEDULES_ENABLED", "STRANDLY_SCHEDULED_ENABLED"):
        monkeypatch.setenv(key, "false" if disabled_key in (key, "both") else "true")
    monkeypatch.setattr(env.service, "_launcher", handler.launch_within_budget)
    calls = []
    monkeypatch.setattr(runtime_client, "launch_run", lambda *args, **kwargs: calls.append(args))
    status, body = env.call("POST", f"/api/schedules/{job['id']}/run", {})
    assert (status, body["code"]) == (503, "disabled")
    assert not calls and not env.launches
    current = env.service.get(job["id"])
    assert current.last_run is None and current.manual_run_at is None and current.version == 1


# --- direct invoke naming a person --------------------------------------------------------------


class FakePeopleClient:
    """A low-level DynamoDB client answering ``by_github`` Queries from fixed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        attr = kwargs["ExpressionAttributeNames"]["#k"]
        key = kwargs["ExpressionAttributeValues"][":v"]["S"]
        return {"Items": [r for r in self.rows if r.get(attr, {}).get("S") == key]}


OCTO_ROW = {"person_id": {"S": "octo"}, "github_login": {"S": "octocat"}, "name": {"S": "Octo Cat"}}
OCTO = Actor("person:octo", "dashboard", issuer=handler.PERSON_ISSUER)


@pytest.fixture
def with_people(env):
    people.reset_cache()
    client = FakePeopleClient([OCTO_ROW, {"person_id": {"S": "gone"}, "github_login": {"S": "gone"},
                                          "enabled": {"BOOL": False}}])
    env.people = people.PeopleDirectory(client, "strandly-test-people")
    yield client
    people.reset_cache()


def test_a_direct_invoke_records_the_person_its_header_names(env, with_people):
    body = {"name": "Site job", "expression": "rate(1 hour)", "prompt": "check"}
    status, resp = env.call("POST", "/api/schedules", body, direct=True, person="github:OctoCat")
    assert status == 201, resp
    job = resp["job"]
    assert job["created_by"] == OCTO.to_item() == {"actor": "person:octo", "surface": "dashboard",
                                                    "issuer": "strandly:people"}
    status, resp = env.call("POST", f"/api/schedules/{job['id']}/pause", {"expected_version": job["version"]},
                            direct=True, person="github:octocat")
    assert status == 200 and resp["job"]["updated_by"] == OCTO.to_item()
    assert resp["job"]["created_by"] == OCTO.to_item()
    status, detail = env.call("GET", f"/api/schedules/{job['id']}", direct=True, person="github:octocat")
    assert status == 200 and {r["actor"]["actor"] for r in detail["revisions"]} == {"person:octo"}
    assert {q["IndexName"] for q in with_people.queries} == {"by_github"}


@pytest.mark.parametrize("header", [None, "github:nobody", "github:gone", "octocat"])
def test_a_direct_invoke_without_a_known_person_is_the_shared_actor(env, with_people, header):
    body = {"name": "Site job", "expression": "rate(1 hour)", "prompt": "check"}
    status, resp = env.call("POST", "/api/schedules", body, direct=True, person=header)
    assert status == 201 and resp["job"]["created_by"] == handler.DIRECT_INVOKE_ACTOR.to_item()


def test_an_api_gateway_caller_header_never_names_the_actor(env, with_people):
    job = env.create()
    status, resp = env.call("POST", f"/api/schedules/{job['id']}/pause", {"expected_version": job["version"]},
                            person="github:octocat")
    assert status == 200 and resp["job"]["updated_by"] == ALICE.to_item()
    assert with_people.queries == []


def test_lambda_handler_wires_the_people_directory_from_env(env, monkeypatch):
    monkeypatch.setattr(handler, "_wired", {"service": env.service, "runs": None,
                                            "people": people.PeopleDirectory(FakePeopleClient([OCTO_ROW]), "t")})
    people.reset_cache()
    event = direct_event("POST", "/api/schedules", {"name": "Site job", "expression": "rate(1 hour)",
                                                    "prompt": "check"})
    event["headers"]["x-strandly-person"] = "github:octocat"
    resp = handler.lambda_handler(event)
    assert resp["statusCode"] == 201 and json.loads(resp["body"])["job"]["created_by"] == OCTO.to_item()
    people.reset_cache()


# --- Slack targets ------------------------------------------------------------------------------


@pytest.mark.parametrize("target", [
    {"kind": "slack", "channel": "D0SOMEONE"},  # another user's DM with the bot
    {"kind": "slack", "channel": "D0SOMEONE", "set_from": "D0SOMEONE"},
    {"kind": "slack", "channel": "D0SOMEONE", "thread_ts": "1.2", "owner": "U0SOMEONE"},
    {"kind": "slack", "channel": "C0ANY"},
    {"kind": "slack", "channel": "C0HOME"},
    {"kind": "slack", "channel": "G0PRIVATE", "thread_ts": "1.2"},
])
@pytest.mark.parametrize("direct", [False, True])
def test_the_api_cannot_point_a_job_at_a_slack_dm_or_channel(env, target, direct):
    body = {"name": "x", "expression": "cron(0 14 * * ? *)", "prompt": "p", "notify": target}
    status, resp = env.call("POST", "/api/schedules", body, direct=direct)
    assert (status, resp["code"]) == (400, "invalid")
    status, resp = env.call("POST", "/api/schedules/preview", body, direct=direct)
    assert status == 200 and resp["ok"] is False
    assert env.service.list_jobs() == []
    job = env.create()
    status, resp = env.call(
        "PUT", f"/api/schedules/{job['id']}", {"expected_version": 1, "notify": target}, direct=direct
    )
    assert (status, resp["code"]) == (400, "invalid")
    assert env.service.get(job["id"]).notify is None


def test_the_api_may_leave_notify_unset_or_name_a_github_thread(env):
    assert env.create()["notify"] is None
    gh = {"kind": "github", "url": "https://github.com/o/r/issues/7"}
    assert env.create(name="gh", notify=gh)["notify"] == gh


def _dm_b_job(env, *, legacy: bool) -> str:
    """A job asked for from DM-B, as the agent creates it (no target); ``legacy``: a row from
    before this rule that still stores DM-B as its target."""
    job = env.service.create(
        JobSpec(name="From DM-B", expression="cron(0 14 * * ? *)", prompt="summarize"),
        Actor("slack:U0BEA", "agent", "slack"),
        origin={"surface": "slack", "channel": "D0B", "thread": "1.1"},
    )
    if legacy:
        items = env.service.store._client.items
        items[(job.id, "job")]["notify"] = {"kind": "slack", "channel": "D0B", "set_from": "D0B"}
        for (job_id, sk), item in items.items():
            if job_id == job.id and sk != "job":
                item["snapshot"]["notify"] = {"kind": "slack", "channel": "D0B", "set_from": "D0B"}
    return job.id


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_an_api_edit_of_a_dm_job_posts_to_no_dm(env, legacy, direct):
    """The cross-DM rewrite: the Schedules page or a direct invoke edits what a DM-B job says,
    and neither its next fire nor run_now posts to DM-B."""
    job_id = _dm_b_job(env, legacy=legacy)
    path = f"/api/schedules/{job_id}"
    dm = {"kind": "slack", "channel": "D0B"}
    status, resp = env.call("PUT", path, {"expected_version": 1, "prompt": "attacker text", "notify": dm}, direct=direct)
    assert (status, resp["code"]) == (400, "invalid")
    status, resp = env.call("PUT", path, {"expected_version": 1, "prompt": "attacker text"}, direct=direct)
    assert status == 200 and resp["job"]["notify"] is None  # a stored DM target is cleared too
    status, resp = env.call("POST", f"{path}/run", direct=direct)
    assert status == 200, resp
    env.clock.now = NOW.replace(hour=14)
    assert env.service.fire(job_id, scheduled_time="2030-01-01T14:00:00Z") == "dispatched"
    status, resp = env.call("POST", f"{path}/revert", {"expected_version": 2, "rev": 1}, direct=direct)
    assert status == 200 and resp["job"]["notify"] is None
    assert len(env.launches) == 2
    for launch in env.launches:
        assert launch["slack"] == {"slackChannelId": "C0HOME"}


def test_run_now_of_a_legacy_dm_row_posts_to_no_dm(env):
    job_id = _dm_b_job(env, legacy=True)
    status, resp = env.call("POST", f"/api/schedules/{job_id}/run")
    assert status == 200, resp
    (launch,) = env.launches
    assert launch["slack"] == {}  # the stored DM is dropped: the run posts nowhere


def test_restoring_a_deleted_dm_job_brings_back_no_dm(env):
    job_id = _dm_b_job(env, legacy=True)
    status, resp = env.call("DELETE", f"/api/schedules/{job_id}", query={"expected_version": "1"})
    assert status == 200, resp
    revs = env.service.store.revisions(job_id)
    status, resp = env.call("POST", f"/api/schedules/{job_id}/revert", {"expected_version": revs[0].rev, "rev": 1})
    assert status == 200 and resp["job"]["notify"] is None
