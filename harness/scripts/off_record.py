"""Who, when and why: the fields every off of a gate carries, in one place.

Stdlib only, because a compliance hook imports it and a hook must never fail to load
over a missing dependency. The gate registry's project layer and the write guard's own
switch both ask `missing()`, so the two can never disagree about what a record is.
"""
from __future__ import annotations

RECORD_KEYS = ("reason", "actor", "ts")


def make(**values) -> dict:
    """One record, keyed by `RECORD_KEYS`: a key the caller does not supply raises,
    so a key added here cannot be written out missing."""
    return {k: values[k] for k in RECORD_KEYS}


def missing(entry) -> list:
    """The record fields `entry` leaves empty or out, in RECORD_KEYS order."""
    if not isinstance(entry, dict):
        return list(RECORD_KEYS)
    return [k for k in RECORD_KEYS if not str(entry.get(k) or "").strip()]
