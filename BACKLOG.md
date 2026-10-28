# Backlog

<!-- generated from docs/backlog.yaml by backlog_register.py — do not edit -->

## Open

### P3
- BL-001 [bug] Capture UrllibFetcher: deadline_s does not bound connect plus response headers (sources.py opener.open is bounded only by the per-recv socket timeout); a server trickling header lines can hold a poll for about header-lines x timeout_s, bounded only by the 900 s watchdog. Fix: an overall timer that closes the socket, or document the watchdog as the bound.  (plans/261022-1147-phase0a-capture code review F8 (2026-10-28))
