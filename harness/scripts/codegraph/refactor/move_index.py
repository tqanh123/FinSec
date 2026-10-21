"""codegraph.refactor.move_index -- top-level bind name -> {dotted module}
across a target tree, the fallback `hs-code port add-module` reads to guess
a home for a leftover import its `module_map` did not resolve.

Port of ob-tools' `portkit.tests_move.move_index` (pinned commit `60075cb`,
`packages/portkit/src/portkit/tests_move/move_index.py`, `_module_names`/
`_name_index`) -- generalised off the DECLARED `import_roots`
(`codegraph.refactor.slice_audit.scan_roots`, the same door `slice_audit`'s
own scan already reuses, and `codegraph._core.layout.dotted_of`, the one
path -> dotted name door) instead of upstream's hard-coded `packages/*/src`
glob -- upstream's tree was always a `packages/<dist>/src` workspace; this
tree also ships plain flat layouts (no `packages/` directory at all), and a
port has to work on either. Upstream's own module-level `_NAME_INDEX` cache
is NOT ported: a process-lifetime cache keyed by a bare `Path` would leak
across an unrelated caller's fixture in the SAME test process (this tree's
tests share interpreters). What is kept is per FILE: the binds one file
parses to, keyed by its resolved path AND its `(st_ino, st_size, st_mtime_ns,
st_ctime_ns)` — every call still walks the tree (a file written, added or
deleted since is seen), only an unchanged file is not parsed again. Measured:
`drift-merge` asks twice per merged file; on a 1698-file target, 171 files
took 6 minutes, all of it re-parsing the same unchanged tree.

WHAT IT CANNOT SEE
    * A name defined only dynamically (`globals()[x] = ...`, a decorator
      that injects an attribute) is not indexed -- only a literal top-level
      `def`/`class`/`Assign`/`AnnAssign`/`Import`/`ImportFrom` counts.
    * Ambiguity: `module_names` returns the FIRST file its scan order finds
      under any declared root, never a "more than one root claims this"
      refusal -- the same leniency upstream's own single-glob `hits[0]`
      had, unexercised in practice because a repo's own layout declares
      non-overlapping roots.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from codegraph._core import walk
from codegraph._core.layout import dotted_of
from ._layout import optional_descriptor

from .slice_audit import scan_roots


def _import_roots(repo: Path):
    """The repo's own declared `import_roots`, or `(".",)` when there is no
    `codemap.yaml` at all -- `dotted_of` needs at least one root string to
    resolve anything, and a repo with no descriptor is a flat tree whose
    only sensible root is itself."""
    descriptor = optional_descriptor(repo)
    return (descriptor.import_roots if descriptor is not None else None) or (".",)


def _top_level_binds(tree: ast.Module) -> Set[str]:
    out: Set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            out.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
        elif isinstance(n, ast.Import):
            out.update(a.asname or a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            out.update(a.asname or a.name for a in n.names)
    return out


_Stamp = Tuple[int, int, int, int]
_BINDS: "Dict[str, Tuple[_Stamp, Optional[Set[str]]]]" = {}
_MODS: "Dict[str, Tuple[tuple, Dict[str, Path]]]" = {}
_INDEX: "Dict[str, Tuple[tuple, Dict[str, Set[str]]]]" = {}


def _stamp(path: str) -> Optional[_Stamp]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _binds_of(path: Path) -> Optional[Set[str]]:
    """`path`'s top-level binds, `None` when unreadable or not Python — parsed
    once per content stamp (module docstring)."""
    key = str(path)
    stamp = _stamp(key)
    if stamp is None:
        return None
    hit = _BINDS.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        binds: Optional[Set[str]] = _top_level_binds(
            ast.parse(Path(key).read_text(encoding="utf-8")))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        binds = None
    _BINDS[key] = (stamp, binds)
    return binds


def _python_files(repo: Path) -> "list[str]":
    """Every `.py` under the declared scan roots, root by root, each root's
    files in path order; the directories no walk enters are pruned."""
    out: "list[str]" = []
    for root in scan_roots(repo):
        root_s = str(root)
        found = []
        for dp, dn, fn in os.walk(root_s):
            rel_dir = os.path.relpath(dp, root_s)
            parts = () if rel_dir == "." else tuple(rel_dir.split(os.sep))
            dn[:] = [d for d in dn if not walk.skipped(parts + (d,))]
            found += [os.path.join(dp, f) for f in fn if f.endswith(".py")]
        out += sorted(found, key=lambda f: f.split(os.sep))
    return out


def _module_to_path(repo: Path) -> Dict[str, Path]:
    """Every `.py` file under the declared scan roots, keyed by its own
    dotted name -- through the SAME one path->dotted-name door,
    `dotted_of`, never a second path -> dotted derivation. Recomputed only
    when the file list or the declared roots change."""
    roots = tuple(_import_roots(repo))
    files = tuple(_python_files(repo))
    key = str(Path(repo).absolute())
    hit = _MODS.get(key)
    if hit is not None and hit[0] == (roots, files):
        return hit[1]
    repo_s = str(repo)
    out: Dict[str, Path] = {}
    for f in files:
        dotted = dotted_of(os.path.relpath(f, repo_s).replace(os.sep, "/"), roots)
        if dotted and dotted not in out:
            out[dotted] = Path(f)
    _MODS[key] = ((roots, files), out)
    return out


def module_names(mod: str, repo: Path) -> Optional[Set[str]]:
    """Top-level names `mod` (a dotted module under `repo`'s own declared
    layout) binds -- own defs, assigns, and imports. `None` when the file is
    not found at all -- the caller treats that as UNKNOWN, never empty."""
    path = _module_to_path(repo).get(mod)
    return None if path is None else _binds_of(path)


def name_index(repo: Path) -> Dict[str, Set[str]]:
    """Top-level bind name -> {dotted module} across the WHOLE `repo`. A
    from-import can also name a SUBMODULE (`from pkg.tools import x`, where
    `x` is itself a module, not a bound name inside `tools`) -- the bind
    scan alone cannot see that, so every dotted module is ALSO indexed under
    every suffix of its own dotted path (`pkg.tools.x` -> keys `pkg.tools.x`,
    `tools.x`, `x`), the caller resolving a module-name import by rewriting
    the PARENT. The same dict comes back while no file changed: read it,
    never mutate it."""
    mods = _module_to_path(repo)
    stamps = tuple((mod, _stamp(str(path))) for mod, path in mods.items())
    key = str(Path(repo).absolute())
    hit = _INDEX.get(key)
    if hit is not None and hit[0] == stamps:
        return hit[1]
    idx: Dict[str, Set[str]] = {}
    for mod, path in mods.items():
        binds = _binds_of(path)
        if binds is None:
            continue
        for name in binds:
            idx.setdefault(name, set()).add(mod)
        parts = mod.split(".")
        for i in range(1, len(parts)):
            idx.setdefault(".".join(parts[i:]), set()).add(mod)
    _INDEX[key] = (stamps, idx)
    return idx
