"""ROPS_CMD_<NAME> -> a verb.

`test` is not special. It is one alias among however many a repo declares:

    ROPS_CMD_TEST=uv run python -m pytest -q
    ROPS_CMD_LINT=uv run ruff check .
    ROPS_CMD_BUILD=docker build -t app .

Each becomes `hs-rops test`, `hs-rops lint`, `hs-rops build`, each taking trailing
arguments: the alias's own options are read off the front, and from the first
token they do not consume (or a `--`) the rest goes to the command as it stands —
`hs-rops test -n 4` as well as `hs-rops test tests/x.py -n 4`. One mechanism instead
of three hardcoded verbs.
"""
from __future__ import annotations

import argparse
import shlex
import types
from typing import Callable

PREFIX = "ROPS_CMD_"


class VerbModule(types.ModuleType):
    """The attributes every verb module exposes (rops.py docstring), typed."""

    VERB: str
    HELP: str
    lenient: bool
    add_args: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], int]


def _make(name: str, command: str, source: str) -> VerbModule:
    mod = VerbModule(f"rops_alias_{name}")
    mod.VERB = name
    mod.HELP = command

    def own_args(p: argparse.ArgumentParser) -> None:
        from rops.verbs.run import (add_lane_arg, add_lock_arg, add_sync_arg, add_timeout_arg,
                                    add_tree_args)
        add_timeout_arg(p, 3000)
        p.add_argument("--tail", type=int, default=None)
        add_tree_args(p)
        add_lane_arg(p)
        add_sync_arg(p)
        add_lock_arg(p)

    def add_args(p: argparse.ArgumentParser) -> None:
        own_args(p)
        p.add_argument("extra", nargs=argparse.REMAINDER)

    def run(args: argparse.Namespace) -> int:
        from rops.verbs.run import execute
        if getattr(args, "argv", None) is not None:
            own, extra = _split(own_args, args.argv)
            from hs_family.cli.contract import CommandParser
            strict = CommandParser(prog=name)
            own_args(strict)
            vars(args).update(vars(strict.parse_args(own)))
        else:
            extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
        # the alias's own command is a shell string; what the caller adds is argv
        full = " ".join([command, *map(shlex.quote, extra)]).strip()
        if not getattr(args, "cores", None):
            # a suite asks its own width; a declared ROPS_CORES is a plain `run`'s
            from rops.ropslib.exec import width
            args.cores = width(full)
        return execute(args, full, label=name)

    mod.add_args = add_args
    mod.run = run
    mod.lenient = True  # `run` reads the whole argv; parse_known_args keeps `-n` from refusing
    return mod


def _split(own_args: Callable[[argparse.ArgumentParser], None], argv: list) -> tuple:
    """(the leading tokens the alias's own options consume, the rest without a leading
    `--`). Measured by the alias's own parser — `--x=v`, a value in the next token,
    abbreviations — so the cut is where that parser stops, never a guess at flag shape."""
    from hs_family.cli.contract import CommandParser, UsageError
    probe = CommandParser(prog="probe")
    own_args(probe)

    def takes(head: list) -> bool:
        try:
            probe.parse_args(head)
            return True
        except UsageError:
            return False

    i = 0
    while i < len(argv) and argv[i] != "--":
        nxt = next((j for j in (i + 1, i + 2) if j <= len(argv) and takes(argv[:j])), None)
        if nxt is None:
            break
        i = nxt
    rest = argv[i:]
    return argv[:i], rest[1:] if rest[:1] == ["--"] else rest


def modules(cfg) -> list[VerbModule]:
    out: list[VerbModule] = []
    for key in cfg.keys():
        if not key.startswith(PREFIX) or not cfg.get(key):
            continue
        name = key[len(PREFIX):].lower()
        if not name.isidentifier():
            continue
        out.append(_make(name, cfg.get(key), cfg.source(key)))
    return out
