"""trust — pin (or re-pin) an agent binary's identity.

`trust <path>` records the path WITH its sha256; a later run over a changed
binary is NAMED as drifted until `--repin` admits the upgrade on purpose.
Bare `trust` lists the pins. Pins are display-and-detection aids: what a
grant keys on is the hash, never the path.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from secret_store import fingerprint as fp
from secret_store import home as home_mod, ledgers
from secret_store.api import Refusal

TRUSTED = "trusted.jsonl"


def _pins(home: Path) -> dict:
    path = home / TRUSTED
    if not path.is_file():
        return {}
    out: dict = {}
    for rec in ledgers.read(path):
        if rec.get("path"):
            out[rec["path"]] = rec
    return out


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("agent", nargs="?", default=None,
                   help="path to an agent binary to pin (default: list the pins)")
    p.add_argument("--repin", action="store_true",
                   help="accept a changed binary at an already-pinned path")


def run(args: argparse.Namespace) -> int:
    home = home_mod.ensure_home(home_mod.secrets_home())
    try:
        if args.agent is None:
            pins = _pins(home)
            if not pins:
                print("no pins yet — trust <path-to-agent-binary> to add one")
            for path, rec in sorted(pins.items()):
                print("%s  sha256 %s  (pinned %s)" % (path, rec.get("sha256", "?")[:16],
                                                      rec.get("ts", "?")))
            above = fp.agent_above()
            if above:
                print("agent above this process: %s (sha256 %s)"
                      % (above[0], above[1][:16]))
            else:
                print("no agent-looking binary above this process")
            return 0
        exe = Path(args.agent).resolve()
        if not exe.is_file():
            print("!! no such binary: %s" % exe)
            return 2
        sha = fp.fingerprint_of(exe)
        if sha is None:
            print("!! cannot read %s to hash it" % exe)
            return 2
        current = _pins(home).get(str(exe))
        if current is None:
            ledgers.append(home / TRUSTED, {"op": "pin", "path": str(exe), "sha256": sha})
            print("pinned %s  sha256 %s" % (exe, sha[:16]))
            return 0
        if current.get("sha256") == sha:
            print("already pinned at sha256 %s" % sha[:16])
            return 0
        if not args.repin:
            print("!! pinned hash changed: %s was %s, the binary is now %s — "
                  "pass --repin to accept the upgrade"
                  % (exe, str(current.get("sha256"))[:16], sha[:16]))
            return 0
        ledgers.append(home / TRUSTED, {"op": "repin", "path": str(exe), "sha256": sha})
        print("repinned %s  sha256 %s" % (exe, sha[:16]))
        return 0
    except Refusal as e:
        print("!! %s" % e)
        return e.code
