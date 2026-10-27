# System Architecture

> Read before every `/hs:plan` and `/hs:cook`. Status, verified facts and the decision log live in [AGENTS.md](../AGENTS.md). The adopted roadmap and the owner decisions (§9, all answered by 2026-10-21) live in [docs/project-roadmap.md](project-roadmap.md). Checked against the code on 2026-10-22; decision status updated 2026-10-21.
> **AS-BUILT** = in the code today. **DECIDED** = AGENTS.md "Decisions" or "Engineering Rules (binding)". **PLANNED** = in the adopted roadmap (ADOPTED 2026-10-21) but not built yet; build it in its phase, and change it only through a dated decision.

## Overview
- FinSec AlphaRadar is a research and alerting platform for US (NYSE/NASDAQ) and Vietnam (HOSE/HNX/UPCOM) equities. It aims to turn disclosures, financial statements and market data into probabilities that are scored against what actually happened (target; nothing does this yet).
- It never places orders and is not investment advice (DECIDED, AGENTS.md "Engineering Rules").
- Maturity: **prototype** (AGENTS.md "Current Status", verified 2026-10-22).
  - About 1,450 lines (OBSERVED 2026-10-22) of in-memory Python under `src/finsec_alpharadar/` (`requires-python >=3.11`, `pyproject.toml:10`). The only third-party imports are numpy and fastapi (`api/server.py:5`).
  - The only HTTP client in `src/` is `capture/sources.py` (urllib GET requests to the HOSE API and the HNX RSS feed, 2026-10-27); there is no WebSocket client or LLM SDK, and the only database is the capture state (stdlib `sqlite3`). The environment-variable reads are `FINSEC_DATA_ROOT` and `FINSEC_BACKUP_DIR`, through `resolve_data_root` / `resolve_external_dir` in `data_root.py`, called by the capture CLI (2026-10-27).
  - No prediction has been made or scored yet.
  - Tests (OBSERVED 2026-10-27): pytest collects 455 tests from `tests/`: the `test_end_to_end()` smoke wrapper, the guard modules `tests/test_no_order_execution.py` and `tests/test_agents_md_paths.py`, and the unit modules `tests/test_vn_price_band.py`, `tests/test_connectors.py`, `tests/test_rag_as_of.py`, `tests/test_forensics_guards.py`, `tests/test_api_server.py` (handlers called directly; skipped without fastapi), `tests/test_data_root.py` (2026-10-27), the eight capture modules `tests/test_capture_*.py` (synthetic payloads only; `tests/test_capture_invariants.py` locks the import and clock rules; 2026-10-27), and the repository checks `tests/test_repo_hygiene.py` and `tests/test_dependency_pins.py` (2026-10-27). `pyproject.toml` sets `pythonpath = ["src"]` and `testpaths` in `[tool.pytest.ini_options]`; all 455 pass in `.venv` (Python 3.13). CI: `.github/workflows/ci.yml` runs them on Python 3.11 and 3.13 for pull requests and pushes to `main`. `make test` (`Makefile:6-7`) runs `python -m pytest -q` (2026-10-22).
- Shape today: independent engines plus a 3-endpoint FastAPI app. Target shape (PLANNED): a disclosure poller feeding an append-only prediction ledger that is scored automatically (see Data flow).

## Components

### As-built (`src/finsec_alpharadar/`)
| Path | Job | Status (checked 2026-10-22) |
|---|---|---|
| `domain/models.py` | `MarketRegion`, `VNExchangeBand`, `IssuerType` enums; `MarketTick`, `RegulatoryFiling`, `FinancialStatementSnapshot` dataclasses; `TickValidationError` and its subclasses, `vn_tick_size`, `vn_price_limits` | Real. Times are float epoch seconds (`timestamp` line 135, `published_at` line 219), not tz-aware datetimes. Static exchange-to-band map (lines 14-17). Since 2026-10-21 every tick rejects non-finite numbers, and a VN tick needs a listed exchange, a positive reference and a price within `price_limits` or within the band widened by one tick (decision D4; lines 148-191). Since 2026-10-22 `FinancialStatementSnapshot` is frozen and carries `issuer_type` (default `UNKNOWN`, line 277). Since 2026-10-22 an unknown market or a VN price of 0 or below raises too. |
| `data_root.py` | `resolve_data_root`, `resolve_external_dir`, `find_work_tree`: absolute external directory from an environment variable, refused inside a git work tree unless git ignores it | Real (2026-10-27); used by `scripts/spike/` and the capture CLI. Resolves links before the parent walk; a `git check-ignore` failure is an error, never an acceptance. |
| `capture/record.py` | Raw record schema v1: `CaptureRecord`, `RecordType`, `PitQuality`, `OutageKind`, JSONL line codec, `canonical_item_bytes`, `capture_code_digest` | Real (2026-10-27). `__post_init__` refuses naive or non-UTC times, `information_time != first_seen_at`, a bad sha256 or byte count, and mixed item/snapshot shapes. |
| `capture/config.py` | Frozen config dataclasses and the `configs/capture.toml` loader (`tomllib`) | Real (2026-10-27). Refuses a source that breaks D5, an unknown kind, `vnstock_source = "kbs"`, and an `error_map` value that is not an `OutageKind`. |
| `capture/schedule.py` | `Schedule`: trading days, active window, `next_due`, `gap_deadline`, `count_due` | Real (2026-10-27). Asia/Ho_Chi_Minh through zoneinfo (tzdata); holidays from the config only. |
| `capture/sources.py` | `UrllibFetcher`, `HoseNewsApiSource`, `RssFeedSource`, `VnstockEventsSource`: fetch, extract ids, classify errors | Real (2026-10-27). Total per-request deadline and a body cap (gzip included); RSS split by regex, no XML parser; vnstock imported lazily in a single timed worker thread, swept round-robin within a token bucket and the poll budget. |
| `capture/storage.py` | `RawArchive` (JSONL), `BlobStore`, `CaptureState`/`StateReader` (SQLite WAL), `durable_commit`, `reconcile_archive` | Real (2026-10-27). fsync before commit; torn-tail repair; snapshots open read-only with `immutable=1`. |
| `capture/poller.py` | `Poller` (`startup`, `tick`, `run_cycle`, `run_forever`), `TokenBucket`, `Watchdog`, `build_sources` | Real (2026-10-27). Applies the `pit_quality` rule, opens one outage per failure run, detects process down, host stalls and clock steps; the watchdog thread exits 70 when ticks stop. |
| `capture/heartbeat.py` | `build_heartbeat`, `write_heartbeat`: OK/DEGRADED/DOWN JSON with a fixed key whitelist | Real (2026-10-27). w32tm and Windows Update probes through an injected `run_command`; a locked replace degrades, never raises. |
| `capture/backup.py` | `run_backup`, `snapshot_state`, `iter_backup_members` | Real (2026-10-27). Closed UTC days only; the SQLite snapshot comes from a separate connection and is switched out of WAL; the reader checks the manifest sha256 and a member whitelist and never extracts. |
| `capture/audit.py` | `audit`: unlogged gaps, ok polls in the active window, heartbeats and clock skew per day | Real (2026-10-27). Reads a snapshot only; virtual edge pairs make trailing silence a gap. |
| `capture/host.py` | `SystemClock`, `system_sleep`, `KeepAwake`, `SingleInstanceLock`, `run_command` | Real (2026-10-27); the only capture module that reads the clock or sleeps. |
| `capture/__main__.py` | CLI `python -m finsec_alpharadar.capture` | Real (2026-10-27): `run`, `once`, `heartbeat`, `backup`, `audit`, `check-root`, `canary-reset`; one process lock for every archive writer. |
| `capture/__init__.py` | Re-exports the public names | Real (2026-10-27); imports no numpy, pandas or vnstock. |
| `connectors/us_alpaca_edgar.py` | `AlpacaStreamAdapter`, `SecEdgarFilingAdapter`: dict to `MarketTick` / `RegulatoryFiling` | Stub. Static parsers, no client, called only by tests. Since 2026-10-21 the Alpaca parser requires trade `S, p, s, t, x` and quote `S, t, bp, ap, bs, as`, raises `PayloadFieldError` (`connectors/errors.py`) on a missing, null or malformed field, and rejects a quote for another symbol or later than the trade (lines 46-81). |
| `connectors/vn_ssi_vnstock.py` | `SSIFastConnectAdapter`, `VnstockBCTCAdapter` | Stub, as above. Since 2026-10-21 the SSI parser requires its 10 tick fields (names ASSUMED until the Phase 6 R-channel spec), turns `Ceiling` and `Floor` into `price_limits`, and holds the VERIFIED `STREAM_URL`/`REST_URL` (lines 36-37) instead of the old, wrong `WS_ENDPOINT`. Absent foreign volumes still read as 0.0; a null, non-numeric or too-large one raises a bare `TypeError`/`ValueError`/`OverflowError`, and a bool is read as a number (`True` -> 1.0), unlike every required number field (Phase 6). |
| `quant/event_study.py` | `EventStudyAnalyzer.compute_car`: OLS market-model AR/CAR and t-stat | Real, test-only (no caller in `src/`). Bare arrays: no timestamp alignment or window checks. |
| `quant/kalman_microstructure.py` | `KalmanPriceFilter`, `StreamingQuantEngine`: VWAP, OBI, CUSUM, volume z-score, VN foreign net flow, alert reasons | Real; wired to `POST /v1/ticks/ingest`. Session-blind. Foreign flow is summed over the 50-tick deque (line 99). Deferring that fix until the SSI field semantics are read (Phase 6) is planned (roadmap §5.2); `tests/test_end_to_end.py:94-95` feeds increments. |
| `quant/deduplicator.py` | `MinHashDeduplicator`: near-duplicate text check | Real, test-only. Linear scan (lines 38-42) despite the "O(1)" docstring (line 10). |
| `forensics/xbrl_bctc_auditor.py` | `ForensicAccountingEngine.evaluate`: Altman Z, 8-variable Beneish M, 9-point Piotroski F | Real, test-only. Since 2026-10-22 an issuer gate runs before any computation (lines 243-246): only `IssuerType.NON_FINANCIAL` is scored; a bank, insurer or securities firm gets `not_applicable_financial_issuer`, and any other issuer type `issuer_type_unknown` (fail-closed). The three scores are independent: each is None with a reason in `unavailable_reasons` when a field its formula reads is not finite, one of its ratios is undefined, or the score itself comes out non-finite (`score_not_finite`) (lines 101-212), and no denominator is clamped. Formula corrections are roadmap Phase 2. |
| `rag/hybrid_temporal_rag.py` | `HybridTemporalRAGEngine`: BM25 + RRF + exponential time decay; sentence-level diff | BM25, RRF and decay are real. The "dense" and ColBERT MaxSim legs use per-token hash vectors (lines 54-59) with no semantics. Since 2026-10-21 `search` takes a required keyword-only `as_of` and never reads the clock: it drops filings with `published_at > as_of` before ranking (line 137), refuses a non-finite one (lines 135-136), and computes the BM25 statistics over the visible filings only (lines 80-108). |
| `router/linucb_bandit.py` | `LinUCBModelRouter`: disjoint LinUCB over model-name arms | Math is real, but no model is ever called. Only the smoke test calls `update_reward` (hard-coded 0.98, test:151); the app never does, so `GET /v1/rag/search` always picks the first arm (line 40). |
| `api/server.py` | FastAPI: `GET /health`, `POST /v1/ticks/ingest`, `GET /v1/rag/search` | Thin. No WebSocket, no auth, untyped `Dict[str, Any]` body (line 82), hard-coded unverified model IDs (lines 19-21). Since 2026-10-21 ingest requires its tick fields and returns 422 on a missing or invalid one (lines 37-95), both research responses carry the bilingual `DISCLAIMER` (line 24), and search takes an optional `as_of` (default: the request time) and echoes it, with 422 for a non-finite one (lines 104-130). fastapi installs into `.venv` with the dev setup (AGENTS.md). |

Outside `src/`:
- `scripts/train_unsloth_slm.py` only prints a hard-coded training recipe; it does not read `configs/unsloth_qwen3_catalyst.yaml`. Stub.
- `docker-compose.yml`: Qdrant (profile `rag`) and TimescaleDB (profile `ticks`); no code uses them. Redpanda was removed (2026-10-22, §9.9).
- `harness/`: vendored SDLC harness v7.2.0 (`/hs:plan`, `/hs:cook`, `/hs:test`, `/hs:ship`). Not product code.
- `.agents/`: another agent tool's agent definitions, skills, rules, hooks and MCP config. DECIDED (§9.13, 2026-10-21): keep it, with its rules rewritten as pointers to AGENTS.md in Phase 0.

### Target modules (PLANNED, roadmap §5.2; none exist yet; `capture/` was built 2026-10-27)
Planned layering (roadmap §5.1): connectors (I/O only) -> store -> features/labels (pure functions taking `as_of`) -> modeling -> ledger -> api/notify (read-only).

| Path | Job | Phase |
|---|---|---|
| `connectors/base.py` | SourceAdapter protocol, RawRecord, token-bucket limiter, retry with backoff, idempotent key (source, source_id) | 1 |
| `store/` | Raw archive writer, operational schema, research views, `as_of()` (SQLite WAL + DuckDB over raw JSONL/Parquet; DECIDED §9.9) | 1 |
| `refdata/` | Security master, exchange membership (incl. the 2026-12-28 HNX-to-HOSE transfer), sectors, corporate actions | 1, 3 |
| `calendar/` | XNYS and VN sessions, holidays, regime flags | 1, 3 |
| `labels/` | Reaction and drift labels wrapping `quant/event_study.py`; outsized, censor, confound and stale flags | 1 |
| `features/` | As-of features: event type, pre-event run-up, size, SUE, forensic components, VN foreign flow / ADV | 2-3 |
| `modeling/` | Baselines, L2 logistic, isotonic calibration | 2 |
| `backtest/` | Walk-forward, metrics, US/VN costs, report | 2 |
| `ledger/` | Append-only predictions and outcomes with a hash chain; nightly scorer | 2 |
| `pipeline/__main__.py` | CLI: `backfill`, `poll`, `predict`, `score`, `digest`, `scoreboard` | 1-4 |
| `notify/telegram.py` | Private Telegram 1:1 alerts to one allowlisted chat id (DECIDED §9.8) | 4 |

Also planned: move `router/` to `experimental/` (Phase 0); rewrite `api/server.py` as read-only GET endpoints bound to localhost (Phase 4).

## Data flow

### As-built
```
POST /v1/ticks/ingest   raw JSON dict (server.py:81-95)
  -> MarketTick built inline, NOT via connectors/ (server.py:37-73): required symbol, market, timestamp, price, volume,
     bid/ask prices and sizes; optional exchange (null = absent), reference_price, price_limits [floor, ceiling]
  -> MarketTick.__post_init__ (models.py:148-191): finite numbers; a VN tick needs HOSE/HNX/UPCOM, a reference, a price in range
     a missing or invalid field, or a VN tick without an exchange, returns 422 (server.py:90-95)
  -> StreamingQuantEngine.process_tick (server.py:96)
  -> {"signal": QuantSignalReport.__dict__, "vn_ceiling_floor": [ceiling, floor] or null, "disclaimer"} (server.py:97-101)

GET /v1/rag/search      (server.py:104-130)
  -> decision time = the as_of query parameter, or the request time when absent (the only clock read on this path)
  -> LinUCBModelRouter.select_model: the chosen arm is returned, never called
  -> HybridTemporalRAGEngine.search(as_of=decision time): drops filings with published_at > as_of before ranking;
     always [] because nothing ingests into the app's engine
  -> {"routed_model", "router_ucb_scores", "results", "as_of", "disclaimer"}

python -m finsec_alpharadar.capture once|run   (capture/, 2026-10-27; not yet deployed)
  -> per due source: fetch (HOSE API pages, HNX RSS feed 3, vnstock events in a worker) -> extract ids only
  -> snapshot blob when the body changed; new or revised items with pit_quality -> raw/<source>/<UTC day>.jsonl
  -> polls row, outages opened or closed -> fsync touched files -> commit state/capture.sqlite
  run also: tick every <= 30 s, heartbeat every 900 s, backup of the closed UTC day at 07:15 ICT, watchdog thread

tests/test_end_to_end.py only:
  connectors parse 2 ticks (asserted only, test:40-59) and 2 filings -> HybridTemporalRAGEngine (test:124-145)
  hand-built MarketTicks -> StreamingQuantEngine (test:71-96)
  hand-built FinancialStatementSnapshot -> ForensicAccountingEngine (test:106)
  raw arrays -> EventStudyAnalyzer (test:101-103)
  raw strings -> MinHashDeduplicator (test:63-64) and LinUCBModelRouter (test:149-152)
```
Where state lives:
- Process memory only: module-level singletons in `api/server.py:17-21`, and per-instance dicts/lists in `kalman_microstructure.py:61-63`, `hybrid_temporal_rag.py:46-48`, `deduplicator.py:20`, `linucb_bandit.py:15-16`.
- Nothing in the API path is persisted or replayable. A restart, or each `--reload` from `Makefile:10`, drops all state; multiple uvicorn workers would each hold separate state.
- The capture poller persists everything under `FINSEC_DATA_ROOT` and `FINSEC_BACKUP_DIR` (schema below).

### Raw capture record schema v1 (`capture/record.py`, 2026-10-27)
The contract that the Phase 1 `store/` and the Phase 3 parser read. One JSON object per line, UTF-8, Vietnamese text kept exactly
as received (no normalisation), written with `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` plus
`\n`. Every instant is a UTC string `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | `1` |
| `record_type` | `"item"` \| `"snapshot"` | item = one disclosure or row; snapshot = one whole response body |
| `source` | str | source name in the config (`hose_news_api`, `hnx_rss_issuers`, `vnstock_events`) |
| `source_id` | str | the source's own id (HOSE `"2506861"`, HNX guid `"638933"`, vnstock `"vci:<id>"`); snapshot: `"sha256:<raw_sha256>"` |
| `information_time` | str UTC | v1: always equal to `first_seen_at` (provisional; the parser recomputes it) |
| `information_time_basis` | `"first_seen"` | the only v1 value |
| `first_seen_at` | str UTC | when the response holding the record arrived (headers, injected clock) |
| `ingested_at` | str UTC | when the record was written; never before `first_seen_at` |
| `pit_quality` | `"first_seen"` \| `"late"` | `late` on a source's first-ever poll, after its gap deadline, or after any outage since its last ok poll |
| `raw_sha256` | 64 lower-case hex | item: sha256 of `canonical_item_bytes(payload)`; snapshot: sha256 of the body as received (after gunzip) |
| `payload_format` | `"json"` \| `"text"` \| `"blob"` | HOSE and vnstock items json; RSS items text (the `<item>` fragment verbatim); snapshots blob |
| `payload` | JSON \| str \| null | item: the verbatim value; snapshot: null |
| `blob_path` | str \| null | snapshot: `blobs/<YYYY-MM-DD>/<sha256>.bin`, relative to the data root |
| `byte_count` | int | bytes of the raw item or body |
| `snapshot_sha256` | str \| null | item: the body that held it (null for vnstock) |
| `request` | object | no secrets: HTTP `{method, url, params, header_names}`; vnstock `{call, args, library_version}` |
| `response` | object | HTTP `{status, http_date, rtt_ms, content_type}`; vnstock `{rows, attrs}` |
| `cycle_id` | str | `<started_at %Y%m%dT%H%M%S%fZ>-<seq>` |
| `capture_version` | str | `<__version__>+<12 hex of capture_code_digest>`, the digest over `capture/*.py` with CRLF read as LF |

- **Read rule.** For one key `(source, source_id, raw_sha256)` the record with the earliest `first_seen_at` wins; a later duplicate
  (if a crash ever let one through) is ignored. A changed payload of a known `source_id` is a new key: every version is kept.
- **On disk.** `$FINSEC_DATA_ROOT/raw/<source>/<YYYY-MM-DD>.jsonl` (UTC day of `ingested_at`, append-only),
  `blobs/<YYYY-MM-DD>/<sha256>.bin`, `state/capture.sqlite` (WAL, `synchronous=FULL`), `state/capture.lock`, `ops/capture.log`
  (ids, counts and times only). `$FINSEC_BACKUP_DIR/heartbeat.json`, `heartbeats/heartbeat-<d>.json` and
  `daily/<d>/{capture-<d>.tar.gz, state-<d>.sqlite, manifest-<d>.json}`; nothing is ever deleted or overwritten.
- **Durability.** Each source's new blobs and touched JSONL files are fsynced before the state commit that follows its poll;
  `startup()` truncates a torn JSONL tail and replays archived lines newer than the last commit into `seen`, so a crash between
  the fsync and the commit cannot write a second record.
- **State tables.** `meta`, `seen(source, source_id, raw_sha256, first_seen_at)`, `polls` (status, outage kind, HTTP status and
  date, rtt, skew, item counts, pages, `run_mode`, `pid`), `outages(kind, source NULL = process-wide, start_at, end_at NULL = open)`,
  `blobs`, `alive`, `heartbeats`, `backups`, `kv` (vnstock cursor, last snapshot sha per source, canary baseline, component status).
  `detail` columns hold the error class, `code`, and the sha256 and length of a vendor message, never its text.
- **Clock.** `skew_ms` = header arrival (local clock) minus the server `Date` header. HNX holds its headers for 40-49 s and stamps
  `Date` when it sends them, so the send/receive midpoint would read half the response time as skew (OBSERVED 2026-10-27).

### Target core loop (PLANNED, roadmap §3)
```
capture -> parse -> as-of features -> predict -> ledger -> alert -> score -> scoreboard
```
- **capture:** EDGAR and VN disclosure polls every 2-5 min; raw archive with `raw_sha256` and `first_seen_at`.
- **parse:** typed Event with `information_time`, day 0, event type, `pit_quality`, confound/stale flags, citation URL.
- **features:** as-of only; inputs must have `available_at` < decision time (`information_time` for R1, entry time for drift targets; roadmap §2.1, §6.4).
- **predict:** baselines and L2 logistic, calibrated on train folds only.
- **ledger:** append-only row (mode backtest/shadow/live, model_version, feature_hash, hash chain), written before the outcome exists.
- **alert:** private card with probability vs base rate, citation and disclaimer (private Telegram 1:1 bot, Vietnamese-first; DECIDED §9.8, §9.12).
- **score / scoreboard:** a nightly job fills matured outcomes; `reports/scoreboard.md` and `GET /v1/scoreboard`.
- DECIDED (§9.3, 2026-10-20): in Phases 0-4 "real-time" means disclosure detection latency (p50 under 5 min US, under 10 min VN). Tick streaming is Phase 6, after the owner checkpoint that follows Phase 4. Do not plan live tick streaming (Alpaca IEX / SSI FastConnect clients, tick storage) before then. Fixes that binding rules require in existing tick-path code (band validation, no `now()` fallback) are not streaming work; deferring other `quant/kalman_microstructure.py` fixes is planned (roadmap §5.2), not covered by §9.3.
- DECIDED (2026-10-21): US pipeline first, VN capture-only poller from week 1 on the always-on capture laptop (§9.2, §9.4); embedded storage (§9.9).

## External dependencies
Only the VN portals and vnstock are called from `src/` (by `capture/`, 2026-10-27). Labels and URLs are copied from AGENTS.md "Verified Facts"; add any new fact there first, with a label and a date.
- **SEC EDGAR** (US filings: 8-K, 10-Q, 10-K, Form 4).
  - Fair access: at most 10 requests/s with a declared User-Agent. VERIFIED https://www.sec.gov/os/accessing-edgar-data
  - Information time: the filing header `<ACCEPTANCE-DATETIME>`, read as America/New_York. The submissions JSON `acceptanceDateTime` is for discovery only; its "Z" did not match UTC. OBSERVED 2026-10-20; the Phase 0 golden test settles the header side.
  - User-Agent is meant to come from `EDGAR_IDENTITY` (Planned, roadmap §5.2). `.env.example:5` holds a placeholder, `.agents/mcp_config.json:23` hard-codes it, and nothing in `src/` reads it.
- **Alpaca Market Data, Basic plan** (paper keys).
  - IEX-only real-time data, 30 WebSocket symbols, 200 historical calls/min. VERIFIED https://docs.alpaca.markets/docs/about-market-data-api
  - Bars `adjustment` defaults to raw; request `all` for return labels. VERIFIED https://docs.alpaca.markets/reference/stockbars
  - SIP history on Basic: ASSUMED.
- **SSI FastConnect Data** (VN; live use is Phase 6 per §9.3. No SSI account or client before Phase 6: DECIDED §9.5, 2026-10-21).
  - Needs an SSI account plus ConsumerID and ConsumerSecret (no private key). REST `https://fc-data.ssi.com.vn/v2.0/Market`; streaming `https://fc-datahub.ssi.com.vn/v2.0`. VERIFIED https://guide.ssi.com.vn/ssi-products/tieng-viet/fastconnect-data/huong-dan-ket-noi
  - The old code constant `wss://stream.ssi.com.vn` was wrong; fixed 2026-10-21 (`STREAM_URL`/`REST_URL`, `vn_ssi_vnstock.py:36-37`); never reuse it. `SSI_PRIVATE_KEY_PATH` is not needed for FC Data and was removed from `.env.example` (2026-10-22).
- **vnstock** (VN prices, statements, events over unofficial broker endpoints).
  - Community licence: personal, study or research use only, no redistribution. VERIFIED https://github.com/thinh-vu/vnstock
  - Gen 5 installs only from the vendor index https://vnstocks.com/api/packages; the PyPI project is quarantined (OBSERVED 2026-10-22). Pinned `==5.1.0` in the `capture` extra (`pyproject.toml:36-38`), installed only through the hash lock `requirements/vnstock/win-py313.lock` in a venv outside the clone (2026-10-27). Imported only lazily inside `capture/sources.py` `VnstockEventsSource`, never at module level.
  - Probe results (OBSERVED 2026-10-26; `docs/phase0-spike-report.md` §1):
    - adjusted prices in thousands of VND, and no raw bars (a Phase 3 blocker);
    - history capped at 2,920 calendar days;
    - no throttle at a 0.5 s cadence;
    - `news()` removed, and `events(source="kbs")` broken.
  - VN disclosure portals (Phase 2 capture, DECIDED 2026-10-27): HOSE API listing plus HNX RSS feed 3 (`docs/phase0-spike-report.md` §2), polled by `capture/sources.py` (2026-10-27).
- **tzdata** (core dependency since 2026-10-27, no version bound): Windows has no system IANA database, so zoneinfo needs the package for Asia/Ho_Chi_Minh and America/New_York.
- **Telegram Bot API**: the alert channel (DECIDED §9.8; built in Phase 4). It is not in AGENTS.md "Verified Facts"; see roadmap §4 for its source.
- **LLM providers**: none called. The model IDs in `api/server.py:20` and `tests/test_end_to_end.py:149` are unverified. Provider choice is deferred to the Phase 5 gate (DECIDED §9.11); extraction starts rule-based.
- **docker-compose services** (`docker-compose.yml`): Qdrant (6333/6334, profile `rag`) and TimescaleDB (5432, profile `ticks`). Unused. Since 2026-10-22 the image tags are pinned, the password comes from `POSTGRES_PASSWORD` in `.env` (compose refuses an empty one), and every port binds 127.0.0.1 only. Per §9.9 and roadmap §5.1: Redpanda is removed, TimescaleDB waits for Phase 6 ticks, Qdrant for Phase 7 at the earliest.

## Boundaries & trust
- **Exposed surface:** the FastAPI app `finsec_alpharadar.api.server:app` on port 8000.
  - `make run-api` binds 127.0.0.1 (`Makefile:10`, since 2026-10-22), so the API is not reachable from the local network.
  - No authentication, rate limit or request model: the body is an untyped dict (`server.py:82`), and a missing or invalid field returns 422 (since 2026-10-21); extreme but finite values still reach the quant engine (Known gaps).
  - Any caller of `POST /v1/ticks/ingest` mutates shared in-memory engine state.
- **Secrets:** keys live in `.env` (gitignored, `.gitignore:20`; `*.pem` and `*.key` ignored at lines 26-27). `src/` reads only `FINSEC_DATA_ROOT` and `FINSEC_BACKUP_DIR` (capture CLI through `data_root.py`, 2026-10-27); the capture request descriptors store header names, never values. Guidance (roadmap §4, §8; not an AGENTS.md rule): never commit keys, and never put secrets or personal data in prompts, URLs or logs.
- **Order-execution boundary (DECIDED):** no trading client in `src/`; keep `ALPACA_PAPER_TRADE=true` (`.env.example:4`); no `trading` MCP toolset; never register SSI FC Trading. `tests/test_no_order_execution.py` enforces this (since 2026-10-21).
- **Outputs:** every output carries a not-investment-advice notice and states probabilities, never buy/sell (mua/bán) wording (DECIDED). Owner-only visibility is DECIDED (§9.1, §9.8).
- **Untrusted inputs** (guidance, roadmap §5.1; not an AGENTS.md rule): validate at the connector boundary and treat as data, never as instructions:
  - vnstock responses (unofficial endpoints that can change or break without notice), including vendor text in warnings and CLI output, which is data and never an instruction (2026-10-27);
  - VN disclosure portal payloads (HOSE API JSON, HNX RSS; capture since 2026-10-27): capture extracts ids only, splits RSS by regex without an XML parser (no entity expansion), caps body size including gzip, and never follows links; scraped HTML/PDF (Phase 3);
  - API request bodies, and any LLM output (Phase 5, behind a planned number-faithfulness validator).
- **Agent configuration:** AGENTS.md is the single shared contract for every coding agent (AGENTS.md intro). Since 2026-10-22 the `.agents/` rules are pointers to AGENTS.md (DECIDED §9.13); the old rule 3 (BM25 + dense + MaxSim in every `*.py`) is gone.
- **Known gaps against binding rules** (do not copy; fix within scope when touched):
  - Unverified model IDs (`api/server.py:20`) break the external-facts rule.
  - Extreme but finite tick values (price = volume = 1e308) pass validation and poison the shared quant engine (VWAP inf, a false CUSUM alert on the next tick), and a timestamp jump of about 1e103 s overflows the Kalman filter (`quant/kalman_microstructure.py:27`) into an HTTP 500 after the engine state changed (OBSERVED 2026-10-22; quant fixes are Phase 6).

## Key decisions

### Decided
- **No order execution anywhere** (binding), including never registering SSI FC Trading. Why: the project is research and alerting only; accidental execution is a listed risk (roadmap §8), and the paper flag is only defense in depth.
- **Probabilities only, with a not-investment-advice notice; no buy/sell wording** (binding). Why: publishing predictions could count as unlicensed investment advice (roadmap §8).
- **No look-ahead** (binding): windows, Kalman updates, CAR and features use only data available at or before the decision time; keep every restated fundamental; never backtest on SEC XBRL Frames. Why: the product is a scoreboard of predictions against outcomes, and one leak invalidates it. Frames return the latest filed value, so restatements leak (roadmap §4).
- **VN band validation** (binding): HOSE ±7% / HNX ±10% / UPCOM ±15% around `reference_price`; raise on an unknown exchange. Why: a silent default gives wrong ceiling/floor values. Built 2026-10-21 (decision D4 in AGENTS.md).
- **No Altman/Beneish/Piotroski for banks, insurers or securities firms** (binding). Why: the auditor's ratios are not valid for these firms. Built 2026-10-22: only `IssuerType.NON_FINANCIAL` is scored, and an unknown issuer type is refused (AGENTS.md "Decisions"). They stay in the research universe and event studies (DECIDED §9.7).
- **Label external facts VERIFIED, OBSERVED or ASSUMED; never invent API names, fields, model IDs or prices** (binding). Why: the prototype carried a wrong SSI endpoint (fixed 2026-10-21) and still carries unverified model IDs.
- **No package named `data/` or `models/`; use `modeling/`** (binding until `.gitignore` is anchored). Why: the unanchored patterns would silently keep such packages out of git (OBSERVED 2026-10-20). Precondition met 2026-10-22: `.gitignore:32-33` now anchor them as `/data/` and `/models/`; `modeling/` stays the planned name.
- **"Real-time" = disclosure capture first; ticks in Phase 6** (owner, 2026-10-20, §9.3). Why: free tick feeds are thin (Alpaca Basic is IEX-only with 30 WebSocket symbols), and VN shares cannot be sold before the T+2 afternoon, so intraday single-stock forecasts cannot be acted on.
- **AGENTS.md holds all project knowledge; `CLAUDE.md` only imports `@AGENTS.md`, plus the generated harness block** (owner, 2026-10-20; AGENTS.md "Decisions"). Why: one contract for every coding agent. Record module status, decisions and verified facts in AGENTS.md in the same change as the code (AGENTS.md "Agent Memory Protocol").

### Decided by the owner on 2026-10-21 (roadmap §9; rationale there)
- Personal research; outputs are owner-only; demos use replayed data and derived signals only (§9.1).
- US pipeline first; VN capture-only poller from week 1; VN is the main research market from Phase 3 (§9.2).
- VN capture runs on a spare always-on laptop configured as a server: no sleep, lid = do nothing, mains power, NTP, daily heartbeat, nightly off-machine backup; gaps mark events `pit_quality = late` (§9.4).
- No SSI account or client before Phase 6; FC Data credentials only (§9.5). Never registering FC Trading was already binding.
- $0 budget plus the capture laptop through Phase 4 (§9.6).
- Broad point-in-time, ADV-ranked universe plus a live watchlist of 30 or fewer per market (§9.7).
- Private Telegram 1:1 bot to one allowlisted chat id (§9.8).
- Embedded storage (raw JSONL/Parquet, SQLite WAL, DuckDB); TimescaleDB only for Phase 6 ticks; Redpanda removed (§9.9).
- Judged on prediction quality; the complexity gate is binding (§9.10).
- LLM provider chosen at the Phase 5 gate; extraction starts rule-based (§9.11).
- English docs; Vietnamese-first alerts (§9.12).
- `.agents/` kept, with its rules rewritten as pointers to AGENTS.md (§9.13).
- Point-in-time record contract (tz-aware UTC `information_time`, `first_seen_at`, `ingested_at`, plus `source`, `source_id`, `pit_quality`; features read only through `store.as_of()`) and the event clock (day-0 rule; feature cutoff = `information_time`): binding (AGENTS.md "Engineering Rules"; roadmap §2.1, §5).

## Constraints & non-goals
- **No order execution**, live or paper, from code or agents (DECIDED).
- **Not investment advice** (DECIDED): a notice on every output, probabilities only, no buy/sell (mua/bán) wording.
- **No redistribution of licensed data.** vnstock's licence forbids it (VERIFIED, AGENTS.md); Alpaca and SSI terms are ASSUMED to forbid it too (roadmap §4). Only derived signals leave the store, and only to the owner (DECIDED §9.1).
- **Point-in-time (DECIDED: no look-ahead).** A plan that adds a feature, label or retrieval step must name its decision time and show that every input was available at or before it. Do not:
  - fall back to `now()` for a data timestamp;
  - use XBRL Frames for history, or overwrite a restated value;
  - feed the RAG ranking into features before `published_at` is the point-in-time `information_time` / `first_seen_at` (roadmap Phase 1); since 2026-10-21 its `as_of` filter reads `published_at` only.
- **VN market constraints.**
  - Shares bought on T can be sold from the T+2 afternoon session (VERIFIED, AGENTS.md). DECIDED (AGENTS.md "Engineering Rules"; roadmap §6.3): a 1-session VN stock target never counts as a tradable call.
  - Validate prices against the band (DECIDED). Treating band-locked sessions as unfillable, and adjusting for corporate actions ourselves from raw bars, are binding too (AGENTS.md "Engineering Rules"; roadmap §6.3).
  - Exchange membership is not static: HNX-listed stocks move to HOSE (last HNX day 2026-12-23, HOSE from 2026-12-28; VERIFIED secondary press), and the band they will use is ASSUMED. Today `exchange` arrives as a per-record string and `VNExchangeBand` (`models.py:14-17`) only maps exchange to band. Time-versioned exchange membership is planned (roadmap §5.2).
- **Complexity gate** (RAG, the bandit, SLM fine-tuning or a new database enter the pipeline only with a measured scoreboard or latency gain): DECIDED (§9.10, 2026-10-21).
- **Out of scope before Phase 6** (DECIDED, §9.3): live tick streaming (Alpaca IEX, SSI FastConnect). Deferring the intraday `quant/kalman_microstructure.py` fixes to Phase 6, and SLM fine-tuning, bandit routing and semantic RAG to Phase 7, is planned (roadmap §5.2, §7).
- Build and verification commands (bash and PowerShell) are in AGENTS.md "Build & Verification Commands"; do not duplicate them here.
