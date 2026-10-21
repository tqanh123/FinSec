# Why hs:diagram is not an hs-run domain (measured)

`hs-run <domain> next` exists for one shape of problem: a multi-step workflow whose progress is
persisted as artifacts on disk (a plan directory, phase files, `verification.json`, ...), so
`next` can re-derive "what step comes next" fresh on every call by reading those artifacts —
never a progress file of its own (`harness/scripts/hs_run.py`'s own docstring: "`next` is a
PROJECTION, never a progress file: every verb here derives its answer by reading on-disk
artifacts fresh, each call"). A second verb in an existing domain only earns its row on one of
three measured grounds (`harness/LESSONS.md`, "A second verb is a second index unless it earns
its row"): it needs an argument `next` cannot invent, it mutates on success, or it answers a
genuinely different question than `next` already does at a different exit rung.

A diagram compile does not fit that shape on any of those three grounds:

- **No cross-call state to project.** Compiling `diagram.json` into `diagram.html` is one
  request -> one response: validate, lay out, render, done. There is no on-disk workflow state
  a `next` call would need to re-derive between one invocation and the next — nothing like "phase
  3 of 7, waiting on a model turn" exists here. The whole operation is what `hs_run.py` calls a
  DETERMINISTIC step done in a single call, not a sequence of them.
- **It already has its own complete CLI.** `scripts/diagram_compile.py --input --format --preset
  --theme --out` is the one command the task needs; wrapping it in a `hs-run diagram next` would
  produce exactly the failure mode measured for `plan status`/`plan next` in LESSONS.md — two
  names, one answer, with the registry entry adding a second surface for the same JSON envelope
  the CLI already returns, never a real question `next` couldn't already answer.
- **The nearest real precedent already lives outside hs-run.** `hs-cli render` pretty-prints one
  envelope and is NOT an `hs-run` domain either, for the identical reason: a complete, single
  operator action belongs on the tool that does the whole thing in one call, not behind a
  step-sequencer built for state that persists between calls.

Conclusion: hs:diagram stays a plain skill with its own CLI (`scripts/diagram_compile.py`),
routed to directly as `/hs:diagram`, never through `hs-run`. If a future card gives diagram
compilation genuine multi-step state (for example, an incremental IR-editing session that must
resume across turns), that state — not this compile step — would be what earns a domain row.
