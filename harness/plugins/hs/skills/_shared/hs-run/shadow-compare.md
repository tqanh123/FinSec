# Optional shadow comparison (off by default)

A second, cheaper judge looks at a decision a skill already made. It never changes that
decision. It runs only when the project turned it on (`HARNESS_SHADOW_COMPARE=1`, or
`enabled: true` in the data home's `shadow-compare.yaml`); the shipped switch is off.

1. Pipe `{profile, consumer, state, legacy, candidates}` to
   `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/shadow_compare.py prepare`. The
   profile names come from `harness/data/shadow-compare.yaml`. More candidates than
   `max_candidates` answers `skipped` — do not trim the list to make it fit.
2. Hand the returned packet (candidate codes only, facts redacted) to a cheap subagent or
   a partner lane. Never add the candidate names back.
3. `shadow_compare.py record --id <id> --verdict <code> --judge <who>`. The legacy answer
   stands; a disagreement is recorded and appears as an `hs-run` warning naming the second
   look to spawn (`hs:independent-revalidator`, or `hs:red-teamer` for weight and risk).
4. After that look, `shadow_compare.py resolve --id <id> --outcome <what the second look found>`.

`rate` reports the disagreement share as k/n with a Wilson interval; `benchmark --labels
<jsonl>` replays the ledger against known answers. Thresholds move on those, not on one
disagreement.
