---
phase: 7
title: "Phase0 Exit Audit"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 7 — Phase0 Exit Audit

## Overview
Kiểm và ghi exit của roadmap Phase 0 (`docs/project-roadmap.md:400-404`): bốn tiêu chí E1-E4 phải cùng đạt. Bình thường không có code mới; ngoại lệ
duy nhất là hotfix theo thủ tục P3 R3.9 khi audit thấy khoảng trống do lỗi code (red-team r1 LT2). Phase này CHỜ cho tới khi có ≥ 5 phiên giao
dịch liên tiếp kể từ ngày bắt đầu cửa sổ (Phase 3, D4), rồi chạy `audit` trên snapshot backup, đối chiếu chéo với Event Log, và ghi kết quả có ngày vào
AGENTS.md. Một khoảng trống chưa ghi KHÔNG bao giờ được "làm mềm": exit không được ghi, chuỗi đếm bắt đầu lại từ ngày giao dịch sau khoảng trống, và
phase chạy lại sau. Phụ thuộc Phase 6 và cạnh liên plan `261021-1314-phase0b-foundations/P5`.

## Requirements
**R7.1 Điều kiện vào (kiểm trước mọi việc khác).** Đọc ngày bắt đầu cửa sổ từ dòng có ngày MỚI NHẤT dạng "capture window started/restarted <date> at
<sha>" trong AGENTS.md "Decisions" (ghi lần đầu ở commit phase đầu tiên sau khi cửa sổ mở — P4, P5 hoặc P6; chưa phase nào ghi thì phase này thêm nó từ
bản ghi bước 9 của P3 trước khi đếm — các lần sau theo thủ tục P3 R3.9 — dòng mới nhất thắng, dòng cũ không bao giờ bị sửa,
AGENTS.md:37; red-team r1 LT2). Đếm ngày giao dịch theo lịch trong
`configs/capture.toml` (thứ 2-6 trừ `[[holidays]]`) từ ngày đó tới ngày UTC đã đóng gần nhất có backup. < 5 → DỪNG phase, không ghi gì, báo ngày sớm nhất
có thể chạy lại.

**R7.2 E4 — poller chạy 5 phiên liên tiếp, có heartbeat, không khoảng trống chưa ghi.**
- Snapshot: `<backup-mirror>\daily\<d_last>\state-<d_last>.sqlite` (chứa toàn bộ lịch sử tới ngày đó); kiểm sha256 của nó với `manifest-<d_last>.json` trước.
- `.venv/Scripts/python -m finsec_alpharadar.capture audit --state <snapshot> --from <window_start> --to <d_last> --days trading --require-days 5 --out $FINSEC_DATA_ROOT/exit/audit-<d_last>.json`
  → exit 0 và `longest_consecutive_passing_trading_days ≥ 5` (luật R2.12: không khoảng trống chưa ghi ở bất kỳ nguồn bật nào, kể cả hai mép; mỗi nguồn
  cổng có ≥ 1 poll ok trong 08:00-17:30 mỗi ngày; ≥ 1 heartbeat mỗi ngày; độ lệch đồng hồ trong giới hạn). Audit mở snapshot ở chế độ chỉ đọc
  `immutable=1`, không tạo file trong `<backup-mirror>` (red-team r1 RT1, RT15).
- File heartbeat đã sync: với MỖI ngày giao dịch của cửa sổ, `<backup-mirror>\heartbeats\heartbeat-<d>.json` tồn tại (không chỉ dòng `heartbeats` trong
  state — red-team r1 RT11).
- Hạn pause Windows Update (trường `wu_pause_expiry` của heartbeat ngày cuối) vẫn sau ngày cuối của cửa sổ; nếu đã hết giữa chừng, mọi reboot do update
  phải hiện như outage đã ghi (LT2).
- Đối chiếu chéo thủ công (research `capture-host-windows.md` §6): với mỗi event 41, 42, 506 (vào Modern Standby), 6008, 1074, Kernel-General 12/13 trong
  `<backup-mirror>\ops\eventlog-*.json` của các ngày cửa sổ, phải có một outage đã ghi phủ thời điểm đó hoặc lý do rõ (ví dụ 506 do tắt màn hình mà poll vẫn
  đều — 506 đơn lẻ không phải outage). Event không giải thích được → ghi OBSERVED; nếu nó trùng một quãng không có poll thì đó là khoảng trống chưa ghi → E4 FAIL.
- Clone laptop vẫn ở SHA của dòng cửa sổ mới nhất (owner chạy `git -C C:\finsec\app rev-parse HEAD` và `git -C C:\finsec\app status --porcelain` → file
  `<backup-mirror>\ops\laptop-head-<yyyyMMdd>.txt`); `capture_version` trong heartbeat các ngày cửa sổ không đổi, hoặc mỗi lần đổi có dòng
  "window restarted" tương ứng trong AGENTS.md.
- E4 FAIL → không ghi exit; ghi một dòng OBSERVED có ngày ("exit audit <date>: unlogged gap <khoảng> on <source>; the 5-session count restarts on <date>").
  Nguyên nhân cần sửa code/config → thủ tục hotfix P3 R3.9 (P7 khai `src/finsec_alpharadar/capture/`, `configs/capture.toml` và `tests/test_capture_*.py`
  chỉ cho đúng trường hợp này; test hồi quy RED→GREEN; commit riêng `fix(capture): …`), rồi dòng MỚI "capture window restarted <date> at <sha>"; chạy lại
  phase sau 5 phiên nữa.

**R7.3 E1 — pytest thu và pass ≥ 15 test, gồm EDGAR golden test.** Regression gate in ra số test; `.venv/Scripts/python -m pytest -q tests/test_edgar_time.py`
pass với 0 skipped.

**R7.4 E2 — bảng spike nằm trong AGENTS.md.** "Verified Facts" có các dòng OBSERVED có ngày từ Phase 1 (vnstock, cổng), Phase 4 (EDGAR), Phase 5 (Alpaca),
Phase 6 (VN timestamps, counts, go/no-go); không còn "ASSUMED until the Phase 0 probe" cho các mục đã probe (`grep -n "until the Phase 0 probe" AGENTS.md docs/system-architecture.md`
→ chỉ còn mục chưa probe, mỗi mục nêu lý do). SSI ngoài phạm vi (§9.5) — ghi rõ là mục 0a duy nhất không làm.

**R7.5 E3 — `/hs:plan` chạy được.** Bằng chứng: plan này được tạo bằng `/hs:plan`; cook chạy (bash, ở main checkout)
`HARNESS_DATA_ROOT="$PWD/.harness" harness/bin/hs-run plan next --plan plans/261022-1147-phase0a-capture` → exit ∈ {0, 10, 11} (không phải 2) và
envelope JSON hợp lệ với `root_source` = `env:HARNESS_DATA_ROOT`; ghi exit code + `state` (OBSERVED). Lệnh đã thử trên plan 0b ngày 2026-10-22 (planner):
exit 10, `state: approval_drifted`, `root_source: env:HARNESS_DATA_ROOT`. KHÔNG đặt `HARNESS_BIN_ROOT` bằng chính repo: OBSERVED cùng ngày, `hs-run` trả
`state: error` "resolved project root … is the SHARED bin root … refusing" — bin root của bản vendored tự suy ra được.

**R7.6 Ghi exit** (chỉ khi E1-E4 cùng đạt): AGENTS.md "Roadmap" Phase 0 → "Done <date>"; "Current Status"; báo cáo mục 7 (bảng E1-E4 với bằng chứng); roadmap §7
Phase 0 Exit. Ghi kèm các khoản nợ đã biết đi tiếp sang Phase 1/3 (ví dụ blocker bar thô nếu có; SSI chưa làm).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | `docs/phase0-spike-report.md` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| Modify (chỉ hotfix R3.9) | `src/finsec_alpharadar/capture/` (thư mục) |
| Modify (chỉ hotfix R3.9) | `configs/capture.toml` |
| Modify (chỉ hotfix R3.9) | `tests/test_capture_*.py` (glob) |
| run | `python -m finsec_alpharadar.capture audit` trên snapshot backup |
| run | `harness/bin/hs-run plan next` (E3) |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

## Implementation Steps
1. **R7.1** — đếm ngày; < 5 → DỪNG.
2. **BLOCKED (owner)** — thả `laptop-head-<yyyyMMdd>.txt` vào `<backup-mirror>\ops\` (R7.2) và xác nhận backup đêm gần nhất đã sync. Phát hiện: file có mặt +
   manifest của `d_last` kiểm đúng. Chưa có → DỪNG.
3. **Tests-before:** không có hành vi mới; LOCK = toàn bộ suite (E1) và `tests/test_agents_md_paths.py`.
4. E4 (R7.2) → E1 (R7.3) → E2 (R7.4) → E3 (R7.5). Bất kỳ cái nào FAIL → ghi OBSERVED, không ghi exit, dừng.
5. **Delta docs** (R7.6) → **Regression gate** → một commit, ví dụ `docs: record the Phase 0 exit` + trailer; push; CI xanh. Mở PR "Ready for review"
   và merge (merge commit, như 0b VL-2; plan này VL-2: một PR, merge sau P7) là việc sau cook, khi có review-decision PASS.

### Delta docs (cùng commit; tiếng Anh; có ngày)
- **AGENTS.md**: "Roadmap" Phase 0 "(done <date>)"; Phase 1 thành "next"; "Current Status" (capture đã chạy N phiên; Phase 0 exit); "Verified Facts" thêm
  **Phase 0 exit (OBSERVED <date>)** — E1 số test, E2, E3 exit code, E4 chuỗi ngày đạt + số outage theo loại + độ trễ phát hiện trung vị trong cửa sổ.
- **docs/system-architecture.md**: Overview — capture đang chạy từ <date>, Phase 0 xong; số test.
- **docs/code-standards.md**: "Testing" — số test ở dòng 50 và 56 (cùng AGENTS.md dòng 51, 169 và `docs/system-architecture.md` dòng 13 — LT12); dòng 57
  ("Planned Phase 0 exit: at least 15 passing tests") → đạt <date>.
- **docs/project-roadmap.md**: §7 Phase 0 tiêu đề "NEXT" → "DONE <date>"; Exit (dòng 400-404) từng gạch "met <date>"; Phase 1 thành NEXT.
- **docs/phase0-spike-report.md** mục 7: bảng E1-E4 + đường dẫn bằng chứng (ngoài repo, chỉ tên file).

### Tests Before
- N/A — phase kiểm/ghi, không hành vi code mới. LOCK: toàn bộ suite (E1), `tests/test_agents_md_paths.py` (AGENTS.md đổi).

### Implement
- Bước 4-5.

### Tests After
- [ ] Suite xanh; `tests/test_edgar_time.py` pass, 0 skipped; `tests/test_agents_md_paths.py` xanh.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` MUST pass, 0 skipped, số test ≥ 15 (KHÔNG phải N/A: AGENTS.md đổi và E1 đọc từ chính lệnh này).
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- Hai lệnh `scaffold_standards.py --check` exit 0.
- Sau push: CI `success` trên 3.11 + 3.13.

## Success Criteria
- [ ] [test] E4: `audit … --require-days 5` exit 0 trên snapshot đã kiểm sha (file báo cáo audit dưới data root); không file mới nào cạnh snapshot.
- [ ] [invariant] Mỗi ngày giao dịch của cửa sổ có `<backup-mirror>\heartbeats\heartbeat-<d>.json` (RT11).
- [ ] [manual] Đối chiếu Event Log không còn event nào không giải thích được trong quãng không có poll (neo `manual_test_anchor.py`; bằng chứng: các
      `eventlog-*.json` và bảng đối chiếu trong báo cáo mục 7).
- [ ] [test] E1: gate in ≥ 15 passed, gồm `tests/test_edgar_time.py`.
- [ ] [invariant] E2: mọi mục 0a đã probe có dòng OBSERVED có ngày trong AGENTS.md; SSI ghi rõ là ngoài phạm vi.
- [ ] [test] E3: `hs-run plan next` exit ∈ {0, 10, 11}, envelope JSON parse được.
- [ ] [invariant] Exit chỉ được ghi khi E1-E4 cùng đạt; một lần FAIL để lại dòng OBSERVED và ngày chạy lại, không có dòng "Done".

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Một khoảng trống chưa ghi làm hỏng chuỗi 5 phiên | Trung bình × Trung bình | Không làm mềm; đếm lại; nguyên nhân thành OBSERVED + sửa (restart cùng SHA = outage đã ghi; hotfix SHA mới = đếm lại, VL-3). |
| Backup chưa sync về máy dev | Trung bình × Thấp | Bước owner 2; dùng snapshot của ngày đã đóng gần nhất có manifest đúng. |
| Cửa sổ rơi vào kỳ nghỉ cuối năm / chuyển sàn 28/12 | Trung bình × Trung bình | Lịch config + tránh 21/12-01/01 (Phase 3 R3.7); một ngày nghỉ được đánh dấu không làm gãy chuỗi. |
| `hs-run` trả lỗi vì môi trường (biến gốc, state installer) | Thấp × Thấp | Chỉ đặt `HARNESS_DATA_ROOT` tuyệt đối, không đặt `HARNESS_BIN_ROOT` bằng repo (OBSERVED R7.5); chạy ở main checkout đã cài installer (AGENTS.md:125). |
