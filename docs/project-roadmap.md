# FinSec AlphaRadar: Direction and Roadmap

> **Status: PROPOSED 2026-10-20 — pending the owner decisions in section 9.** Nothing below is decided until that section records an answer. Decided so far: §9.3 (real-time = disclosure capture first, ticks in Phase 6).
> Last updated 2026-10-20. Merged from five design lenses (data, modeling, Vietnam market, product, risk) and two critiques.
> Evidence labels: **VERIFIED** means a source URL is given. **OBSERVED** means it was seen by running code or fetching data, with the date. **ASSUMED** means unchecked; it must be probed before anything depends on it.
> This is a research and alerting project. Nothing here is investment advice.

---

## 1. Vision and non-goals

**Vision.** A private research and alerting tool. It turns corporate disclosures in two markets into calibrated predictions that are scored automatically:

- **US:** SEC 8-K (by item code), 10-Q/10-K, Form 4.
- **VN:**
  - quarterly and annual consolidated financial statements (BCTC quý/năm hợp nhất);
  - the mandatory explanation filed when profit moves 10% or more (giải trình biến động lợi nhuận);
  - board and shareholder resolutions (Nghị quyết HĐQT/ĐHĐCĐ) on dividends, share issuance, ESOP, buybacks and M&A;
  - insider trade registrations.

Every prediction is written to an append-only ledger before its outcome exists, and it is scored when its horizon matures. The product is the measured scoreboard, not the number of modules.

**What "real-time" means here** (decided 2026-10-20, §9.3). Phases 0-4 deliver real-time *disclosure capture*. Detection latency is measured and reported, with targets of p50 under 5 minutes (US) and under 10 minutes (VN) during market hours. Tick streaming (Alpaca IEX, SSI FastConnect) is Phase 6, and it starts only after an explicit checkpoint with the owner. Two reasons:

- Free tick feeds are thin. Alpaca Basic is IEX-only with a 30-symbol WebSocket cap.
- VN cash shares cannot be sold before the afternoon of T+2, so intraday forecasts for single stocks cannot be acted on.

**Non-goals.**

- **No order execution**, live or paper, from code or from agents.
  - `ALPACA_PAPER_TRADE=true` stays as defense in depth.
  - The `trading` MCP toolset is removed.
  - No VN broker trading API (SSI FC Trading, DNSE trading) is ever registered.
- **Not investment advice.** No buy/sell (mua/bán) wording, no per-user tailoring, and no group or public distribution before a legal review.
- **No redistribution** of licensed or unofficial data (vnstock, Alpaca, SSI). Only derived signals leave the store, and only to the owner.
- **No infrastructure ahead of evidence.** No Kafka/Redpanda, vector DB, bandit or fine-tune until a scoreboard metric justifies it.

---

## 2. What we predict

### 2.1 Event clock (pre-registered; changing it requires a decision-log entry)

**US information time.**
- `information_time` is the `<ACCEPTANCE-DATETIME>` field in the filing header (`-index-headers.html` / `.hdr.sgml`). It is parsed as America/New_York and stored tz-aware in UTC, next to the raw string.
- The data.sec.gov submissions JSON `acceptanceDateTime` is used for discovery only. Its "Z" suffix is not UTC, and the offset is not fixed. OBSERVED on 2026-10-20 by two independent reviews:

| Accession | Header time (ET) | JSON value |
|---|---|---|
| 8-K 0000320193-26-000018 | 16:30:28 | 2026-07-31T00:30:28Z |
| Form 4 0001140361-26-040127 | 2026-10-19 18:32:55 | 2026-10-20T02:32:55Z |
| 10-Q 0000320193-26-000020 | 06:01:02 | 14:01:02Z |
| EST-period 8-K 0000320193-26-000005 | 16:30:33 | 2026-01-30T02:30:33Z |

**VN information time.**
- `information_time` is the earlier of the portal posting time (when one exists) and our own `first_seen_at` across all sources.
- A date-only timestamp is treated as after the close.

**Earliest source wins.** If any source (a press release, a news site) shows the information earlier, `information_time` is the earliest `first_seen_at`.
- Form 4 clusters are keyed on acceptance time, never on transaction date.
- A `reportDate` more than one business day before acceptance sets a `stale` flag.

**Day 0.** The first regular session whose close comes after `information_time`.
- The US close is 16:00 ET. The VN close is the end of the ATC auction at 14:45 ICT.
- An intraday US filing therefore has day 0 on the same day. An after-close US filing, or a VN date-only event, has day 0 on the next session.

**Feature cutoff.** The cutoff is `information_time` itself, never "the day -1 close". Pre-event features may read only rows with `available_at < information_time`, so the day-0 rule cannot leak information in either direction.

**Estimation window.** Trading days [-250, -30] relative to day 0, with at least 120 observations. The model is the OLS market model at the core of `quant/event_study.py`.

**Benchmarks.**
- US: SPY, requested with `adjustment=all` so that stock and benchmark share the same adjustment basis.
- VN, primary: the equal-weighted adjusted return of the liquid research universe, excluding the event stock.
- VN, robustness: VN-Index. It is a price index and it includes the event stock, so it is not the primary benchmark.
- HNX-Index: robustness check only, and only before 2026-12-28.

### 2.2 Targets

| ID | Target | Market | Horizon | Role |
|---|---|---|---|---|
| R1 | P(outsized reaction): abs(AR0) > 2 sigma of estimation residuals, OR day-0 volume >= 2x the 20-session median | US + VN | day 0 | Alerting and magnitude; first end-to-end pipeline check |
| D1 | P(CAR[+1,+5] > 0) and E[CAR] after an 8-K. Item 2.02 first, then 1.01, 5.02, 8.01. Entry at the day-0 close. | US | 5 sessions | Primary US direction target |
| D2 | P(CAR[+2,+10] > 0) after VN BCTC quý (including the 10%+ profit-change explanation) and NQ HĐQT/ĐHĐCĐ by type. Entry at the day+1 close (ATC), long-only. | VN | 10 sessions | Primary VN direction target |
| D3 | CAR[+1,+20] after Form 4 open-market purchase clusters: code P, 2 or more insiders within 10 days of acceptance | US | 20 sessions | Secondary; checks that we find a known effect |
| D4 | VN CAR[+1,+5], only for events whose pit_quality is exact or first_seen | VN | 5 sessions | Secondary, once forward capture has enough events |
| V1 | 5-session realized volatility after events, scored by QLIKE against a HAR baseline | US + VN | 5 sessions | Optional; alert thresholds and risk context |

**Two label families, never mixed.**
- **Reaction** labels (AR0, CAR[0,+1]) feed R1 and the magnitude targets.
- **Tradable drift** labels start at the first close at which entry is feasible. Only these support direction claims.
- A direction hit rate measured on a [0,k] window is never reported as edge.

### 2.3 Diagnostics only, deferred, or dropped

**Diagnostics.** These never appear on the scoreboard as tradable calls:
- VN 1-session returns, such as foreign-flow persistence.
- The sign of the US return in the 60 minutes after an 8-K.
- The Lopez-Lira & Tang figure of about 90% is a portfolio-day hit rate on the initial reaction, which cannot be traded (VERIFIED https://arxiv.org/abs/2304.07619). It is not a per-event benchmark.

**Deferred.**
- Limit-lock continuation. It needs at least 6 months of forward-captured order-queue snapshots; no history exists.
- Follow-through of intraday alerts (Phase 6).
- A 12-month forensic underperformance study. It is descriptive and needs at least 3 years of point-in-time filings.

**Dropped.**
- The HOSE margin-ineligible list as a *target*. It only replicates published rules, so it is used as a feature instead.
- The VN30F opening gap. The US overnight move is known before the VN open, and there is no free, deep intraday futures history.
- Links from a single US stock to a single VN stock.
- Direct return prediction by an LLM.

### 2.4 Success bar

- **Direction (D1, D2):** a mean per-period IC of 0.02-0.05 and a hit rate of 52-55% over base rate is a good outcome.
- **R1:** an AUC of about 0.65 or more is plausible [ASSUMED prior].
- **Leakage audit:** a hit rate above 60% or an IC above 0.10 on D1/D2 triggers an audit, not a celebration.
- **Null results:** a documented null result with stated power is a pass for any phase. Most short-horizon VN edge is expected to vanish after round-trip costs of about 0.4%.

### 2.5 Power and universe

**Power rule.** t ≈ IC x sqrt(N_eff).
- Detecting IC 0.03 at t = 2 needs about 4,400 effectively independent events.
- VN30 alone gives roughly 960 earnings events over 8 years, which detects only IC of about 0.065 or more.
- Events cluster on deadline days, so N_eff < N. CIs therefore use a date-clustered block bootstrap.

**Research universe.** A point-in-time rule computed from our own data, recomputed monthly:
- US: the top ~500 common stocks by trailing 60-session dollar ADV.
- VN: 150-250 HOSE/HNX names by the same rule.
- Delisted names are kept wherever bars exist. Whether US delisted bars are available is ASSUMED and probed in Phase 0; if they are missing, the survivorship caveat is documented.

**Live watchlist.** At most 30 symbols per market. The Alpaca Basic WebSocket cap only matters from Phase 6.

**Financials.** Banks, insurers and securities firms stay in the event studies. They are excluded from the Altman/Beneish/Piotroski features.

---

## 3. Core loop (MVP)

```
capture       EDGAR poll every 2-5 min; VN disclosure poll every 2-5 min;
              raw archive + raw_sha256 + first_seen_at
  -> parse    typed Event (information_time, day0, event_type, items, pit_quality,
              confound/stale flags, citation URL)
  -> features as-of only: event type, pre-event run-up and volume, size,
              SUE with available_at <= entry, forensic components,
              VN foreign flow / ADV, margin-eligibility status
  -> predict  baselines + L2 logistic (calibrated on train folds only)
  -> ledger   append-only row (mode = backtest/shadow/live, model_version,
              feature_hash, hash chain)
  -> alert    private Telegram 1:1 bot, Vietnamese-first card: probability vs base rate,
              N of analogs, citation, journal id, bilingual disclaimer
  -> score    nightly job fills outcomes when the horizon matures
  -> report   reports/scoreboard.md and GET /v1/scoreboard
```

- **One CLI drives it:** `python -m finsec_alpharadar.pipeline {backfill,poll,predict,score,digest,scoreboard}`.
- **Where it runs:** an always-on host with an NTP-synced clock. No Docker is needed before Phase 6.

---

## 4. Data sources

| Source | Market | Kind | Access | Cost | Legal notes | Status |
|---|---|---|---|---|---|---|
| SEC EDGAR submissions API + filing headers | US | Filing events, 8-K items, Form 4, 10-Q/10-K | JSON, no key. Time comes from the header `<ACCEPTANCE-DATETIME>`. Nightly submissions.zip for backfill. | Free | Fair access: at most 10 req/s with a declared User-Agent (throttle to 5). Public records. | VERIFIED https://www.sec.gov/search-filings/edgar-application-programming-interfaces and https://www.sec.gov/os/accessing-edgar-data. JSON time is not UTC: OBSERVED 2026-10-20 |
| SEC XBRL companyfacts / companyconcept (+ companyfacts.zip) | US | Fundamentals (EPS, revenue, receivables, CFO) | JSON. Every fact carries accn and filed. | Free | Same fair-access rules | VERIFIED (same URL). EPS facts sit mostly in the 10-Q/10-K, not the 8-K: OBSERVED in MSFT companyconcept |
| SEC XBRL Frames | US | Cross-section of the latest values | JSON | Free | Never use for backtests: it returns the latest filed value, so restatements leak | VERIFIED (same URL) |
| edgartools | US | Form 4 and filing parsing | Python library, already in the prod extra | Free | SEC rules apply | VERIFIED that it exists: https://edgartools.readthedocs.io/en/latest/complete-guide/. Return schema ASSUMED |
| Alpaca Market Data, Basic plan | US | Daily/minute bars; IEX real-time | REST/WebSocket with paper keys | Free. Algo Trader Plus is $99/mo (SIP real-time, unlimited symbols). | Personal use, no redistribution (ASSUMED) | VERIFIED limits (IEX-only real-time, 30 WS symbols, 200 calls/min, history since 2016, latest 15 min blocked): https://docs.alpaca.markets/docs/about-market-data-api. `adjustment` defaults to raw: VERIFIED https://docs.alpaca.markets/reference/stockbars. SIP history on Basic and delisted coverage: ASSUMED, probe in Phase 0 |
| Alpaca News (Benzinga) | US | Headlines | REST/WebSocket | Free-plan access ASSUMED | Licensed content; store headline + URL only | ASSUMED (https://docs.alpaca.markets/docs/historical-news-data). Phase 5+ |
| Massive (formerly Polygon) EOD | US | Backup bars for reconciliation | REST | About $29/mo, ASSUMED | Personal use | ASSUMED. Optional |
| vnstock Community (KBS / VCI) | VN | Daily OHLCV, foreign flow, BCTC, company events/news | Python library over unofficial broker endpoints. Gen 5 installs from a vendor index. | Free; sponsor tiers paid | Personal, study and research use only. No redistribution, and no rights to the source data. | VERIFIED licence and install path: https://github.com/thinh-vu/vnstock. Rate limits, history depth, price unit, timestamp granularity, delisted coverage: ASSUMED, probe in Phase 0 |
| SSC / HOSE / HNX disclosure portals (congbothongtin.ssc.gov.vn, hsx.vn, hnx.vn) | VN | Official disclosures with posting time | HTML/PDF polling; no known API | Free | Public mandatory disclosures. Poll politely and respect robots.txt. Insider names are personal data under PDPL. | VERIFIED that the portals exist: https://vietnamnews.vn/economy/1661264/hose-listed-firms-required-to-use-single-disclosure-point.html and https://vietnamnews.vn/economy/1651308/ssc-to-launch-single-point-information-disclosure-system-for-listed-firms.html. URLs, robots.txt and ToS: ASSUMED |
| SSI FastConnect Data | VN | Real-time and EOD prices, foreign room, ceiling/floor | Needs an SSI account and ConsumerID + ConsumerSecret. REST https://fc-data.ssi.com.vn/v2.0/Market, streaming https://fc-datahub.ssi.com.vn/v2.0 | Fees and rate limits unknown (ASSUMED) | Broker-licensed; no redistribution (ASSUMED) | VERIFIED https://guide.ssi.com.vn/ssi-products/tieng-viet/fastconnect-data/huong-dan-ket-noi. Foreign-flow field semantics: ASSUMED until the R-channel spec is read |
| DNSE Lightspeed | VN | Real-time fallback | DNSE account plus a signed agreement | ASSUMED | Broker terms | VERIFIED that it exists: https://www.dnse.com.vn/hoc/dnse-lightspeed-api-la-gi. Protocol and fees ASSUMED |
| HOSE margin-ineligible lists | VN | Feature, not target | Exchange notices or news | Free | Public | VERIFIED via news: https://doanhnhan.baophapluat.vn/danh-sach-59-ma-co-phieu-bi-hose-cat-margin-trong-quy-iii-2026-vuong-an-vi-pham-va-thua-lo.html. Page path ASSUMED |
| VSD corporate-action notices | VN | Record dates, ex-rights | Web | Free | Terms ASSUMED | ASSUMED |
| Circular 96/2020 as amended by Circular 68/2024 | VN | Event definitions: insider notice at least 3 working days ahead; explanation for profit change of 10% or more; English disclosures for large caps (periodic from 2025, extraordinary from 2026) | Public law | Free | n/a | VERIFIED https://vbpl.vn/FileData/TW/Lists/VBPQ/Attachments/146048/VanBanGoc_96-2020-TT-BTC.pdf and https://hethongphapluat.com/thong-tu-68-2024-tt-btc-53e9cb20cc74df0319a9ca98e4deccac/dieu-4. BCTC deadlines and later amendments (18/2025, 08/2026): ASSUMED |
| Circular 99/2025 | VN | Accounting regime for periods from 2026 (customizable chart of accounts) | Public law | Free | n/a | VERIFIED https://kpmg.com/vn/en/home/insights/2025/11/key-changes-in-vietnamese-accounting-system-for-enterprises.html |
| VN market-structure reference (bands, settlement, transfer, regimes) | VN | Config with effective dates | Hand-maintained, cross-checked against feed ceiling/floor | Free | n/a | See "VN market-structure reference" below the table |
| VN trading costs | VN | Cost model | Constants in backtest/costs.py | n/a | n/a | 0.1% personal income tax on sale value: VERIFIED https://thuvienphapluat.vn/ma-so-thue/phap-luat-thue/tu-172026-chuyen-nhuong-chung-khoan-nop-thue-tncn-bao-nhieu-phan-tram-218598.html. Brokerage of about 0.13-0.15% per side: ASSUMED (secondary source) |
| Telegram Bot API | Both | Private alerts to the owner | HTTPS bot | Free | About 1 msg/s per chat | VERIFIED https://core.telegram.org/bots/faq |
| VN news (CafeF, Vietstock), GDELT | Both | News context | RSS / scraping / API | Free | ToS risk; headline + URL + features only | ASSUMED. Phase 5+ |
| Vietnamese embeddings (BAAI/bge-m3, AITeamVN/Vietnamese_Embedding) | VN text | Retrieval for analog events | Hugging Face | Free, Apache-2.0 | n/a | VERIFIED model card: https://huggingface.co/AITeamVN/Vietnamese_Reranker. Gains are self-reported. Phase 7 |
| LLM provider | Both | Structured extraction | API | Unknown | Provider terms; no secrets or PII in prompts | ASSUMED. The model IDs in `api/server.py` and `router/linucb_bandit.py` are unverified; `gemini-4-argon-pro` was not found anywhere |
| Research priors | US | Lazy Prices (filing-text similarity); LLM headline scoring | Papers | Free | n/a | VERIFIED https://www.nber.org/papers/w25084 and https://arxiv.org/abs/2304.07619 |

**VN market-structure reference.**
- Price bands of ±7% (HOSE), ±10% (HNX) and ±15% (UPCOM), with ±40% on an UPCOM first trading day: VERIFIED from secondary sources, https://www.ssi.com.vn/en/individual-customer/transaction-regulation-on-upcom and https://www.dsc.com.vn/kien-thuc/bien-do-giao-dich-san-chung-khoan-hose-hnx-upcom.
- Shares can be sold from the T+2 afternoon session: VERIFIED https://hdsd.dnse.com.vn/cau-hoi-thuong-gap/giao-dich/giao-dich-chung-khoan and https://vietnamfinance.vn/du-kien-tu-thang-8-nha-dau-tu-co-the-ban-co-phieu-trong-phien-chieu-ngay-t2-d82128.html.
- HNX-listed stocks move to HOSE: the last HNX trading day is 2026-12-23 and HOSE trading starts 2026-12-28. VERIFIED from secondary press:
  - https://vietnamnet.vn/en/vietnam-to-move-all-hnx-listed-stocks-to-hose-in-december-2560492.html
  - https://thitruongtaichinhtiente.vn/toan-bo-co-phieu-niem-yet-se-giao-dich-tai-hose-tu-cuoi-thang-12-2026-85954.html
  - https://vietnamnews.vn/economy/1784883/nearly-300-stocks-to-move-from-hnx-to-hose-under-market-restructuring-plan.html
- KRX trading system live since 2025-05-05: VERIFIED https://vietnamnews.vn/economy/1717047/krx-system-officially-goes-live.html.
- FTSE secondary-emerging status effective 2026-09-21: VERIFIED https://www.lseg.com/en/media-centre/press-releases/ftse-russell/2025/ftse-russell-country-classification-september-2025 and https://en.vietstock.vn/2026/04/ftse-russel-confirms-upgradation-of-viet-nam8217s-stock-market-from-sept-21-36-630323.htm.
- Still ASSUMED: tick sizes, special-day bands, the band that transferred stocks will use, and UPCOM timing.

---

## 5. Architecture direction

### 5.1 Stack

- **Raw archive.** Append-only gzip JSONL (or Parquet), one file per source per day, with `raw_sha256`, `first_seen_at` and `ingested_at`. This is the replayable log: parsers can be re-run, and reproducibility hashes cover raw data, never vendor-adjusted pulls.
- **Operational store.** SQLite in WAL mode, because the poller and the scorer write from separate processes. Tables: events, predictions, outcomes, data_health, securities, exchange_membership, corporate_actions, sectors.
- **Research.** DuckDB over Parquet plus SQLite for as-of joins. `store.as_of(t)` is the only read path for features.
- **Layering.** connectors (I/O only) -> store -> features/labels (pure functions that take `as_of`) -> modeling -> ledger -> api/notify (read-only). An import-linter contract can enforce this later.
- **Values.** Financial values use Decimal, integer minor units or NUMERIC. VN prices are in full VND, converted at the connector boundary.
- **Deferred or removed.**
  - TimescaleDB: Phase 6, tick storage only.
  - Qdrant: Phase 7 at the earliest, if ever.
  - Redpanda and Pathway: removed.

### 5.2 Module map

| Path | Action | Phase | Note |
|---|---|---|---|
| `AGENTS.md` | fix | 0 | Done 2026-10-20: honest status, proposed direction, verified facts, rules and the agent memory protocol; the WebSocket, semantic-vector, O(1)-dedup and SSI-endpoint claims were corrected. |
| `CLAUDE.md` | keep | 0 | Owner decision 2026-10-20: CLAUDE.md only imports `@AGENTS.md` and keeps the generated harness block. All project knowledge lives in AGENTS.md. |
| `docs/system-architecture.md`, `docs/code-standards.md` | add | 0 | Required by /hs:plan and /hs:cook at Step 0 (harness/standards/README.md). They hold the PIT contract, the event clock, the layering and the testing rules. |
| `docs/project-roadmap.md` | add | 0 | This document. |
| `src/finsec_alpharadar/domain/models.py` | fix | 0-1 | Use tz-aware datetimes. Add Event, FundamentalFact (long format, with accn and published_at), Prediction and Outcome. Raise on an unknown exchange instead of silently using 7% (line 44). Round ceiling/floor to ticks and use full VND. Replace the static `VNExchangeBand` (lines 13-16) with time-versioned exchange membership. |
| `connectors/us_alpaca_edgar.py` | rewrite | 1 | Add a real EDGAR client: header acceptance time, at most 5 rps, User-Agent from EDGAR_IDENTITY, submissions, Form 4 via edgartools, companyfacts by accn, bulk zips. Add an Alpaca daily-bar client with `adjustment=all` and an explicit feed. Remove the `now()` fallback (line 20) and the fabricated bid/ask sizes. Data endpoints only. |
| `connectors/vn_ssi_vnstock.py` | rewrite | 0, 3 | Add a vnstock adapter pinned to the probed version, using KBS for statements as vnstock's docs recommend. Fix the SSI endpoints now; defer the live client to Phase 6, and write per-channel parsers only after reading the R-channel spec. Remove the `now()` fallback and the BidVol/AskVol = 100 defaults. |
| `connectors/base.py` | add | 1 | SourceAdapter protocol, RawRecord, token-bucket limiter, retry with backoff, idempotent key (source, source_id). |
| `capture/` | add | 0 | Capture-only VN disclosure poller with no parsing: raw payload, sha256, first_seen_at, outage log, heartbeat. |
| `store/` | add | 1 | Raw archive writer, SQLite schema, DuckDB views, `as_of()`. |
| `refdata/` | add | 1, 3 | Security master (permanent id, ticker, CIK, exchange, valid_from/valid_to); exchange membership including the 2026-12-28 transfer; versioned sectors; corporate actions. Every join goes through it as of the event date. |
| `calendar/` | add | 1, 3 | XNYS sessions (the exchange_calendars package is ASSUMED suitable). VN sessions derived from VN-Index bar dates, future holidays from HoSE notices, and regime flags. |
| `quant/event_study.py` | keep core | 1 | Keep the OLS market model and t-stat. The bare-array interface is wrapped by `labels/`, which adds timestamp alignment, window assertions, minimum history and censor flags. |
| `labels/` | add | 1 | Reaction and drift labels; outsized flag; censor, confound and stale flags. |
| `features/` | add | 2-3 | Event type, pre-event run-up, size, SUE with available_at, forensic components, VN foreign flow / ADV, margin-eligibility status. |
| `modeling/` | add | 2 | Baselines, L2 logistic and an isotonic calibrator. LightGBM only if the logistic IC CI excludes zero. The package is named `modeling/`, not `models/`, because of `.gitignore`. |
| `backtest/` | add | 2 | walk_forward.py, metrics.py, costs.py (US and VN), report.py. |
| `ledger/` | add | 2 | Append-only predictions and outcomes with mode, model_version, feature_hash and a hash chain; nightly scorer. |
| `pipeline/__main__.py` | add | 1-4 | CLI subcommands. |
| `notify/telegram.py` | add | 4 | Private 1:1 bot to an allowlisted chat id; digests; heartbeat. |
| `forensics/xbrl_bctc_auditor.py` | fix | 2 | Guards for zero division and missing data (return None with a reason). Sector gate. Altman Z'' for non-manufacturers. Beneish on trade receivables only. Piotroski's dilution test ignores stock dividends and bonus shares. Mapping for Circular 99 vs Circular 200. Return the raw components as features. |
| `quant/kalman_microstructure.py` | defer | 6 | Known bugs are recorded: foreign flow is summed over a 50-tick deque (line 99), but fix it only after reading the SSI field semantics, because the test at tests/test_end_to_end.py:94-95 feeds increments. It is also session-blind, never validates the band, and gives OBI = ±1 on a limit lock. The volume z-score and foreign-net logic can be reused on daily bars in Phases 2-3. |
| `quant/deduplicator.py` | keep | 0, 4 | Fix the "O(1)" docstring (it is a linear scan). Use it to collapse disclosures reposted by several sources. |
| `rag/hybrid_temporal_rag.py` | minimal fix, then defer | 0, 5, 7 | Phase 0: add a `published_at <= as_of` filter, fix the falsy `reference_time or now` (line 112) and the age clamp (line 138), with a regression test, and label the "dense"/ColBERT legs as hash-based placeholders. Phase 5: NFC-aware Unicode tokenizer. Phase 7: real embeddings. |
| `router/linucb_bandit.py` | remove from API | 0 | On a cold start the arms tie and the first arm always wins (line 40); no LLM is called and there is no reward. Move it to `experimental/`. |
| `api/server.py` | rewrite | 4 | Read-only GET /v1/events, /v1/predictions, /v1/scoreboard and /v1/data-health, with pydantic models, bound to localhost. Remove the tick ingest, the empty RAG search, the bandit and the hard-coded model IDs. |
| `scripts/train_unsloth_slm.py`, `configs/unsloth_qwen3_catalyst.yaml` | defer | 7 | Use only as a distilled extractor. Drop `abnormal_return_sign_accuracy_reward`; keep the schema and number-faithfulness rewards. |
| `tests/test_end_to_end.py` | fix | 0 | Wrap `run_modular_verification` in `test_end_to_end()`. Add test_edgar_time, test_no_order_execution and test_agents_md_paths (Phase 0), then test_point_in_time and test_labels (Phase 1). |
| `pyproject.toml` | fix | 0 | Add `[tool.pytest.ini_options] pythonpath = ["src"]`. Add a dev extra (pytest, hypothesis, ruff) and a research extra (pandas or polars, pyarrow, duckdb, scikit-learn; these choices are ASSUMED). Move pathway, qdrant-client and pydantic-ai to a deferred extra. Pin vnstock to the probed version. |
| `.gitignore` | fix | 0 | Anchor `data/` and `models/` as `/data/` and `/models/`. Unanchored, they silently ignore any source package with those names (OBSERVED by git check-ignore). |
| `docker-compose.yml` | fix | 0 | Remove redpanda. Put qdrant and timescaledb behind compose profiles. Pin image tags, bind to 127.0.0.1, read the password from env. |
| `Makefile` | fix | 0 | Bind run-api to 127.0.0.1. Document the PowerShell equivalents. |
| `.env.example` | fix | 0 | Drop SSI_PRIVATE_KEY_PATH. Add SSI_API_URL, SSI_STREAM_URL and ALPACA_DATA_FEED. EDGAR_IDENTITY must be a real contact. |
| `.agents/mcp_config.json` | fix | 0 | Remove `trading` from ALPACA_TOOLSETS. Pin git refs and package versions. MCP is for exploration, never a pipeline data source. |
| `.agents/rules/*.md` | fix | 0 | Replace rule 3 (BM25 + dense + MaxSim required in every *.py) with a pointer to AGENTS.md. Band rule: tick rounding, feed-provided bands, special days. Storage rule: NUMERIC plus published_at, with tick tables scoped to Phase 6. |
| `.agents/hooks.json`, `scripts/validate_command_safety.sh` | fix later | 4 | Wired for another tool. The real guards are tests and harness hooks. |

### 5.3 Resolved contradictions

| About | Decision | Why this side wins |
|---|---|---|
| Storage engine | Embedded: raw JSONL/Parquet + SQLite WAL + DuckDB. TimescaleDB only for ticks in Phase 6. | Product and Risk lenses over Data and Modeling: the dev box is Windows, no docker-compose service is used, and there is one developer. |
| Market order | US pipeline first. VN capture-only poller from week 1. VN becomes the main research market from Phase 3. | Free, cleanly timestamped US data makes the first measurable signal cheapest. VN publish times cannot be backfilled, so capture starts at once. The VN-first lens loses on sequencing, not on importance. |
| EDGAR time | Header ACCEPTANCE-DATETIME read as ET; the JSON value is for discovery only. | The mismatch was observed on four accessions. Every lens that read "Z" as UTC was wrong, including the Data lens's "20:30 ET" arithmetic. |
| Day-0 rule | First session whose close comes after information_time; feature cutoff = information_time. | The Modeling lens's rule. It keeps the same-day reaction out of the pre-event window, and the cutoff makes leakage structurally impossible. |
| Label windows | Reaction family vs tradable-drift family | Modeling lens. The other windows mix the announcement jump, which cannot be traded, into direction claims. |
| VN earliest exit | T+2 afternoon session | VERIFIED (DNSE guide). The VN lens's T+3 was wrong. |
| SSI credentials | ConsumerID/Secret only; fc-data / fc-datahub endpoints; drop SSI_PRIVATE_KEY_PATH | The SSI connection guide (VERIFIED). A private key belongs to FC Trading, which this project never uses. Restore it only if a real FC Data call needs a signature. |
| Alpaca Basic SIP history | ASSUMED until probed; `adjustment=all` is mandatory | The docs page does not name feed=sip for Basic. The raw default for `adjustment` is VERIFIED. |
| vnstock limits | ASSUMED; limiter at 20/min until measured | The official README gives no numbers; the figures come from a third-party mirror. |
| Universe size | Broad point-in-time ADV-ranked research universe plus a live watchlist of 30 or fewer | The Modeling lens's power calculation: 30 + 30 names cannot detect a realistic IC. |
| RAG timing | Phase 0: as_of look-ahead fix only. Tokenizer in Phase 5. Embeddings in Phase 7. | A shipped look-ahead bug violates the core rule and agents may copy it. The rest is off the critical path, and English VN disclosures (Circular 68/2024) reduce the urgency of tokenizer and OCR work. |
| LinUCB | Remove from api/server.py now; park in experimental/ | "Remove" and "defer" mean the same in practice. The API should not advertise routing to models that are never called. |
| event_study.py | Keep the OLS core; new interface in labels/ | The maths is fine. The bare-array interface is the leak risk. |
| Telegram vs no auto-posting | Private 1:1 bot to one allowlisted chat id | Compatible with the Risk lens: groups and channels count as publishing and stay off. |
| GRPO reward | Drop the abnormal-return-sign reward | Noisy small-N labels plus pretraining contamination. Three lenses against one. |
| HNX benchmark | Own-universe benchmark; HNX-Index only before the transfer | HNX stocks move to HOSE on 2026-12-28. |
| Margin list | Feature, not target | It replicates published rules on illiquid, distressed names, and each broker keeps its own list. |
| Phase sizing | Phase 0 = spike + foundations. VN split into dataset (Phase 3) and model + live loop (Phase 4). | Both critiques: the original phases exceeded what one person can do in 2-4 weeks. |

---

## 6. Evaluation protocol

### 6.1 Splits

- Walk-forward with an expanding window and monthly retraining, split by `information_time`.
- Purge any training event whose label window overlaps the test period. Embargo for at least the label horizon (5 sessions US, 10 VN).
- The final 12 months are held out and untouched until a phase's decision.
- Trial ledger: every configuration tried is logged and counted (W&B or a local CSV). Report deflated metrics when many variants were tried.

### 6.2 Metrics

Reported per market x target x event type:
- N, N_eff and base rate;
- hit rate vs base rate;
- Brier skill score vs base rate, log loss and reliability bins;
- AUC (R1);
- mean per-period Spearman IC with a Newey-West t;
- date-clustered block-bootstrap 95% CI.

Portfolio-level metrics (decile spread, calendar-time Sharpe after costs, deflated Sharpe) come in only after a baseline shows a non-zero IC.

### 6.3 Costs and VN constraints

**Costs.**
- US: 5-10 bps per side [ASSUMED, conservative].
- VN: 0.1% tax on sale value (VERIFIED) + brokerage of about 0.15% per side [ASSUMED] + half the spread, about 0.4% round trip.

**VN trading rules.**
- Long-only (no retail shorting), 100-share lots.
- Earliest exit is the T+2 afternoon session.
- Entry or exit sessions locked at the band are unfillable, and their labels are censored.
- Suspensions are flagged; names under warning or control are flagged; an ADV liquidity filter applies.

**Corporate actions.**
- Store raw unadjusted bars and adjust them ourselves from `corporate_actions`; VN stock dividends, bonus shares and rights issues are frequent.
- Returns use adjusted prices; band checks use raw prices.
- A window that contains an ex-date is flagged as confounded.

**Regime flags.** Results are reported per regime, and foreign-flow features are never pooled across a break without a dummy.
- KRX go-live, 2025-05-05.
- Circular 99 accounting for periods from 2026.
- FTSE effective 2026-09-21, with tranches into 2027.
- HNX-to-HOSE transfer, 2026-12-28.

### 6.4 Leakage checklist (each item is a pytest)

1. Every feature row has `available_at` < decision time: `information_time` for R1, entry time for the drift labels.
2. EDGAR time golden test on the four pinned accessions, including one EST-period filing. Session-classifier tests cover 06:00, 12:00-16:00 and after-16:00 ET acceptances.
3. XBRL fundamentals:
   - an XBRL fact is used only if its accn was accepted at or before entry;
   - Frames are never used for history;
   - SUE stays missing, never filled from the future, when the 10-Q lags the 8-K;
   - Q4 = FY minus the 9-month YTD value, flagged as approximate.
4. The estimation window ends before day -30 (assertion).
5. Shuffled labels give abs(IC) < 0.01. Placebo random event dates give IC ≈ 0.
6. Shifting features one day into the future must not help. Shifting labels one day earlier must collapse performance.
7. Universe and sector are taken as of the event date through the security master, with delisted names retained.
8. Prefix invariance (hypothesis): appending future records never changes any output at or before t, for features, labels and retrieval.
9. Scalers and calibrators are fit on train folds only.
10. LLM features are used only on events after the model's documented training cutoff (Phase 5).
11. The live scoreboard counts only `mode = live` rows created before their entry or effective session, checked against the hash chain.
12. A robustness run excludes `pit_quality` = inferred/late events and confounded events.

### 6.5 Scoreboard

`reports/scoreboard.md` is regenerated nightly. At each phase exit, its headline numbers are copied into AGENTS.md.

---

## 7. Phased roadmap

Each phase is sized for one person over 2-4 weeks and has a single exit gate.

### Phase 0: Verify and foundations (2 weeks), NEXT

**Goal.** Every load-bearing fact probed, the repo honest and testable, and VN capture running.

**0a. Verification spike (2-3 days).** Results go into the "Verified Facts" section of AGENTS.md, with dates.
- **EDGAR time golden test:** `tests/test_edgar_time.py`, with recorded fixtures for the four accessions in section 2.1.
- **Alpaca probe with the paper keys:**
  - SPY daily bars for 2017 with feed=sip on the Basic plan;
  - `adjustment=all` vs raw on a known split date;
  - bars for 2-3 known delisted tickers.
- **vnstock probe** in an isolated venv. Pin the version, and hash-pin the Gen 5 vendor index as a supply-chain dependency. Measure:
  - the real rate limit;
  - daily and intraday history depth;
  - the price unit per source (thousand vs full VND);
  - whether events/news carry a time of day;
  - delisted coverage;
  - adjusted series on 5 known ex-dates.
- **SSI:** confirm the owner's account status. Read the R-channel spec for the foreign-flow field names and whether they are session-cumulative.
- **VN timestamps:** hand-check 30 VN disclosures, comparing portal posting time with vnstock time.
- **Output:** a counts table (events per type per year, share with intraday timestamps) and a go/no-go per VN target against the power rule.

**0b. Foundations.**
1. Write `docs/system-architecture.md` and `docs/code-standards.md` with `/hs:docs` or `harness/scripts/scaffold_standards.py`. They cover the PIT contract, the event clock, the layering, the module status and the testing rules. `/hs:plan` stops without them.
2. AGENTS.md now carries the status, proposed direction, facts and rules (done 2026-10-20). CLAUDE.md stays a bare `@AGENTS.md` import. Update AGENTS.md as each owner decision in section 9 is answered.
3. Tests:
   - pytest config with `pythonpath`, the `test_end_to_end()` wrapper and the dev extras;
   - `tests/test_no_order_execution.py`: no TradingClient, submit_order or FC Trading imports in `src/`;
   - `tests/test_agents_md_paths.py`: every path that AGENTS.md describes as existing does exist.
   - The local gate is `/hs:test`. Add a GitHub Actions workflow only if a GitHub remote exists (there is no `.github/` today); otherwise add a pre-commit config.
4. Hygiene:
   - anchor the `.gitignore` patterns;
   - remove the `trading` toolset and pin the MCP refs;
   - drop SSI_PRIVATE_KEY_PATH and add the SSI URLs;
   - docker-compose: remove redpanda, add profiles, pin tags, bind 127.0.0.1, read the password from env;
   - Makefile: bind run-api to 127.0.0.1;
   - document the PowerShell commands.
5. Correctness:
   - the RAG as_of fix, with a regression test;
   - `models.py` raises on an unknown exchange;
   - connectors stop defaulting timestamps to `now()`;
   - fix `.agents/rules` rule 3 and the band rule.
6. VN capture-only poller (`capture/`):
   - **Schedule:** every 2-5 minutes from 08:00 to 17:30 ICT on trading days, hourly otherwise.
   - **Sources:** vnstock news/events for the seed universe, plus one official portal listing page (check its URL and robots.txt first).
   - **Stored:** raw payload, raw_sha256, and first_seen_at (UTC, from an NTP-synced host).
   - **Reliability:** an outage log marks gaps so that later first_seen values get `pit_quality = late`; a daily heartbeat; a nightly off-machine backup.

**Exit.** All four must hold:
- pytest collects and passes at least 15 tests, including the EDGAR golden test;
- the spike table is in AGENTS.md;
- `/hs:plan` runs;
- the VN poller has run 5 consecutive trading days with a heartbeat and no unlogged gap.

### Phase 1: US point-in-time event set and labels (3 weeks), NEXT

**Goal.** A reproducible, look-ahead-free US event dataset with labels.

**Deliverables.**
- `connectors/base.py` and `store/` (raw archive, SQLite schema, DuckDB views, `as_of()`).
- EDGAR client:
  - submissions for discovery, header acceptance time and 8-K items;
  - Form 4 via edgartools (transaction code, shares, price, insider role);
  - companyfacts versions by (accn, filed), joined to acceptance time;
  - nightly bulk zips for backfill;
  - at most 5 rps, a real User-Agent, and backoff.
- Alpaca daily bars from 2016 to now for the research universe and SPY. Request `adjustment=all` and store raw bars separately; choose the feed from the spike result.
- `refdata/` security master (CIK, ticker history from EDGAR tickers/formerNames, delisted names), the XNYS calendar, and the point-in-time universe rule.
- `labels/` over `quant/event_study.py`:
  - the day-0 rule and the estimation window;
  - AR0, CAR[0,+1], CAR[+1,+5] and CAR[+1,+20];
  - outsized flag;
  - confound flag (another event or an ex-date within [-1,+k]);
  - stale flag.
- CLI: `pipeline backfill --market us --since 2017-01-01`.
- Tests: point-in-time, session mapping, synthetic CAR, prefix invariance, and an adjustment regression on a known split.

**Exit.**
- At least 5,000 labeled US 8-K events (2017-2026), rebuilt by one command from the raw archive, with the hash of the raw partitions recorded.
- 20 hand-checked events match the EDGAR index "Accepted" time and session.
- All leakage tests pass.

### Phase 2: US baselines, walk-forward scoreboard, shadow ledger (3 weeks)

**Goal.** The first measured prediction number, and forward evidence starting to accumulate.

**Deliverables.**
- `modeling/`:
  - baselines: always-up, zero, pre-event momentum sign, past-only per-type mean, and historical abs(AR) for R1;
  - L2 logistic;
  - isotonic calibration fit on train folds only.
- `features/`: event type and items, pre-event run-up and abnormal volume (before `information_time`), size, SUE with `available_at` ≤ entry, and forensic components after the forensics fixes.
- `backtest/`: walk-forward with purge and embargo, the holdout, metrics, block bootstrap, and US costs.
- `ledger/` and the nightly scorer.
- `pipeline poll` on EDGAR every 2-5 minutes during US hours. It writes `mode = shadow` predictions: R1 before the day-0 close, D1 before entry. Detection latency goes into data_health.
- `reports/scoreboard.md`.

**Exit.**
- A holdout scoreboard for R1 and D1, with CIs and stated power.
- At least 10 trading days of shadow predictions, each created before its entry time.
- A decision recorded in AGENTS.md: edge, or a documented null.
- LightGBM is added only if the logistic IC CI excludes zero.

### Phase 3: VN EOD dataset and labels (3-4 weeks)

**Goal.** A VN event dataset with timestamps we own, and labels that respect VN trading rules.

**Deliverables.**
- vnstock adapter (pinned, with the limiter set from the probe): raw unadjusted daily OHLCV in full VND, foreign trading, statements (KBS), and company events.
- `corporate_actions` with valid_from, and our own adjustment factors.
- `refdata/`: VN securities, exchange membership (HNX to HOSE on 2026-12-28), and sector versions.
- `calendar/vn`: sessions from VN-Index bar dates, HoSE holiday notices, and regime flags.
- Bands:
  - prefer the feed's Ceiling/Floor;
  - fall back to static ±7/10/15% with tick rounding (HOSE 10/50/100 VND, HNX/UPCOM 100 VND; ASSUMED from secondary sources);
  - special-day bands configurable (ASSUMED);
  - censor flags.
- Events:
  - parse the raw capture from Phase 0 and a vnstock events backfill into events with pit_quality;
  - keyword classifier on NFC-normalized titles: BCTC quý/năm, giải trình of 10% or more, NQ HĐQT types, ĐHĐCĐ, insider registration;
  - use the English versions for large caps where they exist (Circular 68/2024).
- The VN benchmark (equal-weighted universe excluding the event stock), with VN-Index as robustness.
- Labels R1, D2 and D4, with confound flags for AGM season, ex-dates and index reviews.

**Exit.**
- CAR tables for at least 3 VN event types with N, N_eff and power, rebuilt from raw data by one command.
- The sensitivity of results to exact vs inferred timestamps is reported.
- The unit and band sanity check passes on 5 known ex-dates.

### Phase 4: VN scoreboard, daily live loop, private alerts (3-4 weeks)

**Goal.** The end-to-end product runs every trading day without the owner touching it.

**Deliverables.**
- The Phase 2 harness run on VN: baselines and logistic for D2 and R1, with the VN cost model and constraints.
- `pipeline poll/predict/score/digest` for both markets. `mode = live` predictions are written before entry.
- `notify/telegram.py`: a private 1:1 bot sending to an allowlisted chat id.
  - Card: Vietnamese-first, showing event type, probability vs base rate, N of historical analogs, calibration bucket, citation URL, journal id and a bilingual disclaimer, with no mua/bán wording.
  - Digests at 08:30 ICT and 09:00 ET, a weekly scoreboard and a heartbeat.
  - A one-tap useful/not-useful reply, stored as feedback.
- The `api/server.py` rewrite (read-only, localhost). The router moves to `experimental/`.

**Exit.**
- 20 consecutive trading days unattended.
- Every live prediction was created before its entry or effective session, verified by the hash chain.
- Matured predictions are scored automatically.
- Live vs backtest Brier is reported.
- Detection latency on the hand-checked sample: p50 under 5 minutes US, under 10 minutes VN.

**Owner checkpoint after Phase 4.** Choose Phase 5, Phase 6, both, or stop and write up the results.

### Phase 5: Text and LLM extraction (gated, 3-4 weeks)

**Gate.** The Phase 2 and Phase 4 scoreboards exist.

**Deliverables.**
- `features/text_similarity.py`: TF-IDF cosine between a filing and the firm's previous filing of the same type (Lazy Prices).
- `vn_text`:
  - NFC and tone-mark normalization;
  - a locale number parser (1.234.567,89; đồng / nghìn / triệu / tỷ đồng) and dd/mm/yyyy dates;
  - detection of legacy encodings (TCVN3/VNI);
  - a Unicode tokenizer in `rag/`.
- `llm/extract.py` produces strict JSON: event type, dividend ratio, issue size and price, buyback volume, plan targets.
- `llm/faithfulness.py` traces every number to a source span within tolerance and rejects the output otherwise.
- LLM operation:
  - rule-based routing with the cheapest model first;
  - cache by sha256 and a daily cost cap;
  - model id and training cutoff logged.
- Evaluation set: 200 hand-labeled VN resolutions (bilingual pairs where available) and 50 8-Ks.

**Exit.**
- Field accuracy of at least 90%.
- Text and LLM features are merged only if the post-cutoff walk-forward Brier or IC improves with a CI that excludes zero. Otherwise the drop is documented.

### Phase 6: Intraday real-time layer (gated on the owner checkpoint, 3-4 weeks)

**Deliverables.**
- Alpaca IEX WebSocket (30 symbols or fewer), with a feed-quality flag.
- SSI FastConnect streaming, only if an account exists: parsers for X-TRADE, X-QUOTE, R, MI and F, with auth, reconnect, heartbeat and gap backfill.
- A TimescaleDB hypertable with 1-minute continuous aggregates, or Parquet if that is enough.
- `quant/kalman_microstructure.py`:
  - make it session-aware: exclude ATO/ATC, put-through and odd-lot prints, and reset the Kalman dt at breaks;
  - compute foreign flow from the verified cumulative semantics (delta from the session open, VND / ADV, room-aware);
  - add a limit-lock state and validate the band;
  - journal alerts with 30-minute and close follow-through against a random-time baseline.

**Exit.**
- 5 trading days unattended, with under 1% of 1-minute bars missing compared with REST bars.
- Alert precision over at least 200 alerts beats the random-time baseline. Otherwise the channel is disabled and the result documented.

### Phase 7: Research extensions (optional)

Each item needs an `/hs:plan` that cites the metric it is expected to improve.

- **Qwen3-1.7B QLoRA/GRPO** as a distilled extractor, with schema and number-faithfulness rewards. It needs a CUDA GPU [ASSUMED]. Keep it only if it retains at least 90% of the frontier extractor's incremental IC at lower cost.
- **LinUCB routing**, only after thousands of logged (context, model, validator pass, cost) tuples exist.
- **Semantic retrieval** of "similar past events and their realized CAR" with a bge-m3-family model, re-evaluated on filings.
- **Index-level US to VN tests:** US overnight moves vs the VN-Index/VN30 open; trade actions vs exporters.
- **Forensic 12-month study.**
- **Alpaca SIP** ($99/mo), only if intraday US signals matter by then.

**Timeline.** Phases 0-4 take about 14-16 weeks to reach a dual-market daily loop with a scoreboard.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Look-ahead from timestamps: the EDGAR JSON "Z" value, after-hours filings, date-only VN disclosures, restated XBRL, and a 10-Q that lags the 8-K for SUE | Header acceptance time; the day-0 rule; feature cutoff = information_time; accn-level availability; golden and prefix-invariance tests; pit_quality robustness runs |
| VN timestamps cannot be backfilled, and a sleeping laptop creates fake late publications | Capture-only poller from Phase 0 on an always-on NTP-synced host; outage log with pit_quality = late; heartbeat; nightly backup |
| Small N and data snooping | Pre-registered targets; power rule; walk-forward with purge/embargo; holdout; trial ledger; block bootstrap; a null result counts as a pass; leakage audit above IC 0.10 |
| Fake abnormal returns from corporate actions and splits (frequent VN stock dividends; Alpaca's default `adjustment=raw`) | Raw bars plus our own adjustment for VN; `adjustment=all` for US stocks and SPY; split regression test; ex-date confound flag |
| VN market-structure breaks: KRX, Circular 99, FTSE tranches into 2027, the HNX-to-HOSE transfer on 2026-12-28 with an unknown band, a possible T+0 or short-selling launch | Time-versioned membership; feed-provided bands; raise on an unknown exchange; regime flags; per-regime reporting; re-check the HoSE notice before 2026-12-23 |
| Unofficial VN endpoints (vnstock) break or change, and the licence forbids redistribution | Adapter isolation; raw archive; pinned and hash-pinned version; source health in the digest; private outputs only; reconciliation against a portal sample |
| Publishing predictions could count as unlicensed investment advice. VN securities advisory is a licensed business (secondary source, ASSUMED). In the US the Advisers Act applies unless the publisher exclusion does: Lowe v. SEC, VERIFIED https://www.law.cornell.edu/supremecourt/text/472/181. Wrong summaries could also count as false information or manipulation; Decree 156/2020 fines are VERIFIED from a secondary source, https://lawnet.vn/thong-tin-phap-luat/en/chinh-sach-moi/a-fine-of-10-times-of-the-illegal-revenue-for-manipulation-of-securities-market-in-vietnam-108568.html | Private 1:1 alerts only; no buy/sell wording; bilingual disclaimer; number-faithfulness validator; legal review before any group, public or paid use. Not legal advice. |
| Personal data in VN insider disclosures and any future user accounts (PDPL 91/2025, effective 2026-01-01, VERIFIED https://english.luatvietnam.vn/dan-su/law-on-personal-data-protection-law-no-91-2025-qh15-405135-d1.html) | Pseudonymize individual names and keep role and amounts; store no user PII |
| Accidental order execution | No trading toolset; a test that bans trading clients; the paper flag as defense in depth; never register FC Trading |
| Secret leakage | Drop unneeded keys (the SSI private key); keep keys outside the repo; load them via `harness/bin/hs-secrets`; optional gitleaks pre-commit; rotate anything ever pasted into chat |
| EDGAR blocks requests (403/429) | At most 5 rps, a real User-Agent, backoff, bulk zips for backfill |
| IEX-only free real-time data misrepresents volume and OBI | Daily bars for labels; feature-quality flag; pay for SIP only after a daily signal shows edge |
| Scope creep for one developer | Complexity gate; one exit criterion per phase; module status in AGENTS.md; owner checkpoint after Phase 4 |
| Agent drift: AGENTS.md overclaims, and .agents rule 3 pushes agents toward RAG | test_agents_md_paths; "last verified" dates; rules that point to AGENTS.md; decision log |
| Code silently not committed because `.gitignore` matches `models/` and `data/` | Anchor the patterns in Phase 0; name the package `modeling/` |
| Supply chain: unpinned MCP git refs, @latest packages, :latest images, the vnstock vendor index | Pin SHAs, versions and digests; hash-pin packages |

---

## 9. Open decisions (owner)

Each decision is unanswered until the owner picks an option. Record the answer here and in AGENTS.md, with the date.

### 9.1 What is the project for, and who will ever see its outputs? This decides the legal and data-licensing posture: VN advisory licensing, the US publisher exclusion, and the vnstock/Alpaca/SSI no-redistribution terms.

- Personal research or study project, with outputs visible only to the owner
- Thesis or course project with a demo shown to graders or a committee
- Public or monetized: website, Telegram/Zalo group, paid tier

**Recommendation:** Personal research or study use, with private outputs. Before any demo shown to others, use replayed historical data with derived signals only. Any public, group or paid use needs a legal review and written data agreements first.

**Decision:** _open_

### 9.2 Which market leads the build?

- Build the US pipeline first (EDGAR + Alpaca), start VN disclosure capture in week 1, and make VN the main research market from Phase 3
- Vietnam only
- Both markets in parallel from day one

**Recommendation:** US pipeline first, with VN capture starting in week 1. US timestamps are clean and free, so the first measurable number comes fastest. VN publish times cannot be backfilled, so VN capture must start immediately whichever option you choose.

**Decision:** _open_

### 9.3 What does 'real-time' (cào realtime) need to mean for you, and when should tick streaming start?

- Real-time disclosure capture, measured by detection latency, in Phases 0-4; decide on tick streaming at a checkpoint after Phase 4
- Tick streaming (Alpaca IEX, SSI) from the start
- Daily batch only, never ticks

**Recommendation:** Disclosure capture with latency targets (median under 5 min for US, under 10 min for VN) first, and decide on ticks at the checkpoint after Phase 4. Free tick feeds are thin, and VN shares cannot be sold before the T+2 afternoon, so intraday single-stock predictions cannot be acted on.

**Decision (owner, 2026-10-20):** option 1. "Real-time" means fast disclosure capture first, measured by detection latency (p50 under 5 min US, under 10 min VN) in Phases 0-4. Tick streaming (Alpaca IEX, SSI FastConnect) is Phase 6, after the owner checkpoint following Phase 4.

### 9.4 Which machine runs the 24/7 VN disclosure capture? Timestamps it misses can never be recovered.

- The owner's Windows laptop with Task Scheduler (it may sleep, which leaves gaps)
- A spare PC that is always on
- A small VPS (cost ASSUMED at a few USD per month)

**Recommendation:** A spare always-on PC or a small VPS, with NTP time sync, a daily heartbeat message and a nightly off-machine backup. Use the laptop only if you accept gaps; gaps are logged and the affected events are marked pit_quality=late.

**Decision:** _open_

### 9.5 Will you open an SSI (or DNSE) brokerage account to get FastConnect market-data credentials?

- Open an SSI account now. Registration is in person or through an account executive, and fees and rate limits are unpublished.
- Defer until Phase 6 (intraday ticks)
- Never; use vnstock and the public portals only

**Recommendation:** Defer unless you already have an SSI account. Phases 0-4 do not need it. If you open one, register only for FC Data (ConsumerID/Secret), never FC Trading.

**Decision:** _open_

### 9.6 What is the budget?

- $0 plus the capture host through Phase 4
- Add Alpaca Algo Trader Plus ($99 per month, VERIFIED) for full SIP real-time data
- Add a vnstock sponsor tier or a paid VN data vendor
- Set a monthly LLM API budget for Phase 5

**Recommendation:** $0 plus the host through Phase 4. Pay for SIP only after a daily-horizon signal shows edge. Set a hard daily LLM cap at the Phase 5 gate. Check whether you have a CUDA GPU before keeping the finetune extra.

**Decision:** _open_

### 9.7 How broad should the research universe be, and should financial firms be included?

- Broad point-in-time universe ranked by ADV (about 500 US names, 150-250 VN names, recomputed monthly, delisted names kept), plus a live watchlist of 30 or fewer symbols per market
- VN30 and about 30 US large caps only
- Exclude banks, insurers and brokers entirely

**Recommendation:** The broad point-in-time universe plus a small live watchlist. 30+30 names cannot detect a realistic IC; detecting IC 0.03 needs about 4,400 effectively independent events. Keep financial firms in the event studies, but exclude them from the Altman/Beneish/Piotroski features.

**Decision:** _open_

### 9.8 Which alert channel?

- Private Telegram 1:1 bot to one allowlisted chat id
- Email digest
- Zalo
- No push alerts; local reports and a read-only API only

**Recommendation:** A private Telegram 1:1 bot with Vietnamese-first cards, a bilingual disclaimer and no buy/sell wording. No groups or channels until a legal review.

**Decision:** _open_

### 9.9 Which storage stack for Phases 0-5?

- Embedded: append-only raw JSONL/Parquet, SQLite (WAL) for operations, DuckDB for research; TimescaleDB only when ticks arrive
- TimescaleDB/Postgres in Docker from the start (the existing docker-compose.yml)

**Recommendation:** Embedded. It runs on Windows without Docker, and none of the docker-compose services is used today. Add TimescaleDB in Phase 6 if tick storage needs it.

**Decision:** _open_

### 9.10 How will the project be judged (for example, if it is a study or thesis project)?

- Prediction quality with honest evaluation (scoreboard, leakage tests, null results allowed)
- A systems-engineering showcase (RAG, streaming, bandit routing, SLM fine-tune)
- Both, with fixed weights

**Recommendation:** Prediction quality and honest evaluation first. The showcase components stay gated in Phases 5-7, so each one enters only if it improves a measured number. If a grader needs the showcase, schedule Phase 5 or 7 explicitly after Phase 4.

**Decision:** _open_

### 9.11 Which LLM provider (and models) for Phase 5 extraction?

- Cheapest hosted model with a JSON schema, a number-faithfulness validator, a cache and a daily cost cap; escalate one tier only when validation fails
- Local model only (privacy, no API cost; GPU needed)
- No LLM; rule-based extraction only

**Recommendation:** Decide at the Phase 5 gate. Start rule-based, then add the cheapest hosted model behind the validator. The model IDs currently hard-coded in api/server.py and router/linucb_bandit.py are unverified ('gemini-4-argon-pro' was not found), so choose models from the provider's live model list.

**Decision:** _open_

### 9.12 What language should docs, labels and alerts use?

- AGENTS.md and docs in English; alerts and the digest Vietnamese-first with a bilingual disclaimer
- Everything in Vietnamese
- Everything in English

**Recommendation:** English for AGENTS.md and docs, because every coding agent reads them. Alerts should be Vietnamese-first for you. Vietnamese labels and keywords stay in Vietnamese, with diacritics, after NFC normalization.

**Decision:** _open_

### 9.13 What should happen to the .agents/ configuration (another agent tool's rules, hooks and MCP servers)?

- Keep it, but rewrite its rules as pointers to AGENTS.md sections and keep the MCP config minimal and pinned
- Retire .agents/ and rely only on AGENTS.md plus the Claude Code harness
- Keep it as it is

**Recommendation:** Keep it, with its rules rewritten as pointers to AGENTS.md. Rule 3 currently requires BM25+dense+MaxSim in every *.py file, and that would pull agents back into RAG work. Remove the Alpaca 'trading' toolset and pin the community MCP servers to commit SHAs.

**Decision:** _open_
