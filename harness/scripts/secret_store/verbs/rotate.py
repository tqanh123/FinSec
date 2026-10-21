"""rotate — replace a live secret's value and stamp rotatedAt on the record.
The old value is gone the moment the backend takes the new one."""
from __future__ import annotations

import argparse

from secret_store import api, backends, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret's name")
    p.add_argument("--value", default=None, metavar="VAL|-",
                   help="the new value, or '-' to read it from stdin; without the "
                        "flag a tty prompts (echo off)")


def run(args: argparse.Namespace) -> int:
    try:
        ref = api.check_ref(args.ref)
        value = api.read_value(args.value, ref)
        home, backend, live = api.open_store(args.backend_choice)
        if ref not in live:
            print("!! no secret named %s — set it first" % ref)
            return 2
        backend.put(ref, value)
        ledgers.append(home / "index.jsonl", {"op": "rotate", "ref": ref,
                                              "rotatedAt": live[ref].get("ts")})
    except (api.Refusal, backends.BackendError) as e:
        print("!! %s" % e)
        return getattr(e, "code", 2)
    print("rotated %s" % ref)
    return 0
