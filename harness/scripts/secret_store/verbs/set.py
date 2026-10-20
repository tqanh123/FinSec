"""set — store a secret's value. The value arrives by prompt or stdin, never
on argv (a value on argv is already in the shell's history and the process
table). One index line records THAT it was set — never WHAT it was."""
from __future__ import annotations

import argparse

from secret_store import api, backends, ledgers


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret's name, e.g. api.key")
    p.add_argument("--value", default=None, metavar="VAL|-",
                   help="the value, or '-' to read it from stdin; without the flag a "
                        "tty prompts (echo off)")


def run(args: argparse.Namespace) -> int:
    try:
        ref = api.check_ref(args.ref)
        value = api.read_value(args.value, ref)
        home, backend, live = api.open_store(args.backend_choice)
        if ref in live:
            print("!! %s already exists — rotate it to replace the value" % ref)
            return 2
        backend.put(ref, value)
        ledgers.append(home / "index.jsonl", {"op": "set", "ref": ref})
    except (api.Refusal, backends.BackendError) as e:
        print("!! %s" % e)
        return getattr(e, "code", 2)
    print("set %s" % ref)
    return 0
