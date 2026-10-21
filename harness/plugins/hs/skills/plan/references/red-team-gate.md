# Red-team gate — adversarial plan review (on-demand)

Runs AFTER plan.md + phase files are written, BEFORE validate. Goal: find where the plan will break in practice, not to praise the plan.

## Spawn `@red-teamer` by default (delegate-by-default)

On a `--hard` plan the red-team pass is **delegate-by-default**: spawn an independent `@red-teamer` subagent over the written plan rather than reasoning through the personas inline. An independent agent re-derives failure modes the author (and the happy path) missed — the inline-persona pass is the fallback, not the default.

```
Task(subagent_type="hs:red-teamer",
     prompt="Adversarially review plan.md + phase files at [plan-dir]. Pick 2-4 personas "
            "from the risk surface below. Apply the two-way Evidence Filter (every finding "
            "needs file:line or a repro command). Dedupe and rank by severity. Write the report to "
            "plans/[plan]/reports/. Return the report path + a 1-line verdict.",
     description="Red-team [plan]")
```

`--in-place` (manual override) falls back to the inline persona pass below; a `--fast` plan skips the red-team gate entirely (small task, 1-2 files, low risk — per the Modes table). When it runs, the disposition table (Accept/Reject per finding) is done at main inside plan.md.

## Personas (choose 2-4 based on the plan's risk surface)

| Persona | Examines |
|---|---|
| Security Adversary | bypass paths, write permissions, injection, secrets, spoofable input |
| Failure Mode Analyst | crash paths, fail-open vs fail-closed, races, partially corrupt state |
| Maintainer 6-months-later | hidden coupling; **naming/SRP**: does the module/script/class name accurately and completely describe its real responsibility? (`core` that also handles artifact I/O is a misleading name -> split) High finding; config drifting away from code |
| Bad-day Operator | rescue commands when a gate is stuck, observability, real rollback |

## Rules

0. **Reach data:** `hs-run plan next` at `needs_redteam` carries `code.fan_in` (importer count) and `code.importers` (who imports it) for every file the plan-graph declares in `files_to_modify`/`files_to_delete` — the blast radius of a changed signature, as a data point.
  A stale or missing code map is said on the facts (with the refresh command) and never holds the round; `--fact-subjects` still reads the planner's symbol facts instead.
1. **Evidence Filter (two-way)**: every finding MUST point to specific evidence (file:line in plan/phase/existing code, or a reproducing command). No evidence -> reject, do not include in the report. The same standard applies to **the planner's own decisions**: an open decision finalized by analogy with no file:line is a finding (the planner owes evidence) — it is not an exempt choice.
2. **Keep the report scannable**: dedupe, rank by severity, and group the long tail so a reader can act on it.
3. Each finding: severity (C/H/M/L) + failure scenario in 1 sentence + suggested fix in 1 sentence. No essays.
4. A plan command that needs the project's environment is run on a lane — `hs-rops run --lane <lane> -- '<cmd>'` — never installed into a read-only worktree; without a lane the finding stays `suspected` with the command named.
5. Report saved to `plans/<plan>/reports/from-code-reviewer-to-planner-red-team-<round>-plan-review-report.md` — ONE report per round, its personas named inside. `<round>` is `r<N>`, N one past the red-team reports already there; `hs-run plan prompt red-teamer` fills it in. A single-persona pass may still use the persona's name.
6. The report carries ONE line `verdict: PASS` or `verdict: REVISE` on its own line, key and value in English whatever the prose language.
   `hs-run plan next` and `plan_approval.py` read it: a REVISE holds the plan at `needs_redteam_disposition` (and refuses APPROVED) until the disposition table below has no empty cell.
   The findings table's first column is headed exactly `id`; a REVISE also holds until every one of those ids has its own row — an earlier round's rows do not cover a later round's ids.
   A report without the line, or a REVISE without an `id` table, is named on the envelope and holds nothing.

## Disposition (required, done inside plan.md)

Under a heading that starts `## Red-team disposition` (the gate finds each table by it). One table per round is fine: `## Red-team disposition — r2 (security)` covers only the ids of the report its suffix names (persona, stem or file name); a table naming no report covers every report. A near-miss heading (another level, `Redteam`, `dispositions`) is quoted back with its line, never read.

Cite where an Accept landed as `` `phases/<file>.md:N` `` or `` `plan.md:N` `` for this plan, `` `plans/<plan>/<file>.md:N` `` for another plan's file, or `` `:N` `` for the file named before it in the cell (a `:N` with none before it is counted and warned on),
each followed by a «quote» of that line: `hs-run plan next` checks every plan-local cite (file there, line in range) and a quoted one by content — `moved` names where the text is now.
A cite without a quote is checked for range only, and the envelope's `disposition_cites` counts say how many.
`redteam_disposition.py --plan <dir> --pin-cites` quotes every unquoted plan-local cite from its line now; run it before moving plan text.
After a move, `--remap-cites --from <git rev or old plan dir>` rewrites each of this plan's cites to where its old lines stand now, quoted; one it cannot place exactly once is left and named.
It aligns the old file with today's, a pointer read as the file it points to, so a blank line or a line that repeats is placed by its neighbours; a file that did not change keeps its cites.
A cite into a document other than the one named before it in the cell names its own path: a bare `:N` always means that earlier file.
A cite followed by `of <doc>` / `của <doc>` is read as that document's line: neither checked nor moved, and counted as `foreign`.

Table: finding -> Accept (how and in which phase the plan is updated) / Reject (with verification source per the "Verified Decisions" rule). An Accepted finding not yet propagated into phase files means the gate is not done. Deferred items -> record via `backlog_register.py add` with a report link in the text; do not create a separate REVIEW.md file.

