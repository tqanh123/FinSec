"""hs-code test check-home -- the two-way-red gate over an approved classify
table. A one-way gate stays green forever when someone forgets to
declare; this one goes red on BOTH sides:

    (a) WRONG PLACE  -- an approved row whose file does not physically live
        under the approved home.
    (b) UNDECLARED   -- a test function touching an in-scope unit with NO
        approved row in the table.
    (c) STALE        -- a row whose file no longer exists in the tree (a
        declaration without a subject is a lie, not a pass).
    (d) UNMEASURED   -- an approved row whose function id the snapshot never
        measured at all: its file sits outside every declared root (a
        `packages/<dist>/tests/` nobody added to `layout.roots`), or the
        table predates a real move and needs re-classifying. NEVER read as
        out-of-scope -- an id absent from the measurement has no known touch
        to be "out of scope" with, so scope cannot excuse it.

Port of ob-tools' `testkit.analysis.home_gate` (pinned commit `60075cb`),
generalised: a classify row's `path` is already repo-relative to the WHOLE
tree (the harness snapshot measures the whole tree, never one caller-given
`tests_dir`), so direction (a)'s path resolution needs no `tests_root`
reinterpretation fallback -- the row's `path` IS the tree-relative path, pre-
move; post-move the file has landed at `<home>/<basename>` instead.

WHAT IT CANNOT SEE
    * Location is FILE-level: inside one file the gate cannot see which
      function sits on which line -- a split file whose halves were moved but
      whose rows still point at the old path reads as (a)/(c), not as a
      precise per-function mismatch.
    * `in-scope` is the units passed in (default: no restriction). Directions
      (a)/(b) are scope-gated for a row/function the snapshot DID measure: a
      touch entirely outside scope belongs to another batch's enablement --
      enable per batch as its dists land. Direction
      (d) is NOT scope-gated (see above).
    * Blind touch (dynamic imports, conftest chains) means a test can touch a
      dist invisibly -- direction (b) catches only VISIBLE touches. The blind
      floor is the test map itself, not the truth.
    * The gate reads the table, not the review intent: a row approved into the
      wrong home by mistake is GREEN here as long as the file sits there.

USAGE
    hs-code test check-home TABLE.yaml [--units a,b,c]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import hs_family
import yaml_io

from codegraph.test.classify import SCHEMA, from_yaml, measure_repo
from codegraph.test.inputs import mapping
from hs_family.cli import Command


def check_home_rows(rows: "list[Any]", all_funcs: "Any", tree_root: Path,
                    units_in_scope: "set[str] | None" = None) -> dict:
    """The pure two-way-red algorithm, split out of `check_home` below so a
    SECOND caller can drive it over its own row/touch shapes without a
    second copy of the gate: `rows` are objects carrying `.id`/`.path`/
    `.home`/`.status` (this module's own `Proposal`, or any other object
    with the same four attributes); `all_funcs` is an iterable of objects
    carrying `.id`/`.touch` (a `frozenset[str]` of unit names) — exactly
    what `codegraph._core.testmap.TestFunc` already offers. Neither
    parameter is tied to this repo's own `hs_code.test.classify/1` schema
    or to `measure_repo`'s snapshot precondition; `check_home()` below
    supplies both from there, unchanged. `hs-code port tests-land`'s
    `--check-home` is the second caller (`port_tests_land.py`): it reads
    upstream's own `testkit.classify/1` rows through its own reader and
    passes `TestFunc`s from the SAME snapshot door
    (`codegraph._core.testmap.build_test_map`) `check_home()`'s own wrapper
    reads below — never a second touch engine.

    Returns {'green': bool, 'wrong_place': [...], 'undeclared': [...],
    'stale': [...]}. Every list entry is a one-line reason with evidence."""
    tree_root = tree_root.resolve()
    all_funcs = list(all_funcs)    # walked twice below; a one-shot iterable would starve the second pass

    def _scoped(units: "frozenset[str]") -> "frozenset[str]":
        return units if units_in_scope is None else units & units_in_scope

    unapproved = [r for r in rows if r.status != "approved"]
    wrong_place: "list[str]" = []
    undeclared: "list[str]" = []
    stale: "list[str]" = []
    unmeasured: "list[str]" = []

    for r in unapproved:
        wrong_place.append(f"(a) {r.id}: status {r.status!r} — approve first; "
                           f"an unreviewed row is not a home")

    # func-id -> in-scope touch: direction (a) only judges rows this batch is
    # accountable for (a mixed file's other-home rows belong to their batch).
    touch_of = {f.id: _scoped(f.touch) for f in all_funcs}

    by_path: "dict[str, list]" = {}
    for r in rows:
        by_path.setdefault(r.path, []).append(r)

    for path, rs in sorted(by_path.items()):
        # A row's `path` is repo-relative by construction. After the move the
        # file lives under the approved HOME under its own name — look there
        # before declaring stale (pre-move: the row's own path exists; post-
        # move: the home path exists; neither: stale).
        src = tree_root / path
        if not src.exists():
            homes = sorted({r.home for r in rs if r.home})
            found = next((tree_root / h / Path(path).name
                         for h in homes
                         if (tree_root / h / Path(path).name).exists()), None)
            if found is None:
                stale.append(f"(c) {path}: declared ({len(rs)} row(s)) but the "
                             f"file is gone from the tree")
                continue
            src = found
        try:
            rel = src.resolve().relative_to(tree_root)
        except ValueError:
            wrong_place.append(f"(a) {path}: lives outside the tree root "
                               f"{tree_root} — cannot judge its home")
            continue
        for r in rs:
            if r.status != "approved" or not r.home:
                continue
            if r.id not in touch_of:
                # NOT the same thing as out-of-scope: this id has no known
                # touch at all because the snapshot never measured it --
                # scope cannot excuse a row the gate cannot see.
                unmeasured.append(
                    f"(d) {r.id}: not measured by the snapshot -- its file "
                    f"sits outside every declared root, or the table "
                    f"predates a move and must be re-classified")
                continue
            if not touch_of[r.id]:
                continue                    # measured, but out of this batch's scope
            if not str(rel).startswith(r.home.rstrip("/") + "/"):
                wrong_place.append(
                    f"(a) {r.id}: approved home {r.home!r} but file sits at "
                    f"{rel} — wrong place or move not executed")

    declared = {r.id for r in rows}
    for fn in all_funcs:
        touched = _scoped(fn.touch)
        if touched and fn.id not in declared:
            undeclared.append(
                f"(b) {fn.id}: touches {', '.join(sorted(touched))} but has "
                f"no row in the table — missing declaration")

    return {"green": not (wrong_place or undeclared or stale or unmeasured),
            "wrong_place": wrong_place, "undeclared": undeclared, "stale": stale,
            "unmeasured": unmeasured}


def check_home(table: dict, tree_root: Path, tmap: Any,
               units_in_scope: "set[str] | None" = None) -> dict:
    """The existing wrapper, unchanged in behavior: this repo's own
    `hs_code.test.classify/1` schema gate (`from_yaml`) + `tmap.all_funcs()`,
    over `check_home_rows` above."""
    rows = from_yaml(table)
    return check_home_rows(rows, tmap.all_funcs(), tree_root, units_in_scope)


def render(result: dict) -> str:
    if result["green"]:
        return "check-home: GREEN — every in-scope test declared and homed"
    lines = [f"check-home: RED — {len(result['wrong_place'])} wrong place(s), "
             f"{len(result['undeclared'])} undeclared, {len(result['stale'])} stale, "
             f"{len(result['unmeasured'])} unmeasured"]
    for key in ("wrong_place", "undeclared", "stale", "unmeasured"):
        for r in result[key]:
            lines.append(f"  {r}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("table", type=Path, help="classify table (must be reviewed/approved)")
    sp.add_argument("--units", default=None,
                    help="comma-separated units in scope (default: no restriction — "
                         "every touch counts)")


def _check_home(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    as_json = bool(getattr(opts, "json", False))

    try:
        table = mapping(yaml_io.yaml_load(args.table.read_text(encoding="utf-8")) or {},
                        args.table)
    except (OSError, yaml_io.YAMLError, ValueError) as e:
        raise hs_family.UsageError(f"check-home: {e}") from e
    if table.get("schema") != SCHEMA:
        raise hs_family.UsageError(
            f"check-home: table schema {table.get('schema')!r} != {SCHEMA}")

    tmap, _dist_of_unit, _descriptor = measure_repo(root, "check-home")
    scope = set(args.units.split(",")) if args.units else None
    try:
        result = check_home(table, root, tmap, scope)
    except ValueError as e:
        raise hs_family.UsageError(f"check-home: {e}") from e

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        print(render(result))
    return 0 if result["green"] else 1


CHECK_HOME = Command(add_args=add_arguments, run=_check_home)
