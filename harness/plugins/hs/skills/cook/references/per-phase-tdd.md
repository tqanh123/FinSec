# Per-phase TDD — red→green execution per phase

References `"${HARNESS_BIN_ROOT:-.}"/harness/rules/tdd-discipline.md` (the single source of truth for the red→green rule). This drawer explains how to apply it per-phase inside hs:cook.

## Three sub-steps per phase

By default all three sub-steps run at MAIN (the workflow-weight posture answers in place); when the posture says subagent, **3.T and 3.I both run INSIDE the `@developer` subagent** (it writes the failing test AND implements) and main owns **3.V verify + a review of the subagent's code AND test + the commit**.

```
3.T  Write test for NEW behavior → run to confirm intentional FAIL (wrong assert / ImportError)
3.I  Implement the minimum required to make the test pass
3.V  Re-run FULL suite — the runner `hs-run test next` prints as `next_command`, i.e. the
     command this repo declares (never assume one) → paired commit test+module
     (scoped to the node's declared files only when the plan carries `defer_suite: true` —
     "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/cook/references/deferred-suite.md)
```

Sub-step 3.T must NOT be green before 3.I — if it is already green, the test is wrong; rewrite it.

## Execution posture (in place by default; the workflow-weight reader decides)

The per-phase **red→green loop (3.T write the failing test + 3.I implement)** runs
**in place at main by default**; a `@developer` subagent takes it only when the
workflow-weight posture says so. Resolve it mechanically:
`hs-run cook prompt developer --phase <id>` (append `--flags parallel`/`subagents`
when the caller forced one) returns `posture_mode` + `prompt_text` — **subagent**
when the phase declares more files than the policy threshold or a forcing flag is
set, **in_place** otherwise. Never size a phase by feel; the reader owns the rule
and the threshold, so a number can move without this page following it.

This is a **prose posture, not a hard gate** — nothing blocks the other mode; the per-phase nudge (`cook_delegate_nudge`) is the soft backstop and fires only when a subagent-posture phase receives main-thread source writes.

On `posture_mode: in_place` the MAIN thread implements red→green itself (test 3.T first, intentional fail, then 3.I), keeps verify (3.V) and the paired commit exactly as before, and `record developer` closes the beat with its implementation notes — the two-beat shape survives both modes.

On `posture_mode: subagent` the main thread keeps verify, a **review of the subagent's code AND test strength** (catch a tautological or weakened test), and the paired commit.

**The reader's answer is the posture, not a suggestion to relitigate.** Want the OTHER mode than it answered? Say so where the reader can see it — split the phase, pass `--flags parallel|subagents` or `--flags in_place` for one call, or declare `posture: in_place|subagent` in plan.md (pinned by the approval hash) — never a silent override.
The forced answer names what the threshold alone would have said, `record` treats a forced in_place as in place, and two forces that disagree are refused (exit 2).

**Sequential subagent (NOT `--parallel`):** a subagent-posture sequential run hands ONE phase at a time to a `@developer` writing **in-place** (no worktree — worktrees are only for `--parallel`, which branches from a base ref; see LESSONS.md on parallel-cook worktree base-ref).
The full delegation-context snippet (task · read/modify globs · acceptance · constraints · env) lives in `references/subagent-patterns.md` (`## Sequential Per-Phase Delegate`). A risky slice still verifies via the tier-2 independent verifier before its commit; Steps 4–6 stay MANDATORY.

## Paired commit

Commit after 3.V includes test + module, conventional commit; AI attribution follows the repo's own commit convention (tdd-discipline.md step 4) — none declared, no AI references. Hard stage gate (`harness/hooks/gate_stage.py`) reads `verification.json` — this artifact must exist before advancing to the next stage.

## Fix loop

When 3.V fails: fix the **code**, do not delete/skip/weaken tests. Report lists ALL failures (name + 1-line reason). Verdict PASS / PASS_WITH_RISK / BLOCKED is written to `plans/<plan>/artifacts/verification.json`. Include `phase: <P-id>` (the matching `plan-graph.yaml` node id) in that verification — on a PASS, the `phase_progress_writer` hook snapshots it to `verification-<phase>.json`, the
per-phase evidence safe auto-close counts.
That snapshot is first-wins (completion and the hard-stage snapshot sweep read it): a re-verify of the same phase lands in `phase-latest-<phase>.json`,
unless it shows the DoD gate strictly more: every check name a `test_types` type, a passing result file (or witnessed manual anchor) for every type the snapshot has one for,
and one more such type or a name the snapshot carried that the gate cannot see. Then it replaces the snapshot, and the old one goes to `phase-superseded-<phase>.jsonl`.
A status-only `--check <type>:PASS` adds no such type; `--check <type>/<name>:PASS:<file>` attaches the result file, copied under `artifacts/results/<phase>/` (a run on another machine is fetched back first: `hs-rops fetch <remote path> <scratch file> --lane <lane>`). Otherwise the re-verify stays in phase-latest (completion reads it only to un-count a regression),
and the DoD test-policy check reads neither — it reads `verification.json`, the plan's last write.
`hs-run cook status` names each phase's `latest_record`, plus a `snapshot_note` when the two differ.

**Preferred write path:** `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/write_verification.py <plan_dir> --phase <id> --stage <stage> --verdict PASS --check <type>:PASS ...` writes the canonical verification, snapshots it, and drives the plan lifecycle deterministically in one run — 
even when the verification is not written through the Write tool (a Bash write never trips the `phase_progress_writer` hook, so the
snapshot/auto-close would otherwise be silently skipped). Hand-writing `verification.json` (a complex case: manual anchors, a grace clause) still works — the PostToolUse hook is the fallback net.
