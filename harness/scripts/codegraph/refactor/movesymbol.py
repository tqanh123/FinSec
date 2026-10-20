"""codegraph.refactor.movesymbol -- AST move-symbol: cut a definition out of
one module, paste into another.

Port of ob-tools' `devkit.codemod.movesymbol` (pinned commit `60075cb`,
`packages/devkit/src/devkit/codemod/movesymbol.py`). WHICH FILE moves is
layout-agnostic (both modules are named by an explicit repo-relative path,
no package resolution needed) -- but the generated `from <dest> import
<name>` re-export line is NOT: upstream always spelled it via a naive
full-path replace (`a/b/c.py` -> `a.b.c`), which is only importable when the
repo root itself is the sole sys.path entry. `_module_of` here tries the
target repo's own `codemap.yaml` `import_roots` first and falls
back to the naive replace only when none is declared -- see `_module_of`'s
own docstring; measured wrong on a `packages/*/src` fixture before this
fallback existed (the generated import named `packages.foo.src.dst`, not
`dst`, and could not resolve). Adapted for this repo: `RenameError` is
`.errors.RefactorError`. Upstream's `find_symbol` (a repo-wide "who defines
this name" search, for a caller that does not already know SRC) is not
ported -- `--symbol` here always takes an explicit `--from FILE`, so no
door ever needed the search, and it had 0 callers, including in tests.

WHY AST CUT-PASTE, NOT LINE RANGES
    A symbol's extent is not a line range a human eyeballed: decorators, a
    multi-line signature, a nested class body, and trailing comments all move
    with it. `ast` gives exact spans, so the cut is byte-exact and the
    leftover keeps every byte outside the span.

WHAT IT UPDATES
    1. the source module gets an explicit `from <dest> import <name>`
       re-export so every existing importer keeps working;
    2. the destination module gets the definition, inserted in sorted
       position among top-level defs (deterministic -- same input, same
       bytes);
    3. `__init__.py` re-exports are left as-is when they already name the
       symbol (a note only, never rewritten -- the public path did not
       change: the re-export still works through the source module's new
       `from <dest> import` line).

WHY IT REFUSES INSTEAD OF GUESSING
    * the symbol must exist exactly once as a top-level definition;
    * the destination must not already define or import that name;
    * a name used by the moved body but defined in the SOURCE module only is
      a dangling reference after the move -- reported per name, refused
      unless `--allow-dangling`.

WHAT IT CANNOT SEE
    * `getattr(module, "name")`, `importlib`, string-keyed registries, and
      any dynamic reference;
    * a `super().method()` lookup that relied on module locality;
    * comments and docstrings elsewhere that mention the old home;
    * conditional definitions (`if TYPE_CHECKING:`, `try/except
      ImportError`) move verbatim, their guards are not analysed;
    * type stubs (`.pyi`) and non-Python references;
    * circular imports the move may create -- the new edge is not simulated.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import attrs

from .errors import RefactorError


@attrs.define(frozen=True)
class DanglingRef:
    """A name the moved body uses that only the SOURCE module defines."""
    name: str
    line: int  # 1-based, inside the source file


@attrs.define
class MovePlan:
    """One symbol's move: exact spans, new sources, and what stayed behind."""
    symbol: str
    src_path: Path                 # relative to root
    dst_path: Path                 # relative to root
    src_new: Optional[str] = None  # source file after the cut
    dst_new: Optional[str] = None  # destination file after the paste
    dangling: Tuple[DanglingRef, ...] = ()
    refused: Optional[str] = None
    notes: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.refused is None and self.src_new is not None


@attrs.define
class MoveResult:
    """All-or-nothing move outcome; `apply()` never touches the source tree
    until every plan in it is `ok`."""
    root: Path
    plans: List[MovePlan] = attrs.field(factory=list)

    @property
    def refused_plans(self) -> List[MovePlan]:
        return [p for p in self.plans if p.refused]

    def apply(self) -> List[str]:
        """Write every changed file IN PLACE under `root`; refuses (writes
        nothing) when any plan in this result is refused."""
        if self.refused_plans:
            raise RefactorError("refusing to apply: " + "; ".join(
                "%s: %s" % (p.symbol, p.refused) for p in self.refused_plans))
        written: List[str] = []
        for p in self.plans:
            if not p.ok:
                continue
            for rel, new in ((p.src_path, p.src_new), (p.dst_path, p.dst_new)):
                if new is None:
                    continue
                dest = self.root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new, encoding="utf-8", newline="")
                written.append(rel.as_posix())
        return written


def _top_level_defs(tree: ast.Module) -> Dict[str, ast.stmt]:
    out: Dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(node.name, node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out.setdefault(tgt.id, node)
    return out


def _span(node: ast.stmt, offsets: List[int]) -> Tuple[int, int]:
    """Byte span of a statement INCLUDING its decorators and trailing newline."""
    start_line = min([node.lineno] + [d.lineno for d in
                                      getattr(node, "decorator_list", [])]) - 1
    end_line = node.end_lineno or node.lineno
    start = offsets[start_line]
    end = offsets[end_line] if end_line < len(offsets) else offsets[-1]
    return start, end


def _module_names(tree: ast.Module) -> set:
    """Every name the module binds at top level (defs, assigns, imports)."""
    names: set = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _used_names(node: ast.AST) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            out.setdefault(sub.id, sub.lineno)
    return out


def _line_offsets(src: str) -> List[int]:
    offsets, at = [0], 0
    for line in src.splitlines(keepends=True):
        at += len(line)
        offsets.append(at)
    return offsets


def _module_of(rel: str, import_roots: Tuple[str, ...] = ()) -> str:
    """`rel` (repo-relative) -> the dotted name it is importABLE as, used in
    the re-export line. Tried through the declared layout schema first
    (`codegraph._core.layout.dotted_of`) -- `packages/foo/src/
    dst.py` under a declared `packages/*/src` root is `dst`, not
    `packages.foo.src.dst`, the second being unimportable under that layout.
    Adapted from upstream, which only ever did the naive full-path replace
    below (correct only when the repo root itself is the sole sys.path
    entry) -- kept as the fallback for a tree with no declared
    `import_roots` at all, never silently dropped."""
    if import_roots:
        from codegraph._core.layout import dotted_of
        dotted = dotted_of(rel, import_roots)
        if dotted:
            return dotted
    return str(Path(rel).with_suffix("")).replace("/", ".")


def _insert_reexport(src: str, dst_mod: str, symbol: str) -> str:
    """Append `from <dst_mod> import <symbol>` to the import block."""
    line = "from %s import %s  # moved: surface kept for importers\n" % (dst_mod, symbol)
    lines = src.splitlines(keepends=True)
    last_import = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith(("import ", "from ")) or not stripped:
            if stripped:
                last_import = i
            continue
        if not stripped.startswith(("#", '"""', "'''")):
            break
    at = last_import + 1 if last_import >= 0 else 0
    return "".join(lines[:at]) + line + "".join(lines[at:])


def _insert_definition(dst_src: str, dst_tree: ast.Module, body: str, symbol: str) -> str:
    """Insert `body` among top-level defs, sorted by name (deterministic)."""
    offsets = _line_offsets(dst_src)
    anchor: Optional[int] = None
    for node in dst_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name > symbol:
                anchor = min([node.lineno] + [d.lineno for d in
                                              getattr(node, "decorator_list", [])]) - 1
                break
    if anchor is None:
        sep = "" if dst_src.endswith("\n\n") or not dst_src else "\n"
        return dst_src + sep + body
    at = offsets[anchor]
    block = body if body.endswith("\n\n") else body.rstrip("\n") + "\n\n"
    return dst_src[:at] + block + dst_src[at:]


def plan_move(root: Path, symbol: str, src_rel: str, dst_rel: str,
             *, allow_dangling: bool = False, import_roots: Tuple[str, ...] = ()) -> MoveResult:
    """Plan moving `symbol` from `src_rel` to `dst_rel` inside `root`.
    `import_roots` (from the target repo's own `codemap.yaml`, when it
    declares one) resolves the destination's re-exportable dotted name
    through the real layout schema -- see `_module_of`."""
    result = MoveResult(root=root)
    src_path, dst_path = root / src_rel, root / dst_rel
    if not src_path.is_file():
        raise RefactorError("source module not found: %s" % src_rel)
    if not dst_path.is_file():
        raise RefactorError("destination module not found: %s" % dst_rel)
    if src_rel == dst_rel:
        raise RefactorError("source and destination are the same module")

    # `Path.read_text(newline=...)` is a 3.13+ signature -- this build's floor is
    # 3.12 (plan.md's own stack line), so newline-preserving reads go through
    # `open()` directly; `write_text(newline="")` below IS 3.10+ and stays as-is.
    src_src = src_path.open(encoding="utf-8", newline="").read()
    dst_src = dst_path.open(encoding="utf-8", newline="").read()
    try:
        src_tree = ast.parse(src_src)
        dst_tree = ast.parse(dst_src)
    except SyntaxError as e:
        raise RefactorError("cannot parse: %s" % e) from e

    defs = _top_level_defs(src_tree)
    node = defs.get(symbol)
    if node is None:
        result.plans.append(MovePlan(
            symbol, Path(src_rel), Path(dst_rel),
            refused="%r is not a top-level definition of %s" % (symbol, src_rel)))
        return result
    if symbol in _module_names(dst_tree):
        result.plans.append(MovePlan(
            symbol, Path(src_rel), Path(dst_rel),
            refused="%s already binds %r -- moving would shadow it" % (dst_rel, symbol)))
        return result

    offsets = _line_offsets(src_src)
    start, end = _span(node, offsets)
    body = src_src[start:end]

    src_names = _module_names(src_tree) - {symbol}
    dst_names = _module_names(dst_tree)
    dangling = tuple(
        DanglingRef(name, line)
        for name, line in sorted(_used_names(node).items())
        if name in src_names and name not in dst_names
        and not hasattr(builtins, name))
    if dangling and not allow_dangling:
        result.plans.append(MovePlan(
            symbol, Path(src_rel), Path(dst_rel), dangling=dangling,
            refused=("%d name(s) would dangle after the move "
                     "(pass --allow-dangling to accept them)" % len(dangling))))
        return result

    dst_mod = _module_of(dst_rel, import_roots)
    leftover = src_src[:start] + src_src[end:]
    leftover = _insert_reexport(leftover, dst_mod, symbol)
    dst_new = _insert_definition(dst_src, dst_tree, body, symbol)

    notes: List[str] = []
    for init_rel in (Path(src_rel).parent / "__init__.py", Path(dst_rel).parent / "__init__.py"):
        init_path = root / init_rel
        if not init_path.is_file():
            continue
        text = init_path.open(encoding="utf-8", newline="").read()
        if ('"%s"' % symbol) in text or ("'%s'" % symbol) in text:
            notes.append("%s: __all__ entry kept (public path unchanged, re-export follows)" % init_rel)

    result.plans.append(MovePlan(
        symbol, Path(src_rel), Path(dst_rel), src_new=leftover, dst_new=dst_new,
        dangling=dangling, notes=tuple(notes)))
    return result
