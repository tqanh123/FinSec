"""`same_tier_edge` — lateral edges the ladder cannot see.

Two groups on the SAME tier importing each other breaks no layer law — that
is exactly the problem: a lateral edge between two same-tier services is an
edge nobody wanted that no tier gate could ever flag. This kind reports
same-tier cross-group edges; declare it `severity: warn` unless a project has
decided its lateral edges are forbidden.

WHAT IT CANNOT SEE
    * Same-file split-group edges are skipped (not real cross-group edges).
    * Undecided ends (a group missing from `tiers`) are notes, not matches —
      a missing rank must not read as "same tier as everything".
"""
from __future__ import annotations

from typing import Any

from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)


class SameTierEdgeCheck:
    CAUSES = ("lateral-edge",)

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        tiers = params.get("tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise RulesError(f"rule {rule_id!r}: `tiers` must be a non-empty mapping")
        self.tiers: dict[str, int] = {str(k): int(v) for k, v in tiers.items()}

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.graph is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a graph in the context")
        violations: list[Violation] = []
        undecided = 0

        def judge(e) -> None:
            nonlocal undecided
            if e.same_file:
                return
            s = self.tiers.get(e.src) if e.src else None
            d = self.tiers.get(e.dst) if e.dst else None
            if s is None or d is None:
                undecided += 1
                return
            if s == d and e.src != e.dst:
                violations.append(Violation(
                    self.rule_id, "lateral-edge", e.label,
                    {"kind": e.kind, "src": e.src, "dst": e.dst, "tier": s}))

        ctx.graph.walk(judge)
        return RuleResult(self.rule_id, "error", tuple(violations),
                          (f"undecided ends skipped: {undecided}",))


register_kind("same_tier_edge", languages="*")(
    require_params("tiers")(SameTierEdgeCheck))
