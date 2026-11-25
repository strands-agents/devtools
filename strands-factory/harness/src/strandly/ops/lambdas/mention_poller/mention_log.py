"""Mention log: one DynamoDB row per processed ``@mention``, for the dashboard's Mentions tab.

Records who mentioned the agent and what the poller decided (dispatched, unauthorized, stale and so
on), newest first, the way the run ledger backs the Runs tab.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from strandly.core.constants import MENTION_LOG_GSI_PK_VALUE

logger = logging.getLogger(__name__)

# Rows older than this have scrolled far off the dashboard; let DynamoDB TTL reap them.
_ROW_TTL_DAYS = 90
# Clip the stored mention text — the log is an index, not a transcript (GitHub holds the thread).
_BODY_LIMIT = 1_000


def record(
    client: Any,
    table: str | None,
    *,
    thread_id: str,
    outcome: str,
    authorized: bool,
    author: str = "",
    repo: str = "",
    number: int | str | None = None,
    is_pull_request: bool = False,
    mention_ts: str | None = None,
    body: str | None = None,
    url: str | None = None,
    session_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Write one mention-log row; fail-open (no table → no-op, any error → log + swallow)."""
    if not table or not client or not thread_id:
        return
    now = now or datetime.now(timezone.utc)
    seen_at = now.isoformat()
    ttl = int((now + timedelta(days=_ROW_TTL_DAYS)).timestamp())
    item: dict[str, Any] = {
        "mention_id": {"S": f"{thread_id}#{mention_ts or 'unknown'}#{outcome}"},
        "gsi_pk": {"S": MENTION_LOG_GSI_PK_VALUE},
        "seen_at": {"S": seen_at},
        "outcome": {"S": outcome},
        "authorized": {"BOOL": bool(authorized)},
        "is_pull_request": {"BOOL": bool(is_pull_request)},
        "ttl": {"N": str(ttl)},
    }
    # DynamoDB rejects empty strings on some paths — set only truthy optionals (ledger pattern).
    for key, value in (
        ("author", author),
        ("repo", repo),
        ("mention_ts", mention_ts),
        ("url", url),
        ("session_id", session_id),
    ):
        if value:
            item[key] = {"S": str(value)}
    if number is not None:
        item["number"] = {"N": str(number)} if str(number).isdigit() else {"S": str(number)}
    if body:
        item["body"] = {"S": body[:_BODY_LIMIT]}
    try:
        client.put_item(TableName=table, Item=item)
    except Exception as e:  # noqa: BLE001 — telemetry is fail-open by design
        logger.warning("mention-log write failed for thread %s (%s): %s; continuing",
                       thread_id, outcome, e)
