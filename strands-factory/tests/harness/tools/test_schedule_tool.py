"""The agent's ``schedule`` tool: authorization from the run's trigger, notify defaults, once
jobs from an ISO time, the depth gate, and a Slack-target job's run getting ``use_slack``."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from fake_schedules import FakeDynamoDB, FakeScheduler
from strandly.core.config import Config
from strandly.core.context import RUN_CONTEXT_STATE_KEY, RuntimeContext
from strandly.ops import runtime_client
from strandly.ops.schedules import dispatch
from strandly.ops.schedules import service as service_module
from strandly.ops.schedules.dispatch import RunOutcome
from strandly.ops.schedules.model import Actor, NotifyTarget
from strandly.ops.schedules.service import JobSpec, ScheduleService
from strandly.ops.schedules.store import JobStore
from strandly.ops.schedules.timer import JobTimer, TimerSettings
from strandly.tools.schedule import (
    expression_for,
    make_schedule_tool,
    requester_of,
    resolve_scheduled_run,
)
from strandly.tools.toolset import build_tools

UTC = timezone.utc
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
GROUP = "strandly-test-jobs"
THREAD = "1791234567.000100"

SLACK = {
    "slackChannelId": "D0AAA",
    "slackUserId": "U0BOB",
    "slackThreadTs": THREAD,
    "triggerSource": "slack:U0BOB",
}
# The team's home channel, and a request from one of its threads.
HOME = "C0HOME"
SLACK_HOME = {**SLACK, "slackChannelId": HOME}
SUB = "11111111-1111-4111-8111-111111111111"
ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_pool"
PORTAL = {"portalUser": SUB, "portalIssuer": ISSUER, "triggerSource": "portal:chat"}
GITHUB = {"triggerSource": "github:@Octocat", "githubUrl": "https://github.com/o/r/pull/42", "triggerId": "mention-1", "githubUserId": "12345", "githubIssuer": "https://github.com"}
SCHEDULED = {
    "triggerSource": "scheduled:remind-x-abc123",
    "slackChannelId": "D0AAA",
    "slackThreadTs": THREAD,
}

SCHEDULE_ENV = {
    "STRANDLY_SCHEDULES_TABLE": "strandly-test-schedules",
    "STRANDLY_SCHEDULES_GROUP": GROUP,
    "STRANDLY_SCHEDULES_TARGET_ARN": "arn:aws:lambda:us-west-2:1:function:strandly-test-scheduled-invoker",
    "STRANDLY_SCHEDULES_ROLE_ARN": "arn:aws:iam::1:role/strandly-test-jobs-scheduler",
}


def _ctx(event: dict[str, Any] | None, session: str | None = None) -> RuntimeContext:
    return RuntimeContext(event=event, session_id=session)


class Env:
    def __init__(self) -> None:
        self.now = NOW
        self.home: str | None = HOME
        self.scheduler = FakeScheduler(clock=lambda: self.now)
        self.launches: list[tuple[str, str]] = []
        self.launched: list[Any] = []  # the jobs as launched
        self.service = ScheduleService(
            JobStore(FakeDynamoDB(), "strandly-test-schedules"),
            JobTimer(
                self.scheduler,
                TimerSettings(GROUP, "arn:fn:invoker", "arn:role:jobs"),
                clock=lambda: self.now,
            ),
            skills=lambda: {"triage"},
            clock=lambda: self.now,
            config=object(),
            launcher=self._launch,
            sleep=lambda _s: None,
        )

    def _launch(self, job, config, *, now, trigger_source):
        self.launches.append((job.id, trigger_source))
        self.launched.append(job)
        return RunOutcome("dispatched", job.session_id(now), task_id="t1")

    def tool(self, event: dict[str, Any] | None, session: str | None = None, **kw: Any):
        kw.setdefault("home_channel", self.home)
        return make_schedule_tool(_ctx(event, session), lambda: self.service, allowed_owners=("strands-agents", "aws/managed-*"), **kw)

    def call(self, event: dict[str, Any] | None, *, session: str | None = None, **args: Any):
        """One tool call made in a turn triggered by ``event``."""
        state = {RUN_CONTEXT_STATE_KEY: _ctx(event, session)}
        out = self.tool(None)(tool_context=SimpleNamespace(invocation_state=state), **args)
        return out["status"], out["content"][0]["text"]

    def only_job(self):
        (job,) = self.service.list_jobs()
        return job


@pytest.fixture
def env() -> Env:
    return Env()


# ---- who may write ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "session", "origin", "actor"),
    [
        (SLACK, "slack-T-D0AAA-1", "slack", "slack:U0BOB"),
        (PORTAL, "portal-1", "portal", f"portal:{SUB}"),
        ({"portalUser": "alice"}, None, "unknown", None),
        (GITHUB, "gh-o-r-pr-42", "github", "github:12345"),
        (SCHEDULED, "sched-remind-x-abc123-20300101", "scheduled", None),
        # A scheduled session is never trusted, whatever its payload says.
        (SLACK, "sched-remind-x-abc123", "scheduled", None),
        ({"triggerSource": "scheduled:x:manual", "portalUser": "alice"}, None, "scheduled", None),
        # An Actions event run (any external author), the CLI/MCP, no trigger at all.
        ({"triggerSource": "event:pull_request.opened"}, None, "unknown", None),
        ({}, None, "unknown", None),
        (None, None, "unknown", None),
        # A Slack user id must be the one the trigger names; a channel alone is not a requester.
        ({**SLACK, "triggerSource": "slack:U0EVE"}, None, "unknown", None),
        ({"slackChannelId": "D0AAA", "slackThreadTs": THREAD}, None, "unknown", None),
        # A portal user may not claim another surface's trigger.
        ({"portalUser": "alice", "triggerSource": "github:@example-maintainer"}, None, "unknown", None),
        ({"portalUser": "alice", "triggerSource": "slack:U0BOB"}, None, "unknown", None),
        ({"portalUser": "bad user!"}, None, "unknown", None),
        ({"triggerSource": "github:@-not-a-login"}, None, "unknown", None),
    ],
)
def test_requester_matrix(event, session, origin, actor):
    who = requester_of(_ctx(event, session))
    assert who.origin == origin
    assert (who.actor.actor if who.actor else None) == actor
    assert who.can_write == (actor is not None)
    if who.actor:
        assert who.actor.surface == "agent"


@pytest.mark.parametrize(
    "event",
    [SCHEDULED, {"triggerSource": "event:pull_request.opened"}, None],
)
@pytest.mark.parametrize(
    "args",
    [
        {"action": "create", "name": "x", "prompt": "p", "when": "2030-01-02T15:00"},
        {"action": "update", "job_id": "existing", "prompt": "new"},
        {"action": "pause", "job_id": "existing"},
        {"action": "resume", "job_id": "existing"},
        {"action": "delete", "job_id": "existing"},
        {"action": "run_now", "job_id": "existing"},
    ],
)
def test_untrusted_runs_are_read_only(env, event, args):
    env.service.create(
        JobSpec(id="existing", name="e", expression="rate(1 day)", prompt="p"),
        Actor("portal:alice", "dashboard"),
    )
    before = env.only_job()
    status, text = env.call(event, **args)
    assert status == "error" and "read-only" in text
    assert env.only_job() == before and env.launches == []


def test_untrusted_runs_may_still_list_and_get(env):
    env.service.create(
        JobSpec(id="existing", name="Nightly", expression="rate(1 day)", prompt="do it"),
        Actor("portal:alice", "dashboard"),
    )
    status, text = env.call(SCHEDULED, action="list")
    assert status == "success" and "existing: 'Nightly' [active]" in text
    status, text = env.call(SCHEDULED, action="get", job_id="existing")
    assert status == "error" and "authenticated requester" in text and "do it" not in text


def test_the_turn_context_wins_over_the_build_time_one(env):
    """The agent is cached per session: authorization follows the turn, not the first build."""
    tool = env.tool(SLACK)
    state = {RUN_CONTEXT_STATE_KEY: _ctx(SCHEDULED, "sched-x")}
    out = tool(
        tool_context=SimpleNamespace(invocation_state=state),
        action="create", name="x", prompt="p", when="2030-01-02T15:00",
    )
    assert out["status"] == "error" and "read-only" in out["content"][0]["text"]
    # Without a turn context authorization fails closed.
    out = tool(
        tool_context=SimpleNamespace(invocation_state={}),
        action="create", name="x", prompt="p", when="2030-01-02T15:00",
    )
    assert out["status"] == "error"
    assert env.service.list_jobs() == []


def test_any_team_member_may_change_any_job(env):
    status, _ = env.call(SLACK, action="create", name="x", prompt="p", when="rate(2 hours)")
    assert status == "success"
    job = env.only_job()
    assert env.call(PORTAL, action="update", job_id=job.id, prompt="q")[0] == "success"
    assert env.call(GITHUB, action="update", job_id=job.id, name="renamed")[0] == "success"
    assert env.call(PORTAL, action="pause", job_id=job.id)[0] == "success"
    job = env.only_job()
    assert job.status == "paused" and job.prompt == "q" and job.name == "renamed"
    assert job.created_by.actor == "slack:U0BOB" and job.updated_by.actor == f"portal:{SUB}"
    assert env.call(GITHUB, action="delete", job_id=job.id)[0] == "success"
    assert env.service.list_jobs() == []


# ---- create: once and recurring ------------------------------------------------------------


def test_create_once_job_from_an_iso_time(env):
    status, text = env.call(
        SLACK,
        action="create",
        name="Check PR 42",
        prompt="Check whether PR 42 is merged.",
        when="2030-01-02T15:00",
        timezone="Europe/Istanbul",
    )
    assert status == "success", text
    job = env.only_job()
    assert job.kind == "once" and job.expression == "at(2030-01-02T15:00:00)"
    assert job.timezone == "Europe/Istanbul" and job.expires_at is None
    assert job.created_by == Actor("slack:U0BOB", "agent", "slack")
    assert "once at Wed 2030-01-02 15:00 Europe/Istanbul (2030-01-02 12:00 UTC)" in text
    assert "Confirm the local time, timezone and destination" in text
    sched = env.scheduler.schedules[(GROUP, job.id)]
    assert sched["ScheduleExpression"] == "at(2030-01-02T15:00:00)"
    assert sched["ScheduleExpressionTimezone"] == "Europe/Istanbul"
    assert sched["ActionAfterCompletion"] == "DELETE"


def test_an_offset_time_is_converted_to_the_timezone():
    assert expression_for("2030-01-02T12:00:00Z", "Europe/Istanbul") == "at(2030-01-02T15:00:00)"
    assert expression_for("2030-01-02T15:00:00+03:00", "UTC") == "at(2030-01-02T12:00:00)"
    assert expression_for("2030-01-02 09:30", "UTC") == "at(2030-01-02T09:30:00)"
    assert expression_for(" cron(0 9 ? * MON-FRI *) ", "UTC") == "cron(0 9 ? * MON-FRI *)"


@pytest.mark.parametrize("when", ["2030-01-02", "tomorrow at 3", "every day"])
def test_a_bad_when_is_explained(env, when):
    status, text = env.call(SLACK, action="create", name="x", prompt="p", when=when)
    assert status == "error" and "when" in text
    assert env.service.list_jobs() == []


def test_a_time_in_the_past_is_rejected(env):
    status, text = env.call(SLACK, action="create", name="x", prompt="p", when="2029-12-31T09:00")
    assert status == "error" and env.service.list_jobs() == []


def test_create_recurring_job_expires_and_lists_next_runs(env):
    status, text = env.call(
        PORTAL,
        action="create",
        name="Weekday digest",
        prompt="Summarize yesterday.",
        when="cron(0 9 ? * MON-FRI *)",
        timezone="America/New_York",
        skill="triage",
        session_mode="single",
    )
    assert status == "success", text
    job = env.only_job()
    assert job.kind == "recurring" and job.skill == "triage" and job.session_mode == "single"
    assert job.expires_at == "2030-04-01T12:00:00Z"
    assert "next: Tue 2030-01-01 09:00 America/New_York (2030-01-01 14:00 UTC)" in text
    assert "Expires 2030-04-01T12:00:00Z" in text


def test_agent_limits_apply(env):
    status, text = env.call(SLACK, action="create", name="x", prompt="p", when="rate(30 minutes)")
    assert status == "error" and "more often than once an hour" in text
    assert env.service.list_jobs() == []


def test_default_timezone_is_configurable(env):
    tool = env.tool(SLACK, default_timezone="Europe/Istanbul")
    tool(
        tool_context=SimpleNamespace(invocation_state={RUN_CONTEXT_STATE_KEY: _ctx(SLACK)}),
        action="create", name="x", prompt="p", when="2030-01-02T15:00",
    )
    assert env.only_job().timezone == "Europe/Istanbul"


# ---- notify defaults -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (SLACK, None),  # a DM: never a destination, so the home channel
        (SLACK_HOME, NotifyTarget("slack", channel=HOME, thread_ts=THREAD)),
        (GITHUB, NotifyTarget("github", url="https://github.com/o/r/pull/42")),
        (PORTAL, None),
        ({**GITHUB, "githubUrl": ""}, None),  # no issue/PR in the payload
    ],
)
def test_results_go_back_where_the_request_came_from(env, event, expected):
    status, text = env.call(event, action="create", name="x", prompt="p", when="2030-01-02T15:00")
    assert status == "success", text
    assert env.only_job().notify == expected


@pytest.mark.parametrize(
    ("notify", "expected"),
    [
        ("none", None),
        ("https://github.com/strands-agents/r/issues/7", NotifyTarget("github", url="https://github.com/strands-agents/r/issues/7")),
        ("slack:C0HOME", NotifyTarget("slack", channel=HOME)),
        (f"slack:C0HOME/{THREAD}", NotifyTarget("slack", channel=HOME, thread_ts=THREAD)),
    ],
)
def test_an_explicit_notify_target(env, notify, expected):
    status, text = env.call(SLACK_HOME, action="create", name="x", prompt="p", when="2030-01-02T15:00", notify=notify)
    assert status == "success", text
    assert env.only_job().notify == expected


def test_a_home_thread_request_cannot_target_another_home_thread(env):
    status, text = env.call(
        SLACK_HOME, action="create", name="x", prompt="p", when="2030-01-02T15:00",
        notify="slack:C0HOME/1791234567.000999",
    )
    assert status == "error" and env.service.list_jobs() == []


def test_a_bad_notify_target_is_refused(env):
    status, text = env.call(SLACK, action="create", name="x", prompt="p", when="2030-01-02T15:00", notify="#general")
    assert status == "error" and "notify" in text and env.service.list_jobs() == []


# ---- update / pause / resume / delete / run_now --------------------------------------------


def test_update_moves_the_time_in_the_jobs_timezone(env):
    env.call(SLACK, action="create", name="x", prompt="p", when="2030-01-02T15:00", timezone="Europe/Istanbul")
    job = env.only_job()
    status, text = env.call(SLACK, action="update", job_id=job.id, when="2030-01-03T10:00", notify="none")
    assert status == "success", text
    job = env.only_job()
    assert job.expression == "at(2030-01-03T10:00:00)" and job.timezone == "Europe/Istanbul"
    assert job.notify is None
    assert env.call(SLACK, action="update", job_id=job.id)[1] == "update needs at least one field to change"


def test_pause_resume_delete(env):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    jid = env.only_job().id
    assert "Paused" in env.call(SLACK, action="pause", job_id=jid)[1]
    assert "Resumed" in env.call(SLACK, action="resume", job_id=jid)[1]
    status, text = env.call(SLACK, action="delete", job_id=jid)
    assert status == "success" and env.service.list_jobs() == []
    assert env.call(SLACK, action="get", job_id=jid) == ("error", f"no job {jid!r}")


def test_run_now_launches_once_per_gap(env):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    jid = env.only_job().id
    status, text = env.call(SLACK, action="run_now", job_id=jid)
    assert status == "success" and "dispatched" in text
    assert env.launches == [(jid, f"scheduled:{jid}:manual")]
    status, text = env.call(SLACK, action="run_now", job_id=jid)
    assert status == "error" and "less than 15 minutes" in text
    env.now += timedelta(minutes=16)
    assert env.call(SLACK, action="run_now", job_id=jid)[0] == "success"


def test_unknown_action_and_missing_ids(env):
    assert env.call(SLACK, action="explode")[0] == "error"
    assert env.call(SLACK, action="pause") == ("error", "pause needs job_id")
    assert env.call(SLACK, action="create", name="x")[1] == "create needs prompt, when"


def test_a_service_failure_is_an_error_result(env):
    def broken():
        raise RuntimeError("no credentials")

    tool = make_schedule_tool(_ctx(SLACK), broken)
    out = tool(tool_context=SimpleNamespace(invocation_state={}), action="list")
    assert out["status"] == "error" and "logged internally" in out["content"][0]["text"]
    assert "no credentials" not in out["content"][0]["text"]


# ---- the depth gate ----------------------------------------------------------------------------


@pytest.fixture
def _no_mcp(monkeypatch):
    import strandly.mcp_clients as mcp_clients

    monkeypatch.setattr(mcp_clients, "build_mcp_clients", lambda config: [])


def _tool_names(values: dict[str, str], event: dict[str, Any] | None, **kw: Any) -> set[str]:
    tools = build_tools(Config(values=values), RuntimeContext(event=event), sandbox=None, **kw)
    return {str(getattr(t, "tool_name", "")) for t in tools}


@pytest.mark.usefixtures("_no_mcp")
def test_schedule_tool_only_for_the_top_unscoped_agent_when_configured():
    assert "schedule" in _tool_names(SCHEDULE_ENV, SLACK, spawn_depth=0)
    assert "schedule" in _tool_names(SCHEDULE_ENV, None, spawn_depth=0)  # read-only, but present
    assert "schedule" not in _tool_names(SCHEDULE_ENV, SLACK, spawn_depth=1)
    assert "schedule" not in _tool_names(
        SCHEDULE_ENV, SLACK, spawn_depth=0, allowed_tools=frozenset({"shell", "use_github"})
    )
    assert "schedule" not in _tool_names({}, SLACK, spawn_depth=0)
    partial = {k: v for k, v in SCHEDULE_ENV.items() if k != "STRANDLY_SCHEDULES_ROLE_ARN"}
    assert "schedule" not in _tool_names(partial, SLACK, spawn_depth=0)


# ---- a home-thread job's run -------------------------------------------------------------------


HOME_ENV = {
    "STRANDLY_SLACK_BOT_TOKEN": "xoxb-1",
    "STRANDLY_SLACK_HOME_CHANNEL": HOME,
    "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true",
}


@pytest.mark.usefixtures("_no_mcp")
def test_a_home_thread_target_rides_the_launch_payload_and_gives_the_run_use_slack(env, monkeypatch):
    env.call(SLACK_HOME, action="create", name="x", prompt="Ping the team.", when="2030-01-02T15:00")
    job = env.only_job()
    sent: list[dict[str, Any]] = []

    def fake_launch_run(arn, region, session_id, prompt, extra):
        sent.append({"sessionId": session_id, "prompt": prompt, **extra})
        return {"status": "accepted", "taskId": "t1"}

    monkeypatch.setattr(runtime_client, "launch_run", fake_launch_run)
    config = Config.load({"STRANDLY_RUNTIME_ARN": "arn:rt", "AWS_REGION": "us-west-2", **HOME_ENV})
    outcome = dispatch.launch(job, config, now=NOW, trigger_source=f"scheduled:{job.id}")
    assert outcome.status == "dispatched"
    (payload,) = sent
    assert payload["slackChannelId"] == HOME and payload["slackThreadTs"] == THREAD
    assert "slackUserId" not in payload  # no personal memory scope, no Slack requester
    assert f"post your result to Slack channel {HOME} (thread " in payload["prompt"]

    run = RuntimeContext(event=payload, session_id=payload["sessionId"])
    names = _tool_names({**HOME_ENV, **SCHEDULE_ENV}, payload, spawn_depth=0)
    assert "use_slack" in names
    assert run.slack_channel_id == HOME and run.slack_thread_ts == THREAD
    # ...and the scheduled run itself may not create or change jobs.
    assert requester_of(run).origin == "scheduled" and not requester_of(run).can_write


def test_a_github_target_needs_no_payload_keys(env):
    env.call(GITHUB, action="create", name="x", prompt="p", when="2030-01-02T15:00")
    job = env.only_job()
    assert dispatch.notify_context(job) == {}
    assert "comment on https://github.com/o/r/pull/42 using use_github" in job.run_prompt()


# ---- run_now through the invoker ------------------------------------------------------------


class FakeLambda:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **req: Any) -> dict[str, Any]:
        self.calls.append(req)
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.answer).encode())}


def test_remote_launcher_asks_the_invoker(env):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    job = env.only_job()
    client = FakeLambda(
        {"status": "ok", "outcome": {"status": "dispatched", "session_id": "sched-x-1", "task_id": "t9", "detail": None}}
    )
    launch = dispatch.remote_launcher(client, "arn:fn:invoker")
    out = launch(job, None, now=NOW, trigger_source="ignored")
    assert out == RunOutcome("dispatched", "sched-x-1", task_id="t9")
    (call,) = client.calls
    assert call["FunctionName"] == "arn:fn:invoker"
    assert json.loads(call["Payload"]) == {"launch": {"job_id": job.id}}


@pytest.mark.parametrize(
    ("answer", "status"), [(RuntimeError("throttled"), "error"), ({"status": "error", "results": {"error": "NotFoundError"}}, "maybe-dispatched")]
)
def test_remote_launcher_failures_preserve_uncertain_acceptance(env, answer, status):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    job = env.only_job()
    out = dispatch.remote_launcher(FakeLambda(answer), "arn:fn")(job, None, now=NOW, trigger_source="")
    assert out.status == status and out.session_id == job.session_id(NOW)


@pytest.mark.asyncio
async def test_run_turn_hands_tools_the_turns_context(monkeypatch):
    import strandly.serve.turn as turn

    seen: list[dict[str, Any]] = []

    class FakeAgent:
        async def stream_async(self, prompt, *, invocation_state, cancel_signal=None):
            seen.append(invocation_state)
            if False:  # pragma: no cover - an async generator that yields nothing
                yield {}

    async def fake_build(config, ctx, *, model=None):
        return FakeAgent()

    monkeypatch.setattr(turn, "build_agent", fake_build)
    ctx = _ctx(SLACK)
    [e async for e in turn.run_turn(Config(values={}), "hi", ctx)]
    assert seen and seen[0][RUN_CONTEXT_STATE_KEY] is ctx


@pytest.mark.parametrize("action", ["resume", "run_now"])
def test_github_confirmation_requires_a_distinct_requester_mention(env, action):
    status, text = env.call(GITHUB, action="create", name="x", prompt="secret", when="rate(2 hours)")
    assert status == "success" and "PENDING CONFIRMATION" in text
    job = env.only_job()
    assert job.status == "paused" and job.pending_trigger == "mention-1"
    assert job.origin == {"surface": "github", "requester_login": "octocat", "url": GITHUB["githubUrl"], "trigger_id": "mention-1"}
    assert env.service.fire(job.id) == "pending-confirmation"
    for event in (GITHUB, {**GITHUB, "triggerId": ""}, {**GITHUB, "githubUserId": "9999", "triggerId": "mention-2"}):
        assert env.call(event, action=action, job_id=job.id)[0] == "error"
        assert env.only_job().pending_trigger == "mention-1" and not env.launches
    assert env.call({**GITHUB, "triggerId": "mention-2"}, action=action, job_id=job.id)[0] == "success"
    assert env.only_job().pending_trigger is None
    assert len(env.launches) == (1 if action == "run_now" else 0)


@pytest.mark.parametrize("changes", [
    {"prompt": "replacement"}, {"notify": "none"}, {"when": "rate(3 hours)"},
    {"timezone": "Europe/Istanbul"}, {"skill": "triage"}, {"session_mode": "single"},
])
def test_sensitive_github_updates_rearm_confirmation(env, changes):
    env.call(GITHUB, action="create", name="x", prompt="p", when="rate(2 hours)")
    job = env.only_job()
    turn2 = {**GITHUB, "triggerId": "mention-2"}
    env.call(turn2, action="resume", job_id=job.id)
    assert env.call(turn2, action="update", job_id=job.id, **changes)[0] == "success"
    assert env.only_job().pending_trigger == "mention-2" and env.only_job().status == "paused"
    assert env.call(turn2, action="update", job_id=job.id, name="renamed")[0] == "success"
    assert env.call(turn2, action="pause", job_id=job.id)[0] == "success"
    assert env.call(turn2, action="run_now", job_id=job.id)[0] == "error"
    assert env.only_job().pending_trigger == "mention-2"


def test_service_revert_and_launch_cannot_bypass_confirmation(env):
    from strandly.ops.schedules.service import ForbiddenError

    env.call(GITHUB, action="create", name="x", prompt="p", when="rate(2 hours)")
    job = env.only_job()
    actor = job.created_by
    with pytest.raises(ForbiddenError):
        env.service.launch(job.id)
    with pytest.raises(ForbiddenError):
        env.service.resume(job.id, Actor("system:seed", "system"), trigger_id="other")
    with pytest.raises(ForbiddenError):
        env.service.revert(job.id, 1, actor)
    reverted = env.service.revert(job.id, 1, actor, trigger_id="mention-1")
    assert reverted.pending_trigger == "mention-1" and reverted.status == "paused"
    env.service.delete(job.id, actor)
    with pytest.raises(ForbiddenError):
        env.service.revert(job.id, 1, actor, trigger_id="mention-2")
    assert not env.launches


@pytest.mark.parametrize("event,notify", [
    (SLACK, "slack:C0FOREIGN"), (PORTAL, "slack:D0AAA"), (GITHUB, "slack:D0AAA"),
    (SLACK, "https://github.com/foreign/repo/issues/1"),
    (GITHUB, "https://github.com/foreign/repo/issues/1"),
    (PORTAL, "https://github.com/aws/unmanaged/issues/1"),
])
def test_foreign_notifications_are_refused_before_write(env, event, notify):
    assert env.call(event, action="create", name="x", prompt="p", when="rate(1 day)", notify=notify)[0] == "error"
    assert env.service.list_jobs() == []


@pytest.mark.parametrize("url", ["https://github.com/o/other/issues/2", "https://github.com/strands-agents/repo/pull/1", "https://github.com/aws/managed-repo/issues/3"])
def test_github_notify_allows_only_origin_owner_or_managed_targets(env, url):
    assert env.call(GITHUB, action="create", name="x", prompt="p", when="rate(1 day)", notify=url)[0] == "success"
    assert env.only_job().notify.url == url


@pytest.mark.parametrize("event", [None, {}, SCHEDULED])
def test_unverified_reads_never_reveal_prompt_notify_or_origin(env, event):
    env.call(SLACK, action="create", name="x", prompt="TOP-SECRET", when="rate(1 day)")
    job = env.only_job()
    for action in ("list", "get"):
        status, text = env.call(event, action=action, job_id=job.id)
        assert "TOP-SECRET" not in text and "D0AAA" not in text and THREAD not in text
        if action == "get":
            assert status == "error"


@pytest.mark.parametrize("event", [SLACK, GITHUB, PORTAL])
def test_every_team_member_reads_every_job_in_full(env, event):
    env.call(SLACK, action="create", name="x", prompt="TOP-SECRET", when="rate(1 day)")
    status, text = env.call(event, action="get", job_id=env.only_job().id)
    assert status == "success" and "TOP-SECRET" in text and "D0AAA" in text
    assert "created by slack:U0BOB" in text


def test_unverified_portal_identities_are_read_only(env):
    env.call(PORTAL, action="create", name="x", prompt="secret", when="rate(1 day)")
    job = env.only_job()
    assert job.created_by == Actor(f"portal:{SUB}", "agent", ISSUER)
    for event in ({**PORTAL, "portalUser": "alice@example.com"}, {**PORTAL, "portalIssuer": ""}):
        status, text = env.call(event, action="pause", job_id=job.id)
        assert status == "error" and "read-only" in text
    assert env.only_job().status == "active"


def test_once_timezone_only_update_clears_old_fire_window_and_requires_confirmation(env):
    env.call(GITHUB, action="create", name="x", prompt="p", when="2030-01-02T15:00", timezone="UTC")
    job = env.only_job()
    turn2 = {**GITHUB, "triggerId": "mention-2"}
    env.call(turn2, action="resume", job_id=job.id)
    from strandly.ops.schedules.model import LastRun
    assert env.service.store.claim(job.id, window="old-window", last_run=LastRun("2030-01-01T12:00:00Z", "dispatched", "sched-x"))
    assert env.only_job().fire_window == "old-window"
    assert env.call(turn2, action="update", job_id=job.id, timezone="Europe/Istanbul")[0] == "success"
    updated = env.only_job()
    assert updated.expression == job.expression and updated.timezone == "Europe/Istanbul"
    assert updated.pending_trigger == "mention-2" and updated.fire_window is None
    assert env.service.fire(job.id, scheduled_time="2030-01-02T15:00:00Z") == "pending-confirmation"


def test_scheduled_slack_tool_never_posts_to_its_stored_dm(monkeypatch):
    from strandly.core.config import SlackSettings
    from strandly.tools import slack

    sent = []
    monkeypatch.setattr(slack, "_slack_api", lambda method, payload, token: sent.append(payload) or {"ok": True})
    tool = slack.make_use_slack(SlackSettings(bot_token="token"), default_channel_id="D0AAA", default_thread_ts=THREAD, pin_destination=True)
    for kwargs in ({"channel_id": "C0FOREIGN"}, {"channel_id": "D0AAA", "thread_ts": "1.2"}, {"channel_id": "D0AAA"}):
        assert tool(action="chat.postMessage", text="x", **kwargs)["status"] == "error"
    assert not sent


def test_ambiguous_remote_launcher_timeout_is_not_retried(env):
    from botocore.exceptions import ReadTimeoutError

    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    attempts = []
    def invoke(**kwargs):
        attempts.append(kwargs)
        raise ReadTimeoutError(endpoint_url="https://lambda.us-west-2.amazonaws.com/123456789012")
    launch = dispatch.remote_launcher(SimpleNamespace(invoke=invoke), "arn:aws:lambda:us-west-2:123456789012:function:private")
    result = launch(env.only_job(), object(), now=NOW, trigger_source="scheduled:x")
    assert result.status == "maybe-dispatched" and len(attempts) == 1
    assert "123456789012" not in str(result) and "arn:" not in str(result)


def test_cached_slack_tool_refuses_a_scheduled_runs_dm(monkeypatch):
    from strandly.core.config import SlackSettings
    from strandly.tools import slack

    sent = []
    monkeypatch.setattr(slack, "_slack_api", lambda method, payload, token: sent.append(payload) or {"ok": True})
    tool = slack.make_use_slack(SlackSettings(bot_token="token"), default_channel_id="D0OLD")
    # The scheduled payload names a DM (a legacy job's stored target): the run still posts nowhere.
    state = SimpleNamespace(invocation_state={RUN_CONTEXT_STATE_KEY: _ctx(SCHEDULED, "sched-x")})
    for channel in ("D0OLD", "D0AAA"):
        assert tool(action="chat.postMessage", channel_id=channel, text="x", tool_context=state)["status"] == "error"
    assert sent == []


def test_conflict_and_sync_errors_do_not_expose_aws_identifiers(env, monkeypatch):
    from strandly.ops.schedules.store import ConflictError

    secret = "arn:aws:iam::123456789012:role/private"
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    job = env.only_job()
    env.service.store.set_sync(job.id, version=job.version, state="unsynced", error=secret)
    assert secret not in env.call(SLACK, action="get", job_id=job.id)[1]
    def fail(*args, **kwargs):
        raise ConflictError(secret)
    monkeypatch.setattr(env.service, "pause", fail)
    status, text = env.call(SLACK, action="pause", job_id=job.id)
    assert status == "error" and "123456789012" not in text and "arn:" not in text


@pytest.mark.parametrize("change", [
    {"githubUserId": ""}, {"githubUserId": None}, {"githubUserId": "octocat"},
    {"githubUserId": "0"}, {"githubUserId": True}, {"githubIssuer": ""},
    {"githubIssuer": "https://github.example.com"},
])
def test_github_identity_fails_closed_without_numeric_subject_and_exact_issuer(env, change):
    event = {**GITHUB, **change}
    assert requester_of(_ctx(event)).actor is None
    assert env.call(event, action="create", name="x", prompt="p", when="rate(1 day)")[0] == "error"
    assert not env.service.list_jobs()


def test_only_the_requesting_github_account_confirms_its_pending_job(env):
    env.call(GITHUB, action="create", name="x", prompt="SECRET", when="rate(1 day)")
    job = env.only_job()
    assert job.created_by == Actor("github:12345", "agent", "https://github.com")
    reassigned = {**GITHUB, "githubUserId": "99999", "triggerId": "mention-2"}
    assert env.call(reassigned, action="resume", job_id=job.id)[0] == "error"
    assert env.only_job().pending_trigger == "mention-1"
    renamed = {**GITHUB, "triggerSource": "github:@renamed", "triggerId": "mention-2"}
    assert env.call(renamed, action="resume", job_id=job.id)[0] == "success"
    assert env.only_job().pending_trigger is None and env.only_job().created_by == job.created_by
    assert env.only_job().origin == job.origin


def test_legacy_login_job_stays_pending_for_the_current_login_holder(env):
    job = env.service.create(
        JobSpec(name="legacy", expression="rate(1 day)", prompt="OLD-SECRET"),
        Actor("github:octocat", "agent", "https://github.com"), trigger_id="legacy",
    )
    assert env.call(GITHUB, action="resume", job_id=job.id)[0] == "error"
    assert env.only_job().pending_trigger == "legacy"


def test_chat_manages_a_dashboard_job_and_keeps_its_creator_for_audit(env):
    owner = Actor(f"portal:{SUB}", "dashboard", ISSUER)
    job = env.service.create(JobSpec(name="UI job", expression="rate(1 day)", prompt="OWNER SECRET"), owner)
    assert "OWNER SECRET" in env.call(PORTAL, action="get", job_id=job.id)[1]
    assert env.call(PORTAL, action="update", job_id=job.id, prompt="edited in chat")[0] == "success"
    updated = env.only_job()
    assert updated.created_by == owner and updated.updated_by.surface == "agent"
    assert env.call(PORTAL, action="pause", job_id=job.id)[0] == "success"
    assert env.call(PORTAL, action="resume", job_id=job.id)[0] == "success"
    assert env.call(PORTAL, action="run_now", job_id=job.id)[0] == "success"
    chat = requester_of(_ctx(PORTAL)).actor
    env.service.delete(job.id, chat)
    restored = env.service.revert(job.id, 1, chat)
    assert restored.created_by == owner and restored.updated_by == chat
    assert env.service.store.revisions(job.id)[0].action == "revert"


@pytest.mark.parametrize("owner", [
    Actor(f"portal:{SUB}", "dashboard"),
    Actor(f"portal:{SUB}", "dashboard", ISSUER + "other"),
    Actor("portal:22222222-2222-4222-8222-222222222222", "dashboard", ISSUER),
])
def test_chat_manages_other_and_legacy_dashboard_jobs(env, owner):
    job = env.service.create(JobSpec(name="UI job", expression="rate(1 day)", prompt="OTHER SECRET"), owner)
    assert "OTHER SECRET" in env.call(PORTAL, action="get", job_id=job.id)[1]
    assert env.call(PORTAL, action="pause", job_id=job.id)[0] == "success"
    assert env.only_job().created_by == owner


@pytest.mark.parametrize("body", [b"", b"{broken", b"\xff", b"[]", b'{}'])
def test_actual_runtime_transport_lost_ack_is_maybe_dispatched(env, monkeypatch, body):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    sent = []
    def invoke(**kwargs):
        sent.append(kwargs)
        return {"response": io.BytesIO(body)}
    monkeypatch.setattr(runtime_client, "_client", lambda *a, **kw: SimpleNamespace(invoke_agent_runtime=invoke))
    outcome = dispatch.launch(
        env.only_job(), Config.load({"STRANDLY_RUNTIME_ARN": "arn:rt", "AWS_REGION": "us-west-2"}),
        now=NOW, trigger_source="scheduled:x",
    )
    assert len(sent) == 1 and outcome.status == "maybe-dispatched"
    assert "do not retry" in outcome.detail


@pytest.mark.parametrize("outcome_status", ["dispatched", "maybe-dispatched"])
def test_tool_preserves_launch_acceptance_when_last_run_write_fails(env, monkeypatch, caplog, outcome_status):
    env.call(PORTAL, action="create", name="x", prompt="p", when="rate(1 day)")
    sent = []
    def launch(job, config, *, now, trigger_source):
        sent.append(job.id)
        return RunOutcome(outcome_status, job.session_id(now))
    def record(*a, **kw):
        raise RuntimeError("arn:aws:dynamodb:us-west-2:123456789012:table/private")
    monkeypatch.setattr(env.service, "_launcher", launch)
    monkeypatch.setattr(env.service.store, "record_run", record)
    status, text = env.call(PORTAL, action="run_now", job_id=env.only_job().id)
    assert status == "success" and outcome_status in text and "do not retry" in text
    assert len(sent) == 1 and "recording failed" in caplog.text
    assert "123456789012" not in text and "arn:" not in text


@pytest.mark.parametrize("outcome", [{}, {"status": "unexpected"}])
def test_remote_malformed_outcome_is_uncertain(env, outcome):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    client = FakeLambda({"status": "ok", "outcome": outcome})
    result = dispatch.remote_launcher(client, "arn:fn")(env.only_job(), None, now=NOW, trigger_source="")
    assert result.status == "maybe-dispatched" and len(client.calls) == 1


def test_recognized_uncertain_remote_outcome_keeps_no_retry_warning(env):
    env.call(SLACK, action="create", name="x", prompt="p", when="rate(1 day)")
    client = FakeLambda({"status": "ok", "outcome": {
        "status": "maybe-dispatched", "session_id": "sched-x", "detail": "internal detail"}})
    out = dispatch.remote_launcher(client, "arn:fn")(env.only_job(), None, now=NOW, trigger_source="")
    assert out.status == "maybe-dispatched"
    assert "do not retry" in (out.detail or "")
    assert "internal detail" not in (out.detail or "")
    assert len(client.calls) == 1


# ---- GitHub Actions requesters ----------------------------------------------------------------

ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
RUN = {
    "issuer": ACTIONS_ISSUER,
    "repository": "strands-agents/harness-sdk",
    "repositoryId": "1000001",
    "workflow": ".github/workflows/triage.yml",
    "runId": "987654321",
    "runAttempt": "1",
    "actor": "octocat",
    "actorId": "583231",
    "eventName": "issues",
    "sha": "abc",
}
ACTIONS = {
    "triggerSource": "github-actions:strands-agents/harness-sdk/triage.yml",
    "triggerId": "gha-1000001-987654321-1",
    "githubActions": RUN,
    "githubUrl": "https://github.com/strands-agents/harness-sdk/issues/7",
}


def _actions_ctx(event: dict[str, Any] | None) -> RuntimeContext:
    return RuntimeContext(event=event, session_id="gh-strands-agents-harness-sdk-issue-7")


def test_a_verified_workflow_run_is_a_requester():
    who = requester_of(_actions_ctx(ACTIONS))
    assert who.origin == "github-actions"
    assert who.actor == Actor("github-actions:1000001", "agent", ACTIONS_ISSUER)
    assert who.notify is not None and who.notify.url == ACTIONS["githubUrl"]
    assert who.provenance == {
        "surface": "github-actions", "repository": "strands-agents/harness-sdk",
        "workflow": ".github/workflows/triage.yml", "run_id": "987654321", "actor": "octocat",
        "url": ACTIONS["githubUrl"], "trigger_id": "gha-1000001-987654321-1",
    }


@pytest.mark.parametrize(
    "event",
    [
        {**ACTIONS, "githubActions": None},
        {**ACTIONS, "githubActions": {**RUN, "issuer": "https://github.com"}},
        {**ACTIONS, "githubActions": {**RUN, "repositoryId": "x1"}},
        {**ACTIONS, "githubActions": {**RUN, "runId": ""}},
        {**ACTIONS, "githubActions": {**RUN, "repository": "not a repo"}},
        {**ACTIONS, "githubActions": {**RUN, "workflow": "triage.yml"}},
        # The trigger must name the verified repository and workflow.
        {**ACTIONS, "triggerSource": "github-actions:strands-agents/other/triage.yml"},
        {**ACTIONS, "triggerSource": "github-actions:strands-agents/harness-sdk/other.yml"},
        # A verified run without the Actions trigger is not an Actions requester.
        {**ACTIONS, "triggerSource": "portal:chat"},
    ],
)
def test_an_unverified_or_inconsistent_run_is_unknown(event):
    who = requester_of(_actions_ctx(event))
    assert who.origin == "unknown" and who.actor is None


def test_notify_origin_stays_inside_the_calling_repository():
    who = requester_of(_actions_ctx({**ACTIONS, "githubUrl": "https://github.com/strands-agents/other/issues/1"}))
    assert who.actor is not None and who.notify is None


def _actions_call(env: Env, opt_in: tuple[str, ...], **args: Any) -> tuple[str, str]:
    tool = make_schedule_tool(_actions_ctx(None), lambda: env.service, actions_schedule_repositories=opt_in)
    state = {RUN_CONTEXT_STATE_KEY: _actions_ctx(ACTIONS)}
    out = tool(tool_context=SimpleNamespace(invocation_state=state), **args)
    return out["status"], out["content"][0]["text"]


CREATE = {"action": "create", "name": "Weekly triage", "prompt": "Triage open issues.",
          "when": "cron(0 9 ? * MON *)"}


def test_schedule_writes_are_denied_without_the_repository_opt_in(env):
    status, text = _actions_call(env, (), **CREATE)
    assert status == "error" and "not opted the repository in" in text
    status, _ = _actions_call(env, ("strands-agents/other",), **CREATE)
    assert status == "error"
    assert env.service.list_jobs() == []
    for verb in ("update", "pause", "resume", "delete", "run_now"):
        assert _actions_call(env, (), action=verb, job_id="j1")[0] == "error"


def test_reads_need_no_opt_in(env):
    assert _actions_call(env, (), action="list") == ("success", "No scheduled jobs.")


def test_an_opted_in_repository_creates_and_owns_its_job(env):
    status, text = _actions_call(env, ("Strands-Agents/Harness-SDK",), **CREATE)
    assert status == "success", text
    job = env.only_job()
    assert job.created_by == Actor("github-actions:1000001", "agent", ACTIONS_ISSUER)
    assert job.status == "active"  # no mention confirmation: the opt-in is the gate
    assert job.notify is not None and job.notify.url == ACTIONS["githubUrl"]
    assert job.origin and job.origin["repository"] == "strands-agents/harness-sdk"
    assert _actions_call(env, ("strands-agents/harness-sdk",), action="pause", job_id=job.id)[0] == "success"


@pytest.mark.usefixtures("_no_mcp")
def test_the_opt_in_list_comes_from_config(monkeypatch):
    seen: dict[str, Any] = {}
    import strandly.tools.schedule as schedule

    real = schedule.make_schedule_tool

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(schedule, "make_schedule_tool", spy)
    values = {**SCHEDULE_ENV, "STRANDLY_ACTIONS_SCHEDULE_REPOSITORIES": " a/b , c/d ,"}
    build_tools(Config(values=values), RuntimeContext(event=ACTIONS), sandbox=None, spawn_depth=0)
    assert seen["actions_schedule_repositories"] == ("a/b", "c/d")
    seen.clear()
    build_tools(Config(values=SCHEDULE_ENV), RuntimeContext(event=ACTIONS), sandbox=None, spawn_depth=0)
    assert seen["actions_schedule_repositories"] == ()


# ---- scheduled runs act as the team ------------------------------------------------------------


TEAM = Actor("system:team", "agent", "strandly")


def _sched(job_id: str, suffix: str = "", **extra: Any) -> tuple[dict[str, Any], str]:
    """A scheduled run's payload and session, as the invoker launches it."""
    return {"triggerSource": f"scheduled:{job_id}{suffix}", **extra}, f"sched-{job_id}-20300101"


def _sched_call(env: Env, run_job: str, /, *, suffix: str = "", **args: Any):
    event, session = _sched(run_job, suffix)
    tool = make_schedule_tool(_ctx(None), lambda: env.service, home_channel=env.home)
    state = {RUN_CONTEXT_STATE_KEY: _ctx(event, session)}
    out = tool(tool_context=SimpleNamespace(invocation_state=state), **args)
    return out["status"], out["content"][0]["text"]


def _slack_job(env: Env, name: str = "Nightly") -> Any:
    status, text = env.call(SLACK_HOME, action="create", name=name, prompt="p", when="rate(2 hours)")
    assert status == "success", text
    return next(j for j in env.service.list_jobs() if j.name == name)


@pytest.mark.parametrize("suffix", ["", ":manual", ":late"])
def test_a_scheduled_run_acts_as_the_team(env, suffix):
    job = _slack_job(env)
    event, session = _sched(job.id, suffix)
    bound = requester_of(_ctx(event, session))
    assert bound.origin == "scheduled" and bound.job_id == job.id and bound.actor is None
    team = resolve_scheduled_run(bound, job)
    assert team.actor == TEAM
    assert team.notify == job.notify
    assert team.trigger_id == f"scheduled:{job.id}{suffix}@{session}"
    status, text = _sched_call(env, job.id, suffix=suffix, action="get", job_id=job.id)
    assert status == "success" and "(this run belongs to it)" in text and "prompt: p" in text


def test_a_scheduled_run_creates_updates_pauses_resumes_and_deletes_jobs(env):
    job = _slack_job(env)
    status, text = _sched_call(env, job.id, action="create", name="Follow-up", prompt="q", when="rate(3 hours)")
    assert status == "success", text
    child = next(j for j in env.service.list_jobs() if j.name == "Follow-up")
    assert child.created_by == TEAM and child.status == "active"
    # "origin" is the job's own destination: its home-channel thread.
    assert child.notify == job.notify == NotifyTarget("slack", channel=HOME, thread_ts=THREAD)
    assert child.origin == {
        "surface": "scheduled", "job_id": job.id, "session_id": f"sched-{job.id}-20300101",
        "trigger": f"scheduled:{job.id}", "job_created_by": "slack:U0BOB",
    }
    for verb in ("pause", "resume"):
        assert _sched_call(env, job.id, action=verb, job_id=child.id)[0] == "success"
    status, _ = _sched_call(env, job.id, action="update", job_id=child.id, prompt="r")
    assert status == "success" and env.service.get(child.id).prompt == "r"
    assert _sched_call(env, job.id, action="delete", job_id=child.id)[0] == "success"
    revisions = env.service.revisions(child.id)
    assert [r.action for r in revisions] == ["delete", "update", "resume", "pause", "create"]
    assert all(r.via and r.via["job_id"] == job.id and r.via["kind"] == "scheduled-run" for r in revisions)
    assert all(r.actor == TEAM for r in revisions)


def test_a_scheduled_run_changes_any_job_but_notify_stays_scoped(env):
    job = _slack_job(env)
    env.call(PORTAL, action="create", name="Theirs", prompt="p", when="rate(2 hours)")
    theirs = next(j for j in env.service.list_jobs() if j.name == "Theirs")
    assert _sched_call(env, job.id, action="update", job_id=theirs.id, prompt="q")[0] == "success"
    assert _sched_call(env, job.id, action="pause", job_id=theirs.id)[0] == "success"
    assert _sched_call(env, job.id, action="delete", job_id=theirs.id)[0] == "success"
    # Notify stays inside the run's origin: its job's home-channel thread, never another channel.
    for target in ("slack:C0OTHER", "slack:D0AAA"):
        status, text = _sched_call(env, job.id, action="create", name="x", prompt="p", when="rate(2 hours)",
                                   notify=target)
        assert status == "error" and "home-channel thread" in text


def test_a_scheduled_run_pauses_its_own_job_without_naming_it(env):
    job = _slack_job(env)
    status, text = _sched_call(env, job.id, action="pause")
    assert status == "success" and f"Paused {job.id}" in text
    assert env.service.get(job.id).status == "paused"
    (latest, *_) = env.service.revisions(job.id)
    assert latest.action == "pause" and latest.actor == TEAM
    assert latest.via == {"kind": "scheduled-run", "job_id": job.id,
                          "session_id": f"sched-{job.id}-20300101", "trigger": f"scheduled:{job.id}"}
    assert _sched_call(env, job.id, action="pause")[0] == "success"


def test_a_scheduled_run_moves_an_expiry_but_never_removes_it(env):
    job = _slack_job(env)
    assert job.expires_at == "2030-04-01T12:00:00Z"
    status, text = _sched_call(env, job.id, action="update", job_id=job.id, expires_at="2030-01-15T00:00:00Z")
    assert status == "success", text
    assert env.service.get(job.id).expires_at == "2030-01-15T00:00:00Z"
    status, text = _sched_call(env, job.id, action="update", job_id=job.id, expires_at="2031-01-01T00:00:00Z")
    assert status == "success" and env.service.get(job.id).expires_at == "2030-04-01T12:00:00Z"
    status, text = _sched_call(env, job.id, action="create", name="Short", prompt="p", when="rate(1 day)",
                               expires_at="2030-01-03T09:00", timezone="Europe/Istanbul")
    assert status == "success", text
    short = next(j for j in env.service.list_jobs() if j.name == "Short")
    assert short.expires_at == "2030-01-03T06:00:00Z"
    status, text = _sched_call(env, job.id, action="update", job_id=job.id, expires_at="soon")
    assert status == "error" and "not an ISO date-time" in text


@pytest.mark.parametrize(
    "creator",
    [
        Actor("person:example-maintainer", "agent", "strandly:people"),
        Actor(f"portal:{SUB}", "dashboard", ISSUER),
        Actor("iam:direct-invoke", "dashboard"),
        Actor("portal:alice", "dashboard"),  # legacy, issuer-less portal creator
        Actor("slack:U0GONE", "agent", "slack"),  # someone who has since left
        Actor("system:migrate", "system"),
    ],
)
def test_a_job_created_by_anyone_runs_as_the_team(env, creator):
    job = env.service.create(JobSpec(id="digest", name="Digest", expression="rate(1 day)", prompt="p"), creator)
    status, text = _sched_call(env, job.id, action="update", job_id=job.id, name="Renamed")
    assert status == "success", text
    status, text = _sched_call(env, job.id, action="create", name="Next", prompt="p", when="rate(2 hours)")
    assert status == "success", text
    (latest, *_) = env.service.revisions(job.id)
    assert latest.actor == TEAM and latest.via and latest.via["job_id"] == job.id
    assert env.service.get(job.id).created_by == creator


def test_a_scheduled_run_pauses_a_job_below_the_minimum_interval(env, monkeypatch):
    with monkeypatch.context() as m:  # a job saved when the minimum was shorter
        m.setattr(service_module, "MIN_INTERVAL_SECONDS", 900)
        job = env.service.create(
            JobSpec(id="digest", name="Digest", expression="cron(*/15 * * * ? *)", prompt="p"),
            Actor("iam:operator", "dashboard"),
        )
    status, text = _sched_call(env, job.id, action="update", job_id=job.id, prompt="q")
    assert status == "error" and "more often than the one-hour minimum" in text
    assert _sched_call(env, job.id, action="pause")[0] == "success"
    assert env.service.get(job.id).status == "paused"


def test_an_unresolvable_scheduled_job_is_read_only(env):
    job = _slack_job(env)
    cases = [
        (_sched("missing-job")),  # the job does not exist
        ({"triggerSource": f"scheduled:{job.id}"}, "portal-1"),  # session not the job's
        ({"triggerSource": f"scheduled:{job.id}"}, f"sched-other-{job.id}"),
        ({"triggerSource": f"scheduled:{job.id}:bogus"}, f"sched-{job.id}-20300101"),
        ({"triggerSource": "scheduled:"}, f"sched-{job.id}"),
        (SLACK, f"sched-{job.id}"),  # a scheduled session with another trigger
    ]
    for event, session in cases:
        assert requester_of(_ctx(event, session)).job_id in (None, "missing-job")
        status, text = env.call(event, session=session, action="create", name="x", prompt="p", when="rate(2 hours)")
        assert status == "error" and "read-only" in text, (event, session)
        status, text = env.call(event, session=session, action="pause", job_id=job.id)
        assert status == "error" and "read-only" in text, (event, session)
    assert env.service.get(job.id).status == "active"
    assert [j.id for j in env.service.list_jobs()] == [job.id]


def test_a_scheduled_run_keeps_the_teams_limits(env):
    job = _slack_job(env)
    status, text = _sched_call(env, job.id, action="create", name="x", prompt="p", when="rate(30 minutes)")
    assert status == "error" and "more often than once an hour" in text
    status, text = _sched_call(env, job.id, action="create", name="x", prompt="p", when="2030-01-01T12:10")
    assert status == "error" and "at least 15 minutes ahead" in text
    status, text = _sched_call(env, job.id, action="create", name="x", prompt="x" * 8001, when="rate(2 hours)")
    assert status == "error" and "longer than 8000" in text
    status, text = _sched_call(env, job.id, action="run_now", job_id=job.id)
    assert status == "error" and "may not run a job now" in text and env.launches == []
    env.service._max_active = 3
    env.call(PORTAL, action="create", name="Portal", prompt="p", when="rate(2 hours)")
    assert _sched_call(env, job.id, action="create", name="j", prompt="p", when="rate(2 hours)")[0] == "success"
    status, text = _sched_call(env, job.id, action="create", name="one-too-many", prompt="p", when="rate(2 hours)")
    assert status == "error" and "the team already has 3 active jobs" in text
    status, text = env.call(SLACK, action="create", name="one-too-many", prompt="p", when="rate(2 hours)")
    assert status == "error" and "at most 3" in text


def test_github_created_jobs_keep_their_confirmation_rules(env):
    env.call(GITHUB, action="create", name="Parent", prompt="p", when="rate(2 hours)")
    parent = env.only_job()
    assert parent.status == "paused" and parent.pending_trigger == "mention-1"
    # The job's own run cannot exist yet; no scheduled run of another job confirms it either.
    other = _slack_job(env)
    for verb in ("resume", "run_now"):
        status, text = _sched_call(env, other.id, action=verb, job_id=parent.id)
        assert status == "error"
    status, text = _sched_call(env, other.id, action="update", job_id=parent.id, prompt="changed")
    assert status == "error" and "Only the requester" in text
    assert env.service.get(parent.id).pending_trigger == "mention-1"
    env.call({**GITHUB, "triggerId": "mention-2"}, action="resume", job_id=parent.id)  # the human confirms
    assert env.service.get(parent.id).status == "active" and not env.service.get(parent.id).pending_trigger
    # Once confirmed, its run acts as the team: what it schedules is active and reports to the thread.
    status, text = _sched_call(env, parent.id, action="create", name="Child", prompt="q", when="rate(3 hours)")
    assert status == "success" and "PENDING CONFIRMATION" not in text
    child = next(j for j in env.service.list_jobs() if j.name == "Child")
    assert child.created_by == TEAM and child.status == "active" and child.notify == parent.notify
    assert env.launches == []


def test_a_github_actions_created_jobs_run_acts_as_the_team(env):
    status, text = _actions_call(env, ("strands-agents/harness-sdk",), **CREATE)
    assert status == "success", text
    job = env.only_job()
    status, text = _sched_call(env, job.id, action="create", name="Child", prompt="q", when="rate(3 hours)")
    assert status == "success", text
    child = next(j for j in env.service.list_jobs() if j.name == "Child")
    assert child.created_by == TEAM and child.notify == job.notify


# ---- a scheduled job never posts to a Slack DM -------------------------------------------------

DM_B = {"slackChannelId": "D0BBB", "slackUserId": "U0BEA", "slackThreadTs": THREAD, "triggerSource": "slack:U0BEA"}
HOME_CONFIG = Config.load(HOME_ENV)
NO_HOME_CONFIG = Config.load({})


def _posts(env: Env, config: Config = HOME_CONFIG) -> list[dict[str, str]]:
    """The Slack payload keys each launched run got."""
    return [dispatch.notify_context(job, config) for job in env.launched]


def _no_dm(contexts: list[dict[str, str]]) -> bool:
    return all(not str(v).startswith("D") for keys in contexts for v in keys.values())


def _legacy_dm_b_job(env: Env) -> Any:
    """A row written before this rule: DM-B's job still storing DM-B as its target."""
    status, text = env.call(DM_B, action="create", name="Remind B", prompt="p", when="rate(2 hours)")
    assert status == "success", text
    job = next(j for j in env.service.list_jobs() if j.name == "Remind B")
    env.service.store._client.items[(job.id, "job")]["notify"] = {
        "kind": "slack", "channel": "D0BBB", "thread_ts": THREAD, "set_from": "D0BBB",
    }
    return env.service.get(job.id)


def test_a_job_created_from_a_dm_reports_to_the_home_channel(env):
    status, text = env.call(DM_B, action="create", name="Remind me", prompt="p", when="2030-01-02T15:00")
    assert status == "success", text
    assert "never post to a Slack DM" in text and f"home channel {HOME}" in text
    job = env.only_job()
    assert job.notify is None
    env.call(DM_B, action="run_now", job_id=job.id)
    assert _posts(env) == [{"slackChannelId": HOME}]


def test_with_the_home_channel_off_a_dm_job_posts_nowhere(env):
    env.home = None
    status, text = env.call(DM_B, action="create", name="Remind me", prompt="p", when="2030-01-02T15:00")
    assert status == "success", text
    assert "never post to a Slack DM" in text and "nowhere outside the run's own session" in text
    env.call(DM_B, action="run_now", job_id=env.only_job().id)
    assert _posts(env, NO_HOME_CONFIG) == [{}]


@pytest.mark.parametrize("legacy", [False, True])
def test_a_run_in_dm_a_editing_a_dm_b_job_posts_nothing_to_dm_b(env, legacy):
    """The cross-DM rewrite: a run in DM-A rewrites a job DM-B created; neither run_now nor the
    next fire posts to DM-B."""
    if legacy:
        job = _legacy_dm_b_job(env)
    else:
        env.call(DM_B, action="create", name="Remind B", prompt="p", when="rate(2 hours)")
        job = env.only_job()
    status, text = env.call(SLACK, action="update", job_id=job.id, prompt="attacker text", notify="slack:D0BBB")
    assert status == "error" and "never posts to a Slack DM" in text
    status, text = env.call(SLACK, action="update", job_id=job.id, prompt="attacker text")
    assert status == "success", text
    edited = env.service.get(job.id)
    assert edited.prompt == "attacker text" and edited.notify is None
    env.call(SLACK, action="run_now", job_id=job.id)
    env.now = NOW + timedelta(hours=2)
    assert env.service.fire(job.id, scheduled_time="2030-01-01T14:00:00Z") == "dispatched"
    assert _posts(env) == [{"slackChannelId": HOME}] * 2
    assert _posts(env, NO_HOME_CONFIG) == [{}] * 2


@pytest.mark.parametrize("event", [SLACK, DM_B, SLACK_HOME, PORTAL, GITHUB])
def test_run_now_of_a_legacy_dm_job_posts_to_no_dm(env, event):
    job = _legacy_dm_b_job(env)
    env.call(event, action="run_now", job_id=job.id)
    assert env.launched, "run_now did not launch"
    assert _posts(env) == [{}] and _no_dm(_posts(env, NO_HOME_CONFIG))


@pytest.mark.parametrize(("event", "notify"), [
    (SLACK, "slack:D0AAA"), (SLACK, f"slack:D0AAA/{THREAD}"), (SLACK, "slack:D0BBB"),
    (SLACK, f"slack:{HOME}"),  # a DM run is not in the home channel
    (SLACK_HOME, "slack:D0AAA"), (SLACK_HOME, "slack:C0OTHER"), (SLACK_HOME, "slack:G0OTHER"),
    (PORTAL, f"slack:{HOME}"),
])
def test_the_tool_refuses_a_dm_or_arbitrary_channel(env, event, notify):
    status, text = env.call(event, action="create", name="x", prompt="p", when="2030-01-02T15:00", notify=notify)
    assert status == "error" and env.service.list_jobs() == []


def test_a_scheduled_run_of_a_legacy_dm_job_passes_on_no_dm(env):
    job = _legacy_dm_b_job(env)
    status, text = _sched_call(env, job.id, action="create", name="Follow-up", prompt="q", when="rate(3 hours)")
    assert status == "success", text
    child = next(j for j in env.service.list_jobs() if j.name == "Follow-up")
    assert child.notify is None
    status, text = _sched_call(env, job.id, action="update", job_id=child.id, notify="slack:D0BBB")
    assert status == "error" and env.service.get(child.id).notify is None
