"""port_slicer.py — symbol slicing for `hs-code port build`'s slice pass, as
one FLAT module under `harness/scripts/` (matching every sibling `port_*.py`:
`hs-run` resolves a module by a bare, dot-free name).

Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
portkit/build/slicer.py` (`slice_file`/`prune_all_list`/
`stdlib_imports_needed`) — no shape change, this engine is pure AST/text and
carries no dist-layout or namespace assumption to replace. Reached from
`codegraph_port_build_verbs.build_dist`'s slice pass (`spec.slice_sources`),
the same closed-set-extension precedent `port_spec.py`'s own docstring
already names for `build`.

WHAT IT CANNOT SEE
    * Slicing follows the snapshot's `lines`: a comment-only change glued
      above a definition rides along with its block, but a comment above a
      BLANK line is left behind — same measurement rule as the snapshot
      (body sha strips comments).
    * `__all__` pruning touches EXACTLY one string-list assignment; any other
      shape of `__all__` passes through untouched.
    * `stdlib_imports_needed` measures names actually used; typing/dataclass
      names are matched BY NAME (a declared heuristic — a missing name fails
      at import time, loudly, never silently).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple


def slice_file(path: Path, keep_names: "Set[str]", lines_of: "Dict[str, Tuple[int, int]]",
               dst: Path, problems: "List[str]") -> "Set[str]":
    """Keep symbols by line span (decorators/comments glued above ride along);
    write the merged body to `dst` and return the names actually present in
    the emitted body — a caller compares this against `keep_names` to catch a
    span the emitted body did not actually carry."""
    del problems  # matching upstream's own unused parameter (kept for signature parity)
    src = path.read_text(encoding="utf-8").splitlines(keepends=True)
    blocks: "List[Tuple[int, int]]" = []
    for name in sorted(keep_names, key=lambda n: lines_of[n]):
        a, b = lines_of[name]
        s = a - 1
        while s - 1 >= 0 and src[s - 1].strip().startswith(("@", "#")):
            s -= 1
        blocks.append((s, b))
    merged: "List[List[int]]" = []
    for a, b in sorted(blocks):
        if merged and a <= merged[-1][1] + 2:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    body = "".join("".join(src[a:b]) + "\n" for a, b in merged)
    dst.write_text(body, encoding="utf-8")
    found: "Set[str]" = set()
    for node in ast.parse(body).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
    return found


def prune_all_list(body: str, fname: str, problems: "List[str]") -> str:
    """A slice's `__all__` must drop names the file does not define (a parent
    symbol staying behind). Mechanical and safe: touches EXACTLY one
    string-list assignment; dropped names are announced — keeping an
    `__all__` pointing at absent names is an ImportError waiting at the
    package door. `problems` is accepted (matching upstream's own signature,
    an announce-only channel — the plan lines carry the note here) but never
    appended to: a dropped name is a note, not a build finding."""
    del problems
    m = re.search(r"^__all__\s*=\s*\[(.*?)\]", body, re.S | re.M)
    if not m:
        return body
    names = re.findall(r'"([^"]+)"', m.group(1))
    defined = set()
    for node in ast.parse(body).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    dropped = [n for n in names if n not in defined]
    if not dropped:
        return body
    keep = [n for n in names if n in defined]
    print("note: %s: __all__ drops %s (absent from the sliced file)" % (fname, dropped))
    return (body[:m.start()] + "__all__ = [\n    "
           + ",\n    ".join('"%s"' % n for n in keep) + ",\n]" + body[m.end():])


def stdlib_imports_needed(body: str) -> "List[str]":
    """Stdlib import lines for a slice body — measured from names actually
    used.

    Two groups: (1) modules used AS A PREFIX (`os.getenv` -> `import os`);
    (2) common typing/dataclass names — `from typing import Optional` leaves
    no module name to find, so they are matched BY NAME. Qualified uses
    (`dataclasses.field`, `typing.cast`, `datetime.datetime.now`) put the
    MODULE name into `used`, so the plain `import <module>` covers them —
    missing one dies at runtime (py_compile cannot catch NameError)."""
    tree = ast.parse(body)
    used = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            used.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            used.add(n.value.id)
    lines = ["from __future__ import annotations\n"]
    plain = sorted(x for x in used if x in {
        "re", "json", "math", "random", "hashlib", "logging", "os", "sys",
        "subprocess", "time", "uuid", "threading", "asyncio", "sqlite3",
        "dataclasses", "typing", "enum", "pathlib", "datetime",
        "collections", "functools", "itertools", "contextlib", "contextvars"})
    if plain:
        lines.append("import " + ", ".join(plain) + "\n")
    typing_names = sorted(used & {
        "Optional", "List", "Dict", "Any", "Tuple", "Set", "FrozenSet", "Iterable",
        "Callable", "Sequence", "Mapping", "Awaitable", "AsyncIterator", "Iterator",
        "Protocol", "TypeVar", "runtime_checkable", "TYPE_CHECKING", "Literal",
        "Annotated", "Union", "Self", "ClassVar", "Generic", "cast", "final"})
    if typing_names:
        lines.append("from typing import " + ", ".join(typing_names) + "\n")
    if used & {"dataclass", "field", "asdict"}:
        lines.append("from dataclasses import "
                     + ", ".join(sorted(used & {"dataclass", "field", "asdict"})) + "\n")
    if used & {"dt"}:
        lines.append("import datetime as dt\n")
    if used & {"Enum", "auto"}:
        lines.append("from enum import " + ", ".join(sorted(used & {"Enum", "auto"})) + "\n")
    if used & {"Path"}:
        lines.append("from pathlib import Path\n")
    return lines if len(lines) > 1 else []
