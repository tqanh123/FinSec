"""hs-code test plan-move -- generate the test-move PLAN from an approved
classify table. The tool PLANS; execution belongs to
the source repo's own MR.

Port of ob-tools' `testkit.analysis.planmove` (pinned commit `60075cb`),
generalised: a classify row's `path` is already repo-relative (the harness
snapshot measures the whole tree, never one caller-given `tests_dir`), so
the plan needs no second `tests_dir` argument -- helper discovery walks only
the directories the table's OWN rows already live in, never the whole repo.

Outputs (under `--out`, `--apply` writes them):
    plan.yaml   per-file decision: move / split / stay, helper housing, notes
    moves.sh    the `git mv` list -- a PLAN artifact, never executed by this verb

WHAT IT CANNOT SEE
    * Split boundaries are MARKED, never cut: for a mixed file the plan lists
      each function's target home and a suggested filename; the actual cut is
      a human/agent edit in the source repo MR.
    * Helper attribution sees only DIRECT imports inside the SAME directories
      the table's rows live in; a helper importing another helper chains
      transitively and is NOT followed -- such chains need a manual look
      before trusting the housing proposal.
    * `import_roots` drift: the plan EMITS the additions a moved unit's tests
      need, but whether the target repo's `codemap.yaml` already carries them
      is not checked here.
    * conftest.py is never moved by proposal: its fixtures often serve several
      homes; the plan only flags it with the homes of its importers.
    * Nothing here verifies the move keeps the suite green -- that is the MR's
      CI, not a static plan. `plan-move` never runs `git mv` itself.

USAGE
    hs-code test plan-move TABLE.yaml --out output/dir [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

import hs_family
import yaml_io

from codegraph.test.classify import SCHEMA, KIND_UNIT, from_yaml
from codegraph.test.inputs import mapping
from hs_family.cli import Command

HELPER_PREFIXES = ("_", "conftest")


def _is_helper(path: str) -> bool:
    name = Path(path).name
    return name.startswith(HELPER_PREFIXES) or name == "conftest.py"


def build_plan(table: dict, repo_root: Path, *,
               testlib: str = "tests/testlib/") -> dict:
    """Assemble the move plan from an approved table. Pure computation; writes
    nothing (the caller dumps). Every entry carries its evidence trail."""
    rows = from_yaml(table)
    unapproved = [r.id for r in rows if r.status != "approved"]
    if unapproved:
        raise ValueError(f"table has {len(unapproved)} unapproved row(s), e.g. "
                         f"{unapproved[0]} — approve first, plan after")

    by_path: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        by_path.setdefault(r.path, []).append(r)

    # helper housing: helpers are found by WALKING each directory a table row
    # already lives in (a batch scoped to one dist's tests never means walking
    # the whole repo); importers are the table's own paths.
    test_dirs = sorted({str(Path(p).parent) for p in by_path})
    helper_paths: "set[str]" = set()
    for d in test_dirs:
        base = repo_root / d
        if not base.is_dir():
            continue
        helper_paths.update(
            str(p.relative_to(repo_root)) for p in base.rglob("*.py")
            if "__pycache__" not in p.parts and _is_helper(str(p.relative_to(repo_root))))
    sorted_helpers = sorted(helper_paths)
    helper_importers: "dict[str, set[str]]" = {}
    for tf_path in by_path:
        if _is_helper(tf_path):
            continue
        text = _read(repo_root / tf_path)
        for cand in sorted_helpers:
            if _imports_module(text, cand):
                helper_importers.setdefault(cand, set()).add(tf_path)

    moves: "list[dict]" = []
    splits: "list[dict]" = []
    stays: "list[dict]" = []
    helpers: "list[dict]" = []

    for path in sorted_helpers:
        importers = sorted(helper_importers.get(path, set()))
        homes = sorted({g.home for g in rows if g.path in importers and g.home})
        if len(homes) == 1:
            helpers.append({"path": path, "action": "move-with-home",
                            "home": homes[0],
                            "why": "single home among direct importers"})
        elif not importers:
            helpers.append({"path": path, "action": "review-manually",
                            "why": "no direct importer found inside the "
                                   "table's own test directories — chained "
                                   "helpers and conftest magic are invisible here"})
        else:
            helpers.append({"path": path, "action": "testlib",
                            "home": testlib,
                            "why": f"{len(homes)} homes use it: "
                                   f"{', '.join(homes)}"})

    for path, funcs in sorted(by_path.items()):
        if _is_helper(path):
            continue                        # table rows are test functions
        homes_here = {f.home for f in funcs}
        kinds = {f.kind for f in funcs}
        if len(homes_here) == 1 and len(kinds) == 1:
            dest = _dest(path, funcs[0].home)
            moves.append({
                "path": path, "from": path, "to": dest,
                "git_mv": [path, dest],
                "funcs": [f.id for f in funcs],
            })
        elif len(homes_here) > 1 or len(kinds) > 1:
            # boundary MARKED, not cut: per-function targets + suggested names
            clusters: "OrderedDict[str, list[str]]" = OrderedDict()
            for f in sorted(funcs, key=lambda f: f.id):
                clusters.setdefault(f"{f.kind}:{f.home}", []).append(f.id)
            stem = Path(path).stem
            suggestions: "dict[str, str]" = {}
            for key in clusters:
                kind, home = key.split(":", 1)
                suggestions[key] = f"{home.rstrip('/')}/{stem}__{kind}.py"
            splits.append({
                "path": path, "action": "split",
                "why": "functions span more than one kind/home",
                "clusters": [{"kindhome": k, "funcs": v} for k, v in clusters.items()],
                "suggested_files": suggestions,
                "note": "boundary marked, not cut — the cut is a source-repo MR edit",
            })
        else:
            stays.append({"path": path, "why": "single kind/home already "
                                               "outside any dist move"})

    homes_touched = sorted({f.home for f in rows if f.kind == KIND_UNIT and f.home})
    return {
        "schema": "hs_code.test.planmove/1",
        "source_table_schema": SCHEMA,
        "repo_root": str(repo_root),
        "execution": "BELONGS TO THE SOURCE REPO MR — this verb plans, "
                     "it never moves a file",
        "moves": moves,
        "splits": splits,
        "stays": stays,
        "helpers": helpers,
        "import_roots_note": {
            "when": "unit tests move into a packages_dir dist's tests/",
            "add": sorted({h for h in (_import_root_hint(home) for home in homes_touched) if h}),
            "why": "pytest must resolve the dist package from its new home",
        },
    }


def _import_root_hint(home: str) -> "str | None":
    """`packages/<dist>/tests/` -> `packages/<dist>/src` -- the sibling
    `import_roots` entry pytest needs once a unit's tests land there. A home
    with fewer than 3 segments (a flat repo's `tests/`, or the shared
    `tests/integration/`/`tests/e2e/` vaults) is not a packages_dir home at
    all, so it hints nothing."""
    parts = [p for p in home.strip("/").split("/") if p]
    if len(parts) < 3:
        return None
    return "/".join(parts[:-1]) + "/src"


def _imports_module(text: str, helper_path: str) -> bool:
    stem = Path(helper_path).stem
    return f"import {stem}" in text or f"from {stem}" in text


def _dest(path: str, home: str) -> str:
    return f"{home.rstrip('/')}/{Path(path).name}"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def write_plan(plan: dict, out_dir: Path) -> "list[Path]":
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "plan.yaml"
    plan_path.write_text(
        yaml_io.safe_dump(plan, sort_keys=False, allow_unicode=True), encoding="utf-8")
    sh = out_dir / "moves.sh"
    lines = ["# PLAN ONLY — EXECUTION BELONGS TO THE SOURCE REPO MR.",
             "# Generated by hs-code test plan-move; review before running anything."]
    for m in plan["moves"]:
        a, b = m["git_mv"]
        lines.append(f"git mv {a} {b}")
    for h in plan["helpers"]:
        if h.get("action") == "move-with-home":
            lines.append(f"# helper: {h['path']} -> {h['home']} (review first)")
    sh.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [plan_path, sh]


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("table", type=Path, help="APPROVED classify table (YAML)")
    sp.add_argument("--testlib", default="tests/testlib/",
                    help="home for a helper shared by more than one home (tests/testlib/)")
    sp.add_argument("--out", type=Path, required=True,
                    help="output dir for plan.yaml + moves.sh (--apply writes them)")


def _planmove(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    try:
        table = mapping(yaml_io.yaml_load(args.table.read_text(encoding="utf-8")) or {},
                        args.table)
    except (OSError, yaml_io.YAMLError, ValueError) as e:
        raise hs_family.UsageError(f"plan-move: {e}") from e

    try:
        plan = build_plan(table, root, testlib=args.testlib)
    except ValueError as e:
        raise hs_family.UsageError(f"plan-move: {e}") from e

    written: "list[Path]" = []
    if apply:
        written = write_plan(plan, args.out)

    if as_json:
        print(json.dumps({
            "plan": plan, "out": str(args.out), "written": apply,
            "moves": len(plan["moves"]), "splits": len(plan["splits"]),
            "helpers": len(plan["helpers"]),
        }, sort_keys=True, ensure_ascii=False))
    else:
        print(f"plan-move: {len(plan['moves'])} move(s), {len(plan['splits'])} split(s), "
              f"{len(plan['helpers'])} helper decision(s)")
        if apply:
            for w in written:
                print(f"  wrote {w}")
        else:
            print(f"  [dry-run: nothing written — --apply writes plan.yaml + moves.sh "
                  f"under {args.out}]", file=sys.stderr)
    return 0


PLANMOVE = Command(add_args=add_arguments, run=_planmove)
