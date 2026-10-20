"""The append-only JSONL ledgers — the store's memory of what happened.

Four files under the store home, one law for all of them: lines are only ever
APPENDED, never rewritten; what exists now is always a fold of the lines
(`fold_index`). Every line carries `actor` + `ts` — attribution, not
authentication, same as every other harness record.

  index.jsonl   set / rotate / delete events        -> the live refs (the fold)
  grants.jsonl  allow / block / revoke decisions    -> the grant table (the fold)
  access.jsonl  one line per value read (resolve)   -> history only, never folded
  trusted.jsonl agent-binary pins (path + sha256)   -> the pin table (last wins)

No line ever carries a secret VALUE. The value lives in the backend; the
ledger says that a ref was set, rotated, deleted — by whom, when.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hook_runtime import resolve_actor  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(path: Path, record: dict) -> dict:
    """One event as one line, stamped with actor+ts. Returns the line written."""
    line = dict(record)
    line.setdefault("actor", resolve_actor())
    line.setdefault("ts", _now())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return line


def read(path: Path) -> list:
    """Every parsed line of a ledger (an unreadable line stops the read — a
    truncated write must surface, not silently truncate the fold)."""
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln))
    return out


def fold_index(records: list) -> dict:
    """The live refs from index lines: the LAST line per ref wins, a delete
    removes it. Returns `{ref: that record}` — the caller reads its op/ts."""
    live: dict = {}
    for rec in records:
        op, ref = rec.get("op"), rec.get("ref")
        if op in ("set", "rotate"):
            live[ref] = rec
        elif op == "delete":
            live.pop(ref, None)
    return live
