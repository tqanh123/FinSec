# Documentation update decision

Load when another skill/workflow must decide whether to update docs. To run the full docs flow, use `/hs:docs init`, `/hs:docs update`, or `/hs:docs summarize`.

## When to update

Update docs when a change affects:

- user-visible behavior
- setup, install, or CLI commands
- architecture, data flow, or public contract
- security posture or operational procedures
- future decisions that a maintainer should not have to rediscover
- an operational ROUTE established or changed by authorized work done OUTSIDE
  the repo — deploy path, log source, credential source, webhook/OAuth/DNS,
  backup/rollback, or a lookup route proven wrong. Nothing in the diff shows
  it, so nobody notices the docs went stale until the route is needed.

Do **not** add noise for purely internal changes unless the repo requires it.

## Standard docs for this repo

| File | Purpose |
|---|---|
| `docs/codebase-summary.md` | codebase overview, entry point |
| `docs/system-architecture.md` | architecture, data flow, components — **auto-loaded by hs:plan/hs:cook; keep THIN** (deep detail chaptered under `docs/system-architecture/`, not auto-loaded) |
| `docs/code-standards.md` | code standards, naming, patterns — **auto-loaded by hs:plan/hs:cook; keep THIN** |
| `docs/project-roadmap.md` or `docs/development-roadmap.md` | roadmap |
| `docs/project-changelog.md` | changelog when present |

The two auto-loaded docs must stay in sync with the code: editing architecture/standards code under `harness/` without updating them drifts the context plan/cook load (the `standards_drift_nudge` hook flags this at turn-end). Resync via `/hs:docs update`.

## Plan and report locations

- Plans: `plans/<timestamp>-<slug>/plan.md` + phase files
- Reports: `plans/reports/` (or `plans/<slug>/reports/`)
- Hard constraint: markdown is created **only** in `docs/` or `plans/`

## Handoff with other skills

- `hs:cook` / `hs:fix`: call docs update at finalize time only when the criteria above are met.
- `hs:plan`: read existing docs before creating architecture plans or phase files.

Read the existing doc BEFORE modifying it. After modifying: verify that dates, links, and claims match actual changes.
