"""EventBridge Scheduler expressions: parse, validate and preview ``at``, ``cron`` and ``rate``.

Only the syntax the Scheduler guide documents is accepted, so a job that validates also creates.
Cron is the Scheduler dialect: six fields, day-of-week 1-7 = SUN-SAT, and exactly one of
day-of-month and day-of-week is ``?``.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

ONCE = "once"
RECURRING = "recurring"

_AT_RE = re.compile(r"^at\((\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\)$")
_RATE_RE = re.compile(r"^rate\(([1-9]\d*) (minute|minutes|hour|hours|day|days)\)$")
_CRON_RE = re.compile(r"^cron\((.*)\)$")
_UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}
_MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
_DAYS = {"SUN": 1, "MON": 2, "TUE": 3, "WED": 4, "THU": 5, "FRI": 6, "SAT": 7}
_CRON_HORIZON_DAYS = 366 * 5


class ExpressionError(ValueError):
    """The expression or timezone is not something EventBridge Scheduler would run."""


def zone(name: str) -> tzinfo:
    """The ``tzinfo`` for an IANA name (``UTC`` needs no tz database)."""
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        if not available_timezones():
            raise ExpressionError(
                f"cannot resolve timezone {name!r}: no IANA timezone database (bundle tzdata)"
            ) from e
        raise ExpressionError(f"unknown timezone {name!r}") from e


def aws_weekday(d: date) -> int:
    """Day-of-week in cron numbering (1 = SUN ... 7 = SAT)."""
    return (d.weekday() + 1) % 7 + 1


@dataclass(frozen=True)
class _Field:
    values: frozenset[int]
    any: bool = False  # ``*`` or ``?``
    last: bool = False  # DOM ``L``
    nearest_weekday: int | None = None  # DOM ``nW``
    nth: tuple[int, int] | None = None  # DOW ``d#n``: the n-th <weekday> of the month


def _value(token: str, lo: int, hi: int, names: dict[str, int] | None) -> int:
    if names and token in names:
        return names[token]
    if names and token.upper() in names:
        raise ExpressionError(f"cron names are upper case: {token.upper()!r}, not {token!r}")
    if not token.isdigit():
        raise ExpressionError(f"bad cron value {token!r}")
    n = int(token)
    if not lo <= n <= hi:
        raise ExpressionError(f"cron value {n} out of range {lo}-{hi}")
    return n


def _parse_field(
    text: str, lo: int, hi: int, *, names: dict[str, int] | None = None, kind: str = ""
) -> _Field:
    if text in ("*", "?"):
        return _Field(frozenset(range(lo, hi + 1)), any=True)
    if kind == "dom":
        if text == "L":
            return _Field(frozenset(), last=True)
        if text.endswith("W"):
            return _Field(frozenset(), nearest_weekday=_value(text[:-1], lo, hi, None))
    if kind == "dow":
        if text == "L":
            return _Field(frozenset({7}))
        if "/" in text:
            raise ExpressionError(f"day-of-week does not take a '/' step: {text!r}")
        if "#" in text:
            day, _, n = text.partition("#")
            nth = _value(n, 1, 5, None)
            return _Field(frozenset(), nth=(_value(day, lo, hi, names), nth))
    values: set[int] = set()
    for item in text.split(","):
        base, _, step_text = item.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ExpressionError(f"bad cron step {item!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _value(a, lo, hi, names), _value(b, lo, hi, names)
            if start > end:
                raise ExpressionError(f"bad cron range {item!r}")
        else:
            start = _value(base, lo, hi, names)
            end = hi if step_text else start
        values.update(range(start, end + 1, step))
    return _Field(frozenset(values))


@dataclass(frozen=True)
class Expression:
    """A parsed schedule expression."""

    text: str
    kind: str  # ONCE | RECURRING
    at: datetime | None = None  # naive local time of a once job
    rate_seconds: int | None = None
    cron: tuple[_Field, ...] | None = None

    def at_time(self, tz: str) -> datetime:
        """The aware time a once job fires."""
        if self.at is None:
            raise ExpressionError("not an at() expression")
        return self.at.replace(tzinfo=zone(tz))

    def min_interval_seconds(self) -> int | None:
        """Smallest gap between two fires (``None`` for a once job).

        Cron is judged by its times of day: one time a day counts as a day apart even when the
        day fields allow fewer, which is all the minimum-interval checks need.
        """
        if self.rate_seconds is not None:
            return self.rate_seconds
        if self.cron is None:
            return None
        minutes, hours = self.cron[0].values, self.cron[1].values
        times = sorted(h * 60 + m for h in hours for m in minutes)
        if len(times) == 1:
            return 86400
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        gaps.append(times[0] + 1440 - times[-1])
        return min(gaps) * 60

    def next_runs(
        self,
        tz: str,
        after: datetime,
        count: int = 5,
        *,
        anchor: datetime | None = None,
        not_before: datetime | None = None,
        until: datetime | None = None,
    ) -> list[datetime]:
        """The next ``count`` fire times (UTC) after ``after``.

        A rate schedule fires at ``anchor + k * period``, the first strictly after ``after``;
        without an ``anchor`` it starts at ``after`` itself (a new schedule with no start date). ``not_before`` / ``until`` clip recurring times to
        ``[not_before, until)`` (a schedule's start and end date).
        """
        after = after.astimezone(timezone.utc)
        if self.kind == ONCE:
            when = self.at_time(tz).astimezone(timezone.utc)
            return [when] if when > after else []
        out: list[datetime] = []
        if not_before is not None and not_before > after:
            after = not_before - timedelta(microseconds=1)
        if self.rate_seconds is not None:
            step = timedelta(seconds=self.rate_seconds)
            if anchor is None:
                first = not_before if not_before is not None and not_before > after else after
            else:
                first = next_aligned(anchor, after, step)
            out = [first + step * i for i in range(count)]
        else:
            for when in self._cron_times(zone(tz), after):
                if until is not None and when >= until:
                    break
                out.append(when)
                if len(out) >= count:
                    break
        return [w for w in out if until is None or w < until]

    def _cron_times(self, tz: tzinfo, after: datetime) -> Iterator[datetime]:
        assert self.cron is not None
        minutes, hours, dom, months, dow, years = self.cron
        times = sorted((h, m) for h in hours.values for m in minutes.values)
        day = after.astimezone(tz).date()
        last = after
        for _ in range(_CRON_HORIZON_DAYS):
            if (
                day.year in years.values
                and day.month in months.values
                and _day_matches(day, dom, dow)
            ):
                for h, m in times:
                    when_utc = _local_to_utc(day, h, m, tz)
                    if when_utc is not None and when_utc > last:
                        last = when_utc
                        yield when_utc
            day += timedelta(days=1)


def next_aligned(anchor: datetime, after: datetime, step: timedelta) -> datetime:
    """The first ``anchor + k * step`` (``k >= 0``) strictly after ``after``."""
    if anchor > after:
        return anchor
    k = (after - anchor) // step + 1
    return anchor + step * k


def _local_to_utc(day: date, h: int, m: int, tz: tzinfo) -> datetime | None:
    """A local wall-clock time in UTC; ``None`` when daylight saving skips it. A time that occurs
    twice maps to its first occurrence."""
    naive = datetime(day.year, day.month, day.day, h, m)
    when_utc = naive.replace(tzinfo=tz).astimezone(timezone.utc)
    if when_utc.astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return when_utc


def _day_matches(day: date, dom: _Field, dow: _Field) -> bool:
    if dom.any:
        return _dow_matches(day, dow)
    return _dom_matches(day, dom)


def _dom_matches(day: date, f: _Field) -> bool:
    last = calendar.monthrange(day.year, day.month)[1]
    if f.last:
        return day.day == last
    if f.nearest_weekday is not None:
        n = min(f.nearest_weekday, last)
        target = date(day.year, day.month, n)
        if target.weekday() == 5:
            target += timedelta(days=-1) if n > 1 else timedelta(days=2)
        elif target.weekday() == 6:
            target += timedelta(days=1) if n < last else timedelta(days=-2)
        return day == target
    return day.day in f.values


def _dow_matches(day: date, f: _Field) -> bool:
    wd = aws_weekday(day)
    if f.nth is not None:
        want, n = f.nth
        return wd == want and (day.day - 1) // 7 + 1 == n
    return wd in f.values


def _parse_cron(body: str) -> tuple[_Field, ...]:
    parts = body.split()
    if len(parts) != 6:
        raise ExpressionError("cron needs 6 fields: minutes hours day-of-month month day-of-week year")
    m, h, dom, mon, dow, yr = parts
    if (dom == "?") == (dow == "?"):
        raise ExpressionError("cron needs '?' in exactly one of day-of-month and day-of-week")
    for name, text in (("minutes", m), ("hours", h), ("month", mon), ("year", yr)):
        if text == "?":
            raise ExpressionError(f"'?' is only allowed in the day fields, not {name}")
    return (
        _parse_field(m, 0, 59),
        _parse_field(h, 0, 23),
        _parse_field(dom, 1, 31, kind="dom"),
        _parse_field(mon, 1, 12, names=_MONTHS),
        _parse_field(dow, 1, 7, names=_DAYS, kind="dow"),
        _parse_field(yr, 1970, 2199),
    )


def parse(text: str) -> Expression:
    """Parse an ``at(...)``, ``cron(...)`` or ``rate(...)`` expression (raises ``ExpressionError``)."""
    text = text.strip()
    if m := _AT_RE.match(text):
        try:
            when = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
        except ValueError as e:
            raise ExpressionError(f"bad at() time {m.group(1)!r}") from e
        return Expression(text, ONCE, at=when)
    if m := _RATE_RE.match(text):
        value, unit = int(m.group(1)), m.group(2)
        if value < 1:
            raise ExpressionError("rate value must be positive")
        if (value == 1) == unit.endswith("s"):
            raise ExpressionError("rate unit must be singular for 1 and plural otherwise")
        return Expression(text, RECURRING, rate_seconds=value * _UNIT_SECONDS[unit.rstrip("s")])
    if m := _CRON_RE.match(text):
        return Expression(text, RECURRING, cron=_parse_cron(m.group(1)))
    raise ExpressionError(f"not an at(), cron() or rate() expression: {text!r}")


def at_expression(when: datetime) -> str:
    """``at(yyyy-mm-ddThh:mm:ss)`` for a local wall-clock time."""
    return f"at({when.strftime('%Y-%m-%dT%H:%M:%S')})"
