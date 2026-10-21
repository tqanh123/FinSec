"""Adapter registry: the book's rows as readers, keyed by suffix — never an if-chain.

WHY A REGISTRY
    Replaces the hand-edited dispatch "if path ends with .py use the python
    reader, elif ... " inside every consumer. The rows are data
    (`book`: the bin's `hs-code-adapters.yaml`, plus a project's `codemap.yaml`
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

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence, Type

from .base import LanguageAdapter
from .book import AdapterError, AdapterRow, load_book, parse_rows
from .command_adapter import CommandAdapter


class AdapterSet:
    """Readers by suffix. Bin rows are classes imported by name; project rows are
    `CommandAdapter`s bound to their repo (trust is checked when one runs)."""

    def __init__(self, classes: "dict[str, Type[Any]]", commands: "dict[str, CommandAdapter]"):
        self._classes, self._commands = classes, commands

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
        return cls(classes, commands)

    @classmethod
    def for_repo(cls, repo: Path, project_rows: "Sequence[AdapterRow]" = ()) -> "AdapterSet":
        if not project_rows:
            return default_set()
        return cls.of(bin_rows() + tuple(project_rows), repo)

    def adapter_for(self, path: Path) -> "LanguageAdapter | None":
        ext = path.suffix.lower()
        if ext in self._commands:
            return self._commands[ext]
        klass = self._classes.get(ext)
        return klass() if klass else None

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


@lru_cache(maxsize=1)
def bin_rows() -> "tuple[AdapterRow, ...]":
    return load_book()


@lru_cache(maxsize=1)
def default_set() -> AdapterSet:
    return AdapterSet.of(bin_rows())


def project_rows(raw: Any, where: str) -> "tuple[AdapterRow, ...]":
    """A project's `adapters:` block as rows; a suffix the bin owns is refused."""
    return parse_rows(raw, where, "project",
                      taken=[e for r in bin_rows() for e in r.extensions])


def adapter_for(path: Path) -> "LanguageAdapter | None":
    """The bin's adapter for one file, or None when the suffix is not registered."""
    return default_set().adapter_for(path)


def by_extension() -> "dict[str, Type[LanguageAdapter]]":
    """Snapshot of the bin's readers (for tests and diagnostics)."""
    return default_set().classes()
