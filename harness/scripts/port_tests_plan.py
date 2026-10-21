"""port_tests_plan.py — `hs-code port tests`'s pure computation, as one FLAT
module under `harness/scripts/` (sibling of `port_spec.py`/
`port_tests_rename.py`/`port_tests_name_home.py`; the CLI verb,
`codegraph_port_tests_verbs.py`, is this module's only caller).

One translation table, one ownership table, one plan — the table MERGES
`module_map`/`renames`/`name_moves` from EVERY port-spec the batch declares
(the single source the built tree was cut by); the ownership is a caller-
given `_tests.yaml` (owned -> move, everything else -> stay and rewrite
imports only). Output (via the CLI verb, under a caller-given `--out`):

    plan.yaml      moves · rewrite-only · unit_spans · unmapped ·
                   monkeypatch-blind · two-way floor
    moves.sh       the `git mv` list — a PLAN artifact, never executed here
                   (execution belongs to the SOURCE repo's own MR)
    imports.patch  unified diff, IMPORT LINES ONLY — a changed line that is
                   not an import line refuses the WHOLE FILE, loudly

Port of ob-tools' `portkit.tests_move.test_move` (pinned commit `60075cb`,
`packages/portkit/src/portkit/tests_move/test_move.py`, `plan_move`/
`rewrite_patch`/`_hunks_touching_non_import`) — `merged_spec_and_unit_dist`,
upstream's own OTHER function of this module, already lives in
`port_spec.py` (ported there for `add-module`'s own closure, taking
`old_prefix` as a parameter instead of a hard-coded `"gsm_agent."` prefix;
see that module's docstring). `_apply_renames`/`_measure_owners` live in
`port_tests_rename.py`; the name-home cross-check lives in
`port_tests_name_home.py` — imported here only transitively, through
`port_tests_rename.apply_renames`.

NO HARD-CODED PACKAGE: upstream's own `rewrite_patch` passes the LITERAL
`"gsm_agent"` string to `rewrite_imports` as `src_module` (`test_move.py
:212` — the exact gap this port domain closes), and imports `_STRING_OLD`/
`_CATCHALL_WARN_AT` from `move_rename.py` (itself hard-coded to `gsm_agent`).
Here, `rewrite_patch` takes a required `package` argument — read by the CLI
verb off `.port/port.yaml sources[].package`, never a literal — and builds
`port_tests_rename.string_old_pattern(package)`/`old_leftover_pattern
(package)` from it.

WHAT IT CANNOT SEE
    * A unit may span several dists after the port (a moved module fans out
      into more than one target package). The plan routes an owned file to
      the unit's PRIMARY dist (most mapped modules) and records `unit_spans`
      — per-file refinement is a caller's own question, not this table's.
    * Relative imports inside the test tree are not resolved to a source
      module the way a landed source file's own relative imports are; a
      relative import passes through UNCHANGED and unremarked.
    * `monkeypatch.setattr("<package>.…")` names a module in a STRING — the
      rewrite cannot touch it. Every such line is LISTED (monkeypatch-blind),
      never silently left: two rulers, two answers.
    * Nothing here runs the suite, and nothing here executes `git mv` — a
      plan's proof is the target repo's own CI, after a human applies it.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import port_tests_rename as rename

_CONT_LINE = re.compile(r"^\s*[\w.]+\s*,?\s*$|^\s*\(\s*$|^\s*\)\s*,?\s*$")


def plan_move(tests_map: dict, tests_dir: Path, merged: Any,
              primary: "Dict[str, str]", spans: "Dict[str, List[str]]") -> dict:
    """Pure computation: the move plan from `_tests.yaml` + the merged
    table."""
    owned = tests_map.get("owned_by_unit") or {}

    def _rel(entry: str) -> str:
        # a caller's own `_tests.yaml` may write REPO-relative paths
        # ("tests/deep/x.py"); the plan speaks TESTS-DIR-relative. Accept
        # both, store one shape — a leading `tests/` left on would double
        # in `to:` and never match `all_files`, silently corrupting the
        # rewrite-only list.
        return entry[len("tests/"):] if entry.startswith("tests/") else entry

    moves: "List[dict]" = []
    unmapped: "List[dict]" = []
    for unit in sorted(owned):
        dist = primary.get(unit)
        if dist is None:
            if owned[unit]:      # an empty unit row is bookkeeping, not work
                unmapped.append({"unit": unit, "files": list(owned[unit])})
            continue
        for entry in owned[unit]:
            rel = _rel(entry)
            moves.append({"path": rel, "unit": unit, "dist": dist,
                          "to": "packages/%s/tests/%s" % (dist, rel)})
    moved = {m["path"] for m in moves}
    all_files = sorted(
        str(p.relative_to(tests_dir)) for p in tests_dir.rglob("*.py")
        if "__pycache__" not in p.parts)
    rewrite_only = [f for f in all_files if f not in moved]
    # two-way floor: no owned file lost, no destination without a source row
    dupes = [p for p, n in Counter(m["path"] for m in moves).items() if n > 1]
    return {
        "schema": "hs_code.port.tests/1",
        "execution": "BELONGS TO THE SOURCE REPO MR — this plans, never moves",
        "moves": moves,
        "rewrite_only": rewrite_only,
        "unit_spans": {u: d for u, d in spans.items() if u in owned},
        "unmapped_units": unmapped,
        "floor": {"owned_files": sum(len(v) for v in owned.values()),
                  "planned_moves": len(moves), "duplicate_paths": dupes,
                  "ok": not unmapped and not dupes
                        and len(moves) == sum(len(v) for v in owned.values())},
    }


def _hunks_touching_non_import(diff: "List[str]") -> "List[str]":
    """Changed lines that are neither import lines nor the CONTINUATION of a
    parenthesized import block in the SAME hunk. A hunk that rewrites an
    import may fold a multi-line paren block into one line — its name items
    (`a,` `b,`) are part of the import, not code. A hunk with NO import line
    at all fails whole: there is nothing import-shaped to explain it."""
    bad: "List[str]" = []
    hunk: "List[str]" = []
    hunks: "List[List[str]]" = []
    for l in diff:
        if l.startswith("@@"):
            if hunk:
                hunks.append(hunk)
            hunk = []
        else:
            hunk.append(l)
    if hunk:
        hunks.append(hunk)
    for h in hunks:
        changed = [x[1:] for x in h
                  if x[:1] in "+-" and not x.startswith(("+++", "---"))]
        if not changed:
            continue
        has_import = any(rename.IMPORT_LINE.match(x) for x in changed)
        ok_cont = all(_CONT_LINE.match(x) for x in changed
                     if not rename.IMPORT_LINE.match(x))
        if not (has_import and ok_cont):
            bad.extend(x for x in changed
                      if not (rename.IMPORT_LINE.match(x) or _CONT_LINE.match(x)))
    return bad


def rewrite_patch(plan: dict, tests_dir: Path, merged: Any,
                  multi: "Dict[str, List[str]]", packages_root: "Optional[Path]",
                  package: str, catch_all: "Optional[Dict[str, str]]" = None,
                  apply_name_home: bool = False) -> "Tuple[str, List[str]]":
    """The import-only unified diff for EVERY file (moved and staying).

    -> (patch text, problems). A rewritten line that is not an import line
    refuses the WHOLE FILE (kept out of the patch) — a half-rewritten file
    reads as done, which is worse than untouched.
    When `catch_all` carries excluded root keys, the old-prefix
    import lines STILL unrewritten after the pass are counted: those are
    the roads the catch-all used to pave into a wrong house. Past the
    threshold the plan warns BY NAME — the honest state is "no mapping",
    never a guessed one."""
    from codegraph.refactor.import_rewrite import rewrite_imports

    string_old = rename.string_old_pattern(package)
    old_leftover = rename.old_leftover_pattern(package)
    problems: "List[str]" = []
    sections: "List[str]" = []
    leftovers: "List[Tuple[str, str]]" = []
    files = [m["path"] for m in plan["moves"]] + plan["rewrite_only"]

    def _full(rel: str) -> Path:
        f = tests_dir / rel
        if not f.is_file() and rel.startswith("tests/"):
            f = tests_dir / rel[len("tests/"):]
        return f

    for rel in files:
        src = _full(rel).read_text(encoding="utf-8")
        renamed = rename.apply_renames(
            src, merged, multi, packages_root, rel, problems, apply_name_home)
        out = rewrite_imports(renamed, package, merged, rel, problems)
        if catch_all:
            leftovers.extend(
                (rel, l.strip()) for l in out.splitlines() if old_leftover.match(l))
        diff = [l for l in difflib.unified_diff(
            src.splitlines(), out.splitlines(), lineterm="",
            fromfile="a/tests/%s" % rel, tofile="b/tests/%s" % rel)]
        non_import = _hunks_touching_non_import(diff)
        if non_import:
            problems.append(
                "%s: rewrite touched %d non-import line(s) outside any import block, "
                "e.g. %r — file left OUT of the patch, rewrite by hand"
                % (rel, len(non_import), non_import[0].strip()[:60]))
            continue
        blind = [(i + 1, l.strip()) for i, l in enumerate(out.splitlines())
                if string_old.search(l)]
        for no, line in blind:
            problems.append(
                "%s:%d: monkeypatch string names %s — AST-blind by design, "
                "fix by hand: %s" % (rel, no, package, line[:70]))
        if diff:
            sections.append("\n".join(diff))
    if catch_all and len(leftovers) > rename.CATCHALL_WARN_AT:
        shown = "; ".join("%s: %s" % (f, l[:48]) for f, l in leftovers[:3])
        problems.append(
            "root catch-all %r excluded from prefix rewriting, and %d old-prefix "
            "import line(s) now have NO mapping (e.g. %s) — map their real homes "
            "or confirm they are dead; never restore the catch-all"
            % (dict(catch_all), len(leftovers), shown))
    return "\n".join(sections), problems
