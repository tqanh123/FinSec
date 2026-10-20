#!/usr/bin/env python3
"""codemap_adoption — does this repo have a code map, and should we offer one now?

The one place that decides it; `hs-code init`, the hs-run facts engine and
`graph init` all ask here instead of re-deriving it.

    adopted      `<root>/codemap.yaml` exists
    unsupported  no file carries a suffix any bin adapter reads
    unattended   the runtime says nobody is at the terminal
                 (`CLAUDE_CODE_SESSION_ATTENDED` set and not "1"; absent = attended)
    declined     this session already said no
    offer        none of the above — ask

A decline is remembered per SESSION (`CLAUDE_CODE_SESSION_ID`/`CLAUDE_SESSION_ID`,
env only: the newest-transcript fallback picks the wrong file when two sessions
share a repo). With no session id a decline holds for `DECLINE_TTL`.

The consent ledger is `<data home of root>/codemap/consent.jsonl`, append-only,
one attributed row per decision, filtered by the absolute `root` so a global
install serving many repos never mixes them up.

Stdlib + yaml_io only: the facts engine must not import codegraph.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional

import harness_paths
import yaml_io

STATES = ("adopted", "offer", "declined", "unattended", "unsupported")
DECISIONS = ("declined", "accepted")
DECLINE_TTL = timedelta(hours=8)
#: The one sentence every "no code map" refusal ends with.
REMEDY = "create it with /hs:codemap, or `hs-code init run --apply` from the repo root"

_SESSION_ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
_ADAPTER_BOOK = Path(__file__).resolve().parents[1] / "data" / "hs-code-adapters.yaml"
# Dirs that hold someone else's code (or none), never the repo's language.
_SKIP_DIRS = frozenset({"node_modules", "venv", "__pycache__", "site-packages",
                        "build", "dist", "target", "vendor"})
_WALK_CAP = 50000


@dataclass(frozen=True)
class Adoption:
    state: str
    reason: str
    languages: tuple = ()
    supported: tuple = ()
    session: str = ""

    def as_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason,
                "languages": list(self.languages), "supported": list(self.supported),
                "session": self.session}


def supported_extensions() -> dict:
    """suffix -> adapter id, read from the bin adapter book (the one source)."""
    book = yaml_io.safe_load(_ADAPTER_BOOK.read_text(encoding="utf-8")) or {}
    out = {}
    for row in book.get("adapters") or []:
        for ext in row.get("extensions") or []:
            out[str(ext)] = str(row.get("id"))
    return out


def consent_path(root) -> Path:
    return harness_paths.data_root_for(root) / "codemap" / "consent.jsonl"


def _session(env: Mapping) -> str:
    for k in _SESSION_ENV_KEYS:
        v = (env.get(k) or "").strip()
        if v:
            return v
    return ""


def _suffixes(root: Path, wanted: frozenset) -> set:
    """Suffixes seen under `root`, stopping at the first one in `wanted`.

    A walk, never a subprocess: hs-run asks this on every `next` of a repo with no
    code map, and a steady-state poll must not pay a process spawn. Only a repo
    with nothing readable walks to the end (capped)."""
    out, seen = set(), 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS]
        for name in filenames:
            if "." in name[1:]:
                ext = "." + name.rsplit(".", 1)[-1].lower()
                out.add(ext)
                if ext in wanted:
                    return out
        seen += len(filenames)
        if seen >= _WALK_CAP:
            break
    return out


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _declined(root: Path, session: str, now: datetime) -> bool:
    try:
        lines = consent_path(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    key = str(root)
    last = None
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("root") != key:
            continue
        if (row.get("session") or "") != session:
            continue
        last = row
    if last is None or last.get("decision") != "declined":
        return False
    if session:
        return True
    try:
        ts = datetime.fromisoformat(str(last.get("ts")))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts < DECLINE_TTL


def status(root, env: Optional[Mapping] = None, now: Optional[datetime] = None) -> Adoption:
    env = os.environ if env is None else env
    root = Path(root).resolve()
    session = _session(env)
    if (root / "codemap.yaml").is_file():
        return Adoption("adopted", "codemap.yaml at %s" % root, session=session)
    book = supported_extensions()
    found = _suffixes(root, frozenset(book))
    langs = tuple(sorted(found))
    supported = tuple(sorted(book))
    if not found & set(book):
        return Adoption("unsupported",
                        "no file with a suffix the code map can read (%s)" % ", ".join(supported),
                        langs, supported, session)
    attended = env.get("CLAUDE_CODE_SESSION_ATTENDED")
    if attended is not None and attended.strip() != "1":
        return Adoption("unattended", "CLAUDE_CODE_SESSION_ATTENDED=%s: nobody to ask" % attended,
                        langs, supported, session)
    if _declined(root, session, _now(now)):
        return Adoption("declined", "declined for session %s" % (session or "(no id, 8h)"),
                        langs, supported, session)
    return Adoption("offer", "no codemap.yaml at %s" % root, langs, supported, session)


def record(root, decision: str, env: Optional[Mapping] = None,
           now: Optional[datetime] = None) -> dict:
    """Append one attributed consent row; returns it as written."""
    if decision not in DECISIONS:
        raise ValueError("decision must be one of %s, got %r" % (DECISIONS, decision))
    env = os.environ if env is None else env
    root = Path(root).resolve()
    hooks = str(Path(__file__).resolve().parents[1] / "hooks")
    if hooks not in sys.path:
        sys.path.append(hooks)
    import hook_runtime
    session = _session(env)
    row = {"actor": hook_runtime.resolve_actor(session or None),
           "ts": _now(now).isoformat(), "root": str(root), "session": session,
           "decision": decision}
    path = consent_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row
