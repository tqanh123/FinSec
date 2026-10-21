"""The docs reference gate: every path a doc under `docs/` names must resolve.

What a path is and which tree it names is `docs_paths`. A doc declared as a source ledger
in the project's `codemap.yaml` (`docs.source_ledgers`) also has its rows checked by
`docs_source_ledger`. Against a snapshot of HEAD, every `module.name` a doc names must be
in it (`docs_symbols`); `--check` finds that snapshot in the project's store, and without
one says so on stderr and judges paths alone. Every decision id a doc cites (each id of a
compound `DEC-<ns>-<a>/<b>` or range `DEC-<ns>-<a>..<b>`) must be in `docs/decisions.yaml` when the
project keeps one (`docs_decs`). Exempt, as history ledgers whose old names are the record:
`EXEMPT`, globs against the root.

CLI: `docs_refs.py --check [--root DIR] [--exempt GLOB ...] [--snap DIR]` prints one
`doc:line: ...` per finding and exits 0 none / 1 some / 2 not a git work tree, a bad
`codemap.yaml`, or a snapshot that is not of HEAD or that the snapshot reader refuses;
`--table` prints per-doc counts by status. The docs workflow (`hs:docs` update,
`docs-manager`) runs `--check` on the user's project.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import codegraph_snapshot_reader
import docs_decs
import docs_source_ledger
import docs_symbols
from docs_paths import Index, Ref, markers, scan_doc
from docs_source_ledger import RowRules

#: history ledgers: their old names are the record, not a reference to repair; globs
#: against the root, replaced as a whole by `--exempt`
EXEMPT: Tuple[str, ...] = ("docs/decisions.md", "CHANGELOG.md")
SCOPE: Tuple[str, ...] = ("docs/**/*.md",)


def _marker_covers(index: Index, sha: str, r: Ref) -> bool:
    """Does `sha` remove the thing `r` names -- an exact path, or (a bare code-span
    token, no `/`) any file of that basename."""
    return r.path in index.removed_names(sha) if "/" not in r.path else index.removed_in(sha, r.path)


def problems(root: Path, scope: Sequence[str] = SCOPE, exempt: Sequence[str] = EXEMPT,
             row_rules: Optional[Dict[str, RowRules]] = None,
             snapshot: Optional[Union[str, Path]] = None) -> List[str]:
    """`doc:line: dead `path`` per dead path, one line per marker that is not a commit or
    removes nothing named on its line, what `row_rules[doc]` finds (by default the
    source ledgers the root's `codemap.yaml` declares), and, given `snapshot` (a snapshot
    directory of HEAD), each dead `module.name` -- the docs reference gate's findings."""
    index = Index(root)
    rules = docs_source_ledger.declared(root) if row_rules is None else row_rules
    docs = docs_in_scope(root, scope, exempt, index)
    out: List[Tuple[str, int, int, str]] = []
    dec_ids = docs_decs.ledger_ids(root)
    if snapshot is not None:
        out += [(d, n, 3, m) for d, n, m in docs_symbols.problems(root, docs, docs_symbols.load(snapshot, root))]
    for doc in docs:
        rows = scan_doc(root, doc, index)
        lines = (Path(root) / doc).read_text(encoding="utf-8", errors="replace").splitlines()
        rule = rules.get(doc)
        skip, used, own = rule(index, doc, lines) if rule else (set(), set(), [])
        out += [(doc, r.line, 0, "dead `%s`" % r.path) for r in rows
                if r.status == "dead" and (r.line, r.token) not in skip]
        for n, line in enumerate(lines, 1):
            for x in markers(line):
                if not index.is_commit(x):
                    out.append((doc, n, 1, "marker %s is not a commit" % x))
                elif (n, x) not in used and not any(
                        r.line == n and r.status != "live" and _marker_covers(index, x, r)
                        for r in rows):
                    out.append((doc, n, 1, "marker %s removes no path named on its line" % x))
        out += [(doc, n, 2, m) for n, m in own]
        if dec_ids is not None:
            out += [(doc, n, 4, m) for n, m in docs_decs.problems(lines, dec_ids)]
    return ["%s:%d: %s" % (d, n, m) for d, n, _, m in sorted(out)]


def exempted(doc: str, exempt: Sequence[str]) -> bool:
    """`exempt` holds globs matched against the root-relative doc path."""
    return any(fnmatch.fnmatchcase(doc, g) for g in exempt)


def _scope_regex(pattern: str) -> "re.Pattern[str]":
    """One `SCOPE` glob, translated to the SAME meaning `git ls-files :(glob)<pattern>`
    gives (proven against a real run -- `test_scope_regex_matches_what_git_glob_matches`):
    `*`/`?` stay inside their own `/`-segment (parity with `:(glob)`'s FNM_PATHNAME --
    plain `fnmatch` lets a bare `*` cross `/`, which is not what git does here), and a
    whole `**` path segment matches zero or more whole segments, INCLUDING none (so
    `docs/**/*.md` matches `docs/decisions.md` too, not only a nested doc). Covers exactly
    the one shape `SCOPE` uses (a literal prefix, at most one `**` segment, a literal
    suffix segment) -- not the full wildmatch grammar (character classes, `**` at either
    end, more than one `**`)."""
    parts = pattern.split("/")
    out = []
    for i, part in enumerate(parts):
        if i > 0 and parts[i - 1] != "**":
            out.append("/")
        if part == "**":
            out.append(r"(?:[^/]+/)*")
        else:
            out.append("".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
                               for c in part))
    return re.compile("^" + "".join(out) + "$")


def docs_in_scope(root: Path, scope: Sequence[str] = SCOPE, exempt: Sequence[str] = EXEMPT,
                   index: Optional[Index] = None) -> List[str]:
    """Every tracked (or would-track) path under `scope`, minus `exempt`. `Index` already
    pays for one full, correctly `-z`-split `ls-files` listing per scan (non-ASCII and
    space-bearing doc paths included) -- filtering it here in Python is the same result a
    second, pathspec-scoped `ls-files` call gave, without asking git for the same tree
    twice. `index=None` (a bare call, as every test here makes) still costs exactly one."""
    files = (index if index is not None else Index(root)).files
    patterns = [_scope_regex(g) for g in scope]
    return sorted(d for d in files if any(p.match(d) for p in patterns)
                  and not exempted(d, exempt))


STATUSES = ("live", "dead", "runtime", "unanchored", "removed")


def table(root: Path, exempt: Sequence[str] = EXEMPT) -> None:
    index = Index(root)
    docs = docs_in_scope(root, exempt=(), index=index)
    total = dict.fromkeys(STATUSES, 0)
    print("%-68s %5s " % ("doc", "refs") + " ".join("%10s" % s for s in STATUSES))
    for doc in docs:
        rows = scan_doc(root, doc, index)
        n = {s: sum(r.status == s for r in rows) for s in STATUSES}
        if not exempted(doc, exempt):
            total = {s: total[s] + n[s] for s in STATUSES}
        print("%-68s %5d " % (doc, len(rows)) + " ".join("%10d" % n[s] for s in STATUSES)
              + ("  (exempt)" if exempted(doc, exempt) else ""))
    print("in scope: %d docs, %d refs; " % (len([d for d in docs if not exempted(d, exempt)]), sum(total.values()))
          + ", ".join("%s %d" % (s, total[s]) for s in STATUSES))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="docs_refs.py", description=__doc__.split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="print one `doc:line: ...` per finding; exit 0 none, 1 some, 2 not a git work tree")
    mode.add_argument("--table", action="store_true", help="per-doc counts by status")
    ap.add_argument("--root", default=".", help="the project whose docs/ to read (default: the cwd's work tree)")
    ap.add_argument("--exempt", action="append", metavar="GLOB",
                    help="a history ledger to skip, relative to --root; repeat it; replaces the default %s"
                         % ", ".join(EXEMPT))
    ap.add_argument("--snap", metavar="DIR",
                    help="the snapshot directory of HEAD to check each `module.name` against "
                         "(default: the project's store's, if it has one)")
    a = ap.parse_args(argv)
    top = subprocess.run(["git", "-C", a.root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        print("docs_refs: %s is not inside a git work tree -- the gate reads what git tracks" % a.root,
              file=sys.stderr)
        return 2
    root = Path(top.stdout.strip())
    exempt = tuple(a.exempt) if a.exempt else EXEMPT
    if a.table:
        table(root, exempt)
        return 0
    snap = a.snap
    if snap is None:
        snap, why = docs_symbols.head_snapshot(root)
        if snap is None:
            print(docs_symbols.NOT_CHECKED % why, file=sys.stderr)
    try:
        found = problems(root, exempt=exempt, snapshot=snap)
    except (docs_source_ledger.LedgerError, docs_symbols.SnapshotMismatch,
            codegraph_snapshot_reader.SnapshotIndexError) as e:
        print("docs_refs: %s" % e, file=sys.stderr)
        return 2
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
