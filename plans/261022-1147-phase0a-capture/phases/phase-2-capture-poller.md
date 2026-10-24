---
phase: 2
title: "Capture Poller"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 2 — Capture Poller

## Overview
Xây `src/finsec_alpharadar/capture/` — poller VN chỉ-thu-thập (roadmap §7 0b.6, `docs/project-roadmap.md:394-398`): hỏi các cổng owner chọn ở
Phase 1 (dự kiến HOSE API listing + HNX RSS feed 3) và vnstock `events()` (nguồn phụ, D2), ghi payload thô append-only theo **schema bản ghi
thô v1**, `first_seen_at` UTC, outage log, `pit_quality = late` sau mỗi khoảng trống, heartbeat JSON vào thư mục cloud (D3), backup đêm có
manifest sha256, và lệnh `audit` làm bằng chứng exit. TDD từng module, clock/fetcher/sleeper tiêm vào; không test nào đọc đồng hồ, ngủ hay
gọi mạng. Thêm `tzdata` vào dependency lõi (phase đầu tiên cần nó). Phụ thuộc Phase 1 (guard `data_root`, quyết định cổng OB3, bảng design
inputs) và cạnh liên plan `261021-1314-phase0b-foundations/P5`.

**"Chỉ thu thập" nghĩa là:** capture không phân loại, không tính `information_time` thật. Nó chỉ trích **id** để khử trùng (HOSE `id` trong
JSON, HNX `<guid>` trong RSS, vnstock cột `id`) — đó là phần parse duy nhất, cố ý. Phép đổi HOSE `postedDate − 25200` (epoch "giả UTC",
research `vn-disclosure-portals.md` §1) và `information_time = min(giờ đăng cổng, first_seen_at)` (roadmap §2.1, `docs/project-roadmap.md:55-57`)
là việc của parser Phase 1/3; v1 ghi `information_time` tạm = `first_seen_at`, nói rõ bằng `information_time_basis`.

## Requirements
**R2.0 Đầu vào từ Phase 1 (kiểm trước khi viết code).** Đọc AGENTS.md "Decisions" (dòng chọn cổng, OB3) và `docs/phase0-spike-report.md` mục 3.
Nếu owner chọn khác "HOSE API listing + HNX RSS feed 3" → DỪNG, trả về main để sửa plan (bộ adapter đổi). Mọi số phụ thuộc đo đạc
(calls/min, seed, `symbols_per_poll`, page size, timeout, hạn vnstock, bảng lỗi vnstock) nằm trong `configs/capture.toml`, không trong code;
giá trị mặc định ghi `[ASSUMED]` và bị bảng design inputs thay.

**R2.1 Bố cục module** (mọi module `from __future__ import annotations`, type hints đầy đủ, record `@dataclass(frozen=True)` — code-standards
`docs/code-standards.md:11-13`; `logging.getLogger(__name__)`, chỉ entry point cấu hình handler — `docs/code-standards.md:79`):

| Module | Trách nhiệm (một) |
|---|---|
| `capture/__init__.py` | re-export tên public (`__all__`), không import gì nặng |
| `capture/record.py` | schema bản ghi thô v1: `CaptureRecord`, `RecordType`, `PitQuality`, `OutageKind`, mã hoá/giải mã dòng JSONL, `canonical_item_bytes`, `format_utc`/`parse_utc`, `capture_code_digest` |
| `capture/config.py` | đọc `configs/capture.toml` (`tomllib`) + `FINSEC_DATA_ROOT` và `FINSEC_BACKUP_DIR` — CẢ HAI qua `data_root.resolve_external_dir` (red-team r1 LT7) → `CaptureConfig` đã kiểm |
| `capture/schedule.py` | ngày giao dịch (thứ 2-6 trừ danh sách nghỉ trong config), cadence theo nguồn, `next_due`, `gap_deadline` |
| `capture/sources.py` | I/O mạng: `UrllibFetcher`; adapter `HoseNewsApiSource`, `RssFeedSource`, `VnstockEventsSource`; trích id; phân loại lỗi → `OutageKind` |
| `capture/storage.py` | `RawArchive` (JSONL append, xoay theo ngày UTC, fsync mỗi chu kỳ, sửa đuôi rách), `BlobStore`, `CaptureState` (SQLite WAL) |
| `capture/poller.py` | một chu kỳ poll, quy tắc `pit_quality`, mở/đóng outage, phát hiện process down / stall / clock step, token bucket, watchdog, vòng `run_forever` |
| `capture/heartbeat.py` | dựng + ghi atomic heartbeat JSON (D3) |
| `capture/backup.py` | backup đêm: tar.gz ngày đã đóng + snapshot SQLite + manifest sha256, kiểm lại, không bao giờ xoá |
| `capture/audit.py` | bằng chứng exit: khoảng trống chưa ghi, heartbeat mỗi ngày, độ lệch đồng hồ, chuỗi ngày giao dịch liên tiếp đạt |
| `capture/host.py` | tích hợp máy: `SystemClock`, `system_sleep`, `KeepAwake` (SetThreadExecutionState), `SingleInstanceLock`, `run_command` — module DUY NHẤT được đọc đồng hồ/ngủ |
| `capture/__main__.py` | CLI `python -m finsec_alpharadar.capture …` |

Capture chỉ import stdlib, `finsec_alpharadar.data_root` và chính `capture.*`; `vnstock` (và pandas qua nó) chỉ import **bên trong hàm** của
`VnstockEventsSource`. Lý do: venv trên laptop là env khoá hash của Phase 1 (vnstock, pandas, numpy, tzdata — R1.3), không `pip install -e .`;
capture chạy từ clone qua `PYTHONPATH=<clone>\src` (Phase 3). `finsec_alpharadar/__init__.py` chỉ có docstring + `__version__` (OBSERVED
`c9168f0`), nên import package không kéo numpy.

**R2.2 Schema bản ghi thô v1 — hợp đồng mà `store/` Phase 1 và parser Phase 3 đọc.** Một object JSON mỗi dòng, UTF-8, giữ nguyên dấu tiếng Việt
như nhận được (không chuẩn hoá payload), `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` + `\n`. Mọi thời điểm
là chuỗi UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

| Trường | Kiểu | Nghĩa |
|---|---|---|
| `schema_version` | int | `1` |
| `record_type` | `"item"` \| `"snapshot"` | item = một công bố/hàng; snapshot = nguyên body một response |
| `source` | str | tên nguồn trong config (`hose_news_api`, `hnx_rss_issuers`, `vnstock_events`) |
| `source_id` | str | id gốc (HOSE `"2506861"`, HNX guid `"638933"`, vnstock `"vci:<id>"`); snapshot: `"sha256:<raw_sha256>"` |
| `information_time` | str UTC | v1: luôn = `first_seen_at` (tạm, parser Phase 1/3 tính lại) |
| `information_time_basis` | `"first_seen"` | v1 chỉ có giá trị này |
| `first_seen_at` | str UTC | lúc nhận response chứa bản ghi (đồng hồ tiêm vào) |
| `ingested_at` | str UTC | lúc ghi vào archive; ≥ `first_seen_at` |
| `pit_quality` | `"first_seen"` \| `"late"` | xem R2.6 |
| `raw_sha256` | 64 hex thường | item: sha256 của `canonical_item_bytes(payload)`; snapshot: sha256 của body đúng như nhận (sau giải nén) |
| `payload_format` | `"json"` \| `"text"` \| `"blob"` | item HOSE/vnstock = json; item RSS = text (đoạn XML `<item>` nguyên văn); snapshot = blob |
| `payload` | JSON \| str \| null | item: giá trị nguyên văn; snapshot: null |
| `blob_path` | str \| null | snapshot: `blobs/<YYYY-MM-DD>/<sha256>.bin` tương đối với data root |
| `byte_count` | int | số byte của item/body thô |
| `snapshot_sha256` | str \| null | item: body chứa nó (null với vnstock) |
| `request` | object | không bí mật: HTTP `{method, url, params, header_names}`; vnstock `{call, args, library_version}` |
| `response` | object | HTTP `{status, http_date, rtt_ms, content_type}`; vnstock `{rows, attrs}` |
| `cycle_id` | str | `<started_at %Y%m%dT%H%M%S%fZ>-<seq>` |
| `capture_version` | str | `<__version__>+<12 hex của capture_code_digest>`; digest trên các `capture/*.py` đã đổi `\r\n`→`\n` (Windows autocrlf) |

`CaptureRecord.__post_init__` raise `CaptureRecordError` khi: datetime naive hoặc offset ≠ 0; `information_time != first_seen_at` hoặc basis khác
`first_seen`; `ingested_at < first_seen_at`; `raw_sha256` sai dạng; `source`/`source_id` rỗng; item có `blob_path` hoặc thiếu `payload`;
snapshot có `payload` hoặc thiếu `blob_path`; `schema_version != 1`. `from_json_line` là nghịch đảo đúng của `to_json_line`.

**Luật đọc của schema v1 (red-team r1 RT12, phán quyết của main):** với cùng một khoá `(source, source_id, raw_sha256)`, bản ghi có `first_seen_at` SỚM NHẤT
thắng; mọi bản trùng về sau (nếu một crash từng lọt qua) bị parser bỏ qua. Câu này chép nguyên vào mục schema của `docs/system-architecture.md`.

**R2.3 Bố cục trên đĩa** (data root ngoài clone, R1.1; không đặt DB/JSONL sống trong thư mục sync — research `capture-host-windows.md` §7.3-7.4):
```
$FINSEC_DATA_ROOT/raw/<source>/<YYYY-MM-DD>.jsonl      ngày UTC của ingested_at; mở "ab"; flush + fsync MỖI file đã chạm, mỗi chu kỳ
$FINSEC_DATA_ROOT/blobs/<YYYY-MM-DD>/<sha256>.bin      tạm → flush + fsync → os.replace; có sẵn thì kiểm sha trước khi bỏ qua
$FINSEC_DATA_ROOT/state/capture.sqlite                 WAL, synchronous=FULL
$FINSEC_DATA_ROOT/state/capture.lock                   khoá một tiến trình
$FINSEC_DATA_ROOT/ops/capture.log                      RotatingFileHandler 5 MB x 10; chỉ id, số đếm, thời gian
$FINSEC_BACKUP_DIR/heartbeat.json                      bản mới nhất (ghi đè atomic)
$FINSEC_BACKUP_DIR/heartbeats/heartbeat-<YYYY-MM-DD>.json   bản cuối của mỗi ngày UTC
$FINSEC_BACKUP_DIR/daily/<YYYY-MM-DD>/{capture-<d>.tar.gz, state-<d>.sqlite, manifest-<d>.json}
```
Khi khởi động: dòng cuối của file JSONL hôm nay thiếu `\n` hoặc không parse được → cắt về dòng tốt cuối, ghi số byte đã cắt vào chi tiết outage
`process_down` (mất điện chỉ làm hỏng phần đuôi — `[PRIOR]`, research §7.2).
- **Bền trước khi commit (red-team r1 RT12, làm CẢ HAI theo phán quyết của main):** (a) cuối mỗi chu kỳ, mọi blob mới và MỌI file JSONL đã chạm được
  flush + `os.fsync` TRƯỚC `commit` của state; `BlobStore.put` gặp file đã có thì đọc lại và so sha256 — lệch (file hỏng sau mất điện) → ghi lại qua tạm +
  `os.replace` và ghi chi tiết vào log; (b) `startup()` đối soát đuôi archive vào `seen`: đọc các dòng JSONL có `ingested_at` sau mốc commit cuối
  (`meta.last_commit_at`; thiếu thì quét hai ngày UTC gần nhất) và `INSERT OR IGNORE` khoá `(source, source_id, raw_sha256)` + `first_seen_at` của
  chúng, cộng khôi phục sha snapshot cuối mỗi nguồn vào `kv` — nên crash giữa fsync và commit không thể sinh bản ghi thứ hai.

**R2.4 State SQLite** (`PRAGMA journal_mode=WAL` phải trả `wal`; `PRAGMA synchronous=FULL` — research §7.3, VERIFIED https://sqlite.org/wal.html):
`meta(key, value)` (có `schema_version=1`); `seen(source, source_id, raw_sha256, first_seen_at, PRIMARY KEY(source, source_id, raw_sha256))` —
payload đổi của một `source_id` cũ là một item mới (giữ mọi phiên bản, AGENTS.md:143); `polls(id, cycle_id, source, started_at, finished_at,
status 'ok'|'error', outage_kind, detail, http_status, http_date, rtt_ms, skew_ms, items_total, items_new, items_revised, pages, run_mode 'run'|'once',
pid)` — `run_mode`/`pid` để cổng R3.3 chỉ đếm poll do task chạy (red-team r1 LT3);`detail` (ở `polls` và `outages`) chỉ chứa tên lớp lỗi, `code`, sha256
và độ dài của message vendor, không bao giờ text nguyên văn (red-team r1 LT8);
`outages(id, kind, source NULL=toàn tiến trình, start_at, end_at NULL=đang mở, detected_at, detail)`; `blobs(sha256, rel_path, first_written_at,
byte_count)`; `alive(id=1, last_alive_at, last_alive_mono, pid)`; `heartbeats(written_at, status, rel_path, sha256)`; `backups(day, manifest_sha256,
verified_at)`; `kv(key, value)` (con trỏ vòng vnstock, sha snapshot cuối mỗi nguồn, baseline canary).

**R2.5 Lịch (D5 — khoá) — `schedule.py`.** Múi giờ `ZoneInfo("Asia/Ho_Chi_Minh")` (thiết kế của main: tzdata lõi), tạo lười trong hàm; thiếu
tzdata → `TimeZoneDataMissing("install tzdata: .venv/Scripts/python -m pip install -e .[dev]")`.
- `is_trading_day(d)` = thứ 2-6 và không nằm trong `[[holidays]]` của config (đọc thông báo, không suy từ Bộ luật Lao động — research
  `vn-calendar-sessions.md` §2.3). Ngày nghỉ còn lại năm 2026: thứ Ba 2026-11-24 (VSDC không thanh toán, VERIFIED https://vsdc.vn/en/ad/198399;
  thông báo nghỉ của HOSE/HNX chưa thấy → `[ASSUMED]`); 2027 là nháp (`status = "provisional"`).
- `interval_at(t, source)` = `active_interval_s` nếu ngày giao dịch và `active_start ≤ giờ địa phương < active_end` (08:00-17:30), ngược lại
  `idle_interval_s` (3600). Config phải có `120 ≤ active_interval_s ≤ 300` và `idle_interval_s == 3600` cho MỌI nguồn (D5; test của config đã ship).
- `next_due(prev)` = `min(prev + interval_at(prev), mốc kế tiếp sau prev)`, mốc = 08:00, 17:30 và 00:00 địa phương mỗi ngày → lúc 08:00 poll ngay.
- `gap_deadline(prev)` = `next_due(next_due(prev)) + max_poll_duration_s` — hạn chót cho poll thành công kế tiếp: bước lịch HAI lần từ poll thành
  công trước (cho phép lỡ đúng một poll, như "2× chu kỳ" ở trạng thái ổn định) rồi cộng thời lượng poll tối đa. Bước theo lịch chứ không nhân đôi khoảng
  đang áp dụng, để ranh giới chặt ở cả hai chiều (thiết kế của planner sau góp ý advisor): poll cuối 07:00 → `next_due` 08:00 → 08:03 → hạn 08:05, nên
  một crash 07:30-08:50 là khoảng trống phải được phủ, không lọt qua "2 × 60 phút". Hệ quả: chuyển từ hằng giờ sang 3 phút lúc 08:00 không phải khoảng
  trống; một ngày giao dịch bị đánh dấu nhầm là ngày nghỉ chỉ làm cadence thưa (hằng giờ) theo đúng config, không phải outage.

**R2.6 Quy tắc `pit_quality`.** Một item mới (kể cả bản sửa) của nguồn S trong một poll thành công lúc t là `late` khi và chỉ khi:
S chưa từng có poll thành công; hoặc `t > gap_deadline(last_ok(S))`; hoặc có outage (của S hoặc toàn tiến trình, mọi loại)
giao với `(last_ok(S), t]`. Ngược lại `first_seen`. Lần chạy đầu tiên → mọi item đều `late` (ta chưa canh). Item sau đó `first_seen`.

**R2.7 Outage (`OutageKind`, `str, Enum`).** `process_down` (lúc khởi động: `now − alive.last_alive_at > alive_threshold`, với `alive_threshold
= 2 × loop_tick_s + max_poll_duration_s`; outage = `[last_alive_at, now]`). `max_poll_duration_s` là ngân sách của MỘT poll của một nguồn (gồm retry và
lật trang; hết ngân sách → poll dừng với `source_unreachable`), và `tick()` chạy giữa từng nguồn trong `run_cycle`, nên một buổi sáng bận nhiều nguồn
không sinh `host_stalled` giả (red-team r1 LT1). `host_stalled` (giữa hai tick, Δmonotonic > alive_threshold; outage
= `[tick trước, now]`); `clock_step` (`|Δwall − Δmono| > clock_step_threshold_s` (2 s), outage = `[tick trước, now]`, detail = delta);
`source_unreachable` (lỗi mạng/timeout/5xx sau `retries`); `source_rejected` (401/403/429 — WAF, rate limit); `source_invalid_payload` (không
trích được id, `success != true`, body > `max_body_bytes`); `coverage_overflow` (hết `max_pages` mà không gặp id đã thấy; RSS không chồng lấn);
`vendor_auth_unreachable`, `vendor_release_expired`, `vendor_quota_exceeded`, `vendor_unavailable` (vnstock không import được) — ánh xạ từ bảng
lỗi của Phase 1 (R1.5 bước 10); lỗi chưa biết → `source_unreachable` kèm tên lớp. Một nguồn lỗi mở MỘT outage (không mở lại mỗi lần retry hay
mỗi chu kỳ), đóng ở poll thành công kế tiếp (`end_at` = `first_seen_at` của poll đó). Outage còn mở từ lần chạy bị crash → đóng lúc khởi động
(`detail="closed_at_restart"`). Lỗi một nguồn không dừng nguồn khác (vnstock chết thì cổng vẫn chạy — D2).

**R2.8 Nguồn — `sources.py`** (fetcher là `Callable[[HttpRequest], HttpResponse]`, tiêm vào; `HttpResponse(status, headers, body, received_at,
rtt_ms)` với `received_at` từ đồng hồ tiêm vào):
- `UrllibFetcher(clock, user_agent, timeout_s, deadline_s, opener=None)`: `HTTPError` → `HttpResponse` với status; `URLError`/timeout →
  `SourceError(source_unreachable)`; User-Agent trung tính, không PII. **Deadline tổng mỗi request** (red-team r1 RT2): socket `timeout_s` chỉ giới hạn
  MỘT lần đọc (OBSERVED red-team r1: timeout 1 s mà đọc nhỏ giọt 12 byte mất 6.06 s, không `TimeoutError`), nên body được đọc theo chunk và sau mỗi chunk
  so `clock.monotonic()` với `deadline_s` → quá hạn = `source_unreachable` (detail `deadline`). **Giới hạn cỡ** (red-team r1 LT9): đọc tối đa
  `max_body_bytes + 1` byte thô; gzip giải bằng `zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, max_body_bytes + 1)` — vượt (kể cả còn
  `unconsumed_tail`) → `source_invalid_payload`; không bao giờ giải nén trọn vào RAM rồi mới so.
- `HoseNewsApiSource`: URL `…/securitiesType/0?pageIndex=k&pageSize=<cfg>&startDate=<D0>&endDate=<hôm nay ICT>`, trong đó `D0` = hôm qua ICT CHỈ ở
  poll đầu tiên của một ngày ICT (poll ok cuối của nguồn nằm ở ngày ICT trước, hoặc chưa có) — để không sót item đăng giữa poll cuối ngày và nửa đêm —
  còn lại `D0` = hôm nay, đúng khuyến nghị `startDate=endDate=hôm nay` của research (`research/vn-disclosure-portals.md` §1; red-team r1 RT13; độ trễ cả hai
  dạng đo ở P1 R1.6); header `Origin`/`Referer` từ config (`[ASSUMED]` cần thiết, research §1). Body phải là
  `{"success": true, "data": {"list": [...]}}`, mỗi item có `id` nguyên → item payload = dict nguyên văn. Lật trang khi MỌI id trên trang đều mới và
  `k < max_pages`, cách nhau ≥ `min_request_spacing_s` (sleeper tiêm); hết trang mà vẫn toàn mới → `coverage_overflow`. Id không đơn điệu → khử trùng
  theo tập id, không theo watermark (research §1).
- `RssFeedSource` (HNX feed 3): không dùng XML parser (không mở rộng entity); tách `<item>…</item>` và `<guid…>…</guid>` bằng regex trên body UTF-8
  có giới hạn cỡ; item payload = đoạn `<item>` nguyên văn (text). Không theo `<link>` cổng nội bộ `:7978` (research §2). Thiếu guid → `source_invalid_payload`.
  Lỗ trong dãy guid KHÔNG phải tín hiệu sót (research §2); chỉ "không chồng lấn với tập đã thấy" mới là `coverage_overflow`.
- `VnstockEventsSource(cfg, call_events=None, limiter)`: trước khi import, `os.environ.setdefault` cho `DO_NOT_TRACK=1`, `VNSTOCK_TELEMETRY=0`,
  `VNSTOCK_DISABLE_AGENT_SETUP=1`, `VNSTOCK_DISABLE_GLOBAL_AGENT=1`; `call_events` mặc định import lười `from vnstock import Reference` (đường
  Gen 5 do Phase 1 xác nhận) và trả `(rows, attrs)` qua `df.to_json(orient="records", date_format="iso", force_ascii=False)`; quét vòng `symbols`
  từ con trỏ trong `kv`, tối đa `symbols_per_poll` mã và theo token bucket `calls_per_min` (`[ASSUMED]` 10 = 50% của 20/min tới khi đo — roadmap §5.3);
  `source_id = "<vnstock_source>:<id>"`. Lỗi mã-không-tồn-tại của một mã → ghi vào `polls.detail`, không phải outage; lỗi tầng vendor → outage.
  - **Worker có timeout** (red-team r1 RT2): mỗi lệnh vnstock chạy trong MỘT worker thread qua `run_with_timeout(fn, timeout_s)` (tiêm vào; mặc định
    `concurrent.futures.ThreadPoolExecutor(max_workers=1)` + `future.result(timeout)`). Quá hạn → `source_unreachable` (detail `vendor_call_timeout`);
    thread bị treo không giết được nên được đánh dấu "bận": chừng nào nó còn sống, nguồn vnstock không gọi thêm (outage giữ nguyên mở), không mở worker thứ hai.
  - **Quota không raise** (red-team r1 RT7): vendor trả frame bị cắt + `UserWarning` + lý do trong `df.attrs` (research `vnstock-gen5.md` §1). Lệnh chạy trong
    `warnings.catch_warnings(record=True)` (trong worker; chỉ có một worker nên bộ lọc warnings toàn tiến trình không bị tranh chấp); có cảnh báo hoặc
    `attrs` có ghi chú cắt/hạn mức → hàng đã nhận vẫn được ghi (là dữ liệu thật), poll là `error` với outage `vendor_quota_exceeded`, và con trỏ vòng KHÔNG
    tiến để mã đó được hỏi lại.
- Không ghi bí mật vào `request` (chỉ tên header, không giá trị cookie).

**R2.9 Poller — `poller.py`.** `startup()`: sửa đuôi JSONL; đối soát đuôi archive vào `seen` (R2.3); phát hiện `process_down`; đóng outage mồ côi.
`tick(now, mono)`: ghi `alive`; báo tiến độ cho watchdog; kiểm stall/clock step.
`run_cycle(now)`: mỗi nguồn bật mà `next_due(last_attempt) ≤ now` (hoặc chưa từng poll, hoặc `--all`) → poll có retry (`retries`, backoff 5 s/15 s qua sleeper)
trong ngân sách `max_poll_duration_s` → ghi snapshot khi sha body khác snapshot cuối của nguồn → ghi item mới/sửa theo R2.6 → `polls` + outage → `tick()`
rồi sang nguồn kế (LT1) → cuối chu kỳ fsync mọi blob mới và mọi JSONL đã chạm, RỒI mới `commit` state (R2.3, RT12). `skew_ms` mỗi poll =
(giữa local gửi/nhận) − `Date` header (độ phân giải 1 s, RFC 9110 §6.6.1). `run_forever(stop)`: tick ≤ `loop_tick_s` (30 s), heartbeat mỗi
`heartbeat_every_s` (900 s), backup ngày UTC vừa đóng lúc `backup_at_local` (07:15 ICT = sau 00:00 UTC), ngủ tới hạn kế tiếp theo từng đoạn ≤ 30 s.
**Watchdog trên daemon thread riêng** (red-team r1 RT2, phán quyết của main): `Watchdog(clock, limit_s, exit_fn, sleep_fn)` chạy trên một
`threading.Thread(daemon=True)` của riêng nó, tự đọc `clock.monotonic()` và so với mốc tiến độ cuối mà `tick()` ghi; quá `watchdog_limit_s` (900 s) →
`exit_fn(70)` (mặc định `os._exit`) để Task Scheduler khởi động lại (Phase 3) — kể cả khi vòng chính đang kẹt trong một request hay một lệnh vnstock.
`check_once()` thuần để test; vòng của thread dùng `sleep_fn` tiêm vào. Lỗi của heartbeat/backup bị bắt riêng trong `run_forever` (R2.10, R2.11) —
không component phụ nào được làm chết tiến trình capture. `TokenBucket(rate_per_min, capacity, clock)` thuần.

**R2.10 Heartbeat — `heartbeat.py` (D3).** `build_heartbeat(state, config, now, probes) -> dict` (thuần trên truy vấn state + kết quả probe), khoá
cố định: `schema` `"capture-heartbeat/1"`, `generated_at`, `status` `OK|DEGRADED|DOWN` + `reasons`, mỗi nguồn (`last_ok_at`, `age_s`, `polls_24h_ok`,
`polls_24h_error`, `expected_polls_24h` theo lịch, `new_items_24h`, `revised_items_24h`, outage đang mở), `outages_24h` theo loại, đồng hồ (`skew_ms`
trung vị/max 24 h; offset `w32tm /stripchart /computer:<host> /dataonly /samples:3` mỗi giờ; các trường `w32tm /query /status` — chạy qua
`run_command` tiêm vào, lỗi → `null` + chuỗi lỗi; dấu của offset stripchart `[ASSUMED]`, research §3.3), đĩa trống, cỡ data root, backup cuối
(ngày, đã kiểm), vnstock (`version`, `release_expires_on`, `days_left` từ config), canary (sha256 `AGENTS.md`/`CLAUDE.md` của clone so với baseline
chụp lúc `run` khởi động và lưu trong `kv` kèm thời điểm; `canary-reset` chụp lại baseline mà không cần khởi động lại — red-team r1 LT4, phán quyết của
main), Windows Update (`wu_pause_expiry`: chuỗi thô của `reg query HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings /v PauseUpdatesExpiryTime` qua
`run_command`, tên khoá `[ASSUMED]`, không có luật status — để owner thấy hạn pause trên điện thoại trong cửa sổ, red-team r1 LT2), tiến trình (pid,
uptime, `capture_version`, phiên bản Python). Không tiêu đề, không payload, không bí mật, không
username. `DOWN` khi một nguồn cổng bật đã quá `gap_deadline` của poll thành công cuối; `DEGRADED` khi có outage mở, |skew| trung vị > 2 s,
canary đổi, `days_left ≤ 30`, đĩa < 10 GB, backup cũ hơn 36 h, hoặc lần ghi heartbeat/backup trước thất bại; ngược lại `OK`. `write_heartbeat` ghi
atomic (tạm + `os.replace`) cả `heartbeat.json` và `heartbeats/heartbeat-<ngày UTC>.json`. **Thư mục sync có thể khoá file** (red-team r1 RT11:
`os.replace` lên một file đang mở handle → `PermissionError [WinError 5]`, OBSERVED red-team r1): `os.replace` qua `replacer` tiêm vào, retry tối đa 3
lần cách 2 s qua sleeper tiêm vào; vẫn lỗi → bắt `OSError`, không raise, lần heartbeat sau mang DEGRADED `heartbeat_write_failed`; dòng `heartbeats` chỉ
được ghi SAU khi replace thành công.

**R2.11 Backup — `backup.py` (D3).** `run_backup(root, backup_dir, state, day, *, now)` — `now` (UTC tz-aware) đến từ đồng hồ tiêm vào, không bao giờ
`date.today()`; chỉ cho ngày UTC đã đóng (`day < now.date()` theo UTC, ngược lại raise) — `backup --day D-1` chạy tay lúc 03:00 ICT (= 20:00 UTC của
D-1, ngày còn mở) bị từ chối (red-team r1 RT14).
`capture-<d>.tar.gz` gồm `raw/*/<d>.jsonl` + `blobs/<d>/*`; `state-<d>.sqlite` bằng `sqlite3.Connection.backup()` (không copy file DB đang mở —
VERIFIED https://sqlite.org/howtocorrupt.html §1.2) rồi chạy `PRAGMA journal_mode=DELETE` trên chính file đích và đóng nó TRƯỚC khi tính sha — snapshot
không còn ở chế độ WAL (byte header 18/19 = 1/1), nên đọc nó không sinh `-wal`/`-shm` trong thư mục sync (red-team r1 RT15); `manifest-<d>.json` (mỗi
file: tên, byte, sha256; mỗi input: sha256 + số dòng). Ghi tên tạm rồi `os.replace` (cùng `replacer` + retry của R2.10; lỗi cuối cùng → `OSError` bị bắt,
DEGRADED `backup_write_failed`, thử lại ở lần lặp sau — red-team r1 RT11); đọc lại và so sha với manifest. Đã có và kiểm đúng → bỏ qua (idempotent);
đã có mà sai → ghi bản `-retry-<n>` và heartbeat DEGRADED. Không bao giờ xoá hay ghi đè file nguồn lẫn backup cũ. `%USERPROFILE%\.vnstock` không bao giờ
nằm dưới data root.
- **Đọc backup an toàn — `iter_backup_members(archive_path, manifest_path)`** (red-team r1 RT9; đặt ở đây vì Phase 6 không được chạm `capture/` khi
  đóng băng): kiểm sha256 của tar với `manifest-<d>.json` TRƯỚC khi mở; duyệt member bằng `TarFile.extractfile`, chỉ nhận file thường có tên khớp whitelist
  `raw/<source>/<d>.jsonl` hoặc `blobs/<d>/<64 hex>.bin`; member khác (đường dẫn tuyệt đối, `..`, symlink, hardlink, thiết bị) → raise; không bao giờ
  `extractall` (Python 3.13.14 `tarfile.TarFile.extraction_filter` → `None` — OBSERVED main 2026-10-22; red-team r1 tái hiện `../escaped.txt` thoát ra ngoài).

**R2.12 Audit — `audit.py` (bằng chứng exit E4).** `audit(state_path, config, date_from, date_to, days="trading"|"all") -> AuditReport`. Đầu vào
là SNAPSHOT (`state-<d>.sqlite` của backup): mở bằng URI `file:<path>?mode=ro&immutable=1` nên không tạo file nào cạnh snapshot và đọc được trong thư
mục chỉ đọc (OBSERVED red-team r1: chỉ `immutable=1` mở được trong thư mục bị deny ghi); file có header WAL (byte 18/19 = 2) → từ chối với thông điệp "pass
a snapshot from `backup`" (red-team r1 RT15). Cặp ảo ở HAI MÉP (red-team r1 RT1): mép đầu `p` = poll ok cuối trước `date_from` của nguồn, hoặc 00:00 địa
phương của `date_from` nếu không có; mép cuối `n` = `alive.last_alive_at` của snapshot (bị chặn bởi 24:00 của `date_to`) — nên một nguồn im lặng sau poll
ok cuối, hay chưa từng có poll ok, vẫn sinh khoảng trống phải được phủ. Với mỗi nguồn bật, mỗi cặp (thật hoặc ảo) `(p, n)` có `n > gap_deadline(p)`:
khoảng `[gap_deadline(p), n − max_poll_duration_s]` phải nằm trọn trong HỢP các outage của nguồn đó hoặc toàn tiến trình (end NULL = còn mở =
phủ tới hiện tại); phần không phủ = khoảng trống chưa ghi (unlogged gap), gán cho các ngày địa phương nó giao. Một ngày ĐẠT khi: không có khoảng
trống chưa ghi; MỖI nguồn cổng đang bật có ≥ 1 poll `ok` trong 08:00-17:30 địa phương của ngày giao dịch đó (outage đã ghi suốt ngày không thay được bằng
chứng thu thật — RT1); có ≥ 1 heartbeat ghi trong ngày; trung vị |`skew_ms`| ≤ 2000 và không poll nào |skew| > 60000 (không có `Date` nào → không đạt,
lý do `no_skew_evidence`). Báo cáo: theo ngày (verdict + lý do + số poll/kỳ vọng + item mới + outage theo loại) và
`longest_consecutive_passing_trading_days`. CLI exit 0 khi chuỗi đó ≥ `--require-days` (mặc định 5).

**R2.13 Host — `host.py`.** `SystemClock` (`datetime.now(timezone.utc)`, `time.monotonic()`), `system_sleep`; `KeepAwake(setter=None, platform=sys.platform)`:
trên `win32` đặt `restype`/`argtypes` rồi gọi `SetThreadExecutionState(0x80000001)` (ES_CONTINUOUS|ES_SYSTEM_REQUIRED) khi vào và `0x80000000` khi ra;
nền khác → no-op. OBSERVED 2026-10-22 (planner, máy dev, `python -I` trong scratchpad): lệnh đặt trả trạng thái trước `0x80000000`, lệnh trả về trả
`0x80000001`. `SingleInstanceLock(path)`: `msvcrt.locking(LK_NBLCK, 1)` trên Windows / `fcntl.flock(LOCK_EX|LOCK_NB)` nơi khác; HĐH nhả khoá khi
tiến trình chết (không có lock mồ côi). OBSERVED 2026-10-22: handle thứ hai trong cùng tiến trình bị từ chối `PermissionError` errno 13, khoá
được lại sau khi nhả. `run_command(argv, timeout)` → `(exit, stdout, stderr)`.

**R2.14 CLI — `__main__.py`** `main(argv=None, *, env=None, factory=None) -> int`, gọi chỉ dưới `if __name__ == "__main__":` (để
`test_importing_the_cli_loads_no_numpy_pandas_or_vnstock` import được mà không chạy gì):
`run` (khoá → KeepAwake → chụp baseline canary → `startup` → watchdog thread → `run_forever`; watchdog thoát 70), `once [--all]` (một chu kỳ, in tóm tắt
số đếm — dry run/burn-in), `heartbeat`, `backup --day YYYY-MM-DD`, `audit --state <snapshot> --from --to [--days trading|all] [--require-days 5] [--out report.json]`,
`check-root`, `canary-reset` (chụp lại baseline canary; chỉ ghi một hàng `kv` qua khoá của chính SQLite với `busy_timeout`, nên chạy được khi `run` đang
chạy — red-team r1 LT4). **Khoá một tiến trình cho mọi lệnh ghi archive/outage** — `run`, `once`, `heartbeat`, `backup` đều lấy `SingleInstanceLock`;
đang có `run` giữ khoá → exit 3 (red-team r1 LT10). `polls.run_mode` = tên lệnh (`run`|`once`), `polls.pid` = pid. Mặc định
`--config` = `<clone>/configs/capture.toml` (tìm qua `Path(__file__).resolve().parents[3]`). `audit` không đọc `FINSEC_DATA_ROOT` hay
`FINSEC_BACKUP_DIR` (chạy trên máy dev, trên snapshot backup). Exit: 0 ok; 1 audit không đạt; 2 lỗi dùng/config (gồm thiếu `FINSEC_BACKUP_DIR` cho
`run`/`once`/`heartbeat`/`backup`); 3 đã có tiến trình chạy; 4 data root bị từ chối.

**R2.15 `configs/capture.toml` (mới)** — TOML (`tomllib` là stdlib từ 3.11, `docs/code-standards.md:10`; không PyYAML): `[schedule]` (D5 + ngưỡng R2.7,
R2.9), `[[holidays]]` (`date`, `status`, `source_url`, `notice_id`), `[[sources]]` (`name`, `kind` ∈ {`hose_news_api`, `rss_feed`, `vnstock_events`},
`enabled`, `url`, `active_interval_s`, `idle_interval_s`, `timeout_s`, `deadline_s`, `retries`, `page_size`, `max_pages`, `min_request_spacing_s`,
`max_body_bytes`, `headers`; vnstock: `vnstock_source`, `symbols`, `symbols_per_poll`, `calls_per_min`, `vendor_call_timeout_s`, `release_expires_on`,
`error_map`), `[heartbeat]`
(`canary_files = ["AGENTS.md", "CLAUDE.md"]`, `stripchart_host`). Giá trị từ bảng design inputs P1; còn lại `[ASSUMED]` có comment. `symbols` của
nguồn `vnstock_events` = thành phần VN30 tại ngày probe P1, kèm comment ghi ngày as-of (VL-1, owner 2026-10-24).

**R2.16 `pyproject.toml`** — thêm `"tzdata"` vào `[project].dependencies` (`pyproject.toml:11-17`), KHÔNG marker, KHÔNG cận dưới (thiết kế của main M1).
Lý do đúng (red-team r1 LT11): zoneinfo tìm `TZPATH` trước rồi mới tới gói `tzdata` (VERIFIED https://docs.python.org/3/library/zoneinfo.html); Windows
không có cơ sở IANA hệ thống nên mọi máy Windows (`.venv` dev, laptop) cần gói, còn CI Linux dùng tz của hệ thống — vì thế CI che lỗi này. Không có
phiên bản tối thiểu nào được yêu cầu; 2026.5 chỉ là bản uv resolve ngày 2026-10-22. `.venv` (3.13.14) hiện thiếu tzdata: `ZoneInfo('America/New_York')`
và `ZoneInfo('Asia/Ho_Chi_Minh')` đều raise `ZoneInfoNotFoundError` (OBSERVED 2026-10-22, main + planner).
**R2.17 `.env.example`** — thêm dưới `FINSEC_DATA_ROOT=` (P1): `# Cloud-synced folder for the heartbeat and nightly backups (laptop only).` + `FINSEC_BACKUP_DIR=`.

## Related Code Files
| Hành động | Tệp |
|---|---|
| Create | `src/finsec_alpharadar/capture/__init__.py` |
| Create | `src/finsec_alpharadar/capture/__main__.py` |
| Create | `src/finsec_alpharadar/capture/config.py` |
| Create | `src/finsec_alpharadar/capture/record.py` |
| Create | `src/finsec_alpharadar/capture/schedule.py` |
| Create | `src/finsec_alpharadar/capture/sources.py` |
| Create | `src/finsec_alpharadar/capture/storage.py` |
| Create | `src/finsec_alpharadar/capture/poller.py` |
| Create | `src/finsec_alpharadar/capture/heartbeat.py` |
| Create | `src/finsec_alpharadar/capture/backup.py` |
| Create | `src/finsec_alpharadar/capture/audit.py` |
| Create | `src/finsec_alpharadar/capture/host.py` |
| Create | `configs/capture.toml` |
| Create | `tests/test_capture_record.py` |
| Create | `tests/test_capture_schedule.py` |
| Create | `tests/test_capture_sources.py` |
| Create | `tests/test_capture_storage.py` |
| Create | `tests/test_capture_poller.py` |
| Create | `tests/test_capture_ops.py` |
| Create | `tests/test_capture_cli.py` |
| Create | `tests/test_capture_invariants.py` |
| Modify | `pyproject.toml` |
| Modify | `.env.example` |
| Modify | `tests/test_dependency_pins.py` |
| Modify | `tests/test_repo_hygiene.py` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `docs/phase0-spike-report.md` (mục 3, design inputs) |
| read | `plans/261022-1147-phase0a-capture/research/capture-host-windows.md` |
| read | `plans/261022-1147-phase0a-capture/research/vn-calendar-sessions.md` |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Ghi chú parser: `.env.example` → advisory đã biết như Phase 1.

Challenge luật ">8 tệp": 29 tệp = 12 module + 10 file test (8 mới, 2 sửa — `tests/test_dependency_pins.py`, `tests/test_repo_hygiene.py`) + config +
`pyproject.toml` + `.env.example` + 4 docs. Đã cân nhắc tách đôi (lõi poll / ops): scaffold của plan cố định 7 phase, và luật đóng băng (Phase 3) đòi
MỌI thứ chạy trên laptop — gồm heartbeat, backup, audit — xong trước khi triển khai; tách ra không giảm rủi ro, chỉ thêm một vòng delta docs. Giữ
một phase, TDD theo thứ tự module cố định (bước 2-10), chạy test đích sau mỗi module, một commit khi cả gate xanh.

## Implementation Steps
0. **R2.0** — đọc quyết định cổng + design inputs; lệch thì DỪNG.
1. **tzdata trước** (các module sau cần nó): viết `test_tzdata_is_an_unconditional_core_dependency` + `test_zoneinfo_loads_new_york_and_ho_chi_minh`
   → RED; sửa `pyproject.toml` (R2.16); `.venv/Scripts/python -m pip install -e ".[dev]"` → xanh.
2. `record.py` ← `tests/test_capture_record.py` (RED: `ModuleNotFoundError`) → xanh.
3. `schedule.py` ← `tests/test_capture_schedule.py`.
4. `storage.py` ← `tests/test_capture_storage.py`.
5. `sources.py` ← `tests/test_capture_sources.py` (payload tổng hợp viết ngay trong test — mã giả `AAA`, `BBB`, tiêu đề bịa, không tên người thật,
   không dữ liệu vnstock thật; không file fixture).
6. `poller.py` ← `tests/test_capture_poller.py`.
7. `heartbeat.py`, `backup.py`, `audit.py` ← `tests/test_capture_ops.py`.
8. `host.py`, `config.py`, `__main__.py`, `configs/capture.toml` ← `tests/test_capture_cli.py`.
9. `tests/test_capture_invariants.py` (khoá cấu trúc) + thêm tham số vào `tests/test_repo_hygiene.py` + `.env.example` (R2.17).
10. **Dry run cục bộ (dev, mạng thật, không phải test):** `FINSEC_DATA_ROOT=<dev root>/capture-dry FINSEC_BACKUP_DIR=<dev root>/backup-dry .venv/Scripts/python -m finsec_alpharadar.capture once --all`
    với nguồn vnstock `enabled = false` (vnstock chỉ chạy dưới user probe/capture — D6) → in số item mỗi cổng, ghi JSONL + blob + state dưới data root;
    rồi `… audit --state <root>/state/capture.sqlite --from <hôm nay> --to <hôm nay> --days all --require-days 0`. `[ASSUMED]` tới lần chạy này.
11. **Delta docs** (dưới) → **Regression gate** → một commit, ví dụ `feat: add the VN capture-only poller` + trailer; push; CI xanh.

### Delta docs (cùng commit; tiếng Anh; có ngày)
- **AGENTS.md**
  - "Decisions" (trên cùng): D5 — `- <date> — VN capture schedule (owner, D5): every 2-5 min from 08:00 to 17:30 ICT on trading days and hourly otherwise, per source in configs/capture.toml; a gap is a missed schedule: no successful poll by the second scheduled due time after the last one, plus the maximum poll duration.`;
    D3 — `- <date> — Capture heartbeat and backup (owner, D3): a heartbeat JSON and nightly backups (tar.gz of closed UTC days, an SQLite snapshot and a sha256 manifest) go to a cloud-synced folder on the laptop; the owner checks the heartbeat from a phone; no new alert channel before Phase 4.`;
    thiết kế của main — `- <date> — tzdata is an unconditional core dependency: zoneinfo reads TZPATH before the tzdata package, and Windows has no system IANA database, so the dev .venv and the laptop need the package (Linux CI uses the system zone files). No minimum version is required.` (red-team r1 LT11)
  - "Package Layout": bullet mới `capture/` (poller chỉ-thu-thập: nguồn, schema bản ghi thô v1, outage log, `pit_quality`, heartbeat, backup, audit;
    chỉ stdlib lúc import; vnstock import lười) — trỏ tới mục schema trong `docs/system-architecture.md`.
  - "Current Status": thêm "VN capture poller built (<date>), not deployed yet"; câu "There is no live connector" (dòng 45) → capture là client HTTP đầu
    tiên trong `src/`; số test do gate in ra ở đủ năm chỗ (P1 "Delta docs", red-team r1 LT12).
  - "Build & Verification Commands": **Capture** — `python -m finsec_alpharadar.capture once --all | run | heartbeat | backup --day <d> | audit --state <db> --from <d> --to <d> | check-root | canary-reset`
    (bash + PowerShell, cần `FINSEC_DATA_ROOT`, `FINSEC_BACKUP_DIR`); ghi chú tzdata: chạy lại `pip install -e ".[dev]"` sau khi kéo commit này.
- **docs/system-architecture.md**: dòng 11 ("No HTTP/WebSocket client … anywhere in `src/`") → `capture/sources.py` là client HTTP duy nhất (urllib,
  GET/POST tới cổng VN); bảng as-built thêm 12 hàng `capture/*`; bỏ hàng `capture/` khỏi "Target modules" (dòng 43); "Data flow → As-built" thêm khối
  capture; mục con mới **"Raw capture record schema v1"** (bảng R2.2 + luật "earliest `first_seen_at` per (source, source_id, raw_sha256) wins" + bố cục
  R2.3 + state R2.4, tiếng Anh) — nguồn sự thật cho `store/` Phase 1; số test dòng 13;
  "Boundaries & trust": env đọc (`FINSEC_DATA_ROOT`, `FINSEC_BACKUP_DIR`), payload cổng/vnstock không tin cậy và chỉ trích id, không parser XML.
- **docs/code-standards.md**: "Timestamps" (dòng 22-25): bản ghi capture là chuỗi UTC `…Z` micro giây, đồng hồ tiêm vào và chỉ `capture/host.py` đọc
  đồng hồ (khoá bởi `tests/test_capture_invariants.py`); "Error handling": phân loại outage R2.7, ghi log theo R2.1; "Testing": số test + "capture tests
  build synthetic payloads inline (made-up tickers and titles), never real portal or vnstock data"; "Dependencies" (dòng 91-92): tzdata lõi; dòng 30 —
  trích lại `[tool.setuptools.packages.find]` vì dòng tzdata làm dời `pyproject.toml` thêm một lần nữa (red-team r1 LT12).
- **docs/project-roadmap.md**: §5.2 hàng `capture/` (dòng 226) "Built <date> (plan 261022-1147, phase 2); deployed in phase 3"; §7 0b.6 (dòng 394-398)
  "Built <date>; sources: <cổng owner chọn> + vnstock events (Gen 5 Community has no `news()`)" — hệ quả DERIVED của dữ kiện, không phải đổi scope.

### Tests Before
Mọi test dùng đồng hồ giả (`FakeClock(now_utc, mono)`), sleeper giả (ghi lại số giây), fetcher giả, `tmp_path`; không mạng, không `sleep`, không
đồng hồ thật. RED chung: `ModuleNotFoundError` cho tới khi module tồn tại.

`tests/test_dependency_pins.py` (thêm):
- [ ] `test_tzdata_is_an_unconditional_core_dependency` — `tomllib`: có requirement `tzdata` trong `[project].dependencies`, không marker; RED.
- [ ] `test_zoneinfo_loads_new_york_and_ho_chi_minh` — FAIL (không skip) với thông điệp "install tzdata: …"; RED trong `.venv` (OBSERVED).

`tests/test_capture_record.py`:
- [ ] `test_record_round_trips_through_one_json_line` (một dòng, kết thúc `\n`, khoá sắp xếp, `from_json_line(to_json_line(r)) == r`).
- [ ] `test_vietnamese_payload_bytes_are_preserved` (chuỗi NFD và NFC đi qua nguyên byte; không chuẩn hoá).
- [ ] `test_naive_or_non_utc_datetimes_are_refused`, `test_information_time_must_equal_first_seen_at_in_v1`, `test_ingested_before_first_seen_is_refused`,
      `test_bad_sha_or_empty_ids_are_refused`, `test_item_and_snapshot_payload_shapes_are_exclusive`, `test_schema_version_is_one`.
- [ ] `test_canonical_item_bytes_ignore_key_order`, `test_capture_code_digest_ignores_crlf`.

`tests/test_capture_schedule.py`:
- [ ] `test_active_window_is_0800_inclusive_to_1730_exclusive_ict_on_trading_days`, `test_weekend_and_listed_holiday_use_the_idle_interval`
      (2026-11-24), `test_next_due_stops_at_the_0800_boundary`, `test_next_due_crosses_midnight`, `test_gap_deadline_steps_the_schedule_twice`,
      `test_gap_deadline_after_a_0700_poll_ends_at_0805_not_0902`,
      `test_missing_tzdata_raises_a_named_error` (ZoneInfo bị monkeypatch để raise).

`tests/test_capture_storage.py`:
- [ ] `test_jsonl_rotates_by_utc_date_of_ingested_at`, `test_every_touched_file_is_fsynced_once_before_commit` (đếm `fsync` theo TỪNG file — JSONL và blob —
      qua hàm tiêm vào; RT12), `test_torn_tail_is_truncated_and_reported`, `test_blob_put_is_idempotent_and_leaves_no_temp_file`,
      `test_existing_blob_with_a_wrong_sha_is_rewritten` (RT12), `test_startup_reconciles_archived_records_into_seen` (archive ghi, state chưa commit →
      khởi động lại → không có bản ghi thứ hai; RT12), `test_state_uses_wal_and_full_sync` (`PRAGMA` trả `wal` và `2`),
      `test_detail_stores_class_code_hash_and_length_only` (LT8),
      `test_changed_payload_of_a_known_id_is_a_new_seen_key`, `test_outage_open_close_and_reopen_state`.

`tests/test_capture_sources.py`:
- [ ] HOSE: `test_hose_ids_are_extracted_from_a_synthetic_page`, `test_hose_request_dates_are_ict_calendar_days` (23:30 UTC = ngày ICT kế tiếp), `test_hose_pages_until_a_seen_id`,
      `test_hose_page_cap_reports_coverage_overflow`, `test_hose_unsuccessful_body_is_invalid_payload`, `test_http_403_and_429_are_source_rejected`,
      `test_http_5xx_and_timeouts_are_source_unreachable`, `test_request_descriptor_holds_no_header_values`.
- [ ] RSS: `test_rss_guids_are_extracted_without_an_xml_parser` (body có `<!DOCTYPE … <!ENTITY …>` vẫn là text, không mở rộng), `test_rss_item_without_guid_is_invalid`,
      `test_rss_without_overlap_reports_coverage_overflow`, `test_oversized_body_is_invalid`, `test_gzip_bomb_is_cut_at_max_body_bytes` (LT9),
      `test_trickling_body_hits_the_total_deadline` (response giả trả 1 byte mỗi lần đọc trong khi đồng hồ giả tiến 5 s; RT2),
      `test_hose_asks_for_yesterday_only_on_the_first_poll_of_an_ict_day` (RT13).
- [ ] vnstock: `test_vnstock_quota_warning_opens_an_outage_and_keeps_the_cursor` (fake phát `UserWarning` + `attrs` cắt; RT7),
      `test_vnstock_call_timeout_is_an_outage_and_blocks_a_second_worker` (`run_with_timeout` giả raise `TimeoutError`; RT2),
      `test_vnstock_rows_become_items_with_prefixed_ids`, `test_vnstock_round_robin_resumes_from_the_cursor`, `test_vnstock_errors_map_to_outage_kinds`
      (lỗi giả có thuộc tính `code`), `test_unknown_symbol_is_not_an_outage`, `test_telemetry_env_is_set_before_import` (importer giả kiểm `os.environ`).
- [ ] `test_urllib_fetcher_maps_http_errors_without_network` (opener giả raise `urllib.error.HTTPError(…, fp=io.BytesIO(…))`).

`tests/test_capture_poller.py`:
- [ ] `test_first_ever_poll_marks_items_late`, `test_steady_state_items_are_first_seen`, `test_items_after_a_source_outage_are_late_once`,
      `test_process_down_is_logged_from_the_last_alive_tick`, `test_outage_left_open_by_a_crash_is_closed_at_restart`,
      `test_clock_step_is_logged_and_makes_the_next_items_late`, `test_monotonic_jump_is_logged_as_host_stalled`,
      `test_one_outage_per_failure_run_not_per_retry`, `test_vnstock_failure_does_not_stop_portal_capture`,
      `test_snapshot_is_written_only_when_the_body_changes`, `test_token_bucket_caps_vnstock_calls_per_minute`,
      `test_retries_use_the_injected_sleeper`, `test_archive_is_synced_before_state_commit`, `test_watchdog_requests_exit_when_ticks_stop`,
      `test_watchdog_thread_exits_while_the_main_loop_is_blocked` (fetcher giả chặn tới khi watchdog gọi `exit_fn` giả qua `check_once` trên đồng hồ giả; RT2),
      `test_busy_multi_source_cycle_does_not_log_host_stalled` (tick giữa các nguồn; LT1), `test_poll_rows_record_run_mode_and_pid` (LT3),
      `test_heartbeat_or_backup_failure_does_not_stop_capture` (RT11).

`tests/test_capture_ops.py`:
- [ ] heartbeat: `test_status_ok_degraded_down_rules`, `test_skew_median_and_max`, `test_vnstock_expiry_within_30_days_degrades`,
      `test_canary_change_degrades`, `test_canary_baseline_is_taken_at_run_start_and_reset_by_canary_reset` (LT4),
      `test_heartbeat_keys_are_a_fixed_whitelist` (không tiêu đề/payload), `test_heartbeat_write_is_atomic_and_daily`,
      `test_locked_replace_retries_then_degrades_without_raising` (replacer giả raise `PermissionError`; dòng `heartbeats` chỉ sau khi thành công; RT11),
      `test_probe_failure_is_null_with_error_not_a_crash`.
- [ ] backup: `test_backup_archives_one_closed_utc_day_with_a_verified_manifest`, `test_backup_is_idempotent`, `test_backup_refuses_today`,
      `test_backup_at_0300_ict_refuses_the_still_open_utc_day` (`now` tiêm vào; RT14), `test_snapshot_is_not_in_wal_mode` (byte header 18/19 = 1; RT15),
      `test_backup_reader_checks_the_manifest_sha_first`, `test_backup_reader_refuses_traversal_links_and_unlisted_members` (tar độc có `../escaped.txt`,
      symlink, đường dẫn tuyệt đối; RT9),
      `test_backup_never_deletes_sources`.
- [ ] audit: `test_day_with_only_logged_outages_and_no_ok_poll_fails`, `test_trailing_silence_without_outage_fails_the_day` (RT1),
      `test_audit_creates_no_file_next_to_the_snapshot` và `test_audit_refuses_a_wal_mode_database` (RT15),
      `test_unlogged_gap_fails_the_day`, `test_gap_covered_by_a_source_outage_passes`, `test_gap_covered_by_chained_outages_passes`,
      `test_process_down_covers_every_source`, `test_0800_switch_is_not_a_gap`, `test_missed_active_polls_after_0800_are_a_gap`,
      `test_holiday_hourly_cadence_is_not_a_gap`,
      `test_day_without_heartbeat_fails`, `test_skew_bounds_and_no_date_evidence`, `test_five_consecutive_trading_days_across_a_weekend_and_2026_11_24`.

`tests/test_capture_cli.py`:
- [ ] `test_shipped_config_loads_and_respects_d5` (`configs/capture.toml`: mọi nguồn `120 ≤ active ≤ 300`, `idle == 3600`), `test_unknown_source_kind_is_a_config_error`,
      `test_missing_backup_dir_exits_2`, `test_check_root_refuses_an_unignored_root_in_a_work_tree_exit_4` (repo git tạm),
      `test_once_writes_records_through_injected_sources`, `test_second_instance_exits_3`,
      `test_once_heartbeat_and_backup_exit_3_while_run_holds_the_lock` (LT10), `test_canary_reset_runs_while_run_holds_the_lock` (LT4),
      `test_backup_dir_inside_an_unignored_work_tree_exits_4` (LT7),
      `test_keep_awake_calls_the_setter_with_es_flags` (setter giả: `0x80000001` rồi `0x80000000`), `test_keep_awake_is_a_no_op_off_windows`.

`tests/test_capture_invariants.py` (khoá cấu trúc, `invariant`):
- [ ] `test_capture_modules_import_no_heavy_package_at_top_level` (AST: không `import vnstock|pandas|numpy` ở cấp module).
- [ ] `test_only_host_reads_the_clock_or_sleeps` (regex trên `capture/*.py`: `datetime.now(`, `datetime.today(`, `date.today(`, `utcnow(`, `time.time(`,
      `time.time_ns(`, `time.monotonic(`, `time.perf_counter(`, `time.sleep(` và mọi `from time import` chỉ được có trong `host.py`; RT14).
- [ ] `test_capture_imports_only_stdlib_data_root_and_itself` (AST: `from finsec_alpharadar.` chỉ `capture.*` và `data_root`).
- [ ] `test_importing_the_cli_loads_no_numpy_pandas_or_vnstock` (subprocess `sys.executable -I -c "import sys; sys.path.insert(0, <src>); import finsec_alpharadar.capture.__main__; assert not {'numpy','pandas','vnstock'} & set(sys.modules)"`).

`tests/test_repo_hygiene.py` — thêm tham số vào `test_data_artefacts_are_ignored` (`tests/test_repo_hygiene.py:48-61`): `data/capture/raw/hose_news_api/2026-10-22.jsonl`,
`data/capture/blobs/2026-10-22/0123abcd.bin`, `data/capture/state/capture.sqlite-wal` — XANH ngay (LOCK: vị trí trong repo duy nhất mà guard chấp nhận
là dưới `/data/`; OBSERVED 2026-10-22 `git check-ignore` → `.gitignore:32:/data/`). Không thêm dạng `capture-root/raw/x.jsonl` (exit 1 — không bị ignore).

### Implement
- Bước 1-9 ở trên, theo đúng thứ tự.

### Tests After
- [ ] Mọi test trên xanh; `tests/test_no_order_execution.py` xanh (quét `src/**/*.py` mới); `tests/test_agents_md_paths.py` xanh sau khi sửa AGENTS.md.
- [ ] Dry run bước 10 ghi được ít nhất 1 snapshot + item mỗi cổng và `audit` chạy hết (exit 0 với `--require-days 0`).

### Regression Gate
- `.venv/Scripts/python -m pip install -e ".[dev]"` (pyproject đổi — để `tzdata` vào `.venv`) rồi `.venv/Scripts/python -m pytest -q` MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục, có tzdata 2026.3) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- Hai lệnh `scaffold_standards.py --check` (như Phase 1) exit 0.
- Sau push: CI `success` trên 3.11 + 3.13 (capture dùng `tomllib`, `zoneinfo`, `msvcrt`/`fcntl` — chân Linux kiểm nhánh `fcntl` của `SingleInstanceLock`).

## Success Criteria
- [ ] [test] Mọi test liệt kê RED trước, xanh sau; suite xanh trong `.venv`, 0 skipped; CI xanh trên 3.11 và 3.13.
- [ ] [invariant] `tests/test_capture_invariants.py` xanh: không đọc đồng hồ ngoài `host.py`, không import nặng lúc import, chỉ phụ thuộc stdlib + `data_root`.
- [ ] [invariant] Không file nào dưới `tests/` chứa dữ liệu cổng/vnstock thật: `git ls-files tests/fixtures` rỗng sau phase này (payload test viết tay trong code).
- [ ] [test] `configs/capture.toml` đã ship tuân D5 (`test_shipped_config_loads_and_respects_d5`).
- [ ] [manual] `configs/capture.toml` chứa đúng giá trị design inputs của Phase 1 (neo `manual_test_anchor.py`; đối chiếu bằng tay với báo cáo mục 3).
- [ ] [manual] Dry run bước 10 trên máy dev ghi được bản ghi v1 hợp lệ cho mỗi cổng được chọn (neo `manual_test_anchor.py`; bằng chứng: output `once` và `audit`).

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Schema v1 thiếu thứ Phase 1/3 cần, mà dữ liệu đã thu không sửa lại được | Trung bình × Cao | Giữ nguyên byte gốc (blob + item nguyên văn + `raw_sha256`), request/response descriptor, `cycle_id`, `capture_version`; parser sau tự suy mọi thứ khác. Đổi schema = v2 thêm trường, không sửa v1. |
| Quy tắc khoảng trống sai làm audit báo nhầm hoặc bỏ sót (lúc đổi lịch 08:00, ngày nghỉ đánh dấu nhầm) | Trung bình × Cao | Hạn chót bước lịch hai lần từ poll thành công trước (R2.5); test riêng cho 08:00 (cả hai chiều), ngày nghỉ, cuối tuần, 2026-11-24. |
| HOSE đổi API nội bộ SPA / WAF chặn | Trung bình × Trung bình | Lỗi → outage có tên (`source_rejected`, `source_invalid_payload`), không bao giờ im lặng; HNX vẫn chạy song song. |
| Trùng lặp item sau crash giữa archive và state | Thấp × Thấp | fsync mọi file đã chạm rồi mới commit state; `startup()` đối soát đuôi archive vào `seen` nên crash không sinh bản ghi thứ hai; nếu vẫn có, luật đọc v1 "first_seen_at sớm nhất thắng" (R2.2, R2.3; red-team r1 RT12). Mất thì không. |
| Một request/lệnh vnstock treo làm đứng cả vòng (RT2); thư mục sync khoá file làm chết tiến trình (RT11) | Trung bình × Cao | Watchdog trên daemon thread riêng + deadline tổng + worker có timeout; ghi heartbeat/backup có retry, lỗi thành DEGRADED, không raise. |
| Audit PASS mà không thu được gì (RT1) | Thấp × Cao | Cặp ảo hai mép + ≥ 1 poll ok mỗi nguồn cổng trong 08:00-17:30 mỗi ngày giao dịch. |
| Phase lớn (29 tệp) | Trung bình × Trung bình | Thứ tự module cố định, test đích sau mỗi module, chỉ commit khi cả gate xanh. |
| Đồng hồ máy lệch (w32time tắt — OBSERVED dev) | Cao × Trung bình | `skew_ms` mỗi poll từ `Date`, stripchart mỗi giờ, outage `clock_step`; audit đòi trung vị |skew| ≤ 2 s. Cấu hình NTP ở Phase 3. |
