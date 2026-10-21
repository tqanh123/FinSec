"""Which decision ids a doc cites, and are they in the project's ledger.

A citation is `DEC-<namespace>-<n>` optionally followed by more ids of the SAME
namespace, as bare numbers: `DEC-<ns>-<a>/<b>` names a and b, `DEC-<ns>-<a>..<b>`
names both ends of the range. The namespace is the shortest letter-led run that
leaves a number after it, so an actor slug that itself ends in digits stays whole.
A flat legacy `DEC-<n>` carries no namespace and is not a citation here.

The ledger is `docs/decisions.yaml` (the register's SSOT); a project without one is
not checked. The docs reference gate (`docs_refs.py`) reports each cited id that is
missing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

import yaml_io

LEDGER = Path("docs") / "decisions.yaml"
_CITE = re.compile(r"\bDEC-([a-z][a-z0-9-]*?)-(\d+)((?:(?:/|\.\.)\d+)*)")
_MORE = re.compile(r"(?:/|\.\.)(\d+)")


def ledger_ids(root: Path) -> Optional[Set[str]]:
    """Every id in the ledger, or None when the project keeps no YAML ledger."""
    path = Path(root) / LEDGER
    if not path.is_file():
        return None
    records = yaml_io.yaml_load(path.read_text(encoding="utf-8")) or []
    return {str(r["id"]) for r in records if isinstance(r, dict) and r.get("id")}


def citations(line: str) -> Iterator[Tuple[str, str]]:
    """(cited id, the citation as written) for every id a line names."""
    for m in _CITE.finditer(line):
        for n in [m.group(2)] + _MORE.findall(m.group(3)):
            yield "DEC-%s-%s" % (m.group(1), n), m.group(0)


def problems(lines: List[str], ids: Set[str]) -> List[Tuple[int, str]]:
    """(line, message) per cited id missing from `ids`."""
    return [(n, "%s (cited as `%s`) is not in %s" % (dec, written, LEDGER.as_posix()))
            for n, line in enumerate(lines, 1)
            for dec, written in citations(line) if dec not in ids]
