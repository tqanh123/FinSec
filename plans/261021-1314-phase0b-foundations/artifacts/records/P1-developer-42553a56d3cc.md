## Phase Implementation Report

### Executed Phase
- Phase: phase-1-test-infra (P1, Test Infra)
- Plan: H:\study\Project\FinSec\plans\261021-1314-phase0b-foundations
- Status: completed (steps 3-8 and 10, plus the regression gate). Steps 9, 11 and 12 are left to main.

### Files Modified
| File | Action | Lines |
|---|---|---|
| `pyproject.toml` | modify | +11/-1. Line 10 changed to `>=3.11` (still line 10). `dev` extra at 33-38 after `finetune`. `[tool.pytest.ini_options]` at 43-45. packages.find moved 34-35 -> 40-41 |
| `tests/test_end_to_end.py` | modify | +4 (wrapper at 166-167; lines 1-163 byte-identical to HEAD) |
| `tests/test_no_order_execution.py` | create | 140 lines, 6 tests |
| `tests/test_agents_md_paths.py` | create | 94 lines, 1 test |
| `.github/workflows/ci.yml` | create | 27 lines, byte-exact to R1.5 |
| `.agents/mcp_config.json` | modify | line 16 only |
| `AGENTS.md` | modify | +15/-3 |
| `docs/code-standards.md` | modify | 16/16 (line count unchanged) |
| `docs/system-architecture.md` | modify | +5/-6 (mandated Known-gaps deletion) |
| `docs/project-roadmap.md` | modify | line 377 in place |

Not committed: `.venv/` and `src/finsec_alpharadar.egg-info/` (both gitignored). `codemap.yaml` is untouched (17 lines, untracked; main adds it).

### Tasks Completed
- [x] Step 3: tests written first. RED with global Python: `1 failed, 7 passed`. The one failure is `test_alpaca_toolsets_are_read_only` with message `alpaca-us: ALPACA_TOOLSETS enables toolsets with write operations: watchlists, trading`.
- [x] Step 4: edited `.agents/mcp_config.json:16`, giving `8 passed`; then `pyproject.toml` (R1.1).
- [x] Step 5: `.venv` (3.13.14) plus `pip install -e ".[dev]"` (exit 0), giving `8 passed`, 0 skipped.
- [x] Step 6: 3.11 leg in the scratchpad via uv, giving `8 passed` (3.11.16).
- [x] Step 7: `ci.yml` exactly as R1.5. The matrix grep has 1 hit and the SHA grep counts 2.
- [x] Step 8: `/hs:test` (`source .venv/Scripts/activate && pytest -q`) gives `8 passed`.
- [x] Step 10: delta docs (AGENTS.md, code-standards, system-architecture, roadmap) and citation sweep. The only moved cite was `pyproject.toml:34-35`, now `40-41`; `mcp_config.json:23` and every `test:` cite (all ≤163) are stable.
- [x] Regression gate run (no git steps).

### Tests Status
- Type/lint check: N/A as a gate (plan: no ruff or mypy config; lint is OUT). Informational: `ruff check` passes on the two new modules. It reports 4 pre-existing findings in the protected lines of `tests/test_end_to_end.py`.
- Unit and invariant tests: pass. N = 8 (1 smoke + 6 order-execution + 1 path) on `.venv` 3.13.14, global 3.13.14 and 3.11.16, with 0 skipped.
- Integration/e2e: `PYTHONPATH=src python tests/test_end_to_end.py` exits 0. Both standards checks print `conform:` and exit 0.
- Mutation probe (scratchpad): 12 config mutations each turn red. The 9 pattern samples behave as specified (`data.alpaca.markets` does not match). On synthetic text the path test flags a missing directory and a missing file.

### Issues Encountered
- **Deviation (needs main's sign-off).** The path test uses `git check-ignore -v -z --stdin` and treats an empty or `!` pattern as "not ignored". The exit-code mapping is the spec's.
  - Why: on this checkout (autocrlf, so the working-tree `.gitignore` has CRLF), git reads blank line 56 as an empty pattern that matches any `dir/` path. Plain `-q` would therefore skip every missing directory token locally.
  - Evidence (OBSERVED, reproduced in throwaway repos): CRLF with a blank line gives rc 0; LF gives rc 1.
  - The HEAD blob is LF, so CI is unaffected.
- `docs/code-standards.md` Testing section kept at 9 lines, so later line numbers (92/98/107/109/112) do not move.
- The mandated deletion of `docs/system-architecture.md:134` shifts later-phase cites: P2 `:136` is now 135, P4 `:179` is now 178, P5 `:149` is now 148. AGENTS.md grew by 12 lines.
- fastapi installed as 0.156.8 (the plan observed 0.156.4). It is unpinned, so direct handler calls are [ASSUMED] until P2.
- The global interpreter's deepeval plugin prints a teardown line; this is environmental.

### Next Steps
- Main: `git add codemap.yaml .github/ tests/test_no_order_execution.py tests/test_agents_md_paths.py` plus the modified files. Then one commit (step 11), `verification-P1.json`, push, draft PR, and watch CI (step 12, VL-1). CI is [ASSUMED] until the first run.
- P2 can start once CI on the draft PR is green.

Status: DONE_WITH_CONCERNS
Summary: Phase 1 test infra is implemented red->green. 8 tests pass on 3.13 (.venv and global) and on 3.11; `/hs:test`, the smoke script and both standards checks are green; docs and citations are updated.
Concerns/Blockers: please ratify the check-ignore deviation (`-v -z --stdin` plus the empty/`!` pattern check). Later phases' system-architecture/AGENTS.md line cites shifted. The first CI run is [ASSUMED]. Git steps 9, 11 and 12 are pending with main.


## Main review addendum (2026-10-21)

- Main re-read both new test modules and the wrapper; RED was real (1 failed / 7 passed, test_alpaca_toolsets_are_read_only naming watchlists, trading; see P1-red.txt).
- Deviation ratified: git check-ignore -v -z --stdin (CRLF empty-pattern quirk reproduced by main: 'git check-ignore -q capture/' rc 0 on this checkout).
- Review workflow wf_332f658f-ced: 7 findings confirmed by 3-skeptic vote, 4 refuted. Applied: roadmap:381, code-standards:45/51, AGENTS.md .gitignore fact, 3 guard patterns, remote-URL keys. Deferred: SSI FC Trading host patterns ([PRIOR], Phase 6).
- Main probe p1_main_probe.py: 21/21 OK. Gate after fixes: .venv 8 passed, global 8 passed, 3.11 8 passed, /hs:test 8 passed, smoke rc 0, both standards checks conform.
- Commit 840ca61 (11 files, exactly the phase table).