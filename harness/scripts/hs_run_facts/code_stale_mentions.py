"""hs_run_facts.code_stale_mentions — the `code.stale_mentions` fact provider.

`provide(ctx) -> {value, basis, warning}`: names the diff REMOVED -- defined in
a changed `.py` file at the merge-base, defined nowhere now -- and where each is
still written (`code_mentions.mention_table`, every kind listed: no live symbol
answers for a removed name, so even a `code` hit is a broken reference).
A docstring that outlives its function, a config key naming a dead class: the
two are invisible to every graph fact, which only knows what still exists.

Subjects are the state's default scope (changed files vs the merge-base, plus
dirty ones). A name still defined anywhere in the snapshot was MOVED, not
removed, and is left out.
"""
from __future__ import annotations

import ast
from pathlib import Path

import codegraph_change_reader
from hs_run_facts import code_mentions, engine

_BASIS = (
    "names a changed .py file defined at the merge-base and no file defines now "
    "(top-level def/class/assignment and methods, by bare name), then "
    "code.mentions' whole-word scan over every git-tracked file -- every kind listed"
)

_WARNING = (
    "code.stale_mentions compares definitions by bare name: a name removed here "
    "but still defined elsewhere is treated as moved and not reported"
)


def _names(text: str) -> "set[str]":
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out: "set[str]" = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
            if isinstance(node, ast.ClassDef):
                out.update(n.name for n in node.body
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            out.update(t.id for t in targets if isinstance(t, ast.Name))
    return {n for n in out if not (n.startswith("__") and n.endswith("__"))}


def _merge_base(root: Path) -> str:
    import review_recall  # noqa: PLC0415 -- the default-branch resolver the engine already uses
    branch = review_recall._main_branch(str(root))
    return engine._git_text(root, "merge-base", branch, "HEAD") if branch else ""


def removed_names(root: Path, idx: dict, paths) -> "set[str]":
    base = _merge_base(root)
    if not base:
        return set()
    alive = {q.partition("::")[2].rsplit(".", 1)[-1]
             for q in codegraph_change_reader.qualified_index(idx)}
    removed: "set[str]" = set()
    for rel in sorted(p for p in paths if p.endswith(".py")):
        before = engine._git_text(root, "show", "%s:%s" % (base, rel))
        if not before:
            continue
        try:
            now = (Path(root) / rel).read_text(encoding="utf-8")
        except OSError:
            now = ""
        removed |= _names(before) - _names(now)
    return removed - alive


def provide(ctx: dict) -> dict:
    idx = ctx["snapshot"]
    root = Path(ctx["root"])
    names = removed_names(root, idx, ctx.get("subjects") or ())
    value = code_mentions.mention_table(root, idx, {n: (n, False) for n in names})
    return {"value": value, "basis": _BASIS, "warning": _WARNING}
