"""Tests for the stuck-run detector (``strandly_harness.ops.lambdas.stuck_runs``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from strandly_harness.core.config import Config
from strandly_harness.ops import metrics
from strandly_harness.ops.lambdas import stuck_runs

_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _row(task_id, status, *, age_minutes=None):
    row = {"task_id": task_id, "status": status}
    if age_minutes is not None:
        row["started_at"] = (_NOW - timedelta(minutes=age_minutes)).isoformat()
    return row


# ---- find_stuck (pure) -----------------------------------------------------------------

def test_find_stuck_flags_only_old_running_rows():
    rows = [
        _row("old-running", "running", age_minutes=45),
        _row("fresh-running", "running", age_minutes=5),
        _row("old-completed", "completed", age_minutes=90),
        _row("old-failed", "failed", age_minutes=90),
    ]
    assert stuck_runs.find_stuck(rows, now=_NOW, threshold_minutes=30) == ["old-running"]


def test_find_stuck_missing_timestamp_is_not_stuck():
    # No started_at → can't prove it's old → not flagged (avoid false-alarm bias).
    rows = [_row("no-ts", "running"), {"status": "running"}, "garbage"]
    assert stuck_runs.find_stuck(rows, now=_NOW, threshold_minutes=30) == []


def test_find_stuck_sorted():
    rows = [
        _row("zzz", "running", age_minutes=60),
        _row("aaa", "running", age_minutes=60),
    ]
    assert stuck_runs.find_stuck(rows, now=_NOW, threshold_minutes=30) == ["aaa", "zzz"]


# ---- scan_running (fail-soft seam) -----------------------------------------------------

class _FakeTable:
    def __init__(self, items=None, raises=False):
        self._items = items or []
        self._raises = raises
        self.kwargs = None

    def query(self, **kwargs):
        self.kwargs = kwargs
        if self._raises:
            raise RuntimeError("boom")
        return {"Items": self._items}


def test_scan_running_queries_recent_gsi():
    table = _FakeTable(items=[_row("a", "running", age_minutes=60)])
    errors: list[str] = []
    rows = stuck_runs.scan_running(table, errors=errors)
    assert rows and rows[0]["task_id"] == "a"
    assert errors == []
    assert table.kwargs["IndexName"] == "recent"


def test_scan_running_failsoft_records_error():
    table = _FakeTable(raises=True)
    errors: list[str] = []
    assert stuck_runs.scan_running(table, errors=errors) == []
    assert errors and "ledger scan failed" in errors[0]


# ---- check (orchestration) -------------------------------------------------------------

def _config(**extra):
    values = {"STRANDLY_RUN_LEDGER_TABLE": "strandly-dev-runledger", **extra}
    return Config(values=values)


def test_check_no_ledger_is_noop():
    report = stuck_runs.check(Config(values={}), now=_NOW)
    assert report.stuck == []
    assert report.errors and "run-ledger not configured" in report.errors[0]


def test_check_finds_stuck_and_emits_gauge(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    table = _FakeTable(
        items=[
            _row("stuck1", "running", age_minutes=120),
            _row("fresh", "running", age_minutes=2),
        ]
    )
    report = stuck_runs.check(_config(), now=_NOW, table=table)
    assert report.stuck == ["stuck1"]
    assert report.scanned == 2

    # The StuckRuns gauge was emitted (value = number stuck).
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.STUCK_RUNS] == 1
    assert doc["surface"] == metrics.SURFACE_MONITORING


def test_check_clean_emits_zero_gauge(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    table = _FakeTable(items=[_row("fresh", "running", age_minutes=2)])
    report = stuck_runs.check(_config(), now=_NOW, table=table)
    assert report.ok is True
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.STUCK_RUNS] == 0  # continuous series even when clean


def test_check_custom_threshold():
    table = _FakeTable(items=[_row("r", "running", age_minutes=10)])
    # default threshold 30 → not stuck; threshold 5 → stuck.
    assert stuck_runs.check(_config(), now=_NOW, table=table).stuck == []
    cfg = _config(STRANDLY_STUCK_RUN_MINUTES="5")
    assert stuck_runs.check(cfg, now=_NOW, table=table).stuck == ["r"]


# ---- notify ----------------------------------------------------------------------------

def test_notify_skips_without_topic():
    report = stuck_runs.StuckReport(stuck=["x"])
    assert stuck_runs.notify(report, _config()) is False


def test_notify_skips_when_clean():
    report = stuck_runs.StuckReport(stuck=[])
    cfg = _config(STRANDLY_MONITORING_SNS_TOPIC_ARN="arn:aws:sns:us-west-2:1:t")
    assert stuck_runs.notify(report, cfg) is False
