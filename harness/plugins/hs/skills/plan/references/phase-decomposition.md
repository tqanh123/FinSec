# Phase decomposition — phase design and plan.md (on-demand)

Runs AFTER research, WHEN the planner writes plan.md + phase files. This is the correct technique for decomposing phases so cook can execute them — not a descriptive outline.

## Delegate phase-writing to `@planner` (sub-by-default; bookends stay at main)

On a `--hard` plan the mechanical middle — writing `plan.md` + the phase files — is **delegate-by-default** to a `@planner` subagent (lane `plans/**`, so it writes in-lane; `plan_approval` hashes the layout AFTER the write, so a delegated write does not skew the hash).
This isolates the plan-writing context and pins the standards read-directive onto a fresh subagent. `--in-place` (manual override, wins over mode) or a `--fast` plan keeps it inline.

**Bookends STAY at main (hard, never delegated):** the four interview steps — **understand**, **scope-challenge**, **validate**, **approval** — run only at main because a subagent has **no TTY**, so `AskUserQuestion` dies inside it, and because these are human decisions the harness must not auto-resolve.
Only the write-phase and red-team are eligible for delegation — the whole-plan consistency sweep stays at main (it feeds the human-approval gate, so it must not be delegated).
The delegation-context passed to `@planner` (task · read/modify globs · acceptance · constraints · env) mirrors `"${HARNESS_BIN_ROOT:-.}"/harness/rules/orchestration-protocol.md`; the planner still self-verifies inline (the two-way Evidence Filter) and the main thread runs the approval gate.

## Plan directory structure

```
plans/<timestamp>-<slug>/
├── research/               # researcher reports (path passed to planner)
├── reports/                # red-team / validate reports
├── plan.md                 # overview, required YAML frontmatter
├── plan-graph.yaml         # machine phase-DAG sidecar (mandatory)
├── artifacts/              # gate evidence — written at cook/approve/review time, NOT by plan
└── phases/
    ├── phase-1-<name>.md
    └── phase-N-<name>.md
```

Slug name: kebab-case, describing scope (e.g. `260615-1200-auth-refactor`).

**MANDATORY — stamp this skeleton with `scaffold.py`; do NOT hand-author the plan dir.** A hand-typed layout drifts from what the gates read: `plan_approval` hashes phase files at `phases/phase-*.md` (and legacy `phase-*.md` at root), so a phase file placed anywhere else is silently OUT of the approval hash — a phase edit then slips past drift detection.
Run:
`python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/scaffold.py plan --slug <slug> --title "<title>" --phases a,b,c` creates the dir, plan.md, and one `phases/phase-N-<name>.md` file each —
already carrying the correct `harness_version`/`kit_digest`/`schema_version` stamp (reused from `artifact_stamp`, so a new plan never ships a stale digest).
`--first 0` numbers the first phase 0; every file number is zero-padded to the width of the last one (`phase-01` … `phase-12`). The harness orders phases by number either way (2 before 10), so never rename files by hand to fix the order. Add a later phase by copying an existing `phases/phase-*.md` and listing it in plan.md `phases:` and `plan-graph.yaml`.
A rerun with the full `--phases` list and `--force` keeps every phase file already there and writes only the new ones, but rewrites plan.md from the template, so use it only before plan.md is filled. Never invent a new phase-file location. Then fill the TBD sections with the real decomposition below; `scaffold.py report --type <t> --slug <slug> --title "<title>"` does the same for a report.

### Reuse an existing discovery-brief folder — do NOT mint a sibling

When `hs:discover` ran first, it wrote `plans/<id>-<slug>/discovery-brief.md`. The plan for that work MUST land in the SAME folder, not a fresh timestamped sibling — otherwise the brief is orphaned and the plan loses its captured direction (the common failure when plan runs a while after the brief, so the handoff context is gone).

Find the brief folder, then scaffold INTO it:

1. If the discover→plan handoff gave a brief path, use its parent dir. Otherwise scan: `ls plans/*/discovery-brief.md` and pick the one whose slug matches this work and has NO sibling `plan.md` yet (an un-planned brief).
2. The dir name is `<id>-<slug>` where `<id>` is `YYMMDD-HHMM`. Pass BOTH to scaffold so it targets that exact dir:
   `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/scaffold.py plan --id <id> --slug <slug> --title "<title>" --phases a,b,c`.
   `--id` defaults to a FRESH `_now_id()` timestamp when omitted — that is what mints the sibling folder, so pass it explicitly whenever a brief dir exists.

The brief file is left in place; scaffold only adds `plan.md` + `phases/` alongside it (it refuses to clobber an existing `plan.md` without `--force`).

## Required YAML frontmatter for plan.md

`scaffold.py` writes these; fill in what it left as `TBD`, never hand-author the block:

```yaml
---
title: "<concise>"
status: pending
mode: hard            # hard | fast
tdd: true
branch: "<the branch this plan's work lands on>"
created: <YYYY-MM-DD>
author: <who owns the plan>
phases:
  - phases/phase-1-<name>.md
---
```

`created` is read from the id stem (`--id 261004-2310` -> `2026-10-04`), not the clock. `decisions:` takes `--decisions <id>,<id>`; no step reads it, so an empty list is fine.

### Optional key cook reads — `posture`

`posture: in_place|subagent` forces cook's per-phase implement posture past the declared-file threshold for every phase of the plan (same effect as `hs-run cook prompt --flags in_place` for one call). It is pinned by the approval hash; a flag that contradicts it is refused.

### Optional key the approval hash reads — `reference_dirs`

Long reference material a phase tells the executor to follow (tables, specs, worked examples) goes under `refs/` in the plan dir, linked from the phase file.
Every file there is pinned by the approval hash, linked or not. `reference_dirs: [refs, <dir>]` names other dirs; the key is itself pinned, and a dir outside the plan or under `artifacts/` refuses approval.
Keep the frontmatter and every heading in the phase file. A section body may move to `refs/`: leave one pointer line under its heading, `→ [refs/<p#>-NN-slug.md](../refs/<p#>-NN-slug.md) (N lines)`.
A reader that parses a section (`## Related Code Files`, plan.md's `## Red-team disposition`) reads a pointer line as the file it points to; a link in prose is not a pointer, and neither is a target the approval hash does not pin (keep it under `refs/` or a `reference_dirs` dir).
Do not name a reference file `phase-*.md` — that reads as a misplaced phase.
Living logs (a checklist, a decision log, evidences) stay outside; approval names any plan-dir file a phase links that the hash does not pin.

### Optional keys — nothing in the harness reads them

Add them only for a human reader (a board, a card preview); no gate or step
consumes them, so leaving them out changes nothing:

```yaml
description: "<1 sentence for card preview>"
priority: P2          # P1 | P2 | P3
effort: "<total estimate>"
tags: [relevant, tags]
# Passive provenance — standards rule ids this plan implements
# (STD-<AREA>-RG<n>-R<n>). Frontmatter is stripped from plan_hash, so adding
# it never forces a re-approval.
applies_rules: [STD-...]
```

## Each phase file must contain

- **Overview** — 1-2 sentence deliverable.
- **Requirements** — explicit functional + non-functional.
- **Related Code Files** — Create / Modify / Delete with full paths. `plan_graph.py` compares this table with the phase's `files_to_*`: it reads each row's first cell naming a path (the file column; a later cell such as a guarding test is not owned), skips a row whose action cell is `read`/`đọc`/`run`/`chạy`, and ignores prose under the table.
- **Implementation Steps** — numbered, specific enough for another developer to execute.
  A command the phase hands cook to paste as-is is a claim that it runs and prints what the phase says.
  If it can run on the plan's base now (read-only, everything it imports already merged), RUN it while planning and write the observed output beside it.
  If it cannot (it reads work an earlier phase has not merged yet), say so beside it, with what it waits on.
  No gate checks this — a syntax check passes a command that runs in the wrong interpreter or counts the wrong lines; only running it shows either.
- **Success Criteria** — measurable checkboxes (not subjective).
- **Risk Assessment** — likelihood x impact; mitigation for High items.

A phase file declares no `status:`. A phase's state is derived from its `verification-<phase>.json`, and `hs-run cook status` reads it in its `phases` rows.

## Decomposition rules

1. **Each phase is self-contained**: no runtime dependency on a parallel phase.
2. **File ownership**: two phases with no path between them in `plan-graph.yaml` can run at the same time, so they must not touch the same file — `plan_graph.py` reports it as `parallel-conflict` (`merger_owned` files excepted). Phases ordered by an edge may both touch a file: the later one builds on the earlier.
3. **>8 files in one phase**: challenge -> split or merge (YAGNI).
4. **>3 phases**: ask the user "can any phases be merged?" before committing the structure.
5. **Naming honesty / SRP** (the Maintainer red-team persona will check): new file/module names must accurately and completely describe their real responsibility — `core.py` that also handles artifact I/O is a misleading name and needs splitting.

## Machine-readable phase-DAG sidecar (MANDATORY) — `plan-graph.yaml`

Every plan MUST ship a `plan-graph.yaml` sidecar next to plan.md so the phase-DAG is machine-checkable — no exception for phase count or `--fast` mode (a trivial plan ships a trivial sidecar; the cost is near-zero and the drift-guard + conflict-detection value is uniform). It carries edges + per-phase file ownership + each node's `post` artifact obligation —
**no status** (status is mutable and lives in plan.md's `## Phases`):

```yaml
edges:
  - {from: P1, to: P2}      # {from: A, to: B} = "A runs BEFORE B" (A is B's prerequisite)
subtasks:
  P3:
    files_to_create: [...]
    files_to_modify: [...]   # no status field — ever
    post: [verification-P3.json]   # end-of-phase artifact obligation
```

Each node **MUST** declare `post:` — the artifacts that phase must emit before it counts as done. Emit `[verification-<node>.json]` for every node; a review phase adds `review-decision.json` (e.g. `post: [verification-<node>.json, review-decision.json]`;
it is read the way the gate reads the artifact, so the `review-decision.yaml` the writer produces satisfies it).
`derive_plan_completion` reads `post` as the single source of truth for completion, and the ship/deploy gate blocks while any node's `post` is
missing. This is **enforced**: `plan_graph.py` HARD-fails (exit 2, both advisory and `--require` modes) on a node with a missing/malformed `post` — a structural contract violation, not an advisory. A sidecar that omits `post` will NOT pass the validate-step graph check or the cook preflight; declare it explicitly on every node (do not rely on a default). Do NOT add a `pre:` key — it is
deferred (the schema tolerates it, nothing reads it).

Conventions: the filename is fixed (`plan-graph.yaml`); the plan.md frontmatter `phase_graph:` marker is read-only (it does not source the hashed name); pin the edge direction explicitly. Parallelism is **derived, not authored** — do not add a `parallel:` field; a parallel batch is a topological antichain, and the real blocker is a shared file, not a logical edge. `plan_approval` raw-hashes
the sidecar (folded into the plan hash, so an edge change re-triggers the drift guard); `plan_graph.py` reads it read-only at Validate.

## Parallel mode — additional requirements

With `--parallel`: add a **dependency matrix** to plan.md (which phases run concurrently, which are sequential) and a **file ownership table** (phase -> files). Cook reads the matrix to spawn agents in parallel without conflicts.

## Deep mode (`--deep`) — per-phase scout

Each phase file in `--deep` must include:
- File inventory table (action, rough size, test impact).
- Test scenario matrix (critical / high / medium paths).
- Dependency map (links to phases this one depends on).

## Backing

- `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/agents/planner.md` — agent that writes plans using this template.
- `"${HARNESS_BIN_ROOT:-.}"/harness/rules/workflow-handoffs.md` — handoff structure plan -> cook.
- `"${HARNESS_BIN_ROOT:-.}"/harness/data/ownership.yaml` — confirm zone before placing a path (-> constraint-scan.md).
- `"${HARNESS_BIN_ROOT:-.}"/harness/scripts/plan_approval.py` — record approval after the user approves the plan.
