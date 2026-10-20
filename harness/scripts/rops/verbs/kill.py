"""kill — take down a remote process group (behavior 5): `kill <PGID>` for a number,
`kill <pattern>` for anything else (the group of every `pgrep -f` match that is not
the kill's own)."""
from __future__ import annotations

import argparse

from hs_family import UsageError
from rops.ropslib import hosts
from rops.ropslib.exec import kill_tree
from rops.ropslib.prog import PROG



def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("pattern", nargs="?", metavar="PGID|PATTERN",
                   help="all digits: that process group; anything else: a `pgrep -f` pattern, "
                        "every match outside this kill's own group is taken down with its "
                        "group. Omit with --stray")
    p.add_argument("--stray", action="store_true",
                   help="reap orphaned CPU-burning shells (background-job residue)")


def run(args: argparse.Namespace) -> int:
    host = hosts.gate(args, need_dir=False)[0]
    if args.stray:
        from rops.ropslib import quiet
        pids = quiet.stray_pids(args.cfg, host)
        if not pids:
            print("no orphaned CPU-burning processes")
            return 0
        host.run("kill -9 " + " ".join(pids))
        print(f"killed {len(pids)} orphaned process(es)")
        return 0
    if not args.pattern:
        raise UsageError(f"{PROG} kill: needs <PGID|pattern> or --stray")
    rc, out = kill_tree(host, args.pattern)
    print(out)
    return rc
