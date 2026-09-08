"""Mention log — one DynamoDB row per processed ``@mention``, powering the dashboard's Mentions tab.

The poller already *decides* what happens to every mention it sees (dispatched, unauthorized,
stale, ...), but those decisions only lived in CloudWatch logs — there was no queryable record of
"who mentioned the agent, and was it allowed to answer?". This module writes that record: one row
per processed mention, so the dashboard can list them newest-first exactly like the run-ledger
powers the Runs tab.

**Fail-open is the rule** (same contract as :mod:`.dedup` and the run-ledger): a mention-log write
must never break or delay a dispatch, so every error is logged and swallowed, and with no table
configured ``record`` is a no-op. This is telemetry, not control flow — authorization itself is
enforced in ``handler.process_notification`` regardless of whether the log write lands.

Schema: string PK ``mention_id`` (``{thread_id}#{mention_ts}#{outcome}`` — a retried mention that
later dispatches produces distinct rows, while an identical re-processing idempotently overwrites),
a constant ``gsi_pk`` ("MENTION") + ISO ``seen_at`` sort key for the ``recent`` GSI (mirrored in
``infra/stacks/common.py`` / ``dashboard/api/handler.py``; canonical values in
``strandly_harness.core.constants`` — the sync test guards them), and a numeric ``ttl`` so DynamoDB
reaps old rows. boto3 is never imported here; the client is passed in (injectable for tests).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from strandly_harness.core.constants import MENTION_LOG_GSI_PK_VALUE

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
