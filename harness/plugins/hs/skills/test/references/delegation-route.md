# Delegation + verdict-route (on-demand)

How hs:test delegates the suite run and how main routes on the verdict. Loaded when the workflow-weight reader answered `required: true` for the diff under test — under every threshold and low-risk, main runs the suite in place and this file is not needed.

## `@tester` = run + report ONLY

Delegate the suite run to a `@tester` subagent to keep the (often large) test output out of main context — the main win. The `@tester`:

- runs the resolved test command + reports a verdict: PASS / PASS_WITH_RISK / BLOCKED;
- lists `checks[]` and the **Unmapped** list — code files with no covering test;
- **does not write tests.** Runs and reports only.

The no-write constraint is a **prose posture**, and nothing else: no table maps `@tester` to a path set, and
`write_deny_policy` decides by PATH and never asks who is writing. The guard here is the
prose carved into `agents/tester.md`. `@tester` also must NOT re-spawn `@tester` / `/hs:test`
(it runs the suite inline via Bash) — that avoids a spawn loop.

In place at main is the DEFAULT (the reader under every threshold); delegation is what this file covers — the measured-over-threshold path.

## Verdict-route threshold (DoD-anchored + counting fallback)

The main thread reads the `@tester` verdict + Unmapped list and routes:

| Gap size | Signal | Route |
|---|---|---|
| **LARGE** | a `test_type` that `test-policy.yaml` REQUIRES for the diff's change-class is missing (a DoD FAIL — `test_policy.evaluate_test_policy`) | `@developer` writes the test **test-first** (the TDD cadence at cook/@developer) — do not patch it hastily at main |
| **LARGE (fallback)** | ≥2 code files unmapped (no covering test), when no test-policy type is explicitly required | `@developer` writes the missing tests |
| **SMALL** | only a coverage nicety (no required DoD type absent), or <2 unmapped files | main fixes inline |

The metric is **DoD-anchored first, a raw unmapped count as the fallback** — a missing required test-type owes a real test; a coverage nicety does not.
