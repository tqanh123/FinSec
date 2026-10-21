"""`landfall` — a dist that landed on the new tree must not carry a residue file, a
slice whose body got overwritten by another, or a module nobody imports and no
ledger line covers.

Ported (algorithm-faithful, narrowed) from `ob-tools@60075cb:packages/portkit/src/
portkit/gates/landfall.py` (the adapter) + `packages/portkit/src/portkit/build/
landfall.py` (`check_overwritten`/`check_missing_body`, the detectors). Three laws
over a built dist:

    LANDFALL-OVERWRITTEN   a file in the dist no spec source points at anymore —
                           residue of a previous build whose spec line was removed
    LANDFALL-MISSING-BODY  several sources share a destination; one source's
                           declared symbols are absent from the built file
    LANDFALL-ORPHAN        a public module of the dist nobody imports and no
                           ledger entry covers

`overwritten`/`missing-body` need the dist's own SPEC (an optional `spec` param
naming the `.yaml` file) — read through `port_spec.Spec.load`, the ONE spec reader
this repo has (`codegraph_port_build_verbs.build_dist` reads the exact same fields
off the exact same loader). No `spec` param declared for this rule row -> both laws
are SKIPPED and the run's own notes say so, never a silent "clean"; a spec
declaring `unit_sources` skips `overwritten` alone (upstream's own rule — its
destinations are unknowable without the source tree) and says so too.

ORPHAN REUSES `pkg_door`, NOT A SECOND FLOW. This kind calls `pkg_door.check_pkg`
directly (its own porting note: "landfall calls `pkg_door.ancestry_covered`
straight, reading the ledger through `read_ledger`") and keeps only the
`module-unimported` finding, relabeled `orphan` — the same door/`__all__`/docstring
laws `pkg_door`'s own rule already reports for every package are NOT repeated by this
one; a dist with no door at all simply has no subject for `orphan` (that dist's
`no-door` is `pkg_door`'s job).

WHAT `overwritten`/`missing-body` CANNOT SEE (inherited from the frozen detectors)
    * `unit_sources` build files FROM THE SOURCE TREE; without it the legitimate
      destination set is unknowable — `overwritten` is skipped, never "clean".
    * `missing-body` only checks slices that declare `symbols:` explicitly; without
      them nothing is inferred from the snapshot.
    * No git history is consulted — "the previous pass" is what is on disk, nothing
      older.

WHERE PACKAGES COME FROM — reads `codegraph.check.tree.workspace()`, the same probe
`pkg_door`/`packaging` use; `dist` names ONE package by its workspace directory name
(matching upstream's own `packages/<dist>/` identity), never a hard-coded path. The
namespace (`ns`) `overwritten`/`missing-body` need to turn a dotted module into a
path comes off the SAME `workspace()` descriptor's own `namespace_root` — never a
second namespace lookup.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from ..base import RuleContext, RuleResult, RulesError, Violation, register_kind, require_params
from . import pkg_door

import port_spec


def _ast_syms_of(p: Path) -> "Set[str]":
    """Top-level symbol names in a .py file (via AST); an unparseable/unreadable
    file reports nothing (never a crash mid-scan)."""
    import ast
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    out: "Set[str]" = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _map(mod: str, spec: "port_spec.Spec") -> str:
    """`import_rewrite.map_module`, guarded against a spec whose `module_map:`
    parsed to `None` (a well-formed but degenerate YAML key) — the same
    landmine `codegraph_port_build_verbs`'s own leftover-import sweep guards
    against."""
    if not spec.module_map:
        return mod
    from codegraph.refactor.import_rewrite import map_module
    return map_module(mod, spec)


def _dest_of_slice(raw_slice: dict, spec: "port_spec.Spec", ns: str, dist_pkg_src: Path) -> Path:
    m = raw_slice["module"]
    tgt_mod = raw_slice.get("to") or _map(m, spec)
    rel = tgt_mod.removeprefix(ns + ".")
    return dist_pkg_src / Path(*rel.split(".")).with_suffix(".py")


def _dest_of_file(fs: dict, spec: "port_spec.Spec", ns: str, dist_pkg_src: Path) -> Path:
    fpath = fs["file"]
    src_mod = fs.get("from_module") or ".".join(Path(fpath).with_suffix("").parts)
    to_mod = fs.get("to") or _map(src_mod, spec)
    rel = to_mod.removeprefix(ns + ".")
    return dist_pkg_src / Path(*rel.split(".")).with_suffix(".py")


def _spec_dest_paths(spec: "port_spec.Spec", ns: str, dist_pkg_src: Path) -> "Set[Path]":
    """Destination paths the spec DECLARES (slice + file + handbuilt); unit
    sources excluded (see `check_overwritten`). This is the KNOWN set — a
    dist file outside it with no unit sources is RED."""
    out: "Set[Path]" = set()
    for raw_slice in spec.slice_sources:
        out.add(_dest_of_slice(raw_slice, spec, ns, dist_pkg_src))
    for fs in spec.file_sources:
        out.add(_dest_of_file(fs, spec, ns, dist_pkg_src))
    for rel in spec.handbuilt:
        path_only = rel.split("#", 1)[0].strip()
        for candidate in dist_pkg_src.rglob(path_only):
            out.add(candidate)
    return out


def check_overwritten(spec: "port_spec.Spec", ns: str, dist_root: Path,
                      dist_pkg_src: Path) -> "List[Tuple[str, str]]":
    """(label, dist-root-relative path) for every RED 'overwritten': a dist
    file no spec source points at anymore — residue of a previous build
    whose spec line was removed."""
    if not dist_pkg_src.is_dir():
        return []
    if spec.unit_sources:
        return []          # destinations unknowable with unit sources — no verdict
    known = {k.resolve() for k in _spec_dest_paths(spec, ns, dist_pkg_src)}
    errs: "List[Tuple[str, str]]" = []
    for p in sorted(dist_pkg_src.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if p.resolve() not in known:
            rel = str(p.relative_to(dist_root))
            errs.append((
                "%s: file present in the dist but no spec source points at it "
                "— likely residue of an earlier build pass" % rel, rel))
    return errs


def check_missing_body(spec: "port_spec.Spec", ns: str, dist_root: Path,
                       dist_pkg_src: Path) -> "List[Tuple[str, str]]":
    """(label, dist-root-relative path) for every RED 'missing body': several
    slices share a destination and one slice's declared symbols are absent
    from the built file (overwritten by another slice)."""
    if not dist_pkg_src.is_dir():
        return []
    by_dest: "Dict[str, List[dict]]" = {}
    for raw_slice in spec.slice_sources:
        dest = _dest_of_slice(raw_slice, spec, ns, dist_pkg_src)
        by_dest.setdefault(str(dest), []).append(raw_slice)

    errs: "List[Tuple[str, str]]" = []
    for dest_str, slices in by_dest.items():
        if all("symbols" not in s for s in slices):
            continue
        dest = Path(dest_str)
        if not dest.is_file():
            continue
        present = _ast_syms_of(dest)
        for s in slices:
            needed = s.get("symbols", [])
            missing = [sym for sym in needed if sym not in present]
            if missing:
                rel = str(dest.relative_to(dist_root))
                errs.append((
                    "%s: source '%s' misses symbols %s in the built file — "
                    "possibly overwritten by another slice" % (rel, s["module"], missing),
                    rel))
    return errs


class LandfallCheck:
    CAUSES = ("overwritten", "missing-body", "orphan")

    def __init__(self, rule_id: str, params: "dict[str, Any]") -> None:
        self.rule_id = rule_id
        self.dist: str = params["dist"]
        self.ledger = Path(params["ledger"]) if params.get("ledger") else None
        self.baseline_path = Path(params["baseline"]) if params.get("baseline") else None
        self.spec_path = Path(params["spec"]) if params.get("spec") else None
        self.source = params.get("source")

    def _in_repo(self, repo: Path, p: "Path | None") -> "Path | None":
        if p is None:
            return None
        return p if p.is_absolute() else repo / p

    def check(self, ctx: RuleContext) -> RuleResult:
        from codegraph.check.tree import workspace
        descriptor, pkgs = workspace(ctx.repo)
        target = next((p for p in pkgs if p.name == self.dist), None)
        if target is None:
            raise RulesError(
                f"rule {self.rule_id!r}: no workspace package named {self.dist!r} "
                f"— known: {', '.join(sorted(p.name for p in pkgs)) or '(none)'}")

        covered = pkg_door.scan_imports(pkgs)
        ledger = self._in_repo(ctx.repo, self.ledger)
        notes: "List[str]" = []
        if ledger is not None:
            old, note = pkg_door.old_tree_imports(ctx.repo, self.source)
            covered |= pkg_door.ancestry_covered(ledger, old)
            notes += [note] if note else []
        baseline_path = self._in_repo(ctx.repo, self.baseline_path)
        baseline = pkg_door.load_baseline(baseline_path) if baseline_path else {}

        v = [Violation(self.rule_id, "orphan", label, detail)
             for cause, label, detail in pkg_door.check_pkg(target, covered, baseline)
             if cause == "module-unimported"]

        spec_path = self._in_repo(ctx.repo, self.spec_path)
        if spec_path is None:
            notes.append(
                "overwritten/missing-body not checked here — no `spec` param "
                "declared for this rule row (see the module docstring)")
        else:
            if not spec_path.is_file():
                raise RulesError(f"rule {self.rule_id!r}: no spec at {spec_path}")
            try:
                spec = port_spec.Spec.load(spec_path)
            except port_spec.SpecError as e:
                raise RulesError(
                    f"rule {self.rule_id!r}: cannot read spec {spec_path}: {e}") from e
            if not descriptor.namespace_root:
                raise RulesError(
                    f"rule {self.rule_id!r}: {ctx.repo}/codemap.yaml declares no "
                    f"`namespace_root` -- overwritten/missing-body need it to turn "
                    f"a dotted module into a path")
            ns = descriptor.namespace_root
            dist_pkg_src = target.code / ns
            if spec.unit_sources:
                notes.append(
                    "OVERWRITTEN skipped: the spec declares `unit_sources` — "
                    "destinations are unknowable without the source tree")
            else:
                for label, rel in check_overwritten(spec, ns, target.dir, dist_pkg_src):
                    v.append(Violation(self.rule_id, "overwritten", label,
                                       {"dist": self.dist, "path": rel}))
            for label, rel in check_missing_body(spec, ns, target.dir, dist_pkg_src):
                v.append(Violation(self.rule_id, "missing-body", label,
                                   {"dist": self.dist, "path": rel}))

        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes))


register_kind("landfall", languages=("python",))(require_params("dist")(LandfallCheck))
