"""`layer_direction` — an edge may look DOWN or ACROSS, never UP.

Port of the frozen check_layers doctrine, generalized: the tier ladder is a
rule PARAM (`tiers: {group: rank}`), not an import of porting physics.

THREE EDGE KINDS, COUNTED SEPARATELY — never merged
    module | lazy | symbol. A reverse edge living only at lazy level imports
    fine and dies at runtime, on exactly the CI branch that never runs. One
    merged number hides exactly that story. A fourth, `type_only`, joins them
    for a record of a language pack (an `import type` is coupling) and stays
    out for Python's `if TYPE_CHECKING:` -- `include_type_only: true|false`
    decides for every record instead.

UNDECIDED IS NOT OK
    An edge with an unranked end (group absent from `tiers`) is UNDECIDED:
    counted, printed, and — when a `baseline` is given — checked BOTH WAYS
    (a new undecided edge the baseline does not carry is red; a baseline entry
    whose edge vanished is red too: a shrinking baseline without anyone
    fixing anything is a spot that learned to hide). Without a baseline the
    undecided set is a NOTE, and a green run says only "among the decidable
    edges, none is reverse".

WHAT IT CANNOT SEE (inherited + own)
    * Symbol edges are a lower bound (getattr, dict dispatch, callbacks).
    * Same-file split-group edges are SKIPPED (a file torn across groups makes
      shared module-level names look like cross-group edges that are not);
      they are counted into a note basket, not violations.
    * The ladder itself is trusted: a group mapped to the wrong rank produces
      confident nonsense.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ...graph.model import Edge
from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)


class LayerDirectionCheck:
    CAUSES = ("reverse-edge", "undecided-new", "undecided-gone")

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        tiers = params.get("tiers")
        if not isinstance(tiers, dict) or not tiers:
            raise RulesError(f"rule {rule_id!r}: `tiers` must be a non-empty mapping")
        self.tiers: dict[str, int] = {str(k): int(v) for k, v in tiers.items()}
        self.type_only = params.get("include_type_only")
        if self.type_only is not None and not isinstance(self.type_only, bool):
            raise RulesError(f"rule {rule_id!r}: `include_type_only` must be true or false")
        self.baseline: dict[str, int] | None = None
        if params.get("baseline") is not None:
            self.baseline = {str(k): int(v) for k, v in params["baseline"].items()}

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.graph is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a graph in the context")
        violations: list[Violation] = []
        counts: Counter[str] = Counter()
        undecided: Counter[str] = Counter()
        same_file_split: Counter[str] = Counter()

        def judge(e: Edge) -> None:
            counts[e.kind] += 1
            if e.same_file:
                same_file_split[e.kind] += 1
                return
            s = self.tiers.get(e.src) if e.src else None
            d = self.tiers.get(e.dst) if e.dst else None
            if s is None or d is None:
                undecided[e.kind] += 1
                return
            if d > s:   # looking UP the ladder
                violations.append(Violation(
                    self.rule_id, "reverse-edge", e.label,
                    {"kind": e.kind, "src": e.src, "dst": e.dst,
                     "src_tier": s, "dst_tier": d}))

        ctx.graph.walk(judge, include_type_only=self.type_only)

        notes = [
            f"edges: {dict(sorted(counts.items())) or 'none'}",
            f"undecided (an end unranked in tiers): {dict(sorted(undecided.items())) or 'none'}",
            f"same-file split-group edges (counted, not judged): "
            f"{dict(sorted(same_file_split.items())) or 'none'}",
        ]
        if self.baseline is not None:
            have = dict(undecided)
            for kind, n in sorted(have.items()):
                if n > self.baseline.get(kind, 0):
                    violations.append(Violation(
                        self.rule_id, "undecided-new",
                        f"undecided {kind} edges grew: {self.baseline.get(kind, 0)} -> {n}",
                        {"kind": kind, "was": self.baseline.get(kind, 0), "now": n}))
            for kind, n in sorted(self.baseline.items()):
                now = have.get(kind, 0)
                if now < n:
                    # grows-only doctrine (the conn-door trap): a baseline that
                    # SHRANK while nobody resolved anything is an edge that
                    # learned to hide, not a win. Any movement touches the
                    # baseline, in the same commit, with a reason.
                    violations.append(Violation(
                        self.rule_id, "undecided-gone",
                        f"baseline undecided {kind} = {n} but this pass saw "
                        f"{now} — the edges moved somewhere, nobody resolved them",
                        {"kind": kind, "was": n, "now": now}))
        return RuleResult(self.rule_id, "error", tuple(violations), tuple(notes),
                          metrics={"edges": dict(counts),
                                   "undecided": dict(undecided),
                                   "same_file_split": dict(same_file_split)})


register_kind("layer_direction", languages="*")(
    require_params("tiers")(LayerDirectionCheck))
