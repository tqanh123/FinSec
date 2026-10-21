"""lock — the machine's pools: who holds seats, who waits and why, who runs outside the
queue and why. `cancel` drops a waiting ticket (its run exits 75, nothing run);
`resize` changes what a waiting ticket asks for (less keeps its place, more goes to the
back of the line)."""
from __future__ import annotations

import argparse

from hs_family import UsageError
from rops.ropslib import hosts
from rops.ropslib.exec import _POOL_FN, _POOL_SRC
from rops.ropslib.transport import sq



def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=("status", "cancel", "resize"), nargs="?", default="status")
    p.add_argument("ticket", nargs="?", help="cancel/resize: the ticket id `status` prints")
    p.add_argument("n", nargs="?", type=int, help="resize: the seats the ticket asks for now")
    p.add_argument("--json", action="store_true", help="status: the ledger as JSON")


def script(action: str, ticket: "str | None" = None, n: "int | None" = None,
           as_json: bool = False) -> str:
    argv = {"status": ["status"] + (["--json"] if as_json else []),
            "cancel": ["cancel", str(ticket)],
            "resize": ["resize", str(ticket), str(n)]}[action]
    return (f"__ROPS_POOL={sq(_POOL_SRC)}\n{_POOL_FN}\n"
            f'__rops_pool {argv[0]} --dir "${{TMPDIR:-/tmp}}/rops-locks-$(id -u)" '
            + " ".join(sq(a) for a in argv[1:]))


def run(args: argparse.Namespace) -> int:
    if args.action in ("cancel", "resize") and not args.ticket:
        raise UsageError(f"rops lock {args.action}: needs the ticket id `hs-rops lock status` prints")
    if args.action == "resize" and (args.n is None or args.n < 1):
        raise UsageError("rops lock resize: needs the seats to ask for, a positive integer")
    host = hosts.gate(args, need_dir=False)[0]
    r = host.run(f"bash -c {sq(script(args.action, args.ticket, args.n, args.json))}", timeout=60)
    out = ((r.stdout or "") + (r.stderr or "")).rstrip()
    if out:
        print(out)
    return r.returncode
