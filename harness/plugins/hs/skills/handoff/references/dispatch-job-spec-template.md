# `hs:handoff --dispatch` job composition

Dispatch is a
**thin composition** over whichever existing lane actually reaches the chosen
runtime (`dispatch-runtime-catalog.md`). This file is the field-mapping this
skill applies when building that lane's own call; it does not invent a new
spec format.

## Field mapping by runtime kind

**task focus / `--task`** — `internal`: into the spawned subagent's prompt,
alongside the artifact path. `gemini`: into
`gemini_companion.py task -p "<text>"`. `ccs`: into
`partner_companion.py task --provider <name> -p "<text>"`. An install-target
ID: into the printed continuation instruction, verbatim.

**`--model`** — `internal`: **rejected**, the role's own default model is
never overridden here. `gemini`: passed through as the CLI-runtime override
the relayer already accepts. `ccs`: **rejected** — ccs selects a destination
by `--provider`, not `--model`; there is no model flag to carry. An
install-target ID: recorded in the printed instruction as a suggestion,
never enforced (nothing here runs that CLI).

**`--cwd`** — `internal`: the subagent inherits the current worktree; a
different `--cwd` needs an explicit isolation call (`hs:worktree`), not a
bare flag. `gemini`/`ccs`: passed through when the lane's task verb accepts a
path context, otherwise noted in the prompt. An install-target ID: printed as
"run this from `<cwd>`".

**`--yes`** — `internal`: flips the subagent's expected approval posture from
"ask before a write" to "proceed" — still bounded by whatever tool
permissions the session already has; never a permission escalation.
`gemini`/`ccs`: not applicable — each lane's own write gate
(`sandbox_write` / `worktree_staged`) is independent of this flag. An
install-target ID: not applicable, nothing here executes the job.

**`--output`** — capture-only; governs where the artifact writes, decided
before any dispatch routing and unaffected by the resolved runtime.

## Never emit

- A bypass flag (`--dangerously-skip-permissions`, `--yolo`, sandbox-off) in
  any prompt or job field this skill builds.
- A model value chosen without an explicit `--model` — no default guess.
- A raw credential in the prompt, the printed instruction, or anywhere else —
  the artifact has already been redacted by the time this composition runs;
  the composition step must not re-introduce a secret from the live flags.

## Composition steps (all runtimes)

1. **Capture** — every dispatch call captures its OWN fresh artifact; there is
   no flag to reuse a prior one, so the state handed to a runtime is never
   stale.
2. **Validate** — `scripts/validate_slip.py <artifact>`; a refusal here is a
   hard stop, printed with the artifact path so no work is lost.
3. **Compose** — per the table above, for the resolved runtime kind.
4. **Hand off**:
   - `internal` → spawn the matched subagent (Agent tool) with the artifact
     path and task text in its prompt; report the subagent's own result.
   - `gemini` / `ccs` → invoke through the relayer subagent exactly as that
     lane's own SKILL.md prescribes; report its provenance-stamped result.
   - an install-target ID → print the report below; nothing runs.
5. **Report**:

```markdown
**Handoff Dispatch Result**
- Handoff artifact: <path>
- Runtime: <resolved-runtime>
- Dispatch engine: internal | gemini | ccs | instructive-only
- Model: <resolved-model-or-n/a>
- Job result: <success|failure|blocked|not-applicable>
- First safe step: <first bulleted next-action from the artifact>
- Next action: <what happened, or the exact command for the human to run>

Unresolved:
- <blockers, or "none">
```

Never inline the artifact body, a subagent's raw output, or a relayer's log
in this report — reference them by path/run-id.
