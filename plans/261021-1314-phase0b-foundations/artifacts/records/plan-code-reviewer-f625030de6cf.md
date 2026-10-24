# Final independent code review, round 1 (cook Step 5), plan 261021-1314-phase0b-foundations

Scope: git diff 7092637..dca8e9d (5 commits, 32 files) against plan.md, phases 1-5, the 15 auto-decisions and AGENTS.md
binding rules. Verdict recorded through harness/scripts/write_review_decision.py (exit 0): PASS_WITH_RISK, rounds-run 1,
with --architecture-checked (docs/system-architecture.md blob 8cd937b4 at dca8e9d) and three --architecture-drift entries.
Probes: scratchpad/final-review/probe_domain.py, probe_asgi.py (+ .out).

Checked (OBSERVED 2026-10-22, HEAD dca8e9d): .venv 245 passed; global 221 passed + 1 skipped module (tests/test_api_server.py);
smoke exit 0; CI run 39713637535 green on 3.11 and 3.13; G1-G7 empty; debt grep only dated history; both standards checks
conform; verify_install --strict OK; compose config gates as specified; ASGI probe: NaN, 1e400, missing exchange, missing
field, as_of=nan/inf -> 422; CI action SHAs, MCP pins verified; Docker Hub returns 200 for both compose tags; every doc
file:line citation into a changed file correct; no secrets added.

## Medium
- M1 forensics/xbrl_bctc_auditor.py:97-108,192-200,238-247: finite inputs can still give a NaN/inf score and a false red
  flag. replace(BASE, total_assets=5e-324, current_assets=-1.0) -> altman_z nan, zone DISTRESS, red flag "(nan < 1.81)";
  total_liabilities=5e-324 -> inf, zone SAFE. Fix: refuse a non-finite score result.
- M2 domain/models.py:178-187: a zero or negative VN price passes when the reference equals the minimum tick
  (UPCOM ref 100: 0.0 and -10.0 accepted; HNX ref 100: -5.0; HOSE ref 10: 0.0). Fix: require price > 0.

## Low
- L1 domain/models.py:159-164: market is not validated (market="vn", exchange="XYZ" accepted).
- L2 forensics/xbrl_bctc_auditor.py:71-75: market_cap=None -> bare TypeError; revenue=10**400 -> bare OverflowError.
- L3 connectors/errors.py:39-41,51-55 and api/server.py:27-32: surrounding whitespace kept in text (" nvda " -> ' NVDA ');
  a bool is accepted as a number (p=True -> 1.0).
- L4 api/server.py:116-119: catching every ValueError from the engine maps any engine bug to 422.
- L5 docs/system-architecture.md:13: the "(handlers called directly; skipped without fastapi)" note now follows
  tests/test_repo_hygiene.py.
- L6 stale dates: section headers (AGENTS.md "verified 2026-10-20", system-architecture :3/:19, code-standards :3); the
  forensics status lines say 2026-10-21 although e2b2da3 was committed 2026-10-22.
- L7 tests/test_api_server.py:98-103,125: the as_of=None test reads the clock through the handler (acknowledged).

## Pre-existing (quant engine, Phase 6 per Out of scope; not blocking)
- R1 extreme finite values (price=volume=1e308) return 200 and poison the shared engine (VWAP inf for 50 ticks, a false
  CUSUM alert); the Kalman state never resets.
- R2 a timestamp jump of 1e103 raises OverflowError at quant/kalman_microstructure.py:27 -> HTTP 500 after state changed,
  contrary to "a missing or invalid field returns 422".

All 15 auto-decisions judged sound (2d7e46b757ef correct but incomplete -> M1; ca7c31c974a0 caused L5).
