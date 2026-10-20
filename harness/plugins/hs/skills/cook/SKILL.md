---
name: hs:cook
injectable: false
description: Execute an approved plan phase by phase — TDD red→green, generate verification/review-decision artifacts, trace every step. Use when a human-approved plan is ready to implement.
argument-hint: "<plan-path> [--phase <id>] [--parallel] [--subagents] [--tdd] [--grid] [--defer-suite] [--review <low|medium|high|xhigh|max>] [--yagni]"
allowed-tools: [Bash, Read, Write, Edit, MultiEdit, Grep, Glob, Task]
metadata:
  compliance-tier: workflow
---

# hs:cook — execute plan by phase

Input: path to a human-approved plan. No approved plan → return to hs:plan (gate `require_plan` blocks hard stages if you try to skip this).

**What each step needs is routed by the CLI, not listed here.** Every envelope carries
`rules` and `references` — the rule files and reference documents THIS state needs — plus
an `advisory` line when the step has one. Read what the envelope names, and only that. But
the envelope routes DOCUMENTS, never the object of the work: reading everything it names is
not enough to have done the step — the code and the phase file are what the step changes,
so open them.

**Context isolation:** cook should run from a CLEAN context — `/clear` after approving the plan, then `/hs:cook <absolute-path>`. Planning carryover (research, debate, red-team) shifts cook's focus. Nothing enforces this and the isolation advisory ships disabled, so noticing that plan and cook are in the same session is yours.

**Flags carried from the plan (`--tdd` / `--parallel` / `--grid`):** NOT new cook decisions — the plan already locked them; hs:plan hands you the exact cook command.
Cook auto-inherits when a flag is absent (TDD plan runs red→green regardless; `--parallel` also resolves from env/config), but the flag makes the behavior **explicit and traced** vs implicit. Surface it to the user, do not swallow it:
- `--tdd` → announce the per-phase Tests Before → Implement → Tests After → Regression Gate split is active for this run (vs the plain red→green cook prints otherwise).
- `--grid` → announce that each phase builds its executor micro coverage-grid BEFORE
  red→green, and that its thin corners land in the phase's test plan — it makes TDD more
  complete, it never replaces a test, relaxes the 100%-pass gate, or hard-blocks. It carries
  from the plan's own `grid: true` with no retyping, so surface "grid carried from plan" —
  never silent, never `--auto`.
- `--parallel` → announce which phases fan out concurrently BEFORE the first slice, and that
  the integration barrier still runs the full suite serially. If the plan's handoff
  recommended sequential but `--parallel` was passed anyway, say so; the partitioner
  re-demotes overlapping batches and is the final arbiter. Never fan out silently.

**Probe-first ★** — the priority discipline: a load-bearing assumption the phase rests on
that CAN be checked empirically gets RUN before the code builds on it. A doc / `--help` /
grep / chain of reasoning is a *hypothesis*, NOT a probe; a plan claim not exercised is
`[ASSUMED]` (`[PRIOR]` if training knowledge), never OBSERVED. Held at MAIN, never
delegated.

## Step 0 — Standards are input

Same as hs:plan: read `docs/code-standards.md` + `docs/system-architecture.md` before writing code. Code follows the shared `docs/code-standards.md` — that is why the harness exists on this machine.

## The loop — `hs-run cook` owns the ORDER, you own the JUDGMENT

```
hs-run cook next
```

Run it, read the `state`, do the judgment that state names, run it again. Every state is
re-derived from artifacts on disk, so deleting an artifact regresses the state — that is
the design. Exit ladder: `0` nothing left · `10` a model turn · `11` one deterministic
step · `2` the CLI cannot answer — **STOP**. Read `warnings` at every exit: at `2` they say
why the run stopped, at `0` they are information the run continued past. Full contract (the
two-beat `prompt`→`record` pattern, why `next` is a projection):
`"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/_shared/hs-run/hs-run-contract.md`.

Every envelope prints its own `state` and exit rung, and `next_action` names the judgment
that state contains. Do that judgment — do not look for the state in a table here. There is
no table: a hand-copied index of a machine that emits dozens of states goes stale where
nothing can see it, and a partial index reads exactly like a complete one.

**One command, and the run prints the rest.** `next` is the only verb you type. Opening the
plan, verifying a phase, recording a delegation, closing out — each arrives as a
`next_command` in the envelope that needs it, `--plan` and `--phase` already filled from the
plan's own frontmatter. Read it out and run it; do not compose one yourself. A hand-typed
verb is a second answer to a question the CLI already answered, and the two drift.

`record` is an ATTESTATION that you ran the step. The CLI checks it against the spawn ledger
and reports `spawn_attested` — but `false` there means *no evidence*, never *no spawn*:
reviewing the subagent's output is still your job, before you call `record`.

## The grid preflight — presence is checkable, QUALITY is yours

The preflight belts (the mandatory `plan-graph.yaml` sidecar, and the MACRO `coverage-grid`
presence gate when grid mode is carried) run inside `next`; a block arrives as a state, not
as a separate command you type. What no gate can check is the part that matters: a grid
below floor (`coverage_ratio < density_tier floor`) or verdict `reject` PASSES presence.
Never skip it silently — surface the thin cells + confab signals and **AskUserQuestion;
proceed only on explicit confirm**. Micro one-pass: a per-phase grid needs no
axis-selection receipt, so cook does not spawn @grid-axis-selector; freeze before fill, no diff.

## Per-phase loop

1. **Conformance checklist**: read the phase file; list files to create/modify; verify naming/format match standards; any deviation from the plan → STOP and ask, never silently change.
   A `warnings` line naming a phase file means the approval hash misses that file —
   re-approve before trusting the plan contract. Advisory by design; the hash is what blocks.
2. **TDD red→green**: test-first intentional FAIL → implement until green → re-run full
   suite (or, on a plan carrying `defer_suite: true`, the current phase's declared test
   files only — the full suite still owes one run before `cook close`) → paired commit
   test+module.
   **Before a phase's red→green (3.T test + 3.I implement): STOP and resolve the workflow-weight posture (`hs-run cook prompt developer --phase <id>`) — in place at main by default, a `@developer` subagent only past the declared-file threshold or on a forcing flag (see §Per-phase red→green).**
   Flag `--tdd` (carried from the plan, or passed here): each phase splits Step 3 into the
   explicit Tests Before → Implement → Tests After → Regression Gate sequence. Without it cook
   still runs red→green; `--tdd` just makes the split explicit and traced.
   On green, before advancing, a **simplification pass**: if the change grew a special case or
   duplicated an existing pattern, collapse it (suite stays green). Pattern library:
   `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/problem-solving/references/simplification-cascades.md`.
3. **Verify before done**: before writing artifact and advancing phase — suite green, no new lint/type errors, every acceptance criterion has evidence (file:line), no silent contract change. If a side effect appears → STOP AskUserQuestion with 2-4 choices; do not self-patch.
4. **Artifacts**: the verify command the envelope prints writes `verification.json` with the
   phase already named — a per-phase snapshot, never a plan-wide one. Review adds
   `review-decision.json` (schema in `harness/schemas/`). The gate reads from the
   **filesystem**, so no commit is needed for it to see an artifact. Whether to commit them at
   finalize is the repo's call, so ask git: `git check-ignore -q plans/<plan>/artifacts/verification.json`
   exits 0 when the repo ignores that path — keep them local; otherwise commit them with the plan.
5. **Trace**: significant steps emit events via `"${HARNESS_BIN_ROOT:-.}"/harness/hooks/trace_log.py` (append_event —
   actor auto-resolved, never hand-craft JSONL). Trace is telemetry fail-open, not a
   compliance gate — gate_stage is the gate, and it runs regardless.

## Per-phase red→green (3.T+3.I): in place by default — resolve the posture before coding

**Before writing ANY test or implementation code for a phase, STOP and resolve
the posture — a checkpoint, not a suggestion.** Run
`hs-run cook prompt developer --phase <id>` (append `--flags parallel|subagents`
when the caller forced one) and read `posture_mode` + `prompt_text`. The
workflow-weight reader owns the rule — **in place by default; `@developer` only
past the declared-file threshold or on a forcing flag** — never a "feels big /
feels small" call. Both branches, the no-relitigating rule, and the nudge seam
are what the CLI routes at `needs_implementation` — read the drawer the
envelope hands you there.

## After the last phase — Steps 4–6, never skip

Cook is NOT done at the last phase's integration barrier + `verification.json`.
All three steps are MANDATORY; whether 4 and 5 DELEGATE follows the measured
disposition `hs-run cook next` reports (one predicate behind `next` and
`close` alike).

- **Step 4 — test**: spawn `@tester` when the disposition owes an independent
  run (any trigger, an above-tiny risk tier, or an unmeasurable range). A
  measured waiver — small, tiny-risk diff — lets the suite run at main.
- **Step 5 — code review**: when the disposition is NOT cleared, spawn
  `@code-reviewer` on the diff — at this final gate, **DO NOT review code
  yourself**. The FINAL review re-derives correctness and catches what
  same-thread self-review misses; it does NOT replace main's per-phase review
  of each `@developer` slice. A measured waiver clears the obligation — earned
  by the numbers, not skipped. **`--review <low|…|max>`** dials how hard the
  owed review digs, never *whether* it runs: no `skip` value exists; the floor
  is a real independent review writing `review-decision.json`.
- **Step 6 — finalize**: plan sync-back, update the docs the change touched,
  commit, close (`close_plan.py`). No delegation required — nothing here
  re-derives a judgment. Skipping any of it leaves the plan half-shipped.

**Hard rule**: Task-tool calls must MATCH the disposition — zero spawns on a
measured waiver is COMPLETE; zero spawns while the disposition still owes
Step 4 or 5 is INCOMPLETE.

## Pause cadence — HARNESS_AUTONOMY

| Level | Behavior |
|---|---|
| `default` | run per-phase sequence automatically; **pause at 2 checkpoints: plan approval + ship** |
| `ask_all` | pause after EVERY phase |
| `god` | no pauses (trace still records fully — autonomy comes with a trace) |

Resolve the level deterministically — never eyeball the env. At each boundary run `python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/cook/scripts/autonomy_policy.py --boundary <plan_approval|phase|ship>` and pause only when it prints `pause` (`--show` emits the resolved level + matrix; the two flags are mutually exclusive). A missing/invalid level falls back to `default`.

No level self-ships: stage `push|pr|ship|deploy` always goes through the artifact gate.

When pausing for human (`ask_all` after a phase, or before writing `review-decision.json`): ask AskUserQuestion with 3 options [Review directly (Plannotator) / Approve / Reject]; choosing (1) → `plannotator_surface.py review <diff>`.

## Parallel execution (`--parallel`, opt-in)

Parallel is **opt-in** (`cook.parallel: false` ships; force with
`--parallel`/`--subagents` or flip the knob): cook otherwise runs one phase at
a time, main implementing in place per the workflow-weight posture. When
forced, independent phases run concurrently to save wall-clock — without
weakening a gate or trusting a subagent on sight. Full protocol:
`references/parallel-execution.md` (this one the CLI cannot route — see below).

Three things the drawer cannot enforce, and they are the ones that go wrong:

- **A returned subagent is an unverified CLAIM, not a result.** Every slice self-verifies
  (its tests + lint, its diff against the phase's acceptance criteria), and a risky one —
  touches a gate/hook, changes a contract, thin test evidence — gets an independent verifier
  that re-derives correctness from the diff ALONE. A slice failing either tier does not merge.
- **Never parallel-edit a shared path**, and never fall back to sequential silently. The
  partitioner demotes both sides of an ownership overlap and reports it; say so.
- **The integration barrier stays serial.** Parallelism speeds the build, never the gate:
  the full suite runs serially after the merges, before any artifact is written.

## Gate wiring (personal-first: generate local, enforce remote)

`"${HARNESS_BIN_ROOT:-.}"/harness/hooks/gate_stage.py` ADVISES on a hard stage when `verification.json` is missing / verdict != PASS — `[advisory]` + `gate_advisory` trace, command proceeds (exit 0). The one hard LOCAL block is the artifact-forgery arm (shell write to a receipt path — the agent cage). Presence enforcement lives in remote CI (receipts-gate).

## References the CLI cannot route

Load these two yourself, on the condition named in the last column. No state expresses that
condition: `--parallel` resolves from a flag, an env var and a config file rather than from
anything on disk, and the artifact schema is needed *while writing* a file, not at a state.

| Drawer | Content | When to load |
|---|---|---|
| `references/parallel-execution.md` | Opt-in `--parallel` protocol: resolve → partition → delegate → verify → integration barrier | When cooking with `--parallel` |
| `_shared/workflow-artifacts.md` | JSON artifact schema (verification/review-decision), approval rules, redaction policy | When writing or validating review/finalize artifacts |

## Observe checkpoint (end-of-work)

When cook finishes, emit at most ONE closed-vocab signal — valid terms here:
`gate-repeat-block`, `plan-revised-post-approval`, `thin-evidence`. Most runs emit nothing.

## Boundaries

- Do not modify `harness-hooks.yaml`/`stage-policy.yaml` to pass the gate — git-tracked, any change shows in diff + trace. Genuinely stuck → ask the human.
- Work that arises outside the plan scope → record via `backlog_register.py add`; do not steer the plan mid-flight.
- Mid-phase, if the planned direction stalls and 2-3 alternatives are measurably comparable, escalate to `hs:bakeoff` (probe the alternatives, decide by numbers) instead of grinding one direction — then resume cook.

## Auto-decision ledger

Record ONLY decisions that DEVIATE from the approved plan.

## Optional shadow comparison (off by default)

Profile `cook-weight` or `risk-tier`; procedure in `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/_shared/hs-run/shadow-compare.md`. The decision already made stands.
