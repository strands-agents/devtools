"""Stuck-run detector — flag run-ledger rows left ``running`` past a threshold.

A deployed run is fire-and-forget: ``agentcore._run`` writes a ``running`` ledger row, does the
work, then writes ``completed``/``failed``. If the AgentCore instance is recycled mid-run (or the
process dies), that terminal write never happens and the row sits ``running`` forever — the
dashboard shows a perpetual "active" run and nothing ever alarms, because the *symptom is the
absence of an event*. No choke-point EMF emit can catch that; only an out-of-band scan can.

So this is a scheduled Lambda (an EventBridge tick, reusing the poller package) that queries the
ledger's ``recent`` GSI, finds rows still ``running`` whose ``started_at`` is older than
``STRANDLY_STUCK_RUN_MINUTES`` (default 30), emits a ``StuckRuns`` gauge (so an alarm can fire on
it), and publishes the offending task ids to SNS when a topic is configured.

It is **read-only** on the ledger — it never rewrites a row's status (a "stuck" run might merely be
a genuinely long one; deciding to mark it failed is a policy call we don't make here). Design
matches ``ingress/``: boto3 is lazy, the DynamoDB query is the one seam tests monkeypatch, and every
path is fail-soft — a scan that errors returns a report carrying the error, never raises.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from strandly_harness.core.constants import RUN_LEDGER_GSI_NAME
from strandly_harness.ops import metrics
from strandly_harness.ops.ledger import GSI_PARTITION_VALUE

if TYPE_CHECKING:
    from strandly_harness.core.config import Config

logger = logging.getLogger(__name__)


@dataclass
class StuckReport:
    """The outcome of one stuck-run scan.

    ``stuck`` is the task ids still ``running`` past the threshold; ``scanned`` is how many rows the
    query returned; ``errors`` records a failed scan (fail-soft — an error contributes no rows
    rather than raising), so a caller can tell "clean" from "couldn't check".
    """

    stuck: list[str] = field(default_factory=list)
    scanned: int = 0
    threshold_minutes: int = 30
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.stuck

    def as_dict(self) -> dict[str, Any]:
        return {
            "stuck": self.stuck,
            "scanned": self.scanned,
            "threshold_minutes": self.threshold_minutes,
            "errors": self.errors,
            "ok": self.ok,
        }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_stuck(rows: list[dict[str, Any]], *, now: datetime, threshold_minutes: int) -> list[str]:
    """Task ids of rows still ``running`` whose ``started_at`` is older than the threshold (pure).

    A row with a missing/unparseable ``started_at`` is treated as **not** stuck: we can't prove it's
    old, and over-flagging a fresh run as stuck would be a false alarm (the opposite bias from the
    audit, where over-reporting is the safe direction — here a spurious page is the harm).
    """
    cutoff = now - timedelta(minutes=threshold_minutes)
    stuck: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "running":
            continue
        started = _parse_ts(row.get("started_at"))
        if started is not None and started < cutoff:
            task_id = row.get("task_id")
            if isinstance(task_id, str) and task_id:
                stuck.append(task_id)
    return sorted(stuck)


def scan_running(table: Any, *, errors: list[str], page_limit: int = 1000) -> list[dict[str, Any]]:
    """Query the ``recent`` GSI for ``running`` rows. Fail-soft via ``errors``; one page.

    Uses the constant-PK ``recent`` GSI (every row shares ``gsi_pk="RUN"``) so this is a Query, not
    a table Scan, with a server-side filter on ``status="running"``. One page (``Limit``) is plenty
    — there should only ever be a handful of concurrent runs; a backlog of thousands of ``running``
    rows is itself the alarm.
    """
    from boto3.dynamodb.conditions import Attr, Key

    try:
        resp = table.query(
            IndexName=RUN_LEDGER_GSI_NAME,
            KeyConditionExpression=Key("gsi_pk").eq(GSI_PARTITION_VALUE),
            FilterExpression=Attr("status").eq("running"),
            Limit=page_limit,
        )
    except Exception as e:  # noqa: BLE001 — fail-soft: a bad query records an error, never raises
        errors.append(f"ledger scan failed: {type(e).__name__}: {e}")
        return []
    items = resp.get("Items") if isinstance(resp, dict) else None
    return items if isinstance(items, list) else []


def check(config: Config, *, now: datetime | None = None, table: Any | None = None) -> StuckReport:
    """Run one scan: find ledger rows stuck ``running``. Fail-soft; emits a ``StuckRuns`` gauge.

    ``table`` is injectable for tests; otherwise the run-ledger's boto3 Table is built lazily. With
    no ledger table configured the scan is a no-op (nothing to read).
    """
    now = now or datetime.now(timezone.utc)
    threshold = config.stuck_run_minutes
    report = StuckReport(threshold_minutes=threshold)

    if not config.run_ledger_enabled:
        report.errors.append("run-ledger not configured (STRANDLY_RUN_LEDGER_TABLE unset)")
        return report

    if table is None:
        from strandly_harness.ops.ledger import RunLedger

        ledger = RunLedger.from_config(config)
        if ledger is None:  # pragma: no cover — guarded by run_ledger_enabled above
            report.errors.append("run-ledger not configured")
            return report
        table = ledger.table()

    rows = scan_running(table, errors=report.errors)
    report.scanned = len(rows)
    report.stuck = find_stuck(rows, now=now, threshold_minutes=threshold)

    # Emit the gauge even when zero, so the alarm has a continuous series (and missing data is a
    # signal the detector itself stopped, not "nothing stuck").
    metrics.emit({metrics.STUCK_RUNS: len(report.stuck)}, surface=metrics.SURFACE_MONITORING)
    return report


def notify(report: StuckReport, config: Config) -> bool:
    """Publish a stuck-run finding to SNS when a topic is configured. Best-effort; returns sent?."""
    if not report.stuck or not config.monitoring_sns_topic_arn:
        return False
    subject = f"\u26a0 strandly: {len(report.stuck)} stuck run(s) (> {report.threshold_minutes}m)"
    try:
        import boto3

        region = config.aws_region
        client = boto3.client("sns", region_name=region) if region else boto3.client("sns")
        client.publish(
            TopicArn=config.monitoring_sns_topic_arn,
            Subject=subject[:100],
            Message=json.dumps(report.as_dict(), indent=2),
        )
        return True
    except Exception as e:  # noqa: BLE001 — notification is best-effort
        logger.warning("stuck-run SNS publish failed: %s", e)
        return False


def lambda_handler(event: Any = None, context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """AWS Lambda entrypoint: scan the ledger for stuck runs and notify on any finding."""
    from strandly_harness.core.config import Config

    config = Config.load()
    report = check(config)
    if report.errors:
        logger.warning("stuck-run scan: degraded — %s", report.errors)
    if report.stuck:
        logger.warning(
            "stuck-run scan: %d run(s) running > %dm: %s",
            len(report.stuck),
            report.threshold_minutes,
            report.stuck,
        )
        notify(report, config)
    else:
        logger.info("stuck-run scan: clean (%d running rows scanned)", report.scanned)
    return {"status": "ok", "report": report.as_dict()}
