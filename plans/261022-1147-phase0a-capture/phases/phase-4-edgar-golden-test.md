---
phase: 4
title: "Edgar Golden Test"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 4 — Edgar Golden Test

## Overview
Ghi fixture header EDGAR thật của bốn accession (roadmap §2.1, `docs/project-roadmap.md:48-53`) bằng một script spike tuân fair access, rồi viết
`tests/test_edgar_time.py`: golden test khẳng định `information_time` = `<ACCEPTANCE-DATETIME>` đọc theo America/New_York, lưu UTC tz-aware cạnh
chuỗi gốc, và parser raise thay vì đoán (roadmap §6.4 mục 2, `docs/project-roadmap.md:327`, gồm cả phân loại phiên 06:00 / 12:00-16:00 / sau 16:00).
Thêm một module thuần nhỏ `connectors/edgar_acceptance.py` — thêm module, không viết lại connector (roadmap §5.2 dòng `connectors/us_alpaca_edgar.py`
là Phase 1; client Phase 1 sẽ import nó). Cook trong lúc chờ 5 phiên (D4). Phụ thuộc Phase 3 (thứ tự chuỗi; dùng chung AGENTS.md/docs) và cạnh liên
plan `261021-1314-phase0b-foundations/P5`. Không chạm `capture/` (luật đóng băng).

## Requirements
**R4.1 BLOCKED (owner) OB6 — `EDGAR_IDENTITY` thật trong `.env`.** Dạng "Tên liên hệ email@domain" (SEC: "Sample Company Name AdminContact@<sample
company domain>.com", VERIFIED https://www.sec.gov/os/accessing-edgar-data). Không bao giờ in, log, lưu vào manifest hay commit. UA không có liên hệ
nhận HTTP 403 "Undeclared Automated Tool" (OBSERVED 2026-10-22, research `edgar-header.md` §4). Phát hiện xong:
`.venv/Scripts/python scripts/spike/record_edgar_headers.py --check-env` exit 0 và in đúng `ok` (từ chối khi thiếu `@`, khi chứa `yourdomain.com` —
placeholder ở `.env.example:5` — hoặc khi rỗng; lỗi chỉ nêu TÊN biến).

**R4.2 `scripts/spike/record_edgar_headers.py` (mới; `.venv`; dùng `_spikelib`).**
- Accession mặc định: 8-K `0000320193-26-000018`, Form 4 `0001140361-26-040127`, 10-Q `0000320193-26-000020`, 8-K kỳ EST `0000320193-26-000005`.
- Hai file mỗi accession: `https://www.sec.gov/Archives/edgar/data/<CIK>/<acc-không-gạch>/<acc>.hdr.sgml` (SEC tài liệu hoá, VERIFIED trang trên) và
  `…/<acc>-index-headers.html` (có header HAI lần — comment thô và `&lt;…&gt;` trong `<PRE>`, OBSERVED research §1.2 qua bản ghi của edgartools).
- CIK: Apple `320193` cho ba accession Apple (`[PRIOR]` + prefix accession). Form 4: thử thư mục CIK agent `1140361` trước; 404 → dừng và đòi
  `--cik <issuer CIK>`; cook tìm issuer CIK qua EDGAR full-text search (`https://efts.sec.gov/LATEST/search-index?q="0001140361-26-040127"` — endpoint
  `[ASSUMED]`) hoặc hỏi owner; ghi `cik_folder_used` và status 404 của thư mục agent (nếu có) làm OBSERVED.
- Header: `User-Agent` = giá trị `EDGAR_IDENTITY` từ `.env` (qua `_spikelib.load_env_file`), `Accept-Encoding: gzip, deflate` (giải nén bằng `gzip`);
  ≤ 5 req/s (roadmap §4, `docs/project-roadmap.md:164`) — nghỉ 0.5 s giữa hai request; tổng ~8-10 request.
- Fail-closed TRƯỚC khi ghi bất kỳ file nào vào `tests/`: status 200; body không chứa "Undeclared Automated Tool"; có `<ACCEPTANCE-DATETIME>` với đúng
  14 chữ số (với `.html`: bản thô và bản escape bằng nhau); accession trong body = accession yêu cầu. Byte gốc lưu trước vào
  `$FINSEC_DATA_ROOT/spike/edgar/<UTC-date>/`. `.hdr.sgml` hiện đại có thể KHÔNG có tag (edgartools ghi nhận hai dialect, research §1.2) → ghi
  `hdr_has_acceptance: false` vào manifest, giữ file làm bằng chứng, `.html` là nguồn chính; KHÔNG tính là lỗi.
- Không ghi đè fixture đã có (không có cờ ép nào cook được dùng).
- **Giảm PII Form 4 (D8):** trong khối `<REPORTING-OWNER>…</REPORTING-OWNER>` (dialect tag), bản escape `&lt;REPORTING-OWNER&gt;` trong `<PRE>`, và dialect
  "space" (`REPORTING-OWNER:` với `COMPANY CONFORMED NAME:`, `BUSINESS ADDRESS:`, `MAIL ADDRESS:`, `BUSINESS PHONE:`) — thay giá trị tên, `STREET1/2`,
  `CITY`, `STATE`, `ZIP`, `PHONE` bằng `REDACTED`, tất định; GIỮ nguyên CIK và `<ACCEPTANCE-DATETIME>`. Apple: không giảm (chỉ địa chỉ doanh nghiệp).
- `tests/fixtures/edgar_headers/manifest.json`: mỗi file — `accession`, `form_type`, `url`, `cik_folder_used`, `fetched_at_utc`, `http_status`, `bytes`,
  `sha256_original`, `sha256_committed`, `redactions` (tên trường + số lần), `user_agent_declared: true` (KHÔNG có giá trị UA), `hdr_has_acceptance`,
  `acceptance_raw` (chuỗi 14 số trích được). Manifest KHÔNG chứa giờ UTC kỳ vọng — giá trị golden do test tự ghi thành literal (R4.4), tránh vòng
  lặp "kỳ vọng tính bằng chính code đang test".

**R4.3 `src/finsec_alpharadar/connectors/edgar_acceptance.py` (mới, thuần, stdlib + tzdata).**
- Lỗi: `EdgarAcceptanceError(ValueError)` và con `EdgarAcceptanceMissing`, `EdgarAcceptanceConflict`, `EdgarAcceptanceFormatError`,
  `EdgarAcceptanceAmbiguous`, `EdgarAcceptanceNonexistent`, `EdgarAcceptanceOutsideHours`; thông điệp nêu chuỗi gốc và luật vi phạm.
- `extract_acceptance_raw(header_text: str) -> str`: gom mọi lần xuất hiện `<ACCEPTANCE-DATETIME>` và `&lt;ACCEPTANCE-DATETIME&gt;`, giá trị = phần tới
  hết dòng, strip; không có → `Missing`; giá trị khác nhau → `Conflict`.
- `parse_acceptance_datetime(raw, *, hours=(time(6, 0), time(22, 0)), weekdays_only=True) -> datetime` (UTC tz-aware), thứ tự lỗi CỐ ĐỊNH để mỗi test
  assert đúng một loại (research §5): (1) `^\d{14}$` → `FormatError`; (2) `strptime("%Y%m%d%H%M%S")` → `FormatError` (lịch: tháng 13, 30/02);
  (3) gắn `ZoneInfo("America/New_York")` (17 CFR 232.13: "Eastern Standard Time or Eastern Daylight Saving Time, whichever is currently in effect" —
  VERIFIED https://www.ecfr.gov/current/title-17/chapter-II/part-232/subpart-A/section-232.13; không bao giờ −05:00 cố định dù PDS ghi "EST");
  kiểm PEP 495: offset của `fold=0` khác `fold=1` → không tồn tại nếu khứ hồi UTC→NY không về đúng giờ tường, ngược lại mơ hồ
  (VERIFIED https://peps.python.org/pep-0495/; zoneinfo tự nó không raise); (4) ngoài [06:00, 22:00] ET hoặc thứ 7/CN → `OutsideHours` (giờ EDGAR,
  VERIFIED research §2; khung là tham số, ngày lễ liên bang không kiểm ở Phase 0); (5) `.astimezone(timezone.utc)`.
- `us_session_bucket(info_time_utc) -> str` ∈ {`pre_open`, `regular`, `after_close`, `non_session_day`}: giờ NY < 09:30 → `pre_open`;
  09:30 ≤ t < 16:00 → `regular`; ≥ 16:00 → `after_close` (16:00:00 đúng là `after_close`: day 0 = phiên đầu tiên có close SAU `information_time`,
  roadmap §2.1, `docs/project-roadmap.md:63-65`); thứ 7/CN → `non_session_day`. Ngày lễ: Phase 1 `calendar/`. Tên module chỉ nói "acceptance"
  trong khi hàm này là logic phiên US: chấp nhận cho Phase 0 vì roadmap §6.4 mục 2 gộp nó vào golden test EDGAR (PL10); docstring của hàm ghi
  "moves to `calendar/` in Phase 1" (consistency sweep 2026-10-24, name-honesty).
- `ZoneInfo` tạo lười; thiếu tzdata → lỗi nêu "install tzdata" (tzdata là dependency lõi từ Phase 2).
- `connectors/__init__.py` re-export các tên public vào `__all__` (`docs/code-standards.md:16`).

**R4.4 `tests/test_edgar_time.py` (mới; đọc fixture bằng `"rb"`; không mạng, không đồng hồ).** Golden: giá trị kỳ vọng là literal viết từ chuỗi gốc
đã ghi + luật DST, kèm comment phép tính; KHÔNG suy từ JSON. Bảng dưới là kỳ vọng tạm — chỉ ngày Form 4 có trong roadmap; giờ ET của cả bốn có trong
roadmap (`docs/project-roadmap.md:50-53`, OBSERVED 2026-10-20), còn NGÀY của ba accession Apple là `[ASSUMED]` (research `edgar-header.md` §6.3) và
cook thay bằng chuỗi gốc thật từ manifest:

| Accession | Chuỗi gốc kỳ vọng | UTC kỳ vọng | Nhãn |
|---|---|---|---|
| 8-K `0000320193-26-000018` | `20260730163028` | `2026-07-30T20:30:28Z` (EDT −4) | giờ OBSERVED, ngày `[ASSUMED]` |
| Form 4 `0001140361-26-040127` | `20261019183255` | `2026-10-19T22:32:55Z` (EDT) | ngày + giờ theo roadmap (OBSERVED 2026-10-20) |
| 10-Q `0000320193-26-000020` | `2026MMDD060102` | `…T10:01:02Z` nếu EDT | ngày chưa biết |
| 8-K kỳ EST `0000320193-26-000005` | `20260129163033` | `2026-01-29T21:30:33Z` (EST −5) | giờ OBSERVED, ngày `[ASSUMED]` |

Tests: xem "Tests Before".

**R4.5 `.gitattributes`** — thêm sau dòng 4 (`harness/** -text`), tiếng Anh (bỏ thụt khi chép):
  ```
  # Recorded SEC headers are compared byte-for-byte with tests/fixtures/edgar_headers/manifest.json — never convert their line endings.
  tests/fixtures/edgar_headers/** -text
  ```
Cùng commit với fixture: `core.autocrlf=true` (OBSERVED `git config --get core.autocrlf` → `true`, 2026-10-22) sẽ đổi LF→CRLF lúc checkout Windows và
sha256 lệch với CI Ubuntu (research §6.1). Đuôi `.sgml`/`.html` không bị ignore (OBSERVED 2026-10-22: `git check-ignore` exit 1 cho
`tests/fixtures/edgar_headers/0000320193-26-000018.hdr.sgml` và `manifest.json`).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Create | `scripts/spike/record_edgar_headers.py` |
| Create | `src/finsec_alpharadar/connectors/edgar_acceptance.py` |
| Create | `tests/test_edgar_time.py` |
| Create | `tests/fixtures/edgar_headers/manifest.json` |
| Create | `tests/fixtures/edgar_headers/0000320193-26-000018-index-headers.html` |
| Create | `tests/fixtures/edgar_headers/0000320193-26-000018.hdr.sgml` |
| Create | `tests/fixtures/edgar_headers/0001140361-26-040127-index-headers.html` |
| Create | `tests/fixtures/edgar_headers/0001140361-26-040127.hdr.sgml` |
| Create | `tests/fixtures/edgar_headers/0000320193-26-000020-index-headers.html` |
| Create | `tests/fixtures/edgar_headers/0000320193-26-000020.hdr.sgml` |
| Create | `tests/fixtures/edgar_headers/0000320193-26-000005-index-headers.html` |
| Create | `tests/fixtures/edgar_headers/0000320193-26-000005.hdr.sgml` |
| Modify | `src/finsec_alpharadar/connectors/__init__.py` |
| Modify | `.gitattributes` |
| Modify | `docs/phase0-spike-report.md` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261022-1147-phase0a-capture/research/edgar-header.md` |
| run | `scripts/spike/record_edgar_headers.py` (mạng; ngoài pytest) |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Ghi chú parser: `plan-graph.yaml` khai thư mục `tests/fixtures/edgar_headers/` thay vì từng file, vì đuôi `sgml` không thuộc `_FILE_EXTS`
(`harness/scripts/plan_graph.py:597-601`) — khai từng `.hdr.sgml` sẽ sinh bốn dòng "declares X; phase file never names it". Một mục thư mục được
`_names` khớp với mọi đường dẫn bên dưới (`plan_graph.py:687`; các hàng `.html`/`.json` ở bảng trên), và chiều ngược bỏ qua thư mục
(`plan_graph.py:719`).

Challenge luật ">8 tệp": 19 tệp = 3 code/test mới + 9 fixture ghi máy (một manifest + 8 header) + 1 re-export + `.gitattributes` + 5 docs (báo cáo spike,
AGENTS.md, ba docs). Công sức viết tay là 4 file code; giữ.

## Implementation Steps
1. **BLOCKED (owner) OB6** — R4.1. Chưa `--check-env` → DỪNG phase.
2. **Tests-before (RED)** — viết `tests/test_edgar_time.py` phần không cần fixture (âm tính tổng hợp, phân loại phiên) + `edgar_acceptance.py` dạng
   shim chỉ có chữ ký và lỗi (để RED là RED theo hành vi, không phải `ImportError`); chạy `.venv/Scripts/python -m pytest -q tests/test_edgar_time.py` → RED.
3. **Ghi fixture:** viết `record_edgar_headers.py`; chạy (mạng thật, ngoài pytest); nếu Form 4 404 dưới CIK agent → tìm issuer CIK (R4.2) và chạy lại với
   `--cik`. Kiểm: 8 file + manifest dưới `tests/fixtures/edgar_headers/`, Form 4 đã giảm PII, `grep -c "REDACTED"` > 0.
4. Thêm dòng `.gitattributes` (R4.5) TRƯỚC `git add` fixture; `git add --renormalize` không cần (file mới).
5. Viết phần golden của test từ manifest (thay bảng R4.4 bằng chuỗi gốc thật) → RED cho tới khi parser xong.
6. **Implement** `edgar_acceptance.py` đầy đủ → xanh; re-export trong `connectors/__init__.py`.
7. **Delta docs** → **Regression gate** → một commit, ví dụ `test: add the EDGAR acceptance-time golden test` + trailer; push; CI xanh trên Ubuntu
   (byte fixture khớp manifest ở cả hai OS nhờ `-text`).

### Delta docs (cùng commit; tiếng Anh; có ngày)
- **AGENTS.md**
  - "Decisions": D8 — `- <date> — Form 4 fixtures (owner, D8): the reporting owner's name, address and phone are replaced with REDACTED; the CIK and <ACCEPTANCE-DATETIME> stay untouched; the manifest keeps sha256_original and sha256_committed.`
  - "Verified Facts" dòng 101-103 (EDGAR time): câu "the Phase 0 golden test settles the header side" → kết quả OBSERVED <date>: bốn chuỗi gốc + UTC, `.hdr.sgml`
    có/không có tag, thư mục CIK agent Form 4 phục vụ hay 404, độ lệch JSON−header quan sát được (+4 h EDT / +5 h EST nếu fixture xác nhận — DERIVED);
    thêm "a User-Agent without a contact gets HTTP 403 'Undeclared Automated Tool' (OBSERVED 2026-10-22)".
  - "Package Layout" `connectors/`: thêm `edgar_acceptance.py` (header acceptance time → UTC, raise thay vì đoán; phân loại phiên US).
  - Số test do gate in ra ở đủ năm chỗ (`AGENTS.md:51`, `AGENTS.md:169`, `docs/code-standards.md:50`, `:56`, `docs/system-architecture.md:13`; red-team r1
    LT12); ghi "includes the EDGAR golden test" (tiêu chí exit E1).
  - **Từ Phase 3 (red-team r1 LT4, phán quyết của main):** dòng Decisions MỚI `- <date> — capture window started <date> at <sha> (capture_version <v>).`
    (lấy từ bước 9 của P3; chưa mở cửa sổ khi cook tới đây → bỏ dòng này, ghi chú trong `verification-P4.json`, và commit phase ĐẦU TIÊN sau khi
    cửa sổ mở — P5, P6, hoặc P7 trước khi đếm (R7.1) — thêm nó; mỗi phase đó kiểm `grep -n "capture window started" AGENTS.md` ở bước docs-sync);
    "Current Status": "VN capture running on the laptop since <date> (5-session exit window open)"; "Verified Facts": **Capture laptop (OBSERVED <date>)** —
    sleep states (S0/S3), w32time sau cấu hình + offset stripchart, active hours, Autologon chạy, sync khi khoá màn hình, quota cloud, cổng logon type R3.3
    đạt, Q1/Q3/Q5/Q6/Q8 của research `capture-host-windows.md`.
  - `docs/system-architecture.md` và `docs/project-roadmap.md` §7 0b.6: "Deployed <date>; exit window started <date>" (cùng nguồn trên; phần "exit window
    started" theo cùng điều kiện với dòng cửa sổ — chưa mở thì phase đầu tiên sau khi cửa sổ mở ghi).
- **docs/system-architecture.md**: bảng as-built thêm hàng `connectors/edgar_acceptance.py`; External dependencies SEC EDGAR (dòng 104-107): "the Phase 0
  golden test settles the header side" → đã settle <date>, trỏ test.
- **docs/code-standards.md**: Timestamps dòng 24 (EDGAR) → trỏ `parse_acceptance_datetime` và golden test; Testing dòng 57 ("Planned … `tests/test_edgar_time.py`")
  → có rồi; quy ước fixture: "recorded fixtures are byte-exact (`.gitattributes` `-text`), carry a manifest with sha256 and fetch date, and personal data
  in them is redacted".
- **docs/project-roadmap.md**: §7 0a gạch EDGAR (dòng 358) "Done <date>"; §6.4 mục 2 (dòng 327) "Done <date> (holidays in Phase 1)"; §5.2 hàng
  `tests/test_end_to_end.py` (dòng 245) "test_edgar_time … done <date>".
- **docs/phase0-spike-report.md** mục 4: bảng bốn accession (chuỗi gốc, UTC, file có tag, CIK folder), 403 không liên hệ.

### Tests Before
`tests/test_edgar_time.py`:
- [ ] Âm tính tổng hợp (RED sau shim): `test_thirteen_digits_is_a_format_error`, `test_non_digit_is_a_format_error`, `test_month_13_and_feb_30_are_format_errors`,
      `test_empty_is_a_format_error`, `test_2300_is_outside_hours`, `test_saturday_noon_is_outside_hours`, `test_2026_11_01_0130_is_ambiguous`
      (cũng ngoài giờ — thứ tự lỗi cố định nên phải là Ambiguous), `test_2026_03_08_0230_is_nonexistent`, `test_missing_tag_raises_missing`,
      `test_two_different_tags_raise_conflict`, `test_raw_and_escaped_copies_must_agree`.
- [ ] Phân loại phiên (RED): `test_session_bucket_0601_is_pre_open`, `test_session_bucket_noon_is_regular`, `test_session_bucket_1600_exactly_is_after_close`,
      `test_session_bucket_1630_is_after_close`, `test_session_bucket_weekend_is_non_session_day`.
- [ ] Golden (RED tới bước 6): `test_fixture_bytes_match_the_manifest` (sha256 file trên đĩa == `sha256_committed`),
      `test_golden_information_time[<4 accession>]` (chuỗi gốc từ `.html` == literal; UTC == literal; `tzinfo` offset 0),
      `test_hdr_sgml_agrees_or_is_recorded_as_tagless` (có tag → bằng chuỗi `.html`; không tag → manifest ghi `hdr_has_acceptance: false` và `extract` raise
      `Missing`), `test_one_edt_and_one_est_filing_are_covered`.
- [ ] Bẫy JSON (RED): `test_submissions_json_value_read_as_utc_is_not_the_information_time[<4>]` — giá trị JSON của roadmap (`docs/project-roadmap.md:50-53`,
      ví dụ `2026-07-31T00:30:28Z`) đọc như UTC ≠ information time.
- [ ] PII (RED): `test_form4_fixture_is_redacted` — không giá trị tên/địa chỉ/phone nào của reporting owner ngoài `REDACTED`; CIK owner còn; manifest
      `redactions` không rỗng và `sha256_original != sha256_committed`.

### Implement
- Bước 3-6.

### Tests After
- [ ] Toàn bộ `tests/test_edgar_time.py` xanh; `tests/test_agents_md_paths.py` xanh; `tests/test_no_order_execution.py` xanh với script mới.
- [ ] `git check-attr text tests/fixtures/edgar_headers/manifest.json` → `text: unset`.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` MUST pass, 0 skipped; số test ≥ 15 và gồm `tests/test_edgar_time.py` (tiêu chí exit E1).
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- Hai lệnh `scaffold_standards.py --check` exit 0.
- Sau push: CI `success` trên 3.11 + 3.13 — chân Ubuntu chứng minh byte fixture không đổi qua checkout (`test_fixture_bytes_match_the_manifest`).
- `git grep -n -F "$EDGAR_IDENTITY"` → không hit (cook chạy trong shell đã nạp `.env`; lệnh không in giá trị khi không hit).

## Success Criteria
- [ ] [test] Mọi test RED trước (sau shim), xanh sau; CI xanh ở cả Windows (local) và Ubuntu.
- [ ] [invariant] Không commit nào chứa `EDGAR_IDENTITY` (grep ở gate) hay giá trị UA (manifest chỉ có `user_agent_declared: true`).
- [ ] [invariant] Fixture Form 4 đã giảm PII (test), CIK và acceptance nguyên vẹn.
- [ ] [test] Thứ tự lỗi cố định: mỗi chuỗi âm tính raise đúng MỘT lớp lỗi đã nêu.
- [ ] [invariant] AGENTS.md không còn câu "the Phase 0 golden test settles the header side" ở thì tương lai: `grep -n "golden test settles" AGENTS.md
      docs/system-architecture.md docs/code-standards.md` → chỉ còn câu đã sửa có ngày; `tests/test_agents_md_paths.py` xanh.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| `.hdr.sgml` hiện đại không có tag | Trung bình × Thấp | `.html` là nguồn chính; ghi OBSERVED; test chấp nhận và ghi lại. |
| Thư mục CIK agent của Form 4 trả 404 | Cao × Thấp | Dừng và đòi `--cik`; tìm issuer CIK; ghi 404 làm OBSERVED. |
| CRLF làm lệch sha giữa Windows và CI | Cao (nếu quên) × Trung bình | `.gitattributes` `-text` cùng commit; test sha; chân CI Ubuntu. |
| SEC chặn (403/429) | Thấp × Thấp | UA có liên hệ, 2 req/s, ~10 request; fail-closed không ghi fixture rác. |
| Giờ ngoài khung 06:00-22:00 ET là thật (ranh giới) | Thấp × Trung bình | Khung là tham số; Phase 1 quyết raise hay cờ `pit_quality` (research §2). |
| Giá trị kỳ vọng vô tình tính bằng chính parser | Thấp × Cao | Literal viết tay + comment phép tính; manifest không chứa UTC kỳ vọng. |
