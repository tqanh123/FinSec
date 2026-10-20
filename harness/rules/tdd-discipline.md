# TDD discipline — shared rules for cook + test (always reference)

## Red -> green, non-negotiable

1. **Test first**: write the test for new or locked behavior, run it to **intentional
   FAIL** (wrong assert / ImportError) — do NOT skip, do NOT fake green.
   That red run LEAVES A TRACE: `track_script_execution` logs each pytest invocation
   under `pytest:<target>` with its exit code, and `hook_runtime.had_red_before_green`
   answers per target whether a failing invocation preceded a passing one. A floor, not
   an authentication — a determined faker can break something first — but the honest
   path leaves evidence and the skipped path leaves a visible hole.
2. **Implement to green**: write the minimum code to make the test pass.
3. **Run the tests that can see this change** while you iterate — the files touching the
   behavior you just moved. Scope it to what the change can reach; a docs edit does not
   owe twelve thousand tests.
   **The FULL suite is required before you commit, push, or ship**
   (`python3 harness/scripts/run_local_tests.py --harness`, or the target repo's suite per
   standards). That boundary is the one that does not move: iteration may be scoped,
   the thing you hand over may not. Judging the blast radius wrong is cheap while you are
   still editing and expensive once it is pushed, which is why the full run sits at the
   handover and not at every save. The target repo's suite is the command
   `hs-run test next` reports as `next_command` — what that repo declares, never an
   assumed runner.
4. **Commit the pair** test+module, conventional commit; any AI-attribution line follows the repo's own commit convention (its CLAUDE.md / CONTRIBUTING, else the session's attribution instructions) — with none declared, no AI reference.

## 100% pass is a gate

- Fail means fix the **code**, or fix a **genuinely wrong test** (state the reason
  explicitly). Do not delete/skip/weaken a test to reach green. "Fix regressions, not
  the test."
- Reports must be honest: list **every** test failure (name + one-line reason); final
  verdict is PASS / PASS_WITH_RISK (state the risk) / BLOCKED.
- Verdict + checks[] are written to `verification.json` (schema
  `harness/schemas/artifact-verification.json`) — this is the hard input for the gate
  stage. See `verification-mechanism.md` for evidence rules.

## An incident that reached a user becomes a test

A defect that got out is institutional memory the model does not carry between sessions.
Write the reproducer FIRST, red on the commit that caused it — a root cause you cannot
turn red is a root cause you have not found yet. Name the test for the behavior, never
for an incident or plan label, land it in the regular suite so it runs on every pass, and
do not delete it to unstick a merge: a sentinel that flakes is worse than one that is
missing, because it teaches the reader to ignore red. Record the binding with
`python3 harness/scripts/sentinel_register.py --add` when you want it findable later.

This replaces a 7.7 KB rule carrying a quarterly-audit schedule, an eight-item checklist
and a decision table. The register it fronted had recorded ZERO bindings (`--list` on a
seeded copy returns the row, so the empty read is real and not a broken reader) — the
ceremony had never once been the thing that made an incident produce a test.
