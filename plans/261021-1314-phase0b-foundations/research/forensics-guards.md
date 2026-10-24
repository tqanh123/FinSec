# Research — Forensics guards (Altman / Beneish / Piotroski) và cổng công ty tài chính

Ngày 2026-10-21. Phạm vi: `src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py` (viết tắt `aud:N`) và `FinancialStatementSnapshot` (`src/finsec_alpharadar/domain/models.py:62-95`). Mọi trích dẫn code là OBSERVED 2026-10-21 (đọc file).

**Nguồn.** [A2000] Altman, "Predicting Financial Distress of Companies: Revisiting the Z-Score and ZETA Models", July 2000 — bản Altman tự viết lại từ bài JoF 1968, KHÔNG phải bản in 1968: https://moodle2.units.it/pluginfile.php/322514/mod_resource/content/0/Zscores.pdf · [B1999] Beneish, "The Detection of Earnings Manipulation", working paper "June 1999 / Comments Welcome" (không phải bản in FAJ): https://www.calctopia.com/papers/beneish1999.pdf · [P2000] Piotroski, Chicago GSB "Selected Paper 84": http://www.chicagobooth.edu/~/media/FE874EE65F624AAEBD0166B1974FD74D.pdf · Thứ cấp: [ACTA] https://acta.ef.jcu.cz/pdfs/aub/2014/02/03.pdf · [WZ] https://en.wikipedia.org/wiki/Altman_Z-score · [WM] https://en.wikipedia.org/wiki/Beneish_M-score · [P123] https://blog.portfolio123.com/detecting-financial-fraud-a-close-look-at-the-beneish-m-score/ · [GMT] https://www.gmtresearch.com/en/accounting-ratio/beneishs-m-score/ · [FT] FinanceToolkit https://raw.githubusercontent.com/JerBouma/FinanceToolkit/main/financetoolkit/models/ (`altman_model.py`, `beneish_model.py`, `piotroski_model.py`). Ba PDF gốc được tải và trích bằng `pdftotext`; pdftotext làm rơi ký hiệu Δ và >, phần đó ghi trong ngoặc vuông.

## 0. Probe: code hiện tại gặp mẫu số suy biến (OBSERVED 2026-10-21)
Script nằm trong scratchpad, không nằm trong repo. Nó gọi `ForensicAccountingEngine.evaluate` trên một snapshot cơ sở, rồi đổi từng mẫu số. Cơ sở: Z=2.876 GREY, M=-2.422, F=7.

| Đầu vào | Kết quả |
|---|---|
| `total_liabilities=0` (hoặc 0.5) | Z=481.916 → SAFE |
| `total_liabilities=0`, `market_cap=8e11` (thang VND) | Z=480000000001.916 → SAFE |
| `market_cap=0` | Z=1.916 GREY (X4 âm thầm thành 0) |
| GM hiện tại = 0 hoặc < 0 | M=2109.05 → bật cờ manipulation |
| `prev_aq` = 0 hoặc < 0 | M=1209.166 → bật cờ manipulation |
| `current_liabilities=0` | F=8 (ΔLIQUID được điểm nhờ clamp) |
| `total_assets=0` / `revenue=0` / `prev_accounts_receivable=0` / `prev_selling_general_admin=0` / dep=PPE=0 | `ZeroDivisionError` |

Tóm lại: mẫu số đúng bằng 0 thì crash. Mẫu số ≤0 hoặc rất nhỏ bị clamp thành giá trị khổng lồ, âm thầm, và lật zone hoặc cờ. `max(1.0, …)` phụ thuộc đơn vị tiền. ASSUMED: với số liệu VND, clamp này gần như chỉ chạy khi giá trị bằng 0.

## 1. Altman Z (Q1)
- **Hệ số.** Bản gốc: "Z = 0.012X1 + 0.014X2 + 0.033X3 + 0.006X4 +0.999X5", trong đó X1–X4 "must be calculated as absolute percentage values". Dạng tiện dụng "Z = 1.2X1 + 1.4X2 + 3.3X3 + 0.6X4 + 1.0X5" "rounds the last coefficient off to equal 1.0 (from 0.99)", và "scores … remain identical". VERIFIED [A2000]. Cùng hệ số, dẫn "Altman, 1968, p. 594": VERIFIED [ACTA]. Dạng 1.2…1.0: VERIFIED [WZ], [FT altman_model.py]. Code `aud:33` khớp dạng 1.2…1.0; dùng 1.0 thay 0.999 không đáng kể.
- **X4.** "X4 = market value equity/book value of total liabilities", với "liabilities include both current and long term" và equity = "combined market value of all shares of stock, preferred and common". VERIFIED [A2000]. [ACTA] lại viết "book value of total debt", nên cách viết lệch giữa các nguồn. [WZ] viết total liabilities.
- **Ngưỡng.** Altman viết "lower bond of the zone-of-ignorance (1.81)", nêu cutoff 2.675, và khuyên 1.81 là cutoff thực tế hơn. VERIFIED [A2000]. Cận trên **2.99** chỉ có ở [WZ] ("Z > 2.99") và [ACTA] ("Z > 2.99", "1.81 < Z < 2.98"); grep [A2000] không thấy 2.99 (OBSERVED 2026-10-21). Vậy 2.99 có dưới 3 nguồn độc lập. Code `aud:34-39` dùng `> 2.99` / `>= 1.81`.
- **Phạm vi mẫu gốc.** Mẫu là "manufacturers" (VERIFIED [A2000]); "The data used in the study are limited to manufacturing corporations" (VERIFIED https://content.wiserfunding.com/insights/research/financial-ratios-discriminant-analysis-and-the-prediction-of-corporate-bankruptcy). [WZ] có lẽ nói các mô hình dựa trên bảng cân đối không được khuyến nghị cho công ty tài chính, nhưng bản fetch bị cắt xén nên ASSUMED.
- **Z'' (một dòng).** "Z" = 6.56 (X1)+ 3.26 (X2) + 6.72 (X3) + 1.05 (X4)", dùng book equity cho X4; bản cho thị trường mới nổi cộng thêm "+3.25". VERIFIED [A2000].
- **Mẫu số phải dương.** `total_assets` (X1, X2, X3, X5 tại `aud:28-32`); `total_liabilities` (X4, `aud:31`); `market_cap` phải có và > 0 (X4). Khi TL = 0, X4 → +∞, tức là không xác định. [A2000] không có quy tắc nào cho trường hợp này (grep, OBSERVED), nên đề xuất trả None. Lệch so với bài: `max(1.0, fs.total_liabilities)` tại `aud:31` là clamp âm thầm, và `market_cap=0` không bị chặn.

## 2. Beneish M (Q2)
- **Hệ số.** Table 3, hàng "Unweighted Probit": "-4.840 .920 .528 .404 .892 .115 -.172 4.679 -.327". VERIFIED [B1999]; cùng bộ hệ số ở [WM] và [FT beneish_model.py], tức ≥3 nguồn. Code `aud:59-69` khớp.
- **Ngưỡng −1.78.** "exceed .0376 (a score greater than -1.78)" là cutoff ứng với tỉ lệ chi phí sai số 20:1 hoặc 30:1. VERIFIED [B1999]; cũng có ở [WM], [P123], [FT]. Code `aud:70` (`> -1.78`) khớp.
- **Ngưỡng −2.22 KHÔNG có trong text [B1999].** Grep "2.2" chỉ trúng một t-stat (OBSERVED 2026-10-21). [GMT] gán −2.22 cho bài Beneish 1999 mà không dẫn trang, nên nguồn gốc chưa kiểm chứng. Mô hình 5 biến (−6.065 + 0.823 DSRI …) được một luận văn gán cho Roxas 2011, với ngưỡng −2.76. Thông tin này chỉ có trong search snippet (conf.hse.ru), nên là ASSUMED.
- **Định nghĩa (Table 2 [B1999], VERIFIED).** DSRI = (AR_t/Sales_t)/(AR_{t-1}/Sales_{t-1}); GMI = GM_{t-1}/GM_t; AQI = [1−(CA_t+PPE_t)/TA_t] / [1−(CA_{t-1}+PPE_{t-1})/TA_{t-1}]; SGI = Sales_t/Sales_{t-1}; DEPI = rate_{t-1}/rate_t, với rate = Dep/(Dep+PPE); SGAI = (SGA_t/Sales_t)/(SGA_{t-1}/Sales_{t-1}); LVGI = [(LTD_t+CL_t)/TA_t] / [(LTD_{t-1}+CL_{t-1})/TA_{t-1}]. Code `aud:42-56` khớp các định nghĩa này. AQI của code không cộng "Securities" như [WM], và điều đó đúng với bài.
- **TATA.** Bản gốc: "[(ΔCurrent Assets − ΔCash) − (ΔCurrent Liabilities − ΔCurrent Maturities of LTD − ΔIncome Tax Payable) − Depreciation and Amortization] / TA_t". VERIFIED [B1999] Table 2. Code `aud:57` dùng (NI − CFO)/TA, tức biến thể "(Income from Continuing Operations − Cash Flows from Operations) / Total Assets" của [WM]. [P123] viết rằng Beneish "later (in 2013) switched to a more widely accepted method based on the cash flow statement". Đây chỉ là nguồn thứ cấp: bài Beneish-Lee-Nichols 2013 (FAJ 69(2)) không đọc được, nên ASSUMED. Thêm một lệch nhỏ: code dùng `net_income` thay cho income from continuing operations.
- **Quy tắc thiếu dữ liệu của chính Beneish (VERIFIED [B1999]).** "I winsorized the data at the 1% and 99 % percentiles for each variable". Khi mẫu số AQI = 0: "I set its value to one (its neutral value) instead of treating the observation as missing". "Similarly, I set the Depreciation and SGA indices to values of one, when elements of the computation … were not available". Code `aud:48` (`max(1e-4, prev_aq)`) mâu thuẫn trực tiếp với quy tắc này; probe cho M=1209. `prev_aq < 0` nghĩa là CA+PPE > TA, tức dữ liệu sai chứ không phải một biên toán học.
- **Mẫu số phải dương hoặc khác 0.** `revenue` và `prev_revenue` cho DSRI, GMI, SGI, SGAI (`aud:42-44,49,53`); `prev_accounts_receivable` cho DSRI (`aud:42`); `total_assets` và `prev_total_assets` cho AQ, LVGI, TATA (`aud:46-47,54-57`); Dep+PPE và `curr_dep_rate` cho DEPI (`aud:50-52`); `prev_selling_general_admin` cho SGAI (`aud:53`).
  - `curr_gm` > 0 cho GMI (`aud:45`): với GM ≤ 0, chỉ số đổi dấu hoặc mất nghĩa. Winsorize là phép cắt ngang trên cả mẫu, không áp được cho một công ty. `prev_gm` > 0 là ASSUMED: tử số âm làm GMI âm, ngược với cách diễn giải.
  - `prev_lev` cho LVGI (`aud:56`): clamp làm LVGI khổng lồ, và hệ số −0.327 kéo M **xuống**, nên che mất cờ (ASSUMED, suy từ công thức).
- **Loại công ty tài chính.** "I eliminated 80 AAERs relating to financial institutions", cộng thêm "five financial institutions". VERIFIED [B1999].

## 3. Piotroski F (Q3)
VERIFIED [P2000], trừ chỗ ghi khác.
- ROA và CFO = "net income before extraordinary items and cash flow from operations, respectively, scaled by beginning of the year total assets". ΔROA = ROA_t − ROA_{t-1}.
- ACCRUAL = (NIBEI − CFO)/TA đầu năm; "F_ACCRUAL equals one if CFO [>] ROA".
- ΔLEVER = "historical change in the ratio of total long-term debt to average total assets"; F_LEVER = 1 (0) "if the firm's leverage ratio fell (rose)".
- ΔLIQUID: current ratio "at fiscal year-end". EQ_OFFER = 1 "if the firm did not issue common equity in the year preceding portfolio formation".
- ΔMARGIN: gross margin chia doanh thu. ΔTURN: "total sales scaled by beginning of the year total assets".
- **Dữ liệu.** "the variable [Δ]ROA requires three years of historical data". Mẫu chỉ giữ firm "with sufficient financial statement data to calculate the various financial performance signals". Grep "SIC|bank|financial firms" không thấy điều khoản loại công ty tài chính (OBSERVED 2026-10-21); như vậy là không tìm thấy, không có nghĩa là bài cho phép.
- **Nguồn thứ hai.** Docstring [FT piotroski_model.py]: "Piotroski (2000) defines dLEVER as the change in the ratio of total *long-term* debt to *average* total assets" (VERIFIED). Nhưng FT lại chia ROA cho TA bình quân, lệch khỏi bài. Ngoài bài gốc chỉ có một nguồn thứ cấp đọc được (Alpha Architect chỉ có qua snippet), tức dưới 3 nguồn.

| Tín hiệu | Code | Bài | Lệch |
|---|---|---|---|
| ROA, CFO, ΔROA | `aud:74-76`, chia TA cuối kỳ | TA đầu kỳ; ΔROA cần TA_{t-2} | schema `models.py:62-95` thiếu `prev_prev_total_assets` |
| ACCRUAL | `aud:86`: CFO > NI | CFO > ROA (cùng mẫu số) | tương đương khi TA_{t-1} > 0 |
| ΔLEVER | `aud:87`: LTD/TA cuối kỳ, so sánh `<=` | LTD/TA bình quân; "fell" | cần TA_{t-2}; bài không nói gì về trường hợp bằng nhau |
| ΔLIQUID | `aud:77-78`: `max(1.0, CL)` | CA/CL, không clamp | clamp âm thầm |
| EQ_OFFER | `aud:89`: shares_t ≤ shares_{t-1} | "did not issue common equity" | proxy sai khi có split, cổ phiếu thưởng hoặc cổ tức bằng cổ phiếu, hoặc khi buyback bù trừ phát hành (ASSUMED); FT dùng cùng proxy |
| ΔMARGIN | `aud:43-44,90` | khớp | cần revenue > 0 cả hai năm |
| ΔTURN | `aud:79-80,91`, chia TA cuối kỳ | TA đầu kỳ | cần TA_{t-2} |

## 4. Quy ước khi một tỷ số không xác định (Q4)

| Cách xử lý | Ai dùng | Đánh giá |
|---|---|---|
| Bỏ cả quan sát (score = None) | Piotroski giữ firm "sufficient … data" (VERIFIED [P2000]); GMT bỏ 19% mẫu vì "we can't calculate the formula" (VERIFIED [GMT]) | trung thực nhất; mất coverage |
| Gán chỉ số = 1 (trung tính) | Beneish với AQI/DEPI/SGAI (VERIFIED [B1999]); [P123] đặt 1 khi AQI/DEPI/SGAI N/A (VERIFIED) | đúng bài cho ba chỉ số này; DEPI/SGAI "not significant", t = (.70)/(−.71) (VERIFIED [B1999] Table 3); phải gắn cờ `imputed` |
| Không tính được thì 0 điểm | [FT piotroski] gọi `.astype(int)` trên kết quả so sánh; việc so sánh NaN ra False rồi thành 0 là PRIOR (probe hỏng vì máy không có pandas) | kéo F xuống một cách âm thầm; loại |
| Không có guard | [FT altman_model.py]: `return market_value_of_equity / total_liabilities` (VERIFIED) | inf/NaN lan ra |
| Clamp | code hiện tại | loại (xem probe §0) |

Bảng này không phải một khảo sát đầy đủ: OpenBB, QuantConnect và code SAS của WRDS chưa được xem.

## 5. Nhận diện công ty tài chính cho refdata (Q5)
- **SIC** (VERIFIED https://www.sec.gov/corpfin/division-of-corporation-finance-standard-industrial-classification-sic-code-list): ngân hàng 6021, 6022, 6029, 6035, 6036, 6099; môi giới chứng khoán 6200, 6211, 6221, 6282; bảo hiểm 6311, 6321, 6324, 6331, 6351, 6361, 6399, 6411; tín dụng 6111–6172; khác: 6770 BLANK CHECKS, 6798 REITs.
- **EDGAR.** Submissions JSON có `sic`, `sicDescription` và `ownerOrg`. OBSERVED 2026-10-21 với CIK0000019617: `"sic": "6021"`, `"ownerOrg": "02 Finance"`.
- **GICS** (sau 2023-03-17):
  - 40 Financials; 4010 Banks (40101010 Diversified, 40101015 Regional, gồm cả thrifts); 4020 Financial Services (402010 gồm 40201040–40201060). VERIFIED https://www.msci.com/documents/1296102/29559863/GICS_Structure_Change_Doc_31_March_2022.pdf
  - 402030 Capital Markets (40203020 Investment Banking & Brokerage) và 4030 Insurance (403010; 40301010–40301050) chỉ VERIFIED qua https://en.wikipedia.org/wiki/Global_Industry_Classification_Standard, tức dưới 3 nguồn.
- **ICB** (VERIFIED https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/icb-taxonomy-overview-cut-sheet.pdf): Industry 30 Financials, với các supersector 3010 Banks (30101010), 3020 Financial Services, 3030 Insurance (303010 Life, 303020 Nonlife).
  - **Sửa premise:** 30202010 là "Asset Managers and Custodians". Môi giới và ngân hàng đầu tư nằm ở sector 302020 "Investment Banking and Brokerage Services". CTCK có lẽ thuộc 30202015 "Investment Services" (ASSUMED).
- **VN dùng mẫu BCTC riêng:**
  - **TCTD:** theo QĐ 16/2007/QĐ-NHNN, được sửa bởi TT 49/2014/TT-NHNN (hiệu lực 15/02/2015) và TT 27/2021/TT-NHNN (01/04/2022). VERIFIED https://caselaw.vn/van-ban-phap-luat/264126-thong-tu-49-2014-tt-nhnn-sua-doi-che-do-bao-cao-tai-chinh-doi-voi-to-chuc-tin-dung-kem-theo-quyet-dinh-16-2007-qd-nhnn-va-he-thong-tai-khoan-ke-toan-cac-to-chuc-tin-dung-kem-theo-quyet-dinh-479-2004-qd-nhnn-do-thong-doc-ngan-hang-nha-nuoc-ban-hanh
  - **CTCK:** theo TT 210/2014/TT-BTC. TT 334/2016/TT-BTC thay Phụ lục 02 (hệ thống tài khoản) và Phụ lục 04 ("Mẫu và giải thích báo cáo tài chính"). VERIFIED https://vanban.vcci.com.vn/thong-tu-3342016tt-btc-cua-bo-tai-chinh-ve-viec-sua-doi-bo-sung-va-thay-the-phu-luc-02-va-04-cua-thong-tu-2102014tt-btc-ngay-30122014-cua-bo-tai-chinh-huong-dan-ke-toan-ap-dung-doi-voi-cong-ty-chung-khoan
  - **Bảo hiểm:** TT 232/2012/TT-BTC cho phi nhân thọ và tái bảo hiểm; nhân thọ theo TT 199/2014/TT-BTC. Chỉ có trong search snippet (luatminhkhue trả 403), nên ASSUMED.
- **HOSE dùng ICB?** Chưa kiểm chứng. Một snippet Wikipedia nói các chỉ số ngành VNAllshare theo GICS (xem câu hỏi mở).

## Kết luận (xếp hạng)
1. **Cổng tài chính chạy trước mọi phép tính** (`aud:25-26`).
   - Thêm `issuer_type: IssuerType = IssuerType.UNKNOWN` vào `FinancialStatementSnapshot` (`models.py:62`), với các giá trị {NON_FINANCIAL, BANK, INSURER, SECURITIES_FIRM, OTHER_FINANCIAL, UNKNOWN}.
   - Mọi loại tài chính → cả ba score None, reason `"not_applicable_financial_issuer"`.
   - UNKNOWN → **từ chối** (None, `"issuer_type_unknown"`). Cách này xếp trên "vẫn tính + cảnh báo" vì luật là binding và fail-closed, giống cách xử lý sàn lạ.
   - Map refdata: SIC 6021–6411 (cân nhắc thêm 6770), GICS 40, ICB 30.
   - Lớp phòng thủ thứ hai ở VN (chỉ là gợi ý cho plan): parser từ chối khi nhận ra mẫu BCTC của TCTD, CTCK hoặc bảo hiểm. Việc các mẫu đó thiếu dòng COGS/SGA/AR/PPE là ASSUMED vì chưa mở mẫu nào.
2. **Kiểu kết quả.** `ForensicAuditResult` (`aud:9-18`) chuyển sang `Optional[float]`, `Optional[int]`, `Optional[bool]`, thêm `reasons: Dict[str, str]` và `imputed: List[str]`. Z = None thì zone = None. Một score None không bao giờ sinh red flag.
3. **Guard trong phạm vi Phase 0b** (chỉ chặn mẫu số suy biến, giữ nguyên công thức):
   - **Altman → None** khi `total_assets <= 0`, khi `total_liabilities <= 0` (bỏ `max(1.0, …)` ở `aud:31`), hoặc khi `market_cap` thiếu hay `<= 0`. `market_cap: float` hiện là trường bắt buộc (`models.py:82`), nên chưa biểu diễn được "thiếu": plan phải chọn giữa `Optional[float]` và quy ước 0 = thiếu.
   - **Beneish → None** khi một trong các điều kiện sau đúng (bỏ mọi `max(1e-4, …)` ở `aud:45,48,52,56`):
     - `revenue` hoặc `prev_revenue <= 0`;
     - `prev_accounts_receivable <= 0`;
     - `curr_gm` hoặc `prev_gm <= 0`;
     - TA hoặc prev_TA `<= 0`;
     - `curr_aq < 0` hoặc `prev_aq < 0` (dữ liệu sai);
     - `prev_lev <= 0`.
   - **Ngoại lệ theo đúng bài:**
     - `prev_aq == 0` → AQI = 1, ghi vào `imputed`.
     - DEPI/SGAI → 1, ghi vào `imputed`, khi thiếu đầu vào (đúng bài). Áp thêm khi mẫu số = 0 là mở rộng của tôi (ASSUMED).
     - Phương án hạng 2: None cho mọi trường hợp.
   - **Piotroski:**
     - Mỗi tín hiệu là `Optional[int]`. F = None nếu có bất kỳ tín hiệu nào None, theo quy tắc chọn mẫu của [P2000].
     - Guard: `total_assets > 0` và `prev_total_assets > 0`; CL > 0 cả hai năm (bỏ `max(1.0, …)` ở `aud:77-78`); revenue > 0 cả hai năm (`aud:90`).
     - Hạng 2: F một phần kèm `signals_computed`, không so với ngưỡng 8–9.
     - Loại: coi tín hiệu undefined = 0.
4. **Sửa công thức cho đúng bài: tách khỏi Phase 0b.** Mục này đổi schema và đổi kết quả của fixture, nên cần một quyết định riêng:
   - Piotroski chia theo TA đầu kỳ và dùng LTD/TA bình quân. Cần thêm `prev_prev_total_assets` (`aud:74-80,87,91`).
   - EQ_OFFER lấy từ `common_equity_issued: Optional[bool]` thay cho proxy số cổ phiếu (`aud:89`).
   - TATA: chọn bản gốc 1999 hay (NI − CFO)/TA (`aud:57`), rồi ghi biến thể đang dùng vào docstring.
5. **Test.** `tests/test_end_to_end.py:106-120` (assert SAFE và F == 9) phải thêm `issuer_type=NON_FINANCIAL`, vì UNKNOWN sẽ bị từ chối. Mỗi dòng của bảng probe §0 cần một test riêng: kết quả None, reason đúng, không còn `ZeroDivisionError`. Chỉ thêm `prev_prev_total_assets` khi làm mục 4.

## Câu hỏi còn mở
- Cận trên 2.99, và cách viết "total debt" hay "total liabilities", cần bản JoF 1968 gốc (paywall).
- Chưa đọc được nguồn gốc ngưỡng −2.22, cũng như định nghĩa TATA chính xác trong Beneish-Lee-Nichols 2013.
- Fixture `period="2026-Q3"` đưa doanh thu một quý vào ba mô hình vốn dùng số liệu năm, nên X5 = sales/TA thấp khoảng 4× (ASSUMED). Cần chọn chính sách: TTM hay chỉ dùng số liệu năm.
- `market_cap` đo tại thời điểm nào: cuối năm tài chính hay thời điểm as-of quyết định? Câu này dính đến luật point-in-time.
- HOSE/HNX dùng ICB hay GICS, và vnstock trả mã ngành nào? ASSUMED cho đến probe Phase 0.
- Có gate REIT (SIC 6798) và SPAC (SIC 6770) không? Luật hiện chỉ nêu ngân hàng, bảo hiểm và công ty chứng khoán.
- Có dùng Z'' thay Z gốc cho công ty không sản xuất hoặc cho VN không? Cần một quyết định riêng.
