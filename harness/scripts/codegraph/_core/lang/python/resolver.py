"""The Python pack's resolver: dotted module names, and what one import points at.

MOVED here from `snapshot/pipeline.py` and `incremental.py` (both rebind the old
names to these same objects): naming a tree's modules (`discover`, with
`import_roots`, flat and workspace roots and the collision rename), the names a
module is importable as (`spelling_aliases`), and resolving one parsed file's
imports against the tree (`edge_targets`, `classify_imports`). The pipeline reaches
them through `PythonResolver`, the class the language book names for `python`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ...adapter import ImportRecord
from ...descriptor.model import Root, scan_py
from ...layout import (dotted_of, flat_leaf_index, package_code_dir, root_owns as _root_owns,
                       spellings_of, workspace_packages)
from ..base import ClassifiedImports, Named, SourceRef, build_symbol_entries


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


def discover(repo: Path, roots: list[Root], scan: dict | None = None,
             payload: Any = None,
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


def classify_imports(parsed: "Any", ctx: Any) -> ClassifiedImports:
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


def module_at(ctx: Any, rel_path: str) -> "str | None":
    """The name `discover` WOULD give `rel_path` (a file the walk did not see -- one
    since deleted) under the root that owns it, or None when no declared root owns it."""
    p = ctx.repo / rel_path
    for r in ctx.roots:
        rdir = r.dir(ctx.repo)
        if rdir not in p.parents or not _root_owns(p, r, ctx.roots, ctx.repo):
            continue
        if r.kind in ("src", "flat"):
            return ".".join(_dotted_src(p, rdir, ctx.repo, ctx.import_roots))
        for d in workspace_packages(rdir):
            code = package_code_dir(d, ctx.src_subdir)
            if code in p.parents:
                return ".".join(_dotted_ws(p, code, ctx.repo, ctx.import_roots))
    return None


def touched_modules(files: Iterable[str],
                    module_at: "Callable[[str], str | None] | None" = None) -> set[str]:
    """Module names for the changed files.

    `module_at` (the tree's own `ImportContext.module_at`) names each file the
    way the snapshot does — `src/app/c.py` is `app.c` under a `src` root, and a
    path-built `src.app.c` matches no module, so the narrowed index would hold
    none of the change. A file no declared root owns maps to nothing: it is
    invisible to the full run alike.

    Without it, by path: `a/b/c.py` -> `a.b.c`; `a/b/__init__.py` -> BOTH `a.b`
    and `a.b.__init__` (an index may key either). Deliberately generous:
    over-inclusion keeps the verdict, and never losing a violation is the whole
    point here.
    """
    mods: set[str] = set()
    if module_at is not None:
        for f in files:
            m = module_at(f)
            if m:
                mods.add(m)
        return mods
    for f in files:
        p = Path(f)
        if p.suffix != ".py":
            continue
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            mods.add(".".join(parts[:-1]))
        if parts:
            mods.add(".".join(parts))
    return mods



class PythonResolver:
    """`python` in the language book."""

    def name_modules(self, repo: Path, roots: list, descriptor: Any, payload: Any) -> Named:
        """`discover` over `roots`; with `languages.python.max_file_bytes` declared
        (opt-in -- Python files are never capped by default) a file over it is listed
        in `skipped_files` and not measured."""
        scan = descriptor.scan.as_dict() if hasattr(descriptor.scan, "as_dict") else descriptor.scan
        sources, ns_roots, notes = discover(repo, roots, scan, payload, descriptor.import_roots,
                                            descriptor.src_subdir)
        cap = (getattr(descriptor, "languages", None) or {}).get("python", {}) or {}
        cap = cap.get("max_file_bytes")
        if cap is None:
            return Named(sources, ns_roots, notes)
        kept, skipped = [], []
        for s in sources:
            size = s.path.stat().st_size
            if size > cap:
                skipped.append({"path": s.path.relative_to(repo).as_posix(), "bytes": size,
                                "reason": "too_large"})
            else:
                kept.append(s)
        return Named(kept, ns_roots, notes,
                     {"files_seen": len(sources), "skipped": len(skipped)}, skipped)

    def prepare(self, repo: Path, sources: "Sequence[SourceRef]", roots: "Sequence[Root]",
                import_roots: "Sequence[str]", all_mods: "set[str]") -> "dict[str, Any]":
        """The tables `classify` reads: spelling aliases, flat-root leaves."""
        alias = spelling_aliases(repo, sources, import_roots or (), all_mods)
        flat_leaf = flat_leaf_index({r.id: r.dir(repo) for r in roots if r.kind == "flat"},
                                    ((_s.root, _s.path, _s.module) for _s in sources))
        return {"alias": alias, "flat_leaf": flat_leaf}

    def classify(self, parsed: Any, ctx: Any) -> ClassifiedImports:
        return classify_imports(parsed, ctx)

    def module_at(self, ctx: Any, rel_path: str) -> "str | None":
        return module_at(ctx, rel_path)

    def symbol_entries(self, parsed: Any) -> "list[dict]":
        return build_symbol_entries(parsed)
