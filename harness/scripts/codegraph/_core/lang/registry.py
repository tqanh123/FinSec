"""The language book as packs — one reader, every key checked.

`harness/data/hs-code-languages.yaml` rows become `LanguagePack`s. An unknown key
is an error, not freedom (a mistyped `extentions` would silently read nothing); a
book with no row, two rows owning one suffix, or a row missing its reader or
resolver is broken. The reader and resolver a row names import when first asked
for, so a broken class surfaces where a file of that language is measured.

`enabled(descriptor)` is the set of packs a tree is measured with: the bin packs the
descriptor's `languages:` block names (absent: Python alone), then a `ProjectPack` for
each `adapters:` row naming a `resolver:` (resolved over `lang.resolver_command`).
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import re

from .base import Engine, LanguageError, LanguagePack, NpmPackage, _import
from .errors import AdapterRefused

#: harness/scripts/codegraph/_core/lang/registry.py -> harness/data/
BOOK = Path(__file__).resolve().parents[4] / "data" / "hs-code-languages.yaml"

_ROW_KEYS = frozenset({"id", "files", "reader", "resolver", "capabilities", "offer", "stamp",
                       "options", "engines", "runners", "checks"})
_ENGINE_KEYS = frozenset({"reader", "requires", "install"})
_REQUIRES_KEYS = frozenset({"python", "node", "npm"})
_NPM_KEYS = frozenset({"name", "version", "integrity", "pin"})
_NODE_FLOOR = re.compile(r">=\d+(\.\d+){0,2}")
#: the seam a test points at a missing wheel; `importlib.util.find_spec` otherwise
find_module = importlib.util.find_spec
_FILES_KEYS = frozenset({"include", "exclude"})
_REF_KEYS = frozenset({"module", "class"})


def _strings(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x for x in v)


def _ref(row: dict, key: str, at: str) -> "tuple[str, str]":
    v = row.get(key)
    if not isinstance(v, dict):
        raise LanguageError("%s: `%s:` must be a mapping {module, class}" % (at, key))
    unknown = sorted(set(v) - _REF_KEYS)
    if unknown:
        raise LanguageError("%s: `%s:` unknown key(s) %s; valid: module, class"
                            % (at, key, ", ".join(unknown)))
    mod, cls = v.get("module"), v.get("class")
    if not (isinstance(mod, str) and mod and isinstance(cls, str) and cls):
        raise LanguageError("%s: `%s:` needs `module:` and `class:`" % (at, key))
    return mod, cls


def _engines(raw: Any, at: str) -> "tuple[Engine, ...]":
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not raw:
        raise LanguageError("%s: `engines:` must be a non-empty mapping of engine name -> "
                            "{reader, requires, install}" % at)
    out = []
    for name, e in raw.items():
        eat = "%s engines.%s" % (at, name)
        if not isinstance(e, dict):
            raise LanguageError("%s must be a mapping {reader, requires, install}" % eat)
        unknown = sorted(set(e) - _ENGINE_KEYS)
        if unknown:
            raise LanguageError("%s: unknown key(s) %s; valid: install, reader, requires"
                                % (eat, ", ".join(unknown)))
        req = e.get("requires") or {}
        if not isinstance(req, dict) or set(req) - _REQUIRES_KEYS or not _strings(req.get("python", [])):
            raise LanguageError("%s: `requires:` must be {python: [module, ...], node: '>=X.Y', "
                                "npm: [{name, version, integrity, pin}]}" % eat)
        node = req.get("node", "")
        if not isinstance(node, str) or (node and not _NODE_FLOOR.fullmatch(node)):
            raise LanguageError("%s: `requires.node:` must be a floor like '>=14.17'" % eat)
        npm = _npm(req.get("npm", []), eat)
        install = e.get("install", "")
        if not isinstance(install, str) or (req and not install):
            raise LanguageError("%s: `install:` must be the command that installs what "
                                "`requires:` names" % eat)
        out.append(Engine(str(name), _ref(e, "reader", eat), tuple(req.get("python", [])), install,
                          requires_node=node, requires_npm=npm))
    return tuple(out)


def _npm(raw: Any, at: str) -> "tuple[NpmPackage, ...]":
    if not isinstance(raw, list):
        raise LanguageError("%s: `requires.npm:` must be a list of {name, version, integrity, pin}" % at)
    out = []
    for i, p in enumerate(raw, 1):
        if not isinstance(p, dict) or set(p) != _NPM_KEYS or not all(isinstance(v, str) and v
                                                                    for v in p.values()):
            raise LanguageError("%s: `requires.npm[%d]` must give exactly name, version, integrity "
                                "and pin, each a non-empty string" % (at, i))
        if not p["integrity"].startswith("sha512-"):
            raise LanguageError("%s: `requires.npm[%d].integrity` must be the lockfile's "
                                "`sha512-...` value" % (at, i))
        out.append(NpmPackage(p["name"], p["version"], p["integrity"], p["pin"]))
    return tuple(out)


def _runners_and_checks(row: dict, at: str) -> None:
    """`runners:` and `checks:` are owned by `test_runners` and `static_checks` (stdlib,
    read by callers without this engine); each checks every key."""
    import static_checks
    import test_runners
    try:
        test_runners.parse_rows(row.get("runners"), at)
    except test_runners.RunnerError as e:
        raise LanguageError(str(e)) from None
    checks = row.get("checks") or []
    if not isinstance(checks, list):
        raise LanguageError("%s: `checks:` must be a list of check rows" % at)
    try:
        ids = [static_checks.parse_row(c, "%s checks[%d]" % (at, i)).id for i, c in enumerate(checks, 1)]
    except static_checks.CheckSpecError as e:
        raise LanguageError(str(e)) from None
    if len(set(ids)) != len(ids):
        raise LanguageError("%s: `checks:` declares a check id twice" % at)


def parse_rows(raw: Any, where: str) -> "tuple[LanguagePack, ...]":
    if not isinstance(raw, list):
        raise LanguageError("%s: `languages:` must be a list of rows" % where)
    packs: "list[LanguagePack]" = []
    owner: "dict[str, str]" = {}
    for i, r in enumerate(raw, 1):
        at = "%s: languages[%d]" % (where, i)
        if not isinstance(r, dict):
            raise LanguageError("%s must be a mapping" % at)
        rid = r.get("id")
        if not isinstance(rid, str) or not rid:
            raise LanguageError("%s: `id:` must be a non-empty string" % at)
        at = "%s (%s)" % (at, rid)
        unknown = sorted(set(r) - _ROW_KEYS)
        if unknown:
            raise LanguageError("%s: unknown key(s) %s; valid: %s"
                                % (at, ", ".join(unknown), ", ".join(sorted(_ROW_KEYS))))
        files = r.get("files")
        if not isinstance(files, dict):
            raise LanguageError("%s: `files:` must be a mapping {include, exclude}" % at)
        unknown = sorted(set(files) - _FILES_KEYS)
        if unknown:
            raise LanguageError("%s: `files:` unknown key(s) %s; valid: include, exclude"
                                % (at, ", ".join(unknown)))
        inc, exc = files.get("include"), files.get("exclude", [])
        if not _strings(inc) or not inc or not all(Path(g).suffix for g in inc):
            raise LanguageError("%s: `files.include` must be a non-empty list of basename "
                                "globs that end in a suffix, like '*.py'" % at)
        if not isinstance(exc, list) or not all(isinstance(g, str) and g for g in exc):
            raise LanguageError("%s: `files.exclude` must be a list of basename globs" % at)
        caps, stamp, offer = r.get("capabilities", []), r.get("stamp", []), r.get("offer", False)
        options = r.get("options", [])
        if not isinstance(options, list) or not all(isinstance(o, str) and o for o in options):
            raise LanguageError("%s: `options:` must be a list of option names" % at)
        if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
            raise LanguageError("%s: `capabilities:` must be a list of names" % at)
        if not isinstance(stamp, list) or not all(isinstance(s, str) and s for s in stamp):
            raise LanguageError("%s: `stamp:` must be a list of paths relative to harness/" % at)
        if not isinstance(offer, bool):
            raise LanguageError("%s: `offer:` must be true or false" % at)
        engines = _engines(r.get("engines"), at)
        _runners_and_checks(r, at)
        if ("reader" in r) == bool(engines):
            raise LanguageError("%s: give a `reader:` or `engines:` (one reader per engine), "
                                "not both and not neither" % at)
        if engines and "engine" not in options:
            raise LanguageError("%s: a row with `engines:` must list `engine` in `options:`" % at)
        pack = LanguagePack(id=rid, include=tuple(inc), exclude=tuple(exc),
                            reader_ref=_ref(r, "reader", at) if "reader" in r else None,
                            resolver_ref=_ref(r, "resolver", at),
                            capabilities=frozenset(caps), offer=offer, stamp=tuple(stamp),
                            options=tuple(options), engines=engines)
        for g, sfx in zip(inc, (Path(g).suffix.lower() for g in inc)):
            if sfx in owner and owner[sfx] != rid:
                raise LanguageError("%s: %s (suffix %s) already belongs to language %r"
                                    % (at, g, sfx, owner[sfx]))
            owner[sfx] = rid
        if any(p.id == rid for p in packs):
            raise LanguageError("%s: language id %r declared twice" % (at, rid))
        packs.append(pack)
    return tuple(packs)


def load(path: Path = BOOK) -> "tuple[LanguagePack, ...]":
    """The bin's packs. A missing, unreadable or row-less book is broken."""
    import yaml_io
    try:
        doc = yaml_io.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml_io.YAMLError) as e:
        raise LanguageError("language book %s unreadable: %s" % (path, e)) from e
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise LanguageError("language book %s: needs `schema_version: 1` and `languages:`" % path)
    cap = doc.get("guard_backup_max_mb", 64)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        raise LanguageError("language book %s: `guard_backup_max_mb:` must be a positive number "
                            "(`dirty_tree_guard`'s cap on the copies it takes)" % path)
    unknown = sorted(set(doc) - {"schema_version", "languages", "guard_backup_max_mb"})
    if unknown:
        raise LanguageError("language book %s: unknown key(s) %s" % (path, ", ".join(unknown)))
    packs = parse_rows(doc.get("languages"), str(path))
    if not packs:
        raise LanguageError("language book %s declares no language: nothing could be read" % path)
    return packs


@lru_cache(maxsize=1)
def packs() -> "tuple[LanguagePack, ...]":
    return load()


def enabled(descriptor: Any = None) -> "tuple[LanguagePack, ...]":
    """The packs `descriptor`'s tree is measured with, in the book's order: the
    ids its `languages:` names (absent there: Python alone), then the project's own
    languages (`adapters:` rows with a `resolver:`)."""
    want = getattr(descriptor, "languages", None) or {DEFAULT_ID: {}}
    return tuple(p for p in packs() if p.id in want) + project_packs(descriptor)


#: the resolver of every project language, named like a book row's
PROJECT_RESOLVER = ("codegraph._core.lang.resolver_command", "ProjectResolver")


@dataclass(frozen=True)
class ProjectPack(LanguagePack):
    """A language a project declares in `codemap.yaml` `adapters:` with a `resolver:`:
    read by the row's command (the project's adapter rows), resolved by the row's
    resolver command."""

    row: Any = field(default=None, compare=False)

    def resolver(self) -> Any:
        if "resolver" not in self._cache:
            self._cache["resolver"] = _import(self.resolver_ref, "language %r resolver" % self.id)(self.row)
        return self._cache["resolver"]


def project_packs(descriptor: Any) -> "tuple[ProjectPack, ...]":
    """The `ProjectPack` of each `adapters:` row of `descriptor` that names a `resolver:`."""
    return tuple(
        ProjectPack(id=r.id, include=tuple("*" + e for e in r.extensions), exclude=(),
                    reader_ref=None, resolver_ref=PROJECT_RESOLVER,
                    capabilities=frozenset({"snapshot", "where", "importers"}), row=r)
        for r in getattr(descriptor, "adapters", None) or () if getattr(r, "resolver", ()))


def is_project(pack: Any) -> bool:
    """`pack` is a project's own language, read by its `adapters:` command."""
    return isinstance(pack, ProjectPack)


def config_inputs(repo: Path, descriptor: Any) -> "tuple[str, ...]":
    """Every enabled pack's `config_inputs` for `descriptor`'s tree that git can report
    the state of, relative to `repo`: inside the repo, or above it but inside the same
    checkout (`../tsconfig.base.json` of a monorepo's package). A file outside the
    checkout (a tsconfig `extends` climbing out of it) is no part of the dirty scope."""
    import os
    from ..git import GitError, git_text
    try:
        top: "Path | None" = Path(git_text(repo, "rev-parse", "--show-toplevel")).resolve()
    except (GitError, OSError):
        top = None
    here = Path(repo).resolve()
    out = set()
    for x in (x for p in enabled(descriptor) for x in p.config_inputs(repo, descriptor)):
        full = (here / x).resolve()
        if full == here or not (full.is_relative_to(here) or (top is not None and full.is_relative_to(top))):
            continue
        out.add(Path(os.path.relpath(full, here)).as_posix())
    return tuple(sorted(out))


def reader_ref(pack: LanguagePack, descriptor: Any = None,
               repo: "Path | None" = None) -> "tuple[str, str]":
    """The reader `pack` measures `descriptor`'s tree (at `repo`) with: its own, or the
    declared engine's. An engine missing what it needs refuses (`AdapterRefused`,
    exit 3) naming the command that installs it -- never another engine."""
    if pack.reader_ref is not None:
        return pack.reader_ref
    name = options_of(descriptor, pack.id).get("engine")
    eng = pack.engine(str(name)) if name is not None else None
    if eng is None:
        raise LanguageError("language %r: no engine %r; the book lists: %s"
                            % (pack.id, name, ", ".join(e.name for e in pack.engines)))
    missing = missing_requirements(eng, repo)
    if missing:
        raise AdapterRefused("language %r engine %r needs %s -- install: %s"
                             % (pack.id, eng.name, "; ".join(missing), install_hint(eng)))
    return eng.reader_ref


_PIP = "python3 -m pip install "


def _uv_cache_dirs(env: "Mapping[str, str]") -> "list[Path]":
    home = Path(env.get("HOME") or "~").expanduser()
    out = [Path(env["UV_CACHE_DIR"])] if env.get("UV_CACHE_DIR") else []
    return out + [Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "uv", home / "Library" / "Caches" / "uv"]


def install_hint(eng: Engine, *, executable: "str | None" = None, prefix: "str | None" = None,
                 env: "Mapping[str, str] | None" = None) -> str:
    """`eng.install`, and -- for a pip line -- the same install for the interpreter that
    lacks the wheels (`python3` on PATH may be another one); for a throwaway `uv run`
    environment under uv's cache, which a pip install does not outlive, the `--with`
    arguments its command needs instead."""
    if not eng.install.startswith(_PIP):
        return eng.install
    exe, where = executable or sys.executable, Path(prefix or sys.prefix)
    env = os.environ if env is None else env
    specs = shlex.split(eng.install[len(_PIP):])
    if any(where == c or c in where.parents for c in _uv_cache_dirs(env)):
        return ("%s -- this interpreter (%s) is a throwaway `uv run` environment: add %s to the "
                "command that starts it (HARNESS_PYTHON)"
                % (eng.install, where, " ".join("--with " + shlex.quote(x) for x in specs)))
    return "%s -- for this interpreter: %s -m pip install %s" % (
        eng.install, shlex.quote(exe), " ".join(shlex.quote(x) for x in specs))


def missing_modules(eng: Engine) -> "list[str]":
    """The Python modules `eng` needs that this interpreter cannot import."""
    return [m for m in eng.requires_python if find_module(m) is None]


def missing_requirements(eng: Engine, repo: "Path | None" = None) -> "list[str]":
    """What `eng` needs and this machine lacks, one phrase each (empty: it can run)."""
    from . import toolchain
    out = []
    mods = missing_modules(eng)
    if mods:
        out.append("the Python module(s) %s, not installed for %s" % (", ".join(mods), sys.executable))
    if eng.requires_node:
        out += toolchain.node_problems(eng.requires_node, repo)
    for pkg in eng.requires_npm:
        out += toolchain.npm_problems(pkg)
    return out


def runnable_engine(pack: LanguagePack, repo: "Path | None" = None) -> "tuple[Engine | None, list[str]]":
    """The first of `pack`'s engines this machine can run once its pinned npm tools are
    installed (those `hs-code init run --only lang-tools` puts in place), else None and
    one line per engine naming what is missing and its install command."""
    from . import toolchain
    lines = []
    for eng in pack.engines:
        gone = []
        mods = missing_modules(eng)
        if mods:
            gone.append("the Python module(s) %s" % ", ".join(mods))
        if eng.requires_node:
            gone += toolchain.node_problems(eng.requires_node, repo)
        if not gone:
            return eng, []
        lines.append("engine %s: %s -- install: %s" % (eng.name, "; ".join(gone), eng.install or "-"))
    return None, lines


def options_of(descriptor: Any, pack_id: str) -> "dict[str, Any]":
    """What `descriptor` sets under `languages.<pack_id>:` ({} when nothing)."""
    return dict((getattr(descriptor, "languages", None) or {}).get(pack_id) or {})


def pack_for(path: "Path | str", among: "Sequence[LanguagePack]") -> "LanguagePack | None":
    """The pack among `among` owning `path`, or None."""
    return next((p for p in among if p.owns(path)), None)


#: The pack a file no pack owns resolves through: a project's `command:` reader
#: hands back dotted imports, which is this pack's resolution.
DEFAULT_ID = "python"


def default_of(among: "Sequence[LanguagePack]") -> LanguagePack:
    return next((p for p in among if p.id == DEFAULT_ID), among[0])
