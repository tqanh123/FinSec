"""The grant table: which agent fingerprint may read which ref, where.

A grant line carries the four key fields — ref, project scope, agent
fingerprint, purpose — plus its own shape (allow or block), an optional
expiry, and an optional allow-host bound. The ledger only appends; the live
table is `fold_grants` over the lines, where a later line for the same key
wins and a REVOKE is a wildcard (its absent fields match anything) that
removes every key its present fields name.

`decide` answers one question — may THIS agent read THIS ref in THIS project
— as ("allow" | "block" | "deny", reason): a block beats however recent an
allow is, an expired allow answers like no grant at all, a user-scoped grant
matches every project while a project-scoped one matches only its own root,
and an allow-host bound only answers for the host it names.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

KEY = ("ref", "project", "agent", "purpose")


def fold_grants(records: list) -> dict:
    """The live grant table from ledger lines, in line order."""
    live: dict = {}
    for rec in records:
        op = rec.get("op")
        if op == "request":
            continue  # a waiting ask is queue, not authority
        if op == "revoke":
            for key in [k for k in live if _matches(k, rec)]:
                del live[key]
            continue
        key = tuple(rec.get(f) for f in KEY)
        held = live.get(key)
        if held is not None and held.get("op") == "block" and op == "allow":
            # a block is not un-done by a newer allow on the same key —
            # revoke it first, on purpose, in front of whoever is watching
            continue
        live[key] = rec
    return live


def _matches(key: tuple, rec: dict) -> bool:
    """A revoke line matches every key its present fields name."""
    for field, value in zip(KEY, key):
        named = rec.get(field)
        if named is not None and named != value:
            return False
    return True


def _expired(rec: dict, now: datetime) -> bool:
    until = rec.get("expires_at")
    if not until:
        return False
    try:
        ends = datetime.fromisoformat(str(until))
    except ValueError:
        return True  # an unreadable expiry is not an open door
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return ends <= now


def _host_ok(rec: dict, dest_host: "str | None") -> bool:
    bound = rec.get("allow_host")
    if not bound or bound == "*":
        return True
    return bool(dest_host) and dest_host == bound


def decide(live: dict, *, ref: str, agent: str,
           project: "str | None", dest_host: "str | None" = None,
           now: "datetime | None" = None) -> Tuple[str, str]:
    """(verdict, reason) for one resolve question."""
    now = now or datetime.now(timezone.utc)
    usable = []
    for (gref, gproj, gagent, _gpurpose), rec in live.items():
        if gref not in (ref, "*"):
            continue
        if gagent != agent:
            continue
        if gproj != "user":
            want = "project:%s" % project if project else None
            if gproj != want:
                continue
        if _expired(rec, now):
            continue
        if rec.get("op") == "allow" and not _host_ok(rec, dest_host):
            continue
        usable.append(((gref, gproj, gagent, _gpurpose), rec))
    if any(rec.get("op") == "block" for _k, rec in usable):
        return "block", "a block grant matches this agent and ref"
    for key, rec in usable:
        if rec.get("op") == "allow":
            return "allow", "granted by %s" % (rec.get("actor") or "a grant line")
    return "deny", "no grant matches agent %s for %s" % (agent[:12], ref)


def scope_of(project: "str | None") -> str:
    """The project field of a grant line: `user`, or `project:<resolved root>`."""
    if not project:
        return "user"
    return "project:%s" % project


def parse_agent(value: str) -> "Optional[str]":
    """Accept `sha256:<hex>` or bare `<hex>`; None when not a fingerprint."""
    hex_part = value[7:] if value.startswith("sha256:") else value
    if len(hex_part) == 64 and all(c in "0123456789abcdef" for c in hex_part):
        return hex_part
    return None
