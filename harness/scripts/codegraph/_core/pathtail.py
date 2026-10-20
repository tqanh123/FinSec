"""The one path-tail matcher: a caller-written tail names a snapshot path on whole segments.

`store.py` names `x/store.py`, never `findings_store.py`. A plain `endswith` answers for
both, and every reader of a path tail (`graph file`, the port assignment rules) inherited
that until they shared this function.
"""
from __future__ import annotations


def tail_matches(path: str, tail: str) -> bool:
    """`path` is `tail`, or ends in `/` + `tail`. A leading `./` or `/` on `tail` is the
    segment boundary spelled out, so it is dropped rather than doubled."""
    tail = tail[2:] if tail.startswith("./") else tail.lstrip("/")
    return bool(tail) and (path == tail or path.endswith("/" + tail))
