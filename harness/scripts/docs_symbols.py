"""Symbol references in docs, checked against a snapshot of the commit.

A symbol reference is a code span outside fenced blocks whose whole content is a dotted
name, optionally called (`trace_log.append_event`, `store.load_dir()`). Its module is the
longest prefix that is a module of the snapshot, by full name or by a dotted tail of two
or more segments exactly one module ends with; one segment alone would take config keys
(`claims.lease_s`) for modules. A submodule after it is a module reference, not counted;
otherwise its first segment must be a name the module defines or imports at module level.
A file name (`docs_refs.py`) is the path rule's, not this one's.

The snapshot is read through `codegraph_snapshot_reader` (no `codegraph` import) and must
be of the root's HEAD. `head_snapshot` finds it in the project's store the way hs-run's
facts do (`hs_run_facts.engine`); a snapshot of an older commit is never used.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, Tuple, Union

import codegraph_snapshot_reader
from hs_run_facts import engine
from docs_paths import _FENCE, _SPAN

_NAME = re.compile(r"\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(?:\(.*\))?\s*")
#: a last segment that makes the name a file, not `module.symbol`
FILE_EXTS: FrozenSet[str] = frozenset({
    "py", "md", "yaml", "yml", "json", "jsonl", "toml", "sh", "txt", "cfg", "ini", "lock",
    "html", "cmd", "ps1", "js", "mjs", "ts", "csv", "xml"})


class SnapshotMismatch(ValueError):
    """The snapshot is of another commit than the tree it would judge."""


@dataclass(frozen=True)
class SymbolRef:
    doc: str
    line: int
    token: str
    module: str
    name: str
    status: str          # live | dead


class Symbols:
    """module -> the names it defines or imports at module level, from `_index.json`."""

    def __init__(self, index: dict) -> None:
        self.names: Dict[str, Set[str]] = {}
        for mods in index.get("units", {}).values():
            for m in mods:
                self.names[m["module"]] = ({s["name"] for s in m.get("symbols", ())}
                                           | {i.split("::", 1)[1] for i in m.get("imports_module_level", ())
                                              if "::" in i})
        tails = Counter(t for m in self.names for t in self._tails_of(m))
        self._owner = {t: m for m in self.names for t in self._tails_of(m) if tails[t] == 1}

    @staticmethod
    def _tails_of(module: str) -> Iterator[str]:
        """The module's dotted tails of two or more segments."""
        parts = module.split(".")
        return (".".join(parts[k:]) for k in range(1, len(parts) - 1))

    def module_of(self, prefix: str) -> Optional[str]:
        return prefix if prefix in self.names else self._owner.get(prefix)

    def resolve(self, token: str) -> Optional[Tuple[str, str]]:
        """(module, first name after it), or None when `token` names no symbol."""
        parts = token.split(".")
        for k in range(len(parts) - 1, 0, -1):
            module = self.module_of(".".join(parts[:k]))
            if module is not None:
                return None if module + "." + parts[k] in self.names else (module, parts[k])
        return None

    def has(self, module: str, name: str) -> bool:
        return name in self.names[module]


#: what `docs_refs.py --check` says when it has no snapshot to judge symbols against
NOT_CHECKED = "symbols: not checked -- no snapshot of HEAD (%s); run hs-code --apply graph snapshot"


def _head(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def head_snapshot(root: Path) -> Tuple[Optional[Path], str]:
    """(the store's snapshot of `root`'s HEAD, "") or (None, why there is none)."""
    try:
        base = engine.store_base(root)
    except engine.DataHomeUnresolved:
        return None, "the snapshot store's data home is unresolved"
    if base is None:
        return None, "no `project:` in codemap.yaml"
    picked = engine.select_snapshot(root, base)
    if picked.status == "fresh" and picked.index.get("commit") == _head(root):
        return picked.dir, ""
    return None, "none under %s" % base


def load(snapshot_dir: Union[str, Path], root: Path) -> Symbols:
    """The snapshot's symbols; refused unless it is of `root`'s HEAD."""
    index = codegraph_snapshot_reader.read_index(snapshot_dir)
    head = _head(root)
    if index.get("commit") != head:
        raise SnapshotMismatch("snapshot %s is of %s, the tree is at %s -- snapshot HEAD first"
                               % (snapshot_dir, str(index.get("commit"))[:7], head[:7]))
    return Symbols(index)


def refs_in(text: str) -> Iterator[Tuple[int, str]]:
    """(line, dotted name) for every code span that is only a dotted name; fences skipped."""
    fence: Optional[str] = None
    for n, line in enumerate(text.splitlines(), 1):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)[0] * 3
            continue
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
            continue
        for span in _SPAN.finditer(line):
            whole = _NAME.fullmatch(span.group(1))
            if whole and whole.group(1).rsplit(".", 1)[1] not in FILE_EXTS:
                yield n, whole.group(1)


def scan_doc(root: Path, doc: str, symbols: Symbols) -> List[SymbolRef]:
    out = []
    for n, tok in refs_in((Path(root) / doc).read_text(encoding="utf-8", errors="replace")):
        hit = symbols.resolve(tok)
        if hit is not None:
            out.append(SymbolRef(doc, n, tok, hit[0], hit[1], "live" if symbols.has(*hit) else "dead"))
    return out


def problems(root: Path, docs: Sequence[str], symbols: Symbols) -> List[Tuple[str, int, str]]:
    return [(r.doc, r.line, "dead symbol `%s`: `%s` has no `%s`" % (r.token, r.module, r.name))
            for d in docs for r in scan_doc(root, d, symbols) if r.status == "dead"]
