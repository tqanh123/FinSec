#!/usr/bin/env python3
"""perf_telemetry.py — load/perf metrics (telemetry, never a gate) + per-language
coverage judgement.

Load/stress/perf is a MEASUREMENT, not a pass/fail: read k6/JMeter JSON
into {p50, p95, error_rate, throughput}, append to the perf channel, and flag a
p95 regression vs the previous baseline as an ADVISORY — never a hard block. The
band is wide on purpose (environment noise; mirrors hs:bakeoff's noise-band
thinking).

Coverage is judged PER LANGUAGE: there is no fair global average across
stacks, so each Cobertura report is compared to its own threshold and a failing
stack is named — never washed out by a passing one.
"""

import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

_PERF_SINK = "perf-metrics.jsonl"


# --- load/perf ----------------------------------------------------------------
def perf_regression(current: dict, baseline: dict, threshold_pct: float = 20) -> dict:
    """Advisory verdict comparing current p95 to a baseline. `regressed` is True
    only when p95 rose more than `threshold_pct` over baseline (a wide band to
    absorb environment noise). ALWAYS advisory — there is no block path."""
    cur = (current or {}).get("p95")
    base = (baseline or {}).get("p95")
    regressed = False
    delta_pct = None
    if isinstance(cur, (int, float)) and isinstance(base, (int, float)) and base > 0:
        delta_pct = (cur - base) / base * 100.0
        regressed = delta_pct > threshold_pct
    return {"regressed": regressed, "delta_pct": delta_pct,
            "enforcement": "advisory", "block": False}


# --- per-language coverage ----------------------------------------------------
