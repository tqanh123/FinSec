# Constraint scan — grep config before settling an open decision (on-demand)

Runs in hard mode (the `needs_planner` design step), BEFORE finalizing any open decision Q-x. Goal: a decision about **location / zone / artifact shape / policy** must be anchored to existing config data, not analogy. This is the work a deep-research subagent covers that hard-mode hs:plan previously skipped.

## Why this file exists

A decision placed by analogy with a neighbouring tree can land outside the zone the config declares — e.g. `ownership.yaml` declares `standards: [harness/standards/]`, not `<root>/standards/`. One grep of the short config file blocks it; that is the reason this step exists.

## Recipe (for each Q-x involving path/zone/artifact)

1. **Zone constraining the path** — read `"${HARNESS_BIN_ROOT:-.}"/harness/data/ownership.yaml` (short, read the whole file): which declared zone does the proposed path fall into? Inside or outside? `grep -n "<path-prefix>" "${HARNESS_BIN_ROOT:-.}"/harness/data/ownership.yaml`
2. **Policy constraining stage/artifact** — `"${HARNESS_BIN_ROOT:-.}"/harness/data/stage-policy.yaml`: does the decision change a required artifact for push/pr/ship/deploy?
3. **Artifact shape** — if the decision touches artifact shape, read the relevant schema in `harness/schemas/` (e.g. `artifact-verification.json`, `standards-artifact.json`) rather than inventing fields.
4. **Zone name is the authority** — the zone in `ownership.yaml` is the source the script-path containment helper uses; writing to an undeclared zone means the script-path containment helper will not accept it -> that is a finding, not a free choice.
5. **Verbatim citation** — does `docs/system-architecture.md` (or the clone-supplied `docs/system-architecture.md`) quote the path/zone under review? Which DEC already named it?

## Result for each Q-x

- Evidence found -> record verbatim as `ownership.yaml:N zone=<z> -> <path> [IN|OUT]` in the phase file next to the decision.
- No evidence found -> **tag `[ASSUMED]`** (or `[PRIOR]` if the position rests on prior/training knowledge), add to the Validation Log, let validate-gate resolve it. Do NOT finalize by analogy.

> The recipe lists sources by fixed file name (YAGNI). When `harness/data/` or
> `harness/schemas/` gains new files not yet listed here -> treat the list as a floor
> and read the newly added files too.
