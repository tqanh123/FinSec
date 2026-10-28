"""Capture heartbeat (plan 261022-1147 phase 2, R2.10, D3; red-team r1 RT11, LT2, LT4).

`collect_probes` runs the host probes (w32tm stripchart hourly, w32tm status, the Windows Update pause registry value,
disk space, the data root size, the canary hashes) through injected callables; a failing probe becomes `null` plus its
error class or exit code, never a crash and never the error text. `build_heartbeat` is pure over state queries and those
probe results and returns a dict whose keys are the fixed whitelist `HEARTBEAT_KEYS`: counts, times, hashes and status
codes only, never a title, a payload, a secret, a path or a user name. `write_heartbeat` writes `heartbeat.json` and
`heartbeats/heartbeat-<UTC day>.json` atomically into the cloud-synced folder, retrying a locked replace; a final failure
is swallowed and shows as DEGRADED `heartbeat_write_failed` in the next heartbeat.

Status: DOWN when an enabled portal source is past the gap deadline of its last ok poll; DEGRADED on an open outage, a
median |skew| over 2 s, a changed canary, vnstock expiring within 30 days, under 10 GB free, a backup older than 36 h (or
none yet), a failed heartbeat or backup write, or a backup set that had to be rewritten as `-retry-<n>`; OK otherwise.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from finsec_alpharadar.capture.config import CaptureConfig, SourceKind
from finsec_alpharadar.capture.poller import component_status_kv_key
from finsec_alpharadar.capture.record import detail_json, error_detail, format_utc, parse_utc, sha256_hex
from finsec_alpharadar.capture.sources import Sleeper
from finsec_alpharadar.capture.schedule import Schedule
from finsec_alpharadar.capture.storage import CaptureState, Replacer, StateReader, Syncer, fsync_file, write_atomic

logger = logging.getLogger(__name__)

HEARTBEAT_SCHEMA = "capture-heartbeat/1"
STATUS_OK, STATUS_DEGRADED, STATUS_DOWN = "OK", "DEGRADED", "DOWN"
WINDOW = timedelta(hours=24)
SKEW_DEGRADED_MS = 2000.0
VNSTOCK_DAYS_LEFT_DEGRADED = 30
DISK_MIN_FREE_BYTES = 10_000_000_000  # 10 GB
BACKUP_MAX_AGE = timedelta(hours=36)
STRIPCHART_EVERY = timedelta(hours=1)
PROBE_TIMEOUT_S = 30.0
CANARY_BASELINE_KV_KEY = "canary_baseline"
STRIPCHART_KV_KEY = "w32tm_stripchart"
HEARTBEAT_FILE = "heartbeat.json"
DAILY_DIR = "heartbeats"
# `w32tm /query /status /verbose` fields kept (research capture-host-windows.md §3.2; English UI labels).
W32TM_FIELDS: Mapping[str, str] = {
    "Leap Indicator": "leap_indicator",
    "Stratum": "stratum",
    "Source": "source",
    "Last Successful Sync Time": "last_successful_sync_time",
    "Poll Interval": "poll_interval",
    "Phase Offset": "phase_offset",
    "State Machine": "state_machine",
    "Last Sync Error": "last_sync_error",
    "Time since Last Good Sync Time": "time_since_last_good_sync_time",
}
# [ASSUMED] key name (research capture-host-windows.md §2); reported raw, no status rule (red-team r1 LT2).
WU_PAUSE_KEY = r"HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
WU_PAUSE_VALUE = "PauseUpdatesExpiryTime"
_STRIPCHART_SAMPLE = re.compile(r",\s*([+-]\d+(?:\.\d+)?)s\s*$")

_SOURCE_KEYS = (
    "kind", "last_ok_at", "age_s", "polls_24h_ok", "polls_24h_error", "expected_polls_24h", "new_items_24h",
    "revised_items_24h", "open_outages",
)
# Every key path of a heartbeat; `*` stands for a data-keyed map entry (a source name, a canary file, an outage kind).
HEARTBEAT_KEYS: frozenset[str] = frozenset(
    {"schema", "generated_at", "status", "reasons", "sources", "sources.*", "outages_24h", "outages_24h.*"}
    | {f"sources.*.{key}" for key in _SOURCE_KEYS}
    | {"clock", "clock.skew_ms_median_24h", "clock.skew_ms_median_abs_24h", "clock.skew_ms_max_abs_24h",
       "clock.polls_with_skew_24h", "clock.stripchart", "clock.w32tm_status"}
    | {f"clock.stripchart.{key}" for key in ("at", "offsets_s", "median_offset_s", "error")}
    | {f"clock.w32tm_status.{key}" for key in (*W32TM_FIELDS.values(), "error")}
    | {"disk", "disk.free_bytes", "disk.free_error", "disk.data_root_bytes", "disk.data_root_error"}
    | {"backup", "backup.last_day", "backup.verified_at", "backup.age_s"}
    | {"vnstock", "vnstock.enabled", "vnstock.version", "vnstock.release_expires_on", "vnstock.days_left"}
    | {"canary", "canary.baseline_at", "canary.files", "canary.files.*", "canary.files.*.sha256",
       "canary.files.*.baseline_sha256", "canary.files.*.changed"}
    | {"windows_update", "windows_update.wu_pause_expiry", "windows_update.error"}
    | {"process", "process.pid", "process.uptime_s", "process.capture_version", "process.python_version"}
)

RunCommand = Callable[[Sequence[str], float], tuple[int, str, str]]
DiskUsage = Callable[[Path], Any]  # returns an object with `.free` (shutil.disk_usage)


def write_status_kv_key(component: str) -> str:
    """`kv` key of the last file write of `heartbeat` or `backup` as the component itself saw it (a swallowed replace
    failure, a `-retry-<n>` set): JSON with `ok`, `at` and optional flags. The poller's `component_status:*` only says
    whether the step raised."""
    return f"write_status:{component}"


@dataclass(frozen=True)
class ProbeResult:
    """A probe value, or None with an error code (an exception class name, `exit_<n>`, or a short reason)."""

    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    started_at: datetime
    capture_version: str
    python_version: str


@dataclass(frozen=True)
class HeartbeatProbes:
    process: ProcessInfo
    disk_free_bytes: ProbeResult
    data_root_bytes: ProbeResult
    stripchart: ProbeResult  # value: {"at", "offsets_s", "median_offset_s"}
    w32tm_status: ProbeResult  # value: {snake_case field: raw text}
    wu_pause_expiry: ProbeResult  # value: the registry data, raw
    canary: Mapping[str, str | None]  # canary file -> sha256 now (None when missing)
    vnstock_version: str | None = None


# ---------------------------------------------------------------- probes


def _command(run_command: RunCommand, argv: Sequence[str]) -> ProbeResult:
    try:
        exit_code, stdout, _stderr = run_command(argv, PROBE_TIMEOUT_S)
    except Exception as err:  # a missing binary or a timeout is a probe result, not a crash
        return ProbeResult(None, type(err).__name__)
    if exit_code != 0:
        return ProbeResult(None, f"exit_{exit_code}")
    return ProbeResult(stdout)


def _stripchart(state: CaptureState, config: CaptureConfig, now: datetime, run_command: RunCommand) -> ProbeResult:
    """Offsets from `w32tm /stripchart` (sign [ASSUMED] server minus local, research §3.3), run at most hourly; the
    last result is cached in `kv` so the 15-minute heartbeat reuses it."""
    host = config.heartbeat.stripchart_host
    if not host:
        return ProbeResult(None, "not_configured")
    cached = state.get_kv(STRIPCHART_KV_KEY)
    if cached is not None:
        try:
            data = json.loads(cached)
            age = now - parse_utc(data["at"])
            if timedelta(0) <= age < STRIPCHART_EVERY:
                return ProbeResult(data.get("value"), data.get("error"))
        except (ValueError, KeyError, TypeError):
            logger.warning("ignoring a malformed stripchart cache")
    answer = _command(run_command, ["w32tm", "/stripchart", f"/computer:{host}", "/dataonly", "/samples:3"])
    result = answer
    if answer.error is None:
        offsets = [float(m.group(1)) for m in map(_STRIPCHART_SAMPLE.search, str(answer.value).splitlines()) if m]
        if offsets:
            result = ProbeResult({"at": format_utc(now), "offsets_s": offsets, "median_offset_s": statistics.median(offsets)})
        else:
            result = ProbeResult(None, "no_samples")
    state.set_kv(STRIPCHART_KV_KEY, detail_json(at=format_utc(now), value=result.value, error=result.error))
    return result


def _w32tm_status(run_command: RunCommand) -> ProbeResult:
    answer = _command(run_command, ["w32tm", "/query", "/status", "/verbose"])
    if answer.error is not None:
        return answer
    fields: dict[str, str | None] = {key: None for key in W32TM_FIELDS.values()}
    for line in str(answer.value).splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip() in W32TM_FIELDS:
            fields[W32TM_FIELDS[label.strip()]] = value.strip()
    return ProbeResult(fields)


def _wu_pause_expiry(run_command: RunCommand) -> ProbeResult:
    answer = _command(run_command, ["reg", "query", WU_PAUSE_KEY, "/v", WU_PAUSE_VALUE])
    if answer.error is not None:
        return answer
    for line in str(answer.value).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == WU_PAUSE_VALUE:
            return ProbeResult(" ".join(parts[2:]))
    return ProbeResult(None, "value_not_found")


def _guarded(fn: Callable[[], Any]) -> ProbeResult:
    try:
        return ProbeResult(fn())
    except OSError as err:
        return ProbeResult(None, type(err).__name__)


def _tree_bytes(*roots: Path) -> int:
    """Total size of the files under `roots` (sizes only, nothing is read)."""
    return sum(path.stat().st_size for root in roots if root.is_dir() for path in root.rglob("*") if path.is_file())


def canary_hashes(config: CaptureConfig, clone_root: Path) -> dict[str, str | None]:
    """sha256 of each canary file of the clone, None when it is missing or unreadable."""
    hashes: dict[str, str | None] = {}
    for name in config.heartbeat.canary_files:
        try:
            hashes[name] = sha256_hex((clone_root / name).read_bytes())
        except OSError:
            hashes[name] = None
    return hashes


def take_canary_baseline(state: CaptureState, config: CaptureConfig, clone_root: Path, now: datetime) -> dict[str, str | None]:
    """Store the canary hashes and the time in `kv` (at `run` start and by `canary-reset`, red-team r1 LT4); the caller
    commits."""
    hashes = canary_hashes(config, clone_root)
    state.set_kv(CANARY_BASELINE_KV_KEY, detail_json(at=format_utc(now), files=hashes))
    return hashes


def collect_probes(
    state: CaptureState,
    config: CaptureConfig,
    now: datetime,
    *,
    clone_root: Path,
    data_root: Path,
    run_command: RunCommand,
    process: ProcessInfo,
    vnstock_version: str | None = None,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> HeartbeatProbes:
    """Run every probe once (the stripchart at most hourly). Each one fails alone into `ProbeResult(None, error)`."""
    return HeartbeatProbes(
        process=process,
        disk_free_bytes=_guarded(lambda: int(disk_usage(data_root).free)),
        data_root_bytes=_guarded(lambda: _tree_bytes(data_root / "raw", data_root / "blobs")),
        stripchart=_stripchart(state, config, now, run_command),
        w32tm_status=_w32tm_status(run_command),
        wu_pause_expiry=_wu_pause_expiry(run_command),
        canary=canary_hashes(config, clone_root),
        vnstock_version=vnstock_version,
    )


# ---------------------------------------------------------------- build


def _json_kv(state: StateReader, key: str) -> dict[str, Any] | None:
    text = state.get_kv(key)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _failed(state: StateReader, component: str) -> bool:
    for key in (component_status_kv_key(component), write_status_kv_key(component)):
        status = _json_kv(state, key)
        if status is not None and status.get("ok") is False:
            return True
    return False


def _sources(state: StateReader, config: CaptureConfig, schedule: Schedule, now: datetime, reasons: list[str]) -> dict[str, Any]:
    since = now - WINDOW
    out: dict[str, Any] = {}
    down: list[str] = []
    for cfg in config.enabled_sources:
        polls = state.polls(cfg.name, since=since, until=now + timedelta(microseconds=1))
        last_ok = state.last_poll(cfg.name, status="ok")
        last_ok_at = last_ok.finished_at if last_ok is not None else None
        # Without any ok poll the deadline runs from the first poll, so a source blocked since deployment reads DOWN
        # (code review F10); with no poll at all there is nothing to judge yet.
        first = state.first_poll(cfg.name) if last_ok_at is None else None
        anchor = last_ok_at if last_ok_at is not None else (first.started_at if first is not None else None)
        if anchor is not None and cfg.kind is not SourceKind.VNSTOCK_EVENTS and now > schedule.gap_deadline(anchor, cfg):
            down.append(f"down:{cfg.name}")
        out[cfg.name] = {
            "kind": cfg.kind.value,
            "last_ok_at": format_utc(last_ok_at) if last_ok_at is not None else None,
            "age_s": (now - last_ok_at).total_seconds() if last_ok_at is not None else None,
            "polls_24h_ok": sum(1 for p in polls if p.status == "ok"),
            "polls_24h_error": sum(1 for p in polls if p.status == "error"),
            "expected_polls_24h": schedule.count_due(since, now, cfg),
            "new_items_24h": sum(p.items_new for p in polls),
            "revised_items_24h": sum(p.items_revised for p in polls),
            "open_outages": [o.kind.value for o in state.open_outages(cfg.name)],
        }
    reasons.extend(down)
    return out


def _clock(state: StateReader, now: datetime, probes: HeartbeatProbes) -> dict[str, Any]:
    skews = [p.skew_ms for p in state.polls(since=now - WINDOW, until=now + timedelta(microseconds=1)) if p.skew_ms is not None]
    absolute = [abs(s) for s in skews]
    stripchart = dict(probes.stripchart.value or {})
    status = dict(probes.w32tm_status.value or {key: None for key in W32TM_FIELDS.values()})
    return {
        "skew_ms_median_24h": statistics.median(skews) if skews else None,
        "skew_ms_median_abs_24h": statistics.median(absolute) if absolute else None,
        "skew_ms_max_abs_24h": max(absolute) if absolute else None,
        "polls_with_skew_24h": len(skews),
        "stripchart": {
            "at": stripchart.get("at"), "offsets_s": stripchart.get("offsets_s"),
            "median_offset_s": stripchart.get("median_offset_s"), "error": probes.stripchart.error,
        },
        "w32tm_status": {**status, "error": probes.w32tm_status.error},
    }


def _canary(state: StateReader, probes: HeartbeatProbes) -> dict[str, Any]:
    baseline = _json_kv(state, CANARY_BASELINE_KV_KEY)
    files: dict[str, Any] = {}
    base_files = baseline.get("files", {}) if baseline is not None else {}
    for name, sha in probes.canary.items():
        base = base_files.get(name) if baseline is not None else None
        files[name] = {"sha256": sha, "baseline_sha256": base, "changed": baseline is not None and sha != base}
    return {"baseline_at": baseline.get("at") if baseline is not None else None, "files": files}


def _vnstock(config: CaptureConfig, schedule: Schedule, now: datetime, probes: HeartbeatProbes) -> dict[str, Any] | None:
    cfg = next((s for s in config.sources if s.kind is SourceKind.VNSTOCK_EVENTS), None)
    if cfg is None:
        return None
    expires = cfg.release_expires_on
    return {
        "enabled": cfg.enabled,
        "version": probes.vnstock_version,
        "release_expires_on": expires.isoformat() if expires is not None else None,
        "days_left": (expires - schedule.local_date(now)).days if expires is not None else None,
    }


def build_heartbeat(state: StateReader, config: CaptureConfig, now: datetime, probes: HeartbeatProbes) -> dict[str, Any]:
    """The heartbeat dict at `now` (keys: `HEARTBEAT_KEYS`). Pure over the state and the probe results."""
    schedule = Schedule.from_config(config)
    reasons: list[str] = []
    sources = _sources(state, config, schedule, now, reasons)
    degraded: list[str] = [
        f"open_outage:{o.kind.value}:{o.source or 'process'}" for o in state.all_open_outages()
    ]
    clock = _clock(state, now, probes)
    if clock["skew_ms_median_abs_24h"] is not None and clock["skew_ms_median_abs_24h"] > SKEW_DEGRADED_MS:
        degraded.append("skew_median")
    canary = _canary(state, probes)
    if any(entry["changed"] for entry in canary["files"].values()):
        degraded.append("canary_changed")
    vnstock = _vnstock(config, schedule, now, probes)
    if vnstock is not None and vnstock["enabled"] and vnstock["days_left"] is not None and (
        vnstock["days_left"] <= VNSTOCK_DAYS_LEFT_DEGRADED
    ):
        degraded.append("vnstock_expiring")
    free = probes.disk_free_bytes.value
    if free is not None and free < DISK_MIN_FREE_BYTES:
        degraded.append("disk_low")
    last_backup = state.last_backup()
    if last_backup is None:
        degraded.append("no_backup_yet")
    elif now - last_backup.verified_at > BACKUP_MAX_AGE:
        degraded.append("backup_stale")
    if _failed(state, "heartbeat"):
        degraded.append("heartbeat_write_failed")
    if _failed(state, "backup"):
        degraded.append("backup_write_failed")
    backup_status = _json_kv(state, write_status_kv_key("backup"))
    if backup_status is not None and backup_status.get("retry_written") is True:
        degraded.append("backup_retry_written")
    reasons.extend(degraded)
    status = STATUS_DOWN if any(r.startswith("down:") for r in reasons) else STATUS_DEGRADED if reasons else STATUS_OK

    outages_24h: dict[str, int] = {}
    for outage in state.outages_overlapping(now - WINDOW, now):
        outages_24h[outage.kind.value] = outages_24h.get(outage.kind.value, 0) + 1
    return {
        "schema": HEARTBEAT_SCHEMA,
        "generated_at": format_utc(now),
        "status": status,
        "reasons": reasons,
        "sources": sources,
        "outages_24h": outages_24h,
        "clock": clock,
        "disk": {
            "free_bytes": free, "free_error": probes.disk_free_bytes.error,
            "data_root_bytes": probes.data_root_bytes.value, "data_root_error": probes.data_root_bytes.error,
        },
        "backup": {
            "last_day": last_backup.day.isoformat() if last_backup is not None else None,
            "verified_at": format_utc(last_backup.verified_at) if last_backup is not None else None,
            "age_s": (now - last_backup.verified_at).total_seconds() if last_backup is not None else None,
        },
        "vnstock": vnstock,
        "canary": canary,
        "windows_update": {"wu_pause_expiry": probes.wu_pause_expiry.value, "error": probes.wu_pause_expiry.error},
        "process": {
            "pid": probes.process.pid,
            "uptime_s": (now - probes.process.started_at).total_seconds(),
            "capture_version": probes.process.capture_version,
            "python_version": probes.process.python_version,
        },
    }


# ---------------------------------------------------------------- write


def write_heartbeat(
    backup_dir: Path,
    heartbeat: Mapping[str, Any],
    state: CaptureState,
    now: datetime,
    *,
    sleeper: Sleeper | None,
    replacer: Replacer | None = None,
    syncer: Syncer = fsync_file,
) -> bool:
    """Write `heartbeat.json` and `heartbeats/heartbeat-<UTC day>.json` atomically (temp + replace, retried 3 times 2 s
    apart). True on success, after which the `heartbeats` row is recorded. A final `OSError` is logged, recorded in
    `kv[write_status:heartbeat]` and swallowed: the next heartbeat is DEGRADED `heartbeat_write_failed` (RT11)."""
    body = (json.dumps(heartbeat, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    day = now.astimezone(timezone.utc).date()
    daily_rel = f"{DAILY_DIR}/heartbeat-{day.isoformat()}.json"
    kwargs: dict[str, Any] = {"sleeper": sleeper, "syncer": syncer}
    if replacer is not None:
        kwargs["replacer"] = replacer
    key = write_status_kv_key("heartbeat")
    try:
        (backup_dir / DAILY_DIR).mkdir(parents=True, exist_ok=True)
        write_atomic(backup_dir / HEARTBEAT_FILE, body, **kwargs)
        write_atomic(backup_dir / daily_rel, body, **kwargs)
    except OSError as err:  # a locked sync folder must not stop capture
        logger.error("heartbeat write failed: class=%s", type(err).__name__)
        state.set_kv(key, error_detail(err, ok=False, at=format_utc(now)))
        return False
    state.record_heartbeat(now, str(heartbeat.get("status")), daily_rel, sha256_hex(body))
    state.set_kv(key, detail_json(ok=True, at=format_utc(now)))
    return True


def run_heartbeat(
    state: CaptureState,
    config: CaptureConfig,
    now: datetime,
    *,
    backup_dir: Path,
    data_root: Path,
    clone_root: Path,
    run_command: RunCommand,
    process: ProcessInfo,
    sleeper: Sleeper | None,
    vnstock_version: str | None = None,
) -> dict[str, Any]:
    """Probe, build and write one heartbeat (the poller's `heartbeat_fn` and the `heartbeat` command); the caller commits."""
    probes = collect_probes(
        state, config, now, clone_root=clone_root, data_root=data_root, run_command=run_command, process=process,
        vnstock_version=vnstock_version,
    )
    heartbeat = build_heartbeat(state, config, now, probes)
    write_heartbeat(backup_dir, heartbeat, state, now, sleeper=sleeper)
    return heartbeat
