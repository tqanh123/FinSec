# Parallel execution for cook (opt-in)

`cook` cooks independent phases concurrently to cut wall-clock. **Opt-in**
(`cook.parallel: false` ships; force with `--parallel`/`--subagents` or flip the
knob) — the partitioner only fans out phases marked `parallel_safe` with disjoint
`owns`; every other phase stays sequential, so a forced run never parallel-edits
a shared path. Without the force, cook runs one phase at a time, main
implementing in place per the workflow-weight posture.
This drawer = full protocol; SKILL.md section = summary. Backing rule: `"${HARNESS_BIN_ROOT:-.}"/harness/rules/orchestration-protocol.md` (delegation context, parallel-ownership safety, `claims.py` 1-winner, status protocol).

## When NOT to use it

Parallelism buys nothing — and adds merge risk — when phases are a dependency chain, when ownership cannot be cleanly split, or when the plan has ≤2 independent phases. The partitioner keeps a coupled or non-`parallel_safe` phase sequential on its own even inside a forced run — the env off-switch (`HARNESS_COOK_PARALLEL=0`) is only for forcing the WHOLE run sequential.

## 1. Resolve the opt-in (deterministic, not eyeballed)

Precedence, highest first:

1. `--parallel` flag on the cook invocation
2. `HARNESS_COOK_PARALLEL` env (`1/true/yes/on` → on)
3. `cook.parallel` in `"${HARNESS_BIN_ROOT:-.}"/harness/data/cook.yaml` (shipped **false** — opt-in)
4. built-in fallback **false** (only reached if the config layer is absent)

`cook.parallel_max` (default 4) is an advisory cap the orchestrating agent applies when fanning out — the planner below emits the safe partition but does NOT itself limit concurrency:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/cook/scripts/cook_parallel_plan.py --root . --phases-json phases.json --expand
# add --parallel to force ON; prints {parallel_enabled, parallel, sequential, conflicts}
```

## 2. Build the phase list

Each phase file declares its parallel eligibility in frontmatter:

```yaml
parallel_safe: true
owns:
  - harness/hooks/simplify_gate.py
  - harness/tests/test_simplify_gate.py
```

`owns` is the set of paths (globs allowed) the phase may create or modify. A phase with no `parallel_safe: true` is always sequential.

## 3. Partition (safety core)

`cook_parallel_plan.partition()` returns three lists:

- `parallel` — `parallel_safe` phases whose `owns` are disjoint from every other `parallel_safe` phase. These may run concurrently.
- `sequential` — everything else: non-parallel-safe phases, plus any phase whose ownership **overlaps** another (overlap demotes BOTH, conservatively).
- `conflicts` — the overlapping pairs and the shared paths, so the fallback to sequential is logged, never silent.

Rule, non-negotiable: **never run two phases that touch a shared path in the same batch.** Same file, a generated artifact, a migration sequence, or shared config all count as shared.

**An import edge is also a shared path.** Two phases can own no file in
common and still be unsafe together: one's file imports the other's, so a
write to it changes what the other sees mid-run. `cook_parallel_plan`
auto-selects the project's own `hs-code graph snapshot` of HEAD from the
store — the same store_base+select_snapshot selection every other hs-run
fact provider already uses, schema- and freshness-checked, so there is no
snapshot directory to name here:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/cook/scripts/cook_parallel_plan.py \
  --root . --phases-json phases.json --expand
```

`cook_parallel_plan.partition` then demotes a disjoint pair joined by a real
import edge, using the SAME check `plan_graph.find_import_edge_conflicts`
runs at plan-approval time, so the two never quietly disagree. No fresh
snapshot of HEAD in the store (nothing adopted, or none current) degrades to
the file-overlap check alone — never a crash, never a false claim of safety.
`--units-json <path>` stays available as an EXPLICIT override, for a fixture
or a snapshot other than the project's own HEAD one.

## 4. Delegate each parallel slice

One `hs:developer` subagent per slice, in its own **worktree** (isolation prevents parallel edits from colliding). Pass `isolation="worktree"` **explicitly on the spawn** — `@developer` does not
self-isolate from its frontmatter, so a slice spawned without the param would run in-place and collide with its peers.
The prompt MUST carry the full delegation context (orchestration-protocol): task · files allowed to read · files allowed to modify (the phase's `owns`) · acceptance criteria · constraints · work-context path · env (CWD, OS). Ownership is enforced 1-winner via
`"${HARNESS_BIN_ROOT:-.}"/harness/scripts/claims.py`.

**Worktree base ref (non-negotiable for dependency-coupled phases).** An isolated worktree branches from the configured base ref — which may be the fresh origin-default, NOT the current branch HEAD. A parallel slice fanned out in the same run as an earlier phase it depends on (it reads a module/file the predecessor just committed) will find that prerequisite ABSENT in the worktree, and the
subagent will improvise — reconstructing the predecessor's files inside its own slice, violating ownership and producing a diff that cannot merge cleanly. Before delegating ANY slice whose phase depends (per the plan-graph edges) on an in-session committed phase: (a) ensure the worktree branches from the current HEAD (so prerequisite phases are present), and (b) verify each prerequisite file
is present in the worktree (`ls <worktree>/<prereq>`). An absent prerequisite means the base ref is wrong — STOP, fix the base, do not let the subagent rebuild it. A phase with no in-session dependency may use the default base. (See `harness/LESSONS.md`.)

Each subagent ends with the status protocol block (`DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT`). `BLOCKED`/`NEEDS_CONTEXT` → change scope/approach, do not re-run the same prompt.

## 5. Verify every slice — MANDATORY, never trust on sight

A returned subagent is an **unverified claim**, not a result. Two tiers:

1. **Self-verify (always)**: cook re-runs that slice's tests + lint and reads the diff against the phase's acceptance criteria. Any red → the slice does not merge.
2. **Independent verify (risky slices)**: spawn an `hs:independent-revalidator` (or `hs:code-reviewer`) subagent that re-derives correctness from the diff ALONE, without the builder's reasoning. Disagreement → the slice returns to sequential rework.

Risky = touches a gate/hook, changes a contract, or has thin test evidence. When unsure, verify independently.

## 6. Integration barrier

After all verified slices merge into the working tree, run the **full suite serially** — the real green gate. Only then write `plans/<plan>/artifacts/verification.json` and commit. `gate_stage.py` is unchanged: `--parallel` never bypasses the artifact gate.

**`--parallel` + `defer_suite` together:** `verification.json` is last-write-wins, so this
barrier's full-suite evidence only survives to `cook close` if the barrier happens to be
the LAST write in the run — a later per-phase scoped write overwrites it. Do not read "the
barrier ran the full suite" as the `defer_suite` debt already paid; check the actual latest
verdict (`"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/cook/references/deferred-suite.md`).

## Failure handling

- Partition reports a conflict → those phases run sequentially; log the shared paths.
- A slice fails verification → pull it out of the parallel batch, rework sequentially.
- Worktree merge conflict → ownership was mis-declared; fix `owns`, fall back to sequential.
- The integration suite goes red after a clean per-slice pass → an integration gap the per-slice tests missed; bisect by slice, this is exactly what the barrier exists to catch.
