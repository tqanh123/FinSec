# P2 developer report (three sequential hs:developer slices + main)

## Main (2026-10-27)
- Resolutions in the brief: source_id per R2.2 (no portal prefix); vnstock update-available warning is log-only; error_map targets existing OutageKind members; no postedDate converter in P2.
- Accepted deviations: gap test uses max_poll_duration_s=120 to match the 08:05 example; backup retry throttled to heartbeat_every_s; one open outage per (kind, source); per-source durable commit; vnstock sweep stops at the poll budget (not an outage).
- Fixed after the dry run: skew_ms = header arrival - Date (was the send/receive midpoint; HNX holds headers 40-49 s, so the midpoint read ~23 s of skew). RED: test_steady_state_items_are_first_seen; GREEN after the poller.py fix.
- Step 10 dry run (real network, vnstock disabled): once --all x3 ok; HOSE 40/20/20 items, HNX 100; all records round-trip; heartbeat DEGRADED no_backup_yet; audit exit 0 with --require-days 0; check-root exit 0/4; backup of the open day exit 2.
- Gate: .venv 455 passed 0 skipped; global 420 passed 1 skipped (fastapi); e2e exit 0; both standards checks conform.

# P2 developer slice A report (steps 1-5)

Status: DONE. Steps 1-5 complete, suite green. Step 6 (poller) not started, as instructed. No git writes.

## Steps done, files

| Step | Files | Lines |
|---|---|---|
| 1 tzdata | `pyproject.toml` (+`"tzdata"` in `[project].dependencies`, no marker, no bound), `tests/test_dependency_pins.py` (+2 tests, docstring) ; `.venv` now has tzdata 2026.5 (`pip install -e ".[dev]"`) | +1 / +22 |
| 2 record | `src/finsec_alpharadar/capture/record.py`, `capture/__init__.py`, `tests/test_capture_record.py` | 282 / 121 / 250 |
| 3 schedule | `capture/schedule.py`, `capture/config.py` (dataclasses only, no loader), `tests/test_capture_schedule.py` | 96 / 245 / 220 |
| 4 storage | `capture/storage.py`, `tests/test_capture_storage.py` | 547 / 366 |
| 5 sources | `capture/sources.py`, `tests/test_capture_sources.py` | 709 / 648 |

`capture/__init__.py` re-exports 53 names (record, config, schedule, storage, sources) and was grown one step at a time. It imports
nothing heavy: after `import finsec_alpharadar.capture`, `{numpy, pandas, vnstock} & sys.modules` is empty (checked).

## RED evidence, then green

| Test file | First failing line (RED) | Final |
|---|---|---|
| `tests/test_dependency_pins.py` (2 new) | `AssertionError: [project].dependencies must hold exactly the bare requirement 'tzdata', got []`; the zoneinfo test `Failed` at `test_dependency_pins.py:145` with "... install tzdata: ..." | 5 passed |
| `tests/test_capture_record.py` | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture'` | 11 passed |
| `tests/test_capture_schedule.py` | `ImportError: cannot import name 'schedule' from 'finsec_alpharadar.capture'` | 10 passed |
| `tests/test_capture_storage.py` | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture.storage'` | 11 passed |
| `tests/test_capture_sources.py` | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture.sources'` | 24 passed |

Every test the phase file lists for steps 1-5 exists under its exact name. Extra tests I added:
- `test_cycle_id_and_outage_kinds_and_error_detail` (record);
- `test_source_config_enforces_d5_and_its_own_shape`, `test_vnstock_source_config_refuses_kbs_and_maps_codes_to_outage_kinds`, `test_capture_config_holds_unique_sources_and_holidays` (schedule file; config.py arrived in this step);
- `test_poll_alive_kv_and_blob_rows_round_trip` (storage);
- `test_vendor_worker_runs_on_a_daemon_thread_and_stays_busy_after_a_timeout` (sources).

**Full suite:** `.venv/Scripts/python -X utf8 -m pytest -q` gives **345 passed** (0 skipped), re-run after the review fixes. The global Python 3.13 runs the 5 touched/new files and passes them all (61 passed).

## Deviations (with reasons)

1. **`test_gap_deadline_after_a_0700_poll_ends_at_0805_not_0902`** uses `max_poll_duration_s = 120`. The phase example only works with
   120 s (07:00 -> 08:00 -> 08:03 -> +2 min). With the brief's 330 s the deadline is 08:08:30. The other schedule tests use 330.
2. **`UrllibFetcher`** takes a keyword `max_body_bytes`, which the phase signature lacks; LT9 needs it. There is also a `UrllibFetcher.for_source(cfg, clock, ua)` helper.
3. **vnstock worker.** The phase injects `run_with_timeout(fn, timeout_s)`. Here the injected object is a `VendorCaller` with `run_with_timeout(fn, timeout_s)` plus a `busy`
   attribute, because "blocks a second worker" needs to know whether the hung call is still alive. The default `VendorWorker` uses a **daemon
   `threading.Thread` + `join(timeout)`, not `ThreadPoolExecutor`**, because ThreadPoolExecutor's non-daemon workers are joined at interpreter exit, so a
   hung vnstock call would hang `once` at exit. One call at a time; while busy, `VendorWorkerBusy` (a `TimeoutError` subclass) is raised and the source
   answers `source_unreachable` with detail `vendor_worker_busy`.
4. **vnstock limiter.** The phase says "theo token bucket"; the source takes any `RateLimiter` with `try_acquire() -> bool`. With no token, the sweep stops
   without sleeping and resumes from that symbol on the next poll. The poller's `TokenBucket` (slice B) must expose `try_acquire()`.
5. **Unknown symbol.** A new config field `symbol_error_codes`: a vendor error whose `code` is in that list is noted in `PollResult.detail`
   (`{"symbol_errors":[{symbol, code, class}]}`) and skipped (the cursor moves on). Shipped default for step 8: `["VNSTOCK_INPUT_SYMBOL_UNKNOWN"]`. Label it **[ASSUMED]**: the code is
   VERIFIED for `ohlcv()` (vendor docs, `research/vnstock-gen5.md` line 149), but whether `events()` raises the same code is not checked (the probe saw no
   unknown symbol for `events()`, and delisted symbols still returned data). The error map also needs `VNSTOCK_RELEASE_EXPIRED -> vendor_release_expired` (AGENTS.md verified fact), plus
   main's three entries.
6. **First-ever poll is never `coverage_overflow`.** Both the HOSE page cap and the RSS no-overlap rule apply only when `ctx.last_ok_at is not None` (a first poll has nothing
   to overlap with, and its items are `late` anyway).
7. **Pages and statuses.** Only 200 responses become `pages` (snapshot candidates). A 200 with an invalid body is still a page (kept for diagnosis).
   Statuses 401/403/429 map to `source_rejected`, 5xx to `source_unreachable`, and any other non-200 to `source_invalid_payload`. The phase names no class for 404 and similar; this is my choice.
8. **RSS with a missing guid.** Items that do have a guid are kept, and the poll ends `source_invalid_payload`. A feed with no `<item>` is also `source_invalid_payload`.
9a. **`quota_warning_patterns` must not be empty** for a vnstock source: `SourceConfig` raises `CaptureConfigError` otherwise (added after review),
   because an empty list would make the RT7 quota rule silently inert. The attrs scan matches the same substrings against the whole attrs JSON, keys
   included, so the shipped patterns must be specific (a benign key containing "limit" would pin the cursor). Check this on the laptop run.
9. **`detail` format.** `polls.detail` and `outages.detail` must be a JSON object string. `CaptureState` refuses free text with `StorageError`, which enforces LT8 structurally.
   Use `record.error_detail(exc, **extra)` or `record.detail_json(**fields)`. Because of this, closing at restart is `close_outage(id, end, closed_at_restart=True)`, which merges a field into the JSON,
   not the literal string `"closed_at_restart"` from the phase text.
10. **`CaptureRecord` is stricter than the phase table.** For an item it also checks `raw_sha256 == sha256(canonical_item_bytes(payload))`
    and `byte_count == len(canonical)`, a snapshot's `source_id == "sha256:<raw_sha256>"`, and `payload_format` against the payload type (str is text, anything else is json).
    `canonical_item_bytes` is strict JSON (`allow_nan=False`), so a NaN payload is refused.
11. **`record.py` imports `from finsec_alpharadar import __version__`**, the root package and not a submodule, for `capture_version`. The step 9 AST invariant
    (`from finsec_alpharadar.` only `capture.*` / `data_root`) passes as worded, but slice B must allow `from finsec_alpharadar import __version__` if it
    checks module `finsec_alpharadar` itself.
12. **The HOSE source** takes its ICT dates from `schedule.local_zone()` (Asia/Ho_Chi_Minh), not from `ScheduleConfig.timezone`. They are the same value; this is noted only for completeness.

13. **Test strings.** Test strings that stand in for vendor messages and cookies are neutral placeholders (`placeholder-value-N`), not key- or token-shaped text,
    so the shared pre-push secret gate cannot match them.
14. **A sanctioned wall-clock wait.** `VendorWorker.run_with_timeout` uses `Thread.join(timeout)`, the one wall-clock wait outside `host.py`. R2.8 sanctions it (the phase had
    `future.result(timeout)` there), so the step 9 invariant should not forbid `join(`. Tests only use `join(0)` and a plain `join()` after the thread has been released.

No file outside the phase's Related Code Files table was touched. `tests/conftest.py` was not created: each test file has its own small fakes.

## Not done / remaining

- Steps 6-11 (poller, heartbeat/backup/audit, host/config loader/CLI/`configs/capture.toml`, invariants + repo-hygiene + `.env.example`, dry run, docs).
- `pyproject.toml` is now LF in the working copy (git warns it will normalize to CRLF on checkout). The content diff is one line.

## API summary for slice B

All modules: `from __future__ import annotations`, stdlib only, and they read no clock. Instants are tz-aware UTC `datetime`s.

### `capture.record`
- `SCHEMA_VERSION = 1`; enums `RecordType(ITEM, SNAPSHOT)`, `PitQuality(FIRST_SEEN, LATE)`, `PayloadFormat(JSON, TEXT, BLOB)`,
  `OutageKind` (11 members: `PROCESS_DOWN, HOST_STALLED, CLOCK_STEP, SOURCE_UNREACHABLE, SOURCE_REJECTED, SOURCE_INVALID_PAYLOAD,
  COVERAGE_OVERFLOW, VENDOR_AUTH_UNREACHABLE, VENDOR_RELEASE_EXPIRED, VENDOR_QUOTA_EXCEEDED, VENDOR_UNAVAILABLE`), all `str, Enum`.
- `class CaptureRecordError(ValueError)`.
- `@dataclass(frozen=True) CaptureRecord(record_type, source, source_id, information_time, first_seen_at, ingested_at, pit_quality,
  raw_sha256, payload_format, payload, blob_path, byte_count, snapshot_sha256, request: Mapping, response: Mapping, cycle_id,
  capture_version, information_time_basis="first_seen", schema_version=1)`. It validates all of R2.2 and normalizes enums and datetimes. Pass
  `information_time=first_seen_at`. An item: `payload` (dict or str), `raw_sha256`/`byte_count` from `canonical_item_bytes(payload)`, `blob_path=None`.
  A snapshot: `payload=None`, `payload_format=BLOB`, `blob_path=BlobPut.rel_path`, `source_id=f"sha256:{sha}"`, `byte_count=len(body)`,
  `snapshot_sha256=None`. Methods: `.to_dict()`, `.to_json_line() -> str` (ends "\n"), `CaptureRecord.from_json_line(str|bytes)`. It is not
  hashable (it has dict fields).
- `format_utc(dt) -> str`, `parse_utc(str) -> datetime`, `make_cycle_id(started_at, seq) -> str`.
- `canonical_item_bytes(payload) -> bytes`, `sha256_hex(bytes) -> str`.
- `message_fingerprint(text) -> {"message_sha256", "message_len"}`; `detail_json(**fields) -> str`;
  `error_detail(exc, **extra) -> str` (keys `class`, `code` (str/int or null), `message_sha256`, `message_len`, plus extra).
- `capture_code_digest(directory: Path | None = None) -> str` (CRLF read as LF); `capture_version(digest=None) -> "0.1.0+<12 hex>"`.
- Readers split JSONL on b"\n" only (U+2028 stays raw inside strings).

### `capture.config` (dataclasses; the loader is yours, in step 8)
- `class CaptureConfigError(ValueError)`; `class SourceKind(str, Enum)`: `HOSE_NEWS_API="hose_news_api"`, `RSS_FEED="rss_feed"`,
  `VNSTOCK_EVENTS="vnstock_events"`. Constants `LOCAL_TIMEZONE`, `D5_ACTIVE_MIN_S=120`, `D5_ACTIVE_MAX_S=300`, `D5_IDLE_S=3600`.
- `Holiday(day: date, status: "announced"|"provisional", source_url=None, notice_id=None)`.
- `ScheduleConfig(active_start: time, active_end: time, loop_tick_s, max_poll_duration_s, clock_step_threshold_s, heartbeat_every_s,
  watchdog_limit_s, backup_at_local: time, timezone="Asia/Ho_Chi_Minh")`; property `alive_threshold_s = 2*loop_tick_s + max_poll_duration_s`.
- `SourceConfig(name, kind, enabled, url, active_interval_s, idle_interval_s, timeout_s, deadline_s, retries, max_body_bytes,
  page_size=None, max_pages=1, min_request_spacing_s=0.0, headers={}, vnstock_source=None, symbols=(), symbols_per_poll=0,
  calls_per_min=0.0, vendor_call_timeout_s=0.0, release_expires_on=None, error_map={}, symbol_error_codes=(),
  quota_warning_patterns=(), ignore_warning_patterns=())`. `__post_init__` raises `CaptureConfigError` when:
  - the name does not match `^[a-z][a-z0-9_]*$`, or the kind is unknown;
  - D5 is broken (120 ≤ active ≤ 300, idle == 3600);
  - `deadline_s < timeout_s`;
  - a portal source has no `https://` url, or `hose_news_api` has no `page_size`;
  - for vnstock: `vnstock_source` is missing or `"kbs"`, `symbols` is empty or has duplicates, `quota_warning_patterns` is empty, or an `error_map` value is not an `OutageKind` member (values are coerced to `OutageKind`).
- `HeartbeatConfig(canary_files: tuple[str, ...], stripchart_host: str | None = None)`.
- `CaptureConfig(schedule, holidays, sources, heartbeat, data_root: Path | None = None, backup_dir: Path | None = None)`. It refuses duplicate
  source names and holiday dates. `.source(name)` and `.enabled_sources`.
- TOML loader notes: convert dates/times yourself (`tomllib` gives `datetime.date`/`time` for TOML date/time literals), turn lists into tuples (or pass lists;
  `symbols` and the pattern lists accept list/tuple).

### `capture.schedule`
- `class TimeZoneDataMissing(RuntimeError)`; `local_zone(name=LOCAL_TIMEZONE) -> tzinfo` creates the zone per call and raises with "install tzdata: ...".
- `Schedule(config: ScheduleConfig, holidays: Iterable[Holiday])`, `Schedule.from_config(CaptureConfig)`; methods `zone()`, `local(t)`,
  `local_date(t)`, `is_trading_day(d)`, `is_active(t)`, `interval_at(t, source) -> timedelta`, `next_boundary(t)`,
  `next_due(prev, source) -> datetime`, `gap_deadline(prev, source) -> datetime` (UTC). A naive datetime raises `ValueError`.

### `capture.storage`
- `Syncer = Callable[[BinaryIO, Path], None]` (default `fsync_file`: flush + `os.fsync`); `class StorageError(RuntimeError)`;
  `snapshot_kv_key(source) -> "last_snapshot_sha256:<source>"`.
- `RawArchive(root, *, syncer=fsync_file)`. The root must exist. Methods:
  - `path_for(source, day)`;
  - `append(record) -> Path`: the file is chosen by the UTC date of `ingested_at`; the handle stays open;
  - `sync() -> list[Path]`: one sync per touched file, then close;
  - `close()`, `source_names()`, `files_for_day(day)`, `read_records(path)` (static);
  - `repair_tail(path) -> int`, `repair_tails(days) -> {path: bytes_cut}`.
- `BlobStore(root, *, syncer=fsync_file, replacer=os.replace)`; `rel_path(sha, day)`; `put(body, day) -> BlobPut(rel_path, sha256,
  byte_count, written, rewritten)`. A new blob is synced before `os.replace`. An intact existing blob gives `written=False`. A corrupt one is rewritten, with a warning
  that logs the sha only.
- `PollRow(cycle_id, source, started_at, finished_at, status "ok"|"error", outage_kind, detail, http_status, http_date, rtt_ms, skew_ms,
  items_total, items_new, items_revised, pages, run_mode "run"|"once", pid, id=None)`.
- `OutageRow(id, kind, source|None, start_at, end_at|None, detected_at, detail)`, `AliveRow(last_alive_at, last_alive_mono, pid)`,
  `ReconcileReport(records_scanned, seen_added, snapshots_restored)`.
- `CaptureState(path, *, busy_timeout_s=5.0)` sets WAL + `synchronous=FULL` on open and creates all R2.4 tables plus `meta.schema_version=1`. The parent's
  parent must exist (the data root). It is a context manager. Methods:
  - lifecycle: `close()` (no commit), `pragma(name)`, `commit(at)` (writes `meta.last_commit_at` and commits), `rollback()`;
  - meta and kv: `get_meta`/`set_meta`, `last_commit_at()`, `get_kv`/`set_kv`;
  - seen: `add_seen(source, source_id, sha, first_seen_at) -> bool` (new?), `is_seen(...)`, `seen_first_at(...)`, `has_source_id(source, source_id)` (a revision of a known id);
  - polls: `insert_poll(PollRow) -> id`, `last_poll(source, *, status=None) -> PollRow | None` (latest `started_at`);
  - outages:
    - `open_outage(kind, source|None, *, start_at, detected_at, detail) -> id`: a no-op returning the existing id while the same (kind, source) is open;
    - `outage(id)`, `open_outages(source|None)`, `all_open_outages()`;
    - `close_outage(id, end_at, **detail_fields)` merges the fields into the JSON detail;
    - `close_open_outages(source|None, end_at, **detail_fields) -> [ids]`;
    - `outages_between(source, start, end)`: the source's own outages plus process-wide ones intersecting (start, end]; an open outage counts as ongoing;
  - other rows: `record_blob(sha, rel_path, first_written_at, byte_count) -> bool`, `set_alive(at, mono, pid)`, `get_alive()`,
    `record_heartbeat(written_at, status, rel_path, sha256)`, `record_backup(day, manifest_sha256, verified_at)`.
  - The poller, heartbeat and audit need more read queries (polls in 24 h, etc.); add them to `CaptureState`.
- `durable_commit(archive, state, *, at) -> list[Path]` runs `archive.sync()` and only then `state.commit(at)` (RT12).
- `reconcile_archive(archive, state, *, now) -> ReconcileReport` replays lines with `ingested_at > last_commit_at` into `seen` (items) and the latest snapshot
  sha per source into `kv`. Without a mark it scans the last two UTC days. It does NOT commit.

### `capture.sources`
- Protocols:
  - `Clock` (`now() -> datetime`, `monotonic() -> float`); host's `SystemClock` must match it;
  - `Sleeper = Callable[[float], None]`;
  - `RateLimiter` (`try_acquire() -> bool`): your `TokenBucket` must implement it;
  - `Opener` (`open(request, timeout)`), `CaptureSource` (`config`, `poll(ctx) -> PollResult`), `VendorCaller` (`busy`, `run_with_timeout(fn, timeout_s)`).
- `SourceError(kind: OutageKind, detail: str, *, http_status=None)`.
- `HttpRequest(method, url, params: tuple[tuple[str,str],...]=(), headers: Mapping={})`, with `.full_url` and `.descriptor()` (header NAMES only).
  `HttpResponse(status, headers (lower-case keys), body, received_at, rtt_ms)`, with `.header(name)` and `.descriptor()` (`status, http_date, rtt_ms,
  content_type`). `Fetcher = Callable[[HttpRequest], HttpResponse]`.
- `UrllibFetcher(clock, user_agent, timeout_s, deadline_s, *, max_body_bytes, opener=None)` and `UrllibFetcher.for_source(cfg, clock, user_agent, *, opener=None)`. It
  sends `Accept-Encoding: gzip` and the given User-Agent. An HTTP error status comes back as a response. Transport errors and the deadline raise `SourceError(SOURCE_UNREACHABLE)`; size and gzip problems raise
  `SourceError(SOURCE_INVALID_PAYLOAD)`. Build one fetcher per source (per-source timeouts and caps).
- `classify_status(status) -> OutageKind | None`.
- `PollContext(now, last_ok_at: datetime | None, is_known: Callable[[str], bool], cursor: int = 0)`. `is_known(source_id)` should be
  `state.has_source_id(source, source_id)`; `cursor` is the int stored in kv for vnstock.
- `PollResult(items: tuple[CapturedItem], pages: tuple[CapturedPage], outage_kind=None, detail=None, next_cursor=None, http_status,
  http_date, rtt_ms, received_at)` with `.ok`. Items and an outage can coexist: archive the items AND record the outage. `next_cursor` is None for
  portals; for vnstock, store it in kv after every poll (it is not advanced past a quota or timeout symbol).
- `CapturedPage(body, sha256, request, response, received_at)` (200 responses only): write a snapshot when `sha256` differs from
  `kv[snapshot_kv_key(source)]`.
- `CapturedItem(source_id, payload, payload_format, raw_sha256, byte_count, first_seen_at, request, response, snapshot_sha256)` maps 1:1 onto
  `CaptureRecord`; the poller adds `pit_quality`, `ingested_at`, `cycle_id`, `capture_version`, `information_time=first_seen_at`. Items are
  already deduplicated on (source_id, raw_sha256) within a poll. New key = `not state.is_seen(source, id, sha)`; revised = new key and
  `state.has_source_id(source, id)`.
- `HoseNewsApiSource(cfg, fetcher, *, sleeper)` and `RssFeedSource(cfg, fetcher)`. Each `poll(ctx)` is one attempt with paging inside; it never raises for
  source errors. The poller owns retries (5 s / 15 s backoff) and the `max_poll_duration_s` budget.
- `VnstockEventsSource(cfg, call_events=None, *, limiter, clock, worker=None, environ=None, importer=None)`, plus attribute `library_version`.
  `call_events(symbol, vnstock_source) -> (rows: list[dict], attrs: dict)`. `VendorWorker()` (default) has `.busy`, `.run_with_timeout(fn, timeout_s)` and `.join_current()`;
  `VendorWorkerBusy(TimeoutError)`. `TELEMETRY_ENV` holds the four env defaults.

# P2 developer slice B report (step 6: poller)

Status: DONE. Step 6 is complete and the suite is green. Steps 7-11 are not started, as instructed. I made no git writes and did not touch docs, AGENTS.md or plans.

## Step done, files

| Step | File | Lines | Change |
|---|---|---|---|
| 6 | `src/finsec_alpharadar/capture/poller.py` | 632 (new) | `TokenBucket`, `Watchdog`, `Poller` (`startup`, `tick`, `run_cycle`, `run_forever`), `build_sources`, report dataclasses, kv-key helpers |
| 6 | `tests/test_capture_poller.py` | 850 (new) | 22 tests |
| 6 | `src/finsec_alpharadar/capture/storage.py` | 547 -> 595 | Adds read queries only: `CaptureState.polls(...)`, `CaptureState.backup_row(day)`, dataclass `BackupRow` and private `_select_polls`. `last_poll` now delegates to `_select_polls`; its behaviour is unchanged and all 11 storage tests still pass. |
| 6 | `src/finsec_alpharadar/capture/__init__.py` | 121 -> 145 | Re-exports the poller names and `BackupRow`. The package now exports 64 names, and importing it still loads none of numpy, pandas or vnstock (checked). |

I found no bug in slice A's modules. The only change to slice A code is the storage read queries above.

## RED evidence, then green

| Test file | First failing line (RED) | Final |
|---|---|---|
| `tests/test_capture_poller.py` | `E   ModuleNotFoundError: No module named 'finsec_alpharadar.capture.poller'` | 22 passed |

Every test the phase file lists for `test_capture_poller.py` exists under its exact name (18 tests). I added 4 more:
- `test_items_of_a_failed_poll_are_archived_late`: items of a `coverage_overflow` poll are archived as `late`, because the poll's own outage is opened before `pit_quality` is computed;
- `test_build_sources_wires_only_enabled_sources`: the factory seam, with no network;
- `test_state_lists_polls_in_a_window`: the new `CaptureState.polls` and `backup_row` queries;
- `test_slow_backup_does_not_log_host_stalled` (found in review): it was RED with `host_stalled {"gap_s":400.0}` and is green after the fix. `run_forever` now runs `tick()` (which commits durably) after a heartbeat or backup instead of a bare commit. The gap before the next poll is then bounded by one sleep (≤ 30 s) plus one poll (≤ 330 s), which is under the 390 s alive threshold (LT1).

**Mutation check (manual, all reverted).** I made ten mutations and the tests caught every one:
- no tick between sources;
- `last_ok` anchored on `started_at` instead of `finished_at`;
- a commit without the archive sync;
- `pit_quality` computed before the outage is opened;
- retries on every outage kind;
- orphan outages not closed at restart;
- component exceptions not caught;
- no clock-step check;
- `revised` counted after `add_seen`;
- a snapshot written on every poll.

The first version survived two of them: the `started_at` anchor and pit-before-outage. I then tightened `test_items_after_a_source_outage_are_late_once`, where the recovering response now arrives 2 s after the poll starts, and added `test_items_of_a_failed_poll_are_archived_late`.

**Full suite:** `.venv/Scripts/python -X utf8 -m pytest -q` gives **367 passed** (0 skipped), which is 345 + 22. On the global Python 3.13.14, `python -X utf8 -m pytest -q` gives **332 passed, 1 skipped** (`tests/test_api_server.py`, no fastapi).

**Invariant pre-check (step 9 regex).** `poller.py` contains none of these: `datetime.now(`, `date.today(`, `utcnow(`, `time.time(`, `time.monotonic(`, `time.perf_counter(`, `time.sleep(`, `from time import`, `getpid` or `print(`. It imports only stdlib modules (`json`, `logging`, `os`, `threading`, `dataclasses`, `datetime`, `email.utils`, `typing`) and `capture.*`.

## Behaviour as built (R2.6, R2.7, R2.9)

- **startup()**, in this order:
  1. It runs `repair_tails` over the UTC days from `last_commit_at` (or yesterday) to today.
  2. It runs `reconcile_archive`.
  3. It closes every open outage at `now` with `closed_at_restart=True`.
  4. If there is an `alive` row and `now - last_alive_at > alive_threshold_s`, it opens `process_down` (process-wide) at `last_alive_at` and closes it at `now`. The detail is `{down_s, bytes_cut, files_repaired, previous_pid}`.
  5. It runs `tick()`, which commits.
  6. It returns a `StartupReport`.

  A first-ever run has no alive row, so no outage is opened.
- **tick(now=None, mono=None)**:
  - It opens `host_stalled` when Δmono exceeds `alive_threshold_s` (detail `gap_s`). It opens `clock_step` when |Δwall − Δmono| exceeds `clock_step_threshold_s` (detail `delta_s`).
  - Both are process-wide outages over `[min(prev, now), max(prev, now)]`, opened and closed at once, so a backward step still has start ≤ end.
  - It then calls `set_alive`, then `watchdog.progress(mono)`, then **durable_commit**.
  - Ticks are compared in memory only; the first tick after a start checks nothing.
- **Every commit in poller.py goes through `storage.durable_commit`** (RT12). `tick()` runs between sources (LT1), so each source's JSONL is synced before the commit that follows it. That gives one sync per touched file per commit; `test_archive_is_synced_before_state_commit` checks the event order.
- **run_cycle(*, all_sources=False)**:
  - A source is due when it has no poll yet or `next_due(last_poll.started_at) <= now`.
  - The cycle id is `make_cycle_id(started_at, seq)`, where `seq` is a per-process counter.
  - After each polled source the cycle calls `tick()`.
- **Retries:**
  - Only `source_unreachable` is retried, up to `cfg.retries` times. Backoff goes through the injected sleeper: 5 s, then 15 s for every later retry.
  - Before each backoff the poller checks that `elapsed + backoff <= max_poll_duration_s`. If it does not fit, the poll stops as `source_unreachable` with detail `{reason: "poll_budget", attempts, budget_s, last}`.
  - These are not retried: 401/403/429, invalid payload, coverage overflow and `vendor_*`. A source whose `poll()` raises (a bug) becomes `source_unreachable` with detail `{class, code, message_sha256, message_len, reason: "source_crashed"}` and is not retried. The other sources go on (D2).
- **Records:**
  - Items and pages from ALL attempts are merged: items are deduplicated on (source_id, raw_sha256), and the first attempt wins, so its `first_seen_at` is kept.
  - An item is new when `not is_seen(...)`. It is revised when it is new AND `has_source_id` was true before `add_seen`.
  - A snapshot is written for each page whose sha differs from `kv[snapshot_kv_key]`, updated page by page. The blob day is the UTC date of `ingested_at`.
  - `ingested_at = max(clock.now(), first_seen_at)`, so a backward clock step during a poll cannot break schema v1. That step is logged as `clock_step` at the next tick.
- **pit_quality (R2.6):**
  - `t` is `clock.now()` after the attempts.
  - An item is `late` when the source has no ok poll, or `t > gap_deadline(last_ok)`, or `outages_between(source, last_ok, t)` is non-empty.
  - The poll's own outage is opened FIRST, so items from a failed poll come out late.
  - **The `last_ok` anchor is the ok poll's `finished_at`** (also used as `PollContext.last_ok_at`). A recovering poll closes its outages at `final.received_at or now`, which is ≤ `finished_at`, so the next poll does not intersect them again ("late once").
- **Outages:**
  - A failed poll calls `open_outage(kind, source, start_at=poll started_at, detected_at=now, detail)`. This is a no-op while the same (kind, source) is open, so a failure run gives one outage.
  - An ok poll calls `close_open_outages(source, received_at or now)`, which closes every kind for that source.
  - The vnstock cursor is stored as `kv[cursor:<source>]` after every poll that returns `next_cursor`.
- **polls rows:** each row has `run_mode` and `pid` (both from the constructor), `http_status`, `http_date` and `rtt_ms` of the final attempt, and `skew_ms`. `skew_ms = (received_at − rtt_ms/2) − parsedate_to_datetime(Date)` in ms, or None when the Date header is missing or unparseable.
- **run_forever(stop)**: each iteration does `run_cycle()`, then `tick()`, then the heartbeat if `heartbeat_every_s` has elapsed on the monotonic clock (the first one runs at once), then the backup, then another `tick()` if either side step ran, then a sleep through the injected sleeper. The sleep lasts `min(loop_tick_s, until the next due source, until the next heartbeat)`, or does not happen when that is 0.
  - **Backup:** it runs once local time is ≥ `backup_at_local` (ICT), for the UTC day `now.utc.date() − 1`, and only when `state.backup_row(day)` is None.
  - **Failures:** an exception from `heartbeat_fn` or `backup_fn` is caught, each separately, and logged by class only. It is recorded in `kv[component_status:<name>]` as `error_detail(err, ok=False, at=...)`; a success writes `{"ok": true, "at": ...}`. Capture continues.
- **Watchdog:** described in the API section below. The thread test drives the real daemon thread with bounded `Event.wait` calls only; no test sleeps.

## Deviations (with reasons)

1. **Backup retry throttle.** After a failed or soft-failed backup try, the next try waits at least `heartbeat_every_s` (900 s). The phase text says "retry at the next iteration", which is every 30 s. Retrying every 30 s against a locked sync folder, or a deterministic failure, could pile up `-retry-<n>` files. Main can drop the throttle if the literal reading is wanted (one condition in `Poller._maybe_backup`).
2. **Only D-1 is backed up automatically.** If the process was down for several days, the earlier closed days need a manual `backup --day`.
3. **`tick()` commits (via durable_commit) between sources, not only at the end of the cycle.** The phase says "at the end of the cycle fsync, THEN commit". The invariant, sync before every commit, still holds. Committing per source keeps `alive` durable during a long cycle, so a crash mid-cycle gives an accurate `process_down` start.
4. **The `last_ok` anchor is `finished_at`, not `started_at`.** Without this, the R2.7 rule "close at the first_seen_at of the recovering poll" would make the poll after recovery late too.
5. **The `component_status` field `ok`** means only "the step returned without raising". A soft failure that slice C's `run_backup` catches itself (for example a locked replace) shows `ok: true` there. The `backups` table and slice C's own flags are the source of truth for "verified".
6. **One open outage per (kind, source), not per source.** R2.7 says a failing source opens ONE outage. A source that goes `source_unreachable` and later `source_rejected` without an ok poll in between keeps two open outages, one per kind, until it recovers, and recovery closes both. Coverage and lateness are the same, because the audit unions them. This is slice A's `open_outage` semantics, which I kept.
7. **The vnstock TokenBucket capacity is `symbols_per_poll` [ASSUMED]**, set in `build_sources`, with rate `calls_per_min`. The vnstock source never waits for a token, so capacity 1 would make one sweep take many polls (§3 row 2 expects one sweep per poll). The bound in any 60 s window is capacity + rate (30 + 60 with the shipped values).

## Cross-slice notes (for main / slice C)

- **vnstock poll vs. the poll budget.** `VnstockEventsSource.poll` never checks `max_poll_duration_s` (with `retries = 0`, the poller's budget check never runs). A vendor that is slow but under its timeout could run a poll of up to `symbols_per_poll × vendor_call_timeout_s` (30 × 60 s): that is `host_stalled` past 390 s and a watchdog exit past 900 s. A fix would be a budget-aware limiter or a deadline in `PollContext`; both touch the source contract (slice A), so I left it.
- **A vnstock poll with no token is an `ok` poll that made no call** (slice A's behaviour). At the 180 s cadence the bucket is full again by the next poll, so only repeated `once --all` runs hit it.
- **HOSE multi-page polls rewrite page 1's snapshot** under the single last-sha rule (`kv[last_snapshot_sha256:<source>]`). This is harmless: blobs are content-addressed, at most one extra record per paging poll.
- **Audit (slice C):** R2.12 pairs consecutive ok polls (p, n). The poller anchors `pit_quality` on `finished_at`, so the audit should use the same column for p for consistency. `started_at` is earlier and therefore stricter.
- **Reconcile after a backward clock step.** Lines whose `ingested_at` ≤ `last_commit_at` are skipped by `reconcile_archive`. A duplicate is then possible after a crash, and the schema v1 read rule ("earliest first_seen_at wins") covers it.
- **A single side step longer than 390 s** (for example a very slow `run_backup`) still logs `host_stalled`: the poller cannot tick inside slice C's function. Keep `run_backup` and `write_heartbeat` short, or let them call a progress callback.
- **step 9 invariant:** `poller.py` refers to `os._exit` (the default `exit_fn`) and uses `threading`. Neither is a clock read or a sleep.

## API summary for slice C

Module `finsec_alpharadar.capture.poller`; every name below is also re-exported from `finsec_alpharadar.capture`.

### Constants and kv keys
- `RETRY_BACKOFF_S = (5.0, 15.0)`, `WATCHDOG_EXIT_CODE = 70`.
- `DEFAULT_USER_AGENT = "finsec-alpharadar-capture/<__version__>"` (neutral, no PII).
- `cursor_kv_key(source) -> "cursor:<source>"`: the vnstock round-robin cursor (int text).
- `component_status_kv_key(component) -> "component_status:<component>"`, with `component` ∈ {`"heartbeat"`, `"backup"`}. The value is JSON: `{"ok": true, "at": "<utc>"}`, or `{"ok": false, "at", "class", "code", "message_sha256", "message_len"}` after an exception. The heartbeat can read it for DEGRADED `heartbeat_write_failed` / `backup_write_failed`, alongside its own flags for failures it catches itself.
- Type aliases:
  - `HeartbeatFn = Callable[[datetime], None]`, called with the tz-aware UTC `now`;
  - `BackupFn = Callable[[date, datetime], None]`, called with `(closed UTC day, now)`;
  - `ExitFn = Callable[[int], Any]`;
  - `FetcherFactory = Callable[[SourceConfig], Fetcher]`.

### `TokenBucket(rate_per_min, capacity, clock)`
It is pure and runs on `clock.monotonic()`. It starts full. `try_acquire() -> bool` never waits; there is also a `tokens` property. A non-positive or bool argument raises `ValueError`.

### `Watchdog(clock, limit_s, exit_fn=None, sleep_fn=None, *, interval_s=30.0)`
- `exit_fn` defaults to `os._exit`.
- `progress(mono=None)` is called by `Poller.tick()`.
- `check_once() -> bool` calls `exit_fn(70)` the first time the monotonic idle time exceeds `limit_s`, and returns True from then on.
- `start() -> threading.Thread` (daemon, named `capture-watchdog`) loops `check_once()` and `sleep_fn(interval_s)`. It needs `sleep_fn` (ValueError otherwise) and can be started only once.
- `stop()`.
- **CLI wiring:** `Watchdog(SystemClock(), cfg.schedule.watchdog_limit_s, sleep_fn=system_sleep)`. Pass it to `Poller(watchdog=...)`, call `poller.startup()`, then `watchdog.start()`, then `poller.run_forever(stop_event.is_set)`.

### `build_sources(config, *, clock, sleeper, user_agent=DEFAULT_USER_AGENT, fetcher_factory=None, call_events=None, vendor_worker=None) -> dict[str, CaptureSource]`
- It builds the default sources factory: one adapter per ENABLED source, in config order.
- Portals get `UrllibFetcher.for_source(cfg, clock, user_agent)` unless `fetcher_factory` is given.
- vnstock gets `TokenBucket(calls_per_min, max(1, symbols_per_poll), clock)`, the default `VendorWorker` and the real lazy `call_events` unless injected.
- For `once` tests, inject `fetcher_factory` / `call_events` / `vendor_worker`, or build the dict yourself.

### `Poller(config, sources, *, archive, blobs, state, clock, sleeper, run_mode, pid, capture_version=None, heartbeat_fn=None, backup_fn=None, watchdog=None)`
- **Arguments and attributes:**
  - `sources: Mapping[str, CaptureSource]` must have exactly the enabled source names, else `ValueError`.
  - `run_mode` ∈ {`"run"`, `"once"`} (`ValueError` otherwise). `pid` is an int the CLI passes (`os.getpid()`): poller.py never reads it.
  - `capture_version` defaults to `record.capture_version()`, computed once.
  - Attributes: `config`, `schedule` (a `Schedule`), `archive`, `blobs`, `state`, `clock`, `sleeper`, `run_mode`, `pid`, `capture_version`, `heartbeat_fn`, `backup_fn`, `watchdog`.
- `startup() -> StartupReport(bytes_cut, files_repaired, reconcile: ReconcileReport, orphans_closed: tuple[int, ...], process_down_id: int | None)`. Call it once per process, before anything else, including `once`.
- `tick(now=None, mono=None) -> list[int]` returns the outage ids it opened. It always commits durably.
- `run_cycle(*, all_sources=False) -> CycleReport(cycle_id, started_at, polls: tuple[SourcePollSummary, ...])`. `SourcePollSummary(source, status, outage_kind, attempts, items_total, items_new, items_revised, snapshots_written, pages)` is meant for the `once` summary print (counts only). `once --all` maps to `run_cycle(all_sources=True)`.
- `run_forever(stop: Callable[[], bool]) -> None` returns when `stop()` is True, checked before each iteration and before each sleep.
- **Side-step seams:**
  - `heartbeat_fn(now)` should close over `state`, `config` and the probes, and write the heartbeat file plus the `heartbeats` row.
  - `backup_fn(day, now)` should call `run_backup(root, backup_dir, state, day, now=now)`, which records `backups(day, ...)`.
  - Both run on the main thread with the same `CaptureState` connection; the poller commits after them.
  - The poller's `component_status` kv write lands in the same uncommitted transaction as anything the step wrote before it raised. A partial `heartbeats` row from a step that failed midway would therefore be committed at the next tick. Write the `heartbeats` / `backups` row only after `os.replace` and the verification succeed (R2.10/R2.11 already require this).
  - Exceptions they raise are caught by the poller; slice C's own `OSError` handling inside them (R2.10/R2.11 retry + DEGRADED) stays theirs.

### `CaptureState` queries added (storage.py)
- `polls(source=None, *, since=None, until=None, status=None) -> list[PollRow]`: `since <= started_at < until`, oldest first; `source=None` gives every source. It serves the heartbeat's 24 h counts and skew median/max, and the audit's pairs.
- `backup_row(day: date) -> BackupRow | None` and `@dataclass(frozen=True) BackupRow(day: date, manifest_sha256: str, verified_at: datetime)`.

## Not done / remaining

- Steps 7-11: heartbeat/backup/audit, host/config loader/CLI/`configs/capture.toml`, invariants + repo hygiene + `.env.example`, the dry run, docs.
- Docs main should carry: the `finished_at` anchor, the backup throttle, the vnstock budget risk, and the `component_status` kv keys.

# P2 developer slice C report (steps 7, 8, 9 + main's vnstock budget fix)

Status: DONE. Steps 7-9 are complete and every suite is green. Step 10 (the real-network dry run) and step 11 (docs, commit) were not started, as instructed. I made no git writes and did not touch AGENTS.md, docs/ or plans/.

## Steps done, files

| Step | File | Lines | Change |
|---|---|---|---|
| main's fix | `src/finsec_alpharadar/capture/sources.py` | 709 -> 725 | `VnstockEventsSource(..., max_poll_duration_s=None)`: the poll budget |
| main's fix | `src/finsec_alpharadar/capture/poller.py` | 632 -> 636 | `build_sources` passes `max_poll_duration_s` to the vnstock source |
| main's fix | `tests/test_capture_sources.py` | +38 | `test_vnstock_sweep_stops_at_the_poll_budget` |
| 7 | `src/finsec_alpharadar/capture/heartbeat.py` | 459 (new) | probes, `build_heartbeat`, `write_heartbeat`, canary baseline, `run_heartbeat` |
| 7 | `src/finsec_alpharadar/capture/backup.py` | 262 (new) | `run_backup`, `snapshot_state`, `verify_backup_set`, `iter_backup_members` |
| 7 | `src/finsec_alpharadar/capture/audit.py` | 235 (new) | `audit`, `AuditReport`, `DayReport`, `Gap` |
| 7 | `src/finsec_alpharadar/capture/storage.py` | 595 -> 698 | Cross-slice change, see "Deviations" |
| 7 | `src/finsec_alpharadar/capture/schedule.py` | 96 -> 105 | `Schedule.count_due(start, end, source)`: expected polls, used by the heartbeat and the audit |
| 7 | `tests/test_capture_ops.py` | 956 (new) | 31 tests |
| 8 | `src/finsec_alpharadar/capture/host.py` | 142 (new) | `SystemClock`, `system_sleep`, `KeepAwake`, `AlreadyRunning`, `SingleInstanceLock`, `run_command` |
| 8 | `src/finsec_alpharadar/capture/config.py` | 247 -> 345 | `parse_config`, `load_config_file`, `resolve_dirs`, `BACKUP_DIR_ENV` |
| 8 | `src/finsec_alpharadar/capture/__main__.py` | 385 (new) | the CLI: `main(argv=None, *, env=None, factory=None)` and `CliFactory` |
| 8 | `configs/capture.toml` | 93 (new) | the shipped config |
| 8 | `tests/test_capture_cli.py` | 420 (new) | 15 tests |
| 9 | `tests/test_capture_invariants.py` | 107 (new) | 4 listed tests; 3 of them are parametrized per module, plus 1 extra test: 38 cases |
| 9 | `tests/test_repo_hygiene.py` | +4 | the three `data/capture/...` parameters, green at once |
| 9 | `.env.example` | +2 | the R2.17 comment line and `FINSEC_BACKUP_DIR=`, directly under `FINSEC_DATA_ROOT=` (CRLF kept) |
| - | `src/finsec_alpharadar/capture/__init__.py` | 145 -> 177 | re-exports the new public names (82 in total). Importing it still loads none of numpy, pandas or vnstock (checked with `python -I`). |

## RED evidence, then green

| Test file | First failing line (RED) | Final |
|---|---|---|
| `tests/test_capture_sources.py` (budget test) | `TypeError: VnstockEventsSource.__init__() got an unexpected keyword argument 'max_poll_duration_s'` | 25 passed |
| `tests/test_capture_ops.py`, heartbeat part | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture.heartbeat'` | 9 passed |
| `tests/test_capture_ops.py`, backup part | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture.backup'` | 17 passed (cumulative) |
| `tests/test_capture_ops.py`, audit part | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture.audit'` | 31 passed |
| `tests/test_capture_cli.py` | `ModuleNotFoundError: No module named 'finsec_alpharadar.capture.__main__'` | 15 passed |
| `tests/test_capture_invariants.py` | The modules already existed, so this file was green at once. A mutation check stands in for the RED run. I added a temporary `capture/zz_violation.py` with `import numpy`, `time.sleep(1)`, `from finsec_alpharadar.connectors import ...` and `from finsec_alpharadar import capture`. Result: `3 failed, 38 passed`, one failure each for heavy import, clock and import whitelist. The file was then deleted. | 38 passed |
| `tests/test_repo_hygiene.py` | green at once, as the phase expects (LOCK) | 17 passed |

Every test the phase lists for steps 7-9 exists under its exact name. Extra tests:
- `test_heartbeat_backup_and_audit_commands`, `test_run_takes_the_canary_baseline_and_polls_until_stopped`, `test_build_sources_passes_the_poll_budget_to_vnstock`, `test_single_instance_lock_is_exclusive_and_reusable` (all in the CLI file);
- `test_capture_package_has_the_expected_modules` (invariants).

## Full results

- `.venv/Scripts/python -X utf8 -m pytest -q`: **455 passed** (0 skipped). That is 367 + 1 budget test + 31 ops + 15 CLI + 38 invariants + 3 hygiene parameters.
- `python -X utf8 -m pytest -q` (global 3.13): **420 passed, 1 skipped**. The skip is `tests/test_api_server.py` (no fastapi).
- `PYTHONPATH=src python tests/test_end_to_end.py`: exit 0.
- With `-W default`, my files raise no ResourceWarning. I made the ops tests close their states with an autouse fixture. 27 warnings remain, all in other slices' tests: an unclosed sqlite connection in `test_capture_poller.py::test_slow_backup_does_not_log_host_stalled` and in `test_vnstock_failure_does_not_stop_portal_capture`, plus one in storage and two in the connectors tests. These are invisible in the default run.

## CLI usage lines (for the AGENTS.md command docs)

Every command takes `--config <path>`, which defaults to `<clone>/configs/capture.toml`. `run`, `once`, `heartbeat` and `backup` need `FINSEC_DATA_ROOT` and `FINSEC_BACKUP_DIR`; both must be absolute, must exist, and must be outside the clone or git-ignored. `canary-reset` needs only `FINSEC_DATA_ROOT`. `check-root` checks `FINSEC_DATA_ROOT`, and `FINSEC_BACKUP_DIR` too when it is set. `audit` reads neither variable.

bash:
```
.venv/Scripts/python -m finsec_alpharadar.capture check-root
.venv/Scripts/python -m finsec_alpharadar.capture once --all [--config <copy.toml>]
.venv/Scripts/python -m finsec_alpharadar.capture run
.venv/Scripts/python -m finsec_alpharadar.capture heartbeat
.venv/Scripts/python -m finsec_alpharadar.capture backup --day YYYY-MM-DD
.venv/Scripts/python -m finsec_alpharadar.capture audit --state <backup_dir>/daily/<d>/state-<d>.sqlite --from YYYY-MM-DD --to YYYY-MM-DD [--days trading|all] [--require-days 5] [--out report.json]
.venv/Scripts/python -m finsec_alpharadar.capture canary-reset
```
PowerShell: the same commands with `.\.venv\Scripts\python`. Set the variables first, e.g. `$env:FINSEC_DATA_ROOT='D:\finsec-data'; $env:FINSEC_BACKUP_DIR='C:\Users\<u>\OneDrive\finsec-backup'`. The laptop runs from the clone with `PYTHONPATH=<clone>\src` and the phase 1 locked venv; `-m` works in `.venv` because the package is installed editable.

Exit codes:
- 0: ok;
- 1: the audit failed (the streak is below `--require-days`), or a backup did not verify (for example a locked replace);
- 2: usage or config error, including a missing or unset directory variable, a relative path, a missing folder, a bad `--day`, `backup` of a UTC day that is still open, or `audit` on a WAL-mode (live) state;
- 3: another capture process holds `state/capture.lock`;
- 4: `FINSEC_DATA_ROOT` or `FINSEC_BACKUP_DIR` is inside a git work tree that does not ignore it.

The `once` summary is one header line plus one line per polled source, with counts only and no titles:
`hnx_rss_issuers status=ok outage=- items_total=2 items_new=2 pages=1`.

For step 10 (main): this command answered exit 4 on a path inside the clone (OBSERVED 2026-10-27):
`FINSEC_DATA_ROOT=$PWD/scratch-root .venv/Scripts/python -m finsec_alpharadar.capture check-root`.
It created no directory. The dry run needs a copy of `configs/capture.toml` with `enabled = false` in the `vnstock_events` block, passed with `--config`.

## Deviations and choices (with reasons)

1. **vnstock poll budget (main's decision).** `VnstockEventsSource(..., max_poll_duration_s=None)`.
   - Before every symbol after the first, the sweep stops when `elapsed + vendor_call_timeout_s > max_poll_duration_s`, so the call that would end past the budget is never started.
   - The first symbol is always asked, so a budget shorter than the timeout cannot starve the source.
   - The stop is not an outage: the poll stays ok, `next_cursor` is the first symbol not asked, and `detail` is `{"reason":"poll_budget","symbols_asked":N}`. When there are per-symbol errors, `symbol_errors` is merged into the same JSON object.
   - `None` (the default) means no budget, which keeps slice A's tests unchanged. `build_sources` passes `config.schedule.max_poll_duration_s`, and a CLI test checks that wiring.
2. **storage.py (cross-slice, slice A's file, but in the phase table).**
   - `CaptureState` now subclasses a new `StateReader`. Every read query moved there unchanged; the writes stay in `CaptureState`.
   - `StateReader.open_snapshot(path)` opens a state read-only (`file:...?mode=ro&immutable=1`). It refuses a file whose header bytes 18/19 are 2 with "...is in WAL mode: pass a snapshot from `backup`, not the live state" (RT15).
   - New reads: `outages_overlapping(start, end)`, `all_outages()`, `heartbeat_times(since, until)` and `last_backup()`.
   - New write helpers `replace_with_retry` and `write_atomic`: temp file + sync + replace, 3 retries 2 s apart through the injected sleeper, and the temp file is removed on failure. The heartbeat and the backup share them.
   - All slice A and B tests pass unchanged.
3. **The backup snapshot uses its own SQLite connection, not `state.connection`.**
   - OBSERVED 2026-10-27 on the dev machine: `Connection.backup()` from a connection that holds an open write transaction never returned. I killed the probe after more than 120 s.
   - In `run` the heartbeat writes rows just before the backup, so the live connection can have an open transaction at that point.
   - A second connection reads the last committed state and does not block (OBSERVED, same day). The snapshot therefore holds the COMMITTED state.
   - It is built and switched to `journal_mode=DELETE` under `<data root>/state/.backup-<d>.sqlite.tmp`, then copied into the sync folder. So no `-wal`/`-shm` file is ever created in the sync folder, and the work file is removed afterwards.
4. **Replace retries: one try plus 3 retries, sleeps `[2, 2, 2]`**, for both the heartbeat and the backup. That is at most 6 s per file, which keeps each side step short.
5. **Heartbeat write status in my own kv keys** (`write_status:heartbeat|backup`), because the poller's `component_status:*` says `ok:true` whenever my function does not raise.
   - DEGRADED `heartbeat_write_failed` / `backup_write_failed` fires on either key.
   - `backup_retry_written` (DEGRADED) is set while the last backup had to write a `-retry-<n>` set.
   - The `heartbeats` row is written only after both replaces succeed. It carries `rel_path = heartbeats/heartbeat-<UTC day>.json` and the sha256 of the bytes written.
6. **Heartbeat status edge cases.**
   - DOWN applies only to enabled portal sources that have had an ok poll, judged on `gap_deadline(last_ok.finished_at)`. A portal that never had an ok poll is not DOWN; its outage makes it DEGRADED.
   - No backup row at all gives DEGRADED `no_backup_yet`, so a fresh install reads DEGRADED until the first 07:15 backup.
   - No canary baseline (only `run` takes one) is reported as `baseline_at: null` and does not degrade.
   - A disabled vnstock source never degrades. `days_left` is counted against the local (ICT) date.
   - A failing probe never degrades the status: it shows as `null` plus an error code.
   - Reasons have a fixed order: `down:*`, `open_outage:<kind>:<source|process>`, `skew_median`, `canary_changed`, `vnstock_expiring`, `disk_low`, `no_backup_yet`/`backup_stale`, `heartbeat_write_failed`, `backup_write_failed`, `backup_retry_written`.
7. **Skew rule.** "Median |skew|" is computed as the median of absolute values, in both the heartbeat and the audit. The heartbeat also reports the signed median and the maximum |skew|.
8. **Heartbeat probes.**
   - The stripchart runs at most hourly. The last result, error included, is cached in `kv[w32tm_stripchart]`; a cache dated in the future is ignored and the stripchart runs again.
   - Only parsed offsets are kept, never the raw output (it names the NTP server IP).
   - `w32tm /query /status /verbose` is parsed into 9 whitelisted fields. The parsing assumes English UI labels [ASSUMED]; with other labels every field is null.
   - The registry `PauseUpdatesExpiryTime` data is kept raw.
   - Probe errors are an exception class or `exit_<n>`, never text.
   - The data-root size sums file sizes under `raw/` and `blobs/` only.
   - The key whitelist `HEARTBEAT_KEYS` is a constant, and the test compares every key path recursively; the maps keyed by source, canary file or outage kind collapse to `*`. No paths, symbols or user names appear: the test checks for `tmp_path`, the tickers, the canary content, `USERNAME` and the IP.
9. **Backup choices.**
   - Tar headers have no owner name and uid/gid 0, and the gzip header has no file name.
   - The archive is built in memory, about one day of raw files plus blobs.
   - "Set exists" means any of its three files exists. Verified -> skipped. Not verified -> kept, and the next free `-retry-<n>` set is written; a later run that finds a verified retry set skips.
   - A write failure (`OSError`, sqlite or snapshot check) is returned as `verified=False, error=<class>` and recorded in kv. Only `BackupRefused` (a day not yet closed) raises.
   - Manifest inputs: `lines` is the line count for `.jsonl` files and `null` for blobs.
10. **Audit choices.**
    - `p` and `n` are both the ok polls' `finished_at` (main's anchor).
    - Gap pieces are clipped to begin no earlier than local 00:00 of `date_from`.
    - `days="trading"` judges trading days only. `days="all"` judges every day but applies the "ok poll in 08:00-17:30" rule only on trading days.
    - The streak counts consecutive judged days that pass.
    - Polls of every `run_mode` count (R2.12 does not filter; the R3.3 gate will). Without an alive row, the tail edge is `date_from` 00:00.
    - On a holiday the schedule still polls at the 17:30 boundary, so the holiday has 25 polls and 25 expected. That is slice A's `next_boundary` behaviour, harmless.
11. **Config loader.**
    - Times and dates must be TOML literals (`08:00:00`, `2026-11-24`); a string like `"07:15"` is refused.
    - Unknown keys anywhere are refused by name (a typo guard).
    - `[[holidays]]` uses `date` (mapped to `Holiday.day`).
    - A source without `url` gets `None`, because TOML has no null.
    - `resolve_dirs(config, env, need_backup_dir=...)` uses `resolve_external_dir` for both variables. The backup dir is checked whenever it is set, even when it is not required.
    - In the CLI, a missing folder gives exit 2.
12. **CLI.**
    - `CliFactory` (a dataclass) replaces every collaborator: clock, sleeper, sources factory, `run_command`, keep-awake, clone root, pid, stop, watchdog sleep and exit, and the output streams.
    - Logging handlers (the rotating ops log plus stderr) are attached to the `finsec_alpharadar` logger per command and removed afterwards.
    - `run` follows the phase order: lock -> KeepAwake -> canary baseline -> startup -> watchdog thread -> `run_forever(stop)`. Ctrl+C returns 0. `backup` returns 1 when the backup did not verify (the phase defines exit 1 only for the audit).
    - `canary-reset` opens the state with `busy_timeout_s = alive_threshold_s` (390 s) [ASSUMED sufficient], and takes no process lock.
13. **configs/capture.toml values.**
    - From the brief and the spike report: the HOSE URL and the Origin/Referer/Accept headers as `portal_probe.py` used them; the HNX feed 3 URL; timeouts, deadlines, retries, `page_size`, `max_pages`; the D5 intervals; VN30 as of 2026-10-26 with an as-of comment; 2026-11-24 with the VSDC source and an [ASSUMED] note on the HOSE/HNX notice.
    - `max_body_bytes = 5000000` [ASSUMED].
    - vnstock `timeout_s`/`deadline_s`/`max_body_bytes` are required by the schema but unused [ASSUMED]; `retries = 0` [ASSUMED].
    - `quota_warning_patterns = ["truncat", "quota", "giới hạn", "hạn mức", "cắt"]` [ASSUMED]: the probe's words without the bare "limit"; the cap warning text was not stored.
    - `ignore_warning_patterns = ["cập nhật", "phiên bản", "update", "version"]` [ASSUMED].
    - `symbol_error_codes = ["VNSTOCK_INPUT_SYMBOL_UNKNOWN"]` [ASSUMED for events()].
    - `error_map`: main's three entries.
    - `stripchart_host = "time.windows.com"` (OBSERVED working in the research). There are no 2027 holidays yet (the brief gives none); a comment says to add them.
    - The User-Agent comes from the code (`DEFAULT_USER_AGENT`), not the config.
14. **KeepAwake against the real Windows API** (OBSERVED 2026-10-27, dev machine): the set call returned 0x80000000 and the reset returned 0x80000001, the same as the planner's 2026-10-22 observation. The tests use only fake setters.
15. **Invariant test scope.**
    - The clock regex is exactly the phase's list, and `host.py` must match it.
    - `Thread.join(timeout)`, `os._exit` and `Event.wait` are not on the list.
    - The import whitelist allows the standard library (`sys.stdlib_module_names`), `finsec_alpharadar.data_root`, `finsec_alpharadar.capture[.*]` and `from finsec_alpharadar import __version__` only; relative imports are refused.
    - The heavy-import check covers module-level imports and imports inside module-level `if`/`try` blocks.

## Lint, simplification, tree

- `ruff check --isolated --select F,E501 --line-length 130` (code-standards: no lint gate, lines up to about 130 characters) passes on all my new and changed files.
  - Three lines over 130 remain in `storage.py`: lines 338, 478 and 554. They are slice A's originals and I left them untouched.
- Simplification pass:
  - `backup` and `heartbeat` use `record.sha256_hex` and `sources.Sleeper`; there are no local copies.
  - The expected-poll count lives in one place, `Schedule.count_due`, used by both the heartbeat and the audit.
  - Atomic writes go through `storage.write_atomic` / `replace_with_retry`, shared by both.
- `git status --short` after my work:
  - Modified by me: `.env.example` and `tests/test_repo_hygiene.py`.
  - New, untracked: `configs/capture.toml`, `src/finsec_alpharadar/capture/` (the whole package is untracked), `tests/test_capture_cli.py`, `tests/test_capture_invariants.py`, `tests/test_capture_ops.py`.
  - Already modified before me: `pyproject.toml`, `tests/test_dependency_pins.py` and the two `plans/.../artifacts/*.jsonl` files.
  - There is no stray file: no `zz_violation.py`, no `scratch-root`, no `.tmp`.

## Known behaviours main should know before step 10

- **A same-day audit of the dry run reports the day as FAIL with `unlogged_gap:<source>`, even when capture works.** This is by design (R2.12). The head edge is local 00:00 of `date_from`, and a first-ever start has no `alive` row, so no `process_down` covers the morning before the first poll. With `--require-days 0` the exit code is still 0.
- **The dry-run audit needs a snapshot.** `backup` refuses today (the UTC day is open) and `audit` refuses the live WAL state with exit 2. Make a snapshot of the live state first, for example (bash, `$ROOT` = the dry-run data root, work and output files outside the sync folder):
  `.venv/Scripts/python -c "import sys; from pathlib import Path; from finsec_alpharadar.capture.backup import snapshot_state; Path(sys.argv[3]).write_bytes(snapshot_state(Path(sys.argv[1]), Path(sys.argv[2])))" "$ROOT/state/capture.sqlite" "$ROOT/state/.snap-work.sqlite" "$ROOT/snap-today.sqlite"`
  then `... audit --state "$ROOT/snap-today.sqlite" --from <today ICT> --to <today ICT> --days all --require-days 0`.
  - OBSERVED 2026-10-27 on an empty scratchpad state: the one-liner wrote the snapshot and removed its work file, and the audit printed `FAIL ... longest_consecutive_passing_trading_days=0 required=0` and exited 0.
- **Backup pile-up under a lock that persists.**
  - A set counts as existing when any of its three files exists. If the archive replace succeeded and the state or manifest replace stayed locked, that set does not verify.
  - The poller retries a failed backup every `heartbeat_every_s` (900 s, slice B's throttle). Each retry writes a new `-retry-<n>` set beside the old one, because nothing is ever overwritten.
  - So while the lock persists, a new `-retry-<n>` set can appear every 15 minutes. The heartbeat shows `backup_write_failed` or `backup_retry_written` meanwhile.
  - This follows RT11 (never overwrite, never delete). An owner would see it and clear the lock.

## Not done / for main

- Step 10, the dry run with a copy of the config where vnstock has `enabled = false`, then `audit --state <snapshot or backup> --days all --require-days 0`.
  - `audit` refuses the live WAL state with exit 2. So after `once --all`, run `backup --day <closed UTC day>`, or make a snapshot, before `audit`.
  - A same-day audit cannot use a backup, because only closed UTC days can be backed up. Main needs a snapshot of today's state, for example a Python one-liner calling `capture.backup.snapshot_state(<root>/state/capture.sqlite, <work file>)` and writing the returned bytes to a file. Otherwise the audit can only cover a later day.
- Step 11: the delta docs.
  - Points to carry into the docs: the `finished_at` anchor; `write_status:*` next to `component_status:*`; the separate-connection snapshot (with the OBSERVED hang); the budget semantics; the `-retry-<n>` rule; `count_due`; `StateReader`; and the CLI lines above.
  - The test counts are: venv 455 passed, global 420 passed and 1 skipped.
- The ResourceWarnings in other slices' tests (listed above) could be closed in a follow-up. They do not affect results.
