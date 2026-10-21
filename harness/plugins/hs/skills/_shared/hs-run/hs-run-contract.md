# `hs-run` — the machine contract shared by `hs:plan` and `hs:cook`

`hs-run <domain> <verb>` owns the **deterministic** half of the workflow: the ORDER of the
steps and the PARAMETERS each step needs. It never decides **whether** a step applies — that
judgment stays in the skill prose and in you. A CLI that starts skipping a step "because it
looked unnecessary" has stopped being a CLI; report it, do not follow it.

Read this once; both skills assume it.

## The loop you actually run

```
hs-run <domain> next          # ask DISK what comes next — never a progress file
```

| exit | meaning | what you do |
|---|---|---|
| `0` | nothing left | stop |
| `1` | findings — the run worked and found something you must act on | read `warnings`, act on the finding, then decide whether to re-run |
| `11` | one more **deterministic** step | run the printed `next_action` verbatim, call `next` again |
| `10` | a **model turn** is required | do the judgment work the state names, then call `next` again |
| `2` | the CLI cannot answer its own question | read `warnings`, fix the input/plan, **do not proceed** |
| `3` | a deliberate refusal — the CLI could proceed but declines on purpose | read `warnings`; the refusal IS the answer, do not force past it |

`status` is the one verb that always exits `0` (read-only report) — except when it hits a
`state_conflict`, where it cannot give a trustworthy answer and exits `2`.

**Never write a driver as `while [ $? -ne 0 ]`.** `1`, `2`, `3`, `10` and `11` are all "not
plain done", but none are interchangeable with each other: `11` you may automate, `10` you may
not, and `1`/`3` both need you to read `warnings` before deciding the next move.

## Three outputs, three readers

Every verb prints **one JSON line on stdout** — the family envelope `hs-envelope/1` that
`hs-code`, `hs-cli` and `hs-rops` print too (`schema`, `cli`, `command`, `exit_code`, `state`,
`warnings`), plus hs-run's own step keys at the top level (`next_action`, `next_command`,
`artifacts`, `registry_sources`, and — when no plan resolved — `searched_dirs`) — and **one
human line on stderr** (`state=… · <numbers> · <next thing> · <re-run command>`). `--text`
prints the human rendering on stdout instead of the envelope. The exit code is the real machine
contract; the JSON is what you and the tests read; the human line is for the person watching.

`artifacts: []` with no `searched_dirs` would be an unverifiable answer — the envelope always
says where it looked.

### Never read the exit code through a pipe

A shell pipeline reports the exit status of its **last** stage, so `hs-run … | anything`
reports the reader's `0` and the blocking `2` disappears. The exit code is the machine
contract; a pipe silently replaces it. Capture it first, then forward it explicitly:

```
hs-run <domain> <verb> > /tmp/env.json; rc=$?
hs-cli render --upstream-rc "$rc" < /tmp/env.json     # exits with the code hs-run gave
```

`hs-cli render` re-derives nothing: `--upstream-rc` **forwards** the status you captured, and
rejects a value outside the closed ladder — a code from somewhere other than `hs-run` is an
error, not a result. Without the flag it exits `0`, which honestly reports on the rendering
and nothing else.

## `next` is a PROJECTION, not a progress ledger

State is re-derived from artifacts on disk on every call. Delete an artifact and the state
regresses — that is correct behaviour, not a bug. Consequences you must respect:

- **The `status:` line in `plan.md` frontmatter has no authority.** It is corroboration only.
  When frontmatter and artifacts disagree, `hs-run` reports `state: state_conflict` at exit
  `2` and names **both** sides. Do not pick a side yourself and carry on; find out which one
  is lying. Proceeding on a state you cannot trust is worse than stopping.
- No verb writes `status:` as a side effect of projecting.
- A step that produced no artifact is **not done**, however sure you are that you did it.

## Steps that need a model are TWO beats

The CLI cannot spawn a subagent — you are the outer loop. So a model-requiring step splits:

```
hs-run <domain> prompt <role>            # beat 1: prints the delegation brief, always exit 10
# you spawn the subagent, get its result, write it to a file
hs-run <domain> record <role> --out F    # beat 2: ingests the result, re-projects, returns the new state
```

`record` is an **attestation that you ran the step**, never proof that the step was done well.
Reviewing the subagent's output is your job, and it happens before you call `record`.

## `ask_user` — the shared "a person must decide" block

A state that needs a HUMAN's judgment (not a model's, not a deterministic step) may carry
`ask_user: {question, facts, options}` alongside exit `10`, in the same envelope as
`next_action`. This is common-layer machinery, not a fourth exit code — the six-rung table
stays exactly as it is, and `ask_user` riding any rung other than `10` is a contract
violation the dispatcher degrades to `EXIT_BROKEN` rather than ships.

A domain declares only the DATA — the `ask_user:` block on its own state row in its
registry file — never the enforcement. The dispatcher is
the one seam that knows both a state's name and the verb's real exit code, so it is the only
place that can check the pairing.

Not every state whose wording sounds like it needs a person carries the block: some already
have a dedicated, command-driven confirm flow of their own (the plan domain's approval
states run `plan_approval.py --verdict ...`, which already carries a verdict and a
rationale) — those declare `ask_user_exempt: <reason>` instead, so the state is still
accounted for without a block that would duplicate the existing flow.

`hs-cli render` prints all three parts when a state carries the block; read `question` and
`options` before answering — `facts` is what backs the question, not a summary to skip.

### `codemap_offer` — the one engine-written question

A state that reads code facts, on a repo with no `codemap.yaml`, answers `codemap_offer`
at exit `10` with the same `ask_user` block in every facts domain, once per session:
`init run --apply` sets the map up, `init decline` keeps it off for the session. The
step's real answer is not lost: `deferred` carries its `state`, `exit_code`,
`next_action` and `next_command`, and `domain_state` names it. After the user answers,
run the step again. The offer never replaces a broken (`2`) or refused (`3`) step, and an
unattended session (`CLAUDE_CODE_SESSION_ATTENDED` set and not `1`) gets a warning, not a
question.

## What `hs-run` deliberately does NOT know

Some obligations cannot be proved from disk. `hs-run` names them in `warnings` instead of
pretending: whether a `Task(...)` delegation really happened, whether a red-team finding was
genuinely dispositioned, whether a consistency sweep really found nothing. When a warning
says "not disk-provable", that is your cue to check it yourself — not permission to skip it.
