"""symbol_uses -- the lines of one importer that use SYMBOL of MODULE.

`graph importers MODULE --symbol SYMBOL` keeps a direct importer only when this finds a
line. The importer set alone over-reads: a file importing `store` to call `save` is no
caller of `load`. The import edge of a symbol alone under-reads: a flat tree calls
`store.load()`, never `from store import load` (measured on this harness: 90 of 91 files
using `yaml_io.safe_load` spell it as an attribute).

A line counts when it holds `from <M> import SYMBOL`, a load of the name that import
bound, or `<m>.SYMBOL` where `<m>` is a name an import in this file bound to M (an alias,
`from <pkg> import <leaf>`, a relative import, or the dotted `import a.b` chain itself),
or `<f>().SYMBOL` where `<f>` is a module accessor: a function (or a `self.`/`cls.` method)
whose every `return` hands back a name an import inside it bound to M -- the lazy-import
shape a package uses to break an import cycle.
M is recognised by every spelling in `spellings` -- its snapshot name plus each dotted
name an `import_roots` entry gives its file.

The whole file is read, not only its functions: a call inside a module-level `try:` or an
`if __name__` block is a caller too, and the snapshot's per-symbol `refs` do not hold it.

WHAT IT CANNOT SEE
    * A name reached as a string: `getattr(m, "load")`, `monkeypatch.setattr(m, "load", ...)`,
      `importlib.import_module`.
    * `from M import *` -- which names it binds is not in the file.
    * Shadowing: a local rebinding the imported name still reads as the import.
    * A receiver it cannot trace (a parameter, a returned value): `attribute_lines` names
      each `.SYMBOL` whose receiver is rooted in no import of the file, so the caller can
      keep such a file apart rather than drop it. `json.load` is rooted in an import of
      another module: traced, and not this module's use.
"""
from __future__ import annotations

import ast
from typing import Iterable

from ..adapter.python_adapter import resolve


def _chain(node: ast.expr) -> "str | None":
    parts: "list[str]" = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _bind(n: ast.AST, names: "set[str]", container: "list[str]", symbol: str,
          lines: "set[int]", sym_names: "set[str]", mod_names: "set[str]") -> None:
    """Record what the import `n` binds: the symbol itself, or a name for the module."""
    if isinstance(n, ast.ImportFrom):
        targets = [".".join(t) for t in resolve(n, container)]
        if n.module is None:
            for a, dotted in zip(n.names, targets):
                if dotted in names:
                    mod_names.add(a.asname or a.name)
            return
        base = targets[0] if targets else ""
        for a in n.names:
            if base in names and a.name == symbol:
                lines.add(n.lineno)
                sym_names.add(a.asname or a.name)
            elif f"{base}.{a.name}" in names:
                mod_names.add(a.asname or a.name)
    elif isinstance(n, ast.Import):
        for a in n.names:
            if a.name in names:
                mod_names.add(a.asname or a.name)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _accessors(tree: ast.AST, names: "set[str]", container: "list[str]") -> "set[str]":
    """Functions whose every `return` is a name an import in that same function bound to
    the module; a nested function or class is its own scope and is not looked into."""
    found: "set[str]" = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local: "set[str]" = set()
        returns: "list[ast.Return]" = []
        stack: "list[ast.AST]" = list(fn.body)
        while stack:
            n = stack.pop()
            if isinstance(n, _SCOPES):
                continue
            if isinstance(n, ast.Return):
                returns.append(n)
            _bind(n, names, container, "", set(), set(), local)
            stack.extend(ast.iter_child_nodes(n))
        if returns and all(r.value is not None and _chain(r.value) in local for r in returns):
            found.add(fn.name)
    return found


def _callee(func: ast.expr) -> "str | None":
    if isinstance(func, ast.Name):
        return func.id
    if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")):
        return func.attr
    return None


def _root(node: ast.expr) -> "str | None":
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def attribute_lines(text: str, symbol: str) -> "list[int] | None":
    """Sorted lines holding `<receiver>.SYMBOL` whose receiver is rooted in no name an
    import of this file bound; None when `text` does not parse."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    imported: "set[str]" = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.asname or a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported |= {a.asname or a.name for a in n.names}
    return sorted({n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and n.attr == symbol
                   and _root(n.value) not in imported})


def use_lines(text: str, importer: str, is_package: bool, spellings: Iterable[str],
              symbol: str) -> "list[int] | None":
    """Sorted lines of `text` that use `symbol` of the module spelled `spellings`;
    None when `text` does not parse. `importer` is the file's own dotted name, which
    anchors a relative import."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    names = set(spellings)
    parts = importer.split(".")
    container = parts if is_package else parts[:-1]
    lines: "set[int]" = set()
    sym_names: "set[str]" = set()
    mod_names: "set[str]" = set()
    for n in ast.walk(tree):
        _bind(n, names, container, symbol, lines, sym_names, mod_names)
    if not (sym_names or mod_names):
        return sorted(lines)
    accessors = _accessors(tree, names, container)
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in sym_names:
            lines.add(n.lineno)
        elif isinstance(n, ast.Attribute) and n.attr == symbol and (
                _chain(n.value) in mod_names
                or (isinstance(n.value, ast.Call) and _callee(n.value.func) in accessors)):
            lines.add(n.lineno)
    return sorted(lines)
