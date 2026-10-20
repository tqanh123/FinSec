"""hs-code refactor carve -- cut named top-level blocks out of one module into siblings.

    hs-code refactor carve SRC --cut MODULE=NAME,NAME [--cut MODULE=A-B] [--share NAME]
                               [--keep-import NAME] [--repoint DIR] [--why PHRASE]

A file past its length seam is answered by a cut. `slice` writes two loose halves and
leaves the facade to hand work; `carve` does the four chores every cut repeats: move the
blocks, give each piece the imports it uses, keep the OLD import path alive as a
re-export, keep the logger name.

    src.py --cut sub=name,name  ->  sub.py   the blocks + the import header they use
                                    src.py   the rest + `from <pkg>.sub import (...)`
                                             + imports pruned to what the rest still uses

The module's dotted name -- what the re-export and a shared logger spell -- is
`layout.dotted_of` over the tree's declared `import_roots`; a module directly on an
import root (no package) re-exports with `from sub import ...`. A module the layout
cannot name is refused, never guessed.

Refused, nothing written (exit 1): a cut block that borrows a name the facade keeps (a
back-edge: the facade imports the cut module, so it is a cycle) unless that name is
`--share`d (a simple assignment, copied); an unknown name; two cuts claiming one block;
output that does not parse or reads an undefined name. `--share logger` rewrites
`getLogger(__name__)` to `getLogger("<old dotted name>")`, so the old logger name holds.
`--repoint DIR` also rewrites importers under DIR (`repoint.py`). Dry-run by default;
`--apply` writes.

WHAT IT CANNOT SEE
    * Without `ruff` the undefined-name check falls back to `slice`'s AST check, which
      does not model comprehension scopes (false reds, never false greens).
    * Names read through `getattr`/strings/`globals()`: a back-edge behind a string is
      invisible (pair with `check f821` and the tests).
    * Top-level statements other than def/class/assign stay in the facade; a cut block
      using a name they bind is reported as a back-edge, their side effects are not.
    * A caller's attribute access to an import the facade no longer needs
      (`mod.some_import`): pruned imports are listed; `--keep-import NAME` keeps one.
    * A shared constant is duplicated (two copies can drift): each is listed.
    * A comment not glued (no blank line) directly above a cut block stays behind.

Exit: 0 planned (written under --apply) · 1 refused, nothing written · 2 malformed request.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import hs_family
from codegraph._core.layout import dotted_of
from codegraph.check.f821 import find_ruff
from codegraph.check.tree import CheckError

from .errors import RefactorError
from .repoint import repoint_tree
from .slice import _collect_imports, _is_future, _unparse_import, build_import_header, check_f821
from hs_family.cli import Command

_LINES = re.compile(r"^(\d+)-(\d+)$")
_SHAREABLE = (ast.Constant, ast.Call, ast.Attribute, ast.Name)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)
Block = Tuple[str, int, int, ast.stmt]


def _defined(node: ast.stmt) -> List[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]  # type: ignore[attr-defined]
    return [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]


def _lead(start: int, lines: List[str]) -> int:
    """First line of the comment run glued (no blank) above `start` (1-based)."""
    s = start
    while s > 1 and lines[s - 2].strip().startswith("#"):
        s -= 1
    return s


def _blocks(tree: ast.Module) -> List[Block]:
    """[(name, first line incl. decorators, last line, node)]; a tuple assignment gives
    one entry per name, same node."""
    out: List[Block] = []
    for n in tree.body:
        if not isinstance(n, _DEFS):
            continue
        decos = getattr(n, "decorator_list", None)
        start = decos[0].lineno if decos else n.lineno
        out += [(nm, start, n.end_lineno or n.lineno, n) for nm in _defined(n)]
    return out


def _resolve_cuts(cuts: List[str], tree: ast.Module, lines: List[str]) -> Tuple[Dict[str, List[Block]], List[str]]:
    """({module: blocks}, errors). `NAME,NAME` or `A-B` (source lines); one node is cut
    once even when several of its names are listed."""
    blocks = _blocks(tree)
    by_name = {b[0]: b for b in blocks}
    out: Dict[str, List[Block]] = {}
    errs: List[str] = []
    claimed: Dict[int, str] = {}
    for spec in cuts:
        mod, _, what = spec.partition("=")
        mod = mod.strip()
        if not mod.isidentifier() or not what:
            errs.append("bad --cut %r: expected MODULE=NAME,NAME or MODULE=A-B" % spec)
            continue
        m = _LINES.match(what.strip())
        if m:
            a, b = int(m[1]), int(m[2])
            picked = [n for (n, s, e, _) in blocks if a <= s and e <= b]
        else:
            picked = [x.strip() for x in what.split(",") if x.strip()]
        for n in picked:
            if n not in by_name:
                errs.append("--cut %s: no top-level def/assign named %r" % (mod, n))
                continue
            _, s, e, node = by_name[n]
            if id(node) in claimed:
                if claimed[id(node)] != mod:
                    errs.append("block %r claimed by both %r and %r" % (n, claimed[id(node)], mod))
                continue
            claimed[id(node)] = mod
            out.setdefault(mod, []).append((n, _lead(s, lines), e, node))
    return out, errs


def _shared_text(name: str, tree: ast.Module, lines: List[str], dotted: str) -> Optional[str]:
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and name in _defined(n):
            val = n.value
            if val is None or not isinstance(val, _SHAREABLE):
                return None
            if isinstance(val, ast.Call) and ast.unparse(val.func).endswith("getLogger"):
                return '%s = %s("%s")' % (name, ast.unparse(val.func), dotted)
            return "".join(lines[n.lineno - 1:(n.end_lineno or n.lineno)]).rstrip("\n")
    return None


def _drop_lines(lines: List[str], ranges: List[Tuple[int, int]]) -> str:
    dead = {i for s, e in ranges for i in range(s, e + 1)}
    txt = "".join(ln for i, ln in enumerate(lines, 1) if i not in dead)
    return re.sub(r"\n{4,}", "\n\n\n", txt)


def _prune_imports(text: str, keep: Set[str]) -> Tuple[str, List[str]]:
    """Drop import aliases the body no longer uses (a `keep` name always stays)."""
    tree = ast.parse(text)
    body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    used = {n.id for b in body for n in ast.walk(b) if isinstance(n, ast.Name)} | keep
    lines = text.splitlines(keepends=True)
    edits: List[Tuple[int, int, str]] = []
    pruned: List[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or _is_future(node):
            continue
        alive = []
        for a in node.names:
            bound = a.asname or a.name.split(".")[0]
            if a.name == "*" or bound in used:
                alive.append(a)
            else:
                pruned.append(bound)
        if len(alive) == len(node.names):
            continue
        node.names = alive
        edits.append((node.lineno, node.end_lineno or node.lineno, _unparse_import(node) + "\n" if alive else ""))
    for s, e, rep in sorted(edits, reverse=True):
        lines[s - 1:e] = [rep] if rep else []
    return "".join(lines), pruned


def _undefined(text: str, label: str, repo: Optional[Path] = None, ruff: Optional[str] = None) -> List[str]:
    """Undefined names: ruff F821 through `check.f821.find_ruff` (the same discovery a
    target repo's own gate uses, its `.venv/bin/ruff` included) when one is on hand, else
    `slice`'s own AST check. `--isolated` means a `pyproject.toml`/`ruff.toml` sitting at
    the CALLER's CWD -- possibly mid-edit, possibly malformed, never this cut's own
    business -- can no longer change what this check sees; a ruff exit outside `(0, 1)` is
    now a refusal (`RefactorError`, exit 2), never a silently swallowed empty list. Measured
    bug this closes: a broken CWD `pyproject.toml` made ruff exit 2 with no stdout, which
    the old code read as "no F821 findings" -- a false "compiles cleanly" for code that
    plainly does not."""
    try:
        exe = find_ruff(repo or Path.cwd(), ruff)
    except CheckError:
        return check_f821(text, label)
    r = subprocess.run([exe, "check", "--select", "F821", "--no-cache", "--isolated",
                        "--output-format", "concise", "--stdin-filename", label, "-"],
                       input=text, capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise RefactorError("ruff failed checking %s: %s"
                            % (label, (r.stderr or r.stdout).strip()[:200]))
    return [ln for ln in r.stdout.splitlines() if "F821" in ln]


def package_of(dotted: str, is_init: bool) -> str:
    """The package a cut module joins: the module's parent, or the package itself when the
    source is its `__init__.py` (`acme.core` -> `acme.core.sub`, not `acme.sub`)."""
    if is_init:
        return dotted
    return dotted.rsplit(".", 1)[0] if "." in dotted else ""


def plan_carve(src: Path, dotted: str, cuts: List[str], share: List[str], keep_import: List[str],
               why: str, repo: Optional[Path] = None,
               ruff: Optional[str] = None) -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
    """({file name: text}, report), or (None, report with errors). Never writes.
    `repo`/`ruff` pass straight to `_undefined`'s own ruff discovery -- `repo` defaults to
    `src`'s own directory when the caller has no better repo root on hand."""
    text = src.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text, filename=str(src))
    pkg = package_of(dotted, src.name == "__init__.py")
    report: Dict[str, Any] = {"modules": {}, "shared": [], "pruned": [], "errors": [], "notes": []}
    groups, errs = _resolve_cuts(cuts, tree, lines)
    report["errors"] += errs
    if report["errors"] or not groups:
        if not groups and not errs:
            report["errors"].append("no --cut given")
        return None, report

    home = (lambda mod: "%s.%s" % (pkg, mod)) if pkg else (lambda mod: mod)
    imports = _collect_imports(tree)
    cut_nodes = {id(x[3]) for g in groups.values() for x in g}
    kept_defs = {nm for n in tree.body if id(n) not in cut_nodes and isinstance(n, _DEFS) for nm in _defined(n)}
    cut_defs = {nm for g in groups.values() for x in g for nm in _defined(x[3])}
    files: Dict[str, str] = {}
    ranges: List[Tuple[int, int]] = []
    reexports: Dict[str, List[str]] = {}
    for mod, items in groups.items():
        items.sort(key=lambda x: x[1])
        nodes = [x[3] for x in items]
        used = {n.id for nd in nodes for n in ast.walk(nd) if isinstance(n, ast.Name)}
        own = {nm for nd in nodes for nm in _defined(nd)}
        for nm in sorted((used & kept_defs) - own - set(share)):
            report["errors"].append("%s: block borrows %r which the facade keeps -- a back-edge (cycle); "
                                    "cut it too, or pass --share %s" % (mod, nm, nm))
        body = "\n\n".join("".join(lines[s - 1:e]).rstrip("\n") + "\n" for _, s, e, _ in items)
        shared = []
        for nm in share:
            if nm in used and nm in kept_defs:
                st = _shared_text(nm, tree, lines, dotted)
                if st is None:
                    report["errors"].append("--share %s: not a simple assignment" % nm)
                else:
                    shared.append(st)
                    report["shared"].append("%s:%s" % (mod, nm))
        head = build_import_header(imports, body + "\n".join(shared))
        cross = ""
        for om, oitems in groups.items():
            names = sorted(((used & cut_defs) - own) & {nm for x in oitems for nm in _defined(x[3])})
            if om != mod and names:
                cross += "from %s import %s\n" % (home(om), ", ".join(names))
                report["notes"].append("%s imports from sibling cut %s: %s" % (mod, om, names))
        doc = ('"""Carved out of `%s` (%s). The old import path `%s` re-exports every name here."""\n'
               % (src.stem, why, dotted))
        parts = [doc, head, cross, "\n".join(shared) + ("\n" if shared else "")]
        # the header builder matches names as words (comments count): prune by the AST after
        files[mod + ".py"], _ = _prune_imports("".join(p for p in parts if p).rstrip("\n") + "\n\n\n" + body, set())
        exported = sorted(own)
        reexports[mod] = exported
        ranges += [(s, e) for _, s, e, _ in items]
        report["modules"][mod] = {"names": exported, "lines": files[mod + ".py"].count("\n")}
    if report["errors"]:
        return None, report

    rest_text = _drop_lines(lines, ranges)
    last_imp = max((n.end_lineno or n.lineno for n in ast.parse(rest_text).body
                    if isinstance(n, (ast.Import, ast.ImportFrom))), default=0)
    block = "".join("from %s import (  # noqa: F401  re-export: carved out of `%s`\n%s)\n"
                    % (home(mod), src.stem, "".join("    %s,\n" % n for n in names))
                    for mod, names in reexports.items())
    rlines = rest_text.splitlines(keepends=True)
    rlines[last_imp:last_imp] = ["\n" + block]
    keep = set(keep_import) | {n for names in reexports.values() for n in names}
    facade, report["pruned"] = _prune_imports("".join(rlines), keep)
    files[src.name] = facade
    for name, out in files.items():
        try:
            ast.parse(out)
        except SyntaxError as e:
            report["errors"].append("%s: does not parse: %s" % (name, e))
            continue
        report["errors"] += _undefined(out, name, repo=repo or src.parent, ruff=ruff)
    report["facade_lines"] = facade.count("\n")
    return (None if report["errors"] else files), report


def _import_roots(repo: Path) -> List[str]:
    from ._layout import load_descriptor
    return list(load_descriptor(repo, "refactor carve").import_roots)


def _carve_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("src", help="the module to cut (repo-relative)")
    ap.add_argument("--cut", action="append", required=True, metavar="MODULE=NAMES|A-B",
                    help="sibling module = comma-separated top-level names, or a source line range A-B")
    ap.add_argument("--share", action="append", default=[], metavar="NAME",
                    help="copy this simple assignment into every cut module that reads it "
                         "(`logger` keeps the OLD logger name)")
    ap.add_argument("--keep-import", action="append", default=[], metavar="NAME",
                    help="never prune this import from the facade (a test or monkeypatch reads it)")
    ap.add_argument("--repoint", action="append", default=[], metavar="DIR",
                    help="also rewrite importers under DIR (repo-relative, repeatable) to each "
                         "moved name's new module")
    ap.add_argument("--why", default="length seam", help="one phrase for the cut module's docstring")


def _carve(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    apply = bool(getattr(opts, "apply", False))
    repo = Path(root).resolve()
    src = (repo / args.src).resolve()
    try:
        if not src.is_file():
            raise RefactorError("no such file: %s" % args.src)
        rel = src.relative_to(repo).as_posix()
        roots = _import_roots(repo)
        dotted = dotted_of(rel, roots)
        if dotted is None:
            raise RefactorError("%s sits under no declared import_roots (%s): its module name, which the "
                                "re-export spells, is unknown" % (rel, ", ".join(roots) or "none"))
        files, rep = plan_carve(src, dotted, args.cut, args.share, args.keep_import, args.why, repo=repo)
    except SyntaxError as e:
        raise hs_family.UsageError("%s does not parse: %s" % (args.src, e)) from e
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    data: Dict[str, Any] = dict(rep, schema="hs-code.refactor.carve/1", src=rel, dotted=dotted,
                                dry_run=not apply, repointed={}, written=[])
    importers: Dict[Path, Tuple[str, int]] = {}
    if files is not None and args.repoint:
        owner = {nm: mod for mod, m in rep["modules"].items() for nm in m["names"]}
        skip = {(src.parent / n).resolve() for n in files}
        importers = repoint_tree(repo, [repo / d for d in args.repoint], skip, dotted, owner, roots,
                                 package_of(dotted, src.name == "__init__.py"))
        data["repointed"] = {f.resolve().relative_to(repo).as_posix(): n for f, (_t, n) in importers.items()}
    if files is not None and apply:
        for name, body in files.items():
            (src.parent / name).write_text(body, encoding="utf-8")
            data["written"].append((src.parent / name).relative_to(repo).as_posix())
        for f, (body, _n) in importers.items():
            f.write_text(body, encoding="utf-8")
            data["written"].append(f.resolve().relative_to(repo).as_posix())
    rc = 1 if files is None else 0
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return rc
    print("hs-code refactor carve %s (%s)" % (rel, dotted))
    for err in data["errors"]:
        print("  REFUSED %s" % err)
    for mod, m in data["modules"].items():
        print("  %s.py <- %d name(s), %d line(s)" % (mod, len(m["names"]), m["lines"]))
    if data["shared"]:
        print("  shared (duplicated): %s" % ", ".join(data["shared"]))
    if data["pruned"]:
        print("  facade imports pruned: %s" % ", ".join(data["pruned"]))
    for f, n in sorted(data["repointed"].items()):
        print("  repoint %s  (%d import statement(s))" % (f, n))
    if rc:
        print("hs-code refactor carve: REFUSED -- nothing written")
    elif apply:
        print("hs-code refactor carve: applied -- %d file(s) written" % len(data["written"]))
    else:
        print("hs-code refactor carve: dry-run -- facade %d line(s); pass --apply to write" % data["facade_lines"])
    return rc


CARVE = Command(add_args=_carve_args, run=_carve)
