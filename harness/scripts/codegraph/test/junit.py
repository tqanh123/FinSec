"""Junit XML -> per-test records, run joining, trend, and the JSONL dump.

Port of ob-tools' `testkit.durations.parse` (pinned commit `60075cb`). Shared
by `durations.py` (its own report), `buckets.py` (its bucket split reads the
same joined averages) and `backlog.py` (`flaky_canary` reads the same
per-test times). Kept as one module rather than copied into each: durations'
own "a blind record is a floor, never zero" rule has to mean the same thing
everywhere it is read.

WHAT IT CANNOT SEE
    * Records without a `time` attribute are a blind floor, not zero — a
      test that never ran (deselected, local-only) is simply absent.
    * Outcome is only `failure`/`error` (first child found) + its message,
      for `redlist`; a skip reads as no outcome. Durations ignore it.
    * Non-pytest junit (jest & friends) is best-effort: `classname`/`name`/
      `time` are honoured, anything else is invisible.
    * A duplicate test_id within one run (a parametrize collision) is kept
      separate and warned about, never merged.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Sequence, cast

from test_result_readers import TestResultError, parse_xml

#: Blind floor marker: a record whose junit carried no `time`.
NO_TIME = None


@dataclass(frozen=True)
class TestRecord:
    """One testcase from one junit run. `time_s` None = blind, not zero;
    `outcome` None = neither failure nor error."""

    test_id: str
    classname: "str | None"
    name: str
    time_s: "float | None"
    run_id: str
    outcome: "str | None" = None
    message: str = ""


@dataclass(frozen=True)
class ParseReport:
    records: "list[TestRecord]" = field(default_factory=list)
    warnings: "list[str]" = field(default_factory=list)


def parse_junit(path: "Path | str", run_id: "str | None" = None) -> ParseReport:
    """Parse one junit XML file into TestRecords (deterministic order)."""
    path = Path(path)
    run_id = run_id or path.stem
    warnings: "list[str]" = []
    try:
        root = parse_xml(path)
    except TestResultError as e:
        raise ValueError(f"{path}: not parseable XML: {e}") from e

    records: "list[TestRecord]" = []
    seen: "dict[str, int]" = {}
    for tc in root.iter("testcase"):
        name = tc.get("name")
        if not name:
            warnings.append(f"{path.name}: testcase without name skipped")
            continue
        classname = tc.get("classname")
        raw = tc.get("time")
        time_s: "float | None"
        try:
            time_s = NO_TIME if raw is None else float(raw)
        except ValueError:
            time_s = NO_TIME
            warnings.append(f"{path.name}: non-numeric time {raw!r} on "
                            f"{classname}::{name} -> blind")
        test_id = f"{classname}::{name}" if classname else f"::{name}"
        seen[test_id] = seen.get(test_id, 0) + 1
        outcome, message = None, ""
        for kind in ("failure", "error"):
            el = tc.find(kind)
            if el is not None:
                outcome, message = kind, el.get("message") or (el.text or "")
                break
        records.append(TestRecord(test_id, classname, name, time_s, run_id, outcome, message))
    for test_id, n in seen.items():
        if n > 1:
            warnings.append(f"{path.name}: test_id {test_id} appears {n}x "
                            f"(parametrize collision?) — kept separate, not merged")
    records.sort(key=lambda r: r.test_id)
    return ParseReport(records, warnings)


# ── run joining ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HistoryBreak:
    """An id present in one run and absent in the other — history CUT, never
    silently re-linked (a rename is a break, not a match)."""

    test_id: str
    in_run: str
    missing_from_run: str


def join_runs(runs: "Sequence[Sequence[TestRecord]]",
              ) -> "tuple[dict[str, float], dict[str, int], list[HistoryBreak], int]":
    """Average per test_id across runs.

    Returns (avg_seconds, observations_per_id, breaks, blind_ids) where
    blind_ids counts ids that NEVER carried a time in any run.
    """
    per_id: "dict[str, list[float | None]]" = {}
    run_of: "dict[str, set[str]]" = {}
    for run in runs:
        rid = run[0].run_id if run else "?"
        for rec in run:
            per_id.setdefault(rec.test_id, []).append(rec.time_s)
            run_of.setdefault(rec.test_id, set()).add(rid)
    all_runs = {r[0].run_id for r in runs if r}
    breaks = [
        HistoryBreak(tid, sorted(ids)[0], sorted(all_runs - ids)[0])
        for tid, ids in run_of.items() if ids != all_runs
    ]
    avg: "dict[str, float]" = {}
    obs: "dict[str, int]" = {}
    blind = 0
    for tid, times in per_id.items():
        known = [t for t in times if t is not NO_TIME]
        obs[tid] = len(times)
        if not known:
            blind += 1          # never zero-filled: blind floor, reported
            continue
        avg[tid] = sum(known) / len(known)
    breaks.sort(key=lambda b: b.test_id)
    return avg, obs, breaks, blind


# ── reporting ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Trend:
    test_id: str
    first_s: float
    last_s: float
    factor: float          # last/first; >1 = getting slower


def trend(avg_by_run: "Sequence[dict[str, float]]", min_runs: int = 3,
          ) -> "list[Trend]":
    """Slowdown candidates: ids with an observation in every one of >=min_runs
    consecutive runs. Sorted by factor desc — top of the list is the canary."""
    out: "list[Trend]" = []
    if len(avg_by_run) < min_runs:
        return out
    common: "set[str] | None" = None
    for run in avg_by_run:
        ids = set(run)
        common = ids if common is None else (common & ids)
    for tid in sorted(common or ()):
        series = [run[tid] for run in avg_by_run]
        first, last = series[0], series[-1]
        if first <= 0:
            continue
        out.append(Trend(tid, first, last, last / first))
    out.sort(key=lambda t: (-t.factor, t.test_id))
    return out


def report(runs: "Sequence[ParseReport]", top: int = 10) -> dict:
    """Aggregate report dict (text rendering lives in render_report)."""
    avg, obs, breaks, blind = join_runs([p.records for p in runs])
    per_run_avg = [{r.test_id: r.time_s for r in p.records if r.time_s is not NO_TIME}
                   for p in runs]
    ranked = sorted(avg.items(), key=lambda kv: (-kv[1], kv[0]))
    delta = None
    if len(per_run_avg) >= 2:
        a, b = per_run_avg[-2], per_run_avg[-1]
        delta = sorted(
            ({"test_id": tid, "from_s": a[tid], "to_s": b[tid],
              "diff_s": b[tid] - a[tid]} for tid in a.keys() & b.keys()),
            key=lambda d: -abs(cast(float, d["diff_s"])))[:top]
    return {
        "runs": [p.records[0].run_id if p.records else "?" for p in runs],
        "total_tests": len(obs),
        "timed_tests": len(avg),
        "blind_tests": blind,            # never had a time — floor, not zero
        "total_seconds": round(sum(avg.values()), 6),
        "top_expensive": [{"test_id": t, "avg_s": round(s, 6)}
                          for t, s in ranked[:top]],
        "delta_last_two": delta,
        "history_breaks": [
            {"test_id": b.test_id, "in": b.in_run, "missing_from": b.missing_from_run}
            for b in breaks],
        "trend_slowdown": [
            {"test_id": t.test_id, "first_s": round(t.first_s, 6),
             "last_s": round(t.last_s, 6), "factor": round(t.factor, 3)}
            for t in trend(per_run_avg)],
        "parse_warnings": [w for p in runs for w in p.warnings],
    }


def render_report(data: dict, out: "IO[str] | None" = None) -> None:
    out = out or sys.stdout            # bind at CALL time (capsys-friendly)
    print(f"durations across {len(data['runs'])} run(s): {', '.join(data['runs'])}",
          file=out)
    print(f"  tests seen: {data['total_tests']}  timed: {data['timed_tests']}  "
          f"BLIND (never timed): {data['blind_tests']}", file=out)
    print(f"  total (avg-sum): {data['total_seconds']:.3f}s", file=out)
    if data["top_expensive"]:
        print("  top expensive (avg per run):", file=out)
        for row in data["top_expensive"]:
            print(f"    {row['avg_s']:>10.3f}s  {row['test_id']}", file=out)
    if data["delta_last_two"]:
        print("  delta last two runs (top by |diff|):", file=out)
        for d in data["delta_last_two"]:
            sign = "+" if d["diff_s"] >= 0 else ""
            print(f"    {sign}{d['diff_s']:>9.3f}s  {d['test_id']} "
                  f"({d['from_s']:.3f} -> {d['to_s']:.3f})", file=out)
    if data["trend_slowdown"]:
        print("  slowdown trend (>=3 runs, factor desc):", file=out)
        for t in data["trend_slowdown"][:10]:
            print(f"    x{t['factor']:>6.2f}  {t['test_id']} "
                  f"({t['first_s']:.3f}s -> {t['last_s']:.3f}s)", file=out)
    if data["history_breaks"]:
        print(f"  HISTORY CUT (rename = break, never re-linked): "
              f"{len(data['history_breaks'])}", file=out)
        for b in data["history_breaks"][:10]:
            print(f"    {b['test_id']}  in {b['in']}, missing from {b['missing_from']}",
                  file=out)
    for w in data["parse_warnings"][:10]:
        print(f"  warn: {w}", file=out)


def dump_jsonl(runs: "Sequence[ParseReport]", out: "IO[str]") -> int:
    """Write durations.jsonl lines: one per test_id per run (append-friendly)."""
    n = 0
    for p in runs:
        for r in p.records:
            out.write(json.dumps({
                "test_id": r.test_id, "classname": r.classname, "name": r.name,
                "time_s": r.time_s, "run_id": r.run_id},
                ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
    return n
