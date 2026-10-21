"""The snapshot pipeline — scan -> parse -> edges -> index -> write.

Template method: the SHAPE of a snapshot pass is fixed here (one pass over
every declared root, cross-root edges included, deterministic naming by
commit time, one unit file per unit, one sidecar index); formats render,
payload providers annotate.

WHY ONE PASS OVER ALL ROOTS
    A repo mid-port has two live trees; an edge from a module in the new tree
    into one still in the old tree is the ONLY thing that says what the port
    still owes. Scanning each root in its own pass throws that edge away —
    not red, not yellow, NONEXISTENT.

WHAT IT CANNOT SEE (inherited wholesale from the adapter; repeated because a
consumer reading only this file must not miss them):
    * `callers` is a LOWER BOUND — attribute calls, getattr, dict dispatch and
      callbacks do not resolve. Empty never means "nobody calls this".
    * Module-level names assigned several times keep only the FIRST entry
      (later ones listed in `dup_module_assign`).
    * Cross-root overlap is REPORTED here (via `descriptor.check_overlap`,
      `overlap_warnings`), never silently invisible -- but `_root_owns` (this
      module) already routes an overlapping file to whichever declared root
      is the DEEPEST path match, so it is scanned exactly once and the
      OUTER root never sees it. Nothing here double-counts a nested pair; the
      warning exists so a human can confirm the nest was deliberate.
    * A `workspace` root skips subdirectories with no `.py` files — it cannot
    distinguish an empty package from one excluded by `scan:`.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

import codegraph_snapshot_reader
import process_group
import secret_scan_before_ship as secret_scan

from ..adapter import AdapterError, AdapterRefused, AdapterSet, ImportRecord
from ..descriptor.model import (ProjectDescriptor, Root, as_scan_spec, check_overlap,
                                path_matches, roots_banner, scan_py, select_roots)
from ..layout import dotted_of, flat_leaf_index, spellings_of
from .path_drivers import find_path_drivers


class SnapshotError(Exception):
    """Honest cannot-run (maps to exit 2): bad path, unparseable file, no commit stamp."""


class SnapshotRefused(SnapshotError):
    """A deliberate stop (maps to exit 3): a project's own reader in an untrusted repo."""


def _interpreter_hint(repo: Path) -> str:
    """`running Python <major>.<minor>`, plus -- when `repo`'s own
    `pyproject.toml` declares `[project].requires-python` -- that value too,
    phrased as a hint to re-run under a matching interpreter. A REAL case
    this closes: a file valid PEP 758 syntax (`except A, B:`) under the
    project's declared floor raised `UNPARSEABLE` under an older interpreter
    running this pipeline, with no line anywhere saying the parser itself
    was the mismatch, not the source. Any read/parse error on the toml
    (missing file, bad table, no `requires-python` key) omits the declared
    half silently -- this is a hint, not a second descriptor, so it must
    never itself raise."""
    import sys
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    declared = None
    try:
        import tomllib
        data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data.get("project", {}).get("requires-python")
    except Exception:  # noqa: BLE001 — a hint must never itself raise
        declared = None
    if declared:
        return (f"running Python {running}; {repo / 'pyproject.toml'} declares "
               f"requires-python {declared!r} — re-run with a matching interpreter: "
               f"HARNESS_PYTHON names it for every harness launcher (a path, or a "
               f"command prefix such as `uv run --no-project --python <ver> "
               f"--with-requirements <harness>/requirements-runtime.txt python`; "
               f"behind a TLS-intercepting proxy add `--system-certs`, or "
               f"`--native-tls` on an older uv)")
    return f"running Python {running}"


@runtime_checkable
class PayloadProvider(Protocol):
    """Annotates units/files with DOMAIN payload the kernel carries verbatim.

    The kernel never interprets payload (it does not know what a `dist` or a
    `tier` is); the snapshot format of the owning domain renders it.
    """

    name: str

    def source_payload(self, repo: Path, root: Root, unit_name: str) -> dict[str, Any]:
        """Payload for every source of `unit_name` under `root` ({}) = none."""
        ...


@dataclass(frozen=True)
class SourceRef:
    """One measurable file, located in the tree shape (old `discover()` row)."""

    path: Path
    module: str
    root: str              # root id
    unit: str               # "<root-id>/<unit-name>"
    unit_name: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotResult:
    out_dir: Path
    snapshot: str
    commit: str
    totals: dict[str, int]
    roots_kept: list[str]
    roots_declared: list[str]
    banner: list[str] = field(default_factory=list)
    overlap_warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unit_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)


def _dotted_src(p: Path, rdir: Path, repo: "Path | None" = None,
                import_roots: "Sequence[str]" = ()) -> list[str]:
    """Module name parts for a `src`/`flat` root, via the ONE door (`layout.dotted_of`):
    the root's own PARENT is the effective import prefix (`rdir.name` becomes the
    first dotted segment) -- same math the inline version used to do by hand.
    Relative to `rdir.parent` directly (never to `repo`): a `path: "."` root's
    parent sits OUTSIDE `repo`, where `.relative_to(repo)` would raise.

    `import_roots` (a project's declared `codemap.yaml` list, when non-empty)
    is tried FIRST, matched repo-relative -- the same door `layout.dotted_of`
    uses for every OTHER reader of that declaration, so a namespace package
    needing a MORE SPECIFIC real sys.path entry than this root's own parent
    (`harness/scripts/rops`, `layout.py`'s own worked example) resolves
    through that entry here too. Falls back to the root-parent-anchored computation above
    (this function's only behavior before `import_roots` existed) when the
    declaration is empty or does not cover this path -- unchanged for every
    project that has not declared the field."""
    if import_roots and repo is not None:
        try:
            rel_repo = p.relative_to(repo).as_posix()
        except ValueError:
            rel_repo = None
        if rel_repo is not None:
            dotted = dotted_of(rel_repo, import_roots)
            if dotted:
                return dotted.split(".")
    dotted = dotted_of(p.relative_to(rdir.parent).as_posix(), ["."])
    return dotted.split(".") if dotted else []


def _dotted_ws(p: Path, code: Path, repo: "Path | None" = None,
              import_roots: "Sequence[str]" = ()) -> list[str]:
    """Module name parts for a `workspace` root's one package dir (`code`), via
    the same door: `code` itself is the effective import prefix. `import_roots`
    is consulted first, same rule and same reason as `_dotted_src`'s own."""
    if import_roots and repo is not None:
        try:
            rel_repo = p.relative_to(repo).as_posix()
        except ValueError:
            rel_repo = None
        if rel_repo is not None:
            dotted = dotted_of(rel_repo, import_roots)
            if dotted:
                return dotted.split(".")
    dotted = dotted_of(p.relative_to(code).as_posix(), ["."])
    return dotted.split(".") if dotted else []


def package_code_dir(package: Path, src_subdir: "str | None" = None) -> Path:
    """Where one `workspace` package keeps its modules: `<package>/<src_subdir>`
    when that directory exists — the declared `layout.src_subdir`, `src` when
    none is declared — else the package directory itself. DETECTED per
    package, not assumed: code directly under the package directory is legal
    and common outside uv, and one tree may mix both shapes. Every reader of a
    workspace package's code asks here, so no two of them judge different
    trees."""
    sub = package / (src_subdir or "src")
    return sub if sub.is_dir() else package


def workspace_packages(root_dir: Path) -> list[Path]:
    """The package directories of one `workspace` root, sorted."""
    return sorted(x for x in root_dir.iterdir() if x.is_dir() and not x.name.startswith("."))


def _root_owns(p: Path, r: Root, roots: "Sequence[Root]", repo: Path) -> bool:
    """True when `r` is the MOST SPECIFIC declared root (by path — longest
    match wins) covering `p` among `roots`. Reuses `dotted_of`'s own
    longest-prefix rule (never a second copy of that algorithm), applied to
    the declared roots' own PATHS instead of `import_roots` — so a file
    reachable through two overlapping roots (a namespace package's own root
    nested inside a coarser flat root, e.g. this repo's `rops` under
    `harness`) is scanned exactly once, by whichever root's path is the
    deepest match, never once per overlapping root. Without this, an
    overlapping pair produced two `SourceRef` rows for the SAME file which,
    once `import_roots` gave both the SAME correctly-resolved module name,
    silently collided in every dict keyed by module downstream — losing one
    root's data, not merely double-counting it (see the module docstring's
    own "cross-root overlap" note, which predates this rule and describes the
    OLDER, weaker symptom)."""
    try:
        rel = p.relative_to(repo).as_posix()
    except ValueError:
        return True
    all_paths = [x.path for x in roots]
    return dotted_of(rel, all_paths) == dotted_of(rel, [r.path])


def discover(repo: Path, roots: list[Root], scan: dict | None = None,
             payload: PayloadProvider | None = None,
             import_roots: "Sequence[str]" = (),
             src_subdir: "str | None" = None) -> tuple[list[SourceRef], set[str], list[str]]:
    """-> (sources, namespace-root names, notes), every root in ONE pass
    (order: declaration, then sorted subdirs, then sorted paths —
    deterministic; an EMPTY root contributes no namespace name).

    `import_roots` is the project descriptor's own declared list, threaded
    through to `_dotted_src`/`_dotted_ws` — see either's docstring.
    `src_subdir` is its declared `layout.src_subdir`, handed to
    `package_code_dir` for every `workspace` package. A file
    covered by more than one declared root is scanned only once, by the most
    specific (`_root_owns`); a residual module-name COLLISION after that
    (two DIFFERENT files whose `import_roots`-derived names still coincide —
    always possible when a bare-stem convention like pytest's rootless
    `conftest.py` meets a flat namespace) is a real ambiguity, never silently
    resolved: each colliding file falls back to its own full, repo-relative
    dotted path (guaranteed unique, the SAME `dotted_of(..., ["."])` door
    every no-import_roots project already uses) and `notes` carries one line
    naming what collided — the non-fatal shape `check_overlap`'s own
    `overlap_warnings` already established for a declared-root anomaly."""
    src: list[SourceRef] = []
    ns_roots: set[str] = set()
    for r in roots:
        rdir = r.dir(repo)
        if r.kind in ("src", "flat"):
            for p in scan_py(rdir, scan):
                if "__pycache__" in str(p):
                    continue
                if not _root_owns(p, r, roots, repo):
                    continue
                rel = p.relative_to(rdir)
                name = rel.parts[0] if len(rel.parts) > 1 else rel.stem
                parts = _dotted_src(p, rdir, repo, import_roots)
                ns_roots.add(rdir.name)
                src.append(SourceRef(
                    path=p, module=".".join(parts), root=r.id,
                    unit=f"{r.id}/{name}", unit_name=name,
                    payload=dict(payload.source_payload(repo, r, name)) if payload else {},
                ))
            continue

        # kind == "workspace": each subdirectory is one unit/package.
        for d in workspace_packages(rdir):
            code = package_code_dir(d, src_subdir)
            for p in scan_py(code, scan):
                if "__pycache__" in str(p):
                    continue
                if not _root_owns(p, r, roots, repo):
                    continue
                parts = _dotted_ws(p, code, repo, import_roots)
                ns_roots.add(parts[0])
                src.append(SourceRef(
                    path=p, module=".".join(parts), root=r.id,
                    unit=f"{r.id}/{d.name}", unit_name=d.name,
                    payload=dict(payload.source_payload(repo, r, d.name)) if payload else {},
                ))

    by_module: "dict[str, list[int]]" = defaultdict(list)
    for i, s in enumerate(src):
        by_module[s.module].append(i)
    notes: list[str] = []
    for mod, idxs in sorted(by_module.items()):
        if len(idxs) < 2:
            continue
        renamed: list[str] = []
        for i in idxs:
            full = dotted_of(src[i].path.relative_to(repo).as_posix(), ["."])
            src[i] = replace(src[i], module=full or src[i].module)
            renamed.append(str(src[i].path.relative_to(repo)))
        notes.append(
            f"⚠ {len(idxs)} files resolved to the same module {mod!r} through "
            f"import_roots (never the same file scanned twice — that is "
            f"`overlap_warnings`' own job) — disambiguated to their full "
            f"repo-relative path instead: {', '.join(sorted(renamed))}")
    # The first segment of each name a src/flat file was finally GIVEN: with
    # `import_roots` that is no longer the root directory's name (`lib/pkg/mod.py`
    # is `pkg.mod`), and a package of this tree left out reads as third-party.
    # Taken AFTER the collision rename: a colliding bare name (`util` in two
    # roots) is ambiguous, and counting it as a namespace would drop the import
    # before `ambiguous_imports` sees it.
    src_kinds = {r.id for r in roots if r.kind in ("src", "flat")}
    ns_roots.update(s.module.split(".")[0] for s in src if s.root in src_kinds)
    return src, ns_roots, notes


@dataclass(frozen=True)
class UnrootedTest:
    """One test-tree file no declared root measures (`unrooted_tests`)."""

    path: Path
    module: str


def git_listed(repo: Path) -> "list[str] | None":
    """Repo-relative files git tracks or would track (untracked, not ignored);
    None outside a git checkout. The ignore rules are what keep a `.venv` or
    a build dir out without a `scan.exclude` line per repo."""
    try:
        proc = process_group.run(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60, env=secret_scan.git_safe_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})


def _pytest_name(repo: Path, rel: str) -> "str | None":
    """The name pytest's default (`prepend`) import mode gives a test file:
    dotted from the first ancestor directory WITHOUT an `__init__.py` — the
    spelling a test uses to import a sibling helper (`from tests_a.helpers
    import g`)."""
    p = repo / rel
    base = p.parent
    while base != repo and (base / "__init__.py").is_file():
        base = base.parent
    return dotted_of(p.relative_to(base).as_posix(), ["."])


def unrooted_tests(repo: Path, descriptor: ProjectDescriptor, owned: "set[str]",
                 all_mods: "set[str]") -> "list[UnrootedTest]":
    """Test-tree files (`FilesLayout.in_test_tree`: tests, helpers, conftest)
    that no declared root measures — `owned` is the repo-relative paths the
    roots DID measure. The common case is a workspace dist keeping
    `tests_<x>/` BESIDE `src/`: the root measures `<dist>/src` alone, yet the
    tests import it, and every reverse walk ("which tests reach this file")
    needs them as nodes. They are no unit's code: written to `_index.json`
    under `test_modules`, never into a unit file.

    Same file filter as a root (`scan.include`/`scan.exclude`, matched on the
    repo-relative path) over git's view of the tree (tracked + untracked
    not-ignored; none outside a git checkout). Named the way pytest imports
    them (`_pytest_name`); a name two files share, or one a measured module
    already carries, falls back to the full repo-relative dotted path — the
    `discover` collision rule. `drift` enumerates through this same door."""
    from ..testfiles import build_test_layout

    listed = git_listed(repo)
    if not listed:
        return []
    layout = build_test_layout(repo, descriptor)
    spec = as_scan_spec(descriptor.scan.as_dict() if hasattr(descriptor.scan, "as_dict")
                        else descriptor.scan)
    inc = spec.include or ("*.py",)
    rels = [r for r in listed
            if r not in owned and layout.in_test_tree(r)
            and "__pycache__" not in r
            and any(path_matches(r, m) for m in inc)
            and not any(path_matches(r, m) for m in spec.exclude)
            and (repo / r).is_file()]
    named = [(r, _pytest_name(repo, r) or dotted_of(r, ["."]) or r) for r in rels]
    seen: "dict[str, int]" = defaultdict(int)
    for _r, n in named:
        seen[n] += 1
    out: "list[UnrootedTest]" = []
    for r, n in named:
        if seen[n] > 1 or n in all_mods:
            n = dotted_of(r, ["."]) or n
        out.append(UnrootedTest(path=repo / r, module=n))
    return out


def measured_paths(repo: Path, descriptor: ProjectDescriptor,
                   roots: "Sequence[Root] | None" = None) -> "tuple[set[str], set[str]]":
    """-> (root sources, test modules): the repo-relative files a snapshot pass
    over `roots` measures, enumerated by the SAME doors the pass uses
    (`discover`, `unrooted_tests`) — a filesystem walk, no parse. `drift`
    judges "new file" against this, so a snapshot taken a second ago is
    never stale by enumeration alone."""
    ctx = import_context(repo, descriptor, roots)
    owned = {s.path.relative_to(repo).as_posix() for s in ctx.sources}
    tests = {t.path.relative_to(repo).as_posix()
             for t in unrooted_tests(repo, descriptor, owned, set(ctx.all_mods))}
    return owned, tests


def spelling_aliases(repo: Path, sources: "Sequence[SourceRef]", import_roots: Sequence[str],
                     all_mods: set[str]) -> dict[str, str]:
    """Every OTHER name a module is importable as -> its name. A file under two
    `import_roots` entries (`.` and `harness/scripts`) is named by the more
    specific one (`grid.loop`), but an import may spell it through the other
    (`harness.scripts.grid.loop`). A spelling that is itself a module's name, or
    that two files share, is not an alias: which one wins depends on sys.path
    order, so it is left unresolved, never guessed."""
    seen: dict[str, set[str]] = defaultdict(set)
    for s in sources:
        try:
            rel = s.path.relative_to(repo).as_posix()
        except ValueError:
            continue
        for sp in spellings_of(rel, import_roots or ()):
            if sp != s.module and sp not in all_mods:
                seen[sp].add(s.module)
    return {sp: next(iter(ms)) for sp, ms in sorted(seen.items()) if len(ms) == 1}


def _canonical(parts: "tuple[str, ...]", all_mods: set[str], alias: dict[str, str]) -> "tuple[str, ...]":
    """`parts` with its longest aliased prefix rewritten to the module's name; a
    prefix that already names a module stops the search."""
    for i in range(len(parts), 0, -1):
        key = ".".join(parts[:i])
        if key in all_mods:
            return parts
        hit = alias.get(key)
        if hit:
            return tuple(hit.split(".")) + tuple(parts[i:])
    return parts


def edge_targets(rec: ImportRecord, ns_roots: set[str], all_mods: set[str],
                 flat_leaf: dict[str, list[str]] | None = None,
                 alias: dict[str, str] | None = None) -> list[str]:
    """Which targets one import statement points at. Empty = outside the tree.

    ALWAYS `<module>` or `<module>::<name>` — never a unit or package name:
    a unit split across packages made every edge into it unattributable, and
    a naive fallback ("dist of the FIRST module") can validate an edge into
    an unrelated tier as if it belonged — a green gate over something it
    never saw.

    `from X import y`: y a module -> `X.y`, else `X::y` (a symbol living in a
    DIFFERENT module than its file would otherwise fake-red the edge). X need
    not be a module: `from <package without __init__> import y` lands on `X.y`
    when that is one, before any shorter prefix of X that IS a module. Any spelling `alias` knows (`spelling_aliases`) is read
    as the module it names.
    Blind spot: `import a.b` then using `a.b.c` records the target as `a.b`
    only; the symbol side of that edge lives in `refs`, not here.

    FLAT ROOTS. A root declared `kind: flat` puts its modules on sys.path by
    DIRECTORY, so they import each other by bare name -- `import yaml_io`, not
    `import harness.scripts.yaml_io`. Such a name is not a namespace root, so
    without `flat_leaf` it fell through to "outside the tree" and landed in
    `imports_external` beside `os`. Measured on a 1207-module flat tree: every
    internal edge vanished and the tree read as having none. `flat_leaf` maps a
    bare leaf to the modules carrying it; a name with exactly ONE carrier is
    rewritten to that module and follows the normal path below. A name with
    SEVERAL carriers is NOT guessed -- which one wins at runtime depends on
    sys.path order, so it belongs in `ambiguous_imports`, not in an edge.
    """
    outs: list[str] = []
    alias = alias or {}
    for parts in rec.resolved:
        if not parts:
            continue
        parts = _canonical(tuple(parts), all_mods, alias)
        if parts[0] not in ns_roots:
            hit = (flat_leaf or {}).get(parts[0])
            if not hit or len(hit) != 1:
                continue
            parts = tuple(hit[0].split(".")) + tuple(parts[1:])
        # Down to ONE segment: naming by the most specific `import_roots` entry
        # gives top-level modules a one-part name (`yaml_io`), and a search
        # stopping at two dropped every bare import of one.
        base = next((".".join(parts[:i]) for i in range(len(parts), 0, -1)
                     if ".".join(parts[:i]) in all_mods), None)
        # `from P import M` with P no module of its own: try `P.M` BEFORE any shorter
        # prefix. That prefix can be a real module (`codegraph` for `from
        # codegraph.check import tree`), so falling back to it is not a pathless node
        # a group check would notice -- it is the wrong module, silently.
        if rec.stmt == "from" and rec.module is not None and base != ".".join(parts):
            subs = [".".join(_canonical(tuple(parts) + (a,), all_mods, alias))
                    for a in rec.names if a != "*"]
            found = [x for x in subs if x in all_mods]
            outs.extend(found)
            if found and len(found) == len(subs):
                continue
        if base is None:
            # No real module matched (namespace package, deleted module,
            # dynamic name): still return the `<pkg>.<unit>` shape so this
            # field has exactly ONE form.
            if len(parts) >= 2:
                outs.append(".".join(parts[:2]))
            continue
        if rec.stmt == "from" and rec.module is not None and base == ".".join(parts):
            for a in rec.names:
                if a == "*":
                    outs.append(base)
                elif f"{base}.{a}" in all_mods:
                    outs.append(f"{base}.{a}")
                else:
                    outs.append(f"{base}::{a}")
        else:
            outs.append(base)
    return outs


@dataclass(frozen=True)
class ImportContext:
    """What resolving ONE file's imports needs from the whole tree: its module
    set, namespace roots, flat-root leaves and spelling aliases. Built once by
    `import_context` (a filesystem walk, no parse); `take_snapshot` reads every
    file through it, and a caller asking about a few files reads those."""
    repo: Path
    sources: "tuple[SourceRef, ...]"
    ns_roots: "frozenset[str]"
    all_mods: "frozenset[str]"
    alias: "dict[str, str]"
    flat_leaf: "dict[str, list[str]]"
    notes: "tuple[Any, ...]" = ()
    roots: "tuple[Root, ...]" = ()
    import_roots: "tuple[str, ...]" = ()
    src_subdir: "str | None" = None
    adapters: "AdapterSet | None" = None     # None: the bin's book alone

    def reader(self, path: Path):
        """The adapter for `path` in this tree (the project's readers included)."""
        return (self.adapters or AdapterSet.for_repo(self.repo)).adapter_for(path)

    def file_of(self, module: str) -> "Path | None":
        """The file the walk named `module`, or None."""
        return next((s.path for s in self.sources if s.module == module), None)

    def module_at(self, rel_path: str) -> "str | None":
        """The module the walk named for `rel_path`; for a path it did not see
        (a file since deleted) the name `discover` WOULD give it under the root
        that owns it, or None when no declared root owns it."""
        for s in self.sources:
            if s.path.relative_to(self.repo).as_posix() == rel_path:
                return s.module
        p = self.repo / rel_path
        if p.suffix != ".py":
            return None
        for r in self.roots:
            rdir = r.dir(self.repo)
            if rdir not in p.parents or not _root_owns(p, r, self.roots, self.repo):
                continue
            if r.kind in ("src", "flat"):
                return ".".join(_dotted_src(p, rdir, self.repo, self.import_roots))
            for d in workspace_packages(rdir):
                code = package_code_dir(d, self.src_subdir)
                if code in p.parents:
                    return ".".join(_dotted_ws(p, code, self.repo, self.import_roots))
        return None


def import_context(repo: Path, descriptor: ProjectDescriptor, roots: "Sequence[Root] | None" = None,
                   payload: "PayloadProvider | None" = None) -> ImportContext:
    roots = list(roots if roots is not None else descriptor.roots)
    sources, ns_roots, notes = discover(
        repo, roots,
        descriptor.scan.as_dict() if hasattr(descriptor.scan, "as_dict") else descriptor.scan,
        payload, descriptor.import_roots, descriptor.src_subdir)
    all_mods = {s.module for s in sources}
    alias = spelling_aliases(repo, sources, descriptor.import_roots or (), all_mods)
    flat_leaf = flat_leaf_index({r.id: r.dir(repo) for r in roots if r.kind == "flat"},
                                ((_s.root, _s.path, _s.module) for _s in sources))
    return ImportContext(repo=repo, sources=tuple(sources), ns_roots=frozenset(ns_roots),
                         all_mods=frozenset(all_mods), alias=alias, flat_leaf=flat_leaf,
                         notes=tuple(notes), roots=tuple(roots),
                         import_roots=tuple(descriptor.import_roots or ()),
                         src_subdir=descriptor.src_subdir,
                         adapters=_adapter_set(repo, descriptor))


def _adapter_set(repo: Path, descriptor: ProjectDescriptor) -> AdapterSet:
    try:
        return AdapterSet.for_repo(repo, tuple(getattr(descriptor, "adapters", ()) or ()))
    except AdapterError as e:
        raise SnapshotError(str(e)) from e


def _parse(adapter: Any, path: Path, text: str, container: "tuple[str, ...]", module: str) -> Any:
    """One file through its reader; a reader outside its contract is this pass's error."""
    try:
        return adapter.parse(path, text, container, module)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    except AdapterError as e:
        raise SnapshotError(str(e)) from e


@dataclass(frozen=True)
class ClassifiedImports:
    module_level: "set[str]"
    function_local: "set[str]"
    external: "set[str]"
    type_only: "set[str]"
    ambiguous: "set[tuple[str, tuple[str, ...]]]"


def classify_imports(parsed: "Any", ctx: ImportContext) -> ClassifiedImports:
    """One parsed file's imports, split module-level / function-local /
    external / type-only, plus the bare names two flat modules share."""
    ns_roots, all_mods, flat_leaf, alias = set(ctx.ns_roots), set(ctx.all_mods), ctx.flat_leaf, ctx.alias
    out = ClassifiedImports(set(), set(), set(), set(), set())
    for rec in parsed.imports:
        if rec.under_type_checking:
            # never loaded at runtime, so no load-order edge; still coupling, which a
            # layer law may count (`forbidden_edge`, `include_type_only`)
            out.type_only.update(edge_targets(rec, ns_roots, all_mods, flat_leaf, alias))
            continue
        for parts in rec.resolved:
            if parts and parts[0] not in ns_roots:
                hit = flat_leaf.get(parts[0])
                if hit and len(hit) > 1:
                    out.ambiguous.add((parts[0], tuple(hit)))
        for tgt in edge_targets(rec, ns_roots, all_mods, flat_leaf, alias):
            # A module record answers "what does THIS file import", so a
            # sibling in the same unit belongs here like any other target.
            # The group-level rule ("an edge inside a group is not a group
            # edge") is applied where the group question is asked, by
            # `coupling.judge` dropping `src == dst`. Enforcing it here
            # instead deleted the edge from every reader, and a flat
            # directory — one unit, the layout this tool exists to
            # untangle — lost its entire internal structure that way.
            (out.function_local if rec.in_function else out.module_level).add(tgt)
        for t in rec.resolved:
            # A bare name carried by this tree's flat roots is INTERNAL,
            # resolved or not: an ambiguous name is still not a third-party
            # dependency, and listing it beside `os` is what made a whole
            # flat tree read as having no internal edges at all.
            if (t and t[0] not in ns_roots and t[0] != "__future__"
                    and t[0] not in flat_leaf):
                out.external.add(t[0])
    return out


def module_imports(ctx: ImportContext, rel_path: str, text: str,
                   module: "str | None" = None) -> "set[str] | None":
    """The modules of this tree one file's TEXT imports at runtime (module-level
    and function-local, `mod::name` as `mod`, type-only left out — the edge set
    `codegraph.check.tree.module_graph` reads off a snapshot), resolved in
    `ctx`. `module` names the file when `ctx` does not hold it (a revision of
    a file since moved); None when neither names it or no adapter reads it.
    Raises `SyntaxError` like the pipeline's own parse."""
    path = ctx.repo / rel_path
    adapter = ctx.reader(path)
    module = module or ctx.module_at(rel_path)
    if adapter is None or module is None:
        return None
    parts = module.split(".")
    container = tuple(parts) if path.name == "__init__.py" else tuple(parts[:-1])
    imp = classify_imports(_parse(adapter, path, text, container, module), ctx)
    return {t.split("::", 1)[0] for t in imp.module_level | imp.function_local
            if t.split("::", 1)[0] in ctx.all_mods and t.split("::", 1)[0] != module}


def build_symbol_entries(parsed: "Any") -> "list[dict]":
    """The per-module `symbols` list a snapshot record carries -- one dict
    per top-level func/class (`name`/`kind`/`lines`/`sha`/`refs`, plus
    `bases` for a class that has any), methods nested the same way. Factored out of the per-file loop below (the ONE
    place that used to build this shape) so a caller measuring a SINGLE file
    outside a full pipeline pass -- `codegraph._core.snapshot_bridge`, for a
    test file living outside every declared root -- builds the IDENTICAL
    shape without re-implementing it. `parsed` is one adapter's
    `ModuleSymbols` (`adapter.parse()`'s own return); this function reads
    only `.symbols`, never re-parses anything."""
    syms: list[dict] = []
    for sym in parsed.symbols:
        entry: dict = {"name": sym.name, "kind": sym.kind,
                       "lines": [sym.lines[0], sym.lines[1]], "sha": sym.sha,
                       "refs": list(sym.refs)}
        if sym.bases:
            entry["bases"] = list(sym.bases)
        if sym.methods:
            entry["methods"] = [
                {"name": m.name, "kind": m.kind,
                 "lines": [m.lines[0], m.lines[1]],
                 "sha": m.sha, "refs": list(m.refs)}
                for m in sym.methods
            ]
        syms.append(entry)
    return syms


def commit_stamp(repo: Path, commit: str | None) -> tuple[str, str]:
    """-> (commit, <yyyymmdd>-<hhmmss> stamp from COMMIT time, not run time).

    The stamp comes from `git log` of the repo: rerunning on the same commit
    yields the same directory name, so two runs never fork two directories for
    one tree state. A missing stamp is an ERROR — an unnameable snapshot would
    break that guarantee silently. (Blind spot: a repo with rewritten dates
    re-stamps; the tree identity is the commit hash, not the stamp.)
    """
    if commit is None:
        try:
            commit = subprocess.check_output(
                ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "rev-parse", "HEAD"],
                text=True, env=secret_scan.git_safe_env(),
            ).strip()
        except Exception:  # noqa: BLE001 — same posture as the upstream reference tool
            commit = "unknown"
    try:
        stamp = subprocess.check_output(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "log", "-1",
             "--date=format:%Y%m%d-%H%M%S", "--pretty=%ad", commit],
            text=True, env=secret_scan.git_safe_env(),
        ).strip()
    except Exception as exc:  # noqa: BLE001
        raise SnapshotError(f"cannot read commit date — no deterministic snapshot name: {exc}") from exc
    if not stamp:
        raise SnapshotError("commit date came back empty — no deterministic snapshot name")
    return commit, stamp


def callers_of(by_unit: "dict[str, list[dict]]") -> "dict[str, list[str]]":
    """Reverse symbol->symbol index: each ref string, exactly as the adapter
    recorded it (qualified or bare), -> sorted unique `module::name` of every
    symbol or method whose refs name it. Written into `_index.json` as
    `callers`; read back by `codegraph.change.impact.callers_index`."""
    callers: "dict[str, list[str]]" = {}
    for mods in by_unit.values():
        for m in mods:
            for s_ in m["symbols"]:
                for holder in [s_] + s_.get("methods", []):
                    me_id = f"{m['module']}::{holder['name']}"
                    for r in holder.get("refs", []):
                        callers.setdefault(r, []).append(me_id)
    return {k: sorted(set(v)) for k, v in callers.items()}


def _measure_unrooted_tests(repo: Path, descriptor: ProjectDescriptor, ctx: ImportContext,
                            readers: AdapterSet) -> "tuple[list[dict], list[str], list[str]]":
    """-> (records, unparsed paths, notes) for `unrooted_tests`. A record has
    a unit record's shape plus `role: test`; its imports resolve against the
    measured modules AND the other test modules (a test importing its
    sibling helper is an edge the reverse walk needs). A file the running
    interpreter cannot parse is SKIPPED and named — a test tree legitimately
    holds broken-on-purpose fixtures, and one must not sink the whole pass —
    and its path is kept (`test_modules_unparsed`) so drift does not report
    it as new forever."""
    owned = {s.path.relative_to(repo).as_posix() for s in ctx.sources}
    tests = unrooted_tests(repo, descriptor, owned, set(ctx.all_mods))
    if not tests:
        return [], [], []
    try:
        readers.ready(t.path for t in tests)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    test_mods = {t.module for t in tests}
    tctx = replace(ctx, all_mods=ctx.all_mods | test_mods,
                   ns_roots=ctx.ns_roots | {m.split(".", 1)[0] for m in test_mods})
    records: "list[dict]" = []
    unparsed: "list[str]" = []
    no_adapter = 0
    for t in tests:
        rel = t.path.relative_to(repo).as_posix()
        adapter = readers.adapter_for(t.path)
        if adapter is None:
            no_adapter += 1
            continue
        parts = t.module.split(".")
        container = tuple(parts) if t.path.name == "__init__.py" else tuple(parts[:-1])
        try:
            parsed = _parse(adapter, t.path, t.path.read_text(encoding="utf-8", errors="replace"),
                            container, t.module)
        except (SyntaxError, SnapshotError):
            unparsed.append(rel)
            continue
        imp = classify_imports(parsed, tctx)
        rec: dict = {
            "path": rel, "root": "", "role": "test", "module": t.module,
            "loc": parsed.loc, "bytes": parsed.nbytes, "file_sha": parsed.file_sha,
            "imports_module_level": sorted(imp.module_level),
            "imports_function_local": sorted(imp.function_local),
            "imports_external": sorted(imp.external),
            "dup_module_assign": list(parsed.dup_module_assign),
            "ambiguous_imports": [{"name": n, "candidates": list(c)}
                                  for n, c in sorted(imp.ambiguous)],
            "symbols": build_symbol_entries(parsed),
            "payload": {}, "path_drivers": [], "invoked_by": [],
        }
        if imp.type_only:
            rec["imports_type_only"] = sorted(imp.type_only)
        records.append(rec)
    notes: "list[str]" = []
    if unparsed:
        notes.append(f"{len(unparsed)} test file(s) outside every root skipped: the running "
                     f"interpreter cannot parse them ({_interpreter_hint(repo)}): "
                     f"{', '.join(unparsed[:10])}{' …' if len(unparsed) > 10 else ''}")
    if no_adapter:
        notes.append(f"{no_adapter} test file(s) outside every root skipped: no registered "
                     f"language adapter")
    return sorted(records, key=lambda r: r["path"]), sorted(unparsed), notes


def take_snapshot(
    repo: Path,
    out_base: Path,
    descriptor: ProjectDescriptor,
    *,
    commit: str | None = None,
    root_filter: list[str] | None = None,
    fmt: Any = None,
    payload: PayloadProvider | None = None,
    mode: str = "port",
    force: bool = False,
    tool_stamp: "dict[str, Any] | None" = None,
    tree_hash: "str | None" = None,
    content_key: "str | None" = None,
    descriptor_sha: "str | None" = None,
) -> SnapshotResult:
    """Run the full pipeline. Deterministic bytes for the same tree+declaration.

    `fmt` is a `SnapshotFormat` (or a registry name resolved by the caller).
    Output goes to `out_base/<stamp>-<hash8>/`; an existing directory without
    `force` is an error; with `force` the previous pass's directories are
    REMOVED first — a rewrite is a clean sweep, never an overlay (a unit file
    of a dropped root would otherwise survive and read as part of the map).

    `tool_stamp` is the caller's OWN provenance identity for this pass
    (`{"version": ..., "hash": ...}`, e.g. `codegraph_snapshot_reader.
    compute_tool_stamp()`) — `None` (the default) writes no `tool` key at
    all, the shape a caller outside this harness (or a caller comparing
    byte-for-byte against a reference tool that never had this field) needs.
    When given, it is written into `_index.json` verbatim AND compared
    against the latest OTHER snapshot already sitting in `out_base` (see the
    mode law right below — same meeting point); a mismatch is appended to
    `notes`, once, computed here so no caller has to remember to ask.

    `tree_hash` is the CONTENT identity of the tree this pass measured —
    `git rev-parse HEAD^{tree}` of `repo` at write time (e.g.
    `codegraph._core.snapshot.worktree_key.head_tree_hash`). Like
    `tool_stamp`: `None` (the default) writes no `tree_hash` key at all.
    A caller (`hs-code graph snapshot --apply`) reads it back to decide
    whether an EXISTING snapshot may be handed back as-is instead of
    rebuilt — this function itself makes no such decision, it only carries
    the value the caller already computed into the written bytes.

    `content_key` is the reuse identity the CALLER already computed
    (`codegraph._core.snapshot.worktree_key.reuse_key`) — `None` (the
    default) writes no `content_key` key and names the directory exactly
    `<stamp>-<commit8>`, same as ever. When given, the plain
    `<stamp>-<commit8>` name is used FIRST (the common case: nothing else
    occupies it yet, so a lone build at a commit is byte-identical to a
    caller that never heard of content keys — a byte-for-byte comparison
    against a reference tool with no reuse concept, e.g. `harness/tests/
    codegraph/test_snapshot_matches_frozen_upstream.py`, depends on this).
    Only when that plain name is ALREADY occupied — necessarily by a
    DIFFERENT content key, since an exact-key match is the caller's own job
    to catch earlier via `store.find_reusable` before ever reaching here —
    does the content key get appended (`<stamp>-<commit8>-
    <content_key[:12]>`), so two content-DIFFERENT builds at the SAME
    commit (two dirty edits, say) land in two DIFFERENT directories instead
    of the second silently overwriting the first (a `force` collision only
    ever happens now when the content key genuinely repeats).

    `descriptor_sha` is the descriptor FILE's own byte hash
    (`codegraph_snapshot_reader.descriptor_file_sha`) — same omit-when-`None`
    rule. A reader holding no descriptor loader (the hs-run facts engine)
    compares it with the file on disk now: a snapshot measured under another
    `codemap.yaml` is stale, not fresh.
    """
    from .format import SnapshotFormat, format_for  # local import: protocol only, no cycle

    if fmt is None or isinstance(fmt, str):
        fmt = format_for(fmt)

    repo = repo.resolve()
    if not repo.is_dir():
        raise SnapshotError(f"no such repo {repo}")

    roots = list(descriptor.roots)
    if not roots and descriptor.auto_discover:
        from ..descriptor.autodiscover import discover_roots
        discovered, rule = discover_roots(repo)
        roots = list(discovered)
        if not roots:
            raise SnapshotError(f"auto-discover found no roots under {repo} (rule: {rule})")

    all_ids = [r.id for r in roots]
    if root_filter:
        unknown = [x for x in root_filter if x not in all_ids]
        if unknown:
            raise SnapshotError(
                f"--root {', '.join(unknown)} not in layout.roots; available: {', '.join(all_ids)}")
    roots_kept, why = select_roots(roots, root_filter)
    banner = roots_banner(roots_kept, len(all_ids), why)
    overlap_warnings = [
        f"⚠ root {inner.id!r} ({inner.path}) nests inside root {outer.id!r} ({outer.path}) "
        f"— {inner.id!r} owns every file under it (the deepest matching path wins), scanned "
        f"once each; {outer.id!r} does not see them"
        for outer, inner in check_overlap(roots_kept)
    ]
    # the tree's readers, asked before anything is written: a refused reader
    # leaves no half-written snapshot behind
    ctx = import_context(repo, descriptor, roots_kept, payload)
    readers = ctx.adapters or AdapterSet.for_repo(repo)
    try:
        readers.ready(s.path for s in ctx.sources)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e

    commit, stamp = commit_stamp(repo, commit)
    dirname = f"{stamp}-{commit[:8]}"
    out = out_base / dirname
    if content_key and out.exists():
        # The plain name may already be occupied by a snapshot of the SAME
        # content key (a rebuild -- e.g. a different `tool_stamp`, which
        # `store.find_reusable` deliberately refuses to hand back as a
        # reuse) or a DIFFERENT one (a distinct dirty edit at this commit).
        # Only the second case needs its own directory; the first must
        # still overwrite in place, matching `force`'s existing "rebuild at
        # this commit sweeps this commit's own directory" law.
        from .store import SnapshotMissing, load_dir
        try:
            existing_key = load_dir(out, record=False).get("content_key")
        except SnapshotMissing:
            existing_key = None  # unreadable neighbour: never claim its identity
        if existing_key != content_key:
            dirname = f"{dirname}-{content_key[:12]}"
            out = out_base / dirname

    # ── the two mode laws ──
    # ONE BASE, ONE ROLE: a base holding snapshots of the other role refuses
    # the pass. Mixing roles changes the DENOMINATOR under readers that
    # already resolved "latest", while every printed number still looks
    # perfectly normal. Snapshots predating the `mode` field count as
    # `port` (`store.snapshot_mode`).
    from .store import SnapshotMissing, load_dir, snapshot_dirs, snapshot_mode
    # Same meeting point the tool stamp is judged at: whichever OTHER
    # snapshot sorts last (== newest by commit-time name) before this pass
    # writes its own — `None` when this base holds nothing else yet, or
    # nothing else this pass can even read.
    latest_other_index: "dict[str, Any] | None" = None
    latest_other_name = ""
    for other in snapshot_dirs(out_base):
        # the TARGET dir itself is included on purpose: a daily pass onto a
        # frozen milestone's name must refuse, not silently replace it
        try:
            other_index = load_dir(other, record=False)
        except (SnapshotMissing, ValueError):
            continue          # a broken neighbour is not this law's subject
        other_mode = snapshot_mode(other_index)
        if other_mode != mode:
            raise SnapshotError(
                f"{out_base} holds `{other_mode}` snapshots ({other.name}) "
                f"but this pass runs `{mode}` — one base, one role. The two "
                f"roles differ in the DENOMINATOR (frozen milestone vs "
                f"today's tree); write to a different base instead.")
        if other.resolve() != out.resolve():
            latest_other_index = other_index   # snapshot_dirs is sorted; last wins
            latest_other_name = other.name

    tool_mismatch_note: "str | None" = None
    if tool_stamp is not None and latest_other_index is not None:
        # module-level import above — same door store.py already uses
        tool_mismatch_note = codegraph_snapshot_reader.stamp_mismatch(
            {"tool": tool_stamp}, latest_other_index,
            labels=(f"this snapshot, by {codegraph_snapshot_reader.current_code_label()}",
                    f"the latest stored one ({latest_other_name})"))

    if mode == "daily" and not force:
        # ROLLING HEAD: re-measuring today's tree is the daily pass's NORMAL
        # job, and a fresh checkout or a `touch` must not silence a real
        # drift by erroring the pass into force-habit. Sweep in place —
        # same clean-sweep semantics as `force`, entered implicitly.
        force = True
    if out.exists() and not force:
        raise SnapshotError(f"snapshot {out} already exists — pass force if you truly mean to overwrite")

    sources, discover_notes = list(ctx.sources), list(ctx.notes)
    if not sources:
        raise SnapshotError(f"no source files found under {len(roots_kept)} root(s) of {repo}")

    mod2unit = {s.module: s.unit for s in sources}

    by_unit: dict[str, list[dict]] = defaultdict(list)
    unit_payloads: dict[str, dict[str, Any]] = {}
    totals = {"modules": 0, "symbols": 0, "methods": 0, "unparseable": 0}
    # Split once, through the registry's own measurable/skipped door -- the
    # SAME split `adapter-blindspots` reports -- so the count below can never
    # silently disagree with what the registry itself would say is unmeasured.
    _, _skipped = readers.split(s.path for s in sources)
    skipped_no_adapter = len(_skipped)
    _skip_paths = set(_skipped)

    for srcinfo in sources:
        if srcinfo.path in _skip_paths:
            continue
        adapter = readers.adapter_for(srcinfo.path)
        assert adapter is not None, (  # guaranteed by the readers.split() above
            f"{srcinfo.path} passed the measurable split but has no adapter")
        text = srcinfo.path.read_text(encoding="utf-8", errors="replace")
        # container: for __init__.py the package itself, else the parent package
        parts = srcinfo.module.split(".")
        if srcinfo.path.name == "__init__.py":
            container = tuple(parts)
        else:
            container = tuple(parts[:-1])
        try:
            parsed = _parse(adapter, srcinfo.path, text, container, srcinfo.module)
        except SyntaxError as e:
            raise SnapshotError(
                f"UNPARSEABLE: {srcinfo.path}: {e} ({_interpreter_hint(repo)})") from e

        # ---- imports: module-level vs function-local, external roots --------
        imp = classify_imports(parsed, ctx)
        imp_mod, imp_fn, imp_ext, imp_type, ambiguous = (
            imp.module_level, imp.function_local, imp.external, imp.type_only, imp.ambiguous)

        # ---- symbols ---------------------------------------------------------
        syms = build_symbol_entries(parsed)
        for sym in parsed.symbols:
            totals["unparseable"] += (sym.sha == "unparseable")
            totals["unparseable"] += sum(m.sha == "unparseable" for m in sym.methods)

        totals["modules"] += 1
        totals["symbols"] += len(parsed.symbols)
        totals["methods"] += sum(len(s.methods) for s in parsed.symbols)
        rec_out: dict = {
            "path": str(srcinfo.path.relative_to(repo)),
            "root": srcinfo.root,
            "module": srcinfo.module,
            "loc": parsed.loc,
            "bytes": parsed.nbytes,
            "file_sha": parsed.file_sha,
            "imports_module_level": sorted(imp_mod),
            "imports_function_local": sorted(imp_fn),
            "imports_external": sorted(imp_ext),
            "dup_module_assign": list(parsed.dup_module_assign),
            "ambiguous_imports": [{"name": n, "candidates": list(c)}
                                  for n, c in sorted(ambiguous)],
            "symbols": syms,
            "payload": dict(srcinfo.payload),   # domain carriage, verbatim
        }
        if imp_type:
            # written only when present, like `invoked_by`: a record without type-only
            # imports keeps the bytes it always had
            rec_out["imports_type_only"] = sorted(imp_type)
        by_unit[srcinfo.unit].append(rec_out)
        unit_payloads.setdefault(srcinfo.unit, srcinfo.payload)

    # ---- the OTHER edge kind: invoked by path, never imported ---------------
    # A script tree's callers are lines in prose, config and shell, which no
    # adapter reads. Measured on a real harness: 79 of 119 CLI modules had no
    # production importer and 105 were named by path. Left out, those edges do
    # not exist for anything downstream — a move plan would silently orphan
    # every one of them. See path_drivers.BLIND for what this still cannot see.
    drv = find_path_drivers(
        repo, {s_.module: s_.path.relative_to(repo).as_posix() for s_ in sources},
        getattr(descriptor, "drivers", None))
    for mods_ in by_unit.values():
        for m_ in mods_:
            m_["path_drivers"] = drv["drivers"].get(m_["module"], [])
            # The subset whose line carries an execution context. Separate
            # because "what a move must fix" and "what still runs" are two
            # questions, and 4 of every 5 references answer only the first.
            m_["invoked_by"] = drv["invoked"].get(m_["module"], [])

    callers = callers_of(by_unit)

    fmt_obj = fmt if not isinstance(fmt, str) else _resolve_format(fmt)
    if not isinstance(fmt_obj, SnapshotFormat):
        raise SnapshotError("format does not satisfy the SnapshotFormat protocol")

    test_modules, test_unparsed, test_notes = _measure_unrooted_tests(repo, descriptor, ctx, readers)

    # ---- the store is touched only from here on --------------------------
    # Everything above can still raise (an unparseable file, no sources); a
    # pass that dies must leave the store exactly as it found it — no empty
    # directory reading as a snapshot, no daily head swept for nothing.
    if mode == "daily":
        # and ROLLING means ONE dir: older daily snapshots of other commits
        # are removed, so `resolve_snapshot(None)` can never pick a stale
        # day. (The mixing guard above already proved they are all daily.)
        for other in snapshot_dirs(out_base):
            if other.resolve() != out.resolve():
                shutil.rmtree(other)
    if out.exists():
        # Sweep EVERY root directory (one snapshot = one pass; mixing two passes
        # in one directory is a lying map), plus pre-layering legacy unit files.
        for rid in all_ids:
            if (out / rid).is_dir():
                shutil.rmtree(out / rid)
        legacy = sorted(x for x in out.glob("*.yaml") if x.name != "_tests.yaml")
        for x in legacy:
            x.unlink()
    out.mkdir(parents=True, exist_ok=True)

    # ---- write one file per unit -------------------------------------------
    for unit, mods in sorted(by_unit.items()):
        f = out / f"{unit}.yaml"            # unit contains '/' -> nests per root
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fmt_obj.unit_yaml({
            "unit": unit, "root": mods[0]["root"], "commit": commit,
            "mods": sorted(mods, key=lambda m: m["path"]),
            "unit_payload": unit_payloads.get(unit) or {},
            "schema_version": descriptor.schema_version,
        }), encoding="utf-8")

    mod_payload = {s.module: s.payload for s in sources if s.payload}
    # Only stamped alongside a REAL content key — see `format.py`'s own
    # `index_json` for why an unconditional wall-clock write would break
    # `test_deterministic_bytes_two_runs`'s byte-for-byte guarantee for every
    # caller, not just the ones that need a reuse tie-break. Captured ONCE,
    # in a variable: `_index.json` and the `_meta.json` sidecar below must
    # stamp the IDENTICAL wall-clock value, not two calls to `time.time()`
    # a few instructions apart that could read a different second.
    measured_at = time.time() if content_key is not None else None
    index_text = fmt_obj.index_json({
        "callers": callers,
        "snapshot": out.name,
        "mode": mode,
        "commit": commit,
        "package": descriptor.project,
        "roots": roots_kept,
        "roots_declared": all_ids,
        "mod_payload": mod_payload,
        "mod_unit": mod2unit,
        "ambiguous_path_refs": drv["ambiguous"],
        "driver_scan": {k: drv[k] for k in
                        ("files_scanned", "excluded", "stale_exclusions",
                         "yaml_fallback", "blind")},
        "unit_payloads": unit_payloads,
        "totals": {
            "units": len(by_unit),
            "modules": totals["modules"],
            "symbols_module_level": totals["symbols"],
            "methods": totals["methods"],
            "entries": totals["symbols"] + totals["methods"],
        },
        "units": {u: mods for u, mods in sorted(by_unit.items())},
        "tool": tool_stamp,
        "tree_hash": tree_hash,
        "content_key": content_key,
        "measured_at": measured_at,
        "descriptor_sha": descriptor_sha,
        "test_modules": test_modules,
        "test_modules_unparsed": test_unparsed,
    })
    (out / "_index.json").write_text(index_text, encoding="utf-8")
    # The cheap sidecar `hs-run`'s facts engine (and any other selection-only
    # reader) opens instead of this file: same values, a few hundred bytes
    # instead of this repo's own measured 14.4 MB. Written from the values
    # already in hand here, never by re-parsing `index_text` -- see
    # `codegraph_snapshot_reader.write_meta`'s own docstring for why.
    codegraph_snapshot_reader.write_meta(
        out, commit=commit, snapshot=out.name, package=descriptor.project,
        roots=[{"id": r.id, "path": r.path, "kind": r.kind} for r in roots_kept],
        tool=tool_stamp, tree_hash=tree_hash, content_key=content_key,
        measured_at=measured_at, descriptor_sha=descriptor_sha)

    notes: list[str] = list(discover_notes) + test_notes
    if skipped_no_adapter:
        notes.append(f"{skipped_no_adapter} file(s) skipped: no registered language adapter")
    if tool_mismatch_note:
        notes.append(tool_mismatch_note)
    if codegraph_snapshot_reader.recording_active():
        from .freshness import measured_row
        codegraph_snapshot_reader.record_measured(
            out, measured_row(repo, [r.path for r in roots_kept]))
    return SnapshotResult(
        out_dir=out, snapshot=out.name, commit=commit,
        totals={"units": len(by_unit), **totals,
                "entries": totals["symbols"] + totals["methods"],
                **({"test_modules": len(test_modules)} if test_modules else {})},
        roots_kept=[r.id for r in roots_kept], roots_declared=all_ids,
        banner=banner, overlap_warnings=overlap_warnings, notes=notes,
        unit_payloads=unit_payloads,
    )


def _resolve_format(name: str):
    from .format import format_for
    return format_for(name)
