# Unified Workflow Steps

All modes share core steps with mode-specific variations.

**Task Tool Fallback:** `TaskCreate`/`TaskUpdate`/`TaskGet`/`TaskList` are CLI-only — unavailable in VSCode extension. If they error, use `TodoWrite` for progress tracking. All workflow steps remain functional without Task tools.

> hs:cook enters at Step 3.O with a human-approved plan; research and planning belong to `hs:plan`. Its pause cadence is governed by `HARNESS_AUTONOMY` (default|ask_all|god).

## Step 3: Implementation

### Step 3.O: Open the plan (all modes, before any code)

**Open the plan deterministically** as the first action of implementation — do NOT hand-edit the frontmatter status:

```bash
# Flips plan.md frontmatter status pending|approved -> in_progress (idempotent, surgical).
# The gate's active-plan resolver returns ONLY an in_progress plan: until this runs the
# plan being cooked is invisible to the gate (its verification/review gates are silently
# skipped, or the resolver latches onto a stale in_progress plan from another session).
# The mirror of the Step 6 close. Fails LOUD (exit 1) on a completed/cancelled plan, so
# cook halts rather than cooking a plan the resolver will never see as active.
python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/open_plan.py <absolute-plan-dir>
```

In `code` mode (which skips Steps 1–2) this is the step that binds the pre-existing plan to the gate; run it right after the Step 0 active-plan detection.

**Output:** `✓ Step 3.O: Plan opened (in_progress)`

Task hydration:
1. `TaskList` first — check for existing tasks (hydrated by planning skill in same session)
2. If tasks exist → pick them up, skip re-creation
3. If no tasks → read plan phases, `TaskCreate` for each unchecked `[ ]` item with priority order and metadata (`phase`, `planDir`, `phaseFile`)
4. Tasks can be blocked by other tasks via `addBlockedBy`

### Conformance Checklist (before writing code)

Before implementing each phase, the developer agent MUST:

1. **Read `docs/code-standards.md`** and confirm naming, file structure, and error-handling patterns still match the repo.
2. **Scout adjacent code patterns** in the files being modified and follow the same import, logging, and error-wrapping style.
3. **Check for existing helpers** before creating new utilities so the change stays DRY.
4. **Verify interface contracts** so new code extends the current surface instead of creating a parallel one.
5. **Cross-check the plan checklist** so every file in the phase inventory is actually addressed.

After each file is modified:
- **Compile check:** run the relevant project compile/type-check command
- **Pattern verify:** confirm the new code matches adjacent conventions
- **Import check:** confirm no circular dependency or dead import was added

### `--tdd` Flag Behavior

When `--tdd` is active, Step 3 splits into sub-steps per phase:

```
Step 3.T: Write the test for NEW/locked behavior → run it to intentional FAIL (red)
Step 3.I: Implement the minimum to make it pass (green)
Step 3.V: Re-run the full suite (node's declared scope only under the plan's
          `defer_suite: true`) → all green + compile gates, then paired commit
```

Step 3.T must be RED before 3.I — that proves the test exercises the new behavior. After 3.I the suite is green; a still-red test means the implementation is incomplete. This mirrors `"${HARNESS_BIN_ROOT:-.}"/harness/rules/tdd-discipline.md`, the single source of truth — including its `defer_suite` exception and default.

**All modes:**
- Use `TaskUpdate` to mark tasks as `in_progress` immediately.
- Execute phase tasks sequentially (Step 3.1, 3.2, etc.)
- Use `hs:ui-ux-designer` for frontend
- Each phase's red→green (3.T+3.I) runs in place at main by default; `@developer` takes it only when the workflow-weight posture says subagent (past the threshold, or a forcing flag) — main keeps verify + review of the slice's code AND test + commit either way (`references/per-phase-tdd.md`)

**Parallel mode:**
- Utilize all tools of Claude Tasks: `TaskCreate`, `TaskUpdate`, `TaskGet` and `TaskList`
- Launch multiple `hs:developer` agents
- When agents pick up a task, use `TaskUpdate` to assign task to agent and mark tasks as `in_progress` immediately.
- Respect file ownership boundaries
- Wait for parallel group before next

**Output:** `✓ Step 3: Implemented [N] files - [X/Y] tasks complete`

### Step 3.S: Conditional Simplify (live-diff gated)

Recompute signals from the live worktree (no hook state):

```bash
totals=$(git diff --numstat HEAD --ignore-all-space)
loc=$(echo "$totals" | awk '{s+=$1+$2} END {print s+0}')
files=$(echo "$totals" | awk 'NF{c++} END {print c+0}')
maxFile=$(echo "$totals" | awk 'BEGIN{m=0} {if ($1>m) m=$1} END {print m+0}')
modified=$(git diff --name-only HEAD)
```

Read thresholds from `"${HARNESS_BIN_ROOT:-.}"/harness/data/simplify-policy.yaml`
(`thresholds.{loc_delta,file_count,single_file_loc}`), shipped at 400 / 8 / 200. If any threshold is breached, spawn the simplifier scoped to the modified files:

```
Task(subagent_type="hs:code-simplifier", prompt="Simplify these files while preserving behavior exactly: [file-list]", description="Simplify recent edits")
```

After the subagent returns, log only — never re-run or block:
- `git diff --shortstat HEAD -- [file-list]` changed → "simplifier made scoped edits"
- unchanged → "simplifier ran clean"

There is no way for you to skip this step. `simplify_gate.py` carries no env knob
by design — an agent cannot turn it off or move a threshold. The only control is
`mode: off|warn|block` in `simplify-policy.yaml`, which is in the write_guard
GUARD_LIST and human-edited only. It ships on `warn`.

**Output:** `✓ Step 3.S: Simplify [ran|skipped] - [scoped changes|clean|under threshold]`

### [Review Gate 3] Post-Implementation (skip if auto mode)
- Present implementation summary (files changed, key changes)
- Use `AskUserQuestion` to ask: "Proceed to testing?" / "Request implementation changes" / "Abort"
- **Auto mode:** Skip this gate

## Step 4: Testing (skip if no-test mode)

**All modes (except no-test):**
- Do NOT write new tests here — each phase's tests were written test-first inside its `@developer` slice (Step 3 red→green); Step 4 runs the EXISTING suite via `@tester`
- When the measured disposition owes an independent run (see Critical Rules), spawn `hs:tester`: `Task(subagent_type="hs:tester", prompt="Run test suite", description="Run tests")`; a measured waiver runs the suite at main
- If failures: spawn `hs:debugger`, fix, repeat
- Forbidden: fake mocks, commented tests, changed assertions

**Output:** `✓ Step 4: Tests [X/X passed] - tester subagent invoked`

### [Review Gate 4] Post-Testing (skip if auto mode)
- Present test results summary
- Use `AskUserQuestion` to ask: "Proceed to code review?" / "Request test fixes" / "Abort"
- **Auto mode:** Skip this gate

## Step 5: Code Review

**Measured disposition** (`hs-run cook next`):
- NOT cleared — any trigger, a risk tier above tiny, an unmeasurable range — **MUST** spawn `hs:code-reviewer` with the (a-e) checks and scout/acceptance context; **DO NOT** review the diff yourself
- A measured waiver (small, tiny-risk diff) EARNS the opt-out: no independent spawn owed, review may fold into main's verify
- Invoke `/hs:code-review` skill for full checklist protocol
- **Effort pass-through:** the cook run's `--review <low|medium|high|xhigh|max>` (default `low`)
  is forwarded as the `--effort` flag to `/hs:code-review` — e.g. `--review high` → the
  subagent runs `hs:code-review --effort high`.
- This dials review depth only; it never skips the gate. No `skip` value exists — the floor is
  always a real review writing `review-decision.json`.
- When `--review` is absent, effort resolves via the skill's own precedence
  (`HARNESS_REVIEW_EFFORT` env > `code-review.yaml` > `low`).

**Interactive/Parallel/Code/No-test:**
- Interactive cycle (max 3)
- Requires user approval

**Auto:**
- Auto-approve only if the `review-decision` verdict is `PASS` and the artifact validator passes
- Auto-fix critical (max 3 cycles)
- Escalate to user after 3 failed cycles

**Fast:**
- Simplified review, no fix loop
- User approves or aborts

**Output:** `✓ Step 5: Review [score]/10 - [Approved|Auto-approved] - code-reviewer subagent invoked`

**Artifact gate:** Step 5 must write the review verdict through `"${HARNESS_BIN_ROOT:-.}"/harness/scripts/write_review_decision.py` (the artifact schema lives in `_shared/workflow-artifacts.md`); a HARD stage needs exactly `PASS`.

For high-risk `--auto`, stop with AskUserQuestion before finalize/commit/ship — the risk ceremony is skill-enforced, so the stop is on you, not on a gate.

## Step 6: Finalize

**All modes — MANDATORY work, delegation optional (nothing here re-derives a judgment):**
1. **MUST** run sync-back (MANDATORY) — inline, or via the `hs:project-manager` agent — for [plan-path]: reconcile all completed Claude Tasks with all phase files, backfill stale completed checkboxes across every phase, then update plan.md frontmatter/table progress. Do NOT only mark
   current phase.
2. **MUST** update the docs the change touched — inline, or `Task(subagent_type="hs:docs-manager", prompt="Update docs for changes.", description="Update docs")`.
3. Project-management sync-back MUST include:

### Status Sync (Finalize)

Use CLI commands for deterministic status updates:

```bash
# Edit per-phase checkboxes directly — only change the Status column cell, preserve table structure
```

   - Sweep all phase files in the plan directory — `phase-XX-*.md` at root AND
     `phases/phase-*.md` in the subdir.
   - Mark every completed item `[ ] → [x]` based on completed tasks (including earlier phases finished before current phase).
   - Return unresolved mappings if any completed task cannot be matched to a phase file.
   - **Close the plan deterministically** once ALL phases are done — do NOT hand-edit the frontmatter status:

```bash
# Flips plan.md frontmatter status in_progress -> completed (idempotent, surgical).
# A plan left in_progress pins the gate's active-plan resolver to a stale plan and
# blocks unrelated shipping — closing it removes that failure mode.
python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/close_plan.py <absolute-plan-dir>
```
4. Use `TaskUpdate` to mark Claude Tasks complete after sync-back confirmation.
5. Onboarding check (API keys, env vars)
6. **MUST** stage and commit — inline, or `Task(subagent_type="hs:git-manager", prompt="Stage and commit changes", description="Commit")`.

Step 6 is incomplete without the sync-back, the docs update, and the commit. Who performs them is your call; skipping any of them is not.

**Auto mode:** Continue to next phase automatically, start from **Step 3**.
**Others:** Ask user before next phase

**Output:** `✓ Step 6: Finalized - Full-plan sync-back completed - Docs updated - Committed`

## Critical Rules

- Never skip steps without mode justification
- **DELEGATION FOLLOWS THE MEASURED DISPOSITION:** Steps 4-5 delegate when the disposition `hs-run cook next` reports owes the independent run (any trigger, above-tiny risk, or unmeasurable range); a measured waiver (small, tiny-risk) lets them run at main.
  - Step 4: `hs:tester` (and `hs:debugger` if failures)
  - Step 5: `hs:code-reviewer` / `/hs:code-review`
  - Step 6 is mechanical: mandatory work, delegation optional.
- Use `TaskCreate` to create Claude Tasks for each unchecked item with priority order and dependencies (or `TodoWrite` if Task tools unavailable).
- Use `TaskUpdate` to mark Claude Tasks `in_progress` when picking up a task (skip if Task tools unavailable).
- Use `TaskUpdate` to mark Claude Tasks `complete` immediately after finalizing the task (skip if Task tools unavailable).
- All step outputs follow format: `✓ Step [N]: [status] - [metrics]`
- **VALIDATION:** Task calls must MATCH the disposition — zero spawns on a measured waiver is complete; zero while it still owes test or review is INCOMPLETE.

