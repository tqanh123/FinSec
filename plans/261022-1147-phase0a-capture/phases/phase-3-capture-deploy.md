---
phase: 3
title: "Capture Deploy"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 3 — Capture Deploy

## Overview
Đưa poller của Phase 2 lên laptop dự phòng chạy như máy chủ (§9.4, AGENTS.md:25): viết `docs/capture-runbook.md`, owner cấu hình máy theo
runbook (power, Windows Update, NTP, tài khoản `capture` + Autologon — D7, thư mục dữ liệu và backup — D3), cài env khoá hash, đăng ký scheduled
task, dry run, burn-in ≥ 24 h, rồi BẮT ĐẦU cửa sổ 5 phiên giao dịch liên tiếp (D4). Từ đó áp luật đóng băng. Phase này phần lớn là vận hành do owner
làm; agent soạn runbook, đọc bằng chứng owner thả vào thư mục sync, và ghi kết quả. Phụ thuộc Phase 2 và cạnh liên plan
`261021-1314-phase0b-foundations/P5`. Phase 4-6 được cook trong lúc chờ 5 phiên (D4); Phase 7 kiểm exit.

**Thứ tự commit (red-team r1 LT4, phán quyết của main — giữ "một commit mỗi phase"):** commit DUY NHẤT của P3 mang runbook + `configs/capture.toml`
cuối cùng và được push TRƯỚC khi laptop triển khai; laptop clone đúng commit đó. Triển khai, burn-in 24 h và `verification-P3.json` diễn ra SAU commit
(phase chỉ "xong" khi burn-in đạt; `verification-P3.json` đi vào commit của Phase 4 — `plan.md` Rollback). Các sự thật OBSERVED về laptop đi vào AGENTS.md trong commit của Phase 4 (P4 "Delta docs"); dòng có ngày
"capture window started <date> at <sha>" đi vào commit phase ĐẦU TIÊN sau khi cửa sổ mở (P4; cửa sổ chưa mở lúc cook P4 thì P5, P6 hoặc P7 — P4 "Delta
docs"). Burn-in đòi đổi code/config → thủ tục hotfix R3.9, không sửa commit P3.

## Requirements
**R3.1 Kênh truyền bằng chứng (mọi bước owner phải để lại FILE, không bao giờ là lời xác nhận miệng).** Thư mục sync của D3 là kênh: trên laptop
`FINSEC_BACKUP_DIR` (ví dụ `C:\Users\capture\OneDrive\FinSecBackup`), con `ops\` chứa bằng chứng owner thả vào; trên máy dev, owner làm cho cùng thư
mục đọc được (cùng tài khoản cloud phụ, hoặc chia sẻ thư mục cho tài khoản chính) và báo đường dẫn phía dev — cook gọi nó là `<backup-mirror>`.
`[ASSUMED]` owner chọn cách nào; phát hiện bằng chính file trong `<backup-mirror>\ops\`.

**R3.2 `docs/capture-runbook.md` (mới, tiếng Anh).** Mỗi bước ghi rõ OWNER hay AGENT, lệnh (PowerShell elevated khi cần — không chạy `powercfg /a`
từ Git Bash vì MSYS đổi `/a`, OBSERVED research `capture-host-windows.md` §1.1), và output kỳ vọng. Mục:
1. Prerequisites — Windows 11, cắm AC, Ethernet nếu có; Python 3.13 x64 từ python.org cài CHO USER `capture`, trong phiên `capture` (red-team r1 RT6 —
   Python per-user của admin không chạy được dưới user khác); `C:\finsec\` do admin tạo rồi `icacls C:\finsec /inheritance:r`, cấp `Administrators` và
   `SYSTEM` Full, `capture` chỉ RX (red-team r1 RT4: `C:\` kế thừa `Authenticated Users:(M)` — OBSERVED main); chỉ data root `C:\finsec-capture` cấp
   `capture` Modify; lưu output `icacls C:\finsec` và `icacls C:\finsec-capture` làm bằng chứng; clone tại `C:\finsec\app` (không dưới Documents/OneDrive — Known Folder Move,
   research §7.4), data root `C:\finsec-capture` (ngoài clone, ngoài thư mục sync; `capture` có quyền Modify).
2. Baseline — `powercfg /getactivescheme`, `powercfg /a`, `powercfg /export C:\finsec-capture\ops\power-before.pow <GUID>`, `sc.exe qc w32time`,
   `sc.exe qtriggerinfo w32time`, `w32tm /stripchart /computer:time.windows.com /dataonly /samples:5`, `Get-WinEvent` 30 ngày (ID ở mục 13).
3. Power (research §1.1): `powercfg /change standby-timeout-ac 0`, `hibernate-timeout-ac 0`, `monitor-timeout-ac 10`, `disk-timeout-ac 0`;
   `powercfg /hibernate off`; LIDACTION = 0 cả AC lẫn DC qua GUID `4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936`
   (alias `LIDACTION` không có trên máy dev — OBSERVED research); giới hạn sạc OEM nếu có (`[ASSUMED]` theo model). Poller tự giữ
   `SetThreadExecutionState` (R2.13) — lớp thứ hai cho Modern Standby (research §0.1-0.2).
4. Windows Update — active hours 07:00-19:00 qua Settings (mặc định 8-17 không đủ, VERIFIED research §2); **Pause updates** 7-14 ngày, sao cho hết
   pause ≥ 2 ngày sau ngày cuối dự kiến của cửa sổ; resume vào cuối tuần. Không dùng `NoAutoRebootWithLoggedOnUsers`. Hạn pause tối đa 35 ngày
   (research `capture-host-windows.md` §2): kiểm lại hạn pause TRƯỚC khi mở cửa sổ (R3.7) và hằng ngày trong cửa sổ qua trường `wu_pause_expiry` của
   heartbeat (P2 R2.10); còn < 2 ngày mà cửa sổ chưa xong → owner gia hạn pause (red-team r1 LT2).
5. Time — KB 2385818 method 1 (`sc.exe triggerinfo w32time delete`, `sc.exe config w32time start= auto`, `net start w32time`); peers
   `0.vn.pool.ntp.org,0x9 0.asia.pool.ntp.org,0x9 1.asia.pool.ntp.org,0x9 time.windows.com,0xb`; `SpecialPollInterval` 1024; `w32tm /resync /rediscover`;
   kiểm `w32tm /query /status /verbose` (Last Sync Error 0). Lý do: trên máy dev `w32time` Stopped, `w32tm /query /status` lỗi `0x80070426`
   (OBSERVED main 2026-10-22).
6. Accounts (D7) — tạo user cục bộ CHUẨN `capture` và `Set-LocalUser capture -PasswordNeverExpires $true` (red-team r1 RT3: chính sách "Maximum password
   age (days): 42" trên máy dev — OBSERVED main 2026-10-22, `net accounts`; trên laptop `[ASSUMED]` giống); bằng chứng `net user capture` → "Password expires
   Never"; Sysinternals Autologon (GUI, không CLI vì mật khẩu vào lịch sử shell) cho `capture`; ghi rõ
   đánh đổi: mật khẩu lưu dạng LSA secret, admin giải mã được (VERIFIED https://learn.microsoft.com/en-us/sysinternals/downloads/autologon); task
   khoá màn hình lúc `capture` logon (`rundll32.exe user32.dll,LockWorkStation`, `[PRIOR]`); OneDrive/Google Drive đăng nhập bằng tài khoản cloud
   PHỤ trong phiên `capture`; kiểm quota (Google 5 GB với tài khoản tạo từ 2026-03-09 — VERIFIED research §5.3).
7. Folders — `C:\finsec-capture` (data root), `FINSEC_BACKUP_DIR` trong thư mục sync; loại `%USERPROFILE%\.vnstock` khỏi mọi thư mục sync (chứa
   key mã hoá — research `vnstock-gen5.md` §1).
8. Install — chép bộ wheel `$FINSEC_DATA_ROOT\wheels\win-py313\` của Phase 1 sang `C:\finsec\wheels\` bằng USB hoặc mạng nội bộ (không qua thư mục
   cloud; red-team r1 RT10). Admin tạm cấp `capture` Modify trên `C:\finsec\venv`; trong phiên `capture`:
   `%LOCALAPPDATA%\Programs\Python\Python313\python.exe -m venv C:\finsec\venv` rồi
   `C:\finsec\venv\Scripts\python -I -m pip install --require-hashes --no-deps --no-index --find-links C:\finsec\wheels -r C:\finsec\app\requirements\vnstock\win-py313.lock`
   (không index nào; cùng dạng lệnh đã chạy ở Phase 1 OB5); rồi admin đổi quyền `capture` trên `C:\finsec\venv` về RX (`icacls C:\finsec\venv /grant:r capture:(OI)(CI)RX`).
   Bằng chứng: `type C:\finsec\venv\pyvenv.cfg` → `home` trong profile của chính `capture` (RT6). KHÔNG `pip install -e` repo (capture chạy qua `PYTHONPATH`, R2.1).
9. vnstock (OB4b, OWNER trong phiên `capture`) — `vnstock_probe.py hash` trước; `C:\finsec\venv\Scripts\vnstock.exe register` (owner tự gõ key);
   `vnstock_probe.py status --out <FINSEC_BACKUP_DIR>\ops`; `hash` sau; ghi số device đã dùng trên tài khoản (`https://vnstocks.com/account?section=devices`).
10. Wrapper `C:\finsec\run-capture.cmd` (ngoài repo, không bí mật) đặt `FINSEC_DATA_ROOT`, `FINSEC_BACKUP_DIR`, `PYTHONPATH=C:\finsec\app\src`,
    `PYTHONUTF8=1`, `DO_NOT_TRACK=1`, `VNSTOCK_TELEMETRY=0`, `VNSTOCK_DISABLE_AGENT_SETUP=1`, `VNSTOCK_DISABLE_GLOBAL_AGENT=1`, rồi
    `PYTHONDONTWRITEBYTECODE=1` (venv và clone chỉ RX), rồi `C:\finsec\venv\Scripts\python.exe -P -m finsec_alpharadar.capture %*` — `-P` để cwd (data root,
    ghi được) không đứng đầu `sys.path`, nên một `json.py` lạ trong data root không chạy được dưới `capture` (red-team r1 LT6). Không dựa vào biến môi trường
    của profile (task có nạp hay không là `[PRIOR]`).
11. Dry run (OWNER trong phiên `capture`, cwd = data root): `C:\finsec\run-capture.cmd check-root`, rồi `C:\finsec\run-capture.cmd once --all`, rồi
    `C:\finsec\run-capture.cmd heartbeat` → `heartbeat.json` hiện trên điện thoại.
12. Scheduled tasks (research §4.3 phương án A, chỉnh theo D7) — `\FinSec\VNCapture`: action `C:\finsec\run-capture.cmd run`, WorkingDirectory
    `C:\finsec-capture` (KHÔNG BAO GIỜ là clone: vnstock trước 4.0.9 ghi vào AGENTS.md của cwd — research §3); trigger `-AtStartup` và `-Once -At <now+1m>
    -RepetitionInterval 5 phút`; settings `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 255 -RestartInterval 1 phút`; **LogonType Password** ("run whether user is logged on or not")
    đăng ký qua bộ tham số `User`/`Password` của `Register-ScheduledTask` — bộ `Principal` KHÔNG có `-Password` (OBSERVED main 2026-10-22, PS 5.1):
    `$c = Get-Credential capture; Register-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' -Action $a -Trigger $t -Settings $s -User $c.UserName -Password $c.GetNetworkCredential().Password -RunLevel Limited`
    (owner gõ mật khẩu vào hộp thoại, không bao giờ literal trong lệnh hay lịch sử shell; red-team r1 RT3). `Export-ScheduledTask` → `ops\vncapture-task.xml`
    (kiểm `PT0S`, `IgnoreNew`, repetition, `LogonType` = `Password`). `\FinSec\EventLogExport` hằng ngày
    07:30 ICT: lệnh `Get-WinEvent` của research §6 (ID 41, 42, 107, 506, 507, 1, 12, 13, 1074, 6005, 6006, 6008 theo provider) →
    `<FINSEC_BACKUP_DIR>\ops\eventlog-<yyyyMMdd>.json`, thời điểm chuẩn hoá UTC, parse từ XML không từ `Message`.
13. Verify — `powercfg /requests` (admin) liệt kê `python.exe` ở mục `SYSTEM:` (yêu cầu `ES_SYSTEM_REQUIRED` của poller; tiến trình vẫn chạy dưới user
    `capture`) khi task đang chạy; heartbeat trên điện thoại; khởi động lại máy một lần
    và thấy task tự chạy lại + outage `process_down` trong heartbeat.
14. Burn-in ≥ 24 h (gập nắp, màn hình tắt) — xem R3.6.
15. Exit window — R3.7; checklist hằng ngày (heartbeat `OK`, `outages_24h`, skew, backup đêm qua đã kiểm, `eventlog-*.json` mới, `wu_pause_expiry` còn
    ≥ 2 ngày sau ngày cuối dự kiến); thủ tục khởi động lại cửa sổ / hotfix ở R3.9.
16. Incidents — mất điện, mất mạng, reboot do update, đầy quota cloud, đồng hồ lệch > 60 s, đĩa < 10 GB, vnstock hết hạn/bị yank (re-pin là bảo trì có
    chủ đích, ghi outage) — mỗi loại: dấu hiệu, lệnh kiểm, hành động, outage ghi thế nào. Thêm:
    - **Mật khẩu `capture` hết hạn hoặc bị đổi** (RT3): Autologon và task cùng chết sau reboot. Sửa: `$c = Get-Credential capture;
      Set-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' -User $c.UserName -Password $c.GetNetworkCredential().Password`, chạy lại Autologon (GUI),
      kiểm `net user capture` → "Password expires Never". Không gõ mật khẩu literal.
    - **"Device limit" của vnstock** (red-team r1 LT3): `register` bị từ chối vì hết slot thiết bị → gỡ thiết bị cũ ở `https://vnstocks.com/account?section=devices`
      rồi `register` lại; trong lúc đó nguồn vnstock là outage đã ghi.
    - **Bản vnstock biến mất / hết hạn** (RT10): cài lại từ `C:\finsec\wheels\` (không cần index); nếu chính bản đó hết hạn 365 ngày → venv chỉ-cổng: chép
      khối `tzdata==…` (kèm mọi `--hash`) từ lock vào `C:\finsec\tzdata-only.txt`, cài bằng cùng lệnh `--no-index --find-links`, đặt `vnstock_events`
      `enabled = false` theo thủ tục hotfix R3.9 (OBSERVED 2026-10-22, planner: lệnh dạng này cài `tzdata==2026.5` từ bộ wheel và `ZoneInfo('Asia/Ho_Chi_Minh')` nạp được).
17. Rollback/uninstall — `powercfg /import` file baseline + `/setactive`; `Unregister-ScheduledTask`; tắt Autologon; trả W32Time về trigger-start
    (`sc.exe triggerinfo w32time start/networkon stop/networkoff` — luôn `sc.exe`, không bao giờ `sc`: trong PowerShell 5.1 `sc` là alias của `Set-Content`,
    red-team r1 LT2). **Dữ liệu (data root và backup) không bao giờ bị xoá.**

**R3.3 LogonType Password — thiết kế của planner, `[ASSUMED]` tới cổng kiểm.** D7 không khoá logon type. S4U bị loại: "no access to either the network
or encrypted files" (VERIFIED https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype) mà kho key vnstock mã hoá theo user
(`~/.vnstock/auth/`, research `vnstock-gen5.md` §1). Interactive phụ thuộc phiên desktop (DAM có thể tạm dừng desktop app — research §4.2). Password
cho chạy từ lúc boot, trước Autologon, và không thêm rủi ro nào ngoài LSA secret mà D7 đã chấp nhận. Cổng: trong burn-in, SAU lần khởi động lại có
chủ đích, ít nhất một poll `vnstock_events` thành công có `polls.run_mode = 'run'` và `pid` khác pid của dry run (red-team r1 LT3: dry run `once` cũng
poll vnstock, nên cột này là thứ duy nhất phân biệt) — đọc trên snapshot backup. Fail → thử Interactive (phiên Autologon) và ghi lại.

**R3.4 Bằng chứng laptop (OB2) — `<backup-mirror>\ops\laptop-config-<yyyyMMdd>.txt`**, owner chạy khối verify của runbook (output chuyển hướng
vào file), cook đối chiếu từng giá trị: `powercfg /a` (ghi S0 hay S3); STANDBYIDLE AC = 0, HIBERNATEIDLE AC = 0, hibernate không khả dụng, LIDACTION
AC = DC = 0; `sc.exe qc w32time` → `AUTO_START`; `sc.exe qtriggerinfo w32time` → không trigger; `w32tm /query /status /verbose` → `Last Sync Error: 0`,
`Source` là một peer đã cấu hình; `w32tm /query /peers` → 4 peer; stripchart 5 mẫu |offset| < 0.5 s; `ActiveHoursStart=7`, `ActiveHoursEnd=19`
(`HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings`, OBSERVED vị trí trên máy dev — research §2); ngày hết pause; phiên bản Windows/Python;
`net user capture` → "Password expires Never" (RT3); `icacls C:\finsec` (không kế thừa; `capture` chỉ RX) và `icacls C:\finsec-capture` (`capture` Modify)
(RT4); `type C:\finsec\venv\pyvenv.cfg` → `home` trong profile của `capture` (RT6); `git -C C:\finsec\app rev-parse HEAD` = SHA commit P3. Thiếu hay lệch
một dòng → bước chưa xong.

**R3.5 Bằng chứng khác.** OB4b: `<backup-mirror>\ops\vnstock-status.txt` (tier, device, hạn dùng; đã redact) + hai file hash before/after khớp nhau.
OB7: `heartbeat.json` xuất hiện trong `<backup-mirror>` + owner xác nhận thấy trên điện thoại (manual); quyết định Autologon (bật theo D7, hoặc owner
từ chối → upload trễ sau reboot, ghi lại).

**R3.6 Burn-in ≥ 24 h — cổng trước cửa sổ.** Gập nắp + màn hình tắt; một lần khởi động lại có chủ đích. Sau đó trên máy dev (agent):
`.venv/Scripts/python -m finsec_alpharadar.capture audit --state <backup-mirror>\daily\<d>\state-<d>.sqlite --from <d0> --to <d1> --days all --require-days 1`
→ exit 0 (không khoảng trống chưa ghi; lần reboot hiện như outage `process_down` đã ghi); `eventlog-*.json` không có 42/506 (vào ngủ) nào nằm
ngoài một outage đã ghi; `polls` có `vnstock_events` ok chạy bởi task (R3.3); `powercfg /requests` có `python.exe` (manual); mỗi ngày burn-in có file
`<backup-mirror>\heartbeats\heartbeat-<d>.json` (red-team r1 RT11). Snapshot ở chế độ DELETE nên audit đọc thẳng trong `<backup-mirror>` mà không tạo
`-wal`/`-shm` (P2 R2.11/R2.12, RT15). Các câu hỏi Q1, Q3, Q5, Q6, Q8 của research `capture-host-windows.md` được trả lời OBSERVED trên chính laptop;
cook ghi chúng vào AGENTS.md ở commit của Phase 4 (LT4). `verification-P3.json` chỉ được ghi khi burn-in đạt.

**R3.7 Bắt đầu cửa sổ + luật đóng băng (D4).** Ngày giao dịch đầu tiên sau khi burn-in đạt, với Windows Update đang pause tới hết cửa sổ + 2 ngày.
Tuần sạch (research `vn-calendar-sessions.md` §6): 02-06/11, 09-13/11, 16-20/11, 30/11-04/12, 07-11/12, 14-18/12/2026; tránh tuần 23-27/11 nếu có thể
(24/11 nghỉ — lịch vẫn đếm đúng nếu config đánh dấu 24/11) và 21/12/2026-01/01/2027 (chuyển HNX→HOSE). Trước khi mở cửa sổ: kiểm lại hạn pause
Windows Update (≥ ngày cuối dự kiến + 2 ngày; LT2). Dòng "capture window started <date> at <sha> (capture_version <v>)" vào AGENTS.md trong commit
phase đầu tiên sau khi cửa sổ mở (P4-P7; LT4). Đóng băng tới Phase 7: không đổi clone laptop, venv, hay `configs/capture.toml`; nhánh dev vẫn đi tiếp (Phase 4-6 không chạm
`capture/` hay `configs/capture.toml`). Một lần khởi động lại tiến trình trên CÙNG SHA (reboot, crash, task restart) tự ghi outage `process_down`; outage
đã ghi được phép (exit đòi "no unlogged gap"), nên chuỗi KHÔNG đếm lại vì nó. Deploy một SHA mới (hotfix code hoặc config, R3.9) thì chuỗi ĐẾM LẠI từ
ngày giao dịch đầu tiên chạy SHA đó (VL-3, owner 2026-10-24).

**R3.8 `configs/capture.toml`** — giá trị cuối nằm trong commit P3 (từ dry run Phase 2 bước 10 + design inputs); `tests/test_capture_cli.py::test_shipped_config_loads_and_respects_d5`
phải xanh trong gate của P3. Đổi sau commit P3 (kể cả do burn-in) chỉ qua R3.9.

**R3.9 Thủ tục khởi động lại cửa sổ / hotfix (red-team r1 LT2, phán quyết của main).**
- **Khi nào:** burn-in hay cửa sổ cần đổi `src/finsec_alpharadar/capture/**` hoặc `configs/capture.toml`; hoặc audit thấy một khoảng trống chưa ghi.
- **Ai được chạm:** CHỈ cook trong một lần hotfix có tên (`/hs:fix` trên nhánh `claude/phase0a-capture`, hoặc trong Phase 7 — P7 khai các file này cho
  đúng trường hợp đó), sau khi owner đồng ý trong phiên; phạm vi chỉ `src/finsec_alpharadar/capture/**`, `configs/capture.toml` và
  `tests/test_capture_*.py`; test hồi quy RED→GREEN; regression gate đầy đủ; commit riêng `fix(capture): …`. Phase 4-6 không bao giờ chạm các file đó.
- **Triển khai:** owner `git -C C:\finsec\app fetch` + checkout SHA mới, khởi động lại task (outage `process_down` tự ghi), dán `laptop-head-<yyyyMMdd>.txt`
  vào `<backup-mirror>\ops\`.
- **Ghi lại:** một dòng MỚI có ngày trong AGENTS.md "capture window restarted <date> at <sha> (reason: <...>)" — dòng mới nhất thắng; KHÔNG BAO GIỜ sửa dòng
  cũ (AGENTS.md:37). Nếu nguyên nhân là khoảng trống chưa ghi, chuỗi 5 phiên đếm lại từ ngày giao dịch sau khoảng trống; mọi hotfix deploy SHA mới
  cũng làm chuỗi đếm lại, từ ngày giao dịch đầu tiên chạy SHA đó (VL-3, owner 2026-10-24): 5 phiên phải chạy trên cùng một phiên bản code đã deploy.
- **Lệnh dịch vụ:** luôn `sc.exe`, không bao giờ `sc` (alias `Set-Content` trong PowerShell 5.1).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Create | `docs/capture-runbook.md` |
| Modify | `configs/capture.toml` (giá trị cuối, trong commit P3 trước khi triển khai) |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261022-1147-phase0a-capture/research/capture-host-windows.md` |
| run | owner-run trên laptop: các khối lệnh của runbook; agent: `python -m finsec_alpharadar.capture audit` trên snapshot backup |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

`docs/code-standards.md` không đổi ở phase này (không quy ước code mới, không test mới).

## Implementation Steps
1. **Tests-before:** không có hành vi code mới. LOCK hiện có: `tests/test_agents_md_paths.py` (đường dẫn runbook backtick trong AGENTS.md phải tồn tại),
   `tests/test_capture_cli.py::test_shipped_config_loads_and_respects_d5` (config cuối).
2. Viết `docs/capture-runbook.md` (R3.2) và chốt `configs/capture.toml` (R3.8) — agent.
3. **Delta docs** (dưới) → **Regression gate** → **commit DUY NHẤT của P3** (LT4), ví dụ `docs: add the capture runbook and the final capture config` +
   trailer; push. Từ đây laptop chỉ clone SHA này; mọi thay đổi sau đi qua R3.9.
4. **BLOCKED (owner) OB2 — cấu hình laptop** (runbook mục 1-7). Phát hiện: `<backup-mirror>\ops\laptop-config-<yyyyMMdd>.txt` đạt R3.4. Chưa có → DỪNG.
5. **BLOCKED (owner) OB7 — thư mục backup + quyết định Autologon** (mục 6-7). Phát hiện: R3.5. Chưa có → DỪNG.
6. **BLOCKED (owner) OB4b — vnstock trên laptop** (mục 8-9). Phát hiện: R3.5. Chưa có → DỪNG.
7. **OWNER-RUN:** wrapper, dry run, tasks (mục 10-13). Phát hiện: `heartbeat.json` + `ops\vncapture-task.xml` trong `<backup-mirror>`.
8. **Burn-in ≥ 24 h** (R3.6) → agent chạy `audit` trên snapshot backup; cần đổi code/config → R3.9 rồi burn-in lại.
9. **Bắt đầu cửa sổ** (R3.7): kiểm lại hạn pause; ghi nhận ngày bắt đầu + SHA để commit phase đầu tiên sau khi cửa sổ mở (P4-P7) ghi vào AGENTS.md.
   Ghi `verification-P3.json` (burn-in đạt).

### Delta docs (commit P3, TRƯỚC khi triển khai; tiếng Anh; có ngày)
- **AGENTS.md**
  - "Decisions": D7 — `- <date> — Capture laptop account (owner, D7): a dedicated local standard user "capture" (password never expires) with Sysinternals Autologon (the password is stored as an LSA secret that an administrator can decrypt; accepted), the scheduled task runs as that user with logon type Password, vnstock is registered under that user, the cloud sync client runs in that session, and %USERPROFILE%\.vnstock is excluded from every synced folder.`;
    D4 — `- <date> — Phase 0 exit window (owner, D4): the poller runs 5 consecutive trading sessions on the laptop with a heartbeat each day and no unlogged gap; the laptop clone, venv and configs/capture.toml are frozen until the exit audit; a process restart on the same SHA is a logged outage, while deploying a new SHA (a hotfix) restarts the 5-session count; changes go only through the window-restart/hotfix procedure in docs/capture-runbook.md, and the newest dated "capture window started/restarted <date> at <sha>" line wins.`
  - "Current Status": "VN capture runbook and final config committed (<date>); laptop deployment per the runbook".
  - "Build & Verification Commands": **Capture deployment** — link `docs/capture-runbook.md`; lệnh audit trên snapshot backup.
- **docs/system-architecture.md**: Deployment — capture chạy trên laptop dưới user `capture` qua Task Scheduler (runbook); External dependencies — thư mục
  cloud sync (heartbeat + backup, D3); Boundaries & trust — Autologon (LSA secret), tài khoản cloud phụ, ACL của `C:\finsec` (capture chỉ RX).
- **docs/project-roadmap.md**: §7 0b.6 (dòng 394-398) "Runbook ready <date>".
- Ngày triển khai, ngày mở cửa sổ + SHA, và các sự thật OBSERVED của laptop (Q1/Q3/Q5/Q6/Q8, sleep states, w32time, Autologon, sync khi khoá, quota,
  cổng R3.3) → commit Phase 4 (P4 "Delta docs").

### Tests Before
- N/A — phase vận hành + tài liệu, không có hành vi code mới. LOCK ở bước 1.

### Implement
- Bước 2-3 (code/docs, trong commit) rồi bước 4-9 (vận hành, sau commit).

### Tests After
- [ ] `tests/test_agents_md_paths.py` xanh sau khi AGENTS.md backtick `docs/capture-runbook.md`; test D5 xanh với config cuối.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` MUST pass, 0 skipped (AGENTS.md đổi — test đường dẫn canh nó; KHÔNG phải N/A).
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` exit 0.
- Sau push: CI `success` trên 3.11 + 3.13.

## Success Criteria
- [ ] [manual] Bằng chứng laptop đạt R3.4 từng dòng (neo `manual_test_anchor.py`; file `laptop-config-<yyyyMMdd>.txt`).
- [ ] [manual] Owner thấy `heartbeat.json` mới (< 30 phút) trên điện thoại, status `OK` (neo `manual_test_anchor.py`).
- [ ] [test] `audit` burn-in exit 0 với `--days all --require-days 1` trên snapshot backup.
- [ ] [test] Cổng logon type R3.3: trên snapshot, ≥ 1 hàng `polls` với `source = 'vnstock_events'`, `status = 'ok'`, `run_mode = 'run'`, sau lần reboot
      có chủ đích (LT3).
- [ ] [invariant] Clone laptop sạch (`git -C C:\finsec\app status --porcelain` rỗng) và HEAD = SHA commit P3 (hoặc SHA hotfix mới nhất theo R3.9); ngày mở
      cửa sổ + SHA được giao cho commit phase đầu tiên sau khi cửa sổ mở (P4-P7) ghi (Phase 7 kiểm lại).
- [ ] [manual] Bằng chứng ACL/mật khẩu/Python (`icacls`, `net user capture`, `pyvenv.cfg`) đạt R3.4 (neo `manual_test_anchor.py`).
- [ ] [test] Regression gate xanh.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Modern Standby/DAM dừng tiến trình khi gập nắp | Trung bình × Cao | Hai lớp (powercfg + power request); burn-in 24 h gập nắp là cổng; fallback: để nắp mở / `monitor-timeout-ac 0` (research §1.3). |
| Windows Update khởi động lại máy trong cửa sổ | Trung bình × Trung bình | Active hours + pause; reboot vẫn tự ghi `process_down`; outage đã ghi được phép. |
| Sync client không chạy khi chưa đăng nhập | Trung bình × Trung bình | Autologon (D7); poller không phụ thuộc phiên (task Password); heartbeat cũ > 2 h trên điện thoại = báo động. |
| Logon type Password không mở được kho key vnstock | Thấp × Trung bình | Cổng R3.3; vnstock là nguồn PHỤ (D2) nên lỗi chỉ là outage đã ghi; fallback Interactive. |
| UDP 123 bị chặn ở mạng nhà → NTP không đồng bộ | Thấp × Trung bình | Stripchart + skew từ `Date` header vẫn đo được lệch; audit đòi trung vị |skew| ≤ 2 s. |
| Owner chậm cấu hình → cửa sổ trượt qua 18/12 | Trung bình × Trung bình | Bước owner nêu từ lúc duyệt plan; tuần sạch kéo dài tới 18/12; sau đó tránh 21/12-01/01 và chờ tuần 04-08/01/2027 (lịch 2027 còn nháp). |
| Quota cloud 5 GB đầy | Thấp × Thấp | Backup nén; heartbeat báo cỡ thư mục; ước lượng vài MB/ngày (P2 R2.11). |
| Mật khẩu `capture` hết hạn giữa cửa sổ (42 ngày — RT3) | Trung bình × Cao | `PasswordNeverExpires` + bằng chứng; sự cố 16 có lệnh cứu không gõ mật khẩu literal. |
| `capture` (hay mã chạy dưới nó) sửa venv/wrapper/clone (RT4) | Thấp × Cao | `C:\finsec` bỏ kế thừa, `capture` chỉ RX; `python -P`; hash canary + `git status` ở P7. |
| Vendor xoá wheel trước khi triển khai (RT10) | Trung bình × Cao | Cài chỉ từ bộ wheel đã tải ở P1 (`--no-index`); fallback venv chỉ-cổng. |
| Cửa sổ gãy giữa chừng, không có đường làm lại (LT2) | Trung bình × Trung bình | Thủ tục R3.9: dòng "window restarted" mới nhất thắng; hotfix có phạm vi và test. |
