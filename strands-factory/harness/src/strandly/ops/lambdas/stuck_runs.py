"""Stuck-run detector: flag run-ledger rows left ``running`` past a threshold.

A run whose process dies never writes its terminal row, and the missing event cannot be caught at a
choke point. This scheduled Lambda queries the ledger's ``recent`` GSI, emits a ``StuckRuns`` gauge
for rows running longer than ``STRANDLY_STUCK_RUN_MINUTES`` and publishes their task ids to SNS.
Rows older than :data:`ORPHAN_REAP_HOURS` (longer than any runtime session can live) are closed with
a conditional update to ``failed`` and counted as ``ReapedRuns``; nothing is deleted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from strandly.core.constants import AGENT_MAX_LIFETIME_SECONDS, RUN_LEDGER_GSI_NAME
from strandly.ops import metrics
from strandly.ops.lambdas import configure_logging
from strandly.ops.ledger import GSI_PARTITION_VALUE

if TYPE_CHECKING:
    from strandly.core.config import Config

logger = logging.getLogger(__name__)


@dataclass
class StuckReport:
    """The outcome of one stuck-run scan.

    ``stuck`` is the task ids still ``running`` past the threshold; ``scanned`` is how many rows the
    query returned; ``errors`` records a failed scan (fail-soft — an error contributes no rows
    rather than raising), so a caller can tell "clean" from "couldn't check".
    """

    stuck: list[str] = field(default_factory=list)
    #: Running past the threshold + the gauge window but not yet reapable: listed, never gauged.
    stale: list[str] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    scanned: int = 0
    threshold_minutes: int = 30
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.stuck

    def as_dict(self) -> dict[str, Any]:
        return {
            "stuck": self.stuck,
            "stale": self.stale,
            "reaped": self.reaped,
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


def find_stuck(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    threshold_minutes: int,
    max_age_minutes: int | None = None,
) -> list[str]:
    """Task ids of rows still ``running`` whose ``started_at`` is older than the threshold (pure).

    With ``max_age_minutes``, rows older than that are left out (they are ``stale``: see the module
    docstring). A row with a missing/unparseable ``started_at`` is treated as **not** stuck: we can't
    prove it's old, and over-flagging a fresh run as stuck would be a false alarm (the opposite bias
    from the audit, where over-reporting is the safe direction — here a spurious page is the harm).
    """
    cutoff = now - timedelta(minutes=threshold_minutes)
    floor = now - timedelta(minutes=max_age_minutes) if max_age_minutes is not None else None
    stuck: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "running":
            continue
        started = _parse_ts(row.get("started_at"))
        if started is not None and started < cutoff and (floor is None or started >= floor):
            task_id = row.get("task_id")
            if isinstance(task_id, str) and task_id:
                stuck.append(task_id)
    return sorted(stuck)


#: How long past the stuck threshold a run keeps counting toward the ``StuckRuns`` gauge. Long enough
#: for the alarm (and its SNS notice) to fire on a crashed run; short enough that a row the reaper
#: cannot close does not hold the alarm indefinitely.
STUCK_GAUGE_WINDOW_HOURS = 24


# Pagination safety valve for scan_running. Each Query page evaluates at most ``page_limit`` items
# (and at most 1 MB of data), so this bounds one scan at ~max_pages MB read from the GSI — a cost
# ceiling, not an expected path. If the ledger ever grows past it (~50k rows with the default
# page_limit), the scan records a truncation error so the degradation is visible. Because we read
# newest-first, a truncated scan drops the *oldest* rows: newly-stuck runs are always still
# detected, but a very old stuck row could eventually age out of the scanned window — hence the
# error, so that trade-off is never silent. (A TTL on the ledger would remove the growth entirely.)
#: A run still ``running`` this long is an orphan, not a slow run: the agent runtime's
#: ``maxLifetime`` is 8 h (``AGENT_MAX_LIFETIME_SECONDS``, mirrored from infra), so the process
#: that would have written its terminal row is gone. One hour of margin on top: 9 h.
ORPHAN_REAP_HOURS = AGENT_MAX_LIFETIME_SECONDS // 3600 + 1
ORPHAN_ERROR = (
    "orphaned: still 'running' after {hours}h, longer than an AgentCore runtime session can live "
    "(the instance was recycled before the run's terminal write); closed by the stuck-run reaper"
)


def find_orphans(
    rows: list[dict[str, Any]], *, now: datetime, reap_after_hours: int
) -> list[dict[str, Any]]:
    """Rows still ``running`` whose ``started_at`` is older than ``reap_after_hours`` (pure)."""
    cutoff = now - timedelta(hours=reap_after_hours)
    orphans = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "running":
            continue
        started = _parse_ts(row.get("started_at"))
        task_id = row.get("task_id")
        if started is not None and started < cutoff and isinstance(task_id, str) and task_id:
            orphans.append(row)
    return sorted(orphans, key=lambda r: r["task_id"])


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException"


def reap(
    table: Any, orphans: list[dict[str, Any]], *, now: datetime, errors: list[str]
) -> list[str]:
    """Close each orphan with a conditional ``UpdateItem``. Returns the task ids actually closed.

    The condition (``status = running``) makes this race-free against a late terminal write: if the
    run finished in between, DynamoDB rejects the update and the row stays as the run wrote it.
    Fail-soft per row (an error is recorded, the rest continue).
    """
    closed: list[str] = []
    for row in orphans:
        task_id = row["task_id"]
        try:
            table.update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET #s = :failed, ended_at = :now, #e = :reason, reaped = :true",
                ConditionExpression="#s = :running",
                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                ExpressionAttributeValues={
                    ":failed": "failed",
                    ":running": "running",
                    ":now": now.isoformat(),
                    ":reason": ORPHAN_ERROR.format(hours=ORPHAN_REAP_HOURS),
                    ":true": True,
                },
            )
        except Exception as e:  # noqa: BLE001 — fail-soft per row, like the scan
            if _is_conditional_failure(e):
                logger.info("reaper: %s finished before it was reaped; left as written", task_id)
            else:
                errors.append(f"reap {task_id} failed: {type(e).__name__}: {e}")
            continue
        logger.warning(
            "reaper: closed orphaned run %s (started %s)", task_id, row.get("started_at")
        )
        closed.append(task_id)
    return closed


_MAX_PAGES = 50


def scan_running(
    table: Any, *, errors: list[str], page_limit: int = 1000, max_pages: int = _MAX_PAGES
) -> list[dict[str, Any]]:
    """Query the ``recent`` GSI for ``running`` rows. Fail-soft via ``errors``; paginated.

    Uses the constant-PK ``recent`` GSI (every row shares ``gsi_pk="RUN"``, sort key
    ``started_at``) so this is a Query, not a table Scan, with a server-side filter on
    ``status="running"``.

    Why this paginates (the ``Limit``-before-filter trap): DynamoDB applies ``Limit`` to items
    *evaluated*, **before** ``FilterExpression`` — it caps rows read, not rows matched. The
    original one-page version ("one page is plenty") also relied on the Query default
    ``ScanIndexForward=True`` (ascending ``started_at``, oldest first), and the ledger has no TTL,
    so once the table exceeded one page of *lifetime* rows the scan only ever evaluated the oldest
    page and went permanently blind to every new run. So: newest-first, and follow
    ``LastEvaluatedKey`` to the end (bounded by ``max_pages`` as a cost ceiling).

    Why there is no time-window shortcut: a stuck row is one still ``running`` with ``started_at``
    *older* than the threshold — and it is never rewritten, so it must keep being reported however
    old it gets (the alarm stays up until a human intervenes). A
    ``KeyConditionExpression started_at >= now - threshold`` would return only rows too *young* to
    be stuck, i.e. exactly the wrong half of the index. The full (bounded) walk is the correct
    contract; the filter keeps the transfer tiny even though every row is evaluated.

    On a mid-pagination error the rows already collected are returned alongside the recorded error
    (partial results still let the alarm fire on whatever was found).
    """
    from boto3.dynamodb.conditions import Attr, Key

    items: list[dict[str, Any]] = []
    last_key: dict[str, Any] | None = None
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {
            "IndexName": RUN_LEDGER_GSI_NAME,
            "KeyConditionExpression": Key("gsi_pk").eq(GSI_PARTITION_VALUE),
            "FilterExpression": Attr("status").eq("running"),
            "Limit": page_limit,
            "ScanIndexForward": False,  # newest first — live runs are at the new end of the index
        }
        if last_key is not None:
            kwargs["ExclusiveStartKey"] = last_key
        try:
            resp = table.query(**kwargs)
        except Exception as e:  # noqa: BLE001 — fail-soft: a bad query records an error, never raises
            errors.append(f"ledger scan failed: {type(e).__name__}: {e}")
            return items
        page_items = resp.get("Items") if isinstance(resp, dict) else None
        if isinstance(page_items, list):
            items.extend(row for row in page_items if isinstance(row, dict))
        last_key = resp.get("LastEvaluatedKey") if isinstance(resp, dict) else None
        if not last_key:
            return items
    # Page bound hit with more data remaining: the oldest rows went unexamined. Surface it —
    # a silently narrowing scan is exactly the failure mode this pagination exists to prevent.
    errors.append(
        f"ledger scan truncated after {max_pages} pages; oldest rows not examined "
        "(consider a TTL on the ledger or raising the page bound)"
    )
    return items


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
        from strandly.ops.ledger import RunLedger

        ledger = RunLedger.from_config(config)
        if ledger is None:  # pragma: no cover — guarded by run_ledger_enabled above
            report.errors.append("run-ledger not configured")
            return report
        table = ledger.table()

    rows = scan_running(table, errors=report.errors)
    report.scanned = len(rows)
    # Close orphans first (older than any runtime session can live), so the gauge below counts only
    # runs that could still be alive — the alarm then means a NEW stuck run, not history.
    orphans = find_orphans(rows, now=now, reap_after_hours=ORPHAN_REAP_HOURS)
    report.reaped = reap(table, orphans, now=now, errors=report.errors)
    closed = set(report.reaped)
    live = [row for row in rows if row.get("task_id") not in closed]
    # Gauge only the runs that crossed the threshold recently; older ones are listed as stale.
    gauge_limit = threshold + STUCK_GAUGE_WINDOW_HOURS * 60
    report.stuck = find_stuck(
        live, now=now, threshold_minutes=threshold, max_age_minutes=gauge_limit
    )
    report.stale = find_stuck(live, now=now, threshold_minutes=gauge_limit)

    # Emit the gauge even when zero, so the alarm has a continuous series (and missing data is a
    # signal the detector itself stopped, not "nothing stuck").
    metrics.emit(
        {metrics.STUCK_RUNS: len(report.stuck), metrics.REAPED_RUNS: len(report.reaped)},
        surface=metrics.SURFACE_MONITORING,
    )
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
    from strandly.core.config import Config

    configure_logging()
    config = Config.load()
    report = check(config)
    if report.errors:
        logger.warning("stuck-run scan: degraded — %s", report.errors)
    if report.reaped:
        logger.warning("stuck-run scan: reaped %d orphan(s): %s", len(report.reaped), report.reaped)
    if report.stale:
        logger.warning(
            "stuck-run scan: %d run(s) running > %dm, past the alarm window (not gauged; reaped "
            "after %dh): %s",
            len(report.stale),
            report.threshold_minutes + STUCK_GAUGE_WINDOW_HOURS * 60,
            ORPHAN_REAP_HOURS,
            report.stale,
        )
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
