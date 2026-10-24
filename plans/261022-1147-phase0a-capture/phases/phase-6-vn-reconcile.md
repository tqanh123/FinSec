---
phase: 6
title: "Vn Reconcile"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 6 — Vn Reconcile

## Overview
Ba đầu ra cuối của roadmap 0a (`docs/project-roadmap.md:371-372`): (1) đối chiếu tay 30 công bố VN — giờ đăng trên cổng so với thời điểm vnstock, phân
tầng theo loại và sàn; (2) bảng counts (sự kiện theo loại theo năm, tỉ lệ có giờ trong ngày) từ một đợt backfill vnstock giới hạn tốc độ; (3) go/no-go
cho từng target VN (R1, D2, D4) theo luật power đã đăng ký trước. Luật power thành một module thuần có test (`quant/power_rule.py`), để kết quả tái lập
được và Phase 3 dùng lại. Cook trong lúc chờ 5 phiên (D4), nên mẫu đối chiếu lấy trước hết từ chính dữ liệu poller đã thu (cho cả độ trễ phát hiện).
Phụ thuộc Phase 5 (thứ tự chuỗi) và cạnh liên plan `261021-1314-phase0b-foundations/P5`. Không chạm `capture/` hay `configs/capture.toml` (đóng băng).

## Requirements
**R6.1 Luật power — `src/finsec_alpharadar/quant/power_rule.py` (mới, thuần, stdlib).** Thiết kế của main (red-team có thể thách):
- `effective_n(n_events, n_distinct_dates, rho) -> float` = `N / (1 + (m̄ − 1)·ρ)`, `m̄ = N / L` (research `vn-calendar-sessions.md` §5.1; ρ = 1 cho đúng `L`,
  ρ = 0 cho đúng `N`). Raise `ValueError` khi `L < 1` mà `N > 0`, `L > N`, số âm, hoặc `ρ ∉ [0, 1]`.
- `mde(n_eff, t=2.0) -> float` = `t / sqrt(n_eff)` — luật đã đăng ký của roadmap t ≈ IC·√N_eff ở t = 2 (§2.5, `docs/project-roadmap.md:120-123`).
- `PowerTier(str, Enum)`: `GO` khi MDE ≤ 0.05; `LIMITED` khi 0.05 < MDE ≤ 0.10; `NO_GO` khi MDE > 0.10 (trên 0.10 mọi hiệu ứng phát hiện được đều tự kích
  hoạt leakage audit — AGENTS.md:83).
- `assess(n_events, n_distinct_dates, rho=0.10) -> PowerAssessment` (frozen): `n_eff`, `mde`, `tier` (từ MDE chính), `mde_floor`/`tier_floor` (N_eff = L),
  `mde_80` = 2.8/√N_eff — CHỈ là dòng độ nhạy (power 80%, α 0.05 hai phía; IC 0.03 cần ~8,711 sự kiện — research §5.1), không bao giờ đổi `tier`.
  `n_events == 0` → `n_eff = 0`, `mde = inf`, `NO_GO`. ρ mặc định 0.10 là `[ASSUMED]` tới khi Phase 3 ước lượng ρ̂.
- `quant/__init__.py` re-export `assess`, `PowerAssessment`, `PowerTier` (`docs/code-standards.md:16`). `quant/__init__.py` import numpy qua các module
  khác (OBSERVED `c9168f0`) — chạy trong `.venv`, không ảnh hưởng capture.

**R6.2 `scripts/spike/vn_counts.py` (mới; env vnstock; KHÔNG import `finsec_alpharadar`; OWNER-RUN dưới user probe — D6).**
- `events --symbols-file <txt> --rate <calls/min> --out <dir>`: với mỗi mã `Reference().company(sym).events(source="vci")`, raw rows → `<out>/raw/` (có bản
  quyền, chỉ ngoài repo); `vnstock status` trước/sau (redact như R1.5); dừng khi quota còn < 30% hoặc gặp lỗi tầng vendor.
- Universe: thành phần VN100 + HNX30 hiện tại (~130 mã; `ref.index.members(...)` — tên nhóm `[ASSUMED]`, nếu không có thì danh sách sàn công bố), cộng các
  mã trong mẫu 30 công bố (R6.3) chưa có; ghi cỡ thật. Thành phần hiện tại không phải point-in-time — chấp nhận cho ước lượng counts, ghi rõ.
- Tổng hợp `counts-summary.json` (KHÔNG dòng dữ liệu): theo (nhóm `event_code`/`category`, năm) — `n_docs`, `n_events` (khoá `(symbol, event_code,
  public_date)`), `n_issuers`, `n_distinct_dates`, `max_events_per_date`, `share_with_intraday_time`; ngày cũ nhất; cỡ universe. Cột tối thiểu theo
  research §4.
- **Ngân sách quota (D4 × D2):** quota dùng chung mọi device (research `vnstock-gen5.md` §1). Chạy NGOÀI 08:00-17:30 ICT (poller ở cadence hằng giờ),
  `--rate` ≤ mức an toàn đo ở Phase 1 trừ phần poller đang dùng; nếu poller trên laptop vì thế gặp `vendor_quota_exceeded` thì đó là outage ĐÃ GHI của
  nguồn phụ, không phải khoảng trống chưa ghi.

**R6.3 `scripts/spike/vn_reconcile.py` (mới; `.venv`, dev user; dùng `_spikelib`, `finsec_alpharadar.capture.record.from_json_line`).**
- `sample --from <d> --to <d>`: đọc `capture-<d>.tar.gz` của các ngày cửa sổ từ `<backup-mirror>\daily\` CHỈ qua
  `finsec_alpharadar.capture.backup.iter_backup_members(archive, manifest)` (P2 R2.11: kiểm sha256 của tar với `manifest-<d>.json` trước, whitelist member,
  không bao giờ `extractall` — red-team r1 RT9; tarfile của Python 3.13.14 không có filter mặc định, OBSERVED main), đọc bản ghi trong bộ nhớ,
  phân loại tiêu đề TRONG BỘ NHỚ (NFC, casefold: BCTC — "báo cáo tài chính"/"bctc"; NQ HĐQT — "nghị quyết hđqt"/"nghị quyết hội đồng quản trị"; đăng ký giao
  dịch người nội bộ — "đăng ký giao dịch"/"người nội bộ"), chọn tất định 30 công bố: 10 mỗi loại, ~5 HOSE + ~5 HNX mỗi loại (sắp theo id, lấy cách đều).
  Tầng thiếu → bù từ lịch sử cổng (HOSE API theo ngày; HNX POST listing — bộ lọc ngày của nó có số đếm khó giải thích, research câu mở 4, nên đối chiếu RSS
  khi còn trong 100 item). `sample.json` (data root): id, sàn, mã, loại, giờ đăng gốc + UTC (HOSE `postedDate − 25200`, DERIVED — Phase 1 làm chính thức
  có golden test; HNX `pubDate` `+0700`), `first_seen_at` của poller nếu có. KHÔNG chép tiêu đề hay tên người ra khỏi data root (PDPL — research
  `vn-disclosure-portals.md` §5).
- `compare`: ghép `sample.json` với rows vnstock của cùng mã (theo mã + loại + ngày ±3) → `reconcile-summary.json` (chỉ tổng hợp): tỉ lệ vnstock có giờ trong
  ngày (kỳ vọng 0 — events chỉ có ngày, VERIFIED tài liệu vendor), tỉ lệ khớp ngày, phân bố trễ ngày (vnstock − cổng), phân bố giờ đăng theo khung
  08:00-14:45 / 14:45-17:30 / 17:30-24:00 / 00:00-08:00 / ngày không giao dịch (câu mở Q1 của research lịch), và **độ trễ phát hiện** `first_seen_at − giờ đăng`
  (trung vị, p90) cho các công bố đăng trong cửa sổ — số đo đầu tiên cho mục tiêu "VN median < 10 min" (AGENTS.md:75).
- `go-no-go`: đọc `counts-summary.json` + `reconcile-summary.json` + đầu ra P1 `--history-check`, gọi `assess`, in bảng markdown cho báo cáo. Cách đếm N:
  **D2** — BCTC quý + NQ HĐQT theo loại, quy ước "chỉ có ngày coi là sau close" nên mọi sự kiện có ngày tin cậy đều đếm (research §5.2); nếu events vnstock
  không có BCTC thì N(BCTC) là DERIVED (≈ 4 sự kiện/mã/năm × U × số năm, research §4) và ghi nhãn đúng như vậy; quy về U = 200 là DERIVED. **R1-VN** — chỉ
  sự kiện có giờ trong ngày (day 0 phải đúng): lịch sử giờ đăng của cổng (độ sâu từ `--history-check`, ECM HOSE từ 2024-08-15) + dữ liệu thu forward;
  luật IC áp cho R1 (một target phân loại) là quy ước của main, ghi rõ. **D4** — chỉ forward (`pit_quality` exact/first_seen), N = số thu được trong
  cửa sổ → kỳ vọng NO-GO tới hết Phase 4 (research §5.2).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Create | `scripts/spike/vn_counts.py` |
| Create | `scripts/spike/vn_reconcile.py` |
| Create | `src/finsec_alpharadar/quant/power_rule.py` |
| Create | `tests/test_power_rule.py` |
| Modify | `src/finsec_alpharadar/quant/__init__.py` |
| Modify | `docs/phase0-spike-report.md` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261022-1147-phase0a-capture/research/vn-calendar-sessions.md` |
| run | owner-run `vn_counts.py` dưới user probe; agent `vn_reconcile.py sample|compare|go-no-go` |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Challenge luật ">8 tệp": 10 tệp = 4 mới + 1 re-export + 5 docs. Giữ.

## Implementation Steps
1. **Điều kiện:** cửa sổ 5 phiên đã mở (Phase 3) và có ≥ 2 ngày backup; nếu chưa, `sample` dùng lịch sử cổng cho mọi tầng (ghi rõ) và bỏ phần độ trễ phát hiện.
2. **Tests-before (RED)** — `tests/test_power_rule.py` (RED: `ModuleNotFoundError: finsec_alpharadar.quant.power_rule`).
3. **Implement** `power_rule.py` + re-export → xanh.
4. Viết `vn_reconcile.py`, `vn_counts.py`; `.venv/Scripts/python -m pytest -q tests/test_no_order_execution.py` xanh.
5. Agent: `vn_reconcile.py sample …` → `sample.json` (data root); copy `vn_counts.py` + danh sách mã vào `C:\finsec-probe\kit\`.
6. **OWNER-RUN (user probe, ngoài 08:00-17:30 ICT):** `C:\finsec-probe\venv\Scripts\python -I C:\finsec-probe\kit\vn_counts.py events --symbols-file … --rate <…> --out C:\finsec-probe\out\<date>\counts`.
   Phát hiện xong: `counts-summary.json` + hai file status trước/sau. Chưa có → DỪNG.
7. Agent: `compare` rồi `go-no-go`; điền báo cáo mục 6.
8. **Delta docs** → **Regression gate** → một commit, ví dụ `feat: add the power rule and record VN counts and go/no-go` + trailer; push.

### Delta docs (cùng commit; tiếng Anh; có ngày)
- **AGENTS.md**
  - "Decisions": `- <date> — VN go/no-go rule (plan 261022-1147, main's design call, pre-registered): MDE = 2/sqrt(N_eff) at t = 2 (roadmap §2.5); N_eff = N/(1+(m-1)·rho) with rho = 0.10 until Phase 3 measures it, floored by the number of distinct event dates; GO if MDE <= 0.05, LIMITED if <= 0.10, NO-GO above; the 80%-power MDE (2.8/sqrt(N_eff)) is a sensitivity line only.`
  - "Verified Facts": **VN timestamps (OBSERVED <date>)** — vnstock events date-only; khớp ngày với cổng <x>%; giờ đăng: <x>% sau 14:45, <y>% sau 17:30, <z>% ngày
    không giao dịch (n = 30); độ trễ phát hiện trung vị/p90 trong cửa sổ. **VN counts and go/no-go (OBSERVED/DERIVED <date>)** — một dòng mỗi target: N, L, N_eff,
    MDE, bậc; nhãn DERIVED cho mọi số quy đổi/ước lượng.
  - "Package Layout" `quant/`: thêm `power_rule.py` (`assess`).
  - "Current Status" Tests: số do gate in ra.
- **docs/system-architecture.md**: bảng as-built thêm hàng `quant/power_rule.py` (thuần, test-only, chưa caller trong `src/`).
- **docs/code-standards.md**: "Testing" — số test ở dòng 50 và 56; AGENTS.md dòng 51 và 169; `docs/system-architecture.md` dòng 13 (red-team r1 LT12).
- **Dòng cửa sổ (red-team r1 LT4):** `grep -n "capture window started" AGENTS.md` không có hit mà cửa sổ đã mở (bước 9 của P3) → thêm dòng Decisions MỚI
  `- <date> — capture window started <date> at <sha> (capture_version <v>).` trong commit này.
- **docs/project-roadmap.md**: §7 0a gạch "VN timestamps" và "Output" (dòng 371-372) "Done <date>"; §2.5 (dòng 120-123) thêm con trỏ tới kết quả và ghi rõ power
  50% của t = 2; Phase 3 Exit (dòng 476-479) / Phase 4: ghi D4 NO-GO tới hết Phase 4 nếu kết quả xác nhận, và điều kiện cho R1-VN.
- **docs/phase0-spike-report.md** mục 6: bảng counts (chỉ tổng hợp), bảng đối chiếu (chỉ tổng hợp, không tiêu đề/tên), bảng go/no-go.

### Tests Before
`tests/test_power_rule.py`:
- [ ] `test_effective_n_is_n_without_correlation_and_l_with_full_correlation` (ρ = 0 → N; ρ = 1 → L).
- [ ] `test_effective_n_matches_the_research_example` (N = 6400, L = 576, ρ = 0.10 → ≈ 3182.3; research `vn-calendar-sessions.md` §5.1 ghi ≈ 3,180).
- [ ] `test_mde_matches_the_roadmap_power_rule` (N_eff 4444.4 → 0.03; 1600 → 0.05; 400 → 0.10 — research §5.1 bảng).
- [ ] `test_tier_upper_bounds_are_inclusive` (0.05 → GO; 0.0500001 → LIMITED; 0.10 → LIMITED; 0.1000001 → NO_GO).
- [ ] `test_zero_events_is_no_go_with_infinite_mde`.
- [ ] `test_invalid_inputs_raise` (L > N; L = 0 với N > 0; ρ < 0 hoặc > 1; số âm).
- [ ] `test_eighty_percent_power_is_sensitivity_only` (`mde_80 == 2.8/sqrt(n_eff)`; ca `mde` ≈ 0.045 / `mde_80` ≈ 0.063 (N_eff ≈ 1975) — `tier` vẫn là
      `GO`, `mde_80` chỉ được báo. Ca cũ "`mde_80 > 0.10` mà `mde ≤ 0.05`" không tồn tại vì `mde_80/mde = 1.4` — red-team r1 LT5).
- [ ] `test_assessment_is_frozen`.

### Implement
- Bước 3-7.

### Tests After
- [ ] `tests/test_power_rule.py` xanh; guard no-order-execution xanh với hai script mới; `tests/test_agents_md_paths.py` xanh.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- Hai lệnh `scaffold_standards.py --check` exit 0.
- Sau push: CI `success` trên 3.11 + 3.13.

## Success Criteria
- [ ] [test] 8 test RED trước, xanh sau.
- [ ] [manual] Mẫu đúng 30 công bố, phân tầng 10/10/10, cả hai sàn trong mỗi loại (hoặc ghi rõ tầng thiếu) (neo `manual_test_anchor.py`; bằng chứng `sample.json`).
- [ ] [invariant] Báo cáo và AGENTS.md không có tiêu đề công bố, tên người, hay dòng dữ liệu vnstock — chỉ số tổng hợp, mã CK và loại (đọc lại diff trước commit).
- [ ] [manual] Bảng go/no-go có một dòng cho mỗi target R1, D2, D4 với N, L, N_eff, MDE, bậc, và dòng độ nhạy 80%; chạy lại `vn_reconcile.py go-no-go` cho
      đúng bảng đó (neo `manual_test_anchor.py`).
- [ ] [manual] `vnstock status` trước/sau đợt backfill có trong `C:\finsec-probe\out\<date>\counts\`; không outage chưa ghi nào trên laptop do đợt này
      (heartbeat; neo `manual_test_anchor.py`).

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Backfill ăn quota chung, poller laptop bị giới hạn trong cửa sổ | Trung bình × Trung bình | Chạy đêm, rate dưới mức an toàn, dừng ở quota < 30%; lỗi quota trên laptop là outage đã ghi (D2). |
| events vnstock không có BCTC | Trung bình × Trung bình | N(BCTC) DERIVED từ hạn công bố (4/mã/năm) và ghi nhãn; Phase 3 thay bằng parse cổng. |
| Phân loại tiêu đề bằng từ khoá sai | Trung bình × Thấp | Chỉ để chọn mẫu; đối chiếu tay 30 dòng là chính; ghi tỉ lệ loại sai thấy được. |
| ρ mặc định 0.10 làm bậc lạc quan | Trung bình × Trung bình | Báo cả `mde_floor` (ρ = 1) và dòng 80%; bậc ghi rõ ρ; Phase 3 đo ρ̂. |
| Mẫu từ cửa sổ quá ít (nếu cook sớm) | Trung bình × Thấp | Bù từ lịch sử cổng; ghi nguồn mỗi tầng. |
| Tar độc từ thư mục cloud ghi đè file trên máy dev (RT9) | Thấp × Cao | Chỉ đọc qua `iter_backup_members` (sha manifest trước, whitelist, không `extractall`); test tar độc ở P2. |
