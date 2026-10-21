# System Architecture

> Read before every `/hs:plan` and `/hs:cook`. Status, verified facts and the decision log live in [AGENTS.md](../AGENTS.md). The proposed direction and the open owner decisions live in [docs/project-roadmap.md](project-roadmap.md) §9. Checked against the code on 2026-10-20.
> **AS-BUILT** = in the code today. **DECIDED** = AGENTS.md "Decisions" or "Engineering Rules (binding)". **PROPOSED** = roadmap direction; not binding until the cited §9 item is answered, or, where no §9 item applies, until the roadmap itself is accepted (it is PROPOSED as a whole; see the roadmap header).

## Overview
- FinSec AlphaRadar is a research and alerting platform for US (NYSE/NASDAQ) and Vietnam (HOSE/HNX/UPCOM) equities. It aims to turn disclosures, financial statements and market data into probabilities that are scored against what actually happened (target; nothing does this yet).
- It never places orders and is not investment advice (DECIDED, AGENTS.md "Engineering Rules").
- Maturity: **prototype** (AGENTS.md "Current Status", verified 2026-10-20).
  - About 850 lines of in-memory Python under `src/finsec_alpharadar/` (`requires-python >=3.10`, `pyproject.toml:10`). The only third-party imports are numpy and fastapi (`api/server.py:4`).
  - No HTTP/WebSocket client, database driver, environment-variable read or LLM SDK anywhere in `src/`.
  - No prediction has been made or scored yet.
  - The only test file is `tests/test_end_to_end.py`. Its entry point `run_modular_verification()` (line 36) is not a `test_*` function, so `PYTHONPATH=src python3 -m pytest` collects 0 tests and exits 5. Without `PYTHONPATH`, collection errors (exit 2): `pyproject.toml` has no `[tool.pytest.ini_options] pythonpath`. `make test` (`Makefile:6-7`) calls bare `pytest`, which is not on PATH on the dev machine (OBSERVED 2026-10-20). The only passing check is the AGENTS.md smoke command (exit 0).
- Shape today: independent engines plus a 3-endpoint FastAPI app. Target shape (PROPOSED): a disclosure poller feeding an append-only prediction ledger that is scored automatically (see Data flow).

## Components

### As-built (`src/finsec_alpharadar/`)
| Path | Job | Status (2026-10-20) |
|---|---|---|
| `domain/models.py` | `MarketRegion`, `VNExchangeBand` enums; `MarketTick`, `RegulatoryFiling`, `FinancialStatementSnapshot` dataclasses | Real. Times are float epoch seconds (`timestamp` line 23, `published_at` line 54), not tz-aware datetimes. Static exchange-to-band map (lines 13-16); an unknown exchange silently gets 7% (line 44); prices are never validated against the band. |
| `connectors/us_alpaca_edgar.py` | `AlpacaStreamAdapter`, `SecEdgarFilingAdapter`: dict to `MarketTick` / `RegulatoryFiling` | Stub. Static parsers, no client, called only by the smoke test. Missing timestamp falls back to `now()` (line 20); bid/ask sizes default to 100.0 (lines 29-30). |
| `connectors/vn_ssi_vnstock.py` | `SSIFastConnectAdapter`, `VnstockBCTCAdapter` | Stub, as above. `now()` fallback (line 22), 100.0 depth defaults (lines 27-28), exchange defaults to HOSE (line 30), wrong `WS_ENDPOINT` (line 15). |
| `quant/event_study.py` | `EventStudyAnalyzer.compute_car`: OLS market-model AR/CAR and t-stat | Real, test-only (no caller in `src/`). Bare arrays: no timestamp alignment or window checks. |
| `quant/kalman_microstructure.py` | `KalmanPriceFilter`, `StreamingQuantEngine`: VWAP, OBI, CUSUM, volume z-score, VN foreign net flow, alert reasons | Real; wired to `POST /v1/ticks/ingest`. Session-blind. Foreign flow is summed over the 50-tick deque (line 99). Deferring that fix until the SSI field semantics are read (Phase 6) is Proposed (roadmap §5.2); `tests/test_end_to_end.py:94-95` feeds increments. |
| `quant/deduplicator.py` | `MinHashDeduplicator`: near-duplicate text check | Real, test-only. Linear scan (lines 38-42) despite the "O(1)" docstring (line 10). |
| `forensics/xbrl_bctc_auditor.py` | `ForensicAccountingEngine.evaluate`: Altman Z, 8-variable Beneish M, 9-point Piotroski F | Real, test-only. No zero-division guards (e.g. line 28) and no sector gate. |
| `rag/hybrid_temporal_rag.py` | `HybridTemporalRAGEngine`: BM25 + RRF + exponential time decay; sentence-level diff | BM25, RRF and decay are real. The "dense" and ColBERT MaxSim legs use per-token hash vectors (lines 47-52) with no semantics. Look-ahead bug: lines 112 and 138, and no `published_at <= as_of` filter (lines 118-122). |
| `router/linucb_bandit.py` | `LinUCBModelRouter`: disjoint LinUCB over model-name arms | Math is real, but no model is ever called. Only the smoke test calls `update_reward` (hard-coded 0.98, test:151); the app never does, so `GET /v1/rag/search` always picks the first arm (line 40). |
| `api/server.py` | FastAPI: `GET /health`, `POST /v1/ticks/ingest`, `GET /v1/rag/search` | Thin. No WebSocket, no auth, untyped `Dict[str, Any]` body (line 29), hard-coded unverified model IDs (lines 18-20). fastapi is not installed on the dev machine yet (AGENTS.md). |

Outside `src/`:
- `scripts/train_unsloth_slm.py` only prints a hard-coded training recipe; it does not read `configs/unsloth_qwen3_catalyst.yaml`. Stub.
- `docker-compose.yml`: Qdrant, TimescaleDB and Redpanda; no code uses them.
- `harness/`: vendored SDLC harness v7.2.0 (`/hs:plan`, `/hs:cook`, `/hs:test`, `/hs:ship`). Not product code.
- `.agents/`: another agent tool's agent definitions, skills, rules, hooks and MCP config. Its future is Proposed (roadmap §9.13).

### Target modules (PROPOSED, roadmap §5.2; none exist yet)
Proposed layering (roadmap §5.1): connectors (I/O only) -> store -> features/labels (pure functions taking `as_of`) -> modeling -> ledger -> api/notify (read-only).

| Path | Job | Phase |
|---|---|---|
| `capture/` | Capture-only VN disclosure poller: raw payload, `raw_sha256`, `first_seen_at`, outage log, heartbeat | 0 |
| `connectors/base.py` | SourceAdapter protocol, RawRecord, token-bucket limiter, retry with backoff, idempotent key (source, source_id) | 1 |
| `store/` | Raw archive writer, operational schema, research views, `as_of()` (engine: Proposed, roadmap §9.9) | 1 |
| `refdata/` | Security master, exchange membership (incl. the 2026-12-28 HNX-to-HOSE transfer), sectors, corporate actions | 1, 3 |
| `calendar/` | XNYS and VN sessions, holidays, regime flags | 1, 3 |
| `labels/` | Reaction and drift labels wrapping `quant/event_study.py`; outsized, censor, confound and stale flags | 1 |
| `features/` | As-of features: event type, pre-event run-up, size, SUE, forensic components, VN foreign flow / ADV | 2-3 |
| `modeling/` | Baselines, L2 logistic, isotonic calibration | 2 |
| `backtest/` | Walk-forward, metrics, US/VN costs, report | 2 |
| `ledger/` | Append-only predictions and outcomes with a hash chain; nightly scorer | 2 |
| `pipeline/__main__.py` | CLI: `backfill`, `poll`, `predict`, `score`, `digest`, `scoreboard` | 1-4 |
| `notify/telegram.py` | Private 1:1 alerts (channel: Proposed, roadmap §9.8) | 4 |

Also proposed: move `router/` to `experimental/` (Phase 0); rewrite `api/server.py` as read-only GET endpoints bound to localhost (Phase 4).

## Data flow

### As-built
```
POST /v1/ticks/ingest   raw JSON dict (server.py:28-44)
  -> MarketTick built inline, NOT via connectors/ (defaults: market=US, exchange=IEX, sizes=100.0)
     a VN tick posted without "exchange" gets exchange="IEX" (server.py:41), so the band silently falls back to 7% (models.py:44)
  -> StreamingQuantEngine.process_tick (server.py:45)
  -> {"signal": QuantSignalReport.__dict__, "vn_ceiling_floor": [ceiling, floor] or null} (server.py:46-49)

GET /v1/rag/search      (server.py:52-66)
  -> LinUCBModelRouter.select_model: the chosen arm is returned, never called
  -> HybridTemporalRAGEngine.search: always [] because nothing ingests into the app's engine

tests/test_end_to_end.py only:
  connectors parse 2 ticks (asserted only, test:40-59) and 2 filings -> HybridTemporalRAGEngine (test:124-145)
  hand-built MarketTicks -> StreamingQuantEngine (test:71-96)
  hand-built FinancialStatementSnapshot -> ForensicAccountingEngine (test:106)
  raw arrays -> EventStudyAnalyzer (test:101-103)
  raw strings -> MinHashDeduplicator (test:63-64) and LinUCBModelRouter (test:149-152)
```
Where state lives:
- Process memory only: module-level singletons in `api/server.py:16-20`, and per-instance dicts/lists in `kalman_microstructure.py:61-63`, `hybrid_temporal_rag.py:37-41`, `deduplicator.py:20`, `linucb_bandit.py:15-16`.
- Nothing is persisted or replayable. A restart, or each `--reload` from `Makefile:10`, drops all state; multiple uvicorn workers would each hold separate state.

### Target core loop (PROPOSED, roadmap §3)
```
capture -> parse -> as-of features -> predict -> ledger -> alert -> score -> scoreboard
```
- **capture:** EDGAR and VN disclosure polls every 2-5 min; raw archive with `raw_sha256` and `first_seen_at`.
- **parse:** typed Event with `information_time`, day 0, event type, `pit_quality`, confound/stale flags, citation URL.
- **features:** as-of only; inputs must have `available_at` < decision time (`information_time` for R1, entry time for drift targets; roadmap §2.1, §6.4).
- **predict:** baselines and L2 logistic, calibrated on train folds only.
- **ledger:** append-only row (mode backtest/shadow/live, model_version, feature_hash, hash chain), written before the outcome exists.
- **alert:** private card with probability vs base rate, citation and disclaimer (channel: Proposed, roadmap §9.8).
- **score / scoreboard:** a nightly job fills matured outcomes; `reports/scoreboard.md` and `GET /v1/scoreboard`.
- DECIDED (§9.3, 2026-10-20): in Phases 0-4 "real-time" means disclosure detection latency (p50 under 5 min US, under 10 min VN). Tick streaming is Phase 6, after the owner checkpoint that follows Phase 4. Do not plan live tick streaming (Alpaca IEX / SSI FastConnect clients, tick storage) before then. Fixes that binding rules require in existing tick-path code (band validation, no `now()` fallback) are not streaming work; deferring other `quant/kalman_microstructure.py` fixes is Proposed (roadmap §5.2), not covered by §9.3.
- Market order (US first, VN capture from week 1) is Proposed (roadmap §9.2). Storage engine is Proposed (roadmap §9.9).

## External dependencies
None of these is called from `src/` today. Labels and URLs are copied from AGENTS.md "Verified Facts"; add any new fact there first, with a label and a date.
- **SEC EDGAR** (US filings: 8-K, 10-Q, 10-K, Form 4).
  - Fair access: at most 10 requests/s with a declared User-Agent. VERIFIED https://www.sec.gov/os/accessing-edgar-data
  - Information time: the filing header `<ACCEPTANCE-DATETIME>`, read as America/New_York. The submissions JSON `acceptanceDateTime` is for discovery only; its "Z" did not match UTC. OBSERVED 2026-10-20; the Phase 0 golden test settles the header side.
  - User-Agent is meant to come from `EDGAR_IDENTITY` (Proposed, roadmap §5.2). `.env.example:5` holds a placeholder, `.agents/mcp_config.json:23` hard-codes it, and nothing in `src/` reads it.
- **Alpaca Market Data, Basic plan** (paper keys).
  - IEX-only real-time data, 30 WebSocket symbols, 200 historical calls/min. VERIFIED https://docs.alpaca.markets/docs/about-market-data-api
  - Bars `adjustment` defaults to raw; request `all` for return labels. VERIFIED https://docs.alpaca.markets/reference/stockbars
  - SIP history on Basic: ASSUMED.
- **SSI FastConnect Data** (VN; live use is Phase 6 per §9.3. Whether to open an account is open: roadmap §9.5, recommendation: defer).
  - Needs an SSI account plus ConsumerID and ConsumerSecret (no private key). REST `https://fc-data.ssi.com.vn/v2.0/Market`; streaming `https://fc-datahub.ssi.com.vn/v2.0`. VERIFIED https://guide.ssi.com.vn/ssi-products/tieng-viet/fastconnect-data/huong-dan-ket-noi
  - The code constant `wss://stream.ssi.com.vn` (`vn_ssi_vnstock.py:15`) is wrong; never reuse it. `SSI_PRIVATE_KEY_PATH` (`.env.example:11`) is not needed for FC Data.
- **vnstock** (VN prices, statements, events over unofficial broker endpoints).
  - Community licence: personal, study or research use only, no redistribution. VERIFIED https://github.com/thinh-vu/vnstock
  - Rate limits, history depth and price units: ASSUMED until the Phase 0 probe.
  - Declared in the `prod` extra (`pyproject.toml:25`), not imported in `src/`.
- **Telegram Bot API**: proposed alert channel (Proposed, roadmap §9.8; Phase 4). It is not in AGENTS.md "Verified Facts"; see roadmap §4 for its source.
- **LLM providers**: none called. The model IDs in `api/server.py:19` and `tests/test_end_to_end.py:149` are unverified. Provider choice is Proposed (roadmap §9.11).
- **docker-compose services** (`docker-compose.yml`): Qdrant (6333/6334), TimescaleDB (5432), Redpanda (19092). Unused, floating `latest` / `latest-pg16` tags, hard-coded password (`docker-compose.yml:17`, repeated in `.env.example:15`), ports published on all interfaces. Their future is Proposed (roadmap §9.9).

## Boundaries & trust
- **Exposed surface:** the FastAPI app `finsec_alpharadar.api.server:app` on port 8000.
  - `make run-api` binds `0.0.0.0` (`Makefile:10`), so the API is reachable from the local network. Binding to 127.0.0.1 is a proposed Phase 0 fix (roadmap §5.2); until then, run it only on a trusted network.
  - No authentication, rate limit or request model: the body is an untyped dict (`server.py:29`), and a missing key raises a 500.
  - Any caller of `POST /v1/ticks/ingest` mutates shared in-memory engine state.
- **Secrets:** keys live in `.env` (gitignored, `.gitignore:20`; `*.pem` and `*.key` ignored at lines 26-27). `src/` reads no environment variables today. Guidance (roadmap §4, §8; not an AGENTS.md rule): never commit keys, and never put secrets or personal data in prompts, URLs or logs.
- **Order-execution boundary (DECIDED):** no trading client in `src/`; keep `ALPACA_PAPER_TRADE=true` (`.env.example:4`); no `trading` MCP toolset; never register SSI FC Trading. A test enforcing this (`tests/test_no_order_execution.py`) is proposed for Phase 0.
- **Outputs:** every output carries a not-investment-advice notice and states probabilities, never buy/sell (mua/bán) wording (DECIDED). Owner-only visibility is Proposed (roadmap §9.1).
- **Untrusted inputs** (guidance, roadmap §5.1; not an AGENTS.md rule): validate at the connector boundary and treat as data, never as instructions:
  - vnstock responses (unofficial endpoints that can change or break without notice);
  - scraped HTML/PDF from VN disclosure portals (proposed Phase 0 and 3 sources);
  - API request bodies, and any LLM output (Phase 5, behind a proposed number-faithfulness validator).
- **Agent configuration:** AGENTS.md is the single shared contract for every coding agent (AGENTS.md intro). `.agents/rules/quant-and-market-rules.md:11` (rule 3: BM25 + dense + MaxSim in every `*.py`) is not an AGENTS.md rule. Rewriting the `.agents/` rules as pointers to AGENTS.md is Proposed (roadmap §9.13).
- **Known gaps against binding rules** (do not copy; fix within scope when touched):
  - RAG gives future-dated documents full weight (`hybrid_temporal_rag.py:112,138`).
  - VN band never validated; unknown exchange silently gets 7% (`domain/models.py:44`).
  - `.agents/mcp_config.json:16` still lists `trading` in `ALPACA_TOOLSETS`.
  - Forensics has no gate excluding banks, insurers and securities firms (`forensics/xbrl_bctc_auditor.py`).
  - Connectors stamp missing timestamps with `now()` (`us_alpaca_edgar.py:20`, `vn_ssi_vnstock.py:22`), which fakes availability time.
  - API responses carry no not-investment-advice notice (`api/server.py:46-49`, `62-66`).
  - Unverified model IDs (`api/server.py:19`) and the wrong SSI endpoint (`vn_ssi_vnstock.py:15`) break the external-facts rule.

## Key decisions

### Decided
- **No order execution anywhere** (binding), including never registering SSI FC Trading. Why: the project is research and alerting only; accidental execution is a listed risk (roadmap §8), and the paper flag is only defense in depth.
- **Probabilities only, with a not-investment-advice notice; no buy/sell wording** (binding). Why: publishing predictions could count as unlicensed investment advice (roadmap §8).
- **No look-ahead** (binding): windows, Kalman updates, CAR and features use only data available at or before the decision time; keep every restated fundamental; never backtest on SEC XBRL Frames. Why: the product is a scoreboard of predictions against outcomes, and one leak invalidates it. Frames return the latest filed value, so restatements leak (roadmap §4).
- **VN band validation** (binding): HOSE ±7% / HNX ±10% / UPCOM ±15% around `reference_price`; raise on an unknown exchange. Why: a silent default gives wrong ceiling/floor values, as `domain/models.py:44` does today.
- **No Altman/Beneish/Piotroski for banks, insurers or securities firms** (binding). Why: AGENTS.md "Current Status" records the auditor as not valid for these firms. Keeping them in event studies is Proposed (roadmap §9.7).
- **Label external facts VERIFIED, OBSERVED or ASSUMED; never invent API names, fields, model IDs or prices** (binding). Why: the prototype already carries a wrong SSI endpoint and unverified model IDs.
- **No package named `data/` or `models/`; use `modeling/`** (binding until `.gitignore` is anchored). Why: the unanchored patterns at `.gitignore:32-33` would silently keep such packages out of git (OBSERVED 2026-10-20).
- **"Real-time" = disclosure capture first; ticks in Phase 6** (owner, 2026-10-20, §9.3). Why: free tick feeds are thin (Alpaca Basic is IEX-only with 30 WebSocket symbols), and VN shares cannot be sold before the T+2 afternoon, so intraday single-stock forecasts cannot be acted on.
- **AGENTS.md holds all project knowledge; `CLAUDE.md` only imports `@AGENTS.md`, plus the generated harness block** (owner, 2026-10-20; AGENTS.md "Decisions"). Why: one contract for every coding agent. Record module status, decisions and verified facts in AGENTS.md in the same change as the code (AGENTS.md "Agent Memory Protocol").

### Proposed (not binding)
- Private, owner-only outputs; demos on replayed data only. Proposed (roadmap §9.1).
- US pipeline first, VN capture-only poller from week 1. Proposed (roadmap §9.2).
- Always-on, NTP-synced capture host. Proposed (roadmap §9.4).
- SSI account deferred; FC Data credentials only. Proposed (roadmap §9.5). Never registering FC Trading is already binding (see Decided).
- $0 budget plus the host through Phase 4. Proposed (roadmap §9.6).
- Broad point-in-time, ADV-ranked universe plus a live watchlist of 30 or fewer per market. Proposed (roadmap §9.7).
- Private Telegram 1:1 bot. Proposed (roadmap §9.8).
- Embedded storage (raw JSONL/Parquet, SQLite WAL, DuckDB); TimescaleDB only for Phase 6 ticks; Redpanda removed. Proposed (roadmap §9.9).
- Complexity gate. Proposed (roadmap §9.10).
- LLM provider chosen at the Phase 5 gate. Proposed (roadmap §9.11).
- English docs, Vietnamese-first alerts. Proposed (roadmap §9.12).
- `.agents/` rules rewritten as pointers to AGENTS.md. Proposed (roadmap §9.13).
- Point-in-time record contract (tz-aware UTC `information_time`, `first_seen_at`, `ingested_at`, plus `source`, `source_id`, `pit_quality`; features read only through `store.as_of()`) and the event clock (day-0 rule; feature cutoff = `information_time`). Proposed (AGENTS.md "Proposed Rules"; roadmap §2.1, §5).

## Constraints & non-goals
- **No order execution**, live or paper, from code or agents (DECIDED).
- **Not investment advice** (DECIDED): a notice on every output, probabilities only, no buy/sell (mua/bán) wording.
- **No redistribution of licensed data.** vnstock's licence forbids it (VERIFIED, AGENTS.md); Alpaca and SSI terms are ASSUMED to forbid it too (roadmap §4). Letting only derived signals leave the store, owner-only, is Proposed (roadmap §9.1).
- **Point-in-time (DECIDED: no look-ahead).** A plan that adds a feature, label or retrieval step must name its decision time and show that every input was available at or before it. Do not:
  - fall back to `now()` for a data timestamp;
  - use XBRL Frames for history, or overwrite a restated value;
  - reuse the RAG ranking until its `as_of` filter is fixed.
- **VN market constraints.**
  - Shares bought on T can be sold from the T+2 afternoon session (VERIFIED, AGENTS.md). Proposed (AGENTS.md "Proposed Rules"; roadmap §6.3): a 1-session VN stock target never counts as a tradable call.
  - Validate prices against the band (DECIDED). Treating band-locked sessions as unfillable, and adjusting for corporate actions ourselves from raw bars, are Proposed (roadmap §6.3).
  - Exchange membership is not static: HNX-listed stocks move to HOSE (last HNX day 2026-12-23, HOSE from 2026-12-28; VERIFIED secondary press), and the band they will use is ASSUMED. Today `exchange` arrives as a per-record string and `VNExchangeBand` (`models.py:13-16`) only maps exchange to band. Time-versioned exchange membership is Proposed (roadmap §5.2).
- **Complexity gate** (RAG, the bandit, SLM fine-tuning or a new database enter the pipeline only with a measured scoreboard or latency gain): Proposed (roadmap §9.10).
  > TBD — pending docs/project-roadmap.md §9.10
- **Out of scope before Phase 6** (DECIDED, §9.3): live tick streaming (Alpaca IEX, SSI FastConnect). Deferring the intraday `quant/kalman_microstructure.py` fixes to Phase 6, and SLM fine-tuning, bandit routing and semantic RAG to Phase 7, is Proposed (roadmap §5.2, §7).
- Build and verification commands (bash and PowerShell) are in AGENTS.md "Build & Verification Commands"; do not duplicate them here.
