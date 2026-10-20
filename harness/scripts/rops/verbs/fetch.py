"""fetch — pull one remote path back. The full log stays remote (behavior 4); this is
how you go get it when the tail is not enough.

`--print`/`--grep` read the path in place and print it: reading a log or an artefact
needs no tree check, so neither one checks, syncs or builds anything — `run` would
refuse on a moved HEAD, and `run --stale-ok` says "measure the old tree", which is not
what reading a file means."""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

from rops.ropslib import hosts
from rops.ropslib.transport import sq



def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("remote_path", help="absolute, or relative to ROPS_REMOTE_DIR")
    p.add_argument("dest", nargs="?", default=None, help="local destination (default: cwd)")
    p.add_argument("--yes", action="store_true", help="overwrite an existing destination")
    p.add_argument("--wait", action="store_true",
                   help="REMOTE_PATH is a run's log: wait until its rc line lands (or its "
                        "process group is gone), print the tail, exit with that rc")
    p.add_argument("--timeout", type=int, default=3600,
                   help="seconds --wait waits, or --print/--grep reads (default 3600)")
    p.add_argument("--lane", default=None, metavar="NAME",
                   help="REMOTE_PATH is relative to lane NAME's dir (~/rops/NAME); the lane is "
                        "only named, never checked, synced or built")
    read = p.add_mutually_exclusive_group()
    read.add_argument("--print", action="store_true",
                      help="print REMOTE_PATH to stdout instead of copying it here")
    read.add_argument("--grep", default=None, metavar="PAT",
                      help="print REMOTE_PATH's lines matching the extended regex PAT, with "
                           "their line numbers; exit 1 when none matches")


def run(args: argparse.Namespace) -> int:
    cfg = args.cfg
    if getattr(args, "lane", None):
        cfg = args.cfg = cfg.with_lane(args.lane, "--lane")
    host = hosts.gate(args, need_dir=False)[0]
    remote = args.remote_path
    if not remote.startswith(("/", "~")):
        remote = f"{cfg.get('ROPS_REMOTE_DIR', '~')}/{remote}"
    if getattr(args, "wait", False):
        return wait_for_rc(cfg, host, remote, timeout_s=args.timeout)
    if getattr(args, "print", False) or getattr(args, "grep", None) is not None:
        return read_in_place(host, remote, args.grep, timeout_s=args.timeout)
    dest = pathlib.Path(args.dest) if args.dest else pathlib.Path.cwd() / pathlib.PurePosixPath(remote).name

    if dest.exists() and not args.yes:
        if not sys.stdin.isatty():
            print(f"!! {dest} exists — pass --yes to overwrite", file=sys.stderr)
            return 2
        if input(f"{dest} exists. overwrite? [y/N] ").strip().lower() not in ("y", "yes"):
            return 2

    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["rsync", "-az", "--stats", f"{host.name}:{remote}", str(dest)],
                       text=True, capture_output=True)
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
        return r.returncode
    print(f"fetched {host.name}:{remote} -> {dest}")
    return 0


def shell_path(path: str) -> str:
    """`path` quoted for the remote shell, with a leading `~/` left bare so it still
    expands: a lane's dir is `~/rops/<name>`, and a quoted `~` is a literal one."""
    if path == "~" or path.startswith("~/"):
        return "~/" + sq(path[2:]) if len(path) > 2 else "~"
    return sq(path)


def read_in_place(host, remote: str, pattern: "str | None", *, timeout_s: int = 3600):
    """Print `remote` (or its lines matching `pattern`, numbered) from where it lies.
    Exit 0 printed, 1 no line matched, 2 no such file or a read error, 124 the read
    ran past `timeout_s`. Bytes that are
    not UTF-8 print as U+FFFD (copy the file for its bytes). Returned as an Outcome
    naming what was read, so a file that is itself one JSON object stays the
    envelope's `output` and is never taken for the command's own `data`."""
    from hs_family.cli.contract import Outcome, state_of
    from rops.ropslib.exec import RC_TIMEOUT
    path = shell_path(remote)
    cmd = (f"test -f {path} || exit 3; "
           + (f"grep -n -a -E -e {sq(pattern)} -- {path}" if pattern is not None
              else f"cat -- {path}"))
    r = host.run(cmd, timeout=timeout_s, errors="replace")
    sys.stdout.write(r.stdout or "")
    if r.returncode == 3:
        print(f"!! {host.name}:{remote}: no such file", file=sys.stderr)
        rc = 2
    elif r.returncode == RC_TIMEOUT:
        print(f"!! {host.name}:{remote}: the read timed out after {timeout_s}s — what it "
              f"had printed is lost; raise --timeout, or --grep a narrower pattern",
              file=sys.stderr)
        rc = RC_TIMEOUT
    elif r.returncode == 1 and pattern is not None:
        print(f"{host.name}:{remote}: no line matches {pattern!r}", file=sys.stderr)
        rc = 1
    elif r.returncode:
        print((r.stderr or "").strip() or f"!! {host.name}:{remote}: read failed rc={r.returncode}",
              file=sys.stderr)
        rc = 2
    else:
        rc = 0
    return Outcome(exit_code=rc, state=state_of(rc),
                   data={"read": f"{host.name}:{remote}",
                         "mode": "grep" if pattern is not None else "print"})


def wait_for_rc(cfg, host, log: str, *, timeout_s: float, poll_s: float = 5.0) -> int:
    """A run that outlived its own timeout goes on detached and still writes its rc line:
    the same watch the tmux lane uses, from now on, then the tail and that rc."""
    import time
    from rops.ropslib import tmux
    from rops.ropslib.exec import report
    if not host.ok(f"test -f {log}", timeout=30):
        print(f"!! {host.name}:{log}: no such log", file=sys.stderr)
        return 2
    res = tmux.watch(host, log, timeout_s, int(cfg.get("ROPS_TAIL", "60")),
                     time.perf_counter(), poll_s=poll_s)
    return report(res)

