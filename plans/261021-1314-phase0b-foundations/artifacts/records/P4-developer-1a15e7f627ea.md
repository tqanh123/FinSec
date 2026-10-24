## Phase Implementation Report

### Executed Phase
- Phase: phase-4-forensics-guards (P4)
- Plan: plans/261021-1314-phase0b-foundations
- Status: completed. Nothing is committed or pushed; main commits.

### Files Modified
The changes match the phase's Related Code Files table: 9 files, 8 modified and 1 created.

| File | Lines | Change |
|---|---|---|
| `src/finsec_alpharadar/domain/models.py` | 273 | +18/-1 |
| `src/finsec_alpharadar/domain/__init__.py` | 27 | +2 |
| `src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py` | 263 | +239/-79 |
| `tests/test_end_to_end.py` | 172 | +2/-2, lines 21 and 118 in place |
| `tests/test_forensics_guards.py` | 174 | new, 22 tests |
| `AGENTS.md` | 170 | +7/-6 |
| `docs/system-architecture.md` | 181 | +5/-6 |
| `docs/code-standards.md` | 107 | +10/-12 |
| `docs/project-roadmap.md` | 713 | +1/-1 |

Evidence files in the session scratchpad: `P4-red.txt` (runs 1-3), `P4-green.txt`, `P4-mutants.txt`, `P4-grep-gates.txt`, `P4-smoke.json`, `P4-frozen-probe.txt`.

### Tasks Completed
- [x] **1. Tests first.**
  - Run 1 on untouched code: `ImportError: cannot import name 'IssuerType'`, `1 error in 0.51s`.
  - Run 2 after only the enum, the field and the export: `22 failed in 0.41s`, every failure behavioural:
    - computed scores where None is expected;
    - `ZeroDivisionError` in exactly the 5 cases the phase names;
    - NaN giving `DISTRESS`;
    - `DID NOT RAISE FrozenInstanceError`.
  - In run 2 the LOCK fails only at its last line (`unavailable_reasons` missing).
  - Supplementary run 3 adds the attribute to the old result type: `21 failed, 1 passed`, and the LOCK passes on the old formulas.
- [x] **2. Implementation (R4.1-R4.4).**
  - `IssuerType(str, Enum)`.
  - Frozen `FinancialStatementSnapshot`, with `issuer_type` last, default UNKNOWN.
  - `IssuerType` exported from `finsec_alpharadar.domain`.
  - Frozen `ForensicAuditResult` with Optional scores and `unavailable_reasons`.
  - The issuer gate runs first and fails closed.
  - Each score has its own finite check over exactly the fields its formula reads, in declaration order, followed by its guards in the phase order.
  - No clamps; the formulas and red-flag text are unchanged.
  - The smoke fixture is edited in place.
- [x] **3. G5 grep** is empty (exit 1).
- [x] **4. Docs.**
  - Delta docs done, including the dated Decisions line and the roadmap §5.2 line-238 "done early" note.
  - Citation sweep re-measured every models.py and auditor cite.
  - `test_agents_md_paths` is green.
  - The Success-Criteria grep is empty.
- [x] **Regression gate** (everything except git and CI).

### Tests Status
- Type/lint check: no gate (the plan marks it N/A). ruff `F` finds nothing in the new or changed code; the one finding, F401 at `tests/test_end_to_end.py:24`, predates this phase.
- Unit tests:

| Run | Result |
|---|---|
| `.venv` Python 3.13.14 | 187 passed, 0 skipped |
| Global Python 3.13.14 | 163 passed, 1 skipped (only `tests/test_api_server.py`) |
| Python 3.11.16 | 187 passed |
| `/hs:test` form | 187 passed |

- Mutation check: 12 of 12 killed.
- Integration: the smoke script exits 0 with `"forensic_f_score": 9`, and its output is byte-identical to P3's. Both standards checks conform, and G1-G7 are empty.

### Issues Encountered
These are deviations I chose; each is detailed in `deviations`:
- **Supplementary RED run 3**, with a shim for `unavailable_reasons`.
- **prev_asset_quality variant**: `prev_property_plant_equipment=29_000` instead of `prev_current_assets`, because Piotroski reads `prev_current_assets`.
- **RT11 scope**:
  - Three cases have no unaffected score; for them the test asserts the other scores are computed.
  - In the gross-margin case only Altman is compared with the base, because Piotroski reads COGS.
- **Assertions added within the named tests**: a stray issuer label, extra LOCK values, and None zone/flag and no-flag checks.
- **No dedicated frozen-snapshot test**; a probe verified it instead.
- **IssuerType placement**: just before the snapshot, so no earlier cite moves.
- **Extra doc edits**:
  - the system-architecture domain row and Key-decisions 'Why';
  - the line count, 1,200 → 1,400;
  - the new module named in both test-module lists;
  - the code-standards Enums and Dataclasses examples;
  - a dated forensics sentence in error handling.
- **Dates**: 2026-10-21 throughout. The local clock read 2026-10-22, but the UTC date was 2026-10-21.
- **`hs-run cook prompt`** was not run (P3 skipped it too).

Concerns, none blocking:
- Without clamps, tiny positive denominators give huge but defined ratios. This is mandated; formula work is roadmap Phase 2.
- Extreme finite inputs could overflow a score to inf.
- A None in a float field raises TypeError; the type contract says float.
- A raw string equal to a member value counts as that member.
- The auditor working-tree file is now LF; the index is unaffected.

### Next Steps
- Main: review, then make one commit, e.g. `fix: guard forensic scores and gate financial issuers`, with the `Co-Authored-By:` trailer. Then `git push` (push with `PYTHONUTF8=1` if the pre-push gate meets non-ASCII), and confirm the CI run for that commit is `success` on 3.11 and 3.13 (`gh run view <id> --json conclusion,jobs`) before Phase 5.
- Optional for main: confirm the 2026-10-21 (UTC) date convention.

Unresolved questions: none blocking.

Status: DONE
Summary: Phase 4 is implemented red→green in place. The forensic scores run only for NON_FINANCIAL issuers and fail closed otherwise. A non-finite input or an undefined ratio gives None with a reason, all clamps are gone, and the LOCK fixture values are unchanged. Every non-git gate passes on 3.13 (venv and global) and 3.11.
Concerns/Blockers: none blocking (see concerns).


## Main review addendum (2026-10-22)

- Main re-read models.py (IssuerType, frozen snapshot), xbrl_bctc_auditor.py and the test module; R4.1-R4.4 hold.
- Developer RED of record (P4-red.txt): ImportError, then 22 failed behaviourally with only the enum/field shim.
- Review workflow wf_1a0f950d-e86: 8 findings confirmed, 0 refuted. Main fixed: derived-denominator underflow guards (probe OBSERVED ZeroDivisionError at 5e-324; RED rows first), unhashable issuer_type fail-closed (RED first), 17 degenerate rows + 29-field finite sweep + frozen snapshot assert; mutation check P4-main-mutants.txt 10/10 KILLED. Accepted as is: NaN-safe guard form is defence in depth (unreachable after the finite check).
- Gate: .venv 231 passed / 0 skipped; global 207 passed + 1 skipped (tests/test_api_server.py only); 3.11 231 passed; /hs:test 231 passed; smoke rc 0 with forensic_f_score 9; G5 empty; both standards checks conform.
- Commit e2b2da3 (9 files, exactly the phase table).