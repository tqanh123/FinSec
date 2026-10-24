# Research: cổng công bố thông tin chính thức VN cho capture-only poller (0b.6)

- Ngày: 2026-10-22. Người làm: hs-researcher (mode `depth`, tự làm, không delegate).
- Câu hỏi: trang/endpoint chính thức nào liệt kê "công bố mới nhất của mọi tổ chức phát hành" mà poller $0 có thể hỏi mỗi 2-5 phút, có giờ đăng (HH:MM[:SS]) rõ múi giờ, phân trang ổn định, lịch sự (robots/ToS)?
- Tiêu chí: độ chính xác giờ đăng; độ phủ (HOSE/HNX/UPCOM); ổn định endpoint và id; chi phí mỗi lượt hỏi; lịch sự/pháp lý; rủi ro sau 2026-12-28.
- Ngoài phạm vi: vnstock (xem `vnstock-gen5.md`), tải PDF đính kèm, parse nội dung, SSI.
- Ghi chú đường dẫn: task chỉ định `H:\study\Project\FinSec\plans\...`, nhưng hook worktree chặn ghi vào main checkout. Report nằm trong plan dir của worktree, cạnh các research khác.

## Phương pháp và giới hạn đo

- Mọi request chạy từ máy dev ngày 2026-10-22, khoảng 04:50-05:10 UTC (11:50-12:10 ICT, phiên sáng/nghỉ trưa), UA trung tính, không gửi dữ liệu cá nhân. Tổng: SSC 6 request, HOSE (www + api) ~16, HNX ~12, VSDC 4.
- **Egress đi qua Cloudflare WARP**: `https://www.cloudflare.com/cdn-cgi/trace` → `colo=SIN`, `loc=VN`, `warp=on` (OBSERVED). Vì vậy mọi số đo thời gian dưới đây đi qua Singapore, KHÔNG phải đường VN dân dụng của laptop capture. Phải đo lại trên laptop (xem Câu hỏi mở).
- Đồng hồ máy dev khớp `Date` header của google.com, vsdc.vn, hsx.vn trong ±4 s (OBSERVED), dù `w32time` đang Stopped (main session OBSERVED).
- Chỉ đọc HTML/JS/JSON/RSS dạng text; không chạy code tải về. JS bundle HOSE (`/static/js/main.d430e296.js`) chỉ được grep, và grep chạy trên bản tải chưa xong (~1.6 MB). Mọi endpoint trích dẫn đều đã được gọi thật để xác nhận.
- Nhãn: VERIFIED (URL, 2026-10-22), OBSERVED (lệnh/URL + kết quả), ASSUMED, PRIOR.

## 1. HOSE (www.hsx.vn → api.hsx.vn)

**Kiến trúc.** `https://www.hsx.vn/` là React SPA: HTML 1,900 byte, `<noscript>You need to enable JavaScript to run this app.</noscript>` (OBSERVED). Nội dung lấy qua JSON API. Grep bundle cho thấy (OBSERVED):
- base: `REACT_APP_API_URL_N:"https://api.hsx.vn/n/api/v1"`; URL builder `$n(un)` = base + `"/"` + language id `Jn()` (vi = `1`).
- route người dùng: `"url":"/thong-tin-cong-bo"` (menu "Thông tin công bố").
- listing: `"".concat($n(un),"/news/securitiesType/").concat(Qv(),"?")` với `lt().stringify({pageIndex:1,pageSize:e,startDate:i,endDate:a})`, ngày `YYYY-MM-DD`.
- loại: `REACT_APP_SECURITIES_NEWS_TYPE_DB_ALL:"0"`, `..._STOCK:"1"`, `..._BOND:"2"`, `..._CW:"3"`, `..._ETF:"4"`, `..._FC:"5"`.
- detail: `En.get("".concat($n(un),"/news/").concat(e)`.
- RSS: `href:"".concat("https://api.hsx.vn/n/api/v1","/News/NewsFeed")`.
- file đính kèm: `filePath` có tiền tố `~` được thay bằng `https://staticfile.hsx.vn` (hàm `hr`); danh sách file qua `concat($n(dn),"/mediafiles/")` (base `/m/api/v1`). Chưa gọi thử → ASSUMED về tham số.

**Endpoint listing đã gọi thật (OBSERVED).**
- `GET https://api.hsx.vn/n/api/v1/1/news/securitiesType/1?pageIndex=1&pageSize=10&startDate=2026-10-21&endDate=2026-10-22` → 200, TTFB 3.57 s, 4,530 byte. Bỏ `/1` (language) → 404.
- Dạng: `{"data":{"list":[...],"object":null,"paging":{"pageIndex":1,"pageSize":10,"totalCount":91,"totalPages":10}},"success":true,"message":null}`.
- Mỗi item: `id`, `catId` (21 = "Tin Tổ chức niêm yết"), `code` (**null trong listing**), `title` (có tiền tố mã, ví dụ `"GAS: Nghị quyết HĐQT số 123 ngày 20/10/2026"`), `publishFrom`, `publishTo`, `postedDate`, `summary` (HTML).
- Detail `GET .../1/news/2506777` → 200, 0.44 s, 818 byte; có `code:"GAS"`, `relatedId:"2496"`, `createdDate`, `approvedDate` (= `postedDate`), `updatedDate:None`; **không có body và không có danh sách file** (OBSERVED).

**Giờ đăng: epoch "giả UTC" (finding quan trọng nhất, OBSERVED, 3 bằng chứng).**
- `postedDate=1792665196` đọc như UTC = `2026-10-22 10:33:16`, nhưng response có `date: Thu, 22 Oct 2026 04:58:51 GMT`. Nếu là UTC thật thì item nằm ở tương lai ~5.5 giờ.
- RSS `NewsByCateFeed/21` cho cùng guid `2506861`: `<a10:updated>2026-10-22T10:33:16+07:00</a10:updated>`.
- Frontend format bằng `ht().utc(e,"X").format("DD/MM/YYYY  HH:mm:ss")` (hàm `dr`), tức hiển thị epoch như UTC để ra giờ tường ICT.
- `publishFrom % 86400 == 0` với cả 50 item (nửa đêm ICT mã hoá kiểu giả UTC).
- Hệ quả: `posted_at_utc = postedDate − 7*3600`. Độ chính xác: giây. Poller phải lưu nguyên số nguyên gốc trong raw payload. Bộ chuyển `information_time` (Phase 1) phải trừ 7 giờ và có golden test.

**Thứ tự, id, phân trang (OBSERVED).**
- Sắp theo `postedDate` giảm dần: true. `id` KHÔNG đơn điệu (`2506678` đăng 08:58:28, sau `2506696` đăng 08:55:54). Không dùng watermark theo id; dedupe theo tập `id` cộng cửa sổ chồng lấn theo thời gian.
- `pageSize` ảnh hưởng mạnh tới độ trễ: 1 → 0.6-5 s; 10 → 3.6 s; 50 → TTFB 11.2 s, tổng 18.9 s. Một lần gọi `pageSize=50` với khoảng 2 ngày treo 135 s rồi `schannel: server closed abruptly`. Khuyến nghị `pageSize ≤ 20`, `startDate=endDate=hôm nay (ICT)`, chỉ lật trang khi page 1 không chồng lấn với lần trước.
- Header: `cache-control: public,max-age=1`, `access-control-allow-origin: *`, `x-envoy-upstream-service-time`, cookie `TS01f06596=...` và `TS0d710d04027=...` (dạng cookie F5 BIG-IP ASM, tức có WAF; nhận dạng F5 là PRIOR).
- `https://api.hsx.vn/robots.txt` → 404 sau 7.9 s. `https://www.hsx.vn/robots.txt` → `Disallow:` rỗng (main session OBSERVED). Có route `/dieu-khoan-su-dung` (Terms of use) trong bundle nhưng nội dung chưa đọc → ToS ASSUMED.

**Khối lượng ngày (`securitiesType/0` = mọi loại chứng khoán, OBSERVED qua `totalCount`).**

| Ngày (ICT) | Ngữ cảnh | totalCount |
|---|---|---|
| 2026-03-31 (Thứ Ba) | quanh hạn BCTC năm | 222 |
| 2026-04-20 (Thứ Hai) | hạn BCTC Q1 (+20 ngày) | 304 |
| 2026-07-20 (Thứ Hai) | hạn BCTC Q2 | 181 |
| 2026-10-14 (Thứ Tư) | ngày thường | 129 |
| 2026-10-17 (Thứ Bảy) | cuối tuần | 0 |
| 2026-10-20 (Thứ Ba) | hạn BCTC Q3 | 281 |
| 2026-10-21 (Thứ Tư) | sau hạn | 177 |
| 2026-10-22 đến ~12:10 | | 60 |

- Riêng cổ phiếu (`securitiesType/1`) 21-22/10 đến trưa: 91, khoảng một nửa so với type 0.
- Định cỡ: ngày đỉnh khoảng 300 item trong ~9.5 giờ, trung bình khoảng 0.5/phút, đỉnh cục bộ chưa đo. Ở nhịp 5 phút, page 1 với `pageSize=20` dư sức. Chưa rõ `startDate` lọc theo `publishFrom` hay `postedDate` (ASSUMED: theo `publishFrom`). Phải hỏi cả "hôm qua" vào đầu ngày để bắt item đăng sau nửa đêm hoặc đăng muộn.
- RSS `https://api.hsx.vn/n/api/v1/News/NewsByCateFeed/21` → 200, 8.1 s, chỉ **10 item** (OBSERVED). Quá nông cho ngày đỉnh, chỉ dùng để đối chiếu.

**Phạm vi.** Chỉ tổ chức niêm yết HOSE (và CW/ETF/trái phiếu niêm yết HOSE). Không có HNX/UPCOM. Đây là tin HOSE đăng lại công bố của doanh nghiệp, qua hệ thống ECM (Decision 49/QĐ-SGDHCM). VERIFIED (secondary press) https://vietnamnews.vn/economy/1661264/hose-listed-firms-required-to-use-single-disclosure-point.html và https://en.vietstock.vn/2024/08/hose-listed-firms-required-to-use-single-disclosure-point-36-578799.htm: hệ thống chạy từ 2024-08-15; file PDF ký số, <15MB; công bố của người nội bộ/cổ đông lớn vẫn đi bằng giấy. Vì vậy `postedDate` là giờ HOSE duyệt/đăng (`approvedDate == postedDate`), không phải giờ doanh nghiệp nộp.

## 2. HNX (hnx.vn)

**Trang listing (OBSERVED).**
- `https://hnx.vn/vi-vn/thong-tin-cong-bo-ny-hnx.html` → 200, 97,858 byte, server-rendered. Tab mặc định "TIN TỪ SỞ". Các tab: `TIN TỪ TỔ CHỨC PHÁT HÀNH`, `TIN TỪ TCPH CHƯA GIAO DỊCH`, `LỊCH THỰC HIỆN QUYỀN`.
- Tab TCPH nạp bằng `POST /ModuleArticles/ArticlesCPEtfs/NextPageTinCPNY_CBTCPH` với `pNumPage, pAction, pNhomTin, pTieuDeTin, pMaChungKhoan, pFromDate, pToDate (dd/MM/yyyy), pOrderBy, pNumRecord` (10/20/30/40/50).
- Gọi thật với `pNumRecord=50` → 200, 78,106 byte, 50 dòng, `Tổng số 139411 bản ghi`. Cột: `Ngày đăng tin | Mã CK | Tên TCPH | Tiêu đề tin | File đính kèm`. Giờ dạng `22/10/2026 11:12`, **chỉ HH:MM**, ICT ngầm định.
- Id: `funcViewDetailArticlesByID(638921,1)`. File đính kèm qua `POST /ModuleArticles/ArticlesCPEtfs/ArticlesFileAttach` (`pArticlesID`). Detail qua `/ModuleArticles/ArticlesCPEtfs/PopupTinCongBoDetail`.
- UPCOM có trang riêng (hàm `SearchAndNextPageTinUpCoM` trong source). URL `thong-tin-cong-bo-up-hnx.html` là PRIOR, chưa gọi.
- Lọc theo ngày `pFromDate=pToDate=20/10/2026` chỉ trả `Tổng số 55 bản ghi` (17:22-17:36 ở trang 1). Con số này mâu thuẫn với RSS (80 item hôm nay tính đến 11:41). **Không dùng bộ lọc ngày để định cỡ**, ghi thành câu hỏi mở.

**RSS (OBSERVED, nguồn HNX tốt nhất).**
- `https://hnx.vn/vi-vn/rss.html` liệt kê `/1/vi_vn/thong-tin-cong-bo-tu-so.rss`, `/2/vi_vn/thong-tin-cong-bo-tu-thanh-vien.rss`, `/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss`.
- Feed 3 `https://hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss` → 200, `application/rss+xml`, 44,027 byte, **100 item**. Mới nhất `<guid>638933</guid> ... <pubDate>Thu, 22 Oct 2026 11:41:04 +0700</pubDate>`, cũ nhất `Wed, 21 Oct 2026 19:07:49 +0700`. Có **giây và offset rõ ràng**.
- Cả 50 id của tab niêm yết đều có trong feed 3. Feed 3 còn có thêm item không thuộc tab niêm yết (ví dụ 638922-638933, 11:12-11:41), có thể là UPCOM (ASSUMED).
- Guid giảm dần đơn điệu. Feed 3 có 9 id trống trong dải 638825-638933; feed 1 và feed 2 không lấp các lỗ đó. Feed 2 dùng chung không gian id và có guid trùng (`638891` hai lần); feed 1 có id `60014940`... **Khoảng trống id không phải tín hiệu bỏ sót.** Dùng chồng lấn thời gian + tập guid.
- Item RSS không có mã CK (chỉ tiêu đề). Mã CK nằm ở listing POST hoặc detail.
- `<link>` lộ cổng nội bộ `http://hnx.vn:7978/tin-cung-cap-rss-vi_vn-638933-1.html`. Không theo link này; lưu guid.
- 20/100 item feed 3 có giờ đăng sau 18:00 ngày 21/10. Ngoài khung 08:00-17:30 vẫn có tin, và nhịp mỗi giờ với độ sâu 100 là đủ.
- Header: `Cache-Control: private`, không có ETag/Last-Modified.

**Mạng (OBSERVED, qua WARP).** Mọi request HNX thành công đều có `time_connect` 19.4-19.8 s. Có 2 lần `Failed to connect to hnx.vn:443 after 21075 ms` (IP `103.9.212.46`). Sau khi kết nối, truyền dữ liệu nhanh (<1-6 s). Có thể SYN đầu bị rơi; nguyên nhân (WARP, IPv6 hay firewall) chưa rõ. Poller cần connect-timeout ≥ 30 s, retry, và ghi mỗi lần lỗi vào outage log.

**Lịch sự/pháp lý.**
- `robots.txt` 404 sau ~20 s (main session OBSERVED).
- `https://hnx.vn/vi-vn/khuyen-cao.html` (OBSERVED, trích): "Sở Giao dịch Chứng khoán Hà nội không chịu trách nhiệm về bất kỳ hậu quả, nghĩa vụ nào gây ra cho người sử dụng các thông tin/tài liệu tại website này... Thông tin tại website này có thể bị thay đổi mà không cần thông báo trước."
- Footer: "Liên hệ với Sở Giao dịch Chứng khoán Hà Nội khi phát hành thông tin từ Website này." Điều này phù hợp với việc dùng riêng owner-only, không phát hành lại (§9.1).
- Không thấy hướng dẫn rate limit.

## 3. SSC: Hệ thống công bố thông tin tập trung (congbothongtin.ssc.gov.vn)

- Oracle ADF (OBSERVED): trang đầu là script `AdfLoopbackUtils.runLoopback(... '_afrLoop', '3811...', ...)` cộng `JSESSIONID` và `SERVERID=ids2`. Phải mô phỏng loopback (`?_afrLoop=<n>&_afrWindowMode=0&Adf-Window-Id=<w>`) mới ra nội dung: 83,935 byte, 15.3 s.
- Trang `faces/NewsSearch` mặc định là tab "Báo cáo tài chính". Các tab: `Định kỳ khác`, `Bất thường 24h-khác`, `Chào bán, phát hành chứng khoán`, `Báo cáo công bố`. Có lọc sàn `HNX | HOSE | UPCOM | Chưa niêm yết...`, hiển thị `(1-15 of 17335 items)`, 15 dòng/trang. Cột `Thời gian gửi` chỉ có **ngày** (`21/10/2026`), không có giờ.
- Chuyển tab và lật trang là ADF partial-page request, có state phía server. Muốn poll ổn định phải dùng headless browser hoặc tái tạo giao thức ADF. Đây là rủi ro cao và tốn tài nguyên.
- **Đồng hồ server lệch ~6 phút** (OBSERVED 2 lần): `date: 04:55:11 GMT` khi máy local là `05:01:05Z`; `date: 05:02:00 GMT` khi local là `05:07:50Z`. Cùng lúc, hsx.vn, vsdc.vn và google.com khớp local trong vài giây. Nếu SSC đóng dấu giờ nộp bằng đồng hồ này thì giờ đó sai ~6 phút. Đây là lý do mạnh để `first_seen_at` phải lấy từ máy NTP của ta.
- `robots.txt` 404 (main session OBSERVED). ToS không tìm thấy.
- Kết luận: không đưa vào vòng 2-5 phút. Có thể đối chiếu tay theo ngày cho BCTC trong bước 0a.6.

## 4. VSDC (corporate actions: ngày ĐKCC, quyền)

- `https://vsd.vn/` → 302 tới `https://vsdc.vn/vi/`. `www.vsd.vn` lỗi `SEC_E_WRONG_PRINCIPAL` (cert `CN=*.vsdc.vn`, SAN `*.vsdc.vn, vsdc.vn`) (OBSERVED). Poller phải trỏ thẳng `vsdc.vn`.
- `https://vsdc.vn/vi/tin-thi-truong-co-so` → 200, 0.69 s, 56,909 byte, server-rendered. 15 dòng/trang, `Hiển thị: 1 - 15 / 98887 bản ghi`. Dòng dạng `PNT: Chi cổ tức năm 2025 bằng tiền | Cập nhật ngày 22/10/2026 - 10:26:31` (có giây, ICT ngầm định). Detail `/vi/ad/{id}` (ví dụ `/vi/ad/201667`). Có meta `__VPToken` (anti-forgery), nên lật trang có thể cần token (ASSUMED).
- Nhãn "**Cập nhật** ngày" là giờ cập nhật, có thể đổi khi bài được sửa (ASSUMED). Raw phải lưu nguyên và dedupe theo `/vi/ad/{id}`, không theo giờ.
- `robots.txt` → redirect `page-not-found` (thực chất không có robots).
- Dùng cho lịch quyền (Phase 3), không phải nguồn tin công bố chính.

## 5. Cơ sở pháp lý và hạn công bố

- Thông tư 96/2020/TT-BTC (secondary, từ snippet tìm kiếm thuvienphapluat.vn, chưa đọc văn bản gốc → ASSUMED về con số):
  - Điều 11 khoản 1: công bố bất thường "trong thời hạn 24 giờ";
  - Điều 14 khoản 3: BCTC quý "20 ngày kể từ ngày kết thúc quý" (công ty mẹ có đơn vị trực thuộc: 30 ngày; quý soát xét: 5 ngày sau ký soát xét, ≤45 ngày);
  - Điều 10: báo cáo thường niên ≤20 ngày sau BCTC năm kiểm toán, ≤110 ngày.
  - PRIOR: BCTC năm kiểm toán 10 ngày sau ký kiểm toán, ≤90 ngày; bán niên soát xét 5 ngày sau ký, ≤45 ngày.
- Số đếm OBSERVED khớp với hạn quý +20 ngày: 304 (20/4), 281 (20/10) so với 129 ngày thường.
- TT 68/2024 thêm công bố tiếng Anh: định kỳ từ 2025-01-01 (đã VERIFIED trong roadmap §4). Poller có thể thấy cặp tin VI/EN cho cùng sự kiện; dedupe ở Phase 1, không ở capture.
- Link `https://vbpl.vn/FileData/TW/Lists/VBPQ/Attachments/146048/VanBanGoc_96-2020-TT-BTC.pdf` (AGENTS.md/roadmap ghi VERIFIED) trả **404** hôm nay (OBSERVED qua WebFetch). Cần thay link.
- Chuyển sàn (secondary press, từ WebSearch, trùng với AGENTS.md):
  - Theo VNX ngày 1/10/2026: HNX giao dịch cổ phiếu niêm yết lần cuối 23/12/2026, nghỉ 24-25/12, giao dịch tại HOSE từ 28/12/2026; căn cứ TT 139/2025/TT-BTC sửa TT 57/2021/TT-BTC.
  - HNX tập trung vào trái phiếu, phái sinh và các sản phẩm khác.
  - Nguồn: https://thitruongtaichinhtiente.vn/toan-bo-co-phieu-niem-yet-se-giao-dich-tai-hose-tu-cuoi-thang-12-2026-85954.html, https://thoibaotaichinhvietnam.vn/chuyen-toan-bo-co-phieu-hnx-sang-hose-trong-nam-2026-nghien-cuu-giao-dich-xuyen-trua-va-noi-bien-do-204943.html, https://doanhnhan.baophapluat.vn/hon-300-co-phieu-roi-hnx-sang-hose-bai-toan-thanh-khoan-va-suc-ep-tieu-chuan-niem-yet-moi.html.
  - UPCOM ở lại đâu và công bố đi qua cổng nào: không nguồn nào nói rõ → ASSUMED HNX giữ UPCOM.
  - Hệ quả cho poller:
    - trước 28/12: HOSE API (HOSE) + HNX RSS (HNX + UPCOM + trái phiếu);
    - từ 28/12: ~300 mã chuyển sang danh sách HOSE, nên khối lượng HOSE tăng và giá trị đếm ngày phải định cỡ lại; HNX RSS vẫn cần cho UPCOM.
    - Ánh xạ issuer → cổng phải có `effective_from`.
- Dữ liệu cá nhân: tiêu đề OBSERVED chứa tên người thật, ví dụ `"RYG: Báo cáo về thay đổi sở hữu của cổ đông lớn <insider-A>, <insider-B>"` (tên thật đã thay bằng placeholder ngày 2026-10-22 — red-team r1 RT8).
  - Luật BVDLCN 91/2025/QH15 có hiệu lực 01/01/2026 (VERIFIED, secondary): https://hatinh.gov.vn/vi/bai-viet/luat-bao-ve-du-lieu-ca-nhan-chinh-thuc-co-hieu-luc-thi-hanh-tu-ngay-112026, https://hoatieu.vn/phap-luat/luat-bao-ve-du-lieu-ca-nhan-so-91-2025-qh15-234849.
  - Điều 16 (công khai dữ liệu) và Điều 19 (xử lý không cần đồng ý) chưa đọc → ASSUMED.
  - Biện pháp: raw JSONL chỉ trong đường dẫn gitignore, backup vào thư mục cloud riêng của owner; không đưa tên vào alert hay log telemetry; không phân phối lại.

## 6. Ma trận đánh đổi

| Tiêu chí | HOSE JSON API | HNX RSS feed 3 | HNX POST listing | VSDC list | SSC ADF |
|---|---|---|---|---|---|
| Giờ đăng | giây, epoch giả UTC (−7 h) | giây, `+0700` rõ | HH:MM | giây, "Cập nhật" | chỉ ngày |
| Độ phủ | HOSE (mọi loại CK) | HNX + (có thể) UPCOM + trái phiếu | HNX niêm yết (UPCOM trang riêng) | quyền mọi sàn | mọi sàn, chia tab |
| Mã CK trong list | tiền tố title (`code` null) | không | có cột | tiền tố title | có |
| Chi phí 1 lượt | 1 GET, 1-19 s tuỳ pageSize | 1 GET, ~20-25 s (connect) | 1 POST ~20 s | 1 GET <1 s | 2+ request có state, 15 s |
| Độ sâu 1 lượt | tuỳ pageSize, khuyên ≤20 | 100 item (~16 h ngày thường) | 10-50 | 15 | 15 |
| Ổn định | API nội bộ SPA, không hứa hẹn; WAF F5 | RSS công khai, được liệt kê ở `/rss.html` | HTML module, dễ đổi | HTML + token | ADF, rất giòn |
| robots/ToS | robots www cho phép; api 404; ToS chưa đọc | robots 404; có Khuyến cáo | như RSS | không robots | robots 404 |
| Rủi ro sau 28/12 | khối lượng tăng | mất cổ phiếu niêm yết, giữ UPCOM? | tab niêm yết rỗng dần | thấp | thấp |
| Độ chín | 2 năm (ECM từ 2024-08) | RSS lâu đời (PRIOR) | | | Version 1.7 (© 2019) |

## Kết luận (xếp hạng)

1. **Nguồn chính A = HOSE JSON API** `GET https://api.hsx.vn/n/api/v1/1/news/securitiesType/0?pageIndex=1&pageSize=20&startDate=<today ICT>&endDate=<today ICT>`, gửi kèm `Origin/Referer: https://www.hsx.vn/`.
   - Lật trang tới khi gặp `id` đã thấy hoặc hết `totalCount`. Lúc 08:00 ICT hỏi thêm ngày hôm trước.
   - Lưu raw JSON nguyên văn + `raw_sha256` + `first_seen_at` (NTP).
   - `postedDate` là **ICT wall-clock dạng epoch**: `information_time_utc = postedDate − 25200` (Phase 1). Cần golden test với fixture `2506861 → 2026-10-22T03:33:16Z`.
   - Dedupe theo `id`, không theo thứ tự id.
2. **Nguồn chính B (song song, không phải fallback) = HNX RSS feed 3** `https://hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss`.
   - Có giây, `+0700`, 100 item mỗi lượt. Lưu nguyên XML.
   - Dedupe theo guid. Không dùng lỗ id làm tín hiệu bỏ sót. Không theo link `:7978`.
   - Connect timeout ≥30 s, retry có backoff. Mỗi lượt hỏng ghi outage log; nếu khoảng trống dài hơn cửa sổ phủ của 100 item, đánh `pit_quality=late` cho item đầu tiên thấy sau đó.
   - Với lịch 2-5 phút, hai nguồn A + B tốn tối đa ~2 request mỗi 2-5 phút. Như vậy là lịch sự.
3. **Fallback HNX = POST `NextPageTinCPNY_CBTCPH`** (`pNumRecord=50`), có mã CK. Dùng khi RSS hỏng hoặc để backfill sau outage. Không tin bộ lọc ngày cho đến khi giải thích được con số 55.
4. **VSDC `https://vsdc.vn/vi/tin-thi-truong-co-so`**: nguồn phụ cho lịch quyền (ĐKCC/cổ tức), hỏi mỗi giờ là đủ ở Phase 0. Có thể để sang Phase 3 nếu cần cắt scope.
5. **SSC loại khỏi vòng poll**: date-only, ADF có state, đồng hồ lệch ~6 phút. Chỉ dùng khi đối chiếu tay (0a.6).
6. Plan phải ghi các bước chặn: owner chọn cổng (đề xuất: A + B); laptop capture đo lại độ trễ và tỉ lệ connect HNX; đọc ToS HOSE; xác nhận PDPL.
7. Escalation: lựa chọn A + B phụ thuộc vào số đo (độ trễ, tỉ lệ lỗi, độ sâu so với cadence). Nên chạy một bakeoff 1 ngày giao dịch trên laptop capture (cả 3 nguồn HOSE API / HNX RSS / HNX POST, ghi `first_seen_at`, lỗi và độ trễ) trước khi tính 5 phiên exit gate. Số liệu trong report này đo qua WARP/SIN, vào giờ nghỉ trưa.

## Câu hỏi mở

1. **Độ trễ/khả dụng từ laptop capture (không WARP):** chạy 1 ngày, mỗi 5 phút, `curl -sS -o NUL -w "%{time_connect} %{time_starttransfer} %{http_code}\n" --connect-timeout 30` cho HOSE API (`pageSize=20`), HNX RSS feed 3 và HNX POST. Ghi CSV. Câu hỏi: HNX connect 20 s là do WARP hay do server?
2. **Tính lịch sự của HOSE dưới WAF F5 qua nhiều ngày:** sau 1 ngày poll, có 403, captcha hay reset không? Đếm mã lỗi theo giờ.
3. **HOSE `startDate` lọc theo trường nào:** hỏi `startDate=endDate=<D>` và kiểm tra item có `postedDate` sau 23:00 hoặc `publishFrom ≠ D`. Đồng thời so `totalCount` lúc 23:59 với lúc 08:00 hôm sau.
4. **HNX bộ lọc ngày = 55 vào 20/10:** POST `pAction=1` với `pFromDate=20/10/2026&pToDate=20/10/2026`, lật hết trang, so với số item `pubDate` 20/10 thu được từ RSS (phải poll RSS trong ngày đó). Có thể lọc theo ngày khác (ngày sự kiện).
5. **RSS feed 3 có UPCOM không:** lấy 5 guid không có trong tab niêm yết, POST `/ModuleArticles/ArticlesCPEtfs/PopupTinCongBoDetail` (`pArticlesID`) hoặc tìm trên trang UPCOM `thong-tin-cong-bo-up-hnx.html` (URL PRIOR) để xem mã thuộc sàn nào.
6. **Độ trễ cổng so với doanh nghiệp:** trong 0a.6, với 30 công bố, so `postedDate` HOSE (−7 h) / `pubDate` HNX với giờ ký trên PDF và giờ vnstock. Đây là số đo "lag cổng" cho `pit_quality`.
7. **VSDC "Cập nhật" có đổi khi sửa bài không:** poll `/vi/tin-thi-truong-co-so` mỗi giờ trong 5 ngày; với cùng `/vi/ad/{id}`, so chuỗi giờ giữa các lần.
8. **ToS HOSE:** mở `https://www.hsx.vn/dieu-khoan-su-dung` trong trình duyệt (SPA), ghi trích dẫn và ngày vào AGENTS.md. Đọc Điều 16/19 Luật 91/2025/QH15 từ văn bản gốc.
9. **Sau 28/12/2026:** UPCOM do ai quản lý, HNX RSS feed 3 còn chạy không, HOSE `catId` cho mã chuyển sàn là gì: probe lại vào 2026-12-28 và 2026-12-29.
10. **Đồng hồ SSC lệch ~6 phút:** đo lại `Date` header 3 lần cách nhau 1 giờ từ laptop NTP. Nếu lệch ổn định, ghi vào AGENTS.md (OBSERVED) như một lý do không dùng giờ SSC.
11. **Thay link TT 96/2020 bị 404** bằng bản gốc trên vbpl.vn hoặc congbao.chinhphu.vn, rồi đọc Điều 10/11/14 để chuyển con số hạn từ ASSUMED sang VERIFIED.

## Nguồn

- OBSERVED (2026-10-22, curl từ máy dev qua WARP): `https://www.hsx.vn/`, `https://www.hsx.vn/static/js/main.d430e296.js`, `https://api.hsx.vn/n/api/v1/1/news/securitiesType/{0,1}?...`, `https://api.hsx.vn/n/api/v1/1/news/2506777`, `https://api.hsx.vn/n/api/v1/News/NewsFeed`, `https://api.hsx.vn/n/api/v1/News/NewsByCateFeed/21`, `https://api.hsx.vn/robots.txt`; `https://hnx.vn/vi-vn/`, `https://hnx.vn/vi-vn/thong-tin-cong-bo-ny-hnx.html`, `POST https://hnx.vn/ModuleArticles/ArticlesCPEtfs/NextPageTinCPNY_CBTCPH`, `https://hnx.vn/vi-vn/rss.html`, `https://hnx.vn/{1,2,3}/vi_vn/*.rss`, `https://hnx.vn/vi-vn/khuyen-cao.html`; `https://congbothongtin.ssc.gov.vn/`, `https://congbothongtin.ssc.gov.vn/faces/NewsSearch`; `https://vsd.vn/`, `https://vsdc.vn/vi/tin-thi-truong-co-so`, `https://vsdc.vn/robots.txt`; `https://www.cloudflare.com/cdn-cgi/trace`.
- VERIFIED secondary: https://vietnamnews.vn/economy/1661264/hose-listed-firms-required-to-use-single-disclosure-point.html, https://en.vietstock.vn/2024/08/hose-listed-firms-required-to-use-single-disclosure-point-36-578799.htm, https://vietnamnews.vn/economy/1651308/ssc-to-launch-single-point-information-disclosure-system-for-listed-firms.html, https://thitruongtaichinhtiente.vn/toan-bo-co-phieu-niem-yet-se-giao-dich-tai-hose-tu-cuoi-thang-12-2026-85954.html, https://thoibaotaichinhvietnam.vn/chuyen-toan-bo-co-phieu-hnx-sang-hose-trong-nam-2026-nghien-cuu-giao-dich-xuyen-trua-va-noi-bien-do-204943.html, https://hatinh.gov.vn/vi/bai-viet/luat-bao-ve-du-lieu-ca-nhan-chinh-thuc-co-hieu-luc-thi-hanh-tu-ngay-112026.
- Secondary (snippet, chưa đọc toàn văn): https://thuvienphapluat.vn/phap-luat/trong-cong-bo-thong-tin-dinh-ky-cong-ty-dai-chung-phai-cong-bo-nhung-noi-dung-nao-viec-cong-bo-thon-7097.html (WebFetch trả 403).
- Tự làm, không delegate; route-all gemini không kiểm tra vì task chỉ định một researcher.
