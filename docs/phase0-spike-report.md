# Phase 0 verification spike report

Plan `261022-1147-phase0a-capture`. Facts only. Every line carries a label (VERIFIED with a URL, OBSERVED with a date, ASSUMED) and a date.

## 0. Purpose, labels, raw data

- **Purpose.** Phase 0 settles the load-bearing facts about the data sources before any capture code depends on them. Sections 1-3 come from phase 1 (vnstock probe, VN disclosure portals, design inputs for phase 2). Sections 4-7 are filled by phases 4-7.
- **Labels.** VERIFIED = read in a primary source (URL given). OBSERVED = seen in a real run on the stated date. ASSUMED = not checked yet.
- **Raw data never lives in the repo.**
  - vnstock probe output: `C:\finsec-probe\out\<date>\` under the separate Windows user `vnprobe` (D6 isolation).
  - Portal and spike output: `$FINSEC_DATA_ROOT/spike/<kind>/<UTC-date>/<run-id>/`. `finsec_alpharadar.data_root` refuses a root inside the clone unless git ignores it.
  - This report copies only counts, latencies, flags and error codes. It holds no data rows, no disclosure titles and no personal names.

## 1. vnstock (OBSERVED 2026-10-26 unless stated)

Runs:
- the owner ran `scripts/spike/vnstock_probe.py probe --symbols VN30` as `vnprobe` from 2026-10-26 17:45 to 17:48 UTC (run `2026-10-26-r2`);
- an earlier run on the same day stopped at import: Smart App Control blocked an unsigned pandas `.pyd` (Windows CodeIntegrity log). The rerun loaded the same bytes without a block.

| Item | Result |
|---|---|
| Distribution | vnstock Gen 5 installs only from the vendor index `https://vnstocks.com/api/packages`; the PyPI project has 0 files (OBSERVED 2026-10-22). Installed offline from a private wheel set with `--require-hashes --no-deps --no-index`. |
| Lock | `requirements/vnstock/win-py313.lock`, compiled by uv 0.12.5 (command in its header), sha256 `6ebe1df80dd0b37aca56541aeedebbdcb9d3a836d147f2b143474c3bb5207aa8`. vnstock 5.1.0 from the vendor index; numpy 2.5.4, pandas 3.1.0, python-dateutil 3.0.0, six 1.17.0, tzdata 2026.5 from PyPI. |
| Runtime | Python 3.13.17, Windows 11 26200, probe run with `python -I`. |
| Version | `vnstock.__version__` = 5.1.0. The vendor index already offers 5.2.0: a `UserWarning` fires on most calls; the lock stays on 5.1.0. |
| Package expiry | `vnstock status`: "5.1.0 usable until 17/10/2027 (Vietnam time)", 355-356 days left. |
| API key expiry | Not captured: the key-validity line of `vnstock doctor` is blank after the probe's token redaction. |
| Tier, device | Community tier. A device id is shown masked (`vnde…eecb`). Store under `~/.vnstock/` (9 entries after `register`). |
| Telemetry env | `DO_NOT_TRACK=1`, `VNSTOCK_TELEMETRY=0`, `VNSTOCK_DISABLE_AGENT_SETUP=1`, `VNSTOCK_DISABLE_GLOBAL_AGENT=1` set before import. |
| `news()` | Removed: `UnsupportedError`, code `VNSTOCK_FEATURE_REMOVED` ("no longer supported since 5.0.0a2"). The signature still exists and returns `NoReturn`. |
| `events()` source `vci` | Works for FPT, VNM, HPG (HOSE), PVS (HNX), ACV (UPCOM): 234, 298, 158, 125 and 27 rows, 0.29-0.75 s per call. 22 columns, including `id`, `event_code`, `public_date`, `record_date`, `exright_date`, `exercise_ratio`. `id` is a string, present in every row and unique within each symbol. All date columns are tz-aware `Asia/Ho_Chi_Minh`. |
| `events()` time of day | Only `public_date` and the two `display_date` columns carry a time of day, and only for a minority of rows. `public_date` has a time of day in 9/27, 56/234, 54/158, 26/125 and 106/298 rows; the other date columns are always at midnight. |
| `events()` source `kbs` | Broken for all 5 symbols: `ProviderError`, code `VNSTOCK_KBS_COMPANY_SHAPE`. |
| Exchanges of the event symbols | ASSUMED: `equity.list()` returned no symbol/exchange columns, so the HOSE/HNX/UPCOM split above is the probe's default and not checked. |
| `ohlcv()` unit | Thousands of VND (price magnitude 10^1) on both `vci` and `kbs`; not full VND. |
| `ohlcv()` adjustment | Adjusted. FPT stock issue 10% (ex-date 2026-09-21): close(T-1)/open(T) = 0.980 (vci) / 0.980 (kbs); a raw series would jump by about 1.1. FPT 2026-06-24, ratio 0.00135: 0.997. PVS stock issue ratio 0.2 (ex-date 2026-09-14): 1.107 on both sources, which fits neither a raw nor an adjusted series; UNRESOLVED. |
| Raw bars | None found. The `ohlcv` signature has no adjustment parameter (`start, end, interval, limit, source, count_back, length, floating`), and the probe found no raw-bar parameter candidate. **Phase 3 blocker**: roadmap §6.3 needs raw bars. |
| `trades()` | `trades(limit, last_id, side, source)`; `vci` returns `time, price, volume, match_type, id` (unique `id`), at most 1,000 trades per call, paged with `last_id`. |
| Daily depth | Community cap: only the last 2,920 calendar days. FPT from 2000-01-01 to 2026-10-23 returned 1,995 sessions, oldest 2018-10-25 on both sources (`vci` dropped 2,954 rows, `kbs` 524, per `df.attrs`), with a `UserWarning` that names the cap. |
| 1-minute depth | One call returned 20,000 candles, the oldest at 2026-06-18 11:00 ICT, for a 200-day request ending 2026-10-23. |
| Delisted symbols | BCG, KPF, PSH (delisted 2024-2026) still return `events` (148, 132, 38 rows) and daily bars up to their last session (2025-10-08, 2025-02-25, 2025-03-11). |
| Rate limit | 50 sequential `events()` calls over the VN30 seed at a 6, 3, 2, 1 and 0.5 s cadence (10 calls each). There was no throttle signal: no exception, warning or truncation note. Median latency was 0.29-0.32 s at every cadence (max 0.76 s). Quota use was below 1% of both the 24-hour and the 30-day quota, before and after. The 0.5 s step was a 10-call burst, not a sustained rate. |
| Exception catalogue | `UserWarning` (update available; Community history cap), `UnsupportedError`/`VNSTOCK_FEATURE_REMOVED`, `ProviderError`/`VNSTOCK_KBS_COMPANY_SHAPE`. Not tried: vendor auth unreachable (optional `hosts` test skipped). |
| Isolation (D6) | Hashes of the agent-instruction files, `.git/hooks` and `.venv` `.pth` files were taken before and after under the dev user: files digest `c5f5fcb0…` both times. Under `vnprobe`, the hashes before the probe, after import and after the probe all had files digest `fa134f99…`. `git diff --exit-code` returned 0. The only new files were in `~/.vnstock/` (from `register`). |

## 2. VN disclosure portals (OBSERVED 2026-10-26 and 2026-10-27)

Runs (`scripts/spike/portal_probe.py`, dev user):
- a smoke round at 2026-10-26 18:03 ICT;
- a history check at 2026-10-27 00:51 ICT;
- 12 rounds at a 300 s interval from 2026-10-27 10:48 to 11:46 ICT. This is the morning session plus the first 16 minutes of the lunch break; no round started late.

Politeness: at least 5 s between requests to one host, and a neutral User-Agent `FinSecAlphaRadar-research/0.1 (personal, non-commercial)`.

| Candidate | robots.txt | Format | Time of day | Id | Items per request | Latency, 12 rounds (headers p50 / total max) | Errors (12 rounds) |
|---|---|---|---|---|---|---|---|
| HOSE API listing, `api.hsx.vn/n/api/v1/1/news/securitiesType/0`, `pageSize=20`, today only | `api.hsx.vn`: unreachable (URLError); `www.hsx.vn`: 200, allows all | JSON | seconds (`postedDate`, pseudo-UTC epoch, see below) | `id` | 20 (`totalCount` 80 all 12 rounds) | 3.9 s / 14.8 s | 0 |
| HOSE API listing, yesterday + today | same | JSON | seconds | `id` | 20 (`totalCount` 320) | 3.9 s / 9.4 s | 1 URLError (round 3) |
| HOSE RSS, `api.hsx.vn/n/api/v1/News/NewsByCateFeed/21` | same | RSS 2.0 with Atom `updated` | seconds, explicit `+07:00` | `guid` (10/10 equal an API `id`) | 10 | 3.8 s / 9.8 s | 1 RemoteDisconnected (round 12) |
| HNX RSS feed 3, `hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss` | `hnx.vn`: 404 (none) | RSS 2.0 | seconds, explicit `+0700` | `guid` (100 unique of 100) | 100 | 46.0 s / 48.8 s | 0 |
| HNX POST listing, `hnx.vn/ModuleArticles/ArticlesCPEtfs/NextPageTinCPNY_CBTCPH` | same | HTML | date only | none | 0 in 12/12 (form values guessed; UNRESOLVED) | 40.3 s / 42.8 s | 0 |
| VSDC, `vsdc.vn/vi/tin-thi-truong-co-so` | `vsdc.vn`: 200, allows the page | HTML | seconds | yes | 15 | 0.58 s / 0.90 s | 0 |
| SSC, `congbothongtin.ssc.gov.vn` (robots + front page only) | 404 | HTML (stateful ADF) | date only (research) | n/a | n/a | 14.5 s (one GET) | 0 |

- **No WAF marker** on any candidate. Cookies: HOSE `TS01…` (load balancer), HNX a session cookie plus `language`, VSDC `__VPToken` plus `lang`.
- **Clock skew.** Measured at header arrival (local time when the headers arrived minus the `Date` header):
  - HOSE: -0.4 to 7.3 s;
  - HNX: -1.0 to 0.4 s;
  - VSDC: -0.6 to 0.7 s;
  - SSC: +354 to +369 s, so the SSC server clock is about 6 minutes off.

  The summary file's `skew_s` uses the midpoint of the request instead, so for HNX it shows -20 to -24 s. That is half of HNX's 40-48 s time to headers, not a clock offset.
- **HNX is slow during the session.** Time to headers was 45-49 s (RSS) and 40-41 s (POST) from 10:48 to 11:46 ICT, against 8.4 s and 1.0 s at 18:03 ICT the evening before. Every request completed within the 90 s timeout.
- **HOSE timestamps are pseudo-UTC epochs.** The newest `postedDate` decodes, read as UTC, to 17:39:02 ICT, 5-7 h after the fetch. The RSS `updated` for the same item is `10:39:02+07:00`, and `publishFrom` falls on a UTC midnight (1793059200). So `posted_at_utc = postedDate - 7 h`. This confirms research `vn-disclosure-portals.md` §1 (three earlier pieces of evidence).
- **HOSE volume.** `totalCount` for today stayed at 80 from 10:48 to 11:46 ICT: no new HOSE item in that hour, with the newest posted at 10:39 ICT. The full day of 2026-10-26 had 240 items by 18:03 ICT, and two days held 320. So HOSE detection latency was not measured in this window.
- **HNX detection, n = 3.** New items appeared in the first fetch after their `pubDate`. The upper bounds were 2 min 16 s (pubDate 11:18:04, headers 11:20:20), 2 min 55 s and 4 min 45 s. The previous fetch came before each `pubDate`, so these bounds measure the 5-minute cadence, not the portal.
- **Overlap between rounds:** HOSE 20/20 and 10/10, VSDC 15/15, HNX RSS 97-100/100. The 0 values for the two HOSE forms in rounds 3 and 12 are the failed requests, not a content change.
- **HOSE history** (`--history-check`, `pageSize=1`, one weekday in October of each year): HTTP 200 for every year 2018-2026. `totalCount` per day was 41 (2018), 51, 67, 83, 93, 111, 95, 124, and 129 (2026). The listing goes back past the ECM start of 2024-08-15. Whether the old items carry real posting times is not checked.
- **Probe fix (2026-10-27).** The time-precision regex used `\b` and read the ISO stamp `T10:39:02` as minute precision; the HOSE RSS row above is corrected by hand. `portal_probe.py` now uses digit lookarounds.

## 3. Design inputs for phase 2

| # | Input | Value | Basis |
|---|---|---|---|
| 1 | Safe vnstock calls/min | 60/min, from the plan formula of 50% of the fastest clean cadence (0.5 s = 120/min). A VN30 sweep needs 30 calls, so 30 s. | OBSERVED 2026-10-26, 10-call burst only; quota under 1%. |
| 2 | Seed universe × cadence | VN30 as of 2026-10-26, 30 symbols: ACB, BID, BSR, CTG, FPT, GAS, GVR, HDB, HPG, LPB, MBB, MCH, MSN, MWG, SAB, SHB, SSB, SSI, STB, TCB, TCX, VCB, VHM, VIB, VIC, VJC, VNM, VPB, VPL, VRE (`Reference().index.members('VN30')`). One `events(source='vci')` sweep per poll cycle. | OBSERVED 2026-10-26. The probe's ohlcv windows end at the fixed date 2026-10-23. |
| 3 | `source_id` rules | HOSE: the API `id` (= RSS `guid`; ids are not monotonic, research §1). HNX: the `guid`. vnstock: `vci:<id>`. The record's `source` names the portal, so portal ids carry no prefix (phase 2 R2.2; corrected 2026-10-27). | OBSERVED 2026-10-27; vnstock `id` is unique within a symbol (OBSERVED 2026-10-26), unique across symbols ASSUMED. |
| 4 | Portal URLs, `pageSize`, `max_pages` | Pending OB3 (owner). HOSE API: `pageSize=20`, `max_pages=2` (one page held a full hour of the session; 240 items/day, so ~2 per 5 min on average and up to 20 in a burst). HOSE: the two-day form on the first poll of an ICT day only (P2 R2.8). HNX RSS: one fixed 100-item page, no paging. | OBSERVED 2026-10-26/27; `max_pages=2` is a proposal. |
| 5 | Timeouts | Connect 30 s, read 90 s (HNX time to headers reached 48.7 s during the session). urllib has one socket timeout, so phase 2 ships HOSE 30 s / 60 s total and HNX 90 s / 150 s total. One retry after a transport error: 2 of 48 HOSE requests failed (URLError, RemoteDisconnected) and both following rounds succeeded. | OBSERVED 2026-10-27. |
| 6 | vnstock error → outage class | `VNSTOCK_FEATURE_REMOVED` → `source_removed` (config error, do not retry). `VNSTOCK_KBS_COMPANY_SHAPE` → `source_broken` (provider; never use `kbs` for events). `UserWarning` "update available" → log only. `UserWarning` history cap → `data_truncated`. Anything else → `source_unreachable` with the class name (the `vendor_auth_unreachable` test was skipped). Phase 2 maps onto the existing outage kinds: `VNSTOCK_FEATURE_REMOVED` → `vendor_unavailable`, `VNSTOCK_KBS_COMPANY_SHAPE` → `source_invalid_payload` (and `kbs` is refused on load), history cap → `vendor_quota_exceeded`. | OBSERVED 2026-10-26. |
| 7 | vnstock expiry | Package 5.1.0 usable until 2027-10-17 (Vietnam time). API key expiry not captured. | OBSERVED 2026-10-26 (`vnstock status`). |
| — | HOSE time conversion | `posted_at_utc = postedDate - 7 h`. Keep the original integer in the raw payload, and add a golden test for the converter. | OBSERVED 2026-10-22 (research) and 2026-10-27. |

## 4. EDGAR

Recorded on 2026-10-28 by `scripts/spike/record_edgar_headers.py` with a declared User-Agent (2 requests/s, 9 requests including one 404 and one
full-text search). Fixtures and manifest: `tests/fixtures/edgar_headers/`; golden test: `tests/test_edgar_time.py`. OBSERVED 2026-10-28.

| Accession | Form | Raw `<ACCEPTANCE-DATETIME>` | Information time (UTC) | Tag in `.hdr.sgml` | CIK folder |
|---|---|---|---|---|---|
| 0000320193-26-000018 | 8-K | `20260730163028` | 2026-07-30T20:30:28Z (EDT) | yes | 320193 |
| 0001140361-26-040127 | 4 | `20261019183255` | 2026-10-19T22:32:55Z (EDT) | yes | 320193 (issuer; agent folder 1140361 gave 404) |
| 0000320193-26-000020 | 10-Q | `20260731060102` | 2026-07-31T10:01:02Z (EDT) | yes | 320193 |
| 0000320193-26-000005 | 8-K | `20260129163033` | 2026-01-29T21:30:33Z (EST) | yes | 320193 |

- The `-index-headers.html` file carries the header twice (an HTML comment with raw tags and a `<PRE>` block with the escaped tag and the
  space dialect); both copies agree on all four.
- The submissions-JSON `acceptanceDateTime` read as UTC is 4 h (EDT) or 5 h (EST) later than the header time on all four (DERIVED from
  roadmap §2.1).
- The Form 4's issuer CIK came from EDGAR full-text search (`efts.sec.gov/LATEST/search-index`), which listed the reporting owner
  0002100523 and the issuer 0000320193. The owner's name and address are `REDACTED` in the committed fixtures (D8); the owner CIK stays.
- A User-Agent without a contact gets HTTP 403 "Undeclared Automated Tool" (OBSERVED 2026-10-22, research `edgar-header.md` §4).

## 5. Alpaca

Pending (phase 5).

## 6. VN reconcile, counts, go/no-go

Pending (phase 6).

## 7. Phase 0 exit

Pending (phase 7).
