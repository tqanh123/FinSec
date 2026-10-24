# Round-3 verdict on c9168f0: PASS_WITH_RISK

One medium issue and five lows remain. None of them is critical or high, and no binding rule is broken. The medium is a new test row that fails on a numpy version the project still declares supported.

## Scope
- **Commit:** c9168f0 ("fix: close the second-round review findings"), diff `9ba4562..c9168f0`, 9 files. It sits on top of round 1 (PASS_WITH_RISK on 7092637..dca8e9d) and round 2 (PASS_WITH_RISK on 9ba4562).
- **Measured against:** round-2 findings M1-M3, L4, L7, L8, L9 and the exchange part of L6. Two items are accepted in auto-decisions `abe8b12005d9`, and their recorded reasons hold, so I did not re-raise them:
  - L5: Decimal snapshot fields;
  - L6: bool SSI foreign volumes (Phase 6 debt).
- **Inputs:** three dimension reviews (fix-correctness, docs-citations, binding-and-resolution). Six findings were confirmed by a skeptic majority and none were refuted.

## What I ran myself
- **Test suite:** `.venv/Scripts/python -m pytest -q` gave **267 passed**, exit code 0.
- **CI:** `gh run list` shows run **39721376654** on c9168f0 (pull_request) finished with **success**.
- **numpy 1.26 reproduction** on fresh `git archive` copies, using the scratch venv (Python 3.11.16, numpy 1.26.4, pytest 9.1.1):
  - HEAD `tests/test_forensics_guards.py`: **1 failed, 72 passed**;
  - the same file at 9ba4562: **70 passed**.
- **Probe:** `C:\Users\tqanh\AppData\Local\Temp\claude\h--study-Project-FinSec\150748de-9cc6-4fdc-ab8c-1549b8ee256d\scratchpad\review-r3\verdict\scripts\probe_lows.py`, run on the HEAD copy under numpy 2.5.4 and numpy 1.26.4.
- **Mutants** on HEAD copies, under `...\scratchpad\review-r3\verdict\mut-*`:
  - canary (store `exchange.lower()`): **killed**, 1 failed and 29 passed, which proves the mutated copy is the one imported;
  - store the exchange without upper-casing: **survived**, 30 passed;
  - reject every exchange on a US tick: **survived**, 30 passed;
  - drop the empty-string check: **survived**, 30 passed;
  - refuse any score that is not a plain Python `int` or `float` (`type(score) not in (int, float)`): **survived**, 74 passed. It does change behaviour: on an all-float32 or all-float64 snapshot it nulls both Z and M.

## Round-2 findings: status
| Finding | Status |
|---|---|
| M1: numpy float32 score skipped the finite check | Resolved. xbrl:209 is now `if not math.isfinite(score)`. float32 and float16 scores get `score_not_finite`, with no false DISTRESS or SAFE zone. |
| M2: system-architecture.md:27 citation | Resolved. It now cites lines 101-212, which covers `_try_score` (196-212). |
| M3: stale dates and line count | Resolved. The dates read 2026-10-22, and "About 1,450 lines" matches the 1,460 lines measured. |
| L4: no test reached the Beneish `score_not_finite` path | Resolved. The `prev_accounts_receivable=1e-310` row reaches it. |
| L5 | Split. The int-sum OverflowError is fixed. Decimal fields stay accepted, and the reason holds. |
| L6 | Split. The API exchange check is fixed (padded text, bool or number gives 422). Bool SSI foreign volumes stay accepted, and the reason holds. |
| L7: docstrings missed the new checks | Resolved. |
| L8: pointer to "Debt" notes that no longer exist | Resolved. |
| L9: roadmap rows 245-246 listed done work as to-do | Resolved. |

## Confirmed findings

### 1. Medium: the new float32 test row fails on NumPy 1.26, which `pyproject.toml` declares supported
- **Where:** `tests/test_forensics_guards.py:154-156`, row `total_liabilities=float32(1e-40)`.
- **Why it fails:** the row only works under NumPy 2 promotion rules (NEP 50).
  - Under numpy 2.5.4, `3_800_000.0 / np.float32(1e-40)` stays float32 and overflows to inf, so the score is refused as expected.
  - Under numpy 1.26.4 the same quotient is promoted to a finite float64, 3.8000204816867853e+46. Altman Z comes out as 2.28e46 in zone SAFE, and the row fails with `AssertionError: ('altman', (2.280012289012071e+46, 'SAFE'))`.
- **Who is affected:** `pyproject.toml` declares `numpy>=1.26.0`, and NumPy 1.26 supports Python 3.11 and 3.12. Any such environment that already has numpy 1.26 keeps it after `pip install -e .[dev]`, and its suite goes red.
- **Why CI is green:** pip resolves numpy 2.x there.
- **A regression, not an old gap:** 9ba4562's version of the test file passes all 70 tests under numpy 1.26.4.
- **The code fix itself is correct** on both numpy versions; only the test row depends on the promotion rule. That is why this is medium, not high.
- **Suggested fix:** make both operands float32 by also overriding `"market_cap": np.float32(3_800_000.0)`. My probe shows:
  - this row gives `score_not_finite` on numpy 1.26.4 and on 2.5.4;
  - on numpy 2.5.4 it still kills the revert to `isinstance(score, float)`.
- **Alternative:** raise the floor to `numpy>=2.0`. That is a dependency decision for the owner.

### 2. Low: the exchange test pins only rejections (`tests/test_api_server.py:155-159`)
- **The gap:** the test covers only `" v "`, `True` and `123`. No test:
  - sends a US tick with a valid exchange and expects success;
  - sends a lower-case exchange;
  - sends `""` or `"  "`.
- **The only positive case is VN `"HOSE"`,** already upper-case, so upper-casing is never tested.
- **Evidence:** the no-uppercase, reject-US-exchange and drop-empty-check mutants each survive (30 passed), while the canary is killed.
- **Behaviour today is correct.**
- **Suggested fix:** add these cases:
  - US `"V"` is accepted;
  - US `"iex"` is accepted and stored as `IEX`;
  - VN `"hose"` is accepted (without upper-casing it raises UnknownExchangeError);
  - add `""` and `"  "` to the rejected values.

### 3. Low: no test pins that a finite numpy score is still returned
- **The gap:** every row, and the LOCK test at `tests/test_forensics_guards.py:282-290`, builds its snapshot from Python floats.
- **Evidence:**
  - the over-refuse mutant survives (74 passed);
  - on HEAD, an all-np.float32 or all-np.float64 snapshot gives Z=104.189 (SAFE), M=-2.348, F=9 and no reasons;
  - under the mutant, the same snapshot gives Z=None and M=None.
- **Suggested fix:** add a test that runs BASE with every number field cast to np.float32 and to np.float64. It should assert:
  - no unavailable reasons and no red flags;
  - zone SAFE and F == 9;
  - the LOCK values, approximately for float32 and exactly for float64.

### 4. Low: an int overflow inside a guard reports `score_not_finite` instead of the guard's reason
- **Where:** `src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py:206`. The OverflowError catch wraps the whole score computation, guard operands included.
- **Evidence, on HEAD:**
  - with the int inputs `prev_current_assets = prev_property_plant_equipment = 10**308`, Beneish reports `score_not_finite`;
  - with the same values as floats (`1e308`), it reports `prev_asset_quality_non_positive`.
- **Why it matters:** it contradicts the class docstring at :234-236 ("the first failing check gives the reason"). The outcome is still None with no red flag, and no realistic input triggers it.
- **Suggested fix:** either narrow the docstrings, or convert each field to float once in `_require_finite` and compute on those floats.

### 5. Low: the SSI foreign-volume debt note misdescribes bools (`docs/system-architecture.md:23`, `docs/code-standards.md:69`)
- **What the docs say:** a malformed foreign volume raises a bare error.
- **What the code does:** `connectors/vn_ssi_vnstock.py:52-53` calls `float(msg.get(...))`, so:
  - `ForeignBuyVolTotal=True` is accepted as 1.0, and `False` as 0.0;
  - `None` raises TypeError, and `'abc'` raises ValueError;
  - a required number field given `True` raises PayloadFieldError (`connectors/errors.py:56-57`).
- **Severity:** the rubric lists a misleading doc as medium. I kept the reviewers' low rating because this is a stub parser with no live client, the deferral itself is recorded (abe8b12005d9), and the fix is wording only.
- **Suggested fix:** reword both lines: "a null or non-numeric string raises a bare TypeError/ValueError; a bool is read as a number (True -> 1.0), unlike every required number field (Phase 6)."

### 6. Low: `docs/code-standards.md:51` leaves out the `score_not_finite` cases
- **The gap:** the summary of `tests/test_forensics_guards.py` names only non-finite inputs and undefined ratios. Five rows test the third reason, a score that itself comes out non-finite:
  - two were added in 9ba4562;
  - three were added in c9168f0.
- **The test module's own docstring** (lines 3-5) has the same gap.
- **Suggested fix:** extend CS:51 to name `score_not_finite`, with its triggers: overflow on a subnormal denominator, a numpy float32 score, and an int sum too large for a float.

## Refuted findings
None. The input list of refuted findings was empty.

## Why PASS_WITH_RISK
- There is no evidenced critical or high issue:
  - no binding-rule violation;
  - no order-execution or buy/sell wording added;
  - the disclaimer is unchanged;
  - CI is green on HEAD;
  - all 122 doc citations hold, per the docs dimension.
- One medium remains (finding 1), so the verdict cannot be PASS.

## Recorded
- **Writer:** `harness/scripts/write_review_decision.py`, run once from the repo root as templated, exit code **0**.
- **Arguments:** `--rounds-run 3`, `--effort high`, reviewer engine `claude`, model `claude-opus-5-5`. I also passed `--architecture-checked`, `--architecture-doc-sha 9f7568536cda588364ad54661fc9daa596f664e0` and one `--architecture-drift` entry for SA:23 (finding 5). That last flag is not in the template: without it, `checked: true` would record "found no drift" despite the confirmed SA:23 error.
- **Snapshot:** `H:\study\Project\FinSec\plans\261021-1314-phase0b-foundations\artifacts\review-decision-r3.json` now exists, with verdict PASS_WITH_RISK.
- **Canonical file:** the writer also overwrote `H:\study\Project\FinSec\plans\261021-1314-phase0b-foundations\artifacts\review-decision.yaml`, as it is designed to.
- **Repo state:** `git status --short` still shows only `?? plans/`, and HEAD is still c9168f0.
- **Scratch files:** the rationale and drift text are in `C:\Users\tqanh\AppData\Local\Temp\claude\h--study-Project-FinSec\150748de-9cc6-4fdc-ab8c-1549b8ee256d\scratchpad\review-r3\verdict\` (`rationale.txt`, `drift.txt`).