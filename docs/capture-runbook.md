# VN capture runbook (laptop deployment)

How to run the VN capture-only poller (`src/finsec_alpharadar/capture/`) unattended on the spare laptop (roadmap §9.4, plan
261022-1147 phase 3). Written 2026-10-28. Decisions: D3 (heartbeat and backup in a cloud-synced folder), D4 (5-session exit
window), D5 (schedule), D7 (dedicated `capture` account). Background and sources: `plans/261022-1147-phase0a-capture/research/capture-host-windows.md`.

Labels: VERIFIED (a source), OBSERVED (seen on the dev machine, with a date), ASSUMED or PRIOR (not checked; the burn-in checks it).

## How to use this runbook

- **Who does what.** OWNER steps need the laptop, an administrator or a password; AGENT steps run on the dev machine from the
  evidence files. An agent never types a password, an API key or a vnstock key.
- **Evidence, not confirmations.** Every OWNER step leaves a FILE in `<FINSEC_BACKUP_DIR>\ops\`. The dev machine reads the same
  folder as `<backup-mirror>` (section 7). A step without its evidence file is not done.
- **Shells.** "Admin PowerShell" means Windows PowerShell 5.1 started with "Run as administrator". Never run `powercfg` from Git
  Bash: MSYS rewrites `/a` into a path (OBSERVED 2026-10-22). Always write `sc.exe`, never `sc`: in PowerShell 5.1 `sc` is an
  alias of `Set-Content`.
- **Fixed paths** (used everywhere below; change them only by editing this runbook first):

| Path | What | Who may write |
|---|---|---|
| `C:\finsec\app` | git clone, checked out at the deployed SHA | Administrators only |
| `C:\finsec\venv` | Python 3.13 venv installed from the hash-pinned lock | Administrators (capture only during section 8) |
| `C:\finsec\wheels` | the wheel set downloaded in phase 1 | Administrators |
| `C:\finsec\run-capture.cmd` | wrapper that sets the environment (section 10) | Administrators |
| `C:\finsec-capture` | `FINSEC_DATA_ROOT`: raw JSONL, blobs, SQLite state, `ops\capture.log` | `capture` (Modify) |
| `C:\Users\capture\OneDrive\FinSecBackup` | `FINSEC_BACKUP_DIR`: `heartbeat.json`, `heartbeats\`, `daily\`, `ops\` | `capture` |

If the sync client is Google Drive instead of OneDrive, replace the `FINSEC_BACKUP_DIR` path everywhere (the wrapper too).

## 1. Prerequisites (OWNER)

1. Windows 11 laptop on mains power; Ethernet if available. Git for Windows installed for all users.
2. Create the `capture` account first (section 6, steps 1-2): the folder rights below name it.
3. Python 3.13 x64 from python.org, installed **for the user `capture`, inside a `capture` session** (sign in as `capture`, run the
   installer, keep "Install for all users" off). A per-user Python of the administrator does not run under another user
   (red-team r1 RT6). Expected path: `C:\Users\capture\AppData\Local\Programs\Python\Python313\python.exe`.
4. Folders and rights (admin PowerShell). `C:\` lets Authenticated Users modify new folders by inheritance (OBSERVED on the dev
   machine), so inheritance is removed:

```powershell
New-Item -ItemType Directory C:\finsec, C:\finsec-capture, C:\finsec-capture\ops | Out-Null
icacls C:\finsec /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "capture:(OI)(CI)RX"
icacls C:\finsec-capture /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "capture:(OI)(CI)M"
git clone --branch claude/phase0a-capture https://github.com/tqanh123/FinSec.git C:\finsec\app
git -C C:\finsec\app checkout --detach <DEPLOY_SHA>
```

   `*S-1-5-32-544` is Administrators and `*S-1-5-18` is SYSTEM (SIDs work on any display language). `<DEPLOY_SHA>` is the commit
   that added this runbook, or the newest hotfix SHA (section 15). The clone is never under Documents or OneDrive (Known Folder
   Move can sync those). Run every `git` command on the laptop from the admin shell: `capture` does not own the clone, so git
   would refuse it as "dubious ownership" there.

   Expected: `icacls C:\finsec` shows no `(I)` entries and `capture:(OI)(CI)(RX)`; `icacls C:\finsec-capture` shows
   `capture:(OI)(CI)(M)`.

## 2. Baseline before any change (OWNER, admin PowerShell)

```powershell
$ops = 'C:\finsec-capture\ops'
$g = ([regex]'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}').Match((powercfg /getactivescheme)).Value
& { powercfg /getactivescheme; powercfg /a; sc.exe qc w32time; sc.exe qtriggerinfo w32time
    w32tm /stripchart /computer:time.windows.com /dataonly /samples:5 } *>&1 | Out-File -Encoding utf8 "$ops\baseline.txt"
powercfg /export "$ops\power-before.pow" $g
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddDays(-30); Id=41,42,107,506,507,1,12,13,1074,6005,6006,6008} -ErrorAction SilentlyContinue |
  Select-Object @{n='utc';e={$_.TimeCreated.ToUniversalTime().ToString('o')}}, ProviderName, Id |
  ConvertTo-Json -Compress | Out-File -Encoding utf8 "$ops\eventlog-baseline.json"
```

`powercfg /a` tells whether the laptop sleeps with S3 or Modern Standby (S0); note it. Copy `baseline.txt` into
`<FINSEC_BACKUP_DIR>\ops\` once section 7 exists.

## 3. Power (OWNER, admin PowerShell)

```powershell
$g = ([regex]'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}').Match((powercfg /getactivescheme)).Value
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 10
powercfg /change disk-timeout-ac 0
powercfg /hibernate off
# Lid close action = Do Nothing on AC and battery (hidden setting; the LIDACTION alias is missing on the dev machine, OBSERVED)
powercfg /setacvalueindex $g 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setdcvalueindex $g 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setactive $g
```

- Expected: `powercfg /query $g 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936` shows AC and DC index
  `0x00000000`; `powercfg /a` lists hibernate as not available.
- Battery charge limit: set the OEM tool to about 80% if the model has one (Lenovo Vantage, Dell Power Manager, MyASUS, HP BIOS;
  ASSUMED per model). Keep the battery in: it is the UPS for short power cuts.
- Second layer: the poller holds `ES_SYSTEM_REQUIRED | ES_CONTINUOUS` while it runs (`capture/host.py` `KeepAwake`).

## 4. Windows Update (OWNER, Settings app)

1. Settings > Windows Update > Advanced options > Active hours: manual, 07:00 to 19:00 (the default 08:00-17:00 ends before
   17:30, VERIFIED https://learn.microsoft.com/en-us/windows/deployment/update/waas-restart). Turn automatic adjustment off.
2. Settings > Windows Update > Pause updates: pick the length that ends **at least 2 days after the planned last day of the exit
   window**. A pause lasts at most 35 days (VERIFIED https://support.microsoft.com/en-us/windows/manage-updates-in-windows-643e9ea7-3cf6-7da6-a25c-95d4f7f099fe).
   Resume on a weekend, so the update restart falls on a non-trading day.
3. Do not use `NoAutoRebootWithLoggedOnUsers` (Microsoft warns that it can stop quality-update reboots altogether).
4. Evidence: `ActiveHoursStart = 7`, `ActiveHoursEnd = 19` and `PauseUpdatesExpiryTime` under
   `HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings` (in the section 13 verify block). The heartbeat reports the pause expiry
   every day as `wu_pause_expiry`.

## 5. Time (OWNER, admin PowerShell)

W32Time is trigger-started by default and was Stopped on the dev machine (`w32tm /query /status` failed with `0x80070426`,
OBSERVED 2026-10-22). KB 2385818 method 1 makes it a normal automatic service:

```powershell
sc.exe triggerinfo w32time delete
sc.exe config w32time start= auto
net start w32time
w32tm /config /manualpeerlist:"0.vn.pool.ntp.org,0x9 0.asia.pool.ntp.org,0x9 1.asia.pool.ntp.org,0x9 time.windows.com,0xb" /syncfromflags:manual /update
reg add HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient /v SpecialPollInterval /t REG_DWORD /d 1024 /f
w32tm /config /update
w32tm /resync /rediscover
```

Expected: `sc.exe qc w32time` shows `AUTO_START`; `sc.exe qtriggerinfo w32time` shows no trigger; `w32tm /query /status /verbose`
shows `Last Sync Error: 0` and a `Source` from the peer list; `w32tm /query /peers` lists 4 peers; the stripchart offsets are
below 0.5 s in absolute value.

## 6. Accounts (OWNER; D7)

1. Admin PowerShell; type the password into the prompt, never on the command line:

```powershell
$p = Read-Host -AsSecureString 'Password for capture'
New-LocalUser -Name capture -Password $p -PasswordNeverExpires -AccountNeverExpires -Description 'FinSec capture'
Add-LocalGroupMember -SID S-1-5-32-545 -Member capture   # Users: a standard account, not an administrator
net user capture
```

   Expected: `net user capture` shows "Password expires Never". The dev machine has "Maximum password age (days): 42" (OBSERVED
   2026-10-22, `net accounts`); an expired password silently kills Autologon and the scheduled task after the next reboot.
2. Sysinternals Autologon for `capture`, through its window (the command-line form leaves the password in the shell history).
   Trade-off accepted in D7: the password is stored as an LSA secret, and an administrator can decrypt it (VERIFIED
   https://learn.microsoft.com/en-us/sysinternals/downloads/autologon). Hold Shift while Windows starts to skip it once.
3. Lock the screen at `capture` logon (PRIOR: `LockWorkStation` locks the session it runs in):

```powershell
$a = New-ScheduledTaskAction -Execute 'rundll32.exe' -Argument 'user32.dll,LockWorkStation'
$t = New-ScheduledTaskTrigger -AtLogOn -User capture
Register-ScheduledTask -TaskPath '\FinSec\' -TaskName 'LockAtLogon' -Action $a -Trigger $t -Principal (New-ScheduledTaskPrincipal -UserId capture -LogonType Interactive)
```

4. In the `capture` session, sign the sync client in with the **secondary** cloud account, never the owner's main account. Check
   the quota: Google accounts created from 2026-03-09 start with 5 GB (VERIFIED research §5.3); OneDrive free is 5 GB.

If the owner refuses Autologon: skip steps 2-3. Capture still runs (the task does not need a session), but the sync client
uploads only after someone signs in, so the phone sees a stale heartbeat after each reboot. Record the choice in
`<FINSEC_BACKUP_DIR>\ops\autologon-decision.txt`.

## 7. Folders and the dev-side mirror (OWNER)

1. In the `capture` session: create `C:\Users\capture\OneDrive\FinSecBackup` and its `ops` subfolder.
2. `%USERPROFILE%\.vnstock` (the encrypted vnstock key store, research `vnstock-gen5.md` §1) must never be inside a synced folder.
   It is at the profile root, outside OneDrive, by default; do not add it to any backup or sync selection.
3. Make the same folder readable on the dev machine: sign the secondary cloud account in there too, or share the folder with the
   main account. Tell the agent the dev-side path; the agent calls it `<backup-mirror>`.

## 8. Install (OWNER)

1. Copy the phase 1 wheel set from the dev machine (`<dev FINSEC_DATA_ROOT>\wheels\win-py313\`) to `C:\finsec\wheels\` by USB stick
   or the local network, **not** through the cloud folder (red-team r1 RT10).
2. Admin PowerShell, give `capture` write access to the venv folder for the install only:

```powershell
New-Item -ItemType Directory C:\finsec\venv | Out-Null
icacls C:\finsec\venv /grant "capture:(OI)(CI)M"
```

3. In the `capture` session (cmd or PowerShell). No index is reachable: every wheel comes from `C:\finsec\wheels` and is checked
   against the lock hashes:

```bat
%LOCALAPPDATA%\Programs\Python\Python313\python.exe -m venv C:\finsec\venv
C:\finsec\venv\Scripts\python -I -m pip install --require-hashes --no-deps --no-index --find-links C:\finsec\wheels -r C:\finsec\app\requirements\vnstock\win-py313.lock
```

4. Admin PowerShell, back to read-only: `icacls C:\finsec\venv /grant:r "capture:(OI)(CI)RX"`.
5. Expected: `type C:\finsec\venv\pyvenv.cfg` shows `home = C:\Users\capture\AppData\Local\Programs\Python\Python313`. Never
   `pip install -e` the repository: capture runs from the clone through `PYTHONPATH` (section 10).

## 9. vnstock on the laptop (OWNER, `capture` session; OB4b)

The owner types the vnstock key; it never appears in a file, a chat or a command line.

```bat
set B=C:\Users\capture\OneDrive\FinSecBackup\ops
C:\finsec\venv\Scripts\python -I C:\finsec\app\scripts\spike\vnstock_probe.py hash --repo C:\finsec\app --out %B% --label before-laptop
C:\finsec\venv\Scripts\vnstock.exe register
C:\finsec\venv\Scripts\python -I C:\finsec\app\scripts\spike\vnstock_probe.py status --out %B%
C:\finsec\venv\Scripts\python -I C:\finsec\app\scripts\spike\vnstock_probe.py hash --repo C:\finsec\app --out %B% --label after-laptop
```

Then write the number of devices now registered on the account (https://vnstocks.com/account?section=devices) into
`%B%\vnstock-devices.txt`. Expected: the before and after hash files match (no agent instruction file changed), and the status
file shows the Community tier and the expiry (redacted by the script).

## 10. Wrapper (OWNER, admin)

Create `C:\finsec\run-capture.cmd` (outside the repository, no secret in it):

```bat
@echo off
set "FINSEC_DATA_ROOT=C:\finsec-capture"
set "FINSEC_BACKUP_DIR=C:\Users\capture\OneDrive\FinSecBackup"
set "PYTHONPATH=C:\finsec\app\src"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "DO_NOT_TRACK=1"
set "VNSTOCK_TELEMETRY=0"
set "VNSTOCK_DISABLE_AGENT_SETUP=1"
set "VNSTOCK_DISABLE_GLOBAL_AGENT=1"
"C:\finsec\venv\Scripts\python.exe" -P -m finsec_alpharadar.capture %*
exit /b %ERRORLEVEL%
```

- `PYTHONDONTWRITEBYTECODE`: the venv and the clone are read-only for `capture`.
- `-P`: the working directory (the data root, writable) is not put first on `sys.path`, so a stray `json.py` there cannot run
  as `capture` (red-team r1 LT6). Not `-I`: that would also drop `PYTHONPATH`.
- The wrapper sets everything itself; it does not rely on profile variables (whether a task loads them is PRIOR).

## 11. Dry run (OWNER, `capture` session)

```bat
cd /d C:\finsec-capture
C:\finsec\run-capture.cmd check-root
C:\finsec\run-capture.cmd once --all
C:\finsec\run-capture.cmd heartbeat
```

Expected: each exits 0 (`echo %ERRORLEVEL%`); `once --all` prints a count summary for `hose_news_api`, `hnx_rss_issuers` and
`vnstock_events`; `heartbeat.json` appears in the backup folder and on the phone. Exit codes: 1 audit failed or backup not
verified; 2 usage or config error; 3 another capture process holds the lock (`once`, `heartbeat` and `backup` exit 3 while the
task runs; stop the task first); 4 a directory inside a git work tree.

## 12. Scheduled tasks (OWNER, admin PowerShell)

`\FinSec\VNCapture` runs the long-lived poller (research §4.3 option A). Logon type Password ("run whether user is logged on or
not") starts it at boot, before Autologon, and lets vnstock open its per-user key store; S4U has "no access to either the network
or encrypted files" (VERIFIED https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype). The `Principal`
parameter set of `Register-ScheduledTask` has no `-Password`, so the `User`/`Password` set is used (OBSERVED 2026-10-22, PS 5.1).
Type the password into the `Get-Credential` dialog only.

```powershell
$a = New-ScheduledTaskAction -Execute 'C:\finsec\run-capture.cmd' -Argument 'run' -WorkingDirectory 'C:\finsec-capture'
$t = @((New-ScheduledTaskTrigger -AtStartup),
       (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)))
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 255 -RestartInterval (New-TimeSpan -Minutes 1)
$c = Get-Credential capture
Register-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' -Action $a -Trigger $t -Settings $s -User $c.UserName -Password $c.GetNetworkCredential().Password -RunLevel Limited
Export-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' | Out-File -Encoding utf8 'C:\Users\capture\OneDrive\FinSecBackup\ops\vncapture-task.xml'
```

- The working directory is the data root, **never** the clone: vnstock before 4.0.9 wrote into the `AGENTS.md` of its working
  directory (research `vnstock-gen5.md` §3).
- Expected in the XML: `<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>`, `<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>`,
  a `<Repetition>` with `PT5M`, `<LogonType>Password</LogonType>`, `<RestartOnFailure>` with `PT1M` and `255`.
- The 5-minute trigger restarts a dead process; a live one makes it a no-op (`IgnoreNew`), and the single-instance lock refuses a
  second copy (exit 3). The poller's own watchdog exits with code 70 when its loop stops ticking for 900 s.

`\FinSec\EventLogExport` exports sleep, wake and shutdown events every day at 07:30. Create `C:\finsec\export-eventlog.ps1`
(admin):

```powershell
$since = (Get-Date).AddDays(-1)
$providers = 'Microsoft-Windows-Kernel-Power','Microsoft-Windows-Power-Troubleshooter','EventLog','User32','Microsoft-Windows-Kernel-General'
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since; Id=41,42,107,506,507,1,12,13,1074,6005,6006,6008} -ErrorAction SilentlyContinue |
  Where-Object { $_.ProviderName -in $providers } |
  Select-Object @{n='utc';e={$_.TimeCreated.ToUniversalTime().ToString('o')}}, ProviderName, Id, @{n='xml';e={$_.ToXml()}} |
  ConvertTo-Json -Compress |
  Out-File -Encoding utf8 ("C:\Users\capture\OneDrive\FinSecBackup\ops\eventlog-{0}.json" -f (Get-Date -Format yyyyMMdd))
```

Times are converted to UTC and the event data is kept as XML: the `Message` text carries direction marks (OBSERVED on the dev
machine). Register it under the same account (PRIOR: a standard user can read the System log; if the export file stays empty,
register it with `-User 'NT AUTHORITY\SYSTEM'` instead):

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\finsec\export-eventlog.ps1'
$t = New-ScheduledTaskTrigger -Daily -At '07:30'
$c = Get-Credential capture
Register-ScheduledTask -TaskPath '\FinSec\' -TaskName 'EventLogExport' -Action $a -Trigger $t -User $c.UserName -Password $c.GetNetworkCredential().Password -RunLevel Limited
```

## 13. Verify (OWNER, admin PowerShell) — produces `laptop-config-<yyyyMMdd>.txt`

Start the task (`Start-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture'`), wait a minute, then run:

```powershell
$B = 'C:\Users\capture\OneDrive\FinSecBackup\ops'
$g = ([regex]'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}').Match((powercfg /getactivescheme)).Value
& {
  '== powercfg /a'; powercfg /a
  '== sleep and hibernate idle (AC)'; powercfg /query $g SUB_SLEEP STANDBYIDLE; powercfg /query $g SUB_SLEEP HIBERNATEIDLE
  '== lid action'; powercfg /query $g 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936
  '== power requests'; powercfg /requests
  '== w32time'; sc.exe qc w32time; sc.exe qtriggerinfo w32time
  w32tm /query /status /verbose; w32tm /query /peers
  w32tm /stripchart /computer:time.windows.com /dataonly /samples:5
  '== windows update'; Get-ItemProperty HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings |
    Format-List ActiveHoursStart, ActiveHoursEnd, PauseUpdatesExpiryTime
  '== versions'; Get-CimInstance Win32_OperatingSystem | Format-List Caption, Version, BuildNumber
  C:\finsec\venv\Scripts\python.exe --version
  '== account'; net user capture
  '== acl'; icacls C:\finsec; icacls C:\finsec-capture
  '== venv'; Get-Content C:\finsec\venv\pyvenv.cfg
  '== clone'; git -C C:\finsec\app rev-parse HEAD; git -C C:\finsec\app status --porcelain
  '== tasks'; Get-ScheduledTask -TaskPath '\FinSec\' | Format-Table TaskName, State
} *>&1 | Out-File -Encoding utf8 ("$B\laptop-config-{0}.txt" -f (Get-Date -Format yyyyMMdd))
```

The agent checks each line (plan R3.4): S0 or S3 noted; STANDBYIDLE and HIBERNATEIDLE AC index 0; hibernate not available; lid
AC = DC = 0; `python.exe` under `SYSTEM:` in `powercfg /requests` (the poller's `ES_SYSTEM_REQUIRED`; the process runs as
`capture`); `AUTO_START`, no trigger, `Last Sync Error: 0`, 4 peers, |offset| < 0.5 s; active hours 7-19 and the pause expiry;
"Password expires Never"; no inherited ACL on `C:\finsec` and `capture` RX there, Modify on `C:\finsec-capture`; `home` in the
`capture` profile; HEAD equal to the deployed SHA and an empty `status --porcelain`.

Then restart the laptop once on purpose. Expected: the task starts again on its own (before Autologon), and the next heartbeat
lists a `process_down` outage in `outages_24h`.

## 14. Burn-in, at least 24 h (OWNER, then AGENT)

OWNER: lid closed, screen off, laptop on mains, for at least 24 hours, including the deliberate restart of section 13.

AGENT, on the dev machine, from `<backup-mirror>` (the nightly backup writes `daily\<d>\state-<d>.sqlite`, a snapshot outside
WAL mode, so the audit reads it in place and creates no `-wal`/`-shm` files):

```bash
.venv/Scripts/python -m finsec_alpharadar.capture audit --state "<backup-mirror>/daily/<d>/state-<d>.sqlite" --from <d0> --to <d1> --days all --require-days 1
```

Pass when all of these hold:
- the audit exits 0: no unlogged gap, and the restart shows as a logged `process_down`;
- no Kernel-Power 42 or 506 (entering sleep) in `eventlog-*.json` lies outside a logged outage;
- the snapshot has at least one `polls` row with `source = 'vnstock_events'`, `status = 'ok'`, `run_mode = 'run'`, after the
  deliberate restart and with a `pid` other than the dry run's. This is the logon-type gate (plan R3.3): the dry run `once` also
  polls vnstock, so only `run_mode` tells them apart. If it fails, re-register `VNCapture` with an Interactive principal in the
  Autologon session and record why;
- `heartbeats\heartbeat-<d>.json` exists for every burn-in day;
- `powercfg /requests` listed `python.exe` (section 13 evidence).

A burn-in that needs a code or config change goes through section 15 and starts again.

## 15. Exit window and the hotfix procedure (D4)

**Opening.** The window opens on the first trading day after a passed burn-in, with the Windows Update pause ending at least 2
days after the planned last day. Clean weeks (research `vn-calendar-sessions.md` §6): 02-06/11, 09-13/11, 16-20/11, 30/11-04/12,
07-11/12, 14-18/12/2026. Avoid 23-27/11 if possible (24/11 is a holiday; the config marks it, so the count stays right) and
21/12/2026-01/01/2027 (HNX-to-HOSE transfer). Check the pause expiry again before opening. The agent records "capture window
started <date> at <sha> (capture_version <v>)" in AGENTS.md in the first phase commit after the opening.

**Freeze.** Until the phase 7 exit audit, the laptop clone, the venv and `configs/capture.toml` do not change. A process restart on
the same SHA (reboot, crash, task restart) logs `process_down` by itself; a logged outage is allowed, so the count goes on.

**Daily checklist (OWNER, phone, or AGENT from `<backup-mirror>`):**
- `heartbeat.json` status `OK` and less than 30 minutes old;
- `outages_24h`: every entry has a known cause;
- clock: median |skew| at most 2 s, and the stripchart offsets small;
- last night's backup verified (`daily\<d>\manifest-<d>.json` present; `backup_stale` absent);
- a new `ops\eventlog-<yyyyMMdd>.json`;
- `wu_pause_expiry` at least 2 days after the planned last day; extend the pause if not.

**Hotfix (plan R3.9).** Only when the burn-in or the window needs a change to `src/finsec_alpharadar/capture/**` or
`configs/capture.toml`, or the audit finds an unlogged gap.
1. The owner agrees in a session; the agent runs a named hotfix (`/hs:fix` on `claude/phase0a-capture`, or in phase 7) limited to
   `src/finsec_alpharadar/capture/**`, `configs/capture.toml` and `tests/test_capture_*.py`, with a regression test RED then GREEN,
   the full regression gate, and its own `fix(capture): ...` commit.
2. OWNER deploys (admin PowerShell): `git -C C:\finsec\app fetch origin`, `git -C C:\finsec\app checkout --detach <new SHA>`,
   `Stop-ScheduledTask` then `Start-ScheduledTask` for `\FinSec\VNCapture`, and
   `git -C C:\finsec\app rev-parse HEAD > <FINSEC_BACKUP_DIR>\ops\laptop-head-<yyyyMMdd>.txt`.
3. The agent adds a NEW dated AGENTS.md line "capture window restarted <date> at <sha> (reason: ...)"; the newest line wins and old
   lines are never edited. Deploying a new SHA restarts the 5-session count from the first trading day on that SHA; an unlogged gap
   restarts it from the trading day after the gap.

## 16. Incidents

| Incident | Sign | Check | Action | How it is logged |
|---|---|---|---|---|
| Power cut | heartbeat stale; event 41/6008 | `eventlog-*.json`, battery level | none if the battery held; else the task restarts at boot | `process_down` at restart (or `host_stalled`) |
| Network down | `source_unreachable` on every portal | `Test-NetConnection api.hsx.vn -Port 443` | fix the router; capture retries on schedule | source outages |
| Update restart | event 1074 by TrustedInstaller | Settings > Windows Update history | check the pause was active; extend it | `process_down` |
| Cloud quota full | heartbeat or backup files stop updating on the phone | cloud storage page | free space in the secondary account; local data is intact | `backup_write_failed` / `heartbeat_write_failed` in the heartbeat |
| Clock off by more than 60 s | heartbeat skew; stripchart | `w32tm /query /status /verbose` | `w32tm /resync /rediscover`; check UDP 123 | `clock_step` when the clock steps |
| Disk below 10 GB | heartbeat `disk_low` | `Get-PSDrive C` | free space; never delete capture data | heartbeat reason |
| vnstock release expired or yanked | `vendor_release_expired` / `vendor_unavailable` | `ops\capture.log` | reinstall from `C:\finsec\wheels` (no index needed); if the release itself expired, run portals only (below) | vnstock source outage; the portals carry the capture (D2) |
| `capture` password expired or changed | task and Autologon fail after a reboot | `net user capture` | see below | `process_down` once the task runs again |
| vnstock "device limit" | `register` refused | https://vnstocks.com/account?section=devices | remove an old device, then `register` again | vnstock source outage meanwhile |

- **Password expired or changed** (red-team r1 RT3): `$c = Get-Credential capture; Set-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' -User $c.UserName -Password $c.GetNetworkCredential().Password`
  (and the same for `EventLogExport`), run Autologon again through its window, then check `net user capture` shows
  "Password expires Never". Never type the password as a literal.
- **Portals-only venv** (vnstock 5.1.0 stops serving data after 2027-10-17): copy the `tzdata==...` block, with every `--hash`, from
  the lock into `C:\finsec\tzdata-only.txt`, install it with the same `--no-index --find-links C:\finsec\wheels` command, and set
  `vnstock_events` `enabled = false` through the hotfix procedure (a command of this form installed `tzdata==2026.5` from the wheel
  set and loaded `ZoneInfo('Asia/Ho_Chi_Minh')`, OBSERVED 2026-10-22). Re-pinning vnstock is planned maintenance: log it as an outage.

## 17. Rollback and uninstall (OWNER, admin PowerShell)

```powershell
Unregister-ScheduledTask -TaskPath '\FinSec\' -TaskName 'VNCapture' -Confirm:$false
Unregister-ScheduledTask -TaskPath '\FinSec\' -TaskName 'EventLogExport' -Confirm:$false
Unregister-ScheduledTask -TaskPath '\FinSec\' -TaskName 'LockAtLogon' -Confirm:$false
powercfg /import C:\finsec-capture\ops\power-before.pow   # prints the new scheme GUID
powercfg /setactive <GUID printed above>
sc.exe triggerinfo w32time start/networkon stop/networkoff
```

Turn Autologon off through its window. **The data root and the backup folder are never deleted.**
