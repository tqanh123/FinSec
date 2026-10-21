# Dispatch-mode acceptance scenarios

## Scenario 1 — internal subagent, fresh capture

**Given** a fresh capture (dispatch always captures its own artifact — there is
no flag to reuse a prior one).
**When** `/hs:handoff --dispatch internal "continue the callback fix"` runs.
**Expect** a fresh artifact writes and passes `validate_slip.py`; the Agent
tool spawns with the artifact path and task text in its prompt; the report
names `internal` as the runtime and `internal` as the dispatch engine.

## Scenario 2 — fresh capture, gemini

**Given** a clean-enough workspace and a stated task focus.
**When** `/hs:handoff --dispatch gemini "review the callback fix"` runs.
**Expect** capture runs exactly as the no-dispatch case, the fresh artifact
passes `validate_slip.py`, then the `hs:lane-relayer` subagent runs
`gemini_companion.py task -p …` with the artifact path and task text; the
report cites gemini's own provenance stamp. There is no flag to reuse a prior
artifact — every dispatch captures its own, so the state a runtime receives
is never stale.

## Scenario 3 — ccs without a provider

**Given** `--dispatch ccs` with no provider named anywhere in the task text.
**When** the skill runs.
**Expect** a stop before any relayer spawn: `hs:partner` requires an explicit
provider on every call (no default), so this skill asks for one rather than
guessing — never a silent pick.

## Scenario 4 — install-target ID, instructive report

**Given** `--dispatch codex "implement the next action"`.
**When** the skill runs.
**Expect** capture + validation proceed exactly as any dispatch; the report's
`Dispatch engine` reads `instructive-only`, `Job result` reads
`not-applicable`, and `Next action` names the exact path plus the sentence a
human pastes into their own Codex session. No process is spawned.

## Scenario 5 — validation failure blocks dispatch

**Given** the just-captured artifact's `Exact next actions` section has no
`**First safe step**` item.
**When** `/hs:handoff --dispatch internal "…"` runs and capture produces that
slip.
**Expect** `validate_slip.py` reports the missing marker; dispatch stops
before any subagent spawn or relayer call; the report names the failing
check(s) and the artifact path so nothing is silently discarded.

## Scenario 6 — unrecognized runtime

**Given** `--dispatch fooruntime`.
**When** the skill runs.
**Expect** immediate refusal quoting `dispatch-runtime-catalog.md`'s menu; no
capture is skipped because of it — capture still runs and the artifact is
still written before the refusal is reported, so no session state is lost.

## Scenario 7 — `--model` rejected for `internal`/`ccs`

**Given** `/hs:handoff --dispatch internal --model claude-sonnet-5 "…"`.
**When** the skill runs.
**Expect** immediate refusal explaining `--model` has no meaning for an
internal subagent's own role default; suggests dropping the flag or choosing
a CLI-backed runtime (`gemini`).

## Scenario 8 — secret in the task-focus string

**Given** the user pastes a bearer token into the positional task text.
**When** `/hs:handoff --dispatch gemini "use Bearer eyJ… to reproduce"` runs.
**Expect** refusal before capture: the task-focus string itself must not be
silently redacted (the user has to see why), so the invocation stops and asks
for a rephrase. No artifact is written; no relayer call happens.

## Scenario 9 — `--yes` on an internal write

**Given** the artifact's `Exact next actions` includes a write step, and
`/hs:handoff --dispatch internal --yes "apply the fix"` runs.
**Expect** the spawned subagent's brief states the approval posture as
"proceed" rather than "confirm before writing" — still inside whatever tool
permissions the session already grants; `--yes` never grants a permission the
session did not already have.
