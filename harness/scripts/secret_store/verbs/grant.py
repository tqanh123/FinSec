"""grant — let one agent fingerprint read one ref, or block it.

The grant keys on the four fields (ref, project scope, fingerprint, purpose);
an allow carries an expiry (24h default) unless --persist, and a block beats
any allow. An agent never grants itself: when the fingerprint being granted
is the one running above this process, the answer is the family's deliberate
refusal (exit 3), not a grant.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from secret_store import api, fingerprint as fp, grants as grants_mod, home as home_mod, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret the grant opens (or closes)")
    p.add_argument("--agent", required=True, metavar="sha256:HEX",
                   help="the agent binary's fingerprint")
    p.add_argument("--project", default=None, metavar="DIR",
                   help="restrict the grant to one project root (default: the user scope)")
    p.add_argument("--purpose", default=None, help="why — recorded with the key")
    p.add_argument("--ttl", type=float, default=24, metavar="HOURS",
                   help="hours until an allow expires (default 24)")
    p.add_argument("--persist", action="store_true", help="no expiry")
    p.add_argument("--block", action="store_true", help="block instead of allow")
    p.add_argument("--allow-host", default=None, metavar="HOST",
                   help="only usable when resolving for this destination host")


def run(args: argparse.Namespace) -> int:
    try:
        ref = api.check_ref(args.ref)
        agent = grants_mod.parse_agent(args.agent)
        if agent is None:
            print("!! --agent must be sha256:<64 hex chars>")
            return 2
        above = fp.agent_above()
        if above and above[1] == agent:
            print("!! an agent above this process may not grant itself — ask the "
                  "operator to run this grant")
            return 3
        scope = grants_mod.scope_of(str(Path(args.project).resolve()) if args.project else None)
        rec = {"op": "block" if args.block else "allow", "ref": ref, "agent": agent,
               "project": scope}
        if args.purpose:
            rec["purpose"] = args.purpose
        if args.allow_host:
            rec["allow_host"] = args.allow_host
        if not args.block and not args.persist:
            rec["expires_at"] = (datetime.now(timezone.utc)
                                 + timedelta(hours=args.ttl)).isoformat(timespec="seconds")
        if args.persist:
            rec["persist"] = True
        home = home_mod.ensure_home(home_mod.secrets_home())
        ledgers.append(home / "grants.jsonl", rec)
    except api.Refusal as e:
        print("!! %s" % e)
        return e.code
    print("%s %s for %s%s" % (rec["op"], ref, agent[:16],
                              " (persist)" if args.persist else ""))
    return 0
