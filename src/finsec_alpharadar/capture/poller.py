"""Capture poller (plan 261022-1147 phase 2, R2.6, R2.7, R2.9): one poll cycle, `pit_quality`, outages, the token bucket,
the watchdog and the `run_forever` loop.

- `Poller.startup()` cuts torn JSONL tails, replays the archive tail into `seen` (RT12), closes outages left open by a
  crash and logs `process_down` from the last `alive` tick.
- `Poller.tick()` checks the gap since the previous tick (`host_stalled`: monotonic gap over the alive threshold;
  `clock_step`: wall and monotonic gaps differ by more than `clock_step_threshold_s`), writes `alive`, reports progress
  to the watchdog and commits durably. It runs between sources (red-team r1 LT1), so a busy cycle is not a stall.
- `Poller.run_cycle()` polls every due source with retries (`retries`, backoff 5 s then 15 s, only for
  `source_unreachable`) inside `max_poll_duration_s`, writes changed snapshots and new or revised items, one `polls`
  row per poll (with `run_mode` and `pid`, LT3) and at most one open outage per (kind, source).
- Every state commit goes through `storage.durable_commit`: the touched JSONL files are synced first (RT12).
- `Watchdog` runs on its own daemon thread and calls `exit_fn(70)` when no tick reported progress within
  `watchdog_limit_s` (RT2), even while the main loop is stuck in a request.

Nothing here reads a clock, sleeps or reads the pid: the clock, the sleeper, the pid, the heartbeat and backup steps
and the sources are injected (the CLI wires the real ones from `capture/host.py`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

from finsec_alpharadar.capture.config import CaptureConfig, SourceConfig, SourceKind
from finsec_alpharadar.capture.record import (
    PACKAGE_VERSION,
    CaptureRecord,
    OutageKind,
    PayloadFormat,
    PitQuality,
    RecordType,
    detail_json,
    error_detail,
    format_utc,
    make_cycle_id,
)
from finsec_alpharadar.capture.record import capture_version as code_capture_version
from finsec_alpharadar.capture.schedule import Schedule
from finsec_alpharadar.capture.sources import (
    CallEvents,
    CapturedItem,
    CapturedPage,
    CaptureSource,
    Clock,
    Fetcher,
    HoseNewsApiSource,
    PollContext,
    PollResult,
    RssFeedSource,
    Sleeper,
    UrllibFetcher,
    VendorCaller,
    VnstockEventsSource,
)
from finsec_alpharadar.capture.storage import (
    RUN_MODES,
    BlobStore,
    CaptureState,
    PollRow,
    RawArchive,
    ReconcileReport,
    durable_commit,
    reconcile_archive,
    snapshot_kv_key,
)

logger = logging.getLogger(__name__)

RETRY_BACKOFF_S = (5.0, 15.0)  # R2.9: 5 s before the second attempt, 15 s before any later one
WATCHDOG_EXIT_CODE = 70  # Task Scheduler restarts the task (phase 3)
DEFAULT_USER_AGENT = f"finsec-alpharadar-capture/{PACKAGE_VERSION}"  # neutral, no contact or user name

HeartbeatFn = Callable[[datetime], None]
BackupFn = Callable[[date, datetime], None]
ExitFn = Callable[[int], Any]
FetcherFactory = Callable[[SourceConfig], Fetcher]


def cursor_kv_key(source: str) -> str:
    """`kv` key of a vnstock source's round-robin cursor (an int as text)."""
    return f"cursor:{source}"


def component_status_kv_key(component: str) -> str:
    """`kv` key of the last run of a side component (`heartbeat`, `backup`): JSON with `ok`, `at` and, after an
    exception, its class, code, message sha256 and length (never the text)."""
    return f"component_status:{component}"


class TokenBucket:
    """Pure token bucket on the injected monotonic clock: starts full with `capacity` tokens and refills at
    `rate_per_min`. `try_acquire()` never waits (the sources' `RateLimiter`). Over any window of T seconds it grants at
    most `capacity + rate_per_min * T / 60` tokens."""

    def __init__(self, rate_per_min: float, capacity: float, clock: Clock) -> None:
        for name, value in (("rate_per_min", rate_per_min), ("capacity", capacity)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        self.rate_per_min = float(rate_per_min)
        self.capacity = float(capacity)
        self._clock = clock
        self._tokens = self.capacity
        self._last = clock.monotonic()

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_min / 60.0)
        self._last = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class Watchdog:
    """Exits the process when no progress was reported for more than `limit_s` (monotonic seconds).

    `progress(mono)` is called by `Poller.tick()`. `check_once()` is pure enough to test: it reads the injected clock and
    calls `exit_fn(70)` once the limit is passed (default `os._exit`, which ends the process even while the main thread
    is blocked in a request or a vendor call). `start()` runs `check_once` every `interval_s` on a daemon thread, waiting
    through the injected `sleep_fn`.
    """

    def __init__(
        self,
        clock: Clock,
        limit_s: float,
        exit_fn: ExitFn | None = None,
        sleep_fn: Sleeper | None = None,
        *,
        interval_s: float = 30.0,
    ) -> None:
        if not limit_s > 0 or not interval_s > 0:
            raise ValueError("limit_s and interval_s must be positive")
        self._clock = clock
        self.limit_s = float(limit_s)
        self.interval_s = float(interval_s)
        self._exit_fn: ExitFn = exit_fn if exit_fn is not None else os._exit
        self._sleep_fn = sleep_fn
        self._lock = threading.Lock()
        self._last_progress = clock.monotonic()
        self._fired = False
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def progress(self, mono: float | None = None) -> None:
        with self._lock:
            self._last_progress = self._clock.monotonic() if mono is None else mono

    def check_once(self) -> bool:
        """True when exit was requested (now or earlier); calls `exit_fn` only the first time."""
        with self._lock:
            if self._fired:
                return True
            idle_s = self._clock.monotonic() - self._last_progress
            if idle_s <= self.limit_s:
                return False
            self._fired = True
        logger.critical("watchdog: no progress for %.1f s (limit %.1f s), exit code %d", idle_s, self.limit_s, WATCHDOG_EXIT_CODE)
        self._exit_fn(WATCHDOG_EXIT_CODE)
        return True

    def start(self) -> threading.Thread:
        if self._sleep_fn is None:
            raise ValueError("Watchdog.start() needs a sleep_fn")
        if self._thread is not None:
            raise RuntimeError("the watchdog thread is already running")
        self._thread = threading.Thread(target=self._run, name="capture-watchdog", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stopped.set()

    def _run(self) -> None:
        assert self._sleep_fn is not None  # checked in start()
        while not self._stopped.is_set():
            if self.check_once():
                return
            self._sleep_fn(self.interval_s)


@dataclass(frozen=True)
class SourcePollSummary:
    """Counts of one poll, for the CLI's `once` summary (no payloads)."""

    source: str
    status: str  # "ok" | "error"
    outage_kind: OutageKind | None
    attempts: int
    items_total: int
    items_new: int
    items_revised: int
    snapshots_written: int
    pages: int


@dataclass(frozen=True)
class CycleReport:
    cycle_id: str
    started_at: datetime
    polls: tuple[SourcePollSummary, ...]


@dataclass(frozen=True)
class StartupReport:
    bytes_cut: int
    files_repaired: int
    reconcile: ReconcileReport
    orphans_closed: tuple[int, ...]
    process_down_id: int | None


def build_sources(
    config: CaptureConfig,
    *,
    clock: Clock,
    sleeper: Sleeper,
    user_agent: str = DEFAULT_USER_AGENT,
    fetcher_factory: FetcherFactory | None = None,
    call_events: CallEvents | None = None,
    vendor_worker: VendorCaller | None = None,
) -> dict[str, CaptureSource]:
    """One adapter per ENABLED source, keyed by name, in config order (the default sources factory of the CLI).

    Portals get one fetcher each (`UrllibFetcher.for_source` unless `fetcher_factory` is given). vnstock gets a
    `TokenBucket(calls_per_min, capacity=symbols_per_poll)` [ASSUMED: one sweep fits one poll], the default vendor worker
    and the real lazy vnstock call unless `call_events` / `vendor_worker` are injected; its sweep stops inside
    `max_poll_duration_s`.
    """
    factory: FetcherFactory = fetcher_factory or (lambda cfg: UrllibFetcher.for_source(cfg, clock, user_agent))
    sources: dict[str, CaptureSource] = {}
    for cfg in config.enabled_sources:
        if cfg.kind is SourceKind.HOSE_NEWS_API:
            sources[cfg.name] = HoseNewsApiSource(cfg, factory(cfg), sleeper=sleeper)
        elif cfg.kind is SourceKind.RSS_FEED:
            sources[cfg.name] = RssFeedSource(cfg, factory(cfg))
        else:
            limiter = TokenBucket(cfg.calls_per_min, max(1, cfg.symbols_per_poll), clock)
            sources[cfg.name] = VnstockEventsSource(
                cfg, call_events, limiter=limiter, clock=clock, worker=vendor_worker,
                max_poll_duration_s=config.schedule.max_poll_duration_s,
            )
    return sources


def _skew_ms(result: PollResult) -> float | None:
    """(local clock when the headers arrived) minus the server `Date` header, in ms; None without a usable Date.
    The header has a 1 s resolution (RFC 9110 §6.6.1). Not the send/receive midpoint: HNX holds its headers for 40-49 s
    and stamps `Date` when it sends them (OBSERVED 2026-10-27), so a midpoint reads half the response time as skew."""
    if result.http_date is None or result.received_at is None:
        return None
    try:
        server = parsedate_to_datetime(result.http_date)
    except (TypeError, ValueError, IndexError):
        return None
    if server.tzinfo is None:  # "-0000" means UTC without a stated zone
        server = server.replace(tzinfo=timezone.utc)
    return (result.received_at - server).total_seconds() * 1000.0


@dataclass(frozen=True)
class _Attempts:
    results: tuple[PollResult, ...]
    budget_spent: bool

    @property
    def final(self) -> PollResult:
        return self.results[-1]


class Poller:
    """The capture process for one config: startup, ticks, cycles and the forever loop (single-threaded; only the
    watchdog runs on another thread, and it never touches the state)."""

    def __init__(
        self,
        config: CaptureConfig,
        sources: Mapping[str, CaptureSource],
        *,
        archive: RawArchive,
        blobs: BlobStore,
        state: CaptureState,
        clock: Clock,
        sleeper: Sleeper,
        run_mode: str,
        pid: int,
        capture_version: str | None = None,
        heartbeat_fn: HeartbeatFn | None = None,
        backup_fn: BackupFn | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        if run_mode not in RUN_MODES:
            raise ValueError(f"run_mode must be one of {sorted(RUN_MODES)}, got {run_mode!r}")
        enabled = [cfg.name for cfg in config.enabled_sources]
        if set(sources) != set(enabled):
            raise ValueError(f"sources {sorted(sources)} must match the enabled sources {sorted(enabled)}")
        self.config = config
        self.schedule = Schedule.from_config(config)
        self.archive = archive
        self.blobs = blobs
        self.state = state
        self.clock = clock
        self.sleeper = sleeper
        self.run_mode = run_mode
        self.pid = pid
        self.capture_version = capture_version if capture_version is not None else code_capture_version()
        self.heartbeat_fn = heartbeat_fn
        self.backup_fn = backup_fn
        self.watchdog = watchdog
        self._sources: tuple[tuple[SourceConfig, CaptureSource], ...] = tuple(
            (cfg, sources[cfg.name]) for cfg in config.enabled_sources
        )
        self._last_tick: tuple[datetime, float] | None = None
        self._cycle_seq = 0
        self._next_heartbeat_mono: float | None = None
        self._last_backup_try_mono: float | None = None

    # ------------------------------------------------------------ startup, ticks, commits

    def _commit(self, at: datetime) -> None:
        """The only way this module commits: archive files synced first, then the state (RT12)."""
        durable_commit(self.archive, self.state, at=at)

    def startup(self) -> StartupReport:
        now = self.clock.now()
        since = self.state.last_commit_at()
        last_day = now.astimezone(timezone.utc).date()
        day = since.date() if since is not None else last_day - timedelta(days=1)
        days: list[date] = []
        while day <= last_day:
            days.append(day)
            day += timedelta(days=1)
        cuts = self.archive.repair_tails(days)
        bytes_cut = sum(cuts.values())
        reconcile = reconcile_archive(self.archive, self.state, now=now)

        orphans = tuple(outage.id for outage in self.state.all_open_outages())
        for outage_id in orphans:
            self.state.close_outage(outage_id, now, closed_at_restart=True)
        if orphans:
            logger.warning("closed %d outage(s) left open by the previous run", len(orphans))

        process_down_id: int | None = None
        alive = self.state.get_alive()
        if alive is not None:
            down_s = (now - alive.last_alive_at).total_seconds()
            if down_s > self.config.schedule.alive_threshold_s:
                detail = detail_json(down_s=down_s, bytes_cut=bytes_cut, files_repaired=len(cuts), previous_pid=alive.pid)
                process_down_id = self.state.open_outage(
                    OutageKind.PROCESS_DOWN, None, start_at=alive.last_alive_at, detected_at=now, detail=detail
                )
                self.state.close_outage(process_down_id, now)
                logger.warning("process_down logged: down_s=%.1f bytes_cut=%d", down_s, bytes_cut)
        if bytes_cut and process_down_id is None:
            logger.warning("archive tails repaired without a process_down: bytes_cut=%d files=%d", bytes_cut, len(cuts))
        self.tick(now, self.clock.monotonic())
        return StartupReport(bytes_cut, len(cuts), reconcile, orphans, process_down_id)

    def tick(self, now: datetime | None = None, mono: float | None = None) -> list[int]:
        """Check the gap since the previous tick, write `alive`, report progress, commit. Returns new outage ids."""
        now = self.clock.now() if now is None else now
        mono = self.clock.monotonic() if mono is None else mono
        opened: list[int] = []
        if self._last_tick is not None:
            prev_now, prev_mono = self._last_tick
            mono_gap_s = mono - prev_mono
            step_s = (now - prev_now).total_seconds() - mono_gap_s
            if mono_gap_s > self.config.schedule.alive_threshold_s:
                opened.append(self._interval_outage(OutageKind.HOST_STALLED, prev_now, now, detail_json(gap_s=mono_gap_s)))
            if abs(step_s) > self.config.schedule.clock_step_threshold_s:
                opened.append(self._interval_outage(OutageKind.CLOCK_STEP, prev_now, now, detail_json(delta_s=step_s)))
        self._last_tick = (now, mono)
        self.state.set_alive(now, mono, self.pid)
        if self.watchdog is not None:
            self.watchdog.progress(mono)
        self._commit(now)
        return opened

    def _interval_outage(self, kind: OutageKind, previous: datetime, now: datetime, detail: str) -> int:
        """A process-wide outage over [previous tick, now], ordered so a backward clock step still has start <= end."""
        start, end = min(previous, now), max(previous, now)
        outage_id = self.state.open_outage(kind, None, start_at=start, detected_at=now, detail=detail)
        self.state.close_outage(outage_id, end)
        logger.warning("%s logged: start=%s end=%s detail=%s", kind.value, format_utc(start), format_utc(end), detail)
        return outage_id

    # ------------------------------------------------------------ one cycle

    def _is_due(self, cfg: SourceConfig, now: datetime) -> bool:
        last = self.state.last_poll(cfg.name)
        return last is None or self.schedule.next_due(last.started_at, cfg) <= now

    def run_cycle(self, *, all_sources: bool = False) -> CycleReport:
        """Poll every enabled source that is due (or every one with `all_sources`), ticking after each source."""
        started_at = self.clock.now()
        self._cycle_seq += 1
        cycle_id = make_cycle_id(started_at, self._cycle_seq)
        summaries: list[SourcePollSummary] = []
        for cfg, source in self._sources:
            if not all_sources and not self._is_due(cfg, self.clock.now()):
                continue
            summaries.append(self._poll_source(cycle_id, cfg, source))
            self.tick()
        return CycleReport(cycle_id, started_at, tuple(summaries))

    def _cursor(self, name: str) -> int:
        text = self.state.get_kv(cursor_kv_key(name))
        if text is None:
            return 0
        try:
            return int(text)
        except ValueError:
            logger.warning("ignoring a malformed cursor for source=%s", name)
            return 0

    def _attempt(
        self, cfg: SourceConfig, source: CaptureSource, last_ok: datetime | None, cursor: int
    ) -> tuple[PollResult, bool]:
        """One `poll()`; a source that raises (a bug) becomes `source_unreachable` and is not retried."""
        name = cfg.name
        ctx = PollContext(
            now=self.clock.now(), last_ok_at=last_ok, is_known=lambda source_id: self.state.has_source_id(name, source_id),
            cursor=cursor,
        )
        try:
            return source.poll(ctx), False
        except Exception as err:  # one broken source must not stop the others (D2); classified, never silent
            logger.error("source poll raised: source=%s class=%s", name, type(err).__name__)
            return PollResult(outage_kind=OutageKind.SOURCE_UNREACHABLE, detail=error_detail(err, reason="source_crashed")), True

    def _attempts(self, cfg: SourceConfig, source: CaptureSource, last_ok: datetime | None, cursor: int) -> _Attempts:
        """Retry `source_unreachable` up to `retries` times (5 s, then 15 s) while the backoff fits the poll budget."""
        budget_s = self.config.schedule.max_poll_duration_s
        start = self.clock.monotonic()
        results: list[PollResult] = []
        while True:
            result, crashed = self._attempt(cfg, source, last_ok, cursor)
            results.append(result)
            if result.ok or crashed or result.outage_kind is not OutageKind.SOURCE_UNREACHABLE or len(results) > cfg.retries:
                return _Attempts(tuple(results), budget_spent=False)
            backoff_s = RETRY_BACKOFF_S[min(len(results) - 1, len(RETRY_BACKOFF_S) - 1)]
            if self.clock.monotonic() - start + backoff_s > budget_s:
                return _Attempts(tuple(results), budget_spent=True)
            logger.info("retrying source=%s attempt=%d backoff_s=%.0f", cfg.name, len(results) + 1, backoff_s)
            self.sleeper(backoff_s)

    def _pit_quality(self, cfg: SourceConfig, last_ok: datetime | None, t: datetime) -> PitQuality:
        """R2.6: late when the source never had an ok poll, the gap deadline passed, or any outage of the source or of
        the whole process intersects (last ok, t]."""
        if last_ok is None or t > self.schedule.gap_deadline(last_ok, cfg):
            return PitQuality.LATE
        return PitQuality.LATE if self.state.outages_between(cfg.name, last_ok, t) else PitQuality.FIRST_SEEN

    def _ingested_at(self, first_seen_at: datetime) -> datetime:
        """Now, but never before `first_seen_at` (schema v1); only a backward clock step during a poll hits the bound,
        and that step is logged as `clock_step` at the next tick."""
        return max(self.clock.now(), first_seen_at)

    def _poll_source(self, cycle_id: str, cfg: SourceConfig, source: CaptureSource) -> SourcePollSummary:
        name = cfg.name
        started_at = self.clock.now()
        last_ok_row = self.state.last_poll(name, status="ok")
        # The anchor is the ok poll's finished_at: it is >= the end_at of an outage that poll closed, so the next poll
        # does not intersect that outage again ("late once").
        last_ok = last_ok_row.finished_at if last_ok_row is not None else None
        attempts = self._attempts(cfg, source, last_ok, self._cursor(name))
        final = attempts.final
        kind, detail = final.outage_kind, final.detail
        if attempts.budget_spent:
            last_detail = json.loads(final.detail) if final.detail else None
            detail = detail_json(
                reason="poll_budget", attempts=len(attempts.results), budget_s=self.config.schedule.max_poll_duration_s,
                last=last_detail,
            )

        items: dict[tuple[str, str], CapturedItem] = {}
        pages: list[CapturedPage] = []
        for result in attempts.results:  # partial results of failed attempts are real data too
            pages.extend(result.pages)
            for item in result.items:
                items.setdefault((item.source_id, item.raw_sha256), item)

        now = self.clock.now()
        if kind is not None:  # opened BEFORE pit_quality: items of a failed poll (overflow, quota) come out late
            self.state.open_outage(kind, name, start_at=started_at, detected_at=now, detail=detail)
        pit = self._pit_quality(cfg, last_ok, now)
        snapshots = self._write_snapshots(cycle_id, name, pages, pit)
        new, revised = self._write_items(cycle_id, name, list(items.values()), pit)
        if kind is None:
            closed = self.state.close_open_outages(name, final.received_at or now)
            if closed:
                logger.info("source recovered: source=%s outages_closed=%d", name, len(closed))
        if final.next_cursor is not None:
            self.state.set_kv(cursor_kv_key(name), str(final.next_cursor))
        status = "ok" if kind is None else "error"
        self.state.insert_poll(
            PollRow(
                cycle_id=cycle_id, source=name, started_at=started_at, finished_at=self.clock.now(), status=status,
                outage_kind=kind, detail=detail, http_status=final.http_status, http_date=final.http_date,
                rtt_ms=final.rtt_ms, skew_ms=_skew_ms(final), items_total=len(items), items_new=new,
                items_revised=revised, pages=len(pages), run_mode=self.run_mode, pid=self.pid,
            )
        )
        logger.info(
            "poll source=%s status=%s kind=%s attempts=%d items=%d new=%d revised=%d snapshots=%d pit=%s",
            name, status, kind.value if kind else None, len(attempts.results), len(items), new, revised, snapshots,
            pit.value,
        )
        return SourcePollSummary(name, status, kind, len(attempts.results), len(items), new, revised, snapshots, len(pages))

    def _write_snapshots(self, cycle_id: str, name: str, pages: list[CapturedPage], pit: PitQuality) -> int:
        """A snapshot (blob + record) for each body whose sha differs from the source's last archived snapshot."""
        last_sha = self.state.get_kv(snapshot_kv_key(name))
        written = 0
        for page in pages:
            if page.sha256 == last_sha:
                continue
            ingested_at = self._ingested_at(page.received_at)
            put = self.blobs.put(page.body, ingested_at.astimezone(timezone.utc).date())
            self.state.record_blob(put.sha256, put.rel_path, ingested_at, put.byte_count)
            self.archive.append(
                CaptureRecord(
                    record_type=RecordType.SNAPSHOT, source=name, source_id=f"sha256:{put.sha256}",
                    information_time=page.received_at, first_seen_at=page.received_at, ingested_at=ingested_at,
                    pit_quality=pit, raw_sha256=put.sha256, payload_format=PayloadFormat.BLOB, payload=None,
                    blob_path=put.rel_path, byte_count=put.byte_count, snapshot_sha256=None, request=page.request,
                    response=page.response, cycle_id=cycle_id, capture_version=self.capture_version,
                )
            )
            self.state.set_kv(snapshot_kv_key(name), put.sha256)
            last_sha = put.sha256
            written += 1
        return written

    def _write_items(self, cycle_id: str, name: str, items: list[CapturedItem], pit: PitQuality) -> tuple[int, int]:
        """Archive every item whose (source, source_id, raw_sha256) is new; a new sha of a known id is a revision."""
        new = revised = 0
        for item in items:
            if self.state.is_seen(name, item.source_id, item.raw_sha256):
                continue
            is_revision = self.state.has_source_id(name, item.source_id)  # before add_seen
            self.archive.append(
                CaptureRecord(
                    record_type=RecordType.ITEM, source=name, source_id=item.source_id,
                    information_time=item.first_seen_at, first_seen_at=item.first_seen_at,
                    ingested_at=self._ingested_at(item.first_seen_at), pit_quality=pit, raw_sha256=item.raw_sha256,
                    payload_format=item.payload_format, payload=item.payload, blob_path=None, byte_count=item.byte_count,
                    snapshot_sha256=item.snapshot_sha256, request=item.request, response=item.response,
                    cycle_id=cycle_id, capture_version=self.capture_version,
                )
            )
            self.state.add_seen(name, item.source_id, item.raw_sha256, item.first_seen_at)
            new += 1
            revised += int(is_revision)
        return new, revised

    # ------------------------------------------------------------ the forever loop

    def _run_component(self, component: str, step: Callable[[], None], now: datetime) -> bool:
        """Run a side step; an exception is logged and recorded in `kv`, never raised (R2.10, R2.11, RT11)."""
        key = component_status_kv_key(component)
        try:
            step()
        except Exception as err:  # the heartbeat or the backup must never kill capture
            logger.error("%s failed: class=%s", component, type(err).__name__)
            self.state.set_kv(key, error_detail(err, ok=False, at=format_utc(now)))
            return False
        self.state.set_kv(key, detail_json(ok=True, at=format_utc(now)))
        return True

    def _maybe_heartbeat(self, now: datetime, mono: float) -> bool:
        if self.heartbeat_fn is None or (self._next_heartbeat_mono is not None and mono < self._next_heartbeat_mono):
            return False
        heartbeat_fn = self.heartbeat_fn
        self._next_heartbeat_mono = mono + self.config.schedule.heartbeat_every_s
        self._run_component("heartbeat", lambda: heartbeat_fn(now), now)
        return True

    def _maybe_backup(self, now: datetime, mono: float) -> bool:
        """Back up the UTC day that just closed, once local time passed `backup_at_local` and no `backups` row exists.
        A failed try is repeated no sooner than `heartbeat_every_s` later."""
        if self.backup_fn is None or self.schedule.local(now).time() < self.config.schedule.backup_at_local:
            return False
        day = now.astimezone(timezone.utc).date() - timedelta(days=1)
        if self.state.backup_row(day) is not None:
            return False
        if self._last_backup_try_mono is not None and mono - self._last_backup_try_mono < self.config.schedule.heartbeat_every_s:
            return False
        backup_fn = self.backup_fn
        self._last_backup_try_mono = mono
        self._run_component("backup", lambda: backup_fn(day, now), now)
        return True

    def _sleep_s(self, now: datetime, mono: float) -> float:
        """Until the next due source or heartbeat, in steps of at most `loop_tick_s`."""
        wait = self.config.schedule.loop_tick_s
        for cfg, _ in self._sources:
            last = self.state.last_poll(cfg.name)
            due = now if last is None else self.schedule.next_due(last.started_at, cfg)
            wait = min(wait, (due - now).total_seconds())
        if self.heartbeat_fn is not None and self._next_heartbeat_mono is not None:
            wait = min(wait, self._next_heartbeat_mono - mono)
        return max(0.0, wait)

    def run_forever(self, stop: Callable[[], bool]) -> None:
        """Cycle, tick, heartbeat, backup, sleep; until `stop()` is True (the CLI passes `threading.Event.is_set`)."""
        while not stop():
            self.run_cycle()
            now, mono = self.clock.now(), self.clock.monotonic()
            self.tick(now, mono)
            ran_heartbeat = self._maybe_heartbeat(now, mono)
            ran_backup = self._maybe_backup(now, mono)
            if ran_heartbeat or ran_backup:  # tick again (it commits): a slow side step must not add to the next poll's gap
                self.tick()
            if stop():
                break
            wait = self._sleep_s(self.clock.now(), self.clock.monotonic())
            if wait > 0:
                self.sleeper(wait)
