"""`change impact --data-key KEY` -- a data key (or enum value) has no symbol of its own;
its blast radius is the radius of the code that reads it by name.

`find` scans the tracked tree for KEY as a whole word (`mentions.scan`, the `graph grep`
classifier): a `data` hit (YAML/TOML/JSON) is a declaration; a `string` or `code` hit in a
`.py` file is a read, attributed to its innermost enclosing symbol in the snapshot; a read
with no enclosing symbol (a module-level constant) is listed as `unplaced` -- it has no
caller index to walk. Docstrings, comments and prose are not reads.

A JSON pointer (`/properties/role/enum/0`) is read by its last segment that is neither an
array index nor a JSON Schema keyword, and says so in a note.

WHAT IT CANNOT SEE
    * A key built at runtime (`"document_" + kind`, an f-string, a constant imported
      under another name): only the literal spelling is found.
    * Which value of an enum a reader branches on: a reader of the key is a reader of
      every value under it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codegraph._core.query import mentions
from codegraph_change_reader import qualified_index, walk_symbols

_SCHEMA_WORDS = frozenset({
    "properties", "patternProperties", "additionalProperties", "items", "prefixItems",
    "enum", "const", "default", "examples", "required", "definitions", "$defs",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas",
})


def key_of(arg: str) -> "tuple[str, str | None]":
    """The key a `--data-key` argument names, and a note when a pointer was read down."""
    if not arg.startswith("/"):
        return arg, None
    segs = [s.replace("~1", "/").replace("~0", "~") for s in arg.split("/")[1:]]
    pick = next((s for s in reversed(segs) if s and not s.isdigit() and s not in _SCHEMA_WORDS),
                None)
    if pick is None:
        raise ValueError(f"--data-key {arg!r}: the pointer has no named segment to search for")
    return pick, f"JSON pointer {arg!r} read as its last named segment {pick!r}"


def _symbol_of(enclosing: str, symbols: "dict[str, dict]") -> "str | None":
    module, _, qual = enclosing.partition("::")
    parts = qual.split(".")
    for cand in [qual, parts[-1], *(".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1))]:
        if f"{module}::{cand}" in symbols:
            return f"{module}::{cand}"
    return None


def _defines(info: dict, key: str, hit: dict) -> bool:
    """The hit is the symbol's own name on its `def`/`class` line: a definition, not a read."""
    lines = info.get("lines") or [None]
    return (hit["kind"] == "code" and hit["line"] == lines[0]
            and re.match(r"\s*(async\s+def|def|class)\s+%s\b" % re.escape(key),
                         hit.get("snippet") or "") is not None)


def find(root: Path, idx: dict, key: str) -> "dict[str, Any]":
    by_path = {m["path"]: m["module"] for _u, m, _s, _p in walk_symbols(idx)}
    symbols = qualified_index(idx)
    hits, notes = mentions.scan(root, rf"(?<!\w){re.escape(key)}(?!\w)",
                                kinds=("data", "string", "code"),
                                module_of=lambda p: by_path.get(p))
    declared: "list[dict]" = []
    readers: "dict[str, list[str]]" = {}
    unplaced: "list[str]" = []
    for h in hits:
        at = f"{h['file']}:{h['line']}"
        if h["kind"] == "data":
            declared.append({"file": h["file"], "line": h["line"], "context": h.get("context")})
            continue
        if not h["file"].endswith(".py"):
            continue
        sym = _symbol_of(h["enclosing"], symbols) if h.get("enclosing") else None
        if sym is None:
            if at not in unplaced:
                unplaced.append(at)
        elif _defines(symbols[sym], key, h):
            continue
        elif at not in readers.setdefault(sym, []):
            readers[sym].append(at)
    return {"key": key, "declared_in": declared, "readers": dict(sorted(readers.items())),
            "unplaced": unplaced, "notes": notes}


def render(found: "dict[str, Any]") -> "list[str]":
    L = [f"data key {found['key']!r}: declared in {len(found['declared_in'])} place(s), "
         f"read by {len(found['readers'])} symbol(s), {len(found['unplaced'])} read(s) "
         f"outside any symbol"]
    for d in found["declared_in"]:
        L.append(f"  declared  {d['file']}:{d['line']}  {d.get('context') or ''}".rstrip())
    for sym, ats in found["readers"].items():
        L.append(f"  read by   {sym}  ({', '.join(ats)})")
    for at in found["unplaced"]:
        L.append(f"  unplaced  {at}  (no enclosing symbol -- no callers to walk)")
    L.extend(f"  note: {n}" for n in found["notes"])
    return L
