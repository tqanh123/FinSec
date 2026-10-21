"""Adapter registry: the book's rows as readers, keyed by suffix — never an if-chain.

WHY A REGISTRY
    Replaces the hand-edited dispatch "if path ends with .py use the python
    reader, elif ... " inside every consumer. The rows are data
    (`book`: the readers of the bin's `hs-code-languages.yaml`, plus a project's `codemap.yaml`
    `adapters:`); `AdapterSet` is the one door every consumer reads through.
    The module-level `adapter_for`/`by_extension`
    answer for the bin's own book; a caller measuring a project tree
    (`snapshot.pipeline`) asks `AdapterSet.for_repo(repo, rows)` so the
    project's readers count too.

WHAT IT CANNOT SEE
    * A file whose suffix has no registered adapter is silently SKIPPED by
      `AdapterSet.split()`. The snapshot pipeline reports the count of skipped
      files so an unmeasured file never reads as a clean tree.
"""
from __future__ import annotations

import contextlib
import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, Type

from .base import LanguageAdapter
from ..lang import registry as lang_registry
from .book import AdapterError, AdapterRow, parse_rows
from .command_adapter import CommandAdapter


class AdapterSet:
    """Readers by suffix. Bin rows are classes imported by name; project rows are
    `CommandAdapter`s bound to their repo (trust is checked when one runs).

    A reader class that keeps a process across files offers `session(repo, paths)`, a
    context manager yielding the reader for one pass over `paths`; `reading` opens it
    once and closes it on the way out, error or not. Every other class is
    instantiated per file."""

    def __init__(self, classes: "dict[str, Type[Any]]", commands: "dict[str, CommandAdapter]",
                 repo: "Path | None" = None, live: "dict[str, Any] | None" = None):
        self._classes, self._commands, self._repo = classes, commands, repo
        self._live = dict(live or {})

    @classmethod
    def of(cls, rows: "Sequence[AdapterRow]", repo: "Path | None" = None) -> "AdapterSet":
        classes: "dict[str, Type[Any]]" = {}
        commands: "dict[str, CommandAdapter]" = {}
        for r in rows:
            if r.origin == "bin":
                try:
                    klass = getattr(importlib.import_module(str(r.module)), str(r.cls))
                except (ImportError, AttributeError) as e:
                    raise AdapterError("adapter %r: %s.%s does not import: %s"
                                       % (r.id, r.module, r.cls, e)) from e
                if not isinstance(klass, type) or not isinstance(klass(), LanguageAdapter):
                    raise AdapterError("adapter %r: %s.%s is no LanguageAdapter"
                                       % (r.id, r.module, r.cls))
                classes.update(dict.fromkeys(r.extensions, klass))
            else:
                if repo is None:
                    raise AdapterError("adapter %r: a project reader needs its repo" % r.id)
                commands.update(dict.fromkeys(r.extensions, CommandAdapter(r, repo)))
        return cls(classes, commands, repo)

    @classmethod
    def for_repo(cls, repo: Path, project_rows: "Sequence[AdapterRow]" = ()) -> "AdapterSet":
        if not project_rows:
            return default_set()
        return cls.of(bin_rows() + tuple(project_rows), repo)

    def adapter_for(self, path: Path) -> "LanguageAdapter | None":
        ext = path.suffix.lower()
        if ext in self._commands:
            return self._commands[ext]
        if ext in self._live:
            return self._live[ext]
        klass = self._classes.get(ext)
        if klass is None:
            return None
        # a reader that runs a tool asks for its repo (`for_repo`): the tool is looked
        # for the way a session's reader looks for it, never inside the repo
        return klass.for_repo(self._repo) if hasattr(klass, "for_repo") else klass()

    @contextlib.contextmanager
    def reading(self, paths: Iterable[Path]) -> Iterator["AdapterSet"]:
        """These readers for one pass over `paths`, each session reader's process open."""
        by_class: "dict[Type[Any], list[Path]]" = {}
        for p in paths:
            ext = p.suffix.lower()
            klass = self._classes.get(ext)
            if ext not in self._commands and klass is not None and hasattr(klass, "session"):
                by_class.setdefault(klass, []).append(p)
        with contextlib.ExitStack() as stack:
            live: "dict[str, Any]" = {}
            for klass, ps in by_class.items():
                reader = stack.enter_context(klass.session(self._repo, ps))
                live.update({e: reader for e, k in self._classes.items() if k is klass})
            yield AdapterSet(self._classes, self._commands, self._repo, live)

    def has(self, path: Path) -> bool:
        ext = path.suffix.lower()
        return ext in self._classes or ext in self._commands

    def split(self, paths: Iterable[Path]) -> "tuple[list[Path], list[Path]]":
        measurable: "list[Path]" = []
        skipped: "list[Path]" = []
        for p in paths:
            (measurable if self.has(p) else skipped).append(p)
        return measurable, skipped

    def ready(self, paths: Iterable[Path]) -> None:
        """Raise `AdapterRefused` now, before anything is written, when a project's
        command would read one of `paths` in an untrusted repo."""
        for ext in sorted({p.suffix.lower() for p in paths} & set(self._commands)):
            self._commands[ext].require_trust()

    def classes(self) -> "dict[str, Type[Any]]":
        return dict(self._classes)


def load_bin_rows(path: Path = lang_registry.BOOK) -> "tuple[AdapterRow, ...]":
    """The bin's rows: each language pack's own reader, keyed by its include globs'
    suffixes. A pack reading through an engine is a project's choice, so it has a row
    only in `rows_for` that project. The language book is checked by its own reader
    (`lang.registry.load`)."""
    return tuple(_row(p, p.reader_ref) for p in lang_registry.load(path) if p.reader_ref)


def _row(p: Any, ref: "tuple[str, str]") -> AdapterRow:
    return AdapterRow(p.id, p.suffixes(), "bin", module=ref[0], cls=ref[1])


def rows_for(packs: "Sequence[Any]", descriptor: Any,
             repo: "Path | None" = None) -> "tuple[AdapterRow, ...]":
    """The bin rows `descriptor`'s tree (at `repo`) is read with: each enabled pack's
    reader, an engine's one as the project declared it (`lang.registry.reader_ref`
    refuses one missing what it needs). A project's own language is read by its
    `adapters:` row, never a bin row."""
    return tuple(_row(p, lang_registry.reader_ref(p, descriptor, repo)) for p in packs
                 if not lang_registry.is_project(p))


@lru_cache(maxsize=1)
def bin_rows() -> "tuple[AdapterRow, ...]":
    return load_bin_rows()


@lru_cache(maxsize=1)
def default_set() -> AdapterSet:
    return AdapterSet.of(bin_rows())


def project_rows(raw: Any, where: str) -> "tuple[AdapterRow, ...]":
    """A project's `adapters:` block as rows; a suffix the bin owns is refused, and so is
    a bin language's id on a row that names a `resolver:`."""
    return parse_rows(raw, where, "project",
                      taken=[e for r in bin_rows() for e in r.extensions],
                      taken_ids=[p.id for p in lang_registry.packs()])


def adapter_for(path: Path) -> "LanguageAdapter | None":
    """The bin's adapter for one file, or None when the suffix is not registered."""
    return default_set().adapter_for(path)


def by_extension() -> "dict[str, Type[LanguageAdapter]]":
    """Snapshot of the bin's readers (for tests and diagnostics)."""
    return default_set().classes()
