"""Capture CLI, host integration and the config loader (plan 261022-1147 phase 2, R2.13-R2.15; red-team r1 LT4, LT7, LT10).

`main` runs in-process with an injected environment and a `CliFactory` of fakes (clock, sleeper, sources, probe runner,
keep-awake): no network, no real clock, no sleep, no vnstock. Directories live under `tmp_path`; the work-tree tests run
`git init` there. Payloads are synthetic (made-up tickers and titles).
"""
from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Sequence

import pytest

from finsec_alpharadar.capture.__main__ import CliFactory, main
from finsec_alpharadar.capture.config import (
    CaptureConfig,
    CaptureConfigError,
    SourceKind,
    load_config_file,
)
from finsec_alpharadar.capture.heartbeat import CANARY_BASELINE_KV_KEY
from finsec_alpharadar.capture.host import AlreadyRunning, KeepAwake, SingleInstanceLock
from finsec_alpharadar.capture.record import OutageKind, RecordType
from finsec_alpharadar.capture.sources import CaptureSource, HttpRequest, HttpResponse, RssFeedSource, VnstockEventsSource
from finsec_alpharadar.capture.storage import CaptureState, RawArchive

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "configs" / "capture.toml"
T0 = datetime(2026, 10, 27, 3, 0, 0, tzinfo=timezone.utc)  # 10:00 ICT, a Tuesday
RSS = "hnx_rss_issuers"

TEST_TOML = """
[schedule]
active_start = 08:00:00
active_end = 17:30:00
loop_tick_s = 30
max_poll_duration_s = 330
clock_step_threshold_s = 2
heartbeat_every_s = 900
watchdog_limit_s = 900
backup_at_local = 07:15:00

[[holidays]]
date = 2026-11-24
status = "announced"
source_url = "https://notice.example.invalid/1"

[[sources]]
name = "hnx_rss_issuers"
kind = "{kind}"
enabled = true
url = "https://portal-b.example.invalid/3/vi_vn/feed.rss"
active_interval_s = 180
idle_interval_s = 3600
timeout_s = 90
deadline_s = 150
retries = 1
max_body_bytes = 2000000

[heartbeat]
canary_files = ["AGENTS.md", "CLAUDE.md"]
stripchart_host = "ntp.example.invalid"
"""


# ---------------------------------------------------------------- fakes


class FakeClock:
    def __init__(self, now: datetime = T0, mono: float = 1000.0) -> None:
        self._now, self._mono = now, mono

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


@dataclass
class FakeSleeper:
    clock: FakeClock
    calls: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class RssFetcher:
    """Answers every request with a synthetic RSS page of the given guids."""

    def __init__(self, clock: FakeClock, guids: Sequence[str]) -> None:
        self.clock, self.guids, self.requests = clock, list(guids), 0

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests += 1
        items = "".join(f"<item><title>AAA: Thông báo bịa {g}</title><guid>{g}</guid></item>" for g in self.guids)
        body = f"<rss><channel>{items}</channel></rss>".encode("utf-8")
        at = self.clock.now()
        headers = {"date": format_datetime(at, usegmt=True), "content-type": "application/rss+xml"}
        return HttpResponse(status=200, headers=headers, body=body, received_at=at, rtt_ms=100.0)


def _runner(argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    return 1, "", "not available in tests"


@dataclass
class Rig:
    tmp_path: Path
    clock: FakeClock
    env: dict[str, str]
    config_path: Path
    factory: CliFactory
    fetcher: RssFetcher

    @property
    def data_root(self) -> Path:
        return Path(self.env["FINSEC_DATA_ROOT"])

    @property
    def backup_dir(self) -> Path:
        return Path(self.env["FINSEC_BACKUP_DIR"])

    def main(self, *argv: str, env: dict[str, str] | None = None) -> int:
        return main([*argv, "--config", str(self.config_path)], env=self.env if env is None else env, factory=self.factory)

    def lock(self) -> SingleInstanceLock:
        return SingleInstanceLock(self.data_root / "state" / "capture.lock")


def _rig(tmp_path: Path, *, kind: str = "rss_feed", guids: Sequence[str] = ("101", "102")) -> Rig:
    data_root, backup_dir, clone = tmp_path / "data", tmp_path / "sync", tmp_path / "clone"
    for directory in (data_root, backup_dir, clone):
        directory.mkdir(parents=True, exist_ok=True)
    (clone / "AGENTS.md").write_bytes(b"# contract\n")
    (clone / "CLAUDE.md").write_bytes(b"@AGENTS.md\n")
    config_path = tmp_path / "capture.toml"
    config_path.write_text(TEST_TOML.replace("{kind}", kind), encoding="utf-8")
    clock = FakeClock()
    fetcher = RssFetcher(clock, guids)

    def sources(config: CaptureConfig, clk: Any, sleeper: Any) -> dict[str, CaptureSource]:
        return {cfg.name: RssFeedSource(cfg, fetcher) for cfg in config.enabled_sources}

    factory = CliFactory(
        clock=clock, sleeper=FakeSleeper(clock), sources=sources, run_command=_runner, keep_awake=_no_keep_awake,
        clone_root=clone, pid=4242,
    )
    env = {"FINSEC_DATA_ROOT": str(data_root), "FINSEC_BACKUP_DIR": str(backup_dir)}
    return Rig(tmp_path, clock, env, config_path, factory, fetcher)


class _NoKeepAwake:
    def __enter__(self) -> _NoKeepAwake:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _no_keep_awake() -> _NoKeepAwake:
    return _NoKeepAwake()


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed with exit {result.returncode}: {result.stderr.strip()}")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_bytes(b"/data/\n")
    return repo


# ---------------------------------------------------------------- config (R2.15)


def test_shipped_config_loads_and_respects_d5(tmp_path: Path) -> None:
    config = load_config_file(SHIPPED_CONFIG)
    assert config.data_root is None and config.backup_dir is None, "the loader reads no environment"
    assert [s.name for s in config.sources] == ["hose_news_api", "hnx_rss_issuers", "vnstock_events"]
    for source in config.sources:
        assert 120 <= source.active_interval_s <= 300 and source.idle_interval_s == 3600, source.name
    hose, hnx, vnstock = config.sources
    assert hose.kind is SourceKind.HOSE_NEWS_API and hose.url == "https://api.hsx.vn/n/api/v1/1/news/securitiesType/0"
    assert (hose.page_size, hose.max_pages, hose.timeout_s, hose.deadline_s, hose.retries) == (20, 2, 30, 60, 1)
    assert set(hose.headers) == {"Origin", "Referer", "Accept"}
    assert hnx.kind is SourceKind.RSS_FEED and hnx.url == "https://hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss"
    assert (hnx.timeout_s, hnx.deadline_s, hnx.retries) == (90, 150, 1)
    assert vnstock.vnstock_source == "vci" and len(vnstock.symbols) == 30 and vnstock.symbols_per_poll == 30
    assert vnstock.calls_per_min == 60 and vnstock.release_expires_on == date(2027, 10, 17)
    assert vnstock.error_map == {
        "VNSTOCK_FEATURE_REMOVED": OutageKind.VENDOR_UNAVAILABLE,
        "VNSTOCK_KBS_COMPANY_SHAPE": OutageKind.SOURCE_INVALID_PAYLOAD,
        "VNSTOCK_RELEASE_EXPIRED": OutageKind.VENDOR_RELEASE_EXPIRED,
    }
    assert vnstock.symbol_error_codes == ("VNSTOCK_INPUT_SYMBOL_UNKNOWN",)
    schedule = config.schedule
    assert (schedule.active_start, schedule.active_end, schedule.backup_at_local) == (time(8), time(17, 30), time(7, 15))
    assert (schedule.loop_tick_s, schedule.max_poll_duration_s, schedule.heartbeat_every_s) == (30, 330, 900)
    assert hnx.deadline_s * 2 + 5 <= schedule.max_poll_duration_s, "one HNX retry fits the poll budget"
    assert [h.day for h in config.holidays] == [date(2026, 11, 24)]
    assert config.heartbeat.canary_files == ("AGENTS.md", "CLAUDE.md")

    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    assert text.count("active_interval_s = 180") == 3
    broken = text.replace("active_interval_s = 180", "active_interval_s = 301", 1)
    with pytest.raises(CaptureConfigError, match="D5"):
        load_config_file(_write(tmp_path / "broken.toml", broken))


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_source_kind_is_a_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = _rig(tmp_path, kind="ftp_feed")
    with pytest.raises(CaptureConfigError, match="unknown kind"):
        load_config_file(rig.config_path)
    assert rig.main("once") == 2
    assert "unknown kind" in capsys.readouterr().err
    typo = _write(tmp_path / "typo.toml", TEST_TOML.replace("{kind}", "rss_feed").replace("retries = 1", "retrys = 1"))
    with pytest.raises(CaptureConfigError, match="retrys"):
        load_config_file(typo)
    garbled = _write(tmp_path / "garbled.toml", "[schedule\n")
    with pytest.raises(CaptureConfigError):
        load_config_file(garbled)
    assert main(["check-root", "--config", str(tmp_path / "missing.toml")], env=rig.env, factory=rig.factory) == 2


def test_missing_backup_dir_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = _rig(tmp_path)
    env = {"FINSEC_DATA_ROOT": rig.env["FINSEC_DATA_ROOT"]}
    for argv in (["run"], ["once"], ["heartbeat"], ["backup", "--day", "2026-10-26"]):
        assert rig.main(*argv, env=env) == 2, argv
    assert "FINSEC_BACKUP_DIR" in capsys.readouterr().err
    assert rig.main("check-root", env=env) == 0, "check-root needs only the data root"
    assert rig.main("once", env={**rig.env, "FINSEC_BACKUP_DIR": str(tmp_path / "absent")}) == 2, "the folder must exist"
    assert not (rig.data_root / "raw").exists(), "nothing was captured"


def test_check_root_refuses_an_unignored_root_in_a_work_tree_exit_4(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    repo = _repo(tmp_path)
    (repo / "capture-root").mkdir()
    (repo / "data" / "capture").mkdir(parents=True)
    assert rig.main("check-root", env={"FINSEC_DATA_ROOT": str(repo / "capture-root")}) == 4
    assert rig.main("check-root", env={"FINSEC_DATA_ROOT": str(repo / "data" / "capture")}) == 0, "git ignores /data/"
    assert rig.main("check-root", env={}) == 2
    assert rig.main("check-root", env={"FINSEC_DATA_ROOT": "relative/root"}) == 2


def test_backup_dir_inside_an_unignored_work_tree_exits_4(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    repo = _repo(tmp_path)
    (repo / "sync").mkdir()
    env = {**rig.env, "FINSEC_BACKUP_DIR": str(repo / "sync")}
    for argv in (["once"], ["heartbeat"], ["run"]):
        assert rig.main(*argv, env=env) == 4, argv
    assert rig.main("check-root", env=env) == 4, "check-root also checks a set FINSEC_BACKUP_DIR"


# ---------------------------------------------------------------- commands (R2.14)


def test_once_writes_records_through_injected_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = _rig(tmp_path)
    assert rig.main("once", "--all") == 0
    out = capsys.readouterr().out
    assert f"{RSS} status=ok outage=- items_total=2 items_new=2 pages=1" in out
    assert "Thông báo" not in out and "AAA" not in out, "the summary carries counts, never titles"
    records = [r for p in sorted((rig.data_root / "raw" / RSS).glob("*.jsonl")) for r in RawArchive.read_records(p)]
    assert sorted(r.source_id for r in records if r.record_type is RecordType.ITEM) == ["101", "102"]
    with CaptureState(rig.data_root / "state" / "capture.sqlite") as state:
        poll = state.last_poll(RSS)
        assert poll is not None and (poll.run_mode, poll.pid, poll.status) == ("once", 4242, "ok")
    assert (rig.data_root / "ops" / "capture.log").is_file()
    with rig.lock():
        pass  # the command released the lock
    assert rig.main("once") == 0 and rig.fetcher.requests == 1, "without --all a source that is not due is skipped"


def test_second_instance_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = _rig(tmp_path)
    with rig.lock():
        with pytest.raises(AlreadyRunning):
            rig.lock().acquire()
        assert rig.main("run") == 3
    assert "already running" in capsys.readouterr().err
    assert rig.fetcher.requests == 0


def test_once_heartbeat_and_backup_exit_3_while_run_holds_the_lock(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    with rig.lock():
        for argv in (["once"], ["once", "--all"], ["heartbeat"], ["backup", "--day", "2026-10-26"]):
            assert rig.main(*argv) == 3, argv
    assert rig.fetcher.requests == 0 and not (rig.backup_dir / "heartbeat.json").exists()


def test_canary_reset_runs_while_run_holds_the_lock(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    (rig.data_root / "state").mkdir()
    with rig.lock(), CaptureState(rig.data_root / "state" / "capture.sqlite") as running:
        assert rig.main("canary-reset", env={"FINSEC_DATA_ROOT": rig.env["FINSEC_DATA_ROOT"]}) == 0
        stored = json.loads(running.get_kv(CANARY_BASELINE_KV_KEY) or "{}")
    assert stored["at"] == "2026-10-27T03:00:00.000000Z" and set(stored["files"]) == {"AGENTS.md", "CLAUDE.md"}


def test_heartbeat_backup_and_audit_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = _rig(tmp_path)
    assert rig.main("once", "--all") == 0
    assert rig.main("heartbeat") == 0
    beat = json.loads((rig.backup_dir / "heartbeat.json").read_text(encoding="utf-8"))
    assert beat["process"]["pid"] == 4242 and beat["sources"][RSS]["polls_24h_ok"] == 1
    assert rig.main("backup", "--day", "2026-10-27") == 2, "today's UTC day is still open"
    assert rig.main("backup", "--day", "26/10/2026") == 2
    assert rig.main("backup", "--day", "2026-10-26") == 0
    snapshot = rig.backup_dir / "daily" / "2026-10-26" / "state-2026-10-26.sqlite"
    assert snapshot.is_file()

    capsys.readouterr()
    report = tmp_path / "report.json"
    base = ["audit", "--state", str(snapshot), "--from", "2026-10-27", "--to", "2026-10-27", "--days", "all"]
    assert main([*base, "--require-days", "0", "--out", str(report), "--config", str(rig.config_path)], env={}) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["longest_consecutive_passing_trading_days"] == 0
    assert "2026-10-27" in capsys.readouterr().out
    assert main([*base, "--config", str(rig.config_path)], env={}) == 1, "the default needs 5 passing days"
    live = rig.data_root / "state" / "capture.sqlite"
    assert main(["audit", "--state", str(live), "--from", "2026-10-27", "--to", "2026-10-27", "--config",
                 str(rig.config_path)], env={}) == 2, "the live WAL state is refused"
    assert main(["audit", "--config", str(rig.config_path)], env={}) == 2, "missing arguments are a usage error"


def test_run_takes_the_canary_baseline_and_polls_until_stopped(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    released = threading.Event()
    exits: list[int] = []
    cycles = [0]

    def stop() -> bool:
        cycles[0] += 1
        return cycles[0] > 3

    rig.factory.stop = stop
    rig.factory.watchdog_sleep = lambda seconds: released.wait()
    rig.factory.watchdog_exit = exits.append
    try:
        assert rig.main("run") == 0
    finally:
        released.set()
    with CaptureState(rig.data_root / "state" / "capture.sqlite") as state:
        baseline = json.loads(state.get_kv(CANARY_BASELINE_KV_KEY) or "{}")
        poll = state.last_poll(RSS)
        heartbeats = state.connection.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
    assert baseline["at"] == "2026-10-27T03:00:00.000000Z", "the baseline is taken when run starts"
    assert poll is not None and poll.run_mode == "run" and heartbeats == 1
    assert (rig.backup_dir / "heartbeat.json").is_file() and exits == []


def test_build_sources_passes_the_poll_budget_to_vnstock(tmp_path: Path) -> None:
    from finsec_alpharadar.capture.poller import build_sources

    config = load_config_file(SHIPPED_CONFIG)
    clock = FakeClock()
    sources = build_sources(config, clock=clock, sleeper=FakeSleeper(clock), call_events=lambda s, v: ([], {}))
    vnstock = sources["vnstock_events"]
    assert isinstance(vnstock, VnstockEventsSource) and vnstock._budget_s == config.schedule.max_poll_duration_s


# ---------------------------------------------------------------- host (R2.13)


def test_keep_awake_calls_the_setter_with_es_flags() -> None:
    calls: list[int] = []

    def setter(flags: int) -> int:
        calls.append(flags)
        return 0x80000000

    with KeepAwake(setter=setter, platform="win32"):
        assert calls == [0x80000001], "ES_CONTINUOUS | ES_SYSTEM_REQUIRED on entry"
    assert calls == [0x80000001, 0x80000000], "ES_CONTINUOUS alone on exit"


def test_keep_awake_is_a_no_op_off_windows() -> None:
    calls: list[int] = []
    with KeepAwake(setter=calls.append, platform="linux"):
        pass
    assert calls == []


def test_single_instance_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "state" / "capture.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    with pytest.raises(AlreadyRunning):
        SingleInstanceLock(path).acquire()
    first.release()
    with SingleInstanceLock(path):
        pass
