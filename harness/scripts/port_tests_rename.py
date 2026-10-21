"""port_tests_rename.py — the import-line passes `hs-code port tests` runs
BEFORE the module-house mapping (`codegraph.refactor.import_rewrite.
rewrite_imports`), as one FLAT module under `harness/scripts/` (sibling of
`port_spec.py`/`port_tests_name_home.py`; `port_tests_plan.rewrite_patch` is
this module's first caller).

Port of ob-tools' `portkit.tests_move.move_rename` (pinned commit `60075cb`,
`packages/portkit/src/portkit/tests_move/move_rename.py`, `_apply_renames`/
`_measure_owners`) — two passes over one `from` import line: (1) renames —
a row in the merged spec's own `renames` table becomes `from <x> import new
as old`, so the name the BODY spells keeps resolving; (2) a multi-home
module — a module several port-specs map to different houses is decided PER
NAME, measured against the target tree via `codegraph.refactor.move_index`
(already ported for `add-module`'s own leftover-import fallback, reused
here rather than a second copy).

NO HARD-CODED PACKAGE: upstream's own `_STRING_OLD`/`_RE_OLD_LEFTOVER`
(literal `"gsm_agent\\."` regexes, `move_rename.py:17-19`) are replaced by
`string_old_pattern`/`old_leftover_pattern`, built from a `package` argument
the caller reads off `.port/port.yaml sources[].package` — the same
"no hard-coded package" rule `codegraph_port_tree_verbs.py` already applies
to `add-module`. `_RE_FROM` is NOT redefined here — it is the exact same
pattern `codegraph.refactor.import_rewrite` already compiles, imported
directly (one door, never a second regex that could drift). Upstream's own
`_OLD_PREFIX` (`move_rename.py:16`, used only by `merged_spec_and_unit_dist`)
is not ported at all — that function already lives in `port_spec.py`, taking
`old_prefix` as a parameter.

WHAT IT CANNOT SEE
    * See `port_tests_plan` — string-only references and getattr dispatch
      are not rewritten by this pass.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from codegraph.refactor import move_index
from codegraph.refactor.import_rewrite import _RE_FROM

import port_tests_name_home as name_home

#: Any physical import line — reused by `port_tests_plan._hunks_touching_
#: non_import` to tell an import-shaped changed line from a code line.
IMPORT_LINE = re.compile(r"^\s*(?:from\s+[\w.]+\s+import\b|import\s+[\w.]+)")

#: Past this many old-prefix leftover lines a root catch-all used to
#: swallow, the plan warns BY NAME instead of staying silent.
CATCHALL_WARN_AT = 5


def string_old_pattern(package: str) -> "re.Pattern[str]":
    """A monkeypatch/importlib STRING naming the declared upstream
    `package` — AST-blind by design (see `port_tests_plan.rewrite_patch`'s
    own docstring for why this can only be listed, never rewritten)."""
    esc = re.escape(package)
    return re.compile(r'"%s\.|\'%s\.' % (esc, esc))


def old_leftover_pattern(package: str) -> "re.Pattern[str]":
    """An import line still naming the declared upstream `package` after
    the rewrite passes ran — the road a root catch-all used to pave, or
    any other unmapped line."""
    esc = re.escape(package)
    return re.compile(r"^\s*(?:from\s+%s[.\s]|import\s+%s[.\s])" % (esc, esc))


def _measure_owners(base: str, target: str, houses: "List[str]",
                    packages_root: Path) -> "List[str]":
    """The house(s) that DEFINE `base` among a multi-home module's
    candidate `houses`, measured on the target tree via
    `codegraph.refactor.move_index` — the fallback `add-module`'s own
    leftover pass already reuses."""
    tail = target.rsplit(".", 1)[-1]
    owners: "List[str]" = []
    for h in houses:
        nm = move_index.module_names(h, packages_root) or set()
        if base in nm:
            owners.append(h)
            continue
        # a package door that binds nothing still SHIPS the name in a
        # submodule named after the old flat module: measure that file too
        sub = move_index.module_names("%s.%s" % (h, tail), packages_root) or set()
        if base in sub:
            owners.append(h)
    if len(owners) > 1:
        deepest = max(owners, key=len)
        if all(o == deepest or deepest.startswith(o + ".") for o in owners):
            # an ANCESTOR chain (a package re-exports what its own submodule
            # defines): the DEEPEST house is the definition — measured,
            # never guessed
            owners = [deepest]
    if len(owners) == 0 and packages_root is not None:
        # spec houses stale (a dist landed after the specs were written):
        # measure the name across the WHOLE tree before refusing
        hits = move_index.name_index(packages_root).get(base, set())
        if len(hits) > 1:
            deepest = max(hits, key=len)
            if all(h == deepest or deepest.startswith(h + ".") for h in hits):
                hits = {deepest}
        if len(hits) == 1:
            mod = next(iter(hits))
            # "from <pkg> import <submodule>": the resolved house is the
            # module FILE; the import line needs its PARENT package as the
            # from-side.
            if target.rsplit(".", 1)[-1] != base and mod.endswith("." + base):
                mod = mod[: -len(base) - 1]
            owners = [mod]
    return owners


def apply_renames(text: str, merged: Any, multi: "Dict[str, List[str]]",
                  packages_root: "Optional[Path]", fname: str,
                  problems: "List[str]", apply_name_home: bool = False) -> str:
    """Import-line passes BEFORE house mapping: (1) renames — `from
    <target> import old` whose row is `<target>::old -> new` becomes `from
    <target> import new as old`, so the name the BODY spells keeps
    resolving and the diff stays import-only; (2) multi-home modules — a
    module several specs map to different houses is decided PER NAME,
    measured against the target tree; unknown names and a missing
    `packages_root` are REFUSED (line left unchanged, listed in
    `problems`), never guessed; (3) name-home cross-check — the table maps
    a SINGLE-home module to one house; the definition index over the
    target tree may say a name it imports lives in exactly one OTHER
    house. Warns by default; `apply_name_home` opts in to the rewrite,
    logged via `port_tests_name_home.rewrite_line`. Skipped for names the
    table itself already rules (renames rows) — those are decisions, not
    drift."""
    out: "List[str]" = []
    for ln in text.splitlines(keepends=True):
        m = _RE_FROM.match(ln.rstrip("\n"))
        if not m:
            out.append(ln)
            continue
        indent, target, rest, comment = (
            m.group(1), m.group(2), m.group(3), m.group(4) or "")
        raw = [n.strip() for n in rest.split(",") if n.strip()]
        names = list(raw)
        # (1) renames
        for i, n in enumerate(names):
            new = (merged.renames or {}).get("%s::%s" % (target, n.split()[0]))
            if new and " as " not in n:
                names[i] = "%s as %s" % (new, n)
        # (2) multi-home split per name
        if target in multi:
            if packages_root is None:
                problems.append(
                    "%s: %s maps to %s and no --packages-root to measure the "
                    "split — file left out, rerun with --packages-root"
                    % (fname, target, multi[target]))
                out.append(ln)
                continue
            by_house: "Dict[str, List[str]]" = {}
            kept_old: "List[str]" = []       # a name the table cannot place is KEPT, never dropped
            for n in names:
                base = n.split()[0]
                owners = _measure_owners(base, target, multi[target], packages_root)
                if len(owners) != 1:
                    problems.append(
                        "%s: %s.%s measured in %d house(s) of %s — refused, "
                        "decide by hand (name kept on the OLD import line, "
                        "never dropped)" % (fname, target, base, len(owners), multi[target]))
                    kept_old.append(n)
                    continue
                by_house.setdefault(owners[0], []).append(n)
            for house, group in by_house.items():
                out.append("%sfrom %s import %s%s\n" % (indent, house, ", ".join(group), comment))
            if kept_old:
                out.append("%sfrom %s import %s%s\n" % (indent, target, ", ".join(kept_old), comment))
            continue
        # a rename applied, or an item already carried its own `as` alias:
        # re-emit the (possibly rewritten) line rather than falling through
        # to the name-home cross-check, which never sees an aliased name.
        if names != raw or [n.split()[0] for n in names] != raw:
            out.append("%sfrom %s import %s%s\n" % (indent, target, ", ".join(names), comment))
            continue
        # (3) name-home cross-check
        if packages_root is not None and target in (merged.module_map or {}):
            found = name_home.mismatches(
                target, names, merged, move_index.name_index(packages_root))
            if found and apply_name_home:
                out.append(name_home.rewrite_line(
                    fname, indent, target, names, comment, found, problems, ln))
                continue
            problems.extend(name_home.warning(fname, target, m) for m in found)
        out.append(ln)
    return "".join(out)
