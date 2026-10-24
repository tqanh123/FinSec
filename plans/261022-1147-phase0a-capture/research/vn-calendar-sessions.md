# Research: lịch giao dịch VN, giờ phiên, thời điểm công bố thông tin (cho lịch poller và bước counts / go-no-go)

- Ngày: 2026-10-22. Chế độ: `depth`. Người viết: hs-researcher (subagent), không cài gói, không chạy file tải về.
- Phạm vi: (1) giờ phiên KRX và cửa sổ poll; (2) lịch nghỉ 2026 còn lại và 2027, quy tắc và các kiểu sai; (3) hạn công bố thông tin theo Circular 96/2020 và các ngày cụ thể của Q3 và Q4 2026; (4) counts kỳ vọng; (5) power rule và go/no-go cho R1, D2, D4; (6) cửa sổ 5 phiên cho exit gate.
- Ngoài phạm vi: probe vnstock, EDGAR, Alpaca (đã có các báo cáo cùng thư mục); nội dung luật ngoài các điều được trích.
- Ghi chú đường dẫn: task chỉ định `H:\study\Project\FinSec\plans\...` (main checkout). Hook PreToolUse chặn ghi vào main checkout từ worktree, nên báo cáo nằm ở bản worktree của cùng thư mục plan, cạnh `alpaca-probe.md`, `edgar-header.md`, `vnstock-gen5.md` và `capture-host-windows.md`.

**Nhãn bằng chứng dùng trong báo cáo**

| Nhãn | Nghĩa |
|---|---|
| VERIFIED | Tôi đã tải trang ngày 2026-10-22 và đọc đúng đoạn được trích (URL có kèm). |
| SNIPPET | Chỉ thấy trong phần tóm tắt của WebSearch, không tự tải trang. Yếu hơn VERIFIED. Khi gate một quyết định thì coi như ASSUMED. |
| OBSERVED | Tôi tự chạy lệnh hoặc request, có ghi lệnh và kết quả. |
| ASSUMED | Chưa kiểm. |
| PRIOR | Kiến thức huấn luyện của tôi, chưa kiểm lại. |
| DERIVED | Tôi tính ra từ các dữ kiện đã có nhãn. Độ tin cậy không cao hơn dữ kiện đầu vào. |

---

## 0. Tóm tắt (5 dòng)

1. **Cửa sổ 08:00-17:30 ICT kèm poll hằng giờ ngoài khung là không đủ cho mục tiêu độ trễ phát hiện VN dưới 10 phút.**
   - Circular 96 cho phép công bố bất thường "trong thời hạn 24 giờ" (Art. 11, 15).
   - Công bố rơi vào ngày nghỉ thì đăng lên website công ty trước, rồi đăng đủ vào ngày làm việc kế tiếp (Art. 4 cl. 7-8).
   - Chính các thông báo VSDC tôi tải về có giờ đăng 17:59:55 và 18:07:33.
   - Khuyến nghị: tách lịch capture khỏi lịch giao dịch. Trang danh sách của portal được poll đều 24/7. vnstock quét vòng theo budget rate limit.
2. **Lịch 2026 còn đúng một ngày nghỉ: thứ Ba 2026-11-24 (Ngày Văn hóa Việt Nam).** Ngày này do Nghị quyết 28/2026/QH16 đặt ra, Phó Thủ tướng đã duyệt phương án nghỉ 1 ngày không hoán đổi, và VSDC đã đưa ngày này vào lịch không thanh toán. Tôi chưa thấy thông báo nghỉ giao dịch của HOSE hay HNX.
3. **Hạn BCTC quý là 20 ngày; công ty mẹ là 30 ngày; quý có soát xét là 45 ngày.** Đề bài ghi "45 ngày cho công ty mẹ/hợp nhất" là sai, theo văn bản gốc tôi đã trích.
   - Q3/2026: 2026-10-20 (thứ Ba) và 2026-10-30 (thứ Sáu).
   - Q4/2026: 2027-01-20 (thứ Tư) và 2027-01-30 (thứ Bảy). Thứ Bảy có thể được lùi sang 2027-02-01; quy tắc lùi ngày là PRIOR.
4. **Power rule t≈IC·√N chỉ cho power 50% khi t=2.**
   - Nếu muốn power 80% thì IC 0.03 cần khoảng 8,700 sự kiện độc lập, không phải 4,400.
   - Tôi đề xuất go/no-go 4 bậc theo MDE (minimum detectable IC) tính trên N_eff đã hiệu chỉnh cụm ngày. Ngưỡng NO-GO là MDE > 0.10: khi đó bất kỳ hiệu ứng nào phát hiện được cũng tự kích hoạt leakage audit (§2.4).
   - D2 với BCTC gộp nhiều khả năng ở mức GO-limited. D4 là NO-GO cho scoreboard đến hết Phase 4. R1-VN phụ thuộc tỉ lệ sự kiện có giờ trong ngày.
5. **Exit gate 5 phiên:** các tuần 02-06/11, 09-13/11, 16-20/11, 30/11-04/12, 07-11/12 và 14-18/12/2026 đều sạch. Tuần 23-27/11 bị cắt bởi ngày 24/11. Nên tránh 21-25/12 vì đợt chuyển HNX sang HOSE.

---

## 1. Giờ phiên trên hệ thống KRX (go-live 2025-05-05)

### 1.1 Lịch phiên

| Sàn | ATO | Liên tục sáng | Nghỉ trưa | Liên tục chiều | ATC | Sau ATC | Nhãn |
|---|---|---|---|---|---|---|---|
| HOSE | 09:00-09:15 | 09:15-11:30 | 11:30-13:00 | 13:00-14:30 | 14:30-14:45 | thỏa thuận sau giờ 14:45-15:00 | VERIFIED (môi giới, thứ cấp) DNSE FAQ: "Khớp lệnh định kỳ mở cửa: 9h00-9h15", "Khớp lệnh định kỳ đóng cửa: 14h30-14h45", "tách phiên giao dịch thỏa thuận từ 14h45 - 15h00 thành phiên "thỏa thuận sau giờ"" (https://hdsd.dnse.com.vn/master/nhung-thay-doi-quan-trong-cua-he-thong-krx/faq-krx-thi-truong-co-so). HSC, 25.04.2025: "Main trading session: 09:00-14:45", "Post-market trading session: 14:45-15:00" cho lệnh thỏa thuận (https://www.hsc.com.vn/en/important-changes-of-new-trading-system). SNIPPET: DSC và Simplize ghi 9h15-11h30 và 13h-14h30 |
| HNX (đến 2026-12-23) | không có (SNIPPET ACBS: "KLĐK mở cửa từ 09h00 đến 09h15 chỉ áp dụng tại HSX") | 09:00-11:30 | 11:30-13:00 | 13:00-14:30 | 14:30-14:45 | PLO / khớp sau giờ 14:45-15:00 | SNIPPET (Finhay, ACBS, Shinhan "G3 Bảng giao dịch sau giờ từ 14h45 đến 15h cho STX"). DNSE FAQ có nói HNX được bổ sung ATC nhưng không ghi giờ (VERIFIED) |
| UPCoM | không | 09:00-11:30 | 11:30-13:00 | 13:00-? | ? | không có PLO (SNIPPET Finhay) | ASSUMED |

- **Văn bản gốc chưa đọc được.** Quy chế HOSE/HNX (bản PDF trên cts.vn, chuyển hướng sang vbse.vn) được nén FlateDecode. Máy không có thư viện PDF, và luật của phiên này cấm cài thêm. Vì vậy giờ phiên theo văn bản gốc vẫn là ASSUMED (OBSERVED: WebFetch chỉ trả về metadata `CreationDate 2025-08-15`).
- **Kết luận cho roadmap §2.1:** VN close = kết thúc ATC 14:45 ICT (= 07:45 UTC) vẫn đúng.
  - Phiên thỏa thuận sau giờ và PLO 14:45-15:00 không tạo ra giá đóng cửa. Chúng chỉ là giao dịch phát sinh sau close.
  - Từ 2026-12-28 mọi cổ phiếu niêm yết giao dịch trên HOSE, nên chỉ còn một lịch phiên cho cổ phiếu niêm yết (VERIFIED từ báo chí thứ cấp, theo AGENTS.md).
- **Múi giờ.** ICT = UTC+7, không có DST (PRIOR). `.venv` không có `tzdata`, nên `ZoneInfo('Asia/Ho_Chi_Minh')` sẽ lỗi giống `America/New_York` (OBSERVED bởi phiên chính).
  - Plan phải thêm `tzdata` vào dependency, hoặc dùng offset cố định `timezone(timedelta(hours=7))` cho VN.
  - Bản thân vnstock 5.1.0 cũng khai `tzdata ; sys_platform == 'win32'` (OBSERVED bởi phiên chính).

### 1.2 Cửa sổ 08:00-17:30 có phủ giờ công bố không?

- **Về quy định: không.**
  - Circular 96 Art. 11 cl. 1 và Art. 15 cl. 2: "phải công bố thông tin bất thường trong thời hạn 24 giờ, kể từ khi xảy ra một trong các sự kiện sau đây". Hạn tính theo giờ, không theo giờ hành chính.
  - Art. 4 cl. 7: "Trường hợp nghĩa vụ công bố thông tin phát sinh vào ngày nghi, ngày lễ … thực hiện công bố thông tin trên phương tiện quy định tại điểm a khoản 1 Điều này [website công ty] và thực hiện đầy đủ nghĩa vụ công bố thông tin … vào ngày làm việc liền sau ngày nghi, ngày lễ."
  - Hệ quả: (a) ngày nghỉ vẫn có công bố, nằm trên website công ty; (b) portal sẽ có một đợt dồn vào sáng của ngày làm việc kế tiếp.
  - Nguồn: VERIFIED, toàn văn Circular 96 trên https://caselaw.vn/van-ban-phap-luat/372205-thong-tu-so-96-2020-tt-btc-ngay-16-11-2020-cua-bo-truong-bo-tai-chinh-huong-dan-cong-bo-thong-tin-tren-thi-truong-chung-khoan (OBSERVED: `curl` trả 200, 724,935 byte).
- **Về thực nghiệm: có bằng chứng yếu.**
  - Sáu thông báo VSDC tôi đọc có giờ đăng: 16:58:13, 17:59:55, 16:27:15, 15:41:47, 16:14:52, 18:07:33. Hai trên sáu đăng sau 17:30.
  - Đây là thông báo của VSDC, không phải của doanh nghiệp, nên chỉ cho thấy giờ đăng trên portal chính thức thường rơi vào cuối giờ hành chính hoặc sau đó.
  - Nguồn: OBSERVED, `curl -sk -L https://vsdc.vn/en/ad/{198931,190545,191685,182206,198399}` ngày 2026-10-22, trường "Date update".
- **Tôi không tìm được nghiên cứu nào đo phân bố giờ công bố trong ngày của doanh nghiệp VN.** Vài nghiên cứu học thuật chỉ đo độ trễ tính theo ngày từ cuối kỳ (SNIPPET https://rsujournals.rsu.ac.th/index.php/aisc/article/view/3345).
  - "Nhiều công bố ra sau giờ đóng cửa hoặc buổi tối" vẫn là ASSUMED.
  - Bước đối chiếu tay 30 công bố trong Phase 0a là probe quyết định câu hỏi này (xem Câu hỏi mở Q1).
- **Độ trễ do lịch roadmap gây ra (DERIVED).**
  - Poll hằng giờ cho độ trễ trung vị khoảng 30 phút với mọi công bố sau 17:30, ngày nghỉ và cuối tuần.
  - Nếu từ 20% công bố trở lên rơi vào các khung đó thì trung vị toàn kỳ vẫn có thể dưới 10 phút, nhưng phần đuôi p90 sẽ lên khoảng 54 phút.
  - Riêng nhóm sự kiện ngoài giờ, vốn có day 0 là phiên kế tiếp, thì độ trễ không ảnh hưởng nhãn. Nó ảnh hưởng `first_seen_at` và tính cạnh tranh "earliest source wins".

### 1.3 Ma trận đánh đổi cho lịch poll

| Phương án | Mô tả | Độ trễ ngoài giờ | Số request/ngày (portal 1 trang + vnstock U mã × E endpoint) | Phụ thuộc lịch giao dịch | Xếp hạng |
|---|---|---|---|---|---|
| A. Roadmap nguyên trạng | 2-5' từ 08:00-17:30 vào ngày giao dịch; hằng giờ ngoài khung | ~30' trung vị | portal ~140-290; vnstock: mỗi lượt poll cần U·E call (200×2=400) | **Có**: lịch sai thì poll thưa đi | 3 |
| B. Mở rộng theo bậc | 2-5' từ 08:00-15:00; 5-10' từ 15:00-22:00; 30' từ 22:00-08:00; ngày nghỉ 15' từ 08:00-22:00 | ~3-5' tới 22:00 | portal ~250-350 | Có, nhưng nhẹ hơn | 2 |
| **C. Tách hai tầng (khuyến nghị)** | Portal: 1 trang danh sách mỗi 3-5', 24/7, không phụ thuộc lịch. vnstock: quét vòng U mã theo budget rate limit (ví dụ 1 lượt/10-15' ban ngày, 1 lượt/60' ban đêm) để làm giàu và đối chiếu | ~3-5' với portal | portal 288-480 (không đáng kể); vnstock bị chặn bởi rate limit thật | **Không** cho capture; lịch chỉ dùng khi gán nhãn day 0 (Phase 3) | **1** |

- **Vì sao C đứng đầu.**
  - (a) Nó gỡ lịch giao dịch khỏi đường capture. Lịch sai, như 2/1/2026 bị thêm vào chỉ một tuần trước (§2.3), không làm hụt `first_seen_at`.
  - (b) Bài toán ngân sách request có lời giải. Quét 200 mã × 2 endpoint mỗi 2-5 phút là 400 call/lượt, tức khoảng 115k-290k call/ngày, gần như chắc chắn vượt hạn mức Community. Hạn mức thật là ASSUMED, chờ probe 1.
  - (c) "Gap" trong outage log có một định nghĩa duy nhất: khoảng giữa hai lần poll portal thành công vượt 2× chu kỳ danh nghĩa.
- **Rủi ro của C.**
  - Portal chính thức có thể chậm hoặc chỉ là SPA. OBSERVED: `https://www.hsx.vn/` trả về vỏ SPA 1,900 byte; bundle `/static/js/main.d430e296.js` timeout sau 120 giây (curl exit 28).
  - Việc chọn portal và kiểm robots/ToS vẫn là bước chặn của owner.

---

## 2. Lịch nghỉ giao dịch

### 2.1 Năm 2026

| Dịp | Ngày nghỉ GD | Trạng thái | Nguồn |
|---|---|---|---|
| Tết Dương lịch | Thu 01/01 + Fri 02/01 | đã qua | VERIFIED VSDC (https://vsdc.vn/en/ad/190545): "Tết Dương lịch Thursday, January 1, 2026 and Friday, January 2, 2026 (working days swapped for holidays)", căn cứ "Document No. 12729/VPCP-KGVX dated December 25, 2025"; làm bù "Saturday, January 10, 2026" |
| Tết Nguyên đán | Mon 16/02 - Fri 20/02 | đã qua | VERIFIED VSDC (https://vsdc.vn/en/ad/191685): "from Monday, 16/02/2026 … to Friday, 20/02/2026" |
| Giỗ Tổ (10/3 âm lịch rơi vào CN 26/4) | Mon 27/04 (nghỉ bù) | đã qua | SNIPPET nhandan/HNX; ASSUMED (chưa đọc VSDC 194660) |
| 30/4 + 1/5 | Thu 30/04 - Fri 01/05 | đã qua | SNIPPET |
| Quốc khánh | Mon 31/08 - Wed 02/09; làm bù Sat 22/08 (không giao dịch) | đã qua | VERIFIED VSDC (https://vsdc.vn/en/ad/198931): "VSDC will be closed from Monday, 31/8/2026 to Wednesday, 02/9/2026, and will work on Saturday, 22/8/2026. On this Saturday, VSDC will not conduct clearing and settlement" |
| **Ngày Văn hóa Việt Nam** | **Tue 24/11/2026** | **sắp tới** | xem dưới |
| Chuyển HNX sang HOSE | ~299 mã: ngày GD cuối trên HNX Wed 23/12, trên HOSE từ Mon 28/12. **Thu 24/12 và Fri 25/12 có thể không giao dịch với nhóm mã này** | ASSUMED | AGENTS.md (VERIFIED từ báo chí thứ cấp, cho 2 mốc ngày); việc 24-25/12 không giao dịch là DERIVED |

**Bằng chứng cho 24/11/2026 (3 nguồn độc lập):**
1. **VSDC.** VERIFIED https://vsdc.vn/en/ad/198399 (đăng 21/07/2026 16:14:52): "Pursuant to Resolution No. 28/2026/QH16 of the National Assembly dated 24/4/2026 (effective from 01/7/2026) … To add 24/11/2026 as "Vietnam Culture Day" to 2026 Non-Settlement Schedule … Tuesday, 24/11/2026".
2. **Phó Thủ tướng duyệt nghỉ 1 ngày, không hoán đổi.** VERIFIED https://baoquocte.vn/chot-lich-nghi-tet-nguyen-dan-dinh-mui-7-ngay-451229.html (03/10/2026): "nghỉ 1 ngày, nhân ngày 24/11", thứ Ba. Phương án nghỉ 4 ngày (21-24/11, làm bù 28/11) đã bị Bộ Nội vụ rút lại (VERIFIED https://daibieunhandan.vn/dieu-chinh-phuong-an-nghi-ngay-van-hoa-viet-nam-nam-2026-10431116.html, 20/09/2026; https://mekongasean.vn/bo-noi-vu-de-xuat-chi-nghi-mot-ngay-dip-ngay-van-hoa-viet-nam-59803.html, 21/09/2026: "giữ nguyên lịch làm việc ngày 23/11").
3. **Nghị quyết.** VERIFIED https://fptshop.com.vn/tin-tuc/for-gamers/nghi-le-24-11-huong-nguyen-luong-209135: "Nghị quyết số 28/2026/QH16 được Quốc hội thông qua ngày 24/4/2026 … có hiệu lực từ 01/7/2026".

- **Còn thiếu thông báo nghỉ giao dịch của chính HOSE/HNX cho 24/11.** Tôi không tìm thấy, nên "HOSE đóng cửa 24/11" là ASSUMED, dù gần như chắc chắn: VSDC không thanh toán ngày đó, và tiền lệ cho thấy sàn nghỉ trùng lịch VSDC.
- **23/11 (thứ Hai) là ngày giao dịch bình thường** (DERIVED từ phương án đã duyệt).

### 2.2 Năm 2027 (tạm tính; lịch chính thức của sàn và VSDC chưa ban hành)

| Dịp | Ngày nghỉ GD dự kiến | Nhãn và nguồn |
|---|---|---|
| Tết Dương lịch | Fri 01/01/2027 | DERIVED (Labour Code Art. 112: 1 ngày); có thể bị hoán đổi muộn như năm 2026 |
| Tết Đinh Mùi | Thu 04/02 - Wed 10/02/2027 (5 phiên) | VERIFIED cho công chức: https://baoquocte.vn/chot-lich-nghi-tet-nguyen-dan-dinh-mui-7-ngay-451229.html ("từ thứ Năm, ngày 4/2/2027 đến hết thứ Tư, ngày 10/2/2027", "5 ngày nghỉ Tết theo quy định và 2 ngày nghỉ bù") và https://cafef.vn/bo-noi-vu-thong-bao-chi-tiet-lich-nghi-tet-am-lich-quoc-khanh-2027-188261010120450778.chn (10/10/2026). Với sàn: ASSUMED |
| Giỗ Tổ (10/3 âm lịch) | Fri 16/04/2027 | SNIPPET officeholidays.com (một nguồn; Twinkl ghi 18/4, mâu thuẫn). ASSUMED |
| 30/4 + 1/5 | Fri 30/04 + **Mon 03/05/2027** (nghỉ bù vì 1/5 rơi vào thứ Bảy) | DERIVED từ Labour Code Art. 111 ("nghỉ bù … vào ngày làm việc kế tiếp", SNIPPET); ASSUMED với sàn |
| Quốc khánh | Thu 02/09 + Fri 03/09/2027 | VERIFIED cho công chức (cafef 10/10/2026: "2/9 và 1 ngày liền kề sau là ngày 3/9/2027") |
| Ngày Văn hóa VN | Wed 24/11/2027 | DERIVED (NQ 28/2026/QH16, hằng năm) |

- **Thời điểm có lịch chính thức.** Lịch 2026 của VSDC là "4228/TB-VSDC dated 20/11/2025" (VERIFIED, trích trong https://vsdc.vn/en/ad/198399). Lịch HOSE 2026 là "Thông báo 2294/TB-SGDHCM năm 2025" (SNIPPET thuvienphapluat).
- **Dự báo (ASSUMED theo tiền lệ):** lịch 2027 sẽ ra khoảng cuối tháng 11/2026. Plan nên coi bảng 2027 ở trên là bản nháp, cần thay khi có thông báo.

### 2.3 Quy tắc "ngày thường trừ danh sách nghỉ" và các kiểu sai

**Quy tắc đề xuất cho Phase 3** (capture theo phương án C không cần quy tắc này):

```
is_trading_day(exchange, d) = d.weekday() < 5
                              and d not in closures[exchange][d.year]
```

- `closures` nằm trong YAML có version, mỗi dòng có `source_url`, `notice_id`, `fetched_at`.
- Thứ Bảy làm bù không bao giờ là ngày giao dịch.

**Các kiểu sai đã quan sát:**

| # | Kiểu sai | Ví dụ có bằng chứng | Biện pháp |
|---|---|---|---|
| F1 | Hoán đổi được quyết muộn, thêm một ngày nghỉ vào lịch năm đã công bố | 02/01/2026 được thêm ngày 25-26/12/2025, sáu ngày trước khi có hiệu lực (VERIFIED VSDC 190545: lịch gốc 4228 ngày 20/11/2025 chỉ có 01/01) | Đối chiếu lại `closures` mỗi tuần với trang tin VSDC; coi "đã có bar của VNINDEX ngày d" là sự thật sau cùng |
| F2 | Ngày lễ mới được luật thêm vào giữa năm | 24/11/2026, NQ 28/2026/QH16 có hiệu lực 01/07/2026, VSDC bổ sung 21/07/2026 (VERIFIED) | Như F1 |
| F3 | Thứ Bảy làm bù bị tính nhầm là ngày giao dịch | 10/01/2026, 22/08/2026: VSDC "will not conduct clearing and settlement" (VERIFIED) | Không bao giờ coi thứ Bảy hay Chủ nhật là ngày giao dịch |
| F4 | Thứ Hai bị hoán đổi thành ngày nghỉ, quy tắc "chỉ lễ cố định" bỏ sót | 31/08/2026 (VERIFIED VSDC 198931) | Đọc ngày nghỉ từ thông báo, không suy ra từ Labour Code |
| F5 | Đóng cửa riêng một sàn hoặc một nhóm mã | 24-25/12/2026 với các mã chuyển từ HNX (ASSUMED) | `closures` tách theo sàn, cộng ngoại lệ theo nhóm mã |
| F6 | Ngừng giao dịch đột xuất (sự cố hệ thống, ngừng một mã) | Không tìm được ví dụ 2025-2026 (ASSUMED là có thể xảy ra) | Đối chiếu sau với bar ngày; ngừng một mã là trạng thái của mã, không phải của lịch |
| F7 | Lịch âm tính sai (Tết, Giỗ Tổ) | 2027: hai nguồn thứ cấp cho Giỗ Tổ lệch nhau (16/4 và 18/4) | Không tự tính lịch âm; chờ thông báo |

---

## 3. Hạn công bố thông tin (nguồn tạo đỉnh khối lượng công bố)

**Văn bản.** Circular 96/2020/TT-BTC, sửa đổi bởi 68/2024, 18/2025 và 08/2026.
- Theo trang tóm tắt hethongphapluat (VERIFIED, trang tóm tắt), 68/2024 Art. 4 và 08/2026 không sửa Art. 10 và Art. 14.
- Nội dung của 18/2025 tôi chưa đọc được (ASSUMED là không sửa Art. 14).
- Bằng chứng gián tiếp rằng hạn 20 ngày còn hiệu lực năm 2026: HOSE nhắc nhở VMD về BCTC quý I/2026, trích "điểm c khoản 3 Điều 14 … 20 ngày kể từ ngày kết thúc quý" (VERIFIED https://doanhnhan.baophapluat.vn/y-duoc-pham-vimedimex-vmd-bi-nhac-nho-vi-cham-nop-bao-cao-tai-chinh-quy-i-2026.html, 08/05/2026).

**Trích nguyên văn** (VERIFIED, caselaw.vn, toàn văn bản gốc 2020):

| Loại | Thời hạn | Trích |
|---|---|---|
| BCTC quý | 20 ngày | Art. 14.3.c: "phải công bố báo cáo tài chính quý trong thời hạn 20 ngày kể từ ngày kết thúc quý" |
| BCTC quý, công ty mẹ hoặc đơn vị cấp trên | **30 ngày** (không phải 45) | "là công ty mẹ của tổ chức khác … thì phải công bố báo cáo tài chính quý trong thời hạn 30 ngày, kể từ ngày kết thúc quý" |
| BCTC quý có soát xét | 5 ngày sau khi ký soát xét, tối đa 45 ngày | "trong thời hạn 05 ngày, kể từ ngày tổ chức kiểm toán ký báo cáo soát xét nhưng không được vượt quá 45 ngày" |
| BCTC bán niên soát xét | 5 ngày, tối đa 45 ngày; công ty mẹ tối đa 60 ngày | Art. 14.2.c: "… không quá 60 ngày, kể từ ngày kết thúc 06 tháng đầu năm tài chính" |
| BCTC năm kiểm toán | 10 ngày sau khi ký, tối đa 90 ngày | Art. 10.1.c: "trong thời hạn 10 ngày, kể từ ngày tổ chức kiểm toán ký báo cáo kiểm toán nhưng không vượt quá 90 ngày, kể từ ngày kết thúc năm tài chính" |
| Báo cáo thường niên | 20 ngày sau BCTC năm (giới hạn trên bị cắt khi trích; 110 ngày là PRIOR) | Art. 10.2 |
| Giải trình lợi nhuận | đi kèm BCTC khi LNST thay đổi từ 10% trở lên, hoặc lãi chuyển lỗ | Art. 14.4.a-b |
| Báo cáo quản trị | 30 ngày sau 6 tháng và sau năm | "trong thời hạn 30 ngày, kể từ ngày kết thúc 06 tháng đầu năm và kết thúc năm đương lịch" |
| Bất thường | 24 giờ | Art. 15.2 |
| Người nội bộ đăng ký giao dịch | tối thiểu 3 ngày làm việc trước giao dịch; ngưỡng 50 triệu/ngày hoặc 200 triệu/tháng theo mệnh giá | Art. 33.1.a: "Trước ngày dự kiến thực hiện giao dịch tối thiểu 03 ngày làm việc"; giữ nguyên ở 68/2024 (VERIFIED hethongphapluat) và 08/2026 (VERIFIED hethongphapluat, có thêm ngoại lệ cho công ty chứng khoán là người liên quan) |
| ĐHĐCĐ thường niên | trong 4 tháng sau năm tài chính, gia hạn tối đa 6 tháng | Luật Doanh nghiệp 2020 Art. 139.2 (SNIPPET) |
| Song ngữ | tổ chức niêm yết công bố định kỳ đồng thời bằng tiếng Anh từ 01/01/2025 | 68/2024 (SNIPPET; AGENTS.md VERIFIED) |

**Ngày cụ thể** (DERIVED với năm tài chính trùng năm dương lịch; công ty có năm tài chính khác thì lệch theo, PRIOR):

| Kỳ | Hạn | Thứ | Ghi chú |
|---|---|---|---|
| Q3/2026 quý thường | 2026-10-20 | Tue | đã qua |
| Q3/2026 công ty mẹ | **2026-10-30** | Fri | **đỉnh sắp tới** |
| Q3/2026 có soát xét | 2026-11-14 | Sat | có thể lùi sang Mon 2026-11-16 (PRIOR: Bộ luật Dân sự 2015 Art. 148, hạn rơi vào ngày nghỉ thì kết thúc vào cuối ngày làm việc kế tiếp; chưa kiểm) |
| Q4/2026 quý thường | 2027-01-20 | Wed | |
| Q4/2026 công ty mẹ | 2027-01-30 | **Sat** | có thể lùi sang Mon 2027-02-01 (PRIOR); trùng hạn báo cáo quản trị năm 2026 |
| BCTC năm 2026 kiểm toán | 2027-03-31 | Wed | |
| Báo cáo thường niên 2026 | ≤ 2027-04-20 | Tue | 110 ngày là PRIOR |
| ĐHĐCĐ 2027 | ≤ 2027-04-30 (gia hạn ≤ 2027-06-30) | Fri / Wed | mùa ĐHĐCĐ rơi vào tháng 3-6, đỉnh tháng 4 (ASSUMED) |
| Bán niên 2027 soát xét | 2027-08-14 / 2027-08-29 (công ty mẹ) | Sat / Sun | lùi ngày như trên |

- **Hệ quả cho poller.** Các đỉnh nằm ở ngày 20 và ngày 30 sau mỗi quý, cuối tháng 3, tháng 4 (ĐHĐCĐ và báo cáo thường niên), giữa và cuối tháng 8. Cộng thêm một đợt dồn vào sáng ngày làm việc đầu tiên sau mỗi kỳ nghỉ, theo Art. 4 cl. 7.
- **Ngày 2026-10-30 đáng cố ý đưa vào đợt chạy thử:** đây là đỉnh tải thật, rất hợp để stress-test.

---

## 4. Counts kỳ vọng (để sanity-check bảng counts của Phase 0a)

**Quy mô vũ trụ:**
- HOSE "403 stocks" (SNIPPET từ báo cáo tháng 7/2026, qua https://vietnamnews.vn/economy/1784883/nearly-300-stocks-to-move-from-hnx-to-hose-under-market-restructuring-plan.html).
- HNX "299 listed stocks" (SNIPPET, tại 30/09/2026).
- UPCoM: 824 mã cuối tháng 5/2026 (SNIPPET) và 852 doanh nghiệp tháng 1/2026 (SNIPPET).
- Vũ trụ nghiên cứu: 150-250 mã HOSE/HNX (roadmap §2.5).

**Ước lượng theo mã mỗi năm** (DERIVED từ §3; số liệu thống kê thật là ASSUMED vì không tìm được):

| event_type | Số sự kiện/mã/năm | Với U=200 | Số tài liệu thô có thể gấp | Cụm ngày |
|---|---|---|---|---|
| BCTC quý (Q1-Q4) | 4 | 800 | ×2-4 (riêng/hợp nhất; tiếng Anh từ 2025; giải trình 10%) | rất mạnh: quanh ngày 20 và 30 |
| BCTC bán niên soát xét + năm kiểm toán | 2 | 400 | ×2-4 | mạnh: giữa và cuối tháng 8; cuối tháng 3 |
| ĐHĐCĐ (tài liệu + NQ) | 1-2 | 200-400 | ×2-3 | mạnh: tháng 4 |
| NQ HĐQT (mọi loại) | ASSUMED 5-20 | 1,000-4,000 | ×1-2 | yếu, rải đều cả năm |
| NQ chia cổ tức / chốt quyền | ASSUMED 1-2 | 200-400 | ×1-2 (cùng một sự kiện có cả NQ HĐQT và thông báo VSDC) | vừa: tháng 5-9 |
| Đăng ký giao dịch người nội bộ | ASSUMED 2-10 | 400-2,000 | ×2 (đăng ký + kết quả) | yếu |

**Quy tắc cho bảng counts.**
- Đếm sự kiện theo khóa `(issuer, event_type, period/subject)`, không đếm theo tài liệu. Bản tiếng Anh, bản riêng/hợp nhất và văn bản giải trình làm số tài liệu thô gấp 2-4 lần (DERIVED).
- Các cột bắt buộc: `n_docs`, `n_events`, `n_issuers`, `n_distinct_dates`, `max_events_per_date`, `share_with_intraday_time`, `median_lag_vs_portal_min`.
- Sanity check: nếu `n_events/U/năm` của BCTC quý lệch xa 4 (dưới 3 hoặc trên 5), nguồn đang thiếu hoặc trùng.

---

## 5. Power rule và go/no-go cụ thể

### 5.1 Số học (DERIVED từ roadmap §2.5)

Từ t ≈ IC·√N suy ra N = (t/IC)².

| IC | N cho t=2 (power ~50%) | N cho power 80%, α=0.05 hai phía (t=2.80) |
|---|---|---|
| 0.03 | 4,444 | 8,711 |
| 0.05 | 1,600 | 3,136 |
| 0.065 | 947 | 1,856 |
| 0.10 | 400 | 784 |

- **Lưu ý:** con số 4,400 trong roadmap là mức phát hiện với xác suất khoảng 50%. Plan nên ghi rõ mức power đã chọn.

**N_eff khi sự kiện dồn cụm theo ngày:**
- Cận dưới L = số ngày sự kiện phân biệt (khi tương quan trong ngày ρ=1).
- Cận trên U = N.
- Ước lượng giữa: N_eff(ρ) = N / (1 + (m̄−1)·ρ), với m̄ = N/L.
- ρ là tương quan trung bình của CAR giữa các sự kiện cùng ngày. Mặc định ρ=0.10 là ASSUMED cho tới khi Phase 3 đo được. MDE = 2/√N_eff.

**Phác họa D2 với BCTC quý gộp** (DERIVED; độ sâu lịch sử vnstock là ASSUMED):
- Giả định U=200 và 8 năm: N = 6,400.
- Nếu mỗi quý có khoảng 18 ngày công bố thì L ≈ 576 và m̄ ≈ 11.1.
- ρ=0.10 cho N_eff ≈ 3,180 và MDE ≈ 0.035.
- ρ=0.20 cho N_eff ≈ 2,120 và MDE ≈ 0.043.
- ρ=1 cho N_eff = 576 và MDE ≈ 0.083.

### 5.2 Quy tắc go/no-go (đề xuất cho plan áp dụng máy móc vào bảng counts)

Với mỗi target × event_type, và với nhóm gộp:

| Bậc | Điều kiện (MDE tính trên N_eff(ρ̂), t=2) | Hành động |
|---|---|---|
| **GO** | MDE ≤ 0.03 **và** MDE(L) ≤ 0.05 | Target lên scoreboard; CI bằng date-clustered block bootstrap |
| **GO-limited** | 0.03 < MDE ≤ 0.05 | Lên scoreboard. Đăng ký trước rằng chỉ phát hiện được IC ≥ MDE; kết quả null được ghi là "không có hiệu ứng > MDE" (§2.4 coi đây là pass) |
| **CONDITIONAL** | 0.05 < MDE ≤ 0.10 | Chỉ làm diagnostic hoặc gộp loại sự kiện; không công bố claim theo từng loại |
| **NO-GO** | MDE > 0.10 | Không xây target. Lý do: mọi hiệu ứng phát hiện được đều > 0.10, tức tự kích hoạt leakage audit (§2.4), nên test không thể cho một kết quả pass đáng tin. Tiếp tục capture |

**Áp dụng cho từng target VN:**
- **D2** (CAR[+2,+10], vào lệnh ở close của day+1).
  - Chịu được timestamp chỉ có ngày: quy tắc "date-only coi là sau close" chỉ làm điểm vào lệnh muộn hơn, không gây leakage.
  - Vì vậy đếm mọi sự kiện có ngày đáng tin. Kỳ vọng: BCTC gộp ở mức GO-limited. Các loại con của NQ HĐQT nhiều khả năng ở mức CONDITIONAL.
- **R1** (|AR0|>2σ hoặc khối lượng ≥2× trung vị) là phân loại, đo bằng AUC, nên rule theo IC không áp dụng được.
  - Placeholder (ASSUMED): SE của AUC dưới H0 theo Hanley-McNeil ≈ √((n₁+n₀+1)/(12·n₁·n₀)). GO khi 2·SE ≤ 0.05, tức phát hiện được AUC 0.55 so với mức kỳ vọng 0.65. Điều kiện này tương đương N_eff ≥ 133/(p(1−p)): khoảng 830 sự kiện khi base rate p=0.2, khoảng 1,480 khi p=0.1.
  - **Điểm then chốt:** R1 cần day 0 đúng. Một sự kiện date-only thực ra đăng trong phiên sẽ có phản ứng rơi vào day −1, và nhãn bị sai.
  - Vì vậy chỉ đếm các sự kiện có giờ trong ngày (từ vnstock hoặc portal) hoặc thu thập forward. Nếu `share_with_intraday_time` thấp, R1-VN lịch sử là NO-GO; R1-VN chỉ chạy trên dữ liệu forward.
- **D4** (CAR[+1,+5], chỉ dùng pit_quality exact hoặc first_seen) chỉ có dữ liệu forward, nên hôm nay N=0.
  - Ước tính forward cho BCTC (DERIVED): 800 sự kiện/năm, ~72 ngày/năm, ρ=0.1. N_eff ≈ 400/năm, nên MDE ≈ 0.10 sau 1 năm, 0.071 sau 2 năm, 0.05 sau 4 năm.
  - Kết luận: **NO-GO cho scoreboard đến hết Phase 4**. Capture-only, đánh giá lại ở mỗi phase gate bằng cùng rule. Thêm sự kiện NQ HĐQT (ít dồn cụm) là cách duy nhất rút ngắn thời gian.

---

## 6. Cửa sổ 5 phiên liên tiếp cho exit gate

| Tuần (2026) | Sạch? | Ghi chú |
|---|---|---|
| 26-30/10 | sạch | chứa đỉnh 30/10, tốt nhất cho stress test, nhưng nhiều khả năng plan chưa cook kịp (laptop của owner còn là bước chặn) |
| 02-06/11, 09-13/11, 16-20/11 | sạch | không có lễ (Labour Code và NQ 28) |
| 23-27/11 | **không** | Tue 24/11 nghỉ. Năm phiên liên tiếp vẫn có thể là 19/11 → 26/11 nếu lịch đánh dấu đúng 24/11; với phương án C thì không ảnh hưởng capture |
| 30/11-04/12, 07-11/12, 14-18/12 | sạch | |
| 21-25/12 | tránh | 24-25/12 với các mã chuyển từ HNX (ASSUMED) |
| 28/12/2026 - 01/01/2027 | tránh | ngày đầu sau khi chuyển sàn; 01/01 nghỉ |

**Định nghĩa gate đề xuất:**
- Có 5 ngày `is_trading_day` liên tiếp.
- Mỗi ngày có heartbeat.
- Outage log không còn khoảng nào giữa hai lần poll portal thành công vượt 2× chu kỳ danh nghĩa mà không được ghi lại.
- Các khoảng đã ghi trong outage log thì được phép, và làm `first_seen` sau gap mang `pit_quality=late`.

---

## 7. Hạ tầng quan sát được (OBSERVED 2026-10-22, dùng cho chọn portal và runbook)

- `https://vsd.vn/en/ad/<id>` trả 301 sang `https://vsdc.vn/...`. Chứng chỉ của `vsd.vn` không khớp (WebFetch: "Host: vsd.vn. is not in the cert's altnames: DNS:*.vsdc.vn, DNS:vsdc.vn"). Hãy dùng `vsdc.vn` trực tiếp.
- Trang VSDC render phía server, có trường "Date update dd/mm/yyyy - HH:MM:SS". Đây là nguồn chính thức tốt cho lịch nghỉ và chốt quyền. Robots.txt của VSDC tôi chưa kiểm (ASSUMED).
- `https://www.hsx.vn/` chỉ là vỏ SPA 1,900 byte ("You need to enable JavaScript"). Bundle JS timeout sau 120 giây (curl exit 28). Poll HOSE sẽ cần tìm API JSON; ToS là ASSUMED.
- `vbpl.vn/.../vbpq-toanvan.aspx?ItemID=146048` là vỏ Next.js, không có nội dung. PDF `VanBanGoc_96-2020-TT-BTC.pdf` trong AGENTS.md mới là bản dùng được. `thuvienphapluat.vn` và `luatminhkhue.vn` trả 403 cho curl và WebFetch. `caselaw.vn` trả toàn văn.

---

## Kết luận (xếp hạng)

1. **Dùng phương án C (tách hai tầng) cho lịch poll.** Plan nên ghi chỉnh lại 0b.6 trong decision log, vì roadmap ghi 08:00-17:30 cộng hằng giờ.
   - Portal: 1 trang danh sách mỗi 3-5 phút, 24/7, kể cả ngày nghỉ (theo Circular 96 Art. 4 cl. 7).
   - vnstock: quét vòng theo budget đo được ở probe 1.
   - Capture không phụ thuộc lịch giao dịch.
   - Nếu owner muốn giữ cấu trúc của roadmap thì tối thiểu là phương án B (2-5' tới 15:00, 5-10' tới 22:00).
2. **Sửa tiền đề về hạn BCTC trong plan.**
   - Hạn: 20 ngày; 30 ngày cho công ty mẹ; 45 ngày cho quý có soát xét; bán niên 45/60 ngày; năm 90 ngày; bất thường 24 giờ; người nội bộ 3 ngày làm việc.
   - Ngày: Q3/2026 = 20/10 và **30/10**; Q4/2026 = 20/01/2027 và 30/01/2027 (thứ Bảy, có thể lùi sang 01/02).
3. **Áp go/no-go 4 bậc theo MDE trên N_eff hiệu chỉnh cụm ngày** (§5.2), ghi rõ power 50% hay 80%.
   - Kỳ vọng: D2 với BCTC gộp ở mức GO-limited; D4 là NO-GO cho scoreboard đến hết Phase 4 (capture-only); R1-VN là GO chỉ khi `share_with_intraday_time` đủ để có N_eff ≥ 133/(p(1−p)).
4. **Lịch giao dịch là YAML có version, theo từng sàn, nguồn là thông báo VSDC/HOSE**, không suy ra từ Labour Code.
   - Năm 2026 còn đúng 24/11. Bảng 2027 là nháp cho tới khi có thông báo năm (dự kiến cuối tháng 11/2026).
   - Đối chiếu hằng tuần, và hậu kiểm bằng bar VNINDEX.
5. **Chạy 5 phiên exit gate trong tuần sạch đầu tiên sau khi laptop sẵn sàng** (02/11 → 18/12/2026), tránh 23-27/11 và 21/12-01/01. Nếu kịp thì nên có ít nhất một ngày đỉnh tải. Sau 30/10, đỉnh tiếp theo là 14-16/11 (quý có soát xét), vốn nhỏ hơn.
6. **Thêm `tzdata` (hoặc offset UTC+7 cố định) vào dependency của capture.** `.venv` hiện không có tzdata.
7. **Bảng counts đếm sự kiện, không đếm tài liệu.** Các cột lấy theo §4; sanity check BCTC ≈ 4 sự kiện/mã/năm.

## Câu hỏi mở (chỉ probe thật mới trả lời được)

- **Q1. Phân bố giờ đăng của công bố VN** (bao nhiêu phần trăm sau 14:45, sau 17:30, ngày nghỉ).
  - Probe: trong bước đối chiếu tay 30 công bố, chọn ngẫu nhiên có phân tầng (10 BCTC quanh 20-30/10, 10 NQ HĐQT, 10 đăng ký người nội bộ).
  - Ghi `portal_posted_at` (ICT), `vnstock_time` (nếu có) và `first_seen_at` của poller.
  - Báo tỉ lệ theo khung 08-14:45, 14:45-17:30, 17:30-24, 00-08 và ngày nghỉ. Nếu tỉ lệ sau 17:30 cộng ngày nghỉ vượt 15% thì phương án A bị loại.
- **Q2. HOSE/HNX có thông báo nghỉ giao dịch 24/11/2026 không, và các mã chuyển từ HNX có nghỉ 24-25/12 không.**
  - Probe: từ 2026-11-01 kiểm hằng tuần trang tin `https://vsdc.vn/en/` (mục thông báo nghỉ), trang tin của HOSE và HNX (tìm "nghỉ giao dịch").
  - Sau ngày đó, gọi `market.equity.ohlcv` cho VNINDEX quanh 23-25/11 và 23-28/12 để xem có bar hay không.
- **Q3. Lịch nghỉ 2027 chính thức** (Giỗ Tổ 16/4? Nghỉ bù 03/05? Tết 04-10/02 với sàn?).
  - Probe: thông báo năm của VSDC và HOSE, dự kiến khoảng 20/11/2026. Thay bảng §2.2 và ghi `notice_id`.
- **Q4. Hạn rơi vào thứ Bảy (30/01/2027, 14/11/2026) có lùi sang thứ Hai không.**
  - Probe: đọc Bộ luật Dân sự 2015 Art. 148 trên caselaw.vn.
  - Sau đó kiểm trên portal xem các công ty mẹ còn đăng BCTC Q4/2026 vào 01/02/2027 mà không bị HOSE nhắc nhở hay không.
- **Q5. Rate limit thật của vnstock Community, và news/events có giờ trong ngày không.** Hai điều này quyết định chu kỳ quét vòng của tầng vnstock.
  - Probe: probe 1 trong venv riêng, gồm burst có kiểm soát và đọc header hoặc lỗi trả về; kiểm `ref.company.news()` và `ref.company.events()` có trường giờ hay không.
- **Q6. Giờ phiên UPCoM dưới KRX, và giờ phiên theo văn bản quy chế gốc.**
  - Probe: đọc quy chế HOSE/HNX (PDF) trên máy có trình đọc PDF; hoặc lấy giờ của tick đầu và cuối từ `market.equity.trades()` cho một mã UPCoM.
- **Q7. ρ (tương quan CAR cùng ngày) và độ sâu lịch sử thật**, dùng để chốt N_eff cho D2 và R1.
  - Probe: Phase 0a counts lấy L và m̄; Phase 3 ước lượng ρ̂ từ residual của market model.
- **Q8. Robots.txt và ToS của `vsdc.vn` và của portal mà owner chọn.**
  - Probe: `curl -s https://vsdc.vn/robots.txt`. Đây là bước chặn của owner.
- **Q9. Nội dung Circular 18/2025 có chạm Art. 14 không.**
  - Probe: đọc văn bản 18/2025 trên caselaw.vn hoặc vbpl (PDF).

## Nguồn chính

- **Circular 96 toàn văn:** https://caselaw.vn/van-ban-phap-luat/372205-thong-tu-so-96-2020-tt-btc-ngay-16-11-2020-cua-bo-truong-bo-tai-chinh-huong-dan-cong-bo-thong-tin-tren-thi-truong-chung-khoan
- **Sửa đổi:** https://hethongphapluat.com/thong-tu-68-2024-tt-btc-53e9cb20cc74df0319a9ca98e4deccac/dieu-4 ; https://hethongphapluat.com/thong-tu-08-2026-tt-btc-db8bfb9bef0f773690fc078e26085b36
- **VSDC:** https://vsdc.vn/en/ad/198399 ; /198931 ; /190545 ; /191685 ; /182206
- **Lịch nghỉ do Chính phủ duyệt:** https://baoquocte.vn/chot-lich-nghi-tet-nguyen-dan-dinh-mui-7-ngay-451229.html ; https://cafef.vn/bo-noi-vu-thong-bao-chi-tiet-lich-nghi-tet-am-lich-quoc-khanh-2027-188261010120450778.chn ; https://daibieunhandan.vn/dieu-chinh-phuong-an-nghi-ngay-van-hoa-viet-nam-nam-2026-10431116.html ; https://mekongasean.vn/bo-noi-vu-de-xuat-chi-nghi-mot-ngay-dip-ngay-van-hoa-viet-nam-59803.html ; https://fptshop.com.vn/tin-tuc/for-gamers/nghi-le-24-11-huong-nguyen-luong-209135 ; https://lsvn.vn/hai-bo-dong-y-nghi-7-ngay-tet-dinh-mui-4-ngay-van-hoa-a177252.html
- **Giờ phiên:** https://hdsd.dnse.com.vn/master/nhung-thay-doi-quan-trong-cua-he-thong-krx/faq-krx-thi-truong-co-so ; https://www.hsc.com.vn/en/important-changes-of-new-trading-system ; SNIPPET: https://www.dsc.com.vn/kien-thuc/khop-lenh-dinh-ky-va-khop-lenh-lien-tuc , https://www.finhay.com.vn/lenh-plo , https://shinhansec.com.vn/uploads/notification/SSV_-_THE_DIFFERENCES_BETWEEN_THE_CURRENT_SYSTEM_AND_THE_KRX_SYSTEM__VN___28042025.pdf
- **Quy mô sàn:** https://vietnamnews.vn/economy/1784883/nearly-300-stocks-to-move-from-hnx-to-hose-under-market-restructuring-plan.html ; https://vietnamnews.vn/economy/1801235/hnx-listed-stocks-to-move-to-hose-from-december-28.html (SNIPPET)
- **Hạn 20 ngày còn hiệu lực 2026:** https://doanhnhan.baophapluat.vn/y-duoc-pham-vimedimex-vmd-bi-nhac-nho-vi-cham-nop-bao-cao-tai-chinh-quy-i-2026.html

**Giới hạn của nghiên cứu này:**
- Không đọc được các PDF quy chế (không có thư viện PDF, và không được cài).
- Không có thống kê thật về số NQ HĐQT, cổ tức, người nội bộ mỗi năm.
- Không có dữ liệu giờ đăng của công bố doanh nghiệp. Bằng chứng giờ đăng chỉ đến từ thông báo VSDC.
- Lịch 2027 chưa có bản của sàn.
