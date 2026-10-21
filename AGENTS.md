# FinSec AlphaRadar — Dual-Market (US & Vietnam) Agentic Engineering Contract

This file is the single shared contract for every coding agent in this repo (Claude Code, Codex, Antigravity, ...). `CLAUDE.md` only imports it.

## Agent Memory Protocol
- Write durable knowledge HERE, in the same change as the code it describes: decisions, module status, verified facts, phase progress, commands. Never put project knowledge in `CLAUDE.md` or another tool-specific file.
- Keep this file short, because it loads into every agent's context. Long-form material goes in `docs/` and is linked from here.
- Date every status or fact line you add or change. Label external facts VERIFIED (URL), OBSERVED (date) or ASSUMED.

## Decisions (owner; newest first)
- 2026-10-21 — All remaining roadmap §9 decisions answered; the roadmap is ADOPTED as the working plan:
  - §9.1: personal research; outputs are owner-only (demos use replayed data and derived signals).
  - §9.2: US pipeline first; VN capture-only poller from week 1; VN is the main research market from Phase 3.
  - §9.4: VN capture runs on a spare always-on laptop configured as a server (no sleep, lid = do nothing, mains power, NTP, daily heartbeat, nightly off-machine backup).
  - §9.5: no SSI account or SSI client before Phase 6; FC Data only, never FC Trading.
  - §9.6: $0 plus the capture laptop through Phase 4.
  - §9.7: broad point-in-time universe (~500 US, 150-250 VN, ranked by ADV) plus a watchlist of ≤30 per market; financials stay in event studies.
  - §9.8: private Telegram 1:1 bot to one allowlisted chat id.
  - §9.9: embedded storage (raw JSONL/Parquet, SQLite WAL, DuckDB); TimescaleDB only for Phase 6 ticks; Redpanda removed.
  - §9.10: judged on prediction quality; the complexity gate is binding.
  - §9.11: LLM provider chosen at the Phase 5 gate (rule-based extraction first).
  - §9.12: AGENTS.md and docs in English; alerts and digests Vietnamese-first.
  - §9.13: keep `.agents/`, with its rules rewritten as pointers to AGENTS.md (Phase 0).
- 2026-10-20 — §9.3: "real-time" means fast disclosure capture first (detection latency); tick streaming is Phase 6.
- 2026-10-20 — `CLAUDE.md` only imports `@AGENTS.md` (plus the generated harness block); project knowledge lives in this file.
- Full wording and options: docs/project-roadmap.md §9. To change a decision, add a new dated line; never edit an old one.

## System Overview
FinSec AlphaRadar is a research and alerting platform. It aims to turn real-time market data, financial statements and corporate decisions for US (NYSE/NASDAQ) and Vietnam (HOSE/HNX/UPCOM) equities into predictions that are scored against what actually happened. It never places orders and is not investment advice.
- **US sources:** Alpaca market data (paper keys); SEC EDGAR filings (8-K, 10-Q, 10-K, Form 4).
- **VN sources:** SSI FastConnect Data; `vnstock`; consolidated financial statements (BCTC hợp nhất); board and shareholder resolutions (Nghị quyết HĐQT/ĐHĐCĐ).

## Current Status (verified 2026-10-20)
- **Prototype.** About 850 lines of numpy-only, in-memory code. There is no live connector, no database and no LLM call. No prediction has been made or scored yet.
- **Works, smoke-tested by `tests/test_end_to_end.py`:**
  - OLS market-model CAR (`quant/event_study.py`);
  - Altman/Beneish/Piotroski (`forensics/xbrl_bctc_auditor.py`); it has no zero-division guards and is not valid for banks, insurers or brokers;
  - MinHash dedup (`quant/deduplicator.py`); it is a linear scan, not O(1);
  - streaming VWAP/OBI/CUSUM/volume z-score (`quant/kalman_microstructure.py`).
- **Stub or placeholder:**
  - connectors are static dict parsers;
  - RAG "dense" and ColBERT vectors are per-token hash vectors with no semantics;
  - the LinUCB router picks a model that is never called;
  - `scripts/train_unsloth_slm.py` only prints a recipe;
  - the docker-compose services (Qdrant, TimescaleDB, Redpanda) are unused.
- **Known bugs:**
  - RAG gives future-dated documents full weight (`rag/hybrid_temporal_rag.py:112,138`).
  - Foreign flow is summed over a 50-tick deque (`quant/kalman_microstructure.py:99`).
  - The VN price band is computed but never validated, and an unknown exchange silently gets 7% (`domain/models.py:44`).
  - On a cold start the router always picks the first arm (`router/linucb_bandit.py:40`).
  - The SSI endpoint constant `wss://stream.ssi.com.vn` is wrong (see Verified Facts).
  - pytest collects 0 tests.

## Package Layout (`src/finsec_alpharadar/`)
- `domain/models.py`: data schemas (`MarketRegion`, `MarketTick` with spread and VN ceiling/floor, `RegulatoryFiling`, `FinancialStatementSnapshot`).
- `connectors/`: payload parsers for Alpaca and SEC EDGAR (`us_alpaca_edgar.py`) and for SSI FastConnect and vnstock (`vn_ssi_vnstock.py`). No network clients yet.
- `quant/`: `MinHashDeduplicator`, `KalmanPriceFilter` + `StreamingQuantEngine` (VWAP, OBI, CUSUM, volume z-score, VN foreign net flow), `EventStudyAnalyzer` (CAR).
- `forensics/xbrl_bctc_auditor.py`: Altman Z-Score, 8-variable Beneish M-Score, 9-point Piotroski F-Score.
- `rag/hybrid_temporal_rag.py`: BM25 + Reciprocal Rank Fusion + exponential time decay + a sentence-level disclosure diff. The dense and MaxSim legs use placeholder hash vectors.
- `router/linucb_bandit.py`: disjoint LinUCB contextual bandit that chooses a model name.
- `api/server.py`: FastAPI REST only: `GET /health`, `POST /v1/ticks/ingest`, `GET /v1/rag/search`. The search always returns [] because nothing is ingested. There is no WebSocket endpoint.
- `harness/` (repo root): vendored SDLC harness v7.2.0, driven by `/hs:plan`, `/hs:cook`, `/hs:test` and `/hs:ship`. It reads `docs/system-architecture.md` and `docs/code-standards.md` (written 2026-10-20) before every plan and code change; keep both in sync with this file.

## Product Direction (ADOPTED 2026-10-21; detail in docs/project-roadmap.md)
- **Vision.** A private research and alerting tool. Each US/VN disclosure becomes a calibrated probability of abnormal return. The probability is logged in an append-only ledger before the outcome exists and scored automatically when its horizon matures.
- **"Real-time" (DECIDED 2026-10-20, §9.3).** In Phases 0-4 this means disclosure detection latency: median under 5 min (US) and under 10 min (VN). Tick streaming is Phase 6, after the owner checkpoint that follows Phase 4.
- **Pre-registered targets** (full definitions in roadmap §2):
  - R1 (alerting, both markets): an outsized reaction, |AR0| > 2σ or day-0 volume ≥ 2× the 20-session median.
  - D1 (US): P(CAR[+1,+5] > 0) after an 8-K, Item 2.02 first. Entry at the day-0 close; benchmark SPY.
  - D2 (VN): P(CAR[+2,+10] > 0) after a BCTC or NQ HĐQT. Entry at the day+1 close, long-only. Benchmark: our liquid universe excluding the event stock.
- **Core loop.** Capture the disclosure → typed event with `information_time` → as-of features → baseline or logistic probability → ledger + private alert with a citation → automatic scoring at the horizon → scoreboard.
- **Success bar.**
  - IC 0.02-0.05, or a 52-55% hit rate, is good.
  - A hit rate above 60% or an IC above 0.10 triggers a leakage audit.
  - A documented null result with stated power counts as a pass.

## Roadmap (ADOPTED 2026-10-21; detail in docs/project-roadmap.md §7)
- **Phase 0 (next, ~2 weeks):**
  - a verification spike (EDGAR time, Alpaca, vnstock, SSI);
  - `docs/system-architecture.md` and `docs/code-standards.md` (done 2026-10-20);
  - test and safety hygiene;
  - a capture-only VN disclosure poller.
- **Phase 1 (~3 weeks):** US point-in-time event set (EDGAR + Alpaca daily bars) and CAR labels.
- **Phase 2 (~3 weeks):** US baselines, walk-forward scoreboard, shadow prediction ledger.
- **Phase 3 (3-4 weeks):** VN end-of-day dataset, corporate actions, trading calendar, exchange membership, labels.
- **Phase 4 (3-4 weeks):** VN scoreboard, daily live loop for both markets, private Telegram alerts, read-only API.
- **Phase 5 (gated):** text features and LLM extraction behind a number-faithfulness validator.
- **Phase 6 (gated):** intraday ticks (Alpaca IEX, SSI FastConnect).
- **Phase 7 (optional):** SLM distillation, bandit routing, semantic RAG.

## Verified Facts (add a source and a date to every new fact)
- **EDGAR time (OBSERVED 2026-10-20).** The data.sec.gov submissions JSON `acceptanceDateTime` ends in "Z" but did not match UTC on four accessions in the research pass. The JSON side was re-checked on 2026-10-20; the Phase 0 golden test settles the header side.
  - Rule: the filing header `<ACCEPTANCE-DATETIME>`, read as America/New_York, is the information time. The JSON value is for discovery only.
  - Fair access: at most 10 requests/s with a declared User-Agent. VERIFIED https://www.sec.gov/os/accessing-edgar-data
- **Alpaca Basic.**
  - IEX-only real-time data, 30 WebSocket symbols, 200 historical calls/min. VERIFIED https://docs.alpaca.markets/docs/about-market-data-api
  - The bars `adjustment` parameter defaults to raw; request `all` for return labels. VERIFIED https://docs.alpaca.markets/reference/stockbars
  - SIP history on Basic: ASSUMED.
- **SSI FastConnect Data.**
  - Needs an SSI account plus ConsumerID and ConsumerSecret (no private key).
  - REST `https://fc-data.ssi.com.vn/v2.0/Market`; streaming `https://fc-datahub.ssi.com.vn/v2.0`.
  - VERIFIED https://guide.ssi.com.vn/ssi-products/tieng-viet/fastconnect-data/huong-dan-ket-noi
- **VN settlement.** Shares bought on T can be sold from the T+2 afternoon session. VERIFIED https://hdsd.dnse.com.vn/cau-hoi-thuong-gap/giao-dich/giao-dich-chung-khoan
- **VN market structure.**
  - HNX-listed stocks move to HOSE: last HNX day 2026-12-23, HOSE from 2026-12-28. VERIFIED (secondary press) https://vietnamnews.vn/economy/1784883/nearly-300-stocks-to-move-from-hnx-to-hose-under-market-restructuring-plan.html
  - The band for those stocks is ASSUMED.
  - FTSE secondary-emerging status is effective 2026-09-21. VERIFIED https://www.lseg.com/en/media-centre/press-releases/ftse-russell/2025/ftse-russell-country-classification-september-2025
- **vnstock licence.** The Community licence allows personal, study or research use only, with no redistribution. VERIFIED https://github.com/thinh-vu/vnstock. Rate limits, history depth and price units: ASSUMED until the Phase 0 probe.
- **`.gitignore` (OBSERVED 2026-10-20).** The unanchored `data/` and `models/` patterns ignore any directory with those names. `harness/**/data/` is re-included explicitly.

## Engineering Rules (binding)
- **No order execution anywhere.**
  - No trading clients in `src/`.
  - Keep `ALPACA_PAPER_TRADE=true`.
  - No `trading` MCP toolset.
  - Never register SSI FC Trading.
- **Outputs.** Every output carries a not-investment-advice notice and states probabilities, never buy/sell (mua/bán) wording.
- **No look-ahead.**
  - Sliding windows, Kalman updates, CAR and features may use only data available at or before the decision time.
  - Keep every restated version of a fundamental.
  - Never backtest on SEC XBRL Frames.
- **VN.** Validate prices against the HOSE ±7% / HNX ±10% / UPCOM ±15% bands around `reference_price`, and raise on an unknown exchange.
- **Forensics.** Altman/Beneish/Piotroski scores never apply to banks, insurers or securities firms.
- **External facts.** Label them VERIFIED, OBSERVED or ASSUMED. Never invent API names, fields, model IDs or prices.
- **Package names.** Do not name a package `data/` or `models/` until the `.gitignore` patterns are anchored; use `modeling/`.
- **Private outputs only (§9.1, §9.8).** Outputs go only to the owner, through the private Telegram 1:1 bot or local reports; never to groups, channels or the public.
- **Complexity gate (§9.10).** RAG, the bandit, SLM fine-tuning or a new database enter the pipeline only with a measured gain on the scoreboard or in latency. Tick streaming is scheduled for Phase 6 (§9.3).
- **Storage (§9.9).** Raw data is append-only JSONL/Parquet; operations use SQLite (WAL); research uses DuckDB. No other database before Phase 6.
- **Point-in-time contract (roadmap §5).** Every record carries tz-aware UTC `information_time`, `first_seen_at`, `ingested_at`, `source`, `source_id` and `pit_quality`. Features read only through `store.as_of()`.
- **VN labels (roadmap §6.3).**
  - Adjust for corporate actions ourselves from raw bars.
  - Treat band-locked sessions as unfillable.
  - No 1-session VN stock target counts as a tradable call.
- **Budget and vendors (§9.5, §9.6).** No paid data or service and no SSI client before the decision allows it ($0 through Phase 4; SSI from Phase 6).
- **Language (§9.12).** Code, AGENTS.md and docs in English; user-facing alerts Vietnamese-first; Vietnamese text keeps its diacritics (NFC).

## Build & Verification Commands
- **End-to-end smoke check.**
  - bash: `PYTHONPATH=src python3 tests/test_end_to_end.py`
  - PowerShell: `$env:PYTHONPATH='src'; python tests/test_end_to_end.py`
  - pytest collects 0 tests until Phase 0 adds a test wrapper.
- **FastAPI server:** `PYTHONPATH=src uvicorn finsec_alpharadar.api.server:app --reload --port 8000`. fastapi is not installed on the dev machine yet.
- **Harness integrity:** `python3.13 harness/scripts/verify_install.py --root . --strict`. Local gate: `/hs:test`.
