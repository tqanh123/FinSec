"""hs-code refactor rebind -- repoint a test import bound to a different KIND of object than
its upstream twin's.

`from <old>.api import main` upstream binds the MODULE `<old>/api/main.py`. A name-home move
that saw only the name `main` found one DEF called `main` elsewhere and rewrote the line to
`from <ns>.cli.cli import main` (a function): every use of `main.<attr>` then dies, and no
import check sees it because BOTH binds resolve.

    from acme.cli.cli import main     ->   from acme.api import main       (upstream: a module)

A line is rewritten only when ALL hold: the test has an upstream twin; every twin binding of
the local name is a `from <pkg> import <name>` whose `<pkg>.<name>` was a SUBMODULE upstream;
the current bind is `from <ns>.<m> import <name>` where `<ns>.<m>.<name>` is NOT a module.
The REVERSE flip (upstream bound a def, the port binds a module of that name) is repaired
the same way, to the def's home. A second pass repoints STRING targets
(`monkeypatch.setattr("<ns>.m.attr")`) whose module no longer resolves `attr`, and a third
repairs ATTRIBUTES (`A.attr` on a module alias whose module lost `attr` to a sibling) through
a new alias -- no re-export is added to the old door. Dry-run by default; `--apply` writes;
a second run is clean.

Adapted from `ob-tools@60075cb:packages/portkit/src/portkit/rebind.py`, with the part of
`portkit/f821/index.py` + `f821/ctx.py` it calls (`Binding`, `upstream_bindings`,
`twin_files`, `upstream_sources`, `resolve_old_module`, `Ctx.old_to_new`/`twin_bindings`,
`_stmt`). What changed, and only this:
    * the old package is the source's `package`, the new namespace the target's
      `namespace_root` (`_port.port_context`), never the `gsm_agent`/`ob` literals;
    * which module a file is -- on BOTH trees -- is the snapshot pipeline's answer
      (`import_context`) over each tree's declared layout (the source's own `codemap.yaml`),
      never `packages/*/src` or `upstream/src`; test trees are `namerepair.suite_roots`;
    * module lookup and the definition index of the target are `namerepair.Tree`, the
      `name-repair` verb's own (upstream's `_Tree`);
    * the batch specs (`--batch B`) are read through `port_spec` (`merged_spec_and_unit_dist`
      for the maps, `Spec.load` for each spec's sources), never a second YAML reader.

WHAT IT CANNOT SEE
    * A bind whose twin is not found (no same-basename upstream file) -- nothing to compare.
    * Only `from <ns>.x import n` at any nesting; `import <ns>.x as n` is not judged, and only
      module-vs-not, never class-vs-function.
    * A module whose old->new mapping is missing or ambiguous is not repaired (skipped
      silently) -- a collect run finds what stays broken.
    * An upstream name bound BOTH as a module and as something else across twin files is left
      alone, listed under `skipped`.
    * String targets are judged by ATTRIBUTE: a module-only string, or an attr no upstream
      string names, is not repaired.
    * Attributes: only `A.attr` and `setattr/getattr/hasattr/delattr(A, "attr", ...)` on an
      alias bound by `import <ns>.x as A` / `from <ns>.x import y as A`; a module with
      `__getattr__` and an attr with 0 or >1 homes are skipped.
    * A comment riding a rewritten import line is dropped.

Exit: 0 nothing left (or all written under --apply) and nothing skipped · 1 rebinds pending
in a dry-run, or a bind skipped as ambiguous (in EVERY mode) · 2 unusable input.
"""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from codegraph._core import layout as _layout
from codegraph._core.verdict import Verdict
from hs_family.cli import Command

from ._port import PortContext, port_context
from .namerepair import Tree, _py_files, suite_roots


@dataclass(frozen=True)
class Binding:
    """One upstream binding of a local name."""
    kind: str            # module | name | def
    module: str          # dotted (old) module for import kinds, the file's own for a def
    orig: str            # imported symbol ("" for `import x as y`)
    local: bool          # bound inside a function body, not at module level


def _parse(path: Path) -> Optional[ast.Module]:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _defs(tree: ast.Module) -> Set[str]:
    out: Set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


def upstream_bindings(path: Path, old_module: str = "") -> Dict[str, List[Binding]]:
    """local name -> every way the upstream file binds it (import or def)."""
    t = _parse(path)
    out: Dict[str, List[Binding]] = {}
    if not t:
        return out

    def add(name: str, b: Binding) -> None:
        out.setdefault(name, []).append(b)

    def walk(node: ast.AST, local: bool) -> None:
        for ch in ast.iter_child_nodes(node):
            inner = local or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(ch, ast.ImportFrom) and ch.level == 0 and ch.module:
                for a in ch.names:
                    add(a.asname or a.name, Binding("name", ch.module, a.name, local))
            elif isinstance(ch, ast.Import):
                for a in ch.names:
                    add(a.asname or a.name.split(".")[0], Binding("module", a.name, "", local))
            elif not local and isinstance(ch, ast.Assign):
                for tg in ch.targets:
                    if isinstance(tg, ast.Name):
                        add(tg.id, Binding("def", old_module, tg.id, local))
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                add(ch.name, Binding("def", old_module, ch.name, local))
            walk(ch, inner)
    walk(t, False)
    return out


def twin_files(upstream: Path, port_file: Path) -> List[Path]:
    """Upstream files that ARE this file's ancestor: same basename, then the longest shared
    path suffix wins."""
    cands = [p for p in upstream.rglob(port_file.name)
             if ".git" not in p.parts and "node_modules" not in p.parts]
    if len(cands) <= 1:
        return cands

    def shared(p: Path) -> int:
        a, b = list(port_file.parts[::-1]), list(p.parts[::-1])
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        return n
    best = max(shared(p) for p in cands)
    return [p for p in cands if shared(p) == best]


def _stmt(mod: str, orig: str, local: str) -> str:
    if not orig:
        return "import %s as %s" % (mod, local)
    return "from %s import %s" % (mod, orig) + (" as %s" % local if orig != local else "")


class Ctx:
    """The facts `rebind` decides against, loaded once: both trees' modules, the target's
    definition index, the batch specs when a batch is named."""

    def __init__(self, pc: PortContext) -> None:
        from codegraph._core.snapshot.pipeline import import_context

        self.repo, self.upstream, self.package, self.ns = (
            pc.port_root, pc.upstream, pc.package, pc.namespace_root)
        self.descriptor = pc.descriptor
        self._tgt = import_context(self.repo, pc.descriptor)
        self._src = import_context(self.upstream, pc.source_descriptor())
        self.mods: Dict[str, Path] = {s.module: s.path for s in self._tgt.sources}
        self.names: Dict[str, Set[str]] = {}
        for mod, f in self.mods.items():
            t = _parse(f)
            for n in (_defs(t) if t else ()):
                self.names.setdefault(n, set()).add(mod)
        self.spec, self.multi = None, {}
        self.sources: Dict[str, List[Path]] = {}
        if pc.specs_dir is not None and pc.specs_dir.is_dir():
            from port_spec import merged_spec_and_unit_dist
            self.spec, _, _, self.multi, _ = merged_spec_and_unit_dist(pc.specs_dir, self.package)
            self.sources = self._upstream_sources(pc.specs_dir)
        self._twin: Dict[Path, Dict[str, List[Binding]]] = {}

    # -- the two trees ------------------------------------------------------------------
    def rel(self, f: Path) -> str:
        return f.resolve().relative_to(self.repo.resolve()).as_posix()

    def dist_of(self, f: Path) -> Optional[str]:
        return _layout.dist_of_path(self.descriptor, self.rel(f))

    def is_old_module(self, dotted: str) -> bool:
        """Was `dotted` a MODULE upstream (the source tree's own file decides)."""
        return dotted in self._src.all_mods

    def old_module_of(self, upstream_file: Path) -> str:
        try:
            rel = upstream_file.resolve().relative_to(self.upstream.resolve()).as_posix()
        except ValueError:
            return ""
        return self._src.module_at(rel) or ""

    def _upstream_sources(self, specs_dir: Path) -> Dict[str, List[Path]]:
        """port module -> upstream files it was cut from, straight from the batch specs
        (file/slice sources, and the old side of renames/name_moves rows) -- the authority on
        ancestry; a basename guess picks the wrong file when two share a name."""
        from port_spec import Spec
        out: Dict[str, List[Path]] = {}

        def add(new: str, old_mod: Optional[str] = None, rel: Optional[str] = None) -> None:
            f = (self.upstream / rel) if rel else (self._src.file_of(old_mod) if old_mod else None)
            if f is not None and f.is_file() and f not in out.setdefault(new, []):
                out[new].append(f)

        for sp in sorted(specs_dir.glob("*.yaml")):
            s = Spec.load(sp)
            for fe in s.file_sources:
                if fe.get("to"):
                    add(fe["to"], rel=fe.get("file"))
            for sl in s.slice_sources:
                if sl.get("to"):
                    add(sl["to"], old_mod=sl.get("module"))
            for table in (s.renames, s.name_moves):
                for k, v in table.items():
                    if "::" in str(k) and "::" in str(v):
                        add(str(v).split("::")[0], old_mod=str(k).split("::")[0])
        return out

    # -- old -> new -------------------------------------------------------------------------
    def _tail_guess(self, old: str, prefer_dist: Optional[str]) -> List[str]:
        """Old dotted module -> port candidates by longest unique tail (the old package
        stripped); a tie narrows to the preferred dist."""
        parts = old.split(".")
        if parts and parts[0] == self.package:
            parts = parts[1:]
        for k in range(len(parts), 0, -1):
            tail = ".".join(parts[-k:])
            hits = [m for m in self.mods if m == tail or m.endswith("." + tail)]
            if hits:
                if len(hits) > 1 and prefer_dist:
                    same = [m for m in hits if self.dist_of(self.mods[m]) == prefer_dist]
                    hits = same or hits
                return sorted(hits)
        return []

    def old_to_new(self, old: str, dist: Optional[str]) -> List[str]:
        """Old module -> port module(s): the batch map (exact / longest prefix), then the
        longest-unique-tail fallback."""
        if self.spec is not None:
            if old in self.multi:
                return [m for m in self.multi[old] if m in self.mods]
            mm = self.spec.module_map or {}
            if old in mm:
                return [mm[old]] if mm[old] in self.mods else []
            pre = [k for k in mm if old.startswith(k + ".")]
            if pre:
                k = max(pre, key=len)
                if mm[k] + old[len(k):] in self.mods:
                    return [mm[k] + old[len(k):]]
        return self._tail_guess(old, dist)

    def twin_bindings(self, f: Path) -> Dict[str, List[Binding]]:
        if f not in self._twin:
            mod = self._tgt.module_at(self.rel(f))
            files = self.sources.get(mod, []) if mod else []
            if not files:
                files = twin_files(self.upstream, f)
            merged: Dict[str, List[Binding]] = {}
            for t in files:
                for k, v in upstream_bindings(t, self.old_module_of(t)).items():
                    merged.setdefault(k, []).extend(v)
            self._twin[f] = merged
        return self._twin[f]

    def own(self, dotted: str) -> bool:
        return dotted == self.ns or dotted.startswith(self.ns + ".")


def _module_bind(b: Binding, c: Ctx) -> bool:
    return b.kind == "name" and c.is_old_module("%s.%s" % (b.module, b.orig))


def _expected(b: Binding, local: str, f: Path, c: Ctx) -> Optional[str]:
    """The statement that binds the MODULE upstream's `from <pkg> import <mod>` meant: the
    upstream module translated through the specs, kept only when it is a module of the tree."""
    subs = sorted({m for m in c.old_to_new("%s.%s" % (b.module, b.orig), c.dist_of(f))
                   if m in c.mods})
    if len(subs) != 1:
        return None
    parent, _, last = subs[0].rpartition(".")
    return _stmt(parent, last, local) if parent else _stmt(subs[0], "", local)


def plan_file(f: Path, c: Ctx) -> Tuple[Optional[str], List[dict], List[dict]]:
    text = f.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None, [], []
    tb = c.twin_bindings(f)
    if not tb:
        return None, [], []
    lines = text.splitlines(keepends=True)
    done: List[dict] = []
    skipped: List[dict] = []
    edits: List[Tuple[int, int, str]] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
                and n.module.startswith(c.ns + ".")):
            continue
        bad: List[Tuple[ast.alias, str]] = []
        for a in n.names:
            local = a.asname or a.name
            twins = tb.get(local, [])
            if not twins:
                continue
            is_mod = "%s.%s" % (n.module, a.name) in c.mods
            kinds = {_module_bind(b, c) for b in twins}
            if kinds == {False} and is_mod and all(b.kind == "name" for b in twins):
                # reverse flip: upstream bound a DEF (or constant), the port binds a module
                dist = c.dist_of(f)
                homes = {h for b in twins for h in c.old_to_new(b.module, dist)
                         if h in c.names.get(b.orig, ()) and b.orig == a.name}
                if len(homes) == 1:
                    bad.append((a, _stmt(next(iter(homes)), a.name, local)))
                continue
            if is_mod:
                continue
            if kinds != {True}:
                if True in kinds:
                    skipped.append({"file": c.rel(f), "name": local,
                                    "why": "twin bindings mix module and non-module"})
                continue
            stmts = {s for b in twins for s in [_expected(b, local, f, c)] if s}
            if len(stmts) == 1:
                bad.append((a, next(iter(stmts))))
        if not bad:
            continue
        keep = [a for a in n.names if all(a is not b for b, _ in bad)]
        ind = " " * n.col_offset
        out = ""
        if keep:
            out += "%sfrom %s import " % (ind, n.module) + ", ".join(
                a.name + (" as %s" % a.asname if a.asname else "") for a in keep) + "\n"
        out += "".join("%s%s\n" % (ind, s) for s in sorted({s for _, s in bad}))
        edits.append((n.lineno, n.end_lineno or n.lineno, out))
        for a, s in bad:
            done.append({"file": c.rel(f), "name": a.asname or a.name,
                         "was": "from %s import %s" % (n.module, a.name), "now": s})
    if not edits:
        return None, [], skipped
    for lo, hi, out in sorted(edits, reverse=True):
        lines[lo - 1:hi] = [out]
    new = "".join(lines)
    ast.parse(new)
    return new, done, skipped


def _twin_trees(f: Path, c: Ctx) -> List[ast.Module]:
    return [t for t in (_parse(p) for p in twin_files(c.upstream, f)) if t is not None]


def plan_strings(f: Path, c: Ctx, tree: Tree) -> Tuple[Optional[str], List[dict]]:
    """String targets (`monkeypatch.setattr("<ns>.m.attr", ...)`, `patch("<ns>.m.attr")`): a
    target whose module does not resolve `attr` is repointed to the module the UPSTREAM twin's
    own string of the same `attr` maps to, when exactly one such module resolves it."""
    text = f.read_text(encoding="utf-8")
    try:
        ast_ = ast.parse(text)
    except SyntaxError:
        return None, []
    up = {n.value.rpartition(".")[::2]: n.value for t in _twin_trees(f, c) for n in ast.walk(t)
          if isinstance(n, ast.Constant) and isinstance(n.value, str)
          and n.value.startswith(c.package + ".") and " " not in n.value}
    by_attr: Dict[str, Set[str]] = {}
    for (mod, attr) in up:
        by_attr.setdefault(attr, set()).add(mod)
    lines = text.splitlines(keepends=True)
    done: List[dict] = []
    dist = c.dist_of(f)
    for n in ast.walk(ast_):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value.startswith(c.ns + ".") and " " not in n.value
                and n.lineno == n.end_lineno):
            continue
        mod, _, attr = n.value.rpartition(".")
        if not mod or tree.binds(mod) is None or tree.resolves(mod, attr) or mod not in c.mods:
            continue
        olds = by_attr.get(attr, set())
        homes = {h for o in olds for h in c.old_to_new(o, dist)
                 if tree.binds(h) is not None and tree.resolves(h, attr)}
        if len(homes) != 1:
            continue
        new = "%s.%s" % (next(iter(homes)), attr)
        ln = lines[n.lineno - 1]
        seg = ln[n.col_offset:n.end_col_offset]
        lines[n.lineno - 1] = ln[:n.col_offset] + seg.replace(n.value, new, 1) + ln[n.end_col_offset:]
        done.append({"file": c.rel(f), "was": n.value, "now": new})
    return ("".join(lines) if done else None), done


_SETTERS = {"setattr", "delattr", "getattr", "hasattr"}


def _module_aliases(tree: ast.Module, tr: Tree, c: Ctx) -> List[Tuple[ast.stmt, str, str]]:
    """(import node, alias, dotted module) for every `import <ns>.x.y as A` /
    `from <ns>.x import y as A` that binds a MODULE of the tree."""
    out: List[Tuple[ast.stmt, str, str]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname and c.own(a.name) and (a.name in c.mods or tr.binds(a.name) is not None):
                    out.append((n, a.asname, a.name))
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and c.own(n.module or ""):
            for a in n.names:
                full = "%s.%s" % (n.module, a.name)
                if full in c.mods or (tr.binds(full) is not None
                                      and full not in c.names.get(a.name, ())):
                    out.append((n, a.asname or a.name, full))
    return out


def _home_for(tr: Tree, mod: str, attr: str) -> Optional[str]:
    """The ONE module (same package first) that binds `attr`, or None."""
    homes = sorted(h for h in tr.homes(attr, mod) if tr.binds(h) is not None)
    if len(homes) > 1:
        pkg = ".".join(mod.split(".")[:2])
        homes = [h for h in homes if h == pkg or h.startswith(pkg + ".")] or homes
    return homes[0] if len(homes) == 1 else None


def plan_attrs(f: Path, c: Ctx, tr: Tree) -> Tuple[Optional[str], List[dict], List[dict]]:
    """`A.attr` on a module alias whose module no longer binds `attr` (a cut moved it to a
    sibling): add `from <parent> import <home> as A_<home>` next to the alias' own import and
    point the uses in that scope at it. No re-export is added to the old door."""
    text = f.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None, [], []
    lines = text.splitlines(keepends=True)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    done: List[dict] = []
    ambiguous: List[dict] = []
    edits: List[Tuple[int, int, int, str]] = []       # (line, col, end_col, replacement)
    inserts: Dict[int, List[str]] = {}
    for node, alias, mod in _module_aliases(tree, tr, c):
        b = tr.binds(mod)
        if b is None or b[1]:
            continue
        scope = next((d for d in ast.walk(tree) if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and d.lineno <= node.lineno <= (d.end_lineno or d.lineno)
                      and d is not node), tree)
        uses: List[Tuple[ast.Name, str]] = []
        for u in ast.walk(scope):
            if isinstance(u, ast.Attribute) and isinstance(u.value, ast.Name) and u.value.id == alias:
                uses.append((u.value, u.attr))
            elif (isinstance(u, ast.Call) and len(u.args) >= 2 and isinstance(u.args[0], ast.Name)
                  and u.args[0].id == alias and isinstance(u.args[1], ast.Constant)
                  and isinstance(u.args[1].value, str)
                  and (getattr(u.func, "attr", getattr(u.func, "id", "")) in _SETTERS)):
                uses.append((u.args[0], u.args[1].value))
        by_home: Dict[str, List[ast.Name]] = {}
        for nm, attr in uses:
            if tr.resolves(mod, attr) or attr.startswith("__"):
                continue
            h = _home_for(tr, mod, attr)
            if h is None:
                ambiguous.append({"file": c.rel(f), "alias": alias, "attr": attr,
                                  "why": "no single home"})
                continue
            by_home.setdefault(h, []).append(nm)
            done.append({"file": c.rel(f), "was": "%s.%s" % (alias, attr),
                         "now": "%s.%s" % (h, attr)})
        for h, names in by_home.items():
            new = "%s_%s" % (alias, h.rsplit(".", 1)[-1])
            while new in used:
                new += "_"
            used.add(new)
            parent, _, last = h.rpartition(".")
            inserts.setdefault(node.end_lineno or node.lineno, []).append(
                "%sfrom %s import %s as %s\n" % (" " * node.col_offset, parent, last, new))
            edits += [(nm.lineno, nm.col_offset, nm.end_col_offset or nm.col_offset, new)
                      for nm in names]
    if not done:
        return None, [], ambiguous
    for ln, col, end, new in sorted(set(edits), reverse=True):
        row = lines[ln - 1]
        lines[ln - 1] = row[:col] + new + row[end:]
    for after in sorted(inserts, reverse=True):
        lines[after:after] = inserts[after]
    out = "".join(lines)
    ast.parse(out)
    return out, done, ambiguous


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--source", default=None, dest="source_id", metavar="ID",
                    help="the `.port/port.yaml` source the tests were cut from")
    ap.add_argument("--batch", default=None, dest="batch_name", metavar="B",
                    help="read the batch's specs (old -> new modules, and each module's ancestry)")
    ap.add_argument("--src", action="store_true",
                    help="also repair attribute uses in the product modules (the attr pass only: "
                         "a source file has no test twin)")


def run(args: argparse.Namespace, root: Path, apply: bool) -> Verdict:
    pc = port_context(root, "rebind", args.source_id, args.batch_name)
    c = Ctx(pc)
    tree = Tree(pc.port_root, pc.descriptor, set())
    done: List[dict] = []
    skipped: List[dict] = []
    strings: List[dict] = []
    attrs: List[dict] = []
    if args.src:
        for f in sorted(c.mods.values()):
            anew, ad, amb = plan_attrs(f, c, tree)
            attrs += ad
            skipped += amb
            if anew is not None and apply:
                f.write_text(anew, encoding="utf-8")
    tests = sorted({f for r in suite_roots(pc.port_root, pc.descriptor, []) for f in _py_files(r)})
    for f in tests:
        new, d, s = plan_file(f, c)
        skipped += s
        if new is not None:
            done += d
            if apply:
                f.write_text(new, encoding="utf-8")
        snew, sd = plan_strings(f, c, tree)
        if snew is not None:
            strings += sd
            if apply:
                f.write_text(snew, encoding="utf-8")
        anew, ad, amb = plan_attrs(f, c, tree)
        attrs += ad
        skipped += amb
        if anew is not None and apply:
            f.write_text(anew, encoding="utf-8")
    # a skipped (ambiguous) bind forces nonzero in EVERY mode: exit 0 over it is a false green
    ok = not skipped and (apply or not (done or strings or attrs))
    return Verdict.of(ok, {"schema": "hs_code.refactor.rebind/1", "rebound": done,
                           "strings": strings, "attrs": attrs, "skipped": skipped,
                           "dry_run": not apply})


def _rebind(args: argparse.Namespace) -> int:
    v = run(args, args.ctx.root, bool(getattr(args.opts, "apply", False)))
    d = v.data
    if getattr(args.opts, "json", False):
        print(json.dumps(d, sort_keys=True, ensure_ascii=False))
    else:
        print("rebind: %d import(s), %d string(s), %d attribute(s)%s, %d skipped" % (
            len(d["rebound"]), len(d["strings"]), len(d["attrs"]),
            "" if d["dry_run"] else " written", len(d["skipped"])))
        for r in d["rebound"] + d["strings"] + d["attrs"]:
            print("  %s: %s -> %s" % (r["file"], r["was"], r["now"]))
        for s in d["skipped"]:
            print("  skipped %s: %s" % (s["file"], s["why"]))
    return int(v.exit)


REBIND = Command(add_args=_args, run=_rebind)
