# P6 implementation notes (2026-10-30)

Posture: subagent. P6 was cooked before P5 on the owner's choice (2026-10-28: Alpaca paper not registered yet; there is
no SSI phase, §9.5). Workflow slices built the three units test-first under main's brief (scratchpad p6-brief.md):
- `quant/power_rule.py` + re-exports: the 8 phase tests RED on `ModuleNotFoundError`, then GREEN (25 tests in the file).
- `scripts/spike/vn_reconcile.py` (`sample|compare|go-no-go`) and `scripts/spike/vn_counts.py` (`universe|events`), with
  new test files `tests/test_vn_reconcile.py` and `tests/test_vn_counts.py` (outside the phase table; main accepted).
- Review round 1 (3 reviewers, adversarial refute): 18 findings, 11 confirmed (1 high: vendor error text reached the
  console), all fixed test-first; re-verify found none still reproducing.

Owner runs and main, 2026-10-28 to 2026-10-30:
- `sample` (main, live portals): 30 items from portal history, 8 requests; window not open, latency skipped.
- vn_counts run 1 (2026-10-28) stopped after 30/143 on `NetworkError VNSTOCK_SOURCE_UNAVAILABLE` and ran on vnstock
  5.2.1, which pip had installed outside the lock between `universe` and `events`. Discarded. The owner reinstalled
  5.1.0 from the lock; main checked the installed files against the locked wheel's RECORD (121 files, 0 mismatch).
- Main added, test-first: a bounded retry for VNSTOCK_SOURCE_UNAVAILABLE (30 s, 90 s) and `merge` (combine disjoint
  runs over one universe, re-aggregated from raw/; refuses overlaps, outside-universe symbols, mixed versions, raw/
  count mismatch). Fixed the `skipped` count that retries broke.
- Owner runs r2 (120 symbols, stop at PVC after both retries) and r3 (the other 23) on 5.1.0; main merged them:
  143/143. `compare` and `go-no-go` (as of 2026-10-30) results are in spike report §6.
- Review round 2 (workflow p6-final-review, 3 lenses + refuters): 19 findings, 12 confirmed, all fixed: 4 code
  (merge failed_list dedup and universe check; merge keeps a quota_warning stop; rate limit counts from the last
  attempt and bounds a retry wait; the quota check no longer skips its turn after retries), each with a RED test first;
  8 doc wording fixes (rates 30 and 15/min, gap 4 h 21 min, 130 + 13 universe, no-row-within-window wording, HOSE
  posting times DERIVED, weekdays not trading days, roadmap item "partly done"). The holiday gap in the R1 session
  count is BL-002 (about 4% on N and L; tier unchanged). A rerun of `merge` with the fixed code gives an identical
  summary.

Deviations: P6 before P5; two new test files; `--symbol-overrides` (HNX RSS titles carry no symbol; left empty, no
guessing); D2 counts only events from 2019 on (price bars start 2018-10-25); 2.5 documents per R1 event (DERIVED from
research x2-4); retry and `merge` added after the first owner run; three owner runs, the first discarded; the
5.2.1 incident and rollback; detection latency and the laptop-heartbeat criterion are N/A (window not open, laptop not
deployed). Open: the owner's `vnstock_probe.py hash` for the 5.2.1 episode (D6 agent-file check) is still owed.

Gate: .venv 793 passed; global 758 passed, 1 skipped (fastapi); smoke 0; both standards checks conform; go-no-go
rerun with `--as-of 2026-10-30` reproduces the §6.4 table; no control characters in the edited files.
