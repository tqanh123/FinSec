---
name: hs:test
injectable: false
description: Run and validate tests for the current change — unit/integration profiles, concise QA report, 100% pass gate. Use when a change needs its tests run and a gate-ready verdict.
argument-hint: "[unit | integration] [--in-place] [--yagni] [--ultra [N] | --debate]"
allowed-tools: [Bash, Read, Write, Grep, Glob, Task]
metadata:
  compliance-tier: workflow
---

# hs:test — disciplined verification

`--yagni` cuts scope not needed for the stated outcome; the default delivers the full ask. Pass it to `hs-run`, which stamps the posture and names the rule.

Run tests for the scope that just changed; report results truthfully — a failure is information, not an enemy.

**Default: main runs the suite in place**; it is delegated to a `@tester` subagent only when the
workflow-weight reader says the diff earned it (§Delegation below), and `--in-place` keeps it at main
even then. In the delegated flow the `@tester` **runs + reports only** (verdict + `checks[]` +
Unmapped); **main persists `verification.yaml`** from that report. The Pre-flight / Profiles / Result-rules sections below describe what gets run and how the
artifact is written — main routes on them; they are not "run it all yourself at main."

**What each step needs is routed by the CLI, not listed here.** Every envelope carries the
`rules` and `references` THIS state needs. Read what the envelope names, and only that. But a
satisfied envelope is not enough to have tested anything — the suite has to actually run, and
the files on disk are what the verdict is about.

## Pre-flight

1-2. **`hs-run test next`** — first reads the verdict the open plan already recorded:
   exit 0 `verified`; exit 10 `suite_failed` / `verdict_unbacked` (the recorded PASS does not
   survive re-derivation against the DoD and the raw result files) / `verification_malformed`.
   Only when nothing is recorded yet does it resolve the stack's own test command AND check
   required deps in one call. Never assume `pytest`; the verb reports what THIS repo declares.
   exit 11 `ready` → `next_command` is the verbatim runner (`pytest`, `go test ./...`,
   `pnpm test`, …); run exactly that. For a runner the harness runs itself (vitest, jest —
   `executor: harness` in the language book) it is `hs-code test run …`: the harness calls the
   runner through `node_modules/.bin`, writes the JUnit and the check ledger; add `--plan` +
   `--phase` so the run lands in the plan, and never run the runner by hand instead.
   exit 10 `no_stack_detected` / `stack_no_test_cmd` → no stack found, or the stack declares
   no runner. Ask the user
   which command to use — do not guess one.
   exit 2 `deps_missing` → `next_action` is the install command; exit 2 `deps_stale` → a
   package imports but sits outside its declared range — pin it. Stop; do not run blindly.
   Run it from INSIDE the target repo: root resolution walks UP to the nearest `plans/` or
   `.git`, so invoking it from elsewhere measures a different tree than you mean.
   `--scope changed` narrows `next_command` to the import-graph test set the changed
   files reach, falling back to the full-suite command (plus a warning) when that set
   is empty or unavailable, and to `facts_stale` (exit 11) when the codegraph snapshot
   backing it is missing or stale — never on a plain `hs-run test next`.
3. Quick import-check of the recently modified module (catching import errors here is cheaper than mid-suite).

## Profiles

| Profile | Scope | When |
|---|---|---|
| `unit` (default) | test the modified module → full unit suite | every TDD cycle |
| `integration` | add e2e (`harness/e2e/run_vertical_slice.py`) | before a hard stage |

Standard command: `python3 -m pytest harness/tests/ -q` (this repo). A target repo runs its own suite — use the `next_command` Pre-flight 1-2 reported, never this literal.

`unit` / `integration` here are **run-scope profiles** (how much to run). Do not confuse them with the canonical `test_type` DoD names the gate keys on (Result rules below) — running the `unit` profile is not the same as emitting a canonical `unit` check.

**Focused first pass** (large suite, small change): run only the tests a change can break, then the full suite before the gate.
The bundled selector reads a measured `hs-code` snapshot through four channels (reverse-BFS import graph, the target repo's own `whole_tree` gates, a path/`-m`/text-mention string channel, and self-select for a changed test) — it is a SUPERSET, never a replacement for the full pre-merge run:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/test/scripts/affected_tests.py --base main --pytest
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/test/scripts/affected_tests.py --changed "${HARNESS_BIN_ROOT:-.}"/harness/scripts/foo.py
```

Same selection engine, reachable directly for a JSON payload (channel-by-channel evidence + the blind spot) instead of a plain file list: `hs-code change test select --base main` / `--changed <file>...` (the envelope's `data` is the machine form; `--text` prints it for a person).

The JSON also carries a `receipt`: a fingerprint (source ref, snapshot commit, whole_tree policy row count), which channel(s) picked up each selected test, and — for every blind-spot test — an explicit `unknown` skip cost, never a fabricated `0`.


## Result rules

- **100% pass is the gate** (tdd-discipline rule): failure → fix the code or fix a genuinely wrong test (with a reason); deleting/skipping/weakening tests to fake green is forbidden.
- QA report: concise, and list **ALL** test failures (name + 1-line reason), notable coverage changes, final verdict: PASS / PASS_WITH_RISK (state the risk) / BLOCKED.
- Verdict + checks[] written to `plans/<plan>/artifacts/verification.yaml` (json accepted as legacy)
  (schema `"${HARNESS_BIN_ROOT:-.}"/harness/schemas/artifact-verification.json`) — the artifact the hard stage gate reads.
  **Owner: in the delegated flow MAIN writes it** from the `@tester`'s reported checks[]; a standalone
  `@tester` (top-level actor, no orchestrating main) writes its own.
- **Never write `verification.yaml` with a raw Bash redirect** (`>` / `cat >`) — the artifact-forgery arm of `gate_stage.py` refuses a shell write into `plans/*/artifacts/`. Write it via `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/write_verification.py` (preferred) **or** the Write tool — both drive the per-phase snapshot + plan lifecycle (`phase_progress_writer`).
- **Grid coverage (advisory consume):** if `plans/<active>/artifacts/coverage-grid.<yaml|json>` exists, fold a `reject`/`needs-detail` verdict into the QA report as one advisory line — it NEVER flips the test verdict (100%-pass stays the only gate). Absent → silent.
- Each DoD-bearing check uses the **canonical `test_type` name** (`"${HARNESS_BIN_ROOT:-.}"/harness/data/test-policy.yaml`
  → `test_types`; map the runner: jest→`unit`, etc.; `<type>/<name>` such as `lint/ruff` keeps the tool's name) and carries `format` + a `file:` the gate re-derives from. Emit a machine-readable result file per stack:

  | stack | runner | reporter → JUnit | file |
  |---|---|---|---|
  | Python | `pytest` | `--junitxml` | `junit.xml` |
  | JS/TS | `vitest` / `jest` | the harness (`hs-code test run`; `--coverage` adds `coverage/total` cobertura + one `coverage/<group>` per floored group) | `artifacts/harness-runs/*.xml` |
  | TS | `tsc -b` / `eslint` (declared under `languages.typescript.checks`) | the harness (`hs-code check run`) — JUnit / SARIF against the check's baseline | `artifacts/harness-runs/*` |
  | Go | `go test` | `gotestsum --junitfile` / `go-junit-report` | `junit.xml` |
  | Rust | `cargo test` | `cargo2junit` | `junit.xml` |
  | Java | `mvn`/`gradle test` | surefire/gradle (native JUnit) | `target/surefire-reports/*.xml` |

- **MANDATE — after writing the artifact, before handing off to any hard stage**, the artifact writer
  (main in the delegated flow; the standalone `@tester` otherwise) runs the well-formedness validator
  and fixes anything it reports:
  `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/artifact_check.py --validate-verification plans/<active-plan>`
  (exit 1 = a non-canonical name or a phantom/unparseable result file — the push gate would block on it).

## Delegation + verdict-route (sized by workflow-weight)

The suite run follows the work's **measured weight**, not a fixed default. Measure the diff being tested through the workflow-weight reader:
`workflow_weight.py review --root . --base <work-start> --head <head>`
(under `${HARNESS_BIN_ROOT:-.}/harness/scripts/`). Delegate the run to a `@tester` subagent only when it
answers `required: true` — over a size threshold, a risk tier above tiny, or the diff cannot be sized.
Under every threshold and low-risk, main runs the suite in place — that is the default, not an opt-out.
When delegating, the win is isolating the (often large) test output from the main context. The `@tester`
**runs + reports ONLY**: verdict + `checks[]` + the **Unmapped** list of code files with no covering test.
It **does not write tests**, and it does not re-spawn `@tester`/`/hs:test` (runs the suite directly via Bash in its own process).

The main thread then routes on the verdict against a **DoD-anchored threshold**: a missing required `test_type` (a DoD FAIL) is a LARGE gap → `@developer` writes the test test-first; a coverage nicety or <2 unmapped files is SMALL → main fixes inline.

## Fix loop and regression

When failing: QA report → hand off to hs:cook (fix) or hs:fix (single bug) → re-run. A bug fix must have a regression test written BEFORE the fix (intentional failure).

## Boundaries

hs:test **only runs and reports** — it does not modify code, does not weaken tests, and does not decide on merge. Modifying code → hs:cook / hs:fix. Post-test review → hs:code-review. Evidence validation → hs:debug when root cause needs deep tracing.

## Related skills

- `manual` evidence tier: exploratory / manual API or UX checks no result file captures. Arm the anchor hook by exporting `HARNESS_MANUAL_TEST_SESSION=<id>` (records every Bash command), or stamp one deliberate probe with `manual_test.py --anchor --cmd <cmd> [--session <id>] [--timeout S]` (default 120 s, max 14400), output kept at `output=`; cite the anchor id in the same verification.yaml.
- `hs:rops`: run the suite on another machine when it is too slow or too heavy here, or when a timing needs a host that is not doing anything else. It returns the real exit code and keeps the full log on the remote, so the verdict written here is the run's own, not a truncated tail's.
- `--ultra [N]`/`--debate`: call `hs:workflow-orchestrate --ultra [N]|--debate --run-id <slug>` to size + drive N independent verdict-route analyses over the SAME test results (no shared context); `hs:escalation-consultant` synthesizes the final verdict written to verification.yaml. Approval gate is `hs:workflow-orchestrate`'s own.

## HARD-GATE (real wiring)

`gate_stage.py` reads verification.yaml (json legacy): any check `FAIL` is a receipt gap on a hard stage — an `[advisory]` locally (the command proceeds), a block in the remote CI receipts-gate. Fraudulent reporting (PASS while failing) surfaces on the next CI re-run; the trace ledger keeps a record of who wrote what (attribution, verification-mechanism rule).

## Optional shadow comparison (off by default)

Profile `workflow-test`; procedure in `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/_shared/hs-run/shadow-compare.md`. The decision already made stands.
