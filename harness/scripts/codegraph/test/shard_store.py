"""The `durations --store` shard store: lane medians, merge, LPT preview,
censoring.

Port of ob-tools' `testkit.durations.store` (pinned commit `60075cb`), minus
the pieces that RUN pytest (`--rops`, the `--run` collect cross-check): running
a suite is `hs-rops`' job.

WHAT IT CANNOT SEE
    * Censoring is a threshold guess (`>= timeout - 0.5s`), not a proof the
      test actually hung.
    * The shard preview is TEST TIME ONLY — per-job startup (image build, dep
      install, cache restore) is in no junit number.
"""
from __future__ import annotations

import re
import statistics
from typing import Sequence

from codegraph.test.junit import NO_TIME, ParseReport

#: `<lane>-rep<N>` run_id convention.
_LANE_RE = re.compile(r"^(?P<lane>.+)-rep(?P<rep>\d+)$")

#: The runner's per-test timeout the censor edge is read against.
DEFAULT_TIMEOUT_S = 180.0


def lane_medians(runs: Sequence[ParseReport],
                 ) -> "dict[str, dict[str, float]]":
    """Group runs by the `<lane>-rep<N>` run_id convention; median each test
    over the lane's reps. Blind (time None) records are skipped, never zero."""
    lanes: "dict[str, list[dict[str, float]]]" = {}
    for rep in runs:
        if not rep.records:
            continue
        rid = rep.records[0].run_id
        m = _LANE_RE.match(rid)
        lanes.setdefault(m.group("lane") if m else rid, []).append(
            {r.test_id: r.time_s for r in rep.records if r.time_s is not NO_TIME})
    return {
        lane: {tid: round(statistics.median(r[tid] for r in reps if tid in r), 3)
               for tid in {t for r in reps for t in r}}
        for lane, reps in lanes.items()}


def merge_lanes(lanes: "dict[str, dict[str, float]]") -> "dict[str, float]":
    """Union of ids; an id measured in several lanes takes the median of its
    lane values (lanes are marker partitions, so this should be empty)."""
    per: "dict[str, list[float]]" = {}
    for lane in lanes.values():
        for tid, secs in lane.items():
            per.setdefault(tid, []).append(secs)
    return {tid: round(statistics.median(v), 3) for tid, v in per.items()}


def lpt_preview(durations: "dict[str, float]", n: int) -> "dict[str, float]":
    """Greedy longest-processing-time balance for n shards; deterministic
    (sort by -seconds then id)."""
    loads = [0.0] * n
    for _tid, secs in sorted(durations.items(), key=lambda kv: (-kv[1], kv[0])):
        loads[loads.index(min(loads))] += secs
    return {"max": round(max(loads), 1), "min": round(min(loads), 1),
            "spread": round(max(loads) - min(loads), 1)}


def censored_ids(durations: "dict[str, float]", timeout_s: float) -> "list[str]":
    """Ids at/over the cap minus 0.5s: hung-then-killed honest upper bounds."""
    edge = timeout_s - 0.5
    return sorted(t for t, s in durations.items() if s >= edge)

