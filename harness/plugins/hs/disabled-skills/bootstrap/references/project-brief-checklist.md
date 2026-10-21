# Project brief checklist (brainstorm gate)

A checklist, not a questionnaire: every item below must be RESOLVED before Step 1 hands off to
`hs:plan` — resolved means stated by the input, inferred from a quick repo scan, or explicitly
recorded as an assumption. Carry every item the brief needs; do not add an item to the output
only because it is listed here with nothing behind it.

## Items

1. **Objective** — what problem needs solving, who the users are.
2. **Hard constraints** — deadline, compute/infra budget, security requirements.
3. **Out of scope** — what this bootstrap explicitly does NOT cover.
4. **Integration** — existing systems/APIs/data this project must connect to.
5. **Quality priority** — ship fast vs long-term maintainability.

Same five items `clarify.md`'s interview asks — the checklist changes HOW they get resolved,
not what needs resolving.

## Default (no `--ask`): resolve, don't interview

For each item, in order:

1. Read it straight from the user's input when stated.
2. When bootstrapping onto an existing tree, a quick scan (`docs/`, `README*`, package
   manifests, existing CI config) may answer it — cite what was read.
3. Still unresolved → write an explicit assumption into the brief (`[ASSUMED] <value> — <one-line
   reason>`). Never leave an item blank, and never stop the flow to ask.

This is the fast path: a bootstrap on a clear brief should never block on a question the input
or the repo already answers, and a genuinely open item still surfaces — as a labelled
assumption the user can correct downstream, not a silent guess.

## `--ask`: interview the unresolved items

Resolve via steps 1-2 above first (repo scan + input), same as the default. For every item
STILL open after that, switch to `references/clarify.md`'s one-question-at-a-time interview —
only for those items, not the ones input/scan already answered.

## Output

Same requirement-brief shape `clarify.md` produces, with an assumption visibly tagged:

```
Objective: <1 sentence>
Users: <description>
Constraints: <list>
Out of scope: <list>
Open questions: <if any, only under --ask>
Assumptions: <[ASSUMED] lines, only under the default path>
```

Do not save a separate file — pass directly to `hs:plan`, same as `clarify.md`.
