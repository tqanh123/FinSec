"""rename_units -- what git recorded as renamed on one line of history, as units a stale-
reference check walks; stale_pairs -- the (unit, file) pairs whose file still names the
unit's old path.

LIVING NAMES
    A unit whose old path exists again at the scanned tree is skipped: the path is a name
    in use, and a mention of it is right. So is one living under a caller's `also_under`
    prefix: the same spelling, read the way that tree spells its paths.

UNITS
    `git log --first-parent -m -M --diff-filter=R` over `base..head` (a merge is diffed
    against its first parent). Each old -> new pair drops the longest common trailing run
    of path components; what is left is a prefix pair. A prefix pair that carries >= 2
    files of one commit AND whose old directory is gone after that commit
    (`git ls-tree -d`) is ONE directory unit; everything else is a unit per file. Without
    the "gone" condition a prefix shrinks to a still-living parent (`harness`) and every
    mention of that parent reads as stale. Renames touching `exclude` (default `plans/`,
    a ledger whose moves are its own business) are not units.

MATCHING
    `oldrefs`' token rule over every scanned file read once: a file unit matches a token
    EQUAL to its old path; a directory unit a token equal to it or under it, and neither
    its new path nor under that. Full paths only, never a stem. A file never mentions
    itself (its own path, or a path under a directory unit's old name).

WHAT IT CANNOT SEE
    * A rename inside a file's content (a key, an artifact name): not a git R event.
    * A rename made and undone inside one branch before it merged.
    * Everything `oldrefs` cannot see (a path built at runtime, an untracked file at `at`).
"""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import subprocess

import attrs

import secret_scan_before_ship as secret_scan
from codegraph._core.git import run_git

from . import oldrefs

@attrs.frozen
class RenameUnit:
    old: str
    new: str
    kind: str                  # "file" | "dir"
    commit: str


@attrs.frozen
class StalePair:
    old: str
    new: str
    path: str
    lines: Tuple[int, ...]


def _renames(repo: Path, base: Optional[str], head: str) -> "List[Tuple[str, str, str]]":
    rev = "%s..%s" % (base, head) if base else head
    raw = run_git(repo, "-c", "core.quotePath=false", "log", rev, "--first-parent", "-m", "-M",
                  "--diff-filter=R", "--name-status", "--format=C %H")
    out, commit = [], ""
    for line in raw.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("C "):
            commit = line[2:].strip()
        elif line.startswith("R"):
            _, old, new = line.split("\t")
            out.append((commit, old, new))
    return out


def _prefixes(old: str, new: str) -> "Tuple[str, str]":
    o, n = old.split("/"), new.split("/")
    k = 0
    while k < min(len(o), len(n)) and o[-1 - k] == n[-1 - k]:
        k += 1
    return ("/".join(o[:len(o) - k]), "/".join(n[:len(n) - k])) if k else ("", "")


def _living_dirs(repo: Path, commit: str, dirs: Iterable[str]) -> "set[str]":
    """One `cat-file -t` per directory: `ls-tree -d` given several pathspecs, one inside
    another, lists the outer one's children instead of the outer one itself."""
    return {d for d in set(dirs)
            if run_git(repo, "cat-file", "-t", "%s:%s" % (commit, d), check=False).strip() == b"tree"}


def rename_units(repo: Path, base: Optional[str] = None, head: str = "HEAD",
                 exclude: Sequence[str] = ("plans/",)) -> "Tuple[RenameUnit, ...]":
    """Every rename unit on `head`'s first-parent line after `base` (from the root when
    `base` is None), in history order."""
    repo = Path(repo)
    by_commit: "Dict[str, List[Tuple[str, str]]]" = defaultdict(list)
    order: List[str] = []
    for commit, old, new in _renames(repo, base, head):
        if any(old.startswith(x) or new.startswith(x) for x in exclude):
            continue
        if commit not in by_commit:
            order.append(commit)
        by_commit[commit].append((old, new))
    units: List[RenameUnit] = []
    for commit in reversed(order):                  # git log is newest first
        groups: "Dict[Tuple[str, str], List[Tuple[str, str]]]" = defaultdict(list)
        for old, new in by_commit[commit]:
            groups[_prefixes(old, new)].append((old, new))
        many = [p for p, files in groups.items() if p[0] and len(files) >= 2]
        alive = _living_dirs(repo, commit, (p[0] for p in many))
        for pair, files in groups.items():
            if pair in many and pair[0] not in alive:
                units.append(RenameUnit(pair[0], pair[1], "dir", commit))
            else:
                units.extend(RenameUnit(o, n, "file", commit) for o, n in files)
    return tuple(units)


def stale_pairs(repo: Path, units: Sequence[RenameUnit], *, at: Optional[str] = None,
                skip: Sequence[str] = (), also_under: Sequence[str] = (),
                texts: "Optional[Dict[str, str]]" = None,
                paths: "Optional[Sequence[str]]" = None) -> "List[StalePair]":
    """Every (unit, file) whose file still names the unit's old path, at `at` (the
    working tree when None), files matching a `skip` glob left out. One pair per distinct
    old path and file. `texts` ({path: text}) stands in for those files' contents (a
    planned edit, a seeded line), as in `oldrefs.old_mentions`; `paths` scans only those
    files instead of the whole tree. An old path that names a
    living file or directory at `at` (a rename undone later, a name reused) is a name,
    not a stale reference -- and so is one that is living read under an `also_under`
    prefix (a tree that spells its paths relative to `docs/`)."""
    repo = Path(repo)
    tracked = oldrefs._tracked(repo, at)
    living = set(tracked) | {p.rsplit("/", k)[0] for p in tracked for k in range(1, p.count("/") + 1)}
    files: "Dict[str, set]" = defaultdict(set)
    dirs: "Dict[str, set]" = defaultdict(set)
    for u in units:
        if u.old not in living and not any(root + u.old in living for root in also_under):
            (dirs if u.kind == "dir" else files)[u.old].add(u.new)
    hits: "Dict[Tuple[str, str], List[int]]" = defaultdict(list)
    news: "Dict[str, set]" = {**files, **dirs}
    scan = [p for p in (tracked if paths is None else paths)
            if not any(fnmatch.fnmatchcase(p, g) for g in skip)]
    found = dict(_contents(repo, at, [p for p in scan if p not in (texts or {})]))
    found.update((p, s) for p, s in (texts or {}).items()
                 if not any(fnmatch.fnmatchcase(p, g) for g in skip))
    for path, body in found.items():
        for n, text in enumerate(body.split("\n"), 1):
            for old in _named(text, files, dirs):
                if path == old or path.startswith(old + "/"):
                    continue
                if n not in hits[(old, path)]:
                    hits[(old, path)].append(n)
    return [StalePair(old, sorted(news[old])[-1], path, tuple(sorted(ns)))
            for (old, path), ns in sorted(hits.items())]


def _contents(repo: Path, at: Optional[str], paths: Sequence[str]) -> "Iterable[Tuple[str, str]]":
    """(path, text) of each non-binary file: read from the working tree, or through ONE
    `git cat-file --batch` at `at` -- reading every file once and looking its tokens up is
    ~40x faster here than a `git grep` per batch of ~10k fixed needles."""
    if at is None:
        for p in paths:
            try:
                data = (repo / p).read_bytes()
            except OSError:
                continue
            if b"\0" not in data[:8000]:
                yield p, data.decode("utf-8", "replace")
        return
    r = subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "cat-file", "--batch"],
                       input="".join("%s:%s\n" % (at, p) for p in paths).encode("utf-8", "surrogateescape"),
                       capture_output=True, env=secret_scan.git_safe_env(), check=True)
    out, i = r.stdout, 0
    for p in paths:
        j = out.index(b"\n", i)
        head = out[i:j].split()
        i = j + 1
        if len(head) < 3 or head[1] != b"blob":
            continue
        size = int(head[2])
        data, i = out[i:i + size], i + size + 1
        if b"\0" not in data[:8000]:
            yield p, data.decode("utf-8", "replace")


def _named(text: str, files: "Dict[str, set]", dirs: "Dict[str, set]") -> "set[str]":
    out: set = set()
    for raw in oldrefs._TOKEN.findall(text):
        t = (raw[2:] if raw.startswith("./") else raw).rstrip(".,")
        if t in files:
            out.add(t)
        parts = t.split("/")
        for k in range(1, len(parts) + 1):
            p = "/".join(parts[:k])
            if p in dirs and not any(t == nw or t.startswith(nw + "/") for nw in dirs[p]):
                out.add(p)
    return out


__all__ = ["RenameUnit", "StalePair", "rename_units", "stale_pairs"]
