# rops configuration

Keys live in `.rops.env` at the repo root. A real environment variable of the same name always
wins, so a one-off override needs no edit:

```
ROPS_HOST=other-box hs-rops doctor
```

`doctor` writes this file on first use in a repo and adds it to `.git/info/exclude` — the LOCAL
ignore list, never the repo's `.gitignore`, which belongs to the repo and may not be yours to
change.

Inline comments are stripped at the first `" #"` (space-hash). A value containing `" #"` would be
truncated — keep values quote-free and comment-free.

## Connection

| Key | Default | Notes |
|---|---|---|
| `ROPS_HOST` | — | **Never detected.** An ssh alias or `user@host`. Put `HostName`/`Port`/`User`/`IdentityFile` in `~/.ssh/config` and add `ControlMaster auto` + `ControlPersist 10m` — without multiplexing every verb pays a fresh TCP+auth handshake, and a verb makes several ssh calls. Measured: the same command took 3.3s on a multiplexed alias and 6.4s on a bare one. |
| `ROPS_SSH_OPTS` | *(empty)* | Extra `ssh` flags, split with `shlex`. |

## Paths

| Key | Default | Notes |
|---|---|---|
| `ROPS_REMOTE_DIR` | `~/rops/<repo-name>` | Detected. A directory the tool owns, not a mirror of a real checkout — so `--delete` cannot eat someone's working tree. A linked worktree: `~/rops/<main>--<dir>`. |
| `ROPS_RSYNC_EXCLUDES` | detected | Comma-separated; the floor (`.git/`, `.venv/`, `__pycache__/`, `node_modules/`, caches) is always present, plus whatever `.gitignore` lists. Negations (`!x`) are dropped: rsync would read a literal `!` filename. |
| `ROPS_LANE_KEEP` | empty | Comma-separated directories a `lane` REBUILD carries over from the old tree, on top of the floor. See **Keeping a directory across a rebuild** below. |
| `ROPS_LOG_DIR` | `/tmp/rops`, or `/tmp/rops/<lane>` when a lane is in effect | Where full logs stay, ON THE REMOTE. Behavior 4. A value declared in a layer weaker than the one naming the lane (a `.rops.env` value under an exported `ROPS_LANE`) yields to the per-lane dir, so two lanes never share one. |
| `ROPS_TAIL` | `60` | Lines returned. The rest stays remote; rops prints the path and line count so you can `fetch` it. |

### Keeping a directory across a rebuild

The floor a `lane` rebuild always carries is `.venv` + `logs`, plus whatever the
`ROPS_CMD_*` lines put on PATH as `$PWD/<name>/bin` — this repo's `.venv-ci` is found
that way and needs no entry. Every kept name is also protected from the lane's
`--delete`, so the two can never disagree.

The shared excludes are perishable on a push: a dir removed locally leaves the lane whole,
caches included. A kept name guards the lane root (`/logs/`), not `app/logs/`. `sync --back` keeps
plain excludes.

Declare a name in `ROPS_LANE_KEEP` only when it lives ONLY on the lane and detection
cannot see it — a model cache, a fixture corpus. A venv named in no list is destroyed twice over, by `rm -rf` on a rebuild and by `--delete` on every
run after.

## Execution

| Key | Default | Notes |
|---|---|---|
| `ROPS_PATH_PREPEND` | — | Prepended to `PATH`. A non-interactive ssh shell often lacks `~/.local/bin`: rc=127 then reads as "uv is not installed", not "PATH is wrong". `doctor` checks every alias's binary. |
| `ROPS_ENV` | — | Comma-separated `K=V` exported before every command. Database URLs belong here. |
| `ROPS_LOCK_ENV` | — | Comma-separated `ROPS_ENV` names that are exclusive (a DB URL): two runs exporting the same value take turns. Every run also locks its remote dir, so two runs on one lane always take turns; a `--at` worktree has its own dir but the lane's env, which only this key covers. The waiting run prints who holds the lock (names, never values). |
| `ROPS_LOCK` | — | Comma-separated lock names a run queues on machine-wide for its whole life, behind any run holding the same name — for what no lane dir or `ROPS_LOCK_ENV` names (a shared fixture server). `run --lock NAME` (repeatable, also on every `ROPS_CMD_*` alias) adds to it for one call. |
| `ROPS_LOCK_WAIT` | `1800` | Seconds a run waits for a seat in any pool, then gives up with rc 75 and nothing run. A lane rebuild waits the same way before it rewrites the tree. |
| `ROPS_CORES` | its pytest `-n` (`all` if none) | A plain `run`'s `cpu` seats (N or `all`), under `--cores`; an alias asks its own width (`references/pools.md`). |
| `ROPS_CPU_CAP` | 70% of `nproc` | Seats in the `cpu` pool (a container's quota). |
| `ROPS_CPU_BRAKE` | `on` | `off`: free seats ignore the 1-minute load. |
| `ROPS_LANE_SETUP` | — | A command every `lane` build or rebuild runs in the lane after its sync (so also a `run --lane` that rebuilds): the repo's own idempotent setup, such as creating the database its `ROPS_ENV` names. Runs with `ROPS_ENV` exported; prints `setup: rc=0`, and a failure fails the build naming this key. rops knows nothing of what it sets up. |
| `ROPS_LANE_SYNC_CMD` | `uv sync --frozen --group dev --all-extras` | The sync a `lane` runs after rsync; set = always runs as typed. The default is skipped — with the reason printed — where the repo has no `uv.lock` (`--frozen` would refuse there and the lane died after every earlier step was right) or neither `pyproject.toml` nor `uv.lock` at all; set this key to force a sync anyway. |
| `ROPS_TMUX_SESSION` | `live` | Only used with `--tmux`. Created on demand with `window-size latest`, so the pane sizes to whichever client is attached rather than the smallest one. |

## Command aliases

Every `ROPS_CMD_<NAME>` becomes a verb. The name is uppercase in the key, lowercase on the
command line:

```
ROPS_CMD_TEST=uv run python -m pytest -q      ->  hs-rops test -- -n 8
ROPS_CMD_LINT=uv run ruff check .             ->  hs-rops lint
ROPS_CMD_BUILD=docker build -t app .          ->  hs-rops build
```

An alias reads its own options (`--timeout`, `--lock`, `--sync`, ...) off the front; from the
first token they do not take, the rest is appended to the command as it stands —
`hs-rops test --lock x -n 8` sends `-n 8`. A `--` ends the alias's own options early, so an
option the alias also has (`--timeout`) can reach the command.

Detection proposes these from the repo's own files: `pyproject.toml` (+`uv.lock`/`[tool.uv]`),
`package.json` scripts (with pnpm/yarn/npm chosen by lockfile), `Makefile` targets, `go.mod`,
`Cargo.toml`. First source that answers a key wins; later ones only fill gaps.

## Suite recipe

`hs-run suite next` reports whether a fresh remote suite run exists for the tree as it stands
now — three keys, never detected, resolved as one recipe by `rops.ropslib.suite.
resolve_suite_recipe` (`doctor`'s own config check reads the same function, so the two callers
can never disagree about what "complete" means):

| Key | Default | Notes |
|---|---|---|
| `ROPS_SUITE_LANE` | — | The alias that runs the suite: `<NAME>` in `ROPS_CMD_<NAME>`, uppercase in the key. |
| `ROPS_JUNIT_<NAME>` | — | The remote path the `ROPS_SUITE_LANE` alias leaves its junit XML at, matching `ROPS_CMD_<NAME>`'s own `<NAME>`. |

`hs-run suite next` and `doctor` both refuse, before either hands out a chain or checks a PATH,
when `ROPS_CMD_<NAME>` is not declared, or when `<name>` lowercased collides with a built-in verb
(`sync`, `lane`, `run`, `fetch`, `ci`, `gitlab`, `bench`, `kill`, `lock`, `doctor`). A collision does not
lose quietly to the alias: it crashes every `hs-rops` invocation for the whole process.

```
ROPS_SUITE_LANE=TEST
ROPS_CMD_TEST=uv run python -m pytest -q --junitxml=/tmp/junit.xml
ROPS_JUNIT_TEST=/tmp/junit.xml
```

`doctor` prints `ROPS_SUITE_LANE` and the matching `ROPS_JUNIT_<NAME>` in its config block only
once a lane is declared, and a `[FAIL] suite ...` line naming both the key and the value that
does not resolve when the recipe is incomplete — never a line at all when `ROPS_SUITE_LANE`
itself is unset.

## Benchmarking

| Key | Default | Notes |
|---|---|---|
| `ROPS_BENCH_REPEAT` | `2` | The floor, not a target — a 50%-flaky test needs ≥10 repetitions to show its base rate. |
| `ROPS_BENCH_PREP` | — | **Required** (or pass `--no-prep`). A shell command run ON THE REMOTE before EACH repetition, to return the ground to a known state: `docker compose -p x restart db`, `redis-cli flushall`, `rm -rf .cache`. A failing prep aborts the bench rather than measuring an unknown state. See below. |
| `ROPS_MAX_LOAD` | `0.2 x cores` | `bench` refuses, and `doctor --measure` reports a failure, above this 1-minute load average. The default is SCALED by the remote's `nproc`. |
| | | A load average is a run-queue length, so it only means something against the cores that can drain it: an absolute 4.0 is 20% utilisation on a 20-core box and 200% on a 2-core one. A 20-core host resolves to 4.00, unchanged; absent `nproc`, it falls back to an absolute 4. |
| | | A value you SET is used as typed, never scaled — scaling a number an operator wrote would make it mean something other than what they wrote. Raise it only if the host genuinely idles higher, never to get past a red gate on a busy machine, which is the exact failure the number exists to stop. |
| `ROPS_COOLDOWN_TIMEOUT` | `300` | How long a hot host is given to COOL before the gate calls it busy. `bench --repeat N` measures run 2 right after run 1's workers exit, so the 1-minute average still carries the run that ENDED — measured 12.94 → 5.46 over five minutes with nothing of ours alive. Raising it does not widen what passes, only how long rops waits. |
| `ROPS_MIN_FREE_KB` | *(unset)* | Refuse to measure when `ROPS_SCRATCH_PATH` has less than this free. OFF unless declared — see **A full scratch filesystem** below. |
| `ROPS_SCRATCH_PATH` | `/tmp` for `ROPS_MIN_FREE_KB` (plus the remote's own `$TMPDIR`); `/dev/shm` for `run`'s own note | Which filesystem `ROPS_MIN_FREE_KB` measures, and which `run` reports on after a command — two call sites, two different defaults. Set it to whatever your command actually writes to. |
| `ROPS_SCRATCH_WARN_PCT` | `85` | Above this, `run` prints a one-line warning AFTER the command — it never deletes and never changes the rc. |
| | | `bench` clears the ground between repetitions via `ROPS_BENCH_PREP`; `run` does not, and `run` is what drives a suite by hand. Measured: three full-suite `run` passes left 19 GB in `/dev/shm`, and the NEXT run died with `Errno 122 Disk quota exceeded` — surfacing as `git init` exit 128, a red test rather than a disk message. |
| `ROPS_LOGIN_SHELL` | `zsh` | What an orphaned shell looks like in `ps`. A shell with PPID 1 in state `R` is never legitimate. |

### A full scratch filesystem returns a number, not an error

`ROPS_MIN_FREE_KB` is off unless you declare it: rops cannot know what your workload writes,
and a guessed floor turns away healthy hosts. Declare it when a repeated `bench` writes real
volume, because running out of room does not announce itself.

Measured (2026-09-01, 20-core host, 32 GiB `/dev/shm`):

- one run of the suite under test wrote **6.4-6.7 GiB** of temp trees;
- pytest keeps the **last three** run directories (`_retention_count=3`), so without a prep
  step that clears them ~20 GiB stays resident;
- the run that tipped it over did **not** say "out of space". One repetition returned a
  plausible number 6s off its siblings; the next died with `INTERNALERROR` on a worker whose
  test passes in isolation.

A wrong number that does not look wrong is the failure this gate exists to stop, which is why
the check lives beside the load check rather than in the workload's own prep step.

**Size the floor for ONE run, not for the leftovers.** Set it to what a single run needs plus
headroom, and clear the previous run's trees in `ROPS_BENCH_PREP`. A floor set to
one-run x retention was measured refusing a host with 20.8 GiB free whose prep had just
cleaned it — a 223 MiB shortfall that could never have mattered, and it cost the final
repetition of a benchmark. A floor exists to stop a run that will not fit; sizing it for
files the caller already deletes makes it a second failure mode.

### `ROPS_BENCH_PREP` runs over there

It is a shell string executed on the REMOTE, so `rops` itself is not available inside it. Name the
underlying command:

```
ROPS_BENCH_PREP=rops db reset        # rc=127, command not found — measured
ROPS_BENCH_PREP=docker compose -p docintake-test -f docker-compose.test.yml up -d --force-recreate postgres-test
```

The gate correctly refuses to measure through a failing prep, so the mistake surfaces immediately
rather than as a quietly wrong number.

## Repo-local keys

A verb under `<repo>/.claude/rops/verbs/` may read any key it likes. It should call
`cfg.require(KEY, why)` rather than defaulting, so a missing value fails loudly.

Worked example — the `db` verb in document-intake declares `ROPS_COMPOSE_PROJECT`,
`ROPS_COMPOSE_FILES`, `ROPS_COMPOSE_ENV`, `ROPS_DB_SERVICE`, `ROPS_DB_CONTAINER`,
`ROPS_DB_USER`, `ROPS_DB_NAME`.

`ROPS_COMPOSE_ENV` is not optional there: `docker compose up -d postgres-test` interpolates the
WHOLE file before it picks a service, so a variable another service marks required must still be
set even when you only touch postgres. Leaving it out fails with an interpolation error naming a
service you never asked to start — and if the verb pipes that through `tail`, the error reaches
you as rc=0 and a reset that silently did nothing.

## Lane ownership

A lane records the checkout that built it in `~/rops/.lane-src/<name>` as `<host>:<path>` plus the
checkout's own id (kept in its git dir, one per worktree). The id decides, so a hostname change does
not lock a tree out of its own lane; a record without an id matches by `<host>:<path>`.

- A rebuild from another checkout refuses with rc 3 and names both trees; `--rebind` hands the lane over.
- A lane built before owners were recorded has none: its first rebuild refuses once (rc 3) and names
  this tree's HEAD and the local worktrees whose history holds the lane's HEAD. Rebuild it with
  `--rebind` from THAT tree, not from wherever the shell stands after a cwd reset.
- An owner that cannot be read (ssh timeout) refuses rather than counting as absent.
- A name is one shell-safe segment (letters of any script, digits, `.`, `_`, `+`, `@`, `-`; not
  starting with `.` or `-`). The bare repo's name and an existing non-lane dir under `~/rops/` are
  refused with rc 2 before anything is pushed: a build moves such a dir aside and deletes it.
- The rebuild a `--lane`/`--sync` call runs first does not take the call's `--lock`/`ROPS_LOCK`
  names or its `cpu` seats: it holds the lane's own locks, waiting up to `ROPS_LOCK_WAIT`. When it fails, the
  command is NOT run, and the envelope's `warnings` say so (with the `--rebind` retry when that
  is the way out).
- A `--at` worktree's sync is the same: lane locks only, the same wait. When the command times
  out, the process group it recorded is killed before its worktree is removed, so a run still
  waiting on a lock is stopped too; one that cannot be stopped is named (`hs-rops kill <pgid>`).
