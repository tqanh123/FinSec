"""Typed, validated capture configuration (plan 261022-1147 phase 2, R2.15, D5).

The frozen dataclasses below are the contract every capture module reads. Construction validates them and raises
`CaptureConfigError`, so a config that breaks D5 (120-300 s active cadence, hourly idle cadence) cannot exist. Every
measured number (timeouts, page sizes, calls per minute, the vnstock error map) comes from `configs/capture.toml`, and
none is hard-coded here. `load_config_file` reads the TOML (`tomllib`, no environment); `resolve_dirs` adds
`FINSEC_DATA_ROOT` and `FINSEC_BACKUP_DIR`, both through `data_root.resolve_external_dir` (red-team r1 LT7).
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from datetime import date, time
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from finsec_alpharadar.capture.record import OutageKind
from finsec_alpharadar.data_root import (
    DATA_ROOT_ENV,
    CheckIgnore,
    WorkTreeFinder,
    find_work_tree,
    git_check_ignore,
    resolve_external_dir,
)

LOCAL_TIMEZONE = "Asia/Ho_Chi_Minh"
D5_ACTIVE_MIN_S = 120.0
D5_ACTIVE_MAX_S = 300.0
D5_IDLE_S = 3600.0
# `vnstock_source = "kbs"` fails for events() on every symbol (VNSTOCK_KBS_COMPANY_SHAPE, OBSERVED 2026-10-26).
REFUSED_VNSTOCK_SOURCES = frozenset({"kbs"})
HOLIDAY_STATUSES = frozenset({"announced", "provisional"})

# A source name becomes a directory under raw/: lower-case letters, digits and underscores only.
_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class CaptureConfigError(ValueError):
    """The capture configuration is malformed or breaks a locked decision (D5)."""


class SourceKind(str, Enum):
    HOSE_NEWS_API = "hose_news_api"
    RSS_FEED = "rss_feed"
    VNSTOCK_EVENTS = "vnstock_events"


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise CaptureConfigError(f"{name} must be a positive number, got {value!r}")
    return float(value)


def _count(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CaptureConfigError(f"{name} must be an int >= {minimum}, got {value!r}")
    return value


def _patterns(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item for item in value):
        raise CaptureConfigError(f"{name} must be a list of non-empty strings, got {value!r}")
    return tuple(value)


@dataclass(frozen=True)
class Holiday:
    """A non-trading weekday, read from an exchange or VSDC notice (never inferred from the Labour Code)."""

    day: date
    status: str  # "announced" | "provisional"
    source_url: str | None = None
    notice_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise CaptureConfigError(f"holiday date must be a date, got {self.day!r}")
        if self.status not in HOLIDAY_STATUSES:
            raise CaptureConfigError(f"holiday status must be one of {sorted(HOLIDAY_STATUSES)}, got {self.status!r}")


@dataclass(frozen=True)
class ScheduleConfig:
    """`[schedule]`: the D5 active window (local time) and the thresholds of R2.7 and R2.9, in seconds."""

    active_start: time
    active_end: time
    loop_tick_s: float
    max_poll_duration_s: float
    clock_step_threshold_s: float
    heartbeat_every_s: float
    watchdog_limit_s: float
    backup_at_local: time
    timezone: str = LOCAL_TIMEZONE

    def __post_init__(self) -> None:
        for name in ("active_start", "active_end", "backup_at_local"):
            if not isinstance(getattr(self, name), time) or getattr(self, name).tzinfo is not None:
                raise CaptureConfigError(f"{name} must be a naive local time, got {getattr(self, name)!r}")
        if not self.active_start < self.active_end:
            raise CaptureConfigError("active_end must be later than active_start")
        for name in ("loop_tick_s", "max_poll_duration_s", "clock_step_threshold_s", "heartbeat_every_s", "watchdog_limit_s"):
            object.__setattr__(self, name, _positive(getattr(self, name), name))

    @property
    def alive_threshold_s(self) -> float:
        """`2 x loop_tick_s + max_poll_duration_s`: a longer silence is `process_down` / `host_stalled` (R2.7)."""
        return 2 * self.loop_tick_s + self.max_poll_duration_s


@dataclass(frozen=True)
class SourceConfig:
    """One `[[sources]]` entry. Portal kinds need an https `url`; `hose_news_api` also pages (`page_size`, `max_pages`).

    vnstock fields: `vnstock_source` (never "kbs"), `symbols` (round-robin), `symbols_per_poll`, `calls_per_min`,
    `vendor_call_timeout_s`, `release_expires_on`, `error_map` (vendor error `code` -> `OutageKind`),
    `symbol_error_codes` (a per-symbol error, logged in `polls.detail`, not an outage), and the warning classifiers
    `quota_warning_patterns` / `ignore_warning_patterns` (case-insensitive substrings of the warning message).
    """

    name: str
    kind: SourceKind
    enabled: bool
    url: str | None
    active_interval_s: float
    idle_interval_s: float
    timeout_s: float
    deadline_s: float
    retries: int
    max_body_bytes: int
    page_size: int | None = None
    max_pages: int = 1
    min_request_spacing_s: float = 0.0
    headers: Mapping[str, str] = field(default_factory=dict)
    vnstock_source: str | None = None
    symbols: tuple[str, ...] = ()
    symbols_per_poll: int = 0
    calls_per_min: float = 0.0
    vendor_call_timeout_s: float = 0.0
    release_expires_on: date | None = None
    error_map: Mapping[str, OutageKind] = field(default_factory=dict)
    symbol_error_codes: tuple[str, ...] = ()
    quota_warning_patterns: tuple[str, ...] = ()
    ignore_warning_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.name, str) or not _SOURCE_NAME.match(self.name):
            raise CaptureConfigError(f"source name must match {_SOURCE_NAME.pattern}, got {self.name!r}")
        try:
            set_(self, "kind", SourceKind(self.kind))
        except ValueError as err:
            raise CaptureConfigError(f"source {self.name}: unknown kind {self.kind!r}") from err
        if not isinstance(self.enabled, bool):
            raise CaptureConfigError(f"source {self.name}: enabled must be a bool")
        active = _positive(self.active_interval_s, "active_interval_s")
        if not D5_ACTIVE_MIN_S <= active <= D5_ACTIVE_MAX_S:
            raise CaptureConfigError(f"source {self.name}: active_interval_s must be within 120-300 s (D5), got {active}")
        if _positive(self.idle_interval_s, "idle_interval_s") != D5_IDLE_S:
            raise CaptureConfigError(f"source {self.name}: idle_interval_s must be 3600 s (D5), got {self.idle_interval_s}")
        set_(self, "active_interval_s", active)
        set_(self, "idle_interval_s", D5_IDLE_S)
        set_(self, "timeout_s", _positive(self.timeout_s, "timeout_s"))
        set_(self, "deadline_s", _positive(self.deadline_s, "deadline_s"))
        if self.deadline_s < self.timeout_s:
            raise CaptureConfigError(f"source {self.name}: deadline_s must be >= timeout_s")
        _count(self.retries, "retries", 0)
        _count(self.max_body_bytes, "max_body_bytes", 1)
        _count(self.max_pages, "max_pages", 1)
        if isinstance(self.min_request_spacing_s, bool) or not isinstance(self.min_request_spacing_s, (int, float)) or (
            self.min_request_spacing_s < 0
        ):
            raise CaptureConfigError(f"source {self.name}: min_request_spacing_s must be >= 0")
        if not isinstance(self.headers, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.headers.items()
        ):
            raise CaptureConfigError(f"source {self.name}: headers must map strings to strings")
        set_(self, "headers", dict(self.headers))
        if self.kind is SourceKind.VNSTOCK_EVENTS:
            self._check_vnstock()
        else:
            self._check_portal()

    def _check_portal(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith("https://"):
            raise CaptureConfigError(f"source {self.name}: url must be an https:// URL, got {self.url!r}")
        if self.kind is SourceKind.HOSE_NEWS_API:
            _count(self.page_size, "page_size", 1)

    def _check_vnstock(self) -> None:
        if not isinstance(self.vnstock_source, str) or not self.vnstock_source:
            raise CaptureConfigError(f"source {self.name}: vnstock_source is required")
        if self.vnstock_source in REFUSED_VNSTOCK_SOURCES:
            raise CaptureConfigError(f"source {self.name}: vnstock_source {self.vnstock_source!r} is refused (kbs events broken)")
        symbols = tuple(self.symbols)
        if not symbols or not all(isinstance(s, str) and s.strip() for s in symbols) or len(set(symbols)) != len(symbols):
            raise CaptureConfigError(f"source {self.name}: symbols must be a non-empty list of unique tickers")
        object.__setattr__(self, "symbols", symbols)
        _count(self.symbols_per_poll, "symbols_per_poll", 1)
        object.__setattr__(self, "calls_per_min", _positive(self.calls_per_min, "calls_per_min"))
        object.__setattr__(self, "vendor_call_timeout_s", _positive(self.vendor_call_timeout_s, "vendor_call_timeout_s"))
        if self.release_expires_on is not None and not isinstance(self.release_expires_on, date):
            raise CaptureConfigError(f"source {self.name}: release_expires_on must be a date")
        mapped: dict[str, OutageKind] = {}
        for code, kind in dict(self.error_map).items():
            try:
                mapped[str(code)] = OutageKind(kind)
            except ValueError as err:
                raise CaptureConfigError(f"source {self.name}: error_map value {kind!r} is not an OutageKind") from err
        object.__setattr__(self, "error_map", mapped)
        for name in ("symbol_error_codes", "quota_warning_patterns", "ignore_warning_patterns"):
            object.__setattr__(self, name, _patterns(getattr(self, name), name))
        if not self.quota_warning_patterns:  # without patterns the quota rule (red-team r1 RT7) could never fire
            raise CaptureConfigError(f"source {self.name}: quota_warning_patterns must not be empty")


@dataclass(frozen=True)
class HeartbeatConfig:
    """`[heartbeat]`: canary files of the clone (sha256 compared with a baseline) and the w32tm stripchart host."""

    canary_files: tuple[str, ...]
    stripchart_host: str | None = None


@dataclass(frozen=True)
class CaptureConfig:
    """The whole validated capture configuration. `data_root` / `backup_dir` are set by the loader from the environment."""

    schedule: ScheduleConfig
    holidays: tuple[Holiday, ...]
    sources: tuple[SourceConfig, ...]
    heartbeat: HeartbeatConfig
    data_root: Path | None = None
    backup_dir: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "holidays", tuple(self.holidays))
        object.__setattr__(self, "sources", tuple(self.sources))
        names = [source.name for source in self.sources]
        if len(set(names)) != len(names):
            raise CaptureConfigError(f"duplicate source names in {names}")
        days = [holiday.day for holiday in self.holidays]
        if len(set(days)) != len(days):
            raise CaptureConfigError("duplicate holiday dates")

    def source(self, name: str) -> SourceConfig:
        for source in self.sources:
            if source.name == name:
                return source
        raise KeyError(name)

    @property
    def enabled_sources(self) -> tuple[SourceConfig, ...]:
        return tuple(source for source in self.sources if source.enabled)


# ---------------------------------------------------------------- loader (R2.15)

BACKUP_DIR_ENV = "FINSEC_BACKUP_DIR"
_TOP_LEVEL_KEYS = frozenset({"schedule", "holidays", "sources", "heartbeat"})


def _table(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CaptureConfigError(f"{name} must be a TOML table")
    return value


def _build(cls: type[Any], data: dict[str, Any], name: str) -> Any:
    """`cls(**data)` with unknown keys refused by name (a typo must not silently fall back to a default)."""
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise CaptureConfigError(f"{name}: unknown key(s) {unknown}")
    try:
        return cls(**data)
    except TypeError as err:  # a missing required key
        raise CaptureConfigError(f"{name}: {err}") from err


def parse_config(data: Mapping[str, Any]) -> CaptureConfig:
    """A validated `CaptureConfig` from parsed TOML. Times and dates must be TOML literals (`08:00:00`, `2026-11-24`)."""
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise CaptureConfigError(f"unknown top-level key(s) {unknown}")
    schedule = _build(ScheduleConfig, dict(_table(data.get("schedule"), "[schedule]")), "[schedule]")
    holidays_raw = data.get("holidays", [])
    sources_raw = data.get("sources", [])
    if not isinstance(holidays_raw, list) or not isinstance(sources_raw, list):
        raise CaptureConfigError("[[holidays]] and [[sources]] must be arrays of tables")
    holidays = []
    for index, item in enumerate(holidays_raw):
        entry = dict(_table(item, f"[[holidays]] #{index + 1}"))
        if "date" in entry:
            entry["day"] = entry.pop("date")
        holidays.append(_build(Holiday, entry, f"[[holidays]] #{index + 1}"))
    sources = []
    for index, item in enumerate(sources_raw):
        entry = dict(_table(item, f"[[sources]] #{index + 1}"))
        label = f"[[sources]] {entry.get('name', f'#{index + 1}')}"
        entry.setdefault("url", None)  # TOML has no null: a vnstock source simply has no url
        for key in ("symbols", "symbol_error_codes", "quota_warning_patterns", "ignore_warning_patterns"):
            if isinstance(entry.get(key), list):
                entry[key] = tuple(entry[key])
        sources.append(_build(SourceConfig, entry, label))
    heartbeat = dict(_table(data.get("heartbeat"), "[heartbeat]"))
    if isinstance(heartbeat.get("canary_files"), list):
        heartbeat["canary_files"] = tuple(heartbeat["canary_files"])
    return CaptureConfig(
        schedule=schedule, holidays=tuple(holidays), sources=tuple(sources),
        heartbeat=_build(HeartbeatConfig, heartbeat, "[heartbeat]"),
    )


def load_config_file(path: Path) -> CaptureConfig:
    """Read and validate `configs/capture.toml` (no environment is read); any problem is a `CaptureConfigError`."""
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as err:
        raise CaptureConfigError(f"cannot read the config {path}: {type(err).__name__}") from err
    except tomllib.TOMLDecodeError as err:
        raise CaptureConfigError(f"{Path(path).name} is not valid TOML: {err}") from err
    return parse_config(data)


def resolve_dirs(
    config: CaptureConfig,
    env: Mapping[str, str],
    *,
    need_backup_dir: bool,
    work_tree_of: WorkTreeFinder = find_work_tree,
    check_ignore: CheckIgnore = git_check_ignore,
) -> CaptureConfig:
    """`config` with `data_root` (FINSEC_DATA_ROOT) and `backup_dir` (FINSEC_BACKUP_DIR), both through the work-tree
    guard (red-team r1 LT7). The backup dir is required when `need_backup_dir`, else resolved only when set. Raises
    `DataRootError` / `DataRootInsideWorkTreeError`."""
    data_root = resolve_external_dir(env, DATA_ROOT_ENV, work_tree_of=work_tree_of, check_ignore=check_ignore)
    backup_dir = None
    if need_backup_dir or env.get(BACKUP_DIR_ENV, "").strip():
        backup_dir = resolve_external_dir(env, BACKUP_DIR_ENV, work_tree_of=work_tree_of, check_ignore=check_ignore)
    return replace(config, data_root=data_root, backup_dir=backup_dir)
