# Research: chạy capture poller 24/7 trên laptop Windows 11 dự phòng (§9.4)

- Ngày: 2026-10-22. Người viết: hs-researcher (mode depth). Plan: `261022-1147-phase0a-capture` (phase 3 `capture-deploy`, ảnh hưởng phase 2 `capture-poller`).
- Câu hỏi trung tâm: cấu hình host và runtime nào để poller VN thu thập liên tục, có `first_seen_at` UTC đáng tin, và mọi khoảng trống đều có log + bằng chứng đối chiếu, với $0 trên một laptop Windows 11?
- Tiêu chí: (1) không ngủ/không mất tiến trình; (2) đồng hồ đo được; (3) phát hiện outage có bằng chứng độc lập; (4) dữ liệu bền và backup ra ngoài máy; (5) chi phí vận hành thấp cho một owner.
- Ngoài phạm vi: thiết kế nguồn dữ liệu (vnstock/portal), parser, Telegram (Phase 4), VPS.
- Nhãn: VERIFIED (URL, đọc ngày 2026-10-22), OBSERVED (lệnh chạy thật trên máy dev ngày 2026-10-22, KHÔNG phải laptop capture), ASSUMED (chưa kiểm), PRIOR (kiến thức huấn luyện, chưa kiểm lại).
- Máy dev: `Microsoft Windows 11 Home`, `10.0.26200.0` (OBSERVED, `Get-CimInstance Win32_OperatingSystem`). Laptop capture có thể khác; mọi OBSERVED dưới đây phải chạy lại trên laptop capture.
- Ghi chú vị trí file: hook PreToolUse chặn ghi vào main checkout (`H:\study\Project\FinSec\plans\...`) từ phiên worktree này, nên report nằm trong cây worktree, cạnh `alpaca-probe.md` và `edgar-header.md`.

## 0. Tóm tắt điều hành

1. **Rủi ro số 1 là Modern Standby, không phải S3 sleep.** Máy dev chỉ có `Standby (S0 Low Power Idle) Network Connected` + `Hibernate`; S1/S2/S3 "disabled when S0 low power idle is supported" (OBSERVED `powercfg -a`). Hôm nay máy dev đã log `Kernel-Power 506 "The system is entering Modern Standby | Reason: Idle Timeout"` lúc 11:00 và `507 ... Reason: Input Keyboard` lúc 11:36 ICT (OBSERVED). Microsoft: hệ thống vào modern standby "when the display turns off", kể cả do idle; pha DAM "suspends desktop applications"; session-0 services bị throttle "no more than one second of activity every 30 seconds"; "As of 24H2, additional session-0 services may be suspended" (VERIFIED https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/prepare-software-for-modern-standby). Không chuyển được sang S3: "Switching the power model is not supported in Windows without a complete OS re-install" (VERIFIED https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/modern-standby).
2. **Giảm thiểu khuyến nghị (c) = cả hai:** cấu hình power (standby/hibernate timeout AC = 0, `hibernate off`, lid = 0) **và** poller tự giữ power request `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`. Doc: power request "will block the NoCS phase indefinitely on AC power" (VERIFIED prepare-software-for-modern-standby). Nhưng power request "cannot be used to prevent the user from putting the computer to sleep ... close the lid ... power button" (VERIFIED https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate) — nên lid action 0 vẫn bắt buộc.
3. **W32Time trên máy workgroup mặc định KHÔNG chạy.** OBSERVED trên dev: `START_TYPE : 3 DEMAND_START`, trigger chỉ `DOMAIN JOINED STATUS` + một custom event; service Stopped. Microsoft KB 2385818 xác nhận đây là hành vi mặc định và cho 3 cách sửa (VERIFIED https://learn.microsoft.com/en-us/troubleshoot/windows-client/active-directory/w32time-not-start-on-workgroup).
4. **`w32tm /stripchart` đo được offset khi service đang Stopped**, không cần admin (OBSERVED: `+00.6556494s`, `+00.6796858s`, `+00.6813058s` vs `time.windows.com`). Đây là thước đo offset tốt nhất cho heartbeat (độ phân giải sub-ms). HTTP `Date` chỉ có độ phân giải 1 s, dùng làm kênh dự phòng/đối chiếu.
5. **OneDrive/Google Drive chỉ upload khi có phiên người dùng.** OneDrive sync app "starts automatically when users sign in to Windows" (VERIFIED https://learn.microsoft.com/en-us/sharepoint/use-group-policy); chưa tìm được nguồn Microsoft nói rõ "không chạy trong session 0" (chỉ PRIOR + forum). Hệ quả thiết kế: poller chạy không phụ thuộc đăng nhập; backup chỉ *copy vào* thư mục sync; upload cần auto-logon (đánh đổi bảo mật) hoặc owner đăng nhập sau mỗi reboot.
6. **Quota Google Drive không còn chắc là 15 GB:** "starting March 9, 2026, new Google Accounts get 5 GB of storage" (+10 GB nếu xác minh số điện thoại) (VERIFIED https://support.google.com/googleone/answer/9312312). OneDrive free 5 GB chỉ có nguồn thứ cấp.
7. **Windows Update đã restart máy dev lúc 01:40 ICT hôm nay**, ngoài active hours 6–21: `User32 1074 ... TrustedInstaller.exe ... has initiated the restart ... Operating System: Upgrade (Planned)` (OBSERVED). Active hours chỉ dời restart, không chặn; cho 5 phiên exit run nên **Pause updates** (tối đa 35 ngày, VERIFIED).

## 1. Power: không ngủ, không hibernate, lid = do nothing

### 1.1 Lệnh (PowerShell **elevated**; KHÔNG chạy `powercfg /a` từ Git Bash)

OBSERVED: trong Git Bash, `powercfg /a` trả `Invalid Parameters -- try "/?" for help` vì MSYS đổi `/a` thành đường dẫn; dùng PowerShell hoặc dạng `-a`.

```powershell
# 0. Ghi lại trạng thái trước khi đổi (bằng chứng cho runbook)
powercfg /getactivescheme          # dev: 381b4222-f694-41f0-9685-ff5bb260df2e (Balanced)
powercfg /a
powercfg /export C:\capture-ops\power-before.pow <ACTIVE_SCHEME_GUID>

# 1. Timeout trên AC (đơn vị phút; 0 = never)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10     # màn hình tắt được; xem 1.3
powercfg /change disk-timeout-ac 0

# 2. Tắt hibernate hoàn toàn (cũng xoá hiberfil.sys)
powercfg /hibernate off

# 3. Lid close action = 0 (Do Nothing) — setting ẩn, dùng GUID
powercfg /setacvalueindex <ACTIVE_SCHEME_GUID> 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
# khuyến nghị cả DC, để sự cố mất điện không làm máy ngủ khi đang gập nắp
powercfg /setdcvalueindex <ACTIVE_SCHEME_GUID> 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setactive <ACTIVE_SCHEME_GUID>

# 4. Kiểm
powercfg /a
powercfg /query <ACTIVE_SCHEME_GUID> 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936
powercfg /requests                 # cần admin (OBSERVED: "This command requires administrator privileges")
```

Nguồn và nhãn:
- `/change` nhận `standby-timeout-ac`, `hibernate-timeout-ac`, `monitor-timeout-ac`, `disk-timeout-ac`, "value, in minutes"; `/hibernate off`; `/availablesleepstates, /A`; `/requests` "Enumerates application and driver Power Requests"; `/setacvalueindex <scheme> <sub> <setting> <index>` (VERIFIED https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options). "0 = never": PRIOR (doc không nói rõ; kiểm bằng `powercfg /query` sau khi đặt).
- LIDACTION: GUID `5ca83367-6e45-459f-a27b-476b1d01c936`, "Hidden setting: Yes", index `0 Do Nothing / 1 Sleep / 2 Hibernate / 3 Shut Down` (VERIFIED https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/power-button-and-lid-settings-lid-switch-close-action).
- `SUB_BUTTONS = 4f971e89-eebd-4455-a8de-9e59040e7347`, `STANDBYIDLE = 29f6c1db-86da-48c5-9fdb-f2b67b1f44da`, `HIBERNATEIDLE = 9d7815a6-7ee4-497e-8888-515a05f02364` (OBSERVED `powercfg -aliases`). `LIDACTION` **không** có trong output aliases của máy dev (OBSERVED) → dùng GUID.
- Alias scheme trên máy dev: chỉ `SCHEME_MAX`, `SCHEME_MIN`, `SCHEME_BALANCED` (OBSERVED `powercfg -aliases | Select-String SCHEME`); help `powercfg -setacvalueindex /?` cũng chỉ nói `<SCHEME_GUID>` (OBSERVED). `SCHEME_CURRENT` không thấy → runbook dùng GUID từ `/getactivescheme`.
- Fast Startup: trên dev "This action is disabled in the current system policy" (OBSERVED). `hibernate off` cũng tắt Fast Startup: PRIOR.

### 1.2 Poller giữ power request (bắt buộc trên máy Modern Standby)

Hằng số (VERIFIED SetThreadExecutionState doc): `ES_CONTINUOUS = 0x80000000`, `ES_SYSTEM_REQUIRED = 0x00000001`, `ES_AWAYMODE_REQUIRED = 0x00000040` (chỉ cho media apps; doc khuyên không dùng trên portable). Doc: "backup agents, and network management applications must use both ES_SYSTEM_REQUIRED and ES_CONTINUOUS" (VERIFIED). Gọi một lần lúc khởi động trên thread chính của tiến trình sống lâu; gọi lại với chỉ `ES_CONTINUOUS` khi thoát. Dạng ctypes `ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)` là PRIOR, cần test thực; xác nhận bằng `powercfg /requests` (mục `SYSTEM:` phải liệt kê python.exe — ASSUMED về cách hiển thị).

Lưu ý: nếu chọn kiến trúc "task ngắn chạy lặp" (mục 4.3 B), power request chỉ tồn tại khi tiến trình đang chạy (ASSUMED: execution state gắn với thread, mất khi thread kết thúc) → giữa hai lần chạy không có power request. Khi đó chỉ còn `standby-timeout-ac 0` và kết quả probe 24 h (Q1).

### 1.3 Màn hình và nắp

- Doc: "A system enters modern standby when the display turns off ... the user closing the lid ... the system idling out" (VERIFIED). Pha NoCS "waits for the sleep timeout to expire" và chờ power requests; power requests "block the NoCS phase indefinitely on AC power" (VERIFIED). Suy luận: màn hình tắt *mở* phiên MS nhưng không đi tới DAM/low-power nếu sleep timeout = never hoặc có power request — ASSUMED cho tới khi probe Q1.
- Vì vậy 506 lúc màn hình tắt KHÔNG đồng nghĩa poller bị dừng → không coi 506/507 là outage trực tiếp; log poll của chính poller là nguồn sự thật, 506/507 là bằng chứng phụ.
- Nếu Q1 cho thấy poller bị suspend khi màn hình tắt dù đã có power request: đặt `monitor-timeout-ac 0` và để nắp mở (màn hình bật 24/7; đánh đổi độ bền panel/nhiệt), hoặc cắm màn hình ngoài. ASSUMED.

### 1.4 Pin: giới hạn sạc

Windows 11 không có giới hạn sạc tổng quát; tính năng do OEM (Lenovo Vantage threshold/"Conservation Mode", Dell Power Manager "Primarily AC Use", MyASUS, HP BIOS). Chỉ có nguồn thứ cấp (https://www.digitalcitizen.life/how-can-i-limit-battery-charge-to-80-on-windows-11/, https://www.how2shout.com/how-to/stop-laptop-charging-at-80-windows-11.html) → ASSUMED, owner tự kiểm theo model (Q7). Pin còn là UPS cho mất điện ngắn — không tháo pin; scheduled task phải cho chạy trên pin (mục 4).

Máy dev: `EstimatedChargeRemaining : 100`, `BatteryStatus : 2` (OBSERVED; `2` = đang cắm AC theo PRIOR về Win32_Battery).

## 2. Windows Update: active hours và tránh restart trong khung 08:00–17:30 ICT

| Cơ chế | Nội dung | Nhãn |
|---|---|---|
| Active hours mặc định | "By default, active hours are from 8 AM to 5 PM on PCs" — 17:00 < 17:30, **không đủ** | VERIFIED https://learn.microsoft.com/en-us/windows/deployment/update/waas-restart |
| Độ dài tối đa | "Later versions support max active hours length of 18 hours" | VERIFIED (cùng URL) |
| UI | Settings > Windows Update > Advanced options > Active hours | VERIFIED (cùng URL, mục Tip) |
| Registry do UI ghi | `HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings` `ActiveHoursStart=6`, `ActiveHoursEnd=21` trên dev | OBSERVED |
| Registry policy | `HKLM\Software\Policies\Microsoft\Windows\WindowsUpdate` `SetActiveHours=1`, `ActiveHoursStart`, `ActiveHoursEnd` (0–23); doc: "Directly editing the Windows registry isn't recommended" | VERIFIED waas-restart |
| `NoAutoRebootWithLoggedOnUsers` (+`AUOptions=4`) | "doesn't work exactly as per description", "can result in no quality update reboots period" | VERIFIED waas-restart |
| Pause updates | "up to 35 days"; "Your device won't restart automatically to finish installation of the updates until the pause ends" | VERIFIED https://support.microsoft.com/en-us/windows/manage-updates-in-windows-643e9ea7-3cf6-7da6-a25c-95d4f7f099fe |
| Win11 Home có tôn trọng key `Policies` | Home không có gpedit; key policy có hiệu lực hay không | PRIOR/ASSUMED |

Bằng chứng thực: máy dev (active hours 6–21) bị `TrustedInstaller.exe` restart lúc 2026-10-22T01:40:52+07:00, `Reason Code: 0x80020003`, `Shutdown Type: restart` (OBSERVED `Get-WinEvent ... User32 1074`). Active hours hoạt động đúng thiết kế (restart ngoài khung) — nhưng vẫn là một gap trong chế độ poll hằng giờ.

Khuyến nghị xếp hạng:
1. **Active hours 07:00–19:00** qua Settings (UI, không đụng policy) — bao trùm 08:00–17:30 với biên 1 h mỗi đầu; tắt chế độ tự điều chỉnh active hours (tên UI là PRIOR).
2. **Pause updates 7–14 ngày ngay trước exit run 5 phiên**, resume vào cuối tuần để restart rơi vào ngày không giao dịch (VN không giao dịch cuối tuần — PRIOR). Mỗi restart vẫn phải được log là outage.
3. Không dùng `NoAutoRebootWithLoggedOnUsers` (Microsoft tự cảnh báo).

## 3. Thời gian: W32Time + đo offset độc lập

### 3.1 Trạng thái mặc định (OBSERVED trên dev; chạy lại trên laptop capture)

- `sc.exe qc w32time` → `START_TYPE : 3 DEMAND_START`, `SERVICE_START_NAME : NT AUTHORITY\LocalService`.
- `sc.exe qtriggerinfo w32time` → `START SERVICE DOMAIN JOINED STATUS : 1ce20aba-... [DOMAIN JOINED]` + `CUSTOM SYSTEM STATE CHANGE EVENT : 2d7a2816-0c5e-45fc-9ce7-570e5ecde9c9`.
- Registry: `NtpServer : time.windows.com,0x9`, `Type : NTP`; `Config\MinPollInterval 10`, `MaxPollInterval 15`, `MaxPosPhaseCorrection 54000`, `MaxNegPhaseCorrection 54000`, `MaxAllowedPhaseOffset 1`, `UpdateInterval 360000`; `NtpClient\SpecialPollInterval 16384` (doc nói default standalone là 604,800 — máy dev khác doc).
- Task `\Microsoft\Windows\Time Synchronization\SynchronizeTime` và `ForceSynchronizeTime`: `Ready`.
- `Kernel-General 1` mới nhất: "The system time has changed ... Time Delta: 0 ms ... Process: '...\svchost.exe'" lúc 2026-10-21T20:47:20+07:00 → đồng bộ có xảy ra thỉnh thoảng dù service hiện Stopped (qua task/trigger), nhưng không liên tục.

Ý nghĩa (VERIFIED https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings):
- `MaxPos/NegPhaseCorrection` 54,000 s (15 h) cho standalone: lệch lớn hơn thì service "logs an event instead" — không tự sửa.
- `SpecialPollInterval`: "Starting with build 1703, SpecialPollInterval is contained by the MinPollInterval and MaxPollInterval" (log2 giây) → với Min=10, giá trị < 1024 s bị kẹp về 1024 s.
- Cờ peer: `0x1 SpecialInterval`, `0x2 UseAsFallbackOnly`, `0x4 SymmetricActive`, `0x8 Client`; "we recommend that you prepare three or more time servers".
- `MaxAllowedPhaseOffset` standalone = 1 s: lệch > 1 s thì đặt đồng hồ ngay (step) thay vì chỉnh dần — sẽ thấy như Kernel-General 1 với Time Delta lớn.

### 3.2 Lệnh cấu hình (PowerShell elevated)

```powershell
# Method 1 của KB 2385818: bỏ trigger mặc định + Automatic
sc.exe triggerinfo w32time delete
sc.exe config w32time start= auto
net start w32time

# Peer list: 3 nguồn chính + 1 fallback (pool khuyên <= 4 server)
w32tm /config /manualpeerlist:"0.vn.pool.ntp.org,0x9 0.asia.pool.ntp.org,0x9 1.asia.pool.ntp.org,0x9 time.windows.com,0xb" /syncfromflags:manual /update

# Poll 1024 s (= 2^10, nằm trong [Min,Max] mặc định, không phải đổi Config)
reg add HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient /v SpecialPollInterval /t REG_DWORD /d 1024 /f
w32tm /config /update
w32tm /resync /rediscover

# Kiểm
sc.exe qc w32time
sc.exe qtriggerinfo w32time
w32tm /query /status /verbose
w32tm /query /peers
w32tm /query /configuration
```

- KB: Method 1 `sc triggerinfo w32time delete` "to delete the trigger event ... and to change the Startup Type ... from Manual to Automatic"; Method 2 `sc triggerinfo w32time start/networkon stop/networkoff`; Method 3 Automatic (Delayed Start) + disable task SynchronizeTime (VERIFIED URL KB ở trên). Chọn Method 1: đơn giản nhất, không phụ thuộc trạng thái mạng.
- `/config /manualpeerlist /syncfromflags:manual /update`, `/resync [/rediscover]`, `/query /status /verbose` (VERIFIED W32Time tools doc).
- `vn.pool.ntp.org`: "There are 5 active servers in this zone" (IPv4), 9 IPv6; trang zone khuyên `0.vn.pool.ntp.org`, `1.asia.pool.ntp.org`, `0.asia.pool.ntp.org` (VERIFIED https://www.ntppool.org/zone/vn). Pool: dùng "no more than four time servers", tránh "tricks with burst or minpoll" (VERIFIED https://www.ntppool.org/en/use.html). Zone VN mỏng → không dựa một mình vào `vn`.
- `0xb` = `0x8|0x2|0x1` (Client + UseAsFallbackOnly + SpecialInterval) cho `time.windows.com`, để peer fallback cũng theo `SpecialPollInterval`; `0xa` (bỏ `0x1`) sẽ không theo poll 1024 s. Cờ VERIFIED, tổ hợp là suy luận.
- Trường dùng được của `w32tm /query /status /verbose`: `Leap Indicator`, `Stratum`, `Source`, `Last Successful Sync Time`, `Poll Interval`, `Phase Offset`, `State Machine`, `Last Sync Error`, `Time since Last Good Sync Time` (VERIFIED mẫu output trong doc). Main session đã OBSERVED lệnh này fail `0x80070426` khi service Stopped.

### 3.3 Đo offset không cần W32Time

| Phương pháp | Độ phân giải | Phụ thuộc | Bằng chứng | Xếp hạng |
|---|---|---|---|---|
| `w32tm /stripchart /computer:<ntp> /dataonly /samples:N` | sub-ms (7 chữ số thập phân) | UDP 123 ra ngoài; chạy khi service Stopped, không cần admin | OBSERVED `Tracking time.windows.com [52.148.114.188:123]` → `+00.6556494s`, `+00.6796858s`, `+00.6813058s`; doc: "uses an ephemeral UDP source port ... to prevent conflicts with the built-in NTP client"; `/rdtsc` in CSV có `RoundtripDelay`, `NtpOffset` (VERIFIED W32Time tools doc) | 1 |
| HTTP `Date` của chính các response đang poll | 1 s (IMF-fixdate, vd `Tue, 15 Nov 1994 08:12:31 GMT`) | không thêm request; server phải có clock; response từ cache cần cộng `Age` | RFC 9110 §6.6.1: "best available approximation of the date and time of message generation"; "An origin server with a clock ... MUST generate a Date header field in all 2xx (Successful), 3xx (Redirection), and 4xx (Client Error) responses" (VERIFIED https://www.rfc-editor.org/rfc/rfc9110.html#name-date) | 2 |
| `Kernel-General 1` "Time Delta" | ms | Event Log | OBSERVED | phát hiện step, không đo offset |

Probe HTTP `Date` (OBSERVED, script `date_skew.py` trong scratchpad, 1 HEAD/host, UA trung tính): `local_mid - server` = `-0.191s` (hsx.vn, rtt 2.930 s), `-0.575s` (google.com, rtt 0.299 s), `-0.171s` (cloudflare.com, rtt 0.508 s), `-0.480s` (learn.microsoft.com, rtt 0.873 s). Vì `Date` bị làm tròn xuống giây, offset thật nằm trong `[d-1, d]` ± rtt/2 → đồng hồ local chậm khoảng 0.2–1.5 s. Stripchart `+0.66 s` nhất quán nếu dấu stripchart là `server − local` (dấu suy ra từ hai phép đo khớp nhau → ASSUMED; Q4 xác nhận).

Khuyến nghị: task ops chạy stripchart 3 mẫu mỗi giờ và ghi offset vào ops log (heartbeat hằng ngày ở mục 10 chỉ tóm tắt các mẫu này; "không cần admin" có bằng chứng: cùng phiên, `powercfg -requests` báo cần admin còn stripchart chạy được); poller ghi `http_date` + `rtt` của mọi response vào dòng quan sát (rẻ, không thêm request) để có chuỗi offset liên tục độ phân giải 1 s. Ngưỡng đề xuất cho plan (không phải yêu cầu roadmap): |offset| > 2 s → cảnh báo; > 60 s → các record trong cửa sổ đó mang cờ chất lượng đồng hồ.

## 4. Task Scheduler: chạy khi khởi động, không cần đăng nhập, tự hồi phục

### 4.1 Tham số đã kiểm (VERIFIED Microsoft Learn)

- `New-ScheduledTaskTrigger -AtStartup`; `-Once -At <t> -RepetitionInterval <ts> [-RepetitionDuration <ts>]` (https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger). Bỏ `-RepetitionDuration` = lặp vô hạn: PRIOR (doc không nói) → kiểm bằng `Export-ScheduledTask`.
- `New-ScheduledTaskSettingsSet`: `-AllowStartIfOnBatteries`, `-DontStopIfGoingOnBatteries`, `-ExecutionTimeLimit`, `-RestartCount`, `-RestartInterval`, `-StartWhenAvailable`, `-MultipleInstances {Parallel|Queue|IgnoreNew}`, `-Priority` (0–10, mặc định 7); không đặt `ExecutionTimeLimit` thì dùng "default of three days" (https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset).
- `ExecutionTimeLimit`: "A value of PT0S will enable the task to run indefinitely" (https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-executiontimelimit-settingstype-element). PowerShell `-ExecutionTimeLimit ([TimeSpan]::Zero)` ánh xạ sang PT0S: PRIOR.
- `RestartOnFailure`: "attempt to restart the task if the task fails for any reason"; `Interval` "maximum time allowed is 31 days, and the minimum time allowed is 1 minute"; `Count` kiểu `unsignedByte` (≤ 255) (https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-restartonfailure-settingstype-element, https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-interval-restarttype-element). Doc không định nghĩa "fail"; việc nó không kích hoạt khi tiến trình *treo* (không thoát) là PRIOR.
- LogonType (https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype): `S4U` "no password is stored by the system and there is no access to either the network or encrypted files"; `PASSWORD` "The password must be supplied at registration time"; `INTERACTIVE_TOKEN` "The task will be run only in an existing interactive session"; `SERVICE_ACCOUNT` "Local System, Local Service, or Network Service". Principal cho phép "run the task regardless of whether that account is logged on" (https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal).

### 4.2 Lựa chọn principal (xếp hạng)

| # | Principal | Ưu | Nhược | Rủi ro |
|---|---|---|---|---|
| 1 | `LOCALSERVICE` hoặc `SYSTEM`, `-LogonType ServiceAccount`, cho poller + heartbeat; user (auto-logon) chỉ chạy OneDrive/Drive client | không lưu mật khẩu cho task; chạy trước khi ai đăng nhập; không vướng giới hạn S4U | data dir và thư mục backup cần ACL cho account đó; SYSTEM có quyền rất rộng (chạy code repo với quyền SYSTEM) | `LOCALSERVICE` hẹp hơn nhưng phải kiểm quyền file + mạng (Q3) |
| 2 | Mọi thứ trong user auto-logon, `-LogonType Interactive` | đơn giản, cùng session với OneDrive | phụ thuộc auto-logon; auto-logon lỗi → không capture | DAM "pause all desktop applications" (VERIFIED) |
| 3 | User `-LogonType Password` | quen thuộc ("Run whether user is logged on or not") | lưu mật khẩu; đổi mật khẩu MSA làm task hỏng | — |
| 4 | User `S4U` | không lưu mật khẩu | doc: "no access to ... the network"; chặn cả HTTPS ra Internet hay chỉ tài nguyên mạng cần credential: PRIOR (tin là cái sau), chưa kiểm | Q3 |

Khuyến nghị: **#1, ưu tiên `LOCALSERVICE` nếu Q3 cho thấy đủ quyền, ngược lại `SYSTEM`.** Lý do: capture không được phụ thuộc phiên đăng nhập; tiến trình session-0 bị DAM nhẹ tay hơn desktop app (VERIFIED: desktop apps bị dừng, session-0 services bị throttle). Lỗ hổng: doc không nói tiến trình do Task Scheduler chạy trong session 0 được DAM xếp là "service" (throttle) hay bị suspend → Q1.

### 4.3 Kiến trúc chạy (xếp hạng)

| Phương án | Mô tả | Ưu | Nhược |
|---|---|---|---|
| **A (khuyến nghị)** Tiến trình sống lâu + trigger watchdog | 1 task, 2 trigger: `-AtStartup` và `-Once -At <now> -RepetitionInterval 00:05:00`; `-MultipleInstances IgnoreNew`; `-ExecutionTimeLimit PT0S`; `-RestartCount 255 -RestartInterval 00:01:00`; poller giữ lock file đơn-instance và power request; tự đổi nhịp 2–5' (08:00–17:30 ICT ngày giao dịch) / 60' (ngoài khung) | power request liên tục; process chết được hồi sinh ≤ 5'; lịch nằm trong code (test được) | process treo không bị giết → cần watchdog nội bộ (thread kiểm "last loop age", tự `os._exit` nếu > N phút để trigger kế tiếp khởi động lại) |
| B Task ngắn lặp | mỗi lần chạy = 1 vòng poll rồi thoát; `-RepetitionInterval` 2–5'; `-StartWhenAvailable` | không treo lâu; lịch sử Task Scheduler là outage record miễn phí | không có power request giữa các lần chạy; khởi động Python/venv mỗi 2'; đổi nhịp theo khung giờ cần 2 task |

Lệnh mẫu cho A (PowerShell elevated; đường dẫn và module là ví dụ — `finsec_alpharadar.capture.run` chưa tồn tại, ASSUMED):

```powershell
$act = New-ScheduledTaskAction -Execute 'C:\finsec\app\.venv\Scripts\python.exe' `
        -Argument '-m finsec_alpharadar.capture.run --data-dir D:\finsec-capture' -WorkingDirectory 'C:\finsec\app'
$trg = @( (New-ScheduledTaskTrigger -AtStartup),
          (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)) )
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 255 -RestartInterval (New-TimeSpan -Minutes 1)
$pri = New-ScheduledTaskPrincipal -UserId 'LOCALSERVICE' -LogonType ServiceAccount   # hoặc 'SYSTEM' sau Q3
Register-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' -Action $act -Trigger $trg -Settings $set -Principal $pri
Export-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' > C:\capture-ops\vncapture-task.xml   # kiểm PT0S, Repetition, IgnoreNew
```

Task thứ hai `\FinSec\VNCaptureNightly` (Daily 02:30 ICT): rotate/backup/heartbeat; cùng settings trừ `ExecutionTimeLimit` 1 h và không lặp.

## 5. OneDrive / Google Drive: phiên tương tác, auto-logon, quota

### 5.1 Phiên người dùng

- OneDrive: "If you disable or don't configure this setting, the OneDrive sync app starts automatically when users sign in to Windows"; policy `EnableAutoStart` "ensuring OneDrive will automatically start every time they sign in to Windows"; nhiều policy nằm dưới `HKCU` (VERIFIED https://learn.microsoft.com/en-us/sharepoint/use-group-policy). Không tìm thấy tài liệu Microsoft cho chạy sync app như service; một reply forum: "runs in an Users Context" (thứ cấp, https://techcommunity.microsoft.com/discussions/onedriveforbusiness/onedrive-sync-client-on-remote-desktop-session/41652). Kết luận "chỉ sync khi có phiên đăng nhập" = 1 nguồn chính gián tiếp + 1 forum + PRIOR → đủ để thiết kế phòng thủ, chưa đủ gọi VERIFIED.
- Google Drive for desktop chạy per-user tương tự: PRIOR (không tìm nguồn).
- Màn hình khoá vẫn là phiên tương tác, sync vẫn chạy: PRIOR (Q6).

### 5.2 Auto-logon (Sysinternals Autologon hoặc netplwiz)

- Autologon: credentials "encrypted in the Registry"; "Although the password is encrypted in the registry as an LSA secret, a user with administrative rights can easily retrieve and decrypt it"; "Autologon does not verify the submitted credentials"; giữ Shift khi boot để bỏ qua; CLI `autologon user domain password` (VERIFIED https://learn.microsoft.com/en-us/sysinternals/downloads/autologon). CLI để mật khẩu trong lịch sử shell → dùng GUI.
- Với tài khoản Microsoft bật chế độ chỉ cho Windows Hello sign-in, auto-logon có thể không hoạt động: PRIOR/ASSUMED.
- Đánh đổi: ai cầm laptop là vào được desktop (và OneDrive) của tài khoản đó. Giảm thiểu: tài khoản local riêng `capture` (không phải MSA chính của owner); OneDrive/Drive đăng nhập bằng MSA/Google phụ chỉ dùng cho backup; task AtLogOn `rundll32.exe user32.dll,LockWorkStation` để khoá ngay sau auto-logon (PRIOR); Device Encryption/BitLocker nếu máy hỗ trợ (ASSUMED trên Home); không để `.env`/API key trong thư mục sync.
- Phương án không auto-logon: owner đăng nhập tay sau mỗi reboot; task ServiceAccount vẫn copy backup vào thư mục sync, chỉ upload chậm. Heartbeat cũ trên thiết bị khác của owner = báo động (dead-man's switch). Chấp nhận được cho 5 phiên khi đã Pause updates.

Khuyến nghị: tài khoản local `capture` + Autologon GUI + khoá màn hình lúc logon + tài khoản cloud phụ. Nếu owner không chấp nhận rủi ro LSA secret → bỏ auto-logon, chấp nhận upload trễ sau reboot.

### 5.3 Quota

| Dịch vụ | Free | Nguồn | Nhãn |
|---|---|---|---|
| Google (Drive+Gmail+Photos dùng chung) | "up to 15 GB"; tài khoản tạo từ 2026-03-09: 5 GB, +10 GB khi xác minh số điện thoại; vượt quota: "can't upload new files" | https://support.google.com/googleone/answer/9312312 | VERIFIED |
| OneDrive (MSA) | 5 GB | https://www.ionos.com/digitalguide/server/tools/what-is-onedrive, https://www.computerworld.com/article/3095451/microsoft-downsizes-onedrive-accounts-enforces-restrictions.html | thứ cấp; trang Microsoft compare-plans timeout khi fetch |

Hệ quả: thiết kế retention theo 5 GB cho cả hai; backup nén (gzip JSONL); giữ snapshot SQLite N đêm gần nhất + mọi JSONL.gz bất biến; heartbeat báo tổng dung lượng thư mục backup. Dung lượng phụ thuộc thiết kế poller (phase 2): khuyến nghị lưu payload đầy đủ chỉ khi `raw_sha256` mới; mọi poll khác chỉ ghi một dòng quan sát (`source`, `source_id`, `raw_sha256`, `first_seen_at`, `http_date`, `rtt`).

## 6. Phát hiện sleep/resume/shutdown từ Event Log (đối chiếu outage)

OBSERVED trên máy dev (bản ghi mới nhất mỗi loại, `Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName=...; Id=...} -MaxEvents 1`):

| Provider / ID | Ý nghĩa | Mẫu OBSERVED (giờ ICT) | Nguồn thêm |
|---|---|---|---|
| `Microsoft-Windows-Kernel-Power` 41 | reboot không shutdown sạch; có `BugcheckCode`, `SleepInProgress`, `PowerButtonTimestamp` | 2026-09-05T01:18:04 "The system has rebooted without cleanly shutting down first..." | VERIFIED https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/event-id-41-restart |
| Kernel-Power 42 / 107 | vào sleep / resume | 42 "The system is entering sleep. Sleep Reason: Battery" 2026-10-09T05:33:04; 107 "The system has resumed from sleep." 05:33:06 | thứ cấp https://www.itechguides.com/?p=423455 |
| Kernel-Power 506 / 507 | vào / ra Modern Standby, có `Reason` | 506 "Reason: Idle Timeout" 2026-10-22T11:00:15; 507 "Reason: Input Keyboard" 11:36:02 | chỉ OBSERVED |
| `Microsoft-Windows-Power-Troubleshooter` 1 | "The system has returned from a low power state", có **Sleep Time / Wake Time UTC** | Sleep Time `2026-10-08T22:33:01.868773500Z`, Wake Time `2026-10-09T01:59:45.315967200Z`, Wake Source Unknown | chỉ OBSERVED |
| `EventLog` 6005 / 6006 / 6008 | log service start / stop sạch / shutdown trước đó bất thường | 6006 01:40:53, 6005 01:41:18 (2026-10-22); 6008 "The previous system shutdown at 12:09:59 AM on 9/5/2026 was unexpected." | VERIFIED event-id-41 doc ("6006 ... adequately turned off", "6008 ... improper or dirty shutdown") |
| `User32` 1074 | process/user khởi tạo shutdown/restart, có reason | TrustedInstaller restart "Operating System: Upgrade (Planned)" 2026-10-22T01:40:52 | VERIFIED event-id-41 doc |
| `Microsoft-Windows-Kernel-General` 12 / 13 | OS start / shutting down, **có system time UTC** | 12 "started at system time 2026-10-21T18:41:06.500000000Z"; 13 "shutting down at system time 2026-10-21T18:40:59.393386100Z" | chỉ OBSERVED |
| Kernel-General 1 | system time changed, có `Time Delta`, process | "Time Delta: 0 ms ... svchost.exe" | chỉ OBSERVED |

Lưu ý: event-id-41 doc: "The time shown in the .evtx file is adjusted to your system's time" (VERIFIED) → chuẩn hoá `TimeCreated.ToUniversalTime()`. `Message` chứa ký tự điều khiển hướng chữ (OBSERVED hiển thị `?2026?-?10?`) → parse từ `$e.ToXml()`/`Properties`, không parse `Message` (giải pháp PRIOR). Máy Modern Standby log 506/507, máy S3 log 42/107 — exporter phải lấy cả hai bộ.

Lệnh export hằng ngày (cú pháp PRIOR; biến thể tương tự đã chạy thành công — OBSERVED):

```powershell
$since = (Get-Date).AddDays(-1)
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since; Id=41,42,107,506,507,1,12,13,1074,6005,6006,6008} -ErrorAction SilentlyContinue |
  Where-Object { $_.ProviderName -in 'Microsoft-Windows-Kernel-Power','Microsoft-Windows-Power-Troubleshooter','EventLog','User32','Microsoft-Windows-Kernel-General' } |
  Select-Object @{n='utc';e={$_.TimeCreated.ToUniversalTime().ToString('o')}}, ProviderName, Id, @{n='xml';e={$_.ToXml()}} |
  ConvertTo-Json -Compress | Out-File -Encoding utf8 "D:\finsec-capture\ops\eventlog-$(Get-Date -Format yyyyMMdd).json"
```

(Id 1 trùng giữa nhiều provider; lọc `ProviderName` xử lý.)

## 7. Python trên Windows: đồng hồ, độ bền ghi, SQLite, vị trí dữ liệu

### 7.1 Đồng hồ

- `time.time()` trên Windows gọi `GetSystemTimePreciseAsFileTime()` (từ 3.13); "can return a lower value than a previous call if the system clock has been set back" (VERIFIED https://docs.python.org/3.13/library/time.html). → `first_seen_at` = `datetime.now(timezone.utc)` (wall clock, UTC, tz-aware) — đúng hợp đồng PIT; không bao giờ suy ra từ monotonic.
- `time.monotonic()` trên Windows gọi `QueryPerformanceCounter()` (VERIFIED Python 3.13 doc). QPC "returns the total number of ticks that have occurred since the Windows operating system was started, including the time when the machine was in a sleep state such as standby, hibernate, or connected standby" (VERIFIED https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps). Python doc không tự cam kết điều này → hành vi ở tầng Python là "cần probe" (Q8). Hệ quả: không phát hiện sleep bằng hiệu (wall − monotonic); phát hiện gap bằng "khoảng giữa hai vòng loop theo monotonic > ngưỡng", phát hiện **clock step** bằng |Δwall − Δmonotonic| > ngưỡng. Clock của Python 3.11/3.12 trên Windows chưa kiểm → ghim Python 3.13 trên laptop capture.
- Lịch ICT: `zoneinfo.ZoneInfo('Asia/Ho_Chi_Minh')` cần `tzdata` trên Windows; main session đã OBSERVED `.venv` thiếu tzdata. Wheel vnstock 5.1.0 đã khai `Requires-Dist: tzdata ; sys_platform == 'win32'` (main session OBSERVED METADATA) nên tzdata không phải dependency mới, nhưng vẫn ghim tường minh trong extra của capture, hoặc dùng offset cố định UTC+7 (VN không có DST — PRIOR).

### 7.2 Ghi JSONL append-only bền

- `os.fsync` trên Windows gọi `_commit()`; "first do f.flush(), and then do os.fsync(f.fileno())" (VERIFIED https://docs.python.org/3.13/library/os.html). `_commit` "forces the operating system to write the file associated with fd to disk" (VERIFIED https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/commit). `FlushFileBuffers` "can be inefficient when used after every write" (VERIFIED https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers). Thiết bị lưu trữ có thể báo đã sync trước khi ghi thật (VERIFIED https://sqlite.org/howtocorrupt.html §3.1).
- Khuyến nghị: một tiến trình ghi duy nhất (lock file đơn-instance); mở `"ab"` (binary, tránh dịch CRLF); mỗi dòng = một JSON hoàn chỉnh + `\n`, UTF-8 (NFC giữ dấu tiếng Việt); `flush()+fsync()` **mỗi vòng poll** (2–5'), không mỗi dòng; rotate theo ngày UTC (`raw-YYYYMMDD.jsonl`) để file hôm qua bất biến trước backup; khi khởi động, kiểm dòng cuối — không parse được (ghi dở do mất điện) thì cắt bỏ và ghi vào outage log (thiết kế; PRIOR rằng mất điện chỉ làm hỏng phần đuôi).
- Tính `raw_sha256` trên **bytes** payload gốc trước khi decode.

### 7.3 SQLite WAL (state + outage log)

- "WAL does not work over a network filesystem"; với `synchronous=NORMAL` trong WAL "transactions are no longer durable and might rollback following a power failure or hard reset"; FULL sync WAL mỗi commit; WAL file "should be kept with the database if the database is copied or moved" (VERIFIED https://sqlite.org/wal.html). → `PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;` cho outage log/state (ghi ít, cần bền).
- Backup: không copy file DB đang có transaction; dùng backup API hoặc `VACUUM INTO` (VERIFIED howtocorrupt §1.2). Python `sqlite3.Connection.backup()`: PRIOR.
- DB live trong thư mục OneDrive/Drive: sqlite docs không nói về sync client → rủi ro khoá file/upload snapshot dở là ASSUMED. **Không đặt DB/JSONL live trong thư mục sync.**

### 7.4 Vị trí dữ liệu

- Ngoài git clone, ngoài thư mục sync, trên NTFS local: vd `D:\finsec-capture\{raw,state,ops,outbox}`. Lý do: `git clean -xfd` xoá cả file bị ignore (PRIOR); clone/worktree mới không thấy dữ liệu; Known Folder Move có thể kéo `Documents`/`Desktop` vào OneDrive (PRIOR) → không đặt clone hay dữ liệu dưới `Documents`. Ngoài clone thoả ràng buộc "dữ liệu nằm ở đường dẫn bị gitignore" mạnh hơn: không thể bị commit, và `git clean -xfd` không chạm tới. Nếu plan vẫn muốn đường dẫn trong repo, dùng `/data/` (đã anchor, AGENTS.md 2026-10-22) và chấp nhận rủi ro `git clean`.
- Backup đích: `%USERPROFILE%\OneDrive\FinSecBackup\` (hoặc thư mục Google Drive), chỉ chứa file bất biến: `raw-YYYYMMDD.jsonl.gz`, `state-YYYYMMDD.sqlite` (từ `VACUUM INTO`), `manifest-YYYYMMDD.json` (sha256 từng file), `heartbeat-YYYYMMDD.json`.
- Ghi file tạm rồi `os.replace` trên cùng volume để sync client không upload file dở (atomic rename: PRIOR).
- ACL: account của task (mục 4.2) phải ghi được thư mục backup trong profile user (Q3).

## 8. Ma trận đánh đổi tổng hợp

| Quyết định | Chọn | Thay thế | Độ tin cậy | Độ phức tạp | Rủi ro áp dụng |
|---|---|---|---|---|---|
| Chống ngủ | powercfg + power request trong poller | chỉ powercfg | cao (2 lớp) | thấp | Modern Standby + DAM chưa probe (Q1) |
| Principal | LOCALSERVICE/SYSTEM ServiceAccount | user Interactive/Password/S4U | cao, độc lập đăng nhập | trung bình (ACL) | quyền SYSTEM rộng |
| Kiến trúc | sống lâu + trigger lặp 5' `IgnoreNew` + watchdog nội bộ | task ngắn lặp | cao, giữ power request | trung bình | treo → cần watchdog |
| Thời gian | W32Time auto + 4 peer + poll 1024 s; đo bằng stripchart + HTTP `Date` | chỉ HTTP `Date` | đo sub-ms | thấp | UDP 123 bị chặn ở mạng nhà (Q4) |
| Update | active hours 07–19 + Pause trước exit run | policy registry | đủ cho 5 phiên | thấp | quên resume → update dồn |
| Backup | file bất biến + manifest vào thư mục sync; auto-logon tài khoản local | không auto-logon | trung bình (phụ thuộc phiên) | trung bình | LSA secret; quota 5 GB |

## 9. Runbook outline cho `docs/capture-runbook.md`

1. **Tiền điều kiện (owner, chặn):** laptop Win11 cắm điện, Ethernet nếu có; tài khoản local admin; Python 3.13 x64; repo clone tại `C:\finsec\app` (không dưới Documents/OneDrive); `.venv` + `pip install -e .` + `tzdata`; vnstock Community đã `vnstock register` (ngoài phạm vi report này); OneDrive/Drive client đăng nhập bằng tài khoản cloud phụ.
2. **Baseline:** `powercfg /a`, `/getactivescheme`, `/export`; `sc.exe qc w32time`, `sc.exe qtriggerinfo w32time`; `w32tm /stripchart /computer:time.windows.com /dataonly /samples:5`; `Get-WinEvent` 30 ngày cho bảng mục 6 → `C:\capture-ops\baseline-YYYYMMDD\`.
3. **Power:** lệnh mục 1.1; giới hạn sạc OEM nếu có; kiểm lại `powercfg /query`.
4. **Windows Update:** active hours 07:00–19:00; Pause 7–14 ngày trước exit run; ghi ngày resume.
5. **Thời gian:** lệnh mục 3.2; kiểm `w32tm /query /status /verbose` có `Last Sync Error: 0` và `Source` là pool/time.windows.com.
6. **Thư mục dữ liệu:** tạo `D:\finsec-capture\...` + ACL; tạo `OneDrive\FinSecBackup`.
7. **Task:** đăng ký `\FinSec\VNCapture` và `\FinSec\VNCaptureNightly` (mục 4.3); `Export-ScheduledTask` lưu XML; `Start-ScheduledTask`; xác nhận `powercfg /requests` liệt kê python.exe.
8. **Auto-logon (tuỳ chọn):** Autologon GUI cho `capture`; task AtLogOn khoá màn hình; reboot thử: poller chạy trước logon, OneDrive chạy sau logon.
9. **Burn-in 24 h (Q1, Q2, Q8):** gập nắp, màn hình tắt; sau 24 h đối chiếu log poll với Event Log 506/507/42/107/PT-1.
10. **Exit run 5 phiên giao dịch liên tiếp:** checklist hằng ngày (mục 10); mọi gap → outage log; tổng kết vào AGENTS.md (ngày + nhãn).
11. **Sự cố:** mất điện, mất mạng, reboot do update, hết quota, đồng hồ lệch > 60 s, disk < 10 GB — mỗi loại: dấu hiệu, lệnh kiểm, hành động, cách ghi outage.
12. **Gỡ/khôi phục:** `powercfg /import` file baseline + `/setactive`; `Unregister-ScheduledTask`; tắt Autologon; trả W32Time về trigger-start nếu muốn (`sc triggerinfo w32time start/networkon stop/networkoff`).

## 10. Heartbeat hằng ngày: nội dung kiểm

Heartbeat = file JSON trong thư mục sync (`heartbeat-YYYYMMDD.json`; Telegram từ Phase 4), sinh ~08:00 ICT (trước phiên) và bản rút gọn sau 17:30.

1. `last_poll_utc` và tuổi; số vòng poll 24 h vs số kỳ vọng theo lịch (2–5' / 60'); danh sách gap > 1.5× chu kỳ (mỗi gap phải có dòng outage log tương ứng — điều kiện "no unlogged gap").
2. Số record mới (`raw_sha256` mới) theo nguồn; số lỗi HTTP theo mã; số lần bị rate-limit.
3. Clock: `w32tm /stripchart /computer:time.windows.com /dataonly /samples:3` (offset); median offset từ HTTP `Date` 24 h; `w32tm /query /status /verbose` → `Last Sync Error`, `Time since Last Good Sync Time`; số Kernel-General 1 có |Time Delta| > 1000 ms.
4. Power/OS: số event 41, 42, 107, 506, 507, Power-Troubleshooter 1, 6008, 1074, Kernel-General 12/13 từ heartbeat trước (kèm UTC); `powercfg /requests` có python.exe; `(Get-CimInstance Win32_OperatingSystem).LastBootUpTime`; cờ pending reboot (PRIOR: key `HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired` tồn tại).
5. Nguồn: `Get-CimInstance Win32_Battery` → `EstimatedChargeRemaining`, `BatteryStatus`.
6. Disk: dung lượng trống volume dữ liệu; kích thước raw/state.
7. Backup: file đêm qua có trong thư mục sync, sha256 khớp manifest với nguồn; tổng dung lượng thư mục backup vs quota.
8. Process: PID, thời gian sống, git SHA của clone, Python version, vnstock version.
9. Trạng thái tổng `OK/DEGRADED/DOWN` + lý do. Heartbeat không cập nhật > 26 h trên thiết bị khác của owner = báo động.

## Kết luận (xếp hạng)

1. **Coi laptop là máy Modern Standby cho tới khi `powercfg /a` trên chính nó chứng minh có S3.** Áp dụng cả hai lớp: `standby-timeout-ac 0`, `hibernate-timeout-ac 0`, `powercfg /hibernate off`, LIDACTION=0 (GUID, AC+DC) **và** poller giữ `ES_CONTINUOUS|ES_SYSTEM_REQUIRED`. Gate: burn-in 24 h gập nắp không có gap trước khi bắt đầu tính exit run (Q1).
2. **Poller chạy dưới `LOCALSERVICE`/`SYSTEM` (ServiceAccount), không phụ thuộc đăng nhập; kiến trúc A** (process sống lâu, trigger AtStartup + lặp 5' `IgnoreNew`, `ExecutionTimeLimit PT0S`, RestartCount 255 / 1', watchdog nội bộ, lock file đơn-instance).
3. **W32Time: Method 1 KB 2385818 + 4 peer (`0.vn`, `0.asia`, `1.asia` với `0x9`; `time.windows.com` với `0xb`) + `SpecialPollInterval 1024`.** Heartbeat đo offset bằng `w32tm /stripchart` (sub-ms, chạy được khi service Stopped); mọi dòng quan sát ghi thêm `http_date` + `rtt` làm chuỗi đối chiếu 1 s.
4. **`first_seen_at` = wall clock UTC; monotonic chỉ để phát hiện gap và clock step.** Ghim Python 3.13; thêm `tzdata`.
5. **Outage log = log poll của chính poller (nguồn sự thật) + export Event Log hằng ngày** (41/42/107/506/507/PT-1/6005/6006/6008/1074/KG-1/12/13, chuẩn UTC) làm bằng chứng đối chiếu; 506 đơn lẻ không phải outage.
6. **Dữ liệu live trên NTFS local ngoài clone và ngoài thư mục sync; JSONL `ab` + flush/fsync mỗi vòng + rotate theo ngày UTC; SQLite WAL `synchronous=FULL`; backup đêm = file bất biến (gzip, `VACUUM INTO`, manifest sha256) ghi atomic vào thư mục sync.**
7. **Windows Update: active hours 07:00–19:00 + Pause 7–14 ngày trước exit run;** không dùng `NoAutoRebootWithLoggedOnUsers`.
8. **Upload backup cần phiên người dùng:** tài khoản local `capture` + Autologon GUI + khoá màn hình + tài khoản cloud phụ; nếu owner từ chối rủi ro LSA secret thì bỏ auto-logon và chấp nhận upload trễ sau reboot.
9. **Quota:** kiểm tuổi tài khoản Google (5 GB nếu tạo từ 2026-03-09); retention thiết kế theo 5 GB.
10. **Từ paper sang probe:** Q1–Q4 là rủi ro đo được và quyết định thiết kế → plan đưa chúng vào phase 3 làm bước gate (một `hs:bakeoff` thu nhỏ trên laptop capture), không quyết bằng lập luận.

## Câu hỏi mở

Mọi probe chạy **trên laptop capture** (không phải máy dev), PowerShell elevated; lưu kết quả vào `C:\capture-ops\probe-YYYYMMDD\`; tóm tắt vào AGENTS.md với ngày + OBSERVED.

- **Q1 — Modern Standby/DAM có dừng một tiến trình session-0 do Task Scheduler chạy (gập nắp, màn hình tắt, có/không power request) không?** Probe: `powercfg /a`; áp dụng mục 1.1; đăng ký 2 task ServiceAccount chạy script ghi `datetime.now(UTC).isoformat()` + `time.monotonic()` mỗi 10 s (bản A gọi `SetThreadExecutionState(0x80000001)`, bản B không); gập nắp 24 h; rồi `Get-WinEvent -FilterHashtable @{LogName='System'; Id=506,507,42,107; StartTime=(Get-Date).AddDays(-1)}` và tìm khoảng > 30 s giữa hai dòng; thêm `powercfg /sleepstudy /duration 1 /output C:\capture-ops\sleepstudy.html` và `powercfg /requests` khi đang chạy.
- **Q2 — Có restart/ngủ nào trong 5 phiên khi đã Pause updates; active hours lưu ở đâu?** Probe: `Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' | Select ActiveHoursStart,ActiveHoursEnd`; hằng ngày `Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='User32'; Id=1074}`.
- **Q3 — Principal nào đủ quyền: `LOCALSERVICE` (ServiceAccount) có ghi được data dir + thư mục OneDrive của user và gọi HTTPS ra ngoài không; `S4U` có chặn HTTPS ra Internet không?** Probe: 3 task thử (`LOCALSERVICE`, `SYSTEM`, user `S4U`), action `python -I -c "import urllib.request,pathlib,datetime;r=urllib.request.urlopen('https://www.hsx.vn/',timeout=20);pathlib.Path(r'<dir>\probe.txt').write_text(str(r.status)+' '+datetime.datetime.now(datetime.UTC).isoformat())"` với `<dir>` lần lượt là data dir và `OneDrive\FinSecBackup`; `Start-ScheduledTask`; đọc `Get-ScheduledTaskInfo` (`LastTaskResult`) và file kết quả.
- **Q4 — UDP 123 ra ngoài có thông ở mạng nhà; dấu của stripchart là gì?** Probe: `w32tm /stripchart /computer:0.vn.pool.ntp.org /dataonly /samples:5` và `w32tm /stripchart /computer:time.windows.com /samples:5 /rdtsc`; chạy song song script HTTP `Date` (như `date_skew.py`); xác nhận dấu bằng cách đo trước/sau `w32tm /resync` khi offset đang khác 0.
- **Q5 — Sau Method 1, W32Time còn chạy sau reboot và `SpecialPollInterval 1024` có hiệu lực?** Probe: reboot; `sc.exe qc w32time`; `sc.exe qtriggerinfo w32time`; `w32tm /query /status /verbose` (kỳ vọng `Poll Interval: 10 (1024s)`); `w32tm /query /peers`.
- **Q6 — Autologon có chạy với tài khoản local trên edition của laptop; OneDrive có sync khi màn hình khoá?** Probe: Autologon GUI → reboot → không chạm bàn phím 10 phút → `Get-Process OneDrive`; tạo file test trong `OneDrive\FinSecBackup`; xác nhận xuất hiện trên web/điện thoại.
- **Q7 — Laptop có giới hạn sạc OEM không?** Probe: BIOS/UEFI và app OEM (Vantage/Power Manager/MyASUS/HP); ghi tên tuỳ chọn và ngưỡng; `powercfg /batteryreport /output C:\capture-ops\battery.html` (design vs full charge capacity).
- **Q8 — Python 3.13 trên laptop: `time.monotonic()` có tính thời gian standby?** Probe: script ghi `(time.time(), time.monotonic())` mỗi 10 s; bấm nút nguồn cho máy vào standby 5 phút; kiểm Δmonotonic ≈ Δwall ≈ 300 s sau khi thức.
- **Q9 — Quota thực của tài khoản backup (Google 15 hay 5 GB; OneDrive 5 GB)?** Probe: trang storage trên web của từng dịch vụ; ghi số vào runbook.

## Nguồn (độ tin cậy)

- Chính, Microsoft Learn/Support (cao): powercfg-command-line-options; power-button-and-lid-settings-lid-switch-close-action; modern-standby; prepare-software-for-modern-standby; nf-winbase-setthreadexecutionstate; waas-restart; support manage-updates-in-windows; windows-time-service-tools-and-settings; w32time-not-start-on-workgroup (KB 2385818); new-scheduledtasksettingsset; new-scheduledtasktrigger; new-scheduledtaskprincipal; principal-logontype; taskschedulerschema RestartOnFailure / Interval / ExecutionTimeLimit; sharepoint use-group-policy; sysinternals autologon; event-id-41-restart; acquiring-high-resolution-time-stamps; nf-fileapi-flushfilebuffers; cpp `_commit`.
- Chính, khác (cao): docs.python.org/3.13 `time`, `os`; sqlite.org `wal`, `howtocorrupt`; RFC 9110 §6.6.1; ntppool.org `use`, `zone/vn`; support.google.com/googleone/answer/9312312.
- Thứ cấp (thấp–trung bình): itechguides (42/107); ionos, computerworld (OneDrive 5 GB); techcommunity forum (OneDrive user context); digitalcitizen, how2shout (giới hạn sạc).
- OBSERVED (máy dev, 2026-10-22): `powercfg -a / -aliases / -getactivescheme / -requests / -setacvalueindex /?`; `sc.exe qc|qtriggerinfo w32time`; registry W32Time + WU UX; `Get-ScheduledTask` Time Synchronization; `Get-WinEvent` 13 loại event; `w32tm /stripchart`; HTTP `Date` 4 host; `Win32_Battery`; `Win32_OperatingSystem`.

## Giới hạn của nghiên cứu

- Không truy cập được laptop capture: mọi OBSERVED là máy dev (Win11 Home 26200, Modern Standby); laptop có thể khác firmware/edition.
- Không chạy lệnh thay đổi cấu hình (không admin, không ghi registry): mọi lệnh cấu hình ở trên chưa chạy thật.
- Không tìm được tài liệu Microsoft cho event 506/507, Power-Troubleshooter 1, Kernel-General 1/12/13 (chỉ quan sát), và không có doc DAM nói rõ tiến trình Task Scheduler session 0 được xếp loại nào.
- Hành vi OneDrive/Google Drive ngoài phiên tương tác và quota OneDrive chưa có nguồn chính trực tiếp.
- Không đánh giá VPS hay máy để bàn (owner đã chốt §9.4).
