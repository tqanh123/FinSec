"""run — any command, on the remote, with its real exit code."""
from __future__ import annotations

import argparse
import shlex

from hs_family import UsageError
from rops.ropslib import hosts, quiet, tree
from rops.ropslib.exec import report, run_remote



def add_args(p: argparse.ArgumentParser) -> None:
    add_timeout_arg(p, 1800)
    p.add_argument("--tail", type=int, default=None)
    p.add_argument("--label", default="run")
    add_tree_args(p)
    add_lane_arg(p)
    add_sync_arg(p)
    add_lock_arg(p)
    p.add_argument("command", nargs=argparse.REMAINDER)


def add_timeout_arg(p: argparse.ArgumentParser, default: int) -> None:
    """`--timeout`: `run` and every `ROPS_CMD_*` alias."""
    p.add_argument("--timeout", type=int, default=default, metavar="S",
                   help=f"seconds this call waits for the remote command, a lock wait "
                        f"included (default {default}); past it the command goes on "
                        f"detached and its rc comes from `fetch --wait <log>`")


def add_lock_arg(p: argparse.ArgumentParser) -> None:
    """`--lock NAME`: `run` and every `ROPS_CMD_*` alias, added to `ROPS_LOCK`."""
    p.add_argument("--lock", action="append", default=[], metavar="NAME",
                   help="queue on this machine-wide lock for the command's whole life, "
                        "behind any run holding the same name (repeatable; adds to "
                        "ROPS_LOCK) — for what no lane dir or ROPS_LOCK_ENV names")


def _with_locks(cfg, names: "list[str]"):
    """`cfg` with `--lock` names added to whatever `ROPS_LOCK` already declares."""
    if not names:
        return cfg
    merged = list(dict.fromkeys(cfg.csv("ROPS_LOCK") + [n.strip() for n in names if n.strip()]))
    return cfg.override("ROPS_LOCK", ",".join(merged), "--lock")


def add_tree_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stale-ok", action="store_true",
                   help="run even when the remote tree is not the local one (the `tree:` line says so)")


def add_sync_arg(p: argparse.ArgumentParser) -> None:
    """`--sync` and `--at`: `run` and every `ROPS_CMD_*` alias (next to `add_lane_arg`)."""
    p.add_argument("--sync", action="store_true",
                   help="bring the remote tree up to the local one first, then run: a lane is "
                        "rebuilt (only when it differs) through the `lane` verb's own steps, a "
                        "plain dir gets the `sync` verb's push")
    p.add_argument("--at", default=None, metavar="REV",
                   help="run in a throwaway remote worktree of exactly this commit (no "
                        "uncommitted or untracked file), with its own sync, removed afterwards "
                        "— also after a failure, a timeout, or an interrupt")


def add_lane_arg(p: argparse.ArgumentParser) -> None:
    """`--lane NAME`: `run` and every `ROPS_CMD_*` alias share this (each calls it
    explicitly, next to `add_tree_args`) — NOT folded into `add_tree_args` itself,
    which `ci`'s own parser also uses for `--stale-ok` and already owns a `--lane`
    of its own (a GitLab pipeline capability lane, a different concept the two
    verbs happen to name the same word for)."""
    p.add_argument("--lane", default=None, metavar="NAME",
                   help="build/select ~/rops/NAME for this call, and (re)build it first when "
                        "the tree check finds it missing or mismatched — same code the `lane` "
                        "verb runs, so `--lane` never drifts from it")


def _ensure_lane(cfg, host, name: str) -> int:
    """(Re)build `name` through the SAME steps the `lane` verb runs — never a second
    copy of that sequence — but only when the tree check says it must: the remote
    directory does not exist yet (a name never built), is not a git tree, or its
    fingerprint no longer matches local.

    `tree.check` deliberately does NOT refuse when the remote has no git tree at all
    (a plain `sync` destination is a legitimate answer to that); a `--lane` name is
    different — there is nothing there to compare against local, so a missing lane
    always builds, not just a mismatched one.
    """
    from rops.verbs import lane as lane_verb
    remote_dir = cfg.get("ROPS_REMOTE_DIR", "~")
    missing = tree.remote(host, remote_dir) is None
    if missing:
        print(f"tree: {host.name}:{remote_dir}: no lane there yet — building it")
    rc = 0 if missing else tree.check(cfg, host, stale_ok=False, rebuilding=True)
    if not missing and not rc:
        return 0
    build_rc = lane_verb.build(cfg, host, name, force=True)
    if build_rc:
        return build_rc
    return tree.check(cfg, host, stale_ok=False)


_LANE_ROOT = "~/rops/"


def _sync_first(cfg, host) -> int:
    """`--sync`: make the remote the local tree, through a path that already exists —
    never a third copy. A lane in effect (ROPS_LANE, or a git tree at `~/rops/<name>`)
    goes through `_ensure_lane`, which rebuilds only on a mismatch and ends in the tree
    check; anything else is a plain sync dir and gets the `sync` verb's push. A git tree
    elsewhere is not something either path may overwrite, so it refuses."""
    name = cfg.get("ROPS_LANE")
    remote_dir = cfg.get("ROPS_REMOTE_DIR", "~")
    if not name and remote_dir.startswith(_LANE_ROOT) and tree.remote(host, remote_dir) is not None:
        name = remote_dir[len(_LANE_ROOT):].strip("/")
    if name and "/" not in name:
        return _ensure_lane(cfg, host, name)
    if tree.remote(host, remote_dir) is not None:
        raise UsageError(f"rops: --sync: {host.name}:{remote_dir} is a git tree but not a lane "
                         f"under {_LANE_ROOT} — run `lane`/`sync` yourself, or pass --lane NAME")
    from rops.verbs import sync as sync_verb
    return sync_verb.push(cfg, host)


def execute(args, command: str, *, label: str = "run") -> int:
    """Shared by `run`, every ROPS_CMD_* alias, and fan-out."""
    if not command.strip():
        raise UsageError("rops: nothing to run")
    if getattr(args, "lock", None):
        args = argparse.Namespace(**{**vars(args), "cfg": _with_locks(args.cfg, args.lock)})
    tmux = getattr(args, "tmux", False)
    at = getattr(args, "at", None)
    if at:
        clash = [f for f, on in (("--lane", getattr(args, "lane", None)),
                                 ("--sync", getattr(args, "sync", False)), ("--tmux", tmux),
                                 ("--hosts", getattr(args, "hosts", None))) if on]
        if clash:
            raise UsageError(f"rops: --at picks the tree itself; it does not combine with "
                             f"{', '.join(clash)}")
        from rops.ropslib import snapshot
        host = hosts.gate(args, need_dir=False)[0]
        return snapshot.run_at(args.cfg, host, at, command, timeout_s=args.timeout,
                               tail=getattr(args, "tail", None), label=label)
    if getattr(args, "hosts", None):
        from rops.ropslib import fanout
        return fanout.execute(args, command, label=label)
    cfg = args.cfg
    lane_name = getattr(args, "lane", None)
    if lane_name:
        # Set for THIS call only (config.py::_apply_lane resolves the same way from
        # any other layer) — never mutates what `args.cfg` resolved for anyone else.
        cfg = cfg.with_lane(lane_name, "--lane")
        host = hosts.gate(args, need_dir=False)[0]
        rc = _ensure_lane(cfg, host, lane_name)
        if rc:
            return rc
    elif getattr(args, "sync", False):
        host = hosts.gate(args, need_dir=False)[0]  # a lane not built yet is --sync's job
        rc = _sync_first(cfg, host)
        if rc:
            return rc
    else:
        host = hosts.gate(args)[0]
        # BEFORE the command: a refused `lane` and a successful one leave the next verb's
        # output byte-identical (`All checks passed` on the previous tree — dev5, 260906).
        # The line names the tree the figures belong to; a mismatch refuses unless asked.
        rc = tree.check(cfg, host, stale_ok=getattr(args, "stale_ok", False))
        if rc:
            return rc
    if tmux:
        from rops.ropslib import tmux as tmux_mod
        res = tmux_mod.run_remote(cfg, host, command,
                                  timeout_s=args.timeout,
                                  tail=getattr(args, "tail", None), label=label)
    else:
        res = run_remote(cfg, host, command, timeout_s=args.timeout,
                         tail=getattr(args, "tail", None), label=label)
    rc = report(res)
    # After the command, because the figure that matters is what it LEFT BEHIND, and the
    # cost lands on the next run rather than this one. Advisory only — the rc is the
    # command's. See quiet.scratch_note for the measurement.
    where = cfg.get("ROPS_SCRATCH_PATH", "/dev/shm")
    note = quiet.scratch_note(quiet.used_pct(host, where),
                              quiet.free_kb(host, where), where,
                              quiet.warn_pct(cfg))
    if note:
        print(note)
    return rc


def command_line(argv: "list[str]") -> str:
    """One argument is a shell string, sent as written (pipes, `&&`, a far-side
    `$VAR`). Several are argv, each kept whole: joined bare, `bash -c 'cd X && cmd'`
    reached the far shell as `bash -c cd X && cmd`."""
    argv = list(argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    return argv[0] if len(argv) == 1 else shlex.join(argv)


def run(args: argparse.Namespace) -> int:
    return execute(args, command_line(args.command), label=args.label)
