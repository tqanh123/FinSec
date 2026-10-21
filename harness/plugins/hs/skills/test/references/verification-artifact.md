# Verification artifact (on-demand)

Load when: writing `verification.yaml` after running the suite.

Backing: schema `"${HARNESS_BIN_ROOT:-.}"/harness/schemas/artifact-verification.json` + rules `"${HARNESS_BIN_ROOT:-.}"/harness/rules/verification-mechanism.md` + `"${HARNESS_BIN_ROOT:-.}"/harness/rules/tdd-discipline.md`.

## When to write

Required after every hs:test run — before handing off to a hard stage (push / pr / ship). `gate_stage.py` reads this file from disk; if missing → an `[advisory]` locally (the command proceeds), a block in the remote CI receipts-gate.

## Location

```
plans/<active-plan>/artifacts/verification.yaml
```

If no plan is active, verification is not gate-able and the hard stage advises (locally, exit 0) with the block deferred to remote CI — create a plan with `status: in_progress` (or set `HARNESS_ACTIVE_PLAN`) so the artifact resolves at `plans/<active>/artifacts/`.

## Required structure (from schema)

Pure-YAML SSOT (the gate also reads a legacy `verification.json`):

```yaml
stage: push
plan: <plan-dir-name>
actor: <resolve_actor() output>
ts: <ISO-8601>
checks:
  - { name: unit,        status: PASS, format: junit,     file: unit.xml }
  - { name: integration, status: PASS, format: junit,     file: integration.xml, detail: "e2e slice OK" }
  - { name: coverage,    status: PASS, format: cobertura, file: coverage.xml, detail: "87%" }
verdict: PASS
```

- **`name` MUST be the canonical `test_type` id** (`"${HARNESS_BIN_ROOT:-.}"/harness/data/test-policy.yaml` → `test_types`) when the check is DoD-bearing — the gate keys on it. A runner-flavoured name like `pytest-unit` / `jest-unit` reads to the gate as a *missing* test type. Map the runner to its canonical id when you write the artifact (jest → `unit`, etc.).
- A DoD-bearing check carries **`format`** (`junit` | `cobertura` | `jacoco` | `sarif` | `manual`) **and a `file:`** under `artifacts/` — the gate RE-DERIVES pass/fail from that raw file, it does not trust the self-declared `status`. A `manual` check carries an evidence anchor instead of a result file.
- To keep the tool's own name, write `<type>/<name>`: `lint/ruff`, `typecheck/mypy`, `unit/full-suite`. The gate reads the part before `/` as the type, and every check of a required type must pass.
- After writing the artifact, run the well-formedness validator before any hard stage: `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/artifact_check.py --validate-verification plans/<active-plan>` (catches a non-canonical name or a phantom result file at the producer, before the push gate does).
- `status` per check: `PASS` | `FAIL` | `SKIP`
- **Any check `FAIL` → a receipt gap on a hard stage**: `[advisory]` locally (`gate_stage.py`, command proceeds), a block in the remote CI receipts-gate
- `verdict`: `PASS` | `PASS_WITH_RISK` | `BLOCKED`
- `actor`: output of `resolve_actor()` — this is attribution, not authorization

## Honesty rules

- Do not self-report PASS when a check is FAIL — CI will expose it immediately on re-run
- Trace records are maintained via `trace_log.append_event` (actor + ts auto-resolved)
- `PASS_WITH_RISK` = risk exists but does not block — provide specific detail in the check
- Claims without a `file:line` or real command output → `UNVERIFIABLE` → downstream rejects

## Resolve actor

```python
from harness.hooks.hook_runtime import resolve_actor
actor = resolve_actor()   # returns "user:<u>[/agent:<a>]" or "ci"
```
