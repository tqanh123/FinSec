# Nghiên cứu: biên độ giá VN, làm tròn trần/sàn, trường SSI (2026-10-21)

Phạm vi: dữ liệu đầu vào cho việc `MarketTick` (src/finsec_alpharadar/domain/models.py) raise khi gặp sàn lạ và kiểm giá VN theo biên độ. Chỉ nghiên cứu, chưa đổi code.

## Nguồn đã đọc (mã nguồn dùng trong báo cáo)
- **S1**: VNX `17/QĐ-HĐTV` ngày 31/3/2022, "Quy chế Niêm yết và giao dịch chứng khoán niêm yết" (Điều 31, 32, Phụ lục III). Bản PDF https://mbs.com.vn/files/uploads/2025/04/17_qd-hdtv_quy-che-niem-yet-va-giao-dich-ck-niem-yet.pdf . **Đã bị thay thế**, xem S2. Tôi đọc toàn văn bằng `pdftotext`.
- **S2**: VNX `22/QĐ-HĐTV` ngày 16/03/2026, thay thế "Quyết định số 22/QĐ-HĐTV ngày 18/4/2025", hiệu lực "kể từ ngày ký". https://luatvietnam.vn/chung-khoan/quyet-dinh-22-qd-hdtv-2026-ban-hanh-quy-che-niem-yet-va-giao-dich-chung-khoan-430733-d1.html . Tôi đọc được Điều 26, 30–33. **Phụ lục III (bảng % theo sàn) bị ẩn** sau đăng nhập.
- **S3**: VNX `23/QĐ-HĐTV` ngày 18/03/2026 về UPCoM (Điều 18–20). Văn bản này thay thế bản 23/QĐ-HĐTV ngày 18/4/2025. https://luatvietnam.vn/chung-khoan/quyet-dinh-23-qd-hdtv-2026-ban-hanh-quy-che-dang-ky-va-quan-ly-giao-dich-chung-khoan-chua-niem-yet-435227-d1.html
- **S4**: Tài liệu môi giới thời KRX (ngày tạo 15/8/2025). Bản HSX: https://www.cts.vn/wp-content/uploads/2025/08/KRX_1.Quy-dinh-giao-dich-chung-khoan-tai-HSX.pdf . Bản HNX/UPCoM: https://www.cts.vn/wp-content/uploads/2025/08/KRX_2.Quy-dinh-giao-dich-chung-khoan-tai-HNX.pdf . Cả hai redirect sang vbse.vn.
- **S5**: Tài liệu SSI FastConnect Data. Streaming: https://guide.ssi.com.vn/ssi-products/fastconnect-data/streaming-data.md . REST: https://guide.ssi.com.vn/ssi-products/fastconnect-data/api-specs.md . Có bản tiếng Việt tương ứng.
- **S6**: Tin về đợt chuyển sàn 2026: vnexpress (1/10/2026) https://vnexpress.net/toan-bo-co-phieu-tren-san-ha-noi-chuyen-sang-hose-tu-28-12-5127079.html ; vietstock (5/10/2026) https://vietstock.vn/2026/10/chuyen-san-co-phieu-hnx-ve-hose-nha-dau-tu-can-luu-y-gi-145-1498821.htm ; cafef (8/10/2026) https://cafef.vn/chuyen-tu-san-hnx-ve-hose-vi-sao-co-phieu-co-the-khong-tang-gia-188261008073136336.chn ; thoibaotaichinhvietnam (30/9/2026) https://thoibaotaichinhvietnam.vn/chot-thoi-diem-chuyen-toan-bo-co-phieu-niem-yet-ve-hose-204623.html
- **S7**: Tiền lệ 2021 (chuyển HOSE→HNX), vneconomy 01/04/2021: https://vneconomy.vn/chuyen-giao-dich-sang-hnx-gia-tham-chieu-va-bien-do-se-tinh-the-nao.htm

## 1. Biên độ ngoài mức thường ±7/10/15%
| Trường hợp | HOSE | HNX (niêm yết) | UPCoM | Nguồn |
|---|---|---|---|---|
| Ngày thường | ±7% | ±10% | ±15% | VERIFIED S1 PL III 1.3/2.3; S3 Đ19.1; S4 |
| (a) Ngày GD đầu tiên, cổ phiếu mới | ±20% | ±30% | ±40%, "cho đến khi có giá tham chiếu được xác lập từ kết quả của phương thức khớp lệnh liên tục" | VERIFIED S1 PL III 1.4/2.4; S3 Đ19.2a; S4 |
| (b) Giao dịch trở lại sau tạm ngừng ≥25 phiên | ±20% | ±30% | ±40% ("không có giao dịch trên 25 ngày giao dịch liên tiếp") | VERIFIED S1; S2 Đ31.3b; S3 Đ19.2b; S4 |
| (c) Ngày không hưởng quyền: tách DN, cổ tức/thưởng bằng cổ phiếu quỹ, cổ tức tiền ≥ giá đóng cửa, TPCĐ cho cổ đông hiện hữu | ±20% | ±30% | ±40% (UPCoM: cổ tức tiền ≥ giá bình quân gia quyền) | VERIFIED S2 Đ31.3c–e; S1 PL III; S3 Đ19.2c–d |
| (d) Ngày đầu trên HOSE sau khi chuyển từ HNX (28/12/2026) | **chưa công bố** | — | — | VERIFIED S6 |

- Các mức 20/30/40% trên đều lấy từ S1 (2022) và S4 (8/2025). Bản 2026 (S2) giữ nguyên danh sách trường hợp đặc biệt ở Điều 31.3, nhưng tôi **không đọc được Phụ lục III của S2**. Vì vậy, mức % của HOSE/HNX năm 2026 chỉ có 2 nguồn đã đọc (S1 cũ, S4), chưa đủ 3 nguồn. Mức ±40% của UPCoM thì có 3 nguồn (S3 2026, bản 2025, S4).
- Ở HOSE, nếu "trong 03 ngày đầu tiên sau khi giao dịch trở lại ... vẫn chưa có giá đóng cửa", HOSE có thể điều chỉnh biên độ (VERIFIED S4). Như vậy biên độ đặc biệt **có thể kéo dài nhiều phiên**, không chỉ một ngày. Lead researcher đã đọc lại S2 Đ31.3 độc lập (2026-10-21). Theo văn bản 2026, với cả HOSE/HNX, ngày GD đầu tiên và trường hợp tạm ngừng ≥25 phiên đều áp biên độ Phụ lục III "cho đến khi xác lập được giá từ khớp lệnh lô chẵn" (VERIFIED S2). Đ32.1, 32.3, 32.4 xác nhận lại quy tắc làm tròn xuống/lên, bump ±1 bước, và sàn ≤0 thì lấy giá tham chiếu (VERIFIED S2, offset 100000).
- UBCKNN có quyền đổi biên độ "trong trường hợp cần thiết để ổn định thị trường" theo khoản 2 Điều 4 Thông tư 120/2020/TT-BTC (VERIFIED S3 Đ19.3). Lộ trình tái cấu trúc 2026 đang "nghiên cứu ... nới biên độ", nhưng chưa phải chính sách (VERIFIED cafef S6).
- **Giá tham chiếu:**
  - HOSE/HNX lấy giá đóng cửa phiên trước (S2 Đ33.2). Khi có quyền, giá đóng cửa được điều chỉnh theo cổ tức hoặc giá trị quyền (S2 Đ33.4). Kết quả "được làm tròn theo đơn vị yết giá" (S2 Đ33.8). Văn bản không nói rõ làm tròn lên hay xuống. VERIFIED.
  - Với cổ phiếu mới, giá tham chiếu do tổ chức niêm yết đề xuất. Sau tạm ngừng ≥25 phiên, giá tham chiếu "do SGDCK quyết định" (S2 Đ33.1, 33.3). VERIFIED.
  - **UPCoM khác HOSE/HNX:** giá tham chiếu là "bình quân gia quyền của các giá trong giao dịch lô chẵn" khớp lệnh liên tục của phiên gần nhất, không phải giá đóng cửa (VERIFIED S3 Đ20.4; S4).
- **(d) Chuyển sàn:** ngày cuối trên HNX là 23/12, tạm ngừng 24–25/12, phiên đầu trên HOSE là 28/12 (VERIFIED vnexpress S6). Theo vnexpress, "Cơ quan quản lý thị trường chưa thông tin phương án điều chỉnh biên độ". Vietstock khuyên "Không nên mặc định ... 20%" và "cũng không nên tự khẳng định sẽ áp dụng 7%" (VERIFIED S6, bài ngày 5/10/2026).
  - Tiền lệ 2021 (chiều ngược lại, HOSE→HNX, công văn `1109/UBCK-PTTT`): giá tham chiếu là "mức giá đóng cửa tại HOSE trong ngày giao dịch cuối cùng", làm tròn theo bước giá sàn mới; biên độ ±10%, tức biên độ thường của sàn đích (VERIFIED S7).
  - Suy ra cho đợt 2026: ngày 28/12 nhiều khả năng dùng ±7% với giá tham chiếu là giá đóng cửa HNX ngày 23/12. ASSUMED, cần thông báo chính thức.
  - Giá HNX luôn là bội số của 100, mà 100 là bội số của mọi bước giá HOSE, nên giá tham chiếu không bị lệch lưới. ASSUMED (suy luận).

## 2. Làm tròn và bước giá
- **Quy tắc** (VERIFIED, 3 văn bản chính: S2 Đ32.1, S1 Đ31.1, S3 Đ19.6): "làm tròn xuống đối với giá trần, làm tròn lên đối với giá sàn theo đơn vị yết giá".
  - Công thức gốc: `Giá trần = Giá tham chiếu + (Giá tham chiếu x Biên độ dao động giá)`.
- **Bump một bước giá** (VERIFIED S2 Đ32.3, S1 Đ31.3, S3 Đ19.5, S4): khi trần hoặc sàn sau làm tròn "bằng giá tham chiếu" thì `Giá trần điều chỉnh = Giá tham chiếu + 01 đơn vị yết giá` và `Giá sàn điều chỉnh = Giá tham chiếu - 01 đơn vị yết giá`.
- **Sàn không xuống 0** (VERIFIED S2 Đ32.4–5; S3 Đ19.6–7):
  - nếu sàn điều chỉnh "nhỏ hơn hoặc bằng không (0), giá sàn sẽ được điều chỉnh bằng giá tham chiếu";
  - nếu giá tham chiếu bằng đúng một bước giá thì trần = tham chiếu + 1 bước, sàn = tham chiếu.
- **Bước giá khớp lệnh:**
  - HOSE: cổ phiếu và CCQ đóng 10 đ (<10.000), 50 đ (10.000–49.950), 100 đ (≥50.000); ETF và chứng quyền 10 đ ở mọi mức giá. VERIFIED S1 PL III 1.2, S4 HSX, vietstock S6.
  - HNX: cổ phiếu 100 đ; ETF 1 đ. VERIFIED S1 PL III 2.2, S4.
  - UPCoM: 100 đ. VERIFIED S3 Đ18, S4.
  - **Giao dịch thỏa thuận có bước giá 1 đ**, nhưng giá vẫn phải "tuân theo quy định về biên độ dao động giá trong ngày". VERIFIED S2 Đ26.2, S1 Đ25.2, S4.
- Chứng quyền: trần/sàn suy ra từ cổ phiếu cơ sở theo tỷ lệ chuyển đổi; sàn ≤0 thì lấy 10 đ (VERIFIED S1 Đ31.2b). Nằm ngoài phạm vi `MarketTick` cổ phiếu.

## 3. Ví dụ số
Tính toán (OBSERVED 2026-10-21, python `decimal`, cài đặt đúng các quy tắc ở mục 2):

| Sàn | Ref | Băng chưa làm tròn | Sàn / Trần thực | % so với ref | Ra ngoài băng chưa làm tròn? |
|---|---|---|---|---|---|
| UPCoM | 500 | 425–575 | 400 / 600 (bump cả hai phía) | ±20% | **CÓ** |
| UPCoM | 600 | 510–690 | 500 / 700 (bump) | ±16,67% | **CÓ** |
| UPCoM | 700 | 595–805 | 600 / 800 | ±14,29% | không |
| UPCoM | 100 (= bước giá) | 85–115 | 100 / 200 | +100% / 0% | **CÓ** |
| HNX | 700 | 630–770 | 600 / 800 (bump) | ±14,29% | **CÓ** |
| HOSE | 1.000 | 930–1.070 | 930 / 1.070 | ±7% | không |
| HOSE | 1.230 | 1.143,9–1.316,1 | 1.150 / 1.310 | ±6,50% | không |
| HOSE | 100 | 93–107 | 90 / 110 (bump) | ±10% | **CÓ** |
| HOSE | 25.350 | 23.575,5–27.124,5 | 23.600 / 27.100 | ±6,90% | không |

- Ngưỡng bump: UPCoM khi ref < ~667 đ, HNX khi ref < 1.000 đ, HOSE khi ref < ~143 đ. Tính toán (OBSERVED 2026-10-21, python).
- Kiểm chéo với mẫu trong tài liệu SSI (S5): tính lại thì khớp cả bốn mẫu. Tính toán (OBSERVED 2026-10-21, python).

| Mẫu SSI | Ref | Trần / Sàn trong mẫu | Băng |
|---|---|---|---|
| ACB (HOSE) | 22.900 | 24.500 / 21.300 | ±7% |
| TAR (HNX) | 21.300 | 23.400 / 19.200 | ±10% |
| MBB (HOSE) | 22.650 | 24.200 / 21.100 | ±7% |
| DailyStockPrice (HOSE) | 20.150 | 21.550 / 18.750 | ±7% |

  Kết luận từ việc khớp này: quy tắc làm tròn là đúng, và mẫu dùng **đơn vị VND** (ví dụ ACB 22.900, không phải 22,9).
- Code hiện tại `round(ref*(1±b), 2)` cho trần 27.124,5 với ref 25.350. Giá trị này không nằm trên lưới bước giá và rộng hơn trần thật (27.100).

## 4. SSI FastConnect Data có cấp sẵn trần/sàn/tham chiếu không?
Có. Tất cả VERIFIED S5; tôi đọc tài liệu chứ chưa gọi API.
- Streaming `X-TRADE` và `X` (Securities snapshot) có các trường `Ceiling`, `Floor`, `RefPrice` (number), cùng `PriorVal`, `LastPrice`, `MarketId`, `Exchange`, `TradingSession`, `TradingStatus`. Ví dụ: `"Ceiling":24500.0, "Floor":21300.0, "RefPrice":22900.0` (ACB, HOSE).
- `OL` (lô lẻ) cũng có `Ceiling`, `Floor`, `RefPrice`. `X-QUOTE` **không** có các trường này. Ở `MI` (chỉ số), `Ceiling`/`Floor` là *số mã* chạm trần/sàn, không phải giá.
- REST `Market/DailyStockPrice`:
  - Bảng mô tả ghi `Ceilingprice`, `Floorprice`, `Refprice`, `Closeprice`, `Closepriceadjusted`, kiểu **string**.
  - JSON mẫu lại viết `CeilingPrice: "21550"`, `FloorPrice`, `RefPrice`, tức chữ hoa/thường khác bảng mô tả. Nên parse không phân biệt hoa thường và ép kiểu từ string.
- REST `Market/SecuritiesDetails` có `TickPrice1..4` và `TickIncrement1..4`, tức bảng bước giá theo từng mã.
- Mã trạng thái: `N`, `D`, `H`, `S`, `NL` (New List), `ND`, `ST`, `SA`, `SP`. Mã phiên: `ATO`, `LO`, `ATC`, `PT`, `C`, `BREAK`, `HALT`. Nguồn: https://guide.ssi.com.vn/ssi-products/fastconnect-data/data-mapping/securities-trading-status.md
- Đơn vị giá: tài liệu **không ghi rõ**. Giá trị mẫu cho thấy đơn vị là VND. ASSUMED cho tới lần gọi thật ở Phase 6, vì quyết định §9.5 chưa cho phép dùng SSI client trước đó.
- vnstock có thể trả giá theo **nghìn VND**. PRIOR, chưa kiểm lại; dự án đang ghi là ASSUMED chờ probe Phase 0.

## 5. Nếu kiểm theo băng thường chưa làm tròn và raise, phiên nào sẽ raise sai?
1. **Ngày GD đầu tiên, giao dịch trở lại sau ≥25 phiên, và các ngày không hưởng quyền đặc biệt** (mục 1 a–c): giá hợp lệ tới ±20/30/40%. Với UPCoM, trạng thái này có thể kéo dài nhiều phiên. Đây là nguồn raise sai lớn nhất. VERIFIED quy tắc; tần suất ASSUMED.
2. **Bump một bước ở cổ phiếu giá thấp** (mục 3): trần/sàn thật nằm ngoài băng chưa làm tròn, tới ±20% (UPCoM 500 đ) hoặc +100% (ref = bước giá). Tính toán (OBSERVED 2026-10-21, python).
3. **Lệch đơn vị:** giá tính bằng nghìn VND trong khi ref tính bằng VND (hoặc ngược lại) thì luôn raise. Nếu cả hai cùng tính bằng nghìn VND thì băng % vẫn qua, nhưng phép làm tròn theo bước giá sẽ sai. ASSUMED.
4. **Sai giá tham chiếu:** UPCoM lấy VWAP của phiên trước. Nếu code tự lấy giá đóng cửa làm ref thì sẽ raise sai. Ngày không hưởng quyền cũng vậy nếu dùng giá đóng cửa chưa điều chỉnh. VERIFIED quy tắc (S3 Đ20.4, S2 Đ33.4).
5. **Ngày chuyển sàn 28/12/2026** và vài phiên quanh đó: biên độ chưa công bố. Nếu `exchange` vẫn ghi HNX thì kiểm theo ±10%; nếu đổi sang HOSE thì ±7%. Cả hai đều có thể sai. VERIFIED S6.
6. **Giao dịch thỏa thuận:** vẫn nằm trong băng, nhưng giá có thể lẻ tới 1 đ. Chỉ raise sai nếu code ép giá phải nằm trên lưới bước giá. VERIFIED S2 Đ26.2.
7. **ATO/ATC:** giá ATO/ATC được hệ thống chặn trong trần/sàn ("thấp hơn mức giá sàn thì hệ thống giao dịch ghi nhận là giá sàn", VERIFIED S1 Đ17). Vì vậy không gây raise sai, trừ khi lỗi thuộc các mục 1–4.
- Chiều ngược lại: với giá ≥ ~1.000 đ, băng chưa làm tròn **rộng hơn** băng thật, nên kiểm theo nó không raise sai mà chỉ bỏ sót các giá nằm giữa trần thật và trần lý thuyết.

## Kết luận (xếp hạng)
1. **Ưu tiên trần/sàn do feed cấp** (SSI `Ceiling`/`Floor`/`RefPrice`; REST `CeilingPrice`/`FloorPrice`/`RefPrice`). Thêm vào `MarketTick` hai trường tùy chọn `ceiling_price`/`floor_price` và kiểm chặt `floor ≤ price ≤ ceiling`, không cần dung sai. Khi có cả hai trường, chỉ cần đối chiếu với giá trị tự tính để cảnh báo, không raise.
2. **Nếu feed không có, tính bằng hàm thuần** `vn_price_limits(exchange, reference_price, band)`:
   - dùng `Decimal`;
   - trần làm tròn xuống, sàn làm tròn lên theo bước giá;
   - bump ±1 bước khi trùng ref; sàn ≤0 thì lấy ref; ref = bước giá thì sàn = ref.
   Bảng biên độ nên tra theo `(exchange, session_kind)`, với `session_kind ∈ {normal, first_day, resumed_25, ex_rights_special}`, mặc định `normal`. Có hai cách dùng bảng này:
   - Cách 1: chỉ dùng HOSE/HNX/UPCOM mức thường mà mục 1 đã VERIFIED, còn các ngày đặc biệt bắt buộc truyền `band` tường minh.
   - Cách 2: mã hóa thêm cả 20/30/40%. Tôi khuyên cách 1, vì Phụ lục III của bản 2026 chưa đọc được.
3. **Cho phép ghi đè tường minh:** `band_override: Decimal` hoặc `price_limits_override: (floor, ceiling)`. Bắt buộc kèm `override_reason` (enum, gồm cả `exchange_transfer`) để lưu vào bản ghi PIT. Cơ chế này dùng cho ngày chuyển sàn 28/12/2026 và mọi quyết định đột xuất của UBCKNN.
4. **Dung sai:** chỉ dùng khi buộc phải kiểm theo băng chưa làm tròn (không có bước giá, hoặc nguồn không rõ). Khi đó nới thêm **đúng 1 bước giá** mỗi phía, đủ để bao các trường hợp bump. Tính toán (OBSERVED, python): UPCoM ref 500 cho 575+100 ≥ 600; ref 100 cho 115+100 ≥ 200. Không áp kiểm lưới bước giá cho giao dịch thỏa thuận (`TradingSession == "PT"`).
5. **Sàn lạ thì raise** như kế hoạch. Nhưng sàn phải được xác định as-of ngày giao dịch: cổ phiếu niêm yết HNX hết giao dịch sau 23/12/2026 và lên HOSE từ 28/12/2026. Chuẩn hóa đơn vị về VND ngay ở connector, không làm trong `MarketTick`. Có thể thêm guard tỉ lệ `price/ref ∉ [0.5, 2]` để báo lỗi đơn vị với thông điệp rõ ràng (ASSUMED ngưỡng).
6. Với dữ liệu lịch sử (bar ngày), kiểm `low ≥ floor` và `high ≤ ceiling`. Phiên nào vi phạm thì gắn cờ `pit_quality`, thay vì raise làm hỏng cả lô ingest.

## Câu hỏi còn mở
- Phụ lục III của `22/QĐ-HĐTV` (16/03/2026): HOSE ±20% và HNX ±30% còn nguyên không? Cần bản toàn văn (luatvietnam yêu cầu đăng nhập) hoặc trang vietnamexchange.vn/vi/van-ban-phap-ly/6.
- Biên độ và giá tham chiếu chính thức cho phiên 28/12/2026 của khoảng 300 mã chuyển từ HNX. Cần theo dõi thông báo VNX/HOSE, và cập nhật Verified Facts khi có.
- Ở HOSE, khi giá vượt mốc 10.000 hoặc 50.000 thì làm tròn theo bước giá của mức nào? Ví dụ ref 9.380 cho trần lý thuyết 10.036,6: nếu dùng bước 50 đ thì trần là 10.000, nếu dùng bước 10 đ thì trần là 10.030. Văn bản chỉ ghi "theo đơn vị yết giá". Có thể kiểm bằng dữ liệu thật hoặc `TickPrice*`/`TickIncrement*` của SSI. ASSUMED: dùng bước giá của mức giá được tính.
- Giá tham chiếu VWAP của UPCoM có được làm tròn theo bước 100 đ không? Phần S3 tôi đọc được không thấy câu nào về việc này.
- Đơn vị giá thật của SSI và vnstock: cần probe khi quyết định cho phép (vnstock ở Phase 0; SSI ở Phase 6).
- Sau khi chuyển sàn, UPCoM vẫn do SGDCKHN vận hành (S3 Đ11.1: "hệ thống giao dịch UPCoM của SGDCKHN"). Mã `HNX` trong feed có còn mã cổ phiếu nào sau 28/12/2026 không?
