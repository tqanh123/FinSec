"""The two-way baseline every "known findings" check compares against.

    key     (tool, file repo-relative, code|rule, message normalised, scope) -> count.
            No line number: an edit above a finding moves it, never renews it.
    new     a key the baseline never held, or one more occurrence of one it does
    stale   a key the baseline holds more of than the tool still reports
    freeze  writes the FIRST file; refuses over one
    shrink  takes min(found, baseline) per key: never adds, never raises a count
    major   the tool's major version sits in the file's HEADER, never in the key. A key
            carrying it turns every old finding new on upgrade, and the rebuild that
            follows would launder what was really new; a header that disagrees refuses,
            naming the rebuild, and a rebuild returns the keys it lets in.

`compare`/`shrink`/`count` are pure over `Counter`; the JSON file (machine-written, tracked
in the project, `actor` + `ts` on every write) is `freeze`/`load`/`rebuild`. A client with a
file format of its own (f821's YAML) uses the pure half only."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

SCHEMA = "hs-code.baseline/1"


class Key(NamedTuple):
    tool: str
    file: str
    code: str
    message: str
    scope: str = ""


class BaselineError(Exception):
    """A baseline file this engine will not read or write."""


class BaselineMajorMismatch(BaselineError):
    """The file was written under another major version of the tool."""


def normalize_message(message: str, roots: Iterable[str] = ()) -> str:
    """`message` with every absolute `roots` prefix cut, longest first."""
    for r in sorted({str(x).rstrip("/") for x in roots if x}, key=len, reverse=True):
        message = message.replace(r + "/", "")
    return message


def count_keys(rows: Iterable[dict], tool: str, roots: Iterable[str] = ()) -> "List[Key]":
    """Each finding row's key (`file`, `code`, `message`, optional `scope`; any `line`
    ignored), in row order."""
    roots = list(roots)
    return [Key(tool, str(r["file"]), str(r.get("code") or ""),
                normalize_message(str(r.get("message") or ""), roots), str(r.get("scope") or ""))
            for r in rows]


def count(rows: Iterable[dict], tool: str, roots: Iterable[str] = ()) -> "Counter[Key]":
    return Counter(count_keys(rows, tool, roots))


def compare(found: "Counter[Key]", base: "Counter[Key]") -> Dict[str, List[dict]]:
    new: List[dict] = []
    stale: List[dict] = []
    for k in sorted(set(found) | set(base)):
        row = {"key": k, "found": found[k], "baseline": base[k]}
        if found[k] > base[k]:
            new.append(row)
        elif base[k] > found[k]:
            stale.append(row)
    return {"new": new, "stale": stale}


def shrink(found: "Counter[Key]", base: "Counter[Key]") -> "Counter[Key]":
    return Counter({k: min(v, found[k]) for k, v in base.items() if found[k] > 0})


def _actor() -> str:
    import harness_checks
    return harness_checks.actor()


def _write(path: Path, tool: str, major: int, counts: "Counter[Key]", extra: Optional[dict] = None) -> None:
    doc = {"schema": SCHEMA, "tool": tool, "tool_major": int(major), "actor": _actor(),
           "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **(extra or {}),
           "findings": [{**k._asdict(), "count": n} for k, n in sorted(counts.items()) if n > 0]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def freeze(path: Path, tool: str, major: int, counts: "Counter[Key]") -> None:
    path = Path(path)
    if path.exists():
        raise BaselineError("%s exists -- freeze only writes the FIRST baseline" % path)
    _write(path, tool, major, counts)


def load(path: Path) -> Tuple[int, "Counter[Key]"]:
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        rows = doc["findings"]
        return int(doc["tool_major"]), Counter({Key(r["tool"], r["file"], r["code"], r["message"],
                                                    r.get("scope") or ""): int(r["count"]) for r in rows})
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise BaselineError("baseline %s unreadable: %s" % (path, e)) from e


def load_for(path: Path, tool: str, major: int, *, rebuild: str) -> "Counter[Key]":
    """The file's counts, refused when its header's major is not `major`."""
    was, counts = load(path)
    if was != int(major):
        raise BaselineMajorMismatch(
            "%s was written under %s major %d, this run is major %d: its messages may all have changed. "
            "Rebuild it, approved: %s" % (path, tool, was, int(major), rebuild))
    return counts


def shrink_file(path: Path, tool: str, major: int, found: "Counter[Key]") -> "Counter[Key]":
    """Rewrite the file as `shrink(found, its counts)`; returns what it now holds."""
    counts = shrink(found, load_for(path, tool, major, rebuild="(a major change needs --rebuild-baseline)"))
    _write(Path(path), tool, major, counts)
    return counts


def rebuild(path: Path, tool: str, major: int, found: "Counter[Key]", *, approved_by: str) -> Dict[str, List[Key]]:
    """Rewrite under `major` from `found`; returns the keys it lets in and drops."""
    if not approved_by:
        raise BaselineError("a rebuild needs --approved-by (the decision that allows it)")
    _was, old = load(path)
    _write(Path(path), tool, major, found, {"approved_by": approved_by})
    return {"added": sorted(k for k in found if found[k] > old[k]),
            "removed": sorted(k for k in old if old[k] > found[k])}
