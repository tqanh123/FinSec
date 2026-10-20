"""hs-code check plan-baseline — the baseline numbers a restructure plan cites, off one
fresh snapshot: modules and units, import edges (module-level and in function bodies),
ambiguous bare imports, the highest fan-in, and the measured rung ladder.

TWO EDGE COUNTS, ON PURPOSE
    The edge counts and the fan-in table are RAW: one import-list entry is one edge.
    The ladder is the DEDUPED module graph (`codegraph.check.tree.module_graph`): depth
    needs only "is there an edge"; "how much does A depend on B" needs the count.

AMBIGUOUS BARE IMPORTS
    Each module's own `ambiguous_imports` (a bare `import X` with several candidates),
    never the snapshot's `ambiguous_path_refs` (prose naming a module by stem).

WHAT IT CANNOT SEE
    Every blind spot of the snapshot: a missing edge reads as looser coupling and a
    shallower rung, never the other way. A hub is whatever the fan-in table ranks high.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from codegraph._core.testfiles import build_test_layout, is_test_dir
from codegraph.check.scc import cycles, ladder
from codegraph.check.tree import CheckError, add_pin_args, measure, module_graph, pin_of
from hs_family.cli import Command


def is_test_unit(unit: str, layout: Any = None) -> bool:
    """A unit key (`<root-id>/<unit-name>`, no filename) whose own segments
    are shaped like a test tree — `layout.is_test_dir` (DECLARED `layout.
    tests_subdir` wins outright; UNDECLARED falls back to the convention
    `tests?(_.+)?`), not a bespoke `"test"/"tests"` equality check: the old
    check never matched `tests_repo`/`tests_sdk`-style directories, which is
    exactly the shape a `packages/<p>/tests_*` layout uses. `layout=None`
    (no descriptor available at this call site) falls back to the bare
    module-level `is_test_dir` — the UNDECLARED convention only."""
    return layout.is_test_dir(unit) if layout is not None else is_test_dir(unit)


def compute(idx: Dict[str, Any], top: int = 7, root: "Path | None" = None) -> Dict[str, Any]:
    layout = build_test_layout(root) if root is not None else None
    rows = [(u, m) for u, ms in idx.get("units", {}).items() for m in ms]
    mods = {m["module"] for _, m in rows}

    def raw(m, key):
        return [b for b in (t.split("::", 1)[0] for t in m.get(key) or ()) if b in mods and b != m["module"]]

    ml = fl = 0
    fan_in: Dict[str, int] = {}
    for unit, m in rows:
        a, b = raw(m, "imports_module_level"), raw(m, "imports_function_local")
        ml, fl = ml + len(a), fl + len(b)
        if not is_test_unit(unit, layout):
            for t in a + b:
                fan_in[t] = fan_in.get(t, 0) + 1
    ambiguous = [{"unit": u, "module": m["module"], "name": a["name"], "candidates": a.get("candidates", [])}
                 for u, m in rows for a in m.get("ambiguous_imports") or ()]
    graph = module_graph([m for _, m in rows])
    rung = ladder(graph)
    found = cycles(graph)
    by_rung: Dict[str, int] = {}
    for r in rung.values():
        by_rung[str(r)] = by_rung.get(str(r), 0) + 1
    unit_range: Dict[str, List[int]] = {}
    for unit, m in rows:
        r = rung[m["module"]]
        lo, hi = unit_range.get(unit, [r, r])
        unit_range[unit] = [min(lo, r), max(hi, r)]
    ranked = sorted(fan_in.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    fan_top = [{"module": k, "importers": v} for k, v in ranked]
    return {
        "modules_total": len(mods), "units": len({u for u, _ in rows}),
        "unparseable": (idx.get("totals") or {}).get("unparseable", 0),
        "by_unit": {u: len(ms) for u, ms in idx.get("units", {}).items()},
        "edges_module_level": ml, "edges_function_local": fl,
        "edges_function_local_pct": fl / (ml + fl) if ml + fl else 0.0,
        "edge_definition": "raw import-list entries to another in-tree module, self-loops dropped",
        "ambiguous_bare_imports": len(ambiguous), "ambiguous_bare_refs": ambiguous,
        "fan_in_denominator": "module-level + function-local edges from non-test units",
        "fan_in_top": fan_top,
        "tiering": {"components": len(graph) - sum(len(c) for c in found) + len(found),
                    "cycles": len(found), "largest_scc": max((len(c) for c in found), default=0),
                    "by_rung": dict(sorted(by_rung.items(), key=lambda kv: int(kv[0]))),
                    "unit_rung_range": dict(sorted(unit_range.items())),
                    "hub_rungs": {k: rung[k] for k, _ in ranked}},
    }


def render(d: Dict[str, Any], snapshot: str) -> str:
    t = d["tiering"]
    total = d["edges_module_level"] + d["edges_function_local"]
    out = ["## Baseline — snapshot `%s`" % snapshot, "",
           "%d modules · %d units · %d unparseable file(s)." % (d["modules_total"], d["units"], d["unparseable"]),
           "", "| Unit | Modules |", "|---|---:|"]
    out += ["| `%s` | %d |" % kv for kv in sorted(d["by_unit"].items(), key=lambda kv: (-kv[1], kv[0]))]
    out += ["", "- **%d** module-level edges, **%d** in function bodies (%d%% of %d; %s)."
            % (d["edges_module_level"], d["edges_function_local"], round(d["edges_function_local_pct"] * 100),
               total, d["edge_definition"]),
            "- **%d** ambiguous bare imports." % d["ambiguous_bare_imports"], "",
            "**Highest fan-in.** Denominator: %s." % d["fan_in_denominator"], "",
            "| Module | Importers |", "|---|---:|"]
    out += ["| `%s` | %d |" % (r["module"], r["importers"]) for r in d["fan_in_top"]]
    rungs = sorted(int(k) for k in t["by_rung"])
    out += ["", "## Measured rungs", "",
            "Condensation + longest-path depth (rung 0 imports nothing in-tree). **%d components · "
            "%d cycles · largest SCC %d modules.**" % (t["components"], t["cycles"], t["largest_scc"]), "",
            "| rung | " + " | ".join(map(str, rungs)) + " |", "|---|" + "|".join("---" for _ in rungs) + "|",
            "| modules | " + " | ".join(str(t["by_rung"][str(r)]) for r in rungs) + " |", "",
            "| Unit | Lowest rung | Highest rung |", "|---|---|---|"]
    out += ["| `%s` | %d | %d |" % (u, *t["unit_rung_range"][u])
            for u in sorted(d["by_unit"], key=lambda u: (-d["by_unit"][u], u)) if u in t["unit_rung_range"]]
    if t["hub_rungs"]:
        out += ["", "Hub rungs: " + " · ".join("`%s` **%d**" % kv for kv in t["hub_rungs"].items()) + "."]
    return "\n".join(out)


def _plan_baseline_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--top", type=int, default=7, metavar="N", help="rows in the fan-in table (default 7)")
    add_pin_args(ap)


def _plan_baseline(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        tree = measure(Path(root), pin_of(args, opts))
    except CheckError as e:
        print("hs-code check plan-baseline: %s" % e)
        return 2
    data = compute(tree.idx, args.top, Path(root))
    data["snapshot"], data["commit"] = tree.idx.get("snapshot"), tree.idx.get("commit")
    print(json.dumps(data, sort_keys=True, ensure_ascii=False) if getattr(opts, "json", False)
          else render(data, str(data["snapshot"])))
    return 0


PLAN_BASELINE = Command(add_args=_plan_baseline_args, run=_plan_baseline)
