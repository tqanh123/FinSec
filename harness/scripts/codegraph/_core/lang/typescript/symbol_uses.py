"""The lines of one TypeScript/JavaScript importer that use SYMBOL of a module -- the
pack's half of `graph importers MODULE --symbol SYMBOL`.

A line counts when it holds an import or re-export of SYMBOL from the module (`import
{SYMBOL as x}`, `export {SYMBOL} from`, `export * from`), a reference to the name that
import bound (a value, a type, a JSX tag), or `ns.SYMBOL` where `ns` is a namespace the
file bound to the module (`import * as ns`, `import ns = require()`, `const ns =
require()`). `default` is the default export: `import x from`, `const x = require()`.
Which module an import names is the pack's resolver's answer on the same tree.

Each engine lists the file's references (`Refs`) from its own syntax tree, never from the
raw text: a name in a comment or a string is no use.

WHAT IT CANNOT SEE
    * Shadowing: a parameter or a local of the imported name in a nested scope reads as
      the import.
    * A dynamic `import()` bound to a name (`const m = await import("./x")`), a name
      reached by a string (`ns[k]`), a namespace re-bound to another name.
    * A use through another module's re-export: only the module's DIRECT importers are
      read, as for Python.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Callable

from .facts import Refs, TsImport


def use_lines(imports: "tuple[TsImport, ...]", refs: Refs, names_target: "Callable[[TsImport], bool]",
              symbol: str) -> "list[int]":
    """The sorted lines using `symbol`, given the file's `imports` and `refs`;
    `names_target(imp)` says whether an import resolves to the module asked about."""
    lines: "set[int]" = set()
    local: "set[str]" = set()
    spaces: "set[str]" = set()
    reqs = [TsImport("require", spec, tuple((i, loc, False) for i, loc in bound), False, line)
            for line, spec, bound in refs.requires]
    for imp in tuple(imports) + tuple(reqs):
        if not names_target(imp):
            continue
        if imp.kind == "export_all" and not imp.bindings:
            if symbol != "default":
                lines.add(imp.line)
            continue
        for imported, name, _type_only in imp.bindings:
            if imported == symbol:
                lines.add(imp.line)
                if imp.kind != "export_from":
                    local.add(name)
            elif imported == "*" and imp.kind != "export_from":
                spaces.add(name)
                if symbol == "default" and imp.kind in ("require", "import_equals"):
                    local.add(name)
    lines |= {ln for ln, n in refs.plain if n in local}
    lines |= {ln for ln, obj, prop in refs.member if obj in spaces and prop == symbol}
    return sorted(lines)


def names_from(resolve: "Callable[[TsImport], Any]", target: str) -> "Callable[[TsImport], bool]":
    """`names_target` for `use_lines`: `resolve(imp)` (`resolve_static.resolve`'s result)
    is internal and lands on `target`."""
    def hit(imp: TsImport) -> bool:
        if imp.spec is None:
            return False
        res = resolve(imp)
        return res.status == "internal" and res.target == target
    return hit


class Reading:
    """One engine's reading of importer files for a run of `use_lines` questions: the
    `tree-sitter` engine in this process, the `typescript` engine through one bridge
    process opened on the first file and closed with the context."""

    def __init__(self, engine: str, repo: "Path | None" = None):
        self.engine, self.repo = engine, repo
        self._stack = contextlib.ExitStack()
        self._bridge: Any = None

    def __enter__(self) -> "Reading":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()

    def read(self, rel: str, text: str) -> "tuple[tuple[TsImport, ...], Refs]":
        path = Path(rel)
        if self.engine == "tree-sitter":
            from .treesitter import reader
            return reader.TreeSitterTsReader().parse(path, text, (), rel).imports, reader.refs(path, text)
        from .node import engine
        if self._bridge is None:
            self._bridge = self._stack.enter_context(engine.open_bridge(self.repo))
        (payload,) = self._bridge.read([(path, text)])
        (raw,) = self._bridge.refs([(rel, text)])
        return (engine.module_symbols(path, text, (), rel, payload).imports,
                Refs(tuple(tuple(x) for x in raw["plain"]), tuple(tuple(x) for x in raw["member"]),
                     tuple((ln, spec, tuple(tuple(b) for b in bound)) for ln, spec, bound in raw["requires"])))


@contextlib.contextmanager
def importer_lines(repo: Path, target: str, symbol: str, descriptor: Any):
    """`lines(rel, text)` -> the lines of importer `rel` using `symbol` of module `target`,
    read with the engine `descriptor` declares and resolved on the tree its snapshot
    measures (the resolver's own workspaces, tsconfigs and aliases)."""
    from ...walk import git_listed
    from .. import registry as lang_registry
    from . import resolve_static, resolver

    opts = lang_registry.options_of(descriptor, resolver.ID)
    roots = list(getattr(descriptor, "roots", ()) or ())
    tree = resolve_static.Tree.from_parts(repo, git_listed(repo), resolver.configs(repo, roots, descriptor),
                                          use_aliases=bool(opts.get("aliases", True)))
    with Reading(opts.get("engine", "tree-sitter"), repo) as reading:
        def lines(rel: str, text: str) -> "list[int]":
            imports, refs = reading.read(rel, text)
            return use_lines(imports, refs, names_from(lambda imp: resolve_static.resolve(tree, rel, imp), target),
                             symbol)
        yield lines
