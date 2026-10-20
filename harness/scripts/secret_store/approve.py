"""The pending-request queue a denied resolve leaves behind.

A request line in grants.jsonl is an ASK, not authority: it never enters the
live table (the fold skips it). It stops being pending the moment its own
four-field key holds a live allow or block — answered, either way.
"""
from __future__ import annotations

from pathlib import Path

from secret_store import grants as grants_mod, ledgers


def pending(home: Path) -> list:
    """Every request whose (ref, project, agent) holds no live grant — latest
    ask per key. Purpose is deliberately NOT matched: an allow granted
    without one covers every purpose, which is how an operator answers a
    specific ask with one general grant."""
    records = ledgers.read(home / "grants.jsonl")
    live = grants_mod.fold_grants(records)
    held = {(k[0], k[1], k[2]) for k in live}
    out: dict = {}
    for rec in records:
        if rec.get("op") != "request":
            continue
        key = (rec.get("ref"), rec.get("project"), rec.get("agent"))
        if key in held:
            continue  # already answered by an allow or a block
        out[key] = rec
    return [out[k] for k in sorted(out)]
