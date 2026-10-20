"""repoint -- rewrite importers of a carved module to the module that now owns each name.

`carve` keeps the old import path alive as a facade re-export: right for outside callers,
wrong for siblings. A sibling that still imports a moved name through the facade keeps an
edge on the facade, so the cut buys no independence and can close a cycle (the facade
imports the cut module, the sibling imports the facade).

    from acme.x.mod import moved, kept as k      # before (mod carved into sub)
    from acme.x.sub import moved
    from acme.x.mod import kept as k             # after

Absolute and relative spellings both match; a relative one is resolved through the
importer's own dotted name, which `layout.dotted_of` reads from the declared
`import_roots`. Aliases, function-level imports and trailing comments survive; star
imports are skipped.

WHAT IT CANNOT SEE
    * String references to the old path (`monkeypatch.setattr("acme.x.mod.moved")`,
      `patch(...)`, `importlib.import_module`).
    * `import acme.x.mod` / `mod.moved` attribute use: the facade keeps it working, the
      edge stays on the facade.
    * Importers outside the directories passed in keep the facade path (still valid).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from codegraph._core.layout import dotted_of


def _resolve(node: ast.ImportFrom, importer: Optional[str], is_init: bool) -> Optional[str]:
    """Dotted target of a `from ... import` (None when a relative hop has no anchor)."""
    if not node.level:
        return node.module
    if importer is None:
        return None
    base = importer.split(".")
    if not is_init:
        base = base[:-1]
    if node.level - 1 > len(base):
        return None
    base = base[:len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _alias(a: ast.alias) -> str:
    return "%s as %s" % (a.name, a.asname) if a.asname else a.name


def repoint_text(text: str, importer: Optional[str], is_init: bool, dotted_src: str,
                 owner: Dict[str, str], pkg: str) -> Tuple[str, int]:
    """(new text, rewritten `from` statements). `owner` maps a moved name to its cut module."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, 0
    lines = text.splitlines(keepends=True)
    edits: List[Tuple[int, int, List[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or _resolve(node, importer, is_init) != dotted_src:
            continue
        if any(a.name == "*" for a in node.names):
            continue
        moved: Dict[str, List[str]] = {}
        stay: List[str] = []
        for a in node.names:
            (moved.setdefault(owner[a.name], []) if a.name in owner else stay).append(_alias(a))
        if not moved:
            continue
        end = node.end_lineno or node.lineno
        first, last = lines[node.lineno - 1], lines[end - 1]
        indent = first[:len(first) - len(first.lstrip())]
        end_col = node.end_col_offset or 0
        if first[len(indent):len(indent) + 4] != "from" or last[end_col:].strip().startswith(";"):
            continue                              # `a; from ... import`: the facade still serves it
        tail = last[end_col:].rstrip("\n")
        nl = "\n" if last.endswith("\n") else ""
        new: List[str] = []
        if stay:
            src = ("." * node.level) + (node.module or "") if node.level else dotted_src
            new.append("%sfrom %s import %s" % (indent, src, ", ".join(stay)))
        for mod in sorted(moved):
            home = "%s.%s" % (pkg, mod) if pkg else mod
            new.append("%sfrom %s import %s" % (indent, home, ", ".join(moved[mod])))
        edits.append((node.lineno, end, [ln + tail + nl for ln in new]))   # a trailing lint waiver covers every line
    for s, e, new in sorted(edits, reverse=True):
        lines[s - 1:e] = new
    return "".join(lines), len(edits)


def repoint_tree(repo: Path, roots: Sequence[Path], skip: Set[Path], dotted_src: str,
                 owner: Dict[str, str], import_roots: Sequence[str], pkg: str) -> Dict[Path, Tuple[str, int]]:
    """{file: (new text, n)} for every `.py` under `roots` importing a moved name from
    `dotted_src`; a cut module `m` is `<pkg>.m` (`m` when `pkg` is empty)."""
    out: Dict[Path, Tuple[str, int]] = {}
    for root in roots:
        for f in sorted(root.rglob("*.py")):
            if f.resolve() in skip or "__pycache__" in f.parts:
                continue
            rel = f.resolve().relative_to(repo.resolve()).as_posix()
            new, n = repoint_text(f.read_text(encoding="utf-8"), dotted_of(rel, import_roots),
                                  f.name == "__init__.py", dotted_src, owner, pkg)
            if n:
                out[f] = (new, n)
    return out
