"""Language pack seam — the kernel owns the SHAPE, a pack owns the MEANING.

A pack is one row of the bin's language book (`harness/data/hs-code-languages.yaml`,
read by `lang.registry`): the files it owns, the reader that parses one of them
(`adapter.base.LanguageAdapter`), and the resolver that names a tree's modules and
classifies one parsed file's imports against them. The pipeline asks the pack that
owns a file; it never branches on a language.

    LanguagePack      a row; its reader becomes a bin adapter row
                      (`adapter.registry.load_bin_rows`), its resolver imports lazily
    Resolver          naming + resolution: what a module is called, what an import
                      points at -- language semantics live here, nowhere else
    ClassifiedImports one file's imports split the way every record carries them
    SourceRef         one measurable file, located in the tree shape; its `role`
                      (test | story) is the pack's test convention, decided when the
                      pack names the file and written on the record

WHAT IT CANNOT SEE
    * A pack is trusted bin code: its reader and resolver import in-process. A
      project's own language comes in as a `command:` reader (`adapter.book`), run
      only in a trusted repo.
"""
from __future__ import annotations

import fnmatch
import importlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .errors import AdapterError


#: a reader's `problems` kinds meaning a region of the file did not parse -- the
#: tree-sitter reader's `ERROR`/`MISSING`, the compiler's `syntax`, a reader's own
#: `EXCEPTION`; what it read around one is not the whole file
BROKEN_PROBLEMS = frozenset({"ERROR", "MISSING", "syntax", "EXCEPTION"})


class LanguageError(AdapterError):
    """A language book row, or what it names, is outside the contract."""


@dataclass(frozen=True)
class SourceRef:
    """One measurable file, located in the tree shape (old `discover()` row)."""

    path: Path
    module: str
    root: str              # root id
    unit: str               # "<root-id>/<unit-name>"
    unit_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: the pack that named it; a Python record carries no `lang` key
    lang: str = "python"
    #: "test" | "story" by the pack's own test convention, None for a source (and for
    #: every Python file: Python's convention is `FilesLayout`'s path predicate)
    role: "str | None" = None


@dataclass(frozen=True)
class ClassifiedImports:
    module_level: "set[str]"
    function_local: "set[str]"
    external: "set[str]"
    type_only: "set[str]"
    ambiguous: "set[tuple[str, tuple[str, ...]]]"
    #: (specifier, reason) the resolver could not place; a pack that never leaves
    #: one unplaced (Python) never fills it
    unresolved: "frozenset[tuple[str, str]]" = frozenset()
    #: what the record says beside an unplaced (specifier, reason) -- `target_pkg`,
    #: `candidates`, `notes`
    unresolved_detail: "dict[tuple[str, str], dict[str, Any]]" = field(default_factory=dict)
    #: what the record says beside its edges about a package boundary each crosses
    #: (`import_flags`); a pack without packages (Python) never fills it
    flags: "tuple[dict[str, Any], ...]" = ()


@dataclass(frozen=True)
class Named:
    """What one pack's walk of a tree found: its measurable files, its namespace
    roots, notes, and the census a reader of the snapshot needs beside the modules
    -- `files_seen` (the denominator), `skipped`, `ignored_skipped` -- with the files
    it saw but did not parse (`skipped_files`: {path, bytes, reason})."""

    sources: "list[SourceRef]"
    ns_roots: "set[str]" = field(default_factory=set)
    notes: "list[str]" = field(default_factory=list)
    census: "dict[str, Any]" = field(default_factory=dict)
    skipped_files: "list[dict[str, Any]]" = field(default_factory=list)
    #: what `classify` reads that the walk already computed (kept in the import
    #: context's `tables`, never written to the snapshot)
    tables: "dict[str, Any]" = field(default_factory=dict)


def build_symbol_entries(parsed: "Any") -> "list[dict]":
    """The per-module `symbols` list a snapshot record carries -- one dict
    per top-level func/class (`name`/`kind`/`lines`/`sha`/`refs`, plus
    `bases` for a class that has any), methods nested the same way. Factored out of the
    pipeline's per-file loop (the ONE place that used to build this shape) so a caller
    measuring a SINGLE file
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


class Resolver(Protocol):
    """Module naming + import resolution for one language. `ctx` is the
    pipeline's `ImportContext` (the tree's module set and the pack's prepared
    tables)."""

    def name_modules(self, repo: Path, roots: list, descriptor: Any, payload: Any) -> Named: ...

    def prepare(self, repo: Path, sources: "list[SourceRef]", roots: list,
                import_roots: "tuple[str, ...]") -> "dict[str, Any]": ...

    def classify(self, parsed: Any, ctx: Any) -> ClassifiedImports: ...

    def module_at(self, ctx: Any, rel_path: str) -> "str | None": ...

    def symbol_entries(self, parsed: Any) -> "list[dict]": ...

    def default_unit(self, rel_path: str, root_path: str) -> str:
        """The unit a file of this language belongs to when no declared
        `languages.<id>.units` rule matches it (`lang.unit_policy`); `root_path` is
        its root's repo-relative directory. Asked of a pack whose modules are path
        ids; a Python unit is its root's first directory, named while its modules are."""
        ...


def _import(ref: "tuple[str, str]", what: str) -> Any:
    mod, cls = ref
    try:
        klass = getattr(importlib.import_module(mod), cls)
    except (ImportError, AttributeError) as e:
        raise LanguageError("%s %s.%s does not import: %s" % (what, mod, cls, e)) from e
    if not isinstance(klass, type):
        raise LanguageError("%s %s.%s is not a class" % (what, mod, cls))
    return klass


@dataclass(frozen=True)
class NpmPackage:
    """An npm package an engine runs, pinned by version and lockfile integrity; `pin`
    names the shipped `<pin>.pin.json` + `<pin>.pin.lock.json` pair (relative to
    harness/scripts) it is installed from."""

    name: str
    version: str
    integrity: str
    pin: str


@dataclass(frozen=True)
class Engine:
    """One way a pack reads its files (`engines.<name>` of a row): its reader, what
    that reader needs -- Python modules, a Node.js version, pinned npm packages -- and
    the line that installs them."""

    name: str
    reader_ref: "tuple[str, str]"
    requires_python: "tuple[str, ...]" = ()
    install: str = ""
    #: a `>=X.Y[.Z]` floor on the `node` the reader runs, or "" for none
    requires_node: str = ""
    requires_npm: "tuple[NpmPackage, ...]" = ()


@dataclass(frozen=True)
class LanguagePack:
    """One row of the language book. A pack reads its files with its own `reader`,
    or with the engine a project names (`languages.<id>.engine`) among `engines`."""

    id: str
    include: "tuple[str, ...]"
    exclude: "tuple[str, ...]"
    reader_ref: "tuple[str, str] | None"
    resolver_ref: "tuple[str, str]"
    capabilities: "frozenset[str]" = frozenset()
    offer: bool = False
    stamp: "tuple[str, ...]" = ()
    #: the keys a project may set under `languages.<id>:` in its `codemap.yaml`
    options: "tuple[str, ...]" = ()
    engines: "tuple[Engine, ...]" = ()
    _cache: "dict[str, Any]" = field(default_factory=dict, compare=False, repr=False, hash=False)

    def owns(self, path: "Path | str") -> bool:
        name = PurePosixPath(str(path)).name
        return (any(fnmatch.fnmatchcase(name, g) for g in self.include)
                and not any(fnmatch.fnmatchcase(name, g) for g in self.exclude))

    def suffixes(self) -> "tuple[str, ...]":
        """The `Path.suffix` of each include glob -- the key a reader is found by."""
        return tuple(dict.fromkeys(PurePosixPath(g).suffix.lower() for g in self.include))

    def engine(self, name: str) -> "Engine | None":
        return next((e for e in self.engines if e.name == name), None)

    def resolver(self) -> Resolver:
        if "resolver" not in self._cache:
            self._cache["resolver"] = _import(self.resolver_ref,
                                              "language %r resolver" % self.id)()
        return self._cache["resolver"]

    def config_inputs(self, repo: Path, descriptor: Any) -> "tuple[str, ...]":
        """Repo files outside the measured roots whose content changes how this pack
        names and resolves `descriptor`'s tree (a resolver's `config_inputs`; none by
        default) -- part of the dirty scope, so an uncommitted edit to one is seen."""
        fn = getattr(self.resolver(), "config_inputs", None)
        return tuple(fn(repo, list(getattr(descriptor, "roots", ()) or ()), descriptor)) if fn else ()
