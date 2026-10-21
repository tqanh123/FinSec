---
name: hs:handoff
description: Create a concise, redacted conversation handoff for a fresh agent session. Use when switching context, ending a work session, or preserving decisions and blockers.
user-invocable: true
when_to_use: Invoke for conversation-state compaction, not a git-derived project status report.
category: utilities
keywords: [handoff, session, context, decisions, blockers, dispatch, redaction]
argument-hint: "[task focus] [--output PATH] [--include-diff] [--include-status] [--dispatch <runtime> [--model NAME] [--cwd PATH] [--yes]]"
injectable: false
allowed-tools: [Read, Glob, Grep, Write, Bash]
metadata:
  compliance-tier: workflow
---

# Handoff

Create one Markdown artifact — a **continuation contract** — that lets a
fresh agent session resume in-progress work with minimal rediscovery: goal,
current state, decisions and why, verification status, blockers, and the
exact next safe action. Never a transcript dump, never invented context.

## Flags

| Flag | Effect |
|---|---|
| _(bare)_ | Capture with an auto-derived slug + timestamp. |
| `[task focus]` | Single-string focus for the successor; quoted verbatim in Mission. |
| `--output PATH` | Explicit artifact path (workspace-relative, parent created if missing). No auto slug/timestamp. Refuses on a path outside the project or an existing file at that path — there is no override; write elsewhere or read/supersede the existing one. |
| `--include-diff` | Append a redacted `git diff --stat` + first 200 lines of `git diff`, truncation marked. |
| `--include-status` | Append a redacted `git status --short` snapshot. |
| `--dispatch <runtime>` | Capture, validate, then hand off — see Dispatch below. Runtime must be in `references/dispatch-runtime-catalog.md`; an undeclared name is refused, never substituted. |
| `--model NAME` | Dispatch-only. Rejected for `--dispatch internal` and `--dispatch ccs` (see `references/dispatch-job-spec-template.md`). |
| `--cwd PATH` | Dispatch-only. Workspace root passed to the resolved engine; defaults to the current workspace root. |
| `--yes` | Dispatch-only. Flips the dispatched job's approval posture from confirm-first to proceed — never a permission escalation beyond what the session already grants. |

`--model`, `--cwd`, and `--yes` are capture no-ops without `--dispatch`
(Scenario 7/8 in `references/capture-scenarios.md`) — capture and dispatch
stay separate acts.

## Workflow

1. Read project instructions and relevant plans before drafting. Read a prior
   handoff for the same focus if one exists (`plans/reports/handoff-*.md`).
2. Batch independent, bounded, read-only probes before drafting Current
   state: `git rev-parse --is-inside-work-tree`, then (only if inside a repo)
   `--show-toplevel`, `--abbrev-ref HEAD`, `HEAD`, `status --short` (first 200
   entries, `… truncated …` past that), and — only under the matching flag —
   `git diff --stat` / bounded `git diff`. No repo → state
   `Not captured in this session` for every git-derived field and stop after
   the first probe.
3. Capture goal, current state, decisions + rationale, rejected approaches,
   blockers, verification status, source pointers — reference plans, issues,
   diffs, tests instead of copying them in. A section with nothing
   trustworthy gets the literal text `Not captured in this session`, never an
   empty body or an invented inference.
4. Redact every section and every appended diff/status block through
   `python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/handoff/scripts/handoff_redact.py`
   (`redact_text`) before write — see `references/redaction-patterns.md`. A
   secret inside the `[task focus]` string itself is NOT silently redacted:
   refuse the invocation and ask for a rephrase.
5. Write the artifact in `references/artifact-schema.md`'s exact section
   order, then validate it:
   `python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/handoff/scripts/validate_slip.py <path>`.
   A capture-only run still writes on a validator refusal (a human can fix it
   by hand) but reports the refusal beside the path. Save under
   `plans/reports/handoff-<timestamp>-<slug>.md` unless `--output` named an
   explicit path; if the project has no `plans/` root, ask for a safe
   location first.
6. Print the absolute artifact path and one continuation line:
   `Read <path> and verify Current state against the repo before acting.`
   Never print the artifact body inline.

## Dispatch (`--dispatch <runtime>`)

Capture first, always — never dispatch without the artifact already saved
and passing `validate_slip.py`. A validation failure is a hard stop: print
the failing check(s) and the artifact path, do not dispatch, do not "repair"
the slip silently (`references/dispatch-scenarios.md` Scenario 5).

1. Confirm `<runtime>` is declared in
   `references/dispatch-runtime-catalog.md` — the POLICY menu (which IDs may
   receive a job), never the readiness check (whether one is reachable right
   now, owned by the engine below). An undeclared name refuses immediately
   with the menu, no near-match substitution.
2. Compose the call per `references/dispatch-job-spec-template.md`'s field
   mapping for the resolved runtime kind.
3. Hand off:
   - `internal` → spawn the matched subagent (Agent tool) with the artifact
     path and task text in its prompt.
   - `gemini` → route through `hs:gemini` (its `hs:lane-relayer` subagent
     runs `gemini_companion.py task -p …`).
   - `ccs` → route through `hs:partner` (its `hs:lane-relayer` subagent
     runs `partner_companion.py task --provider <name> -p …`); refuse first
     if no provider was named anywhere in the task text — `hs:partner`
     requires one on every call, no default.
   - any other declared ID (`claude-code`, `codex`, `cursor`, `grok`, `pi`,
     `omp`, `agy`, `dsh`, `portable`) → this harness has no live spawn
     mechanism for it; report the artifact path and the exact instruction for
     a human to paste into that runtime's own session.
4. Report exactly the shape in `references/dispatch-job-spec-template.md`
   (`Handoff Dispatch Result`), never inlining the artifact body or a
   relayer/subagent's raw output.

Runtime selection, safety gates, and consolidation for `internal`/`gemini`/`ccs`
belong to those lanes — this skill composes the call and reports the result,
it does not re-implement any of their preflight or approval logic.

## Required Shape

See `references/artifact-schema.md` for the full contract; summary:

    # HANDOFF: <short title>
    Generated: <timestamp> · Session focus: <one line>

    ## Mission and current status
    ## Scope and guardrails
    ## Current state
    ## Decisions and rationale
    ## Work performed
    ## Verification
    ## Open risks and blockers
    ## Exact next actions
    ## Source pointers

`Exact next actions` is numbered; item 1 is bold-prefixed
`**First safe step**` — the validator refuses a slip missing it.

## References

Load only when the task needs it:

| Drawer | Content |
|---|---|
| `references/artifact-schema.md` | Full section contracts + the validator's pass conditions |
| `references/capture-scenarios.md` | Clean/dirty/repo-less/redaction/collision fixtures |
| `references/redaction-patterns.md` | The secret-pattern table + marker format + carve-outs |
| `references/dispatch-runtime-catalog.md` | The 12-ID policy menu, live-vs-instructive split |
| `references/dispatch-job-spec-template.md` | Flag-to-field mapping per runtime kind |
| `references/dispatch-scenarios.md` | Dispatch acceptance fixtures |

## Boundaries

- Not a status report. A report derived from branches, worktrees, plans, and
  repository history is a different job; this captures conversation state.
- Do not duplicate root project instructions or introduce new decisions.
- Never write a handoff outside the agreed project or reports location.
- Without `--dispatch`, stop after saving. Capture and dispatch are separate
  acts, and handing work to another runtime is not something to infer from a
  request to write a handoff.
