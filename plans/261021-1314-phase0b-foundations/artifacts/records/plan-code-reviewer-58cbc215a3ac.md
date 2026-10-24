# Round-2 verdict review: commit 9ba4562 (plan 261021-1314-phase0b-foundations)

**Verdict: PASS_WITH_RISK.** The decision writer exited 0. It wrote `H:\study\Project\FinSec\plans\261021-1314-phase0b-foundations\artifacts\review-decision.yaml` and its snapshot `review-decision-r2.json`: verdict PASS_WITH_RISK, rounds_run 2, effort high, architecture_review checked, doc_sha 60bdf8e6, and 2 drift entries.

There is no evidenced critical or high issue. Every round-1 counterexample is fixed. Three medium findings and six low findings remain; all of them need unrealistic input or are documentation problems.

## Scope
- Commit 9ba4562 (`git diff dca8e9d..9ba4562`, 15 files), on top of the round-1 PASS_WITH_RISK review of 7092637..dca8e9d (findings M1, M2, L1-L7, R1/R2).
- Checked against the round-1 report, AGENTS.md "Engineering Rules (binding)" and docs/code-standards.md.

## What I checked myself
- **Tests:** `.venv/Scripts/python -m pytest -q` gives **261 passed**. The dimension reviewers report that the global Python 3.13 passes 234 with only `tests/test_api_server.py` skipped, and that Python 3.11 passes 261; I did not rerun those two.
- **CI:** `gh run list` shows run **39716865556 on 9ba4562: completed, success**.
- **Probes**, in `C:\Users\tqanh\AppData\Local\Temp\claude\h--study-Project-FinSec\150748de-9cc6-4fdc-ab8c-1549b8ee256d\scratchpad\review-r2\verdict\`: `probe_verdict.py` and `probe_api_verdict.py`, each with a `.out` file. The API probe drives the real ASGI app.
- **Round-1 findings, all fixed:**
  - **M1:** total_assets=5e-324 with current_assets=-1, and total_liabilities=5e-324, now give Altman None with `score_not_finite` and no red flag.
  - **M2:** a VN price of 0 against UPCOM ref 100 returns 422 "price 0.0 must be above 0".
  - **L1:** market "vn" returns 422.
  - **L2:** market_cap=None and revenue=10**400 are in the test suite, which passes.
  - **L3:** symbol " x " and price true return 422.
  - **L4:** an engine `ValueError` now surfaces as a 500. as_of nan, inf, -inf, 1e400 and abc return 422; 0 or no as_of returns 200.
  - **L5:** the parenthetical now follows test_api_server.py.
  - **L7:** grep finds no clock read in tests/.
  - **R1/R2:** documented in AGENTS.md:61, system-architecture.md:138 and code-standards.md:69. system-architecture.md:126 now points to Known gaps.
- **Binding rules:** the added lines contain no trading-client code and no buy/sell (mua/bán) wording; the only order/trading match is the filename tests/test_no_order_execution.py in a doc line. The DISCLAIMER is unchanged, quant/ is untouched and the issuer gate is unchanged. Baseline values are unchanged: Z=104.189 SAFE, M=-2.348, F=9.

## Confirmed findings (duplicates merged at the higher severity)

### Medium
1. **The M1 fix is partial: the score check skips numpy float32** (`src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py:209`).
   - The line is `if isinstance(score, float) and not math.isfinite(score)`. np.float32 is not a float subclass, so the check is skipped.
   - Evidence (`probe_verdict.out`, numpy 2.5.4): float32 total_assets=1e-40 with current_assets=-1 gives `Z=np.float32(nan) zone=DISTRESS flags=['Altman Z-Score in Distress Zone (nan < 1.81)']`. float32 total_liabilities=1e-40 gives `Z=np.float32(inf) zone=SAFE`. np.float64 is a float subclass and is correctly refused.
   - Realism is low: the field is annotated float and the input must still overflow.
   - Fix: `if not math.isfinite(score):`, plus a float32 test row.
2. **The citation does not cover the new claim** (`docs/system-architecture.md:27`).
   - The row now says "...or the score itself comes out non-finite (`score_not_finite`) (lines 101-193)". Lines 101-193 hold only the three score functions; `score_not_finite` is set in `_try_score` at 209-211 (function lines 196-212).
   - Fix: cite "lines 101-212", or add "`_try_score` lines 196-212".
   - Recorded as architecture drift.
3. **L6 is only partly fixed: stale dates remain.**
   - code-standards.md:77 still says "done for the guards (2026-10-21)". `git log -S` shows it was added in e2b2da3 on 2026-10-22, and every sibling line was moved to 2026-10-22.
   - The code-standards.md:49 and :68 headers say "Current state (OBSERVED 2026-10-21)" above content dated 2026-10-22.
   - AGENTS.md:45 and system-architecture.md:10 say "About 1,400 lines (OBSERVED 2026-10-21)", but src/ had 1,246 lines at 507f8f7 (the last 2026-10-21 commit). It reached 1,430 with e2b2da3 on 2026-10-22, and HEAD has 1,458.
   - Fix: re-date these lines to 2026-10-22, or drop the header dates.
   - The system-architecture.md:10 part is recorded as architecture drift.

### Low
4. **No test reaches the Beneish `score_not_finite` path** (`tests/test_forensics_guards.py:149-157`). At HEAD, prev_accounts_receivable=1e-310 gives Beneish None with `score_not_finite`, but both new test rows are Altman cases. The reviewers' mutant "M1-altman-only" survived with 70 passed. Fix: add that input as a DEGENERATE_CASES row.
5. **L2 residual: two kinds of input still raise a bare exception out of `evaluate()`** (`xbrl_bctc_auditor.py:71-79, 106-107`; the same int sums at 127, 130, 151).
   - current_assets=10**308 with current_liabilities=-10**308 raises a bare `OverflowError`; the float version is correctly refused.
   - A Decimal total_assets raises `TypeError`, and Decimal('sNaN') raises `ValueError`.
   - This contradicts the commit message's "huge integer field reports `<field>_not_finite`".
   - Fix: have the finite check return float(value) for each field and compute on those floats, and also catch ValueError.
6. **The commit message claims more than the code does for exchanges and bools** (`api/server.py:67`; `connectors/vn_ssi_vnstock.py:52-53`).
   - Over ASGI, a US exchange " v " is stored as ' V ', true as 'TRUE', and 123 as '123', each with HTTP 200. A VN exchange is still checked, so " hose " returns 422.
   - SSI ForeignBuyVolTotal=True becomes 1.0. That field is already recorded Phase 6 debt.
   - Impact today is none, because nothing reads a US exchange. Fix: validate exchange the way symbol is validated.
7. **Public docstrings miss the new checks.** The ForensicAccountingEngine docstring (`xbrl_bctc_auditor.py:233-236`) omits `score_not_finite`. The MarketTick docstring (`domain/models.py:120-126`) omits the market check and the VN price above 0 check.
8. **The debt-sync rule points to notes that no longer exist** (`docs/code-standards.md:106`). It names "the Debt notes in this file", but `grep Debt` finds only :5 and :106. The three debt lists also still differ; that predates this branch.
9. **Two roadmap rows still list finished work as to-do** (`docs/project-roadmap.md:245-246`). The test_end_to_end and pyproject rows have no done marker, although the work exists and roadmap :377 says the tests are done. This predates the commit.

## Refuted findings (for context)
- **Forensics scores a bool field as 1 or 0:** refuted 2-1. My probe shows depreciation=True gives M=94.365 with a false manipulation flag, but this is outside L2/L3 scope (bool is an int subclass, the field is annotated float, and no connector builds a snapshot yet). It would be low at most.
- **SSI foreign volumes accept bools, as a standalone L3 finding:** refuted 3-0, because it is recorded Phase 6 debt. It remains in finding 6 only as a commit-message overclaim.
- **The docs give the harness version as v7.2.0 instead of 7.3.0:** refuted 3-0; an older issue the plan defers.

## For information (not findings)
- phase-commits.jsonl books 9ba4562 under P5, and plan.md Rollback assumes one commit per phase, so 9ba4562 must be reverted before dca8e9d.
- `rag/__init__.py` is outside every phase's file table. The recorded reason (decision 3de30677d8a0, the code-standards Imports rule) is adequate.
