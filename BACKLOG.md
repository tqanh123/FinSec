# Backlog

<!-- generated from docs/backlog.yaml by backlog_register.py — do not edit -->

## Open

### P3
- BL-001 [bug] Capture UrllibFetcher: deadline_s does not bound connect plus response headers (sources.py opener.open is bounded only by the per-recv socket timeout); a server trickling header lines can hold a poll for about header-lines x timeout_s, bounded only by the 900 s watchdog. Fix: an overall timer that closes the socket, or document the watchdog as the bound.  (plans/261022-1147-phase0a-capture code review F8 (2026-10-28))
- BL-002 [bug] vn_reconcile.py go-no-go: count_trading_days subtracts only the holidays in configs/capture.toml, which lists one (2026-11-24); the VN market holidays of 2024-08-15..2026-10-30 (about 20 weekdays: Tet, 30/4-1/5, 2/9, Hung Kings, New Year) are counted as sessions, so R1 N and L run about 4% high (MDE 0.031 to about 0.032, tier unchanged). Fix: a VN exchange calendar (Phase 3 trading calendar) or the holiday list in the counting rule.  (plans/261022-1147-phase0a-capture phase 6 final review C7 (2026-10-30))
