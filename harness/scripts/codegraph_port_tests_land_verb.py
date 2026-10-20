"""hs-code's `port` domain — `port tests-land`, the LAND verb. A FLAT module
under `harness/scripts/` (same `hs-code-registry.d` reachability reason
every other `codegraph_port_*_verbs.py` sibling is; sibling of
`codegraph_port_tests_verbs.py`, which owns the PLAN verb, `port tests`).

`TESTS_LAND` wires `port_tests_land.run` (algorithm-faithful port of
ob-tools' `portkit.tests_move.tests_land`, pinned commit `60075cb`) to this
repo's own `.port/` doors — the SAME doors every other `codegraph_port_*_
verbs.py` sibling reads, ONE per resource, never a caller-typed override:
  * `port_workspace.find_port_root` / `port_config.load_and_validate` /
    `find_source` — `package` comes off the resolved source, never a
    literal (`_OLD_ROOT`, upstream's own hard-coded old root).
  * `codegraph._core.layout.target_descriptor` — the target's `codemap.yaml`
    descriptor (`namespace_root`, the dist layout), never upstream's `ob`/
    `packages/*/src` literals; no `codemap.yaml` -> exit 2, onboard first.
  * The upstream worktree (upstream's own `--source`) is NOT a free flag:
    it is `(port_root / source_entry["repo"]).resolve()`, the SAME `repo:`
    key `add-module` already reads this way (`codegraph_port_tree_verbs.py`
    `ADD_MODULE`, `repo_rel = source.get("repo")`). A source declaring
    no `repo` is refused by name, exit 2, same wording that verb already
    uses — a second, caller-typed worktree path would let this verb land a
    file from a tree `.port/port.yaml` never declared, silently diverging
    from what `add-module`/`port tests` read the same source as. No
    upstream semantic needs an override here: the landed file's bytes ARE
    the declared source's own tree, always.
  * `--ledger` is DROPPED — `port_ledger_reader.ledger_path(port_root)` is
    already the one door every write verb in this tree resolves the ledger
    through (`codegraph_port_rehome_verbs.py`'s own docstring states the
    same rule); a caller-typed override would be a second ledger location.
  * `--specs-dir` is DROPPED — `--batch NAME` names a `.port/batches/<name>/
    batch.yaml`, and `port_batch_reader.load_batch(port_root, name).
    specs_dir` is the specs door, exactly as `port tests`'s own `TESTS`
    reads it (`codegraph_port_tests_verbs.py`). `--batch` is required in
    LAND mode (the module map a landed file's imports resolve through);
    `--sync-owners` mode never reads specs, so it stays batch-free.
  * A NEW `--source-id` flag picks the `.port/port.yaml sources[]` entry
    (falling back to the sole declared source the same way `port_config.
    find_source` already does everywhere else) — unrelated to `port
    tests`'s own `--source` flag (that one is `--source-id`'s cousin over
    THAT verb's own argv namespace; the two verbs' argv are independent).

Exit: 2 for anything malformed (unknown flag, invalid `.port/port.yaml`, a
source with no `package`/`repo`, an unknown/invalid `--batch`, an unreadable
ledger); 1 when any landed file is RED (no unique owner, dest exists,
unresolved import) or `--check-home` finds a real problem; 0 otherwise —
mirrors `port_tests_land.run`'s own `Verdict.exit` (0/1/2) directly; this
module adds no exit-3 refusal arm of its own (no `--out`-outside-repo style
check applies here — every write this verb makes is INSIDE the target repo
by construction: the dist's own test home off the layout door, and the ledger
path itself).

GIT: none — matches every other module in this glob (`harness/tests/port/
test_port_verb_modules_never_write_git.py`'s own AST gate).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_tests_land  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core import layout as _layout  # noqa: E402
from hs_family.cli import Command  # noqa: E402


def _tests_land_args(ap: argparse.ArgumentParser) -> None:
    """`--land`/`--owner` are each repeatable — the SAME repeated-flag shape
    `port map`'s own `--to`/`--blocked-by` already use."""
    ap.add_argument("--sync-owners", action="store_true", dest="sync_owners")
    ap.add_argument("--check-home", action="store_true", dest="check_home")
    ap.add_argument("--source-id", default=None)
    ap.add_argument("--batch", default=None, dest="batch_name")
    ap.add_argument("--tests-root", default="tests")
    ap.add_argument("--classify-dir", default=None, type=Path)
    ap.add_argument("--sha", default=None)
    ap.add_argument("--land", action="append", default=[])
    ap.add_argument("--owner", action="append", default=[])


def _check_tests_land_args(args: argparse.Namespace) -> None:
    if args.sync_owners:
        if not args.classify_dir:
            raise hs_family.UsageError("port tests-land: --sync-owners needs --classify-dir")
    else:
        if not args.batch_name:
            raise hs_family.UsageError("port tests-land: --batch is required")
        if not args.land:
            raise hs_family.UsageError("port tests-land: at least one --land FILE is required")


def _tests_land(args: argparse.Namespace) -> int:
    _check_tests_land_args(args)
    root, opts = args.ctx.root, args.opts
    args.owner = args.owner or None
    args.apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        raise hs_family.UsageError(str(e)) from e

    config_path = port_workspace.port_yaml_path(port_root)
    if not config_path.is_file():
        raise hs_family.UsageError(
            "no `.port/port.yaml` under %s — run /hs:port to plan a port" % port_root)
    cfg, problems = port_config.load_and_validate(config_path)
    if problems:
        raise hs_family.UsageError(
            "`.port/port.yaml` is invalid:\n" + "\n".join("  · %s" % p for p in problems))

    source_entry, source_problem = port_config.find_source(cfg, args.source_id)
    if source_problem:
        raise hs_family.UsageError(str(source_problem))
    package = source_entry.get("package")
    if not package:
        raise hs_family.UsageError(
            "source %r declares no `package` — port tests-land needs it to tell an "
            "old-package import from the target's own" % source_entry.get("id"))
    repo_rel = source_entry.get("repo")
    if not repo_rel:
        raise hs_family.UsageError(
            "source %r declares no `repo` — port tests-land needs the upstream "
            "worktree's location to read the landed file's bytes from"
            % source_entry.get("id"))
    descriptor, layout_problem = _layout.target_descriptor(port_root)
    if layout_problem:
        raise hs_family.UsageError(layout_problem)
    args.package = package
    args.descriptor = descriptor
    args.repo = port_root
    args.source = (port_root / repo_rel).resolve()
    args.ledger = port_ledger_reader.ledger_path(port_root)

    if args.sync_owners:
        args.specs_dir = None
    else:
        try:
            batch = port_batch_reader.load_batch(port_root, args.batch_name)
        except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
            raise hs_family.UsageError(str(e)) from e
        args.specs_dir = batch.specs_dir

    # `--classify-dir` is the one remaining caller-typed path; it resolves
    # against the TARGET repo, never the process CWD — same rule `port
    # tests`'s own `--tests-map`/`--tests-dir`/`--out` already hold to.
    if args.classify_dir is not None and not args.classify_dir.is_absolute():
        args.classify_dir = port_root / args.classify_dir

    v = port_tests_land.run(args)

    if as_json:
        print(json.dumps(v.data, sort_keys=True, ensure_ascii=False))
        return v.exit

    if v.exit == 2:
        return v.exit    # Verdict.broken already printed the reason to stderr
    for f in v.data.get("files", []):
        mark = "RED " if f.get("red") else "    "
        print(f"{mark}{f['file']} -> {f.get('dest')}  "
              f"(owner {f.get('owner')} [{f.get('owner_why')}], "
              f"{len(f.get('tests', []))} test(s))")
        if f.get("red"):
            print(f"     {f['red']}")
    for d, r in v.data.get("check_home", {}).items():
        if "skipped" in r:
            print(f"  check-home SKIPPED {d}: {r['skipped']}")
        elif r.get("measured") is False:
            print(f"  check-home UNMEASURED {d}: {r.get('reason')}")
        else:
            print(f"  check-home {d}: {'GREEN' if r.get('green') else 'RED'}")
    if v.data.get("mode") == "sync-owners":
        print(f"tests-land sync-owners: {v.data['rows_planned']} row(s) planned, "
              f"{v.data['rows_written']} written"
              + ("  (dry-run; --apply writes)" if not args.apply else ""))
    else:
        n = len(v.data.get("files", []))
        red_n = len(v.data.get("red", []))
        print(f"tests-land: {n - red_n}/{n} file(s) {('landed' if args.apply else 'planned')}, "
              f"{v.data.get('rows_written', 0)} owner row(s)"
              + ("  (dry-run; --apply writes)" if not args.apply else ""))
    return v.exit


TESTS_LAND = Command(add_args=_tests_land_args, run=_tests_land)
