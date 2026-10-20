"""Resolving which host(s) a call runs on, and the transport gate every verb pays.

The gate is deliberately cheap: ssh reachable + destination dir exists. It catches the
case a detected destination created the wrong directory, which otherwise reads exactly
like a clean run. Load and orphan checks are NOT here — running work on a busy machine
is legitimate; only `bench` refuses that.
"""
from __future__ import annotations

import sys

from hs_family import UsageError
from rops.ropslib.prog import PROG
from rops.ropslib.transport import Host


def resolve(args) -> list[Host]:
    cfg = args.cfg
    opts = cfg.get("ROPS_SSH_OPTS")
    if getattr(args, "hosts", None):
        names = [h.strip() for h in args.hosts.split(",") if h.strip()]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            # Two runs on one machine share its CPU and whatever database they touch;
            # a fan-out that silently did that would poison every number it produced.
            raise UsageError(
                f"rops: --hosts repeats {', '.join(sorted(dupes))} — hosts must differ. "
                f"Two lanes on one machine share its CPU and its services.")
        if len(names) < 2:
            raise UsageError("rops: --hosts needs at least two hosts; use --host for one")
        return [Host(n, opts) for n in names]
    name = getattr(args, "host", None) or cfg.get("ROPS_HOST")
    if not name:
        raise UsageError(
            "rops: no host. ROPS_HOST is never detected — guessing one means running "
            "commands on someone else's machine.\n"
            f"      declare it in {cfg.repo}/.rops.env, export ROPS_HOST=..., or pass --host")
    return [Host(name, opts)]


def check_transport(cfg, host: Host, *, need_dir: bool = True) -> str | None:
    """Returns a human-readable problem, or None. Two ssh round-trips at most."""
    if not host.ok("echo ok", timeout=15):
        return f"ssh {host.name} unreachable"
    if need_dir:
        remote = cfg.get("ROPS_REMOTE_DIR", "~")
        if not host.ok(f"test -d {remote}", timeout=15):
            src = cfg.source("ROPS_REMOTE_DIR")
            lane = cfg.get("ROPS_LANE", "")
            build = (f"`{PROG} lane --name {lane}`" if lane else
                     f"`{PROG} lane --name <name>` (a git tree and its venv; `{PROG} sync` "
                     f"copies files only, so a gate reading `git ls-files` there sees nothing)")
            return (f"{host.name}:{remote} does not exist "
                    f"(destination came from: {src}) — run {build} first")
    return None


def gate(args, *, need_dir: bool = True) -> list[Host]:
    """Every verb's front door. Refuses before doing work, never mid-way."""
    hosts = resolve(args)
    problems = [(h, check_transport(args.cfg, h, need_dir=need_dir)) for h in hosts]
    bad = [(h, p) for h, p in problems if p]
    if bad:
        for h, p in bad:
            print(f"!! {p}", file=sys.stderr)
        raise SystemExit(2)
    return hosts
