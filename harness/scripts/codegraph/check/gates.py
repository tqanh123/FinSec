"""hs-code check — run the gate book.

`hs-code check gates [--page P]... [--only NAME]...` runs every enabled row of the
gate registry (`hs_family.gate_registry`) and accounts for each in exactly one bucket:

  pass / fail / broken   the page's runner ran it
  runtime                a hook enforces it when its event fires (`run: runtime`)
  judged                 a review rule with no machine detector, judged at review
  off                    switched off, with the layer's reason
  unrunnable             its page declares no `run:`

Exit: 2 when a row is broken or unrunnable, 1 when one failed, else 0. A runner is
`fn(target, rows)`; one that returns no result for a row it was given leaves that row
broken. A runner that reads a snapshot also takes `pin=` (`codegraph.check.tree.Pin`,
set by `--snap REF [--out BASE]`; unpinned, it measures the tree) — a project's own
runner without that parameter still runs.

The book is the target repo's own when it carries one, else the harness's.
"""
from __future__ import annotations

import argparse
import json
import importlib
import inspect
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

import attrs
import hs_family
from hs_family import gate_registry

from codegraph._core.git import GitError, source_files
from codegraph.check import tree
from hs_family.cli import Command

_ROOT = Path(__file__).resolve().parents[4]  # harness/scripts/codegraph/check/ -> repo
_RAN = ("pass", "fail", "broken")


@attrs.define(frozen=True, kw_only=True)
class RowResult:
    name: str
    page: str
    status: str
    detail: str = ""


def run_result(row, status: str, detail: str = "") -> RowResult:
    return RowResult(name=row.name, page=row.page, status=status, detail=detail)


# ------------------------------------------------------------------ runners

def _parse_junit(path: Path):
    """The junit report's root, parsed with defusedxml like every XML test result."""
    from defusedxml.ElementTree import parse
    return parse(str(path)).getroot()


def run_tests(root: Path, rows) -> List[RowResult]:
    """One pytest over every row's `file:`; a row passes when its file's tests do.
    A repo whose python runner is not pytest is refused, never handed a pytest run."""
    root = Path(root)
    import test_runners
    test_runners.require(root, "python", "pytest", tool="the test gate (check gates)")
    by_file = {}
    out = []
    for r in rows:
        f = r.fields.get("file")
        if not f or not (root / f).is_file():
            out.append(run_result(r, "broken", "no test file %s" % f))
        else:
            by_file[str(f)] = r
    if not by_file:
        return out
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "junit_family=xunit1", "--junitxml=%s" % xml, *sorted(by_file)],
            cwd=str(root), capture_output=True, text=True)
        cases = _parse_junit(xml).iter("testcase") if xml.is_file() else iter(())
        seen: Dict[str, List[str]] = {f: [] for f in by_file}
        for case in cases:
            f = case.get("file") or ""
            if f not in seen:
                continue
            for kind in ("error", "failure"):
                el = case.find(kind)
                if el is not None:
                    msg = el.get("message") or ""
                    seen[f].append("broken" if "collection failure" in msg else "fail")
                    break
            else:
                seen[f].append("pass")
    for f, r in by_file.items():
        marks = seen[f]
        if not marks:
            tail = (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines() or [""])[-1]
            out.append(run_result(r, "broken", "pytest ran no test (rc %d): %s"
                                  % (proc.returncode, tail[:160])))
        elif "broken" in marks:
            out.append(run_result(r, "broken", "could not collect %s" % f))
        elif "fail" in marks:
            out.append(run_result(r, "fail", "%d of %d failed" % (marks.count("fail"), len(marks))))
        else:
            out.append(run_result(r, "pass"))
    return out


def run_review(root: Path, rows) -> List[RowResult]:
    """A rule with a machine detector runs it over the repo's tracked files in its
    scope; a rule without one is judged by the reviewer at code review."""
    import mechanical_runner
    root = Path(root)
    detected = [r for r in rows if isinstance(r.fields.get("detector"), dict)]
    out = [run_result(r, "judged", "judged at code review") for r in rows if r not in detected]
    if not detected:
        return out
    # `source_files(tracked_only=True)`: the repo's TRACKED files only (this
    # docstring's own "over the repo's tracked files" scope) -- `-z`
    # (NUL-separated) under the hood, so a non-ASCII path never comes back
    # C-quoted under git's default `core.quotePath=true` (a detector could
    # not resolve a quoted string back to the file it names -- a silent
    # miss, not a refusal). Reads through the ONE door every tracked-only
    # caller shares rather than a private `ls-files -z` call of its own.
    try:
        files = source_files(root, tracked_only=True)
    except GitError:
        files = None
    if files is None:
        return out + [run_result(r, "broken", "git ls-files failed in %s" % root) for r in detected]
    for r in detected:
        found = mechanical_runner.run([r.as_rule()], files, str(root))
        if any(f.get("skipped") for f in found):
            out.append(run_result(r, "broken", found[0]["match"]))
        elif r.fields["detector"].get("type") == "shell":
            bad = [f for f in found if f.get("rc") != 0]
            out.append(run_result(r, "fail", bad[0]["match"]) if bad else run_result(r, "pass"))
        elif found:
            out.append(run_result(r, "fail", "%d hit(s), first %s:%s"
                                  % (len(found), found[0]["file"], found[0]["line"])))
        else:
            out.append(run_result(r, "pass"))
    return out


def run_code(root: Path, rows, pin=None) -> List[RowResult]:
    """A row is a rule the repo declares in `codemap.yaml`; the rows run together
    over one snapshot (fresh unless `pin` names one), judged exactly as `hs-code graph
    rules` judges them. A `severity: warn` rule and a shape that only reports pass, with what they
    saw in the detail."""
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph._core.rules import RulesError
    from codegraph.check.tree import CheckError, snapshot_idx
    from codegraph.graph.rules import judge
    root = Path(root)
    try:
        descriptor = load(root / "codemap.yaml", root).descriptor
    except DescriptorError as e:
        return [run_result(r, "broken", str(e).splitlines()[0]) for r in rows]
    declared = {d["id"]: d for d in descriptor.rules}
    out = [run_result(r, "broken", "not declared in codemap.yaml rules:")
           for r in rows if r.name not in declared]
    rules = [declared[r.name] for r in rows if r.name in declared]
    if not rules:
        return out
    try:
        idx = snapshot_idx(root, descriptor, pin, "check")
        results, cannot = judge(root, descriptor, rules, idx)
    except (CheckError, RulesError) as e:
        return out + [run_result(r, "broken", str(e)) for r in rows if r.name in declared]
    by_row = {r.name: r for r in rows}
    for rule, res in zip(rules, results):
        row = by_row[rule["id"]]
        if rule["id"] in cannot:
            out.append(run_result(row, "broken", cannot[rule["id"]]))
        elif res.is_red:
            v = res.violations[0]
            out.append(run_result(row, "fail", "%d violation(s), first %s  %s"
                                  % (len(res.violations), v.cause, v.label)))
        else:
            seen = ["%d warned" % len(res.violations)] if res.violations else []
            out.append(run_result(row, "pass", "; ".join(seen + list(res.notes[:1]))))
    return out


# ------------------------------------------------------------------- the verb

def _book_root(target: Path) -> Path:
    return target if (target / gate_registry.PAGES_DIR).is_dir() else _ROOT


def _offs(book, target: Path) -> Dict[str, str]:
    """name -> why, for every gate not fully on: `hs-cli gates`'s own answer
    (`gate_registry.gates_off`), for the data home of `target`, not of the session."""
    import harness_paths
    home = harness_paths.data_root_for(target)
    offs = gate_registry.gates_off(book, _ROOT,
                                   None if harness_paths.data_root_unresolved(home) else home)
    out: Dict[str, str] = {}
    for g in offs:
        if g["state"] == "off":  # an advisory gate still runs
            out.setdefault(g["name"], g["reason"] if g["layer"] == "shipped" and not g["who"]
                           else "%s (%s%s)" % (g["reason"], g["layer"],
                                               ", %s, %s" % g["who"] if g["who"] else ""))
    return out


def _pack_sys_path(book_root: Path, rows) -> None:
    """A pack-owned page's `run: {module: ...}` names a module living under
    `<book_root>/packs/<pack>/`, never on `sys.path` by default (unlike
    `harness/scripts`, which the shell's own bootstrap already adds) — so its
    `importlib.import_module` would fail with the pack's own module unresolved.
    `book_root` is where the page FILE (and so the pack directory) actually
    lives — `_book_root(target)`, not `target` itself: under a global install
    checking an external project with no gate-registry.d of its own, the book
    (and every pack it names) comes from the shared bin, not the checked tree.
    Every row `_run_page` was given for one page shares one file, hence one
    pack (or none, for a core page), so any row's `.pack` names the directory
    to add."""
    for pack in sorted({r.pack for r in rows if r.pack}):
        d = str(Path(book_root) / gate_registry.PACKS_DIR / pack)
        if d not in sys.path:
            sys.path.insert(0, d)


def _run_page(page: str, rows, spec, target: Path, pin, book_root: Path) -> List[RowResult]:
    if spec is None:
        return [run_result(r, "unrunnable", "no runner: page %s declares no run:" % page)
                for r in rows]
    if spec == gate_registry.RUNTIME:
        return [run_result(r, "runtime", "enforced when its event fires") for r in rows]
    _pack_sys_path(book_root, rows)
    fn = getattr(importlib.import_module(str(spec["module"])), str(spec["entry"]))
    kw = {"pin": pin} if "pin" in inspect.signature(fn).parameters else {}
    got = {res.name: res for res in fn(target, rows, **kw)}
    return [got.get(r.name) or run_result(r, "broken", "runner %s.%s returned no result"
                                          % (spec["module"], spec["entry"])) for r in rows]


def _gates_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--page", action="append", default=[], help="run only this page (repeatable)")
    ap.add_argument("--only", action="append", default=[], help="run only this gate (repeatable)")
    tree.add_pin_args(ap)


def _gates(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    target = Path(root)
    try:
        pin = tree.pin_of(args, opts)
    except tree.CheckError as e:
        raise hs_family.UsageError(str(e)) from e
    book_root = _book_root(target)
    try:
        book = gate_registry.load(book_root, target=target)
        offs = _offs(book, target)
    except hs_family.registry.RegistryError as e:
        raise hs_family.UsageError(str(e)) from e
    rows = [r for r in book.rows if (not args.page or r.page in args.page)
            and (not args.only or r.name in args.only)]
    unknown = sorted(set(args.only) - {r.name for r in book.rows})
    if unknown:
        raise hs_family.UsageError("no gate named %s in the registry" % ", ".join(unknown))
    results: List[RowResult] = [run_result(r, "off", "reason: %s" % offs[r.name])
                                for r in rows if r.name in offs]
    for page in sorted({r.page for r in rows}):
        live = [r for r in rows if r.page == page and r.name not in offs]
        if live:
            results += _run_page(page, live, book.runs.get(page), target, pin, book_root)
    results.sort(key=lambda x: (x.page, x.name))
    n = {s: sum(1 for x in results if x.status == s)
         for s in (*_RAN, "runtime", "judged", "off", "unrunnable")}
    ran = sum(n[s] for s in _RAN)
    rc = (int(hs_family.EXIT_BROKEN) if n["broken"] or n["unrunnable"]
          else int(hs_family.EXIT_FINDINGS) if n["fail"] else int(hs_family.EXIT_DONE))
    if getattr(opts, "json", False):
        print(json.dumps({"schema": "hs-code.check.gates/1",
                          "results": [{"status": x.status, "page": x.page, "name": x.name, "detail": x.detail}
                                      for x in results],
                          "totals": {"total": len(results), "ran": ran, **n}}, sort_keys=True))
        return rc
    for res in results:
        print(("%s %s %s %s" % (res.status, res.page, res.name, res.detail)).rstrip())
    line = "total %d = ran %d + runtime %d + judged %d + off %d" % (
        len(results), ran, n["runtime"], n["judged"], n["off"])
    if n["unrunnable"]:
        line += " + unrunnable %d" % n["unrunnable"]
    print("%s (pass %d, fail %d, broken %d)" % (line, n["pass"], n["fail"], n["broken"]))
    return rc


GATES = Command(add_args=_gates_args, run=_gates)
