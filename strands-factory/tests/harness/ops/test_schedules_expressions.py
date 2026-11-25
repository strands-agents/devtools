"""Schedule expressions: at / cron / rate syntax, intervals, and next-run previews."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strandly.ops.schedules import expressions
from strandly.ops.schedules.expressions import ExpressionError, parse

UTC = timezone.utc


@pytest.mark.parametrize(
    "text",
    [
        "rate(1 day)",
        "rate(15 minutes)",
        "rate(2 hours)",
        "cron(0 14 ? * * *)",
        "cron(0 15 ? * MON *)",
        "cron(0/30 9-17 ? * MON-FRI *)",
        "cron(0 9 L * ? *)",
        "cron(0 9 15W * ? *)",
        "cron(0 9 ? * L *)",
        "cron(0 9 ? * 3#2 *)",
        "cron(0 9 1 JAN,JUL ? 2030-2031)",
        "at(2030-01-02T03:04:05)",
    ],
)
def test_valid_expressions_parse(text):
    assert parse(text).text == text


@pytest.mark.parametrize(
    "text",
    [
        "rate(1 days)",
        "rate(2 day)",
        "rate(0 minutes)",
        "rate(1 week)",
        "cron(0 9 * * * *)",  # both day fields set
        "cron(0 9 ? * ? *)",  # neither day field set
        "cron(0 9 * *)",  # too few fields
        "cron(60 9 * * ? *)",
        "cron(0 24 * * ? *)",
        "cron(0 9 ? * 8 *)",
        "cron(? 9 * * ? *)",
        "cron(0 9 5-1 * ? *)",
        "at(2030-13-01T00:00:00)",
        "at(2030-01-01 00:00:00)",
        "every day",
        # Not in the EventBridge Scheduler guide:
        "cron(0 9 ? * 6L *)",  # nL in day-of-week
        "cron(0 9 LW * ? *)",
        "cron(0 9 ? * 2/2 *)",  # step in day-of-week
        "cron(0 9 ? * mon *)",  # lower-case names
        "cron(0 9 1 jan ? *)",
        "cron(0 9 ? * 3#1,6#3 *)",
        "rate(01 hour)",  # zero-padded value
        "rate(010 minutes)",
    ],
)
def test_invalid_expressions_are_rejected(text):
    with pytest.raises(ExpressionError):
        parse(text)


def test_kind_follows_the_expression():
    assert parse("at(2030-01-01T00:00:00)").kind == expressions.ONCE
    assert parse("rate(1 day)").kind == expressions.RECURRING
    assert parse("cron(0 9 * * ? *)").kind == expressions.RECURRING


def test_min_interval():
    assert parse("rate(15 minutes)").min_interval_seconds() == 900
    assert parse("rate(1 day)").min_interval_seconds() == 86400
    assert parse("cron(0 14 ? * * *)").min_interval_seconds() == 86400
    assert parse("cron(0 15 ? * MON *)").min_interval_seconds() == 86400
    assert parse("cron(0/30 9-17 ? * MON-FRI *)").min_interval_seconds() == 1800
    assert parse("cron(0 * * * ? *)").min_interval_seconds() == 3600
    # 23:00 and 00:30 are 90 minutes apart across midnight.
    assert parse("cron(0,30 0,23 * * ? *)").min_interval_seconds() == 30 * 60
    assert parse("cron(0 0,23 * * ? *)").min_interval_seconds() == 60 * 60
    assert parse("at(2030-01-01T00:00:00)").min_interval_seconds() is None


def test_timezones():
    assert expressions.zone("UTC") is UTC
    assert expressions.zone("Europe/Istanbul").utcoffset(datetime(2030, 1, 1)) is not None
    with pytest.raises(ExpressionError):
        expressions.zone("Mars/Olympus")


def test_at_time_is_local_to_the_timezone():
    expr = parse("at(2030-06-01T15:00:00)")
    assert expr.at_time("UTC") == datetime(2030, 6, 1, 15, tzinfo=UTC)
    # Istanbul is UTC+3.
    assert expr.at_time("Europe/Istanbul").astimezone(UTC) == datetime(2030, 6, 1, 12, tzinfo=UTC)


def test_cron_next_runs_weekly_and_daily():
    after = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)  # a Tuesday
    runs = parse("cron(0 15 ? * MON *)").next_runs("UTC", after, 2)
    assert runs == [datetime(2030, 1, 7, 15, tzinfo=UTC), datetime(2030, 1, 14, 15, tzinfo=UTC)]
    daily = parse("cron(0 14 ? * * *)").next_runs("UTC", datetime(2030, 1, 1, 14, tzinfo=UTC), 2)
    assert daily == [datetime(2030, 1, 2, 14, tzinfo=UTC), datetime(2030, 1, 3, 14, tzinfo=UTC)]


def test_cron_next_runs_respect_the_timezone():
    after = datetime(2030, 1, 1, tzinfo=UTC)
    (run,) = parse("cron(0 9 * * ? *)").next_runs("Europe/Istanbul", after, 1)
    assert run == datetime(2030, 1, 1, 6, tzinfo=UTC)


def test_cron_special_days():
    after = datetime(2030, 2, 1, tzinfo=UTC)
    assert parse("cron(0 9 L * ? *)").next_runs("UTC", after, 1)[0].day == 28
    # 2030-06-15 is a Saturday: 15W is Friday the 14th.
    assert parse("cron(0 9 15W * ? *)").next_runs("UTC", datetime(2030, 6, 1, tzinfo=UTC), 1)[
        0
    ].day == 14
    # Second Tuesday of January 2030 is the 8th; L (Saturday) after Tue 1st is the 5th.
    assert parse("cron(0 9 ? * 3#2 *)").next_runs("UTC", datetime(2030, 1, 1, tzinfo=UTC), 1)[
        0
    ].day == 8
    assert parse("cron(0 9 ? * L *)").next_runs("UTC", datetime(2030, 1, 1, tzinfo=UTC), 1)[
        0
    ].day == 5


def test_cron_that_never_fires_again_has_no_runs():
    assert parse("cron(0 9 1 1 ? 2020)").next_runs("UTC", datetime(2030, 1, 1, tzinfo=UTC)) == []


def test_once_and_rate_previews():
    after = datetime(2030, 1, 1, tzinfo=UTC)
    assert parse("at(2030-01-02T00:00:00)").next_runs("UTC", after) == [
        datetime(2030, 1, 2, tzinfo=UTC)
    ]
    assert parse("at(2029-01-02T00:00:00)").next_runs("UTC", after) == []
    assert parse("rate(2 hours)").next_runs("UTC", after, 2)[1] == datetime(
        2030, 1, 1, 2, tzinfo=UTC
    )


def test_rate_preview_keeps_the_anchor_phase():
    expr = parse("rate(1 day)")
    anchor = datetime(2029, 12, 20, 19, 28, tzinfo=UTC)
    runs = expr.next_runs("UTC", datetime(2030, 1, 1, 21, tzinfo=UTC), 2, anchor=anchor)
    assert runs == [datetime(2030, 1, 2, 19, 28, tzinfo=UTC), datetime(2030, 1, 3, 19, 28, tzinfo=UTC)]
    # A future anchor is the first fire.
    future = datetime(2030, 1, 5, 8, tzinfo=UTC)
    assert expr.next_runs("UTC", datetime(2030, 1, 1, tzinfo=UTC), 1, anchor=future) == [future]


def test_previews_are_clipped_to_the_start_and_end_dates():
    after = datetime(2030, 1, 1, tzinfo=UTC)
    cron = parse("cron(0 9 * * ? *)")
    runs = cron.next_runs(
        "UTC",
        after,
        5,
        not_before=datetime(2030, 1, 3, tzinfo=UTC),
        until=datetime(2030, 1, 5, 9, tzinfo=UTC),
    )
    assert runs == [datetime(2030, 1, d, 9, tzinfo=UTC) for d in (3, 4)]
    rate = parse("rate(12 hours)")
    runs = rate.next_runs("UTC", after, 5, anchor=after, until=datetime(2030, 1, 2, 1, tzinfo=UTC))
    assert runs == [datetime(2030, 1, 1, 12, tzinfo=UTC), datetime(2030, 1, 2, tzinfo=UTC)]


def test_cron_preview_follows_daylight_saving():
    # 2030-03-10: New York skips 02:00-03:00; 2030-11-03 repeats 01:00-02:00.
    expr = parse("cron(30 1,2,3 * * ? *)")
    spring = expr.next_runs("America/New_York", datetime(2030, 3, 10, 5, tzinfo=UTC), 4)
    assert [w.astimezone(expressions.zone("America/New_York")).hour for w in spring] == [1, 3, 1, 2]
    assert len(set(spring)) == len(spring)
    autumn = expr.next_runs("America/New_York", datetime(2030, 11, 3, 4, tzinfo=UTC), 3)
    # 01:30 runs once (the first, EDT one), then 02:30 and 03:30 EST.
    assert autumn == [
        datetime(2030, 11, 3, 5, 30, tzinfo=UTC),
        datetime(2030, 11, 3, 7, 30, tzinfo=UTC),
        datetime(2030, 11, 3, 8, 30, tzinfo=UTC),
    ]


def test_at_expression_formats_local_time():
    assert expressions.at_expression(datetime(2030, 1, 2, 3, 4, 5)) == "at(2030-01-02T03:04:05)"


def test_missing_timezone_database_is_loud(monkeypatch):
    monkeypatch.setattr(expressions, "available_timezones", lambda: set())
    monkeypatch.setattr(expressions, "ZoneInfo", _no_zone)
    with pytest.raises(ExpressionError, match="no IANA timezone database"):
        expressions.zone("Europe/Istanbul")
    assert expressions.zone("UTC") is UTC


def _no_zone(name):
    raise expressions.ZoneInfoNotFoundError(name)


def test_trigger_lambda_bundle_ships_tzdata():
    from pathlib import Path

    script = Path(__file__).parents[3] / "infra" / "scripts" / "build-poller-package.sh"
    assert 'uv pip install --python "${PYTHON:-python3}" "tzdata==${TZDATA_VERSION}" --target "${target}" --no-deps' in script.read_text()
