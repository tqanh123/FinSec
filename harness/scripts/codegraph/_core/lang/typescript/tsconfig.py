"""tsconfig as the TypeScript/JavaScript pack models it -- one model, two readers.

A `TsConfig` is one config with its `extends` chain applied the compiler's way: an
option of a later base wins over an earlier one and the config's own over every base
(`paths` is one option, so it is replaced whole, never merged by key); `files`,
`include` and `exclude` are the nearest declaration's; `references` are the config's
own, never inherited. Every path is repo-relative and resolved against the config
that declared it (`${configDir}`: the leaf), so two readers agree on a value however
they spelled it. An undeclared `exclude` is the compiler's default, `outDir` and
`declarationDir`; `node_modules`, `bower_components` and `jspm_packages` are always out.

Engine `tree-sitter` reads the JSON-with-comments here; engine `typescript` asks the
pinned compiler (`readJsonConfigFile` + `parseJsonSourceFileConfigFileContent`, the
bridge's `configs` op) and converts its answer into the same model. The module
resolution in effect is computed once, here, for both (`effective_mr`): the declared
value, else the default of the TypeScript version the workspace declares (6.x:
`bundler`, `nodenext`/`node16` for those `module`s; older: from `module`, itself from
`target`); with no version declared, both defaults (`mr_source: ambiguous` when they
differ).

A config is governed-by for a file the way an editor finds it: the nearest
`tsconfig.json`/`jsconfig.json` above the file if it includes the file, else every
config its `references` reach (breadth first) that does; none is `[]`.

WHAT IT CANNOT SEE
    * `extends` into an npm package that is not a workspace package: never read from
      node_modules; the options it could set are named in `unknown_inherited`.
    * Options outside the model (`jsx`, `strict`, ...): not carried.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import globs
from .workspace import WorkspaceMap

DECISIVE = ("moduleResolution", "module", "paths", "baseUrl", "customConditions", "allowJs",
            "resolveJsonModule", "rootDirs", "moduleSuffixes", "allowImportingTsExtensions")
_ENUMS = ("moduleResolution", "module", "target")
_BOOLS = ("allowJs", "resolveJsonModule", "allowImportingTsExtensions", "composite")
_PATHS = ("baseUrl", "rootDir", "outDir", "declarationDir")
_ALIASES = {"node": "node10", "nodejs": "node10", "es6": "es2015"}
_ALWAYS_OUT = ("node_modules", "bower_components", "jspm_packages")
_CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
_EXTENDS_NOT_FOUND = 6053
_CONFIG_DIR = "${configDir}"


@dataclass(frozen=True)
class TsConfig:
    path: str
    extends_chain: "tuple[str, ...]" = ()
    references: "tuple[str, ...]" = ()
    files: "tuple[str, ...] | None" = None
    include: "tuple[str, ...] | None" = None
    exclude: "tuple[str, ...]" = ()
    options: "dict[str, Any]" = field(default_factory=dict)
    unknown_inherited: "tuple[str, ...]" = ()
    effective_mr: "tuple[str, ...]" = ()
    mr_source: str = ""

    @property
    def dir(self) -> str:
        return posixpath.dirname(self.path) or "."


# ------------------------------------------------------------------ JSONC

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|,(?=\s*[}\]])', re.S)


def read_jsonc(text: str) -> Any:
    """JSON with `//` and `/* */` comments and trailing commas (ValueError if broken)."""
    return json.loads(_TOKEN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text))


# ------------------------------------------------------------------ paths

def _canon(value: Any) -> str:
    v = str(value).lower()
    return _ALIASES.get(v, v)


def _rel(repo: Path, base: str, p: str) -> str:
    if os.path.isabs(p):
        try:
            return Path(os.path.relpath(p, repo)).as_posix()
        except ValueError:
            return Path(p).as_posix()
    return posixpath.normpath(posixpath.join(base, p))


def _subst(repo: Path, origin: str, leaf: str, p: str) -> str:
    if p.startswith(_CONFIG_DIR):
        return _rel(repo, leaf, "." + p[len(_CONFIG_DIR):])
    return _rel(repo, origin, p)


def _reference_config(repo: Path, rel: str) -> str:
    return rel if rel.endswith(".json") and not (repo / rel).is_dir() else posixpath.join(rel, "tsconfig.json")


def _extends_list(ext: Any) -> "list[str]":
    if isinstance(ext, str):
        return [ext]
    return [x for x in ext if isinstance(x, str)] if isinstance(ext, list) else []


def ts_major(spec: "str | None") -> "int | None":
    m = re.search(r"(\d+)", spec or "")
    return int(m.group(1)) if m and not (spec or "").startswith(("workspace:", "link:", "file:")) else None


def _mr_ts6(module: "str | None") -> str:
    if module == "nodenext":
        return "nodenext"
    return "node16" if module in ("node16", "node18", "node20") else "bundler"


def _mr_ts5(module: "str | None", target: "str | None") -> str:
    if module is None:
        module = "commonjs" if target in (None, "es3", "es5") else "es2015"
    if module == "commonjs":
        return "node10"
    if module in ("node16", "node18", "node20"):
        return "node16"
    return {"nodenext": "nodenext", "preserve": "bundler"}.get(module, "classic")


def effective_mr(options: "dict[str, Any]", ts_version: "str | None") -> "tuple[tuple[str, ...], str]":
    if options.get("moduleResolution"):
        return (options["moduleResolution"],), "explicit"
    v6, v5 = _mr_ts6(options.get("module")), _mr_ts5(options.get("module"), options.get("target"))
    major = ts_major(ts_version)
    if major is None:
        both = tuple(dict.fromkeys((v6, v5)))
        return both, "ambiguous" if len(both) > 1 else "default_unversioned"
    return ((v6,), "default_ts6") if major >= 6 else ((v5,), "default_ts_lt6")


# ------------------------------------------------------------------ the JSONC reader

def _options(repo: Path, leaf: str, merged: "dict[str, tuple[Any, str]]") -> "dict[str, Any]":
    out: "dict[str, Any]" = {}
    for k, (v, origin) in merged.items():
        if k in _ENUMS and isinstance(v, str):
            out[k] = _canon(v)
        elif k in _BOOLS:
            out[k] = bool(v)
        elif k in _PATHS and isinstance(v, str):
            out[k] = _subst(repo, origin, leaf, v)
        elif k == "rootDirs" and isinstance(v, list):
            out[k] = tuple(_subst(repo, origin, leaf, str(x)) for x in v)
        elif k == "customConditions" and isinstance(v, list):
            out[k] = tuple(str(x) for x in v)
        elif k == "paths" and isinstance(v, dict):
            out[k] = {str(key): tuple(str(t) for t in (ts if isinstance(ts, list) else [])) for key, ts in v.items()}
            out["paths_base_dir"] = origin
        elif k == "moduleSuffixes":
            out[k] = True
    return out


# ------------------------------------------------------------------ the model of a tree

class Configs:
    """The tsconfigs of a repo, read once each by the engine's reader."""

    def __init__(self, repo: Path, maps: "Sequence[WorkspaceMap]", engine: str = "tree-sitter"):
        self.repo, self.maps, self.engine = repo, list(maps), engine
        self._cache: "dict[str, TsConfig | None]" = {}
        self._raw: "dict[str, Any]" = {}
        self.notes: "list[str]" = []

    # ---- reading
    def get(self, rel: str) -> "TsConfig | None":
        rel = posixpath.normpath(rel)
        if rel not in self._cache:
            self._load([rel])
        return self._cache[rel]

    def _load(self, rels: "Iterable[str]") -> None:
        todo = [r for r in dict.fromkeys(rels) if r not in self._cache]
        if not todo:
            return
        present = [r for r in todo if (self.repo / r).is_file()]
        self._cache.update({r: None for r in todo if r not in present})
        if self.engine == "typescript":
            from .node.engine import read_configs
            for rel, payload in zip(present, read_configs(self.repo, [self.repo / r for r in present])):
                self._cache[rel] = self._finish(self._from_compiler(rel, payload))
        else:
            for rel in present:
                self._cache[rel] = self._finish(self._from_jsonc(rel))

    def _jsonc(self, rel: str) -> "dict[str, Any]":
        if rel not in self._raw:
            try:
                raw = read_jsonc((self.repo / rel).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.notes.append("tsconfig_unreadable: %s (%s)" % (rel, e))
                raw = {}
            self._raw[rel] = raw if isinstance(raw, dict) else {}
        return self._raw[rel]

    def _extends_target(self, spec: str, from_dir: str) -> "str | None":
        if spec.startswith((".", "/")) or os.path.isabs(spec):
            base = _rel(self.repo, from_dir, spec)
            return next((c for c in (base, base + ".json") if (self.repo / c).is_file()), None)
        parts = spec.split("/")
        n = 2 if spec.startswith("@") else 1
        name, sub = "/".join(parts[:n]), "/".join(parts[n:])
        pkg = next((m.by_name()[name] for m in self.maps if name in m.by_name()), None)
        if pkg is None:
            return None
        base = posixpath.normpath(posixpath.join(pkg.dir, sub or "tsconfig.json"))
        return next((c for c in (base, base + ".json", posixpath.join(base, "tsconfig.json"))
                     if (self.repo / c).is_file()), None)

    def _chain(self, rel: str, stack: "tuple[str, ...]"):
        raw = self._jsonc(rel)
        d = posixpath.dirname(rel) or "."
        opts: "dict[str, tuple[Any, str]]" = {}
        specs: "dict[str, tuple[list, str]]" = {}
        chain: "list[str]" = []
        unresolved = False
        for e in _extends_list(raw.get("extends")):
            target = self._extends_target(e, d)
            if target is None:
                unresolved = True
                continue
            if target in stack:
                self.notes.append("tsconfig_extends_cycle: %s -> %s" % (rel, target))
                continue
            o, s, c, u = self._chain(target, stack + (target,))
            opts.update(o)
            specs.update(s)
            chain += [target] + c
            unresolved |= u
        co = raw.get("compilerOptions")
        for k, v in (co.items() if isinstance(co, dict) else ()):
            opts[k] = (v, d)
        for k in ("files", "include", "exclude"):
            if isinstance(raw.get(k), list):
                specs[k] = ([str(x) for x in raw[k]], d)
        return opts, specs, chain, unresolved

    def _from_jsonc(self, rel: str) -> TsConfig:
        opts, specs, chain, unresolved = self._chain(rel, (rel,))
        leaf = posixpath.dirname(rel) or "."
        options = _options(self.repo, leaf, opts)

        def spec(key: str) -> "tuple[str, ...] | None":
            if key not in specs:
                return None
            vals, origin = specs[key]
            return tuple(_subst(self.repo, origin, leaf, v) for v in vals)

        exclude = spec("exclude")
        if exclude is None:
            exclude = tuple(options[k] for k in ("outDir", "declarationDir") if k in options)
        refs = self._jsonc(rel).get("references")
        references = tuple(_reference_config(self.repo, _rel(self.repo, leaf, str(r["path"])))
                           for r in refs or [] if isinstance(r, dict) and isinstance(r.get("path"), str)) \
            if isinstance(refs, list) else ()
        return TsConfig(path=rel, extends_chain=tuple(dict.fromkeys(chain)), references=references,
                        files=spec("files"), include=spec("include"), exclude=exclude, options=options,
                        unknown_inherited=tuple(k for k in DECISIVE if k not in opts) if unresolved else ())

    def _from_compiler(self, rel: str, p: "dict[str, Any]") -> TsConfig:
        """The bridge's `configs` answer for `rel`, in the model's terms."""
        if "error" in p:
            self.notes.append("tsconfig_unreadable: %s (%s)" % (rel, p["error"]))
            return TsConfig(path=rel)
        leaf = posixpath.dirname(rel) or "."
        raw = dict(p.get("options") or {})
        options: "dict[str, Any]" = {}
        for k, v in raw.items():
            if k in _ENUMS:
                options[k] = _canon(v)
            elif k in _PATHS:
                options[k] = _rel(self.repo, leaf, v)
            elif k == "rootDirs":
                options[k] = tuple(_rel(self.repo, leaf, x) for x in v)
            elif k == "customConditions":
                options[k] = tuple(v)
            elif k == "paths":
                options[k] = {key: tuple(ts) for key, ts in v.items()}
            elif k == "paths_base_dir":
                options[k] = _rel(self.repo, leaf, v)
            else:
                options[k] = v

        def spec(key: str) -> "tuple[str, ...] | None":
            vals = p.get(key)
            return None if vals is None else tuple(_rel(self.repo, leaf, v) for v in vals)

        unresolved = any(e.get("code") == _EXTENDS_NOT_FOUND for e in p.get("errors") or ())
        return TsConfig(path=rel, extends_chain=tuple(dict.fromkeys(_rel(self.repo, leaf, x) for x in p.get("extends") or ())),
                        references=tuple(_reference_config(self.repo, _rel(self.repo, leaf, r))
                                         for r in p.get("references") or ()),
                        files=spec("files"), include=spec("include"), exclude=spec("exclude") or (),
                        options=options,
                        unknown_inherited=tuple(k for k in DECISIVE if k not in raw) if unresolved else ())

    def _workspace_of(self, rel: str) -> "WorkspaceMap | None":
        hits = [m for m in self.maps if m.root == "." or rel == m.root or rel.startswith(m.root + "/")]
        return max(hits, key=lambda m: (m.root != ".", len(m.root)), default=None)

    def _finish(self, cfg: TsConfig) -> TsConfig:
        ws = self._workspace_of(cfg.path)
        mr, source = effective_mr(cfg.options, ws.ts_version if ws else None)
        return replace(cfg, effective_mr=mr, mr_source=source)

    # ---- the tree
    def discover(self) -> "dict[str, TsConfig]":
        """Every config a workspace package directory holds or finds above it on the way
        to the repo root (the walk `governing` makes), plus all they reference."""
        start: "list[str]" = []
        for m in self.maps:
            for d in [m.root] + [p.dir for p in m.packages]:
                d = posixpath.normpath(d)
                while True:
                    start += [posixpath.normpath(posixpath.join(d, n)) for n in _CONFIG_NAMES]
                    if d in (".", "") or d.startswith(".."):
                        break
                    d = posixpath.dirname(d) or "."
        seen: "dict[str, TsConfig]" = {}
        frontier = list(dict.fromkeys(start))
        while frontier:
            self._load(frontier)
            nxt = []
            for rel in frontier:
                cfg = self._cache.get(rel)
                if cfg is not None and rel not in seen:
                    seen[rel] = cfg
                    nxt += [r for r in cfg.references if r not in seen and r not in self._cache]
            frontier = list(dict.fromkeys(nxt))
        return dict(sorted(seen.items()))

    def out_dirs(self) -> "tuple[str, ...]":
        return tuple(sorted({c.options[k] for c in self.discover().values()
                             for k in ("outDir", "declarationDir") if k in c.options}))

    def inputs(self) -> "tuple[str, ...]":
        """Every config file the discovered configs were read from."""
        return tuple(sorted({x for c in self.discover().values() for x in (c.path, *c.extends_chain)}))

    def governing(self, rel_file: str) -> "list[TsConfig]":
        d = posixpath.dirname(rel_file) or "."
        while True:
            for name in _CONFIG_NAMES:
                rel = posixpath.normpath(posixpath.join(d, name))
                cfg = self.get(rel) if (self.repo / rel).is_file() else None
                if cfg is not None:
                    return [cfg] if matches(cfg, rel_file) else self._by_reference(cfg, rel_file)
            if d == ".":
                return []
            d = posixpath.dirname(d) or "."

    def _by_reference(self, cfg: TsConfig, rel_file: str) -> "list[TsConfig]":
        out: "list[TsConfig]" = []
        seen = {cfg.path}
        queue = deque(cfg.references)
        while queue:
            rel = queue.popleft()
            if rel in seen:
                continue
            seen.add(rel)
            ref = self.get(rel)
            if ref is None:
                continue
            if matches(ref, rel_file):
                out.append(ref)
            queue.extend(ref.references)
        return out


def _as_pattern(spec: str) -> str:
    last = spec.rsplit("/", 1)[-1]
    return spec if any(c in last for c in "*?") or "." in last else spec.rstrip("/") + "/**/*"


def matches(cfg: TsConfig, rel_file: str) -> bool:
    """`cfg` includes `rel_file` (its files, include and exclude)."""
    if cfg.files is not None and rel_file in cfg.files:
        return True
    if any(seg in _ALWAYS_OUT for seg in rel_file.split("/")):
        return False
    include = cfg.include if cfg.include is not None else (() if cfg.files is not None else
                                                          (posixpath.join(cfg.dir, "**/*"),))
    if not any(globs.match(_as_pattern(s), rel_file) or s == rel_file for s in include):
        return False
    return not any(globs.match(_as_pattern(s), rel_file) or s == rel_file for s in cfg.exclude)
