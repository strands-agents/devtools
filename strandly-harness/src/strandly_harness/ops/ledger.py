"""Durable run-ledger — a write-through copy of each deployed run to DynamoDB.

The deployed AgentCore runtime's task store (:class:`serve.agentcore_app._TaskStore`) is
**best-effort and per-instance**: a poll after the instance recycles returns ``"unknown"``, and
there is no queryable history of what the agent did. GitHub is the durable *outcome* record (the
agent reports via ``use_github``), but GitHub holds no runtime telemetry — how long a run took, how
many tokens it burned, which PR/issue it targeted, whether it failed.

This module adds an **optional, durable** ledger: one DynamoDB row per invocation. It is gated on
``STRANDLY_RUN_LEDGER_TABLE`` exactly like every other capability — set it (and grant the execution
role ``dynamodb:PutItem``) and runs persist; leave it unset and the runtime behaves exactly as
before (in-memory only). It is **fail-open**: a ledger write must never break or delay a run, so
every boto/Dynamo error is logged and swallowed — the run's real result still goes to GitHub.

It powers the Strandly dashboard (see ``dashboard/``): the dashboard's read API queries this table
for its Overview and Runs views. Rows carry a constant ``gsi_pk`` ("RUN") and an ISO-8601
``started_at`` sort key so the dashboard can list recent runs newest-first via a single Query on the
``recent`` GSI instead of scanning the table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from strandly_harness.core.config import Config
from strandly_harness.ops import metrics

logger = logging.getLogger(__name__)

# Constant partition key for the "recent runs" GSI: every row shares it so the dashboard can Query
# newest-first (ScanIndexForward=False) instead of scanning. The GSI sort key is ``started_at``.
GSI_PARTITION_VALUE = "RUN"

# Keep stored text bounded — the ledger is a telemetry index, not a transcript store (GitHub holds
# the full result). Mirrors the spirit of the in-memory store: a convenience copy.
_RESULT_SUMMARY_LIMIT = 4_000
_ERROR_LIMIT = 2_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunLedger:
    """Write-through ledger for deployed runs. Build via :meth:`from_config`; ``None`` if disabled.

    Every method is fail-open: it catches and logs any exception rather than letting a telemetry
    write disrupt the run. The boto3 Table is created lazily on first use (so constructing a
    ledger never touches AWS), and may be injected for tests.
    """

    def __init__(self, table_name: str, region: str | None = None, *, table: Any | None = None):
        self.table_name = table_name
        self.region = region
        self._table = table  # injectable / lazily built

    @classmethod
    def from_config(cls, config: Config) -> RunLedger | None:
        """Return a ledger if ``STRANDLY_RUN_LEDGER_TABLE`` is set, else ``None`` (disabled)."""
        if not config.run_ledger_enabled:
            return None
        return cls(table_name=config.run_ledger_table or "", region=config.aws_region)

    # ---- public API ----------------------------------------------------------------

    def start(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        github_target: str | None = None,
        repo: str | None = None,
        prompt: str | None = None,
        trace_id: str | None = None,
        started_at: str | None = None,
    ) -> None:
        """Record a run as ``running``. Safe to call before any work happens."""
        item: dict[str, Any] = {
            "task_id": task_id,
            "gsi_pk": GSI_PARTITION_VALUE,
            "status": "running",
            "started_at": started_at or _now_iso(),
        }
        _put_optional(item, session_id=session_id, github_target=github_target, repo=repo,
                      trace_id=trace_id)
        if prompt:
            item["prompt"] = prompt[:_RESULT_SUMMARY_LIMIT]
        self._put(item)

    def finish(
        self,
        task_id: str,
        *,
        result: str = "",
        usage: dict[str, int] | None = None,
        duration_ms: int | None = None,
        session_id: str | None = None,
        github_target: str | None = None,
        repo: str | None = None,
        prompt: str | None = None,
        trace_id: str | None = None,
        started_at: str | None = None,
    ) -> None:
        """Record a run as ``completed`` with its result summary, token usage, and duration."""
        item = self._terminal_item(
            task_id, "completed", duration_ms=duration_ms, session_id=session_id,
            github_target=github_target, repo=repo, prompt=prompt, trace_id=trace_id,
            started_at=started_at,
        )
        if result:
            item["result_summary"] = result[:_RESULT_SUMMARY_LIMIT]
        if usage:
            for dst, src in (("tokens_in", "input"), ("tokens_out", "output"),
                             ("tokens_total", "total")):
                if isinstance(usage.get(src), int):
                    item[dst] = usage[src]
        self._put(item)

    def fail(
        self,
        task_id: str,
        *,
        error: str = "",
        duration_ms: int | None = None,
        session_id: str | None = None,
        github_target: str | None = None,
        repo: str | None = None,
        prompt: str | None = None,
        trace_id: str | None = None,
        started_at: str | None = None,
    ) -> None:
        """Record a run as ``failed`` with its error and duration."""
        item = self._terminal_item(
            task_id, "failed", duration_ms=duration_ms, session_id=session_id,
            github_target=github_target, repo=repo, prompt=prompt, trace_id=trace_id,
            started_at=started_at,
        )
        if error:
            item["error"] = error[:_ERROR_LIMIT]
        self._put(item)

    # ---- internals -----------------------------------------------------------------

    def _terminal_item(
        self, task_id: str, status: str, *, duration_ms: int | None, session_id: str | None,
        github_target: str | None, repo: str | None, prompt: str | None,
        trace_id: str | None, started_at: str | None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "task_id": task_id,
            "gsi_pk": GSI_PARTITION_VALUE,
            "status": status,
            "ended_at": _now_iso(),
        }
        # Preserve the original start time on the row (and the GSI sort key) when the caller knows
        # it; otherwise the put still carries a started_at so the row remains queryable on the GSI.
        item["started_at"] = started_at or item["ended_at"]
        if isinstance(duration_ms, int):
            item["duration_ms"] = duration_ms
        _put_optional(item, session_id=session_id, github_target=github_target, repo=repo,
                      trace_id=trace_id)
        # The terminal put_item REPLACES the start() row wholesale, so the prompt written at
        # start() would silently vanish from every finished run (the dashboard's session
        # descriptions all showed "(no description)"). Re-carry it on the terminal write.
        if prompt:
            item["prompt"] = prompt[:_RESULT_SUMMARY_LIMIT]
        return item

    def table(self) -> Any:
        """The boto3 DynamoDB ``Table`` resource, built lazily (cached)."""
        if self._table is None:
            import boto3

            session = boto3.Session(region_name=self.region) if self.region else boto3.Session()
            self._table = session.resource("dynamodb").Table(self.table_name)
        return self._table

    def _put(self, item: dict[str, Any]) -> None:
        """Write one item; fail-open (log + swallow) so telemetry never disrupts a run."""
        try:
            self.table().put_item(Item=item)
        except Exception:  # noqa: BLE001 - fail-open by design
            logger.warning("run-ledger write failed (task_id=%s); continuing",
                           item.get("task_id"), exc_info=True)
            # A swallowed write means the dashboard silently goes blank — surface it as a metric so
            # an alarm can fire on it (the write itself stays fail-open; this emit is too).
            metrics.emit({metrics.LEDGER_WRITE_FAILED: 1}, surface=metrics.SURFACE_AGENTCORE)


def _put_optional(item: dict[str, Any], **fields: str | None) -> None:
    """Set only the keys whose value is truthy (DynamoDB rejects empty strings on some paths)."""
    for key, value in fields.items():
        if value:
            item[key] = value
