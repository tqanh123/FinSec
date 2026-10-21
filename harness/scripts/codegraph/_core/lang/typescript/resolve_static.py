"""The static resolver of the TypeScript/JavaScript pack: one specifier written in one
file -> one `ResolveResult`, computed from the tree's files and manifests alone (no
`node_modules`, nothing executed). Where the answer depends on what cannot be read, the
result is `unresolved`/`ambiguous` with a reason code (`REASONS`) -- never a guess.

The steps, in order, for specifier `s` in file `F`:

0. Syntax: not a string literal -> `dynamic`; `http(s):`/`data:` -> external `url`;
   `virtual:`/`\\0` -> `bundler_virtual`; `a!b` -> `loader_syntax`; a `?query` (or a
   non-leading `#hash`) is cut, `via` says `query_stripped`.
1. Context: the workspace package owning F, the nearest package.json (its `type`,
   `imports`, self-name), the tsconfigs governing F (`tsconfig.Configs.governing`), the
   module resolution in effect, and the condition set -- `types`, `import` or
   `require` by the import's syntax (by the file's format under node16/nodenext),
   `node` under node16/nodenext, plus `customConditions`.
2. Builtin: `node:x`, or a name of `harness/data/node-builtins.json` that neither a
   `paths` key nor an alias entry claims.
3. A path (`./`, `../`; a rooted `/x` names no file of the tree): probed against the SET
   of the tree's files, case-sensitively, so every OS answers alike -- an ESM spelling
   substitutes (`.js` -> `.ts .tsx .d.ts .js .jsx`, `.mjs` -> `.mts .d.mts`, `.cjs` ->
   `.cts .d.cts`), an extensionless one tries `.ts .tsx .d.ts .js .jsx` (`allowJs`
   changes what the program accepts, never this order), the file before the directory
   (its package.json `types`/`main`, then `index`); never an extensionless `.mts`/`.cts`. Under
   node16/nodenext an ESM import must spell its extension. A miss tries `rootDirs`. A
   match only when case is ignored is `not_found` noted `case_mismatch`.
4. `#imports` of the nearest package.json (not under node10/classic).
5. Overrides, each computed on its own: `paths` (an exact key, else the longest prefix
   before `*`; substitutions in order; all missing falls through), legacy `baseUrl`
   (TypeScript < 7 or no version declared), the bundler alias table of F's package
   (`aliases`). Two that hit different targets are `alias_vs_paths`.
6. A package name: self-name (the scope's own `exports`), a workspace package
   (`exports` by the Node algorithm with the condition set; without `exports` --
   or under node10 -- `types`/`main`, `typesVersions`, `index`), else external (the
   package name; `node_modules` is never opened).

An `exports`/`main` target missing from the tree but under a tsconfig's `outDir` is
mapped back to its source under `rootDir` (`via: out_to_src`, noted `inferred`) when
exactly one source file answers; else `unbuilt_output`. Several governing tsconfigs,
or a module resolution that two TypeScript versions default differently, run the steps
once each: different answers are `multi_tsconfig` / `mr_default`.

Engine `typescript` (`Tree.backend`) asks the pinned compiler's `ts.resolveModuleName`
for steps 3-6 over a host that sees only the tree's files (`node.engine.ResolveBackend`);
the syntax filter, builtins, aliases, external packages, `declared` and outDir->rootDir
stay these steps'. Where the compiler finds nothing but these steps find a file, the
answer is `engine_disagrees` -- never the static guess in the compiler's place.

`declared` is how the importing package reaches a package name: `own:<field>`,
`ancestor:<pkg>:<field>`, `types_only` (only `@types/<name>`, for a type-only import)
or `undeclared`; `n/a` for a path.

WHAT IT CANNOT SEE
    * `moduleSuffixes` (React Native): any probe under it is `module_suffixes`.
    * `typesVersions` version ranges: the first range is taken.
    * Which pnpm `linkWorkspacePackages` is in force: a plain semver range on a
      workspace package's name is `workspace_or_registry`.
    * Yarn PnP, `browser` objects, `/// <reference>`, an implicit `react/jsx-runtime`.
"""
from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass, field, replace
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Sequence

from ... import regex_bound
from ...walk import git_listed
from . import aliases as alias_mod
from . import pkgjson, tsconfig, workspace
from .facts import TsImport
from .pkgjson import Pkg
from .tsconfig import TsConfig

REASONS = ("dynamic", "bundler_virtual", "loader_syntax", "not_found", "extension_required",
           "mode_unsupported", "not_exported", "no_matching_condition", "invalid_package_target",
           "unbuilt_output", "opaque_alias_target", "opaque_alias_scope", "alias_ignored",
           "tsconfig_extends_external", "multi_tsconfig", "mr_default", "alias_vs_paths", "rootDirs",
           "duplicate_workspace_name", "workspace_or_registry", "dir_manifest_broken", "module_suffixes")
BUILTINS = Path(__file__).resolve().parents[5] / "data" / "node-builtins.json"

MODULE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_TS = (".ts", ".tsx", ".d.ts")
_JS = (".js", ".jsx")
_SUBST = {".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"), ".jsx": (".tsx", ".d.ts", ".jsx"),
          ".mjs": (".mts", ".d.mts", ".mjs"), ".cjs": (".cts", ".d.cts", ".cjs")}
_VITE_ORDER = (".mjs", ".js", ".mts", ".ts", ".jsx", ".tsx", ".json")
_OUT = (".d.ts", ".d.mts", ".d.cts", ".js", ".mjs", ".cjs", ".jsx")
_SRC = (".ts", ".tsx", ".mts", ".cts")
_LINKED = ("workspace:", "link:", "file:", "portal:")
_HIT = ("internal", "asset")
_LEGACY_MR = ("node10", "classic")
_NODE_MR = ("node16", "nodenext")


@dataclass(frozen=True)
class ResolveResult:
    status: str                     # internal | external | builtin | asset | unresolved | ambiguous
    reason: str = ""
    target: "str | None" = None
    target_pkg: "str | None" = None
    via: "tuple[str, ...]" = ()
    declared: str = "n/a"
    runtime_resolvable: str = "unknown"
    candidates: "tuple[str, ...]" = ()
    trace: "tuple[str, ...]" = ()
    notes: "tuple[str, ...]" = ()

    def key(self) -> "tuple[str, str, str | None]":
        return self.status, self.reason, self.target


@lru_cache(maxsize=1)
def builtins() -> "tuple[frozenset[str], frozenset[str]]":
    """(names importable bare or with `node:`, names importable only with `node:`)."""
    doc = json.loads(BUILTINS.read_text(encoding="utf-8"))
    return frozenset(doc["builtins"]), frozenset(doc["prefix_only"])


def _walk(repo: Path) -> "list[str]":
    out = []
    for cur, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
        rel = Path(cur).relative_to(repo).as_posix()
        out += [f if rel == "." else rel + "/" + f for f in files]
    return out


def repo_files(repo: Path, listed: "Sequence[str] | None" = None) -> "frozenset[str]":
    """The tree's files: what git lists (`listed`, else asked), every file outside a
    checkout; never one under `node_modules`."""
    if listed is None:
        listed = git_listed(repo)
    return frozenset(f for f in (listed if listed is not None else _walk(repo))
                     if "node_modules" not in f.split("/"))


@dataclass(frozen=True)
class Tree:
    """What resolving needs from the whole tree, read once per snapshot."""
    repo: Path
    files: "frozenset[str]"
    maps: "Sequence[workspace.WorkspaceMap]"
    configs: tsconfig.Configs
    aliases: "dict[str, tuple[alias_mod.AliasTable, ...]]"
    budget: regex_bound.ScanBudget = field(default_factory=regex_bound.ScanBudget, compare=False)
    notes: "tuple[str, ...]" = ()
    #: engine `typescript`: the compiler's resolution (`node.engine.ResolveBackend`)
    backend: Any = field(default=None, compare=False)

    @classmethod
    def scan(cls, repo: Path, maps: "Sequence[workspace.WorkspaceMap] | None" = None,
             engine: str = "tree-sitter", use_aliases: bool = True) -> "Tree":
        """The tree at `repo` (its workspace found from the repo root when `maps` is None)."""
        repo = Path(repo)
        maps = list(maps) if maps is not None else [workspace.discover(repo, ".")]
        return cls.from_parts(repo, git_listed(repo), tsconfig.Configs(repo, maps, engine=engine), use_aliases)

    @classmethod
    def from_parts(cls, repo: Path, listed: "Sequence[str] | None", configs: tsconfig.Configs,
                   use_aliases: bool = True) -> "Tree":
        """`listed`: what git lists (None outside a checkout: every file under `repo`).
        Engine `typescript` (`configs.engine`) resolves through the compiler."""
        files = repo_files(repo, listed if listed is not None else _walk(repo))
        backend = None
        if configs.engine == "typescript":
            from .node.engine import ResolveBackend
            backend = ResolveBackend(repo, sorted(files), {p.name: p.dir for m in configs.maps
                                                           for p in reversed(m.packages) if p.name})
        notes: "list[str]" = []
        tables: "dict[str, tuple[alias_mod.AliasTable, ...]]" = {}
        if use_aliases:
            try:
                tables = alias_mod.load(repo, files, [p.dir for m in configs.maps for p in m.packages],
                                        backend.alias_root if backend is not None else None)
            except ImportError as e:
                notes.append("aliases_unread: %s" % e)
        notes += [n for ts in tables.values() for t in ts for n in t.notes]
        return cls(repo, files, configs.maps, configs, tables, notes=tuple(notes), backend=backend)

    @cached_property
    def dirs(self) -> "frozenset[str]":
        out = set()
        for f in self.files:
            d = posixpath.dirname(f)
            while d and d not in out:
                out.add(d)
                d = posixpath.dirname(d)
        return frozenset(out)

    @cached_property
    def lower(self) -> "dict[str, tuple[str, ...]]":
        out: "dict[str, list[str]]" = {}
        for f in self.files:
            out.setdefault(f.lower(), []).append(f)
        return {k: tuple(sorted(v)) for k, v in out.items()}

    @cached_property
    def _pkgs(self) -> "dict[str, Pkg | None]":
        return {}

    def pkg_at(self, d: str) -> "Pkg | None":
        if d not in self._pkgs:
            self._pkgs[d] = pkgjson.load(self.repo, d)[0] if pkgjson.manifest(d) in self.files else None
        return self._pkgs[d]

    def scope_of(self, rel_file: str) -> "Pkg | None":
        """The nearest package.json at or above the file."""
        d = posixpath.dirname(rel_file) or "."
        while True:
            p = self.pkg_at(d)
            if p is not None:
                return p
            if d == ".":
                return None
            d = posixpath.dirname(d) or "."

    @cached_property
    def by_name(self) -> "dict[str, tuple[Pkg, ...]]":
        out: "dict[str, list[Pkg]]" = {}
        for m in self.maps:
            for p in m.packages:
                if p.name and p not in out.get(p.name, []):
                    out.setdefault(p.name, []).append(p)
        return {k: tuple(v) for k, v in out.items()}

    def workspace_of(self, rel: str) -> "workspace.WorkspaceMap | None":
        hits = [m for m in self.maps if m.root == "." or rel == m.root or rel.startswith(m.root + "/")]
        return max(hits, key=lambda m: (m.root != ".", len(m.root)), default=None)

    @cached_property
    def repo_abs(self) -> str:
        return self.repo.resolve().as_posix()


class _Cx:
    """One specifier under one tsconfig and one module resolution."""

    def __init__(self, tree: Tree, importer: str, imp: TsImport, cfg: "TsConfig | None", mr: str,
                 ts_version: "str | None"):
        self.tree, self.importer, self.imp, self.cfg, self.mr = tree, importer, imp, cfg, mr
        self.opts: "dict[str, Any]" = dict(cfg.options) if cfg is not None else {}
        self.ts_major = tsconfig.ts_major(ts_version)
        self.owner = workspace.owner_of(list(tree.maps), importer)
        self.scope = tree.scope_of(importer)
        low = importer.lower()
        if low.endswith((".mts", ".mjs")):
            esm = True
        elif low.endswith((".cts", ".cjs")):
            esm = False
        else:
            esm = self.scope is not None and self.scope.type == "module"
        self.node_mode = mr in _NODE_MR
        if imp.kind in ("require", "import_equals"):
            self.fmt = "require"
        elif imp.kind == "dynamic" or not self.node_mode:
            self.fmt = "import"
        else:
            self.fmt = "import" if esm else "require"
        self.conds = frozenset({"types", self.fmt} | ({"node"} if self.node_mode else set())
                               | set(self.opts.get("customConditions", ())))
        home = self.owner or self.scope
        self.tables = tree.aliases.get(home.dir, ()) if home is not None else ()
        self.trace: "list[str]" = []
        self.tried: "list[str]" = []

    @property
    def type_only(self) -> bool:
        return self.imp.type_only or (bool(self.imp.bindings) and all(b[2] for b in self.imp.bindings))

    def chain(self) -> "list[Pkg]":
        """The importing package, then the workspace packages above it (nearest first)."""
        own = self.owner or self.scope
        if own is None:
            return []
        above = [p for m in self.tree.maps for p in m.packages
                 if p.dir != own.dir and (p.dir == "." or own.dir.startswith(p.dir + "/"))]
        return [own] + sorted(above, key=lambda p: -len(p.dir) if p.dir != "." else 1)


# ------------------------------------------------------------------ helpers

def _relative(spec: str) -> bool:
    return spec in (".", "..") or spec.startswith(("./", "../", "/"))


def split_name(spec: str) -> "tuple[str, str]":
    """(package name, subpath as `exports` spells it: "." or "./x")."""
    parts = spec.split("/")
    n = 2 if spec.startswith("@") and len(parts) > 1 else 1
    rest = parts[n:]
    return "/".join(parts[:n]), ("./" + "/".join(rest)) if rest else "."


def types_name(name: str) -> str:
    return "@types/" + (name[1:].replace("/", "__") if name.startswith("@") else name)


def _norm(*parts: str) -> str:
    return posixpath.normpath(posixpath.join(*parts))


def _escapes(path: str) -> bool:
    return path == ".." or path.startswith("../") or posixpath.isabs(path)


def _found(t: Tree, target: str, via: "tuple[str, ...]", notes: "tuple[str, ...]" = (),
           candidates: "tuple[str, ...]" = ()) -> ResolveResult:
    status = "internal" if target.lower().endswith(MODULE_EXTS) else "asset"
    pkg = workspace.owner_of(list(t.maps), target)
    return ResolveResult(status, target=target, target_pkg=pkg.name if pkg else None, via=via,
                         runtime_resolvable="yes", notes=notes, candidates=candidates)


def _miss(cx: _Cx, reason: str = "not_found", **kw: Any) -> ResolveResult:
    notes, cands = tuple(kw.pop("notes", ())), tuple(kw.pop("candidates", ()))
    if reason == "not_found":
        folded = sorted({f for c in cx.tried for f in cx.tree.lower.get(c.lower(), ()) if f != c})
        if folded:
            notes, cands = notes + ("case_mismatch",), tuple(folded)
    return ResolveResult("unresolved", reason, notes=notes, candidates=cands, **kw)


# ------------------------------------------------------------------ step 3: probing

def _first(cx: _Cx, paths: "Sequence[str]") -> "str | None":
    for p in paths:
        cx.tried.append(p)
        if p in cx.tree.files:
            return p
    return None


def _file(cx: _Cx, path: str, exts: "Sequence[str]") -> "str | None":
    """`path` as a file: substituted when it spells an emitted extension, as written, or
    with each of `exts` appended."""
    low = path.lower()
    for ext, subst in _SUBST.items():
        if low.endswith(ext):
            return _first(cx, [path[:-len(ext)] + s for s in subst])
    return _first(cx, [path] if posixpath.splitext(path)[1] else []) or _first(cx, [path + e for e in exts])


def _directory(cx: _Cx, base: str, exts: "Sequence[str]", via: "tuple[str, ...]") -> "ResolveResult | None":
    if base not in cx.tree.dirs:
        return None
    pkg = cx.tree.pkg_at(base)
    broken = False
    if pkg is not None:
        for name, value in (("types", pkg.types), ("main", pkg.main)):
            if not isinstance(value, str) or not value:
                continue
            target = _norm(base, value)
            hit = _file(cx, target, exts) or _index(cx, target, exts)
            if hit and not broken:
                return _found(cx.tree, hit, via + (name,))
            broken = broken or not hit
    index = _index(cx, base, exts)
    if broken:
        return ResolveResult("ambiguous", "dir_manifest_broken", target_pkg=pkg.name if pkg else None,
                             candidates=(index,) if index else ())
    return _found(cx.tree, index, via + ("index",)) if index else None


def _index(cx: _Cx, base: str, exts: "Sequence[str]") -> "str | None":
    return _first(cx, [posixpath.join(base, "index" + e) for e in exts])


def probe(cx: _Cx, base: str, via: "tuple[str, ...]" = (), bare_dir: bool = False) -> ResolveResult:
    """The file or directory `base` (repo-relative, no query) names. `bare_dir`: a
    package directory, read as a directory only and never held to ESM's extension rule."""
    if cx.opts.get("moduleSuffixes"):
        return ResolveResult("ambiguous", "module_suffixes")
    if _escapes(base):
        return _miss(cx)
    exts = _TS + _JS
    low = base.lower()
    if not bare_dir:
        if any(low.endswith(e) for e in _SUBST):
            hit = _file(cx, base, ())
            return _found(cx.tree, hit, via) if hit else _miss(cx)
        if posixpath.splitext(base)[1] and _first(cx, [base]):
            return _found(cx.tree, base, via)
        if cx.node_mode and cx.fmt == "import":
            return _miss(cx, "extension_required")
        hit = _first(cx, [base + e for e in exts])
        if hit:
            return _found(cx.tree, hit, via, *_shadow(cx, base, hit))
    d = _directory(cx, base, exts, via)
    return d if d is not None else _miss(cx)


def _shadow(cx: _Cx, base: str, hit: str) -> "tuple[tuple[str, ...], tuple[str, ...]]":
    """A bundler probing in its own order would pick another file: noted `shadowed`."""
    vite = next((base + e for e in _VITE_ORDER if base + e in cx.tree.files), None)
    return (("shadowed",), (vite,)) if vite is not None and vite != hit else ((), ())


def _relative_step(cx: _Cx, spec: str) -> ResolveResult:
    if spec.startswith("/"):
        cx.trace.append("rooted path: a filesystem path, never a file of the tree")
        return _miss(cx)
    base = _norm(posixpath.dirname(cx.importer) or ".", spec)
    res = probe(cx, base, ("relative",))
    if res.reason != "not_found" or not cx.opts.get("rootDirs"):
        return res
    rds = [d for d in cx.opts["rootDirs"]]
    home = max((d for d in rds if d == "." or cx.importer.startswith(d + "/")), key=len, default=None)
    if home is None:
        return res
    inner = posixpath.relpath(posixpath.dirname(cx.importer) or ".", home)
    hits: "list[ResolveResult]" = []
    for d in rds:
        if d == home:
            continue
        r = probe(cx, _norm(d, inner, spec), ("rootDirs",))
        if r.status in _HIT and r.target not in [h.target for h in hits]:
            hits.append(r)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return ResolveResult("ambiguous", "rootDirs", candidates=tuple(h.target or "" for h in hits))
    return res


# ------------------------------------------------------------------ exports / imports

def _target(t: Any, star: "str | None", conds: "frozenset[str]", imports: bool) -> "tuple[str, Any]":
    if t is None:
        return "null", None
    if isinstance(t, str):
        resolved = t.replace("*", star) if star is not None else t
        if not t.startswith("./"):
            if imports and not t.startswith(("../", "/")) and "://" not in t:
                return "bare", resolved
            return "invalid", None
        segs = resolved[2:].split("/")
        if any(s in ("", ".", "..") or s.lower() == "node_modules" for s in segs[:-1]) or \
                segs[-1] in (".", "..") or segs[-1].lower() == "node_modules":
            return "invalid", None
        return "ok", resolved
    if isinstance(t, list):
        bad = False
        for x in t:
            st, v = _target(x, star, conds, imports)
            if st == "invalid":
                bad = True
                continue
            if st == "nocond":
                continue
            return st, v
        return ("invalid" if bad else "nocond"), None
    if isinstance(t, dict):
        for k, v in t.items():
            if k == "default" or k in conds:
                st, val = _target(v, star, conds, imports)
                if st != "nocond":
                    return st, val
        return "nocond", None
    return "invalid", None


def lookup_map(m: Any, key: str, conds: "frozenset[str]", imports: bool = False) -> "tuple[str, Any]":
    """PACKAGE_IMPORTS_EXPORTS_RESOLVE: (ok | bare | null | nomatch | nocond | invalid, value)."""
    if not imports:
        if isinstance(m, dict):
            dots = [str(k).startswith(".") for k in m]
            if dots and not any(dots):
                m = {".": m}
            elif not all(dots):
                return "invalid", None
        else:
            m = {".": m}
    if not isinstance(m, dict):
        return "invalid", None
    if key in m and "*" not in key:
        return _target(m[key], None, conds, imports)
    best: "str | None" = None
    for k in m:
        if k.count("*") != 1:
            continue
        head, tail = k.split("*")
        if key.startswith(head) and key != head and (not tail or (key.endswith(tail) and len(key) >= len(k))):
            if best is None or (len(head), len(k)) > (len(best.split("*")[0]), len(best)):
                best = k
    if best is None:
        return "nomatch", None
    head, tail = best.split("*")
    return _target(m[best], key[len(head):len(key) - len(tail)], conds, imports)


_EXPORT_MISS = {"null": "not_exported", "nomatch": "not_exported", "nocond": "no_matching_condition",
                "invalid": "invalid_package_target", "bare": "invalid_package_target"}


def _pkg_target(cx: _Cx, pkg: Pkg, value: str, via: "tuple[str, ...]") -> ResolveResult:
    path = _norm(pkg.dir, value)
    hit = _file(cx, path, ())
    if hit:
        return _found(cx.tree, hit, via)
    return _out_to_src(cx, path, pkg, via)


def _out_to_src(cx: _Cx, path: str, pkg: Pkg, via: "tuple[str, ...]") -> ResolveResult:
    stems: "list[str]" = []
    under_out = False
    for cfg in cx.tree.configs.discover().values():
        for k in ("outDir", "declarationDir"):
            out = cfg.options.get(k)
            if not out or not (path == out or path.startswith(out + "/")):
                continue
            under_out = True
            rel = posixpath.relpath(path, out)
            low = rel.lower()
            stem = next((rel[:-len(e)] for e in _OUT if low.endswith(e)), rel)
            for base in _source_bases(cfg):
                stems.append(_norm(base, stem))
    found = sorted({s + e for s in stems for e in _SRC if s + e in cx.tree.files})
    if len(found) == 1:
        return replace(_found(cx.tree, found[0], via + ("out_to_src",)), notes=("inferred",))
    if found or under_out:
        return _miss(cx, "unbuilt_output", target_pkg=pkg.name, candidates=tuple(found))
    return _miss(cx, target_pkg=pkg.name)


def _source_bases(cfg: TsConfig) -> "list[str]":
    if cfg.options.get("rootDir"):
        return [cfg.options["rootDir"]]
    bases = [s.split("*", 1)[0].rstrip("/") or cfg.dir for s in cfg.include or ()]
    return list(dict.fromkeys(bases or [cfg.dir]))


def _types_versions(pkg: Pkg) -> "dict[str, Any] | None":
    tv = pkg.typesVersions
    if not isinstance(tv, dict) or not tv:
        return None
    first = next(iter(tv.values()))
    return first if isinstance(first, dict) else None


def _pattern_targets(table: "dict[str, Any]", key: str) -> "list[str] | None":
    """`paths`/`typesVersions` matching: an exact key, else the longest prefix before `*`."""
    if key in table and "*" not in key:
        vals, star = table[key], None
    else:
        best = None
        for k in table:
            if k.count("*") == 1:
                head, tail = k.split("*")
                if key.startswith(head) and key.endswith(tail) and len(key) >= len(head) + len(tail):
                    if best is None or len(head) > len(best.split("*")[0]):
                        best = k
        if best is None:
            return None
        head, tail = best.split("*")
        vals, star = table[best], key[len(head):len(key) - len(tail)]
    vals = vals if isinstance(vals, (list, tuple)) else [vals]
    return [str(v).replace("*", star) if star is not None else str(v) for v in vals]


def _package(cx: _Cx, pkg: Pkg, sub: str, via: "tuple[str, ...]") -> ResolveResult:
    if pkg.exports is not None and cx.mr not in _LEGACY_MR:
        st, val = lookup_map(pkg.exports, sub, cx.conds)
        cx.trace.append("exports %s of %s: %s" % (sub, pkg.name, st))
        if st == "ok":
            return _pkg_target(cx, pkg, val, via + ("exports",))
        return _miss(cx, _EXPORT_MISS[st], target_pkg=pkg.name)
    res = _legacy(cx, pkg, sub, via)
    if res.status == "unresolved" and pkg.exports is not None:
        return replace(res, reason="mode_unsupported", notes=())
    return res


def _legacy(cx: _Cx, pkg: Pkg, sub: str, via: "tuple[str, ...]") -> ResolveResult:
    tv = _types_versions(pkg)
    if sub == ".":
        res = probe(cx, pkg.dir, via, bare_dir=True)
    else:
        rel = sub[2:]
        for cand in _pattern_targets(tv, rel) or [] if tv else []:
            r = probe(cx, _norm(pkg.dir, cand), via + ("typesVersions",))
            if r.status in _HIT:
                return replace(r, target_pkg=pkg.name)
        res = probe(cx, _norm(pkg.dir, rel), via)
    return replace(res, target_pkg=res.target_pkg or pkg.name)


# ------------------------------------------------------------------ step 5: overrides

def _paths(cx: _Cx, spec: str) -> "ResolveResult | None":
    paths = cx.opts.get("paths")
    if not paths:
        return None
    subs = _pattern_targets(paths, spec)
    if subs is None:
        return None
    base = cx.opts["baseUrl"] if "baseUrl" in cx.opts and _legacy_base(cx) else \
        cx.opts.get("paths_base_dir", cx.cfg.dir if cx.cfg is not None else ".")
    for s in subs:
        cx.trace.append("paths substitution %s" % s)
        r = probe(cx, _norm(base, s), ("paths",))
        if r.status in _HIT or r.status == "ambiguous":
            return r
    cx.trace.append("paths: every substitution missed, falling through")
    return None


def _legacy_base(cx: _Cx) -> bool:
    return cx.ts_major is None or cx.ts_major < 7


def _base_url(cx: _Cx, spec: str) -> "ResolveResult | None":
    if "baseUrl" not in cx.opts or not _legacy_base(cx):
        return None
    r = probe(cx, _norm(cx.opts["baseUrl"], spec), ("baseUrl_legacy",))
    return r if r.status in _HIT else None


def _alias(cx: _Cx, spec: str) -> "ResolveResult | None":
    for table in cx.tables:
        m = alias_mod.lookup(table, spec, cx.tree.budget)
        if m is None:
            continue
        cx.trace.append("alias %s of %s: %s" % (m.entry.find, table.path, m.state))
        if m.state == "ignored":
            return _miss(cx, "alias_ignored")
        if m.state != "ok":
            return _miss(cx, "opaque_alias_target")
        last = _miss(cx)
        for rw in m.rewrites:
            kind, val = alias_mod.classify(table, m.entry, rw, cx.tree.repo_abs)
            if kind == "path":
                r = probe(cx, val, ("alias",))
            elif kind == "relative":
                r = probe(cx, _norm(posixpath.dirname(cx.importer) or ".", val), ("alias",))
            elif kind == "bare":
                r = replace(_bare(cx, val), via=("alias",))
            else:
                r = _miss(cx)
            if r.status != "unresolved":
                return r
            last = r
        return last
    return None


def _alias_claims(cx: _Cx, spec: str) -> bool:
    return any(alias_mod.lookup(t, spec, cx.tree.budget) is not None for t in cx.tables)


def _overrides(cx: _Cx, spec: str) -> "ResolveResult | None":
    ts_side = _paths(cx, spec) or _base_url(cx, spec)
    bundler = _alias(cx, spec)
    if bundler is None:
        return ts_side
    if ts_side is None or ts_side.key() == bundler.key():
        return bundler if ts_side is None else replace(ts_side, via=ts_side.via + ("alias",))
    return ResolveResult("ambiguous", "alias_vs_paths",
                         candidates=tuple(x for x in (ts_side.target, bundler.target) if x))


# ------------------------------------------------------------------ step 6: packages

def _dep(cx: _Cx, name: str) -> "tuple[int, Pkg, str, str] | None":
    for i, p in enumerate(cx.chain()):
        if name in p.deps:
            f, spec = p.deps[name]
            return i, p, f, spec
    return None


def declared(cx: _Cx, name: str) -> str:
    d = _dep(cx, name)
    if d is not None:
        i, p, f, _spec = d
        return "own:%s" % f if i == 0 else "ancestor:%s:%s" % (p.name or p.dir, f)
    if cx.type_only and _dep(cx, types_name(name)) is not None:
        return "types_only"
    return "undeclared"


def _semver_only(spec: str) -> bool:
    return bool(spec) and spec != "*" and not spec.startswith(_LINKED + ("npm:", "http", "git"))


def _bare(cx: _Cx, spec: str) -> ResolveResult:
    name, sub = split_name(spec)
    if cx.scope is not None and cx.scope.name == name and cx.scope.exports is not None:
        return _package(cx, cx.scope, sub, ("self_name",))
    gate = _workspace_gate(cx, name)
    if gate is not None:
        return gate
    pkgs = cx.tree.by_name.get(name, ())
    if pkgs:
        return _package(cx, pkgs[0], sub, ("workspace",))
    return ResolveResult("external", target=name, target_pkg=name)


def _workspace_gate(cx: _Cx, name: str) -> "ResolveResult | None":
    """A workspace package name no resolver can place: two packages hold it, or pnpm
    may link the registry's copy instead."""
    pkgs = cx.tree.by_name.get(name, ())
    if len(pkgs) > 1:
        return ResolveResult("ambiguous", "duplicate_workspace_name", target_pkg=name,
                             candidates=tuple(p.dir for p in pkgs))
    d = _dep(cx, name) if pkgs else None
    ws = cx.tree.workspace_of(cx.importer)
    if d is not None and ws is not None and ws.manager == "pnpm" and _semver_only(d[3]):
        return ResolveResult("ambiguous", "workspace_or_registry", target_pkg=name, candidates=(pkgs[0].dir,))
    return None


def _attach_declared(cx: _Cx, spec: str, res: ResolveResult) -> ResolveResult:
    name = split_name(spec)[0]
    if res.status != "external" and name not in cx.tree.by_name:
        return res
    how = declared(cx, name)
    if res.status == "external" and how == "undeclared":
        if cx.cfg is not None and "paths" in cx.cfg.unknown_inherited:
            return _miss(cx, "tsconfig_extends_external", target_pkg=name)
        if any(t.opaque_scope for t in cx.tables):
            return _miss(cx, "opaque_alias_scope", target_pkg=name)
    if res.status == "external":
        rr = "unknown" if how.startswith(("own:", "ancestor:")) else "no"
    elif res.status in _HIT:
        d = _dep(cx, name)
        rr = "no" if d is None else ("yes" if d[3].startswith(_LINKED) or d[3] in ("*", "") else "unknown")
        if cx.scope is not None and cx.scope.name == name:
            how, rr = "self", "yes"
    else:
        rr = res.runtime_resolvable
    return replace(res, declared=how, runtime_resolvable=rr, target_pkg=res.target_pkg or name)


def _with_bypass(cx: _Cx, spec: str, res: ResolveResult) -> ResolveResult:
    """A `paths` hit into a workspace package whose `exports` would refuse the subpath."""
    if res.status not in _HIT or "paths" not in res.via:
        return res
    name, sub = split_name(spec)
    pkgs = cx.tree.by_name.get(name, ())
    if len(pkgs) != 1 or pkgs[0].exports is None or (cx.owner is not None and cx.owner.dir == pkgs[0].dir):
        return res
    st, _v = lookup_map(pkgs[0].exports, sub, cx.conds)
    return replace(res, notes=res.notes + ("bypasses_exports",)) if st in ("null", "nomatch") else res


# ------------------------------------------------------------------ the steps

def _one(cx: _Cx, spec: str) -> ResolveResult:
    if spec.startswith("node:"):
        return ResolveResult("builtin", target=spec, via=("builtin",), runtime_resolvable="yes")
    if _relative(spec):
        return _relative_step(cx, spec)
    if spec.startswith("#"):
        if cx.mr in _LEGACY_MR:
            return _miss(cx, "mode_unsupported")
        scope = cx.scope
        if scope is None or not isinstance(scope.imports, dict):
            return _miss(cx)
        st, val = lookup_map(scope.imports, spec, cx.conds, imports=True)
        if st == "ok":
            return _pkg_target(cx, scope, val, ("imports",))
        if st == "bare":
            return _attach_declared(cx, val, replace(_bare(cx, val), via=("imports",)))
        return _miss(cx, "not_found" if st in ("null", "nomatch") else _EXPORT_MISS[st])
    hit = _builtin(cx, spec)
    if hit is not None:
        return hit
    over = _overrides(cx, spec)
    if over is not None:
        return _attach_declared(cx, spec, _with_bypass(cx, spec, over))
    return _attach_declared(cx, spec, _bare(cx, spec))


def _builtin(cx: _Cx, spec: str) -> "ResolveResult | None":
    names, prefix_only = builtins()
    if spec.startswith("node:"):
        return ResolveResult("builtin", target=spec, via=("builtin",), runtime_resolvable="yes")
    if spec in names and spec not in prefix_only and not (cx.opts.get("paths") and spec in cx.opts["paths"]) \
            and not _alias_claims(cx, spec):
        return ResolveResult("builtin", target="node:" + spec, via=("builtin",), runtime_resolvable="yes")
    return None


def _one_engine(cx: _Cx, spec: str) -> ResolveResult:
    """Engine `typescript`: the compiler resolves paths, `#imports`, `paths`/`baseUrl`,
    self-name and workspace packages; the syntax filter, builtins, bundler aliases,
    external packages, `declared` and outDir->rootDir stay the static steps'. A target
    under a workspace package is internal however the compiler flags it (every path
    through the virtual `node_modules` is an "external library import" to it)."""
    bare = not _relative(spec) and not spec.startswith("#")
    if bare:
        hit = _builtin(cx, spec)
        if hit is not None:
            return hit
        gate = _workspace_gate(cx, split_name(spec)[0])
        if gate is not None:
            return gate
    bundler = _alias(cx, spec) if bare else None
    got = cx.tree.backend.resolve(cx.importer, spec, cx.fmt, cx.cfg.path if cx.cfg is not None else None, cx.mr)
    if got is None:
        if bundler is not None:
            return _attach_declared(cx, spec, bundler)
        static = _one(cx, spec)
        # an asset and an outDir mapping are the static steps' own: the compiler resolves neither
        if static.status == "internal" and "out_to_src" not in static.via:
            return _miss(cx, "engine_disagrees", candidates=(static.target or "",), target_pkg=static.target_pkg)
        return static
    target = got["fileName"]
    if got.get("external") and workspace.owner_of(list(cx.tree.maps), target) is None:
        name = got.get("packageId") or split_name(spec)[0]
        return _attach_declared(cx, spec, ResolveResult("external", target=name, target_pkg=name))
    res = _found(cx.tree, target, ("typescript",))
    if bundler is not None and (_paths(cx, spec) or _base_url(cx, spec)) is not None and bundler.key() != res.key():
        return ResolveResult("ambiguous", "alias_vs_paths", candidates=tuple(x for x in (res.target, bundler.target) if x))
    if bundler is not None and (_paths(cx, spec) or _base_url(cx, spec)) is None:
        res = bundler
    return _attach_declared(cx, spec, _with_bypass(cx, spec, res)) if bare else res


def _merge(results: "list[ResolveResult]", reason: str) -> ResolveResult:
    first = results[0]
    if all(r.key() == first.key() for r in results[1:]):
        return first
    cands = tuple(dict.fromkeys(r.target or "%s:%s" % (r.status, r.reason) for r in results))
    return ResolveResult("ambiguous", reason, candidates=cands,
                         trace=tuple(x for r in results for x in r.trace))


def _cut_query(spec: str) -> "tuple[str, bool]":
    cut = [i for i in (spec.find("?"), spec.find("#", 1)) if i > 0]
    return (spec[:min(cut)], True) if cut else (spec, False)


def resolve(tree: Tree, importer: str, imp: TsImport) -> ResolveResult:
    """What the specifier of `imp`, written in the file `importer`, names."""
    spec = imp.spec
    if spec is None:
        return ResolveResult("unresolved", "dynamic", trace=("not a string literal",))
    if spec.startswith(("http://", "https://", "data:")):
        return ResolveResult("external", "url", target=spec)
    if spec.startswith("virtual:") or "\0" in spec:
        return ResolveResult("unresolved", "bundler_virtual")
    if "!" in spec:
        return ResolveResult("unresolved", "loader_syntax")
    path, cut = _cut_query(spec)
    ws = tree.workspace_of(importer)
    ts_version = ws.ts_version if ws is not None else None
    per_config = []
    governing: "list[TsConfig | None]" = list(tree.configs.governing(importer)) or [None]
    for cfg in governing:
        mrs = cfg.effective_mr if cfg is not None else tsconfig.effective_mr({}, ts_version)[0]
        runs = []
        for mr in mrs:
            cx = _Cx(tree, importer, imp, cfg, mr, ts_version)
            r = _one_engine(cx, path) if tree.backend is not None else _one(cx, path)
            runs.append(replace(r, trace=tuple(cx.trace) + r.trace))
        per_config.append(_merge(runs, "mr_default"))
    res = _merge(per_config, "multi_tsconfig")
    return replace(res, via=("query_stripped",) + res.via) if cut else res
