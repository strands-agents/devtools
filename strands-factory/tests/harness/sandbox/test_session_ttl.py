"""The sandbox's sliding session TTL: one EventBridge Scheduler schedule per sandbox session.

Every wake and every command upserts a one-time schedule whose universal target calls
``DeleteCapacityProviderSession`` 5 days later (a sessionless invoke's throwaway session: one fixed
schedule, the agent's max lifetime + 1 day out); a later use moves it (same name). Fail-soft,
throttled per process, off the event loop. No AWS: a fake Scheduler client records the calls.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from test_runtime_sandbox import RUNTIME_ARN, FakeAgentCoreClient, FakeClientError

from strandly.core.config import Config
from strandly.core.constants import (
    AGENT_MAX_LIFETIME_SECONDS,
    SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS,
    SANDBOX_SESSION_TTL_SECONDS,
    SANDBOX_TTL_BUMP_INTERVAL_SECONDS,
    SANDBOX_TTL_FAILURE_RETRY_SECONDS,
    SANDBOX_TTL_THROTTLE_MAX_SESSIONS,
)
from strandly.ops import metrics
from strandly.sandbox import select as select_module
from strandly.sandbox import ttl as ttl_module
from strandly.sandbox.runtime import RuntimeSandbox
from strandly.sandbox.select import build_sandbox
from strandly.sandbox.ttl import (
    SessionTtl,
    SessionTtlConfig,
    at_expression,
    schedule_name,
    schedule_request,
)

CP_ID = "strandly_dev_sandbox_ec2-AbCdE12345"
GROUP = "strandly-dev-sandbox-ttl"
ROLE = "arn:aws:iam::123456789012:role/strandly-dev-sandbox-ttl-scheduler"
CONFIG = SessionTtlConfig(CP_ID, GROUP, ROLE)
NOW = datetime(2026, 3, 1, 12, 30, 45, 999_000, tzinfo=timezone.utc)
SCHEDULE_NAME_RE = re.compile(r"^[0-9a-zA-Z\-_.]{1,64}$")


class FakeScheduler:
    def __init__(self, *, create_fails: list[Exception] | None = None) -> None:
        self.creates: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.create_fails = list(create_fails or [])
        self.update_fails: list[Exception] = []

    def create_schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.creates.append(kwargs)
        if self.create_fails:
            raise self.create_fails.pop(0)
        return {"ScheduleArn": f"arn:aws:scheduler:us-west-2:1:schedule/{GROUP}/{kwargs['Name']}"}

    def update_schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        if self.update_fails:
            raise self.update_fails.pop(0)
        return {"ScheduleArn": "x"}


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh_throttle():
    ttl_module.reset_throttle()
    yield
    ttl_module.reset_throttle()


@pytest.fixture
def emitted(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "emit", lambda m, **kw: calls.append(dict(m)) or True)
    return calls


def _inline(fn):
    """Run a "background" upsert inline, so the wake/command tests can assert on it directly."""
    return fn()


def _ttl(client: FakeScheduler, clock: Clock | None = None) -> SessionTtl:
    return SessionTtl(
        CONFIG, client=client, clock=clock or Clock(), now=lambda: NOW, submit=_inline
    )


# ---- the request ------------------------------------------------------------------------------


def test_schedule_name_is_deterministic_legal_and_per_session():
    a = schedule_name(CP_ID, "sbx-session-a" + "x" * 90)
    assert a == schedule_name(CP_ID, "sbx-session-a" + "x" * 90)
    assert SCHEDULE_NAME_RE.match(a) and a.startswith("ttl-") and len(a) == 44
    assert a != schedule_name(CP_ID, "sbx-session-b" + "x" * 90)
    # Same session id on another capacity provider (a generation bump) is another schedule.
    assert a != schedule_name("strandly_dev_sandbox_xl_g2-ZzZzZ12345", "sbx-session-a" + "x" * 90)
    # The two ids are separated, so moving characters across the boundary is another schedule.
    assert schedule_name("ab", "c") != schedule_name("a", "bc")


def test_at_expression_is_utc_to_the_second():
    assert at_expression(NOW) == "at(2026-03-01T12:30:45)"
    plus_two = timezone(timedelta(hours=2))
    assert (
        at_expression(datetime(2026, 3, 1, 14, 30, 45, tzinfo=plus_two))
        == "at(2026-03-01T12:30:45)"
    )
    assert (
        at_expression(datetime(2026, 3, 1, 12, 30, 45)) == "at(2026-03-01T12:30:45)"
    )  # naive = UTC


def test_schedule_request_shape():
    req = schedule_request(CONFIG, "sbx-s1", NOW)
    assert req["Name"] == schedule_name(CP_ID, "sbx-s1")
    assert req["GroupName"] == GROUP
    assert req["ScheduleExpression"] == at_expression(
        NOW + timedelta(seconds=SANDBOX_SESSION_TTL_SECONDS)
    )
    assert req["ScheduleExpression"] == "at(2026-03-06T12:30:45)"  # 5 days
    assert req["ScheduleExpressionTimezone"] == "UTC"
    assert req["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert req["ActionAfterCompletion"] == "DELETE"
    target = req["Target"]
    assert (
        target["Arn"]
        == "arn:aws:scheduler:::aws-sdk:bedrockagentcore:deleteCapacityProviderSession"
    )
    assert target["RoleArn"] == ROLE
    # Universal-target input: the API request members in PascalCase.
    assert json.loads(target["Input"]) == {"CapacityProviderId": CP_ID, "SessionId": "sbx-s1"}
    retry = target["RetryPolicy"]
    assert 1 <= retry["MaximumRetryAttempts"] <= 185
    assert 60 <= retry["MaximumEventAgeInSeconds"] <= 86_400
    assert len(req["Description"]) <= 512


def test_the_request_validates_against_the_botocore_scheduler_model():
    """The kwargs are exactly what botocore's CreateSchedule/UpdateSchedule accept (no network)."""
    from botocore.session import get_session
    from botocore.validate import ParamValidator

    model = get_session().get_service_model("scheduler")
    req = schedule_request(CONFIG, "sbx-s1", NOW)
    for op in ("CreateSchedule", "UpdateSchedule"):
        report = ParamValidator().validate(req, model.operation_model(op).input_shape)
        assert not report.has_errors(), report.generate_report()


def test_the_target_input_matches_the_agentcore_api_members():
    """PascalCase of the bedrock-agentcore model's own member names: a rename there breaks here."""
    from botocore.session import get_session

    shape = (
        get_session()
        .get_service_model("bedrock-agentcore")
        .operation_model("DeleteCapacityProviderSession")
        .input_shape
    )
    members = {name[0].upper() + name[1:] for name in shape.members}
    assert members == set(json.loads(schedule_request(CONFIG, "s", NOW)["Target"]["Input"]))
    assert set(shape.required_members) == {"capacityProviderId", "sessionId"}


def test_config_needs_all_three_values():
    assert SessionTtlConfig.from_values(CP_ID, GROUP, ROLE) == CONFIG
    assert SessionTtlConfig.from_values(CP_ID, GROUP, None) is None
    assert SessionTtlConfig.from_values(None, None, None) is None
    assert SessionTtlConfig.from_values("", GROUP, ROLE) is None


# ---- upsert -----------------------------------------------------------------------------------


def test_first_bump_creates(emitted):
    client = FakeScheduler()
    assert _ttl(client).bump("sbx-s1") is True
    assert len(client.creates) == 1 and client.updates == []
    assert emitted == [{metrics.SANDBOX_TTL_UPSERTS: 1, metrics.SANDBOX_TTL_UPSERT_FAILED: 0}]


def test_conflict_falls_through_to_a_full_update(emitted):
    client = FakeScheduler(create_fails=[FakeClientError("ConflictException", "exists")])
    assert _ttl(client).bump("sbx-s1") is True
    assert client.updates == client.creates  # the same full definition replaces the old one
    assert emitted[-1][metrics.SANDBOX_TTL_UPSERT_FAILED] == 0


def test_a_later_bump_slides_the_same_schedule():
    client, clock = FakeScheduler(), Clock()
    ttl = _ttl(client, clock)
    ttl.bump("sbx-s1")
    clock.t += SANDBOX_TTL_BUMP_INTERVAL_SECONDS + 1
    later = NOW + timedelta(hours=2)
    ttl._now = lambda: later
    client.create_fails = [FakeClientError("ConflictException")]
    assert ttl.bump("sbx-s1") is True
    assert client.updates[0]["Name"] == client.creates[0]["Name"]
    assert client.updates[0]["ScheduleExpression"] == at_expression(
        later + timedelta(seconds=SANDBOX_SESSION_TTL_SECONDS)
    )


def test_throttled_within_the_interval_per_session():
    client, clock = FakeScheduler(), Clock()
    ttl = _ttl(client, clock)
    assert ttl.bump("sbx-s1") is True
    clock.t += SANDBOX_TTL_BUMP_INTERVAL_SECONDS - 1
    assert ttl.bump("sbx-s1") is False
    # The throttle is process-wide (a new RuntimeSandbox per run shares it) and per session.
    assert _ttl(client, clock).bump("sbx-s1") is False
    assert ttl.bump("sbx-s2") is True
    clock.t += 2
    assert ttl.bump("sbx-s1") is True
    assert len(client.creates) == 3


@pytest.mark.parametrize(
    "error",
    [
        FakeClientError("AccessDeniedException"),
        FakeClientError("ThrottlingException"),
        OSError("dns"),
    ],
)
def test_failures_are_soft_counted_and_retried_after_a_short_backoff(emitted, caplog, error):
    client, clock = FakeScheduler(create_fails=[error]), Clock()
    ttl = _ttl(client, clock)
    assert ttl.bump("sbx-s1") is False  # never raises
    assert emitted == [{metrics.SANDBOX_TTL_UPSERTS: 1, metrics.SANDBOX_TTL_UPSERT_FAILED: 1}]
    assert "sandbox session TTL upsert failed" in caplog.text
    # Not hammered on every command while Scheduler is down ...
    clock.t += SANDBOX_TTL_FAILURE_RETRY_SECONDS - 1
    assert ttl.bump("sbx-s1") is False and len(client.creates) == 1
    # ... but retried by the next use after the short backoff, not suppressed for an hour.
    clock.t += 1
    assert SANDBOX_TTL_FAILURE_RETRY_SECONDS < SANDBOX_TTL_BUMP_INTERVAL_SECONDS
    assert ttl.bump("sbx-s1") is True and len(client.creates) == 2
    # A success then throttles normally.
    assert ttl.bump("sbx-s1") is False


def test_the_throttle_is_bounded_lru():
    client, clock = FakeScheduler(), Clock()
    ttl = _ttl(client, clock)
    for i in range(SANDBOX_TTL_THROTTLE_MAX_SESSIONS + 10):
        ttl.bump(f"sbx-{i}")
    assert len(ttl_module._bumped) == SANDBOX_TTL_THROTTLE_MAX_SESSIONS
    # The oldest were evicted (one extra upsert each); the newest are still throttled.
    assert ttl.bump("sbx-0") is True
    assert ttl.bump(f"sbx-{SANDBOX_TTL_THROTTLE_MAX_SESSIONS + 9}") is False


def test_an_ephemeral_session_is_scheduled_once_past_the_longest_run():
    client, clock = FakeScheduler(), Clock()
    ttl = _ttl(client, clock)
    assert SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS >= AGENT_MAX_LIFETIME_SECONDS + 24 * 3600
    assert ttl.bump("ephemeral-1", ephemeral=True) is True
    clock.t += 10 * SANDBOX_TTL_BUMP_INTERVAL_SECONDS
    assert ttl.bump("ephemeral-1", ephemeral=True) is False  # fixed, not slid
    assert len(client.creates) == 1


def test_a_failed_update_after_a_conflict_is_soft(emitted):
    client = FakeScheduler(create_fails=[FakeClientError("ConflictException")])
    client.update_fails = [FakeClientError("ValidationException")]
    assert _ttl(client).bump("sbx-s1") is False
    assert emitted[-1][metrics.SANDBOX_TTL_UPSERT_FAILED] == 1


def test_no_client_is_built_until_the_first_bump(monkeypatch):
    built: list[str | None] = []
    fake = FakeScheduler()

    def factory(region):
        built.append(region)
        return fake

    monkeypatch.setattr(ttl_module, "_scheduler_client", factory)
    ttl = SessionTtl(CONFIG, region="us-west-2", now=lambda: NOW)
    assert built == []  # constructing (or importing) builds nothing: Runtime V2 snapshot-safe
    ttl.bump("sbx-s1")
    assert built == ["us-west-2"] and len(fake.creates) == 1


# ---- the wake path ----------------------------------------------------------------------------


INIT_TIMEOUT = (
    "Runtime initialization time exceeded. Please make sure that initialization completes in 120s."
)


@pytest.fixture(autouse=True)
def _no_wake_delays(monkeypatch):
    from strandly.sandbox import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_sleep", lambda _seconds: None)


def _sandbox(
    tmp_path, *, ttl: SessionTtl | None, **kwargs: Any
) -> tuple[RuntimeSandbox, FakeAgentCoreClient]:
    workdir = tmp_path / "workspace"
    workdir.mkdir(exist_ok=True)
    client = FakeAgentCoreClient(workdir=str(workdir))
    kwargs.setdefault("session_key", "gh-strands-agents-sdk-python-b00b0499-issue-42")
    sandbox = RuntimeSandbox(
        runtime_arn=RUNTIME_ARN, workdir=str(workdir), client=client, ttl=ttl, **kwargs
    )
    return sandbox, client


def _expires(req: dict[str, Any]) -> datetime:
    when = req["ScheduleExpression"].removeprefix("at(").removesuffix(")")
    return datetime.fromisoformat(when).replace(tzinfo=timezone.utc)


def test_a_wake_schedules_the_session(tmp_path):
    scheduler = FakeScheduler()
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler))
    sandbox._wake()
    assert client.wakes == [sandbox.session_id]
    (req,) = scheduler.creates
    assert json.loads(req["Target"]["Input"])["SessionId"] == sandbox.session_id
    assert _expires(req) - NOW.replace(microsecond=0) == timedelta(
        seconds=SANDBOX_SESSION_TTL_SECONDS
    )


def test_a_sessionless_invoke_gets_a_fixed_ttl_past_the_longest_run(tmp_path):
    """Its throwaway ``ephemeral-<uuid>`` session is never reused by another run: one schedule, set
    at the first wake, that no run (max lifetime 14 days) can outlive."""
    scheduler = FakeScheduler()
    sandbox, _ = _sandbox(tmp_path, ttl=_ttl(scheduler), session_key=None)
    sandbox._wake()
    (req,) = scheduler.creates
    assert "ephemeral" in json.loads(req["Target"]["Input"])["SessionId"]
    assert _expires(req) - NOW.replace(microsecond=0) == timedelta(
        seconds=SANDBOX_EPHEMERAL_SESSION_TTL_SECONDS
    )


def test_an_explicit_session_id_is_scheduled_under_that_id(tmp_path):
    """A caller that names the session itself (session_id ≠ a derived key) still gets a TTL, for
    exactly the id it uses — the sliding one, since it may wake it again."""
    scheduler = FakeScheduler()
    explicit = "sandbox-explicit-session-id-0123456789abcdef"
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler), session_key=None, session_id=explicit)
    sandbox._wake()
    assert client.wakes == [explicit]
    (req,) = scheduler.creates
    assert json.loads(req["Target"]["Input"])["SessionId"] == explicit
    assert _expires(req) - NOW.replace(microsecond=0) == timedelta(
        seconds=SANDBOX_SESSION_TTL_SECONDS
    )


def test_no_ttl_wiring_schedules_nothing(tmp_path):
    sandbox, client = _sandbox(tmp_path, ttl=None)
    sandbox._wake()
    assert client.wakes  # and nothing else happened


def test_a_restart_rewakes_and_slides_the_same_schedule(tmp_path):
    clock = Clock()
    scheduler = FakeScheduler()
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler, clock))
    sandbox._wake()
    clock.t += SANDBOX_TTL_BUMP_INTERVAL_SECONDS + 1
    sandbox._restart_session_sync("test: 502 burst")
    names = {r["Name"] for r in scheduler.creates}
    assert len(scheduler.creates) == 2 and len(names) == 1, "same session, same schedule"
    assert client.stops == [sandbox.session_id]


def test_a_failing_ttl_never_fails_the_wake(tmp_path, emitted):
    scheduler = FakeScheduler(create_fails=[FakeClientError("AccessDeniedException")])
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler))
    sandbox._wake()  # does not raise
    assert client.wakes == [sandbox.session_id]
    assert emitted[-1][metrics.SANDBOX_TTL_UPSERT_FAILED] == 1


def test_a_wake_rejected_outright_schedules_nothing(tmp_path):
    """An access or validation error means no session was started: nothing to delete."""
    scheduler = FakeScheduler()
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler))
    client.invoke_agent_runtime = _raise(FakeClientError("AccessDeniedException"))  # type: ignore[method-assign]
    with pytest.raises(FakeClientError):
        sandbox._wake()
    assert scheduler.creates == []


def test_a_wake_that_gives_up_on_transient_errors_still_schedules_the_session(tmp_path):
    """Init timeouts can leave a launched instance and an attached volume behind: when the wake gives
    up, the session is scheduled for deletion all the same (closing the never-woke gap)."""
    from strandly.sandbox.runtime import SandboxUnavailableError

    scheduler = FakeScheduler()
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler))
    client.invoke_agent_runtime = _raise(  # type: ignore[method-assign]
        FakeClientError("RuntimeClientError", INIT_TIMEOUT)
    )
    with pytest.raises(SandboxUnavailableError):
        sandbox._wake()
    (req,) = scheduler.creates
    assert json.loads(req["Target"]["Input"])["SessionId"] == sandbox.session_id


def _raise(error: Exception):
    def boom(**_: Any) -> Any:
        raise error

    return boom


# ---- wiring -----------------------------------------------------------------------------------


def test_build_sandbox_wires_the_ttl_only_with_all_three_values():
    arn = {"STRANDLY_SANDBOX_RUNTIME_ARN": RUNTIME_ARN, "AWS_REGION": "us-west-2"}
    wired = {
        "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": CP_ID,
        "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": GROUP,
        "STRANDLY_SANDBOX_TTL_ROLE_ARN": ROLE,
    }
    sandbox = build_sandbox(Config(values={**arn, **wired}))
    assert isinstance(sandbox, RuntimeSandbox)
    assert sandbox._ttl is not None and sandbox._ttl.config == CONFIG
    partial = {k: v for k, v in wired.items() if k != "STRANDLY_SANDBOX_TTL_ROLE_ARN"}
    sandbox = build_sandbox(Config(values={**arn, **partial}))
    assert isinstance(sandbox, RuntimeSandbox) and sandbox._ttl is None


# ---- the command path: every use slides the TTL, off the event loop ---------------------------


async def test_commands_keep_sliding_the_ttl_after_one_wake(tmp_path):
    """A run that wakes once and then uses its sandbox for days must not be deleted mid-run: every
    command slides the schedule (once per throttle interval), not only the wake."""
    clock = Clock()
    scheduler = FakeScheduler()
    sandbox, client = _sandbox(tmp_path, ttl=_ttl(scheduler, clock))
    for _ in range(4):
        result = await sandbox.execute("true")
        assert result.exit_code == 0
        clock.t += SANDBOX_TTL_BUMP_INTERVAL_SECONDS / 2 + 1  # 4 commands over > 1.5 intervals
    assert client.wakes == [sandbox.session_id]  # exactly one wake ...
    assert len(scheduler.creates) == 2  # ... and the commands moved the TTL again
    assert len({r["Name"] for r in scheduler.creates}) == 1
    # File operations are commands too.
    clock.t += SANDBOX_TTL_BUMP_INTERVAL_SECONDS + 1
    await sandbox.write_file(str(tmp_path / "workspace" / "f.txt"), b"x")
    assert len(scheduler.creates) == 3


async def test_a_failed_upsert_is_retried_by_the_next_command(tmp_path, emitted):
    clock = Clock()
    scheduler = FakeScheduler(create_fails=[FakeClientError("ThrottlingException")])
    sandbox, _ = _sandbox(tmp_path, ttl=_ttl(scheduler, clock))
    await sandbox.execute("true")  # the wake's upsert fails
    assert emitted[-1][metrics.SANDBOX_TTL_UPSERT_FAILED] == 1
    clock.t += SANDBOX_TTL_FAILURE_RETRY_SECONDS + 1
    await sandbox.execute("true")  # a command, not a wake, retries it
    assert len(scheduler.creates) == 2
    assert emitted[-1][metrics.SANDBOX_TTL_UPSERT_FAILED] == 0


class SlowScheduler(FakeScheduler):
    """A degraded Scheduler: every call blocks until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def create_schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.entered.set()
        self.release.wait(30)
        return super().create_schedule(**kwargs)


async def test_neither_the_wake_nor_a_command_waits_on_the_scheduler(tmp_path):
    scheduler = SlowScheduler()
    # The real background pool, not the inline runner.
    ttl = SessionTtl(CONFIG, client=scheduler, clock=Clock(), now=lambda: NOW)
    sandbox, client = _sandbox(tmp_path, ttl=ttl)
    try:
        started = time.monotonic()
        result = await asyncio.wait_for(sandbox.execute("echo hi"), timeout=20)
        assert result.stdout.strip() == "hi"
        assert scheduler.entered.wait(5), "the upsert was queued"
        await sandbox.execute("true")  # still stuck in the Scheduler: does not wait, queues nothing
        assert time.monotonic() - started < 15
        assert scheduler.creates == []  # nothing has completed yet
    finally:
        scheduler.release.set()
        assert ttl_module.drain(10)
    assert len(scheduler.creates) == 1, "one upsert in flight per session, not one per command"


def test_bump_soon_is_fire_and_forget(monkeypatch):
    queued: list[Any] = []
    client = FakeScheduler()
    ttl = SessionTtl(CONFIG, client=client, clock=Clock(), now=lambda: NOW, submit=queued.append)
    assert ttl.bump_soon("sbx-s1") is True
    assert ttl.bump_soon("sbx-s1") is False  # already in flight
    assert client.creates == []  # nothing ran on the caller's thread
    queued.pop()()
    assert len(client.creates) == 1
    assert ttl.bump_soon("sbx-s1") is False  # throttled now


def test_a_pool_that_refuses_work_is_soft(caplog):
    def refuse(_fn: Any) -> Any:
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    ttl = SessionTtl(CONFIG, client=FakeScheduler(), clock=Clock(), now=lambda: NOW, submit=refuse)
    assert ttl.bump_soon("sbx-s1") is False
    assert "not queued" in caplog.text
    assert ttl_module._inflight == set()


_ARN = {"STRANDLY_SANDBOX_RUNTIME_ARN": RUNTIME_ARN, "AWS_REGION": "us-west-2"}
_WIRED = {
    "STRANDLY_SANDBOX_CAPACITY_PROVIDER_ID": CP_ID,
    "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP": GROUP,
    "STRANDLY_SANDBOX_TTL_ROLE_ARN": ROLE,
}


@pytest.fixture
def fresh_ttl_warning():
    select_module._reset_ttl_warning()
    yield
    select_module._reset_ttl_warning()


@pytest.mark.parametrize(
    "unset",
    [
        ["STRANDLY_SANDBOX_TTL_ROLE_ARN"],
        ["STRANDLY_SANDBOX_TTL_ROLE_ARN", "STRANDLY_SANDBOX_TTL_SCHEDULE_GROUP"],
    ],
)
def test_a_partial_ttl_wiring_warns_and_counts_once_per_process(
    fresh_ttl_warning, emitted, caplog, unset
):
    partial = {k: v for k, v in _WIRED.items() if k not in unset}
    for _ in range(3):  # a sandbox per run: still one warning, one count
        sandbox = build_sandbox(Config(values={**_ARN, **partial}))
        assert isinstance(sandbox, RuntimeSandbox) and sandbox._ttl is None
    warnings = [r for r in caplog.records if "sandbox session TTL is OFF" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelname == "WARNING"
    assert all(name in warnings[0].getMessage() for name in unset)
    assert emitted == [{metrics.SANDBOX_TTL_MISCONFIGURED: 1}]


def test_a_runtime_sandbox_with_no_ttl_wiring_warns_once(fresh_ttl_warning, emitted, caplog):
    build_sandbox(Config(values=_ARN))
    build_sandbox(Config(values=_ARN))
    warnings = [r for r in caplog.records if "sandbox session TTL is OFF" in r.getMessage()]
    assert len(warnings) == 1 and "none of" in warnings[0].getMessage()
    assert emitted == [{metrics.SANDBOX_TTL_MISCONFIGURED: 1}]


def test_a_full_ttl_wiring_is_silent(fresh_ttl_warning, emitted, caplog):
    sandbox = build_sandbox(Config(values={**_ARN, **_WIRED}))
    assert isinstance(sandbox, RuntimeSandbox) and sandbox._ttl is not None
    assert "sandbox session TTL is OFF" not in caplog.text and emitted == []
