# Run-state, resume & metrics

Machine-written run-state and the metrics corpus live under the **harness state dir**
(`harness_paths.state_dir()` — gitignored, e.g. `.harness/state/`), NOT under `plans/reports/`
(that is human-facing report scratch). `plan_orchestration.py` emits the resolved `state_path`
and `history_path` in its proposal.

For a real fan-out (above a small threshold — a two-sub spawn does not need it), record every
job transition through the script's own door — never hand-edit `state.json`; a hand edit is
exactly the partial write the atomic path exists to prevent:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/workflow-orchestrate/scripts/run_state.py \
    record <state_path> <job-id> <status> \
    [--set model=<m>] [--set task=<t>] [--set exitCode=<n>] [--set durationMs=<n>] [--set worktree=<dir>]
```

`state_path` is the `state_path` field of the Step-2 plan JSON
(`<state_dir>/orchestrate/<run-id>/state.json`). A row records per job: id, runtime, model,
task, status, exitCode, durationMs, timedOut, attempts, worktree. A `--set` value that parses
as JSON lands typed (`exitCode=0` → int), anything else stays a string.

On a crash, power loss, or killed session, resume instead of restarting from zero: for each job
in the plan ask the resume door and honor its answer — `dispatch: false` means the job already
reached `success`, skip it; `dispatch: true` re-dispatches it as attempt `attempt`:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/workflow-orchestrate/scripts/run_state.py \
    should-dispatch <state_path> <job-id>
# {"job": "scout-api", "dispatch": true, "attempt": 2}
```

Writes are atomic (`.tmp` + replace) so a partial state.json never wedges the resume; an
unknown verb exits 2 with usage — a state tool must not no-op silently on a mistyped command.

Append one JSON line per finished job to `<state_dir>/orchestrate-history.jsonl` via
`orchestrate_metrics.py`: run id, job id, runtime, model, task, duration, status, exit code,
attempts, and token/cost when the harness reports them. Every row is stamped with actor + ts.
The corpus is **advisory only**: at report time, when enough rows for a runtime/model/task
pair contradict the routing table, add a reviewable "routing suggestions" section to the
report. **Never silently edit a routing reference mid-run** — routing changes stay reviewable.
