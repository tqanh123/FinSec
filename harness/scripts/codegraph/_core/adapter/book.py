"""The adapter book: one row per reader, declared as data, checked in one place.

Two layers, one row shape:
    * the bin's book, `harness/data/hs-code-adapters.yaml` -- in-process readers
      (`module:` + `class:`), trusted because they ship with the harness;
    * a project's `codemap.yaml` `adapters:` -- a reader for the project's own
      language, always a `command:` (`command_adapter`), run only in a repo the
      operator trusted. An in-process `module:` from a project is refused: it would
      be the project's code imported into this process, trust or not.

Every key is checked; an unknown one is an error, not freedom (a mistyped
`extentions` would silently read nothing). A project row may not claim a suffix a
bin row owns: the bin's reader is the one every gate was measured with.

WHAT IT CANNOT SEE
    * Whether a declared command is the reader it claims to be: the book checks the
      row's shape, the command's answer is checked per file (`command_adapter`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: harness/scripts/codegraph/_core/adapter/book.py -> harness/data/
BIN_BOOK = Path(__file__).resolve().parents[4] / "data" / "hs-code-adapters.yaml"

_BIN_KEYS = frozenset({"id", "extensions", "module", "class"})
_PROJECT_KEYS = frozenset({"id", "extensions", "command"})


class AdapterError(Exception):
    """A book row, or a reader's answer, is outside the contract."""


class AdapterRefused(AdapterError):
    """A reader could run but may not: a project's command in an untrusted repo."""


@dataclass(frozen=True)
class AdapterRow:
    id: str
    extensions: "tuple[str, ...]"
    origin: str                              # "bin" | "project"
    module: "str | None" = None              # bin rows
    cls: "str | None" = None
    command: "tuple[str, ...]" = ()          # project rows


def parse_rows(raw: Any, where: str, origin: str,
               taken: "Iterable[str]" = ()) -> "tuple[AdapterRow, ...]":
    """`raw` (a YAML list) as rows of `origin`; `taken` are suffixes another layer
    already owns. Raises `AdapterError` naming `where` and the row."""
    if not isinstance(raw, list):
        raise AdapterError("%s: `adapters:` must be a list of rows" % where)
    keys = _BIN_KEYS if origin == "bin" else _PROJECT_KEYS
    owned = set(taken)
    rows = []
    for i, r in enumerate(raw, 1):
        at = "%s: adapters[%d]" % (where, i)
        if not isinstance(r, dict):
            raise AdapterError("%s must be a mapping" % at)
        unknown = sorted(set(r) - keys)
        if origin == "project" and ({"module", "class"} & set(r)):
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
                               % (at, rid, ", ".join(clash),
                                  " in the bin's book" if origin == "project" else ""))
        owned |= set(norm)
        if origin == "bin":
            mod, cls = r.get("module"), r.get("class")
            if not (isinstance(mod, str) and mod and isinstance(cls, str) and cls):
                raise AdapterError("%s (%s): a bin row needs `module:` and `class:`" % (at, rid))
            rows.append(AdapterRow(rid, norm, origin, module=mod, cls=cls))
        else:
            cmd = r.get("command")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
                raise AdapterError("%s (%s): `command:` must be a non-empty list of argv strings"
                                   % (at, rid))
            rows.append(AdapterRow(rid, norm, origin, command=tuple(cmd)))
    return tuple(rows)


def load_book(path: Path = BIN_BOOK) -> "tuple[AdapterRow, ...]":
    """The bin's rows. A missing, unreadable or row-less book is broken."""
    import yaml_io
    try:
        doc = yaml_io.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml_io.YAMLError) as e:
        raise AdapterError("adapter book %s unreadable: %s" % (path, e)) from e
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise AdapterError("adapter book %s: needs `schema_version: 1` and `adapters:`" % path)
    unknown = sorted(set(doc) - {"schema_version", "adapters"})
    if unknown:
        raise AdapterError("adapter book %s: unknown key(s) %s" % (path, ", ".join(unknown)))
    rows = parse_rows(doc.get("adapters"), str(path), "bin")
    if not rows:
        raise AdapterError("adapter book %s declares no adapter: nothing could be read" % path)
    return rows
