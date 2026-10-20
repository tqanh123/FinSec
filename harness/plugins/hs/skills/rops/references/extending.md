# Adding a verb

## Where it goes

| Scope | Location | Appears |
|---|---|---|
| Every repo | a module in `harness/scripts/rops/verbs/` + one row in `harness/data/hs-rops-registry.d/core.yaml` | always, `[ship]` |
| One repo | `<repo>/.claude/rops/verbs/<name>.py` | only in that repo, `[repo: <path>]` |

Prefer the repo-local location for anything that names a service, container, database, or
deploy target. Those are the repo's facts, not rops's.

## Contract

A repo-local file declares four names (a shipped module declares only `add_args` and `run`;
its name and help line are its registry row: `deploy: {module: rops.verbs.deploy, help: "..."}`):

```python
"""<one line: what the verb does>."""
from __future__ import annotations
import argparse
from rops.ropslib import hosts
from rops.ropslib.exec import report, run_remote

VERB, HELP = "deploy", "build an image and run it over there"

def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tag", default="dev")
    p.add_argument("--timeout", type=int, default=1800)

def run(args: argparse.Namespace) -> int:
    host = hosts.gate(args)[0]
    return report(run_remote(args.cfg, host,
                             f"docker build -t app:{args.tag} . && docker run --rm app:{args.tag}",
                             timeout_s=args.timeout, label="deploy"))
```

That is the whole registration. hs-rops builds one command table from three sources ranked
shipped registry → the repo's own directory (sorted filename order — not filesystem order, an OS
detail that would make a merge's meaning depend on it) → the `ROPS_CMD_*` aliases. No command
module is imported to build it, except the repo's own files (their `HELP` lives in the file).

A repo-local verb written before the bare `from ropslib import ...` / `from verbs import ...`
shape above still loads: the router aliases `ropslib`/`verbs` onto those old top-level names
before it loads anything under `<repo>/.claude/rops/verbs/`, so an existing file needs no edit.
Use the dotted form (`rops.ropslib`, `rops.verbs`) in anything new.

`--help` lists every command sorted by name, each with the source it came from. A repo file
that fails to import is skipped and named in the envelope's `warnings`; the other commands still
run. An old `ORDER` constant is ignored.

## Rules

- **Never edit `rops.py` to add a verb.** If you are adding a branch to the router, the design
  has been broken. Borrowed from `hs-run` (`"${HARNESS_BIN_ROOT:-.}"/harness/scripts/hs_run.py`), survival invariant #3.
- A name two sources declare (a repo file shadowing a shipped command, an alias colliding with
  either) is a **hard error naming both**. Silent last-wins would turn a mistyped filename into a
  verb that quietly disappears.
- Go through `run_remote()`, never a raw `Host.run(...)`, for anything the user is waiting on.
  `run_remote` is what carries Behaviors 3, 4 and the tmux-jam redirect (see
  `ropslib/tmux.py`); a bare call carries none of them. Raw calls are for short, non-measured
  queries (that is all `doctor` uses them for).
- **Return through `report()`.** Returning `run_remote(...).rc` throws the tail away, so a failing call prints nothing.
- **Never pipe the remote command through `tail` or `tee` for tidiness.** The pipe hands back the
  pipe's exit code, so a failure returns rc=0. This is behavior 3, and it bit inside a verb, not
  just in the core.
- `cfg.require(KEY, why)` for anything the verb cannot work without. A default that silently
  papers over a missing value is how a command ends up pointing somewhere unintended.

## Calling one verb from another

Import the module and call `run` with a hand-built namespace:

```python
from rops.verbs import db as db_verb
db_verb.run(argparse.Namespace(action="reset", sql=None, cfg=args.cfg,
                               host=args.host, hosts=None, tmux=False))
```

Build the namespace with **every** attribute that verb's `add_args` declares, plus the router's
own (`cfg`, `host`, `hosts`, `tmux`; a command run through `hs-rops` also gets `ctx`). A missing one raises `AttributeError` deep inside the
callee, far from the line that forgot it.

Prefer `ROPS_BENCH_PREP` over doing this from `bench`: a shell string in config keeps the core
from knowing what any particular repo's ground looks like.
