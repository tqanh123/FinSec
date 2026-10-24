---
phase: 2
title: "Tick Correctness"
plan: 261021-1314-phase0b-foundations
created: 2026-10-21
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 2 — Tick Correctness

## Overview
`MarketTick` kiểm tick VN theo D4 và không còn sàn mặc định bịa; connector Alpaca/SSI và `POST /v1/ticks/ingest` thôi bịa giá trị (thời gian,
size, bid/ask, sàn, market) và từ chối payload thiếu trường; API trả 422 thay vì 500 và mọi response nghiên cứu mang disclaimer; hằng endpoint
SSI được sửa; fixture smoke chuyển sang VND đầy đủ. Phụ thuộc Phase 1 (pytest config, `.venv`, dev extra có fastapi).

## Requirements
**R2.1 `src/finsec_alpharadar/domain/models.py`**
- `MarketTick.exchange: Optional[str] = None` (thay `str = "IEX"` ở dòng 31).
- Thêm `price_limits: Optional[Tuple[float, float]] = None` là **field cuối** (sau `foreign_sell_volume`, dòng 33); thứ tự `(floor, ceiling)`.
  Không caller nào dựng `MarketTick` theo vị trí (OBSERVED: `api/server.py:30-44`, `connectors/us_alpaca_edgar.py:21-32`,
  `connectors/vn_ssi_vnstock.py:19-33`, `tests/test_end_to_end.py:71-96` đều dùng keyword).
- Lỗi có tên (mọi lớp là `ValueError`): `TickValidationError(ValueError)`; `UnknownExchangeError(TickValidationError)`;
  `PriceOutOfBandError(TickValidationError)`. Thông điệp nêu symbol, tên trường, giá trị sai, và với lỗi biên độ thì nêu khoảng cho phép và
  gợi ý truyền `price_limits` cho phiên đặc biệt (code-standards "Error handling", dòng 81).
- Hàm thuần (không đọc trạng thái):
  - `vn_tick_size(exchange: str, price: float) -> float`: HOSE `price < 10_000 → 10.0`, `price < 50_000 → 50.0`, còn lại `100.0`; HNX `100.0`;
    UPCOM `100.0`; sàn khác → `UnknownExchangeError`. Bảng VERIFIED (research `vn-price-bands.md` §2; S4 HSX/HNX, S3 Đ18). Chỉ cho cổ
    phiếu/CCQ (ETF, chứng quyền OUT).
  - `vn_price_limits(exchange: str, reference_price: float) -> Tuple[float, float]`: trả `(floor, ceiling)` = `(ref * (1 - band),
    ref * (1 + band))`, **chưa làm tròn**, `band = VNExchangeBand[exchange].value`; sàn lạ → `UnknownExchangeError`; `ref <= 0` →
    `TickValidationError`. Tập sàn hợp lệ lấy từ `VNExchangeBand.__members__` (DRY, không thêm hằng song song).
- `__post_init__` **chỉ kiểm, không chuẩn hoá** (dataclass frozen; connector tự `.upper()`), theo thứ tự:
  0. **Mọi market, trước mọi kiểm khác** (red-team r1 RT1): `timestamp`, `price`, `volume`, `bid_price`, `ask_price`, `bid_size`, `ask_size`,
     `foreign_buy_volume`, `foreign_sell_volume`, và `reference_price` / hai phần tử của `price_limits` khi chúng không `None`, phải qua
     `math.isfinite` → sai → `TickValidationError` nêu symbol, tên trường và giá trị. Lý do: `json.loads` (Starlette dùng nó) nhận `NaN` và
     `Infinity`, và NaN làm mọi so sánh thành `False` — tick US `"price": NaN` → 200 và nhiễm trạng thái Kalman/VWAP dùng chung của symbol;
     theo đúng spec cũ, tick HOSE `price=inf, reference_price=inf` và `price_limits=(1.0, inf)` đều PASS (OBSERVED 2026-10-21, red-team r1
     `redteam-r1/scripts/asgi_nan.py`, `band_spec.py`). Tick US không có `exchange` vẫn hợp lệ (bước 0 không đụng `exchange`).
  1. `price_limits is not None` mà `market != MarketRegion.VN` → `TickValidationError`.
  2. Với `market == MarketRegion.VN`:
     a. `exchange is None` → `UnknownExchangeError` ("missing exchange"); `exchange not in VNExchangeBand.__members__` → `UnknownExchangeError`;
     b. `reference_price is None` hoặc `<= 0` → `TickValidationError`;
     c. nếu có `price_limits`: phải là 2 số với `0 < floor <= ceiling`, sai → `TickValidationError`; rồi `floor <= price <= ceiling`, sai →
        `PriceOutOfBandError` (kiểm chặt, không dung sai);
     d. nếu không: `(floor, ceiling) = vn_price_limits(exchange, reference_price)`, `tick = vn_tick_size(exchange, ceiling)` (bước tại mức giá
        trần = bước lớn hơn), yêu cầu `floor - tick <= price <= ceiling + tick`, sai → `PriceOutOfBandError`.
  - **Không** kiểm bid/ask/size: `bp`/`ap` = 0 nghĩa là "không có bid/ask hoạt động", sổ lệnh một phía là hợp lệ (VERIFIED research
    `alpaca-timestamps.md` §3 [HQ][LQ]).
  - Tick US không có `exchange` là hợp lệ.
- `vn_ceiling_floor_proximity` (dòng 40-45): `market != VN` → `None`; có `price_limits` → trả `(price_limits[1], price_limits[0])` — **giữ
  thứ tự (ceiling, floor)** của property (ngược với `price_limits`); không có → `(round(ceiling, 2), round(floor, 2))` từ `vn_price_limits`
  (giữ làm tròn 2 chữ số; làm tròn theo bước giá là OUT). Bỏ nhánh fallback `else 0.07` (dòng 44).
- Docstring `MarketTick`: giá VN là VND đầy đủ ở biên domain (D4); `price_limits` = (floor, ceiling) từ feed hoặc caller; **giới hạn đã
  biết** (red-team r1 RT10): kiểm D4 giả định VND đầy đủ — một tick mà cả giá lẫn tham chiếu tính bằng nghìn VND sẽ lọt qua (vd HOSE
  `reference_price=5.0, price=14.0` PASS vì dung sai 1 bước giá tuyệt đối nuốt cả biên — OBSERVED 2026-10-21, `redteam-r1/scripts/band_spec.py`);
  kiểm đơn vị là OUT (plan.md "Out of scope").

**R2.2 `src/finsec_alpharadar/domain/__init__.py`**: export thêm `TickValidationError`, `UnknownExchangeError`, `PriceOutOfBandError`,
`vn_tick_size`, `vn_price_limits` trong import và `__all__` (code-standards "Imports", dòng 16).

**R2.3 `src/finsec_alpharadar/connectors/errors.py` (mới)**: `class PayloadFieldError(ValueError)` — "a raw source payload lacks a required
field or carries a malformed one". Thông điệp nêu source (`"alpaca"`/`"ssi"`), symbol (hoặc `None` khi chính symbol bị thiếu), tên trường, và giá
trị sai nếu có. Luôn raise bằng `raise PayloadFieldError(...) from err` khi bọc `KeyError`/`ValueError`/`TypeError`. Lớp con của `ValueError`
(không phải `KeyError`, vì `str(KeyError(msg))` bọc thông điệp trong dấu nháy). Re-export trong `connectors/__init__.py` (R2.4).

**R2.4 `src/finsec_alpharadar/connectors/__init__.py`**: thêm `PayloadFieldError` vào import và `__all__`.

**R2.5 `src/finsec_alpharadar/connectors/us_alpaca_edgar.py` — `AlpacaStreamAdapter.parse_trade_and_quote`** (dòng 17-32)
- Trường bắt buộc: trade `S, p, s, t, x`; quote `S, t, bp, ap, bs, as` (research `alpaca-timestamps.md:92` liệt kê `S`, `t` là bắt buộc
  của quote). Thiếu → `PayloadFieldError(source="alpaca")`; trường số không ép được `float` → `PayloadFieldError` kèm giá trị.
- Trường chuỗi bắt buộc (`S`, `x`, `t` của trade; `S`, `t` của quote) phải là `str` không rỗng; `None` hay kiểu khác → `PayloadFieldError` —
  không bao giờ để `str(None)` thành `"None"` (cùng lớp lỗi với red-team r1 RT13a; tìm thấy ở Consistency Sweep 2026-10-21).
- **Ghép cặp trade/quote** (red-team r1 RT13b; luật "No look-ahead", AGENTS.md:124): `quote["S"] != trade["S"]` → `PayloadFieldError`
  (quote của mã khác); `t` của quote parse cùng cách với `t` của trade (dưới) và phải `<=` `t` của trade, ngược lại → `PayloadFieldError`
  (quote sau giờ trade = look-ahead). Bằng nhau là hợp lệ.
- `t`: phải là `str`, parse bằng `datetime.fromisoformat(t)`; `tzinfo is None` → `PayloadFieldError` ("timestamp has no UTC offset");
  `ValueError`/`TypeError` → `PayloadFieldError`. `timestamp = dt.timestamp()`. Bỏ `.replace("Z", "+00:00")` và nhánh `datetime.now(...)`
  (dòng 20). OBSERVED 2026-10-21 trên 3.11.16 và 3.13.14: `"2026-10-17T04:30:00.123456789Z"` → `2026-10-17T04:30:00.123456+00:00`, epoch
  `1792211400.123456`; `.91Z` → `.910000`; chữ thường `z` → `ValueError`; chuỗi không offset → datetime **naive** (không raise) — nên bắt buộc
  kiểm `tzinfo`. Không cần regex trên ≥ 3.11.
- `exchange = str(trade_msg["x"])` giữ mã thô (`"V"` = IEX, VERIFIED research §2); bỏ default `"IEX"` (dòng 31).
- `bid_price`/`ask_price`/`bid_size`/`ask_size` chỉ lấy từ quote (`bp`, `ap`, `bs`, `as`); bỏ fallback về `trade_msg["p"]` (dòng 27-28) và
  `100.0` (dòng 29-30); `0` được chấp nhận.
- Docstring lớp: `t` là RFC-3339, 0-9 chữ số thập phân, "Z" (VERIFIED); `bs`/`as` là **shares** từ 2025-11-03 với SIP (VERIFIED changelog);
  đơn vị của feed IEX `[ASSUMED]`; tài liệu WS không có danh sách required — việc Alpaca luôn gửi đủ các trường này là `[ASSUMED]`
  (research §1).
- Chỉ bọc phần **đọc + ép kiểu trường** trong `try/except (KeyError, ValueError, TypeError)`; dựng `MarketTick(...)` **ngoài** khối `try`, để lỗi
  của domain (`TickValidationError` và lớp con) nổi lên nguyên dạng, không bị đổi thành `PayloadFieldError`.
- Bỏ import `datetime.now`/`timezone` nếu không còn dùng. `WS_ENDPOINT` của Alpaca và `SecEdgarFilingAdapter` giữ nguyên (OUT).

**R2.6 `src/finsec_alpharadar/connectors/vn_ssi_vnstock.py` — `SSIFastConnectAdapter`** (dòng 9-33)
- Trường bắt buộc: `Symbol, LastPrice, LastVol, BidPrice1, AskPrice1, BidVol1, AskVol1, RefPrice, Exchange, Timestamp` → thiếu/sai →
  `PayloadFieldError(source="ssi")`. Tên trường của stub là `[ASSUMED]` tới khi đọc spec R-channel ở Phase 6 (§9.5); riêng `Ceiling`, `Floor`,
  `RefPrice` là tên VERIFIED của X-TRADE (research `vn-price-bands.md` §4). `Timestamp` vẫn là epoch float của stub (`[ASSUMED]`).
- `Symbol` và `Exchange` phải là `str` không rỗng; `None` hay kiểu khác → `PayloadFieldError` (như R2.5).
- `exchange = str(msg["Exchange"]).upper()` (giữ chuẩn hoá hoa); bỏ default `"HOSE"` (dòng 30), bỏ `datetime.now(...)` (dòng 22), bỏ
  `100.0` (dòng 27-28), bỏ fallback bid/ask về `LastPrice` (dòng 25-26).
- `Ceiling` và `Floor` cùng có → `price_limits=(float(msg["Floor"]), float(msg["Ceiling"]))`; chỉ có một → `PayloadFieldError`; không có cả hai
  → `None`.
- Như R2.5: chỉ bọc phần đọc/ép kiểu trường; dựng `MarketTick(...)` ngoài `try` — `test_ssi_unknown_exchange_propagates` khoá rằng
  `UnknownExchangeError` không bị bọc thành `PayloadFieldError`.
- Foreign volume giữ `.get("ForeignBuyVolTotal", 0.0)` / `.get("ForeignSellVolTotal", 0.0)` (dòng 31-32) — OUT, roadmap Phase 6.
- Hằng: xoá `WS_ENDPOINT = "wss://stream.ssi.com.vn"` (dòng 15); thêm `STREAM_URL = "https://fc-datahub.ssi.com.vn/v2.0"` và
  `REST_URL = "https://fc-data.ssi.com.vn/v2.0/Market"` (VERIFIED, AGENTS.md:99-102). Không client (§9.5). `grep -rn "WS_ENDPOINT" src/` hôm
  nay chỉ thấy hai định nghĩa trong hai adapter (OBSERVED) → không caller nào vỡ.
- Docstring lớp (dòng 10-13) viết lại: bỏ URL sai và câu "DNSE Lightspeed MQTT" (không có code nào làm việc đó); nêu "payload parser only, no
  network client; field names ASSUMED until the Phase 6 R-channel spec". **Không** viết tên sản phẩm giao dịch của SSI trong `src/` (mẫu
  `fc[\s_-]?trading` của `tests/test_no_order_execution.py` sẽ đỏ) — viết "FC Data only".
- Giá: mẫu tài liệu SSI dùng VND (`"Ceiling":24500.0` cho ACB, research §3-4) → adapter SSI không đổi đơn vị. `VnstockBCTCAdapter` giữ nguyên (OUT).

**R2.7 `src/finsec_alpharadar/api/server.py`**
- Hằng module `DISCLAIMER`: tiếng Việt trước, song ngữ, NFC; phải chứa "không phải khuyến nghị đầu tư" và "not investment advice"; không chứa
  các từ buy/sell/mua/bán (luật Outputs, AGENTS.md:123). Gợi ý: `"Chỉ dùng cho nghiên cứu, không phải khuyến nghị đầu tư. For research only;
  not investment advice."` (cook có thể chỉnh trong các bất biến trên).
- `ingest_tick` (dòng 28-49): trường bắt buộc `symbol, market, timestamp, price, volume, bid_price, ask_price, bid_size, ask_size` (bỏ
  `payload.get("market", "US")` dòng 32, bỏ fallback bid/ask dòng 36-37, bỏ `100.0` dòng 38-39, bỏ `"IEX"` dòng 41); tuỳ chọn `exchange`
  (có và khác `None`: `str(...).upper()`, cùng chuẩn hoá với connector SSI; JSON `null` coi như vắng → `None`, không bao giờ thành chuỗi
  `"NONE"` — red-team r1 RT13a), `reference_price`, `price_limits` (`[floor, ceiling]` → tuple 2 float; docstring của handler ghi rõ request
  nhận `[floor, ceiling]` còn response trả `vn_ceiling_floor` theo thứ tự `[ceiling, floor]` — red-team r1 RT13c);
  foreign volume giữ default `0.0` (OUT). Bọc việc dựng tick: `except (KeyError, TypeError, ValueError) as err: raise
  HTTPException(status_code=422, detail=...) from err` — `KeyError` → detail nêu tên trường thiếu; `TickValidationError` → thông điệp của nó.
  Response: `{"signal": ..., "vn_ceiling_floor": ..., "disclaimer": DISCLAIMER}`. `symbol` phải là `str` không rỗng; `null` → 422 (như R2.5).
- `search_disclosures` (dòng 52-66): thêm `"disclaimer": DISCLAIMER` vào response (chữ ký giữ nguyên tới Phase 3).
- `/health` giữ nguyên — không mang nội dung nghiên cứu (DERIVED).
- Import `HTTPException` ngay trên dòng 4 (`from fastapi import FastAPI, HTTPException, Query`).

**R2.8 `tests/test_end_to_end.py` — sửa fixture TRONG DÒNG (không thêm/xoá dòng nào ở dòng 1-163)**
- dòng 41: `{"S": "NVDA", "p": 189.50, "s": 2500, "x": "V", "t": "2026-10-17T04:30:00.123456789Z"},`
- dòng 42: `{"S": "NVDA", "t": "2026-10-17T04:29:59.950000Z", "bp": 189.48, "ap": 189.52, "bs": 1200, "as": 800},` — quote có `S` và `t`
  sớm hơn trade (R2.5 ghép cặp, red-team r1 RT13b); vẫn một dòng.
- dòng 46, 48, 49, 52: `142500.0`, `142400.0`, `142600.0`, `138000.0` (dòng 50-51 là size, giữ nguyên).
- dòng 59: `(147660.0, 128340.0)` — OBSERVED 2026-10-21: `round(138000.0 * 1.07, 2) == 147660.0`, `round(138000.0 * 0.93, 2) == 128340.0`.
- dòng 86, 88, 89, 92: `price=138_000.0 + 150.0 * step + (2_900.0 if is_spike else 0.0)`, `bid_price=142_800.0`, `ask_price=142_900.0`,
  `reference_price=138_000.0`. Giá FPT lớn nhất = 143.000, nằm trong [128.240; 147.760] (DERIVED: biên 128.340-147.660 ± bước 100).
- Tick US vòng quant (dòng 71-81) không cần `exchange`.
- OBSERVED 2026-10-21 (script scratch `quant_spike.py`): `alert_reasons` của FPT giống hệt ở ×1 và ×1000 — volume z, CUSUM log-return, OBI và
  foreign flow không phụ thuộc mức giá (`quant/kalman_microstructure.py:83-113`).

**R2.9 Test mới** — xem "Tests Before". Phi chức năng: không mạng, không đồng hồ; test API gọi handler trực tiếp, không `TestClient`
(không cần httpx).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | `src/finsec_alpharadar/domain/models.py` |
| Modify | `src/finsec_alpharadar/domain/__init__.py` |
| Create | `src/finsec_alpharadar/connectors/errors.py` |
| Modify | `src/finsec_alpharadar/connectors/__init__.py` |
| Modify | `src/finsec_alpharadar/connectors/us_alpaca_edgar.py` |
| Modify | `src/finsec_alpharadar/connectors/vn_ssi_vnstock.py` |
| Modify | `src/finsec_alpharadar/api/server.py` |
| Modify | `tests/test_end_to_end.py` |
| Create | `tests/test_vn_price_band.py` |
| Create | `tests/test_connectors.py` |
| Create | `tests/test_api_server.py` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261021-1314-phase0b-foundations/research/vn-price-bands.md` |
| read | `plans/261021-1314-phase0b-foundations/research/alpaca-timestamps.md` |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Challenge luật ">8 tệp": 15 tệp = 11 code/test + 4 docs. Thay đổi hợp đồng `MarketTick` (bỏ default `exchange`, thêm kiểm VN) làm vỡ cùng lúc
cả ba nơi dựng tick (`api/server.py:30`, `connectors/us_alpaca_edgar.py:21`, `connectors/vn_ssi_vnstock.py:19`) và fixture smoke; tách
domain/connector/API thành các phase riêng sẽ để lại commit trung gian đỏ hoặc API trả 500 cho tick VN không có sàn → giữ một phase, nhưng
làm TDD theo module.

## Implementation Steps
1. **Tests-before (RED).** Viết `tests/test_vn_price_band.py`, `tests/test_connectors.py`, `tests/test_api_server.py` (danh sách dưới). Chạy
   `.venv/Scripts/python -m pytest -q tests/test_vn_price_band.py tests/test_connectors.py tests/test_api_server.py` → RED: lỗi thu thập
   `ImportError` (chưa có `UnknownExchangeError`, `PayloadFieldError`, …). Thêm trước **chỉ** các lớp lỗi + chữ ký hàm để có RED theo hành vi
   (vd `MarketTick(..., market=VN, exchange="XYZ")` dựng được bình thường — OBSERVED: `domain/models.py:19-45` không có `__post_init__`).
2. **Domain → xanh** (R2.1, R2.2): chạy `tests/test_vn_price_band.py`.
3. **Connector → xanh** (R2.3-R2.6): chạy `tests/test_connectors.py`. Smoke sẽ đỏ ở đây (fixture Alpaca chưa có `t`) — đúng kỳ vọng.
4. **API → xanh** (R2.7): chạy `tests/test_api_server.py`.
5. **Fixture → xanh** (R2.8): `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
6. **Grep gate** (kỳ vọng rỗng, exit 1; baseline OBSERVED 2026-10-21 trong plan.md "Acceptance"): G2, G3, G4, G6, G7, và phần connectors của
   G1: `grep -rn --include=*.py "datetime.now" src/finsec_alpharadar/connectors`.
7. **Delta docs + citation sweep** (dưới).
8. **Regression gate** → commit duy nhất, ví dụ `fix: validate VN tick bands and stop fabricating tick fields` + trailer `Co-Authored-By:`.

### Delta docs (cùng commit; tiếng Anh; ghi ngày)
- **AGENTS.md**
  - "Decisions": dòng mới trên cùng:
    ```
    - 2026-10-21 — VN price validation (D4): a VN `MarketTick` raises on a missing or unknown exchange, a missing reference price, or a price outside its allowed range.
      - With `price_limits` (floor, ceiling) from the feed or the caller the check is strict; without them it uses the normal band (HOSE 7%, HNX 10%, UPCOM 15%, unrounded, around `reference_price`) widened by one tick on each side.
      - Special sessions (first listing day, resumption after 25+ sessions, some ex-rights days, the 2026-12-28 HNX-to-HOSE transfer) must pass `price_limits`.
      - VN prices are full VND at the domain boundary. The check assumes full VND: a tick whose price and reference are both in thousands of VND passes undetected (unit checks wait for the vnstock probe).
    ```
  - "Current Status": bullet **Tests** (viết ở Phase 1) — cập nhật `<N>` và ngày OBSERVED theo số test thật sau phase này, và ghi kết quả CI của lần push trước (run id, OBSERVED — VL-1); cùng số đó ở
    `docs/code-standards.md` "Current state" (red-team r1 RT8).
  - "Current Status" → "Known bugs": xoá dòng 49 (VN band … silently gets 7%) và dòng 51 (SSI endpoint constant … wrong). Dòng 41 "connectors
    are static dict parsers" → thêm "; missing, null or malformed fields raise, and an Alpaca quote must share the trade's symbol and not
    postdate it (<date>)".
  - "Package Layout": dòng 55 (`domain/models.py`) thêm "VN tick validation (D4), `price_limits`; every tick rejects non-finite numbers"; dòng
    61 (`api/server.py`) thêm "required tick fields, 422 on bad input, a bilingual disclaimer on research responses".
  - "Verified Facts": thêm 2 mục ngắn (URL lấy từ research):
    ```
    - **VN price limits.**
      - Ticks: HOSE stocks 10 VND below 10,000, 50 VND from 10,000 to 49,950, 100 VND from 50,000; HNX and UPCOM 100 VND. Ceilings round down and floors round up to the tick; a limit equal to the reference moves one tick. VERIFIED https://www.cts.vn/wp-content/uploads/2025/08/KRX_1.Quy-dinh-giao-dich-chung-khoan-tai-HSX.pdf and https://www.cts.vn/wp-content/uploads/2025/08/KRX_2.Quy-dinh-giao-dich-chung-khoan-tai-HNX.pdf
      - First trading day, resumption after 25+ sessions and some ex-rights days: ±20% HOSE, ±30% HNX, ±40% UPCOM. VERIFIED (UPCOM, 2026) https://luatvietnam.vn/chung-khoan/quyet-dinh-23-qd-hdtv-2026-ban-hanh-quy-che-dang-ky-va-quan-ly-giao-dich-chung-khoan-chua-niem-yet-435227-d1.html; HOSE/HNX figures from the 2025 brochures above (the 2026 appendix was not readable).
      - SSI X-TRADE messages carry `Ceiling`, `Floor` and `RefPrice`; the doc samples are in VND. VERIFIED https://guide.ssi.com.vn/ssi-products/fastconnect-data/streaming-data.md
    - **Alpaca stream fields.** `t` is RFC-3339 with 0-9 fractional digits and "Z" (VERIFIED https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data); exchange code `V` is IEX (VERIFIED https://docs.alpaca.markets/us/reference/stockmetaexchanges-1); SIP quote sizes are shares since 2025-11-03 (VERIFIED https://docs.alpaca.markets/us/changelog/marketdata-bid-and-ask-size-display-change); the IEX-feed size unit is ASSUMED.
    ```
- **docs/system-architecture.md**
  - bảng As-built: dòng 21 (`domain/models.py`: bỏ "an unknown exchange silently gets 7% (line 44); prices are never validated"; ghi kiểm D4,
    `price_limits`, mọi tick từ chối số không hữu hạn; giữ câu timestamp float), dòng 22 (`us_alpaca_edgar.py`: bỏ `now()`/100.0; ghi trường
    bắt buộc + `PayloadFieldError` + ghép cặp quote/trade),
    dòng 23 (`vn_ssi_vnstock.py`: bỏ `now()`/100.0/HOSE/`WS_ENDPOINT` sai; ghi hằng URL VERIFIED, tên trường stub ASSUMED), dòng 30
    (`api/server.py`: 422, disclaimer; "untyped body" vẫn đúng).
  - Data flow dòng 62-66: thay bằng luồng mới (trường bắt buộc, VN không có sàn → 422, response có `disclaimer`); dòng 68-70 thêm `disclaimer`.
  - Boundaries dòng 121: "a missing key raises a 500" → "a missing or invalid field returns 422".
  - Known gaps: xoá dòng 133 (VN band), dòng 136 (`now()` của connector), dòng 137 (API notice); dòng 138 chỉ giữ phần model ID chưa kiểm.
  - External dependencies dòng 109: câu hằng `wss://stream.ssi.com.vn` → "was wrong; fixed <date> (`STREAM_URL`/`REST_URL`)".
  - Key decisions dòng 146: bỏ "as `domain/models.py:44` does today" (đã sửa).
- **docs/code-standards.md**
  - dòng 18 "Precision": cập nhật trích dẫn dòng `domain/models.py` (dời); giữ "band prices are rounded to 2 dp rather than to tick size".
  - dòng 21: thay câu ASSUMED về fixture nghìn VND bằng: "Binding (decision 2026-10-21, D4): VN prices are full VND at the domain boundary; the
    smoke fixture uses VND (FPT 142,500). The vnstock unit stays ASSUMED until its probe; its connector must convert."
  - dòng 68-69 "Current state": `src/` nay có `raise`; connector raise `PayloadFieldError`; `api/server.py` trả 422 (bỏ các trích KeyError cũ).
  - dòng 73-75 "fabricated-default debt": xoá mục `now()` của connector, size 100, bid/ask = last price, `"HOSE"`, `"IEX"`, "unknown exchange
    silently gets 7%"; **giữ** mục RAG (`reference_time or now`, age clamp — Phase 3).
  - dòng 76 "VN bands": bỏ câu Debt; mô tả luật D4 + `vn_tick_size`/`vn_price_limits`; "HNX-to-HOSE band ASSUMED — callers pass `price_limits`".
  - dòng 94 "SSI": câu hằng sai → "fixed <date>".
  - dòng 104: `api/server.py:19` có thể dời (thêm `DISCLAIMER`) → sửa số dòng.
- **docs/project-roadmap.md**: §7 0b mục 5, dòng 391 ("`models.py` raises on an unknown exchange") và dòng 392 ("connectors stop defaulting
  timestamps to `now()`") thêm "(done <date>)".

### Citation sweep (file Phase 2 sửa; kiểm kê OBSERVED 2026-10-21)
`grep -nE "models\.py:[0-9]|us_alpaca_edgar\.py:[0-9]|vn_ssi_vnstock\.py:[0-9]|server\.py:[0-9]|test_end_to_end\.py:[0-9]|test:[0-9]" AGENTS.md docs/system-architecture.md docs/code-standards.md docs/project-roadmap.md`
- `domain/models.py`: AGENTS.md:49 (xoá); code-standards:13 (`:19,48`, `:59`, `:62`), :14 (`:8`, `:13`), :18 (`:24-29,32-33,67-95`, `:45`),
  :22 (`:23`), :75 (`:31`, `:44` — câu bị xoá), :76 (`:41-45`); system-architecture:21, :64 (`models.py:44`), :133, :146, :179 (`:13-16`).
- `connectors/us_alpaca_edgar.py`: code-standards:69 (`:22,44`), :73 (`:20`), :74 (`:27-28`, `:29-30`); system-architecture:22, :136 (`:20`).
- `connectors/vn_ssi_vnstock.py`: code-standards:8 (`:48,51` — chuỗi của `VnstockBCTCAdapter`, sẽ dời), :69 (`:20,23-24,45`), :73 (`:22`),
  :74 (`:25-26`, `:27-28`), :75 (`:30`), :94 (`:15`); system-architecture:23, :109 (`:15`), :136 (`:22`), :138 (`:15`).
- `api/server.py`: code-standards:69 (`:31`), :74 (`:36-37`, `:38-39`), :75 (`:41`), :104 (`:19`); system-architecture:10 (`:4` — giữ dòng
  4), :30 ("line 29", "lines 18-20"), :62 (`:28-44`), :64 (`:41`), :65 (`:45`), :66 (`:46-49`), :68 (`:52-66`), :80 (`:16-20`), :115 (`:19`),
  :121 (`:29`), :137 (`:46-49`), :138 (`:19`).
- `tests/test_end_to_end.py`: không dời nếu tôn trọng R2.8 (sửa trong dòng).
Mỗi hit: hoặc sửa số dòng cho đúng file mới, hoặc xoá/viết lại câu nếu debt đã sửa.

### Tests Before
`tests/test_vn_price_band.py` (unit domain):
- [x] `test_unknown_vn_exchange_raises` ("XYZ") và `test_missing_vn_exchange_raises` (`None`) → `UnknownExchangeError` — khoá luật "raise on
      an unknown exchange"; RED: hôm nay dựng được (fallback 7%, `domain/models.py:44`).
- [x] `test_vn_reference_price_required` (param `None`, `0.0`, `-1.0`) → `TickValidationError`; RED.
- [x] `test_price_inside_normal_band_passes` (param: HOSE ref 10.000 giá 10.700; HNX ref 10.000 giá 11.000; UPCOM ref 10.000 giá 11.500) —
      LOCK (hôm nay không raise).
- [x] `test_price_outside_normal_band_raises` (param: HOSE ref 10.000 giá 10.800 [trần 10.700 + bước 50]; HOSE giá 9.200 [sàn 9.300 − 50];
      HNX ref 10.000 giá 11.200; UPCOM ref 10.000 giá 11.700) → `PriceOutOfBandError`; RED.
- [x] `test_low_price_one_tick_bump_passes` (UPCOM ref 500: giá 600 và 400 pass — trần/sàn thật 600/400, research `vn-price-bands.md` §3; HOSE
      ref 100: giá 110 và 90 pass) và `test_low_price_beyond_tolerance_raises` (UPCOM ref 500 giá 700 → raise, giới hạn 675); RED cho ca raise.
- [x] `test_price_limits_allow_special_session` (HOSE ref 10.000, `price_limits=(8_000.0, 12_000.0)`, giá 12.000 pass) **và** cùng giá không có
      `price_limits` → raise (khoá rằng chính override cho qua); RED (field chưa có).
- [x] `test_price_outside_given_limits_raises` (giá 12.050 với `(8_000.0, 12_000.0)`) → `PriceOutOfBandError` (không dung sai); RED.
- [x] `test_invalid_price_limits_raise` (param `(12_000.0, 8_000.0)`, `(0.0, 12_000.0)`, `(-1.0, 5.0)`) → `TickValidationError`; RED.
- [x] `test_us_tick_without_exchange_is_valid` — LOCK; `test_us_tick_with_price_limits_raises` → RED.
- [x] `test_bid_ask_zero_is_not_validated` (tick VN hợp lệ với `bid_price=0.0`, `ask_size=0.0`) — LOCK quyết định không kiểm sổ lệnh.
- [x] `test_ceiling_floor_proximity_order` — có `price_limits=(8_000.0, 12_000.0)` → `(12_000.0, 8_000.0)`; không có → `(round(ceiling, 2),
      round(floor, 2))`; RED.
- [x] `test_vn_tick_size_table` (param: HOSE 9.990 → 10; 10.000 → 50; 49.950 → 50; 50.000 → 100; HNX 5.000 → 100; UPCOM 5.000 → 100; "XYZ" →
      `UnknownExchangeError`); RED.
- [x] `test_non_finite_numbers_raise` (param: US `price=nan`; US `price=inf`; US `bid_size=nan`; US `timestamp=nan`; HOSE `price=inf,
      reference_price=inf`; HOSE `price_limits=(1.0, inf)`) → `TickValidationError` nêu tên trường — red-team r1 RT1; RED (hôm nay dựng được).
- Lưu ý: không assert hành vi **đúng tại** mép đã nới (sai số float, vd `10_000 * 1.07` không chính xác nhị phân); giá hợp lệ thật luôn nằm
  trong mép ít nhất 1 bước (DERIVED: trần/sàn thật nằm trên lưới bước giá, mép = biên chưa làm tròn ± 1 bước).

`tests/test_connectors.py` (unit connector; mỗi test dựng payload từ một helper hợp lệ đầy đủ — trade + quote có `S`, `t` với quote ≤ trade,
SSI đủ 10 trường — rồi bỏ hoặc sửa đúng một trường, để test LOCK không đỏ vì thiếu trường khác):
- [x] `test_alpaca_missing_required_field_raises` (param 11 trường: trade `S, p, s, t, x`; quote `S, t, bp, ap, bs, as`) → `PayloadFieldError`
      nêu tên trường; RED (hôm nay `t`/`x`/`bp`/`ap`/`bs`/`as` có default, `S`/`t` của quote không được đọc, còn `S`/`p`/`s` của trade ném
      `KeyError` trần — `us_alpaca_edgar.py:19-31`).
- [x] `test_alpaca_quote_symbol_mismatch_raises` (quote `"S": "AMD"` với trade `NVDA`) và `test_alpaca_quote_after_trade_raises` (quote `t`
      sau trade `t` 1 ms) → `PayloadFieldError` — red-team r1 RT13b; RED. `test_alpaca_quote_at_trade_time_is_accepted` (cùng `t`) — LOCK.
- [x] `test_alpaca_nan_price_raises` (`"p": float("nan")`) → `TickValidationError` (lỗi domain, không bị bọc thành `PayloadFieldError`) —
      red-team r1 RT1; RED.
- [x] `test_alpaca_nanosecond_z_timestamp` (`"2026-10-17T04:30:00.123456789Z"` → `pytest.approx(1792211400.123456, abs=1e-6)`) — LOCK: code cũ
      `.replace("Z", "+00:00")` cũng parse được 9 chữ số trên ≥ 3.11 (research `alpaca-timestamps.md` §4, dòng "9 số + `+00:00`" OK trên 3.13);
      test khoá rằng parser mới không làm hỏng ca này.
- [x] `test_alpaca_naive_timestamp_raises` (`"2026-10-17T04:30:00.123456"`) và `test_alpaca_non_string_timestamp_raises` (`1792211400`) →
      `PayloadFieldError`; RED.
- [x] `test_alpaca_malformed_price_raises` (`"p": "n/a"`) → `PayloadFieldError` nêu giá trị; RED (hôm nay `ValueError` trần).
- [x] `test_alpaca_keeps_raw_exchange_code` (`"x": "V"` → `tick.exchange == "V"`) — LOCK.
- [x] `test_alpaca_one_sided_quote_is_accepted` (`"bp": 0`) — LOCK.
- [x] `test_ssi_missing_required_field_raises` (param 10 trường) → `PayloadFieldError`; RED.
- [x] `test_ssi_ceiling_floor_become_price_limits` (`Ceiling: 147_600.0`, `Floor: 128_400.0` → `price_limits == (128_400.0, 147_600.0)`); RED.
- [x] `test_ssi_single_limit_raises` (param chỉ `Ceiling` / chỉ `Floor`) → `PayloadFieldError`; RED.
- [x] `test_ssi_unknown_exchange_propagates` (`"Exchange": "XYZ"` → `UnknownExchangeError`, không bị bọc); RED.
- [x] `test_ssi_endpoints_are_verified` (`STREAM_URL`, `REST_URL` đúng hai URL VERIFIED; không thuộc tính nào chứa `stream.ssi.com.vn`) — hồi quy
      cho hằng sai cũ; RED.
- [x] `test_null_string_fields_raise` (param: Alpaca trade `S=None`, `x=None`; SSI `Symbol=None`, `Exchange=None`) → `PayloadFieldError`; RED
      (hôm nay không raise).

`tests/test_api_server.py` (handler gọi trực tiếp; dòng đầu module: `fastapi = pytest.importorskip("fastapi")`):
- [x] `test_vn_tick_without_exchange_returns_422` → `pytest.raises(HTTPException)`, `status_code == 422`, `detail` nhắc `exchange`; RED (hôm
      nay `exchange="IEX"` → 200).
- [x] `test_missing_required_field_returns_422` (param ít nhất `bid_size`, `market`, `timestamp`) → 422, `detail` nêu tên trường; RED (hôm nay
      default hoặc `KeyError` → 500).
- [x] `test_ingest_response_carries_disclaimer` (tick US hợp lệ, symbol riêng vd `"TSTA"`) → `resp["disclaimer"] == server.DISCLAIMER`; RED.
- [x] `test_search_response_carries_disclaimer` → gọi `search_disclosures(q="alpha", market=None, symbol=None, top_k=5)`; RED.
- [x] `test_disclaimer_text_invariants` — NFC (`unicodedata.is_normalized("NFC", s)`), chứa "không phải khuyến nghị đầu tư" và "not investment
      advice" (không phân biệt hoa thường), không khớp `\b(buy|sell|mua|bán)\b` (`re.IGNORECASE`); RED.
- [x] `test_non_finite_price_returns_422` (tick US `"price": float("nan")`, symbol riêng) → 422 — red-team r1 RT1; RED (hôm nay 200 và nhiễm
      trạng thái engine, OBSERVED `redteam-r1/scripts/asgi_nan.py`).
- [x] `test_vn_null_exchange_reports_missing` (tick VN `"exchange": None`) → 422, `detail` nói thiếu sàn và không chứa `"NONE"` — red-team r1
      RT13a; RED (hôm nay 200). `test_null_symbol_returns_422` (`"symbol": None`) → 422; RED (hôm nay 200).
- Bẫy gọi trực tiếp (OBSERVED 2026-10-21, probe scratch fastapi 0.156.4 trên 3.11.16 + 3.13.14): decorator `@app.get/post` trả về hàm gốc;
  `HTTPException` nổi lên với `.status_code`; nhưng tham số `Query(...)` bị bỏ qua nhận **object `Query`**, không phải `None` → luôn truyền
  **đủ mọi tham số** (`market=None, symbol=None, top_k=5`); gọi trực tiếp bỏ qua kiểm `ge/le` → không viết test cho kiểm đó. Engine module-level
  (`api/server.py:16-20`) dùng chung giữa các test → mỗi test một symbol riêng.

### Implement
- Bước 2-5 ở trên, theo thứ tự domain → connector → API → fixture.

### Tests After
- [x] Ba file test mới xanh; `test_end_to_end` xanh với fixture VND; `test_agents_md_paths_exist` xanh sau khi sửa AGENTS.md (token mới như
      `price_limits` không phải path).
- [x] Grep gate ở bước 6 rỗng.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` (bash; PowerShell `.\.venv\Scripts\python -m pytest -q`) MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục, không fastapi) pass; skip đúng 1 module `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` và
  `python3.13 harness/scripts/scaffold_standards.py --type code-standards --check docs/code-standards.md` exit 0.
- Sau commit: `git push` (draft PR từ Phase 1 tự chạy CI — VL-1); run CI của commit này `success` trên 3.11 + 3.13
  (`gh run view <id> --json conclusion,jobs`) trước khi bắt đầu Phase 3.

## Success Criteria
- [x] RED được ghi lại cho từng nhóm test (lỗi thu thập rồi lỗi hành vi) trước khi implement.
- [x] `.venv/Scripts/python -m pytest -q` → toàn bộ pass, 0 skipped; số test tăng ít nhất 40 so với Phase 1 (đếm cả case tham số hoá).
- [x] Grep gate G2, G3, G4, G6, G7 và G1-phần-connectors → không output.
- [x] `PYTHONPATH=src python tests/test_end_to_end.py` in `"vn_fpt_hose_bands": [147660.0, 128340.0]` và exit 0.
- [x] `grep -n "silently gets 7%\|stream.ssi.com.vn" AGENTS.md docs/system-architecture.md docs/code-standards.md` → chỉ còn ghi chú lịch sử có ngày.
- [x] Hai standards check exit 0.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Kiểm VN chặt raise sai cho phiên đặc biệt / cổ phiếu giá thấp | Trung bình × Cao | `price_limits` override; nới 1 bước giá (bao bump: UPCOM ref 500 → 400/600 nằm trong 325-675); thông điệp nêu khoảng + gợi ý `price_limits`; chưa có feed tick VN sống trước Phase 6. |
| Đảo thứ tự tuple (`price_limits` = floor, ceiling; property = ceiling, floor) | Trung bình × Trung bình | Test `test_ceiling_floor_proximity_order` khoá cả hai nhánh; docstring nêu thứ tự. |
| Gọi handler FastAPI trực tiếp nhận object `Query` | Thấp × Trung bình | OBSERVED; luôn truyền đủ tham số; fallback `TestClient` + `httpx2` nếu fastapi đổi hành vi. |
| Trích dẫn `file:line` trong 3 docs bị dời (4 file src đổi) | Cao × Trung bình | Citation sweep ở trên + grep; fixture smoke sửa trong dòng. |
| Docstring SSI vô tình chứa tên sản phẩm giao dịch → guard test đỏ | Thấp × Thấp | Yêu cầu R2.6 ghi rõ; test sẽ bắt ngay. |
| `float` sai số tại mép biên | Thấp × Thấp | Không assert đúng-tại-mép; giá thật cách mép ≥ 1 bước. |
