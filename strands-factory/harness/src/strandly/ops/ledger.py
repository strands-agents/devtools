"""Durable run ledger: one DynamoDB row per deployed run.

The runtime's task store is per-instance and GitHub holds outcomes, not telemetry; this table
records each run's status, timing, tokens and target for the dashboard. Enabled by
``STRANDLY_RUN_LEDGER_TABLE`` and fail-open: a write error is logged and never breaks or delays a
run. Rows carry a constant ``gsi_pk`` and an ISO ``started_at`` so the ``recent`` GSI lists runs
newest first without a scan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from strandly.core.config import Config
from strandly.ops import metrics

logger = logging.getLogger(__name__)

# Constant partition key for the "recent runs" GSI: every row shares it so the dashboard can Query
# newest-first (ScanIndexForward=False) instead of scanning. The GSI sort key is ``started_at``.
GSI_PARTITION_VALUE = "RUN"

# Run statuses. ``cancelled``: superseded by a newer message on its session, or stopped.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

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
        model: str | None = None,
        effort: str | None = None,
        trigger_source: str | None = None,
        mode: str | None = None,
        requested_by: str | None = None,
    ) -> None:
        """Record a run as ``running``. Safe to call before any work happens.

        ``trigger_source`` records HOW the run started (e.g. ``event:pull_request.opened`` /
        ``portal:curate`` / ``mention:@user`` / ``slack``) and ``mode`` records the portal role
        of the turn (``draft`` — agent working ahead / ``curate`` — human-driven in the portal /
        ``post`` — publishing the distilled result). Both are optional and written only when
        truthy, so existing callers keep producing identical rows. ``requested_by`` records who
        asked for a portal, CLI or ACP run: ``person:<id>`` (the bound person) or ``portal:<sub>``."""
        item: dict[str, Any] = {
            "task_id": task_id,
            "gsi_pk": GSI_PARTITION_VALUE,
            "status": STATUS_RUNNING,
            "started_at": started_at or _now_iso(),
        }
        _put_optional(item, session_id=session_id, github_target=github_target, repo=repo,
                      trace_id=trace_id, model=model, effort=effort, trigger_source=trigger_source, mode=mode,
                      requested_by=requested_by)
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
        tool_errors: int | None = None,
        stop_reason: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        trigger_source: str | None = None,
        mode: str | None = None,
        requested_by: str | None = None,
        status: str = STATUS_COMPLETED,
    ) -> None:
        """Record a finished run (``completed``, or ``cancelled``) with its result summary, token
        usage, and duration.

        ``tool_errors`` (how many tool calls inside the turn returned an error result) and
        ``stop_reason`` (the model's terminal stop reason, e.g. ``end_turn`` / ``max_tokens``)
        qualify what "completed" means: a turn that *finished* isn't necessarily a task that
        *succeeded* — e.g. every ``use_github`` call failing on an expired token still ends in a
        polite final message. ``tool_errors`` is written even when 0 so a healthy run is
        distinguishable from a pre-upgrade row that never carried the field. ``model`` and
        ``effort`` are the top agent's model string and effort in effect for this run (the
        deployment's, or the one a portal user chose for the turn), so per-model token usage can
        be attributed across model changes."""
        item = self._terminal_item(
            task_id, status, duration_ms=duration_ms, session_id=session_id,
            github_target=github_target, repo=repo, prompt=prompt, trace_id=trace_id,
            started_at=started_at, model=model, effort=effort, trigger_source=trigger_source, mode=mode,
            requested_by=requested_by,
        )
        if result:
            item["result_summary"] = result[:_RESULT_SUMMARY_LIMIT]
        if usage:
            for dst, src in (("tokens_in", "input"), ("tokens_out", "output"),
                             ("tokens_total", "total"), ("tokens_cache_read", "cacheRead"),
                             ("tokens_cache_write", "cacheWrite")):
                if isinstance(usage.get(src), int):
                    item[dst] = usage[src]
        if isinstance(tool_errors, int) and not isinstance(tool_errors, bool):
            item["tool_errors"] = tool_errors
        if stop_reason:
            item["stop_reason"] = str(stop_reason)
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
        tool_errors: int | None = None,
        model: str | None = None,
        effort: str | None = None,
        trigger_source: str | None = None,
        mode: str | None = None,
        requested_by: str | None = None,
        only_if_running: bool = False,
    ) -> None:
        """Record a run as ``failed`` with its error, duration, and tool-error count.

        ``only_if_running`` makes the write conditional on the row still being ``running`` (the
        SIGTERM handler): a run that finished meanwhile keeps its own terminal row.
        """
        item = self._terminal_item(
            task_id, STATUS_FAILED, duration_ms=duration_ms, session_id=session_id,
            github_target=github_target, repo=repo, prompt=prompt, trace_id=trace_id,
            started_at=started_at, model=model, effort=effort, trigger_source=trigger_source, mode=mode,
            requested_by=requested_by,
        )
        if error:
            item["error"] = error[:_ERROR_LIMIT]
        if isinstance(tool_errors, int) and not isinstance(tool_errors, bool):
            item["tool_errors"] = tool_errors
        self._put(item, only_if_running=only_if_running)

    # ---- internals -----------------------------------------------------------------

    def _terminal_item(
        self, task_id: str, status: str, *, duration_ms: int | None, session_id: str | None,
        github_target: str | None, repo: str | None, prompt: str | None,
        trace_id: str | None, started_at: str | None, model: str | None = None,
        effort: str | None = None,
        trigger_source: str | None = None, mode: str | None = None, requested_by: str | None = None,
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
                      trace_id=trace_id, model=model, effort=effort, trigger_source=trigger_source, mode=mode,
                      requested_by=requested_by)
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

    def _put(self, item: dict[str, Any], *, only_if_running: bool = False) -> None:
        """Write one item; fail-open (log + swallow) so telemetry never disrupts a run."""
        kwargs: dict[str, Any] = {}
        if only_if_running:
            kwargs = {
                "ConditionExpression": "#status = :running",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {":running": STATUS_RUNNING},
            }
        try:
            self.table().put_item(Item=item, **kwargs)
        except Exception as e:  # noqa: BLE001 - fail-open by design
            if only_if_running and _is_conditional_check_failure(e):
                logger.info("run-ledger row %s is no longer running; left as is", item.get("task_id"))
                return
            logger.warning("run-ledger write failed (task_id=%s); continuing",
                           item.get("task_id"), exc_info=True)
            # A swallowed write means the dashboard silently goes blank — surface it as a metric so
            # an alarm can fire on it (the write itself stays fail-open; this emit is too).
            metrics.emit({metrics.LEDGER_WRITE_FAILED: 1}, surface=metrics.SURFACE_AGENTCORE)


def _is_conditional_check_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code == "ConditionalCheckFailedException"


def _put_optional(item: dict[str, Any], **fields: str | None) -> None:
    """Set only the keys whose value is truthy (DynamoDB rejects empty strings on some paths)."""
    for key, value in fields.items():
        if value:
            item[key] = value
