"""Alarm notifier Lambda: posts monitoring-topic messages to the team's Slack home channel.

Subscribed to the ``strandly-<env>-monitoring`` SNS topic. Each notification is a title line (an
alarm's state and name, or another message's subject) with the details (reason, metric and console
link, or the truncated body) as the first reply in its thread, like ``notify_team``. Only ``ALARM``
and ``OK`` transitions post. The thread link (``strandly.ops.slack``) gives replies the details.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any

from strandly.core.config import SLACK_HOME_CHANNEL_ENABLED, home_channel_enabled, slack_channel_id
from strandly.ops.lambdas.slack_ingress.handler import (
    HOME_CHANNEL_SECRET_KEY,
    load_secrets,
)
from strandly.ops.slack import (
    TRANSIENT_ERRORS,
    error_code,
    put_thread_link_client,
    slack_error_message,
    thread_link_item,
)

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
_USER_AGENT = "strandly-slack-alarms/1.0"

POSTED_STATES = ("ALARM", "OK")
_STATE_EMOJI = {"ALARM": ":rotating_light:", "OK": ":white_check_mark:"}
_OPERATORS = {
    "GreaterThanThreshold": ">",
    "GreaterThanOrEqualToThreshold": "≥",
    "LessThanThreshold": "<",
    "LessThanOrEqualToThreshold": "≤",
}
REASON_MAX = 600
BODY_MAX = 1500
# The run ledger, set only with the home channel enabled: where thread links are written.
RUN_LEDGER_TABLE_ENV = "STRANDLY_RUN_LEDGER_TABLE"


class TransientSlackError(RuntimeError):
    """A post that may succeed on retry."""


def escape(text: str) -> str:
    """Slack mrkdwn control characters (``&``, ``<``, ``>``) as entities."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def alarm_region(alarm: Mapping[str, Any]) -> str:
    """The alarm's region code, from its ARN (``Region`` in the message is a display name)."""
    parts = str(alarm.get("AlarmArn") or "").split(":")
    if len(parts) > 3 and parts[3]:
        return parts[3]
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def console_url(region: str, alarm_name: str) -> str:
    name = urllib.parse.quote(alarm_name, safe="")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#alarmsV2:alarm/{name}"
    )


def describe_metric(trigger: Any) -> str:
    """The alarm's metric (or metric-math expression) and threshold, or ``""``."""
    if not isinstance(trigger, Mapping):
        return ""
    op = _OPERATORS.get(str(trigger.get("ComparisonOperator") or ""), "")
    threshold = trigger.get("Threshold")
    condition = f"{op} {threshold:g}" if op and isinstance(threshold, (int, float)) else ""
    name = trigger.get("MetricName")
    if name:
        metric = f"`{trigger.get('Namespace') or ''}/{name}`"
        stat = trigger.get("Statistic") or trigger.get("ExtendedStatistic")
        details = [
            str(stat).lower() if stat else "",
            f"{trigger.get('Period')}s" if trigger.get("Period") else "",
        ]
        details.append(condition)
        detail = ", ".join(d for d in details if d)
        return f"{metric} ({detail})" if detail else metric
    metrics = trigger.get("Metrics")
    if isinstance(metrics, list):
        expressions = [
            str(m.get("Expression"))
            for m in metrics
            if isinstance(m, Mapping) and m.get("Expression") and m.get("ReturnData", True)
        ]
        names = []
        for m in metrics:
            stat = m.get("MetricStat") if isinstance(m, Mapping) else None
            spec = stat.get("Metric") if isinstance(stat, Mapping) else None
            if isinstance(spec, Mapping) and spec.get("MetricName"):
                names.append(f"{spec.get('Namespace') or ''}/{spec.get('MetricName')}")
        if expressions or names:
            text = f"`{escape(expressions[0])}`" if expressions else ""
            if names:
                text += (" over " if text else "") + ", ".join(f"`{n}`" for n in names)
            return f"{text} ({condition})" if condition else text
    return ""


def format_alarm(alarm: Mapping[str, Any]) -> tuple[str, str] | None:
    """The ``(title, details)`` posts for one CloudWatch alarm notification, or ``None`` to skip it."""
    state = str(alarm.get("NewStateValue") or "")
    if state not in POSTED_STATES:
        return None
    name = str(alarm.get("AlarmName") or "unknown alarm")
    head = f"{_STATE_EMOJI[state]} *{state}* `{escape(name)}`"
    old = alarm.get("OldStateValue")
    if old:
        head += f" (was {escape(str(old))})"
    lines: list[str] = []
    description = alarm.get("AlarmDescription")
    if description:
        lines.append(escape(_clip(" ".join(str(description).split()), 300)))
    reason = alarm.get("NewStateReason")
    if reason:
        lines.append(f"*Reason:* {escape(_clip(str(reason), REASON_MAX))}")
    metric = describe_metric(alarm.get("Trigger"))
    if metric:
        lines.append(f"*Metric:* {metric}")
    lines.append(f"<{console_url(alarm_region(alarm), name)}|Open in CloudWatch>")
    return head, "\n".join(lines)


def format_record(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """The ``(title, details)`` for one SNS record: an alarm, or any other message's subject + body."""
    sns = record.get("Sns")
    if not isinstance(sns, Mapping):
        return None
    message = str(sns.get("Message") or "")
    try:
        parsed = json.loads(message)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, Mapping) and "AlarmName" in parsed and "NewStateValue" in parsed:
        return format_alarm(parsed)
    subject = str(sns.get("Subject") or "Strandly monitoring notification")
    body = escape(_clip(message.strip(), BODY_MAX))
    return f":warning: *{escape(subject)}*", f"```{body}```" if body else "(no message body)"


def sns_records(event: Any) -> Iterator[Mapping[str, Any]]:
    records = event.get("Records") if isinstance(event, Mapping) else None
    for record in records if isinstance(records, list) else ():
        if isinstance(record, Mapping):
            yield record


def _slack_post(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    """POST one Slack Web API call to the fixed slack.com host (the network seam tests patch)."""
    req = urllib.request.Request(
        f"{SLACK_API}/{method}", data=json.dumps(payload).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", _USER_AGENT)
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — fixed slack.com host
        return json.loads(resp.read().decode())


def post(token: str, channel: str, text: str, thread_ts: str = "") -> str | None:
    """Post ``text`` to ``channel`` (under ``thread_ts``); its ``ts`` (``""`` if Slack sent none)
    on success, ``None`` on a permanent failure. Raises :class:`TransientSlackError`."""
    payload: dict[str, Any] = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        result = _slack_post("chat.postMessage", payload, token)
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500:
            raise TransientSlackError(f"chat.postMessage HTTP {e.code}; delivery uncertain") from None
        logger.error("slack alarms: chat.postMessage HTTP %d; not retryable", e.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TransientSlackError("chat.postMessage transport failure; delivery uncertain") from None
    if result.get("ok"):
        ts = result.get("ts")
        return ts if isinstance(ts, str) else ""
    error = error_code(result)
    if error in TRANSIENT_ERRORS:
        raise TransientSlackError(f"chat.postMessage: {error}")
    logger.error("slack alarms: %s", slack_error_message(result))
    return None


def post_details(token: str, channel: str, root: str, details: str) -> None:
    """The details reply under a posted title. Never raises: a retry would repeat the title."""
    if not root:
        return
    try:
        if post(token, channel, details, thread_ts=root) is None:
            logger.warning("slack alarms: details reply failed; the title stands alone")
    except TransientSlackError as e:
        logger.warning("slack alarms: details reply: %s; not retried (the title posted)", e)


def _ledger_client() -> Any | None:
    if not os.environ.get(RUN_LEDGER_TABLE_ENV):
        return None
    import boto3

    return boto3.client("dynamodb")


def _origin(record: Mapping[str, Any]) -> str:
    sns = record.get("Sns")
    try:
        parsed = json.loads(str(sns.get("Message") or "")) if isinstance(sns, Mapping) else None
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, Mapping) and parsed.get("AlarmName"):
        return f"alarm:{parsed['AlarmName']}"
    return "monitoring"


def lambda_handler(event: Any, context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """SNS → Slack. Returns counts; raises only on a transient Slack failure (SNS retries)."""
    records = list(sns_records(event))
    if not home_channel_enabled(os.environ.get(SLACK_HOME_CHANNEL_ENABLED)):
        return {"status": "disabled", "posted": 0, "skipped": len(records)}
    secrets = load_secrets()
    token = secrets.get("bot_token") or ""
    channel = slack_channel_id(secrets.get(HOME_CHANNEL_SECRET_KEY))
    if not token or not channel:
        logger.info(
            "slack alarms: no bot_token/home_channel in the Slack secret; %d record(s) skipped",
            len(records),
        )
        return {"status": "unconfigured", "posted": 0, "skipped": len(records)}
    logger.info("slack alarms: %d record(s) for the home channel", len(records))
    ledger, table = _ledger_client(), os.environ.get(RUN_LEDGER_TABLE_ENV)
    posted = skipped = failed = 0
    transient: list[str] = []
    for record in records:
        formatted = format_record(record)
        if formatted is None:
            skipped += 1
            continue
        title, details = formatted
        try:
            root = post(token, channel, title)
            if root is not None:
                posted += 1
                if root:
                    put_thread_link_client(ledger, table, thread_link_item(
                        channel_id=channel, thread_ts=root, title=title, details=details,
                        origin=_origin(record),
                    ))
                post_details(token, channel, root, details)
            else:
                failed += 1
                # Permanent credential/scope/channel failures affect the whole batch. Do not
                # repeat the same failed API call for every SNS record.
                skipped += len(records) - posted - skipped - failed - len(transient)
                break
        except TransientSlackError as e:
            logger.warning("slack alarms: %s", e)
            transient.append(str(e))
    logger.info(
        "slack alarms: done posted=%d skipped=%d failed=%d transient=%d",
        posted,
        skipped,
        failed,
        len(transient),
    )
    if transient:
        raise TransientSlackError("; ".join(transient))
    return {"status": "failed" if failed else "ok", "posted": posted, "skipped": skipped, "failed": failed}
