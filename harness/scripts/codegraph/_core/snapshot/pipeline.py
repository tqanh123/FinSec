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

import contextlib
import shutil
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol, Sequence, runtime_checkable

import codegraph_snapshot_reader
import secret_scan_before_ship as secret_scan

from ..adapter import AdapterError, AdapterRefused, AdapterSet
from ..adapter import registry as adapter_registry
from ..descriptor.model import (ProjectDescriptor, Root, as_scan_spec, check_overlap,
                                path_matches, roots_banner, select_roots)
from ..lang import ids as lang_ids
from ..lang import registry as lang_registry
from ..lang import unit_policy
from ..lang.base import ClassifiedImports as ClassifiedImports  # noqa: PLC0414 -- re-export
from ..lang.base import BROKEN_PROBLEMS, Resolver, SourceRef as SourceRef  # noqa: PLC0414 -- re-export
from ..lang.base import build_symbol_entries as build_symbol_entries  # noqa: PLC0414
from ..lang.python import resolver as _python
from ..layout import dotted_of, package_code_dir as package_code_dir  # noqa: PLC0414
from ..layout import workspace_packages as workspace_packages  # noqa: PLC0414
from ..walk import git_listed as git_listed  # noqa: PLC0414 -- re-export
from .path_drivers import find_path_drivers

# Module naming (`discover`, `_dotted_src`, `_dotted_ws`, `_pytest_name`), the names a
# module is importable as (`spelling_aliases`, `_canonical`) and import resolution
# (`edge_targets`, `classify_imports`) MOVED to `lang.python.resolver`, the Python
# pack's resolver; `ClassifiedImports`, `SourceRef` and `build_symbol_entries` to
# `lang.base`, the seam; `package_code_dir`, `workspace_packages` and `_root_owns` to
# `layout`. Rebound to the SAME objects, never reimplemented beside them -- the
# pattern `testfiles.py` uses -- so every importer of the old names keeps working
# while the pipeline itself reaches a file's imports only through the pack that
# owns it (`ImportContext.resolver_for`).
_dotted_src = _python._dotted_src
_dotted_ws = _python._dotted_ws
discover = _python.discover
_pytest_name = _python._pytest_name
spelling_aliases = _python.spelling_aliases
_canonical = _python._canonical
edge_targets = _python.edge_targets
classify_imports = _python.classify_imports
_root_owns = _python._root_owns


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


@dataclass(frozen=True)
class UnrootedTest:
    """One test-tree file no declared root measures (`unrooted_tests`)."""

    path: Path
    module: str


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
    packs: "tuple[Any, ...]" = ()            # () : the packs the bin enables by default
    #: what each pack's `prepare` computed beyond `alias`/`flat_leaf`
    tables: "dict[str, Any]" = field(default_factory=dict)

    def _packs(self) -> "tuple[Any, ...]":
        return self.packs or lang_registry.enabled(None)

    def owns(self, rel_path: "Path | str") -> bool:
        """A pack reads `rel_path` as a module of the import graph."""
        return lang_registry.pack_for(rel_path, self._packs()) is not None

    def resolver_for(self, path: "Path | str") -> Resolver:
        """The resolver of the pack owning `path`. A file no pack owns -- one a
        project's `command:` reader parses -- carries dotted imports
        (`adapter.base.ImportRecord`), so the default pack's resolver reads it."""
        packs = self._packs()
        return (lang_registry.pack_for(path, packs) or lang_registry.default_of(packs)).resolver()

    def reader(self, path: Path):
        """The adapter for `path` in this tree (the project's readers included)."""
        return (self.adapters or AdapterSet.for_repo(self.repo)).adapter_for(path)

    @cached_property
    def _file_by_module(self) -> "dict[str, Path]":
        out: "dict[str, Path]" = {}
        for s in self.sources:
            out.setdefault(s.module, s.path)
        return out

    @cached_property
    def _module_by_rel(self) -> "dict[str, str]":
        out: "dict[str, str]" = {}
        for s in self.sources:
            out.setdefault(s.path.relative_to(self.repo).as_posix(), s.module)
        return out

    def file_of(self, module: str) -> "Path | None":
        """The file the walk named `module`, or None."""
        return self._file_by_module.get(module)

    def module_at(self, rel_path: str) -> "str | None":
        """The module the walk named for `rel_path`; for a path it did not see
        (a file since deleted) the name `discover` WOULD give it under the root
        that owns it, or None when no declared root owns it."""
        if rel_path in self._module_by_rel:
            return self._module_by_rel[rel_path]
        pack = lang_registry.pack_for(rel_path, self._packs())
        return pack.resolver().module_at(self, rel_path) if pack is not None else None


def import_context(repo: Path, descriptor: ProjectDescriptor, roots: "Sequence[Root] | None" = None,
                   payload: "PayloadProvider | None" = None) -> ImportContext:
    """What resolving the tree's imports needs (`ImportContext`). A pack refusing while
    it names its modules (an engine missing what it needs, asked for a tsconfig) is this
    pass's refusal (exit 3), a pack breaking its contract this pass's error (exit 2)."""
    try:
        return _import_context(repo, descriptor, roots, payload)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    except AdapterError as e:
        raise SnapshotError(str(e)) from e


def _import_context(repo: Path, descriptor: ProjectDescriptor, roots: "Sequence[Root] | None",
                    payload: "PayloadProvider | None") -> ImportContext:
    roots = list(roots if roots is not None else descriptor.roots)
    packs = lang_registry.enabled(descriptor)
    named = [(pack, pack.resolver().name_modules(repo, roots, descriptor, payload))
             for pack in packs]
    named = [(pack, replace(n, sources=_with_units(repo, descriptor, pack, n.sources, roots)))
             for pack, n in named]
    # a file another enabled pack owns is that pack's, even where the descriptor's `scan:`
    # globs hand it to the default pack's walk too (a project reader's suffix)
    others = [p for p in packs if p.id != lang_registry.DEFAULT_ID]
    named = [(pack, n if pack.id != lang_registry.DEFAULT_ID or not others else
              replace(n, sources=[s for s in n.sources if lang_registry.pack_for(s.path, others) is None]))
             for pack, n in named]
    sources = [s for _p, n in named for s in n.sources]
    ns_roots = {x for _p, n in named for x in n.ns_roots}
    notes = [t for _p, n in named for t in n.notes]
    try:
        lang_ids.check_ids(sources, repo)
    except AdapterError as e:
        raise SnapshotError(str(e)) from e
    all_mods = {s.module for s in sources}
    tables: "dict[str, Any]" = {}
    for pack, n in named:
        tables.update(pack.resolver().prepare(repo, n.sources, roots, descriptor.import_roots or (),
                                              all_mods))
    for _p, n in named:
        tables.update(n.tables)
    alias, flat_leaf = tables.pop("alias", {}), tables.pop("flat_leaf", {})
    tables["census"] = {pack.id: n.census for pack, n in named if n.census}
    tables["skipped_files"] = sorted((f for _p, n in named for f in n.skipped_files),
                                     key=lambda f: f["path"])
    return ImportContext(repo=repo, sources=tuple(sources), ns_roots=frozenset(ns_roots),
                         all_mods=frozenset(all_mods), alias=alias, flat_leaf=flat_leaf,
                         notes=tuple(notes), roots=tuple(roots),
                         import_roots=tuple(descriptor.import_roots or ()),
                         src_subdir=descriptor.src_subdir,
                         adapters=_adapter_set(repo, descriptor), packs=packs, tables=tables)


def _container(module: str, path: Path, lang: str) -> "tuple[str, ...]":
    """The dotted package a Python file sits in (an `__init__.py` is its own); a path id
    has none."""
    if lang != lang_ids.PYTHON:
        return ()
    parts = module.split(".")
    return tuple(parts) if path.name == "__init__.py" else tuple(parts[:-1])


def _languages(records: "list[dict]", census: "dict[str, dict[str, int]]") -> "dict[str, dict[str, int]]":
    """Modules per language, beside each pack's walk census (`files_seen`, `skipped`,
    `ignored_skipped`): `modules` alone is no rate without what the walk saw."""
    from .format import language_summary  # local: no cycle
    out = language_summary(records)
    for pid, counts in sorted(census.items()):
        row = out.setdefault(pid, {"modules": sum(lang_ids.lang_of(r) == pid for r in records)})
        row.update(counts)
    return out


def _with_units(repo: Path, descriptor: ProjectDescriptor, pack: Any,
                sources: "list[SourceRef]", roots: "Sequence[Root]") -> "list[SourceRef]":
    """`sources` of a pack whose modules are path ids, each in the unit the declared
    policy names (`languages.<id>.units`, else the pack's default). A Python unit is
    named while its modules are, by its root's layout."""
    if pack.id == lang_registry.DEFAULT_ID:
        return sources
    rules = unit_policy.parse_rules(lang_registry.options_of(descriptor, pack.id).get("units", []),
                                    f"languages.{pack.id}.units")
    resolver = pack.resolver()
    root_path = {r.id: r.path for r in roots}
    out = []
    for s in sources:
        rel = s.path.relative_to(repo).as_posix()
        # a pack may name the default unit while it names the module (a workspace package)
        name = unit_policy.assign(rel, rules, lambda p, _r=root_path.get(s.root, "."), _s=s:
                                  _s.unit_name or resolver.default_unit(p, _r))
        out.append(replace(s, unit=f"{s.root}/{name}", unit_name=name))
    return out


def _adapter_set(repo: Path, descriptor: ProjectDescriptor) -> AdapterSet:
    """The tree's readers: each enabled pack's (an engine's as declared), then the
    project's own `command:` readers. A declared engine whose wheel is missing is a
    refusal (exit 3), never a skip."""
    project = tuple(getattr(descriptor, "adapters", ()) or ())
    packs = tuple(p for p in lang_registry.enabled(descriptor) if not lang_registry.is_project(p))
    try:
        if all(p.reader_ref is not None for p in packs) and \
                {p.id for p in packs} == {lang_registry.DEFAULT_ID}:
            return AdapterSet.for_repo(repo, project)
        rows = adapter_registry.rows_for(packs, descriptor, repo)
        owned = {e for r in rows for e in r.extensions}
        clash = sorted({e for r in project for e in r.extensions} & owned)
        if clash:
            raise AdapterError("codemap.yaml `adapters:` claims %s, which an enabled language "
                               "pack reads" % ", ".join(clash))
        return AdapterSet.of(rows + project, repo)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
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


@contextlib.contextmanager
def _reading(readers: AdapterSet, paths: Iterable[Path]) -> Iterator[AdapterSet]:
    """`readers.reading(paths)`, a reader refusing or breaking as it opens being this
    pass's refusal (exit 3) or error (exit 2)."""
    try:
        with readers.reading(paths) as reading:
            yield reading
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    except AdapterError as e:
        raise SnapshotError(str(e)) from e


def _primes(resolver: Any, ctx: ImportContext) -> bool:
    """`resolver` asks a command about the whole pass before its first `classify`."""
    primes = getattr(resolver, "primes", None)
    return bool(primes(ctx)) if callable(primes) else hasattr(resolver, "prime")


def _prime(ctx: ImportContext, read: "Sequence[tuple[SourceRef, Any]]") -> None:
    """Hand each resolver that asks a command (`resolver_command`) every file it will
    classify this pass, once, before the first `classify` -- one process per pass, not
    one per file. A command refusing or breaking is this pass's refusal or error."""
    by_pack: "dict[str, tuple[Any, list[Any]]]" = {}
    for srcinfo, parsed in read:
        pack = lang_registry.pack_for(srcinfo.path, ctx._packs())
        if pack is not None and _primes(pack.resolver(), ctx):
            by_pack.setdefault(pack.id, (pack.resolver(), []))[1].append(parsed)
    try:
        for resolver, parsed_all in by_pack.values():
            resolver.prime(ctx, parsed_all)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    except AdapterError as e:
        raise SnapshotError(str(e)) from e


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
    pack = lang_registry.pack_for(path, ctx._packs())
    container = _container(module, path, pack.id if pack else lang_ids.PYTHON)
    parsed = _parse(adapter, path, text, container, module)
    broken = next((p_ for p_ in getattr(parsed, "problems", ()) if p_.get("kind") in BROKEN_PROBLEMS), None)
    if broken is not None:
        # a reader that reads around a syntax error: the edges it saw are not the file's
        raise SyntaxError("line %s" % broken.get("line"))
    try:
        imp = ctx.resolver_for(path).classify(parsed, ctx)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    except AdapterError as e:
        raise SnapshotError(str(e)) from e
    return {t.split("::", 1)[0] for t in imp.module_level | imp.function_local
            if t.split("::", 1)[0] in ctx.all_mods and t.split("::", 1)[0] != module}


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
        container = _container(t.module, t.path, lang_ids.PYTHON)  # unrooted tests: Python dotted names
        try:
            parsed = _parse(adapter, t.path, t.path.read_text(encoding="utf-8", errors="replace"),
                            container, t.module)
        except (SyntaxError, SnapshotError):
            unparsed.append(rel)
            continue
        resolver = tctx.resolver_for(t.path)
        imp = resolver.classify(parsed, tctx)
        rec: dict = {
            "path": rel, "root": "", "role": "test", "module": t.module,
            "loc": parsed.loc, "bytes": parsed.nbytes, "file_sha": parsed.file_sha,
            "imports_module_level": sorted(imp.module_level),
            "imports_function_local": sorted(imp.function_local),
            "imports_external": sorted(imp.external),
            "dup_module_assign": list(parsed.dup_module_assign),
            "ambiguous_imports": [{"name": n, "candidates": list(c)}
                                  for n, c in sorted(imp.ambiguous)],
            "symbols": resolver.symbol_entries(parsed),
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
    config_inputs: "Sequence[str] | None" = None,
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
    from .format import SnapshotFormat, format_for, snapshot_schema_version  # local: no cycle

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
        for pack in ctx._packs():
            ready = getattr(pack.resolver(), "ready", None)
            if ready is not None:
                ready(ctx)
    except AdapterRefused as e:
        raise SnapshotRefused(str(e)) from e
    except AdapterError as e:
        raise SnapshotError(str(e)) from e

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

    def _record(srcinfo: SourceRef, parsed: Any) -> dict:
        # ---- imports: module-level vs function-local, external roots --------
        resolver = ctx.resolver_for(srcinfo.path)
        imp = resolver.classify(parsed, ctx)
        imp_mod, imp_fn, imp_ext, imp_type, ambiguous = (
            imp.module_level, imp.function_local, imp.external, imp.type_only, imp.ambiguous)

        # ---- symbols ---------------------------------------------------------
        syms = resolver.symbol_entries(parsed)
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
        if srcinfo.lang != lang_ids.PYTHON:
            rec_out["lang"] = srcinfo.lang
        if srcinfo.role:
            rec_out["role"] = srcinfo.role
        if imp_type:
            # written only when present, like `invoked_by`: a record without type-only
            # imports keeps the bytes it always had
            rec_out["imports_type_only"] = sorted(imp_type)
        if imp.unresolved:
            rec_out["imports_unresolved"] = [{"spec": s, "reason": r, **imp.unresolved_detail.get((s, r), {})}
                                             for s, r in sorted(imp.unresolved)]
        if imp.flags:
            rec_out["import_flags"] = [dict(f) for f in imp.flags]
        if parsed.problems:
            rec_out["parse_problems"] = [dict(p) for p in parsed.problems]
        return rec_out

    # one pass: a reader that keeps a process (the `typescript` engine's bridge)
    # opens it once for every file below and closes it on the way out. A file whose
    # resolver primes (asks a command about the whole pass) waits for the read to
    # end; every other file is recorded as it is read and its text dropped.
    slots: "list[tuple[SourceRef, dict | None]]" = []
    read: "list[tuple[int, SourceRef, Any]]" = []
    with _reading(readers, (s.path for s in sources if s.path not in _skip_paths)) as reading:
        for srcinfo in sources:
            if srcinfo.path in _skip_paths:
                continue
            adapter = reading.adapter_for(srcinfo.path)
            assert adapter is not None, (  # guaranteed by the readers.split() above
                f"{srcinfo.path} passed the measurable split but has no adapter")
            text = srcinfo.path.read_text(encoding="utf-8", errors="replace")
            container = _container(srcinfo.module, srcinfo.path, srcinfo.lang)
            try:
                parsed = _parse(adapter, srcinfo.path, text, container, srcinfo.module)
            except SyntaxError as e:
                raise SnapshotError(
                    f"UNPARSEABLE: {srcinfo.path}: {e} ({_interpreter_hint(repo)})") from e
            if _primes(ctx.resolver_for(srcinfo.path), ctx):
                read.append((len(slots), srcinfo, parsed))
                slots.append((srcinfo, None))
            else:
                slots.append((srcinfo, _record(srcinfo, parsed)))
    _prime(ctx, [(srcinfo, parsed) for _i, srcinfo, parsed in read])
    for i, srcinfo, parsed in read:
        slots[i] = (srcinfo, _record(srcinfo, parsed))
    for srcinfo, rec in slots:
        assert rec is not None
        by_unit[srcinfo.unit].append(rec)
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
    all_records = [m for mods in by_unit.values() for m in mods] + list(test_modules)
    schema_version = snapshot_schema_version(all_records)
    languages = _languages(all_records, ctx.tables.get("census", {}))

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
            "schema_version": schema_version,
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
    config_inputs = list(lang_registry.config_inputs(repo, descriptor) if config_inputs is None
                         else config_inputs)
    index_text = fmt_obj.index_json({
        "callers": callers,
        "snapshot": out.name,
        "mode": mode,
        "commit": commit,
        "package": descriptor.project,
        "roots": roots_kept,
        "config_inputs": config_inputs,
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
        "schema_version": schema_version,
        "languages": languages,
        "skipped_files": ctx.tables.get("skipped_files", []),
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
        measured_at=measured_at, descriptor_sha=descriptor_sha, schema_version=schema_version,
        languages=languages, config_inputs=config_inputs)

    notes: list[str] = list(discover_notes) + test_notes
    if skipped_no_adapter:
        notes.append(f"{skipped_no_adapter} file(s) skipped: no registered language adapter")
    if tool_mismatch_note:
        notes.append(tool_mismatch_note)
    if codegraph_snapshot_reader.recording_active():
        from .freshness import measured_row
        codegraph_snapshot_reader.record_measured(
            out, measured_row(repo, [r.path for r in roots_kept] + config_inputs))
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
