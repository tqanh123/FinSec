# Research: Alpaca Market Data probe (paper keys, gói Basic) cho Phase 0a

Ngày: 2026-10-22. Phạm vi: chỉ đọc tài liệu và trang web công khai; KHÔNG gọi API Alpaca (không có key), không cài gói, không tải wheel. Mọi kết luận về hành vi thật của API là hypothesis cho tới khi probe chạy.

Nhãn: VERIFIED (URL, fetch 2026-10-22) · OBSERVED (tôi tự chạy, có lệnh và kết quả) · ASSUMED (chưa kiểm) · PRIOR (kiến thức huấn luyện, chưa kiểm lại).

Ghi chú nguồn: tài liệu docs.alpaca.markets có bản Markdown máy đọc được; tôi tải bằng `curl -s -A "Mozilla/5.0 (research)" https://docs.alpaca.markets/us/<path>.md` (OBSERVED 2026-10-22, HTTP 200 cho `reference/stockbars` 17585 B, `reference/stockbarsingle-1` 16740 B, `docs/market-data-faq` 34729 B, `docs/historical-stock-data-1` 2765 B, `docs/historical-api` 925 B, `docs/about-market-data-api` 7924 B, `reference/get-v2-assets-symbol_or_asset_id` 11057 B). Trích dẫn dưới đây lấy nguyên văn từ các file đó.

---

## 1. Request bars lịch sử chính xác

**Host và auth.**
- Base URL lịch sử: `https://data.alpaca.markets/{version}` — VERIFIED https://docs.alpaca.markets/us/docs/historical-api (updatedAt 2025-09-24).
- Header: `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`; FAQ checklist: "make sure the host is `data.alpaca.markets` for historical" — VERIFIED https://docs.alpaca.markets/us/docs/market-data-faq (updatedAt 2026-09-21), mục "Why am I getting HTTP 403 (Forbidden)?". Reference stockbars cũng nêu hai header này hoặc HTTP Basic auth — VERIFIED https://docs.alpaca.markets/reference/stockbars.
- Paper keys dùng cho data: FAQ ghi "when you reset your paper account, you need to regenerate your credentials" trong checklist cho market data → suy ra paper keys là credential bình thường cho data API. Đây là suy luận từ doc → ASSUMED cho tới khi probe nhận HTTP 200 đầu tiên.
- Repo đặt tên biến là `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (OBSERVED `.env.example`), khác tên trong doc (`APCA_API_KEY_ID`). Probe đọc tên của repo rồi gửi dưới tên header của Alpaca.

**Endpoint.**
- Đa mã: `GET https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL,TSLA&timeframe=1Day&...` — VERIFIED reference stockbars (updatedAt 2026-05-27).
- Một mã: `GET https://data.alpaca.markets/v2/stocks/{symbol}/bars?...` — VERIFIED `reference/stockbarsingle-1.md` (servers `https://data.alpaca.markets`, sandbox `https://data.sandbox.alpaca.markets`) và ví dụ curl trong FAQ: `"https://data.alpaca.markets/v2/stocks/AAPL/bars?feed=sip&timeframe=1Day&start=2023-09-29&limit=1"`.

**Tham số (reference stockbars, VERIFIED):**

| Param | Giá trị / mặc định (nguyên văn khi quan trọng) |
|---|---|
| `symbols` | bắt buộc ở endpoint đa mã, phân cách dấu phẩy |
| `timeframe` | `[1-59]Min`/`T`, `[1-23]Hour`/`H`, `1Day`/`1D`, `1Week`/`1W`, `[1,2,3,4,6,12]Month`/`M` |
| `start` | "RFC-3339 or YYYY-MM-DD". "Default: the beginning of the current day, but at least 15 minutes ago if the user doesn't have real-time access for the feed." |
| `end` | "Default: the current time if the user has a real-time access for the feed, otherwise 15 minutes before the current time." |
| `limit` | 1–10000, mặc định 1000; "The limit applies to the total number of data points, not per symbol!" |
| `adjustment` | `raw` (default), `split`, `dividend`, `spin-off`, `all`; "Multiple values can be combined with commas" |
| `asof` | `YYYY-MM-DD`, mặc định ngày hiện tại; "The special value of "-" means symbol mapping is skipped... The same happens if the queried symbol is not found on the given `asof` date." |
| `feed` | `sip`, `iex`, `boats`, `otc` (schema `"default": "sip"`) |
| `currency` | mặc định USD |
| `page_token`, `sort` (`asc` mặc định / `desc`) | phân trang, thứ tự |

**Response.**
- Đa mã: `bars` là object keyed by symbol → mảng bar; `next_page_token` (string|null); `currency`. Mỗi bar bắt buộc `t, o, h, l, c, v, n, vw` — VERIFIED reference stockbars.
- Một mã: `bars` là mảng, kèm `symbol` và `next_page_token` — VERIFIED ví dụ FAQ.
- Phân trang: "Results are sorted by symbol, then by timestamp. A first page may contain only one symbol." → lặp đến khi `next_page_token` null — VERIFIED reference stockbars.
- Timestamp bar ngày: FAQ ví dụ `"t": "2023-09-29T04:00:00Z"`, và "The (SIP) timestamp of the trade is truncated ... to the day (in New York) for daily bars" — VERIFIED FAQ "How are bars aggregated?". Nghĩa là `t` của bar ngày = nửa đêm New York tính ra UTC: `04:00Z` mùa hè (EDT), dự kiến `05:00Z` mùa đông (EST) — phần mùa đông ASSUMED; probe ghi `t` của một bar tháng 1 và một bar tháng 7/2017. Liên quan trực tiếp tới việc `.venv` thiếu `tzdata` (OBSERVED bởi main session): code parse phải chuyển `t` → ngày New York bằng `zoneinfo`, nên `tzdata` cần vào dependency.
- Bar chỉ được phát ra khi không trường nào trong o/h/l/c/v bằng 0 ("the bar is only emitted if none of its fields (open, high, low, close, volume) are 0") — VERIFIED FAQ. Ngày không có giao dịch hợp lệ sẽ thiếu bar, không phải bar rỗng.
- Lỗi được tài liệu hoá: 400, 401, 403, 429, 500; header `X-RateLimit-Limit` ("Request limit per minute"), `X-RateLimit-Remaining`, `X-RateLimit-Reset` ("The UNIX epoch when the remaining quota changes") — VERIFIED `reference/stockbars.md` (components.headers và responses).

## 2. Gói Basic có lấy được `feed=sip` cho bar ngày 2017 không?

**Kết luận theo tài liệu: CÓ, miễn là `end` cũ hơn 15 phút. Mức tin cao (nhiều nguồn Alpaca), nhưng vẫn là ASSUMED cho tới khi probe chạy.**

| # | Nguồn | Nội dung nguyên văn | Độ tin |
|---|---|---|---|
| 1 | Docs FAQ (VERIFIED https://docs.alpaca.markets/us/docs/market-data-faq) | "For historical queries, the `end` parameter must be at least 15 minutes old to query SIP data without a subscription." | Chính thức, updatedAt 2026-09-21 |
| 2 | About Market Data API (VERIFIED https://docs.alpaca.markets/docs/about-market-data-api) | Basic: Historical limitation "latest 15 minutes"; Historical timeframe "Since 2016" | Chính thức |
| 3 | Forum, staff Dan_Whitnable_Alpaca, 2024-05-29 (VERIFIED https://forum.alpaca.markets/t/cant-read-spy-minutes/14293) | "One can access any SIP data older than 15 minutes, just not current real-time data." | Nhân viên Alpaca, 2024 |
| 4 | Forum, staff, 2024-03-13 (VERIFIED https://forum.alpaca.markets/t/paper-account-data-query-denied/13822) | "if one is using the free market data then only historical SIP data can be fetched"; người hỏi dùng "paper account and ... the free data plan" | Nhân viên Alpaca |
| 5 | Alpaca Learn, 2021-02-27 (VERIFIED https://alpaca.markets/learn/fetch-historical-data) | "Data older than 15 minutes is accessible on all feeds." | Bài hướng dẫn; 2021-02-27 là ngày đăng gốc, nội dung đã được cập nhật sau (nhắc feed `boats`/`overnight`) |

Mâu thuẫn: trang Historical Stock Data nói về `iex` "This is the only feed that can be used without a subscription" (VERIFIED https://docs.alpaca.markets/us/docs/historical-stock-data-1, updatedAt 2026-02-11). Câu này trái với nguồn 1–5; nhiều khả năng nó chỉ nói về dữ liệu real-time/latest (ASSUMED). Chỉ probe phân xử được → bước SPY 2017 `feed=sip` là load-bearing.

**Khi bị từ chối.** Body: `{"code":42210000,"message":"subscription does not permit querying recent SIP data"}` — VERIFIED FAQ (ví dụ với `/v2/stocks/AAPL/trades/latest?feed=sip`). HTTP status: KHÔNG trang nào nêu. FAQ đặt thông điệp này trong checklist dưới tiêu đề "Why am I getting HTTP 403 (Forbidden)?", còn mã `42210000` gợi ý 422 → status thật ASSUMED; probe ghi lại.

**Mặc định `feed` mâu thuẫn trong chính tài liệu Alpaca:**
- schema OpenAPI: `"default": "sip"` (VERIFIED `reference/stockbars.md`, `reference/stockbarsingle-1.md`);
- FAQ: "The default value for `feed` is always the "best" available feed based on the user's subscription" (VERIFIED);
- forum 2024-03-11: người dùng gói free thấy request "suddenly ... routed to SIP"; staff khuyên "always explicitly include a feed parameter" (VERIFIED forum 13822).
→ Chính sách: probe và client Phase 1 LUÔN truyền tường minh `feed`, `adjustment`, `asof`, và ghi URL đầy đủ.

**Clock skew là rủi ro thật cho probe.** Forum 2024-08-04, staff: "The issue is probably your local machine’s clock is not synched" khi gặp lỗi 42210000; staff khác 2024-08-05: "you can simply leave it out" (bỏ `end`, để server tự tính) — VERIFIED https://forum.alpaca.markets/t/subscription-does-not-permit-querying-recent-sip-data/14751. Máy dev đang tắt w32time (OBSERVED bởi main session). Quy tắc: probe dùng `start`/`end` dạng `YYYY-MM-DD` cố định hoàn toàn trong quá khứ, KHÔNG tính `end` từ đồng hồ máy. Điều này cũng làm URL tái lập được.

## 3. `adjustment=all` so với `raw`: ngày split và tỉ lệ kỳ vọng

Ngữ nghĩa adjustment chỉ có MỘT nguồn chính (reference stockbars): "`split`: adjust price and volume for forward and reverse stock splits", "`dividend`: adjust price for cash dividends", "`spin-off`: adjust price for spin-offs", "`all`: apply all above adjustments", default `raw`. FAQ và bài Learn không bàn về adjustment; không tìm được nguồn thứ hai/ba. Cách điều chỉnh cổ tức (hệ số nhân hay trừ) không được tài liệu hoá → ASSUMED.

**Ngày split (ngày đầu giao dịch theo giá đã chia = ex-date):**

| Mã | Tỉ lệ | Record | Phân phối | Ngày đầu split-adjusted | Bằng chứng |
|---|---|---|---|---|---|
| AAPL | 4:1 | 2020-08-24 | 10-K: "effected ... on August 28, 2020" | 2020-08-31 | VERIFIED sec.gov 10-Q https://www.sec.gov/Archives/edgar/data/320193/000032019320000062/aapl-20200627.htm và 10-K FY2020 https://www.sec.gov/Archives/edgar/data/320193/000032019320000096/aapl-20200926.htm (qua snippet tìm kiếm, chưa mở toàn văn) + appleinsider/fool (thứ cấp) |
| NVDA | 10:1 | 2024-06-06 | sau đóng cửa 2024-06-07 | 2024-06-10 | VERIFIED https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2025: "Trading is expected to commence on a split-adjusted basis at market open on Monday, June 10, 2024." + nasdaq.com (thứ cấp) |
| TSLA | 3:1 | 2022-08-17 | sau đóng cửa 2022-08-24 | 2022-08-25 | Trang IR gốc https://ir.tesla.com/press-release/tesla-announces-three-one-stock-split trả 403 khi fetch; VERIFIED (thứ cấp, trích nguyên văn thông cáo) businesswire/nasdaq.com/driveteslacanada |
| GOOGL | 20:1 | 2022-07-01 | sau đóng cửa 2022-07-15 | 2022-07-18 | Tỉ lệ/record/phân phối: trích filing qua snippet; ngày 2022-07-18: VERIFIED chỉ từ thứ cấp (Morningstar, Nasdaq, capital.com) |

Probe nên tự xác nhận các ngày này bằng endpoint corporate actions của chính Alpaca (mục 4): `forward_splits` có `old_rate`, `new_rate`, `ex_date`.

**Kỳ vọng, đặt T = ngày đầu split-adjusted, N = tỉ lệ:**
- `raw`: `c(T-1)/c(T)` ≈ N × (1 + lợi suất ngày T) → trong khoảng N×[0.85, 1.15]; `v(T)/v(T-1)` tăng cỡ N lần (biên độ: PRIOR).
- `split`: `c(T-1)/c(T)` ≈ 1 (lợi suất ngày bình thường); mọi bar trước T có giá = raw/N và volume = raw×N (theo định nghĩa trong reference; độ chính xác làm tròn ASSUMED).
- `all` so với `split`: khác nhau bởi hệ số cổ tức tích luỹ của mọi ex-date cổ tức SAU bar đó → với AAPL, NVDA và GOOGL (bắt đầu trả cổ tức năm 2024 — PRIOR) tỉ lệ `raw/all` trước T hơi lớn hơn N, không đúng bằng N. TSLA không trả cổ tức tiền mặt (PRIOR) → TSLA là case sạch nhất cho `raw` vs `all`.
- Vì vậy probe gọi BA biến thể `raw`, `split`, `all` cho mỗi cửa sổ T−7..T+7 ngày lịch.
- SPY: không split, có cổ tức quý → `raw/all` của bar 2017-01-03 > 1; kiểm tra trực tiếp rằng `all` có điều chỉnh cổ tức (kỳ vọng PRIOR).
- Hệ quả thiết kế đã có trong roadmap: label dùng `adjustment=all`, lưu raw riêng (docs/project-roadmap.md dòng 418). Chuỗi `all` thay đổi mỗi khi có cổ tức mới → bar đã điều chỉnh không bất biến; chỉ raw là append-only (suy luận, ASSUMED là cần cho PIT).

## 4. Mã đã huỷ niêm yết, symbol reuse, và tra asset không dùng host trading

**Ứng viên (ngày giao dịch cuối):**

| Mã | Sự kiện | Ngày giao dịch cuối trên sàn | Bằng chứng | Ghi chú |
|---|---|---|---|---|
| TWTR | Sáp nhập tiền mặt $54.20 (X Holdings II), hiệu lực 2022-10-27 | 2022-10-27 (NYSE ngừng giao dịch trước giờ mở 2022-10-28; rút niêm yết hiệu lực 2022-11-08) | VERIFIED NYSE Form 25-NSE https://www.sec.gov/Archives/edgar/data/1418091/000087666122000890/ruleprovisionnotice.htm và SC 13D https://www.sec.gov/Archives/edgar/data/1418091/000119312522274034/d393652dsc13d.htm (qua snippet); thứ cấp finder.com, xtb.com | Không lên OTC (PRIOR) → case sạch |
| ATVI | Microsoft mua, $95.00 tiền mặt, hoàn tất 2023-10-13 | 2023-10-12 (công ty yêu cầu Nasdaq halt trước giờ mở ngày đóng giao dịch) | VERIFIED thứ cấp (tipranks/The Fly trích filing); 8-K gốc chưa mở → ngày cuối ASSUMED | Case sạch |
| SIVB | Nasdaq halt 2023-03-10, suspend 2023-03-28, Form 25 hiệu lực 2023-05-12; sau đó OTC `SIVBQ` | 2023-03-09 (ASSUMED: nguồn chỉ xác nhận halt ngày 2023-03-10, chưa rõ có giao dịch trước halt hay không) | VERIFIED 8-K https://www.sec.gov/Archives/edgar/data/719739/000119312523073665/d485308d8k.htm và Form 25-NSE https://www.sec.gov/Archives/edgar/data/719739/000135445723000327/sivbdelistreason.txt (qua snippet); thứ cấp investorplace | Phần OTC: Alpaca không phục vụ cho user thường |
| FRC | Ngân hàng bị đóng 2023-05-01; NYSE "suspend trading immediately" | ≈ 2023-04-28 (thứ Sáu) — ASSUMED; probe xác nhận bằng bar cuối | VERIFIED thông báo NYSE https://s2.q4cdn.com/154085107/files/doc_news/NYSE-to-Suspend-Trading-Immediately-in-First-Republic-Bank-FRC-and-Commence-Delisting-Proceedings-2023.pdf (qua snippet) | Có OTC sau đó (ticker OTC chưa xác nhận) |

Khuyến nghị chọn 3: TWTR, ATVI (sáp nhập tiền mặt, sạch) + SIVB (phá sản → OTC, case khó). FRC dự phòng.

**Alpaca có phục vụ bar cho asset không còn hoạt động không?** Tài liệu im lặng; bằng chứng gián tiếp:
- FAQ "Why can't I find market data for a particular symbol": "Make sure the asset is active. Check the `status` field" (VERIFIED FAQ) → gợi ý asset inactive có thể không có dữ liệu.
- OTC: "Market data for OTC symbols can only be queried with a special subscription currently available only for broker partners" (VERIFIED FAQ).
- Forum 2025-12-18, staff: "at the moment we do not have an endpoint for historical assets"; "Another example is a stock which becomes de-listed. The original symbol is no longer valid, so Alpaca will replace the symbol with a number, or more typically, append a suffix to the symbol." (VERIFIED https://forum.alpaca.markets/t/delisted-tickers/18227). Thread không trả lời bar có tồn tại hay không.
- Forum 2022-07-25: không có bar cho WRCDF vì là OTC (VERIFIED https://forum.alpaca.markets/t/get-historical-data-for-inactive-stocks/10097).
→ Độ phủ delisted là ASSUMED-không-chắc; đúng như roadmap, chỉ probe phân xử.

**Ma trận `asof` bắt buộc.** Doc `asof`: "Data is returned based on the symbol alone without looking up previous names. The same happens if the queried symbol is not found on the given `asof` date", và forum nói mã delisted bị đổi thành suffix/số. Vì vậy mỗi mã delisted gọi 3 biến thể (cùng `feed=sip`, `adjustment=raw`, cửa sổ [last−30 ngày, last+30 ngày]):
- (a) không truyền `asof` (mặc định hôm nay);
- (b) `asof=<ngày giao dịch cuối>`;
- (c) `asof=-`.
Phân loại: có bar ở cả ba → phục vụ đầy đủ; chỉ (b)/(c) có → lookup hôm nay thất bại do symbol đã bị đổi; không biến thể nào có → không phủ (ghi caveat survivorship). Bar xuất hiện SAU ngày giao dịch cuối với `asof=-` → dấu hiệu symbol bị tái sử dụng hoặc forward-fill (forum 18227 có lời kể người dùng về dữ liệu "forward filled" sau khi mất niêm yết).

**Symbol reuse.** Rủi ro thật cho survivorship: ticker cũ có thể được gán cho công ty khác (ví dụ kinh điển: SBC dùng ticker `T` sau khi mua AT&T năm 2005 — PRIOR). Tôi KHÔNG có nguồn về việc TWTR/ATVI/SIVB/FRC đã bị gán lại hay chưa → Câu hỏi mở. Khoá danh tính phải là CIK (EDGAR) + CUSIP, không phải ticker (khớp `refdata/` Phase 1, roadmap dòng 419).

**Tra asset mà không chạm host trading.** Endpoint `/v2/assets/{symbol}` chỉ có server trên host trading (OBSERVED: `reference/get-v2-assets-symbol_or_asset_id.md` khối `servers` chỉ liệt kê host paper và live của trading API; ví dụ CGRNQ trong FAQ cũng dùng host trading — chỉ là bằng chứng, URL đó KHÔNG được xuất hiện trong `scripts/`). Guard `tests/test_no_order_execution.py` chặn đúng mẫu host đó. Thay thế trên host data:
- `GET https://data.alpaca.markets/v1/corporate-actions?symbols=TWTR,ATVI,SIVB,FRC&types=cash_merger,stock_merger,stock_and_cash_merger,name_change,worthless_removal&start=2015-01-01&end=2026-10-21&limit=1000` — VERIFIED https://docs.alpaca.markets/reference/corporateactions-1 (servers `https://data.alpaca.markets`; types gồm `forward_split`, `reverse_split`, `cash_merger`, `stock_merger`, `name_change`, `worthless_removal`, ...; `cash_mergers` có `acquiree_symbol`, `acquiree_cusip`, `effective_date`; `name_changes` có `old_symbol`, `new_symbol`, `old_cusip`, `new_cusip`).
- `start` và `end` đều "Default: current day" → PHẢI truyền khoảng tường minh. Độ sâu lịch sử không được nêu → probe ghi `process_date` sớm nhất. Doc cảnh báo "no guarantees on the creation time of corporate actions" → không dùng làm nguồn PIT.
- Gói Basic/paper có gọi được endpoint này không: không trang nào nói → ASSUMED; probe ghi status.
- Cùng endpoint với `types=forward_split&symbols=AAPL,NVDA,TSLA,GOOGL&start=2020-01-01&end=2024-12-31` để kiểm tra chéo bảng split ở mục 3.
- Nguồn danh tính ngoài Alpaca: EDGAR submissions (`formerNames`, CIK) — đã nằm trong kế hoạch Phase 1.

## 5. Rate limit 200 lần/phút

- Basic "Historical API calls: 200 / min" — VERIFIED https://docs.alpaca.markets/docs/about-market-data-api; AGENTS.md đã ghi VERIFIED.
- Header `X-RateLimit-Limit/Remaining/Reset`, 429 khi vượt — VERIFIED reference stockbars.
- Probe cần khoảng 30 request (mục 8) → không chạm giới hạn. Quy tắc an toàn: một luồng, nghỉ ≥ 0.5 s giữa các request (≤ 120/phút); nếu `X-RateLimit-Remaining` ≤ 5 hoặc gặp 429 thì ngủ tới `X-RateLimit-Reset` + 1 s, tối đa 3 lần thử lại; ghi `X-RateLimit-Limit` thật để xác nhận con số 200.
- Cho backfill Phase 1 (~500 mã × 2016–nay): endpoint đa mã với `limit=10000` (limit tính tổng mọi mã) → ước lượng ~500×2,700 bar ≈ 1.35 triệu bar ≈ 135 trang, dưới 2 phút ở 120 req/phút (số học thô, ASSUMED về số bar/trang thực tế).

## 6. Guard `tests/test_no_order_execution.py` cấm gì

OBSERVED (đọc file trong worktree, commit 9ba4562). Quét `src/**/*.py`, `scripts/**/*.py`, `scripts/**/*.sh`, `tests/**/*.py` (trừ chính file guard), từng dòng, không phân biệt hoa thường, kể cả comment và docstring. Các regex (nguyên văn):

```
TradingClient | submit_order | alpaca\.trading | place_\w*order | fc[\s_-]?trading |
(paper-)?api\.alpaca\.markets | /v2/orders | /v2/positions |
alpaca[_-]trade[_-]api | close_\w*position | cancel_\w*order
```

Hệ quả cho probe:
- Chỉ dùng chuỗi host `data.alpaca.markets` (không khớp regex host vì không chứa `api.`). Không viết URL assets/account, không nhắc tên client trading, kể cả trong comment.
- Không import alpaca-py (`alpaca.trading` nằm cùng package) và không import SDK cũ.
- Tránh tên hàm kiểu `place_*order`, `cancel_*order`, `close_*position`; đặt tên `fetch_bars`, `fetch_corporate_actions`.
- Đặt script trong `scripts/spike/` (gợi ý) để guard TỰ ĐỘNG quét nó — chặt hơn đặt ngoài `scripts/`. Bài Learn chính thức của Alpaca dùng client trading trong ví dụ (VERIFIED https://alpaca.markets/learn/fetch-historical-data: option contracts "through `TradingClient`") → không copy mẫu từ đó.
- Guard không bắt `data.alpaca.markets/v2/stocks/...`; không cần sửa guard.

## 7. alpaca-py `StockHistoricalDataClient` hay HTTP thuần?

| Tiêu chí | urllib (stdlib) | alpaca-py 0.44.0 |
|---|---|---|
| Phụ thuộc | 0 | OBSERVED `https://pypi.org/pypi/alpaca-py/json`: `msgpack`, `pandas>=1.5.3`, `pydantic<3`, `pytz`, `requests`, `sseclient-py`, `websockets`; Apache-2.0; Python >=3.10 |
| Code trading lên đĩa | không | có (`alpaca.trading` cùng package; guard chỉ cấm import, nhưng tăng bề mặt rủi ro) |
| Ghi raw body + sha256 + header rate-limit | trực tiếp | client parse sang model/DataFrame, phải đào lại response gốc (PRIOR) |
| Phân trang | tự viết ~10 dòng | có sẵn |
| Feed/adjust mặc định | tường minh | mặc định SDK có thể khác server (PRIOR) |
| Rủi ro phiên bản | không | SDK 0.x (PRIOR: đổi API thường xuyên) |

Khuyến nghị: urllib thuần. Probe cần đúng cái SDK che đi: URL thật, status, header, byte gốc. Phase 1 cũng nên giữ HTTP thuần cho client daily-bar (roadmap dòng 223 đã ghi "Data endpoints only").

## 8. Probe phải ghi gì để AGENTS.md có dòng VERIFIED/OBSERVED

**Mỗi request một dòng JSONL** (thư mục gitignored, ví dụ `data/raw/spike/alpaca/<UTC-date>/requests.jsonl` + body gốc `.json.gz`; OBSERVED `git check-ignore -v data/raw/spike/alpaca/x.json.gz` → `.gitignore:32:/data/`):
`requested_at_utc` (đồng hồ máy, kèm cờ `clock_synced` vì w32time đang tắt), `key_id_prefix` (4 ký tự đầu, KHÔNG ghi secret), `method`, `url` đầy đủ (key nằm ở header, không ở URL), `http_status`, `x_ratelimit_limit/remaining/reset`, `body_sha256`, `body_bytes`, `error_code`/`error_message` nếu có, `bar_count` theo mã, `first_t`, `last_t`, `next_page_token_present`, `pages`.

**Danh sách request (~30):**
1. SPY 2017, `feed=sip`, `adjustment=raw`, `start=2017-01-01`, `end=2017-12-31`, `asof=-`, `limit=10000` → kỳ vọng 251 bar (số phiên NYSE 2017: PRIOR). Lặp với `feed=iex` để so `v`, `n` (FAQ cho thấy IEX ít hơn nhiều).
2. SPY 2017 `adjustment=all` → `raw/all` ngày đầu và cuối năm.
3. 4 mã split × {raw, split, all}, cửa sổ T−7..T+7, `feed=sip`, `asof=-` = 12 request.
4. 3 mã delisted × {asof mặc định, asof=ngày cuối, asof=-} = 9 request.
5. Corporate actions: 1 request merger/name-change cho mã delisted, 1 request forward_split cho 4 mã split.
6. Một request lấy HTTP status thật của `42210000`: ví dụ của FAQ `GET https://data.alpaca.markets/v2/stocks/AAPL/trades/latest?feed=sip` (endpoint đọc dữ liệu).

**Ba dòng AGENTS.md (mẫu, điền sau probe):**
- `Alpaca SIP history on Basic (OBSERVED <date>, paper keys)`: `GET /v2/stocks/SPY/bars?feed=sip&timeframe=1Day&start=2017-01-01&end=2017-12-31&adjustment=raw&asof=-` → HTTP <s>, <n> bars, first t <..>, last t <..>; X-RateLimit-Limit <..>. Doc nền VERIFIED https://docs.alpaca.markets/us/docs/market-data-faq.
- `Alpaca adjustment (OBSERVED <date>)`: raw c(T−1)/c(T) cho AAPL 2020-08-31 / NVDA 2024-06-10 / TSLA 2022-08-25 / GOOGL 2022-07-18 = <..>; split-adjusted = <..>; hệ số all/split = <..>; SPY 2017-01-03 raw/all = <..>.
- `Alpaca delisted coverage (OBSERVED <date>)`: TWTR/ATVI/SIVB × 3 biến thể asof → bar count + last t; corporate-actions trả <..>; kết luận phủ/không phủ, caveat survivorship.

**Chính sách lưu raw:** Không commit body Alpaca vào repo public — điều khoản tái phân phối dữ liệu Alpaca là ASSUMED (roadmap dòng 168: "Personal use, no redistribution (ASSUMED)"). Fixture cho test split (Phase 1) nên dùng số tổng hợp hoặc chỉ tỉ lệ dẫn xuất.

---

## Kết luận (xếp hạng)

1. **SIP lịch sử trên Basic gần như chắc chắn dùng được cho 2017** (FAQ, trang plan, 2 câu trả lời staff, bài Learn), chỉ bị chặn ở 15 phút gần nhất. Vẫn ASSUMED tới khi probe chạy; trang Historical Stock Data có câu mâu thuẫn. → Plan: giữ bước SPY 2017 `feed=sip` làm gate; nếu thất bại, Phase 1 dùng `feed=iex` và ghi caveat volume (IEX "approximately ~2.5% of the market volume" — VERIFIED historical-stock-data-1).
2. **Luôn truyền tường minh `feed`, `adjustment`, `asof`, `start`, `end` (ngày cố định trong quá khứ)**; không suy `end` từ đồng hồ máy. Lý do: mặc định `feed` mâu thuẫn trong doc, và lỗi 42210000 có tiền lệ do lệch đồng hồ trong khi máy dev tắt w32time.
3. **Probe bằng urllib thuần, đặt ở `scripts/spike/` để guard quét**, chỉ host `data.alpaca.markets`, không alpaca-py; tránh mọi chuỗi khớp regex guard, kể cả trong comment.
4. **Tra delisting/đổi tên qua `GET https://data.alpaca.markets/v1/corporate-actions`** (khoảng ngày tường minh), không qua endpoint assets (chỉ có trên host trading, bị guard chặn). Danh tính dài hạn = CIK + CUSIP.
5. **Kiểm tra adjustment bằng ba biến thể raw/split/all**; TSLA 2022-08-25 là case sạch nhất; SPY 2017 raw/all kiểm tra cổ tức. Ngữ nghĩa adjustment chỉ có một nguồn → kết quả probe là bằng chứng chính.
6. **Delisted: ma trận 3 biến thể `asof`** cho TWTR, ATVI, SIVB (FRC dự phòng). Không có bar → ghi caveat survivorship như roadmap §4, không mua dữ liệu ($0).
7. **Lưu raw body + sha256 dưới `/data/` (gitignored)**, không commit dữ liệu Alpaca; chỉ đưa tỉ lệ/đếm dẫn xuất vào AGENTS.md.
8. **Thêm `tzdata` vào dependency** trước khi code nào parse `t` của bar ngày theo America/New_York.

Bước chặn của owner (không giả định là đã có): Alpaca paper keys trong `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`). Không có key → toàn bộ mục này dừng ở ASSUMED.

## Câu hỏi mở

Chỉ probe thật (cần paper keys) trả lời được. Header chung: `-H "APCA-API-KEY-ID: $ALPACA_API_KEY" -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"`.

1. Basic có trả SIP 2017 không, bao nhiêu bar? → `curl -s -D - "https://data.alpaca.markets/v2/stocks/SPY/bars?feed=sip&timeframe=1Day&start=2017-01-01&end=2017-12-31&adjustment=raw&asof=-&limit=10000"`; ghi status, số bar, `t` đầu/cuối, `X-RateLimit-Limit`.
2. HTTP status thật của `42210000`? → `curl -s -o /dev/null -w "%{http_code}" "https://data.alpaca.markets/v2/stocks/AAPL/trades/latest?feed=sip"`.
3. `t` bar ngày mùa đông có là `05:00:00Z` không? → trong kết quả câu 1, đọc `t` của 2017-01-03 và 2017-07-03.
4. `adjustment` đúng tỉ lệ? → với (mã, T) ∈ {(AAPL, 2020-08-31), (NVDA, 2024-06-10), (TSLA, 2022-08-25), (GOOGL, 2022-07-18)} và `adj` ∈ {raw, split, all}: `.../v2/stocks/<SYM>/bars?feed=sip&timeframe=1Day&start=<T−7d>&end=<T+7d>&adjustment=<adj>&asof=-`; tính `c(T−1)/c(T)` và `v(T)/v(T−1)`.
5. Có bar cho mã delisted không, symbol có bị đổi/tái sử dụng? → với SYM ∈ {TWTR (last 2022-10-27), ATVI (2023-10-12), SIVB (2023-03-09)} và `asof` ∈ {bỏ trống, <last>, -}: `.../v2/stocks/bars?symbols=<SYM>&feed=sip&timeframe=1Day&start=<last−30d>&end=<last+30d>&adjustment=raw&asof=<..>`; bar sau ngày last với `asof=-` = dấu hiệu tái sử dụng/forward-fill.
6. Paper keys/Basic có gọi được corporate actions và lịch sử sâu bao nhiêu? → `curl -s "https://data.alpaca.markets/v1/corporate-actions?symbols=TWTR,ATVI,SIVB,FRC&types=cash_merger,stock_merger,stock_and_cash_merger,name_change,worthless_removal&start=2015-01-01&end=2026-10-21&limit=1000"` và `...?symbols=AAPL,NVDA,TSLA,GOOGL&types=forward_split&start=2020-01-01&end=2024-12-31`; ghi status, `process_date` sớm nhất, `old_rate/new_rate/ex_date`.
7. Ngày giao dịch cuối của FRC và ATVI (hiện ASSUMED/thứ cấp)? → bar cuối từ câu 5, đối chiếu 8-K/Form 25 trên EDGAR.
8. Điều khoản dữ liệu Alpaca có cấm lưu/commit fixture không? → owner đọc Market Data Agreement trong dashboard Alpaca (API không trả lời được); tới lúc đó coi là cấm tái phân phối.
9. Chênh lệch IEX vs SIP cho SPY 2017? → so `v`, `n` giữa câu 1 và cùng request với `feed=iex`.

## Giới hạn của research này

- Không có key → không quan sát được phản hồi API nào; mọi hành vi API là ASSUMED dù có nguồn VERIFIED.
- Ngữ nghĩa `adjustment` chỉ có một nguồn chính; ngày split TSLA/GOOGL dựa vào nguồn thứ cấp (trang IR Tesla trả 403); một số filing SEC chỉ đọc qua snippet tìm kiếm.
- Không đọc Market Data Agreement của Alpaca.
- Không tìm nguồn về việc TWTR/ATVI/SIVB/FRC có bị gán lại ticker.
- File này nằm trong worktree; hook chặn ghi vào main checkout `H:\study\Project\FinSec\plans\...` (đã tạo sẵn thư mục rỗng `research/` ở đó trước khi bị chặn).

Nguồn chính: https://docs.alpaca.markets/reference/stockbars · https://docs.alpaca.markets/us/docs/market-data-faq · https://docs.alpaca.markets/docs/about-market-data-api · https://docs.alpaca.markets/us/docs/historical-stock-data-1 · https://docs.alpaca.markets/reference/corporateactions-1 · https://forum.alpaca.markets/t/cant-read-spy-minutes/14293 · https://forum.alpaca.markets/t/paper-account-data-query-denied/13822 · https://forum.alpaca.markets/t/subscription-does-not-permit-querying-recent-sip-data/14751 · https://forum.alpaca.markets/t/delisted-tickers/18227 · https://forum.alpaca.markets/t/get-historical-data-for-inactive-stocks/10097 · https://alpaca.markets/learn/fetch-historical-data · https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2025 · https://pypi.org/pypi/alpaca-py/json
