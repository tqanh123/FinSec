"""Capture schedule (`finsec_alpharadar.capture.schedule`, plan 261022-1147 phase 2, R2.5, D5) and the typed config it reads.

Fixed aware instants only (ICT = UTC+7, no DST); no clock, no network. 2026-10-27 is a Tuesday, 2026-10-31 a Saturday.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from finsec_alpharadar.capture import schedule as schedule_module
from finsec_alpharadar.capture.config import (
    CaptureConfig,
    CaptureConfigError,
    HeartbeatConfig,
    Holiday,
    ScheduleConfig,
    SourceConfig,
    SourceKind,
)
from finsec_alpharadar.capture.record import OutageKind
from finsec_alpharadar.capture.schedule import Schedule, TimeZoneDataMissing

ICT = ZoneInfo("Asia/Ho_Chi_Minh")
HOLIDAY = Holiday(day=date(2026, 11, 24), status="announced", source_url="https://vsdc.vn/en/ad/198399", notice_id=None)


def _ict(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=ICT).astimezone(timezone.utc)


def _schedule_config(**changes: Any) -> ScheduleConfig:
    fields: dict[str, Any] = {
        "active_start": time(8, 0),
        "active_end": time(17, 30),
        "loop_tick_s": 30.0,
        "max_poll_duration_s": 330.0,
        "clock_step_threshold_s": 2.0,
        "heartbeat_every_s": 900.0,
        "watchdog_limit_s": 900.0,
        "backup_at_local": time(7, 15),
    }
    fields.update(changes)
    return ScheduleConfig(**fields)


def _source(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "hose_news_api",
        "kind": SourceKind.HOSE_NEWS_API,
        "enabled": True,
        "url": "https://portal.example.invalid/news/securitiesType/0",
        "active_interval_s": 180.0,
        "idle_interval_s": 3600.0,
        "timeout_s": 30.0,
        "deadline_s": 60.0,
        "retries": 1,
        "max_body_bytes": 2_000_000,
        "page_size": 20,
        "max_pages": 2,
        "min_request_spacing_s": 1.0,
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _schedule(**changes: Any) -> Schedule:
    return Schedule(_schedule_config(**changes), (HOLIDAY,))


def test_active_window_is_0800_inclusive_to_1730_exclusive_ict_on_trading_days() -> None:
    sched, source = _schedule(), _source()
    active, idle = timedelta(seconds=180), timedelta(seconds=3600)
    assert sched.interval_at(_ict(2026, 10, 27, 7, 59, 59), source) == idle
    assert sched.interval_at(_ict(2026, 10, 27, 8, 0), source) == active
    assert sched.interval_at(_ict(2026, 10, 27, 12, 0), source) == active
    assert sched.interval_at(_ict(2026, 10, 27, 17, 29, 59), source) == active
    assert sched.interval_at(_ict(2026, 10, 27, 17, 30), source) == idle
    # 00:30 UTC on the 27th is 07:30 ICT; 10:00 UTC on the 26th is 17:00 ICT (still active).
    assert sched.interval_at(datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc), source) == active
    assert sched.interval_at(datetime(2026, 10, 27, 0, 30, tzinfo=timezone.utc), source) == idle
    other = _source(name="hnx_rss_issuers", kind=SourceKind.RSS_FEED, active_interval_s=300.0, page_size=None, max_pages=1)
    assert sched.interval_at(_ict(2026, 10, 27, 9, 0), other) == timedelta(seconds=300)
    with pytest.raises(ValueError):
        sched.interval_at(datetime(2026, 10, 27, 9, 0), source)  # naive


def test_weekend_and_listed_holiday_use_the_idle_interval() -> None:
    sched, source = _schedule(), _source()
    idle = timedelta(seconds=3600)
    assert sched.is_trading_day(date(2026, 10, 27))
    assert not sched.is_trading_day(date(2026, 10, 31)) and not sched.is_trading_day(date(2026, 11, 1))
    assert not sched.is_trading_day(date(2026, 11, 24))
    assert sched.interval_at(_ict(2026, 10, 31, 10, 0), source) == idle
    assert sched.interval_at(_ict(2026, 11, 1, 10, 0), source) == idle
    assert sched.interval_at(_ict(2026, 11, 24, 10, 0), source) == idle
    assert sched.interval_at(_ict(2026, 11, 25, 10, 0), source) == timedelta(seconds=180)
    unlisted = Schedule(_schedule_config(), ())
    assert unlisted.is_trading_day(date(2026, 11, 24)), "a holiday comes only from the config list"


def test_next_due_stops_at_the_0800_boundary() -> None:
    sched, source = _schedule(), _source()
    assert sched.next_due(_ict(2026, 10, 27, 7, 30), source) == _ict(2026, 10, 27, 8, 0)
    assert sched.next_due(_ict(2026, 10, 27, 7, 59, 59), source) == _ict(2026, 10, 27, 8, 0)
    assert sched.next_due(_ict(2026, 10, 27, 8, 0), source) == _ict(2026, 10, 27, 8, 3)
    assert sched.next_due(_ict(2026, 10, 27, 17, 28), source) == _ict(2026, 10, 27, 17, 30)
    assert sched.next_due(_ict(2026, 10, 27, 17, 30), source) == _ict(2026, 10, 27, 18, 30)
    # The boundaries exist every day; on a Saturday 08:00 only re-aligns the hourly cadence.
    assert sched.next_due(_ict(2026, 10, 31, 7, 30), source) == _ict(2026, 10, 31, 8, 0)
    assert sched.next_due(_ict(2026, 10, 31, 8, 0), source) == _ict(2026, 10, 31, 9, 0)


def test_next_due_crosses_midnight() -> None:
    sched, source = _schedule(), _source()
    assert sched.next_due(_ict(2026, 10, 26, 23, 30), source) == _ict(2026, 10, 27, 0, 0)
    assert sched.next_due(_ict(2026, 10, 30, 23, 59, 59), source) == _ict(2026, 10, 31, 0, 0)
    assert sched.next_due(_ict(2026, 10, 27, 0, 0), source) == _ict(2026, 10, 27, 1, 0)
    # The local midnight is 17:00 UTC: a UTC date change is not a boundary.
    assert sched.next_due(datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc), source) == datetime(
        2026, 10, 28, 0, 30, tzinfo=timezone.utc
    )
    assert sched.next_due(datetime(2026, 10, 27, 16, 30, tzinfo=timezone.utc), source) == datetime(
        2026, 10, 27, 17, 0, tzinfo=timezone.utc
    )


def test_gap_deadline_steps_the_schedule_twice() -> None:
    sched, source = _schedule(), _source()
    assert sched.gap_deadline(_ict(2026, 10, 27, 10, 0), source) == _ict(2026, 10, 27, 10, 6) + timedelta(seconds=330)
    assert sched.gap_deadline(_ict(2026, 10, 31, 12, 0), source) == _ict(2026, 10, 31, 14, 0) + timedelta(seconds=330)
    # 17:27 -> 17:30 -> 18:30: the second step already uses the idle interval.
    assert sched.gap_deadline(_ict(2026, 10, 27, 17, 27), source) == _ict(2026, 10, 27, 18, 30) + timedelta(seconds=330)
    assert sched.gap_deadline(_ict(2026, 11, 24, 7, 0), source) == _ict(2026, 11, 24, 9, 0) + timedelta(seconds=330)


def test_gap_deadline_after_a_0700_poll_ends_at_0805_not_0902() -> None:
    # The phase example (07:00 -> 08:00 -> 08:03 -> 08:05) uses a two-minute maximum poll duration.
    sched, source = _schedule(max_poll_duration_s=120.0), _source()
    deadline = sched.gap_deadline(_ict(2026, 10, 27, 7, 0), source)
    assert deadline == _ict(2026, 10, 27, 8, 5)
    assert deadline != _ict(2026, 10, 27, 9, 2)


def test_missing_tzdata_raises_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_zone(key: str) -> ZoneInfo:
        raise ZoneInfoNotFoundError(f"No time zone found with key {key}")

    sched, source = _schedule(), _source()
    monkeypatch.setattr(schedule_module, "ZoneInfo", no_zone)
    with pytest.raises(TimeZoneDataMissing, match=r"install tzdata: \.venv/Scripts/python -m pip install -e \.\[dev\]"):
        sched.interval_at(_ict(2026, 10, 27, 9, 0), source)
    with pytest.raises(TimeZoneDataMissing):
        schedule_module.local_zone()


def test_source_config_enforces_d5_and_its_own_shape() -> None:
    for active in (119.0, 301.0):
        with pytest.raises(CaptureConfigError, match="active_interval_s"):
            _source(active_interval_s=active)
    with pytest.raises(CaptureConfigError, match="idle_interval_s"):
        _source(idle_interval_s=1800.0)
    with pytest.raises(CaptureConfigError, match="kind"):
        _source(kind="ftp_dump")
    for name in ("", "Hose", "../x", "a b"):
        with pytest.raises(CaptureConfigError, match="name"):
            _source(name=name)
    for url in ("http://portal.example.invalid/x", "file:///etc/passwd", None):
        with pytest.raises(CaptureConfigError, match="url"):
            _source(url=url)
    with pytest.raises(CaptureConfigError, match="deadline_s"):
        _source(timeout_s=90.0, deadline_s=60.0)
    with pytest.raises(CaptureConfigError, match="page_size"):
        _source(page_size=None)
    assert _source(kind="hose_news_api").kind is SourceKind.HOSE_NEWS_API


def test_vnstock_source_config_refuses_kbs_and_maps_codes_to_outage_kinds() -> None:
    base: dict[str, Any] = {
        "name": "vnstock_events",
        "kind": SourceKind.VNSTOCK_EVENTS,
        "url": None,
        "page_size": None,
        "max_pages": 1,
        "vnstock_source": "vci",
        "symbols": ("AAA", "BBB"),
        "symbols_per_poll": 2,
        "calls_per_min": 60.0,
        "vendor_call_timeout_s": 60.0,
        "release_expires_on": date(2027, 10, 17),
        "error_map": {"VENDOR_GONE": "vendor_unavailable"},
        "quota_warning_patterns": ("truncat",),
    }
    source = _source(**base)
    assert source.error_map == {"VENDOR_GONE": OutageKind.VENDOR_UNAVAILABLE}
    with pytest.raises(CaptureConfigError, match="kbs"):
        _source(**{**base, "vnstock_source": "kbs"})
    with pytest.raises(CaptureConfigError, match="error_map"):
        _source(**{**base, "error_map": {"VENDOR_GONE": "source_removed"}})
    with pytest.raises(CaptureConfigError, match="symbols"):
        _source(**{**base, "symbols": ()})
    with pytest.raises(CaptureConfigError, match="quota_warning_patterns"):
        _source(**{**base, "quota_warning_patterns": ()})
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.enabled = False  # type: ignore[misc]


def test_capture_config_holds_unique_sources_and_holidays() -> None:
    schedule_cfg = _schedule_config()
    heartbeat = HeartbeatConfig(canary_files=("AGENTS.md", "CLAUDE.md"), stripchart_host=None)
    config = CaptureConfig(schedule=schedule_cfg, holidays=(HOLIDAY,), sources=(_source(),), heartbeat=heartbeat)
    assert config.source("hose_news_api").name == "hose_news_api"
    assert Schedule.from_config(config).is_trading_day(date(2026, 11, 24)) is False
    with pytest.raises(CaptureConfigError, match="duplicate"):
        CaptureConfig(schedule=schedule_cfg, holidays=(), sources=(_source(), _source()), heartbeat=heartbeat)
    with pytest.raises(CaptureConfigError, match="duplicate"):
        CaptureConfig(schedule=schedule_cfg, holidays=(HOLIDAY, HOLIDAY), sources=(), heartbeat=heartbeat)
    with pytest.raises(CaptureConfigError, match="active_end"):
        _schedule_config(active_start=time(17, 30), active_end=time(8, 0))
    assert schedule_cfg.alive_threshold_s == 2 * 30.0 + 330.0
