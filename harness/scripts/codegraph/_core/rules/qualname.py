"""The name a rule gives a def: its `__qualname__`, numbered when a scope defines it twice.

`A.run`, `outer.<locals>.run`; the second definition at one qualname is `run#2`, and so
on in line order. A verdict or finding keyed `path::name` stays attached to ONE def.
"""
from __future__ import annotations

import ast

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def qualnames(tree: ast.AST) -> dict[int, str]:
    """id(def node) -> its `__qualname__`."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for c in ast.iter_child_nodes(node):
            if isinstance(c, DEFS):
                out[id(c)] = prefix + c.name
                walk(c, prefix + c.name + ("." if isinstance(c, ast.ClassDef) else ".<locals>."))
            else:
                walk(c, prefix)
    walk(tree, "")
    return out


class Namer:
    """`namer(node, kind)` -> the node's qualname, `#n` from the second time one `kind`
    names it. Call in line order."""

    def __init__(self, tree: ast.AST) -> None:
        self._qual = qualnames(tree)
        self._seen: dict[tuple[str, str], int] = {}

    def __call__(self, node: ast.AST, kind: str = "") -> str:
        q = self._qual[id(node)]
        n = self._seen[kind, q] = self._seen.get((kind, q), 0) + 1
        return q if n == 1 else f"{q}#{n}"
