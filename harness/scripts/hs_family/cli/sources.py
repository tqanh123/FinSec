"""Where command rows come from. Each source yields `CommandRow`s and never imports a
command module while doing so (a repo file is the exception: its `HELP` lives in the file).

    RegistrySource  rank 0   the CLI's own `<cli>-registry.d/`           [ship]
    PackSource      rank 10  an enabled pack's `<cli>-registry.d/`       [pack]
    RepoVerbSource  rank 20  `*.py` files a repo drops in a directory    [repo]
    AliasSource     rank 30  command aliases from config                 [alias]

A registry row is `{module, entry?, help}`: `entry` names a `Command` (or any object with
`add_args`/`run`) in `module`; without it the module itself is the command. In a one-level
CLI the row sits directly under `domains:` (`domains: {sync: {module, help}}`)."""
from __future__ import annotations

import functools
import importlib
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .. import registry as hs_registry
from ..errors import UsageError
from .contract import as_command
from .table import CommandRow, Origin


def _entry_loader(module: str, entry: Optional[str]) -> Callable[[], Any]:
    def load():
        where = "%s:%s" % (module, entry) if entry else module
        mod = importlib.import_module(module)
        obj = mod if entry is None else getattr(mod, entry, None)
        if obj is None:
            raise UsageError("%s: no such entry" % where)
        return as_command(obj, where)
    return load


def _rows_of(loaded, depth: int, kind: str) -> Iterable[CommandRow]:
    for domain, verbs in loaded.domains.items():
        if depth == 1:
            items: Iterable = [((domain,), verbs, (domain, next(iter(verbs), "")))]
        else:
            items = [((domain, v), spec, (domain, v)) for v, spec in verbs.items()]
        for path, spec, key in items:
            if not isinstance(spec, dict) or not spec.get("module"):
                raise UsageError("%s: %s declares no `module:`"
                                 % (loaded.origins.get(key, "registry"), " ".join(path)))
            yield CommandRow(path=path, help=str(spec.get("help") or ""),
                             origin=Origin(kind, str(loaded.origins.get(key, loaded.sources[0]))),
                             load=_entry_loader(str(spec["module"]), spec.get("entry")))
    for path, target in sorted(loaded.moved.items()):
        yield CommandRow(path=tuple(path), help="moved to `%s`" % target,
                         origin=Origin(kind, ", ".join(loaded.sources)),
                         load=lambda: None, moved_to=target)


class RegistrySource:
    kind = "ship"

    def __init__(self, env: "hs_registry.RegistryEnv", root: Path, *, depth: int, rank: int = 0,
                 check_moved: bool = True):
        self.env, self.root, self.depth, self.rank = env, Path(root), depth, rank
        self.check_moved = check_moved
        self.warnings: list = []

    def load(self):
        try:
            return hs_registry.load_registry(self.env, self.root, check_moved=self.check_moved)
        except hs_registry.RegistryError as e:
            raise UsageError(str(e)) from e

    def rows(self) -> Iterable[CommandRow]:
        return list(_rows_of(self.load(), self.depth, self.kind))


class PackSource:
    kind = "pack"

    def __init__(self, dirs: Iterable[Path], *, depth: int, rank: int = 10):
        self.dirs, self.depth, self.rank = tuple(Path(d) for d in dirs), depth, rank
        self.warnings: list = []

    def rows(self) -> Iterable[CommandRow]:
        try:
            loaded = hs_registry.load_fragment_dirs(self.dirs)
        except hs_registry.RegistryError as e:
            raise UsageError(str(e)) from e
        return list(_rows_of(loaded, self.depth, self.kind)) if loaded.sources else []


class RepoVerbSource:
    """`<dir>/*.py` (not `_*.py`), each declaring `VERB`/`HELP` + `add_args`/`run`. A file
    that fails to load is skipped with a warning naming it; the other commands still run."""
    kind = "repo"

    def __init__(self, directory: Path, load_file: Callable[[Path], Any], *, rank: int = 20):
        self.directory, self.load_file, self.rank = Path(directory), load_file, rank
        self.warnings: list = []

    def rows(self) -> Iterable[CommandRow]:
        out: list = []
        if not self.directory.is_dir():
            return out
        for path in sorted(self.directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                mod = self.load_file(path)
            except Exception as exc:  # noqa: BLE001 — someone else's file; report, keep going
                self.warnings.append("repo command %s did not load, skipping it — %s: %s"
                                     % (path, type(exc).__name__, exc))
                continue
            name = getattr(mod, "VERB", None) if mod is not None else None
            if name is None:
                continue
            out.append(CommandRow(path=(str(name),), help=str(getattr(mod, "HELP", "") or ""),
                                  origin=Origin(self.kind, str(path)),
                                  load=functools.partial(as_command, mod, str(path))))
        return out


class AliasSource:
    kind = "alias"

    def __init__(self, modules: Callable[[], Iterable[Any]], where_of: Callable[[Any], str], *,
                 rank: int = 30):
        self.modules, self.where_of, self.rank = modules, where_of, rank
        self.warnings: list = []

    def rows(self) -> Iterable[CommandRow]:
        return [CommandRow(path=(str(m.VERB),), help=str(getattr(m, "HELP", "") or ""),
                           origin=Origin(self.kind, self.where_of(m)),
                           load=functools.partial(as_command, m, self.where_of(m)))
                for m in self.modules()]


__all__ = ["AliasSource", "PackSource", "RegistrySource", "RepoVerbSource"]
