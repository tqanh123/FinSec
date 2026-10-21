"""The TypeScript/JavaScript pack's resolver: a module is its repo path, an import is
resolved by the specifier's own path.

Naming walks each declared root for the pack's globs (`walk.walk_files`: a
`node_modules/` is never entered), keeps what git tracks or would track (a gitignored
build output is counted, not measured), and lists a file over
`languages.typescript.max_file_bytes` (default 1 MiB) in `skipped_files` instead of
parsing it -- a bundle checked into the tree is not source. A file under the `outDir`
(or `declarationDir`) of any tsconfig the workspace holds is compiler output: counted
in `out_dir_skipped`, not measured, unless `exclude_out_dir: false`.

A module's default unit is the workspace package that owns it (`workspace`), as a path
under its root (`packages/kit`); a file no package below the root owns falls back to
its first directory under the root. The workspace manifests, every tsconfig read and
every bundler config an alias table came from are the pack's `config_inputs`: an
uncommitted edit to one is a dirty tree.

Each specifier is resolved by `resolve_static` and lands in one bucket of the record:
an `internal` target is an edge -- `imports_type_only` for a type-only import (never a
runtime edge), else `imports_function_local` inside a function, else
`imports_module_level`; it names the symbol bound (`<id>::<name>`, `<id>::default`), the
module for a namespace, side-effect, `export *`, `import()` or `require()`. An
`external` package (its name), a `builtin` (`node:<x>`) and an `asset`
(`asset:<path>`) go to `imports_external`. What is `unresolved`/`ambiguous` goes to
`imports_unresolved` with its reason code, and `target_pkg`/`candidates`/`notes` when
it has them; a target the tree holds but no declared root measures is
`outside_roots`. Option `aliases: false` turns the bundler alias tables off.

Option `resolver_command` (argv; `resolver_timeout_s`, default 120) is the project's own
resolver (`lang.resolver_command`, `hs-code-resolve/1`), asked once per pass about every
bare specifier BEFORE the pack resolves it: its answer wins (`via: project_resolver`),
`defer` hands the import back to the pack. The bindings, type-only and in-function
slots stay the reader's.
"""
from __future__ import annotations

import posixpath
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from ...layout import root_owns
from ...walk import git_listed, walk_files
from .. import registry as lang_registry
from .. import resolver_command
from ..base import ClassifiedImports, Named, SourceRef, build_symbol_entries
from . import aliases, boundary, resolve_static, tests, tsconfig, workspace

ID = "typescript"
DEFAULT_MAX_FILE_BYTES = 1 << 20
#: the import context's key for this pack's `resolve_static.Tree`
TREE = "typescript_tree"
#: ... and for the project's resolver command, when `resolver_command` declares one
COMMAND = "typescript_resolver_command"


def _bare(imp: Any) -> bool:
    """A literal specifier that is not a path (`./x`, `../x`, `/x`)."""
    return imp.spec is not None and not imp.spec.startswith((".", "/"))


def _payload(imp: Any) -> "dict[str, Any]":
    return {"kind": imp.kind, "spec": imp.spec, "line": imp.line, "type_only": imp.type_only,
            "in_function": imp.in_function, "bindings": [list(b) for b in imp.bindings]}


def _unique_flags(flags: "Sequence[dict[str, Any]]") -> "tuple[dict[str, Any], ...]":
    """`flags` once each, in order. Two imports of one target are one flag only when every
    key agrees: a deep import that bypasses `exports` beside a public one is two."""
    return tuple({tuple(sorted(f.items())): f for f in flags}.values())


def _from_answer(a: Any) -> "resolve_static.ResolveResult":
    via = ("project_resolver",)
    if a.status in ("unresolved", "ambiguous"):
        return resolve_static.ResolveResult(a.status, reason=a.reason, candidates=a.candidates, via=via)
    return resolve_static.ResolveResult(a.status, target=a.target, via=via)


def _targets(imp: Any, module: str) -> "list[tuple[str, bool]]":
    """(edge target, type_only) per binding of one resolved import."""
    if not imp.bindings:
        return [(module, imp.type_only)]
    return [(module if name == "*" else f"{module}::{name}", imp.type_only or type_only)
            for name, _local, type_only in imp.bindings]


def _under(rel: str, d: str) -> bool:
    return d == "." or rel == d or rel.startswith(d + "/")


def _package_unit(maps: "list[workspace.WorkspaceMap]", rel: str, root_path: str) -> str:
    """The owning workspace package as a path under the root; "" when none below it."""
    pkg = workspace.owner_of(maps, rel)
    root_path = posixpath.normpath(root_path or ".")
    if pkg is None or pkg.dir == "." or pkg.dir == root_path or not _under(pkg.dir, root_path):
        return ""
    return pkg.dir if root_path == "." else posixpath.relpath(pkg.dir, root_path)


def workspaces(repo: Path, roots: list) -> "list[workspace.WorkspaceMap]":
    """The workspace of each declared root, once each."""
    out: "dict[str, workspace.WorkspaceMap]" = {}
    for r in roots:
        ws = workspace.discover(repo, posixpath.normpath(r.path or "."))
        out.setdefault(ws.root, ws)
    return list(out.values())


def configs(repo: Path, roots: list, descriptor: Any) -> "tsconfig.Configs":
    engine = lang_registry.options_of(descriptor, ID).get("engine", "tree-sitter")
    return tsconfig.Configs(repo, workspaces(repo, roots), engine=str(engine))


class TypeScriptResolver:
    """`typescript` in the language book."""

    def name_modules(self, repo: Path, roots: list, descriptor: Any, payload: Any) -> Named:
        pack = next(p for p in lang_registry.packs() if p.id == ID)
        opts = lang_registry.options_of(descriptor, ID)
        tests.check(opts, "codemap.yaml: languages.%s" % ID)
        cap = opts.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
        cfgs = configs(repo, roots, descriptor)
        out_dirs = cfgs.out_dirs() if opts.get("exclude_out_dir", True) else ()
        listed = git_listed(repo)
        listed_set = set(listed) if listed is not None else None
        tree = resolve_static.Tree.from_parts(repo, listed, cfgs, use_aliases=bool(opts.get("aliases", True)))
        notes = [] if listed is not None else [
            "typescript: not a git checkout -- gitignored files are measured like any other"]
        notes += [n for m in cfgs.maps for n in m.notes] + cfgs.notes + list(tree.notes)
        root_path = {r.id: r.path for r in roots}
        sources: "list[SourceRef]" = []
        skipped: "list[dict[str, Any]]" = []
        seen = ignored = 0
        out_hits: "dict[str, int]" = {}
        for r in roots:
            for p in walk_files(r.dir(repo), pack.include, pack.exclude):
                if not root_owns(p, r, roots, repo):
                    continue
                rel = p.relative_to(repo).as_posix()
                out_dir = next((d for d in out_dirs if _under(rel, d)), None)
                if out_dir is not None:
                    out_hits[out_dir] = out_hits.get(out_dir, 0) + 1
                    continue
                if listed_set is not None and rel not in listed_set:
                    ignored += 1
                    continue
                seen += 1
                size = p.stat().st_size
                if size > cap:
                    skipped.append({"path": rel, "bytes": size, "reason": "too_large"})
                    continue
                sources.append(SourceRef(
                    path=p, module=rel, root=r.id, unit="",
                    unit_name=_package_unit(cfgs.maps, rel, root_path.get(r.id, ".")), lang=ID,
                    role=tests.role_of(rel, opts),
                    payload=dict(payload.source_payload(repo, r, rel)) if payload else {}))
        census: "dict[str, Any]" = {"files_seen": seen, "skipped": len(skipped)}
        if ignored:
            census["ignored_skipped"] = ignored
        if out_hits:
            census["out_dir_skipped"] = {"files": sum(out_hits.values()), "dirs": sorted(out_hits)}
        tables: "dict[str, Any]" = {TREE: tree}
        if opts.get("resolver_command"):
            tables[COMMAND] = resolver_command.ResolverCommand(
                opts["resolver_command"], repo, what="languages.%s.resolver_command" % ID, language=ID,
                timeout=opts.get("resolver_timeout_s", resolver_command.TIMEOUT), allow_defer=True)
        return Named(sources, set(), notes, census, skipped, tables=tables)

    def config_inputs(self, repo: Path, roots: list, descriptor: Any) -> "tuple[str, ...]":
        """The repo files whose content changes how this pack names and resolves."""
        cfgs = tsconfig.Configs(repo, workspaces(repo, roots))
        bundler = aliases.config_files(resolve_static.repo_files(repo), [p.dir for m in cfgs.maps for p in m.packages]) \
            if lang_registry.options_of(descriptor, ID).get("aliases", True) else []
        cmd = lang_registry.options_of(descriptor, ID).get("resolver_command")
        import trust_store
        scripts = trust_store.command_files(repo, [cmd]) if cmd else []
        return tuple(sorted({x for m in cfgs.maps for x in m.inputs} | set(cfgs.inputs()) | set(bundler)
                            | set(scripts)))

    def ready(self, ctx: Any) -> None:
        cmd = ctx.tables.get(COMMAND)
        if cmd is not None:
            cmd.require_trust()

    def primes(self, ctx: Any) -> bool:
        """This pass asks a project resolver command (`prime`) before classifying."""
        return ctx.tables.get(COMMAND) is not None

    def prime(self, ctx: Any, parsed: "Sequence[Any]") -> None:
        """Ask the project's resolver command, once, about every bare specifier of `parsed`."""
        cmd = ctx.tables.get(COMMAND)
        if cmd is None:
            return
        files = [(p.module, [(i, _payload(imp)) for i, imp in enumerate(p.imports) if _bare(imp)])
                 for p in parsed]
        got = cmd.resolve_batch(sorted(ctx.all_mods), [f for f in files if f[1]])
        ctx.tables.setdefault(resolver_command.answers_key(ID), {}).update(
            {resolver_command.revision(p): got.get(p.module, {}) for p in parsed})

    def _answers(self, parsed: Any, ctx: Any) -> "dict[int, Any]":
        if ctx.tables.get(COMMAND) is None:
            return {}
        key = resolver_command.answers_key(ID)
        if resolver_command.revision(parsed) not in ctx.tables.get(key, {}):
            self.prime(ctx, [parsed])
        return ctx.tables[key][resolver_command.revision(parsed)]

    def prepare(self, repo: Path, sources: "Sequence[SourceRef]", roots: Any, import_roots: Any,
                all_mods: "set[str]") -> "dict[str, Any]":
        return {}

    def classify(self, parsed: Any, ctx: Any) -> ClassifiedImports:
        tree = ctx.tables[TREE]
        out = ClassifiedImports(set(), set(), set(), set(), set())
        unresolved: "set[tuple[str, str]]" = set()
        detail: "dict[tuple[str, str], dict[str, Any]]" = {}
        flags: "list[dict[str, Any]]" = []
        answers = self._answers(parsed, ctx)
        for i, imp in enumerate(parsed.imports):
            a = answers.get(i)
            if a is not None and a.status != "defer":
                res = _from_answer(a)
                flags += a.flags
            else:
                res = resolve_static.resolve(tree, parsed.module, imp)
            spec = imp.spec if imp.spec is not None else (imp.spec_expr or {}).get("text", "")
            if res.status == "internal" and res.target not in ctx.all_mods:
                res = replace(res, status="unresolved", reason="outside_roots", candidates=(res.target or "",))
            if res.status in ("internal", "external"):
                edges = _targets(imp, res.target) if res.status == "internal" and res.target else []
                flags += boundary.flags_for(res, edges, imp.type_only)
            if res.status == "internal" and res.target is not None:
                for target, type_only in _targets(imp, res.target):
                    if type_only:
                        out.type_only.add(target)
                    else:
                        (out.function_local if imp.in_function else out.module_level).add(target)
            elif res.status in ("external", "builtin"):
                out.external.add(res.target or spec)
            elif res.status == "asset":
                out.external.add("asset:%s" % res.target)
            else:
                key = (spec, res.reason)
                unresolved.add(key)
                extra = {k: v for k, v in (("target_pkg", res.target_pkg), ("candidates", list(res.candidates)),
                                           ("notes", list(res.notes))) if v}
                if extra:
                    detail[key] = {**detail.get(key, {}), **extra}
        return ClassifiedImports(out.module_level, out.function_local, out.external, out.type_only,
                                 out.ambiguous, frozenset(unresolved), detail,
                                 _unique_flags(flags))

    def module_at(self, ctx: Any, rel_path: str) -> "str | None":
        return rel_path

    def symbol_entries(self, parsed: Any) -> "list[dict]":
        return build_symbol_entries(parsed)

    def default_unit(self, rel_path: str, root_path: str) -> str:
        """Outside every workspace package: the first directory under the root; a file
        directly in it is its own unit."""
        rel = PurePosixPath(rel_path)
        if root_path not in ("", "."):
            rel = rel.relative_to(root_path)
        return rel.parts[0] if len(rel.parts) > 1 else rel.stem
