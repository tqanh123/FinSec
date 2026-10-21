"""hs-code refactor rename -- one identifier renamed as NAME tokens, all or nothing.

    hs-code refactor rename --old NAME --new NAME [--path DIR ...]

WHY SPAN SURGERY, NOT `tokenize.untokenize`
    `untokenize` re-synthesizes spacing and can shift bytes it was not asked to touch.
    Only the (row, col) spans of NAME tokens equal to the old identifier are spliced;
    comments, strings, whitespace and line endings are copied verbatim, so a rename and
    its inverse give back the file byte for byte.

WHY REFUSE WHEN THE NEW NAME EXISTS
    Merging two bindings into one name silently is the bug a rename must not write. A
    file where the new name already occurs as a NAME refuses, and one refusal stops the
    whole run: a half-renamed tree is worse than no rename.

Dry-run by default; `--apply` writes in place, like `refactor move` (git is the undo).
Scans every `.py` under each `--path` (repo-relative, default the repo), skipping tool,
VCS and port source-checkout directories (`codegraph._core.walk`).

WHAT IT CANNOT SEE  (listed as blind spots, never rewritten)
    * Strings and comments: the old name in prose, SQL, YAML or docs survives.
    * `__slots__` entries, `getattr`/`setattr`/`delattr`/`hasattr` name strings, and
      CLI flags (`add_argument("--old")` derives `args.old`: the token is renamed, the
      flag is not).
    * Dynamic imports and any other name-as-string dispatch.
    * Scope: every NAME token with the spelling changes, in any scope; scope-precise
      renaming needs scope analysis this verb does not do.
    * Non-`.py` files.

Exit: 0 every candidate planned (written under --apply) · 1 a file refused, or the name
occurs nowhere -- nothing written · 2 the request is malformed.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import io
import json
import keyword
import tokenize
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import attrs
import hs_family

from . import oldrefs
from codegraph._core import walk

from .errors import RefactorError
from hs_family.cli import Command

#: Builtins whose `(obj, "name", ...)` form hides a real attribute use.
DYNAMIC_ATTR_CALLS = frozenset({"getattr", "setattr", "delattr", "hasattr"})


@attrs.frozen
class BlindSpot:
    kind: str       # slots-string | attr-call-string | cli-flag-string | bare-string
    line: int
    detail: str


@attrs.frozen
class FilePlan:
    path: str                                   # repo-relative
    renamed: int = 0
    blind_spots: Tuple[BlindSpot, ...] = ()
    refused: Optional[str] = None
    new_source: Optional[str] = None

    @property
    def is_candidate(self) -> bool:
        return self.refused is not None or self.renamed > 0 or bool(self.blind_spots)


def _check_names(old: str, new: str) -> None:
    for name in (old, new):
        if not name.isidentifier() or keyword.iskeyword(name):
            raise RefactorError("%r is not a usable identifier (a non-keyword identifier)" % name)
    if old == new:
        raise RefactorError("--old and --new are both %r" % old)


def _blind_spots(tree: ast.AST, old: str) -> Tuple[BlindSpot, ...]:
    """The places a NAME-token renamer cannot reach. A string already named as a slot or
    an attribute-call name is not listed again as a bare string."""
    spots: List[BlindSpot] = []
    claimed: set = set()
    flags = {"-" + old, "--" + old, "--" + old.replace("_", "-")}

    def strings(node: Optional[ast.expr]) -> List[ast.Constant]:
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [e for e in node.elts if isinstance(e, ast.Constant)]
        return [node] if isinstance(node, ast.Constant) else []

    for node in ast.walk(tree):
        targets: List[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        for t in targets:
            name = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
            if name == "__slots__":
                for c in strings(value):
                    if c.value == old:
                        claimed.add(id(c))
                        spots.append(BlindSpot("slots-string", c.lineno, '__slots__ holds "%s"' % old))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in DYNAMIC_ATTR_CALLS and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == old):
            claimed.add(id(node.args[1]))
            spots.append(BlindSpot("attr-call-string", node.lineno, '%s(.., "%s", ..)' % (node.func.id, old)))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in claimed:
            continue
        if node.value in flags:
            spots.append(BlindSpot("cli-flag-string", node.lineno,
                                   'flag "%s" derives a dest the rename orphans' % node.value))
        elif node.value == old:
            spots.append(BlindSpot("bare-string", node.lineno,
                                   'string "%s" (config, SQL, importlib? judge by hand)' % old))
    return tuple(sorted(spots, key=lambda s: (s.line, s.kind)))


def plan_source(rel: str, src: str, old: str, new: str) -> FilePlan:
    try:
        tree = ast.parse(src)
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (SyntaxError, tokenize.TokenError) as e:
        return FilePlan(rel, refused="does not parse: %s" % e)
    spans = [(t.start, t.end) for t in tokens if t.type == tokenize.NAME and t.string == old]
    clash = next((t.start[0] for t in tokens if t.type == tokenize.NAME and t.string == new), None)
    spots = _blind_spots(tree, old)
    if clash is not None and spans:
        return FilePlan(rel, blind_spots=spots, refused="%r already present (line %d)" % (new, clash))
    if not spans:
        return FilePlan(rel, blind_spots=spots)
    offsets = [0]
    for line in src.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    out = src
    for (sr, sc), (er, ec) in sorted(spans, reverse=True):
        out = out[:offsets[sr - 1] + sc] + new + out[offsets[er - 1] + ec:]
    return FilePlan(rel, renamed=len(spans), blind_spots=spots, new_source=out)


def _read(p: Path) -> str:
    with open(p, encoding="utf-8", newline="") as fh:
        return fh.read()


def _files(repo: Path, paths: List[str]) -> List[Path]:
    out: set = set()
    for rel in paths or ["."]:
        base = (repo / rel).resolve()
        if not base.exists() or repo.resolve() not in (base, *base.parents):
            raise RefactorError("--path %s: no such directory or file under %s" % (rel, repo))
        found = [base] if base.is_file() else base.rglob("*.py")
        for p in found:
            parts = p.relative_to(repo.resolve()).parts
            if p.suffix == ".py" and not walk.skipped(parts[:-1]):
                out.add(p)
    return sorted(out)


def plan_tree(repo: Path, old: str, new: str, paths: List[str]) -> Tuple[int, List[FilePlan]]:
    """(files scanned, the candidate plans)."""
    _check_names(old, new)
    files = _files(repo, paths)
    plans = [plan_source(p.relative_to(repo.resolve()).as_posix(), _read(p), old, new) for p in files]
    return len(files), [p for p in plans if p.is_candidate]


def _diff(repo: Path, plans: List[FilePlan]) -> str:
    out: List[str] = []
    for p in plans:
        if p.new_source is not None:
            out += difflib.unified_diff(_read(repo / p.path).splitlines(keepends=True),
                                        p.new_source.splitlines(keepends=True), p.path, p.path)
    return "".join(out)


def _rename_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--old", required=True, help="the identifier to rename")
    ap.add_argument("--new", required=True, help="its new spelling")
    ap.add_argument("--path", action="append", default=[], metavar="DIR",
                    help="repo-relative directory or file to scan (repeatable; default the repo)")


def _rename(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    apply = bool(getattr(opts, "apply", False))
    repo = Path(root).resolve()
    try:
        scanned, plans = plan_tree(repo, args.old, args.new, args.path)
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    refused = [p for p in plans if p.refused]
    tokens = sum(p.renamed for p in plans)
    rc = 1 if refused or not tokens else 0
    _history, _relative_roots = oldrefs.declared(repo)
    written: List[str] = []
    if apply and rc == 0:
        for p in plans:
            if p.new_source is not None:
                with open(repo / p.path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(p.new_source)
                written.append(p.path)
    data: Dict[str, Any] = {
        "schema": "hs-code.refactor.rename/1", "old": args.old, "new": args.new, "scanned": scanned,
        "applied": bool(written), "renamed_tokens": tokens, "written": written,
        "files": [{"path": p.path, "renamed": p.renamed} for p in plans if p.renamed and not p.refused],
        "refused": [{"path": p.path, "why": p.refused} for p in refused],
        "blind_spots": [{"path": p.path, "line": s.line, "kind": s.kind, "detail": s.detail}
                        for p in plans for s in p.blind_spots],
        # Python is the token renamer's own; outside it the old name is only listed.
        # history/relative_roots come from THIS repo's own codemap.yaml (oldrefs.declared).
        "old_mentions": oldrefs.as_rows(args.old, oldrefs.old_mentions(
            repo, args.old, args.new, kinds=("md", "yaml-json", "other"),
            history=_history, relative_roots=_relative_roots)),
    }
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return rc
    print("hs-code refactor rename %s -> %s: scanned %d file(s), %d token(s)"
          % (args.old, args.new, scanned, tokens))
    for p in plans:
        if p.refused:
            print("  REFUSED %s: %s" % (p.path, p.refused))
        elif p.renamed:
            print("  ok      %s  (%d token(s))" % (p.path, p.renamed))
    oldrefs.print_report(data["old_mentions"])
    if data["blind_spots"]:
        print("  blind spots (fix by hand):")
        for s in data["blind_spots"]:
            print("    %s:%d  %-16s %s" % (s["path"], s["line"], s["kind"], s["detail"]))
    if refused:
        print("hs-code refactor rename: REFUSED -- all or nothing, nothing written")
    elif not tokens:
        print("hs-code refactor rename: %r occurs nowhere as a name -- check the spelling" % args.old)
    elif written:
        print("hs-code refactor rename: applied -- %d file(s) written" % len(written))
    else:
        print(_diff(repo, plans), end="")
        print("hs-code refactor rename: dry-run -- pass --apply to write")
    return rc


RENAME = Command(add_args=_rename_args, run=_rename)
