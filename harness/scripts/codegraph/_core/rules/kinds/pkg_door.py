"""`pkg_door` — the DOOR of a workspace package: what is public, what is not.

Three declarations a distributable package owes (ported (algorithm-faithful) from
`ob-tools@60075cb:packages/portkit/src/portkit/gates/pkg_door.py`):

    PKG-DOOR-DOC    the door `__init__.py` carries a docstring
    PKG-DOOR-ALL    the door declares `__all__`
    MODULE-PRIVATE-DOOR   a public module under the door has a real user, an
                          underscore name (`_x.py`), or a baseline line

"INTERNAL MODULE" IS EVIDENCE, NOT A FILE-NAME GUESS. A public module under the door is
flagged only when NOTHING imports it in the packages tree AND no ledger line carried a
used symbol into it — the same two-piece-of-evidence design upstream paid for (a corpus
where 59% of "public" modules WERE imported by another dist, and 51 underscore-named
modules were imported from outside anyway: naming alone proves nothing either way). A
module with a known future user is declared in the baseline WITH ITS REASON — a two-way,
shrink-only baseline: a baselined module that gained a real user, or vanished from the
tree, is `baseline-stale` (delete the line in the SAME commit).

WHERE PACKAGES COME FROM — the T13 fix this port carries. Upstream's own `pkg_door.py`
walked `<packages_dir>/<name>/src` by hand, and its sibling `packaging.py` probed the
same tree with a SECOND, differently-shaped function (`code_dir`) — two package
detectors judging two different trees at the same moment. This kind never re-derives a
package list: it reads `codegraph.check.tree.workspace()`, the one door every reader of
a workspace package's code already shares (`package_code_dir` — `<pkg>/src` when
present, else the package directory itself), driven by the project's OWN `codemap.yaml`
`workspace` roots — never a hard-coded `packages/` or namespace prefix.

WHAT THIS KIND CANNOT SEE
    * The underscore is a CONVENTION, not a fence: `from pkg._store import X` still
      runs — this counts NAMING, not who walks around it.
    * In-edges count IMPORTS, not CALLS: a name only ever `from X import Y`-ed and
      never called still counts as used; `getattr` dispatch is invisible.
    * `ancestry_covered` (below) counts a LANDED ledger line only when the OLD tree
      really imported the line's source module (upstream's own filter), measured by
      `old_tree_imports` over the port's source tree (`.port/port.yaml` `sources[]`,
      read through that tree's own layout and the snapshot pipeline's import
      resolution). Outside a port there is no old tree to read: every landed line
      counts, and the run says so in a note.
    * `__all__` is never cross-checked against reality, and the docstring is checked
      for PRESENCE only.
    * It does not know whether a package SHOULD exist; splitting packages is a human
      decision, this only demands a split package declare its own door.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ..base import RuleContext, RuleResult, RulesError, Violation, register_kind

#: subdirectories of a package that are not package code. Declared, not guessed.
NOT_PKG_CODE = frozenset({"tests", "test", "docs", "scripts", "migrations"})

#: ledger statuses whose line actually CARRIED code into the new tree.
_LANDED = frozenset({"ported", "split", "merged"})


def door_of(code_dir: Path) -> "Path | None":
    """The DEEPEST `__init__.py` that is still a door: with PEP 420 namespaces,
    `<code>/ns/di/x/` has its door at `x/__init__.py` — `ns/`/`di/` carry none."""
    inits = sorted(code_dir.rglob("__init__.py"), key=lambda p: len(p.parts))
    return inits[0] if inits else None


def dotted_name(p: Path, code_dir: Path) -> str:
    """Dotted module name of a file under the package's code dir — PEP 420:
    namespace segments are NOT dropped."""
    parts = list(p.relative_to(code_dir).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports_of_tree(base: Path) -> "set[str]":
    """Modules IMPORTED within one code tree, by absolute or resolved-relative
    module name — two passes: pass 1 collects every file's own real module name (to
    resolve `from P import x` where `x` is a submodule, not a symbol); pass 2 resolves
    every import against it. Unparseable files are SKIPPED, never hidden as clean."""
    if not base.is_dir():
        return set()
    mods: "set[str]" = set()
    parsed: "list[tuple[str, ast.Module]]" = []
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        relparts = p.relative_to(base).with_suffix("").parts
        pkg = list(relparts)
        if p.name != "__init__.py":
            pkg = pkg[:-1]
        if pkg:
            mods.add(".".join(relparts))
        parsed.append((".".join(pkg), tree))
    out: "set[str]" = set()
    for pkg_name, tree in parsed:
        pp = pkg_name.split(".") if pkg_name else []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                if n.level == 0:
                    bmod = n.module or ""
                else:
                    cur = pp
                    b = cur[: len(cur) - (n.level - 1)] if n.level > 1 else cur
                    bmod = ".".join(b + ([n.module] if n.module else []))
                if not bmod:
                    continue
                out.add(bmod)
                for a in n.names:
                    if f"{bmod}.{a.name}" in mods:
                        out.add(f"{bmod}.{a.name}")
    return out


def scan_imports(pkgs) -> "set[str]":
    """Modules imported by code anywhere in the packages tree (one code dir each)."""
    out: "set[str]" = set()
    for pkg in pkgs:
        out |= imports_of_tree(pkg.code)
    return out


def old_tree_imports(repo: Path, source_id: "str | None" = None) -> "tuple[set[str] | None, str | None]":
    """(modules the port's SOURCE tree imports anywhere, None), or (None, note) when
    `repo` is no port. The source is `.port/port.yaml`'s `sources[]` entry
    (`source_id`, or its only one); its modules and imports are the snapshot
    pipeline's answer over its own layout (`sources[].descriptor`, else the source's
    `codemap.yaml`). A file that does not parse is skipped, as upstream skipped it. A
    port whose source cannot be read refuses (`RulesError`): an unknown old tree is
    no filter, not an empty one."""
    import port_config
    import port_workspace
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph._core.snapshot.pipeline import import_context, module_imports
    cfg_path = port_workspace.port_yaml_path(repo)
    if not cfg_path.is_file():
        return None, ("old-tree import filter off: %s is no port (no .port/port.yaml) — every "
                      "landed ledger line counts" % repo)
    cfg, problems = port_config.load_and_validate(cfg_path)
    if problems:
        raise RulesError("%s is invalid: %s" % (cfg_path, "; ".join(map(str, problems))))
    source, problem = port_config.find_source(cfg, source_id)
    if problem:
        raise RulesError(str(problem))
    upstream = (repo / str(source.get("repo") or "")).resolve()
    dpath = ((repo / source["descriptor"]).resolve() if source.get("descriptor")
             else upstream / "codemap.yaml")
    if not upstream.is_dir() or not dpath.is_file():
        raise RulesError("the port's source tree cannot be read (%s, layout %s) — the "
                         "old-tree import filter needs it" % (upstream, dpath))
    try:
        ctx = import_context(upstream, load(dpath, upstream).descriptor)
    except DescriptorError as e:
        raise RulesError("source layout %s: %s" % (dpath, e)) from e
    out: "set[str]" = set()
    for ref in ctx.sources:
        try:
            got = module_imports(ctx, ref.path.relative_to(upstream).as_posix(),
                                 ref.path.read_text(encoding="utf-8"), ref.module)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        out |= got or set()
    return out, None


def ancestry_covered(ledger_path: Path, old_imported: "set[str] | None" = None) -> "set[str]":
    """New-tree modules that RECEIVED a used old-tree symbol, per the port ledger:
    a `ported`/`split`/`merged` line's `to` carries the destination key(s)
    (`<anchor>::<module>::<name>`) — read through `port_ledger_reader.read_ledger`,
    never a second loader. With `old_imported` (`old_tree_imports`), a line counts
    only when its SOURCE module is one the old tree imported. A broken line refuses OUTRIGHT (`RulesError`, honest 2):
    a rule whose ancestry cannot even be read is not a softer verdict, it is a
    different kind of failure (see `no_orphan_code`/`harness-contract` on fail-closed
    compliance checks vs fail-open telemetry — this is the former)."""
    import port_ledger_reader
    import port_ledger_spec
    result = port_ledger_reader.read_ledger(ledger_path)
    if result.broken:
        shown = ", ".join("line %d" % n for n, _raw, _msg in result.broken[:10])
        raise RulesError(
            f"kind 'pkg_door': {len(result.broken)} broken line(s) in {ledger_path} "
            f"({shown}) — fix by hand first (append-only: never edit a line in place)")
    state: "dict[str, Any]" = {}
    for ev in result.events:
        state[ev.node] = ev  # last event per node wins (replay)
    out: "set[str]" = set()
    for ev in state.values():
        if ev.status not in _LANDED:
            continue
        if old_imported is not None:
            try:
                if port_ledger_spec.parse_key(ev.node).module not in old_imported:
                    continue
            except port_ledger_spec.PortingLedgerError:
                continue
        for to in ev.extra.get("to") or ():
            try:
                key = port_ledger_spec.parse_key(to)
            except port_ledger_spec.PortingLedgerError:
                continue
            out.add(key.module)
    return out


def load_baseline(path: Path) -> "dict[str, str]":
    """The pkg-door baseline: public modules WAITING for a user, each with its
    reason. Shrink-only and two-way: a module that gained a real user but still
    sits here is RED; so is a line whose module vanished. No file = nobody is
    waiting — the STRICT posture."""
    if not path.is_file():
        return {}
    import yaml_io
    data = yaml_io.yaml_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def check_pkg(pkg: Any, covered: "set[str]", baseline: "dict[str, str]") -> "list[tuple[str, str, dict]]":
    """(cause, label, detail) for every declaration one package is missing.

    `covered`: modules WITH users — imported in the packages tree, or covered by
    ledger ancestry."""
    out: "list[tuple[str, str, dict]]" = []
    door = door_of(pkg.code)
    if door is None:
        out.append((
            "no-door",
            f"{pkg.name}: no `__init__.py` anywhere under its code dir — a package "
            f"without a door lets nobody say what its API is",
            {"package": pkg.name}))
        return out
    try:
        tree = ast.parse(door.read_text(encoding="utf-8"))
    except SyntaxError as e:
        raise RulesError(f"{door} does not parse — {e}") from e
    rel_door = door.relative_to(pkg.dir)
    if not ast.get_docstring(tree):
        out.append((
            "door-doc",
            f"{pkg.name}: `{rel_door}` has no docstring — a door that does not "
            f"state why the package exists",
            {"package": pkg.name, "door": str(rel_door)}))
    has_all = any(
        (isinstance(n, ast.Assign)
         and any(getattr(x, "id", "") == "__all__" for x in n.targets))
        or (isinstance(n, ast.AnnAssign)
            and getattr(n.target, "id", "") == "__all__" and n.value is not None)
        for n in tree.body)
    if not has_all:
        out.append((
            "door-all",
            f"{pkg.name}: `{rel_door}` declares no `__all__` — its API is "
            f"'whatever happens to be there'",
            {"package": pkg.name, "door": str(rel_door)}))
    door_dir = door.parent
    for p in sorted(door_dir.rglob("*.py")):
        if p.name == "__init__.py" or any(x in NOT_PKG_CODE for x in p.relative_to(door_dir).parts):
            continue
        if p.name.startswith("_"):
            continue
        mod = dotted_name(p, pkg.code)
        if mod in covered or mod in baseline:
            continue
        out.append((
            "module-unimported",
            f"{pkg.name}: `{p.relative_to(pkg.dir)}` is a PUBLIC module nothing "
            f"imports in the packages tree, and no ledger line carried a used "
            f"old-tree symbol into it. If it is API for a user that has not "
            f"landed yet, declare it in the pkg-door baseline with the dist that "
            f"will use it; otherwise rename it `_{p.name}`",
            {"package": pkg.name, "module": mod}))
    return out


def _present_modules(pkgs) -> "set[str]":
    out: "set[str]" = set()
    for pkg in pkgs:
        door = door_of(pkg.code)
        if door is None:
            continue
        for p in door.parent.rglob("*.py"):
            if p.name == "__init__.py" or any(x in NOT_PKG_CODE for x in p.relative_to(door.parent).parts):
                continue
            out.add(dotted_name(p, pkg.code))
    return out


class PkgDoorCheck:
    CAUSES = ("no-door", "door-doc", "door-all", "module-unimported", "baseline-stale")

    def __init__(self, rule_id: str, params: "dict[str, Any]") -> None:
        self.rule_id = rule_id
        self.ledger = Path(params["ledger"]) if params.get("ledger") else None
        self.baseline_path = Path(params["baseline"]) if params.get("baseline") else None
        self.source = params.get("source")

    def _in_repo(self, repo: Path, p: "Path | None") -> "Path | None":
        if p is None:
            return None
        return p if p.is_absolute() else repo / p

    def check(self, ctx: RuleContext) -> RuleResult:
        from codegraph.check.tree import workspace
        _descriptor, pkgs = workspace(ctx.repo)
        notes: "list[str]" = []
        if not pkgs:
            notes.append("no workspace package under any `workspace` root — the "
                         "three door laws have NO SUBJECT here")
            return RuleResult(self.rule_id, "error", (), tuple(notes))

        covered = scan_imports(pkgs)
        ledger = self._in_repo(ctx.repo, self.ledger)
        if ledger is not None:
            old, note = old_tree_imports(ctx.repo, self.source)
            covered |= ancestry_covered(ledger, old)
            notes += [note] if note else []
        else:
            notes.append("ledger ancestry NOT consulted (no `ledger` param): modules "
                         "whose users still live in the old tree can flag as unimported")
        baseline_path = self._in_repo(ctx.repo, self.baseline_path)
        baseline = load_baseline(baseline_path) if baseline_path else {}

        present = _present_modules(pkgs)
        v: "list[Violation]" = []
        for pkg in pkgs:
            for cause, label, detail in check_pkg(pkg, covered, baseline):
                v.append(Violation(self.rule_id, cause, label, detail))
        for m in sorted(x for x in baseline if x in covered or x not in present):
            why = "it now has a real user" if m in covered else "the module is no longer in the tree"
            v.append(Violation(
                self.rule_id, "baseline-stale",
                f"pkg-door baseline holds `{m}` but {why} — delete the line in the "
                f"SAME commit",
                {"module": m, "why": why}))
        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes))


register_kind("pkg_door")(PkgDoorCheck)
