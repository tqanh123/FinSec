"""approve — answer the asks a denied resolve left behind.

`--json` lists the pending requests (the machine-readable view the operator
scripts); without it, each pending ask is offered for an interactive
y/N, and a yes writes the allow line the agent was asking for.
"""
from __future__ import annotations

import argparse
import json
import sys

from secret_store import approve as approve_mod, grants as grants_mod
from secret_store import home as home_mod, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="list pending requests as one JSON array")


def run(args: argparse.Namespace) -> int:
    home = home_mod.ensure_home(home_mod.secrets_home())
    queue = approve_mod.pending(home)
    if args.as_json:
        print(json.dumps(queue, ensure_ascii=False, sort_keys=True))
        return 0
    if not queue:
        print("no pending requests")
        return 0
    answered = 0
    for rec in queue:
        line = "grant %s to agent %s (%s)%s? [y/N] " % (
            rec.get("ref"), str(rec.get("agent"))[:16], rec.get("project", "user"),
            " purpose %s" % rec["purpose"] if rec.get("purpose") else "")
        if not sys.stdin.isatty():
            print(line.rstrip() + " — needs a tty (or --json to list)")
            continue
        answer = input(line).strip().lower()
        if answer in ("y", "yes"):
            grant = {"op": "allow", "ref": rec.get("ref"), "agent": rec.get("agent"),
                     "project": rec.get("project", "user")}
            if rec.get("purpose"):
                grant["purpose"] = rec["purpose"]
            ledgers.append(home / "grants.jsonl", grant)
            answered += 1
    if answered:
        print("granted %d request(s)" % answered)
    return 0
