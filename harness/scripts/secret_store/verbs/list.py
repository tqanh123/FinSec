"""list — every live ref and when it last changed. No value column, by
construction: this is the inventory, not the vault door."""
from __future__ import annotations

import argparse

from secret_store import api


def add_args(p: argparse.ArgumentParser) -> None:
    pass


def run(args: argparse.Namespace) -> int:
    _home, _backend, live = api.open_store(args.backend_choice)
    if not live:
        print("no secrets")
        return 0
    for ref in sorted(live):
        print("%s  updated %s" % (ref, live[ref].get("ts", "?")))
    return 0
