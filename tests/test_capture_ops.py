"""Capture ops: heartbeat, backup and audit (plan 261022-1147 phase 2, R2.10-R2.12; red-team r1 RT1, RT9, RT11, RT14, RT15, LT2,
LT4).

Every instant is passed in (no real clock), every probe command, replacer and sleeper is a fake, and every file lives under
`tmp_path`. State rows are synthetic (made-up source names, ids and counts); no portal or vnstock data, no fixture files.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest

from finsec_alpharadar.capture.audit import AuditReport, audit
from finsec_alpharadar.capture.backup import BackupError, BackupRefused, iter_backup_members, run_backup, snapshot_state
from finsec_alpharadar.capture.config import CaptureConfig, HeartbeatConfig, Holiday, ScheduleConfig, SourceConfig, SourceKind
from finsec_alpharadar.capture.heartbeat import (
    CANARY_BASELINE_KV_KEY,
    HEARTBEAT_KEYS,
    HEARTBEAT_SCHEMA,
    ProcessInfo,
    build_heartbeat,
    collect_probes,
    take_canary_baseline,
    write_heartbeat,
    write_status_kv_key,
)
from finsec_alpharadar.capture.poller import component_status_kv_key
from finsec_alpharadar.capture.record import OutageKind, detail_json, format_utc
from finsec_alpharadar.capture.schedule import Schedule
from finsec_alpharadar.capture.storage import CaptureState, PollRow, StateReader, StorageError

T0 = datetime(2026, 10, 27, 3, 0, 0, tzinfo=timezone.utc)  # 10:00 ICT on a Tuesday
HOSE_URL = "https://portal-a.example.invalid/n/api/v1/1/news/securitiesType/0"
RSS_URL = "https://portal-b.example.invalid/3/vi_vn/feed.rss"
RSS = "hnx_rss_issuers"
HOSE = "hose_news_api"
GB = 1_000_000_000


# ---------------------------------------------------------------- fakes and builders


def _schedule(**changes: Any) -> ScheduleConfig:
    fields: dict[str, Any] = {
        "active_start": time(8, 0), "active_end": time(17, 30), "loop_tick_s": 30.0, "max_poll_duration_s": 330.0,
        "clock_step_threshold_s": 2.0, "heartbeat_every_s": 900.0, "watchdog_limit_s": 900.0,
        "backup_at_local": time(7, 15),
    }
    fields.update(changes)
    return ScheduleConfig(**fields)


def _portal(name: str, kind: SourceKind, **changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": name, "kind": kind, "enabled": True, "url": HOSE_URL if kind is SourceKind.HOSE_NEWS_API else RSS_URL,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 30.0, "deadline_s": 60.0, "retries": 1,
        "max_body_bytes": 2_000_000, "page_size": 20 if kind is SourceKind.HOSE_NEWS_API else None,
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _vnstock(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "vnstock_events", "kind": SourceKind.VNSTOCK_EVENTS, "enabled": True, "url": None,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 60.0, "deadline_s": 60.0, "retries": 0,
        "max_body_bytes": 2_000_000, "vnstock_source": "vci", "symbols": ("AAA", "BBB"), "symbols_per_poll": 2,
        "calls_per_min": 60.0, "vendor_call_timeout_s": 60.0, "release_expires_on": date(2027, 10, 17),
        "quota_warning_patterns": ("truncat",),
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _config(*sources: SourceConfig, holidays: Sequence[Holiday] = (), **schedule: Any) -> CaptureConfig:
    sources = sources or (_portal(RSS, SourceKind.RSS_FEED), _portal(HOSE, SourceKind.HOSE_NEWS_API))
    return CaptureConfig(
        schedule=_schedule(**schedule), holidays=tuple(holidays), sources=sources,
        heartbeat=HeartbeatConfig(canary_files=("AGENTS.md", "CLAUDE.md"), stripchart_host="ntp.example.invalid"),
    )


_OPEN_STATES: list[CaptureState] = []


@pytest.fixture(autouse=True)
def _close_states() -> Any:
    """Close every state a test opened (closing twice is harmless)."""
    yield
    while _OPEN_STATES:
        _OPEN_STATES.pop().close()


def _open_state(path: Path) -> CaptureState:
    state = CaptureState(path)
    _OPEN_STATES.append(state)
    return state


def _state(tmp_path: Path) -> CaptureState:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    return _open_state(root / "state" / "capture.sqlite")


def _poll(state: CaptureState, source: str, started: datetime, *, status: str = "ok", skew_ms: float | None = 100.0,
          items_new: int = 1, items_revised: int = 0, run_mode: str = "run", duration_s: float = 5.0) -> None:
    state.insert_poll(PollRow(
        cycle_id=f"c-{started.timestamp():.0f}", source=source, started_at=started,
        finished_at=started + timedelta(seconds=duration_s), status=status,
        outage_kind=None if status == "ok" else OutageKind.SOURCE_UNREACHABLE,
        detail=None if status == "ok" else detail_json(reason="reset"), http_status=200 if status == "ok" else None,
        http_date=None, rtt_ms=100.0, skew_ms=skew_ms, items_total=items_new, items_new=items_new,
        items_revised=items_revised, pages=1, run_mode=run_mode, pid=4242,
    ))


@dataclass
class FakeRunner:
    """`run_command` fake: answers by the first two argv words; an exception instance is raised."""

    answers: dict[tuple[str, ...], Any] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        answer = self.answers.get(tuple(argv[:2]), (1, "", "not scripted"))
        if isinstance(answer, BaseException):
            raise answer
        return answer


STRIPCHART_OK = (0, "Tracking ntp.example.invalid [192.0.2.1:123].\nCollecting 3 samples.\n"
                    "10:00:01, +00.6556494s\n10:00:03, +00.6796858s\n10:00:05, +00.6813058s\n", "")
STATUS_OK = (0, "Leap Indicator: 0(no warning)\nStratum: 3 (secondary reference - syncd by (S)NTP)\n"
                "Last Successful Sync Time: 27/10/2026 9:55:00\nSource: ntp.example.invalid\nPoll Interval: 10 (1024s)\n"
                "Phase Offset: 0.0012345s\nState Machine: 2 (Sync)\nLast Sync Error: 0 (The command completed successfully.)\n"
                "Time since Last Good Sync Time: 300.0000000s\nSomething Else: ignored\n", "")
REG_OK = (0, "\nHKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings\n"
             "    PauseUpdatesExpiryTime    REG_SZ    2026-11-20T03:00:00Z\n\n", "")


def _runner() -> FakeRunner:
    return FakeRunner({("w32tm", "/stripchart"): STRIPCHART_OK, ("w32tm", "/query"): STATUS_OK, ("reg", "query"): REG_OK})


@dataclass
class Disk:
    free: int


def _clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir(exist_ok=True)
    (clone / "AGENTS.md").write_bytes(b"# contract v1\n")
    (clone / "CLAUDE.md").write_bytes(b"@AGENTS.md\n")
    return clone


PROCESS = ProcessInfo(pid=4242, started_at=T0 - timedelta(hours=2), capture_version="0.1.0+0123456789ab",
                      python_version="3.13.14")


def _probes(state: CaptureState, config: CaptureConfig, tmp_path: Path, now: datetime = T0, *,
            runner: FakeRunner | None = None, free: int = 50 * GB, disk_usage: Callable[[Path], Any] | None = None,
            vnstock_version: str | None = None) -> Any:
    return collect_probes(
        state, config, now, clone_root=_clone_dir(tmp_path), data_root=tmp_path / "root", run_command=runner or _runner(),
        process=PROCESS, vnstock_version=vnstock_version, disk_usage=disk_usage or (lambda path: Disk(free)),
    )


def _clone_dir(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    return clone if clone.is_dir() else _clone(tmp_path)


def _healthy(state: CaptureState, config: CaptureConfig, tmp_path: Path, now: datetime = T0) -> None:
    """Every enabled source polled ok a minute ago, yesterday backed up, a canary baseline taken."""
    for cfg in config.enabled_sources:
        _poll(state, cfg.name, now - timedelta(seconds=60))
    state.record_backup(now.date() - timedelta(days=1), "ab" * 32, now - timedelta(hours=3))
    take_canary_baseline(state, config, _clone_dir(tmp_path), now - timedelta(hours=2))


def _heartbeat(state: CaptureState, config: CaptureConfig, tmp_path: Path, now: datetime = T0, **probes: Any) -> dict[str, Any]:
    return build_heartbeat(state, config, now, _probes(state, config, tmp_path, now, **probes))


# ---------------------------------------------------------------- heartbeat (R2.10)


def test_status_ok_degraded_down_rules(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    beat = _heartbeat(state, config, tmp_path)
    assert beat["schema"] == HEARTBEAT_SCHEMA and beat["generated_at"] == format_utc(T0)
    assert (beat["status"], beat["reasons"]) == ("OK", [])
    rss = beat["sources"][RSS]
    assert rss["last_ok_at"] == format_utc(T0 - timedelta(seconds=55)) and rss["age_s"] == 55.0
    assert rss["polls_24h_ok"] == 1 and rss["polls_24h_error"] == 0 and rss["new_items_24h"] == 1
    assert rss["expected_polls_24h"] > 100, "a 180 s active cadence over a trading morning"

    outage = state.open_outage(OutageKind.SOURCE_REJECTED, HOSE, start_at=T0 - timedelta(seconds=30), detected_at=T0,
                               detail=detail_json(reason="http_status", http_status=403))
    beat = _heartbeat(state, config, tmp_path)
    assert beat["status"] == "DEGRADED" and beat["reasons"] == ["open_outage:source_rejected:hose_news_api"]
    assert beat["sources"][HOSE]["open_outages"] == ["source_rejected"]
    assert beat["outages_24h"] == {"source_rejected": 1}
    state.close_outage(outage, T0)

    later = T0 + timedelta(minutes=20)  # past the gap deadline of both sources (two 3-min steps + 330 s)
    beat = _heartbeat(state, config, tmp_path, later)
    assert beat["status"] == "DOWN" and beat["reasons"] == [f"down:{RSS}", f"down:{HOSE}"]

    _poll(state, RSS, later - timedelta(seconds=30))
    _poll(state, HOSE, later - timedelta(seconds=30))
    assert _heartbeat(state, config, tmp_path, later, free=9 * GB)["reasons"] == ["disk_low"]

    state.set_kv(write_status_kv_key("heartbeat"), detail_json(ok=False, at=format_utc(T0)))
    state.set_kv(component_status_kv_key("backup"), detail_json(ok=False, at=format_utc(T0), **{"class": "OSError"}))
    beat = _heartbeat(state, config, tmp_path, later)
    assert beat["status"] == "DEGRADED" and beat["reasons"] == ["heartbeat_write_failed", "backup_write_failed"]
    state.set_kv(write_status_kv_key("heartbeat"), detail_json(ok=True, at=format_utc(T0)))
    state.set_kv(component_status_kv_key("backup"), detail_json(ok=True, at=format_utc(T0)))

    stale = later + timedelta(hours=34)  # the backup verified at T0 - 3 h is now 37 h old
    for name in (RSS, HOSE):
        _poll(state, name, stale - timedelta(seconds=30))
    assert _heartbeat(state, config, tmp_path, stale)["reasons"] == ["backup_stale"]

    fresh = _state(tmp_path / "fresh")
    _poll(fresh, RSS, T0 - timedelta(seconds=60))
    _poll(fresh, HOSE, T0 - timedelta(seconds=60), status="error")
    fresh.open_outage(OutageKind.SOURCE_UNREACHABLE, HOSE, start_at=T0 - timedelta(seconds=60), detected_at=T0, detail=None)
    beat = _heartbeat(fresh, config, tmp_path / "fresh")
    assert beat["status"] == "DEGRADED", "a source whose first poll is recent is not DOWN yet, but its outage shows"
    assert beat["reasons"] == ["open_outage:source_unreachable:hose_news_api", "no_backup_yet"]
    assert beat["sources"][HOSE]["last_ok_at"] is None and beat["sources"][HOSE]["age_s"] is None
    assert beat["canary"]["baseline_at"] is None, "no baseline yet (only `run` takes one): reported, not degraded"


def test_source_that_never_had_an_ok_poll_goes_down_after_its_first_poll(tmp_path: Path) -> None:
    # A WAF that answers 403 from the first deployment on must read DOWN, not DEGRADED forever (code review F10).
    config = _config()
    state = _state(tmp_path)
    _poll(state, HOSE, T0 - timedelta(seconds=60), status="error")
    later = T0 + timedelta(minutes=20)  # past the gap deadline anchored on that first poll
    _poll(state, RSS, later - timedelta(seconds=30))
    _poll(state, HOSE, later - timedelta(seconds=30), status="error")
    beat = _heartbeat(state, config, tmp_path, later)
    assert beat["status"] == "DOWN" and f"down:{HOSE}" in beat["reasons"] and f"down:{RSS}" not in beat["reasons"]
    assert beat["sources"][HOSE]["last_ok_at"] is None

def test_skew_median_and_max(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    for minutes, skew in ((10, -500.0), (20, 3000.0), (30, None)):
        _poll(state, RSS, T0 - timedelta(minutes=minutes), skew_ms=skew)
    _poll(state, RSS, T0 - timedelta(hours=25), skew_ms=99_999.0)  # outside the 24 h window
    clock = _heartbeat(state, config, tmp_path)["clock"]
    # in the window: 100 (HOSE), 100 (RSS, 1 min ago), -500, 3000
    assert clock["polls_with_skew_24h"] == 4
    assert clock["skew_ms_median_24h"] == 100.0
    assert clock["skew_ms_median_abs_24h"] == 300.0
    assert clock["skew_ms_max_abs_24h"] == 3000.0
    assert clock["stripchart"]["offsets_s"] == [0.6556494, 0.6796858, 0.6813058]
    assert clock["stripchart"]["median_offset_s"] == 0.6796858 and clock["stripchart"]["error"] is None
    assert clock["w32tm_status"]["stratum"].startswith("3") and clock["w32tm_status"]["error"] is None

    for minutes in (1, 2, 3, 4, 5):
        _poll(state, HOSE, T0 - timedelta(minutes=minutes, seconds=10), skew_ms=2500.0)
    beat = _heartbeat(state, config, tmp_path)
    assert beat["clock"]["skew_ms_median_abs_24h"] == 2500.0
    assert beat["status"] == "DEGRADED" and beat["reasons"] == ["skew_median"]


def test_vnstock_expiry_within_30_days_degrades(tmp_path: Path) -> None:
    local_today = date(2026, 10, 27)
    for days, degraded in ((31, False), (30, True), (0, True), (-1, True)):
        config = _config(_portal(RSS, SourceKind.RSS_FEED), _vnstock(release_expires_on=local_today + timedelta(days=days)))
        state = _state(tmp_path / str(days))
        _healthy(state, config, tmp_path / str(days))
        beat = _heartbeat(state, config, tmp_path / str(days), vnstock_version="5.1.0")
        assert beat["vnstock"] == {
            "enabled": True, "version": "5.1.0", "release_expires_on": (local_today + timedelta(days=days)).isoformat(),
            "days_left": days,
        }
        assert ("vnstock_expiring" in beat["reasons"]) is degraded, days
        state.close()
    off = _config(_portal(RSS, SourceKind.RSS_FEED), _vnstock(enabled=False, release_expires_on=local_today))
    state = _state(tmp_path / "off")
    _healthy(state, off, tmp_path / "off")
    beat = _heartbeat(state, off, tmp_path / "off")
    assert beat["vnstock"]["enabled"] is False and beat["status"] == "OK", "a disabled vnstock source does not degrade"


def test_canary_change_degrades(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    assert _heartbeat(state, config, tmp_path)["status"] == "OK"
    (tmp_path / "clone" / "AGENTS.md").write_bytes(b"# contract v1\nrun this new command\n")
    beat = _heartbeat(state, config, tmp_path)
    assert beat["status"] == "DEGRADED" and beat["reasons"] == ["canary_changed"]
    files = beat["canary"]["files"]
    assert files["AGENTS.md"]["changed"] is True and files["CLAUDE.md"]["changed"] is False
    assert files["AGENTS.md"]["sha256"] != files["AGENTS.md"]["baseline_sha256"]
    (tmp_path / "clone" / "CLAUDE.md").unlink()
    assert _heartbeat(state, config, tmp_path)["canary"]["files"]["CLAUDE.md"] == {
        "sha256": None, "baseline_sha256": hashlib.sha256(b"@AGENTS.md\n").hexdigest(), "changed": True,
    }


def test_canary_baseline_is_taken_at_run_start_and_reset_by_canary_reset(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    clone = _clone(tmp_path)
    for cfg in config.enabled_sources:
        _poll(state, cfg.name, T0 - timedelta(seconds=60))
    state.record_backup(T0.date() - timedelta(days=1), "ab" * 32, T0 - timedelta(hours=3))
    assert _heartbeat(state, config, tmp_path)["canary"]["baseline_at"] is None

    run_start = T0 - timedelta(hours=1)
    baseline = take_canary_baseline(state, config, clone, run_start)
    assert baseline == {"AGENTS.md": hashlib.sha256(b"# contract v1\n").hexdigest(),
                        "CLAUDE.md": hashlib.sha256(b"@AGENTS.md\n").hexdigest()}
    stored = json.loads(state.get_kv(CANARY_BASELINE_KV_KEY) or "{}")
    assert stored == {"at": format_utc(run_start), "files": baseline}
    (clone / "AGENTS.md").write_bytes(b"# contract v2\n")
    assert _heartbeat(state, config, tmp_path)["reasons"] == ["canary_changed"]

    reset_at = T0 - timedelta(minutes=5)  # canary-reset: the owner accepted the change
    take_canary_baseline(state, config, clone, reset_at)
    beat = _heartbeat(state, config, tmp_path)
    assert beat["status"] == "OK" and beat["canary"]["baseline_at"] == format_utc(reset_at)


def _keys(value: Any, path: str = "") -> set[str]:
    """Every key path of a JSON value; map entries keyed by data (source names, file names, kinds) collapse to `*`."""
    found: set[str] = set()
    if isinstance(value, dict):
        dynamic = path in ("sources", "canary.files", "outages_24h")
        for key, inner in value.items():
            name = f"{path}.*" if dynamic else (f"{path}.{key}" if path else key)
            found.add(name)
            found |= _keys(inner, name)
    return found


def test_heartbeat_keys_are_a_fixed_whitelist(tmp_path: Path) -> None:
    config = _config(_portal(RSS, SourceKind.RSS_FEED), _portal(HOSE, SourceKind.HOSE_NEWS_API), _vnstock())
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    state.open_outage(OutageKind.COVERAGE_OVERFLOW, RSS, start_at=T0, detected_at=T0, detail=detail_json(items=100))
    beat = _heartbeat(state, config, tmp_path, vnstock_version="5.1.0")
    assert _keys(beat) == HEARTBEAT_KEYS
    text = json.dumps(beat, ensure_ascii=False)
    for private in (str(tmp_path), tmp_path.name, "AAA", "BBB", "contract", os.environ.get("USERNAME", "\0"), "192.0.2.1"):
        assert private not in text, f"{private!r} leaked into the heartbeat"


def test_heartbeat_write_is_atomic_and_daily(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    backup_dir = tmp_path / "sync"
    backup_dir.mkdir()
    replaced: list[tuple[Path, Path]] = []

    def replacer(src: Path, dst: Path) -> None:
        replaced.append((Path(src), Path(dst)))
        assert Path(src).parent == Path(dst).parent and Path(src).is_file() and Path(src).name.endswith(".tmp")
        os.replace(src, dst)

    sleeps: list[float] = []
    beat = _heartbeat(state, config, tmp_path)
    assert write_heartbeat(backup_dir, beat, state, T0, sleeper=sleeps.append, replacer=replacer) is True
    latest, daily = backup_dir / "heartbeat.json", backup_dir / "heartbeats" / "heartbeat-2026-10-27.json"
    assert latest.read_bytes() == daily.read_bytes() and json.loads(latest.read_text(encoding="utf-8")) == beat
    assert [dst for _, dst in replaced] == [latest, daily] and sleeps == []
    assert sorted(p.name for p in backup_dir.rglob("*") if p.is_file()) == ["heartbeat-2026-10-27.json", "heartbeat.json"]
    rows = state.connection.execute("SELECT written_at, status, rel_path, sha256 FROM heartbeats").fetchall()
    daily_sha = hashlib.sha256(daily.read_bytes()).hexdigest()
    assert rows == [(format_utc(T0), "OK", "heartbeats/heartbeat-2026-10-27.json", daily_sha)]

    next_day = T0 + timedelta(days=1)
    beat2 = _heartbeat(state, config, tmp_path, next_day)
    assert write_heartbeat(backup_dir, beat2, state, next_day, sleeper=sleeps.append, replacer=replacer)
    assert json.loads(latest.read_text(encoding="utf-8"))["generated_at"] == format_utc(next_day)
    assert json.loads(daily.read_text(encoding="utf-8"))["generated_at"] == format_utc(T0), "a closed day's file stays"
    assert (backup_dir / "heartbeats" / "heartbeat-2026-10-28.json").is_file()


def test_locked_replace_retries_then_degrades_without_raising(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    backup_dir = tmp_path / "sync"
    backup_dir.mkdir()
    attempts: list[Path] = []

    def locked(src: Path, dst: Path) -> None:
        attempts.append(Path(dst))
        raise PermissionError(13, "the sync client holds the file")

    sleeps: list[float] = []
    beat = _heartbeat(state, config, tmp_path)
    assert write_heartbeat(backup_dir, beat, state, T0, sleeper=sleeps.append, replacer=locked) is False
    assert len(attempts) == 4 and sleeps == [2.0, 2.0, 2.0], "one try plus three retries, 2 s apart"
    assert state.connection.execute("SELECT COUNT(*) FROM heartbeats").fetchone() == (0,), "no row before a replace succeeds"
    assert not [p for p in backup_dir.rglob("*") if p.is_file()], "the temp file is cleaned up"
    assert json.loads(state.get_kv(write_status_kv_key("heartbeat")) or "{}")["ok"] is False
    beat = _heartbeat(state, config, tmp_path, T0 + timedelta(minutes=1))
    assert beat["status"] == "DEGRADED" and beat["reasons"] == ["heartbeat_write_failed"]

    flaky_left = [2]

    def flaky(src: Path, dst: Path) -> None:
        if flaky_left[0]:
            flaky_left[0] -= 1
            raise PermissionError(13, "locked")
        os.replace(src, dst)

    sleeps.clear()
    assert write_heartbeat(backup_dir, beat, state, T0 + timedelta(minutes=1), sleeper=sleeps.append, replacer=flaky)
    assert sleeps == [2.0, 2.0]
    assert state.connection.execute("SELECT COUNT(*) FROM heartbeats").fetchone() == (1,)
    assert json.loads(state.get_kv(write_status_kv_key("heartbeat")) or "{}")["ok"] is True


def test_probe_failure_is_null_with_error_not_a_crash(tmp_path: Path) -> None:
    config = _config()
    state = _state(tmp_path)
    _healthy(state, config, tmp_path)
    runner = FakeRunner({
        ("w32tm", "/stripchart"): (0, "Tracking ntp.example.invalid.\nerror: 0x800705B4\n", ""),
        ("w32tm", "/query"): OSError("w32tm is not on PATH"),
        ("reg", "query"): (1, "", "ERROR: The system was unable to find the specified registry key or value."),
    })

    def broken_disk(path: Path) -> Any:
        raise PermissionError(13, "denied")

    probes = _probes(state, config, tmp_path, runner=runner, disk_usage=broken_disk)
    beat = build_heartbeat(state, config, T0, probes)
    assert beat["clock"]["stripchart"]["offsets_s"] is None and beat["clock"]["stripchart"]["error"] == "no_samples"
    assert beat["clock"]["w32tm_status"]["stratum"] is None and beat["clock"]["w32tm_status"]["error"] == "OSError"
    assert beat["windows_update"] == {"wu_pause_expiry": None, "error": "exit_1"}
    assert beat["disk"]["free_bytes"] is None and beat["disk"]["free_error"] == "PermissionError"
    assert "w32tm is not on PATH" not in json.dumps(beat), "an error is its class or exit code, never the text"
    assert beat["status"] == "OK", "a probe failure is reported, not a degradation of capture"

    good = _probes(state, config, tmp_path, T0 + timedelta(minutes=15), runner=_runner())
    assert good.stripchart.error == "no_samples", "the stripchart runs hourly: a cached result is reused for 15 min"
    later = _probes(state, config, tmp_path, T0 + timedelta(minutes=61), runner=(runner2 := _runner()))
    assert later.stripchart.error is None and ["w32tm", "/stripchart"] == runner2.calls[0][:2]
    beat = build_heartbeat(state, config, T0, _probes(state, config, tmp_path, runner=_runner()))
    assert beat["windows_update"] == {"wu_pause_expiry": "2026-11-20T03:00:00Z", "error": None}


# ---------------------------------------------------------------- backup (R2.11)

DAY = date(2026, 10, 26)
BLOB = b"<rss>synthetic body</rss>"
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()


def _data_root(tmp_path: Path) -> tuple[Path, CaptureState, dict[str, bytes]]:
    """A data root holding two sources' files of DAY and of the next day, one blob of DAY, and a committed state."""
    root = tmp_path / "root"
    files = {
        f"raw/{RSS}/2026-10-26.jsonl": b'{"source_id":"101"}\n{"source_id":"102"}\n',
        f"raw/{HOSE}/2026-10-26.jsonl": b'{"source_id":"7"}\n',
        f"raw/{RSS}/2026-10-27.jsonl": b'{"source_id":"103"}\n',
        f"blobs/2026-10-26/{BLOB_SHA}.bin": BLOB,
        "blobs/2026-10-27/" + "0" * 64 + ".bin": b"next day",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    state = _open_state(root / "state" / "capture.sqlite")
    _poll(state, RSS, datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc))
    state.commit(T0)
    return root, state, files


def _sync_dir(tmp_path: Path) -> Path:
    backup_dir = tmp_path / "sync"
    backup_dir.mkdir(exist_ok=True)
    return backup_dir


def _all_files(directory: Path) -> dict[str, bytes]:
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_backup_archives_one_closed_utc_day_with_a_verified_manifest(tmp_path: Path) -> None:
    root, state, files = _data_root(tmp_path)
    backup_dir = _sync_dir(tmp_path)
    result = run_backup(root, backup_dir, state, DAY, now=T0)
    assert result.verified and not result.skipped and not result.retry_written and result.error is None
    folder = backup_dir / "daily" / "2026-10-26"
    assert sorted(p.name for p in folder.iterdir()) == [
        "capture-2026-10-26.tar.gz", "manifest-2026-10-26.json", "state-2026-10-26.sqlite",
    ], "no temp file, no -wal/-shm next to the snapshot"
    manifest_bytes = (folder / "manifest-2026-10-26.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["day"] == "2026-10-26" and manifest["created_at"] == format_utc(T0)
    for entry in manifest["files"]:
        data = (folder / entry["name"]).read_bytes()
        assert entry == {"name": entry["name"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert {i["path"]: (i["sha256"], i["lines"]) for i in manifest["inputs"]} == {
        f"raw/{HOSE}/2026-10-26.jsonl": (hashlib.sha256(files[f"raw/{HOSE}/2026-10-26.jsonl"]).hexdigest(), 1),
        f"raw/{RSS}/2026-10-26.jsonl": (hashlib.sha256(files[f"raw/{RSS}/2026-10-26.jsonl"]).hexdigest(), 2),
        f"blobs/2026-10-26/{BLOB_SHA}.bin": (BLOB_SHA, None),
    }
    members = dict(iter_backup_members(folder / "capture-2026-10-26.tar.gz", folder / "manifest-2026-10-26.json"))
    assert members == {rel: data for rel, data in files.items() if "2026-10-26" in rel}, "only the closed day, byte-exact"
    row = state.backup_row(DAY)
    assert row is not None and row.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    with tarfile.open(folder / "capture-2026-10-26.tar.gz") as tar:
        assert {(m.uname, m.gname, m.uid, m.gid) for m in tar.getmembers()} == {("", "", 0, 0)}, "no owner names"
    snapshot = StateReader.open_snapshot(folder / "state-2026-10-26.sqlite")
    try:
        assert [p.source for p in snapshot.polls()] == [RSS], "the snapshot holds the committed state"
    finally:
        snapshot.close()


def test_backup_is_idempotent(tmp_path: Path) -> None:
    root, state, _ = _data_root(tmp_path)
    backup_dir = _sync_dir(tmp_path)
    first = run_backup(root, backup_dir, state, DAY, now=T0)
    before = _all_files(backup_dir)
    again = run_backup(root, backup_dir, state, DAY, now=T0 + timedelta(hours=1))
    assert again.verified and again.skipped and again.manifest_sha256 == first.manifest_sha256
    assert _all_files(backup_dir) == before, "a verified backup is not rewritten"

    archive = backup_dir / "daily" / "2026-10-26" / "capture-2026-10-26.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"rot")  # the sync client damaged it
    damaged = archive.read_bytes()
    retry = run_backup(root, backup_dir, state, DAY, now=T0 + timedelta(hours=2))
    assert retry.verified and retry.retry_written and retry.suffix == "-retry-1"
    folder = archive.parent
    assert archive.read_bytes() == damaged, "the old backup is never overwritten"
    assert sorted(p.name for p in folder.iterdir()) == [
        "capture-2026-10-26-retry-1.tar.gz", "capture-2026-10-26.tar.gz", "manifest-2026-10-26-retry-1.json",
        "manifest-2026-10-26.json", "state-2026-10-26-retry-1.sqlite", "state-2026-10-26.sqlite",
    ]
    assert json.loads(state.get_kv(write_status_kv_key("backup")) or "{}")["retry_written"] is True
    assert run_backup(root, backup_dir, state, DAY, now=T0 + timedelta(hours=3)).skipped, "the verified retry set counts"


def test_backup_refuses_today(tmp_path: Path) -> None:
    root, state, _ = _data_root(tmp_path)
    backup_dir = _sync_dir(tmp_path)
    for day in (T0.date(), T0.date() + timedelta(days=1)):
        with pytest.raises(BackupRefused):
            run_backup(root, backup_dir, state, day, now=T0)
    assert not (backup_dir / "daily").exists()


def test_backup_at_0300_ict_refuses_the_still_open_utc_day(tmp_path: Path) -> None:
    root, state, _ = _data_root(tmp_path)
    backup_dir = _sync_dir(tmp_path)
    now = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)  # 03:00 ICT on 2026-10-27: UTC 2026-10-26 is still open
    with pytest.raises(BackupRefused):
        run_backup(root, backup_dir, state, DAY, now=now)
    assert run_backup(root, backup_dir, state, DAY - timedelta(days=1), now=now).verified


def test_snapshot_is_not_in_wal_mode(tmp_path: Path) -> None:
    root, state, _ = _data_root(tmp_path)
    assert (root / "state" / "capture.sqlite").read_bytes()[18:20] == bytes([2, 2]), "the live state is WAL"
    backup_dir = _sync_dir(tmp_path)
    run_backup(root, backup_dir, state, DAY, now=T0)
    snapshot = backup_dir / "daily" / "2026-10-26" / "state-2026-10-26.sqlite"
    assert snapshot.read_bytes()[18:20] == bytes([1, 1])
    assert [p.name for p in (root / "state").iterdir() if "backup" in p.name] == [], "the work copy is removed"


def _malicious(tmp_path: Path, name: str, build: Callable[[tarfile.TarFile], None], inputs: list[str]) -> tuple[Path, Path]:
    folder = tmp_path / name
    folder.mkdir()
    archive = folder / "capture-2026-10-26.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        build(tar)
    data = archive.read_bytes()
    manifest = folder / "manifest-2026-10-26.json"
    manifest.write_text(json.dumps({
        "schema": "capture-backup/1", "day": "2026-10-26",
        "files": [{"name": archive.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
        "inputs": [{"path": p, "bytes": 1, "sha256": "0" * 64, "lines": 1} for p in inputs],
    }), encoding="utf-8")
    return archive, manifest


def _file(tar: tarfile.TarFile, name: str, data: bytes = b"x") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def test_backup_reader_checks_the_manifest_sha_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, state, _ = _data_root(tmp_path)
    backup_dir = _sync_dir(tmp_path)
    run_backup(root, backup_dir, state, DAY, now=T0)
    folder = backup_dir / "daily" / "2026-10-26"
    archive, manifest = folder / "capture-2026-10-26.tar.gz", folder / "manifest-2026-10-26.json"
    archive.write_bytes(archive.read_bytes()[:-1] + b"!")

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the archive was opened before its sha256 was checked")

    import finsec_alpharadar.capture.backup as backup_module

    monkeypatch.setattr(backup_module.tarfile, "open", never)
    with pytest.raises(BackupError, match="sha256"):
        list(iter_backup_members(archive, manifest))
    stray = folder / "capture-2026-10-26-copy.tar.gz"
    stray.write_bytes(archive.read_bytes())
    with pytest.raises(BackupError, match="not listed"):
        list(iter_backup_members(stray, manifest))


def test_backup_reader_refuses_traversal_links_and_unlisted_members(tmp_path: Path) -> None:
    good = f"raw/{RSS}/2026-10-26.jsonl"

    def traversal(tar: tarfile.TarFile) -> None:
        _file(tar, good)
        _file(tar, "../escaped.txt")

    def absolute(tar: tarfile.TarFile) -> None:
        _file(tar, "/raw/abs/2026-10-26.jsonl")

    def symlink(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(good)
        info.type, info.linkname = tarfile.SYMTYPE, "../../outside.txt"
        tar.addfile(info)

    def hardlink(tar: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(good)
        info.type, info.linkname = tarfile.LNKTYPE, "../../outside.txt"
        tar.addfile(info)

    def unlisted(tar: tarfile.TarFile) -> None:
        _file(tar, good)
        _file(tar, f"raw/{HOSE}/2026-10-26.jsonl")

    def other_day(tar: tarfile.TarFile) -> None:
        _file(tar, f"raw/{RSS}/2026-10-25.jsonl")

    cases = {"traversal": traversal, "absolute": absolute, "symlink": symlink, "hardlink": hardlink, "unlisted": unlisted,
             "other_day": other_day}
    listed = [good, f"raw/{RSS}/2026-10-25.jsonl", "../escaped.txt", "/raw/abs/2026-10-26.jsonl"]
    for name, build in cases.items():
        archive, manifest = _malicious(tmp_path, name, build, listed)
        with pytest.raises(BackupError):
            for _member in iter_backup_members(archive, manifest):
                pass
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path.parent / "escaped.txt").exists()
    archive, manifest = _malicious(tmp_path, "fine", lambda tar: _file(tar, good, b"ok"), [good])
    assert list(iter_backup_members(archive, manifest)) == [(good, b"ok")]


def test_backup_never_deletes_sources(tmp_path: Path) -> None:
    root, state, files = _data_root(tmp_path)
    backup_dir = _sync_dir(tmp_path)

    def locked(src: Path, dst: Path) -> None:
        raise PermissionError(13, "the sync client holds the file")

    sleeps: list[float] = []
    failed = run_backup(root, backup_dir, state, DAY, now=T0, sleeper=sleeps.append, replacer=locked)
    assert not failed.verified and failed.error == "PermissionError" and sleeps == [2.0, 2.0, 2.0]
    assert state.backup_row(DAY) is None, "no backups row before a verified write"
    assert json.loads(state.get_kv(write_status_kv_key("backup")) or "{}")["ok"] is False
    assert _all_files(backup_dir) == {}, "temp files are removed, nothing half-written is left"

    first = run_backup(root, backup_dir, state, DAY, now=T0 + timedelta(minutes=15))
    archive = backup_dir / "daily" / "2026-10-26" / "capture-2026-10-26.tar.gz"
    archive.write_bytes(b"damaged")
    run_backup(root, backup_dir, state, DAY, now=T0 + timedelta(minutes=30))
    assert first.verified and archive.read_bytes() == b"damaged", "an old backup file is kept as it is"
    assert {rel: (root / rel).read_bytes() for rel in files} == files, "every source file is still there, unchanged"
    assert json.loads(state.get_kv(write_status_kv_key("backup")) or "{}")["ok"] is True


# ---------------------------------------------------------------- audit (R2.12)

ICT = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh has no DST
HOLIDAY = Holiday(date(2026, 11, 24), "announced", source_url="https://vsdc.example.invalid/notice")


def _ict(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ICT).astimezone(timezone.utc)


def _audit_config(**changes: Any) -> CaptureConfig:
    return _config(holidays=(HOLIDAY,), **changes)


Window = tuple[str | None, datetime, datetime]


def _simulate(state: CaptureState, config: CaptureConfig, start: datetime, end: datetime, *, skip: Sequence[Window] = (),
              skew: float | None = 100.0, no_heartbeat: Sequence[tuple[datetime, datetime]] = ()) -> None:
    """Ok polls of every enabled source at each scheduled due time in [start, end] (none inside a `skip` window of that
    source, or of every source when the name is None), an hourly heartbeat, and the last alive tick at `end`."""
    schedule = Schedule.from_config(config)
    for cfg in config.enabled_sources:
        t = start
        while t <= end:
            if not any((name in (None, cfg.name)) and a <= t < b for name, a, b in skip):
                _poll(state, cfg.name, t, skew_ms=skew)
            t = schedule.next_due(t, cfg)
    beat = start
    while beat <= end:
        if not any(a <= beat < b for a, b in no_heartbeat):
            state.record_heartbeat(beat, "OK", "heartbeats/heartbeat-x.json", "0" * 64)
        beat += timedelta(hours=1)
    state.set_alive(end, 0.0, 4242)


def _snapshot(state: CaptureState, tmp_path: Path) -> Path:
    state.commit(T0)
    folder = tmp_path / "snapshot"
    folder.mkdir(exist_ok=True)
    path = folder / "state-x.sqlite"
    path.write_bytes(snapshot_state(state.path, tmp_path / "work.sqlite"))
    state.close()
    return path


def _run_audit(tmp_path: Path, config: CaptureConfig, first: date, last: date, *, days: str = "trading",
               build: Callable[[CaptureState], None]) -> AuditReport:
    state = _state(tmp_path)
    build(state)
    return audit(_snapshot(state, tmp_path), config, first, last, days=days)


def _verdicts(report: AuditReport) -> dict[str, tuple[bool, tuple[str, ...]]]:
    return {d.day.isoformat(): (d.passed, d.reasons) for d in report.days}


MON, TUE, WED = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)


def test_day_with_only_logged_outages_and_no_ok_poll_fails(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23), skip=[(HOSE, _ict(WED, 0), _ict(WED, 23, 59))])
        state.open_outage(OutageKind.SOURCE_REJECTED, HOSE, start_at=_ict(WED, 0), detected_at=_ict(WED, 0), detail=None)

    report = _run_audit(tmp_path, config, WED, WED, build=build)
    assert _verdicts(report) == {"2026-11-25": (False, (f"no_active_ok_poll:{HOSE}",))}, "logged, but nothing was captured"
    assert report.days[0].unlogged_gap_s == 0.0 and report.days[0].outages_by_kind == {"source_rejected": 1}


def test_trailing_silence_without_outage_fails_the_day(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 17), skip=[(RSS, _ict(WED, 12), _ict(WED, 18))])

    report = _run_audit(tmp_path, config, WED, WED, build=build)
    assert _verdicts(report) == {"2026-11-25": (False, (f"unlogged_gap:{RSS}",))}
    gap = report.gaps[0]
    assert gap.source == RSS and gap.start < _ict(WED, 12, 15) and gap.end == _ict(WED, 17) - timedelta(seconds=330)

    def never_polled(state: CaptureState) -> None:
        _simulate(state, _config(_portal(RSS, SourceKind.RSS_FEED), holidays=(HOLIDAY,)), _ict(MON, 0) - timedelta(days=1),
                  _ict(WED, 23))

    silent = _run_audit(tmp_path / "silent", config, WED, WED, build=never_polled)
    assert (f"unlogged_gap:{HOSE}" in silent.days[0].reasons) and not silent.days[0].passed, "a source never polled is a gap"


def test_audit_creates_no_file_next_to_the_snapshot(tmp_path: Path) -> None:
    config = _audit_config()
    state = _state(tmp_path)
    _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23))
    path = _snapshot(state, tmp_path)
    before = sorted(p.name for p in path.parent.iterdir())
    report = audit(path, config, MON, WED, days="all")
    assert sorted(p.name for p in path.parent.iterdir()) == before == ["state-x.sqlite"]
    assert [d.passed for d in report.days] == [True, True, True]


def test_audit_refuses_a_wal_mode_database(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.commit(T0)
    state.close()
    with pytest.raises(StorageError, match="snapshot from `backup`"):
        audit(tmp_path / "root" / "state" / "capture.sqlite", _audit_config(), MON, MON)


def test_unlogged_gap_fails_the_day(tmp_path: Path) -> None:
    config = _audit_config()
    hole = (RSS, _ict(WED, 10), _ict(WED, 11))

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23), skip=[hole])

    report = _run_audit(tmp_path, config, MON, WED, build=build)
    assert _verdicts(report) == {
        "2026-11-23": (True, ()), "2026-11-25": (False, (f"unlogged_gap:{RSS}",)),
    }, "the holiday 2026-11-24 is not evaluated in trading mode"
    wed = report.days[1]
    # last ok poll finished 09:57:05; deadline 10:00:05 -> 10:03:05 + 330 s = 10:08:35; next ok poll finished 11:00:05
    assert wed.unlogged_gap_s == 2760.0, "from 10:08:35 to 11:00:05 - 330 s = 10:54:35"


def _gap_with(outages: Sequence[tuple[OutageKind, str | None, datetime, datetime | None]]) -> Callable[[CaptureState], None]:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23), skip=[(RSS, _ict(WED, 10), _ict(WED, 11))])
        for kind, source, start, end in outages:
            outage = state.open_outage(kind, source, start_at=start, detected_at=start, detail=None)
            if end is not None:
                state.close_outage(outage, end)

    return build


def test_gap_covered_by_a_source_outage_passes(tmp_path: Path) -> None:
    covered = _gap_with([(OutageKind.SOURCE_UNREACHABLE, RSS, _ict(WED, 10), _ict(WED, 11))])
    assert _verdicts(_run_audit(tmp_path, _audit_config(), WED, WED, build=covered)) == {"2026-11-25": (True, ())}
    wrong_source = _gap_with([(OutageKind.SOURCE_UNREACHABLE, HOSE, _ict(WED, 10), _ict(WED, 11))])
    report = _run_audit(tmp_path / "other", _audit_config(), WED, WED, build=wrong_source)
    assert not report.days[0].passed, "another source's outage does not cover this source"
    still_open = _gap_with([(OutageKind.SOURCE_UNREACHABLE, RSS, _ict(WED, 10), None)])
    assert _run_audit(tmp_path / "open", _audit_config(), WED, WED, build=still_open).days[0].unlogged_gap_s == 0.0


def test_gap_covered_by_chained_outages_passes(tmp_path: Path) -> None:
    chained = _gap_with([
        (OutageKind.SOURCE_UNREACHABLE, RSS, _ict(WED, 10), _ict(WED, 10, 20)),
        (OutageKind.CLOCK_STEP, None, _ict(WED, 10, 20), _ict(WED, 10, 21)),
        (OutageKind.SOURCE_REJECTED, RSS, _ict(WED, 10, 21), _ict(WED, 11)),
    ])
    assert _run_audit(tmp_path, _audit_config(), WED, WED, build=chained).days[0].passed
    holed = _gap_with([
        (OutageKind.SOURCE_UNREACHABLE, RSS, _ict(WED, 10), _ict(WED, 10, 20)),
        (OutageKind.SOURCE_REJECTED, RSS, _ict(WED, 10, 21), _ict(WED, 11)),
    ])
    report = _run_audit(tmp_path / "holed", _audit_config(), WED, WED, build=holed)
    assert not report.days[0].passed and report.days[0].unlogged_gap_s == 60.0


def test_process_down_covers_every_source(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23), skip=[(None, _ict(WED, 10), _ict(WED, 11))])
        outage = state.open_outage(OutageKind.PROCESS_DOWN, None, start_at=_ict(WED, 9, 58), detected_at=_ict(WED, 11),
                                   detail=None)
        state.close_outage(outage, _ict(WED, 11))

    report = _run_audit(tmp_path, config, WED, WED, build=build)
    assert _verdicts(report) == {"2026-11-25": (True, ())}
    assert report.days[0].outages_by_kind == {"process_down": 1}


def test_0800_switch_is_not_a_gap(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23))

    report = _run_audit(tmp_path, config, WED, WED, build=build)
    assert _verdicts(report) == {"2026-11-25": (True, ())}
    state = StateReader.open_snapshot(tmp_path / "snapshot" / "state-x.sqlite")
    try:
        starts = [p.started_at for p in state.polls(RSS, since=_ict(WED, 6, 59), until=_ict(WED, 8, 4))]
    finally:
        state.close()
    assert starts == [_ict(WED, 7), _ict(WED, 8), _ict(WED, 8, 3)], "hourly until 07:00, then 08:00 at once, then 3 min"


def test_missed_active_polls_after_0800_are_a_gap(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23),
                  skip=[(RSS, _ict(WED, 7, 30), _ict(WED, 8, 30))])

    report = _run_audit(tmp_path, config, WED, WED, build=build)
    assert _verdicts(report) == {"2026-11-25": (False, (f"unlogged_gap:{RSS}",))}
    gap = report.gaps[0]
    assert (gap.start, gap.end) == (_ict(WED, 8, 3) + timedelta(seconds=330), _ict(WED, 8, 30) + timedelta(seconds=5 - 330))


def test_holiday_hourly_cadence_is_not_a_gap(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23))

    report = _run_audit(tmp_path, config, MON, WED, days="all", build=build)
    assert _verdicts(report) == {"2026-11-23": (True, ()), "2026-11-24": (True, ()), "2026-11-25": (True, ())}
    tue = report.days[1]
    # hourly from 00:00, plus the 17:30 boundary: 00:00-17:00, 17:30, 18:30-23:30
    assert not tue.trading_day and tue.ok_polls[RSS] == tue.expected_polls[RSS] == 25
    assert report.days[0].ok_polls[RSS] > 150, "a trading day polls every 3 min from 08:00 to 17:30"


def test_day_without_heartbeat_fails(tmp_path: Path) -> None:
    config = _audit_config()

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(WED, 23),
                  no_heartbeat=[(_ict(WED, 0), _ict(WED, 23, 59))])

    report = _run_audit(tmp_path, config, MON, WED, build=build)
    assert _verdicts(report) == {"2026-11-23": (True, ()), "2026-11-25": (False, ("no_heartbeat",))}


def test_skew_bounds_and_no_date_evidence(tmp_path: Path) -> None:
    config = _audit_config()
    start = _ict(MON, 0) - timedelta(days=1)

    def skewed(skew: float | None) -> Callable[[CaptureState], None]:
        def build(state: CaptureState) -> None:
            _simulate(state, config, start, _ict(MON, 23), skew=skew)
        return build

    assert _run_audit(tmp_path / "ok", config, MON, MON, build=skewed(-1999.0)).days[0].passed
    assert _run_audit(tmp_path / "median", config, MON, MON, build=skewed(2001.0)).days[0].reasons == ("skew_median",)
    assert _run_audit(tmp_path / "none", config, MON, MON, build=skewed(None)).days[0].reasons == ("no_skew_evidence",)

    def one_outlier(state: CaptureState) -> None:
        _simulate(state, config, start, _ict(MON, 23))
        _poll(state, RSS, _ict(MON, 12, 1), skew_ms=-60_001.0)

    outlier = _run_audit(tmp_path / "max", config, MON, MON, build=one_outlier).days[0]
    assert outlier.reasons == ("skew_max",) and outlier.skew_ms_max_abs == 60_001.0


def test_five_consecutive_trading_days_across_a_weekend_and_2026_11_24(tmp_path: Path) -> None:
    config = _audit_config()
    last = date(2026, 11, 30)  # Monday; trading days: 23, 25, 26, 27, 30 (24 is a holiday, 28-29 a weekend)

    def build(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(last, 23, 59))

    report = _run_audit(tmp_path, config, MON, last, build=build)
    assert [d.day.day for d in report.days] == [23, 25, 26, 27, 30] and all(d.passed for d in report.days)
    assert report.longest_consecutive_passing_trading_days == 5
    data = report.to_dict()
    assert data["longest_consecutive_passing_trading_days"] == 5 and len(data["days"]) == 5
    json.dumps(data)

    def broken(state: CaptureState) -> None:
        _simulate(state, config, _ict(MON, 0) - timedelta(days=1), _ict(last, 23, 59),
                  no_heartbeat=[(_ict(date(2026, 11, 26), 0), _ict(date(2026, 11, 27), 0))])

    assert _run_audit(tmp_path / "broken", config, MON, last, build=broken).longest_consecutive_passing_trading_days == 2
