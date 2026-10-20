# `hs:handoff --dispatch` runtime catalog

This file owns the **dispatch POLICY** of `hs:handoff --dispatch` — which
runtime IDs may appear in the `--dispatch <runtime>` menu. It never asserts
whether a candidate is reachable RIGHT NOW (binary present, lane turned on,
credential set) — that is READINESS, checked at dispatch time by whichever
engine actually reaches the runtime (`hs:coding-agent-orchestration`,
`hs:gemini`, `hs:partner`, or the Agent tool for `internal`). Keeping the two
questions apart means a policy edit here can never silently claim a
capability, and a readiness change there can never silently widen who is
allowed to receive a job.

## Candidates

Every ID below is either declared in `harness/data/runtime-targets.yaml` (the
harness's own install-target capability table) or is one of the two internal
delegated lanes plus the in-session subagent — every runtime declared in
runtime-targets, the partner lanes, and the internal subagent.

| ID | Kind | Dispatch engine | Readiness owned by |
|---|---|---|---|
| `internal` | In-session subagent | `Agent` tool spawn (a role matched to the task, fed the artifact path) | Always reachable — no external process |
| `gemini` | CLI, external | `hs:gemini` → `gemini_companion.py task -p …` via the `hs:lane-relayer` subagent | `hs:gemini`'s own config gate (`gemini-partner.yaml` `master: on`) + engine credential probe |
| `ccs` | CLI, external | `hs:partner` → `partner_companion.py task --provider <name> -p …` via the `hs:lane-relayer` subagent | `hs:partner`'s own preflight (`command -v ccs` + the named provider present in ccs's live discovery list) |
| `claude-code` | Install target | No live spawn mechanism in this harness — dispatch hands off the artifact + the continuation instruction for a human to paste into a new Claude Code session | n/a — instructive only |
| `codex` | Install target | No live spawn mechanism | n/a — instructive only |
| `cursor` | Install target | No live spawn mechanism | n/a — instructive only |
| `grok` | Install target | No live spawn mechanism | n/a — instructive only |
| `pi` | Install target | No live spawn mechanism | n/a — instructive only |
| `omp` | Install target | No live spawn mechanism | n/a — instructive only |
| `agy` | Install target | No live spawn mechanism | n/a — instructive only |
| `dsh` | Install target | No live spawn mechanism | n/a — instructive only |
| `portable` | Install target | No live spawn mechanism | n/a — instructive only |

**Why the 9 install targets are listed at all** even though nothing here can
launch them: a user legitimately hands off to a coding agent that already has
its own session open on the same repo (a human runs Codex or Cursor
separately, and wants a portable continuation artifact for it). `hs:handoff`
still captures and validates the same way; it is honest that it cannot press
"go" on their behalf. Naming the ID in `--dispatch` gets the correctly-shaped
report (Scenario in `dispatch-scenarios.md`) instead of a bespoke workaround.

**Live dispatch** (a real process actually starts) exists for exactly three
IDs: `internal`, `gemini`, `ccs`.

## Not dispatchable

| ID | Rejection reason |
|---|---|
| any ID not in the table above | Not a declared runtime — `harness/data/runtime-targets.yaml` is the SSOT for install targets; the partner lanes and `internal` are the only additions. Refuse rather than guess a near match. |

## User-supplied IDs

An unrecognized `--dispatch <id>` refuses immediately with:

> `<id>` is not in the hs:handoff --dispatch menu. Declared runtimes:
> internal, gemini, ccs, claude-code, codex, cursor, grok, pi, omp, agy, dsh,
> portable. See references/dispatch-runtime-catalog.md.

No substitution, ever — the whole point of naming a runtime is that the user
chose it.

## Adding or removing a runtime

- Adding: the ID must already exist as either a `harness/data/runtime-targets.yaml`
  target, or a declared internal/partner lane. This file never invents a new
  runtime on its own — it is a menu over capabilities declared elsewhere.
- Removing: move the row out and update the refusal list. Cite the underlying
  change (a target retired from `runtime-targets.yaml`, a lane removed).

## Invariants

- No live-probe result is recorded here — a "ready now" fact belongs to the
  engine that owns that runtime, never to this policy file.
- No model/flag/capability claim beyond "dispatch engine exists or does not."
- Every "Install target" row stays instructive-only until this harness grows
  a real spawn mechanism for it; claiming otherwise here would be the exact
  overclaim the harness's own honesty discipline refuses elsewhere.
