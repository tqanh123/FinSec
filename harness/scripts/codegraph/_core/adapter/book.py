"""The adapter book: one row per reader, declared as data, checked in one place.

Two layers, one row shape:
    * the bin's rows, the `reader:` of each language pack in
      `harness/data/hs-code-languages.yaml` (`lang.registry`, the one reader of that
      book; `adapter.registry.load_bin_rows` turns its packs into rows) -- in-process
      readers, trusted because they ship with the harness;
    * a project's `codemap.yaml` `adapters:` -- a reader for the project's own
      language, always a `command:` (`command_adapter`), run only in a repo the
      operator trusted. An in-process `module:` from a project is refused: it would
      be the project's code imported into this process, trust or not. A row that
      also names a `resolver:` command is a whole language (`lang.resolver_command`):
      its modules are path ids under its own `lang`, so its `id` may not be one of
      the bin's languages.

Every key is checked; an unknown one is an error, not freedom (a mistyped
`extentions` would silently read nothing). A project row may not claim a suffix a
bin row owns: the bin's reader is the one every gate was measured with.

WHAT IT CANNOT SEE
    * Whether a declared command is the reader it claims to be: the book checks the
      row's shape, the command's answer is checked per file (`command_adapter`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# `AdapterError`/`AdapterRefused` MOVED to `lang.errors`, a leaf both this package and
# the language seam import (the seam's own errors subclass them); rebound here.
from ..lang.errors import AdapterError as AdapterError  # noqa: PLC0414 -- re-export
from ..lang.errors import AdapterRefused as AdapterRefused  # noqa: PLC0414 -- re-export

_PROJECT_KEYS = frozenset({"id", "extensions", "command", "resolver"})


@dataclass(frozen=True)
class AdapterRow:
    id: str
    extensions: "tuple[str, ...]"
    origin: str                              # "bin" | "project"
    module: "str | None" = None              # bin rows
    cls: "str | None" = None
    command: "tuple[str, ...]" = ()          # project rows
    #: a project row's resolver command; () keeps the dotted-import reader contract
    resolver: "tuple[str, ...]" = ()


def parse_rows(raw: Any, where: str, origin: str, taken: "Iterable[str]" = (),
               taken_ids: "Iterable[str]" = ()) -> "tuple[AdapterRow, ...]":
    """`raw` (a project's YAML list) as rows of `origin`; `taken` are suffixes another
    layer already owns, `taken_ids` the language ids a row with a `resolver:` may not
    take. Raises `AdapterError` naming `where` and the row."""
    if not isinstance(raw, list):
        raise AdapterError("%s: `adapters:` must be a list of rows" % where)
    keys = _PROJECT_KEYS
    owned, langs = set(taken), set(taken_ids)
    rows = []
    for i, r in enumerate(raw, 1):
        at = "%s: adapters[%d]" % (where, i)
        if not isinstance(r, dict):
            raise AdapterError("%s must be a mapping" % at)
        unknown = sorted(set(r) - keys)
        if {"module", "class"} & set(r):
            raise AdapterError("%s: a project's reader is a `command:` (run only in a trusted "
                               "repo), never an in-process `module:`" % at)
        if unknown:
            raise AdapterError("%s: unknown key(s) %s; valid: %s"
                               % (at, ", ".join(unknown), ", ".join(sorted(keys))))
        rid, exts = r.get("id"), r.get("extensions")
        if not isinstance(rid, str) or not rid:
            raise AdapterError("%s: `id:` must be a non-empty string" % at)
        if (not isinstance(exts, list) or not exts
                or not all(isinstance(e, str) and e.startswith(".") and len(e) > 1 for e in exts)):
            raise AdapterError("%s (%s): `extensions:` must be a non-empty list of suffixes "
                               "like '.py' (a leading dot, the way `Path.suffix` spells one)"
                               % (at, rid))
        norm = tuple(e.lower() for e in exts)
        clash = sorted(set(norm) & owned)
        if clash:
            raise AdapterError("%s (%s): suffix %s already has a reader%s"
                               % (at, rid, ", ".join(clash), " in the bin's book"))
        owned |= set(norm)
        cmd = r.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
            raise AdapterError("%s (%s): `command:` must be a non-empty list of argv strings"
                               % (at, rid))
        res = r.get("resolver")
        if res is not None and (not isinstance(res, list) or not res
                                or not all(isinstance(c, str) and c for c in res)):
            raise AdapterError("%s (%s): `resolver:` must be a non-empty list of argv strings"
                               % (at, rid))
        if res is not None and rid in langs:
            raise AdapterError("%s: id %r is a language of the bin's book; a row with a "
                               "`resolver:` is a language of its own -- give it another id"
                               % (at, rid))
        rows.append(AdapterRow(rid, norm, origin, command=tuple(cmd), resolver=tuple(res or ())))
    return tuple(rows)
