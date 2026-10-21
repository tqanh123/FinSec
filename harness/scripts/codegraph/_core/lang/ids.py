"""Module identity across packs: what an id is, and the invariants every id keeps.

A module id is a pack's opaque key. Python keeps dotted names (`pkg.mod`). A pack
whose modules are files of another language names each by its repo-relative path
WITH the extension (`packages/kit/src/a.ts`), and its record carries `lang`.

THE PATH-ID INVARIANT
    No strict prefix of a path id is ever a module. A dotted reader walks prefixes
    (`pkg.mod.Class.attr` -> `pkg.mod`); on `packages/kit/src/a.ts` that walk finds
    `packages/kit/src/a` -- a broken `./a` import silently attributed to a same-stem
    file. So every prefix walk answers a path id exactly, and a path id is never
    `<module>.<attr>`: its symbols are `<id>::<name>`.

WHAT IT CANNOT SEE
    * A string alone: `is_path_id` reads a slash, or a suffix a non-Python pack
      owns. A top-level path id (no `/`) that is not a module walks its dotted
      prefixes like any dotted name (`lookup_keys`). A top-level `index.ts` and a Python module `index.ts` (`index/ts.py`)
      spell the same; a tree holding both is refused by `check_ids`.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .base import LanguageError, SourceRef

PYTHON = "python"


def lang_of(record: "dict[str, Any]") -> str:
    """The pack that named `record` (a Python record carries no `lang`)."""
    return str(record.get("lang") or PYTHON)


_memo: "tuple[Any, frozenset[str]]" = (None, frozenset())


def _path_suffixes() -> "frozenset[str]":
    """Suffixes the non-Python packs own -- asked once per edge of a graph walk, so
    kept until the book's packs tuple is another object."""
    global _memo
    from . import registry
    packs = registry.packs()
    if _memo[0] is not packs:
        _memo = (packs, frozenset(s for p in packs if p.id != registry.DEFAULT_ID
                                  for s in p.suffixes()))
    return _memo[1]


def is_path_id(module: str, suffixes: "Iterable[str] | None" = None) -> bool:
    """`module` (a `::symbol` tail allowed) is a path id: it holds a `/`, or ends in a
    suffix a non-Python pack owns. A dotted Python name never holds a `/`."""
    base = module.split("::", 1)[0]
    if "/" in base:
        return True
    sfx = PurePosixPath(base).suffix.lower()
    return bool(sfx) and sfx in (frozenset(suffixes) if suffixes is not None else _path_suffixes())


def check_ids(refs: "Iterable[SourceRef]", repo: Path) -> None:
    """Refuse an id two files share across packs, an id holding `::`, and a path id
    that is not its own repo-relative path."""
    seen: "dict[str, SourceRef]" = {}
    for r in refs:
        rel = r.path.relative_to(repo).as_posix()
        if "::" in r.module:
            raise LanguageError("module id %r (%s, %s) holds `::`, the symbol separator"
                                % (r.module, r.lang, rel))
        if r.lang != PYTHON and r.module != rel:
            raise LanguageError("module id %r of %s file %s is not its path" % (r.module, r.lang, rel))
        prev = seen.get(r.module)
        if prev is not None:
            raise LanguageError(
                "module id %r names two files: %s %s and %s %s -- an id is one file's"
                % (r.module, prev.lang, prev.path.relative_to(repo).as_posix(), r.lang, rel))
        seen[r.module] = r


def lookup_keys(target: str, lang: "str | None" = None) -> "list[str]":
    """The module ids `target` may name, longest first: a dotted target (with
    trailing attribute segments, or a `::symbol` tail) walks its dotted prefixes; a
    path id holding a `/` is itself alone -- the path-id invariant, in the one place
    every prefix walk reads it. A string with no `/` walks too, its whole self first: a
    top-level `index.ts` is matched exactly before anything shorter, and a dotted
    `app.clock.ts` (`mock.patch` of an attribute named `ts`) still reaches `app.clock`.
    `lang`, the language of the record the target was read from: another pack's resolver
    named its targets as module ids, so they are matched whole."""
    base = target.split("::", 1)[0]
    if "/" in base or (lang is not None and lang != PYTHON):
        return [base]
    parts = base.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
