"""hs-code test buckets -- split tests into N CI buckets balanced by junit
durations (readiness-gated: >= 3 stable runs).

Port of ob-tools' `testkit.analysis.buckets` (pinned commit `60075cb`).

    hs-code test buckets RUN1.xml RUN2.xml RUN3.xml --buckets 4
    hs-code test buckets RUN*.xml --strategy stratified --exclude '*live*'

`--out PATH` writes the plan as `buckets.json`, gated behind `--apply` like
every other write in this domain (a dry run still computes and prints the
full plan). A RED readiness never writes, `--apply` or not: a split over
oscillating data would reshuffle every run.

Exit: 0 readiness green, plan computed (written under --apply) · 1 readiness
RED (not enough stable runs, or a test oscillates beyond the amplitude limit)
· 2 a junit file could not be parsed, or an unknown --strategy was named.

WHAT IT CANNOT SEE
    * Tests with no timed observation anywhere are NOT bucketed — they land
      in `unbucketed_blind`. An empty bucket set is never padded with
      guesses.
    * Readiness only judges ids seen in EVERY run; an id missing from one run
      is a history break, not instability.
    * Excluded patterns (own-lane tests) are listed, not dropped.
    * Bucket sums use averaged durations; real wall-clock per bucket also
      pays collection + interpreter startup, which junit does not measure.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

import hs_family

from codegraph.test.junit import ParseReport, join_runs, parse_junit
from hs_family.cli import Command

#: Machine condition: bucket only on >= this many stable consecutive runs.
DEFAULT_MIN_RUNS = 3
#: Stability: per-test amplitude (max-min)/mean across runs must stay under 20%.
AMPLITUDE_LIMIT = 0.20


class BucketStrategy(Protocol):
    """Strategy seam: swap the split without touching callers."""

    name: str

    def assign(self, timed: "dict[str, float]", n_buckets: int,
               ) -> "dict[int, list[str]]": ...


class BalancedLPT:
    """Greedy longest-processing-time: deterministic, minimises makespan."""

    name = "balanced"

    def assign(self, timed: "dict[str, float]", n_buckets: int,
               ) -> "dict[int, list[str]]":
        sums = [0.0] * n_buckets
        buckets: "dict[int, list[str]]" = {i: [] for i in range(n_buckets)}
        for test_id, t in sorted(timed.items(), key=lambda kv: (-kv[1], kv[0])):
            i = min(range(n_buckets), key=lambda k: (sums[k], k))
            buckets[i].append(test_id)
            sums[i] += t
        return buckets


class StratifiedByHouse:
    """Spread each classname-house across buckets before balancing."""

    name = "stratified"

    def assign(self, timed: "dict[str, float]", n_buckets: int,
               ) -> "dict[int, list[str]]":
        buckets: "dict[int, list[str]]" = {i: [] for i in range(n_buckets)}
        sums = [0.0] * n_buckets
        houses: "dict[str, list[tuple[str, float]]]" = {}
        for test_id, t in timed.items():
            house = test_id.split("::", 1)[0]
            houses.setdefault(house, []).append((test_id, t))
        for house in sorted(houses):
            for test_id, t in sorted(houses[house], key=lambda kv: (-kv[1], kv[0])):
                j = min(range(n_buckets), key=lambda k: (sums[k], k))
                buckets[j].append(test_id)
                sums[j] += t
        for i in buckets:
            buckets[i].sort()
        return buckets


STRATEGIES: "dict[str, BucketStrategy]" = {
    BalancedLPT.name: BalancedLPT(),
    StratifiedByHouse.name: StratifiedByHouse(),
}


# ── readiness ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Readiness:
    ok: bool
    reasons: "list[str]" = field(default_factory=list)
    unstable_ids: "list[str]" = field(default_factory=list)


def bucket_readiness(runs: Sequence[Sequence], min_runs: int = DEFAULT_MIN_RUNS,
                     amplitude_limit: float = AMPLITUDE_LIMIT) -> Readiness:
    """Green only on >=min_runs consecutive runs with every common id stable.

    A test is unstable when (max-min)/mean across runs exceeds the limit.
    Red readiness is a REASON, never a silent worse split.
    """
    reasons: "list[str]" = []
    if len(runs) < min_runs:
        reasons.append(f"need >= {min_runs} runs, have {len(runs)}")
        return Readiness(False, reasons, [])
    per_run = [{r.test_id: r.time_s for r in run if r.time_s is not None}
               for run in runs]
    common: "set[str] | None" = None
    for run in per_run:
        ids = set(run)
        common = ids if common is None else common & ids
    unstable: "list[str]" = []
    for tid in sorted(common or ()):
        series = [run[tid] for run in per_run]
        mean = sum(series) / len(series)
        if mean <= 0:
            continue
        if (max(series) - min(series)) / mean > amplitude_limit:
            unstable.append(tid)
    if unstable:
        reasons.append(
            f"{len(unstable)} test(s) oscillate beyond "
            f"{amplitude_limit:.0%} amplitude — split would chase noise")
    return Readiness(not reasons, reasons, unstable)


# ── bucket assembly ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BucketPlan:
    strategy: str
    n_buckets: int
    buckets: "dict[int, list[str]]"
    bucket_seconds: "dict[int, float]"
    excluded: "list[str]"
    unbucketed_blind: "list[str]"
    history_breaks: "list[dict]"
    readiness: Readiness

    def spread_s(self) -> float:
        s = self.bucket_seconds
        return max(s.values()) - min(s.values()) if s else 0.0


def build_plan(runs: Sequence[ParseReport], n_buckets: int = 4,
               strategy: str = BalancedLPT.name,
               exclude: Sequence[str] = (),
               min_runs: int = DEFAULT_MIN_RUNS) -> BucketPlan:
    if n_buckets < 1:
        raise ValueError("n_buckets must be >= 1")
    try:
        strat = STRATEGIES[strategy]
    except KeyError:
        raise ValueError(
            f"unknown strategy {strategy!r}; known: {', '.join(sorted(STRATEGIES))}"
        ) from None
    readiness = bucket_readiness([p.records for p in runs], min_runs=min_runs)

    avg, _obs, breaks, _blind_count = join_runs([p.records for p in runs])
    excluded = sorted(tid for tid in avg
                      if any(fnmatch.fnmatch(tid, pat) for pat in exclude))
    timed = {tid: t for tid, t in avg.items() if tid not in excluded}
    all_times: "dict[str, list[float | None]]" = {}
    for p in runs:
        for r in p.records:
            all_times.setdefault(r.test_id, []).append(r.time_s)
    blind_ids = [tid for tid, ts in all_times.items()
                 if all(t is None for t in ts)]     # never timed in ANY run
    buckets = strat.assign(timed, n_buckets)
    seconds = {i: round(sum(avg[t] for t in ids), 6) for i, ids in buckets.items()}
    return BucketPlan(strategy=strat.name, n_buckets=n_buckets, buckets=buckets,
                      bucket_seconds=seconds, excluded=excluded,
                      unbucketed_blind=sorted(blind_ids),
                      history_breaks=[{"test_id": b.test_id, "in": b.in_run,
                                       "missing_from": b.missing_from_run}
                                      for b in breaks],
                      readiness=readiness)


def plan_json(plan: BucketPlan) -> dict:
    return {
        "strategy": plan.strategy, "buckets": plan.n_buckets,
        "readiness": {"ok": plan.readiness.ok, "reasons": plan.readiness.reasons,
                      "unstable_ids": plan.readiness.unstable_ids},
        "bucket_seconds": plan.bucket_seconds,
        "spread_s": round(plan.spread_s(), 6),
        "buckets_detail": {str(i): ids for i, ids in plan.buckets.items()},
        "excluded_own_lane": plan.excluded,
        "unbucketed_blind": plan.unbucketed_blind,
        "history_breaks": plan.history_breaks,
    }


def render_plan(plan: BucketPlan, out=None) -> None:
    out = out or sys.stdout
    r = plan.readiness
    print(f"bucket plan: {plan.strategy} x {plan.n_buckets} — readiness "
          f"{'GREEN' if r.ok else 'RED'}", file=out)
    for reason in r.reasons:
        print(f"  reason: {reason}", file=out)
    for i in sorted(plan.bucket_seconds):
        print(f"  bucket {i}: {plan.bucket_seconds[i]:>10.3f}s  "
              f"({len(plan.buckets[i])} tests)", file=out)
    print(f"  spread (max-min): {plan.spread_s():.3f}s", file=out)
    if plan.excluded:
        print(f"  excluded (own lane): {len(plan.excluded)}", file=out)
    if plan.unbucketed_blind:
        print(f"  BLIND never-timed, NOT bucketed: {len(plan.unbucketed_blind)}", file=out)
    if plan.history_breaks:
        print(f"  history cuts: {len(plan.history_breaks)}", file=out)


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("junit", nargs="+", help="junit XML file(s), one per run")
    sp.add_argument("--buckets", type=int, default=4, help="bucket count (default 4)")
    sp.add_argument("--strategy", choices=sorted(STRATEGIES), default="balanced")
    sp.add_argument("--exclude", action="append", default=[],
                    help="fnmatch pattern for own-lane tests (repeatable)")
    sp.add_argument("--min-runs", type=int, default=DEFAULT_MIN_RUNS,
                    help=f"readiness threshold (default {DEFAULT_MIN_RUNS})")
    sp.add_argument("--out", type=Path, default=None,
                    help="write buckets.json here (--apply writes it)")


def _buckets(args: argparse.Namespace) -> int:
    opts = args.opts
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    runs: "list[ParseReport]" = []
    for path in args.junit:
        try:
            runs.append(parse_junit(path))
        except (OSError, ValueError) as e:
            raise hs_family.UsageError(f"buckets: {e}") from e
    try:
        plan = build_plan(runs, n_buckets=args.buckets, strategy=args.strategy,
                          exclude=args.exclude, min_runs=args.min_runs)
    except ValueError as e:
        raise hs_family.UsageError(f"buckets: {e}") from e

    if not plan.readiness.ok:
        # RED readiness: reasons on stderr, exit 1, NO buckets.json written —
        # a split over oscillating data would reshuffle every run.
        payload = plan_json(plan)
        if as_json:
            print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        else:
            print("buckets: readiness RED — not splitting:", file=sys.stderr)
            for reason in plan.readiness.reasons:
                print(f"  {reason}", file=sys.stderr)
        return 1

    payload = plan_json(plan)
    wrote = None
    if args.out and apply:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                            encoding="utf-8")
        wrote = str(args.out)
    if args.out:
        payload = {**payload, "out": str(args.out), "dry_run": not apply}
    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        render_plan(plan)
        if args.out and not wrote:
            print(f"[dry-run: nothing written — --apply writes {args.out}]")
    return 0


BUCKETS = Command(add_args=add_arguments, run=_buckets)
