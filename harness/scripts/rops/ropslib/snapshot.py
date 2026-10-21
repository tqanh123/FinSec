"""`run --at <rev>` — the command runs in a throwaway remote worktree of exactly one
commit, and that worktree is gone when the call returns.

Why: a lane carries the WORKING tree, so a file an editor is still writing, or an
untracked scratch file, reaches the run too (reported from real use: half-written files
made a lane run red). A commit is a fixed input. Same machinery as `lane` (bare repo,
`push --no-verify`, `git worktree add --detach`), but the worktree is created per call
under a unique name next to the bare repo and removed in a `finally` — after a pass, a
failure, a timeout, or an interrupt. Processes still running with their cwd inside it
(a timed-out command keeps running over there once ssh is cut) are killed first, so
nothing is left reading a tree that no longer exists.

Its own venv: the lane's sync command (`lane.sync_plan`) runs inside the worktree, so a
uv repo gets a fresh `.venv` there. What a `kill -9` of THIS process leaves behind is
the one thing `finally` cannot reach: the dir is printed up front (`at-<sha>-<pid>-<t>`
next to the bare repo) so it can be removed by hand.
"""
from __future__ import annotations

import os
import posixpath
import signal
import subprocess
import sys
import time

from hs_family import UsageError
from rops.ropslib.exec import report, run_remote
from rops.ropslib.transport import sq

REF_PREFIX = "refs/rops-at/"


def push_url(host, bare: str) -> str:
    """scp-like url relative to the remote home; the ssh alias carries port/key."""
    return f"{host.name}:{bare.removeprefix('~/')}"


def resolve_commit(repo, rev: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                        f"{rev}^{{commit}}"], text=True, capture_output=True)
    sha = r.stdout.strip()
    if r.returncode or not sha:
        raise UsageError(f"rops: --at {rev}: not a commit in {repo}")
    return sha


def cleanup_script(bare: str, wt: str, ref: str) -> str:
    """Kill what still runs inside `wt`, then remove the worktree, its admin entry and
    the pushed ref. Every step tolerates a half-built state (the add may have failed)."""
    return f"""W=$(cd {wt} 2>/dev/null && pwd -P) || W=
if [ -n "$W" ]; then
  for p in /proc/[0-9]*; do
    c=$(readlink "$p/cwd" 2>/dev/null) || continue
    case "$c" in "$W"|"$W"/*) kill -9 "${{p#/proc/}}" 2>/dev/null;; esac
  done
fi
git -C {bare} worktree remove --force {wt} 2>/dev/null || rm -rf {wt}
git -C {bare} worktree prune 2>/dev/null
git -C {bare} update-ref -d {ref} 2>/dev/null
test ! -e {wt}"""


def run_at(cfg, host, rev: str, command: str, *, timeout_s: int, tail: int | None,
           label: str) -> int:
    from rops.verbs import lane as lane_verb

    sha = resolve_commit(cfg.repo, rev)
    bare = cfg.get("ROPS_BARE", "~/rops/di.git")
    tag = f"{sha[:12]}-{os.getpid()}-{int(time.time())}"
    ref = REF_PREFIX + tag
    wt = posixpath.join(posixpath.dirname(bare.rstrip("/")) or "~", f"at-{tag}")

    r = host.run(f"command -v git >/dev/null || {{ echo no-git; exit 3; }}; "
                 f"test -d {bare} || git init -q --bare {bare}", timeout=60)
    if r.returncode:
        print(f"!! remote: {(r.stdout or r.stderr or '').strip()}", file=sys.stderr)
        return r.returncode

    # SIGTERM (a supervisor's timeout) must reach `finally` too, not end the process.
    prev = signal.signal(signal.SIGTERM, _raise_interrupt) if _main_thread() else None
    try:
        p = subprocess.run(["git", "-C", str(cfg.repo), "push", "-q", "--force", "--no-verify",
                            push_url(host, bare), f"{sha}:{ref}"], text=True, capture_output=True)
        if p.returncode:
            print(p.stderr.strip(), file=sys.stderr)
            return p.returncode
        r = host.run(f"bash -c {sq(f'git -C {bare} worktree add -q --detach {wt} {sha}')}",
                     timeout=300)
        if r.returncode:
            print(f"!! worktree add: {(r.stderr or r.stdout or '').strip()}", file=sys.stderr)
            return r.returncode
        print(f"tree: {host.name}:{wt}: clean worktree at {sha[:8]} (--at {rev}); "
              f"removed when this call returns")
        shared_env_note(cfg)
        at_cfg = cfg.override("ROPS_REMOTE_DIR", wt, "--at")
        cmd, why = lane_verb.sync_plan(cfg, cfg.repo)
        if cmd is None:
            print(why)
        else:
            path = cfg.get("ROPS_PATH_PREPEND", "$HOME/.local/bin")
            rc = report(run_remote(at_cfg, host, f"export PATH={path}:$PATH && {cmd}",
                                   timeout_s=900, tail=3, label=f"{label}-at-sync"))
            if rc:
                return rc
        return report(run_remote(at_cfg, host, command, timeout_s=timeout_s, tail=tail,
                                 label=label))
    finally:
        if prev is not None:
            signal.signal(signal.SIGTERM, prev)
        done = host.run(f"bash -c {sq(cleanup_script(bare, wt, ref))}", timeout=120)
        print(f"at: {wt} removed" if done.returncode == 0
              else f"!! at: could not remove {host.name}:{wt} — remove it by hand")


def shared_env_note(cfg) -> None:
    """A snapshot worktree is its own dir but exports the lane's ROPS_ENV: whatever
    those name (a DB) is the lane's too. Names only — never a value."""
    names = [e.split("=", 1)[0] for e in cfg.csv("ROPS_ENV")]
    loose = [n for n in names if n not in set(cfg.csv("ROPS_LOCK_ENV"))]
    if loose:
        print(f"note: --at exports the lane's ROPS_ENV ({', '.join(loose)}) — a DB it names "
              f"is shared with every run on the lane; list it in ROPS_LOCK_ENV and runs "
              f"exporting the same value take turns")


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def _main_thread() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()
