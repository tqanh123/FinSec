"""PythonAdapter — the first (and this slice: only) `LanguageAdapter` impl.

Algorithm-faithful port of a reference implementation's per-file AST
extraction. Any change here should be re-verified against a real tree before
it ships — the whole point of this module is that its output is stable
byte-for-byte across runs of the same tree.

WHAT IT CANNOT SEE (inherited contract — every item is load-bearing, measured
against a real tree; do not shorten this list when editing):
    * `body sha` strips comments (`ast.unparse`) — a comment-only change is
      invisible. For trees whose rules live in comments that is a REAL loss.
    * `ast.unparse` normalizes literals — `"a"` and `'a'` hash equal. Intended,
      but a deliberate quote-style change stays silent.
    * Dynamically created symbols (`globals()[name] = ...`, decorator-generated
      functions, `type()` at runtime) are not in the AST, not in the map.
    * A module-level name assigned several times records only the FIRST
      assignment; later ones are listed in `dup_module_assign`.
    * `refs` resolves through two per-file tables (module-local definitions,
      imported names). Method calls on an object (`self.rt.client.query()`),
      `getattr(x, name)()`, dict dispatch, callbacks, and names shadowed by
      local variables do NOT resolve. `refs` is a LOWER BOUND; an empty `refs`
      never means "nobody calls this".
    * Name resolution walks `ast.Name`/`ast.Attribute` loads only; string-typed
      references (`monkeypatch.setattr("pkg.mod.attr", ...)`) are invisible.
    * On a `kind: flat` root, a bare import name is matched against the LEAF of
      every module in that root. One carrier resolves; several do not, and land
      in `ambiguous_imports` rather than an edge — at runtime sys.path order
      decides, and the snapshot has no sys.path. So on a flat tree with repeated
      basenames the edge list is a LOWER bound by construction, and the missing
      edges are named rather than dropped silently.
"""
from __future__ import annotations

import ast
import hashlib
from collections import deque
from pathlib import Path

from .base import ImportRecord, ModuleSymbols, SymbolInfo


def _walk_with_parents(tree: ast.AST):
    """Breadth-first over every node under `tree`, yielding `(node, parent)` —
    `parent` is `None` only for `tree` itself. Same traversal order `ast.walk`
    gives when you drop the parent half, because it is the SAME algorithm
    (`ast.walk`'s own source: a deque seeded with `tree`, each pop extended by
    `ast.iter_child_nodes`). `parse()` used to run three separate `ast.walk`
    passes over the whole tree (parent map, import records, bound-name
    tables) — each one calls `ast.iter_child_nodes` once per node internally,
    so three passes paid for that per-node child lookup three times. One
    walk, reused for all three, pays for it once; a node's parent is always
    yielded before the node itself (BFS order), so a caller building the
    parent map inline — `parents[id(n)] = p` right where `n` is yielded —
    still has every ancestor of `n` on hand by the time `n` arrives.
    """
    todo: deque = deque([(tree, None)])
    while todo:
        node, parent = todo.popleft()
        for child in ast.iter_child_nodes(node):
            todo.append((child, node))
        yield node, parent


def body_hash(node: ast.AST) -> str:
    """sha256[:16] of `ast.unparse(node)` — comments stripped, format normalized."""
    try:
        return hashlib.sha256(ast.unparse(node).encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001 — parity: any unparse failure is recorded, never fatal
        return "unparseable"


def resolve(node: ast.Import | ast.ImportFrom, container: list[str]) -> list[list[str]]:
    """Dotted parts an import statement refers to, relative levels resolved."""
    out: list[list[str]] = []
    if isinstance(node, ast.ImportFrom):
        if node.level:
            base = container[: len(container) - (node.level - 1)]
            if node.module is None:
                for a in node.names:
                    out.append(base + [a.name])
            else:
                out.append(base + node.module.split("."))
        elif node.module:
            out.append(node.module.split("."))
    else:
        for a in node.names:
            out.append(a.name.split("."))
    return out


class PythonAdapter:
    """Reads one `.py` file. `ext` is the registration key in `adapter.registry`."""

    ext = frozenset({".py"})

    def parse(self, path: Path, text: str, container: tuple[str, ...],
              module: str) -> ModuleSymbols:
        tree = ast.parse(text)
        cont = list(container)

        # Parent map, import records and bound-name tables were three
        # separate `ast.walk(tree)` passes; `_walk_with_parents` does the
        # same breadth-first walk ONCE and hands back each node's parent
        # inline, so all three build together in a single pass.
        parents: dict[int, ast.AST] = {}

        def in_function(n: ast.AST) -> bool:
            q = parents.get(id(n))
            while q is not None:
                if isinstance(q, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return True
                q = parents.get(id(q))
            return False

        def under_type_checking(n: ast.AST) -> bool:
            q = parents.get(id(n))
            while q is not None:
                if isinstance(q, ast.If):
                    t = q.test
                    nm = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
                    if nm == "TYPE_CHECKING":
                        return True
                q = parents.get(id(q))
            return False

        # ---- import statements, flags attached, and the per-file name -----
        # ---- resolution tables their targets feed ---------------------------
        imports: list[ImportRecord] = []
        bound_sym: dict[str, str] = {}   # local name -> "<module>::<origin name>"
        bound_mod: dict[str, str] = {}   # alias -> "<module>" (longest dotted form)
        for n, p in _walk_with_parents(tree):
            if p is not None:
                parents[id(n)] = p
            if isinstance(n, ast.ImportFrom):
                # `resolve()` used to run twice per ImportFrom node — once
                # building `resolved` here, again in the old third loop
                # feeding bound_sym/bound_mod. Same node, same container,
                # same answer both times; compute it once and reuse it.
                targets = resolve(n, cont)
                imports.append(ImportRecord(
                    stmt="from", level=getattr(n, "level", 0) or 0,
                    module=n.module, names=tuple(a.name for a in n.names),
                    resolved=tuple(tuple(parts) for parts in targets),
                    in_function=in_function(n),
                    under_type_checking=under_type_checking(n),
                ))
                for t in targets:
                    tgt = ".".join(t)
                    for a in n.names:
                        if a.name == "*":
                            continue
                        bound = a.asname or a.name
                        if n.module is None:
                            bound_mod[bound] = tgt
                        else:
                            bound_sym[bound] = f"{tgt}::{a.name}"
                            bound_mod.setdefault(bound, f"{tgt}.{a.name}")
            elif isinstance(n, ast.Import):
                imports.append(ImportRecord(
                    stmt="import", level=getattr(n, "level", 0) or 0,
                    module=None, names=tuple(a.name for a in n.names),
                    resolved=tuple(tuple(parts) for parts in resolve(n, cont)),
                    in_function=in_function(n),
                    under_type_checking=under_type_checking(n),
                ))
                for a in n.names:
                    bound_mod[a.asname or a.name.split(".")[0]] = a.name

        # ---- per-file name resolution tables (local defs) ----------------------
        local_defs: set[str] = set()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local_defs.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                for x in tg:
                    if isinstance(x, ast.Name):
                        local_defs.add(x.id)

        def refs_of(node: ast.AST) -> list[str]:
            """Symbols `node` references, resolved. Never guesses."""
            found: set[str] = set()
            for x in ast.walk(node):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                    nm = x.id
                    if nm in bound_sym:
                        found.add(bound_sym[nm])
                    elif nm in local_defs:
                        found.add(f"{module}::{nm}")
                elif isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name):
                    base = x.value.id
                    if base in bound_mod and base not in local_defs:
                        found.add(f"{bound_mod[base]}::{x.attr}")
            return sorted(found)

        # ---- symbols -----------------------------------------------------------
        syms: list[SymbolInfo] = []
        seen_names: set[str] = set()
        dup_names: list[str] = []
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name in seen_names:
                    if n.name not in dup_names:
                        dup_names.append(n.name)
                    continue
                seen_names.add(n.name)
                syms.append(SymbolInfo(
                    name=n.name, kind="func",
                    lines=(n.lineno, n.end_lineno or n.lineno), sha=body_hash(n),
                    refs=tuple(refs_of(n)),
                ))
            elif isinstance(n, ast.ClassDef):
                meths = []
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        meths.append(SymbolInfo(
                            name=f"{n.name}.{m.name}", kind="method",
                            lines=(m.lineno, m.end_lineno or m.lineno), sha=body_hash(m),
                            refs=tuple(refs_of(m)), parent=n.name,
                        ))
                syms.append(SymbolInfo(
                    name=n.name, kind="class",
                    lines=(n.lineno, n.end_lineno or n.lineno), sha=body_hash(n),
                    refs=tuple(refs_of(n)), methods=tuple(meths),
                    bases=tuple(ast.unparse(b) for b in n.bases),
                ))
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                tgts: list[ast.expr] = n.targets if isinstance(n, ast.Assign) else [n.target]
                for tv in tgts:
                    if isinstance(tv, ast.Name):
                        # A name assigned SEVERAL times at module level keeps only
                        # the FIRST entry — otherwise the (name, file) key is not
                        # unique and every later comparison drifts. Later
                        # assignments surface in `dup_module_assign` instead.
                        if tv.id in seen_names:
                            if tv.id not in dup_names:
                                dup_names.append(tv.id)
                            continue
                        seen_names.add(tv.id)
                        syms.append(SymbolInfo(
                            name=tv.id, kind="const",
                            lines=(n.lineno, n.end_lineno or n.lineno), sha=body_hash(n),
                            refs=tuple(refs_of(n)),
                        ))

        return ModuleSymbols(
            path=path, module=module, container=container,
            loc=text.count("\n"),
            nbytes=len(text.encode()),
            file_sha=hashlib.sha256(text.encode()).hexdigest()[:16],
            text=text,
            imports=tuple(imports),
            symbols=tuple(syms),
            dup_module_assign=tuple(dup_names),
        )
