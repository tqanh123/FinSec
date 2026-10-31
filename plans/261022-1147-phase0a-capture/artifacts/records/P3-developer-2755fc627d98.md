# P3 implementation notes (main, in place; 2026-10-28)

Steps 1-3 of phase-3-capture-deploy.md (the agent part, before the laptop deployment):
- `docs/capture-runbook.md` (new): sections 1-17 of R3.2, each step marked OWNER or AGENT, with commands and expected output.
  Evidence files land in `<FINSEC_BACKUP_DIR>\ops\`; section 13 writes `laptop-config-<yyyyMMdd>.txt` with every R3.4 line.
- `configs/capture.toml`: unchanged. The shipped values are final (R3.8); `test_shipped_config_loads_and_respects_d5` is green.
  No 2027 holiday was added: the config only takes announced weekday holidays.
- Delta docs: AGENTS.md (Decisions D4 and D7 dated 2026-10-28 from plan.md D4/D7 and the phase file text; Current Status;
  Build & Verification "Capture deployment"), docs/system-architecture.md (data flow, cloud sync folder, capture laptop
  boundary), docs/project-roadmap.md §7 item 6 "runbook ready 2026-10-28".

Before the P3 commit, three capture fixes landed from the independent code review (21cbfe3, d13ddb9) and one regression
found while writing the runbook (7cf551b: a backup that returns without a verified row was retried every loop).

Deviations from the phase file:
- `C:\finsec-capture` also has its inheritance removed (Administrators F, SYSTEM F, capture M), stricter than R3.2, which only
  asks for capture Modify.
- The ACL commands name Administrators and SYSTEM by SID so they work on any display language.
- A `LockAtLogon` task (Interactive) implements the screen lock of R3.2 item 6; `EventLogExport` runs as `capture` with a
  documented fallback to SYSTEM if the System log is not readable (PRIOR).

Regression gate: .venv 472 passed; global Python 437 passed, 1 skipped (fastapi); smoke exit 0; both standards checks conform.
Steps 4-9 (OB2, OB7, OB4b, dry run, tasks, burn-in, window) are owner-run and BLOCKED until their evidence files exist.
`verification-P3.json` is written only after the burn-in passes.
