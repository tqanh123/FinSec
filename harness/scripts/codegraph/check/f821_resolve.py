"""hs-code check f821 --resolve -- propose (dry-run) or write (--apply) the statement each
undefined name needs.

The gate (`check f821`) says WHICH names are undefined; this says where each one lives. A
finding resolves ONLY from a measurement, in this order:

    pin      a per-case ruling a human made (`--pins FILE`, `"<file>::<name>"` keys):
             stmt | tc | copy_from (+name) | blocked, with an optional add_dep
    stdlib   the name is a stdlib module                 -> `import name`
    typing   the name is exported by `typing`            -> `from typing import name`
    known    an enabled pack's `data/f821-known-names.json` `imports` -> its statement
    copy     ... its `copy` table (a per-module name like `logger`) -> the text, copied
    twin     the UPSTREAM source(s) of this module (batch specs, else a same-basename
             twin) bound it -> the same binding, the old module translated by the batch
             module map; a module alias with SEVERAL houses is decided by the attributes
             the file reads off it (`C.cmd_scan`)
    moved    upstream DEFINED it and a renames/name_moves row says where it went
    home     exactly one module of the tree defines it   -> `from <mod> import name`
    copy     upstream defined it, no legal import reaches it, and it is <= --copy-max
             lines -> the upstream def is COPIED verbatim

Anything else is LISTED with every candidate and left alone -- never guessed. A binding
upstream made INSIDE a function is re-made inside the using function; a top-level import
that would close an import cycle drops to the using function too. A statement whose target
the file's dist may not import (its pyproject declares no such dependency) becomes a
`TYPE_CHECKING` import when every use is an annotation, else a copy, else an orphan.
`--apply` iterates to a fixpoint (a copied def can itself lose names), at most 6 rounds.

Adapted from `ob-tools@60075cb:packages/portkit/src/portkit/f821/` -- `resolve.py`
(`resolve`, `_translate`, `_split_alias`, `_alias_pick`, `_moved`, `_twin_copy`, `_legal`,
`_pinned`, `add_dependency`, `_round`, `run_resolve`), `edit.py` (`plan_file`,
`annotation_only`, `enclosing_def`, `top_insert_line`), `ctx.py` (`Ctx`, `upstream_def`) and
`index.py` (`bound_names`, `used_attrs`, `import_graph`, `reaches`, `def_segment`,
`edge_legal`). The upstream-facing half (`Binding`, `upstream_bindings`, `twin_files`,
`old_to_new`) is `refactor.rebind`'s, already adapted. What changed:
    * the tree's modules, and which module a file is, are the snapshot pipeline's answer
      over the declared layout (`import_context`), never `packages/*/src`;
    * a dist is `layout.dist_of_path`; its declared dependencies are `pkgsync.declare`'s
      reading of its pyproject, compared by normalized project name; a file outside every
      workspace package's code directory may import any dist (upstream exempted only
      paths without `/src/`, and a `@tests` pseudo-dist);
    * the upstream half is on only in a port (`.port/port.yaml`, `--source`/`--batch`):
      twin bindings, batch renames/name_moves and upstream defs come through
      `rebind.Ctx`; elsewhere a name resolves from pin/stdlib/typing/known/home only;
    * known names are PACK data -- every enabled pack's `data/f821-known-names.json`, in
      `preset:` order -- never a file shipped with the harness (upstream's held
      OpsBrain's `psycopg.rows.dict_row`);
    * the findings are ruff's over the gate's own scopes (`f821.ruff_rows`);
    * exit 1 also while resolvable names are pending in a dry-run (upstream: 0).

WHAT IT CANNOT SEE
    * Only TOP-LEVEL bindings count as "defined in": a name built by `globals()`, a
      decorator-registered name or a class attribute has no home, and stays orphan.
    * The import graph holds top-level absolute imports only: a cycle that exists only
      at call time is not seen (and cannot break import).
    * Same name, two houses: `home` needs exactly one definer; `twin`/`moved` follow
      upstream -- a rename upstream itself never made is invisible.
    * `copy` duplicates code on purpose -- the escape from an illegal dist edge, not a
      design; the copy is reported so a later dedupe can find it.
    * A `TYPE_CHECKING`-only upstream binding is treated as top-level.
    * Statement ORDER: a copied constant lands right after the imports, before code
      that may rebind a name it uses; a use in a class body or lambda default has no
      enclosing function, so a `local` statement for it falls back to top-level and
      says so.
    * Nothing here runs the code -- the proof is the gate re-run and the tests.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import typing
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from codegraph._core import layout as _layout
from codegraph.check.tree import CheckError
from codegraph.refactor.rebind import Binding, _defs, _parse, _stmt, twin_files, upstream_bindings

KNOWN_NAMES = Path("data") / "f821-known-names.json"
ROUNDS = 6


# -- the tree's facts ---------------------------------------------------------------------

def bound_names(path: Path) -> Set[str]:
    """Names a module binds at top level: defs AND import aliases (a package door
    re-exporting a symbol still answers `pkg.symbol`)."""
    t = _parse(path)
    if not t:
        return set()
    out = _defs(t)
    for n in t.body:
        if isinstance(n, ast.ImportFrom):
            out |= {a.asname or a.name for a in n.names}
        elif isinstance(n, ast.Import):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
    return out


def used_attrs(path: Path, local: str) -> Set[str]:
    """Attributes the file reads off the name `local` (`C.cmd_scan` -> cmd_scan)."""
    t = _parse(path)
    return {n.attr for n in ast.walk(t) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == local} if t else set()


def import_graph(mods: Dict[str, Path]) -> Dict[str, Set[str]]:
    """module -> modules of the tree it imports at TOP level (absolute imports only)."""
    g: Dict[str, Set[str]] = {}
    for mod, f in mods.items():
        t = _parse(f)
        edges: Set[str] = set()
        for n in (t.body if t else ()):
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                edges.add(n.module)
                edges |= {"%s.%s" % (n.module, a.name) for a in n.names}
            elif isinstance(n, ast.Import):
                edges |= {a.name for a in n.names}
        g[mod] = {e for e in edges if e in mods}
    return g


def reaches(g: Dict[str, Set[str]], start: str, goal: str) -> bool:
    seen: Set[str] = set()
    todo = [start]
    while todo:
        cur = todo.pop()
        if cur == goal:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        todo.extend(g.get(cur, ()))
    return False


def def_segment(path: Path, name: str) -> Optional[Tuple[str, int]]:
    """Source text (+ line count) of the top-level def/class/assign `name`, decorators
    included."""
    t = _parse(path)
    if not t:
        return None
    text = path.read_text(encoding="utf-8")
    for n in t.body:
        hit = ((isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)
               or (isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id == name
                                                     for x in n.targets))
               or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
                   and n.target.id == name))
        if hit:
            start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
            end = n.end_lineno or n.lineno
            return "\n".join(text.splitlines()[start - 1:end]), end - start + 1
    return None


def known_names(repo: Path, harness_root: Path) -> Dict[str, Dict[str, str]]:
    """Every enabled pack's `data/f821-known-names.json`, merged in `preset:` order (a
    later pack's entry wins); a pack without one contributes nothing."""
    import pack_config
    try:
        preset = pack_config.read_enabled_preset(repo)
    except pack_config.PackConfigError as e:
        raise CheckError(str(e)) from e
    dirs, problems = pack_config.enabled_pack_dirs(harness_root, preset)
    if problems:
        raise CheckError("; ".join(problems))
    out: Dict[str, Dict[str, str]] = {"imports": {}, "copy": {}}
    for d in dirs:
        f = d / KNOWN_NAMES
        if not f.is_file():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            raise CheckError("%s is not JSON: %s" % (f, e)) from e
        for key in out:
            out[key].update({str(k): str(v) for k, v in (doc.get(key) or {}).items()})
    return out


def _dists(packages) -> Tuple[Dict[str, str], Dict[str, Set[str]]]:
    """(package dir name -> normalized project name, dir name -> normalized declared deps)."""
    from codegraph.check.pkgsync import declare, norm
    names: Dict[str, str] = {}
    deps: Dict[str, Set[str]] = {}
    for p in packages:
        try:
            d = declare(p, "")
            names[p.name], deps[p.name] = norm(d.name), {norm(x) for x in d.deps}
        except CheckError:
            names[p.name], deps[p.name] = norm(p.name), set()
    return names, deps


class Ctx:
    """The facts `--resolve` decides against, loaded once per round."""

    def __init__(self, repo: Path, descriptor: Any, packages, port=None, copy_max: int = 40,
                 pins: Optional[Dict[str, dict]] = None,
                 known: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        from codegraph._core.snapshot.pipeline import import_context
        self.repo, self.descriptor, self.port = repo.resolve(), descriptor, port
        self.copy_max, self.pins = copy_max, dict(pins or {})
        self.ic = port._tgt if port is not None else import_context(self.repo, descriptor)
        self.mods: Dict[str, Path] = {s.module: s.path for s in self.ic.sources}
        if port is not None:
            self.names = port.names
        else:
            self.names = {}
            for mod, f in self.mods.items():
                t = _parse(f)
                for n in (_defs(t) if t else ()):
                    self.names.setdefault(n, set()).add(mod)
        spec = port.spec if port is not None else None
        self.moved: Dict[str, str] = ({**(spec.renames or {}), **(spec.name_moves or {})}
                                      if spec is not None else {})
        self.multi: Dict[str, List[str]] = dict(port.multi) if port is not None else {}
        known = known or {}
        self.known, self.copy_text = dict(known.get("imports") or {}), dict(known.get("copy") or {})
        self.graph = import_graph(self.mods)
        self.code_dirs = {p.name: p.code.resolve() for p in packages}
        self.pkg_dirs = {p.name: p.dir for p in packages}
        self.dist_names, self.deps = _dists(packages)
        self._twin: Dict[Path, Dict[str, List[Binding]]] = {}
        self.splits: Dict[Tuple[Path, str], dict] = {}
        self.extra: Dict[Path, List[Path]] = {}
        self._up_defs: Optional[Dict[str, List[Tuple[Path, str]]]] = None

    @property
    def package(self) -> Optional[str]:
        return self.port.package if self.port is not None else None

    def rel(self, f: Path) -> str:
        return f.resolve().relative_to(self.repo).as_posix()

    def module_of(self, f: Path) -> Optional[str]:
        return self.ic.module_at(self.rel(f))

    def dist_of(self, f: Path) -> Optional[str]:
        return _layout.dist_of_path(self.descriptor, self.rel(f))

    def in_code(self, f: Path) -> bool:
        """Inside a workspace package's code directory -- the only files a dist edge binds."""
        r = f.resolve()
        return any(d == r or d in r.parents for d in self.code_dirs.values())

    def edge_legal(self, src: Optional[str], dst: Optional[str]) -> bool:
        if src is None or dst is None or src not in self.code_dirs or src == dst \
                or dst not in self.code_dirs:
            return True
        return self.dist_names[dst] in self.deps.get(src, set())

    def old_to_new(self, old: str, dist: Optional[str]) -> List[str]:
        return self.port.old_to_new(old, dist) if self.port is not None else []

    def old_module_file(self, old: str) -> Optional[Path]:
        return self.port._src.file_of(old) if self.port is not None else None

    def twin_bindings(self, f: Path) -> Dict[str, List[Binding]]:
        if self.port is None:
            return {}
        if f not in self._twin:
            mod = self.module_of(f)
            files = list(self.port.sources.get(mod, []) if mod else [])
            if not files:
                files = twin_files(self.port.upstream, f)
            files += [p for p in self.extra.get(f, []) if p not in files]
            merged: Dict[str, List[Binding]] = {}
            for t in files:
                for k, v in upstream_bindings(t, self.port.old_module_of(t)).items():
                    merged.setdefault(k, []).extend(v)
            self._twin[f] = merged
        return self._twin[f]

    def upstream_def(self, name: str, prefer: Optional[List[Path]] = None) -> Optional[Tuple[str, Path]]:
        """The ONLY upstream top-level def of `name`: (text, file), or None when absent,
        ambiguous or longer than --copy-max. `prefer` narrows a tie to the upstream files
        this module already borrows from."""
        if self.port is None:
            return None
        if self._up_defs is None:
            self._up_defs = defaultdict(list)
            for s in self.port._src.sources:
                t = _parse(s.path)
                for n in (t.body if t else ()):
                    names = ([n.name] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                             else [x.id for x in getattr(n, "targets", []) if isinstance(x, ast.Name)])
                    for nm in names:
                        self._up_defs[nm].append((s.path, nm))
        hits = self._up_defs.get(name, [])
        if len(hits) > 1 and prefer:
            hits = [h for h in hits if h[0] in prefer] or hits
        segs = {(def_segment(p, nm) or ("", 0))[0]: p for p, nm in hits}
        if len(segs) != 1 or "" in segs:
            return None
        seg, p = next(iter(segs.items()))
        return (seg, p) if seg.count("\n") + 1 <= self.copy_max else None


# -- deciding -----------------------------------------------------------------------------

def _alias_pick(houses: List[str], f: Path, local: str, c: Ctx) -> List[str]:
    """Several houses for a module alias: keep those that bind EVERY attribute the file
    reads off the alias; ancestor chains collapse to the deepest."""
    attrs = used_attrs(f, local)
    if len(houses) <= 1 or not attrs:
        return houses
    ok = [h for h in houses if h in c.mods and attrs <= bound_names(c.mods[h])]
    if len(ok) > 1:
        deepest = max(ok, key=len)
        if all(o == deepest or deepest.startswith(o + ".") for o in ok):
            ok = [deepest]
    return ok


def _moved(b: Binding, c: Ctx) -> Optional[Tuple[str, str]]:
    """(new module, new name) from a renames/name_moves row, when it exists in the tree."""
    row = c.moved.get("%s::%s" % (b.module, b.orig))
    if not row:
        return None
    mod, _, nm = str(row).partition("::")
    return (mod, nm or b.orig) if mod in c.mods else None


def _split_alias(b: Binding, local: str, f: Path, c: Ctx) -> Optional[str]:
    """A module alias some of whose attributes the batch MOVED to another house
    (name_moves rows): keep the alias for the rest, and give each moved attribute's house
    its own alias -- the file's `L.attr` reads are rewritten. Returns the primary
    statement, with the split recorded on `c.splits`."""
    old = "%s.%s" % (b.module, b.orig)
    moved: Dict[str, Tuple[str, str]] = {}
    for a in used_attrs(f, local):
        row = str(c.moved.get("%s::%s" % (old, a)) or "")
        if "::" in row and row.split("::")[0] in c.mods:
            moved[a] = (row.split("::")[0], row.split("::")[1])
    if not moved:
        return None
    rest = used_attrs(f, local) - set(moved)
    homes = [h for h in c.old_to_new(old, c.dist_of(f))
             if h in c.mods and rest <= bound_names(c.mods[h])]
    if len(homes) > 1:
        homes = [max(homes, key=len)]
    if len(homes) != 1:
        return None
    parent, _, last = homes[0].rpartition(".")
    by_mod: Dict[str, List[str]] = {}
    for a, (m, _n) in moved.items():
        by_mod.setdefault(m, []).append(a)
    extras = {}
    for m, attrs in by_mod.items():
        alias = "%s_%s" % (local, m.rsplit(".", 1)[-1])
        pm, _, lm = m.rpartition(".")
        extras[alias] = {"stmt": _stmt(pm, lm, alias) if pm else "import %s as %s" % (m, alias),
                         "attrs": {a: moved[a][1] for a in attrs}}
    c.splits[(f, local)] = extras
    return _stmt(parent, last, local) if parent else _stmt(homes[0], "", local)


def _translate(b: Binding, local: str, f: Path, c: Ctx) -> Tuple[Optional[str], List[str]]:
    """One upstream binding -> a statement of the tree, or (None, candidates)."""
    dist = c.dist_of(f)
    if b.kind == "def":
        moved = _moved(b, c)
        return (_stmt(moved[0], moved[1], local), []) if moved else (None, [])
    if b.module.split(".")[0] != c.package:                   # stdlib / third party
        return _stmt(b.module, b.orig, local), []
    if b.kind == "name":                                      # `from pkg import mod as L` whose attrs moved apart
        split = _split_alias(b, local, f, c)
        if split:
            return split, []
    if b.kind == "module":
        homes = _alias_pick(c.old_to_new(b.module, dist), f, local, c)
        if len(homes) == 1:
            m = homes[0]
            return (_stmt(m.rsplit(".", 1)[0], m.rsplit(".", 1)[1], local)
                    if "." in m else _stmt(m, "", local)), []
        return None, homes
    moved = _moved(b, c)                                      # a renamed / moved NAME
    if moved:
        return _stmt(moved[0], moved[1], local), []
    old = "%s.%s" % (b.module, b.orig)
    sub = c.old_to_new(old, dist)                             # `from pkg import submodule`
    if sub and all(m.rsplit(".", 1)[-1] == b.orig or m in c.multi.get(old, []) for m in sub):
        sub = _alias_pick(sub, f, local, c)
        if len(sub) == 1:
            parent, _, last = sub[0].rpartition(".")
            return (_stmt(parent, last, local) if parent else _stmt(sub[0], "", local)), []
    homes = sorted(c.names.get(b.orig, ()))
    if len(homes) > 1:
        pref = c.old_to_new(b.module, dist)
        homes = [m for m in homes if m in pref or any(m.startswith(p + ".") for p in pref)] or homes
    return (_stmt(homes[0], b.orig, local), []) if len(homes) == 1 else (None, homes)


def _twin_copy(tb: List[Binding], name: str, c: Ctx) -> Optional[Tuple[str, Path]]:
    """Copy source straight from the module the upstream binding pointed at."""
    for b in tb:
        if b.kind == "module" or not b.module:
            continue
        p = c.old_module_file(b.module)
        if p is None:
            continue
        seg = def_segment(p, b.orig or name)
        if seg and seg[1] <= c.copy_max:
            return seg[0], p
    return None


def _legal(stmt: str, f: Path, c: Ctx) -> Optional[str]:
    """None when the statement's target may be imported from `f`, else why not."""
    tgt = stmt.split()[1] if stmt.startswith("from ") else None
    if not tgt or tgt not in c.mods or not c.in_code(f):
        return None
    d, td = c.dist_of(f), c.dist_of(c.mods[tgt])
    return None if c.edge_legal(d, td) else "illegal dist edge %s -> %s" % (d, td)


def _pinned(f: Path, name: str, c: Ctx) -> Optional[dict]:
    pin = c.pins.get("%s::%s" % (c.rel(f), name))
    if not pin:
        return None
    if "blocked" in pin:
        return {"unresolved": "blocked: %s" % pin["blocked"], "candidates": []}
    extra = {"add_dep": pin["add_dep"]} if "add_dep" in pin else {}
    if "stmt" in pin:
        return {"stmt": pin["stmt"], "how": "pin", "local": bool(pin.get("local")), **extra}
    if "tc" in pin:
        return {"stmt": pin["tc"], "how": "pin+type-checking", "local": False, "tc": True,
                "annotation_only_check": True, "why": "pinned type-only import"}
    if "copy_from" in pin and c.port is not None:
        p = c.port.upstream / pin["copy_from"]
        seg = def_segment(p, pin.get("name", name)) if p.is_file() else None
        if seg:
            return {"copy": seg[0], "src": str(p), "how": "pin+copy", "local": False}
    return {"unresolved": "pin unusable: %s" % pin, "candidates": []}


def resolve(f: Path, name: str, c: Ctx) -> dict:
    """(file, name) -> {"stmt"|"copy", "how", "local"} or {"unresolved", "candidates"}."""
    pinned = _pinned(f, name, c)
    if pinned is not None:
        return pinned
    mod = c.module_of(f)
    dist = c.dist_of(f)
    tb = c.twin_bindings(f).get(name, [])
    if name in sys.stdlib_module_names and all(b.kind == "module" and b.module == name for b in tb):
        return {"stmt": "import %s" % name, "how": "stdlib", "local": False}
    if name in typing.__all__ and not any(b.kind == "def" for b in tb):
        return {"stmt": "from typing import %s" % name, "how": "typing", "local": False}
    if name in c.known:
        return {"stmt": c.known[name], "how": "known", "local": False}
    if name in c.copy_text:                     # per-module names: never imported from a sibling
        return {"copy": c.copy_text[name], "how": "copy-known", "local": False}
    stmts: Dict[str, bool] = {}
    cands: List[str] = []
    for b in tb:
        s, cs = _translate(b, name, f, c)
        if s:
            stmts[s] = stmts.get(s, True) and b.local
        cands += cs
    if len(stmts) > 1:
        return {"unresolved": "twin bindings disagree", "candidates": sorted(stmts)}
    if stmts:
        s, loc = next(iter(stmts.items()))
        how = "twin"
    else:
        homes = sorted(m for m in c.names.get(name, ()) if m != mod)
        same = [m for m in homes if c.dist_of(c.mods[m]) == dist]
        homes = same if len(homes) > 1 and same else homes
        if len(homes) > 1 and c.in_code(f):     # only houses this dist may import
            ok = [h for h in homes if c.edge_legal(dist, c.dist_of(c.mods[h]))]
            homes = ok or homes
        if len(homes) > 1:                      # the module upstream DEFINED it in, translated
            anc = {h for b in tb if b.kind == "def" and b.module for h in c.old_to_new(b.module, dist)}
            homes = [h for h in homes if h in anc] or homes
        if len(homes) == 1:
            s, loc, how = "from %s import %s" % (homes[0], name), False, "home"
        else:
            got = c.upstream_def(name, c.extra.get(f))
            if got:
                return {"copy": got[0], "src": str(got[1]), "how": "copy-def", "local": False}
            return {"unresolved": "no home" if not homes else "several homes",
                    "candidates": sorted(set(map(str, cands)) | set(homes))}
    why = _legal(s, f, c)
    if why:
        got = _twin_copy(tb, name, c) or c.upstream_def(name, c.extra.get(f))
        if got:
            return {"copy": got[0], "src": str(got[1]), "how": "copy-def", "local": False}
        return {"stmt": s, "how": "type-checking", "local": False, "tc": True, "why": why,
                "annotation_only_check": True}
    if (f, name) in c.splits:
        return {"stmt": s, "how": "twin+split-alias", "local": loc,
                "split": {"local": name, **c.splits[(f, name)]}}
    tgt = s.split()[1] if s.startswith("from ") else None
    if not loc and mod and tgt in c.mods and reaches(c.graph, tgt, mod):
        return {"stmt": s, "how": how + "+cycle", "local": True}
    return {"stmt": s, "how": how, "local": loc}


# -- writing ------------------------------------------------------------------------------

def enclosing_def(tree: ast.Module, line: int) -> "Optional[ast.FunctionDef | ast.AsyncFunctionDef]":
    best: "Optional[ast.FunctionDef | ast.AsyncFunctionDef]" = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.lineno <= line <= (n.end_lineno or n.lineno):
            if best is None or n.lineno >= best.lineno:
                best = n
    return best


def top_insert_line(tree: ast.Module) -> int:
    """1-based line AFTER which top-level imports go (0 = file start)."""
    last = 0
    for i, n in enumerate(tree.body):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            last = n.end_lineno or n.lineno
        elif i == 0 and isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant):
            last = n.end_lineno or n.lineno
    return last


def annotation_only(text: str, at: List[Tuple[int, int]]) -> bool:
    """True when EVERY (row, col) sits inside an annotation not evaluated at import time
    -- a STRING annotation, or any annotation under `from __future__ import annotations`
    -- the only uses a TYPE_CHECKING-only import can serve without a NameError."""
    tree = ast.parse(text)
    future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                 and any(a.name == "annotations" for a in n.names) for n in tree.body)
    spans = []
    for n in ast.walk(tree):
        roots = ([n.annotation] if isinstance(n, (ast.arg, ast.AnnAssign)) and n.annotation else []) \
            + ([n.returns] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns else [])
        for a in roots:
            if future or (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                spans.append((a.lineno, a.col_offset, a.end_lineno, a.end_col_offset))
    return bool(at) and all(any((r, col) >= (s0, c0) and (r, col) < (e0, ce) for s0, c0, e0, ce in spans)
                            for r, col in at)


def plan_file(text: str, items: List[dict]) -> Tuple[str, List[str]]:
    """items: {"name", "stmt"?, "copy"?, "local", "at": [lines]} -> (new text, notes).
    Never returns text that does not parse."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    top: List[str] = []
    copies: List[str] = []
    local: Dict[int, Tuple[Any, List[str]]] = {}
    notes: List[str] = []
    tc: List[str] = []
    for it in items:
        if it.get("tc"):
            tc.append(it["stmt"])
        elif "copy" in it:
            copies.append(it["copy"])
        elif not it["local"]:
            top.append(it["stmt"])
        else:
            for ln in it["at"]:
                d = enclosing_def(tree, ln)
                if d is None:
                    top.append(it["stmt"])
                    notes.append("%s: local statement, no enclosing def at line %d — placed top-level"
                                 % (it["name"], ln))
                else:
                    local.setdefault(d.lineno, (d, []))[1].append(it["stmt"])
    for it in items:                                # split-alias: extra imports + `L.attr` rewrites
        for alias, ex in (it.get("split") or {}).items():
            if alias == "local":
                continue
            top.append(ex["stmt"])
            for attr in ex["attrs"]:
                for n in ast.walk(tree):
                    if (isinstance(n, ast.Attribute) and n.attr == attr and isinstance(n.value, ast.Name)
                            and n.value.id == it["split"]["local"]):
                        ln = lines[n.value.lineno - 1]
                        c0 = n.value.col_offset
                        lines[n.value.lineno - 1] = ln[:c0] + alias + ln[c0 + len(n.value.id):]
    inserts: List[Tuple[int, str]] = []
    if tc:
        if not re.search(r"^\s*from typing import [^\n]*\bTYPE_CHECKING\b", text, re.M):
            top.append("from typing import TYPE_CHECKING")
        copies.append("if TYPE_CHECKING:\n" + "".join("    %s\n" % s for s in sorted(set(tc))).rstrip("\n"))
    if top or copies:
        blk = "".join(s + "\n" for s in sorted(set(top)))
        if copies:
            blk += "\n" + "\n\n".join(dict.fromkeys(copies)) + "\n"
        inserts.append((top_insert_line(tree), blk))
    for d, stmts in local.values():
        b0 = d.body[0]
        if isinstance(b0, ast.Expr) and isinstance(getattr(b0, "value", None), ast.Constant) \
                and len(d.body) > 1:
            after, indent = (b0.end_lineno or b0.lineno), " " * d.body[1].col_offset
        else:
            after, indent = b0.lineno - 1, " " * b0.col_offset
        inserts.append((after, "".join("%s%s\n" % (indent, s) for s in sorted(set(stmts)))))
    for after, blk in sorted(inserts, key=lambda x: -x[0]):
        lines.insert(after, blk)
    new = "".join(lines)
    ast.parse(new)
    return new, notes


def add_dependency(dist_dir: Path, dep: str) -> bool:
    """Append `dep` to the dist's `dependencies = [...]` (idempotent). A layering decision
    a human pinned -- the tool never adds an edge on its own."""
    pp = dist_dir / "pyproject.toml"
    text = pp.read_text(encoding="utf-8")
    m = re.search(r"^dependencies\s*=\s*\[([^\]]*)\]", text, re.M)
    if not m or '"%s"' % dep in m.group(1):
        return False
    body = m.group(1).rstrip()
    new = 'dependencies = [%s%s"%s"]' % (body, ", " if body.strip() else "", dep)
    pp.write_text(text[:m.start()] + new + text[m.end():], encoding="utf-8")
    return True


def _round(rows: List[Tuple[Path, str, int, int]], c: Ctx, apply: bool) -> Tuple[list, list, int]:
    by_file: Dict[Path, Dict[str, List[Tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    for f, name, row, col in rows:
        by_file[f][name].append((row, col))
    resolved, orphan, edits = [], [], defaultdict(list)
    for f, names in sorted(by_file.items()):
        for name, at in sorted(names.items()):
            res = resolve(f, name, c)
            rec = {"file": c.rel(f), "name": name, "uses": len(at)}
            if res.pop("annotation_only_check", False) and not annotation_only(
                    f.read_text(encoding="utf-8"), at):
                res = {"unresolved": res["why"] + " (and a use outside an annotation)",
                       "candidates": [res["stmt"]]}
            if "stmt" in res or "copy" in res:
                resolved.append({**rec, **res})
                edits[f].append({"name": name, "at": [r for r, _ in at], **res})
            else:
                orphan.append({**rec, **res})
    for f, items in edits.items():
        for it in items:
            if it.get("src"):
                borrowed = c.extra.setdefault(f, [])
                if Path(it["src"]) not in borrowed:
                    borrowed.append(Path(it["src"]))
                c._twin.pop(f, None)
    written = 0
    if apply:
        for f, items in edits.items():
            new, _notes = plan_file(f.read_text(encoding="utf-8"), items)
            f.write_text(new, encoding="utf-8")
            written += 1
            dist = c.dist_of(f)
            for it in items:
                if it.get("add_dep") and dist in c.pkg_dirs:
                    add_dependency(c.pkg_dirs[dist], it["add_dep"])
    return resolved, orphan, written


def run_resolve(scan, make_ctx, apply: bool) -> dict:
    """`scan()` -> the findings as (file, name, row, col); `make_ctx()` -> a fresh `Ctx`.
    --apply iterates to a fixpoint: a copied def can itself lose names."""
    c = make_ctx()
    all_resolved: list = []
    orphan: list = []
    written = findings0 = 0
    for i in range(ROUNDS if apply else 1):
        if i:
            keep = c.extra
            c = make_ctx()
            c.extra = keep
        rows = scan()
        findings0 = findings0 or len(rows)
        resolved, orphan, w = _round(rows, c, apply)
        all_resolved += resolved
        written += w
        if not apply or not resolved:
            break
    return {"schema": "hs-code.check.f821-resolve/1", "findings": findings0,
            "resolved": all_resolved, "orphan": orphan, "applied_files": written,
            "dry_run": not apply}
