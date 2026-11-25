"""Tests for the stuck-run detector (``strandly.ops.lambdas.stuck_runs``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from strandly.core.config import Config
from strandly.ops import metrics
from strandly.ops.lambdas import stuck_runs

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
    """Simulates DynamoDB Query pagination: one response dict per call, in order.

    ``pages`` is a list of response dicts (``{"Items": [...], "LastEvaluatedKey": {...}?}``).
    ``raise_on_call`` makes the Nth call (0-based) raise, to exercise mid-pagination fail-soft.
    Every call's kwargs are recorded in ``calls`` (``kwargs`` keeps the last, for old tests).
    """

    def __init__(self, items=None, pages=None, raise_on_call=None):
        if pages is None:
            pages = [{"Items": items or []}]
        self._pages = pages
        self._raise_on_call = raise_on_call
        self._cursor = 0
        self.calls = []
        self.kwargs = None

    def query(self, **kwargs):
        call_index = len(self.calls)
        self.calls.append(kwargs)
        self.kwargs = kwargs
        if self._raise_on_call is not None and call_index == self._raise_on_call:
            raise RuntimeError("boom")
        # No ExclusiveStartKey → a fresh pagination sequence starts from page 0 (as in DynamoDB).
        if "ExclusiveStartKey" not in kwargs:
            self._cursor = 0
        if self._cursor < len(self._pages):
            page = self._pages[self._cursor]
            self._cursor += 1
            return page
        return {"Items": []}  # ran past the scripted pages (a LastEvaluatedKey bug in the code)


def _page(items, last_key=None):
    page = {"Items": items}
    if last_key is not None:
        page["LastEvaluatedKey"] = last_key
    return page


def test_scan_running_queries_recent_gsi_newest_first():
    table = _FakeTable(items=[_row("a", "running", age_minutes=60)])
    errors: list[str] = []
    rows = stuck_runs.scan_running(table, errors=errors)
    assert rows and rows[0]["task_id"] == "a"
    assert errors == []
    assert table.kwargs["IndexName"] == "recent"
    # Newest first: the Query default (ascending, oldest-first) is exactly the bug — combined with
    # Limit-before-filter it pinned the scan to the oldest page of a TTL-less table forever.
    assert table.kwargs["ScanIndexForward"] is False
    # Single page with no LastEvaluatedKey → exactly one query, no phantom follow-ups.
    assert len(table.calls) == 1
    assert "ExclusiveStartKey" not in table.calls[0]


def test_scan_running_follows_last_evaluated_key():
    # A running row on page 3 must still be found (the original one-page code lost it). Pages
    # arrive newest-first; the filter means whole pages can legitimately come back empty.
    key1, key2 = {"gsi_pk": "RUN", "started_at": "k1", "task_id": "t1"}, {
        "gsi_pk": "RUN", "started_at": "k2", "task_id": "t2",
    }
    table = _FakeTable(
        pages=[
            _page([_row("new-running", "running", age_minutes=5)], last_key=key1),
            _page([], last_key=key2),  # all-terminal page: filter yields nothing, must continue
            _page([_row("old-running", "running", age_minutes=999)]),  # no key → last page
        ]
    )
    errors: list[str] = []
    rows = stuck_runs.scan_running(table, errors=errors)
    assert [r["task_id"] for r in rows] == ["new-running", "old-running"]
    assert errors == []
    # The cursor is threaded page to page, and pagination stops at the missing LastEvaluatedKey.
    assert len(table.calls) == 3
    assert "ExclusiveStartKey" not in table.calls[0]
    assert table.calls[1]["ExclusiveStartKey"] == key1
    assert table.calls[2]["ExclusiveStartKey"] == key2
    # The non-cursor kwargs must persist on every follow-up page, not just the first.
    for call in table.calls:
        assert call["IndexName"] == "recent"
        assert call["ScanIndexForward"] is False
        assert "FilterExpression" in call and "KeyConditionExpression" in call


def test_scan_running_page_bound_records_truncation():
    # Every page advertises more data: the bound must stop the walk AND surface the truncation
    # (a silently narrowed scan is the exact failure mode this fix removes).
    pages = [
        _page([_row(f"r{i}", "running", age_minutes=60)], last_key={"started_at": f"k{i}"})
        for i in range(5)
    ]
    table = _FakeTable(pages=pages)
    errors: list[str] = []
    rows = stuck_runs.scan_running(table, errors=errors, max_pages=3)
    assert [r["task_id"] for r in rows] == ["r0", "r1", "r2"]  # partial results are kept
    assert len(table.calls) == 3
    assert errors and "truncated after 3 pages" in errors[0]


def test_scan_running_exact_page_bound_fit_is_not_truncation():
    # Boundary: the data ends exactly at max_pages (final page has no LastEvaluatedKey) — that is
    # a complete walk, not a truncated one, so no error may be recorded.
    pages = [
        _page([_row("r0", "running", age_minutes=60)], last_key={"started_at": "k0"}),
        _page([_row("r1", "running", age_minutes=90)]),  # last page: no key
    ]
    table = _FakeTable(pages=pages)
    errors: list[str] = []
    rows = stuck_runs.scan_running(table, errors=errors, max_pages=2)
    assert [r["task_id"] for r in rows] == ["r0", "r1"]
    assert len(table.calls) == 2
    assert errors == []


def test_scan_running_failsoft_records_error():
    table = _FakeTable(raise_on_call=0)
    errors: list[str] = []
    assert stuck_runs.scan_running(table, errors=errors) == []
    assert errors and "ledger scan failed" in errors[0]


def test_scan_running_midpagination_error_keeps_partial_rows():
    # Page 1 succeeds, page 2 raises: the rows already collected still reach the detector
    # (partial visibility beats none), and the error is recorded so "degraded" is visible.
    table = _FakeTable(
        pages=[_page([_row("seen", "running", age_minutes=60)], last_key={"started_at": "k"})],
        raise_on_call=1,
    )
    errors: list[str] = []
    rows = stuck_runs.scan_running(table, errors=errors)
    assert [r["task_id"] for r in rows] == ["seen"]
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
