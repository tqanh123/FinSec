# Delegation judgment — shared by `hs:plan` and `hs:cook`

`hs-run` tells you **when** a step needs a model. It cannot tell you whether you delegated
that step or quietly did it inline. Nothing mechanical catches that, so the discipline is
entirely on you. This file is the discipline.

## The bookend rule: anything that asks the user stays at MAIN

A subagent has **no TTY**, so `AskUserQuestion` dies there. Every step that must ask a human
is therefore held at main and is never delegated — for planning that is understand ·
scope-challenge · validate · approval · the probe-first empirical checks · the constraint scan ·
the final consistency sweep; for execution it is every STOP-and-ask and the per-phase review
of a delegated slice.

The delegated middle is the work that produces an artifact for you to judge: research,
writing the phases, attacking the plan, implementing a phase, running the suite, reviewing
the diff.

## Execution posture: in place by default; a subagent is a MEASURED answer, not a mood

For **cook**, the per-phase executor is the workflow-weight posture's answer, resolved
mechanically (`hs-run cook prompt developer --phase <id>`): **in place at main by
default**; a `@developer` subagent takes the phase's WHOLE red→green only when the phase
declares more files than the policy threshold or a forcing flag (`--parallel`/
`--subagents`) says so. Never size a phase by feel; the reader owns both the rule and
the number. Main keeps verification, the review of a subagent's code *and* test, and the
paired commit either way. The per-phase nudge watches this seam from the other side:
it fires when a subagent-posture phase receives main-thread source writes made through the
Write/Edit/MultiEdit tools — a Bash-spelled write is outside its sight; that path belongs to
the write-guard layer, not the nudge.

For **plan** on `--hard`, the delegated middle (research, writing the phases, attacking
the plan) still goes to subagents by default — that lane is a deliberate heavy mode the
caller chose.

**Already inside a worktree is not a posture either.** A plain spawn with no isolation
parameter runs in-place and inherits the current working directory, so its writes land
in the worktree you are already in. Only a spawn that explicitly asks for worktree
isolation gets its own tree, and that is for concurrent slices editing shared files — never
for a normal sequential phase.

**There is no write-lane exception.** Writes are
decided by PATH, never by who is writing — one deny floor blocks `.git/`, the secrets set,
and the installed core for a subagent and for main alike. So there is no "the subagent
cannot reach the phase's paths, main cooks it inline" case. If a phase's owned paths sit on
that deny floor or in a read-only bin zone, no one in this session may write them — stop and
ask instead of routing the write around the floor.

## The end-of-work delegations follow the measured disposition

Independent test and independent review are not decorative tails, but they are **measured
obligations, not flat ones**: `hs-run cook next` reports the disposition (ONE predicate
behind both it and `close`). A diff that stays under the thresholds at a tiny risk tier
EARNS its waiver — the steps may run at main and zero end-of-work spawns is complete. Any
trigger over threshold, a risk tier above tiny, or a range that cannot be measured keeps
the independent spawn mandatory — reviewing your own diff at the final gate is not a
substitute, because the whole point is that an independent reviewer re-derives
correctness without your reasoning in its context.

## Two subagent hazards that fail silently

1. **A mistyped subagent type is silently ignored** and the spawn falls back to the DEFAULT
   general-purpose agent. No error — just the wrong agent running your gate. If a generic
   agent shows up where you named a specific one, suspect the typo and respawn.
2. **Do not average reviewers.** Any evidenced critical issue blocks, regardless of how many
   other lenses came back clean and how high their scores were. A mean is not a verdict.

## Trigger a domain-risk lens only when the domain is actually touched

Auth · secrets · payments · database schema · public API contracts · CI/deploy/release ·
migrations · destructive filesystem operations · production config. Spawn the specialist lens
when the changed files touch one of these — not on every run. A lens fired reflexively on
unrelated work trains everyone to ignore it.
