"""Slack helpers shared by notification and ingress callers: sanitized diagnostics and thread links.

A thread link is one run-ledger row per home-channel notification thread, keyed by
:func:`thread_link_key`. The poster (the ``notify_team`` tool, the alarm notifier) writes it; the
Slack ingress reads it when someone replies in that thread, so the reply's run starts with the
notification's title, details and originating session. Rows carry no ``gsi_pk``, so run listings
never see them.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Mirrored as SLACK_THREAD_LINK_KEY_PREFIX in infra/stacks/common.py (tests/test_infra_constants_sync):
# the ingress GetItem and alarm notifier PutItem grants are limited to keys with this prefix.
THREAD_LINK_KEY_PREFIX = "slack-thread#"
THREAD_LINK_TITLE_MAX = 150
THREAD_LINK_DETAILS_MAX = 3000
_TS_RE = re.compile(r"^\d+\.\d+$")

# Do not echo arbitrary upstream bodies, identifiers, credentials, or exception text.
_SCOPES = frozenset({
    "chat:write", "reactions:write", "im:history", "im:read", "im:write",
    "channels:history", "groups:history", "channels:read", "groups:read",
})
TRANSIENT_ERRORS = frozenset({"ratelimited", "internal_error", "service_unavailable", "fatal_error"})
_PERMANENT_ERRORS = frozenset({
    "missing_scope", "not_in_channel", "channel_not_found", "invalid_auth", "token_revoked",
    "account_inactive", "no_permission", "is_archived", "restricted_action", "not_authed",
    "already_reacted", "no_reaction", "invalid_arguments", "invalid_arg_name",
})


def error_code(response: Mapping[str, Any]) -> str:
    value = response.get("error")
    return value if isinstance(value, str) and value in TRANSIENT_ERRORS | _PERMANENT_ERRORS else "unknown_error"


def slack_error_message(response: Mapping[str, Any]) -> str:
    code = error_code(response)
    if code == "missing_scope":
        needed = sorted(set(str(response.get("needed", "")).split(",")) & _SCOPES)
        scopes = ", ".join(needed) or "required scope not supplied by Slack; inspect the method's Slack documentation"
        return (
            f"Slack API error: missing_scope; needed: {scopes}. Not retryable. "
            "Operator must review app scopes, obtain workspace approval if required, and reinstall "
            "before retrying. No approval is implied; keep home-channel features disabled meanwhile."
        )
    return f"Slack API error: {code}; " + (
        "transient failure (delivery may be uncertain)." if code in TRANSIENT_ERRORS
        else "not retryable without operator intervention."
    )


def is_slack_ts(value: object) -> bool:
    """Whether ``value`` is a Slack message timestamp (``1712345678.123456``)."""
    return isinstance(value, str) and bool(_TS_RE.match(value))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def thread_link_key(channel_id: str, thread_ts: str) -> str:
    """The run-ledger ``task_id`` of the link for one home-channel thread."""
    return f"{THREAD_LINK_KEY_PREFIX}{channel_id}#{thread_ts}"


def thread_link_item(
    *,
    channel_id: str,
    thread_ts: str,
    title: str,
    details: str,
    origin_session_id: str = "",
    origin: str = "",
    now: datetime | None = None,
) -> dict[str, str]:
    """The link row as plain strings (a boto3 ``Table`` item; see :func:`to_attribute_values`)."""
    item = {
        "task_id": thread_link_key(channel_id, thread_ts),
        "kind": "slack-thread-link",
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "title": clip(title.strip(), THREAD_LINK_TITLE_MAX),
        "details": clip(details.strip(), THREAD_LINK_DETAILS_MAX),
        "origin_session_id": origin_session_id,
        "origin": origin,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    return {k: v for k, v in item.items() if v}


def to_attribute_values(item: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """A string-only item in the low-level DynamoDB client's ``{"S": ...}`` shape."""
    return {k: {"S": v} for k, v in item.items()}


def put_thread_link_client(ddb_client: Any, table: str | None, item: Mapping[str, str]) -> bool:
    """Write a link with the low-level client unless the thread already has one. Never raises."""
    if ddb_client is None or not table:
        return False
    try:
        ddb_client.put_item(
            TableName=table,
            Item=to_attribute_values(item),
            ConditionExpression="attribute_not_exists(task_id)",
        )
    except Exception as e:  # noqa: BLE001 - the link is context for later replies, not delivery
        response = getattr(e, "response", None)
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        if code != "ConditionalCheckFailedException":
            logger.warning("slack thread link: write failed (%s)", type(e).__name__)
        return False
    return True


def read_thread_link(
    ddb_client: Any, table: str | None, channel_id: str, thread_ts: str
) -> dict[str, str] | None:
    """The link for one thread, or ``None`` when absent, unconfigured or unreadable. Never raises."""
    if ddb_client is None or not table or not channel_id or not is_slack_ts(thread_ts):
        return None
    try:
        response = ddb_client.get_item(
            TableName=table,
            Key={"task_id": {"S": thread_link_key(channel_id, thread_ts)}},
            ConsistentRead=True,
        )
    except Exception as e:  # noqa: BLE001 - context, not control flow: the reply still dispatches
        logger.warning("slack thread link: read failed (%s)", type(e).__name__)
        return None
    raw = response.get("Item") if isinstance(response, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    item = {k: v["S"] for k, v in raw.items() if isinstance(v, Mapping) and isinstance(v.get("S"), str)}
    return item if item.get("title") or item.get("details") else None
