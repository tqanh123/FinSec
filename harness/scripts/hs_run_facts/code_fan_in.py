"""hs_run_facts.code_fan_in — the `code.fan_in` fact provider.

`provide(ctx) -> {value, basis, warning, [not_in_snapshot]}`. `value` =
`{subject_path: N}`, the DIRECT (one-hop) importer COUNT for each subject --
`codegraph_change_reader.fan_in`'s own shape, the SAME door
`harness/hooks/simplify_gate.py` already calls for its diff-size nudge
(reused here, not re-derived, so the two callers can never disagree about
what "fan-in" means). A subject ABSENT from the snapshot is never folded
into `value` as `0` (which would read as "measured, zero importers") -- it
is named in `not_in_snapshot` instead, the same convention `code.importers`
already uses.

This fact is a DATA POINT for a review/red-team report, never a severity
input: `harness/tests/test_fan_in_stays_out_of_severity.py` is the AST gate
that keeps `risk_rubric.derive_risk`/`review_recall.assess_scope` (and every
function either calls) from ever reading it.
"""
from __future__ import annotations

import codegraph_change_reader

_BASIS = (
    "direct (one-hop) import edges only (`codegraph_change_reader.fan_in`) "
    "-- a DATA POINT surfaced in the report, never a severity/risk input; a "
    "module invoked only via subprocess, prose, or dynamic dispatch carries "
    "no import edge and is undercounted here (see no-orphan-code.md)"
)

_WARNING = (
    "code.fan_in counts direct (one-hop) import edges only, and is reported "
    "as data -- it never enters a severity or risk score; a module reached "
    "only via subprocess/prose/dynamic dispatch will undercount here"
)


def provide(ctx: dict) -> dict:
    idx = ctx["snapshot"]
    subjects = sorted(set(ctx.get("subjects") or ()))

    counts = codegraph_change_reader.fan_in(idx, subjects)
    not_in_snapshot = [p for p in subjects if p not in counts]

    out = {"value": counts, "basis": _BASIS, "warning": _WARNING}
    if not_in_snapshot:
        out["not_in_snapshot"] = not_in_snapshot
    return out
