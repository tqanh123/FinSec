"""hs-code refactor clone-dedupe -- collapse an IDENTICAL production clone into
one home + imports.

Port of ob-tools' `devkit.clones.dedupe` (pinned commit `60075cb`). Keep the
copy in the home the others already depend on, delete the definition
elsewhere, put `from <home module> import <name>` in its place.

HOMES AND EDGES COME FROM ONE MEASUREMENT
    The tree is measured fresh (`codegraph.check.tree.measure`). A home is a
    workspace package, or a module that sits in none (`clones.homes_of`);
    edges are the measured import graph lifted to homes. Upstream read only
    `packages/<dist>/src` and the `ob-*` entries of each pyproject's
    `dependencies`, so on a flat tree there was nothing to dedupe and a
    non-`ob` workspace had no edges at all.

HOME is decided by DATA, not by the tool: `--homes FILE` (JSON
`{"<name>": "<home>"}`, a home spelled as `clone-dedupe`'s envelope `data` prints it),
else the one home every other home of the group already reaches.

A group is APPLIED only when its bucket is `identical`; the name has exactly
one top-level definition in the rewritten file; the rewritten file still
parses; and the edge is legal:
    * between two workspace packages: the target already reaches the home and
      the home does not reach back; a DIRECT edge is required -- a transitive
      one is `needs-dep` unless `--add-deps` appends the home package's
      `[project].name` to the target's `dependencies` (the lockfile is the
      caller's);
    * otherwise (a module): the new import IS the edge, so the one law is
      that the home must not already reach the target (`cycle`).
Everything else is LISTED with its reason, never guessed. Dry-run by default;
`--apply` writes. No verb-own `--log`: an unstamped plain-text log with 0
readers is exactly what `name-repair`'s own `--log` was dropped for
(docs/STANDARDIZE.md) -- the shell's `--write FILE` already gives this
verb an atomic, stamped record of the same envelope `data`.

Exit: 0 nothing owed (applied under --apply, or nothing to do) · 1 an edit is
owed (dry-run), a group was listed, or an import went unused after --apply ·
2 cannot run (no codemap.yaml, unreadable --homes).

WHAT IT CANNOT SEE
    * MODULE STATE is guarded, not proven: a function/class is moved only when
      every module-level name it reads is bound the same way in the target and
      the home module (same import, same constant, same def) and none of them
      mentions `__name__`/`__file__`/`__package__`; a mutable container constant
      and any dunder name are never moved. State reached through a call chain
      (`helper()` whose own globals differ) is not seen.
    * REBOUND state: a name any function of either module declares `global` is
      a cache/flag REASSIGNED per module -- importing it would freeze a copy,
      so it (and every function reading it) is never moved.
    * Imports that the removed definition alone used are NOT removed; with
      `ruff` on PATH the verb reports the F401 findings of the touched files
      (`unused_imports`).
    * Only module-level functions/classes/single-target constants.
    * Edges are MEASURED imports: a declared-but-unused dependency permits no
      edge here (pkgsync is the gate that keeps declared and imported equal).
    * Code that rebinds or `monkeypatch`es the removed name in the target
      module now moves the target's binding only.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import hs_family
from codegraph.check.f821 import find_ruff
from codegraph.check.tree import CheckError

from .clones import buckets_for
from .errors import RefactorError
from .movesymbol import _module_of
from hs_family.cli import Command


def _reach(graph: Dict[str, Set[str]], a: str) -> Set[str]:
    seen: Set[str] = set()
    todo = [a]
    while todo:
        for d in graph.get(todo.pop(), ()):
            if d not in seen:
                seen.add(d)
                todo.append(d)
    return seen


def home_graph(tree: Any) -> Dict[str, Set[str]]:
    """home -> homes it imports, lifted from the module import graph."""
    from codegraph.check.tree import module_graph
    from .clones import homes_of
    home = homes_of(tree)
    out: Dict[str, Set[str]] = {h: set() for h in home.values()}
    for mod, targets in module_graph(tree.records()).items():
        for t in targets:
            if home.get(t) and home[t] != home[mod]:
                out[home[mod]].add(home[t])
    return out


def _top_def(tree: ast.Module, name: str) -> List[ast.stmt]:
    hits: List[ast.stmt] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            hits.append(n)
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            hits.append(n)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                and n.target.id == name and n.value is not None:
            hits.append(n)
    return hits


def _span(n: ast.stmt) -> Tuple[int, int]:
    first = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
    return first, n.end_lineno or n.lineno


def _insert_line(tree: ast.Module) -> int:
    """0-based line index just after the last top-level import (or the docstring)."""
    last = 0
    for i, n in enumerate(tree.body):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            last = n.end_lineno or n.lineno
        elif i == 0 and isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str):
            last = n.end_lineno or n.lineno
    return last


_ID = ("__name__", "__file__", "__package__")


def _binds(tree: ast.Module) -> Dict[str, str]:
    """module-level bind name -> a canonical text of how it is bound."""
    out: Dict[str, str] = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ast.unparse(n)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = ast.unparse(n.value)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value:
            out[n.target.id] = ast.unparse(n.value)
        elif isinstance(n, ast.Import):
            for a in n.names:
                out[(a.asname or a.name).split(".")[0]] = f"import {a.name} as {a.asname}"
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out[a.asname or a.name] = f"from {'.' * n.level}{n.module} import {a.name}"
    return out


def _free_names(node: ast.stmt) -> Set[str]:
    """Names a definition READS from module scope (loads not bound inside it)."""
    import builtins
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
    bound: Set[str] = set()
    for x in ast.walk(node):
        if isinstance(x, ast.arg):
            bound.add(x.arg)
        elif isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            bound.add(x.id)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(x.name)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            bound.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in x.names)
    loads = {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
    return {n for n in loads - bound if n not in dir(builtins)}


_MUTABLE_PREFIX = ("set(", "dict(", "list(", "defaultdict(", "OrderedDict(", "deque(",
                   "Counter(", "threading.")


def state_reason(repo: Path, target_rel: str, home_rel: str, name: str,
                 together: FrozenSet[str] = frozenset()) -> Optional[str]:
    """Why moving `name` from target to home could change behaviour, or None."""
    if name.startswith("__") and name.endswith("__"):
        return "dunder-name"
    tt = ast.parse((repo / target_rel).read_text(encoding="utf-8"))
    ht = ast.parse((repo / home_rel).read_text(encoding="utf-8"))
    hits = _top_def(tt, name)
    if len(hits) != 1:
        return None
    node = hits[0]
    if any(isinstance(x, ast.Global) for x in ast.walk(node)):
        return "declares-global"
    if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
        val = ast.unparse(node.value)
        if val in ("{}", "[]") or val.startswith(_MUTABLE_PREFIX):
            return "mutable-state"
    tb, hb = _binds(tt), _binds(ht)
    rebound = {n for t in (tt, ht) for g in ast.walk(t) if isinstance(g, ast.Global) for n in g.names}
    if name in rebound:
        return "rebound-global"
    # names deduped in the same pass (same target home -> same home) are equal by construction
    diff = sorted(n for n in _free_names(node)
                  if n != name and n in tb and n not in together
                  and (hb.get(n) != tb[n] or any(i in tb[n] for i in _ID) or n in rebound))
    return f"module-state: {diff}" if diff else None


def plan(tree: Any, ruling: Dict[str, str], only: Optional[Set[str]], allow_private: bool,
         add_deps: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(edits, listed) -- pure measurement, never writes."""
    rep = buckets_for(tree)
    graph = home_graph(tree)
    packages = {p.name for p in tree.packages}
    roots = tuple(tree.descriptor.import_roots or ())
    edits: List[Dict[str, Any]] = []
    listed: List[Dict[str, Any]] = []
    for e in rep["identical"]:
        name = e["name"]
        if "." in name or (only and name not in only):
            continue
        if name.startswith("_") and not allow_private:
            listed.append({"name": name, "why": "private-name (pass --allow-private)"})
            continue
        homes = e["homes"]
        home = ruling.get(name)
        if home is None:
            cand = [h for h in homes if all(o == h or h in _reach(graph, o) for o in homes)]
            home = cand[0] if len(cand) == 1 else None
        if home is None or home not in homes:
            listed.append({"name": name, "why": "undecided", "homes": homes})
            continue
        home_sites = [b for b in e["bodies"] if b["home"] == home]
        if len(home_sites) != 1:
            listed.append({"name": name, "why": "same-home", "homes": [home]})
            continue
        home_rel = home_sites[0]["rel"]
        hmod = _module_of(home_rel, roots)
        for b in e["bodies"]:
            target = b["home"]
            if target == home:
                continue
            direct = True
            if target in packages and home in packages:
                if home not in _reach(graph, target) or target in _reach(graph, home):
                    listed.append({"name": name, "why": "no-edge-or-cycle", "target": target,
                                   "home": home})
                    continue
                direct = home in graph.get(target, set())
                if not direct and not add_deps:
                    listed.append({"name": name, "why": "needs-dep", "target": target, "home": home})
                    continue
            elif target in _reach(graph, home):
                listed.append({"name": name, "why": "cycle", "target": target, "home": home})
                continue
            edits.append({"name": name, "file": b["rel"], "home": home, "home_mod": hmod,
                          "_home_rel": home_rel, "_target": target, "direct": direct})
    # state guard as a fixpoint: an edit whose free names are moved by sibling
    # edits (same target, same home) is fine; drop the ones that still differ.
    while True:
        drop = []
        for ed in edits:
            together = frozenset(o["name"] for o in edits
                                 if o["_target"] == ed["_target"] and o["home"] == ed["home"])
            why = state_reason(tree.repo, ed["file"], ed["_home_rel"], ed["name"], together)
            if why:
                drop.append((ed, why))
        if not drop:
            break
        for ed, why in drop:
            edits.remove(ed)
            listed.append({"name": ed["name"], "why": why, "target": ed["file"]})
    for ed in edits:
        ed.pop("_home_rel")
        ed["target"] = ed.pop("_target")
    return edits, listed


def apply_edit(repo: Path, ed: Dict[str, Any], write: bool) -> Optional[str]:
    """None when applied/appliable, else the skip reason."""
    f = repo / ed["file"]
    src = f.read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = _top_def(tree, ed["name"])
    if len(hits) != 1:
        return "duplicate-in-file" if hits else "not-found"
    lines = src.splitlines(keepends=True)
    s, e = _span(hits[0])
    ins = _insert_line(tree)
    stmt = f"from {ed['home_mod']} import {ed['name']}\n"
    del lines[s - 1:e]
    if ins >= s:                                  # the def sat above the import block
        ins -= (e - s + 1)
    lines.insert(ins, stmt)
    new = "".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return "rewrite-does-not-parse"
    if write:
        f.write_text(new, encoding="utf-8")
    return None


def _project_name(pkg_dir: Path) -> str:
    try:
        doc = tomllib.loads((pkg_dir / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return pkg_dir.name
    return str((doc.get("project") or {}).get("name") or pkg_dir.name)


def add_dep(target_dir: Path, dep: str) -> List[str]:
    """Append `dep` to the target package's `dependencies` list once."""
    pp = target_dir / "pyproject.toml"
    txt = pp.read_text(encoding="utf-8")
    m = re.search(r"^(dependencies\s*=\s*\[)(.*?)(\])", txt, re.S | re.M)
    if not m or f'"{dep}"' in m.group(2):
        return []
    body = m.group(2).rstrip()
    body = f'{body}, "{dep}"' if body.strip() else f'"{dep}"'
    pp.write_text(txt[:m.start(2)] + body + txt[m.end(2):], encoding="utf-8")
    return [f"{target_dir.name}->{dep}"]


def _unused_imports(repo: Path, files: Set[str]) -> List[str]:
    """Advisory only (a `LEFTOVER` note, never a refusal): the SAME ruff discovery
    `check.f821.find_ruff`/`carve._undefined` use (a target repo's own `.venv/bin/ruff`
    included), rather than a second `shutil.which` copy that never looked there."""
    if not files:
        return []
    try:
        ruff = find_ruff(repo, None)
    except CheckError:
        return []
    p = subprocess.run([ruff, "check", "--select", "F401", "--output-format", "concise",
                        "--no-cache", *sorted(files)], cwd=repo, capture_output=True, text=True)
    return [ln for ln in p.stdout.splitlines() if " F401 " in ln]


def _print_report(res: Dict[str, Any]) -> None:
    verb = "applied" if res["applied"] else "would edit"
    print(f"clone-dedupe: {verb} {len(res['edits'])} in {len({e['file'] for e in res['edits']})} "
          f"file(s) · listed {len(res['listed'])}")
    for ed in res["edits"]:
        print(f"  {ed['file']}: {ed['name']} -> {ed['home_mod']}")
    for x in res["listed"]:
        print(f"  LIST {x['why']}: {x['name']}  {x.get('homes') or x.get('target') or ''}")
    for u in res["unused_imports"]:
        print(f"  LEFTOVER {u}")
    if not res["applied"] and res["edits"]:
        print("dry-run: pass --apply to write")


def run(repo: Path, args: argparse.Namespace, apply: bool) -> Dict[str, Any]:
    from codegraph.check.tree import CheckError, measure
    try:
        ruling = json.loads(Path(args.homes).read_text(encoding="utf-8")) if args.homes else {}
    except (OSError, ValueError) as e:
        raise RefactorError("clone-dedupe: cannot read --homes %s: %s" % (args.homes, e)) from None
    try:
        tree = measure(repo)
    except CheckError as e:
        raise RefactorError("clone-dedupe: %s" % e) from None
    edits, listed = plan(tree, ruling, set(args.name) if args.name else None,
                         args.allow_private, args.add_deps)
    applied: List[Dict[str, Any]] = []
    for ed in sorted(edits, key=lambda x: (x["file"], x["name"])):
        why = apply_edit(repo, ed, write=apply)
        if why:
            listed.append({"name": ed["name"], "why": why, "target": ed["file"]})
        else:
            applied.append(ed)
    files_done = {ed["file"] for ed in applied}
    added: List[str] = []
    if apply:
        dirs = {p.name: p.dir for p in tree.packages}
        for ed in applied:
            if not ed["direct"]:
                added += add_dep(dirs[ed["target"]], _project_name(dirs[ed["home"]]))
    unused = _unused_imports(repo, files_done) if apply else []
    return {"applied": apply, "edits": applied, "listed": listed,
            "deps_added": sorted(set(added)), "unused_imports": unused,
            "ok": not listed and not unused and (apply or not applied)}


def _clone_dedupe_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--homes", metavar="FILE",
                    help="JSON {'<name>': '<home>'}: the owner's ruling for a home")
    ap.add_argument("--name", action="append", help="restrict to this name (repeatable)")
    ap.add_argument("--add-deps", action="store_true",
                    help="between workspace packages: also apply edits whose edge is only "
                         "transitive, declaring the direct dependency in the target's pyproject")
    ap.add_argument("--allow-private", action="store_true",
                    help="also dedupe `_private` names")


def _clone_dedupe(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        res = run(Path(root), args, bool(getattr(opts, "apply", False)))
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    if getattr(opts, "json", False):
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(res)
    return 0 if res["ok"] else 1


CLONE_DEDUPE = Command(add_args=_clone_dedupe_args, run=_clone_dedupe)
