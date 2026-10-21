"""sync — rsync the tree to the remote, and back."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from rops.ropslib import hosts, tree
from rops.verbs.lane import ROPS_ENV_FILTER, anchored_keeps, pushed_excludes



def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--delete", action="store_true", help="delete remote files not present locally")
    p.add_argument("--back", action="store_true", help="pull the remote tree back over the local one")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")


def _confirm(question: str, *, auto: bool) -> bool:
    if auto:
        return True
    if not sys.stdin.isatty():
        print("!! refusing without --yes (not a terminal)", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _dirty(repo) -> list[str]:
    r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                       text=True, capture_output=True)
    return [line for line in r.stdout.splitlines() if line.strip()]


def _rsync(cfg, src: str, dst: str, *, delete: bool, back: bool = False) -> int:
    # Pushed as a lane pushes (`lane.pushed_excludes`: a kept name plain, the rest
    # perishable); pulled back, a plain exclude: what the local side excludes in a
    # directory the remote lacks stays home.
    patterns = cfg.csv("ROPS_RSYNC_EXCLUDES")
    excludes: list[str] = ([x for e in patterns for x in ("--exclude", e)] if back
                           else pushed_excludes(cfg, patterns + anchored_keeps(cfg, patterns)))
    # A linked worktree's `.git` is a FILE (`gitdir: …/.git/worktrees/<name>`), and the
    # `.git/` exclude (trailing slash = directory) does not match it. Measured 260906
    # (dev6): syncing over a `hs-rops lane` checkout replaced the lane's own `.git` pointer
    # with one aimed at a path that does not exist over there — the lane stopped being
    # a git tree, 60 repo gates went red, and rsync reported rc=0. Measured 260907
    # (lead): the SAME hole from the other side — local `.git` a directory, remote lane
    # `.git` a pointer file, `sync --delete` → the pointer is "not in the source" and
    # `.git/` does not protect a file, so rsync deleted it (rc=0, lane no longer git).
    # The remote's shape is unknown from here; exclude the root `.git` in both forms,
    # always — an exclude also protects the path from --delete.
    excludes += ["--exclude", "/.git"]
    # `.rops.env*` is this checkout's config. Out: hidden from the sender only, so a
    # stale remote copy is still removed by --delete. Back: excluded, which also
    # protects the local file from --delete.
    excludes += (["--exclude", "/.rops.env*"] if back else list(ROPS_ENV_FILTER))
    cmd = (["rsync", "-az", "--stats"] + (["--delete"] if delete else [])
           + excludes + [src, dst])
    t = time.perf_counter()
    r = subprocess.run(cmd, text=True, capture_output=True)
    for line in r.stdout.splitlines():
        if line.startswith(("Number of files:", "Total transferred")):
            print(line)
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
    print(f"rsync {time.perf_counter() - t:.2f}s rc={r.returncode}")
    return r.returncode


def run(args: argparse.Namespace) -> int:
    cfg = args.cfg
    host = hosts.gate(args, need_dir=False)[0]
    remote = cfg.get("ROPS_REMOTE_DIR", "~")
    local = str(cfg.repo).rstrip("/")

    if args.back:
        # The single most destructive path here: it writes over the working tree.
        dirty = _dirty(cfg.repo)
        if dirty:
            print(f"!! {len(dirty)} uncommitted change(s) in {cfg.repo}:", file=sys.stderr)
            for line in dirty[:20]:
                print(f"     {line}", file=sys.stderr)
            if len(dirty) > 20:
                print(f"     ... and {len(dirty) - 20} more", file=sys.stderr)
            print("   --back would overwrite them with the remote copy.", file=sys.stderr)
            if not _confirm("overwrite anyway?", auto=args.yes):
                return 2
        if args.delete and not _confirm(
                f"--back --delete removes local files absent on {host.name}. proceed?",
                auto=args.yes):
            return 2
        return _rsync(cfg, f"{host.name}:{remote}/", local + "/", delete=args.delete, back=True)

    if args.delete and cfg.is_detected("ROPS_REMOTE_DIR"):
        # The destination was guessed, not declared, and --delete removes things there.
        if not _confirm(f"--delete against {host.name}:{remote} (a DETECTED path). proceed?",
                        auto=args.yes):
            return 2
    return push(cfg, host, delete=args.delete)


def push(cfg, host, *, delete: bool = False) -> int:
    """The local tree over ROPS_REMOTE_DIR — this verb's forward path, also what
    `run --sync` drives for a destination that is not a lane."""
    remote = cfg.get("ROPS_REMOTE_DIR", "~")
    host.run(f"mkdir -p {remote}")
    rc = _rsync(cfg, str(cfg.repo).rstrip("/") + "/", f"{host.name}:{remote}/", delete=delete)
    rc = rc or mirror_index(cfg, host, remote) or _repair_editables(cfg, host, remote)
    # The same `tree:` line `run` prints, so a sync that left a difference says so
    # here, with its paths, instead of `run` refusing a tree `sync` called done.
    return rc or tree.check(cfg, host, synced=True)


def _repair_editables(cfg, host, remote: str) -> int:
    """New code synced under an editable member built while its code dir was empty: the
    venv still has no import hook for it. Only where the remote already has a `.venv`
    the lane's sync command would manage — a sync never creates one."""
    from rops.verbs import lane as lane_verb
    cmd, _why = lane_verb.sync_plan(cfg, cfg.repo)
    if cmd is None or not host.ok(f"test -d {remote}/.venv", timeout=30):
        return 0
    path = cfg.get("ROPS_PATH_PREPEND", "$HOME/.local/bin")
    return lane_verb.repair_editables(cfg, host, remote, path, cmd)


def mirror_index(cfg, host, remote: str) -> int:
    """Onto a git tree at the local HEAD (a lane), make its index say what the local
    index says — rsync moved bytes only, and a gate listing the tree through git reads
    the tracked set. The lane index is reset to its HEAD first, so a path unstaged here
    since the last sync is untracked there again. A plain dir is left alone; a tree at
    another HEAD too (its `tree:` line already refuses it — `lane` rebuilds it)."""
    head = host.out(f"cd {remote} 2>/dev/null && git rev-parse --is-inside-work-tree "
                    f">/dev/null 2>&1 && git rev-parse HEAD", timeout=60).strip()
    if not head:
        return 0
    r = subprocess.run(["git", "-C", str(cfg.repo), "rev-parse", "HEAD"],
                       text=True, capture_output=True)
    if r.returncode or r.stdout.strip() != head:
        print(f"index not mirrored: {remote} is at HEAD {head[:8]}, local at "
              f"{r.stdout.strip()[:8] or '?'} — `lane` rebuilds it")
        return 0
    from rops.verbs.lane import _staged, index_mirror_cmd
    reset = host.run(f"cd {remote} && git reset -q", timeout=120)
    if reset.returncode:
        print(f"!! index reset failed: {(reset.stderr or reset.stdout or '').strip()}",
              file=sys.stderr)
        return reset.returncode
    cmd = index_mirror_cmd(cfg.repo, remote)
    if cmd:
        m = host.run(cmd, timeout=120)
        if m.returncode:
            print(f"!! index mirror failed: {(m.stderr or m.stdout or '').strip()}",
                  file=sys.stderr)
            return m.returncode
    add, drop = _staged(cfg.repo)
    print(f"index: {len(add)} staged add/modify, {len(drop)} staged delete mirrored from local")
    return 0
