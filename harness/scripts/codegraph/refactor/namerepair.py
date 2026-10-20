"""hs-code refactor name-repair -- move an imported name to the module that defines it.

    from acme.alpha import helper, other   ->   from acme.alpha import other
                                                  from acme.beta.tools import helper

After a move the name a test imports can live in another module; the import breaks and
nothing but a collect run finds it. This verb rewrites those lines on the current tree.

Everything tree-shaped is read from the target's `codemap.yaml`, never assumed:
    * where a dotted module's file is -- `import_roots` (a `*` segment expands);
    * which imports are the project's own -- `namespace_root` when declared, else the
      top-level names of every module under the import roots; any other import passes;
    * which trees are scanned -- each `--tests DIR`, else `tests/` plus
      `<packages_dir>/*/<tests_subdir>` when the tree declares that layout.

A name is moved only when it does not resolve at the module it is imported from and
the definition index (top-level defs, classes and assignments of every module under the
import roots, scanned files excluded) gives it exactly ONE other home -- a submodule of
that name counts, and rewrites the parent package. Ambiguous and absent names are
listed, never guessed; `--pins FILE` (YAML `{"<from.module>.<name>" | "*.<name>": home}`)
is the owner's ruling for an ambiguous one. Dry-run by default; `--apply` writes.

WHAT IT CANNOT SEE
    * A name bound only by a re-export is no index home; a module's own imports count
      as resolving the names they bind.
    * A missing own module counts as unresolvable; a module built at runtime
      (`sys.modules[...] = ...`) would be misreported.
    * A module with `__getattr__` or a top-level star import resolves anything.
    * Names bound in a `for`/`while` body, by `global`, or via `globals()[...]` are not
      seen (`if`/`try`/`with` bodies and tuple targets are).
    * A comment on a name's line travels with the name; one that belongs to no name
      rides on the first statement written.
    * Plain `import a.b`, `monkeypatch.setattr("a.b...")` and `importlib` strings.

Exit: 0 nothing left to rewrite (or all written under --apply) and nothing unresolved ·
1 rewrites pending in a dry-run, or a name unresolved · 2 the request is malformed.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import tokenize
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import hs_family

from codegraph._core import walk
from codegraph._core.layout import test_home_of

from ._layout import _expand_root, load_descriptor
from .errors import RefactorError
from hs_family.cli import Command


def _py_files(base: Path) -> List[Path]:
    return sorted(p for p in base.rglob("*.py")
                  if not walk.skipped(p.relative_to(base).parts[:-1]))


def _toplevel(body: List[ast.stmt]) -> Iterator[ast.stmt]:
    """Module-level statements, looking through `if`/`try`/`with` blocks."""
    for n in body:
        yield n
        if isinstance(n, ast.If):
            yield from _toplevel(n.body + n.orelse)
        elif isinstance(n, ast.Try):
            yield from _toplevel(n.body + n.orelse + n.finalbody + [s for h in n.handlers for s in h.body])
        elif isinstance(n, ast.With):
            yield from _toplevel(n.body)


class Tree:
    """The definition index and module lookup over the declared import roots."""

    def __init__(self, repo: Path, descriptor, skip: Set[Path]) -> None:
        self.dirs = [d for e in descriptor.import_roots for d in _expand_root(repo, e)]
        self.idx: Dict[str, Set[str]] = {}
        mods: Set[str] = set()
        for d in self.dirs:
            for f in _py_files(d):
                if f.resolve() in skip:
                    continue
                mod = ".".join(f.relative_to(d).with_suffix("").parts).removesuffix(".__init__")
                if mod == "__init__":
                    continue
                mods.add(mod)
                try:
                    body = ast.parse(f.read_text(encoding="utf-8")).body
                except (OSError, SyntaxError, UnicodeDecodeError):
                    continue
                for n in body:
                    name = None
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        name = n.name
                    elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                        name = n.targets[0].id
                    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                        name = n.target.id
                    if name:
                        self.idx.setdefault(name, set()).add(mod)
                # a from-import may name a SUBMODULE: index every dotted suffix of it
                parts = mod.split(".")
                for i in range(1, len(parts)):
                    self.idx.setdefault(".".join(parts[i:]), set()).add(mod)
        ns = descriptor.namespace_root
        self.own: Set[str] = {ns} if ns else {m.split(".")[0] for m in mods}
        self._binds: Dict[str, Optional[Tuple[Set[str], bool]]] = {}

    def is_own(self, mod: str) -> bool:
        return mod.split(".")[0] in self.own

    def mod_file(self, mod: str) -> Tuple[Optional[Path], bool]:
        """(file, is a package) for a dotted module; (None, True) for a namespace package."""
        rel = mod.replace(".", "/")
        for d in self.dirs:
            if (d / (rel + ".py")).is_file():
                return d / (rel + ".py"), False
        for d in self.dirs:
            if (d / rel / "__init__.py").is_file():
                return d / rel / "__init__.py", True
        for d in self.dirs:
            if (d / rel).is_dir():
                return None, True
        return None, False

    def binds(self, mod: str) -> Optional[Tuple[Set[str], bool]]:
        """(names bound at top level, opaque) for a module; None = no such module."""
        if mod in self._binds:
            return self._binds[mod]
        f, is_pkg = self.mod_file(mod)
        out: Optional[Tuple[Set[str], bool]] = (set(), False) if is_pkg else None
        if f is not None:
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                tree = None
            if tree is not None:
                names: Set[str] = set()
                opaque = False
                for n in _toplevel(tree.body):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(n.name)
                        opaque |= n.name == "__getattr__"
                    elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                        names.update(x.id for t in tg for x in ast.walk(t)
                                     if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store))
                    elif isinstance(n, ast.Import):
                        names.update((a.asname or a.name).split(".")[0] for a in n.names)
                    elif isinstance(n, ast.ImportFrom):
                        for a in n.names:
                            opaque |= a.name == "*"
                            names.add(a.asname or a.name)
                out = (names, opaque)
        self._binds[mod] = out
        return out

    def resolves(self, mod: str, name: str) -> bool:
        b = self.binds(mod)
        if b is None:
            return not self.is_own(mod)          # a missing own module is a broken import
        names, opaque = b
        f, is_pkg = self.mod_file("%s.%s" % (mod, name))
        return opaque or name in names or f is not None or is_pkg

    def homes(self, name: str, at: str) -> Set[str]:
        out: Set[str] = set()
        for h in self.idx.get(name, ()):
            if not self.is_own(h):
                continue
            tgt = h.rsplit(".", 1)[0] if h.endswith("." + name) or h == name else h
            if tgt != at and self.resolves(tgt, name):
                out.add(tgt)
        return out


def _fmt(indent: str, mod: str, items: List[Tuple[str, str]], opener: str = "") -> str:
    """`items` = [(text, trailing comment)]; `opener` rides on the `(` line."""
    one = "%sfrom %s import %s" % (indent, mod, ", ".join(i for i, _ in items))
    if len(one) <= 100 and not opener and not any(c for _, c in items):
        return one + "\n"
    body = "".join("%s    %s,%s\n" % (indent, i, ("  " + c) if c else "") for i, c in items)
    return "%sfrom %s import (%s\n%s%s)\n" % (indent, mod, ("  " + opener) if opener else "", body, indent)


def _comments(seg: str, first: int) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for tok in tokenize.generate_tokens(io.StringIO(seg).readline):
        if tok.type == tokenize.COMMENT:
            out[first + tok.start[0] - 1] = tok.string
    return out


def repair_source(src: str, rel: str, tree: Tree,
                  pins: Dict[str, str]) -> Tuple[Optional[str], List[dict], List[dict]]:
    """(new text or None, rewrites, unresolved). Never writes."""
    try:
        mod_ast = ast.parse(src)
    except SyntaxError:
        return None, [], []
    lines = src.splitlines(keepends=True)
    edits: List[Tuple[int, int, str]] = []
    rewrites: List[dict] = []
    unresolved: List[dict] = []
    for n in ast.walk(mod_ast):
        if not (isinstance(n, ast.ImportFrom) and n.level == 0 and n.module and tree.is_own(n.module)):
            continue
        end = n.end_lineno or n.lineno
        seg = "".join(lines[n.lineno - 1:end])
        cm = _comments(seg, n.lineno) if "#" in seg else {}
        keep: List[Tuple[str, str]] = []
        moved: Dict[str, List[Tuple[str, str]]] = {}
        pend: List[dict] = []
        for a in n.names:
            text = a.name + (" as %s" % a.asname if a.asname else "")
            cmt = cm.pop(getattr(a, "end_lineno", None) or a.lineno, "")
            if a.name == "*" or tree.resolves(n.module, a.name):
                keep.append((text, cmt))
                continue
            pin = pins.get("%s.%s" % (n.module, a.name)) or pins.get("*.%s" % a.name)
            hs = {pin} if pin else tree.homes(a.name, n.module)
            rec = {"file": rel, "line": n.lineno, "from": n.module, "name": a.name}
            if len(hs) == 1:
                home = next(iter(hs))
                moved.setdefault(home, []).append((text, cmt))
                pend.append(dict(rec, to=home))
            else:
                unresolved.append(dict(rec, candidates=sorted(hs), why="ambiguous" if hs else "absent"))
                keep.append((text, cmt))
        if not moved:
            continue
        rewrites += pend
        indent = " " * n.col_offset
        opener = "  ".join(cm[k] for k in sorted(cm))
        out = ""
        if keep:
            out, opener = _fmt(indent, n.module, keep, opener), ""
        for home in sorted(moved):
            out, opener = out + _fmt(indent, home, moved[home], opener), ""
        edits.append((n.lineno - 1, end, out))
    if not edits:
        return None, rewrites, unresolved
    for s, e, out in sorted(edits, reverse=True):
        lines[s:e] = [out]
    return "".join(lines), rewrites, unresolved


def suite_roots(repo: Path, descriptor, given: List[str]) -> List[Path]:
    if given:
        roots = [repo / g for g in given]
        missing = [g for g, r in zip(given, roots) if not r.is_dir()]
        if missing:
            raise RefactorError("--tests %s: not a directory under %s" % (", ".join(missing), repo))
        return roots
    flat_home = repo / test_home_of(descriptor)
    roots = [flat_home] if flat_home.is_dir() else []
    if descriptor.packages_dir:
        for dist_dir in sorted((repo / descriptor.packages_dir).glob("*")):
            if not dist_dir.is_dir():
                continue
            home = repo / test_home_of(descriptor, dist_dir.name)
            if home.is_dir():
                roots.append(home)
    return roots


def _pins(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    import yaml_io
    try:
        doc = yaml_io.yaml_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml_io.YAMLError) as e:
        raise RefactorError("--pins %s: %s" % (path, " ".join(str(e).split()))) from None
    if not isinstance(doc, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in doc.items()):
        raise RefactorError("--pins %s: must map `<from.module>.<name>` or `*.<name>` to a home module" % path)
    return dict(doc)


def plan(repo: Path, tests: List[str], pins_file: Optional[str]) -> Tuple[Dict[Path, str], List[dict], List[dict]]:
    """({file: new text}, rewrites, unresolved) for the scanned test trees."""
    descriptor = load_descriptor(repo, "refactor name-repair")
    if not descriptor.import_roots:
        raise RefactorError("codemap.yaml declares no `import_roots` -- name-repair cannot find a "
                            "module's file without one real sys.path entry declared")
    files = [f for r in suite_roots(repo, descriptor, tests) for f in _py_files(r)]
    tree = Tree(repo, descriptor, {f.resolve() for f in files})
    pins = _pins(pins_file)
    texts: Dict[Path, str] = {}
    rewrites: List[dict] = []
    unresolved: List[dict] = []
    for f in files:
        new, rw, un = repair_source(f.read_text(encoding="utf-8"), f.relative_to(repo).as_posix(), tree, pins)
        rewrites += rw
        unresolved += un
        if new is not None:
            texts[f] = new
    return texts, rewrites, unresolved


def _name_repair_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--tests", action="append", default=[], metavar="DIR",
                    help="repo-relative tree to scan (repeatable; default tests/ plus the declared "
                         "<packages_dir>/*/<tests_subdir>)")
    ap.add_argument("--pins", default=None, metavar="FILE",
                    help="YAML {'<from.module>.<name>' | '*.<name>': home}: the owner's ruling "
                         "for an ambiguous name")


def _name_repair(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    apply = bool(getattr(opts, "apply", False))
    repo = Path(root).resolve()
    try:
        texts, rewrites, unresolved = plan(repo, args.tests, args.pins)
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    if apply:
        for f, text in texts.items():
            f.write_text(text, encoding="utf-8")
    ok = not unresolved and (apply or not rewrites)
    data = {"schema": "hs-code.refactor.name-repair/1", "applied": apply and bool(texts),
            "rewrites": rewrites, "unresolved": unresolved, "ok": ok}
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return 0 if ok else 1
    print("hs-code refactor name-repair: %s %d, unresolved %d"
          % ("applied" if apply else "would rewrite", len(rewrites), len(unresolved)))
    for r in rewrites:
        print("  %s:%d  %s.%s -> %s" % (r["file"], r["line"], r["from"], r["name"], r["to"]))
    for u in unresolved:
        print("  RED %s  %s:%d  %s.%s%s" % (u["why"], u["file"], u["line"], u["from"], u["name"],
                                           "  candidates=%s" % u["candidates"] if u["candidates"] else ""))
    if rewrites and not apply:
        print("hs-code refactor name-repair: dry-run -- pass --apply to write")
    return 0 if ok else 1


NAME_REPAIR = Command(add_args=_name_repair_args, run=_name_repair)
