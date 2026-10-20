"""One CLI's command table: rows from every source, merged by the source's `rank` (never by
the order sources — or the mixins that append them — are listed). A command path two
sources declare is a `CommandConflict` naming both; nothing wins silently."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, Protocol

import attrs

from ..errors import UsageError


@attrs.define(frozen=True)
class Origin:
    kind: str   # ship | pack | repo | alias
    where: str

    def label(self) -> str:
        return "[ship]" if self.kind == "ship" else "[%s: %s]" % (self.kind, self.where)


@attrs.define(frozen=True, kw_only=True)
class CommandRow:
    path: tuple
    help: str
    origin: Origin
    load: Callable[[], Any] = attrs.field(eq=False, repr=False)
    moved_to: Optional[str] = None


class CommandSource(Protocol):
    rank: int
    warnings: list

    def rows(self) -> Iterable[CommandRow]: ...


class CommandConflict(UsageError):
    pass


@attrs.define(frozen=True)
class CommandTable:
    rows: tuple
    depth: int
    warnings: tuple = ()

    @classmethod
    def build(cls, sources: Iterable[CommandSource], *, depth: int) -> "CommandTable":
        by_path: dict = {}
        warnings: list = []
        for src in sorted(sources, key=lambda s: s.rank):
            for row in src.rows():
                # A moved row may sit one word below a live command (`trust list` in a
                # one-level CLI): it is refused by name at dispatch, never routed.
                if (len(row.path) != depth if row.moved_to is None
                        else not 1 <= len(row.path) <= max(depth, 2)):
                    raise UsageError("%s %s: a command path of depth %d in a depth-%d CLI"
                                     % (row.origin.label(), " ".join(row.path), len(row.path), depth))
                first = by_path.get(row.path)
                if first is not None:
                    raise CommandConflict("command %r declared twice — [%s] %s and [%s] %s" % (
                        " ".join(row.path), first.origin.kind, first.origin.where,
                        row.origin.kind, row.origin.where))
                by_path[row.path] = row
            warnings.extend(src.warnings)
        return cls(tuple(by_path.values()), depth, tuple(warnings))

    @property
    def live(self) -> tuple:
        return tuple(r for r in self.rows if r.moved_to is None)

    @property
    def moved(self) -> dict:
        return {r.path: r.moved_to for r in self.rows if r.moved_to is not None}

    def find(self, path: tuple) -> Optional[CommandRow]:
        for r in self.rows:
            if r.path == tuple(path):
                return r
        return None

    def names(self, prefix: tuple) -> tuple:
        n = len(prefix)
        return tuple(sorted({r.path[n] for r in self.live if r.path[:n] == tuple(prefix)}))


__all__ = ["CommandConflict", "CommandRow", "CommandSource", "CommandTable", "Origin"]
