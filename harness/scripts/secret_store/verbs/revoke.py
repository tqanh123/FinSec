"""revoke — remove grants. A revoke line's absent fields match anything, so
`revoke <ref> --agent <fp>` closes that agent's door on that ref whatever its
scope or purpose was; refusing to revoke nothing keeps a typo from reading
as success."""
from __future__ import annotations

import argparse

from secret_store import grants as grants_mod, home as home_mod, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret to close")
    p.add_argument("--agent", default=None, metavar="sha256:HEX",
                   help="the fingerprint to revoke (default with --all: every agent)")
    p.add_argument("--all", action="store_true",
                   help="revoke every agent on this ref")


def run(args: argparse.Namespace) -> int:
    if not args.all and not args.agent:
        print("!! pass --agent <sha256:HEX> or --all")
        return 2
    agent = grants_mod.parse_agent(args.agent) if args.agent else None
    if args.agent and agent is None:
        print("!! --agent must be sha256:<64 hex chars>")
        return 2
    home = home_mod.ensure_home(home_mod.secrets_home())
    path = home / "grants.jsonl"
    before = grants_mod.fold_grants(ledgers.read(path))
    rec = {"op": "revoke", "ref": args.ref}
    if agent:
        rec["agent"] = agent
    ledgers.append(path, rec)
    after = grants_mod.fold_grants(ledgers.read(path))
    gone = len(before) - len(after)
    if gone <= 0:
        print("!! no grant matched that revoke")
        return 2
    print("revoked %d grant(s) on %s" % (gone, args.ref))
    return 0
