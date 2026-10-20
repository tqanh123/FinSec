"""hs-code test backlog -- test-debt signals in one report: coverage gaps,
flaky canary, assertions, fixture graph, skips, birth-new coverage.

Port of ob-tools' `testkit.analysis.backlog` (pinned commit `60075cb`).

WHY A CATALOG AND NOT A SCORE
    Every signal here answers "where is the test suite lying to us". They are
    ranked, never summed: a suite with 40 coverage gaps and 0 flaky canaries
    and a suite with 0 gaps and 40 flaky canaries need different work, and one
    number would hide which one you have.

THE SIGNALS
    coverage_gap        units no test function touches
    flaky_canary        tests whose duration RANK moves hard between runs
    no_assert           test functions with no assertion anywhere in the body
    fixture_shared      fixtures used by more than one house (a move blocker)
    skip_expiry         skip/xfail with no expiry marker
    birth_new_uncovered symbols new between two snapshots that no test touches

    hs-code test backlog tests --junit a.xml --junit b.xml --junit c.xml
    hs-code test backlog tests --old-snap .snap-old --new-snap .snap-new

`coverage_gap`/`birth_new_uncovered` need a unit map: measured through the
SAME snapshot door every other `hs-code` verb reads (`codegraph.check.tree.
snapshot_idx`, unpinned = fresh HEAD) and the harness's own
`codegraph._core.testmap.build_test_map`. A tree with no `codemap.yaml` (or
whose snapshot cannot be measured) still runs every signal that needs only
the tests directory — `coverage_gap`/`birth_new_uncovered` come back
`measured=False`, never a guessed zero. This is the one deliberate departure
from upstream's own `--src-root/--pkg` flags (an ad-hoc `<src>/<pkg>` unit
scan): a target repo's units are whatever its OWN `codemap.yaml` already
declares, never a second, unrelated unit notion.

Exit: 0 always — this verb reports a catalog, it never gates (no bar to be
red or green against). A tree that cannot be read at all (bad `tests_dir`) is
exit 2.

WHAT IT CANNOT SEE
    * coverage_gap is STATIC touch, not execution: an upper bound.
    * no_assert sees `assert` statements and `pytest.raises` only.
    * flaky_canary needs >= 3 runs with times; fewer reports BLIND. It
      measures DURATION movement, not pass/fail movement.
    * skip_expiry reads a convention (a date or an issue id in the reason).
    * birth_new_uncovered needs two snapshots; with fewer it is blind.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hs_family

from codegraph.test.junit import parse_junit
from hs_family.cli import Command

#: Minimum runs before the flaky canary may speak.
MIN_RUNS_FOR_FLAKY = 3
#: A test whose mean rank moves by more than this many places between runs is
#: a canary. Rank, not seconds: a machine that got slower moves everything.
RANK_SWING = 5
#: A skip reason carrying a date or an issue/ticket id is treated as MARKED.
_MARKER_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|[A-Z]{2,}-\d+|#\d+)\b")
_SKIP_DECORATORS = {"skip", "skipif", "xfail"}


@dataclass
class Signal:
    """One backlog entry. `rows` are the findings; `blind` says what it misses."""

    name: str
    rows: "list[str]" = field(default_factory=list)
    blind: str = ""
    measured: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return {"signal": self.name, "count": len(self.rows),
                "rows": self.rows[:50], "blind": self.blind,
                "measured": self.measured, "note": self.note}


# ── coverage gap ─────────────────────────────────────────────────────────────

def coverage_gap(tmap: Any, all_units: "set[str]") -> Signal:
    """Units no test function touches."""
    touched: "set[str]" = set()
    for fn in tmap.all_funcs():
        touched |= set(fn.touch)
    gap = sorted(set(all_units) - touched)
    return Signal(
        "coverage_gap", gap,
        blind=("static touch only: getattr/dynamic dispatch, fixture-provided "
               "objects and parametrised indirection all read as untouched. "
               "This is an UPPER bound on the gap, not a coverage number."),
        note=f"{len(touched)}/{len(all_units)} unit(s) touched by >=1 test",
    )


# ── flaky canary ─────────────────────────────────────────────────────────────

def flaky_canary(junit_paths: "list[Path]") -> Signal:
    """Tests whose duration RANK swings hard between runs."""
    if len(junit_paths) < MIN_RUNS_FOR_FLAKY:
        return Signal(
            "flaky_canary", [], measured=False,
            blind=("duration rank movement needs >= "
                   f"{MIN_RUNS_FOR_FLAKY} junit runs (got {len(junit_paths)}); "
                   "this also only sees DURATION movement — a test flipping "
                   "red/green at constant speed is invisible."),
            note=f"BLIND: {len(junit_paths)} run(s), need {MIN_RUNS_FOR_FLAKY}")

    runs: "list[dict[str, float]]" = []
    for p in junit_paths:
        try:
            rep = parse_junit(p)
        except (OSError, ValueError) as e:
            return Signal("flaky_canary", [], measured=False,
                          blind=f"unreadable junit: {e}", note="BLIND")
        runs.append({r.test_id: r.time_s for r in rep.records if r.time_s is not None})

    ranks: "dict[str, list[int]]" = {}
    for run in runs:
        for pos, (tid, _) in enumerate(sorted(run.items(), key=lambda kv: -kv[1]), start=1):
            ranks.setdefault(tid, []).append(pos)

    rows = []
    for tid, rs in sorted(ranks.items()):
        if len(rs) < MIN_RUNS_FOR_FLAKY:
            continue          # not in every run: a history cut, not a canary
        if max(rs) - min(rs) > RANK_SWING:
            rows.append(f"{tid}  ranks {rs}  swing {max(rs) - min(rs)}")
    rows.sort(key=lambda r: -int(r.rsplit("swing ", 1)[1]))
    return Signal(
        "flaky_canary", rows,
        blind=(f"duration rank swing > {RANK_SWING} across {len(runs)} run(s). "
               "Sees DURATION only, and only for tests present in every run — "
               "a test that skipped one run is dropped, not flagged."),
        note=f"{len(runs)} run(s), {len(ranks)} test id(s) seen")


# ── no-assert census ─────────────────────────────────────────────────────────

def no_assert(tests_root: Path) -> Signal:
    """Test functions whose body contains no assertion at all."""
    rows: "list[str]" = []
    blind_notes = 0
    for path in sorted(tests_root.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            blind_notes += 1
            continue
        rel = path.relative_to(tests_root)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            has_assert = any(isinstance(n, ast.Assert) for n in ast.walk(node))
            has_raises = any(isinstance(n, ast.Attribute) and n.attr == "raises"
                             for n in ast.walk(node))
            if not (has_assert or has_raises):
                rows.append(f"{rel}::{node.name}  line {node.lineno}")
    return Signal(
        "no_assert", rows,
        blind=("sees `assert` statements and `pytest.raises` only. A test that "
               "checks a mock's call count, or that relies on a downstream "
               "exception, reads here as assertion-free and is NOT. "
               f"{blind_notes} unparseable file(s) were skipped."),
        note=f"{len(rows)} test function(s) with no visible assertion")


# ── fixture shared across houses ─────────────────────────────────────────────

def fixture_shared(tests_root: Path, houses: "dict[str, str] | None" = None) -> Signal:
    """Fixtures defined once and used from more than one house."""
    defs: "dict[str, str]" = {}
    uses: "dict[str, set[str]]" = {}
    for path in sorted(tests_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(tests_root))
        house = (houses or {}).get(rel, rel.split("/")[0] if "/" in rel else ".")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Attribute) and dec.attr == "fixture":
                        defs[node.name] = rel
                    elif isinstance(dec, ast.Name) and dec.id == "fixture":
                        defs[node.name] = rel
                for a in (x.arg for x in node.args.args + node.args.kwonlyargs):
                    if a == "self":
                        continue
                    uses.setdefault(a, set()).add(house)

    rows = []
    for name, hs in sorted(uses.items()):
        if name not in defs:
            continue
        real = {h for h in hs if h}
        if len(real) > 1:
            rows.append(f"{name}  defined {defs[name]}  used by {sorted(real)}")
    return Signal(
        "fixture_shared", rows,
        blind=("fixtures passed by name through `request.getfixturevalue`, "
               "built in conftest by string, or injected via plugin are not "
               "seen; a fixture used from a house whose files live in one "
               "directory reads as single-house."),
        note=f"{len(rows)} fixture(s) crossing a house boundary")


# ── skip expiry ──────────────────────────────────────────────────────────────

def skip_expiry(tests_root: Path) -> Signal:
    """`skip`/`xfail` whose reason carries no date and no issue id."""
    rows: "list[str]" = []
    marked = 0
    for path in sorted(tests_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(tests_root)
        for node in ast.walk(tree):
            decs = getattr(node, "decorator_list", None)
            if decs is None:
                continue
            for dec in decs:
                if isinstance(dec, (ast.Attribute, ast.Name)):
                    bare = dec.attr if isinstance(dec, ast.Attribute) else dec.id
                    if bare in _SKIP_DECORATORS:
                        rows.append(f"{rel}::{getattr(node, 'name', '?')}  "
                                    f"{bare} — no reason at all")
                    continue
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                name = fn.attr if isinstance(fn, ast.Attribute) else \
                    fn.id if isinstance(fn, ast.Name) else ""
                if name not in _SKIP_DECORATORS:
                    continue
                reason = ""
                for kw in dec.keywords:
                    if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                        reason = str(kw.value.value)
                if not reason:
                    rows.append(f"{rel}::{getattr(node, 'name', '?')}  "
                                f"{name}() — no reason at all")
                elif _MARKER_RE.search(reason):
                    marked += 1
                else:
                    rows.append(f"{rel}::{getattr(node, 'name', '?')}  "
                                f"{name}({reason!r}) — no date, no issue id")
    return Signal(
        "skip_expiry", rows,
        blind=("reads a CONVENTION: a date (2026-01-31) or an issue id "
               "(ABC-12, #42) in the reason. A skip with a prose reason is "
               "reported unmarked, which is not the same as overdue — and a "
               "marked one whose date has passed is not flagged here at all. "
               f"{marked} marked skip(s) were left alone."),
        note=f"{len(rows)} unmarked skip(s)/xfail(s), {marked} marked")


# ── birth-new coverage ───────────────────────────────────────────────────────

def birth_new_uncovered(old_index: "dict | None", new_index: "dict | None",
                        tmap: Any = None) -> Signal:
    """Symbols new between two snapshots that no test function touches."""
    if old_index is None or new_index is None:
        return Signal("birth_new_uncovered", [], measured=False,
                      blind="needs two snapshots (old and new); got fewer.",
                      note="BLIND: one or zero snapshot(s)")

    def names(idx: dict) -> "dict[str, str]":
        out: "dict[str, str]" = {}
        for unit, mods in (idx.get("units") or {}).items():
            for m in mods:
                for sy in m.get("symbols") or []:
                    out[f"{m['module']}::{sy['name']}"] = unit
        return out

    before, after = names(old_index), names(new_index)
    born = sorted(set(after) - set(before))
    if tmap is None:
        return Signal(
            "birth_new_uncovered", born, measured=False,
            blind=("no test map given: the born symbols are listed but nothing "
                   "is known about whether a test touches them."),
            note=f"BLIND on coverage: {len(born)} new symbol(s)")

    touched: "set[str]" = set()
    for fn in tmap.all_funcs():
        touched |= set(fn.touch)
    rows = [s for s in born if not any(s in t or t in s for t in touched)]
    return Signal(
        "birth_new_uncovered", rows,
        blind=("touch is resolved by name from the static map; a new symbol "
               "exercised only through a dynamic lookup reads as uncovered."),
        note=f"{len(rows)}/{len(born)} new symbol(s) untouched")


# ── assembly ─────────────────────────────────────────────────────────────────

def build_backlog(tests_root: Path, *, tmap: Any = None, all_units: "set[str] | None" = None,
                  junit_paths: "list[Path] | None" = None,
                  old_index: "dict | None" = None, new_index: "dict | None" = None,
                  houses: "dict[str, str] | None" = None,
                  coverage_blind: "str | None" = None) -> "list[Signal]":
    """Every signal that can be measured from what was handed in.

    Signals whose evidence is missing come back `measured=False` with the
    reason — never silently dropped, and never filled with a zero.
    """
    signals = [coverage_gap(tmap, all_units or set()) if tmap is not None else Signal(
        "coverage_gap", [], measured=False,
        blind=coverage_blind or "no snapshot could be measured for this tree.",
        note="BLIND: no test map")]
    signals.append(flaky_canary(list(junit_paths or [])))
    signals.append(no_assert(tests_root))
    signals.append(fixture_shared(tests_root, houses))
    signals.append(skip_expiry(tests_root))
    signals.append(birth_new_uncovered(old_index, new_index, tmap))
    return signals


def render(signals: "list[Signal]", top: int = 15) -> str:
    lines = ["test backlog — signals are RANKED, never summed "
             "(one number would hide which problem you have)", ""]
    for s in signals:
        if not s.measured:
            lines.append(f"  {s.name:22} BLIND — {s.blind}")
            continue
        lines.append(f"  {s.name:22} {len(s.rows):5}  {s.note}")
        for row in s.rows[:top]:
            lines.append(f"      {row}")
        if len(s.rows) > top:
            lines.append(f"      … {len(s.rows) - top} more")
    lines.append("")
    lines.append("  Every count above carries a blind — read it before acting. "
                 "A zero with a blind is not a clean zero.")
    return "\n".join(lines)


def _measure_tmap(root: Path) -> "tuple[Any, set[str], str | None]":
    """The test map + its unit universe, through the one snapshot door every
    other verb reads (`codegraph.check.tree.snapshot_idx`, unpinned = fresh
    HEAD) — or (None, set(), reason) when the tree cannot be measured that
    way (no `codemap.yaml`, not a git repo, ...)."""
    try:
        from codegraph._core.descriptor import DescriptorError, load
        from codegraph._core.testmap import build_test_map
        from codegraph.check.tree import CheckError, snapshot_idx
        descriptor = load(root / "codemap.yaml", root).descriptor
        idx = snapshot_idx(root, descriptor, None, "backlog")
    except (DescriptorError, CheckError, OSError) as e:
        return None, set(), str(e)
    return build_test_map(idx, root), set(idx.get("units", {})), None


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("tests_dir", help="tests root, repo-relative")
    sp.add_argument("--junit", action="append", default=[], type=Path,
                    help="junit XML per run (repeatable; >=3 for flaky canary)")
    sp.add_argument("--old-snap", type=Path, default=None,
                    help="a stored snapshot directory (the older one)")
    sp.add_argument("--new-snap", type=Path, default=None,
                    help="a stored snapshot directory (the newer one)")
    sp.add_argument("--top", type=int, default=15, help="rows per signal")


def _backlog(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    tests_root = root / args.tests_dir
    if not tests_root.is_dir():
        raise hs_family.UsageError(f"backlog: not a directory: {tests_root}")

    tmap, all_units, blind_reason = _measure_tmap(root)

    def _load_snap(p: "Path | None") -> "dict | None":
        if p is None:
            return None
        from codegraph._core.snapshot.store import SnapshotMissing, load_dir
        try:
            return load_dir(p)
        except SnapshotMissing as e:
            raise hs_family.UsageError(f"backlog: {e}") from e

    try:
        old_index = _load_snap(args.old_snap)
        new_index = _load_snap(args.new_snap)
    except (OSError, ValueError) as e:
        raise hs_family.UsageError(f"backlog: {e}") from e

    signals = build_backlog(
        tests_root, tmap=tmap, all_units=all_units, junit_paths=args.junit,
        old_index=old_index, new_index=new_index, coverage_blind=blind_reason)

    if getattr(opts, "json", False):
        print(json.dumps({"signals": [s.to_dict() for s in signals]},
                         ensure_ascii=False, sort_keys=True))
    else:
        print(render(signals, top=args.top))
    return 0


BACKLOG = Command(add_args=add_arguments, run=_backlog)
