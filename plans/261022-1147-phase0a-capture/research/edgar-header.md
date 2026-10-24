# Research: EDGAR filing-header fixtures + golden test cho information time

- Ngày: 2026-10-22. Mode: depth. Phạm vi: phase-4 (`tests/test_edgar_time.py`) của plan 261022-1147-phase0a-capture.
- Nhãn: VERIFIED (URL, fetch 2026-10-22), OBSERVED (tự chạy/fetch, kèm lệnh + kết quả), ASSUMED (chưa kiểm), PRIOR (kiến thức training, chưa kiểm lại).
- Không cài package, không tải binary. Chỉ 1 request tới `www.sec.gov/Archives` (bị 403, xem §4). Không gửi thông tin cá nhân.
- `[NO_AGENT_DELEGATE]`: tự research, không spawn thêm agent.
- Ghi chú đường dẫn: task yêu cầu `H:\study\Project\FinSec\plans\...`, nhưng hook worktree chặn ghi vào main checkout; file này nằm trong worktree `phase0a-verification-vn-poller-fd37f6` ở cùng đường dẫn tương đối.

## Câu hỏi nghiên cứu
Làm sao ghi fixture header EDGAR của 4 accession (roadmap §2.1) đúng fair-access, không commit danh tính, rồi viết golden test khẳng định `information_time` = `<ACCEPTANCE-DATETIME>` đọc theo `America/New_York`, lưu UTC tz-aware cạnh raw string; parser phải raise thay vì đoán.

Tiêu chí: đúng nguồn sec.gov; chạy offline trong CI public (Ubuntu) và trên Windows dev; byte-stable; tối thiểu PII.

---

## 1. URL header, thư mục CIK, định dạng `<ACCEPTANCE-DATETIME>`

### 1.1 URL
- SEC tài liệu hoá đường dẫn `.hdr.sgml` (có thư mục accession không gạch) và `-index.html`, `.txt`. VERIFIED https://www.sec.gov/os/accessing-edgar-data, nguyên văn:
  - `/Archives/edgar/data/1122304/000119312515118890/0001193125-15-118890.hdr.sgml` — "the SGML header contents. Note the additional 'accession-number-without-dashes' directory in the path."
  - "All the documents submitted for a given filing will be in this directory."
- `-index-headers.html` KHÔNG được nhắc trên trang đó (OBSERVED: WebFetch trang trên, chỉ có `-index.html`). Nhưng nó tồn tại: edgartools commit nhiều bản ghi lấy từ sec.gov, ví dụ `data/headers/index-headers.html`, `data/headers/0001971857-23-000246-index-headers.html`, `data/headers/23AndMe.index-headers.html` (OBSERVED: `gh search code "ACCEPTANCE-DATETIME" --repo dgunning/edgartools`, và `curl https://raw.githubusercontent.com/dgunning/edgartools/main/data/headers/index-headers.html` → 200, 5304 bytes).
- Mẫu URL cho plan (ASSUMED cho từng accession cụ thể đến khi ghi fixture):
  - `https://www.sec.gov/Archives/edgar/data/<CIK>/<acc-no-dashes>/<acc>.hdr.sgml`
  - `https://www.sec.gov/Archives/edgar/data/<CIK>/<acc-no-dashes>/<acc>-index-headers.html`
  - Apple CIK = 320193 [PRIOR + suy từ prefix accession `0000320193-`, tức Apple tự nộp; chưa fetch]. Ví dụ: `https://www.sec.gov/Archives/edgar/data/320193/000032019326000018/0000320193-26-000018.hdr.sgml`.

### 1.2 Cấu trúc hai loại file
- `-index-headers.html` mang header HAI lần (OBSERVED, file edgartools `index-headers.html`):
  - dòng 4, trong HTML comment, dạng tag thô: `<ACCEPTANCE-DATETIME>20240603160821`
  - dòng 53, trong `<PRE>`, dạng escape: `&lt;ACCEPTANCE-DATETIME&gt;20240603160821`, tiếp theo là dòng kiểu "space dialect": `ACCESSION NUMBER:\t\t0000905729-24-000095`, `FILED AS OF DATE:\t\t20240603`.
  - Dòng đầu comment: `<SEC-HEADER>0000905729-24-000095.hdr.sgml : 20240603` → header gắn tên file `.hdr.sgml`.
- `.hdr.sgml` hiện đại: nội dung chính xác CHƯA kiểm (ASSUMED). Rủi ro thật: edgartools ghi nhận hai dialect cho cùng root tag `<SEC-HEADER>` (tagged `<ACCESSION-NUMBER>` và "space" `ACCESSION NUMBER:\t...`), và ví dụ `.hdr.sgml` trước 2004 của họ KHÔNG có dòng `ACCEPTANCE-DATETIME` (OBSERVED: `edgar/sgml/sgml_parser.py:220-235` trên nhánh main). PDS spec cũng không định nghĩa tag này (§1.3). → Script ghi fixture phải assert tag có mặt trong từng file trước khi lưu.

### 1.3 Định dạng `<ACCEPTANCE-DATETIME>` — 14 chữ số, không zone
- Nguồn sec.gov KHÔNG định nghĩa tag này. OBSERVED: tải PDS Technical Specification (https://www.sec.gov/files/info/edgar/specifications/pds_dissemination_spec.pdf), `pdftotext -layout`, `grep -i "ACCEPTANCE-DATETIME"` → 0 dòng. Spec chỉ có:
  - Control block (Table 6): "Acceptance Date 8 Date the submission was accepted by EDGAR. Format is YYYYMMDD." và "Acceptance Time 6 Time (EST) at which the submission was accepted by EDGAR. Format is HHMMSS." (VERIFIED, cùng URL) — tương tự về ngữ nghĩa, không phải tag header.
  - Bẫy cần tránh: tag `<TIMESTAMP>` (chỉ cho Post Acceptance Correction) có format `YYYYMMDD:HHMMSS` CÓ dấu hai chấm — tag khác, đừng nhầm.
- Bằng chứng cho `YYYYMMDDHHMMSS`:
  1. Bản ghi sec.gov trong edgartools: `20240603160821`, `20240607181030`, `20230606213204`, `20241101060136` (Apple 0000320193-24-000123, `docs/guides/sgml.md`) (OBSERVED, gh search).
  2. Regex edgartools `edgar/sgml/sgml_header.py:23`: `_ACCEPTANCE_RE = re.compile(r'<ACCEPTANCE-DATETIME>(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})')` (OBSERVED).
  3. PDS control block tách `YYYYMMDD` + `HHMMSS` (VERIFIED, chỉ là tương tự).
  - Chỉ 1 nguồn độc lập thật sự (edgartools; mục 3 là tương tự) → chưa đủ 3 nguồn. Fixture ghi thật sẽ đóng câu này.
- Cảnh báo: edgartools trả datetime NAIVE: `sgml_header.py:453-456` `return datetime.strptime(acceptance, "%Y-%m-%d %H:%M:%S")` (OBSERVED). Không được dùng giá trị đó làm `information_time` mà không gắn zone. → Viết parser riêng (~20 dòng), không phụ thuộc edgartools; cũng khớp ràng buộc "không viết lại connector ở Phase 0".

### 1.4 Thư mục CIK cho Form 4 0001140361-26-040127
- Không kiểm được (bị 403, §4). Trạng thái:
  - Prefix accession = CIK bên NỘP, "could be the company or a third-party filer agent" (VERIFIED https://www.sec.gov/os/accessing-edgar-data).
  - "Some filer agents without a regulatory requirement to make disclosure filings with the SEC have a CIK but no searchable presence in the public EDGAR database." (VERIFIED, cùng trang) → thư mục CIK agent 1140361 có thể không chứa filing.
  - Ví dụ chính SEC: accession `0001193125-15-118890` (prefix agent 1193125) nằm dưới CIK công ty 1122304 (VERIFIED, cùng trang) → thư mục là của bên báo cáo, không phải agent.
  - Index Form 4 liệt kê cả Issuer và Reporting với CIK riêng (VERIFIED qua kết quả tìm kiếm, ví dụ https://www.sec.gov/Archives/edgar/data/1080224/000108022407000006/0001080224-07-000006-index.html). Fixture Form 4 của edgartools có 3 CIK: agent 1062993 (prefix), issuer 0000914156, owner 0001641388 (OBSERVED), nhưng không ghi đường fetch.
  - Kết luận: ASSUMED thư mục issuer và reporting-owner đều phục vụ filing; thư mục agent có thể 404.
- Khuyến nghị: script ghi nhận danh sách CIK theo thứ tự [issuer, reporting owner, agent], dừng ở 200 đầu tiên, ghi `cik_folder_used` vào manifest. Issuer CIK của Form 4 này KHÔNG có trong roadmap → plan phải nhận nó làm input (tra EDGAR company search hoặc submissions JSON bằng UA khai báo).

## 2. Eastern time và giờ EDGAR

- Giờ ET, DST theo giờ hiện hành:
  - 17 CFR 232.13(a)(2): "on or before 5:30 p.m. Eastern Standard Time or Eastern Daylight Saving Time, whichever is currently in effect" (VERIFIED https://www.ecfr.gov/current/title-17/chapter-II/part-232/subpart-A/section-232.13).
  - PDS control block: "Time (EST)" (VERIFIED, PDS spec ở trên). Chữ "EST" ở đây là BẪY: không được dùng offset cố định −05:00. Parser dùng `ZoneInfo("America/New_York")`. Cặp 8-K tháng 7 (EDT) và 8-K tháng 1 (EST) trong roadmap §2.1 chính là cặp bắt lỗi này.
  - Danh sách viết tắt PDS spec: "ET Eastern Time" (VERIFIED).
- Giờ nhận filing:
  - "EDGAR is available for filing from 6:00 a.m. to 10:00 p.m. ET, Monday through Friday, except federal holidays." và "EDGAR will not process transmissions submitted or attempted to be submitted after 10:00 p.m." (VERIFIED https://sec.gov/node/956846).
  - Cùng khung "from 6:00 a.m. to 10:00 p.m., ET", sau "5:30 p.m. ET" một số loại sang ngày làm việc sau (VERIFIED https://www.sec.gov/os/accessing-edgar-data).
  - Ngoại lệ 10 p.m. (cùng ngày filing date): Forms 3, 4, 5, Form 144, Schedule 14N, 13D/13G, Rule 462(b) (VERIFIED eCFR 232.13(a)(3)-(4)). → Form 4 lúc 18:32:55 vẫn có FILING-DATE cùng ngày; 8-K sau 17:30 thì FILING-DATE là ngày làm việc sau. `FILED AS OF DATE` không bao giờ là information time.
- Hệ quả cho parser:
  - Khung DST gap/fold của New York (01:00-03:00 giờ địa phương, Chủ nhật) nằm HOÀN TOÀN ngoài giờ EDGAR → một acceptance hợp lệ không bao giờ mơ hồ. Vẫn phải kiểm và raise (dữ liệu hỏng/tag nhầm).
  - Từ chối ngoài [06:00, 22:00] ET là hợp lý, nhưng ranh giới có rủi ro (ASSUMED): acceptance có thể trễ hơn lúc bắt đầu truyền vài phút; SEC đôi khi đổi giờ; accession do SEC tự upload (`0000000000-`) hay bản giấy có thể khác. → Raise lỗi có tên (`EdgarAcceptanceOutsideHours`), khung giờ là tham số; Phase 1 quyết định giữ hay hạ thành cờ `pit_quality`.

## 3. Fair access, User-Agent, `EDGAR_IDENTITY`

- UA khai báo: "Please declare your user agent in request headers:" mẫu `Sample Company Name AdminContact@<sample company domain>.com`, kèm `Accept-Encoding: gzip, deflate`, `Host: www.sec.gov` (VERIFIED https://www.sec.gov/os/accessing-edgar-data và https://www.sec.gov/os/webmaster-faq).
- Tốc độ: "Current max request rate: 10 requests/second." (VERIFIED accessing-edgar-data); "no more than 10 requests per second, regardless of the number of machines" (VERIFIED https://www.sec.gov/privacy). Roadmap đã chọn throttle 5 rps (`docs/project-roadmap.md:164`).
- Hình thức 403: xem §4 (OBSERVED). Trang 403 nói: "If a user or application submits more than 10 requests per second, further requests from the IP address(es) may be limited for a brief period. Once the rate of requests has dropped below the threshold for 10 minutes, the user may resume accessing content on SEC.gov."
- Giữ danh tính ngoài git:
  - `.env` đã bị gitignore (OBSERVED `.gitignore`), `.env.example:5` đã có placeholder `EDGAR_IDENTITY="FinSecAlphaRadar research@yourdomain.com"` (OBSERVED).
  - Tên biến trùng quy ước edgartools: `edgar/settings.py`: `edgar_identity = 'EDGAR_IDENTITY'` (OBSERVED gh search) → sau này dùng chung được.
  - Script ghi fixture (đặt dưới `scripts/spike/`, KHÔNG dưới `tests/`): đọc `EDGAR_IDENTITY` từ môi trường; thiếu hoặc không chứa `@` → thoát lỗi trước khi gửi request; `time.sleep(≥0.2)` giữa các request (≤5 rps); tổng 8 request (4 accession × 2 file) + vài lần thử CIK.
  - Script phải từ chối lưu nếu: status ≠ 200; body chứa "Undeclared Automated Tool"; không có `<ACCEPTANCE-DATETIME>` đúng 14 chữ số; accession trong body ≠ accession yêu cầu. (Trang 403 là HTML hợp lệ, nếu không kiểm sẽ thành fixture rác.)
  - Manifest chỉ ghi `user_agent_declared: true`, KHÔNG ghi giá trị UA. Không log header request.
  - `requests`/`httpx` không có trong deps; dùng `urllib.request` của stdlib cho spike (giải nén gzip tay hoặc không gửi `Accept-Encoding`) để khỏi đụng `pyproject.toml` (ASSUMED: lựa chọn thiết kế).

## 4. Probe thật: UA không có liên hệ bị từ chối (OBSERVED)

Lệnh (1 request duy nhất, UA trung tính, không email):
```
curl -sS --compressed -A "FinSec-AlphaRadar-research-probe" -D hdrs -o body -w "%{http_code} %{size_download}\n" \
  "https://www.sec.gov/Archives/edgar/data/320193/000032019326000018/0000320193-26-000018-index-headers.html"
```
Kết quả, 2026-10-22 04:52:04 GMT:
- `403 2163`; `Content-Type: text/html`; `Content-Encoding: gzip`; `Cache-control: no-cache, no-store, must-revalidate`.
- `<title>SEC.gov | Your Request Originates from an Undeclared Automated Tool</title>`
- "Your request has been identified as part of a network of automated tools outside of the acceptable policy and will be managed until action is taken to declare your traffic. Please declare your traffic by updating your user agent to include company specific information."
- `Reference ID: 0.cfd0dd17.1792644724.facb5aed`
- Dừng theo chỉ dẫn task: không thử lại với UA có liên hệ, không tải 4 header. Vì vậy ngày của 3/4 accession, thư mục CIK Form 4 và layout `.hdr.sgml` vẫn là ASSUMED.

## 5. Python: zoneinfo, tzdata, fold

- tzdata trên Windows: "Some systems, including notably Windows systems, do not have an IANA database available, and so for projects targeting cross-platform compatibility that require time zone data, it is recommended to declare a dependency on tzdata." Thứ tự tìm: `TZPATH` rồi package `tzdata`; nếu không có cả hai thì mọi `ZoneInfo(...)` raise `ZoneInfoNotFoundError` (subclass `KeyError`). (VERIFIED https://docs.python.org/3/library/zoneinfo.html)
- `.venv` (Python 3.13.14) không có tzdata → `ZoneInfo('America/New_York')` raise (OBSERVED bởi main session 2026-10-22). `pyproject.toml` hiện không khai tzdata (OBSERVED `pyproject.toml:11-17`).
- Điểm mù CI: runner `ubuntu-24.04` (OBSERVED `.github/workflows/ci.yml:13`) có tz hệ thống [PRIOR], nên test xanh trên CI nhưng đỏ trên máy Windows. → Thêm `tzdata` vào `[project].dependencies` (runtime, không phải dev, vì parser nằm trong `src/`). Marker `sys_platform == "win32"` (giống METADATA vnstock 5.1.0) đủ cho Windows; khuyến nghị KHÔNG marker để Linux/Windows dùng cùng bộ dữ liệu IANA đã pin (ASSUMED: lựa chọn; package chỉ là dữ liệu). Thêm test riêng: `ZoneInfo("America/New_York")` load được, lỗi có thông điệp "install tzdata". Thay đổi `pyproject.toml` → khai cạnh liên plan từ `261021-1314-phase0b-foundations`.
- Fold/gap:
  - zoneinfo: với thời điểm mơ hồ, `fold=0` dùng offset trước chuyển đổi, `fold=1` dùng offset sau (VERIFIED zoneinfo docs). Docs không mô tả thời điểm không tồn tại (OBSERVED: không có trong trang).
  - PEP 495: trong gap, `fold=0` dùng luật trước chuyển đổi, `fold=1` dùng luật sau; không có hàm stdlib phát hiện, mẫu "Strict Invalid Time Checking": `u = dt.utcoffset()`, `v = dt.replace(fold=not dt.fold).utcoffset()`; `u == v` → hợp lệ; ngược lại mơ hồ nếu `(u < v) == dt.fold`, còn lại là không tồn tại (VERIFIED https://peps.python.org/pep-0495/).
  - Nghĩa là zoneinfo KHÔNG raise với giờ trong gap; nó lặng lẽ cho một offset → phải tự kiểm. Raise thay vì đoán vì đoán sai lệch 1 giờ đúng quanh 16:00 ET có thể đổi Day 0 (roadmap §2.1 "Day 0 ... close comes after information_time") — tức là look-ahead.
- Thuật toán đề xuất (spec cho plan, không phải code đã chạy):
  1. Regex chặt `^\d{14}$` trên giá trị sau tag (strip `\r\n` cuối dòng).
  2. `datetime.strptime(raw, "%Y%m%d%H%M%S")` → lỗi lịch (tháng 13, 30/02) raise `ValueError` gói thành `EdgarAcceptanceFormatError`.
  3. Gắn `ZoneInfo("America/New_York")`, kiểm PEP 495 (fold 0 vs 1) → `EdgarAcceptanceAmbiguous` / `EdgarAcceptanceNonexistent`.
  4. Kiểm giờ [06:00, 22:00] ET và thứ Hai-Sáu → `EdgarAcceptanceOutsideHours`. Ngày lễ liên bang KHÔNG kiểm ở Phase 0 (cần lịch nghỉ; thuộc Phase 1).
  5. `.astimezone(timezone.utc)`; trả về `(raw, utc)`.
  - Thứ tự lỗi cố định như trên (định dạng → lịch → DST → giờ) để test assert đúng một loại lỗi. Nhiều tag `<ACCEPTANCE-DATETIME>` khác giá trị trong một file → raise; trong `-index-headers.html` hai lần xuất hiện (thô + escape) phải bằng nhau.

## 6. Thiết kế golden test

### 6.1 Layout
```
tests/fixtures/edgar_headers/
  manifest.json
  0000320193-26-000018.hdr.sgml
  0000320193-26-000018-index-headers.html
  0001140361-26-040127.hdr.sgml          (đã giảm PII, xem 6.4)
  0001140361-26-040127-index-headers.html (đã giảm PII)
  0000320193-26-000020.hdr.sgml
  0000320193-26-000020-index-headers.html
  0000320193-26-000005.hdr.sgml
  0000320193-26-000005-index-headers.html
tests/test_edgar_time.py
scripts/spike/record_edgar_headers.py     (gọi mạng; không bao giờ chạy trong pytest)
```
- Không có thư mục `data/`. Đuôi `.sgml`/`.html` không bị ignore (OBSERVED `.gitignore`: các pattern dữ liệu là `*.jsonl.gz`, `*.parquet`, `*.duckdb*`, `*.sqlite*`, `*.db*`, cùng `*.bin`, `*.pem`, `*.key`... không pattern nào khớp `.sgml`/`.html`), và không đụng `test_data_artefacts_are_ignored` (`tests/test_repo_hygiene.py:59`).
- PHÁT HIỆN QUAN TRỌNG — line endings: repo chạy `core.autocrlf=true` trên Windows (AGENTS.md "`.gitignore` (OBSERVED 2026-10-22)"), và `.gitattributes` hiện chỉ có `harness/** -text` (OBSERVED). Fixture text sẽ bị đổi LF→CRLF khi checkout Windows → sha256 trên Windows ≠ trên CI Ubuntu. → Thêm `tests/fixtures/edgar_headers/** -text` (hoặc `binary`) vào `.gitattributes` trong cùng commit; test đọc bằng `open(path, "rb")`.

### 6.2 `manifest.json` (mỗi accession, mỗi file)
`accession`, `form_type`, `url`, `cik_folder_used`, `fetched_at_utc` (ISO-8601 Z), `http_status`, `bytes`, `sha256_original` (byte đúng như nhận, sau giải nén gzip), `sha256_committed`, `redactions` (danh sách khối đã thay, rỗng cho Apple), `user_agent_declared: true`, `expected_acceptance_raw`, `expected_information_time_utc`, `json_acceptanceDateTime` (giá trị discovery-only để test âm). Không có giá trị UA.

### 6.3 Assertions
- Toàn vẹn: sha256 của byte trên đĩa == `sha256_committed`.
- Mỗi accession: raw string trích từ `.hdr.sgml` == raw từ `-index-headers.html` (cả hai lần xuất hiện) == `expected_acceptance_raw`; UTC == `expected_information_time_utc`; `tzinfo` là UTC (offset 0), không naive.
- Giá trị kỳ vọng phải SUY RA từ raw đã ghi + `America/New_York`, KHÔNG từ JSON. Roadmap chỉ cho ngày của Form 4. Giá trị "tính sẵn, chờ fixture" (ASSUMED ngày):

  | Accession | raw (ASSUMED trừ Form 4) | UTC kỳ vọng |
  |---|---|---|
  | 8-K 0000320193-26-000018 | `20260730163028` | 2026-07-30T20:30:28Z (EDT, −4) |
  | Form 4 0001140361-26-040127 | `20261019183255` (ngày theo roadmap) | 2026-10-19T22:32:55Z (EDT) |
  | 10-Q 0000320193-26-000020 | `2026MMDD060102`, ngày chưa biết | 10:01:02Z nếu EDT |
  | 8-K 0000320193-26-000005 | `20260129163033` | 2026-01-29T21:30:33Z (EST, −5) |

- Test âm cho bẫy JSON: với cả 4, `parse(json_acceptanceDateTime as UTC)` != information time → khoá quy tắc "JSON chỉ để discovery".
- Session classifier (roadmap §7 Phase 0 mục 2, `docs/project-roadmap.md:327`): 06:01:02 → pre-market; 16:30:28 → after-close; 12:00-16:00 nằm trong phiên (cần 1 giá trị tổng hợp, không cần fixture). Kiểm Day 0 nếu plan đưa classifier vào Phase 0.
- Case âm (chuỗi tổng hợp, không cần fixture): 13 chữ số; có ký tự không phải số; tháng 13; 2026-02-30; `230000` (23:00 → ngoài giờ); 2026-11-01 01:30:00 (mơ hồ); 2026-03-08 02:30:00 (không tồn tại); thiếu tag; hai tag khác giá trị; header trống. 2026-03-08 và 2026-11-01 là Chủ nhật (tính từ 2026-01-01 là thứ Năm [PRIOR lịch]) — luật US: DST bắt đầu Chủ nhật thứ hai tháng 3, kết thúc Chủ nhật đầu tháng 11 [PRIOR]; test xác nhận qua chính zoneinfo.
- Không `pytest.skip` khi thiếu tzdata: fail to, thông điệp rõ.

### 6.4 Form 4 và PII trong repo public
- Hợp pháp: "Information presented on sec.gov is considered public information and may be copied or further distributed" (VERIFIED https://www.sec.gov/privacy). Có tiền lệ: edgartools commit nguyên header Form 4 có `<CONFORMED-NAME>` và `<MAIL-ADDRESS>` của reporting owner (OBSERVED `data/headers/form4.index-headers.html`).
- Nhưng test chỉ cần `ACCEPTANCE-DATETIME`, `ACCESSION-NUMBER`, `TYPE`, `FILING-DATE`. Nguyên tắc tối thiểu dữ liệu + repo public → khuyến nghị giảm: thay deterministically nội dung trong `<REPORTING-OWNER>` (`CONFORMED-NAME`, `MAIL-ADDRESS`/`BUSINESS-ADDRESS`, `PHONE`) bằng `REDACTED`, ở CẢ hai lần xuất hiện của `-index-headers.html` (comment + `<PRE>`). Giữ CIK owner (định danh công khai, hữu ích để tái ghi). Manifest giữ cả `sha256_original` và `sha256_committed` để lần ghi lại vẫn kiểm được bản gốc.
- Apple 8-K/10-Q: chỉ có địa chỉ doanh nghiệp → commit nguyên byte (redactions rỗng).

---

## Ma trận trade-off

| Lựa chọn | Đúng nguồn | Byte-stable CI/Win | PII | Công sức | Rủi ro |
|---|---|---|---|---|---|
| A. `.hdr.sgml` + `-index-headers.html`, cả hai làm fixture, kiểm chéo | Cao (một file SEC tài liệu hoá, một file có tag chắc chắn) | Có, nếu `-text` | Giảm được | 8 request, ~1 ngày | `.hdr.sgml` có thể thiếu tag → giữ HTML làm nguồn chính khi đó |
| B. Chỉ `-index-headers.html` | Tag chắc chắn có (OBSERVED qua edgartools) | Có, nếu `-text` | Phải giảm 2 chỗ | Thấp | URL không được SEC tài liệu hoá |
| C. Chỉ `.hdr.sgml` | SEC tài liệu hoá | Có | Một chỗ | Thấp | Có thể không có `ACCEPTANCE-DATETIME` (ASSUMED) → test không có gì để kiểm |
| D. Dùng edgartools `acceptance_datetime` | Trung bình | — | — | Thấp | Trả naive datetime (OBSERVED) → sai giờ nếu ai đó coi là UTC; thêm dependency |

Xếp hạng: A > B > C > D.

## Rủi ro chấp nhận
- edgartools: thư viện cộng đồng, cập nhật thường xuyên (CHANGELOG có sửa parser header, OBSERVED), không phải nguồn chính thức → chỉ dùng làm bằng chứng phụ, không làm dependency cho test.
- SEC có thể đổi trang 403/ngưỡng; script ghi phải fail-closed.

## Giới hạn của research này
- Không tải được 4 header (403 với UA không liên hệ; dừng theo chỉ dẫn) → ngày 3/4 accession, layout `.hdr.sgml` hiện đại, CIK folder Form 4 vẫn ASSUMED.
- Không tìm được bản Filer Manual Vol II hiện hành; dùng eCFR + 2 trang sec.gov cho giờ ET (đủ 3 nguồn cho "6:00-22:00 ET").
- Định dạng 14 chữ số chỉ có 1 nguồn độc lập (edgartools) + 1 tương tự (PDS control block).

## Kết luận (xếp hạng)
1. **Parser spec (khoá trước tiên).** Information time = giá trị sau `<ACCEPTANCE-DATETIME>`, regex `^\d{14}$`, `strptime("%Y%m%d%H%M%S")`, gắn `ZoneInfo("America/New_York")` (DST-aware; không bao giờ −05:00 cố định dù PDS ghi "EST"), kiểm PEP 495 fold/gap → raise, kiểm 06:00-22:00 ET Thứ Hai-Sáu → raise lỗi có tên (khung là tham số), rồi `.astimezone(UTC)`; trả cả raw lẫn UTC. Parser riêng trong `src/` (~20 dòng), không dùng `edgartools.acceptance_datetime` (naive).
2. **tzdata là runtime dependency.** Thêm `tzdata` vào `[project].dependencies` (đề xuất không marker) + test khẳng định zone load được. Đây là thay đổi `pyproject.toml` → khai cạnh liên plan `{from: 261021-1314-phase0b-foundations/<phase>, to: phase-4-edgar-golden-test}`.
3. **`.gitattributes`: `tests/fixtures/edgar_headers/** -text`** trong cùng commit với fixture, nếu không sha256 lệch giữa Windows (autocrlf) và CI.
4. **Thủ tục ghi fixture (bước owner chặn).** `scripts/spike/record_edgar_headers.py`, UA từ `EDGAR_IDENTITY` trong `.env` (đã có placeholder ở `.env.example:5`), fail nếu thiếu; ≤5 rps; 8 file; fail-closed khi 403/"Undeclared Automated Tool"/thiếu tag/accession lệch; thử CIK theo [issuer, owner, agent] và ghi `cik_folder_used`; manifest không chứa UA.
5. **Fixture A (cả hai loại file) + kiểm chéo raw giữa chúng**; nếu `.hdr.sgml` không có tag thì ghi OBSERVED vào AGENTS.md và dùng `-index-headers.html` làm nguồn chính.
6. **Giá trị kỳ vọng suy từ raw đã ghi**, không từ JSON; thêm test âm khẳng định JSON-as-UTC ≠ information time cho cả 4.
7. **Form 4: giảm PII** khối `<REPORTING-OWNER>` (tên, địa chỉ, phone) ở mọi lần xuất hiện, giữ CIK, ghi `sha256_original` + `sha256_committed`. Hợp pháp vẫn được commit nguyên (sec.gov/privacy), nhưng tối thiểu hoá rẻ và đúng §9.1.
8. **Case âm tổng hợp** (13 chữ số, tháng 13, 30/02, 23:00, 2026-11-01 01:30, 2026-03-08 02:30, thiếu tag, tag trùng khác giá trị) với thứ tự lỗi cố định.
9. Ghi 2 dòng Verified Facts mới vào AGENTS.md khi cook: (a) OBSERVED 2026-10-22: UA không liên hệ → 403 "Undeclared Automated Tool"; (b) kết quả ghi fixture (ngày thật của 4 accession, CIK folder, `.hdr.sgml` có tag hay không). Đụng AGENTS.md → khai cạnh liên plan.

## Câu hỏi mở
1. **Ngày + raw thật của 4 accession, `.hdr.sgml` có tag không.** Probe (owner, sau khi đặt `EDGAR_IDENTITY` trong `.env`): `python scripts/spike/record_edgar_headers.py --out tests/fixtures/edgar_headers` — tương đương tay: `curl -sS --compressed -A "$EDGAR_IDENTITY" -o x.hdr.sgml https://www.sec.gov/Archives/edgar/data/320193/000032019326000018/0000320193-26-000018.hdr.sgml` rồi `grep -c "<ACCEPTANCE-DATETIME>" x.hdr.sgml`; lặp cho `-20`, `-05` và `-index-headers.html`, nghỉ ≥0.2 s.
2. **Issuer CIK và reporting-owner CIK của Form 4 0001140361-26-040127.** Probe: `curl -sS --compressed -A "$EDGAR_IDENTITY" "https://www.sec.gov/Archives/edgar/data/1140361/000114036126040127/0001140361-26-040127-index-headers.html" -w "%{http_code}\n"`; nếu 404, lấy issuer CIK từ EDGAR company search (UA khai báo) rồi thử `.../data/<issuerCIK>/000114036126040127/...`.
3. **Thư mục CIK agent có phục vụ filing không** (giả thuyết: 404). Cùng probe ở câu 2; ghi `http_code` vào AGENTS.md (OBSERVED).
4. **Ranh giới giờ:** có acceptance thật nào ngoài 06:00-22:00 ET cho 8-K/10-Q/Form 4 không? Probe Phase 1 (không phải Phase 0): đếm acceptance ngoài khung trên header của một mẫu từ nightly `submissions.zip`; quyết định raise hay cờ `pit_quality`.
5. **Owner quyết:** giảm PII Form 4 (khuyến nghị) hay commit nguyên byte; `tzdata` có marker `win32` hay không.
6. **Line endings thật của file SEC** (LF hay CRLF) — chỉ ghi fixture mới biết; `-text` làm câu này vô hại nhưng sha256 phải tính trên byte nhận được.

## Nguồn
- https://www.sec.gov/os/accessing-edgar-data (VERIFIED 2026-10-22)
- https://www.sec.gov/os/webmaster-faq (VERIFIED 2026-10-22)
- https://www.sec.gov/privacy (VERIFIED 2026-10-22)
- https://sec.gov/node/956846 (VERIFIED 2026-10-22)
- https://www.ecfr.gov/current/title-17/chapter-II/part-232/subpart-A/section-232.13 (VERIFIED 2026-10-22)
- https://www.sec.gov/files/info/edgar/specifications/pds_dissemination_spec.pdf (VERIFIED 2026-10-22, đọc qua pdftotext)
- https://docs.python.org/3/library/zoneinfo.html (VERIFIED 2026-10-22)
- https://peps.python.org/pep-0495/ (VERIFIED 2026-10-22)
- https://github.com/dgunning/edgartools (main): `edgar/sgml/sgml_header.py`, `edgar/sgml/sgml_parser.py`, `edgar/settings.py`, `data/headers/*.html` (OBSERVED 2026-10-22 qua `gh search code` và raw.githubusercontent.com)
- Probe 403: §4 (OBSERVED 2026-10-22)
