# Research: Alpaca stock trade/quote fields, `t` format, exchange codes, quote sizes (2026-10-21)

## Tóm tắt điểm quan trọng (đọc trước)
- **`bs`/`as` KHÔNG còn là round lots.** Từ **2025-11-03** Alpaca hiển thị size quote CTA/UTP theo **shares**. Trang WS vẫn ghi "round lots" (cũ); reference OpenAPI + changelog nói shares. Giả định "đổi khoảng 2023-2024" trong đề bài là sai: staff forum vẫn xác nhận "still in lots" tới 2025-02. VERIFIED [CL][HQ][LQ][F3][F4]
- **Feed IEX (gói Basic) có đổi đơn vị cùng ngày không thì không nguồn nào nói** (changelog chỉ nói CTA/UTP). ASSUMED shares → phải đo bằng key thật.
- **`bp`/`ap` = 0 là hợp lệ** ("0 means the security has no active bid/ask") → quote một phía, không phải thiếu field. VERIFIED [HQ][LQ]
- **`t` có số chữ số thập phân thay đổi** (0/2/3/6/8/9 đã thấy). Python 3.10 `fromisoformat` từ chối cả "Z" lẫn số chữ số ≠ 3/6 → cần parser riêng. OBSERVED 2026-10-21 (probe bên dưới).
- **`t` chỉ là string khi dùng JSON.** alpaca-py dùng `Content-Type: application/msgpack` và gọi `msg["t"].to_datetime()` (msgpack Timestamp). VERIFIED [PY-WS][ST]

## 1. Field của message WS trade (`"T":"t"`) và quote (`"T":"q"`)
- Trade: `T, S, i, x, p, s, c, t, z` — `t` = "RFC-3339 formatted timestamp with nanosecond precision", `s` = "trade size" (không ghi đơn vị). VERIFIED [RT]
- Quote: `T, S, ax, ap, as, bx, bp, bs, c, t, z` — `as`/`bs` = "ask/bid size in round lots" (trang WS, `updatedAt: 2026-05-18`). VERIFIED [RT]
- Ví dụ verbatim [RT]:
  - `{"T": "t", "i": 96921, "S": "AAPL", "x": "D", "p": 126.55, "s": 1, "t": "2021-02-22T15:51:44.208Z", "c": ["@", "I"], "z": "C"}`
  - `{"T": "q", "S": "AMD", "bx": "U", "bp": 87.66, "bs": 1, "ax": "Q", "ap": 87.68, "as": 4, "t": "2021-02-22T15:51:45.335689322Z", "c": ["R"], "z": "C"}`
- Trang WS **không có danh sách required**. Danh sách required chỉ có trong OpenAPI historical: quote `["t","bx","bp","bs","ap","as","ax","c","z"]`, trade `["t","i","x","p","s","c","z"]`; trade historical có thêm `u` tuỳ chọn ("canceled" / "incorrect" / "corrected"). VERIFIED [HQ][HT]. Việc "WS luôn gửi đủ các field này" là ASSUMED (suy theo historical).
- Quote `c`: "If the array contains one flag, it applies to both the bid and ask. If the array contains two flags, the first one applies to the bid and the second one to the ask." VERIFIED [HQ]
- alpaca-py map `t→timestamp, ax→ask_exchange, ap→ask_price, as→ask_size, bx, bp, bs, c→conditions, z→tape`; trade `t,p,s,x,i,c,z`. Model `Quote`: `bid_exchange`/`ask_exchange`/`conditions`/`tape` là `Optional[...] = None`, còn `bid_size`/`ask_size` là `float` bắt buộc. VERIFIED [PY-M][PY-Q]

### Format `t`
- Các mẫu thật/tài liệu: `.208Z` (3), `.335689322Z` (9), cancel `13:15:42.83540958Z` (8), bar `19:15:00Z` (0) [RT]; `2021-02-06T13:35:08.946977536Z` [HQ]; fixture SDK có `05:00:16.91Z` (2), `.547942Z` (6), `.000059Z` (6) [PY-T]. → **Độ dài phần thập phân thay đổi, 0-9 chữ số, luôn "Z" trong mọi mẫu.** VERIFIED (3 nguồn: [RT], [HQ], [PY-T]).
- "Bỏ số 0 ở cuối" là suy luận từ các mẫu trên. Cơ chế giống Go `RFC3339Nano = "2006-01-02T15:04:05.999999999Z07:00"`, "printed to the given number of decimal places, with trailing zeros removed" [GO]; việc server Alpaca dùng Go/RFC3339Nano là ASSUMED.
- Luôn "Z"? Mọi mẫu đều "Z"; không có câu nào trong docs cam kết điều đó → parser vẫn nên nhận `±HH:MM`. ASSUMED.

## 2. Mã sàn `x` / `bx` / `ax` — IEX = `"V"`
- `/v2/stocks/meta/exchanges` (OpenAPI example): `{"N": "New York Stock Exchange", "V": "IEX"}`; `ax`/`bx`/`x`: "See `v2/stocks/meta/exchanges` for more details." VERIFIED [EX][HQ][HT]
- alpaca-py `class Exchange(str, Enum)`: `V (str): IEX`, cùng `Z, I, M, U, L, W, X, B, D, J, P, Q, S, A, E, N, T, Y, C, H, K`. VERIFIED [PY-E]
- CTA Symbol File Spec v1.2 (2026-01-29), bảng Participant ID: `V  IEX`, và có mã mới `F  Texas Stock Exchange` (thêm v1.1, 08/01/2025), `G  24X`, `M  NYSE Texas`. VERIFIED [CTA]. Bảng này là "Primary Listing Market Participant ID", chưa chắc trùng 100% với mã trong message (ASSUMED trùng).
- Hệ quả: enum alpaca-py đã cũ (`M` = "Chicago Stock Exchange", không có `F`/`G`) → **không hard-code enum đóng**; lưu chuỗi thô, map tên qua `/v2/stocks/meta/exchanges` lúc chạy. ASSUMED (khuyến nghị).

## 3. Đơn vị `bs`/`as`, có luôn có mặt không, có bằng 0 không
- Changelog "CTA/UTP bid and ask size display change": SIP sẽ "represent quotation sizes ... in terms of the number of shares, rounded down to the nearest multiple of the round lot size assigned to the security"; "Activation will take place on Monday, November 3, 2025."; "Alpaca will display the CTA/UTP (US stock) quote sizes in shares, instead of round lots, starting November 3, 2025." VERIFIED [CL]
- Reference OpenAPI (`updatedAt: 2026-05-27`): `as` = "Ask size in shares (round lots prior to November 3, 2025).", `bs` tương tự, kiểu `uint32`. VERIFIED [HQ][LQ]
- Trước đó: staff "Gergely_Alpaca" — 2022-12-12 "The bid & ask sizes represent the number of round lot orders" [F1]; 2023-08-31 "Quote sizes in our API are reported in lots" (kể cả ví dụ feed IEX: 300 shares → 3) [F2]; 2025-01-13 "Nothing has changed ... the quote sizes are still in lots" [F3]; 2025-02-03 "15 * 100 = 1500 shares" [F4]. VERIFIED
- Nguồn độc lập: Massive.com cùng đổi ngày 2025-11-03, trích Release No. 34-101070 (2024-09-18), và tạo lại Flat Files lịch sử theo shares. VERIFIED [MS]. Alpaca có restate dữ liệu historical trước 2025-11-03 không: câu "(round lots prior to November 3, 2025)" gợi ý **không** → historical bị lẫn hai đơn vị. ASSUMED.
- Round lot giờ không còn luôn là 100 (bậc theo giá, cập nhật tháng 5 và tháng 11) [CL]; CTA symbol file có cột "Round Lot Size" [CTA]. VERIFIED
- Trang WS [RT] vẫn ghi "in round lots" → **hai trang tài liệu mâu thuẫn**; tin reference + changelog (mới hơn, cụ thể hơn). Feed IEX: không nguồn nào nói → ASSUMED.
- `bp`/`ap` = 0: "0 means the security has no active bid/ask." VERIFIED [HQ][LQ]. Áp dụng cho WS: ASSUMED.

## 4. Parser RFC-3339 an toàn cho Python 3.10 (chỉ stdlib)
- Docs 3.10: chỉ hỗ trợ `YYYY-MM-DD[*HH[:MM[:SS[.fff[fff]]]][+HH:MM[:SS[.ffffff]]]]` và "This does *not* support parsing arbitrary ISO 8601 strings". VERIFIED [PY310]
- Docs hiện tại: "Return a datetime corresponding to a *date_string* in any valid ISO 8601 format"; "Changed in version 3.11: Previously, this method only supported formats that could be emitted by date.isoformat() or datetime.isoformat()." VERIFIED [PYDOC]. Câu "anything beyond 6 will be truncated" chỉ nằm ở `time.fromisoformat`, không ở `datetime.fromisoformat` → việc 3.13 cắt 9 chữ số là OBSERVED, không phải doc-verified.
- **Probe** — OBSERVED 2026-10-21, `uv run --no-project --python 3.10 python -I probe.py` (3.10.21) và `python3.13 -I probe.py` (3.13.14), script ở `C:\Users\tqanh\AppData\Local\Temp\claude\h--study-Project-FinSec\3f230a0b-8542-4bab-912a-d96120eed79b\scratchpad\research-probe\alpaca-ts\probe.py`:

| Input | 3.10 stdlib | 3.13 stdlib | Parser dưới (cả 2 bản) |
|---|---|---|---|
| 9 số + Z `.335689322Z` | ValueError | `.335689+00:00` | `.335689+00:00`, ns=1614009105335689322 |
| 8 số + Z / 7 số + Z | ValueError | cắt còn µs | đúng µs; ns giữ đủ (…835409580 / …335689300) |
| 6 số + Z / 3 số + Z / không phân số + Z | ValueError (vì "Z") | OK | OK |
| 2 số `.91Z` / 1 số `.2Z` | ValueError | `.910000` / `.200000` | OK |
| 9 số + `+00:00` | ValueError | OK | OK |
| chữ thường `z` | ValueError | **ValueError** | OK |
| `-05:00` | ValueError | giữ tz `UTC-05:00` | đổi sang UTC `15:51:45.335689+00:00` |
| thiếu tz, 9 số | ValueError | **naive, không raise** | ValueError (đúng yêu cầu) |
| thiếu tz, 6 số | **naive, không raise** | **naive, không raise** | ValueError |

- Thêm (OBSERVED 2026-10-21, `uv run --no-project --python 3.10 python -I replace_z.py`): cách hiện tại `fromisoformat(s.replace("Z","+00:00"))` trên 3.10 chỉ chạy với 0/3/6 chữ số; hỏng với 1, 2, 7, 8, 9 — tức là hỏng với nhiều mẫu thật của Alpaca.
- Hai ts cách nhau 100 ns: `datetime` bằng nhau, `ns` vẫn phân biệt thứ tự ("µs-equal: True | ns-ordered: True"). OBSERVED 2026-10-21.
- Snippet (rút gọn từ `probe.py`, logic giữ nguyên):
```python
_RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$", re.IGNORECASE)
def parse_rfc3339(s: str) -> tuple[datetime, int]:
    m = _RFC3339.match(s)
    if m is None:
        raise ValueError(f"not an RFC-3339 timestamp with offset: {s!r}")
    base, frac, tz = m.groups()
    frac9 = (frac or "").ljust(9, "0")
    tz = "+00:00" if tz in ("Z", "z") else tz
    dt = datetime.fromisoformat(f"{base.upper()}.{frac9[:6]}{tz}").astimezone(timezone.utc)
    ns = int(dt.replace(microsecond=0).timestamp()) * 1_000_000_000 + int(frac9)
    return dt, ns
```
- Lưu ý: không xử lý leap second `:60` (PRIOR: RFC 3339 cho phép); input không phải `str` (msgpack) sẽ ném `TypeError` → kiểm tra `isinstance(s, str)` trước và raise rõ ràng. ASSUMED.
- Nên compile thêm cờ `re.ASCII`: không có cờ này, `\d` còn khớp chữ số Unicode, và `int()` vẫn đổi được chúng thành số. Probe: `.٣٣٥Z` (Arabic-Indic) khớp regex khi không có `re.ASCII`, không khớp khi có; `int('٣٣٥') == 335`. OBSERVED 2026-10-21, `python -I -c …` (3.13).

## 5. Condition `c` / tape `z` (ngắn)
- Tape: "A: New York Stock Exchange, B: NYSE Arca, Bats, IEX and other regional exchanges, C: NASDAQ, N: Overnight, O: OTC". Nghĩa của condition phụ thuộc tape (vd `B` = "Average Price Trade" ở AB nhưng "Bunched Trade" ở C). VERIFIED [HQ][FAQ]
- Bảng FAQ: `I` Odd Lot, `Z` Sold Out Of Sequence, `T`/`U` extended hours, `M`/`Q` official close/open, `9` Corrected Consolidated Close… mỗi mã ghi rõ có cập nhật open/close/high/low/volume hay không; latest-trades loại trade có `I` ("will never appear on this endpoint"). Cancel/correct đến qua message riêng `T:"x"` / `T:"c"` (tự subscribe theo kênh trades). VERIFIED [FAQ][LT][RT]
- Khuyến nghị: lưu thô `c`, `z`; lọc theo bảng FAQ ở tầng feature (không ở connector). ASSUMED.

## Nguồn (đã fetch 2026-10-21; bản `.md` của docs lấy bằng curl)
- [RT] https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data (updatedAt 2026-05-18) · [ST] https://docs.alpaca.markets/us/docs/streaming-market-data (2026-08-27)
- [HQ] https://docs.alpaca.markets/us/reference/stockquotes-1 (2026-05-27) · [LQ] https://docs.alpaca.markets/us/reference/stocklatestquotes-1 · [HT] https://docs.alpaca.markets/us/reference/stocktrades-1 · [LT] https://docs.alpaca.markets/us/reference/stocklatesttrades-1
- [EX] https://docs.alpaca.markets/us/reference/stockmetaexchanges-1 (2026-05-27) · [FAQ] https://docs.alpaca.markets/us/docs/market-data-faq (2026-09-21) · [CL] https://docs.alpaca.markets/us/changelog/marketdata-bid-and-ask-size-display-change
- [F1] https://forum.alpaca.markets/t/is-the-ask-bid-size-represented-in-round-lots-or-as-individual-shares/11038 · [F2] https://forum.alpaca.markets/t/bid-and-ask-size-for-iex-data-is-not-as-reported-by-iex/12712 · [F3] https://forum.alpaca.markets/t/quote-bid-ask-size/15786 · [F4] https://forum.alpaca.markets/t/why-can-i-see-odd-lot-quotes-in-sip-feed/16000
- [PY-M][PY-Q][PY-WS][PY-E][PY-T] https://raw.githubusercontent.com/alpacahq/alpaca-py/master/ + `alpaca/data/mappings.py`, `alpaca/data/models/quotes.py`, `alpaca/data/live/websocket.py`, `alpaca/data/enums.py`, `tests/data/test_historical_stock_data.py`
- [CTA] https://www.ctaplan.com/publicdocs/ctaplan/CTA_Symbol_File_Specification.pdf (v1.2, 2026-01-29) · [MS] https://massive.com/blog/change-stocks-quotes-round-lots-to-shares
- [PYDOC] https://docs.python.org/3/library/datetime.html#datetime.datetime.fromisoformat · [PY310] https://raw.githubusercontent.com/python/cpython/3.10/Doc/library/datetime.rst · [GO] https://raw.githubusercontent.com/golang/go/master/src/time/format.go

## Kết luận (xếp hạng)
1. **Parser:** dùng regex + `fromisoformat` như snippet trên (đã chạy trên 3.10.21 và 3.13.14), thêm cờ `re.ASCII`. Lưu ba thứ: chuỗi `t` thô, `datetime` UTC aware (µs), và `ns` int (để sắp thứ tự và dedup). Bỏ hẳn fallback `datetime.now()`.
2. **Field bắt buộc (thiếu → raise, không bịa giá trị):** trade `T,S,i,x,p,s,t`; quote `T,S,bx,bp,bs,ax,ap,as,t`. Optional/passthrough: `c`, `z` (lưu nếu có); `u` chỉ có ở historical.
3. **Bỏ mọi default hiện tại:** không `bs/as=100.0`, không `x="IEX"`, không dùng giá trade thay `bp/ap`. Quote phải đến từ message `q` riêng, không ghép từ trade.
4. **`bp==0` hoặc `ap==0` → quote một phía:** giữ nguyên, gắn cờ, loại khỏi spread/OBI; không raise.
5. **Đơn vị size:** ghi `size_unit` theo thời điểm (`round_lots` trước 2025-11-03, `shares` từ đó, cho SIP). Với IEX: gắn `ASSUMED` cho tới khi probe thật xác nhận; không tự nhân 100.
6. **Exchange:** lưu chuỗi thô (`"V"` = IEX); kiểm tra theo bảng lấy từ `/v2/stocks/meta/exchanges` lúc chạy, không theo enum cứng.
7. **Encoding:** khoá kết nối ở `Content-Type: application/json` (connector parse string). Nếu sau này dùng msgpack thì `t` là Timestamp → cần nhánh parse riêng.

## Câu hỏi còn mở
- Feed `v2/iex` sau 2025-11-03: `bs`/`as` là shares hay lots? Cách kiểm: so một quote Alpaca IEX với size hiển thị của IEX cho cùng thời điểm (cần key Basic thật; probe Phase 0).
- WS có bao giờ bỏ field (`bx`/`ax`/`c`/`z`) khi một phía trống không? Docs WS không có danh sách required.
- Historical quotes trước 2025-11-03 có bị restate sang shares không (Massive làm, Alpaca chưa thấy nói)?
- Server có bao giờ gửi offset khác "Z" hoặc leap second không? Chưa thấy mẫu nào.
