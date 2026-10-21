"""Path globs the way package managers and tsconfig spell them, matched by segment.

`fnmatch` gets these wrong: its `*` crosses `/` and matches a dot name. Here a pattern
and a path are split on `/` and matched segment by segment: `**` is zero or more whole
segments, `*`/`?`/`[...]` stay inside one segment and never match a name starting
with `.` unless the pattern segment itself starts with `.`. A pattern list is read in
order, the last pattern that matches deciding; an odd number of leading `!` negates.
"""
from __future__ import annotations

import fnmatch
from functools import lru_cache
from typing import Iterable


def _split(p: str) -> "tuple[str, ...]":
    p = p.strip()
    while p.startswith("./"):
        p = p[2:]
    return tuple(s for s in p.strip("/").split("/") if s and s != ".")


def _segment(pat: str, name: str) -> bool:
    if name.startswith(".") and not pat.startswith(".") and pat != name:
        return False
    return fnmatch.fnmatchcase(name, pat)


@lru_cache(maxsize=4096)
def _match(pat: "tuple[str, ...]", path: "tuple[str, ...]") -> bool:
    if not pat:
        return not path
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(_match(rest, path[i:]) for i in range(len(path) + 1)
                   if all(not s.startswith(".") for s in path[:i]))
    return bool(path) and _segment(head, path[0]) and _match(rest, path[1:])


def match(pattern: str, path: str) -> bool:
    """`pattern` matches the whole repo-relative `path` (`/`-separated)."""
    return _match(_split(pattern), _split(path))


def selected(path: str, patterns: Iterable[str]) -> bool:
    """`path` is in the set `patterns` describes (last match wins, `!` negates)."""
    hit = False
    for p in patterns:
        bangs = len(p) - len(p.lstrip("!"))
        if match(p[bangs:], path):
            hit = bangs % 2 == 0
    return hit
