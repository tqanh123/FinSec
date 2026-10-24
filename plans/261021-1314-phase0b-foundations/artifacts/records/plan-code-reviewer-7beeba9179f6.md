# Round-4 verdict on a76e7c9: PASS

No critical, high or medium issue remains. Three low findings are left for follow-up, and none of them blocks.

## Scope
- **Commit:** a76e7c9 ("test: close the third-round review findings"), delta `c9168f0..a76e7c9`, 6 files, +40/-14.
  - It changes tests, docs and one engine docstring rewritten in place (`xbrl_bctc_auditor.py:234-236`).
  - No production behaviour changes.
- **Earlier rounds:** round 1 (PASS_WITH_RISK on 7092637..dca8e9d), round 2 (PASS_WITH_RISK on 9ba4562) and round 3 (PASS_WITH_RISK on c9168f0).
- **Measured against:** round-3 findings M1 and L2-L6.
- **Not re-raised,** because their recorded reasons still hold:
  - Decimal snapshot fields and bool SSI foreign volumes (`abe8b12005d9`);
  - the reason reported for an int overflow inside a guard (`31868b18decb`);
  - the R1/R2 quant risks (`3de30677d8a0`).
- **Inputs:** three dimension reviews. Four findings were confirmed, two of which describe the same issue. None was refuted.

## What I ran myself
| Check | Result |
|---|---|
| `.venv` (Python 3.13.14, numpy 2.5.4), `pytest -q` | 274 passed |
| `py311-np126` (Python 3.11.16, numpy 1.26.4, fastapi 0.159.1), `pytest -q` | 274 passed |
| CI run 39725093812 on a76e7c9 (`gh run list`) | success |
| `git diff --check c9168f0..a76e7c9` | clean; committed blobs are LF (`git ls-files --eol`: `i/lf`) |
| The two float32 tests under `-W error` | 2 passed; the targeted `filterwarnings` at `tests/test_forensics_guards.py:241` predates this delta |
| `probe_r4.py` on numpy 2.5.4 and 1.26.4 | backs findings 1-3 below |
| Mutants on `git archive` copies of a76e7c9 | see finding 1 |

All runs used `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. `git status --short` showed only `?? plans/` before and after, and HEAD stayed at a76e7c9.

## Round-3 findings: status
| Finding | Status |
|---|---|
| M1: the float32 row failed on numpy 1.26 | **Resolved.** Both operands are now float32. The probe gives `score_not_finite` on numpy 1.26.4 and on 2.5.4. |
| L2: the exchange tests pinned only rejections | **Resolved.** US `V` is accepted, `iex` is stored as `IEX` and VN `hose` as `HOSE`; `""` and `"  "` are rejected. |
| L3: no test showed that a finite numpy score is returned | **Resolved.** All-float32 and all-float64 snapshots give Z=104.189 (SAFE), M=-2.348, F=9 and no reasons, on both numpy versions. Leftover: finding 1. |
| L4: an int overflow inside a guard reports `score_not_finite` | **Accepted** (`31868b18decb`). The docstring is narrowed and matches the behaviour. |
| L5: SA:23 and CS:69 misdescribed bool SSI foreign volumes | **Resolved.** The probe gives None -> TypeError, `'abc'` -> ValueError, True -> 1.0. Leftover: finding 3. |
| L6: CS:51 and the test docstring left out `score_not_finite` | **Resolved.** Leftover: finding 2. |

## Confirmed findings (all low)

### 1. Low: the new numpy-snapshot test cannot tell a False flag from None
- **Where:** `tests/test_forensics_guards.py:279`, which asserts `assert not result.is_manipulation_suspect`.
- **Why it matters:** `not None` is also true.
  - The test also asserts that M is computed, so a None flag next to a real M is exactly the case it should rule out.
  - The LOCK test (line 303) pins `is False` only for Python-float input.
  - HEAD behaves correctly: it returns `np.False_`.
- **Evidence (my run):**
  - Mutant at `xbrl_bctc_auditor.py:264`: `is_manip = (beneish_m > -1.78) if type(beneish_m) is float else None`.
  - Run on `test_forensics_guards.py`, `test_end_to_end.py` and `test_api_server.py`, the mutant **survives** with 111 passed on numpy 2.5.4 and on 1.26.4.
  - A canary (`"SAFE"` -> `"SAFE_"`) is killed (4 failed), which proves the mutated copy is the one imported.
- **Suggested fix (verified):** `assert result.is_manipulation_suspect is not None and not result.is_manipulation_suspect`.
  - Against the mutant: 2 failed (both dtype params).
  - On HEAD: 111 passed.
  - Both results hold on numpy 2.5.4 and on 1.26.4.

### 2. Low: "a numpy float32 score" is listed as a `score_not_finite` trigger
- **Where:** `docs/code-standards.md:51` and `tests/test_forensics_guards.py:6-7`. Confirmed findings 2 and 4 are this one issue.
- **Why it is wrong:** a finite float32 score is returned, not refused.
  - The new test pins this: on numpy 2.5.4 the probe gives `Z=np.float32(104.189)` and `reasons={}`.
  - What actually triggers the reason is a float32 quotient that overflows to inf (the row at lines 156-159).
- **Why low and not a medium "misleading doc":**
  - The phrase sits inside a clause already limited to "a score that itself comes out non-finite", so it is imprecise rather than wrong about behaviour.
  - It is round 3's own suggested wording, word for word.
  - No snapshot builder exists yet.
- **Suggested fix:** in both places, replace "a numpy float32 score" with "a float32 quotient that overflows to inf".

### 3. Low: the SSI foreign-volume note leaves out OverflowError
- **Where:** `docs/system-architecture.md:23` and `docs/code-standards.md:69`.
- **Evidence:** `connectors/vn_ssi_vnstock.py:52-53` call `float(msg.get(...))`. The probe gives `10**400 -> builtins.OverflowError: int too large to convert to float`. The cases the docs do list hold.
- **Why low:**
  - What the docs say is true.
  - The missing case is unrealistic input to a stub parser that has no client.
  - The deferral is recorded (`abe8b12005d9`).
  - Required fields already turn OverflowError into PayloadFieldError (`bdcbf8e66a83`).
- **Suggested fix:** at both lines write "a null, non-numeric or too-large one raises a bare TypeError/ValueError/OverflowError".

## Refuted findings
None. The refuted list was empty.

## Why PASS
- **No critical or high issue is evidenced:**
  - no binding rule is touched;
  - no order-execution or buy/sell wording is added;
  - the disclaimer is unchanged;
  - CI is green on HEAD;
  - both numpy 1.26.4 and 2.5.4 pass all 274 tests.
- **No medium remains:** round-3 M1 is fixed and verified on numpy 1.26.4.
- The rubric gives PASS when there is no critical, high or unresolved medium issue. The three lows above are listed for follow-up.

## Recorded
- **Writer:** `harness/scripts/write_review_decision.py`, run once, **exit 0**.
- **Arguments:** `--verdict PASS --rounds-run 4 --effort high --reviewer-engine claude --reviewer-model claude-opus-5-5 --architecture-checked --architecture-doc-sha 9e106c03a2c70f4946e1c828882550375705c8be`, plus one `--architecture-drift` entry for SA:23 (finding 3).
- **Two departures from the template:**
  - **`--rounds-run 4` instead of 3,** as the scope note says. `review-decision-r3.json` already existed and the snapshot keeps the first write. With 3, the writer would have overwritten the canonical yaml, written no snapshot for this round, and still exited 0.
  - **The added drift entry:** without it, `checked: true` would record that no drift was found, even though a confirmed SA:23 gap exists. Drift is audit-only and does not block the gate (`artifact_check._architecture_review_consistency`).
- **Snapshot:** `H:\study\Project\FinSec\plans\261021-1314-phase0b-foundations\artifacts\review-decision-r4.json` exists with verdict PASS, `rounds_run` 4 and one drift entry; the rationale contains no CR.
- **Round-3 record:** `review-decision-r3.json` is unchanged (PASS_WITH_RISK, round 3).
- **Canonical file:** the writer overwrote `H:\study\Project\FinSec\plans\261021-1314-phase0b-foundations\artifacts\review-decision.yaml`, as designed.
- **Repo state:** `git status --short` still shows only `?? plans/`, and HEAD is still a76e7c9.
- **Scratch evidence,** in `C:\Users\tqanh\AppData\Local\Temp\claude\h--study-Project-FinSec\150748de-9cc6-4fdc-ab8c-1549b8ee256d\scratchpad\review-r3\verdict-r4\`:
  - `probe_r4.py` and `probe_r4.out`;
  - `mutants_r4.out` and `fixtest_r4.out`;
  - the copies `mut-flag-none`, `mut-canary`, `fixtest-on-mutant` and `fixtest-on-head`;
  - `rationale.txt` and `drift.txt`.
- **Blocked command:** a safety check stopped an `rm -rf` of two scratch directories. The directories did not exist yet, so the command was not needed. I created fresh directories instead, and nothing needs deleting.