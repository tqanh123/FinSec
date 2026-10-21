"""Static-check output read into finding rows: `{file, line, code, message, broken}`.

`file` is repo-relative ("" for a finding with no location), `broken` marks a row that
says the check never got to judge the code (a parse error, a config error). Exit codes
are not read: tsc's config error is rc 1 on 6.x and rc 2 on 7.x, and eslint exits 2 over a
clean JSON when a suppression went unused.

    tsc_lines            `tsc -b --pretty false`: `path(l,c): error TSnnnn: msg`, a
                         location-less `error TSnnnn: msg`, indented continuation lines
    eslint_json          `eslint -f json`: severity 2 only; `suppressedMessages` count too
                         (eslint hides them only while the file's count holds)
    eslint_suppressions  `eslint-suppressions.json` as (file, rule) -> count
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Dict, Iterable, List, Tuple

_TSC = re.compile(r"^(?:(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): )?error (?P<code>TS\d+): (?P<msg>.*)$")


class ParseError(ValueError):
    """Output the parser does not recognise: the check is broken, not clean."""


def _rel(path: str, cwd: str, root: str) -> str:
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    rel = os.path.relpath(os.path.normpath(full), root)
    return rel.replace(os.sep, "/")


def tsc_lines(text: str, *, cwd: str, root: str) -> List[dict]:
    rows: List[dict] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[:1].isspace() and rows:
            rows[-1]["message"] += " " + raw.strip()
            continue
        m = _TSC.match(raw)
        if m is None:
            raise ParseError("tsc printed a line that is no diagnostic: %r" % raw[:200])
        rows.append({"file": _rel(m["file"], cwd, root) if m["file"] else "",
                     "line": int(m["line"] or 0), "code": m["code"], "message": m["msg"], "broken": False})
    return rows


def split_broken(rows: Iterable[dict], broken_codes: Iterable[str]) -> Tuple[List[dict], List[dict]]:
    """(rows saying the check never judged the code, the findings)."""
    pats = tuple(broken_codes)
    broken: List[dict] = []
    found: List[dict] = []
    for r in rows:
        (broken if r.get("broken") or any(fnmatch.fnmatchcase(r["code"], p) for p in pats)
         else found).append(r)
    return broken, found


def eslint_json(text: str, *, cwd: str, root: str) -> List[dict]:
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ParseError("eslint printed no JSON (%s): %r" % (e, text[:200])) from None
    if not isinstance(doc, list):
        raise ParseError("eslint's JSON is not a list of file results")
    rows: List[dict] = []
    for f in doc:
        if not isinstance(f, dict) or "filePath" not in f:
            raise ParseError("eslint's JSON holds an entry with no filePath")
        rel = _rel(str(f["filePath"]), cwd, root)
        for m in list(f.get("messages") or []) + list(f.get("suppressedMessages") or []):
            if m.get("severity") != 2:
                continue
            fatal = bool(m.get("fatal")) or m.get("ruleId") is None
            rows.append({"file": rel, "line": int(m.get("line") or 0), "code": str(m.get("ruleId") or ""),
                         "message": str(m.get("message") or ""), "broken": fatal})
    return rows


def eslint_suppressions(text: str) -> Dict[Tuple[str, str], int]:
    try:
        doc = json.loads(text)
        return {(str(f), str(rule)): int(v["count"]) for f, rules in doc.items() for rule, v in rules.items()}
    except (ValueError, AttributeError, KeyError, TypeError) as e:
        raise ParseError("eslint-suppressions.json is not {file: {rule: {count}}}: %s" % e) from None


PARSERS = {"tsc_lines": tsc_lines, "eslint_json": eslint_json}
