"""Mixins only APPEND a source; ordering is the table's job (by rank), so `class X(A, B)`
and `class X(B, A)` build the same table."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Iterable

from .sources import AliasSource, PackSource, RepoVerbSource


class PackMixin:
    def sources(self, flags) -> list:
        return super().sources(flags) + [  # type: ignore[misc]
            PackSource(self.pack_frag_dirs(flags), depth=self.depth)]  # type: ignore[attr-defined]

    def pack_frag_dirs(self, flags) -> Iterable[Path]:
        return ()


class CustomCommandMixin:
    custom_dir = ""

    def sources(self, flags) -> list:
        return super().sources(flags) + [  # type: ignore[misc]
            RepoVerbSource(self.repo_root(flags) / self.custom_dir, self.load_custom_file)]

    def repo_root(self, flags) -> Path:
        return self.project_root(flags)  # type: ignore[attr-defined]

    def load_custom_file(self, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location("hs_repo_command_%s" % path.stem, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class CommandAliasMixin:
    alias_prefix = ""

    def sources(self, flags) -> list:
        return super().sources(flags) + [  # type: ignore[misc]
            AliasSource(lambda: self.alias_modules(flags), self.alias_where)]

    def alias_modules(self, flags) -> Iterable[Any]:
        return ()

    def alias_where(self, mod: Any) -> str:
        return "%s%s" % (self.alias_prefix, str(mod.VERB).upper())


__all__ = ["CommandAliasMixin", "CustomCommandMixin", "PackMixin"]
