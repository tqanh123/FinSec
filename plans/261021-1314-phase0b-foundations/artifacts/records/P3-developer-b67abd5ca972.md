## Phase Implementation Report

### Executed Phase
- Phase: phase-3-rag-as-of (P3)
- Plan: plans/261021-1314-phase0b-foundations
- Status: completed. Nothing is committed or pushed; main commits.

### Files Modified
The changes match the phase table exactly (9 files).

| File | Lines | Change |
|---|---|---|
| `src/finsec_alpharadar/rag/hybrid_temporal_rag.py` | 189 | +46/-28 |
| `src/finsec_alpharadar/api/server.py` | 123 | +10/-1 |
| `tests/test_end_to_end.py` | 172 | +1/-1, line 146 in place |
| `tests/test_rag_as_of.py` | 90 | new, 5 tests |
| `tests/test_api_server.py` | 146 | +18/-3, 21 tests |
| `AGENTS.md` | 169 | +4/-5 |
| `docs/system-architecture.md` | 182 | +19/-18 |
| `docs/code-standards.md` | 109 | +10/-12 |
| `docs/project-roadmap.md` | 713 | +2/-2 |

Evidence is in the session scratchpad: `P3-red.txt`, `P3-green.txt`, `P3-grep-gates.txt`, `P3-idf-leak-check.txt`, `P3-smoke.json`, `P3-asgi-probe.txt`.

### Tasks Completed
- [x] **1. Tests first, with the RT7 shim.**
  - The engine shim plus a minimal handler shim made the RED behavioural: `7 failed, 19 passed`, with no raised TypeError.
  - The 7 failures are the 4 rag tests, the 2 echo tests, and the disclaimer test, whose key set now includes `as_of`.
  - The LOCK test `test_document_published_exactly_at_as_of_is_visible` PASSED.
  - Supplementary run 0 on untouched code: 8 failed (TypeErrors).
- [x] **2. Implementation (R3.1, R3.2, R3.3, R3.5).**
  - R3.1 engine:
    - `search(..., *, as_of: float)` is required and keyword-only, and the engine never reads the clock.
    - `V = published_at <= as_of` is computed first; candidates = `V` ∩ the market/symbol filters.
    - BM25 N/df/avg_dl are computed over `V`; the ingest-time `_doc_freqs`/`_avg_dl` are removed.
    - No age clamp.
    - The class docstring says the dense and MaxSim legs are hash placeholders with no semantics.
  - R3.2 API: the handler takes optional `as_of`, defaults to the request time (the only clock read, `server.py:113`) and echoes `as_of`.
  - R3.3: the smoke call uses `as_of=now_ts` in place; the file is still 172 lines.
  - R3.5: done in step 1 (see Issues).
- [x] **IDF-leak check, in a scratch copy only.** With whole-corpus BM25 statistics, `test_results_invariant_to_documents_after_as_of` FAILED: P1 has bm25_rank 2 instead of 1, matching spike (c). Result `1 failed, 4 passed`; the control copy had 5 passed.
- [x] **3. Grep gates.**
  - G1 is empty, and so are the `reference_time|max(0.0` check on the rag module and `reference_time` across `src/` and `tests/`.
  - G2-G7 are still empty.
- [x] **4. Delta docs and citation sweep.**
  - Every moved cite was re-measured against the source with grep, including the bare "(line N)" cites in system-architecture rows.
  - `test_agents_md_paths` is green.
  - The Success-Criteria debt grep returns nothing.
- [x] **Regression gate** (everything except git/CI).

### Tests Status
- Type/lint check: no gate (the plan marks it N/A). Ruff `F` is clean on the touched code; the only finding, at `test_end_to_end.py:24`, is pre-existing.
- Unit tests:
  - `.venv` 3.13.14: 152 passed, 0 skipped.
  - Global 3.13.14: 131 passed, 1 skipped (only `tests/test_api_server.py`).
  - 3.11.16: 152 passed.
  - `/hs:test`: 152 passed.
- Integration:
  - The handlers are called directly in tests.
  - A raw ASGI probe through the real FastAPI app confirmed:
    - an explicit `as_of` is echoed;
    - an omitted `as_of` becomes the request time;
    - `as_of=0` is honoured;
    - `abc` gets 422.
  - The smoke script exits 0 and its output is identical to P2's.
  - Both standards checks conform.

### Issues Encountered
Deviations, all detailed in the structured `deviations`:
- **Handler shim.** The handler also needed a RED shim, or the API tests would fail with an unexpected-kwarg TypeError.
- **R3.5 timing.** R3.5 was done in step 1, so the RED of record is 7 rather than 6.
- **Supplementary run 0** was recorded.
- **code-standards.** The "Known fabricated-default debt" header was removed together with its last item.
- **Constraints bullet.** It was reworded so it reads correctly under "Do not:".
- **Extra doc edits:**
  - roadmap §5.2 rag row, whose line cites were stale;
  - the `as_of` clause in the system-architecture api row;
  - the new module named in both test-module lists.
- **test docstring.** The `test_api_server` module docstring's clock statement was amended.
- **`hs-run cook prompt`** was not run.

Concern: `?as_of=nan` and `?as_of=inf` return 200 with `"as_of": null`. This is outside R3.2. A possible fix is `Query(..., allow_inf_nan=False)`, but it can only be tested over HTTP, and httpx is not in the dev extra.

### Next Steps
- Main: review, then make one commit, e.g. `fix: make RAG search answer as of a required decision time`, with the `Co-Authored-By:` trailer. Then `git push` and confirm the CI run for that commit is `success` on 3.11 and 3.13 (`gh run view <id> --json conclusion,jobs`) before starting Phase 4.
- Optional for main: decide on `allow_inf_nan=False` for `as_of`.

Unresolved questions: none blocking.

Status: DONE
Summary: Phase 3 is implemented red→green in place. RAG search answers only as of a required `as_of`, with no clock read in the engine and BM25 statistics over the visible filings only. The API echoes `as_of`, all gates pass on 3.13 (venv and global) and 3.11, and the IDF-leak check turns the invariance test RED.
Concerns/Blockers: none blocking. A non-finite `as_of` over HTTP is accepted and echoed as null, which is outside the phase scope.


## Main review addendum (2026-10-21)

- Main re-read rag/hybrid_temporal_rag.py, the search handler and both test modules; R3.1-R3.5 hold (V first, BM25 statistics over V, as_of required keyword-only, clock read only at the API).
- Developer RED of record (P3-red.txt): 7 failed behaviourally after the RT7 shims, LOCK passed; IDF-leak check in a scratch copy turned the invariance test RED (P3-idf-leak-check.txt).
- Review workflow wf_f5427358-595: 6 findings confirmed, 0 refuted. Main fixed all: non-finite as_of -> ValueError in the engine and 422 at the API (RED first: 5 failed, P3-main-review-red.txt); 13 lock tests (3 statistic fixtures x 2 ingest orders, filters, epoch filing, forwarding via a private engine); mutation check P3-main-mutants.txt: M1 M2 M3 M4 M6 M7 all KILLED.
- Gate: .venv 165 passed / 0 skipped; global 141 passed + 1 skipped (tests/test_api_server.py only); 3.11 165 passed; /hs:test 165 passed; smoke rc 0; G1 and the reference_time greps empty; both standards checks conform.
- Commit 507f8f7 (9 files, exactly the phase table).