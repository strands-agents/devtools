"""The alarm notifier Lambda: SNS parsing, message formatting, home-channel gating, retries."""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from strandly.ops.lambdas import slack_alarms
from strandly.ops.lambdas.slack_ingress import handler as ingress

ALARM_ARN = "arn:aws:cloudwatch:us-west-2:111111111111:alarm:strandly-prod-failure-rate"


def _alarm(**overrides: Any) -> dict[str, Any]:
    alarm = {
        "AlarmName": "strandly-prod-failure-rate",
        "AlarmDescription": "Run failure rate above 20% over 15 minutes.",
        "AlarmArn": ALARM_ARN,
        "NewStateValue": "ALARM",
        "OldStateValue": "OK",
        "NewStateReason": "Threshold Crossed: 1 datapoint [42.0] was greater than the threshold (20.0).",
        "Region": "US West (Oregon)",
        "Trigger": {
            "MetricName": "FailureRate",
            "Namespace": "Strandly-prod",
            "Statistic": "AVERAGE",
            "Period": 900,
            "ComparisonOperator": "GreaterThanThreshold",
            "Threshold": 20.0,
        },
    }
    alarm.update(overrides)
    return alarm


def _event(*messages: Any, subject: str | None = None) -> dict[str, Any]:
    records = []
    for message in messages:
        sns: dict[str, Any] = {
            "Message": message if isinstance(message, str) else json.dumps(message)
        }
        if subject:
            sns["Subject"] = subject
        records.append({"EventSource": "aws:sns", "Sns": sns})
    return {"Records": records}


@pytest.fixture
def secrets(monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("STRANDLY_SLACK_HOME_CHANNEL_ENABLED", "true")
    values = {"bot_token": "xoxb-1", "home_channel": "C0HOME", "signing_secret": "s"}
    monkeypatch.setattr(slack_alarms, "load_secrets", lambda: values)
    return values


@pytest.fixture
def posts(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        calls.append({"method": method, "token": token, **payload})
        return {"ok": True, "ts": f"1712345678.{len(calls):06d}"}

    monkeypatch.setattr(slack_alarms, "_slack_post", fake)
    return calls


# ---- formatting -------------------------------------------------------------------------------


def test_alarm_post_is_a_title_with_reason_metric_and_link_as_details():
    formatted = slack_alarms.format_alarm(_alarm())
    assert formatted is not None
    title, text = formatted
    assert title == ":rotating_light: *ALARM* `strandly-prod-failure-rate` (was OK)"
    lines = text.splitlines()
    assert lines[0] == "Run failure rate above 20% over 15 minutes."
    assert "*Reason:* Threshold Crossed" in text
    assert "*Metric:* `Strandly-prod/FailureRate` (average, 900s, > 20)" in text
    assert lines[-1] == (
        "<https://us-west-2.console.aws.amazon.com/cloudwatch/home?region=us-west-2"
        "#alarmsV2:alarm/strandly-prod-failure-rate|Open in CloudWatch>"
    )


def test_ok_transition_is_posted():
    formatted = slack_alarms.format_alarm(_alarm(NewStateValue="OK", OldStateValue="ALARM"))
    assert formatted is not None and formatted[0].startswith(":white_check_mark: *OK*")


def test_insufficient_data_is_skipped():
    assert slack_alarms.format_alarm(_alarm(NewStateValue="INSUFFICIENT_DATA")) is None


def test_mrkdwn_control_characters_are_escaped():
    formatted = slack_alarms.format_alarm(_alarm(NewStateReason="a < b & <!channel>"))
    assert formatted is not None
    text = "\n".join(formatted)
    assert "a &lt; b &amp; &lt;!channel&gt;" in text
    assert "<!channel>" not in text


def test_long_reason_is_clipped():
    formatted = slack_alarms.format_alarm(_alarm(NewStateReason="x" * 5000))
    assert formatted is not None and len(formatted[1]) < 2000


def test_metric_math_alarm():
    trigger = {
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "Threshold": 1,
        "Metrics": [
            {"Id": "e1", "Expression": "m1 / m2 * 100", "ReturnData": True},
            {
                "Id": "m1",
                "ReturnData": False,
                "MetricStat": {"Metric": {"Namespace": "Strandly-prod", "MetricName": "Failures"}},
            },
        ],
    }
    assert slack_alarms.describe_metric(trigger) == (
        "`m1 / m2 * 100` over `Strandly-prod/Failures` (≥ 1)"
    )


def test_console_link_quotes_the_alarm_name_and_uses_the_arn_region():
    url = slack_alarms.console_url("eu-west-1", "a b/c")
    assert url.endswith("#alarmsV2:alarm/a%20b%2Fc") and "region=eu-west-1" in url
    alarm = _alarm(AlarmArn="arn:aws:cloudwatch:eu-west-1:1:alarm:x")
    assert slack_alarms.alarm_region(alarm) == "eu-west-1"


def test_non_alarm_message_posts_subject_and_body():
    record = _event({"stuck": ["t1"]}, subject="⚠ strandly: 1 stuck run(s)")["Records"][0]
    formatted = slack_alarms.format_record(record)
    assert formatted is not None
    assert formatted[0] == ":warning: *⚠ strandly: 1 stuck run(s)*"
    assert '"stuck"' in formatted[1]


def test_malformed_records_are_ignored():
    assert list(slack_alarms.sns_records({"Records": "x"})) == []
    assert list(slack_alarms.sns_records(None)) == []
    assert slack_alarms.format_record({"Sns": None}) is None


# ---- handler ---------------------------------------------------------------------------------


def test_handler_posts_alarms_to_the_home_channel(secrets, posts):
    out = slack_alarms.lambda_handler(_event(_alarm(), _alarm(NewStateValue="INSUFFICIENT_DATA")))
    assert out == {"status": "ok", "posted": 1, "skipped": 1, "failed": 0}
    assert [p["method"] for p in posts] == ["chat.postMessage", "chat.postMessage"]
    assert posts[0]["channel"] == "C0HOME" and posts[0]["token"] == "xoxb-1"
    assert posts[0]["unfurl_links"] is False
    # Title at the top level, details as the first reply in its thread.
    assert "thread_ts" not in posts[0] and posts[0]["text"].startswith(":rotating_light: *ALARM*")
    assert posts[1]["thread_ts"] == "1712345678.000001" and "*Reason:*" in posts[1]["text"]


class _Ledger:
    def __init__(self, error: Exception | None = None) -> None:
        self.items: list[dict[str, Any]] = []
        self.error = error

    def put_item(self, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.items.append(kwargs)


def test_handler_records_the_thread_link(secrets, posts, monkeypatch):
    ledger = _Ledger()
    monkeypatch.setenv("STRANDLY_RUN_LEDGER_TABLE", "strandly-prod-runledger")
    monkeypatch.setattr(slack_alarms, "_ledger_client", lambda: ledger)
    slack_alarms.lambda_handler(_event(_alarm()))
    (call,) = ledger.items
    assert call["TableName"] == "strandly-prod-runledger"
    assert call["ConditionExpression"] == "attribute_not_exists(task_id)"
    item = {k: v["S"] for k, v in call["Item"].items()}
    assert item["task_id"] == "slack-thread#C0HOME#1712345678.000001"
    assert item["origin"] == "alarm:strandly-prod-failure-rate"
    assert "*Reason:*" in item["details"] and "gsi_pk" not in item


def test_a_failed_link_write_or_details_reply_does_not_fail_the_alarm(secrets, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        calls.append(payload)
        if payload.get("thread_ts"):
            return {"ok": False, "error": "ratelimited"}
        return {"ok": True, "ts": "1712345678.000001"}

    monkeypatch.setenv("STRANDLY_RUN_LEDGER_TABLE", "t")
    monkeypatch.setattr(slack_alarms, "_ledger_client", lambda: _Ledger(RuntimeError("denied")))
    monkeypatch.setattr(slack_alarms, "_slack_post", fake)
    assert slack_alarms.lambda_handler(_event(_alarm()))["posted"] == 1
    assert len(calls) == 2


@pytest.mark.parametrize(
    "values",
    [
        {"bot_token": "xoxb-1"},
        {"home_channel": "C0HOME"},
        {"bot_token": "x", "home_channel": "general"},
    ],
)
def test_handler_is_inert_without_a_home_channel(monkeypatch, posts, values):
    monkeypatch.setenv("STRANDLY_SLACK_HOME_CHANNEL_ENABLED", "true")
    monkeypatch.setattr(slack_alarms, "load_secrets", lambda: values)
    out = slack_alarms.lambda_handler(_event(_alarm()))
    assert out["status"] == "unconfigured" and posts == []


def test_permanent_slack_error_is_logged_not_raised(secrets, monkeypatch):
    monkeypatch.setattr(
        slack_alarms, "_slack_post", lambda *a: {"ok": False, "error": "not_in_channel"}
    )
    assert slack_alarms.lambda_handler(_event(_alarm()))["failed"] == 1


@pytest.mark.parametrize(
    "behaviour",
    [
        lambda *a: {"ok": False, "error": "ratelimited"},
        lambda *a: (_ for _ in ()).throw(urllib.error.URLError("down")),
    ],
)
def test_transient_slack_error_raises_for_an_sns_retry(secrets, monkeypatch, behaviour):
    monkeypatch.setattr(slack_alarms, "_slack_post", behaviour)
    with pytest.raises(slack_alarms.TransientSlackError):
        slack_alarms.lambda_handler(_event(_alarm()))


def test_reads_the_same_secret_key_as_the_ingress():
    assert ingress.HOME_CHANNEL_SECRET_KEY == "home_channel"
