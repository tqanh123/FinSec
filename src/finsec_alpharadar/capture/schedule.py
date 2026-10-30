"""Capture schedule (plan 261022-1147 phase 2, R2.5, D5): trading days, per-source cadence, `next_due`, `gap_deadline`.

Every instant in and out is a tz-aware datetime; results are UTC. Local time is `Asia/Ho_Chi_Minh` (no DST), whose
`ZoneInfo` is created inside each call, so a missing tzdata package surfaces as `TimeZoneDataMissing` at first use, not
at import. Nothing here reads a clock: the caller passes every instant.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finsec_alpharadar.capture.config import LOCAL_TIMEZONE, CaptureConfig, Holiday, ScheduleConfig, SourceConfig

INSTALL_HINT = "install tzdata: .venv/Scripts/python -m pip install -e .[dev]"
MIDNIGHT = time(0, 0)


class TimeZoneDataMissing(RuntimeError):
    """The IANA zone database is not available (Windows needs the tzdata package)."""


def local_zone(name: str = LOCAL_TIMEZONE) -> tzinfo:
    """`ZoneInfo(name)`, or `TimeZoneDataMissing` with the install command when tzdata is absent."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as err:
        raise TimeZoneDataMissing(f"time zone {name} not found; {INSTALL_HINT}") from err


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a tz-aware datetime, got {value!r}")
    return value


class Schedule:
    """D5 cadence: `active_interval_s` of a source on trading days from `active_start` (inclusive) to `active_end`
    (exclusive) local time, `idle_interval_s` otherwise. A trading day is Monday-Friday and not a configured holiday.
    """

    def __init__(self, config: ScheduleConfig, holidays: Iterable[Holiday]) -> None:
        self.config = config
        self.holiday_dates = frozenset(holiday.day for holiday in holidays)

    @classmethod
    def from_config(cls, config: CaptureConfig) -> Schedule:
        return cls(config.schedule, config.holidays)

    def zone(self) -> tzinfo:
        return local_zone(self.config.timezone)

    def local(self, t: datetime) -> datetime:
        """`t` in local time."""
        return _require_aware(t, "t").astimezone(self.zone())

    def local_date(self, t: datetime) -> date:
        return self.local(t).date()

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holiday_dates

    def is_active(self, t: datetime) -> bool:
        """True on a trading day within [active_start, active_end) local time."""
        local = self.local(t)
        return self.is_trading_day(local.date()) and self.config.active_start <= local.time() < self.config.active_end

    def interval_at(self, t: datetime, source: SourceConfig) -> timedelta:
        seconds = source.active_interval_s if self.is_active(t) else source.idle_interval_s
        return timedelta(seconds=seconds)

    def next_boundary(self, t: datetime) -> datetime:
        """The first local 00:00, `active_start` or `active_end` strictly after `t`, in UTC."""
        local = self.local(t)
        zone = local.tzinfo
        marks = sorted({MIDNIGHT, self.config.active_start, self.config.active_end})
        for day in (local.date(), local.date() + timedelta(days=1)):
            for mark in marks:
                candidate = datetime.combine(day, mark, tzinfo=zone)
                if candidate > local:
                    return candidate.astimezone(timezone.utc)
        raise AssertionError("a local midnight always follows within one day")  # pragma: no cover

    def next_due(self, prev: datetime, source: SourceConfig) -> datetime:
        """`min(prev + interval_at(prev), next boundary after prev)`: at 08:00 local a poll is due at once."""
        stepped = _require_aware(prev, "prev").astimezone(timezone.utc) + self.interval_at(prev, source)
        return min(stepped, self.next_boundary(prev))

    def count_due(self, start: datetime, end: datetime, source: SourceConfig) -> int:
        """Number of scheduled due times in (start, end] when stepping `next_due` from `start` (expected polls)."""
        count, t = 0, start
        while True:
            t = self.next_due(t, source)
            if t > end:
                return count
            count += 1

    def gap_deadline(self, prev: datetime, source: SourceConfig) -> datetime:
        """Deadline of the next successful poll after one at `prev`: two schedule steps plus `max_poll_duration_s`.

        Stepping the schedule (not doubling the current interval) keeps the bound tight at 08:00: a last poll at 07:00
        gives 08:00 -> 08:03 -> 08:03 + max_poll_duration_s.
        """
        second = self.next_due(self.next_due(prev, source), source)
        return second + timedelta(seconds=self.config.max_poll_duration_s)
