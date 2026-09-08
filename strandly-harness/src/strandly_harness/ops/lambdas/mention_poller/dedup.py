"""Durable dispatch backstop for the mention poller — a tiny DynamoDB table, fail-open.

The poller's primary dedup signal is the notification thread's ``last_read_at`` (handled in
``mentions.py``). That signal is good but lives entirely in GitHub state: if a poll run dispatched a
mention but then crashed *before* marking the notification read, the next poll would re-dispatch the
same mention. This module is the durable backstop: one row per notification thread recording the
timestamp of the last mention we dispatched, so a restart can't re-fire an already-handled mention.

**Fail-open is the rule.** A read failure (table missing, throttled, no perms) returns "not yet
dispatched" so the poller still dispatches — this gate can only *suppress* duplicates, never *drop*
a genuinely new or edited mention. A write failure is swallowed with a warning for the same reason:
the ``last_read_at`` gate is still in force, so losing the backstop write degrades to the
original behavior rather than failing the run.

**Ordering & residual window.** The poller records the dispatch *intent* via ``record_dispatch``
**before** it invokes the runtime, so a crash *after* a successful invoke but *before* mark-read
can't re-fire (this row suppresses the retry). If the invoke is then *rejected* (an HTTP-200 error
body) **or raises** (unresolved ARN, boto throttle/timeout/5xx), the poller calls ``clear_dispatch``
to roll the row back so the mention still retries (fail-closed, see
``mentions.process_notification`` — both the rejection branch and the surrounding ``except`` clear
the intent). One narrow residual window remains and is documented honestly: a crash *between*
writing the intent row and observing the invoke outcome (i.e. the rollback never runs) leaves the
row written without a confirmed dispatch — the next poll will treat it as already-dispatched and
suppress it (at-most-once in that sub-second window, trading a vanishingly rare miss for never
double-firing into a live agent session). With **no** ``dedup_table`` configured every function
here is a no-op and dedup falls back to ``last_read_at`` alone.

The table schema is minimal: a string partition key ``thread_id`` and a string ``last_dispatched_ts``
(an ISO-8601 instant). An optional numeric ``ttl`` attribute lets DynamoDB expire stale rows. boto3
is imported lazily and the client is injectable, so the unit tests stay AWS-free.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Rows older than this are meaningless (the notification is long gone); let DynamoDB TTL reap them.
_ROW_TTL_DAYS = 30


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 instant (``...Z`` or offset) to an aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def already_dispatched(
    client: Any, table: str | None, thread_id: str, mention_ts: str | None
) -> bool:
    """True iff this thread's recorded dispatch is at least as new as ``mention_ts``.

    Fail-open: returns ``False`` (→ "dispatch") when the table is unset, the row is missing, either
    timestamp is unparseable, or the read errors — so the backstop never drops a real mention.
    """
    if not table or not thread_id:
        return False
    mention_dt = _parse_ts(mention_ts)
    if mention_dt is None:
        return False  # no comparable timestamp → fail open (never suppress a real mention)
    try:
        resp = client.get_item(
            TableName=table,
            Key={"thread_id": {"S": thread_id}},
            ConsistentRead=True,
        )
    except Exception as e:  # noqa: BLE001 — backstop is best-effort; fail open on any read error
        logger.warning("dedup: get_item failed for thread %s: %s; failing open", thread_id, e)
        return False
    item = resp.get("Item") or {}
    recorded = _parse_ts((item.get("last_dispatched_ts") or {}).get("S"))
    if recorded is None:
        return False
    # Already handled iff we previously dispatched a mention at or after this one's timestamp.
    return recorded >= mention_dt


def record_dispatch(client: Any, table: str | None, thread_id: str, mention_ts: str | None) -> bool:
    """Atomically record the dispatch intent for ``mention_ts``. Returns False iff we LOST the race.

    The write is **conditional** (``attribute_not_exists OR last_dispatched_ts < :ts``) to close
    the check-then-write (TOCTOU) race between ``already_dispatched`` and this call: two
    overlapping polls (a Lambda retry on timeout, EventBridge's at-least-once delivery, a manual
    invoke racing the schedule) could both pass the ``GetItem`` and — with the old unconditional
    put — both dispatch into the same live session, which is exactly the double-fire this module
    exists to prevent. With the condition, exactly one poller wins the intent row; the loser gets
    ``ConditionalCheckFailedException`` → ``False`` → skips its dispatch.

    The condition uses lexicographic comparison on the ISO-8601 instants, which is
    chronologically correct because both sides are same-format UTC ``...Z`` strings from GitHub.
    An *equal* timestamp fails the condition (the same mention was already recorded) — matching
    ``already_dispatched``'s ``recorded >= mention`` suppression semantics.

    Still best-effort on infrastructure failures: any *other* error is swallowed with a warning
    and returns ``True`` (fail-open — the ``last_read_at`` gate remains in force, so losing the
    backstop write degrades to the original behavior rather than dropping the mention).
    """
    if not table or not thread_id or not mention_ts:
        return True
    ttl = int((datetime.now(timezone.utc) + timedelta(days=_ROW_TTL_DAYS)).timestamp())
    try:
        client.put_item(
            TableName=table,
            Item={
                "thread_id": {"S": thread_id},
                "last_dispatched_ts": {"S": mention_ts},
                "ttl": {"N": str(ttl)},
            },
            ConditionExpression="attribute_not_exists(thread_id) OR last_dispatched_ts < :ts",
            ExpressionAttributeValues={":ts": {"S": mention_ts}},
        )
    except Exception as e:  # noqa: BLE001 — losing the write degrades to last_read_at-only dedup
        if "conditionalcheckfailed" in f"{type(e).__name__}: {e}".lower():
            logger.info(
                "dedup: lost the intent-write race for thread %s (ts=%s); a concurrent poll "
                "already dispatched — suppressing duplicate",
                thread_id,
                mention_ts,
            )
            return False
        logger.warning("dedup: put_item failed for thread %s: %s; continuing", thread_id, e)
    return True


def clear_dispatch(client: Any, table: str | None, thread_id: str) -> None:
    """Roll back a recorded dispatch intent for ``thread_id``. Best-effort (never raises).

    Used when a dispatch we already recorded the *intent* for is then rejected by the runtime: we
    delete the backstop row so the next poll re-dispatches instead of the row suppressing the retry.
    A delete failure is swallowed with a warning — the worst case is one suppressed retry, which the
    ``last_read_at`` gate (left untouched on a rejection) will still re-surface on a later edit.
    """
    if not table or not thread_id or client is None:
        return
    try:
        client.delete_item(TableName=table, Key={"thread_id": {"S": thread_id}})
    except Exception as e:  # noqa: BLE001 — backstop is best-effort; a failed rollback isn't fatal
        logger.warning("dedup: delete_item failed for thread %s: %s; continuing", thread_id, e)
