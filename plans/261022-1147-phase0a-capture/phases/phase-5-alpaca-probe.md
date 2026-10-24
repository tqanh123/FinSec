---
phase: 5
title: "Alpaca Probe"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 5 — Alpaca Probe

## Overview
Probe Alpaca Market Data bằng paper keys (roadmap §7 0a, `docs/project-roadmap.md:359-362`): SPY bar ngày 2017 với `feed=sip` trên gói Basic (cổng
quyết định feed của Phase 1), `adjustment=all` so với `raw`/`split` quanh một ngày split đã biết, và bar của 2-3 mã đã huỷ niêm yết. Chỉ host dữ
liệu `https://data.alpaca.markets`, urllib thuần, không SDK; body thô ra data root; chỉ tỉ lệ/số đếm dẫn xuất vào AGENTS.md. Ghi feed đã chọn vào
`.env.example` (`ALPACA_DATA_FEED`, roadmap §5.2 dòng `.env.example`, `docs/project-roadmap.md:250`). Cook trong lúc chờ 5 phiên (D4). Phụ thuộc
Phase 4 (thứ tự chuỗi; dùng chung AGENTS.md/docs) và cạnh liên plan `261021-1314-phase0b-foundations/P5`.

## Requirements
**R5.1 BLOCKED (owner) OB1 — paper keys trong `.env`.** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (tên biến của repo, `.env.example:2-3`; script gửi chúng
dưới tên header của Alpaca `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY` — VERIFIED https://docs.alpaca.markets/us/docs/market-data-faq); `ALPACA_PAPER_TRADE=true`
giữ nguyên (`.env.example:4`, luật binding AGENTS.md:137). Phát hiện xong: `.venv/Scripts/python scripts/spike/alpaca_probe.py --check-env` exit 0 và in đúng
`ok` (từ chối placeholder `your_alpaca_paper_key`/`your_alpaca_paper_secret` và giá trị rỗng; lỗi chỉ nêu TÊN biến; không bao giờ in giá trị).

**R5.2 `scripts/spike/alpaca_probe.py` (mới; `.venv`; dùng `_spikelib`).**
- Hằng `DATA_HOST = "https://data.alpaca.markets"`; endpoint `/v2/stocks/{symbol}/bars`, `/v2/stocks/bars`, `/v1/corporate-actions` (VERIFIED research
  `alpaca-probe.md` §1, §4). KHÔNG BAO GIỜ host trading, kể cả để tra asset (`/v2/assets` chỉ có trên host trading — OBSERVED research §4).
- Viết sao cho `tests/test_no_order_execution.py` không khớp dòng nào, kể cả comment và docstring (regex ở `tests/test_no_order_execution.py:19-35`:
  `TradingClient`, `submit_order`, `alpaca\.trading`, `place_\w*order`, `fc[\s_-]?trading`, `(paper-)?api\.alpaca\.markets`, `/v2/orders`, `/v2/positions`,
  `alpaca[_-]trade[_-]api`, `close_\w*position`, `cancel_\w*order`); tên hàm kiểu `fetch_bars`, `fetch_corporate_actions`. Không import alpaca-py.
- Mọi request truyền tường minh `feed`, `adjustment`, `asof`, `start`, `end` là NGÀY CỐ ĐỊNH trong quá khứ (không suy `end` từ đồng hồ máy: mặc định feed
  mâu thuẫn trong docs và lỗi 42210000 từng do lệch đồng hồ — research §2; `w32time` máy dev đang Stopped — OBSERVED main).
- Danh sách (~30 request, research §8): (1) SPY `2017-01-01..2017-12-31` `feed=sip` `adjustment=raw` `asof=-` `limit=10000` — **cổng**: 200 + ~251 bar →
  `sip`; bị từ chối → lặp với `feed=iex`, ghi caveat volume IEX (~2.5% thị trường, VERIFIED research §2) và chọn `iex`; (2) cùng request `feed=iex` để so
  `v`, `n`; (3) SPY 2017 `adjustment=all` → `raw/all` ngày đầu/cuối (cổ tức); (4) TSLA quanh 2022-08-25 (3:1, sạch nhất — không cổ tức tiền mặt `[PRIOR]`)
  × {`raw`, `split`, `all`}, cửa sổ T−7..T+7; thêm AAPL 2020-08-31, NVDA 2024-06-10, GOOGL 2022-07-18 nếu ngân sách cho phép; (5) TWTR (cuối 2022-10-27),
  ATVI (2023-10-12), SIVB (2023-03-09) × `asof` ∈ {bỏ trống, ngày cuối, `-`}, cửa sổ cuối−30..cuối+30; (6) corporate actions: một request merger/name-change
  cho mã huỷ niêm yết (`start=2015-01-01`, `end=2026-10-21` tường minh — mặc định là "current day"), một request `forward_split` cho mã split; (7) status HTTP
  thật của lỗi `42210000`: `GET /v2/stocks/AAPL/trades/latest?feed=sip` (endpoint đọc dữ liệu; status chưa có trang nào nêu — research §2).
- Một luồng, ≥ 0.5 s giữa hai request (≤ 120/phút < 200/phút của Basic — AGENTS.md:105); 429 hoặc `X-RateLimit-Remaining ≤ 5` → ngủ tới
  `X-RateLimit-Reset` + 1 s, tối đa 3 lần. Phân trang tới khi `next_page_token` null.
- Log JSONL mỗi request vào `$FINSEC_DATA_ROOT/spike/alpaca/<UTC-date>/requests.jsonl`: `requested_at_utc`, `clock_synced: "unverified"`, `key_id_prefix`
  (4 ký tự đầu, KHÔNG secret), `method`, `url` đầy đủ (key ở header, không ở URL), `http_status`, `x_ratelimit_limit/remaining/reset`, `body_sha256`,
  `body_bytes`, `error_code/message`, `bar_count` theo mã, `first_t`, `last_t`, `pages`. Body gốc nén `.json.gz` cùng thư mục. Điều khoản dữ liệu Alpaca:
  không tái phân phối (`[ASSUMED]`, roadmap §4 dòng 168) → không commit body, không fixture bar thật.
- `summary.json`: chỉ số dẫn xuất — số bar, `t` đầu/cuối (kể cả `t` mùa đông có là `05:00:00Z` không — research câu mở 3), `c(T−1)/c(T)` và `v(T)/v(T−1)`
  theo biến thể, `raw/all` SPY, ma trận phủ delisted, `X-RateLimit-Limit` thật, status 42210000.

**R5.3 `.env.example`** — thêm ngay sau dòng 4 (`ALPACA_PAPER_TRADE=true`): `ALPACA_DATA_FEED=<sip|iex theo cổng (1)>` với comment tiếng Anh
`# Feed for historical daily bars; chosen by the Phase 0 Alpaca probe (AGENTS.md "Verified Facts").` Chưa code nào đọc biến này (client Phase 1).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Create | `scripts/spike/alpaca_probe.py` |
| Modify | `.env.example` |
| Modify | `tests/test_repo_hygiene.py` |
| Modify | `docs/phase0-spike-report.md` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261022-1147-phase0a-capture/research/alpaca-probe.md` |
| run | `scripts/spike/alpaca_probe.py` (mạng; ngoài pytest) |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Ghi chú parser: `.env.example` → advisory đã biết. `docs/code-standards.md` có đổi: phase này thêm một test nên số test ở `docs/code-standards.md:50`
và `:56` phải cập nhật (red-team r1 LT12).

## Implementation Steps
1. **BLOCKED (owner) OB1** — R5.1. Chưa `--check-env` → DỪNG phase.
2. **Tests-before:** thêm `test_env_example_declares_the_alpaca_data_feed` vào `tests/test_repo_hygiene.py` → RED (chưa có dòng). LOCK:
   `tests/test_no_order_execution.py` phải xanh với script mới.
3. Viết `alpaca_probe.py`; `.venv/Scripts/python -m pytest -q tests/test_no_order_execution.py` → xanh TRƯỚC khi chạy mạng.
4. Chạy probe (mạng thật); đọc `summary.json`; quyết feed theo cổng (1).
5. Sửa `.env.example` (R5.3) → test hygiene xanh.
6. **Delta docs** → **Regression gate** → một commit, ví dụ `docs: record the Alpaca Basic probe` + trailer; push.

### Delta docs (cùng commit; tiếng Anh; có ngày)
- **AGENTS.md** "Verified Facts" khối Alpaca Basic (dòng 104-107): thay "SIP history on Basic: ASSUMED." bằng
  `SIP daily history on Basic (OBSERVED <date>, paper keys): GET /v2/stocks/SPY/bars?feed=sip&timeframe=1Day&start=2017-01-01&end=2017-12-31&adjustment=raw&asof=- → HTTP <s>, <n> bars …; X-RateLimit-Limit <..>.`;
  thêm `Alpaca adjustment (OBSERVED <date>)` (tỉ lệ raw/split/all quanh TSLA 2022-08-25 …, SPY raw/all), `Alpaca delisted coverage (OBSERVED <date>)`
  (TWTR/ATVI/SIVB × 3 biến thể asof; caveat survivorship nếu không phủ), status thật của `42210000`. Thêm vào "Build & Verification Commands" > Spikes:
  `scripts/spike/alpaca_probe.py --check-env`.
- **docs/system-architecture.md** External dependencies Alpaca (dòng 108-111): "SIP history on Basic: ASSUMED" → kết quả + feed đã chọn; số test dòng 13.
- **docs/code-standards.md**: "Testing" — số test ở dòng 50 và 56 (red-team r1 LT12); AGENTS.md: số test ở dòng 51 và 169.
- **Dòng cửa sổ (red-team r1 LT4):** `grep -n "capture window started" AGENTS.md` không có hit mà cửa sổ đã mở (bước 9 của P3) → thêm dòng Decisions MỚI
  `- <date> — capture window started <date> at <sha> (capture_version <v>).` trong commit này (P4 bỏ qua vì cửa sổ chưa mở).
- **docs/project-roadmap.md**: §7 0a gạch Alpaca (dòng 359-362) "Done <date>"; §4 hàng Alpaca (dòng 168) "SIP history on Basic and delisted coverage:
  probed <date>"; §5.2 hàng `.env.example` (dòng 250) "Add ALPACA_DATA_FEED (done <date>)"; §5.3 hàng "Alpaca Basic SIP history" (dòng 266) kết quả.
- **docs/phase0-spike-report.md** mục 5: bảng request → status/bar/tỉ lệ (chỉ số dẫn xuất).

### Tests Before
- [ ] `test_env_example_declares_the_alpaca_data_feed` (`tests/test_repo_hygiene.py`) — có đúng một dòng `ALPACA_DATA_FEED=` với giá trị ∈ {`sip`, `iex`};
      `ALPACA_PAPER_TRADE=true` vẫn còn; RED (dòng chưa có).
- [ ] LOCK `tests/test_no_order_execution.py::test_code_has_no_order_execution` với `scripts/spike/alpaca_probe.py` mới (guard quét `scripts/**/*.py`,
      `tests/test_no_order_execution.py:59-64`).

### Implement
- Bước 3-5.

### Tests After
- [ ] Test hygiene mới xanh; guard no-order-execution xanh; `tests/test_agents_md_paths.py` xanh.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- Hai lệnh `scaffold_standards.py --check` (`--type system-architecture` và `--type code-standards`) exit 0.
- `git grep -n -F "$ALPACA_API_KEY"` và `git grep -n -F "$ALPACA_SECRET_KEY"` → không hit (shell đã nạp `.env`; không in giá trị khi không hit).
- Sau push: CI `success` trên 3.11 + 3.13.

## Success Criteria
- [ ] [test] Test hygiene RED trước, xanh sau; guard no-order-execution xanh với script mới.
- [ ] [manual] `summary.json` trả lời đủ 7 câu R5.2 (neo `manual_test_anchor.py`; hệ thống ngoài sống, không double được).
- [ ] [invariant] Không body Alpaca, không key trong repo (grep ở gate; `git status --porcelain` chỉ có file trong bảng).
- [ ] [invariant] `grep -n "SIP history on Basic: ASSUMED" AGENTS.md docs/system-architecture.md` → không hit (thay bằng dòng OBSERVED có ngày);
      `tests/test_agents_md_paths.py` xanh.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Basic không cho SIP 2017 | Thấp × Trung bình | Cổng (1): rơi về `iex`, ghi caveat volume; Phase 1 dùng feed đã ghi. |
| Script vô tình khớp regex guard (kể cả comment) | Trung bình × Thấp | Chạy guard trước khi chạy mạng (bước 3). |
| Đồng hồ lệch gây 42210000 | Thấp × Thấp | Ngày cố định trong quá khứ; không `end` suy từ đồng hồ. |
| Mã huỷ niêm yết bị đổi/tái dùng ticker | Trung bình × Trung bình | Ba biến thể `asof`; bar sau ngày cuối với `asof=-` = dấu hiệu tái dùng; danh tính dài hạn = CIK + CUSIP (Phase 1 `refdata/`). |
| Key lọt vào log | Thấp × Cao | Chỉ 4 ký tự đầu của key id; secret không bao giờ ghi; grep ở gate. |
