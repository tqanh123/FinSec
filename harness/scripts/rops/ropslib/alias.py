"""ROPS_CMD_<NAME> -> a verb.

`test` is not special. It is one alias among however many a repo declares:

    ROPS_CMD_TEST=uv run python -m pytest -q
    ROPS_CMD_LINT=uv run ruff check .
    ROPS_CMD_BUILD=docker build -t app .

Each becomes `hs-rops test`, `hs-rops lint`, `hs-rops build`, each taking trailing
arguments after `--`. One mechanism instead of three hardcoded verbs.
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
    add_args: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], int]


def _make(name: str, command: str, source: str) -> VerbModule:
    mod = VerbModule(f"rops_alias_{name}")
    mod.VERB = name
    mod.HELP = command

    def add_args(p: argparse.ArgumentParser) -> None:
        from rops.verbs.run import (add_lane_arg, add_lock_arg, add_sync_arg, add_timeout_arg,
                                    add_tree_args)
        add_timeout_arg(p, 3000)
        p.add_argument("--tail", type=int, default=None)
        add_tree_args(p)
        add_lane_arg(p)
        add_sync_arg(p)
        add_lock_arg(p)
        p.add_argument("extra", nargs=argparse.REMAINDER)

    def run(args: argparse.Namespace) -> int:
        from rops.verbs.run import execute
        extra = args.extra[1:] if args.extra[:1] == ["--"] else args.extra
        # the alias's own command is a shell string; what the caller adds is argv
        full = " ".join([command, *map(shlex.quote, extra)]).strip()
        return execute(args, full, label=name)

    mod.add_args = add_args
    mod.run = run
    return mod


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
