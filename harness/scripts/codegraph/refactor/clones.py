"""Clone buckets, PRODUCTION <-> PRODUCTION: one name defined in two HOMES.

Port of the scanning half of ob-tools' `devkit.clones.cloneprod` (pinned
commit `60075cb`) -- what `clone-dedupe` reads. The baseline/check half of that
verb is not here: it is a gate of its own, not something this door calls.

A HOME is where a copy lives, as the measured tree groups it: the workspace
package a module sits in (a `workspace` root of `codemap.yaml`), or the module
itself when it sits in no package. Upstream knew only `packages/<dist>/src`, so
a flat tree had no homes at all and no clone could ever be found in one.

THREE BUCKETS (a name in >=2 homes falls in exactly one):
    IDENTICAL   byte-equal bodies after `ast.unparse` (formatting erased).
    DRIFTED     not byte-equal but similarity >= SIMILAR_MIN, scored as the
                MAX of (raw unparsed text, name-blanked shape) -- a raw-only
                bar misses exactly the copy someone tried to hide by renaming.
    COINCIDENT  same name, different code below the bar. Not a finding.

WHAT IT CANNOT SEE
    * Name-keyed. Two copies of one calculation under two DIFFERENT names
      are invisible here.
    * `ast.unparse` erases comments: two copies differing only in comments
      read IDENTICAL.
    * Only module-level defs, `Class.method` pairs, and module-level
      constants.
    * A re-export (`from x import y`) is not a definition and never a
      sighting; `__init__.py` files are skipped (doors re-export by design).
    * `getattr`/`setattr` string targets are invisible.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

#: Names too conventional for a collision to mean anything. DECLARED, not
#: grown: every line makes the scan blind in one more place.
COMMON: frozenset = frozenset({
    "__init__", "__repr__", "__str__", "__eq__", "__hash__", "__enter__",
    "__exit__", "__aenter__", "__aexit__", "__call__", "__len__", "__iter__",
    "__next__", "main", "run", "close", "reset", "setup", "teardown",
    "logger", "log", "LOG", "LOGGER", "__all__", "T", "_T",
})

#: Minimum similarity to call two bodies ONE drifted copy (measured upstream on
#: its own source tree at `60075cb`: real cases >= 0.80, coincidences below 0.60).
SIMILAR_MIN = 0.80


class _Blank(ast.NodeTransformer):
    """Erase identifiers, keep structure and constants."""

    def visit_Name(self, n):            # noqa: N802
        return ast.copy_location(ast.Name(id="_", ctx=n.ctx), n)

    def visit_arg(self, n):             # noqa: N802
        return ast.copy_location(ast.arg(arg="_", annotation=None), n)

    def visit_Attribute(self, n):       # noqa: N802
        self.generic_visit(n)
        return ast.copy_location(ast.Attribute(value=n.value, attr="_", ctx=n.ctx), n)

    def visit_FunctionDef(self, n):     # noqa: N802
        self.generic_visit(n)
        n.name, n.decorator_list, n.returns = "_", [], None
        return n

    visit_AsyncFunctionDef = visit_FunctionDef


def _shape(node: ast.AST) -> str:
    return ast.unparse(_Blank().visit(ast.parse(ast.unparse(node))))


def similarity(a: ast.AST, b: ast.AST) -> float:
    """MAX of raw-text and name-blanked ratios -- see module docstring."""
    raw = difflib.SequenceMatcher(None, ast.unparse(a), ast.unparse(b)).ratio()
    shaped = difflib.SequenceMatcher(None, _shape(a), _shape(b)).ratio()
    return max(raw, shaped)


def _body_sha(node: ast.AST) -> str:
    return hashlib.sha256(ast.unparse(node).encode("utf-8")).hexdigest()[:8]


def collect_defs(py: Path) -> List[Tuple[str, ast.AST]]:
    """Module-level defs, `Class.method`, and module-level constants."""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    out: List[Tuple[str, ast.AST]] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((n.name, n))
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((f"{n.name}.{m.name}", m))
        elif isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    out.append((tgt.id, n.value))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                and n.value is not None:
            out.append((n.target.id, n.value))
    return out


def homes_of(tree: Any) -> Dict[str, str]:
    """module -> its home: the workspace package it sits in, else itself."""
    package_of = tree.package_of()
    return {m["module"]: package_of.get(m["module"], m["module"]) for m in tree.records()}


def buckets_for(tree: Any) -> Dict[str, Any]:
    """The three buckets over one measured tree (`codegraph.check.tree.Tree`).
    Every body carries `home`, `module` and the repo-relative `rel`."""
    home = homes_of(tree)
    sights: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    nodes: Dict[str, List[ast.AST]] = defaultdict(list)
    files = 0
    for m in sorted(tree.records(), key=lambda r: r.get("path") or ""):
        rel = m.get("path")
        if not rel or not rel.endswith(".py") or Path(rel).name == "__init__.py":
            continue
        files += 1
        try:
            defs = collect_defs(tree.repo / rel)
        except (OSError, SyntaxError):
            continue
        for name, node in defs:
            if name in COMMON:
                continue
            sights[name].append({"home": home[m["module"]], "module": m["module"],
                                 "rel": rel, "sha": _body_sha(node)})
            nodes[name].append(node)
    out: Dict[str, Any] = {"identical": [], "drifted": [], "coincident": []}
    for name in sorted(sights):
        bodies = sights[name]
        homes = sorted({b["home"] for b in bodies})
        if len(homes) < 2:
            continue
        if len({b["sha"] for b in bodies}) == 1:
            bucket = "identical"
        elif similarity(nodes[name][0], nodes[name][1]) >= SIMILAR_MIN:
            bucket = "drifted"
        else:
            bucket = "coincident"
        out[bucket].append({"name": name, "homes": homes, "bucket": bucket, "bodies": bodies})
    out["totals"] = {k: len(out[k]) for k in ("identical", "drifted", "coincident")}
    out["measured_files"] = files
    out["similar_min"] = SIMILAR_MIN
    return out
