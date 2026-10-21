"""Source ledgers: doc tables whose rows each say where a borrowed piece came from.

A project declares them in its `codemap.yaml`:

    docs:
      source_ledgers:
      - path: docs/STANDARDIZE.md
        repos: [PS, ck, github]        # the repo names a row may cite

Each table row's Nguồn/Source cell opens with a kind tag from `KINDS` (or
`[đã gỡ ở commit X]`) and is checked by it; see
`harness/tests/test_standardize_rows_declare_their_source.py`. Evidence cells name test
files bare (`test_x.py::T`), so a bare `test_*.py` name must be a tracked file's name or
one a commit on the row removed. Nothing declared: no row is checked.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Sequence, Set, Tuple

from codegraph._core.descriptor.store import DescriptorError, load
from docs_paths import _FILE, _SPAN, _tokens, classify, is_path, markers, refs_in, Index

#: closed list; a new kind is one more entry (plus its rule in `row_problems`)
KINDS: Tuple[str, ...] = ("trong repo", "repo ngoài", "mẫu repo ngoài", "thiết kế mới")
REMOVED = "đã gỡ ở commit"

#: (index, doc, lines) -> (paths not to look for, (line, marker) pairs the rules used,
#: (line, finding) pairs)
RowRules = Callable[[Index, str, Sequence[str]],
                    Tuple[Set[Tuple[int, str]], Set[Tuple[int, str]], List[Tuple[int, str]]]]


class LedgerError(Exception):
    """The project's `codemap.yaml` cannot be read: the gate cannot know its ledgers."""


def declared(root: Path) -> Dict[str, RowRules]:
    """{doc: its row rules} for each source ledger the root's `codemap.yaml` declares."""
    path = Path(root) / "codemap.yaml"
    if not path.is_file():
        return {}
    try:
        ledgers = load(path).descriptor.source_ledgers
    except DescriptorError as e:
        raise LedgerError(str(e)) from None

    def rules(repos: Tuple[str, ...]) -> RowRules:
        return lambda index, doc, lines: row_problems(index, doc, lines, repos)
    return {led["path"]: rules(led["repos"]) for led in ledgers}


_TAG = re.compile(r"^\[([^\]]+)\]")
_REMOVED_TAG = re.compile(r"^đã gỡ ở commit\s+([0-9a-f]{7,40})$")
_SEP = re.compile(r"^\|\s*:?-")
_BARE_TEST = re.compile(r"(?<![\w/.-])(test_\w+\.py)\b")
_HOW = ("open the Nguồn cell with [trong repo], [repo ngoài], [mẫu repo ngoài], [đã gỡ ở commit <sha>] "
        "or [thiết kế mới]")


def cells(line: str) -> List[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def table_rows(lines: Sequence[str]) -> Iterator[Tuple[int, List[str], List[str]]]:
    """(line number, header cells, row cells) for every table body row."""
    head: List[str] = []
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            head = [] if line.strip() else head
            continue
        if i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            head = cells(line)
        elif head and not _SEP.match(line):
            yield i + 1, head, cells(line)


def source_cell(head: List[str], row: List[str]) -> Tuple[bool, str]:
    col = next((j for j, h in enumerate(head) if h.startswith(("Nguồn", "Source"))), None)
    if col is None:
        return False, ""
    return True, row[col] if col < len(row) else ""


def kind_of(cell: str) -> str:
    """The declared kind, `REMOVED` for a removal tag, `""` for none, else the raw tag."""
    m = _TAG.match(cell)
    if not m:
        return ""
    return REMOVED if _REMOVED_TAG.match(m.group(1)) else m.group(1)


def path_shaped(cell: str) -> List[str]:
    """Tokens that name a file or directory in SOME tree: what the docs rule calls a path,
    plus a bare `name.ext` in a code span and a `dir/` in prose. `VL-6/17` is not one."""
    out = []
    for m in _SPAN.finditer(cell):
        out += [t for t in _tokens(m.group(1)) if is_path(t, allow_dir=True) or _FILE.match(t)]
    out += [t for t in _tokens(_SPAN.sub(" ", cell)) if is_path(t, allow_dir=True)]
    return out


def _names_repo(text: str, repos: Sequence[str]) -> bool:
    return any(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(r), text) for r in repos)


def row_problems(index: Index, doc: str, lines: Sequence[str], repos: Sequence[str]
                 ) -> Tuple[Set[Tuple[int, str]], Set[Tuple[int, str]], List[Tuple[int, str]]]:
    """(paths not to look for, markers a bare name used, findings) over the table rows;
    `repos` are the names a row may cite."""
    skip: Set[Tuple[int, str]] = set()
    used: Set[Tuple[int, str]] = set()
    out: List[Tuple[int, str]] = []
    names = {f.rsplit("/", 1)[-1] for f in index.files}
    for n, head, row in table_rows(lines):
        has_col, cell = source_cell(head, row)
        if not has_col:
            out.append((n, "the table has no Nguồn/Source column to declare a source kind in"))
            continue
        kind = kind_of(cell)
        body = _TAG.sub("", cell)
        toks = [t for _, _, t in refs_in(body)]
        shaped = path_shaped(body)
        if not kind:
            out.append((n, "no source kind -- " + _HOW))
        elif kind == "repo ngoài":
            skip.update((n, t) for t in toks)
            if not (shaped and _names_repo(body, repos)):
                out.append((n, "[repo ngoài] needs `<repo> <path>`, <repo> one of: " + ", ".join(repos)))
        elif kind == "trong repo":
            if not toks:
                out.append((n, "[trong repo] names no path"))
        elif kind == "mẫu repo ngoài":
            if shaped:
                out.append((n, "[mẫu repo ngoài] names a path (%s) -- declare [repo ngoài]" % shaped[0]))
            elif not _names_repo(body, repos):
                out.append((n, "[mẫu repo ngoài] names no repo, one of: " + ", ".join(repos)))
        elif kind == "thiết kế mới":
            if shaped:
                out.append((n, "[thiết kế mới] names a path (%s) -- declare [trong repo], [repo ngoài] "
                               "or [đã gỡ ở commit <sha>]" % shaped[0]))
            elif _names_repo(body, repos):
                out.append((n, "[thiết kế mới] names a repo -- a pattern from it is [mẫu repo ngoài]"))
        elif kind == REMOVED:
            sha = _REMOVED_TAG.match(_TAG.match(cell).group(1)).group(1)  # type: ignore[union-attr]
            if index.is_commit(sha) and not any(
                    index.removed_in(sha, classify(index, doc, t, "code")[0]) for t in toks):
                out.append((n, "[%s %s] removes no path in its source cell" % (REMOVED, sha)))
        else:
            out.append((n, "unknown source kind [%s] -- %s" % (kind, _HOW)))
    for n, line in enumerate(lines, 1):             # notes under a table cite tests too
        for name in _BARE_TEST.findall(line):
            if name in names:
                continue
            by = [x for x in markers(line) if name in index.removed_names(x)]
            used.update((n, x) for x in by)
            if not by:
                out.append((n, "dead test name `%s`" % name))
    return skip, used, out
