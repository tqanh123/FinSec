---
phase: 4
title: "Forensics Guards"
plan: 261021-1314-phase0b-foundations
created: 2026-10-21
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 4 — Forensics Guards (kéo sớm từ roadmap Phase 2, D1)

## Overview
`ForensicAccountingEngine.evaluate` không còn chia cho 0 hay kẹp mẫu số âm thầm: mỗi điểm (Altman Z, Beneish M, Piotroski F) là `None` kèm lý
do khi tỷ số không xác định; điểm chỉ được tính cho `IssuerType.NON_FINANCIAL` (ngân hàng, bảo hiểm, công ty chứng khoán → không áp dụng; loại
chưa biết → từ chối, fail-closed). Đây là phần guard + sector gate của dòng roadmap §5.2 `forensics/xbrl_bctc_auditor.py` (Phase 2,
`docs/project-roadmap.md:238`) được **kéo sớm** theo D1; mọi sửa công thức vẫn ở roadmap Phase 2. Phụ thuộc Phase 3 (thứ tự chuỗi; dùng chung
`domain/models.py`, `domain/__init__.py`, `tests/test_end_to_end.py` và docs với P2/P3).

## Requirements
**R4.1 `src/finsec_alpharadar/domain/models.py`**
- `class IssuerType(str, Enum)` với `NON_FINANCIAL`, `BANK`, `INSURER`, `SECURITIES_FIRM`, `UNKNOWN` (giá trị = tên), theo quy ước Enum
  (`docs/code-standards.md:14`).
- `FinancialStatementSnapshot` (dòng 62-95) → `@dataclass(frozen=True)`. Bằng chứng an toàn: không có gán thuộc tính trên snapshot
  (`grep -n "fs\.[a-z_]* *=[^=]" src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py` → exit 1; `grep -rn "dataclasses.replace\|setattr"` trên
  `src/` và `tests/` → không hit — OBSERVED 2026-10-21); codemap chỉ thấy một reader (`forensics/xbrl_bctc_auditor.py:25`) và một nơi dựng
  (`tests/test_end_to_end.py:106`).
- Thêm `issuer_type: IssuerType = IssuerType.UNKNOWN` là **field cuối** (sau `prev_shares_outstanding`, dòng 95).
- `market_cap: float` giữ bắt buộc; quy ước: `market_cap <= 0` (kể cả `0` dùng như "thiếu") → Altman `None` (không đổi schema sang `Optional`).

**R4.2 `src/finsec_alpharadar/domain/__init__.py`**: export `IssuerType` (import + `__all__`).

**R4.3 `src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py`**
- `ForensicAuditResult` (dòng 9-18) → `@dataclass(frozen=True)`: `symbol: str`, `period: str`, `altman_z_score: Optional[float]`,
  `altman_zone: Optional[str]`, `beneish_m_score: Optional[float]`, `is_manipulation_suspect: Optional[bool]`, `piotroski_f_score: Optional[int]`,
  `red_flags: List[str] = field(default_factory=list)`, `unavailable_reasons: Dict[str, str] = field(default_factory=dict)` (khoá `"altman"`,
  `"beneish"`, `"piotroski"`). Chỉ một nơi dựng (`xbrl_bctc_auditor.py:94`, keyword) và một consumer (`tests/test_end_to_end.py:120`).
- **Cổng chạy trước mọi phép tính** (dòng 25-26): `issuer_type in {BANK, INSURER, SECURITIES_FIRM}` → cả ba điểm `None`, mọi reason =
  `"not_applicable_financial_issuer"`; `issuer_type != IssuerType.NON_FINANCIAL` (UNKNOWN hoặc giá trị lạ bất kỳ) → cả ba `None`, reason =
  `"issuer_type_unknown"` (fail-closed như sàn lạ; research `forensics-guards.md` "Kết luận" 1).
- **Guard từng điểm** (độc lập: một điểm `None` không chặn điểm khác); công thức giữ nguyên, chỉ thêm guard và xoá **mọi** clamp
  `max(1.0, …)` / `max(1e-4, …)` (dòng 31, 45, 48, 52, 56, 77, 78 — baseline G5, OBSERVED):
  - **Trước mọi guard của một điểm: số hữu hạn** (red-team r1 RT2). Mọi field số của snapshot mà công thức của điểm đó đọc (cook liệt kê từ
    code: Altman dòng 27-33, Beneish dòng 42-70, Piotroski dòng 74-92) phải qua `math.isfinite`; field đầu tiên (theo thứ tự khai báo trong
    `FinancialStatementSnapshot`) không hữu hạn → điểm đó `None`, reason `"<field>_not_finite"`. Lý do: `nan <= 0` là `False`, nên guard
    `<= 0` không bắt được NaN — hôm nay `retained_earnings=NaN` hoặc `market_cap=NaN` → `DISTRESS` + red flag giả; `net_income=NaN` →
    `M=nan`, `F=6` (cơ sở 9) mà không báo gì (OBSERVED 2026-10-21, `redteam-r1/scripts/forensics_nan.py`). Các guard bên dưới viết dạng
    `not (x > 0)` / `not (x >= 0)` thay cho `x <= 0` / `x < 0`, để vẫn đúng nếu một giá trị suy ra (vd `gross_margin`) thành NaN.
  - Altman `None` khi `total_assets <= 0`, `total_liabilities <= 0`, hoặc `market_cap <= 0` (dòng 27-33).
  - Beneish `None` khi `revenue <= 0` hoặc `prev_revenue <= 0`; `prev_accounts_receivable <= 0`; gross margin hiện tại `<= 0` hoặc kỳ trước
    `<= 0`; `total_assets <= 0` hoặc `prev_total_assets <= 0`; `prev_aq <= 0` hoặc `curr_aq < 0` (`curr_aq < 0` nghĩa là CA + PPE > TA, dữ liệu
    sai); `depreciation + property_plant_equipment <= 0` (năm nay hoặc năm trước) hoặc `depreciation <= 0`; `prev_selling_general_admin <= 0`;
    đòn bẩy kỳ trước `<= 0` (dòng 42-70).
  - Piotroski `None` khi `total_assets <= 0` hoặc `prev_total_assets <= 0`; `current_liabilities <= 0` hoặc `prev_current_liabilities <= 0`;
    `revenue <= 0` hoặc `prev_revenue <= 0` (dòng 74-92).
  - Mã reason: `"<input>_non_positive"` cho guard `<= 0`, `"<input>_negative"` cho guard `< 0`; `<input>` là tên field của snapshot hoặc một
    trong các tên suy ra `gross_margin`, `prev_gross_margin`, `asset_quality`, `prev_asset_quality`, `dep_plus_ppe`, `prev_dep_plus_ppe`,
    `prev_leverage`; `"<field>_not_finite"` cho kiểm số hữu hạn. Khi nhiều guard cùng sai, báo guard **đầu tiên**: kiểm số hữu hạn luôn đứng
    trước, rồi tới các guard theo đúng thứ tự liệt kê ở trên.
- Điểm `None` → zone/cờ tương ứng `None` và **không sinh red flag**; red flag chỉ từ điểm đã tính (dòng 39-40, 71-72).
- **Lựa chọn có chủ đích:** `None` cho mọi tỷ số không xác định; quy tắc trung tính = 1 của chính Beneish cho AQI/DEPI/SGAI (VERIFIED [B1999]
  trong research §2) bị **hoãn** cùng các sửa công thức ở roadmap Phase 2 — vì nó cần cờ `imputed` và đổi M trên dữ liệu suy biến, tức là một
  quyết định mô hình, không phải guard; `None` là phương án trung thực nhất (research §4) và không bao giờ sinh cờ giả.

**R4.4 `tests/test_end_to_end.py` — sửa TRONG DÒNG, không thêm dòng** (để các trích dẫn `test:124-145`, `:146`, `:149-152`,
`tests/test_end_to_end.py:149,151`, `:94-95`, `:37` không dời):
- dòng 21 `    FinancialStatementSnapshot,` → `    FinancialStatementSnapshot, IssuerType,` (lệch nhẹ khỏi style một-tên-một-dòng; repo không có
  isort/ruff config — cách khác là thêm dòng import và chạy citation sweep cho ~16 trích dẫn);
- dòng 118 thêm `issuer_type=IssuerType.NON_FINANCIAL` vào cuối danh sách kwarg.
- Assert `audit.altman_zone == "SAFE" and audit.piotroski_f_score == 9` (dòng 120) **không đổi**. Bằng chứng (OBSERVED 2026-10-21, chạy engine
  hiện tại trên đúng fixture, script `fx_spike.py`): Z = 104.189 SAFE, M = −2.348, F = 9, `red_flags == []`; đầu vào của các clamp: TL = 22.500
  (> 1.0), curr_gm = 0.7610, prev_aq = 0.2333, curr_dep_rate = 0.0637, prev_lev = 0.2846 (đều > 1e-4), CL = 14.000, prev CL = 13.200 (> 1.0) →
  mọi clamp là no-op; mọi điều kiện guard đều sai (mọi mẫu số > 0, curr_aq = 0.2337 ≥ 0, prev_gm = 0.7469 > 0) → kết quả giữ nguyên (DERIVED).

**R4.5 `tests/test_forensics_guards.py` (mới)** — xem "Tests Before".

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | `src/finsec_alpharadar/domain/models.py` |
| Modify | `src/finsec_alpharadar/domain/__init__.py` |
| Modify | `src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py` |
| Modify | `tests/test_end_to_end.py` |
| Create | `tests/test_forensics_guards.py` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261021-1314-phase0b-foundations/research/forensics-guards.md` |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Challenge luật ">8 tệp": 9 tệp = 5 code/test + 4 docs (delta bắt buộc) → giữ.

## Implementation Steps
1. **Tests-before (RED).** Viết `tests/test_forensics_guards.py` với helper dựng snapshot cơ sở = fixture smoke + `issuer_type=NON_FINANCIAL`, và
   biến thể bằng `dataclasses.replace(base, **overrides)` (chạy được với frozen). Chạy
   `.venv/Scripts/python -m pytest -q tests/test_forensics_guards.py` → RED: `ImportError` cho `IssuerType`; sau khi thêm riêng enum + field,
   RED theo hành vi (lý do từng test dưới).
2. **Implement → xanh** (R4.1-R4.4).
3. **Grep gate**: G5 `grep -nE "max\((1e-4|1\.0)," src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py` → không output.
4. **Delta docs + citation sweep** (dưới).
5. **Regression gate** → commit duy nhất, ví dụ `fix: guard forensic scores and gate financial issuers` + trailer `Co-Authored-By:`.

### Delta docs (cùng commit; tiếng Anh; ghi ngày)
- **AGENTS.md**
  - "Decisions": dòng mới trên cùng:
    ```
    - 2026-10-21 — Forensics guards are pulled forward from roadmap Phase 2 (§5.2, `forensics/xbrl_bctc_auditor.py` row) into Phase 0b: an undefined ratio or a non-finite input returns None with a reason, and the three scores run only for `IssuerType.NON_FINANCIAL` (financial issuers get "not applicable"; an unknown issuer type is refused). Formula corrections stay in Phase 2.
    ```
  - "Current Status" dòng 37: "it has no zero-division guards and is not valid for banks, insurers or brokers" → "undefined ratios return None
    with a reason; scores run only for non-financial issuers (<date>); formula corrections are Phase 2".
  - "Package Layout" dòng 55 (`domain/models.py`): thêm `IssuerType`; dòng 58 (`forensics/…`): thêm "issuer gate, None-with-reason guards".
  - "Current Status": bullet **Tests** — cập nhật `<N>` và ngày OBSERVED theo số test thật sau phase này, và ghi kết quả CI của lần push trước (run id, OBSERVED — VL-1); cùng số đó ở
    `docs/code-standards.md` "Current state" (red-team r1 RT8).
- **docs/system-architecture.md**: bảng As-built dòng 27 (forensics: bỏ "No zero-division guards (e.g. line 28) and no sector gate"; ghi guard +
  gate fail-closed); Known gaps xoá dòng 135 (không có cổng loại công ty tài chính).
- **docs/code-standards.md**
  - dòng 13 "Dataclasses": bỏ `FinancialStatementSnapshot` (`domain/models.py:62`) và `ForensicAuditResult` (`forensics/xbrl_bctc_auditor.py:9`)
    khỏi danh sách kiểu kết quả mutable (cả hai đã frozen); cập nhật số dòng.
  - dòng 18: trích `forensics/xbrl_bctc_auditor.py:97-99` (làm tròn) → số dòng mới.
  - dòng 70-71 "Current state": xoá danh sách `ZeroDivisionError` và "silent clamps" (đã sửa).
  - dòng 77 "Forensics": bỏ câu Debt (không có sector gate/field) → mô tả `IssuerType` + cổng fail-closed.
  - dòng 83: "Planned (roadmap §5.2) for forensics: return `None` with a reason instead of a score" → "Done for the guards (<date>); formula
    corrections remain planned (roadmap Phase 2)".
- **docs/project-roadmap.md**: §5.2 dòng 238 (`forensics/xbrl_bctc_auditor.py`), cột Note, thêm ở đầu: "Guards and the sector gate done early
  (<date>, Phase 0b); the formula items below remain Phase 2."

### Citation sweep (kiểm kê OBSERVED 2026-10-21)
`grep -nE "models\.py:[0-9]|xbrl_bctc_auditor\.py:[0-9]|aud:[0-9]|test_end_to_end\.py:[0-9]|test:[0-9]" AGENTS.md docs/system-architecture.md docs/code-standards.md docs/project-roadmap.md`
- `domain/models.py` (dời thêm vì `IssuerType`): code-standards:13 (`:19,48`, `:59`, `:62`), :14 (`:8`, `:13`), :18, :22 (`:23`); system-architecture:179 (`:13-16`).
- `forensics/xbrl_bctc_auditor.py`: code-standards:13 (`:9`), :18 (`:97-99`), :70 (danh sách dòng — xoá), :77 (`:25`); system-architecture:27 ("line 28").
- `tests/test_end_to_end.py`: không dời nếu tôn trọng R4.4.

### Tests Before
- [x] `test_financial_issuers_get_no_scores` (param `BANK`, `INSURER`, `SECURITIES_FIRM`) → cả ba điểm `None`, ba reason =
      `"not_applicable_financial_issuer"`, `red_flags == []` — khoá luật binding "Forensics"; RED (enum chưa có / hôm nay luôn tính).
- [x] `test_unknown_issuer_type_fails_closed` (mặc định `UNKNOWN`) → cả ba `None`, reason `"issuer_type_unknown"`; RED.
- [x] `test_degenerate_input_gives_none_with_reason` (param, một dòng cho mỗi hàng của bảng probe research `forensics-guards.md` §0 — điểm bị ảnh
      hưởng phải `None` kèm reason, không `ZeroDivisionError`):
      `total_liabilities=0` → Altman (hôm nay Z = 481.916 SAFE); `market_cap=0` → Altman (hôm nay X4 âm thầm = 0, GREY); `cost_of_goods_sold >= revenue`
      (GM ≤ 0) → Beneish (hôm nay M = 2109.05, bật cờ); `prev_aq <= 0` (vd `prev_current_assets + prev_property_plant_equipment >= prev_total_assets`)
      → Beneish (hôm nay M = 1209.166); `current_liabilities=0` → Piotroski (hôm nay F = 8 nhờ clamp); `total_assets=0` → cả ba; `revenue=0` →
      Beneish và Piotroski; `prev_accounts_receivable=0` → Beneish; `prev_selling_general_admin=0` → Beneish; `depreciation=0` và
      `property_plant_equipment=0` → Beneish (5 ca cuối hôm nay `ZeroDivisionError`); thêm các ca số không hữu hạn (red-team r1 RT2):
      `retained_earnings=nan` → Altman, reason `retained_earnings_not_finite` (hôm nay `DISTRESS` + red flag); `market_cap=nan` → Altman (hôm nay
      `DISTRESS`); `total_liabilities=inf` → Altman; `net_income=nan` → Beneish và Piotroski (hôm nay `M=nan`, `F=6`). RED.
      **Độc lập** (red-team r1 RT11): mỗi ca còn assert rằng các điểm **không** bị ảnh hưởng bằng đúng kết quả của
      `ForensicAccountingEngine.evaluate(base)` tính ngay trong test (không hard-code) — vd `prev_accounts_receivable=0` → Altman và Piotroski
      giữ nguyên; `net_income=nan` → Altman giữ nguyên.
- [x] `test_none_score_adds_no_red_flag` (GM ≤ 0 → không còn red flag Beneish) — RED (hôm nay bật cờ).
- [x] `test_zero_liabilities_is_not_safe` (`total_liabilities=0` → `altman_zone is None`, không phải `"SAFE"`) — RED.
- [x] `test_non_financial_fixture_scores_unchanged` (cơ sở `NON_FINANCIAL` → `altman_zone == "SAFE"`, `piotroski_f_score == 9`,
      `beneish_m_score == -2.348`, `unavailable_reasons == {}`) — LOCK giá trị OBSERVED (Z/M/F hiện tại) để chứng minh xoá clamp không đổi fixture.
- [x] `test_audit_result_is_frozen` — gán `result.altman_zone = "X"` → `dataclasses.FrozenInstanceError`; RED.

### Implement
- Bước 2 ở trên.

### Tests After
- [x] Toàn bộ test trên xanh; `test_end_to_end` xanh (assert dòng 120 không đổi).
- [x] G5 rỗng.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` (bash; PowerShell `.\.venv\Scripts\python -m pytest -q`) MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0 (in `"forensic_f_score": 9`).
- `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` và
  `python3.13 harness/scripts/scaffold_standards.py --type code-standards --check docs/code-standards.md` exit 0.
- Sau commit: `git push` (draft PR tự chạy CI — VL-1); run CI của commit này `success` trên 3.11 + 3.13
  (`gh run view <id> --json conclusion,jobs`) trước khi bắt đầu Phase 5.

## Success Criteria
- [x] RED ghi lại (ImportError, rồi lỗi hành vi) trước khi implement; sau implement toàn bộ xanh.
- [x] Không ca nào của `test_degenerate_input_gives_none_with_reason` ném `ZeroDivisionError` (pytest báo pass, không error).
- [x] G5 rỗng.
- [x] `grep -n "no zero-division guards\|ZeroDivisionError\|no sector gate" AGENTS.md docs/system-architecture.md docs/code-standards.md` → chỉ còn ghi chú lịch sử có ngày.
- [x] AGENTS.md có dòng Decisions về việc kéo sớm; `docs/project-roadmap.md:238` có ghi chú "done early".
- [x] Hai standards check exit 0.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| UNKNOWN fail-closed làm mọi caller tương lai nhận `None` | Trung bình × Thấp | Có chủ đích (luật binding, fail-closed); caller duy nhất hôm nay (smoke) truyền `NON_FINANCIAL`; phân loại thật thuộc refdata (roadmap Phase 1/3). |
| Guard sai thứ tự → reason khác kỳ vọng | Thấp × Thấp | Thứ tự guard cố định trong R4.3; test assert reason của ca một-guard. |
| Xoá clamp đổi kết quả fixture | Thấp × Trung bình | OBSERVED mọi clamp là no-op với fixture; `test_non_financial_fixture_scores_unchanged` khoá Z/M/F. |
| `frozen=True` vỡ code gán thuộc tính | Thấp × Thấp | OBSERVED không có gán; `dataclasses.replace` vẫn dùng được. |
