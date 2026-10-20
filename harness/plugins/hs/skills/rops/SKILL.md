---
name: hs:rops
injectable: false
description: >-
  Run a heavy build, test, benchmark, or container on a remote machine over ssh + rsync, from
  ANY repo — destination, excludes and aliases detected from the stack. Use when a run needs a
  faster or quieter host.
metadata:
  compliance-tier: workflow
disable-model-invocation: true
user-invocable: false
---

# rops — run it over there

One CLI for "do this on another machine": sync the tree, run anything, drive whatever the
repo needs, benchmark on a quiet host, kill a runaway, pull results back.

```
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-rops <verb> [...]
```

It works in ANY repo. Nothing about a stack, a database, or a directory layout is baked in —
the first `doctor` run detects what it can, writes what it detected to `.rops.env`, and tells
you where every value came from.

## Two things, deliberately separated

rops is **remote control** with a **measurement discipline** layered on top, and the two are
not the same job:

- `sync` · `run` · `fetch` · `kill` · aliases — plumbing correctness. Getting the real exit
  code back, keeping the full log, killing a whole tree.
- `bench` — the only verb that asks *is this machine quiet enough to believe a number*. It
  refuses on a busy host, resets the ground before each repetition, runs at least twice, and
  calls a >10% spread noise rather than a result.

Running work on a busy machine is legitimate. MEASURING on one is not. That is why only
`bench` (and `doctor --measure`) checks load.

## Start with `doctor`

```
hs-rops doctor              # resolved config + provenance + transport
hs-rops doctor --measure    # also: orphaned processes, load average
```

In a repo it has never seen, `doctor` detects the destination, the rsync excludes, and the
command aliases; writes them to `.rops.env`; and adds that file to `.git/info/exclude` (the
LOCAL ignore list — never the repo's `.gitignore`).

**Read what it wrote.** Every line names its source (the lines are the envelope's `output`, mirrored live to stderr; `hs-rops --text doctor` prints them alone):

```
  remote_dir     ~/rops/document-intake        [detected]
  cmd_test       uv run python -m pytest -q    [.rops.env]
  host           homelab                       [env]
```

`[detected]` means rops guessed. A guessed destination is why `sync --delete` asks before it
runs. `ROPS_HOST` is never guessed — a wrong host means running commands on someone else's
machine.

## Verbs

| Verb | Use |
|---|---|
| `doctor` | What rops resolved, where from, and whether the far side answers. |
| `sync [--delete] [--back]` | rsync there; `--back` pulls the tree over your working copy (it refuses over uncommitted changes). |
| `run [--sync] -- <cmd>` | Anything. Real exit code, full log kept remote. One argument is a shell string (`'a | b && c'`); several are argv, each quoted whole (`bash -c 'cd X && c'` keeps its `cd`) — same for an alias's extra args and `bench`. `--sync` first brings the remote tree up to yours (a lane is rebuilt only when it differs): an edit costs one command. |
| `run --at <rev> -- <cmd>` | In a throwaway clean worktree of that commit, with its own sync, removed afterwards (even on failure, timeout or interrupt): nothing uncommitted or half-written reaches the run. |
| `lane [--name N]` | A real git worktree of this tree on the remote (bare repo + push + rsync), for gates that enumerate the tree through git; `run --lane N -- <cmd>` runs inside it. |
| `<alias> -- <args>` | Every `ROPS_CMD_<NAME>` becomes a verb: `hs-rops test`, `hs-rops lint`, `hs-rops build`. |
| `bench --label L -- <cmd>` | ≥2 repetitions on a quiet machine, spread reported. |
| `fetch <remote-path> [dest]` | Pull a log or artifact back. `--print` / `--grep PAT` read it in place, and `--lane NAME` names the lane's dir. None of the three checks or syncs a tree. |
| `kill <PGID>` / `kill <pattern>` / `kill --stray` | A number is a PGID: that group gets TERM, then KILL, and the reply counts what is left. Anything else is a `pgrep -f` pattern, never matching the kill itself. `--stray` reaps orphaned CPU burners. |
| `ci list\|run\|preview\|flatten [jobs]` | Run a GitLab pipeline remotely (capability-gated). |
| `gitlab [action]` | A real GitLab + runner on the remote — the heavy lane `ci` can target. |

stdout is exactly one JSON envelope; stderr is the live mirror of the run. Parse stdout
alone — piped with `2>&1`, the mirror lands after the envelope and `json.loads` fails with
"Extra data" at the envelope's length.

`--` separates rops's flags from your command. Add `--tmux` to run through a pane you can
attach to; add `--hosts a,b` to fan out across machines.

### What `lane` puts on the far side

- **Your working tree** over the pushed HEAD: tracked edits, untracked files, deletions.
  `.rops.env*` never travels (a copy an older run left there is removed).
- **Your index.** What you staged here (`git mv`, `git add`, `git rm`) is staged there, so
  a gate reading `git ls-files` sees the same path set. Unstaged new files stay untracked
  on both sides. The `tree:` fingerprint ignores the index, so it does not move.
- **The host checkout's `plans/`.** From a linked worktree, `lane` also copies the MAIN
  checkout's untracked/ignored `plans/` (the `plans/: N entries … from <host>/plans` line).
  A linked worktree has none of its own, and the gates that read plan files would otherwise
  SKIP, which reads as green. Paths the worktree itself tracks under `plans/` are never
  overwritten by the host's copy.

## The behaviors — do not "simplify" them away

Each one is a measurement this codebase already lost. They live in `ropslib/`; this table is
here so you recognize the symptom rather than routing around the guard.

| # | Behavior | The failure it prevents |
|---|---|---|
| 1 | Wait for the tmux pane to be idle, judged by the shell's **child count** (`pgrep -cP`) | A busy pane feeds keystrokes to the running process's **stdin** — the command "ran" and changed nothing. `pane_current_command` cannot judge this: it names the wrapper's shell, not whether that shell is the prompt or a running job — tmux reported `zsh` mid-run while `pgrep -cP` reported 2. |
| 2 | Single-quote-wrap before handing text to ssh | `ssh host tmux send-keys -l -- "$LINE"` does not preserve argv quoting; the remote shell re-splits and the pieces rejoin **without spaces** → `parse error near '}'`. |
| 3 | `pipefail` around the user command | `{ CMD; echo $?; } \| tee` returns **tee's** rc → a red run reported green. |
| 4 | The full log stays remote; only a tail comes back | `\| tail -N` on the run you need to analyze destroys the evidence. Use `fetch` to go get it. |
| 5 | Kill by **PGID**, never by pid | `kill -9 <pid>` does not take down a `uv run` tree — processes still alive 18 minutes later. |
| 6 | Reset the ground before **each** benchmark repetition (`ROPS_BENCH_PREP`) | A `kill` does not return postgres connections. Measuring on a dirty server attributes the wrong root cause. |
| 7 | `bench` runs ≥2 repetitions and prints the spread | The observed failure shape is "first run fast, later runs slow". A single run proves nothing. |
| 8 | Clear the line buffer (`C-c`) before typing; confirm the command **started** | A leftover partial line puts zsh in PS2 — Enter only adds a newline, `__RC=` never appears, and the tool burns the whole timeout reporting **124 as if the work had run and overrun**. |
| 9 | Check load and reap orphans before measuring | Background jobs a command spawns are **adopted by init** when the tree is killed by PGID. Orphans keep burning CPU and poison every measurement taken while they live. |

Behavior 8 is why **125** ("never started") is distinct from **124** ("started, overran").
Never collapse them.

### The plain lane does not use `tee`

Measured: a background job the command spawns inherits the write end of the pipe, so `tee`
never sees EOF and **ssh never returns**. The plain lane redirects to a file instead; the tmux
lane keeps `tee` because the pane has to see the output — and there the same jam surfaces as
"pane busy forever", which is a symptom you can diagnose rather than a hang.

## Adapting it to a repo

Three layers, weakest first — and `doctor` tells you which one every value came from:

1. **Detected** — destination (`~/rops/<repo>`), excludes (from `.gitignore` + a hard floor),
   aliases (pyproject/uv, package.json scripts, Makefile targets, go.mod, Cargo.toml).
2. **`.rops.env`** at the repo root — edit anything the detection got wrong.
3. **Real environment variables** — a one-off override needs no edit.

For something detection cannot reach — a compose stack, a database, a deploy step — drop a
verb file in `<repo>/.claude/rops/verbs/`. It appears only in that repo. See
`references/extending.md`.

## Boundaries

- **Do not switch branches** in the local tree — other sessions work in it. Read another
  branch with `git show <branch>:<path>`.
- The remote destination is `~/rops/<repo>` — a directory the tool owns, not a mirror of a
  real checkout. `--delete` still asks when the destination was detected rather than declared.
- `sync --back` writes over your working tree. It refuses over uncommitted changes.
- rops sends whatever you give it to a remote shell. Read the command before you run it.
- When a measurement contradicts an earlier conclusion, the measurement wins — say so plainly
  and correct the earlier claim rather than reconciling it.

## Quick reference

| Content | Drawer |
|---|---|
| Every `.env` key, with defaults and why | `references/config.md` |
| Verb-module contract + worked example | `references/extending.md` |
| Measurement recipes that survived review | `references/measuring.md` |
