#!/usr/bin/env python3
"""rule_view.py — review-time consumer of the standards operational zone.

Reads the standards SSOT-YAML tree (standards_graph), selects the operational
rule-leaves whose scope intersects the changed files (scope_match), derives the
language from scope, and emits a rule-scan.json that honours the
artifact-rule-scan.json byte-contract the gate validates (artifact_check
_rule_scan_consistency). This is the single source for the operational review
rules — it replaced the retired flat review-rules/<lang>/*.md tree; operational
rules now live in the std tree.

Producers (code-review, review-pr) call load_rules_dual, which is fail-soft: if
the consumer raises it returns an empty result tagged 'tree-error' so a review
is never blocked by a consumer defect.

Lang is DERIVED from scope (standards_graph.lang_from_scope), not stored — a
rule with scope `**/*.py` is a python rule with no `lang:` field.

API:
    load_rules_from_tree(root, changed_files) -> {rules, rules_applied, langs}
    load_rules_dual(root, changed_files) -> (result, source)
    build_rule_scan(root, changed_files, *, violations, reviewer, verdict, base) -> dict
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import standards_graph
from scope_match import scope_matches as _canonical_scope_matches

# resolve_actor lives in the hooks package (attribution, not auth) — reuse it so
# the rule-scan reviewer field is stamped the same way every other artifact is.
_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.append(str(_HOOKS_DIR))
import hook_runtime  # noqa: E402


def _rule_nodes_in_zone(graph: Dict[str, Any], zone: str,
                        types: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """Rule-leaves whose area lives in `zone`, INCLUDING disabled ones — the
    override layer may flip enabled either way, so callers run over the full set
    before the enabled filter. `types` post-filters by node type."""
    area_ids = {n.get("id") for n in graph["nodes"]
                if n.get("type") == "std_area" and n.get("zone") == zone}
    rg_ids = {n.get("id") for n in graph["nodes"]
              if n.get("type") == "rule_group" and n.get("std_area") in area_ids}
    return [n for n in graph["nodes"]
            if n.get("type") in types and n.get("rule_group") in rg_ids]


def _all_operational_rule_nodes(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every operational rule-leaf (the common case), disabled included."""
    return _rule_nodes_in_zone(graph, "operational", ("rule",))



def _review_gates(graph, overrides) -> Dict[str, Dict[str, Any]]:
    """The review page of the gate book: every SHIPPED operational rule, disabled
    included. `floor` is read after the repo's override, which may raise a rule to
    floor (tighten) but not lower it; a floor rule is a core gate no project layer
    turns off. `enabled` is the rule's state after the override; a rule off carries
    `off_layer` (shipped | override) and `off_reason`, so the book can say who."""
    import user_override
    shipped = _all_operational_rule_nodes(graph)
    applied = {n.get("id"): n for n in user_override.apply(shipped, overrides)[0]}
    out: Dict[str, Dict[str, Any]] = {}
    for r in shipped:
        now = applied.get(r["id"], r)
        row = {"severity": r.get("severity"), "enabled": now.get("enabled", True),
               "rule_group": r.get("rule_group"), "floor": bool(now.get("floor")),
               "core": bool(now.get("floor")), "detector": r.get("detector"),
               "scope": r.get("scope") or []}
        if now.get("enabled", True) is False:
            by_override = r.get("enabled", True) is not False
            row["off_layer"] = "override" if by_override else "shipped"
            row["off_reason"] = (now.get("_user_override_reason") if by_override
                                 else "shipped default")
        out[r["id"]] = row
    return out


def shipped_rule_gates(root) -> Dict[str, Any]:
    """Gate-registry source for the `review` page (`_review_gates`)."""
    import user_override
    return _review_gates(standards_graph.build_graph(Path(root)), user_override.load(root))


def _project_gate_offs(graph, overrides) -> set:
    """Rules this project switched off on the record (`<data home>/gates.yaml`), judged
    exactly as `hs-cli gates` judges them (`project_gate_layer.layer_problem`)."""
    import harness_paths
    import project_gate_layer as pgl
    home = harness_paths.data_root()
    if harness_paths.data_root_unresolved(home) or not (home / pgl.GATES_LAYER).is_file():
        return set()
    try:
        layer = pgl.read(home / pgl.GATES_LAYER)
    except pgl.LayerFileError:
        return set()  # an unreadable layer turns nothing off; `hs-cli gates` reports it
    known = {n: {"page": "review", "layer": pgl.GATES_LAYER, "floor": g["floor"]}
             for n, g in _review_gates(graph, overrides).items()}
    return set(pgl.offs(layer, known))

def load_rules(root, *, scope_intersects=None, zone: str = "operational",
               types: Tuple[str, ...] = ("rule",), severity: Optional[str] = None,
               floor: Optional[bool] = None,
               conflicts: bool = False) -> Dict[str, Any]:
    """Faceted single-door loader over the standards tree + override layer.

    The override layer is applied over the full zone set (disabled included)
    BEFORE filtering — an override may flip enabled or change scope; floor rules
    are refused there. Facets then narrow the set:
      - `scope_intersects`: keep only rules whose scope matches >=1 of these
        files (gitignore semantics, case-sensitive). `None` = no scope filter
        (the full enabled set); `[]` = matches nothing.
      - `zone` / `types`: which areas / node types to draw from.
      - `severity` / `floor`: exact-match facets on the rule's posture.
      - `conflicts`: REVIEW-only. When True, attach a `conflicts` list (layer-b
        new rules whose scope overlaps a shipped rule with opposite severity),
        computed through the content-hash cache. Default OFF so the gate
        hot-path (load_rules_from_tree) never triggers the audit.
    `langs` is derived from the matched rules' scopes, not stored on the rule.
    """
    graph = standards_graph.build_graph(Path(root))

    import user_override
    nodes = _rule_nodes_in_zone(graph, zone, tuple(types))
    overrides = user_override.load(root)
    nodes, warnings = user_override.apply(nodes, overrides)
    offs = _project_gate_offs(graph, overrides)
    if offs:
        nodes = [dict(n, enabled=False) if n.get("id") in offs else n for n in nodes]

    files = None if scope_intersects is None else list(scope_intersects)
    applied: List[Dict[str, Any]] = []
    for r in nodes:
        if r.get("enabled") is False:
            continue
        if floor is not None and bool(r.get("floor")) != bool(floor):
            continue
        if severity is not None and r.get("severity") != severity:
            continue
        scope = r.get("scope") or []
        if files is not None:
            if not (scope and _canonical_scope_matches(
                    scope, files, case_insensitive=False)):
                continue
        applied.append(r)

    langs = sorted({standards_graph.lang_from_scope(r.get("scope")) for r in applied})
    out: Dict[str, Any] = {
        "rules": applied,
        "rules_applied": sorted({r.get("id") for r in applied}),
        "langs": langs,
        "override_warnings": warnings,
    }
    if conflicts:
        import rule_audit_cache
        out["conflicts"] = rule_audit_cache.audit_or_cached(root)["conflicts"]
    return out


def load_rules_from_tree(root, changed_files) -> Dict[str, Any]:
    """Select the operational rules applicable to `changed_files`.

    Thin facet over `load_rules` (kept as the producer-stable name + signature):
    a rule applies when it is enabled AND its scope matches >=1 changed file.
    """
    return load_rules(root, scope_intersects=list(changed_files or []))


def load_rules_dual(root, changed_files) -> Tuple[Dict[str, Any], str]:
    """Load the operational rules for a REVIEW (the producer path: code-review,
    review-pr). The standards tree is the single source now (the flat
    review-rules tree is retired); the name is kept for producer-call stability.
    Surfaces `conflicts` (review-only — the gate path uses load_rules_from_tree,
    which never audits). Fail-soft: if the consumer raises, return an empty
    result tagged 'tree-error' so a review is never blocked by a consumer
    defect (the gate's coverage check then sees no applicable rules)."""
    try:
        return load_rules(root, scope_intersects=list(changed_files or []),
                          conflicts=True), "tree"
    except Exception:
        return ({"rules": [], "rules_applied": [], "langs": [],
                 "override_warnings": [], "conflicts": []}, "tree-error")


def _verdict_for(violations: List[Dict[str, Any]]) -> str:
    """Derive the scan verdict from violation severities: any critical -> BLOCKED;
    any info -> PASS_WITH_RISK; none -> PASS. Only the recognized severities
    (critical/info — the gate's enum) drive the verdict; an off-enum/missing
    severity does NOT inflate a clean scan to PASS_WITH_RISK (the gate's own enum
    check still rejects the malformed violation, so this stays fail-closed)."""
    sevs = {str(v.get("severity", "")).strip().lower() for v in violations}
    if "critical" in sevs:
        return "BLOCKED"
    if "info" in sevs:
        return "PASS_WITH_RISK"
    return "PASS"


def build_rule_scan(root, changed_files, *, violations: Optional[List] = None,
                    reviewer: Optional[str] = None,
                    verdict: Optional[str] = None, base: str = "HEAD") -> Dict[str, Any]:
    """Build a rule-scan.json record for one review.

    `rules_applied` is computed from the operational tree; `violations` is the
    reviewer's findings (default none = clean). `verdict` is derived from the
    violations unless explicitly given. `changed_files` is recorded verbatim
    it is the single diff source the coverage-gate reads (the gate
    never re-derives git, so the universe it checks is exactly what the producer
    reviewed). `base` is the revision the diff is against — HEAD for a
    working-tree review, the PR's base for a PR range — and is what the
    `structural` verdict is judged against."""
    import structural_change
    changed_files = list(changed_files or [])
    violations = list(violations or [])
    loaded = load_rules_from_tree(root, changed_files)
    return {
        "rules_applied": loaded["rules_applied"],
        "violations": violations,
        "verdict": verdict or _verdict_for(violations),
        "reviewer": reviewer or hook_runtime.resolve_actor(),
        "ts": datetime.now(timezone.utc).isoformat(),
        # the exact files the producer reviewed — the only diff source.
        "changed_files": changed_files,
        "langs": loaded["langs"],
        # whether that diff is structural, judged NOW against the reviewed base:
        # by ship the diff may be committed and the base gone, so the gate reads
        # this back rather than re-deriving it.
        "structural": structural_change.judge(root, changed_files, base=base).as_dict(),
    }


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description="Emit a rule-scan from the operational standards tree.")
    ap.add_argument("--root", default=".", help="repo root (contains harness/standards/)")
    ap.add_argument("--emit", default=None,
                    help="write the rule-scan to this path instead of stdout")
    ap.add_argument("--base", default="HEAD",
                    help="revision the diff is against (a PR's base for a PR range)")
    ap.add_argument("files", nargs="*", help="changed files")
    args = ap.parse_args()
    scan = build_rule_scan(args.root, args.files, base=args.base)
    text = json.dumps(scan, indent=2, ensure_ascii=False, default=str)
    if args.emit:
        Path(args.emit).parent.mkdir(parents=True, exist_ok=True)
        Path(args.emit).write_text(text + "\n", encoding="utf-8")
        print(args.emit)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
