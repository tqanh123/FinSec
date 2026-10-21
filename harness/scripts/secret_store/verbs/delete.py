"""delete — remove a secret's value and record the event. Not reversible: the
ledger keeps the delete line forever; only the value is gone."""
from __future__ import annotations

import argparse

from secret_store import api, backends, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret's name")


def run(args: argparse.Namespace) -> int:
    try:
        ref = api.check_ref(args.ref)
        home, backend, live = api.open_store(args.backend_choice)
        if ref not in live:
            print("!! no secret named %s" % ref)
            return 2
        backend.delete(ref)
        ledgers.append(home / "index.jsonl", {"op": "delete", "ref": ref})
    except (api.Refusal, backends.BackendError) as e:
        print("!! %s" % e)
        return getattr(e, "code", 2)
    print("deleted %s" % ref)
    return 0
