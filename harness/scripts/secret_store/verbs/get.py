"""get — a secret's metadata with the value redacted. The store never prints a
value from the default path: what a caller can see without a grant decision is
THAT the ref exists and when it last changed. `--reveal` is the HUMAN door —
it refuses (exit 3) whenever an agent binary sits above this process, because
an agent reads through grants (`resolve`), never through the operator's own
command."""
from __future__ import annotations

import argparse

from secret_store import api, backends, fingerprint as fp

REDACTED = "[redacted]"


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret's name")
    p.add_argument("--reveal", action="store_true",
                   help="print the value; refused while an agent runs above this process")


def run(args: argparse.Namespace) -> int:
    try:
        ref = api.check_ref(args.ref)
        home, backend, live = api.open_store(args.backend_choice)
        rec = live.get(ref)
        if rec is None:
            print("!! no secret named %s" % ref)
            return 2
        if getattr(args, "reveal", False):
            if fp.agent_above() is not None:
                print("!! an agent runs above this process — agents read through "
                      "grants (resolve), not through --reveal")
                return 3
            value = backend.get(ref)
            if value is None:
                print("!! no value stored for %s" % ref)
                return 2
            print(value)
            return 0
        stamp = rec.get("ts", "?")
        extra = "  rotated %s" % rec["ts"] if rec.get("op") == "rotate" else ""
        print("%s  updated %s%s  value %s" % (ref, stamp, extra, REDACTED))
    except (api.Refusal, backends.BackendError) as e:
        print("!! %s" % e)
        return getattr(e, "code", 2)
    return 0
