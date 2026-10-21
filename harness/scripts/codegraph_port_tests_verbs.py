"""hs-code's `port` domain — `port tests`, the test-move PLAN verb. A FLAT
module under `harness/scripts/` (same `hs-code-registry.d` reachability
reason every other `codegraph_port_*_verbs.py` sibling is).

`TESTS` wires `port_tests_plan.plan_move`/`rewrite_patch` (algorithm-
faithful ports of ob-tools' `portkit.tests_move.test_move`, pinned commit
`60075cb`) to this repo's own `.port/` batch/spec doors — the same doors
`codegraph_port_tree_verbs.py`'s `ADD_MODULE` already reads (`port_
workspace.find_port_root`, `port_config.load_and_validate`/`find_source`,
`port_batch_reader.load_batch`, `port_spec.merged_spec_and_unit_dist`).
`merged_spec_and_unit_dist` already reads `old_prefix` as a parameter
(ported there for `add-module`'s own closure); this verb is its SECOND
caller, reaching the `primary`/`spans` halves upstream's own function
computes but `add-module` never needed.

NO HARD-CODED PACKAGE: same rule as `add-module` — `package` is read off
`.port/port.yaml sources[].package`, never a literal `"gsm_agent"`. A
source declaring no `package` is refused by name (exit 2): the plan needs
it to tell a leftover/blind-string import of the upstream package from the
target's own.

Outputs (under `--out`, `--apply` writes them — mirrors `codegraph.test.
planmove`'s own dry-run/apply shape, the closest sibling BY FUNCTION):
    plan.yaml               moves · rewrite-only · unit_spans · unmapped ·
                             floor (two-way: owned files == planned moves)
    moves.sh                the `git mv` list — a PLAN artifact, never
                             executed here (execution belongs to the SOURCE
                             repo's own MR)
    imports.patch            unified diff, IMPORT LINES ONLY
    problems.txt              monkeypatch-blind lines, refused files,
                             name-home warnings, catch-all leftovers
    name-home-rewrites.txt   only when `--apply-name-home` actually applied
                             a rewrite (upstream's own conditional file)

Exit: 2 for anything malformed (unknown batch/flag, invalid `.port/
port.yaml`, a source with no `package`, an unreadable `--tests-map`); 3 when
a resolved `--out` lands outside the target repo (a well-formed request a
rule blocks, never a broken-state exit 2); 1 when the plan's own two-way
floor is not `ok` (RED, matching upstream's `return 0 if f["ok"] else 1`);
0 otherwise.

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
import port_spec  # noqa: E402
import port_tests_plan  # noqa: E402
import port_workspace  # noqa: E402
import yaml_io  # noqa: E402
from hs_family.cli import Command  # noqa: E402


def _tests_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--tests-map", required=True, dest="tests_map")
    ap.add_argument("--tests-dir", required=True, dest="tests_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--apply-name-home", action="store_true", dest="apply_name_home")


def _tests(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    batch_name, source_id = args.batch_name, args.source_id
    tests_map_path, tests_dir_path = Path(args.tests_map), Path(args.tests_dir)
    apply_name_home, out_path = args.apply_name_home, Path(args.out)
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        raise hs_family.UsageError(str(e)) from e

    # `--tests-map`/`--tests-dir`/`--out` resolve against the TARGET repo,
    # never the process CWD — the same "no CWD dependence" the port domain's
    # own root resolution already holds to (`port_workspace.py`'s own
    # docstring); an absolute path is left untouched.
    if not tests_map_path.is_absolute():
        tests_map_path = port_root / tests_map_path
    if not tests_dir_path.is_absolute():
        tests_dir_path = port_root / tests_dir_path
    if not out_path.is_absolute():
        out_path = port_root / out_path

    config_path = port_workspace.port_yaml_path(port_root)
    if not config_path.is_file():
        raise hs_family.UsageError(
            "no `.port/port.yaml` under %s — run /hs:port to plan a port" % port_root)
    cfg, problems = port_config.load_and_validate(config_path)
    if problems:
        raise hs_family.UsageError(
            "`.port/port.yaml` is invalid:\n" + "\n".join("  · %s" % p for p in problems))

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        raise hs_family.UsageError(str(e)) from e

    source, source_problem = port_config.find_source(cfg, source_id)
    if source_problem:
        raise hs_family.UsageError(str(source_problem))
    package = source.get("package")
    if not package:
        raise hs_family.UsageError(
            "source %r declares no `package` — port tests needs it to tell a "
            "leftover/blind-string import of the upstream package from the "
            "target's own" % source.get("id"))

    try:
        tests_map = yaml_io.yaml_load(tests_map_path.read_text(encoding="utf-8")) or {}
    except OSError as e:
        raise hs_family.UsageError("%s could not be read (%s)" % (tests_map_path, e)) from e
    except yaml_io.YAMLError as e:
        raise hs_family.UsageError(
            "%s does not parse as YAML (%s)" % (tests_map_path, e)) from e
    if not isinstance(tests_map, dict):
        raise hs_family.UsageError(
            "%s does not parse to a mapping (got %s)"
            % (tests_map_path, type(tests_map).__name__))

    if not tests_dir_path.is_dir():
        raise hs_family.UsageError("--tests-dir %s is not a directory" % tests_dir_path)

    try:
        merged, primary, spans, multi, catch_all = port_spec.merged_spec_and_unit_dist(
            batch.specs_dir, package)
    except port_spec.SpecError as e:
        raise hs_family.UsageError(str(e)) from e

    plan = port_tests_plan.plan_move(tests_map, tests_dir_path, merged, primary, spans)
    patch, plan_problems = port_tests_plan.rewrite_patch(
        plan, tests_dir_path, merged, multi, port_root, package, catch_all,
        apply_name_home)

    out_resolved = str(out_path.resolve())
    port_root_resolved = str(port_root.resolve())
    if not (out_resolved == port_root_resolved
            or out_resolved.startswith(port_root_resolved + "/")):
        raise hs_family.RefusedError(
            "port tests: --out %s resolves OUTSIDE the target repo %s"
            % (out_path, port_root))

    written: "list[Path]" = []
    if apply:
        out_path.mkdir(parents=True, exist_ok=True)
        plan_path = out_path / "plan.yaml"
        plan_path.write_text(
            yaml_io.safe_dump(plan, sort_keys=False, allow_unicode=True), encoding="utf-8")
        written.append(plan_path)

        sh = ["# PLAN ONLY — EXECUTION BELONGS TO THE SOURCE REPO MR."]
        for m in plan["moves"]:
            sh.append("mkdir -p packages/%s/tests" % m["dist"])
            sh.append("git mv tests/%s %s" % (m["path"], m["to"]))
        moves_path = out_path / "moves.sh"
        moves_path.write_text("\n".join(sh) + "\n", encoding="utf-8")
        written.append(moves_path)

        patch_path = out_path / "imports.patch"
        patch_path.write_text(patch + "\n" if patch else "", encoding="utf-8")
        written.append(patch_path)

        applied = [p for p in plan_problems if "name-home repair applied:" in p]
        if applied:
            rewrites_path = out_path / "name-home-rewrites.txt"
            rewrites_path.write_text("\n".join(applied) + "\n", encoding="utf-8")
            written.append(rewrites_path)

        problems_path = out_path / "problems.txt"
        problems_path.write_text(
            "\n".join(plan_problems) + "\n" if plan_problems else "", encoding="utf-8")
        written.append(problems_path)

    floor = plan["floor"]
    payload = {
        "schema": "port.tests/1", "apply": apply, "out": str(out_path),
        "floor": floor, "problems": len(plan_problems),
        "written": [str(w) for w in written],
    }

    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return 0 if floor["ok"] else 1

    if catch_all:
        print("root catch-all excluded from prefix rewriting: %r "
              "(its roads now surface as no-mapping leftovers)" % (catch_all,))
    print("tests plan: %d move(s), %d rewrite-only, %d unit(s) spanning dists"
         % (floor["planned_moves"], len(plan["rewrite_only"]), len(plan["unit_spans"])))
    print("floor two-way: %s (owned %d vs planned %d, unmapped units %d, dupes %d)"
         % ("OK" if floor["ok"] else "RED", floor["owned_files"], floor["planned_moves"],
            len(plan["unmapped_units"]), len(floor["duplicate_paths"])))
    print("problems: %d (monkeypatch-blind + refused files) -> problems.txt" % len(plan_problems))
    if apply:
        for w in written:
            print("  wrote %s" % w)
    else:
        print("  [dry-run: nothing written — --apply writes plan.yaml, moves.sh, "
             "imports.patch, problems.txt under %s]" % out_path, file=sys.stderr)
    return 0 if floor["ok"] else 1


TESTS = Command(add_args=_tests_args, run=_tests)
