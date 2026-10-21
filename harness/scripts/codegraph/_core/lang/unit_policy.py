"""Which unit a path-id module belongs to: a declared policy, first match wins.

A project sets `languages.<id>.units:` (for a pack declaring the `units` option)
to a list of rules. Each rule `match`es a repo-relative path glob (the rule
`groups:` uses) and names the unit either outright (`unit: shell`) or by its
leading path segments (`depth: 2` -> `apps/admin`). The first matching rule wins;
none matching, the pack's own default does. A unit name may hold `/`: the index key stays `<root>/<unit>`, split
on its first `/` (`units.resolve_unit`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..descriptor.model import path_matches
from .base import LanguageError

_KEYS = frozenset({"match", "depth", "unit"})


@dataclass(frozen=True)
class UnitRule:
    match: str
    depth: "int | None" = None
    unit: "str | None" = None


def parse_rules(raw: Any, where: str) -> "list[UnitRule]":
    if not isinstance(raw, list):
        raise LanguageError("%s must be a list of rules {match, depth|unit}" % where)
    rules: "list[UnitRule]" = []
    for i, r in enumerate(raw, 1):
        at = "%s[%d]" % (where, i)
        if not isinstance(r, dict):
            raise LanguageError("%s must be a mapping {match, depth|unit}" % at)
        unknown = sorted(set(r) - _KEYS)
        if unknown:
            raise LanguageError("%s: unknown key(s) %s; valid: depth, match, unit"
                                % (at, ", ".join(unknown)))
        m = r.get("match")
        if not isinstance(m, str) or not m:
            raise LanguageError("%s: `match` must be a non-empty path glob" % at)
        if ("depth" in r) == ("unit" in r):
            raise LanguageError("%s: give exactly one of `depth`/`unit`" % at)
        depth, unit = r.get("depth"), r.get("unit")
        if "depth" in r and (not isinstance(depth, int) or isinstance(depth, bool) or depth < 1):
            raise LanguageError("%s: `depth` must be a whole number of path segments, 1 or more" % at)
        if "unit" in r and (not isinstance(unit, str) or not unit):
            raise LanguageError("%s: `unit` must be a non-empty name" % at)
        rules.append(UnitRule(m, depth=depth, unit=unit))
    return rules


def assign(rel_path: str, rules: "Sequence[UnitRule]", pack_default: "Callable[[str], str]") -> str:
    """The unit of `rel_path` (repo-relative, `/`-separated)."""
    for r in rules:
        if path_matches(rel_path, r.match):
            if r.unit is not None:
                return r.unit
            parts = rel_path.split("/")[:-1] or rel_path.split("/")
            return "/".join(parts[:r.depth])
    return pack_default(rel_path)
