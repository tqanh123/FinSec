"""port_tests_name_home.py — the name-home cross-check `hs-code port tests`
applies while planning a test move, as one FLAT module under
`harness/scripts/` (sibling of `port_spec.py`: same dot-free-module reachability
reason, and `port_tests_plan.rewrite_patch` is this module's first caller).

Port of ob-tools' `portkit.tests_move.name_home` (pinned commit `60075cb`,
`packages/portkit/src/portkit/tests_move/name_home.py`, `Mismatch`/
`_same_branch`/`mismatches`/`warning`/`_is_submodule`/`rewrite_line`) —
near-verbatim: the upstream module carries no hard-coded package name at all
(it takes `target`/`names`/`merged`/`idx` as plain arguments), so there is no
`gsm_agent` to replace here — the hard-coded prefix this port domain removes
lives one caller up, in `port_tests_plan.py`.

The port-spec table maps a module to ONE house; the definition index over the
destination tree may say a name it imports lives in exactly one OTHER house
(upstream's own measured case: a spec routed `gsm_agent.ops_loop.observations
-> ob.analytics` but `build_route_facts` is defined only in
`ob.ontology.hotspot_cluster`). Default stays warn-only; the caller's own
`apply_name_home` flag performs the rewrite — every rewrite is LOGGED (a
problems line prefixed `name-home repair applied:`), and a mismatch that
cannot be applied safely still warns.

WHAT IT CANNOT SEE
    * An import line carrying an `as` alias never reaches the cross-check —
      the caller's own rebuild branch short-circuits before it (measured,
      not fixed here: changing that branch reshapes every aliased line).
    * A submodule-shaped mismatch (the measured home ends in `.<name>`) is
      warned, never applied — the parent-package rewrite belongs to a
      different door, not to a name move.
    * The index sees TOP-LEVEL binds only; a name reached by `__getattr__`
      or bound inside an `if` is ambiguous-or-absent and stays silent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


@dataclass(frozen=True)
class Mismatch:
    text: str          # the import item as written (`name` or `name as alias`)
    base: str           # the bound name
    mapped: str         # the house the table gave
    home: str           # the single house the tree defines it in


def _same_branch(mapped: str, home: str) -> bool:
    return (mapped == home or home.startswith(mapped + ".")
            or mapped.startswith(home + "."))


def mismatches(target: str, names: "List[str]", merged: Any,
               idx: "Dict[str, Set[str]]") -> "List[Mismatch]":
    """Names of one `from <target> import …` whose measured single home is
    off the table house's branch. Names ruled by renames/name_moves are
    decisions, not drift, and are skipped."""
    mapped = (merged.module_map or {}).get(target)
    if mapped is None:
        return []
    out: "List[Mismatch]" = []
    for n in names:
        base = n.split()[0]
        if ((merged.renames or {}).get("%s::%s" % (target, base))
                or base in (getattr(merged, "name_moves", None) or {})):
            continue
        hits = idx.get(base, set())
        if len(hits) != 1:
            continue                  # unknown or ambiguous: nothing to say
        home = next(iter(hits))
        if not _same_branch(mapped, home):
            out.append(Mismatch(n, base, mapped, home))
    return out


def warning(fname: str, target: str, m: Mismatch) -> str:
    return ("%s: %s.%s mapped to %s but the tree defines it only in %s — "
           "name-home mismatch, verify by hand" % (fname, target, m.base, m.mapped, m.home))


def _is_submodule(m: Mismatch) -> bool:
    return m.home.endswith("." + m.base) or m.home == m.base


def rewrite_line(fname: str, indent: str, target: str, names: "List[str]",
                 comment: str, found: "List[Mismatch]", problems: "List[str]",
                 line: str) -> str:
    """The apply: -> the replacement text (one or more physical lines).
    Applicable mismatches move to `from <home> import …` lines (one per
    home, sorted); the rest stay on the original line. A submodule-shaped
    mismatch is left in place and warned."""
    movable = [m for m in found if not _is_submodule(m)]
    for m in found:
        if _is_submodule(m):
            problems.append(warning(fname, target, m))
    if not movable:
        return line
    moved = {m.text for m in movable}
    keep = [n for n in names if n not in moved]
    by_home: "Dict[str, List[str]]" = {}
    for m in movable:
        by_home.setdefault(m.home, []).append(m.text)
        problems.append("%s: name-home repair applied: %s.%s -> %s (table said %s)"
                        % (fname, target, m.base, m.home, m.mapped))
    out: "List[str]" = []
    if keep:
        out.append("%sfrom %s import %s%s\n" % (indent, target, ", ".join(keep), comment))
    for home in sorted(by_home):
        out.append("%sfrom %s import %s\n" % (indent, home, ", ".join(by_home[home])))
    if not keep and comment:              # keep a trailing comment on the last line
        out[-1] = out[-1].rstrip("\n") + comment + "\n"
    return "".join(out)
