"""grants — the live grant table, one line per key. No value column."""
from __future__ import annotations

import argparse

from secret_store import grants as grants_mod, home as home_mod, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    pass


def run(args: argparse.Namespace) -> int:
    home = home_mod.ensure_home(home_mod.secrets_home())
    live = grants_mod.fold_grants(ledgers.read(home / "grants.jsonl"))
    if not live:
        print("no grants")
        return 0
    for (ref, project, agent, purpose) in sorted(live):
        rec = live[(ref, project, agent, purpose)]
        life = ("persist" if rec.get("persist") or not rec.get("expires_at")
                else "expires %s" % rec["expires_at"])
        print("%-6s %s  agent %s  %s  %s%s" % (
            rec.get("op", "?"), ref, agent[:16], project, life,
            "  purpose %s" % purpose if purpose else ""))
    return 0
