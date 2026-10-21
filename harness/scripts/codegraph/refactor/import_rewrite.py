"""codegraph.refactor.import_rewrite -- rewrite one file's OWN import lines
through a spec's `module_map` (longest source-prefix wins), for landing a
file that never sat anywhere in the TARGET repo before this call.

Port of ob-tools' `portkit.build.import_rewrite` (pinned commit `60075cb`,
`packages/portkit/src/portkit/build/import_rewrite.py`). `add-module`'s own
closure reaches `map_module`/`rewrite_imports` (this file's first half);
`rewrite_from_package`/`measure_deps`/`workspace_dist_map` (the built-tree
leftover-import sweep and the cross-dist dependency measure) are `hs-code
port build`'s own
job and are reached from `codegraph_port_build_verbs.build_dist` -- ported
here, algorithm-faithful, the same closed-set-extension precedent
`port_spec.py`'s own docstring already names for `build`. `ns` (upstream:
`measure_deps(..., ns: str = "ob")`) is a REQUIRED parameter here, never
defaulted -- the one hard-coded namespace literal this port exists to
replace comes from `.port/port.yaml namespace_root`, read by the caller.

WHY THIS IS A SEPARATE ENGINE FROM `movemodule` (measured, not assumed --
see this package's own `movemodule.py` docstring for the mirror of this
note): `movemodule.plan_module_move` moves a file ALREADY tracked in the
target repo (`git mv`) and scans the WHOLE repo (tokenize + AST) for every
reference to it, including a bare dotted expression outside any import line
(`result = pkg.mod.VALUE`) -- there is a real, closed tree to scan, because
nothing outside it is being touched. `add-module` lands bytes read from a
separate, READ-ONLY source repo that were never committed into the target at
all; there is no "whole repo" to scan for stray references to a file that
does not exist yet, and a directory-shaped `module_map` prefix (a whole
subpackage, not one tracked `.py` file) is something `movemodule` refuses
outright (`"... is a package, not a module"`) rather than something it could
express with a different call shape. The two engines solve genuinely
different jobs; merging them would either drop `movemodule`'s whole-body
scan (changing its own observable output) or make this line-based rewriter
scan a foreign, uncommitted tree it was never meant to touch.

WHAT IT CANNOT SEE
    * Regex is LINE-based (`^from X import`, any indent): a dotted reference
      OUTSIDE an import line (`result = pkg.tools.x.VALUE`) is never
      rewritten -- a bare, no-alias `import pkg.tools.x` whose body spells
      the full path is reported as a problem ("rewrite by hand") instead,
      because this door cannot see whether the body uses it that way.
    * `import X.Y` long-form with NO alias is left alone for the same
      reason (an aliased `import X.Y as Z` IS rewritten -- the alias is the
      only name the body can reach it through, so moving the house behind
      it breaks nothing).
    * `name_moves` (name-level house splits) key on the RESOLVED source
      module; a moved name imported through a star import is invisible.
    * `rewrite_from_package`'s own "map the bare package after the submodule
      candidate misses" branch is UNREACHABLE by the algorithm's own string
      matching (faithful to upstream): any `module_map` key that prefix-matches
      the `from`-target also prefix-matches `target.<first_name>` (the
      submodule candidate tried first), since the candidate only ever
      extends the target with one more dotted segment — so the submodule
      probe can never miss while the bare-package probe hits.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from codegraph._core import dist_names

_RE_FROM = re.compile(r"^(\s*)from\s+([.\w]+)\s+import\s+(.+?)(\s*#.*)?$")
_RE_IMPORT_AS = re.compile(r"^(\s*)import\s+([.\w]+)\s+as\s+(\w+)(\s*#.*)?$")

#: stdlib -- taken from the running interpreter, matching upstream's own
#: `measure_deps` source (`sys.stdlib_module_names`).
STDLIB = set(sys.stdlib_module_names)

def mapped_module(mod: str, spec: Any) -> "Optional[str]":
    """LONGEST prefix of `spec.module_map` wins -- a plain prefix match
    without the longest-wins tiebreak silently loses a child module to a
    shorter, unrelated entry. `None` when no entry covers `mod`: the map
    declares no new home for it."""
    best_src: "Optional[str]" = None
    best_dst: str = ""
    for src, dst in spec.module_map.items():
        if mod == src or mod.startswith(src + "."):
            if best_src is None or len(src) > len(best_src):
                best_src, best_dst = src, dst
    if best_src is None:
        return None
    return best_dst + mod[len(best_src):]


def map_module(mod: str, spec: Any) -> str:
    """`mapped_module`, keeping `mod` itself when no entry covers it -- the
    import rewriter's reading: an import of an unmapped module stays put."""
    mapped = mapped_module(mod, spec)
    return mod if mapped is None else mapped


def _map_prefix_len(mod: str, spec: Any) -> int:
    """Length of the LONGEST matching `module_map` prefix of `mod` (0 = no
    match) -- the tiebreak that lets a longer, separately-mapped child
    module win over its parent's own mapping: a `from` line whose FIRST
    name is itself that longer submodule follows the longer branch."""
    best = 0
    for src in spec.module_map:
        if (mod == src or mod.startswith(src + ".")) and len(src) > best:
            best = len(src)
    return best


def _rewrite_plain_import(ln: str, strip: str, src_module: str, spec: Any,
                          fname: str, problems: List[str]) -> "Optional[str]":
    """`import <src> as <alias>` whose module moved house -> the new line,
    or `None` when this is not that shape. The no-alias long form is left
    ALONE and named as a problem: the body may reference it by the full
    dotted path, which this door cannot see."""
    m = _RE_IMPORT_AS.match(strip)
    if m:
        indent, target, alias, comment = (m.group(1), m.group(2), m.group(3), m.group(4) or "")
        if target.startswith("."):
            lvl = len(target) - len(target.lstrip("."))
            parts = src_module.split(".")
            target = ".".join(parts[:len(parts) - lvl] + target.lstrip(".").split("."))
        mapped = map_module(target, spec)
        if mapped != target:
            return "%simport %s as %s%s\n" % (indent, mapped, alias, comment)
        return None
    bare_m = re.match(r"^\s*import\s+([.\w]+)\s*(#.*)?$", strip)
    if bare_m:
        target = bare_m.group(1)
        if not target.startswith(".") and map_module(target, spec) != target:
            problems.append(
                "%s: bare `import %s` maps to a new house but has no alias "
                "— the body spells the full path; rewrite by hand "
                "(import … as …)" % (fname, target))
    return None


def _emit_from(indent: str, target: str, rest: str, comment: str, src_module: str,
              spec: Any, fname: str, problems: List[str]) -> List[str]:
    """One `from` line (or an already-flattened paren block) through the
    whole machine: relative resolution, `module_map`, the longer-child-wins
    branch split, the `name_moves` house split."""
    if target.startswith("."):
        lvl = len(target) - len(target.lstrip("."))
        base = target.lstrip(".")
        parts = src_module.split(".")
        anchor = parts[:len(parts) - lvl]
        target = ".".join(anchor + ([base] if base else []))
        if not base and "." in rest:
            target = ".".join(anchor)
    mapped = map_module(target, spec)
    items36 = [it.strip() for it in rest.split(",") if it.strip()]
    if items36:
        tgt_len = _map_prefix_len(target, spec)
        moved36: List[Tuple[str, str]] = []
        keep36: List[str] = []
        for it in items36:
            parts = it.split(" as ")
            name = parts[0].strip()
            binding = parts[1].strip() if len(parts) == 2 else name
            cand = "%s.%s" % (target, name)
            if name.isidentifier() and _map_prefix_len(cand, spec) > tgt_len:
                moved36.append((binding, map_module(cand, spec)))
            else:
                keep36.append(it)
        if moved36:
            lines36: List[str] = []
            if keep36:
                m = map_module(target, spec)
                lines36.append("%sfrom %s import %s\n"
                               % (indent, m if m != target else target, ", ".join(keep36)))
            for binding, mod in moved36:
                lines36.append("%simport %s as %s\n" % (indent, mod, binding))
            if comment:
                lines36[-1] = lines36[-1].rstrip("\n") + comment + "\n"
            return lines36
    move_prefix = target + "::"
    items = [it.strip() for it in rest.split(",") if it.strip()]
    if any((spec.name_moves or {}).get(move_prefix + it.split()[0]) for it in items):
        groups: "dict[str, List[str]]" = {}
        for it in items:
            dst = (spec.name_moves or {}).get(move_prefix + it.split()[0]) or mapped
            groups.setdefault(dst, []).append(it)
        lines: List[str] = []
        last = len(groups) - 1
        for k, (dst, names_) in enumerate(groups.items()):
            cmt = comment if k == last else ""
            lines.append("%sfrom %s import %s%s\n" % (indent, dst, ", ".join(names_), cmt))
        return lines
    return ["%sfrom %s import %s%s\n" % (indent, mapped, rest, comment)]


def rewrite_imports(text: str, src_module: str, spec: Any, fname: str,
                    problems: List[str]) -> str:
    """Every ABSOLUTE import through `spec.module_map`; a RELATIVE one is
    resolved to absolute first (against `src_module`, the landed file's own
    dotted identity), then mapped the same way. `spec` is duck-typed
    (`.module_map`, `.name_moves`) -- this module never imports a `.port/`-
    aware `Spec` type, so it stays reusable outside the port domain."""
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        strip = ln.rstrip("\n")
        repl = _rewrite_plain_import(ln, strip, src_module, spec, fname, problems)
        if repl is not None:
            out.append(repl)
            i += 1
            continue
        m = _RE_FROM.match(strip)
        if m:
            indent, target, rest = m.group(1), m.group(2), m.group(3)
            comment = m.group(4) or ""
            if rest.strip() == "(" and spec.name_moves:
                j = i + 1
                while j < len(lines):
                    no_cmt = lines[j].split("#", 1)[0].rstrip()
                    if no_cmt == ")" or no_cmt.endswith(")"):
                        break
                    j += 1
                if j < len(lines):
                    inner = lines[i + 1:j]
                    closing = lines[j].rstrip("\n").strip()
                    closing_comment = comment
                    if closing != ")":
                        cut = closing.rfind(")")
                        head = closing[:cut].rstrip().rstrip(",")
                        if head:
                            inner.append(head + "\n")
                        tail = closing[cut + 1:].strip()
                        if tail:
                            closing_comment = " " + tail
                    body = "".join(inner)
                    if "#" in body:
                        problems.append(
                            "%s: paren import of %s has inner comments -- "
                            "name_moves skipped it, rewrite by hand" % (fname, target))
                        out.extend(lines[i:j + 1])
                    else:
                        rest = ", ".join(
                            b.strip().rstrip(",") for b in body.splitlines() if b.strip())
                        out.extend(_emit_from(indent, target, rest, closing_comment,
                                              src_module, spec, fname, problems))
                    i = j + 1
                    continue
            if target.startswith("."):
                lvl = len(target) - len(target.lstrip("."))
                parts = src_module.split(".")
                if lvl > len(parts) - 1:
                    problems.append("%s: relative import escapes the package: %s"
                                    % (fname, strip.strip()))
                    out.append(ln)
                    i += 1
                    continue
            out.extend(_emit_from(indent, target, rest, comment, src_module, spec,
                                  fname, problems))
            i += 1
            continue
        out.append(ln)
        i += 1
    return "".join(out)


# ── from-package sweep over the built tree ───────────────────────────────────

@dataclass
class FromPackageResult:
    text: str
    unresolved: "List[str]" = field(default_factory=list)


def _map_longest(mod: str, module_map: "dict[str, str]") -> "Optional[str]":
    best: "Optional[Tuple[str, str]]" = None
    for src, dst in module_map.items():
        if mod == src or mod.startswith(src + "."):
            if best is None or len(src) > len(best[0]):
                best = (src, dst)
    if best is None:
        return None
    return best[1] + mod[len(best[0]):]


def rewrite_from_package(text: str, src_module: str, module_map: "dict[str, str]",
                         src_roots: "frozenset[str]", fname: str) -> FromPackageResult:
    """Second sweep: every `from`-line whose target's FIRST dotted segment is a
    declared source root, tried through `module_map` `X.Y` (the submodule)
    BEFORE the bare `X` -- the same longer-branch-wins rule `rewrite_imports`
    already applies at copy time. No match -> reported in `.unresolved`
    (never red on its own; the caller decides).

    `src_module` is accepted (matching upstream's own signature) but unused
    here -- a relative import is skipped outright (line below), so nothing
    in this pass needs the importing file's own dotted identity."""
    del src_module
    out: "List[str]" = []
    unresolved: "List[str]" = []
    for ln in text.splitlines(keepends=True):
        strip = ln.rstrip("\n")
        m = _RE_FROM.match(strip)
        if not m:
            out.append(ln)
            continue
        indent, target, rest = m.group(1), m.group(2), m.group(3)
        comment = m.group(4) or ""
        if target.startswith("."):
            out.append(ln)          # relative — not this pass's business
            continue
        root_pkg = target.split(".")[0]
        if root_pkg not in src_roots:
            out.append(ln)          # not a source-package import
            continue
        name_part = rest.strip().split()[0].rstrip(",")
        candidate = "%s.%s" % (target, name_part)
        mapped_full = _map_longest(candidate, module_map)
        if mapped_full is not None:
            # `from gsm_agent.ops_loop import thresholds` -> `from ob.kpi
            # import thresholds`: the long branch maps the SUBMODULE; the
            # line keeps the from-package one level up and the original
            # name list.
            mapped_mod = ".".join(mapped_full.split(".")[:-1]) or mapped_full
            out.append("%sfrom %s import %s%s\n" % (indent, mapped_mod, rest.strip(), comment))
            continue
        mapped = _map_longest(target, module_map)
        if mapped is not None:
            out.append("%sfrom %s import %s%s\n" % (indent, mapped, rest.strip(), comment))
            continue
        unresolved.append("%s: from %s import %s" % (fname, target, rest.strip()))
        out.append(ln)
    return FromPackageResult(text="".join(out), unresolved=unresolved)


def workspace_dist_map(repo_packages: Path, ns: str) -> "dict[str, str]":
    """module -> dist name, MEASURED from the built workspace tree. A
    namespace shared by several dists cannot be derived from the
    import prefix alone (`ob.mode.*` spans four dists, not one) -- every
    module's home is where its file actually lives, under `repo_packages/
    */src/<ns>/`. Empty when `repo_packages` does not exist (a flat,
    single-dist target has no sibling dists to measure)."""
    out: "dict[str, str]" = {}
    if not repo_packages.is_dir():
        return out
    for proj in sorted(repo_packages.glob("*")):
        src = proj / "src" / ns
        if not src.is_dir():
            continue
        for p in src.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(src).with_suffix("")
            parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
            out.setdefault(".".join((ns, *parts)), proj.name)
    return out


def _ws_dist_of(mod: str, ns: str, ws_map: "dict[str, str] | None") -> "Optional[str]":
    """Longest module-prefix hit in the measured `ws_map`; `None` when no
    map was passed at all (a flat, single-dist target with nothing to
    cross-check against)."""
    if ws_map is None:
        return None
    parts = mod.split(".")
    for i in range(len(parts), 0, -1):
        hit = ws_map.get(".".join(parts[:i]))
        if hit:
            return hit
    return None


def measure_deps(built_dir: Path, ns: str, *,
                 ws_map: "dict[str, str] | None" = None) -> "Tuple[List[str], List[str]]":
    """(third-party, workspace) imported by the BUILT tree -- BOTH levels
    (module and function-local; upstream's own dependency-measurement
    contract). `ns` names the target namespace root -- REQUIRED (see module
    docstring), never a hard-coded `"ob"` default; a module under `ns.` with
    no hit in `ws_map` is a FUTURE dist's module (not yet built) rather than
    a third-party package, and is silently skipped, never guessed at."""
    third: "set[str]" = set()
    ws: "set[str]" = set()
    for p in sorted(built_dir.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            # A file the build's own py_compile self-check already reported
            # as `generated file does not compile` (a syntax error carried
            # verbatim from the source — the leftover-import sweep is a
            # line-regex pass, never an AST check, so it cannot have fixed
            # this). Unmeasurable is not a reason to abort every OTHER
            # file's dependency measurement.
            continue
        for node in ast.walk(tree):
            mods: "List[str]" = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for m in mods:
                root = m.split(".")[0]
                if root in STDLIB or root == "__future__":
                    continue
                d = _ws_dist_of(m, ns, ws_map)
                if d:
                    ws.add(d)
                elif ws_map is not None and m.startswith(ns + "."):
                    pass   # a FUTURE dist's module: no house yet, and not
                           # the PyPI package `ns` either
                else:
                    third.add(dist_names.candidates(m)[0])
    return sorted(third), sorted(ws)
