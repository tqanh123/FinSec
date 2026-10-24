## Phase Implementation Report

### Executed Phase
- Phase: phase-2-tick-correctness (P2)
- Plan: plans/261021-1314-phase0b-foundations
- Status: completed (no commit, no push; main commits)

### Files Modified
Exactly the phase's file table (15 files):
- `src/finsec_alpharadar/domain/models.py`: modified, 256 lines.
- `src/finsec_alpharadar/domain/__init__.py`: modified, 25 lines.
- `src/finsec_alpharadar/connectors/errors.py`: new, 52 lines.
- `src/finsec_alpharadar/connectors/__init__.py`: modified, 11 lines.
- `src/finsec_alpharadar/connectors/us_alpaca_edgar.py`: modified, 103 lines.
- `src/finsec_alpharadar/connectors/vn_ssi_vnstock.py`: modified, 91 lines.
- `src/finsec_alpharadar/api/server.py`: modified, 114 lines.
- `tests/test_end_to_end.py`: modified, 172 lines; 11 in-place edits at lines 41, 42, 46, 48, 49, 52, 59, 86, 88, 89, 92.
- `tests/test_vn_price_band.py`: new, 221 lines, 42 tests.
- `tests/test_connectors.py`: new, 194 lines, 40 tests.
- `tests/test_api_server.py`: new, 117 lines, 17 tests.
- `AGENTS.md`: modified, 170 lines.
- `docs/system-architecture.md`: modified, 181 lines.
- `docs/code-standards.md`: modified, 111 lines.
- `docs/project-roadmap.md`: modified, 713 lines (2 lines changed).

Evidence files are in the session scratchpad: `P2-red.txt`, `P2-green.txt`, `P2-grep-gates.txt`, `P2-smoke.json`.

### Tasks Completed
- [x] 1. Tests first, RED recorded:
  - run 1: collection ImportError ×2;
  - run 1b: the API module on untouched code, 17 failed;
  - run 2: after the shim, behavioural, `86 failed, 13 passed`; the 13 are exactly the LOCK tests.
- [x] 2. Domain (R2.1, R2.2): `tests/test_vn_price_band.py`, `42 passed`.
- [x] 3. Connectors (R2.3-R2.6): `82 passed` with the domain tests. The smoke was red as predicted: `field 'trade.t' is missing`.
- [x] 4. API (R2.7): `17 passed`.
- [x] 5. Fixture (R2.8), in place: the smoke exits 0 and prints `"vn_fpt_hose_bands": [147660.0, 128340.0]`; the quant alert reasons are identical to Phase 1.
- [x] 6. Grep gates G1 (connectors part), G2, G3, G4, G6, G7: all empty. The patterns reproduce the plan's baseline 2/6/4/6/1/2 on HEAD.
- [x] 7. Delta docs and citation sweep:
  - every rewritten `file:line` was checked mechanically against the source;
  - `tests/test_agents_md_paths.py` is green;
  - only dated history remains for `stream.ssi.com.vn`, and `silently gets 7%` has no hits.
- [x] Regression gate (everything except git/CI).

### Tests Status
- Type/lint check: no gate (the plan marks it N/A). Pyflakes (F) is clean on touched src/ and the new tests; the remaining ruff findings are style rules the repo's conventions reject, or pre-existing.
- Unit tests:
  - `.venv` (3.13.14): 107 passed, 0 skipped.
  - Global 3.13.14 without fastapi: 90 passed, 1 skipped (only `tests/test_api_server.py`).
  - 3.11.16 leg: 107 passed.
  - Phase 1 had 8 tests, so this is +99 (the bar is +40).
- Integration: the API handlers are called directly (17 pass); the smoke exits 0; both standards checks conform.

### Issues Encountered
Deviations, all listed in detail in the structured `deviations`:
- The RED shim also added the bare `price_limits` field and placeholder stubs, so the RED is behavioural.
- The API module's RED was captured as run 1b.
- 4 extra tests; the API missing-field test covers all 9 required fields.
- Non-numeric values raise `TickValidationError`; `price_limits` shape is checked in step 0.
- The shared package-private field readers live in `connectors/errors.py`.
- SSI foreign volumes keep `.get(..., 0.0)`.
- The new Verified Facts headers are dated.
- Some consistency doc edits go beyond the listed delta: line count, test counts, module count, the `/hs:test` count, the KeyError citations kept for the filing adapters.
- The unused `List` import was removed.
- `hs-run cook prompt` was not run.

### Next Steps
- Main: review, then one commit, e.g. `fix: validate VN tick bands and stop fabricating tick fields` with the `Co-Authored-By:` trailer. Then `git push`, and confirm the CI run for this commit is `success` on 3.11 and 3.13 (`gh run view <id> --json conclusion,jobs`) before Phase 3.
- Optional for main:
  - mark roadmap §5.2 rows 222-224 as partly done;
  - refresh the "Status (2026-10-20)" header of the system-architecture as-built table.

Unresolved questions: none blocking.

Status: DONE
Summary: Phase 2 is implemented red→green in place. All gates pass on 3.13 (venv and global) and on 3.11, the smoke prints the VND band, and the grep gates are empty.
Concerns/Blockers: none blocking. The notes are roadmap §5.2 rows that still read as planned, the stale table header date, pre-existing lint in `test_end_to_end.py`, and global-interpreter plugin noise.


## Main review addendum (2026-10-21)

- Main re-read models.py, connectors/errors.py, both adapters, api/server.py and the 3 new test modules; check order and contracts match R2.1-R2.7.
- RED evidence of the developer: P2-red.txt (collection ImportError; API module 17 failed on untouched code; behavioural shim 86 failed / 13 LOCK passed).
- Review workflow wf_9aee0dfd-437: 10 findings confirmed, 0 refuted. Main fixed all: OverflowError in _required_number/_number (RED first: 3 failed, see P2-main-review-red.txt), 38 lock/regression tests, roadmap 5.2 rows 222-224, AGENTS.md connector claim, SSI foreign-volume note in code-standards and system-architecture.
- Gate: .venv 145 passed / 0 skipped; global 126 passed + 1 skipped (tests/test_api_server.py only); 3.11 145 passed; /hs:test 145 passed; smoke rc 0 with vn_fpt_hose_bands [147660.0, 128340.0]; G1(connectors) G2 G3 G4 G6 G7 empty; both standards checks conform.
- Commit 69fd8d1 (15 files, exactly the phase table).