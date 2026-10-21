# Solution Design

## A decision that must satisfy several hard constraints at once

When a design call has to hold ≥2 hard constraints that trade off against each other — perf vs back-compat vs a fixed schema —
do NOT settle it in one linear pass. Write the trade-off out FIRST and lock the call only after an externalized, revisable
trace. This is a requirement, not a suggestion: a wrong multi-constraint call does not stay local, it propagates into
every phase built on top of it, and by then the reasoning that produced it is gone.

Build the plan's verification design on evidence a third party can re-derive — a file:line, a command that reproduces, a
stored artifact — never on the model's own report that a step was done. A model cannot inspect its own execution, and a gate
that only checks whether an artifact is PRESENT cannot tell a real one from a hand-written one. That is why the anchor has to
sit outside the thing being verified.

## Backing

This page is routed at `needs_planner` for non-trivial design — it has no gate/script of its own. Load-bearing items route to real harness anchors, not this file, once a decision is made:

- **Security Assessment** — the Security Adversary persona in `references/red-team-gate.md` is the actual gate; findings there need `file:line` evidence (Evidence Filter).
- **Architecture / location decisions** — anchor to existing config via `references/constraint-scan.md` (Step 4), not analogy.
- Every finalized decision still needs a `file:line` anchor per `"${HARNESS_BIN_ROOT:-.}"/harness/rules/verification-mechanism.md`; an item on this checklist with no evidence is `[ASSUMED]` (or `[PRIOR]` if it rests on prior/training knowledge), not a settled design choice.
