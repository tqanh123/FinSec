"""hs-code refactor slice-audit -- diff every ported module against its
`.orig` twin.

Port of ob-tools' `portkit.gates.slice_audit` + `slice_audit_ruled` (pinned
commit `60075cb`). The slice-loss family: the port drops a top-level symbol or
an imported name, the file still PARSES, imports still LIVE, and the loss only
surfaces as a runtime NameError in a test far away. This verb finds it at port
time by diffing the pair the slice leaves behind: `<module>.py` next to
`<module>.py.orig`.

WHERE IT LOOKS
    Upstream globbed `packages/*/src/**/*.py.orig` only. Here the pairs are
    found under every directory the target repo's `codemap.yaml` declares in
    `import_roots` (globs expanded), or under the whole repo when it declares
    none -- a flat tree audits like a workspace one.

A name counts as PRESENT when the port file binds it at top level OR an
`__init__.py` up its package chain (stopping at the import root it was found
under) re-exports it (from-import bind, plain import bind, or `__all__`
member) -- a deliberate re-home through the package door is not a loss.

`--ledger FILE` (a port ledger, read through `port_ledger_writer.Ledger`):
a lost name whose node is currently `dropped` with a non-blank `reason` is
RULED, not red. A node key is `<anchor>::<dotted.module>::<Qualified.name>`;
it matches when the module's last component equals the port file's stem (the
package name for an `__init__.py`) and the qualified name's first component
is the lost name. `new@...` nodes are ignored.

Exit: 0 no loss and no skipped pair · 1 a loss, or an `.orig` with no port
twin · 2 cannot run (bad codemap.yaml, unreadable ledger).

WHAT IT CANNOT SEE
    * Modules without a `.orig` twin -- handbuilt modules and later hand
      rewrites are out of scope by construction; they are COUNTED, and a
      clean run over a mostly-handbuilt tree proves nothing about those.
    * Losses below top level -- a method dropped from a class, a branch from
      a function -- and decorator deltas (a def that survived but lost
      `@dataclass(frozen=True)` is INVISIBLE here; the name still binds).
    * A `.orig` that is itself stale: the diff is against the LOCAL twin,
      not the upstream commit -- that comparison belongs to snapshot-diff.
    * Ledger rulings match by module STEM + name, not the full dotted path:
      over-forgiving on stem collisions, and a lost IMPORT is never a ledger
      node, so it can never be ruled.
    * Conditional binds ARE seen (bodies of top-level `if`/`try`/`with`) but
      flattened: a name bound in only ONE branch reads as present.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import hs_family
from codegraph._core import walk

from ._layout import _expand_root, optional_descriptor
from .errors import RefactorError
from hs_family.cli import Command



def _module_level(body: List[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement that binds at MODULE scope: the body itself plus the
    bodies of top-level `if`/`try`/`with`. Never descends into def/class."""
    for n in body:
        yield n
        if isinstance(n, ast.If):
            yield from _module_level(n.body)
            yield from _module_level(n.orelse)
        elif isinstance(n, ast.Try):
            yield from _module_level(n.body)
            for h in n.handlers:
                yield from _module_level(h.body)
            yield from _module_level(n.orelse)
            yield from _module_level(n.finalbody)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            yield from _module_level(n.body)


def _top_names(tree: ast.Module) -> Dict[str, Tuple[str, int]]:
    """Top-level bind name -> (kind, lineno). First bind wins."""
    out: Dict[str, Tuple[str, int]] = {}
    for n in _module_level(tree.body):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, ("def", n.lineno))
        elif isinstance(n, ast.ClassDef):
            out.setdefault(n.name, ("class", n.lineno))
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, ("assign", n.lineno))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.setdefault(n.target.id, ("assign", n.lineno))
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.setdefault(a.asname or a.name.split(".")[0], ("import", n.lineno))
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.setdefault(a.asname or a.name, ("import", n.lineno))
    return out


def _reexported(name: str, module_file: Path, stop: Path) -> bool:
    """True when an `__init__.py` up the module's package chain, no higher
    than `stop`, SHIPS `name`. An unreadable init never saves a name."""
    pkg = module_file.parent
    while True:
        init = pkg / "__init__.py"
        if init.is_file():
            try:
                tree = ast.parse(init.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                return False
            for n in _module_level(tree.body):
                if isinstance(n, ast.ImportFrom):
                    if any((a.asname or a.name) == name for a in n.names):
                        return True
                elif isinstance(n, ast.Import):
                    if any((a.asname or a.name.split(".")[0]) == name for a in n.names):
                        return True
                elif (isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
                      and isinstance(n.value, (ast.List, ast.Tuple))):
                    if any(isinstance(e, ast.Constant) and e.value == name for e in n.value.elts):
                        return True
        if pkg == stop or pkg == pkg.parent:
            return False
        pkg = pkg.parent


def port_stem(module_rel: str) -> str:
    """`a/b/y.py` -> `y`; `a/pkg/__init__.py` -> `pkg`."""
    p = Path(module_rel)
    return p.parent.name if p.stem == "__init__" else p.stem


def ruled_drops(ledger_path: Path) -> Dict[Tuple[str, str], str]:
    """{(module stem, first name component): reason} for every currently
    `dropped` old-tree node that carries a reason."""
    from port_ledger_writer import BrokenLedgerError, Ledger
    # the reader takes a missing file for an empty ledger; a --ledger the caller
    # named that is not there is a typo, not a ledger that rules nothing
    if not ledger_path.is_file():
        raise RefactorError("slice-audit: cannot read ledger %s: no such file" % ledger_path)
    try:
        led = Ledger.load_checked(ledger_path)
    except (OSError, BrokenLedgerError) as e:
        raise RefactorError("slice-audit: cannot read ledger %s: %s" % (ledger_path, e)) from None
    out: Dict[Tuple[str, str], str] = {}
    for node, ev in led.replay().items():
        reason = str(ev.extra.get("reason") or "").strip()
        if ev.status != "dropped" or not reason:
            continue
        parts = node.split("::")
        if len(parts) != 3 or parts[0].startswith("new@"):
            continue
        out[(parts[1].rsplit(".", 1)[-1], parts[2].split(".")[0])] = reason
    return out


def scan_roots(repo: Path) -> List[Path]:
    """The declared import roots, or the repo itself when none is declared."""
    descriptor = optional_descriptor(repo)
    if descriptor is None:
        return [repo]
    roots: List[Path] = []
    for entry in descriptor.import_roots or ():
        roots += [d for d in _expand_root(repo, entry) if d not in roots]
    return roots or [repo]


def _origs(root: Path) -> Iterator[Path]:
    for orig in sorted(root.rglob("*.py.orig")):
        if not walk.skipped(orig.relative_to(root).parts):
            yield orig


def audit(repo: Path, ledger: Optional[Path] = None) -> Dict[str, Any]:
    """Every `<x>.py` + `<x>.py.orig` pair under the scan roots, the names the
    port lost, re-export aware."""
    pairs: List[str] = []
    skipped: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []
    ruled: List[Dict[str, Any]] = []
    drops = ruled_drops(ledger) if ledger is not None else {}
    seen: set = set()
    roots = scan_roots(repo)
    for root in roots:
        for orig in _origs(root):
            if orig in seen:
                continue
            seen.add(orig)
            port = orig.with_suffix("")            # x.py.orig -> x.py
            rel = port.relative_to(repo).as_posix()
            if not port.is_file():
                skipped.append({"orig": orig.relative_to(repo).as_posix(), "why": "no port twin"})
                continue
            pairs.append(rel)
            try:
                src_names = _top_names(ast.parse(orig.read_text(encoding="utf-8")))
                port_names = _top_names(ast.parse(port.read_text(encoding="utf-8")))
            except (OSError, SyntaxError) as exc:
                # a pair that cannot be parsed BOTH ways cannot be audited honestly
                findings.append({"module": rel, "name": None, "kind": "parse",
                                 "orig_line": None, "error": str(exc)[:120]})
                continue
            for name, (kind, line) in sorted(src_names.items()):
                if name in port_names or _reexported(name, port, root):
                    continue
                why = drops.get((port_stem(rel), name)) if kind != "import" else None
                if why is not None:
                    ruled.append({"module": rel, "name": name, "kind": kind,
                                  "orig_line": line, "reason": why})
                    continue
                findings.append({"module": rel, "name": name, "kind": kind, "orig_line": line})
    return {"roots": [r.relative_to(repo).as_posix() or "." for r in roots],
            "pairs": len(pairs), "skipped": skipped, "lost": findings,
            "ruled": ruled, "ok": not findings and not skipped}


def _print_report(res: Dict[str, Any]) -> None:
    print(f"slice-audit: {res['pairs']} pair(s) with a .orig twin under "
          f"{', '.join(res['roots'])}, {len(res['skipped'])} skipped, "
          f"{len(res['lost'])} lost, {len(res['ruled'])} ruled")
    for s in res["skipped"]:
        print(f"  skip {s['why']}: {s['orig']}")
    for f in res["lost"]:
        if f["kind"] == "parse":
            print(f"  RED  {f['module']}: cannot parse ({f['error']})")
        else:
            print(f"  RED  {f['module']}: lost {f['kind']} `{f['name']}` (orig line {f['orig_line']})")
    print("floor:", "OK" if res["ok"] else "RED")


def _slice_audit_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--ledger", type=Path, default=None,
                    help="port ledger: a lost name that is a `dropped` node with a reason "
                         "counts as RULED, not red")


def _slice_audit(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        res = audit(Path(root), args.ledger)
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    if getattr(opts, "json", False):
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(res)
    return 0 if res["ok"] else 1


SLICE_AUDIT = Command(add_args=_slice_audit_args, run=_slice_audit)
