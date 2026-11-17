"""Tests for scheduled self-invocations — the job registry + the generic invoker (no AWS, no SDK).

The dispatch seam (``serving.runtime_client.launch_run``, via ``run_job``) is monkeypatched, so these
exercise the routing/wiring: which job a payload runs, the prompt/session assembly, fail-soft on a
bad job, and the noop on an empty invoke.
"""

from __future__ import annotations

import strandly_harness.ops.lambdas.scheduled.invoker as invoker
from strandly_harness.core.config import Config
from strandly_harness.ops.lambdas.scheduled.invoker import build_prompt, build_session_id
from strandly_harness.ops.lambdas.scheduled.jobs import JOBS, ScheduledJob, by_name

# ---- registry ----------------------------------------------------------------------------

def test_jobs_have_unique_names():
    names = [j.name for j in JOBS]
    assert len(names) == len(set(names)), "duplicate job names"


def test_by_name_found_and_missing():
    assert by_name("daily-activity-review") is not None
    assert by_name("nope") is None


def test_session_id_deterministic_per_date():
    job = by_name("daily-activity-review")
    assert build_session_id(job, date="20260101") == "sched-daily-activity-review-20260101"
    # Different dates → different threads.
    assert build_session_id(job, date="20260101") != build_session_id(job, date="20260102")


def test_build_prompt_prepends_skill_activation():
    job = ScheduledJob(name="x", schedule="rate(1 day)", prompt="Do the thing.", skill="code-review")
    p = build_prompt(job)
    assert p.startswith('skill(action="activate", name="code-review")')
    assert p.endswith("Do the thing.")


def test_build_prompt_without_skill_is_bare():
    job = ScheduledJob(name="x", schedule="rate(1 day)", prompt="Do the thing.")
    assert build_prompt(job) == "Do the thing."


# ---- invoker dispatch --------------------------------------------------------------------

def _config() -> Config:
    return Config(values={"STRANDLY_RUNTIME_ARN": "arn:rt", "AWS_REGION": "us-west-2"})


def test_dispatch_runs_named_job(monkeypatch):
    calls = {}

    def fake_run_job(job, config, **kw):
        calls["ran"] = job.name
        return {"status": "accepted", "taskId": "t1"}

    monkeypatch.setattr(invoker, "run_job", fake_run_job)
    out = invoker.dispatch_jobs(["daily-activity-review"], _config())
    assert out["status"] == "ok"
    assert out["dispatched"] == ["daily-activity-review"]
    assert calls["ran"] == "daily-activity-review"


def test_dispatch_unknown_job_is_soft(monkeypatch):
    monkeypatch.setattr(invoker, "run_job", lambda *a, **k: {"status": "accepted", "taskId": "t"})
    out = invoker.dispatch_jobs(["does-not-exist"], _config())
    assert out["results"]["does-not-exist"] == "unknown-job"
    assert out["dispatched"] == []


def test_dispatch_error_is_caught(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(invoker, "run_job", boom)
    out = invoker.dispatch_jobs(["daily-activity-review"], _config())
    assert out["status"] == "error"
    assert "kaboom" in out["results"]["daily-activity-review"]


def test_runtime_rejection_not_counted_dispatched(monkeypatch):
    monkeypatch.setattr(invoker, "run_job", lambda *a, **k: {"status": "error", "error": "x"})
    out = invoker.dispatch_jobs(["daily-activity-review"], _config())
    assert out["dispatched"] == []
    assert out["results"]["daily-activity-review"].startswith("error:")


# ---- lambda_handler event routing --------------------------------------------------------

def test_handler_reads_job_from_event(monkeypatch):
    seen = {}
    monkeypatch.setattr(invoker, "dispatch_jobs", lambda names, *a, **k: seen.setdefault("names", names) or {"status": "ok"})
    invoker.lambda_handler({"job": "daily-activity-review"})
    assert seen["names"] == ["daily-activity-review"]


def test_handler_reads_jobs_list(monkeypatch):
    seen = {}
    monkeypatch.setattr(invoker, "dispatch_jobs", lambda names, *a, **k: seen.setdefault("names", names) or {"status": "ok"})
    invoker.lambda_handler({"jobs": ["a", "b"]})
    assert seen["names"] == ["a", "b"]


def test_handler_noop_on_empty_event():
    out = invoker.lambda_handler({})
    assert out["status"] == "noop" and out["dispatched"] == []
