"""Capture poller (`finsec_alpharadar.capture.poller`, plan 261022-1147 phase 2, R2.6, R2.7, R2.9; red-team r1 RT2, RT11,
RT12, LT1, LT3).

Everything runs on a FakeClock (wall and monotonic advance together unless a test steps one of them), a recording
sleeper, scripted fetchers and `tmp_path`. Synthetic payloads only (made-up tickers AAA, BBB, made-up titles). No network,
no real clock, no sleep: the one cross-thread test synchronises with bounded `threading.Event.wait` calls.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

import pytest

from finsec_alpharadar.capture.config import CaptureConfig, HeartbeatConfig, ScheduleConfig, SourceConfig, SourceKind
from finsec_alpharadar.capture.poller import (
    WATCHDOG_EXIT_CODE,
    Poller,
    TokenBucket,
    Watchdog,
    build_sources,
    component_status_kv_key,
    cursor_kv_key,
)
from finsec_alpharadar.capture.record import (
    CaptureRecord,
    OutageKind,
    PitQuality,
    RecordType,
    detail_json,
    sha256_hex,
)
from finsec_alpharadar.capture.sources import (
    CaptureSource,
    HoseNewsApiSource,
    HttpRequest,
    HttpResponse,
    PollContext,
    PollResult,
    RssFeedSource,
    SourceError,
    UrllibFetcher,
    VnstockEventsSource,
)
from finsec_alpharadar.capture.storage import BlobStore, CaptureState, RawArchive, snapshot_kv_key

T0 = datetime(2026, 10, 27, 3, 0, 0, tzinfo=timezone.utc)  # 10:00 ICT on a Tuesday: inside the active window
VERSION = "0.1.0+0123456789ab"
HOSE_URL = "https://portal-a.example.invalid/n/api/v1/1/news/securitiesType/0"
RSS_URL = "https://portal-b.example.invalid/3/vi_vn/feed.rss"
UNREACHABLE = OutageKind.SOURCE_UNREACHABLE


# ---------------------------------------------------------------- fakes


class FakeClock:
    """Wall and monotonic time advance together; `step_wall` / `jump_mono` move only one of them."""

    def __init__(self, now: datetime = T0, mono: float = 1000.0) -> None:
        self._now, self._mono = now, mono

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    def step_wall(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class FakeSleeper:
    """Records every requested sleep and advances the fake clock by it (time passes, nothing waits)."""

    clock: FakeClock
    calls: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


Answer = bytes | int | SourceError | Callable[[HttpRequest], HttpResponse]


class ScriptedFetcher:
    """Answers each request with the next scripted answer: a 200 body, a status code, a raised `SourceError`, or a
    callable. `received_at` and the `Date` header come from the fake clock; `advance_s` is the time one request takes."""

    def __init__(self, clock: FakeClock, *answers: Answer, advance_s: float = 0.0) -> None:
        self.clock = clock
        self.answers: list[Answer] = list(answers)
        self.requests: list[HttpRequest] = []
        self.advance_s = advance_s

    def add(self, *answers: Answer) -> None:
        self.answers.extend(answers)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        assert self.answers, "the poller sent a request the test did not script"
        answer = self.answers.pop(0)
        if callable(answer) and not isinstance(answer, (bytes, int)):
            return answer(request)
        if isinstance(answer, SourceError):
            raise answer
        self.clock.advance(self.advance_s)
        status, body = (answer, b"") if isinstance(answer, int) else (200, answer)
        at = self.clock.now()
        headers = {"date": format_datetime(at, usegmt=True), "content-type": "application/rss+xml"}
        return HttpResponse(status=status, headers=headers, body=body, received_at=at, rtt_ms=100.0)


def _unreachable() -> SourceError:
    return SourceError(UNREACHABLE, detail_json(reason="reset"))


def _rss(guids: list[str], *, edition: str = "") -> bytes:
    items = "".join(
        f"<item><title>AAA: Thông báo {g}{edition}</title><guid isPermaLink=\"false\">{g}</guid></item>" for g in guids
    )
    return f"<?xml version=\"1.0\" encoding=\"utf-8\"?><rss><channel>{items}</channel></rss>".encode("utf-8")


def _hose(ids: list[int]) -> bytes:
    rows = [{"id": n, "title": f"BBB: Nghị quyết số {n}", "postedDate": 1793000000 + n} for n in ids]
    return json.dumps({"data": {"list": rows}, "success": True}, ensure_ascii=False).encode("utf-8")


def _schedule(**changes: Any) -> ScheduleConfig:
    fields: dict[str, Any] = {
        "active_start": time(8, 0), "active_end": time(17, 30), "loop_tick_s": 30.0, "max_poll_duration_s": 330.0,
        "clock_step_threshold_s": 2.0, "heartbeat_every_s": 900.0, "watchdog_limit_s": 900.0,
        "backup_at_local": time(7, 15),
    }
    fields.update(changes)
    return ScheduleConfig(**fields)


def _rss_cfg(name: str = "hnx_rss_issuers", **changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": name, "kind": SourceKind.RSS_FEED, "enabled": True, "url": RSS_URL, "active_interval_s": 180.0,
        "idle_interval_s": 3600.0, "timeout_s": 90.0, "deadline_s": 150.0, "retries": 1, "max_body_bytes": 2_000_000,
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _hose_cfg(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "hose_news_api", "kind": SourceKind.HOSE_NEWS_API, "enabled": True, "url": HOSE_URL,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 30.0, "deadline_s": 60.0, "retries": 1,
        "max_body_bytes": 2_000_000, "page_size": 20, "max_pages": 2, "min_request_spacing_s": 1.0,
        "headers": {"Origin": "https://portal-a.example.invalid"},
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _vnstock_cfg(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "vnstock_events", "kind": SourceKind.VNSTOCK_EVENTS, "enabled": True, "url": None,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 60.0, "deadline_s": 60.0, "retries": 0,
        "max_body_bytes": 2_000_000, "vnstock_source": "vci", "symbols": ("AAA", "BBB", "CCC", "DDD"),
        "symbols_per_poll": 2, "calls_per_min": 60.0, "vendor_call_timeout_s": 60.0,
        "error_map": {"VENDOR_FEATURE_GONE": "vendor_unavailable"},
        "quota_warning_patterns": ("truncat", "quota"),
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _config(*sources: SourceConfig, **schedule: Any) -> CaptureConfig:
    return CaptureConfig(
        schedule=_schedule(**schedule), holidays=(), sources=sources, heartbeat=HeartbeatConfig(canary_files=("AGENTS.md",))
    )


def _rel(path: Path) -> str:
    return "/".join(path.parts[-3:])


class RecordingSyncer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __call__(self, handle: BinaryIO, path: Path) -> None:
        handle.flush()
        self.events.append(f"fsync:{_rel(path)}")


class RecordingArchive(RawArchive):
    def __init__(self, root: Path, events: list[str]) -> None:
        super().__init__(root, syncer=RecordingSyncer(events))
        self.events = events

    def append(self, record: CaptureRecord) -> Path:
        path = super().append(record)
        self.events.append(f"append:{_rel(path)}")
        return path


class RecordingState(CaptureState):
    def __init__(self, path: Path, events: list[str]) -> None:
        super().__init__(path)
        self.events = events

    def commit(self, at: datetime) -> None:
        self.events.append("commit")
        super().commit(at)


class Rig:
    """One poller process over `root`: a new Rig on the same root and clock is a restart."""

    def __init__(
        self,
        root: Path,
        clock: FakeClock,
        config: CaptureConfig,
        sources: dict[str, CaptureSource],
        *,
        run_mode: str = "run",
        pid: int = 4242,
        **extra: Any,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.clock, self.events = root, clock, []
        self.archive = RecordingArchive(root, self.events)
        self.blobs = BlobStore(root, syncer=RecordingSyncer(self.events))
        self.state = RecordingState(root / "state" / "capture.sqlite", self.events)
        self.sleeper = FakeSleeper(clock)
        self.poller = Poller(
            config, sources, archive=self.archive, blobs=self.blobs, state=self.state, clock=clock,
            sleeper=self.sleeper, run_mode=run_mode, pid=pid, capture_version=VERSION, **extra,
        )

    def crash(self) -> None:
        """The process dies: open handles and the uncommitted transaction are lost."""
        self.state.close()

    def records(self, source: str) -> list[CaptureRecord]:
        directory = self.root / "raw" / source
        paths = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
        return [record for path in paths for record in RawArchive.read_records(path)]

    def items(self, source: str) -> list[CaptureRecord]:
        return [r for r in self.records(source) if r.record_type is RecordType.ITEM]

    def snapshots(self, source: str) -> list[CaptureRecord]:
        return [r for r in self.records(source) if r.record_type is RecordType.SNAPSHOT]

    def pit(self, source: str) -> dict[str, PitQuality]:
        return {r.source_id: r.pit_quality for r in self.items(source)}

    def outages(self, kind: OutageKind | None = None) -> list[Any]:
        rows = self.state.connection.execute("SELECT id FROM outages ORDER BY id").fetchall()
        found = [self.state.outage(int(row[0])) for row in rows]
        return [o for o in found if kind is None or o.kind is kind]


def _rss_rig(
    root: Path, clock: FakeClock, *answers: Answer, cfg: SourceConfig | None = None, schedule: dict[str, Any] | None = None,
    **poller: Any,
) -> tuple[Rig, ScriptedFetcher]:
    cfg = cfg or _rss_cfg()
    fetcher = ScriptedFetcher(clock, *answers)
    return Rig(root, clock, _config(cfg, **(schedule or {})), {cfg.name: RssFeedSource(cfg, fetcher)}, **poller), fetcher


RSS = "hnx_rss_issuers"


# ---------------------------------------------------------------- pit_quality (R2.6)


def test_first_ever_poll_marks_items_late(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, _ = _rss_rig(tmp_path, clock, _rss(["101", "102"]))
    rig.poller.startup()
    report = rig.poller.run_cycle()

    assert [p.source for p in report.polls] == [RSS] and report.polls[0].status == "ok"
    items = rig.items(RSS)
    assert sorted(r.source_id for r in items) == ["101", "102"]
    assert {r.pit_quality for r in items} == {PitQuality.LATE}, "nothing was watched before the first poll"
    assert all(r.information_time == r.first_seen_at == T0 and r.capture_version == VERSION for r in items)
    assert [s.pit_quality for s in rig.snapshots(RSS)] == [PitQuality.LATE]
    poll = rig.state.last_poll(RSS)
    assert poll is not None and (poll.items_total, poll.items_new, poll.items_revised, poll.pages) == (2, 2, 0, 1)


def test_steady_state_items_are_first_seen(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, fetcher = _rss_rig(tmp_path, clock, _rss(["101", "102"]), _rss(["103", "101", "102"]))
    rig.poller.startup()
    rig.poller.run_cycle()

    clock.advance(60)
    assert rig.poller.run_cycle().polls == (), "not due yet: 60 s after a poll in the active window"
    assert len(fetcher.requests) == 1

    clock.advance(120)
    rig.poller.run_cycle()
    assert rig.pit(RSS) == {"101": PitQuality.LATE, "102": PitQuality.LATE, "103": PitQuality.FIRST_SEEN}
    poll = rig.state.last_poll(RSS)
    assert poll is not None and (poll.items_total, poll.items_new, poll.items_revised) == (3, 1, 0)
    # Header arrival, not the request midpoint: HNX holds the headers for 40-49 s and stamps Date when it sends them
    # (OBSERVED 2026-10-27), so a midpoint would read half the response time as clock skew.
    assert poll.skew_ms == pytest.approx(0.0), "header arrival (100 ms rtt) minus the Date header"

    clock.advance(180)
    fetcher.add(_rss(["103", "101", "102"], edition=" (sửa)"))  # every payload changed: three revisions
    rig.poller.run_cycle()
    poll = rig.state.last_poll(RSS)
    assert poll is not None and (poll.items_new, poll.items_revised) == (3, 3)
    assert len(rig.items(RSS)) == 6


def test_items_after_a_source_outage_are_late_once(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, fetcher = _rss_rig(tmp_path, clock, _rss(["101"]))
    rig.poller.startup()
    rig.poller.run_cycle()

    clock.advance(180)
    fetcher.add(_unreachable(), _unreachable())
    failed_at = clock.now()
    rig.poller.run_cycle()
    [outage] = rig.outages()
    assert (outage.kind, outage.source, outage.start_at, outage.end_at) == (UNREACHABLE, RSS, failed_at, None)

    clock.advance(180)
    fetcher.add(_rss(["101", "102"]))
    fetcher.advance_s = 2.0  # the recovering response arrives 2 s after the poll started
    rig.poller.run_cycle()
    assert rig.pit(RSS)["102"] is PitQuality.LATE, "the outage intersects (last ok, t]"
    [closed] = rig.outages()
    assert closed.end_at == rig.items(RSS)[-1].first_seen_at, "closed at the first_seen_at of the recovering poll"

    clock.advance(180)
    fetcher.add(_rss(["101", "102", "103"]))
    rig.poller.run_cycle()
    assert rig.pit(RSS)["103"] is PitQuality.FIRST_SEEN, "late once, then first_seen again"


def test_items_of_a_failed_poll_are_archived_late(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, fetcher = _rss_rig(tmp_path, clock, _rss(["101"]), _rss(["101", "102"]))
    rig.poller.startup()
    rig.poller.run_cycle()
    clock.advance(180)
    rig.poller.run_cycle()
    assert rig.pit(RSS)["102"] is PitQuality.FIRST_SEEN

    clock.advance(180)
    fetcher.add(_rss(["201", "202"]))  # no overlap with anything seen: coverage overflow, but the items are real
    report = rig.poller.run_cycle()
    assert (report.polls[0].status, report.polls[0].outage_kind) == ("error", OutageKind.COVERAGE_OVERFLOW)
    assert (rig.pit(RSS)["201"], rig.pit(RSS)["202"]) == (PitQuality.LATE, PitQuality.LATE), "the poll's own outage"
    [overflow] = rig.state.open_outages(RSS)
    assert overflow.kind is OutageKind.COVERAGE_OVERFLOW


# ---------------------------------------------------------------- outages (R2.7)


def test_process_down_is_logged_from_the_last_alive_tick(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, _ = _rss_rig(tmp_path, clock, _rss(["101"]))
    rig.poller.startup()
    rig.poller.run_cycle()
    alive = rig.state.get_alive()
    assert alive is not None and alive.pid == 4242
    torn = b'{"schema_version":1,"record_type":"it'
    with (tmp_path / "raw" / RSS / "2026-10-27.jsonl").open("ab") as handle:
        handle.write(torn)  # power loss in the middle of a line
    rig.crash()

    clock.advance(3600)
    restarted, _ = _rss_rig(tmp_path, clock, _rss(["101", "102"]))
    restarted.poller.startup()
    [down] = restarted.outages(OutageKind.PROCESS_DOWN)
    assert (down.source, down.start_at, down.end_at, down.detected_at) == (None, alive.last_alive_at, clock.now(), clock.now())
    detail = json.loads(down.detail)
    assert detail["bytes_cut"] == len(torn) and detail["down_s"] == pytest.approx(3600.0)
    restarted.poller.run_cycle()
    assert restarted.pit(RSS)["102"] is PitQuality.LATE
    assert len(restarted.items(RSS)) == 2, "the torn tail was cut and the archived item was not written twice"
    restarted.crash()

    clock.advance(60)  # a quick restart is not an outage: 60 s < 2 x 30 s + 330 s
    quick, _ = _rss_rig(tmp_path, clock)
    quick.poller.startup()
    assert len(quick.outages(OutageKind.PROCESS_DOWN)) == 1


def test_outage_left_open_by_a_crash_is_closed_at_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, _ = _rss_rig(tmp_path, clock, 403)
    rig.poller.startup()
    rig.poller.run_cycle()
    [open_] = rig.outages()
    assert open_.kind is OutageKind.SOURCE_REJECTED and open_.end_at is None
    rig.crash()

    clock.advance(120)
    restarted, _ = _rss_rig(tmp_path, clock)
    restarted.poller.startup()
    [closed] = restarted.outages()
    assert closed.id == open_.id and closed.end_at == clock.now()
    assert json.loads(closed.detail)["closed_at_restart"] is True
    assert restarted.state.all_open_outages() == []
    assert restarted.outages(OutageKind.PROCESS_DOWN) == []


def test_clock_step_is_logged_and_makes_the_next_items_late(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, fetcher = _rss_rig(tmp_path, clock, _rss(["1"]), _rss(["1", "2"]))
    rig.poller.startup()
    rig.poller.run_cycle()
    clock.advance(180)
    rig.poller.run_cycle()
    assert rig.pit(RSS)["2"] is PitQuality.FIRST_SEEN
    before = clock.now()

    clock.step_wall(30)  # NTP steps the wall clock; the monotonic clock does not move
    rig.poller.tick()
    [step] = rig.outages(OutageKind.CLOCK_STEP)
    assert (step.source, step.start_at, step.end_at) == (None, before, clock.now())
    assert json.loads(step.detail)["delta_s"] == pytest.approx(30.0)

    clock.advance(150)
    fetcher.add(_rss(["1", "2", "3"]))
    rig.poller.run_cycle()
    assert rig.pit(RSS)["3"] is PitQuality.LATE
    clock.advance(180)
    fetcher.add(_rss(["1", "2", "3", "4"]))
    rig.poller.run_cycle()
    assert rig.pit(RSS)["4"] is PitQuality.FIRST_SEEN

    clock.step_wall(-10)  # a backward step still gives an interval with start <= end
    rig.poller.tick()
    back = rig.outages(OutageKind.CLOCK_STEP)[-1]
    assert back.start_at <= back.end_at and json.loads(back.detail)["delta_s"] == pytest.approx(-10.0)
    assert rig.outages(OutageKind.HOST_STALLED) == []


def test_monotonic_jump_is_logged_as_host_stalled(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, _ = _rss_rig(tmp_path, clock)
    rig.poller.startup()
    clock.advance(300)  # under the 390 s alive threshold
    rig.poller.tick()
    assert rig.outages() == []
    before = clock.now()

    clock.advance(500)
    rig.poller.tick()
    [stall] = rig.outages()
    assert (stall.kind, stall.source, stall.start_at, stall.end_at) == (OutageKind.HOST_STALLED, None, before, clock.now())
    assert json.loads(stall.detail)["gap_s"] == pytest.approx(500.0)


def test_one_outage_per_failure_run_not_per_retry(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, fetcher = _rss_rig(tmp_path, clock, _rss(["101"]))
    rig.poller.startup()
    rig.poller.run_cycle()
    for _ in range(2):
        clock.advance(180)
        fetcher.add(_unreachable(), _unreachable())
        rig.poller.run_cycle()
    assert len(fetcher.requests) == 1 + 2 * 2, "retries = 1: two attempts per failing poll"
    assert [p.status for p in rig.state.polls(RSS)] == ["ok", "error", "error"], "one polls row per poll, not per attempt"
    [outage] = rig.outages()
    assert outage.end_at is None

    clock.advance(180)
    fetcher.add(_rss(["101", "102"]))
    rig.poller.run_cycle()
    [outage] = rig.outages()
    assert outage.end_at is not None


class ExplodingSource:
    """A source with a bug: `poll` raises instead of returning a classified result."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    def poll(self, ctx: PollContext) -> PollResult:
        raise RuntimeError("placeholder-value-1")


class VendorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("placeholder-value-2")
        self.code = code


class InlineWorker:
    busy = False

    def run_with_timeout(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        return fn()


class FakeLimiter:
    def try_acquire(self) -> bool:
        return True


def test_vnstock_failure_does_not_stop_portal_capture(tmp_path: Path) -> None:
    clock = FakeClock()
    vn_cfg, rss_cfg = _vnstock_cfg(), _rss_cfg()

    def broken_events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raise VendorError("VENDOR_FEATURE_GONE")

    vnstock = VnstockEventsSource(vn_cfg, broken_events, limiter=FakeLimiter(), clock=clock, worker=InlineWorker())
    fetcher = ScriptedFetcher(clock, _rss(["101", "102"]))
    sources: dict[str, CaptureSource] = {vn_cfg.name: vnstock, rss_cfg.name: RssFeedSource(rss_cfg, fetcher)}
    rig = Rig(tmp_path / "a", clock, _config(vn_cfg, rss_cfg), sources)
    rig.poller.startup()
    report = rig.poller.run_cycle()
    assert [(p.source, p.status, p.outage_kind) for p in report.polls] == [
        ("vnstock_events", "error", OutageKind.VENDOR_UNAVAILABLE), (RSS, "ok", None),
    ]
    assert len(rig.items(RSS)) == 2
    [outage] = rig.state.open_outages("vnstock_events")
    assert outage.kind is OutageKind.VENDOR_UNAVAILABLE

    fetcher2 = ScriptedFetcher(clock, _rss(["101", "102"]))
    crashing = Rig(tmp_path / "b", clock, _config(vn_cfg, rss_cfg),
                   {vn_cfg.name: ExplodingSource(vn_cfg), rss_cfg.name: RssFeedSource(rss_cfg, fetcher2)})
    crashing.poller.startup()
    report = crashing.poller.run_cycle()
    assert [(p.source, p.status) for p in report.polls] == [("vnstock_events", "error"), (RSS, "ok")]
    poll = crashing.state.last_poll("vnstock_events")
    assert poll is not None and poll.outage_kind is UNREACHABLE
    detail = json.loads(poll.detail or "{}")
    assert (detail["class"], detail["reason"]) == ("RuntimeError", "source_crashed")
    assert "placeholder-value-1" not in (poll.detail or ""), "the message is stored as sha256 + length only (LT8)"
    assert len(fetcher2.requests) == 1, "a crashing source is not retried"


# ---------------------------------------------------------------- cycle mechanics (R2.9)


def test_snapshot_is_written_only_when_the_body_changes(tmp_path: Path) -> None:
    clock = FakeClock()
    body_a, body_b = _rss(["1"]), _rss(["1", "2"])
    rig, fetcher = _rss_rig(tmp_path, clock, body_a, body_a, body_b)
    rig.poller.startup()
    for _ in range(3):
        rig.poller.run_cycle()
        clock.advance(180)

    snapshots = rig.snapshots(RSS)
    assert [s.raw_sha256 for s in snapshots] == [sha256_hex(body_a), sha256_hex(body_b)]
    for snapshot in snapshots:
        assert snapshot.source_id == f"sha256:{snapshot.raw_sha256}" and snapshot.payload is None
        assert snapshot.blob_path is not None and (tmp_path / snapshot.blob_path).read_bytes() in (body_a, body_b)
    assert rig.state.get_kv(snapshot_kv_key(RSS)) == sha256_hex(body_b)
    assert {r.source_id: r.snapshot_sha256 for r in rig.items(RSS)} == {"1": sha256_hex(body_a), "2": sha256_hex(body_b)}
    assert [p.pages for p in rig.state.polls(RSS)] == [1, 1, 1]


def test_token_bucket_caps_vnstock_calls_per_minute(tmp_path: Path) -> None:
    clock = FakeClock()
    bucket = TokenBucket(6, 3, clock)
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]
    clock.advance(10)  # 6 per minute = one token per 10 s
    assert [bucket.try_acquire(), bucket.try_acquire()] == [True, False]

    window = TokenBucket(60, 5, clock)
    granted = 0
    for second in range(61):
        while window.try_acquire():
            granted += 1
        if second < 60:
            clock.advance(1)
    assert granted == 5 + 60, "over any 60 s window: at most capacity + rate"
    with pytest.raises(ValueError):
        TokenBucket(0, 1, clock)
    with pytest.raises(ValueError):
        TokenBucket(10, 0, clock)

    calls: list[str] = []

    def events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        calls.append(symbol)
        return [{"id": f"{symbol}-1", "symbol": symbol, "event_code": "DIV"}], {}

    cfg = _vnstock_cfg(symbols=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"), symbols_per_poll=4, calls_per_min=60.0)
    config = _config(cfg)
    sources = build_sources(config, clock=clock, sleeper=FakeSleeper(clock), call_events=events, vendor_worker=InlineWorker())
    rig = Rig(tmp_path, clock, config, sources)
    rig.poller.startup()
    counts = []
    for advance in (0, 0, 2):
        clock.advance(advance)
        before = len(calls)
        rig.poller.run_cycle(all_sources=True)
        counts.append(len(calls) - before)
    assert counts == [4, 0, 2], "a bucket of symbols_per_poll tokens refilled at calls_per_min"
    assert rig.state.get_kv(cursor_kv_key("vnstock_events")) == "6"
    assert [p.status for p in rig.state.polls("vnstock_events")] == ["ok", "ok", "ok"]


def test_retries_use_the_injected_sleeper(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, fetcher = _rss_rig(tmp_path / "a", clock, _unreachable(), _unreachable(), _rss(["1"]), cfg=_rss_cfg(retries=2))
    rig.poller.startup()
    rig.poller.run_cycle()
    assert rig.sleeper.calls == [5.0, 15.0], "backoff 5 s then 15 s, through the injected sleeper"
    assert [p.status for p in rig.state.polls(RSS)] == ["ok"] and len(fetcher.requests) == 3
    assert rig.outages() == [], "a poll that recovers within its retries is not an outage"

    budget, fetcher = _rss_rig(tmp_path / "b", clock, _unreachable(), _unreachable(), cfg=_rss_cfg(retries=2),
                               schedule={"max_poll_duration_s": 12.0})
    budget.poller.startup()
    budget.poller.run_cycle()
    assert budget.sleeper.calls == [5.0], "5 s fits the 12 s budget; 5 + 15 s does not"
    poll = budget.state.last_poll(RSS)
    assert poll is not None and poll.outage_kind is UNREACHABLE
    assert json.loads(poll.detail or "{}")["reason"] == "poll_budget" and len(fetcher.requests) == 2

    rejected, fetcher = _rss_rig(tmp_path / "c", clock, 403, cfg=_rss_cfg(retries=2))
    rejected.poller.startup()
    rejected.poller.run_cycle()
    assert rejected.sleeper.calls == [] and len(fetcher.requests) == 1, "403/429 are not retried"


def test_archive_is_synced_before_state_commit(tmp_path: Path) -> None:
    clock = FakeClock()
    hose_cfg, rss_cfg = _hose_cfg(), _rss_cfg()
    hose_fetcher, rss_fetcher = ScriptedFetcher(clock, _hose([7, 8])), ScriptedFetcher(clock, _rss(["1", "2"]))
    sources: dict[str, CaptureSource] = {
        hose_cfg.name: HoseNewsApiSource(hose_cfg, hose_fetcher, sleeper=FakeSleeper(clock)),
        rss_cfg.name: RssFeedSource(rss_cfg, rss_fetcher),
    }
    rig = Rig(tmp_path, clock, _config(hose_cfg, rss_cfg), sources)
    rig.poller.startup()
    rig.poller.run_cycle()

    appended = {e.split(":", 1)[1] for e in rig.events if e.startswith("append:")}
    assert appended == {"raw/hose_news_api/2026-10-27.jsonl", "raw/hnx_rss_issuers/2026-10-27.jsonl"}
    assert sum(e.startswith("fsync:blobs/") for e in rig.events) == 2, "each new blob is synced when written"
    pending: set[str] = set()
    for event in rig.events:
        kind, _, target = event.partition(":")
        if kind == "append":
            pending.add(target)
        elif kind == "fsync":
            pending.discard(target)
        elif kind == "commit":
            assert not pending, f"state committed before {sorted(pending)} was synced"
    assert rig.events[-1] == "commit"
    for target in appended:
        assert rig.events.count(f"fsync:{target}") == 1, "one sync per touched file"


def test_watchdog_requests_exit_when_ticks_stop(tmp_path: Path) -> None:
    clock = FakeClock()
    exits: list[int] = []
    watchdog = Watchdog(clock, 900.0, exits.append)
    rig, _ = _rss_rig(tmp_path, clock)
    rig.poller = Poller(
        rig.poller.config, {RSS: RssFeedSource(_rss_cfg(), ScriptedFetcher(clock))}, archive=rig.archive, blobs=rig.blobs,
        state=rig.state, clock=clock, sleeper=rig.sleeper, run_mode="run", pid=4242, capture_version=VERSION,
        watchdog=watchdog,
    )
    rig.poller.startup()
    clock.advance(899)
    assert watchdog.check_once() is False and exits == []
    rig.poller.tick()  # progress resets the watchdog
    clock.advance(901)
    assert watchdog.check_once() is True and exits == [WATCHDOG_EXIT_CODE] == [70]
    watchdog.check_once()
    assert exits == [70], "exit is requested once"


def test_watchdog_thread_exits_while_the_main_loop_is_blocked(tmp_path: Path) -> None:
    clock = FakeClock()
    entered, released = threading.Event(), threading.Event()
    exits: list[int] = []

    def blocked_fetch(request: HttpRequest) -> HttpResponse:
        entered.set()
        assert released.wait(10), "the watchdog never fired while the fetch was stuck"
        raise SourceError(UNREACHABLE, detail_json(reason="released"))

    def exit_fn(code: int) -> None:
        exits.append(code)
        released.set()

    def watchdog_sleep(seconds: float) -> None:
        assert entered.wait(10)
        clock.advance(seconds)  # time passes only while the main loop is blocked

    watchdog = Watchdog(clock, 900.0, exit_fn, watchdog_sleep, interval_s=300.0)
    cfg = _rss_cfg(retries=0)
    rig = Rig(tmp_path, clock, _config(cfg), {cfg.name: RssFeedSource(cfg, blocked_fetch)}, watchdog=watchdog)
    rig.poller.startup()
    thread = watchdog.start()
    assert thread.daemon
    rig.poller.run_cycle()  # blocks inside the fetch until the watchdog thread calls exit_fn
    thread.join(10)
    assert not thread.is_alive() and exits == [70]
    assert clock.monotonic() - 1000.0 == pytest.approx(1200.0), "fired on the first check past 900 s"


def test_busy_multi_source_cycle_does_not_log_host_stalled(tmp_path: Path) -> None:
    clock = FakeClock()
    hose_cfg, rss_cfg, extra_cfg = _hose_cfg(), _rss_cfg(), _rss_cfg("hnx_rss_extra")
    slow = {name: ScriptedFetcher(clock, body, advance_s=200.0) for name, body in
            (("hose", _hose([7])), ("rss", _rss(["1"])), ("extra", _rss(["9"])))}
    sources: dict[str, CaptureSource] = {
        hose_cfg.name: HoseNewsApiSource(hose_cfg, slow["hose"], sleeper=FakeSleeper(clock)),
        rss_cfg.name: RssFeedSource(rss_cfg, slow["rss"]),
        extra_cfg.name: RssFeedSource(extra_cfg, slow["extra"]),
    }
    rig = Rig(tmp_path, clock, _config(hose_cfg, rss_cfg, extra_cfg), sources)
    rig.poller.startup()
    report = rig.poller.run_cycle()
    assert [p.status for p in report.polls] == ["ok", "ok", "ok"]
    assert clock.monotonic() - 1000.0 == pytest.approx(600.0), "the cycle took longer than the 390 s alive threshold"
    assert rig.outages() == [], "tick() runs between sources (LT1)"
    alive = rig.state.get_alive()
    assert alive is not None and alive.last_alive_mono == clock.monotonic()


def test_poll_rows_record_run_mode_and_pid(tmp_path: Path) -> None:
    clock = FakeClock()
    cfg = _rss_cfg(retries=0)
    fetcher = ScriptedFetcher(clock, _rss(["1"]), 503)
    rig = Rig(tmp_path, clock, _config(cfg), {cfg.name: RssFeedSource(cfg, fetcher)}, run_mode="once", pid=31337)
    rig.poller.startup()
    rig.poller.run_cycle(all_sources=True)
    rig.poller.run_cycle(all_sources=True)
    rows = rig.state.polls(RSS)
    assert [(r.status, r.run_mode, r.pid) for r in rows] == [("ok", "once", 31337), ("error", "once", 31337)]
    assert rows[0].cycle_id != rows[1].cycle_id and rows[1].http_status == 503
    with pytest.raises(ValueError):
        Rig(tmp_path / "x", clock, _config(cfg), {cfg.name: RssFeedSource(cfg, fetcher)}, run_mode="daemon")
    with pytest.raises(ValueError):
        Rig(tmp_path / "y", clock, _config(cfg), {}, run_mode="run")  # an enabled source without an adapter


def test_heartbeat_or_backup_failure_does_not_stop_capture(tmp_path: Path) -> None:
    clock = FakeClock()
    heartbeats: list[datetime] = []
    backups: list[date] = []

    def failing_heartbeat(now: datetime) -> None:
        heartbeats.append(now)
        raise PermissionError(13, "placeholder-value-3")

    def failing_backup(day: date, now: datetime) -> None:
        backups.append(day)
        raise OSError("placeholder-value-4")

    rig, _ = _rss_rig(tmp_path / "a", clock, _rss(["1"]), _rss(["1", "2"]),
                      heartbeat_fn=failing_heartbeat, backup_fn=failing_backup)
    rig.poller.startup()
    rig.poller.run_forever(lambda: clock.now() >= T0 + timedelta(seconds=200))
    assert [p.status for p in rig.state.polls(RSS)] == ["ok", "ok"], "capture went on after both failures"
    assert len(rig.items(RSS)) == 2
    assert heartbeats == [T0] and backups == [date(2026, 10, 26)], "the closed UTC day; retried no sooner than heartbeat_every_s"
    assert rig.sleeper.calls and max(rig.sleeper.calls) <= 30.0, "sleeps in steps of at most loop_tick_s"
    for name, error in (("heartbeat", "PermissionError"), ("backup", "OSError")):
        status = json.loads(rig.state.get_kv(component_status_kv_key(name)) or "{}")
        assert (status["ok"], status["class"]) == (False, error)
        assert "placeholder-value" not in json.dumps(status)

    done: list[date] = []
    clock2 = FakeClock()
    ok_rig, _ = _rss_rig(tmp_path / "b", clock2, _rss(["1"]), _rss(["1"]))

    def good_backup(day: date, now: datetime) -> None:
        done.append(day)
        ok_rig.state.record_backup(day, "0" * 64, now)

    ok_rig.poller = Poller(
        ok_rig.poller.config, {RSS: RssFeedSource(_rss_cfg(), ScriptedFetcher(clock2, _rss(["1"]), _rss(["1"])))},
        archive=ok_rig.archive, blobs=ok_rig.blobs, state=ok_rig.state, clock=clock2, sleeper=ok_rig.sleeper,
        run_mode="run", pid=4242, capture_version=VERSION, heartbeat_fn=lambda now: None, backup_fn=good_backup,
    )
    ok_rig.poller.startup()
    ok_rig.poller.run_forever(lambda: clock2.now() >= T0 + timedelta(seconds=200))
    assert done == [date(2026, 10, 26)], "a recorded backup is not repeated"
    assert json.loads(ok_rig.state.get_kv(component_status_kv_key("backup")) or "{}")["ok"] is True
    assert ok_rig.state.backup_row(date(2026, 10, 26)) is not None


def test_slow_backup_does_not_log_host_stalled(tmp_path: Path) -> None:
    clock = FakeClock()

    def slow_backup(day: date, now: datetime) -> None:
        clock.advance(200)  # a tar.gz into a cloud-synced folder takes a while

    cfg = _rss_cfg()
    fetcher = ScriptedFetcher(clock, _rss(["1"]), _rss(["1", "2"]), advance_s=200.0)
    rig = Rig(tmp_path, clock, _config(cfg), {cfg.name: RssFeedSource(cfg, fetcher)}, backup_fn=slow_backup)
    rig.poller.startup()
    rig.poller.run_forever(lambda: clock.now() >= T0 + timedelta(seconds=500))
    assert [p.status for p in rig.state.polls(RSS)] == ["ok", "ok"]
    assert rig.outages(OutageKind.HOST_STALLED) == [], "a tick follows the side steps, so poll + backup is not a stall"


# ---------------------------------------------------------------- seams for the CLI (slice C)


def test_build_sources_wires_only_enabled_sources(tmp_path: Path) -> None:
    clock = FakeClock()
    config = _config(_hose_cfg(), _rss_cfg(), _vnstock_cfg(enabled=False))
    built: list[str] = []

    def factory(cfg: SourceConfig) -> Any:
        built.append(cfg.name)
        return ScriptedFetcher(clock)

    sources = build_sources(config, clock=clock, sleeper=FakeSleeper(clock), fetcher_factory=factory)
    assert list(sources) == ["hose_news_api", RSS] == built
    assert isinstance(sources["hose_news_api"], HoseNewsApiSource) and isinstance(sources[RSS], RssFeedSource)
    default = build_sources(_config(_rss_cfg()), clock=clock, sleeper=FakeSleeper(clock))
    assert isinstance(default[RSS]._fetcher, UrllibFetcher)  # built, never called: no network


def test_state_lists_polls_in_a_window(tmp_path: Path) -> None:
    clock = FakeClock()
    rig, _ = _rss_rig(tmp_path, clock, _rss(["1"]), _rss(["1"]))
    rig.poller.startup()
    rig.poller.run_cycle()
    clock.advance(180)
    rig.poller.run_cycle()
    assert len(rig.state.polls()) == 2 and len(rig.state.polls(RSS, since=T0 + timedelta(seconds=1))) == 1
    assert rig.state.polls(RSS, until=T0) == [] and len(rig.state.polls(RSS, status="ok")) == 2
    assert rig.state.backup_row(date(2026, 10, 26)) is None
