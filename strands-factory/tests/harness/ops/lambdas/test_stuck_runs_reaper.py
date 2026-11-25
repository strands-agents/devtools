"""The stuck-run detector's orphan reaper (rows ``running`` longer than a runtime session can live)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from strandly.core import constants
from strandly.core.config import Config
from strandly.ops import metrics
from strandly.ops.lambdas import stuck_runs

_NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


def _row(task_id: str, status: str = "running", *, age_hours: float) -> dict:
    return {
        "task_id": task_id,
        "status": status,
        "started_at": (_NOW - timedelta(hours=age_hours)).isoformat(),
    }


class _ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _Table:
    def __init__(self, items, *, finished_meanwhile=(), broken=()):
        self.items = items
        self.updates: list[dict] = []
        self._finished = set(finished_meanwhile)
        self._broken = set(broken)

    def query(self, **_kwargs):
        return {"Items": self.items}

    def update_item(self, **kwargs):
        task_id = kwargs["Key"]["task_id"]
        if task_id in self._finished:
            raise _ConditionalCheckFailed("The conditional request failed")
        if task_id in self._broken:
            raise RuntimeError("throttled")
        self.updates.append(kwargs)


def _config() -> Config:
    return Config(values={"STRANDLY_RUN_LEDGER_TABLE": "strandly-dev-runledger"})


def test_find_orphans_only_running_rows_past_the_session_lifetime():
    rows = [
        _row("orphan", age_hours=30 * 24),
        _row("long-but-alive", age_hours=3),
        _row("seven-hours-and-alive", age_hours=7),
        _row("old-done", "completed", age_hours=30 * 24),
        {"task_id": "no-ts", "status": "running"},
    ]
    found = stuck_runs.find_orphans(rows, now=_NOW, reap_after_hours=stuck_runs.ORPHAN_REAP_HOURS)
    assert [r["task_id"] for r in found] == ["orphan"]


def test_reaper_threshold_exceeds_the_runtime_max_lifetime():
    # The agent runtime's maxLifetime is 8 h on the microVM: anything younger may still be a live
    # run and must not be closed.
    assert stuck_runs.ORPHAN_REAP_HOURS == 9
    assert stuck_runs.ORPHAN_REAP_HOURS * 3600 > constants.AGENT_MAX_LIFETIME_SECONDS


def test_check_reaps_orphans_conditionally_and_alarms_only_on_live_stuck_runs(capsys, monkeypatch):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    table = _Table(
        [
            _row("orphan-aug", age_hours=90 * 24),
            _row("orphan-oct", age_hours=20 * 24),
            _row("stuck-now", age_hours=2),
            _row("fresh", age_hours=0.1),
        ]
    )
    report = stuck_runs.check(_config(), now=_NOW, table=table)

    assert report.reaped == ["orphan-aug", "orphan-oct"]
    assert report.stuck == ["stuck-now"]  # the alarm now sees only the run that could be alive
    update = table.updates[0]
    assert update["Key"] == {"task_id": "orphan-aug"}
    assert update["ConditionExpression"] == "#s = :running"
    values = update["ExpressionAttributeValues"]
    assert values[":failed"] == "failed" and values[":running"] == "running"
    assert "orphaned" in values[":reason"] and values[":true"] is True

    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.STUCK_RUNS] == 1
    assert doc[metrics.REAPED_RUNS] == 2


def test_reaper_never_clobbers_a_run_that_finished_meanwhile():
    table = _Table([_row("raced", age_hours=10)], finished_meanwhile={"raced"})
    report = stuck_runs.check(_config(), now=_NOW, table=table)
    assert report.reaped == [] and report.errors == []
    assert table.updates == []
    # Not reaped -> still counted as stuck this scan (the next scan will not see it as running).
    assert report.stuck == ["raced"]


def test_reaper_is_fail_soft_per_row():
    table = _Table([_row("a", age_hours=16 * 24), _row("b", age_hours=16 * 24)], broken={"a"})
    report = stuck_runs.check(_config(), now=_NOW, table=table)
    assert report.reaped == ["b"]
    assert report.errors and "reap a failed" in report.errors[0]
    # Not closed, so still listed — as stale (16 days is far past the alarm window), not gauged.
    assert report.stale == ["a"] and report.stuck == []


def test_report_carries_reaped_ids():
    assert stuck_runs.StuckReport(reaped=["x"]).as_dict()["reaped"] == ["x"]


def _config_minutes(minutes: int) -> Config:
    return Config(
        values={
            "STRANDLY_RUN_LEDGER_TABLE": "strandly-dev-runledger",
            "STRANDLY_STUCK_RUN_MINUTES": str(minutes),
        }
    )


def test_an_orphan_pages_between_the_session_maximum_and_the_reaper(capsys, monkeypatch):
    """At the deployed threshold (8 h, the session maximum) a row pages only once no session can
    still be running it, and the reaper closes it an hour later."""
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    table = _Table(
        [
            _row("long-run", age_hours=7.5),  # may be alive: nothing
            _row("orphan-new", age_hours=8.5),  # no session can be this old: pages
            _row("orphan", age_hours=9.5),  # reaped
        ]
    )
    report = stuck_runs.check(_config_minutes(480), now=_NOW, table=table)
    assert report.stuck == ["orphan-new"]
    assert report.stale == []
    assert report.reaped == ["orphan"]
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.STUCK_RUNS] == 1


def test_a_row_the_reaper_cannot_close_pages_for_one_window_then_is_listed_not_gauged(
    capsys, monkeypatch
):
    monkeypatch.setenv(metrics.NAMESPACE_ENV, "Strandly-dev")
    window = stuck_runs.STUCK_GAUGE_WINDOW_HOURS
    table = _Table(
        [
            _row("in-window", age_hours=8 + window - 1),
            _row("past-window", age_hours=8 + window + 1),
        ],
        broken={"in-window", "past-window"},
    )
    report = stuck_runs.check(_config_minutes(480), now=_NOW, table=table)
    assert report.reaped == [] and len(report.errors) == 2
    assert report.stuck == ["in-window"]
    assert report.stale == ["past-window"]
    assert report.as_dict()["stale"] == ["past-window"]
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc[metrics.STUCK_RUNS] == 1


def test_find_stuck_max_age_bounds_the_window():
    rows = [_row("a", age_hours=1), _row("b", age_hours=5), _row("c", age_hours=50)]
    assert stuck_runs.find_stuck(rows, now=_NOW, threshold_minutes=30) == ["a", "b", "c"]
    assert stuck_runs.find_stuck(rows, now=_NOW, threshold_minutes=30, max_age_minutes=6 * 60) == [
        "a",
        "b",
    ]


def test_the_handler_logs_stale_runs(caplog, monkeypatch):
    table = _Table([_row("stale", age_hours=24 * 5)], broken={"stale"})
    real_check = stuck_runs.check
    monkeypatch.setattr(
        stuck_runs, "check", lambda config: real_check(config, now=_NOW, table=table)
    )
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: _config_minutes(480)))
    with caplog.at_level("WARNING"):
        out = stuck_runs.lambda_handler({}, None)
    assert out["report"]["stale"] == ["stale"] and out["report"]["stuck"] == []
    assert "past the alarm window" in caplog.text
