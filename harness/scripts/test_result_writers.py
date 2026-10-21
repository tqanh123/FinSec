"""What the harness writes from a tool it ran itself, for the DoD gate's readers
(`test_result_readers.read_junit` / `read_sarif`): a test run's verdict, read from the
runner's own JSON result (vitest and jest both write the jest shape), and its JUnit; a
static check's findings as SARIF (lint) or JUnit (typecheck).

The exit code is not the verdict: a failing test and a config that never loaded both
exit 1 (measured), so the result FILE decides -- its absence is the caller's BROKEN.
"""
from __future__ import annotations

import os
from typing import Any
from xml.sax.saxutils import escape, quoteattr

_KEYS = ("numTotalTests", "numFailedTests", "success", "testResults")
_SKIPPED = ("pending", "skipped", "todo", "disabled")


def jest_json_verdict(doc: Any) -> "tuple[str, str]":
    """(PASS|FAIL|EMPTY, detail). A run that wrote a snapshot did not compare against one."""
    if not isinstance(doc, dict) or any(k not in doc for k in _KEYS):
        raise ValueError("not a jest-shaped result: needs %s" % ", ".join(_KEYS))
    total, failed = int(doc["numTotalTests"] or 0), int(doc["numFailedTests"] or 0)
    # a skipped or todo test is counted in the total (measured): it did not run
    skipped = int(doc.get("numPendingTests") or 0) + int(doc.get("numTodoTests") or 0)
    ran = total - skipped
    added = int(((doc.get("snapshot") or {}).get("added")) or 0)
    if ran <= 0 and not failed:
        return "EMPTY", ("the run held no test" if not total else
                         "the run held %d test(s), every one skipped" % total)
    if added:
        return "FAIL", "the run wrote %d new snapshot(s) instead of comparing against one" % added
    tail = ", %d skipped" % skipped if skipped else ""
    if failed or not doc["success"]:
        return "FAIL", "%d test(s) ran, %d failed%s%s" % (
            ran, failed, tail, "" if failed else " (the runner reports the run unsuccessful)")
    return "PASS", "%d test(s) ran, 0 failed%s" % (ran, tail)


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root) if root and os.path.isabs(path) else path
    except ValueError:
        return path


def jest_json_to_junit(doc: dict, name: str, *, root: str = "") -> str:
    """One <testsuite> per test file, one <testcase> per assertion; file paths relative
    to `root`. Raises on a run of no test: a 0-test JUnit reads as a clean pass."""
    if not int(doc.get("numTotalTests") or 0):
        raise ValueError("%s: the run held no test -- no JUnit is written for it" % name)
    suites = []
    for res in doc.get("testResults") or []:
        f = _rel(str(res.get("name") or ""), root)
        cases, n_fail, n_skip = [], 0, 0
        for a in res.get("assertionResults") or []:
            title = str(a.get("fullName") or a.get("title") or "")
            secs = float(a.get("duration") or 0) / 1000.0
            body = ""
            if a.get("status") == "failed":
                n_fail += 1
                msg = "\n".join(str(m) for m in a.get("failureMessages") or [])
                body = "<failure message=%s>%s</failure>" % (quoteattr(msg[:200]), escape(msg))
            elif a.get("status") in _SKIPPED:
                n_skip += 1
                body = "<skipped/>"
            cases.append('    <testcase classname=%s name=%s file=%s time="%.3f">%s</testcase>'
                         % (quoteattr(f), quoteattr(title), quoteattr(f), secs, body))
        suites.append('  <testsuite name=%s tests="%d" failures="%d" errors="0" skipped="%d">\n%s\n'
                      '  </testsuite>' % (quoteattr(f), len(cases), n_fail, n_skip, "\n".join(cases)))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<testsuites name=%s>\n%s\n</testsuites>\n'
            % (quoteattr(name), "\n".join(suites)))


# ---------------------------------------------------------------- static checks

#: a finding's `level`: `error` fails the check, `note` is one the baseline holds,
#: `warning` is one an advisory check reports
_LEVELS = ("error", "warning", "note")


def findings_to_sarif(tool: str, rows: "list[dict]") -> str:
    """SARIF 2.1.0 of a static check's findings (`file`, `line`, `code`, `message`,
    `level`); `test_result_readers.sarif_verdict` fails it on an `error` alone."""
    import json
    results = []
    for r in rows:
        if r["level"] not in _LEVELS:
            raise ValueError("finding level %r is none of %s" % (r["level"], ", ".join(_LEVELS)))
        res: dict = {"ruleId": r["code"], "level": r["level"], "message": {"text": r["message"]}}
        if r.get("file"):
            res["locations"] = [{"physicalLocation": {"artifactLocation": {"uri": r["file"]},
                                                      "region": {"startLine": max(int(r.get("line") or 1), 1)}}}]
        results.append(res)
    doc = {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
           "runs": [{"tool": {"driver": {"name": tool}}, "results": results}]}
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def findings_to_junit(name: str, rows: "list[dict]", summary: str) -> str:
    """One case per finding (an `error` fails) and one verdict case that fails when any
    does: a clean check is one passing case, never a 0-test file."""
    cases, failed = [], 0
    for r in rows:
        where = "%s:%d" % (r.get("file") or "(no file)", int(r.get("line") or 0))
        title = "%s %s %s" % (where, r["code"], r["message"])
        body = "<system-out>%s</system-out>" % escape(r["level"])
        if r["level"] == "error":
            failed += 1
            body = "<failure message=%s>%s</failure>" % (quoteattr(r["message"][:200]), escape(title))
        cases.append("    <testcase classname=%s name=%s file=%s>%s</testcase>"
                     % (quoteattr(name), quoteattr(title), quoteattr(r.get("file") or ""), body))
    verdict = "<failure message=%s/>" % quoteattr(summary) if failed else ""
    cases.append("    <testcase classname=%s name=%s>%s</testcase>"
                 % (quoteattr(name), quoteattr("%s: %s" % (name, summary)), verdict))
    total, n_fail = len(cases), failed + (1 if failed else 0)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<testsuites name=%s>\n'
            '  <testsuite name=%s tests="%d" failures="%d" errors="0" skipped="0">\n%s\n  </testsuite>\n'
            '</testsuites>\n' % (quoteattr(name), quoteattr(name), total, n_fail, "\n".join(cases)))


def coverage_to_cobertura(found: int, hit: int) -> str:
    """One whole-report `line-rate` (`read_cobertura`): what a change class's line floor
    reads. Lines only -- the one metric every runner measures the same way."""
    rate = (hit / found) if found else 0.0
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<coverage line-rate="%.6f" lines-valid="%d" lines-covered="%d" version="hs-code">\n'
            '  <packages/>\n</coverage>\n' % (rate, found, hit))


def coverage_group_to_junit(name: str, group: str, found: int, hit: int,
                            floor: "float | None", failure: str = "") -> str:
    """One group's coverage as one test case: it fails when `failure` says why (below the
    group's declared floor)."""
    pct = 100.0 * hit / found if found else 0.0
    title = "%s: %.1f%% of %d line(s)%s" % (group, pct, found,
                                             "" if floor is None else " (floor %s)" % floor)
    body = "<failure message=%s/>" % quoteattr(failure) if failure else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<testsuites name=%s>\n'
            '  <testsuite name=%s tests="1" failures="%d" errors="0" skipped="0">\n'
            '    <testcase classname=%s name=%s>%s</testcase>\n  </testsuite>\n</testsuites>\n'
            % (quoteattr(name), quoteattr(name), 1 if failure else 0, quoteattr(name),
               quoteattr(title), body))
