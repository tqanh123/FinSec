#!/usr/bin/env python3
"""rops — run things on a remote machine: sync · run · test · bench · kill · lock · doctor.

Router only: `HsRopsCli` is the family core (`hs_family.cli.CliBase`) with two sources
appended. No verb logic here, and no if/else chain on verb names — a new command is a
row in `harness/data/hs-rops-registry.d/`, never a router edit.

Commands come from three places, merged by rank (a name two of them declare is a HARD
ERROR naming both — silent last-wins would turn a mistyped filename into a command that
quietly disappears):
    1. hs-rops-registry.d/             the core            [ship]
    2. $repo/.claude/rops/verbs/*.py   repo-specific       [repo]  (VERB/HELP + add_args/run)
    3. ROPS_CMD_<NAME> in config       command aliases     [alias]

Every command reads `args.cfg` / `args.host` / `args.hosts` / `args.tmux` off its namespace,
as it always has, and `ns.ctx` like every family command.

The measurement behaviors that make a number trustworthy live in ropslib/exec.py and
ropslib/quiet.py; the evidence for each is in the phase0 gate and the 260901 findings.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import pathlib
import shlex
import sys
from types import ModuleType
from typing import Any, Iterable, Mapping

from hs_family.cli import CliBase, CommandAliasMixin, Context, CustomCommandMixin, ShellFlag
from rops.ropslib import config as config_mod  # noqa: E402
from rops.ropslib.prog import PROG


def _load_path(path: pathlib.Path) -> ModuleType | None:
    spec = importlib.util.spec_from_file_location(f"rops_repo_verb_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _LegacyPackageLoader(importlib.abc.Loader):
    """Returns the ALREADY-canonical module object verbatim — never a second
    file-backed copy. `create_module` short-circuits Python's normal
    "exec the file" path entirely; `exec_module` is a no-op because the
    canonical module is already fully executed.

    Subclasses importlib.abc.Loader to DECLARE that protocol (the import
    machinery, not our code, drives these two methods) instead of leaving it
    implicit in the duck typing."""

    def __init__(self, real_module: ModuleType) -> None:
        self._real = real_module

    def create_module(self, spec):
        return self._real

    def exec_module(self, module: ModuleType) -> None:
        pass


class _LegacyPackageFinder:
    """Redirects `<alias>[.sub...]` imports to `<canonical>[.sub...]`.

    Aliasing only the TOP-level name (`sys.modules["ropslib"] = ropslib_pkg`)
    covers `from ropslib import hosts` (an ATTRIBUTE lookup on the already-
    loaded package — no second import happens), but not `from ropslib.transport
    import Host`: that asks the import system to load `ropslib.transport` AS
    ITS OWN MODULE, and `ropslib.__path__` — inherited from the real package
    it IS — still points at the real files on disk, so the default
    path-based finder happily imports `transport.py` a SECOND time under a
    second module identity (a different `Host` class, `isinstance` checks
    across the two copies silently false). This finder is inserted at the
    FRONT of `sys.meta_path` so it is consulted before that path-based
    finder ever gets the chance to create the duplicate; every submodule is
    handled uniformly (not one hand-picked list), so a module added to
    `ropslib`/`verbs` later needs no matching update here."""

    def __init__(self, aliases: dict) -> None:
        self._aliases = aliases  # {"ropslib": "rops.ropslib", "verbs": "rops.verbs"}

    def find_spec(self, fullname, path, target=None):
        head, _, rest = fullname.partition(".")
        canonical_head = self._aliases.get(head)
        if canonical_head is None:
            return None
        real_name = canonical_head + ("." + rest if rest else "")
        try:
            real_mod = importlib.import_module(real_name)
        except ImportError:
            return None
        return importlib.util.spec_from_loader(
            fullname, _LegacyPackageLoader(real_mod),
            is_package=hasattr(real_mod, "__path__"))


_LEGACY_FINDER_INSTALLED = False


def _alias_legacy_import_names(verbs_pkg: ModuleType) -> None:
    """A repo-local verb predates the move into `harness/scripts/rops/` and is
    documented (`extending.md`) to `from ropslib import ...` / `from verbs import
    ...` — bare top-level names that resolved back when `rops/` itself sat on
    `sys.path`. Alias the SAME already-loaded package objects under those old names
    so a `from ropslib import hosts` finds `hosts` as an attribute rather than
    importing the file a second time under a second module identity, and install
    a meta-path finder (idempotent — installed once per process) so a DOTTED
    submodule import (`from ropslib.transport import Host`, the shape
    `extending.md` itself documents) resolves to the same canonical object too
    — real users depend on this shape (`document-intake/.claude/rops/verbs/db.py`)."""
    global _LEGACY_FINDER_INSTALLED
    import rops.ropslib as ropslib_pkg
    sys.modules.setdefault("ropslib", ropslib_pkg)
    sys.modules.setdefault("verbs", verbs_pkg)
    if not _LEGACY_FINDER_INSTALLED:
        sys.meta_path.insert(0, _LegacyPackageFinder(
            {"ropslib": "rops.ropslib", "verbs": "rops.verbs"}))
        _LEGACY_FINDER_INSTALLED = True


class HsRopsCli(CustomCommandMixin, CommandAliasMixin, CliBase):
    prog, depth = PROG, 1
    custom_dir, alias_prefix = ".claude/rops/verbs", "ROPS_CMD_"
    shell_flags = (ShellFlag("--host", "host", takes_value=True, help="override ROPS_HOST for this call"),
                   ShellFlag("--hosts", "hosts", takes_value=True,
                             help="comma list; run on each in parallel (must differ)"),
                   ShellFlag("--tmux", "tmux", help="run through a tmux pane (attachable, survives this session)"))
    description = (__doc__ or "").split("\n\n")[0]
    footer = ("exit codes: 0 done  ·  1 findings  ·  2 broken/usage  ·  3 refused  ·  "
              "124 timed out  ·  125 never started  ·  126 vanished",)

    def __init__(self, cfg: Any = None, **kw: Any) -> None:
        super().__init__(**kw)
        self._cfg = cfg

    def main(self, argv: Any = None) -> int:
        """The exit code once more, on a stderr line that is not JSON: a filter that
        drops envelope lines (`grep -v '^{'`) dropped the only place it was printed."""
        rc = super().main(argv)
        sys.stdout.flush()  # last on a shared pipe too: stdout into a pipe is block-buffered
        print(f"[rops] exit_code={rc}", file=sys.stderr)
        return rc

    def finish(self, row: Any, ctx: Any, outcome: Any, output: str) -> dict:
        """A failed call's `!! …` lines go into `warnings` too: a refusal (rc 3) said why
        only in `error`, and a rebuild cut at its timeout (rc 124) nowhere a reader of
        `warnings` looks. One that names `--rebind` gets the call again with it."""
        env = super().finish(row, ctx, outcome, output)
        said = [ln[3:].strip() for ln in (self._call.get("stderr") or "").splitlines()
                if ln.startswith("!! ")]
        if not outcome.exit_code or not said:
            return env
        warnings = list(env.get("warnings") or ()) + said
        argv = [str(a) for a in self._call.get("argv", ())]
        if any("--rebind" in s for s in said) and "--rebind" not in argv:
            cut = argv.index("--") if "--" in argv else len(argv)
            warnings.append("to hand the lane to this tree: " + shlex.join(
                [PROG, *argv[:cut], "--rebind", *argv[cut:]]))
        env["warnings"] = warnings
        return env

    @property
    def cfg(self) -> Any:
        if self._cfg is None:
            self._cfg = config_mod.load()
        return self._cfg

    def repo_root(self, flags: Mapping[str, Any]) -> pathlib.Path:
        return pathlib.Path(self.cfg.repo)

    def project_root(self, flags: Mapping[str, Any]) -> pathlib.Path:
        return pathlib.Path(self.cfg.repo)

    def load_custom_file(self, path: pathlib.Path) -> Any:
        import rops.verbs as verbs_pkg
        _alias_legacy_import_names(verbs_pkg)
        return _load_path(path)

    def alias_modules(self, flags: Mapping[str, Any]) -> Iterable[Any]:
        from rops.ropslib import alias
        return alias.modules(self.cfg)

    def alias_where(self, mod: Any) -> str:
        key = self.alias_prefix + str(mod.VERB).upper()
        return "%s (%s)" % (key, self.cfg.source(key))

    def context(self, command: tuple, flags: Mapping[str, Any]) -> Context:
        return Context(cli=self.prog, command=command, root=self.project_root(flags), flags=flags,
                       config=self.cfg)

    def bind(self, ns: Any, ctx: Context) -> None:
        ns.cfg = ctx.config
        for name in ("host", "hosts", "tmux"):
            setattr(ns, name, ctx.flags.get(name))


def discover(cfg) -> list:
    """Every live command object, ship then repo then alias — read off the one table."""
    return [row.load() for row in HsRopsCli(cfg).table().live]


def main(argv: list[str] | None = None) -> int:
    return HsRopsCli().main(argv)
