# Capture-mode acceptance scenarios

Fixtures for validating the capture half of `hs:handoff` (no `--dispatch`).
Load when checking capture behavior, not routinely.

## Scenario 1 — clean workspace, bare invocation

**Given** a clean workspace on a feature branch, no prior handoff for this focus.
**When** `/hs:handoff` runs with no flags.
**Expect** the artifact writes under `plans/reports/handoff-<timestamp>-<slug>.md`
(or `plans/handoffs/` if that convention already exists in the project); all
nine sections present; `Current state` records branch/HEAD/"working tree
clean"; sections with nothing trustworthy read `Not captured in this
session`; the return prints the absolute path plus one continuation line.

## Scenario 2 — dirty workspace with `--include-diff --include-status`

**Given** modified and untracked files exist.
**When** `/hs:handoff --include-diff --include-status` runs.
**Expect** `Current state` lists changed/untracked files (bounded to 200,
marked `… truncated …` past that); a `git diff --stat` + first 200 lines of
`git diff` appear, redacted; a `git status --short` snapshot appears,
redacted; `Work performed` cites the commands actually run.

## Scenario 3 — missing git repository

**Given** the working directory is not inside a git repo.
**When** `/hs:handoff` runs.
**Expect** the artifact still writes; `Current state` is present with
`Not captured in this session` for every git-derived field; no git command
beyond the initial `rev-parse --is-inside-work-tree` probe runs.

## Scenario 4 — secret redaction fixture

**Given** the session captured output containing (fake, never real) values
like `AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` and
`Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.sig-fake-signature-01`.
**When** `/hs:handoff` runs.
**Expect** the artifact contains `[redacted:aws-secret-key]` and
`[redacted:jwt]`; no substring of either raw value appears anywhere; `Work
performed` states the total redaction count. See `redaction-patterns.md` for
why the JWT-shaped value collapses to one marker rather than two.

## Scenario 5 — explicit `--output`

**Given** `--output plans/reports/handoff-callback.md`.
**When** `/hs:handoff --output plans/reports/handoff-callback.md` runs.
**Expect** the artifact writes to exactly that path (parent directory created
if needed); no auto-derived slug/timestamp is applied; the return prints the
same explicit path; a path resolving outside the project workspace is refused.

## Scenario 6 — collision

**Given** the target path already exists.
**When** `/hs:handoff --output <existing path>` runs.
**Expect** refusal, naming the existing file's mtime and suggesting a
different `--output`; no overwrite. (Capture mode has no `--force` — the
existing skill's redacted-artifact contract treats an existing handoff as
something to read and supersede explicitly with a new path, not silently
clobber; a project wanting an update writes a fresh slip and links back to
the prior one under Source pointers.)

## Scenario 7 — `--model` / `--cwd` are capture no-ops without `--dispatch`

**Given** `/hs:handoff --model claude-sonnet-5 --cwd /tmp/other-repo` runs with
no `--dispatch`.
**Expect** the artifact captures the CURRENT session's state exactly as
Scenario 1 — `--model` and `--cwd` are dispatch-only fields
(`dispatch-job-spec-template.md`) and are ignored (not silently honored, not
an error) outside `--dispatch`, since capture and dispatch are separate acts.

## Scenario 8 — `--yes` alone does not dispatch

**Given** `/hs:handoff --yes "continue the callback fix"` runs with no
`--dispatch`.
**Expect** capture only, same as Scenario 1. `--yes` changes the approval
posture of a dispatched job; on its own it authorizes nothing, because there
is no job.
