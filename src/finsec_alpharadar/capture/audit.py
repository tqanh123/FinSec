"""Capture audit: the exit evidence E4 (plan 261022-1147 phase 2, R2.12; red-team r1 RT1, RT15).

`audit` reads a state SNAPSHOT from `backup` (`StateReader.open_snapshot`: read-only, `immutable=1`, a WAL-mode file is
refused) and judges each local day in [date_from, date_to].

Gaps. For every enabled source, consecutive ok polls (p, n) are paired on `finished_at` (the poller's `last_ok`
anchor), with two virtual edges: before the first poll, p = the last ok poll before `date_from` or local 00:00 of
`date_from`; after the last one, n = the snapshot's `alive.last_alive_at` capped at local 24:00 of `date_to`. So a
source that falls silent, or never polled, still yields a gap. When n > gap_deadline(p), the interval
[gap_deadline(p), n - max_poll_duration_s] must lie inside the union of that source's outages and the process-wide ones
(an open outage covers to the end); whatever is left is an unlogged gap, assigned to the local days it touches.

A day passes when it has no unlogged gap; on a trading day, every enabled portal source has at least one ok poll that
started within the active window (08:00-17:30 local; a logged outage is not capture, RT1); at least one heartbeat was
written that day; and the polls of the day carry a `Date` skew with median |skew| <= 2 s and no |skew| over 60 s (no
skew at all fails with `no_skew_evidence`). Polls of every `run_mode` count here (the phase 3 gate filters on it).
`days="trading"` judges trading days only, so a weekend or a holiday never breaks the streak of passing days.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from finsec_alpharadar.capture.config import CaptureConfig, SourceKind
from finsec_alpharadar.capture.record import format_utc
from finsec_alpharadar.capture.schedule import Schedule
from finsec_alpharadar.capture.storage import OutageRow, PollRow, StateReader

logger = logging.getLogger(__name__)

DAY_MODES = frozenset({"trading", "all"})
SKEW_MEDIAN_MAX_MS = 2000.0
SKEW_ABS_MAX_MS = 60000.0


@dataclass(frozen=True)
class Gap:
    """An unlogged gap of one source (UTC instants)."""

    source: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class DayReport:
    day: date
    trading_day: bool
    passed: bool
    reasons: tuple[str, ...]
    ok_polls: Mapping[str, int]
    expected_polls: Mapping[str, int]
    items_new: int
    outages_by_kind: Mapping[str, int]
    unlogged_gap_s: float
    heartbeats: int
    skew_ms_median_abs: float | None
    skew_ms_max_abs: float | None


@dataclass(frozen=True)
class AuditReport:
    date_from: date
    date_to: date
    days_mode: str
    days: tuple[DayReport, ...]
    gaps: tuple[Gap, ...]
    longest_consecutive_passing_trading_days: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready: dates as ISO strings, instants as UTC strings."""
        data = asdict(self)
        data["date_from"], data["date_to"] = self.date_from.isoformat(), self.date_to.isoformat()
        data["days"] = [{**asdict(day), "day": day.day.isoformat(), "reasons": list(day.reasons)} for day in self.days]
        data["gaps"] = [{"source": g.source, "start": format_utc(g.start), "end": format_utc(g.end)} for g in self.gaps]
        return data


def _uncovered(start: datetime, end: datetime, cover: list[tuple[datetime, datetime | None]]) -> list[tuple[datetime, datetime]]:
    """Parts of [start, end] outside the union of `cover` intervals (end None = open, covers to the future)."""
    pieces: list[tuple[datetime, datetime]] = []
    cursor = start
    for c_start, c_end in sorted(cover, key=lambda c: c[0]):
        if cursor >= end:
            break
        if c_end is not None and c_end <= cursor:
            continue
        if c_start > cursor:
            pieces.append((cursor, min(c_start, end)))
        cursor = end if c_end is None else max(cursor, c_end)
    if cursor < end:
        pieces.append((cursor, end))
    return [(a, b) for a, b in pieces if b > a]


class _Days:
    """Local-day arithmetic of the audit window."""

    def __init__(self, schedule: Schedule) -> None:
        self.schedule = schedule
        self.zone = schedule.zone()

    def midnight(self, day: date) -> datetime:
        """Local 00:00 of `day`, in UTC."""
        return datetime.combine(day, time(0, 0), tzinfo=self.zone).astimezone(timezone.utc)

    def split(self, start: datetime, end: datetime) -> list[tuple[date, float]]:
        """(local day, seconds) of [start, end] cut at local midnights."""
        parts: list[tuple[date, float]] = []
        day = self.schedule.local_date(start)
        cursor = start
        while cursor < end:
            stop = min(end, self.midnight(day + timedelta(days=1)))
            parts.append((day, (stop - cursor).total_seconds()))
            cursor, day = stop, day + timedelta(days=1)
        return parts


def _source_gaps(
    schedule: Schedule, config: CaptureConfig, name: str, polls: list[PollRow], outages: list[OutageRow],
    window_start: datetime, tail: datetime,
) -> list[Gap]:
    cfg = config.source(name)
    max_poll = timedelta(seconds=config.schedule.max_poll_duration_s)
    finished = sorted(p.finished_at for p in polls if p.status == "ok")
    before = [t for t in finished if t < window_start]
    anchors = [before[-1] if before else window_start] + [t for t in finished if window_start <= t <= tail]
    cover = [(o.start_at, o.end_at) for o in outages if o.source in (None, name)]
    gaps: list[Gap] = []
    for p, n in zip(anchors, anchors[1:] + [tail]):
        deadline = schedule.gap_deadline(p, cfg)
        if n <= deadline:
            continue
        start, end = max(deadline, window_start), n - max_poll
        if end > start:
            gaps.extend(Gap(name, a, b) for a, b in _uncovered(start, end, cover))
    return gaps


def _median_abs(values: list[float]) -> float | None:
    return statistics.median(abs(v) for v in values) if values else None


def audit(state_path: Path, config: CaptureConfig, date_from: date, date_to: date, days: str = "trading") -> AuditReport:
    """Judge each local day of [date_from, date_to] from a backup snapshot (see the module docstring)."""
    if days not in DAY_MODES:
        raise ValueError(f"days must be one of {sorted(DAY_MODES)}, got {days!r}")
    if date_to < date_from:
        raise ValueError("date_to must not be before date_from")
    schedule = Schedule.from_config(config)
    local = _Days(schedule)
    window_start, window_end = local.midnight(date_from), local.midnight(date_to + timedelta(days=1))
    with StateReader.open_snapshot(state_path) as state:
        alive = state.get_alive()
        tail = min(alive.last_alive_at, window_end) if alive is not None else window_start
        polls = state.polls()
        outages = state.all_outages()
        heartbeats = state.heartbeat_times(window_start, window_end)
    sources = config.enabled_sources
    by_source = {cfg.name: [p for p in polls if p.source == cfg.name] for cfg in sources}
    gaps = [
        gap
        for cfg in sources
        for gap in _source_gaps(schedule, config, cfg.name, by_source[cfg.name], outages, window_start, tail)
    ]

    gap_seconds: dict[tuple[date, str], float] = {}
    for gap in gaps:
        for day, seconds in local.split(gap.start, gap.end):
            gap_seconds[(day, gap.source)] = gap_seconds.get((day, gap.source), 0.0) + seconds

    reports: list[DayReport] = []
    day = date_from
    while day <= date_to:
        trading = schedule.is_trading_day(day)
        if days == "all" or trading:
            reports.append(_judge_day(day, trading, config, schedule, local, by_source, outages, heartbeats, gap_seconds))
        day += timedelta(days=1)
    streak = best = 0
    for report in reports:
        streak = streak + 1 if report.passed else 0
        best = max(best, streak)
    return AuditReport(date_from, date_to, days, tuple(reports), tuple(gaps), best)


def _judge_day(
    day: date,
    trading: bool,
    config: CaptureConfig,
    schedule: Schedule,
    local: _Days,
    by_source: Mapping[str, list[PollRow]],
    outages: list[OutageRow],
    heartbeats: list[datetime],
    gap_seconds: Mapping[tuple[date, str], float],
) -> DayReport:
    start, end = local.midnight(day), local.midnight(day + timedelta(days=1))
    reasons: list[str] = []
    ok_polls: dict[str, int] = {}
    expected: dict[str, int] = {}
    items_new = 0
    skews: list[float] = []
    for cfg in config.enabled_sources:
        day_polls = [p for p in by_source[cfg.name] if start <= p.started_at < end]
        ok = [p for p in day_polls if p.status == "ok"]
        ok_polls[cfg.name] = len(ok)
        expected[cfg.name] = schedule.count_due(start, end, cfg)
        items_new += sum(p.items_new for p in day_polls)
        skews.extend(p.skew_ms for p in day_polls if p.skew_ms is not None)
        if gap_seconds.get((day, cfg.name), 0.0) > 0:
            reasons.append(f"unlogged_gap:{cfg.name}")
        if trading and cfg.kind is not SourceKind.VNSTOCK_EVENTS and not any(schedule.is_active(p.started_at) for p in ok):
            reasons.append(f"no_active_ok_poll:{cfg.name}")
    heartbeat_count = sum(1 for t in heartbeats if start <= t < end)
    if not heartbeat_count:
        reasons.append("no_heartbeat")
    median_abs = _median_abs(skews)
    max_abs = max((abs(s) for s in skews), default=None)
    if median_abs is None:
        reasons.append("no_skew_evidence")
    else:
        if median_abs > SKEW_MEDIAN_MAX_MS:
            reasons.append("skew_median")
        if max_abs is not None and max_abs > SKEW_ABS_MAX_MS:
            reasons.append("skew_max")
    by_kind: dict[str, int] = {}
    for outage in outages:
        if outage.start_at < end and (outage.end_at is None or outage.end_at > start):
            by_kind[outage.kind.value] = by_kind.get(outage.kind.value, 0) + 1
    unlogged = sum(seconds for (d, _), seconds in gap_seconds.items() if d == day)
    return DayReport(
        day=day, trading_day=trading, passed=not reasons, reasons=tuple(reasons), ok_polls=ok_polls,
        expected_polls=expected, items_new=items_new, outages_by_kind=by_kind, unlogged_gap_s=unlogged,
        heartbeats=heartbeat_count, skew_ms_median_abs=median_abs, skew_ms_max_abs=max_abs,
    )
