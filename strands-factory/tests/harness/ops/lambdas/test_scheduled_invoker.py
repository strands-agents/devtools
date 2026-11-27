"""The scheduled-invoker Lambda (event routing) and the shared launch path."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import strandly.ops.lambdas.scheduled.invoker as invoker
from fake_schedules import FakeDynamoDB, FakeScheduler
from strandly.core.config import Config
from strandly.ops import runtime_client
from strandly.ops.schedules import dispatch
from strandly.ops.schedules.dispatch import RunOutcome
from strandly.ops.schedules.model import KIND_RECURRING, Actor, Job, NotifyTarget
from strandly.ops.schedules.service import JobSpec, ScheduleService
from strandly.ops.schedules.store import JobStore
from strandly.ops.schedules.timer import JobTimer, TimerSettings

UTC = timezone.utc
NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
ARN = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/strandly-test"


def _config() -> Config:
    return Config.load({"STRANDLY_RUNTIME_ARN": ARN, "AWS_REGION": "us-west-2"})


# ---- dispatch.launch ------------------------------------------------------------------------


class Launches:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response: object = {"status": "accepted", "taskId": "t1"}

    def __call__(self, arn, region, session_id, prompt, extra):
        self.calls.append(
            {"arn": arn, "region": region, "session": session_id, "prompt": prompt, **extra}
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def launches(monkeypatch) -> Launches:
    fake = Launches()
    monkeypatch.setattr(runtime_client, "launch_run", fake)
    return fake


def _job() -> Job:
    actor = Actor("portal:a", "dashboard")
    return Job(
        id="dependabot-review",
        name="dependabot-review",
        kind=KIND_RECURRING,
        expression="cron(0 9 * * ? *)",
        timezone="UTC",
        prompt="Review the open Dependabot PRs.",
        skill="dependabot-review",
        created_by=actor,
        updated_by=actor,
        created_at="",
        updated_at="",
    )


def test_launch_rejects_when_busy_and_names_the_trigger(launches):
    out = dispatch.launch(_job(), _config(), now=NOW, trigger_source="scheduled:dependabot-review")
    assert out == RunOutcome("dispatched", "sched-dependabot-review-20300101", task_id="t1")
    (call,) = launches.calls
    assert call["queueStrategy"] == "reject"
    assert call["triggerSource"] == "scheduled:dependabot-review"
    assert call["arn"] == ARN and call["region"] == "us-west-2"
    assert call["prompt"].startswith('skills(skill_name="dependabot-review")\n\n')


def test_launch_busy_is_a_skip(launches):
    launches.response = {"status": "rejected", "taskId": "t0"}
    assert dispatch.launch(_job(), _config(), now=NOW, trigger_source="x").status == "busy"


def test_launch_runtime_rejection_is_an_error(launches):
    launches.response = {"status": "error", "error": "x"}
    out = dispatch.launch(_job(), _config(), now=NOW, trigger_source="x")
    assert out.status == "error" and "did not accept" in (out.detail or "")


def test_launch_read_timeout_is_maybe_dispatched(launches):
    from botocore.exceptions import ReadTimeoutError

    launches.response = ReadTimeoutError(endpoint_url="https://bedrock-agentcore")
    out = dispatch.launch(_job(), _config(), now=NOW, trigger_source="x")
    assert out.status == "maybe-dispatched" and len(launches.calls) == 1


def test_launch_pre_send_error_is_an_error(launches):
    from botocore.exceptions import EndpointConnectionError

    launches.response = EndpointConnectionError(endpoint_url="https://x")
    assert dispatch.launch(_job(), _config(), now=NOW, trigger_source="x").status == "error"


def _home_config() -> Config:
    return Config.load(
        {
            "STRANDLY_RUNTIME_ARN": ARN,
            "AWS_REGION": "us-west-2",
            "STRANDLY_SLACK_BOT_TOKEN": "xoxb-1",
            "STRANDLY_SLACK_HOME_CHANNEL": "C0HOME",
            "STRANDLY_SLACK_HOME_CHANNEL_ENABLED": "true",
        }
    )


def test_launch_defaults_the_notify_target_to_the_home_channel(launches):
    dispatch.launch(_job(), _home_config(), now=NOW, trigger_source="x")
    prompt = launches.calls[0]["prompt"]
    assert prompt.startswith(_job().run_prompt())
    assert prompt.endswith(dispatch.HOME_NOTIFY_INSTRUCTION)
    assert "notify_team" in prompt and launches.calls[0]["slackChannelId"] == "C0HOME"


def test_launch_formats_an_explicit_home_target_as_a_notification(launches):
    from dataclasses import replace

    from strandly.ops.schedules.model import NotifyTarget

    job = replace(_job(), notify=NotifyTarget("slack", channel="C0HOME"))
    dispatch.launch(job, _home_config(), now=NOW, trigger_source="x")
    prompt = launches.calls[0]["prompt"]
    assert prompt == replace(job, notify=None).run_prompt() + dispatch.HOME_NOTIFY_INSTRUCTION


def test_launch_keeps_a_home_thread_target_as_a_thread_reply(launches):
    from dataclasses import replace

    from strandly.ops.schedules.model import NotifyTarget

    job = replace(_job(), notify=NotifyTarget("slack", channel="C0HOME", thread_ts="1712345678.000100"))
    dispatch.launch(job, _home_config(), now=NOW, trigger_source="x")
    assert launches.calls[0]["prompt"] == job.run_prompt()


def test_launch_keeps_the_jobs_own_notify_target(launches):
    from dataclasses import replace

    from strandly.ops.schedules.model import NotifyTarget

    job = replace(_job(), notify=NotifyTarget("github", url="https://github.com/o/r/issues/1"))
    dispatch.launch(job, _home_config(), now=NOW, trigger_source="x")
    assert launches.calls[0]["prompt"] == job.run_prompt()


def test_launch_without_a_home_channel_adds_no_notify_line(launches):
    dispatch.launch(_job(), _config(), now=NOW, trigger_source="x")
    assert launches.calls[0]["prompt"] == _job().run_prompt()


def test_launch_without_a_runtime_is_an_error(launches, monkeypatch):
    monkeypatch.setattr(runtime_client, "resolve_runtime_arn", lambda explicit=None: None)
    out = dispatch.launch(_job(), Config.load({}), now=NOW, trigger_source="x")
    assert out.status == "error" and launches.calls == []


# ---- lambda_handler ------------------------------------------------------------------------


class Env:
    def __init__(self) -> None:
        self.ddb = FakeDynamoDB()
        self.launched: list[tuple[str, str]] = []
        self.now = NOW
        self.scheduler = FakeScheduler(clock=lambda: self.now)
        self.service = ScheduleService(
            JobStore(self.ddb, "t"),
            JobTimer(
                self.scheduler,
                TimerSettings("strandly-test-jobs", "arn:fn", "arn:role"),
                clock=lambda: self.now,
            ),
            clock=lambda: self.now,
            sleep=lambda _s: None,
            config=object(),
            launcher=self._launch,
        )

    def _launch(self, job, config, *, now, trigger_source):
        self.launched.append((job.id, trigger_source))
        return RunOutcome("dispatched", job.session_id(now))


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()
    monkeypatch.setattr(invoker, "_service", lambda: e.service)
    monkeypatch.setitem(invoker._cold, "start", False)
    monkeypatch.delenv(invoker.ENV_ENABLED, raising=False)
    return e


def test_job_id_event_fires_the_table_job(env):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    out = invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-01T12:00:00Z"})
    assert out == {"status": "ok", "dispatched": ["nightly"], "results": {"nightly": "dispatched"}}
    again = invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-01T12:00:00Z"})
    assert again["status"] == "noop" and again["results"] == {"nightly": "duplicate"}
    assert env.launched == [("nightly", "scheduled:nightly")]


def test_job_id_of_a_deleted_job_is_a_noop(env):
    out = invoker.lambda_handler({"job_id": "gone"})
    assert out["status"] == "noop" and out["results"] == {"gone": "missing"}


def test_unconfigured_table(monkeypatch):
    monkeypatch.setattr(invoker, "_service", lambda: None)
    assert invoker.lambda_handler({"job_id": "x"})["results"] == {"x": "unconfigured"}
    assert invoker.lambda_handler({"reconcile": True})["status"] == "error"


def test_reconcile_event(env):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    env.scheduler.schedules.clear()
    out = invoker.lambda_handler({"reconcile": True})
    assert out == {"status": "ok", "reconcile": {"created": ["nightly"]}}


def test_empty_event_is_a_noop(env):
    assert invoker.lambda_handler({}) == {"status": "noop", "dispatched": [], "results": {}}
    assert invoker.lambda_handler(None)["status"] == "noop"


def test_schedules_disabled_env_skips_fires(env, monkeypatch):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    monkeypatch.setenv(invoker.ENV_ENABLED, "false")
    out = invoker.lambda_handler({"job_id": "nightly"})
    assert out["results"] == {"nightly": "disabled"} and env.launched == []


def test_only_job_id_names_a_job(env):
    env.service.create(
        JobSpec(id="nightly", name="nightly", expression="rate(1 day)", prompt="p"),
        Actor("portal:a", "dashboard"),
    )
    out = invoker.lambda_handler({"job": "nightly"})
    assert out == {"status": "noop", "dispatched": [], "results": {}} and env.launched == []


def test_handler_never_raises(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(env.service, "fire", boom)
    out = invoker.lambda_handler({"job_id": "x"})
    assert out["status"] == "error" and "logged internally" in out["results"]["error"]


def test_cold_start_resyncs_unsynced_rows_once(env, monkeypatch):
    env.scheduler.fail.add("create_schedule")
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    env.scheduler.fail.clear()
    monkeypatch.setitem(invoker._cold, "start", True)
    calls: list[dict] = []
    real = env.service.reconcile
    monkeypatch.setattr(env.service, "reconcile", lambda **kw: calls.append(kw) or real(**kw))
    invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-02T12:00:00Z"})
    invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-03T12:00:00Z"})
    assert calls == [{"only_unsynced": True, "max_late": 0}]
    assert ("strandly-test-jobs", "nightly") in env.scheduler.schedules


def test_cold_start_reconcile_failure_does_not_stop_the_fire(env, monkeypatch):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    monkeypatch.setitem(invoker._cold, "start", True)

    def boom(**kw):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(env.service, "reconcile", boom)
    out = invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-01T12:00:00Z"})
    assert out["status"] == "ok" and env.launched == [("nightly", "scheduled:nightly")]


# ---- logging ---------------------------------------------------------------------------------


@pytest.fixture
def in_lambda(monkeypatch):
    """A Lambda's logging: the root logger at WARNING, the package logger unset."""
    import logging

    package = logging.getLogger("strandly")
    root = logging.getLogger()
    saved = (package.level, root.level)
    package.setLevel(logging.NOTSET)
    root.setLevel(logging.WARNING)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "strandly-scheduled-invoker")
    monkeypatch.delenv("AWS_LAMBDA_LOG_LEVEL", raising=False)
    yield
    package.setLevel(saved[0])
    root.setLevel(saved[1])


def test_each_invocation_logs_one_summary_line_at_info(env, in_lambda, caplog):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-01T12:00:00Z"})
    invoker.lambda_handler({"job_id": "nightly", "scheduled_time": "2030-01-01T12:00:00Z"})
    invoker.lambda_handler({"reconcile": True})

    lines = [r.getMessage() for r in caplog.records if r.name == invoker.__name__]
    assert lines == [
        "scheduled: status=ok results={'nightly': 'dispatched'} reconcile={}",
        "scheduled: status=noop results={'nightly': 'duplicate'} reconcile={}",
        "scheduled: status=ok results={} reconcile={}",
    ]


# ---- {"launch": ...}: the agent's run_now --------------------------------------------------


def test_launch_event_runs_the_job_now_without_a_window_or_record(env):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    out = invoker.lambda_handler({"launch": {"job_id": "nightly"}})
    assert out == {
        "status": "ok",
        "outcome": {
            "status": "dispatched",
            "session_id": "sched-nightly-20300101",
            "task_id": None,
            "detail": None,
        },
    }
    assert invoker.lambda_handler({"launch": {"job_id": "nightly"}})["status"] == "ok"
    assert env.launched == [("nightly", "scheduled:nightly:manual")] * 2
    job = env.service.get("nightly")
    assert job.last_run is None and job.fire_window is None  # the caller records the run


def test_launch_event_for_an_unknown_job_is_a_structured_noop(env):
    out = invoker.lambda_handler({"launch": {"job_id": "nope"}})
    assert out["status"] == "noop" and out["outcome"]["status"] == "missing" and env.launched == []


def test_launch_carries_a_home_channel_thread_as_the_slack_payload_keys(launches):
    job = _job().with_changes(notify=NotifyTarget("slack", channel="C0HOME", thread_ts="1791234567.000100"))
    dispatch.launch(job, _home_config(), now=NOW, trigger_source="scheduled:dependabot-review")
    (call,) = launches.calls
    assert call["slackChannelId"] == "C0HOME" and call["slackThreadTs"] == "1791234567.000100"
    assert "use_slack" in call["prompt"]
    plain = _job()
    assert dispatch.notify_context(plain) == {}
    assert dispatch.notify_context(plain, _home_config()) == {"slackChannelId": "C0HOME"}
    # Without an enabled home channel no Slack target passes.
    assert dispatch.notify_context(job) == {}


@pytest.mark.parametrize("item", [
    {"kind": "slack", "channel": "D0VICTIM"},
    {"kind": "slack", "channel": "D0VICTIM", "thread_ts": "1.2", "set_from": "D0VICTIM"},
    {"kind": "slack", "channel": "D0VICTIM", "owner": "U0VICTIM"},
])
def test_launch_never_gives_a_run_a_dm(launches, item):
    """A legacy row's stored DM target, however it was recorded, posts nothing to that DM."""
    planted = _job().with_changes(
        notify=NotifyTarget.from_item(item), origin={"surface": "slack", "channel": "D0VICTIM"}
    )
    for config in (_config(), _home_config()):
        dispatch.launch(planted, config, now=NOW, trigger_source="scheduled:dependabot-review")
    for call in launches.calls:
        assert "slackChannelId" not in call and "slackThreadTs" not in call
        assert "D0VICTIM" not in call["prompt"] and "use_slack" not in call["prompt"]


@pytest.mark.parametrize("key", [invoker.ENV_ENABLED, "STRANDLY_SCHEDULED_ENABLED"])
def test_launch_kill_switch_refuses_before_loading_service(monkeypatch, key):
    monkeypatch.setenv(key, "false")
    def forbidden():
        raise AssertionError("disabled launch must not load clients")
    monkeypatch.setattr(invoker, "_service", forbidden)
    result = invoker.lambda_handler({"launch": {"job_id": "nightly"}})
    assert result == {"status": "noop", "outcome": {"status": "disabled", "session_id": ""}}


def test_invoker_exception_is_logged_not_returned(monkeypatch, caplog):
    def fail():
        raise RuntimeError("arn:aws:iam::123456789012:role/secret")
    monkeypatch.setattr(invoker, "_service", fail)
    result = invoker.lambda_handler({"launch": {"job_id": "nightly"}})
    assert result["status"] == "error"
    assert "123456789012" not in str(result) and "arn:" not in str(result)
    assert "arn:aws:iam::123456789012:role/secret" in caplog.text



@pytest.mark.parametrize("response", [{}, {"raw": "not json"}, [], {"status": "unexpected"}])
def test_unrecognized_launch_response_is_unknown_not_retryable(launches, response):
    launches.response = response
    outcome = dispatch.launch(_job(), _config(), now=NOW, trigger_source="x")
    assert outcome.status == "maybe-dispatched" and len(launches.calls) == 1


def test_undecodable_launch_response_is_unknown_not_retryable(launches):
    launches.response = UnicodeDecodeError("utf8", b"\xff", 0, 1, "bad byte")
    outcome = dispatch.launch(_job(), _config(), now=NOW, trigger_source="x")
    assert outcome.status == "maybe-dispatched" and len(launches.calls) == 1


@pytest.mark.parametrize("values,expected", [
    ({}, True),
    ({"STRANDLY_SCHEDULES_ENABLED": "true", "STRANDLY_SCHEDULED_ENABLED": "1"}, True),
    ({"STRANDLY_SCHEDULES_ENABLED": "false"}, False),
    ({"STRANDLY_SCHEDULED_ENABLED": "false"}, False),
    ({"STRANDLY_SCHEDULES_ENABLED": "true", "STRANDLY_SCHEDULED_ENABLED": "off"}, False),
    ({"STRANDLY_SCHEDULES_ENABLED": "  FALSE  "}, False),
    ({"STRANDLY_SCHEDULES_ENABLED": "invalid"}, False),
])
def test_shared_dispatch_kill_switch(values, expected):
    assert dispatch.schedules_enabled(values) is expected


@pytest.mark.parametrize("flag", ["STRANDLY_SCHEDULES_ENABLED", "STRANDLY_SCHEDULED_ENABLED"])
def test_direct_launcher_honors_kill_switch(launches, monkeypatch, flag):
    monkeypatch.setenv(flag, "false")
    assert dispatch.launch(_job(), _config(), now=NOW, trigger_source="x").status == "disabled"
    assert not launches.calls


def test_invoker_honors_legacy_kill_switch(env, monkeypatch):
    env.service.create(
        JobSpec(id="nightly", name="n", expression="rate(1 day)", prompt="p"), Actor("portal:a", "dashboard")
    )
    monkeypatch.setenv("STRANDLY_SCHEDULES_ENABLED", "true")
    monkeypatch.setenv("STRANDLY_SCHEDULED_ENABLED", "false")
    out = invoker.lambda_handler({"job_id": "nightly"})
    assert out["results"] == {"nightly": "disabled"} and env.launched == []


@pytest.mark.parametrize("token", ["", "test"])
@pytest.mark.parametrize("home", ["", "C0HOME"])
@pytest.mark.parametrize("flag", [None, "false", "invalid"])
def test_disabled_home_does_not_add_scheduler_posting(launches, token, home, flag):
    values = {"STRANDLY_RUNTIME_ARN": ARN, "AWS_REGION": "us-west-2",
              "STRANDLY_SLACK_BOT_TOKEN": token, "STRANDLY_SLACK_HOME_CHANNEL": home}
    if flag is not None:
        values["STRANDLY_SLACK_HOME_CHANNEL_ENABLED"] = flag
    dispatch.launch(_job(), Config(values=values), now=NOW, trigger_source="scheduled:test")
    call = launches.calls[0]
    assert "slackChannelId" not in call and "use_slack" not in call["prompt"]


def test_enabled_scheduler_accepts_only_the_home_channel():
    for channel, allowed in [("D0ORIGIN", False), ("C0HOME", True), ("C0OTHER", False), ("G0OTHER", False)]:
        job = _job().with_changes(notify=NotifyTarget("slack", channel=channel, thread_ts="1.2"))
        assert bool(dispatch.notify_context(job, _home_config())) is allowed
