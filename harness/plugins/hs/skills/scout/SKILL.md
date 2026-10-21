---
name: hs:scout
injectable: true
description: Fast codebase exploration using parallel agents — find files, locate code, gather context before implementing or debugging. Output goes to plans/reports/.
argument-hint: "[ext] [--ultra [N] | --debate]"
allowed-tools: [Bash, Read, Write, Grep, Glob, Task]
metadata:
  compliance-tier: workflow
---

# hs:scout — structured codebase exploration

Find files/patterns/code relationships token-efficiently via codebase split + parallel agents. Result = scout report in `plans/reports/`.

## Modes / Flags

| Argument | When | Backing |
|---|---|---|
| _(default)_ | internal scouting with `Explore` subagents | `references/internal-scouting.md` |
| `ext` | external scouting with Gemini/OpenCode CLI | `references/external-scouting.md` |

No argument → parse prompt, auto-pick mode.

## Fast path — one lookup

One pathname or one symbol: search locally, return the verified path and its role in the
answer. No subagents, no report file. Reuse an existing scout report while its revision
and the files it covers are unchanged. The workflow below is for SEVERAL independent
areas; `--ultra` keeps its own five-candidate contract regardless.

A question that follows imports MORE than one hop ("who imports X, directly or through
modules that import it") goes to the import graph, not a hand trace: read
`references/import-graph.md`. One hop stays a search.

A question about where a STRING appears -- an env name, a config key, a message, a name in
docs or docstrings -- goes to `graph grep`: read `references/string-search.md`. Each match is
typed by where it sits, so a docstring mention never reads as a use.

## Workflow

### 1. Analyze the task
- Parse prompt: search target, extension, directory, pattern.
- Estimate SCALE (agents needed): small ≤3, medium 4-5, large ≥6.
- If SCALE ≤2: do not spawn subagents — use Grep/Glob directly (overhead not worth it).

### 2. Divide and conquer
- Partition the codebase by directory/pattern — no overlap.
- Each agent receives a distinct, clearly scoped assignment.

### 3. Register tasks (when SCALE ≥ 3)
- `TaskList` first — check for existing scout tasks and reuse if present.
- If none: `TaskCreate` per agent, mark `in_progress` before spawning.
- Schema detail: `references/task-management.md`.

### 4. Spawn agents in parallel
- **Route first through `hs:workflow-orchestrate`** (before any spawn) — state `reason` (why this scout fan-out), `strategy` (mode + base + angle→count), `scope` (SCALE + directory surface, variable count).
  Scout's count is **variable** (SCALE from step 1), so the route also sizes/groups: consume `route_depth` — `light` → proceed via the fallback below; `agent` → escalate the `@workflow-orchestrator` agent before spawning.
  The exact sizing commands + the `groupCap`/`earlyWrite` handoff live in `"${HARNESS_BIN_ROOT:-.}"/harness/rules/orchestration-protocol.md`.
- **ultracode opt-in present** → orchestrate the parallel fan-out (+ mechanical path-dedup) via the shared `Workflow({name:"hs:base-fanout-consolidate", args:{lenses, findingsSchema, dedupKeyFields}})` (one lens per search-angle; deterministic fan-out; `scriptPath` if the name is not registered). Search-angle prompts are built as data, not callbacks.
- **opt-in absent** (mandatory fallback — Workflows are plan-gated) → the inline parallel spawns below:
  - **Internal (default):** `references/internal-scouting.md` (Explore subagents — spawn each with `model:"haiku"`, see that reference).
  - **External (ext):** `references/external-scouting.md` (Gemini/OpenCode CLI).
  - Send all spawns in a single message (1 message = true parallelism).
  - 3-minute timeout per agent — drop unresponsive agents, do not retry.
- **Stamp** the path that ran (`Workflow(name)` | `Workflow(scriptPath)` | `inline-Task fallback`) in the report. Resolve the opt-in per `"${HARNESS_BIN_ROOT:-.}"/harness/rules/orchestration-protocol.md`.
- **Route-all (gemini partner lane, opt-in — DORMANT at factory)** — before the fan-out, check `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/gemini_partner_config.py --should-route scout`.
Factory default is `claude` (mode=partner) so this branch does nothing on a shipped install. Only when it prints `route` (mode=route-all AND scout ∈ `route_all_surface`) run each search-angle through the single chokepoint —
  `python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/scripts/gemini_companion.py research -p "<angle>"` — and consume its provenance-stamped JSON. A `degraded`/`inert` reply → record **"degraded to claude"** LOUDLY and fall back to the native scout, never a silent swap.

### 5. Collect + report
- `TaskUpdate` each task: `completed` or `metadata.error: "timeout"`.
- Merge results, deduplicate paths, write scout report to `plans/reports/`.
- Return absolute path of the report + list of open questions.

## Report format

```markdown
# Scout Report — <target>

## Relevant files
- `/absolute/path/to/file.py` — short description

## Observed patterns

## Open questions
```

## Boundaries

- DO NOT edit code or create files outside `plans/reports/`.
- Scout output is input for `hs:research` (external), `hs:debug`, `hs:fix`,
  `hs:code-review` — scout does not make implementation decisions.
- The `ext` flag requires Gemini/OpenCode to be installed; if missing → fall back to internal automatically and record `[FALLBACK_INTERNAL]` in the report.
- Large context budget (SCALE ≥ 6): check `hs:context-engineering` before spawning.
- To pack the full repo before scouting: run the `repomix` CLI (outputs one XML file for the LLM).
- `--ultra [N]`/`--debate`: call `hs:workflow-orchestrate --ultra [N]|--debate --run-id <slug>` to size + drive N independent scout passes over the SAME target (on top of this skill's own internal Explore fan-out, no shared context between the N); `hs:escalation-consultant` synthesizes the findings. Approval gate is `hs:workflow-orchestrate`'s own.

## HARD-GATE (real wiring)

- Reports must exist only inside `plans/reports/` (CLAUDE.md hard-constraint 3) — markdown elsewhere breaks it; no gate blocks it; the advisory fence scan (`check_fence.py`) reports an out-of-zone file afterwards.
- Scout does not approve or implement anything — all next steps require a human or another skill that takes the scout report as input.
