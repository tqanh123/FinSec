"""hs-code check scc — import cycles, at package and at module level.

    package  cycles of the graph between workspace packages (edges = measured imports),
             with members, edge counts and TIER CONFLICTS: each package declares its rung
             as `[tool.<namespace_root>] tier` in its pyproject.toml; an edge is legal only
             looking down or level, so a cross-rung cycle holds an upward edge
    module   `--dist NAME`: cycles among one package's modules
    ladder   `--ladder` / `--seeds M,..`: on any tree, the rung of every module once its
             cycles are collapsed, and what moving a set of modules drags along

A probe by default: cycles are numbers, exit 0. `--strict` makes a package cycle or a
module owned by two packages exit 1 (a module cycle under `--ladder --strict` too).

WHAT IT CANNOT SEE
    * Edges the snapshot does not record: `importlib`, entry points, sys.path tricks.
    * A package without a declared rung, or a rung outside the order, is UNDECIDED:
      listed, its edges judged neither legal nor illegal.
    * A rung is depth on the CONDENSATION; a missing edge makes a module look
      shallower, never deeper.
"""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import graph_core
from codegraph.check.tree import (CheckError, Tree, add_pin_args, import_counts,
                                   measure, module_graph, pin_of)
from hs_family.cli import Command

#: rung -> rank, 0 at the bottom; another order: --tiers FILE (`rungs: {name: rank}`).
#: Two rungs may share a rank: an edge between them is level, and level is legal.
DEFAULT_RUNGS: Dict[str, int] = {"contract": 0, "platform": 1, "sdk": 2, "profile": 3,
                                 "service": 4, "deployment": 5}


def cycles(graph: Dict[str, set]) -> List[List[str]]:
    return sorted(sorted(c) for c in graph_core._scc_tarjan({k: sorted(v) for k, v in graph.items()})
                  if len(c) > 1)


def ladder(graph: Dict[str, set]) -> Dict[str, int]:
    """Rung per module: the LONGEST path down to something importing nothing, on the
    condensation — through a cycle a walk has no length."""
    comps = [sorted(c) for c in graph_core._scc_tarjan({k: sorted(v) for k, v in graph.items()})]
    owner = {n: i for i, c in enumerate(comps) for n in c}
    down: Dict[int, set] = {i: set() for i in range(len(comps))}
    for src, tgts in graph.items():
        down[owner[src]].update(owner[d] for d in tgts if owner[d] != owner[src])
    depth: Dict[int, int] = {}
    for start in range(len(comps)):
        stack = [(start, iter(sorted(down[start])))]
        while stack:
            c, it = stack[-1]
            nxt = next((n for n in it if n not in depth), None)
            if nxt is not None:
                stack.append((nxt, iter(sorted(down[nxt]))))
                continue
            stack.pop()
            depth[c] = max((depth[n] + 1 for n in down[c]), default=0)
    return {n: depth[owner[n]] for n in graph}


def closure(graph: Dict[str, set], seeds: List[str]) -> Dict[str, Any]:
    """What moving `seeds` moves (transitively), and who stays behind pointing in."""
    missing = [s for s in seeds if s not in graph]
    if missing:
        raise CheckError("no module %s in this tree" % ", ".join(repr(m) for m in missing))
    seen: set = set()
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(graph[cur])
    inbound: Dict[str, List[str]] = {}
    for src, tgts in graph.items():
        if src not in seen:
            for dst in tgts & seen:
                inbound.setdefault(dst, []).append(src)
    return {"seeds": sorted(seeds), "closure": sorted(seen), "closed": not inbound,
            "inbound_from_outside": {k: sorted(v) for k, v in sorted(inbound.items())}}


def _rungs(path) -> Dict[str, int]:
    if path is None:
        return dict(DEFAULT_RUNGS)
    import yaml_io
    doc = yaml_io.yaml_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = doc.get("rungs") if isinstance(doc, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise CheckError("%s: no `rungs:` name->rank mapping" % path)
    return {str(k): int(v) for k, v in raw.items()}


def tiers(tree: Tree, rungs: Dict[str, int]) -> Tuple[Dict[str, str], List[str]]:
    """package -> rung from `[tool.<namespace_root>] tier`; the rest undecided."""
    ns = tree.descriptor.namespace_root
    out: Dict[str, str] = {}
    undecided: List[str] = []
    for p in tree.packages:
        try:
            doc = tomllib.loads((p.dir / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        tier = ((doc.get("tool") or {}).get(ns) or {}).get("tier") if ns else None
        if tier in rungs:
            out[p.name] = tier
        else:
            undecided.append(p.name)
    return out, undecided


def _conflict(members, edges, rung_of, rungs) -> Tuple[Any, str]:
    if any(m not in rung_of for m in members):
        return None, "undecided: %s has no rung" % ", ".join(sorted(m for m in members if m not in rung_of))
    for src in sorted(members):
        for dst, n in sorted((edges.get(src) or {}).items()):
            if dst in members and rungs[rung_of[dst]] > rungs[rung_of[src]]:
                return True, "upward edge %s (%s) -> %s (%s), %d import(s)" % (
                    src, rung_of[src], dst, rung_of[dst], n)
    return False, "every in-cycle edge looks down or level"


def probe_packages(tree: Tree, rungs: Dict[str, int], dist: Any, top: int) -> Dict[str, Any]:
    if not tree.packages:
        raise CheckError("no workspace root in codemap.yaml `layout.roots` (kind: workspace): "
                         "no packages to probe — ask the module question with --ladder")
    owner = tree.package_of()
    edges = import_counts(tree.records(), owner)
    rung_of, undecided = tiers(tree, rungs)
    pkg_cycles = []
    for comp in cycles({p.name: set(edges.get(p.name, {})) for p in tree.packages}):
        members = set(comp)
        conflict, why = _conflict(members, edges, rung_of, rungs)
        pkg_cycles.append({"members": comp, "conflict": conflict, "why": why,
                           "edges": sum(n for s in comp for d, n in (edges.get(s) or {}).items()
                                        if d in members),
                           "tiers": sorted({rung_of[m] for m in comp if m in rung_of})})
    mod_cycles: List[List[str]] = []
    if dist:
        if dist not in {p.name for p in tree.packages}:
            raise CheckError("--dist %s: no such package" % dist)
        mod_cycles = cycles(module_graph(tree.records(), {m for m, p in owner.items() if p == dist}))
    return {"schema": "hs-code.check.scc/1", "packages": len(tree.packages), "modules": len(owner),
            "package_edges": sum(sum(c.values()) for c in edges.values()),
            "dist_cycles": pkg_cycles, "module_cycles": mod_cycles[:top],
            "module_cycles_total": len(mod_cycles), "undecided_tiers": undecided,
            "module_ownership_clashes": tree.ownership_clashes()}


def probe_modules(tree: Tree, seeds: Any, top: int) -> Dict[str, Any]:
    graph = module_graph(tree.records())
    rung = ladder(graph)
    found = cycles(graph)
    by_rung: Dict[int, List[str]] = {}
    for mod, r in sorted(rung.items()):
        by_rung.setdefault(r, []).append(mod)
    out = {"schema": "hs-code.check.scc.ladder/1", "modules": len(graph),
           "edges": sum(len(v) for v in graph.values()),
           "edge_definition": "distinct module->module pairs, in-tree targets only, "
                              "module-level and function-local imports both",
           "rungs": len(by_rung), "cycles": found[:top], "cycles_total": len(found),
           "largest_cycle": max((len(c) for c in found), default=0),
           "by_rung": {str(r): mods for r, mods in sorted(by_rung.items())}}
    if seeds:
        out["seeds"] = closure(graph, [s.strip() for s in seeds.split(",") if s.strip()])
    return out


def _print_packages(p: Dict[str, Any]) -> None:
    print("scc: %d package(s), %d module(s), %d measured package edge(s)"
          % (p["packages"], p["modules"], p["package_edges"]))
    for c in p["dist_cycles"]:
        mark = {True: "CONFLICT", False: "cycle", None: "UNDECIDED"}[c["conflict"]]
        print("  package %s: %s  (%d in-cycle edge(s), tiers %s) — %s"
              % (mark, " -> ".join(c["members"]), c["edges"], c["tiers"], c["why"]))
    for c in p["module_cycles"]:
        print("  module cycle: %s" % " -> ".join(c))
    if p["module_cycles_total"] > len(p["module_cycles"]):
        print("  … %d more module cycle(s)" % (p["module_cycles_total"] - len(p["module_cycles"])))
    if p["undecided_tiers"]:
        print("  undecided tiers: %s" % ", ".join(p["undecided_tiers"]))
    for c in p["module_ownership_clashes"]:
        print("  RED module ownership: %s" % c)


def _print_modules(p: Dict[str, Any]) -> None:
    print("scc: %d module(s) · %d edge(s) · %d rung(s) · %d cycle(s)"
          % (p["modules"], p["edges"], p["rungs"], p["cycles_total"]))
    for r, mods in list(p["by_rung"].items())[:10]:
        print("  rung %-3s %3d  %s" % (r, len(mods), ", ".join(mods[:6]) + (" …" if len(mods) > 6 else "")))
    for c in p["cycles"]:
        print("  CYCLE (%d): %s" % (len(c), ", ".join(c)))
    sd = p.get("seeds")
    if sd:
        print("  seeds %d -> closure %d module(s)%s" % (len(sd["seeds"]), len(sd["closure"]),
                                                     "  · CLOSED, the set can move as a unit" if sd["closed"] else ""))
        for dst, srcs in sd["inbound_from_outside"].items():
            print("    points in: %s  <- %3d  %s" % (dst, len(srcs), ", ".join(srcs[:5])
                                                     + (", +%d more" % (len(srcs) - 5) if len(srcs) > 5 else "")))


def _scc_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--strict", action="store_true", help="exit 1 on a package cycle or a module owned twice")
    ap.add_argument("--dist", metavar="NAME", help="also list module cycles inside this package")
    ap.add_argument("--tiers", metavar="FILE", help="yaml `rungs: {name: rank}` (default: the six-rung order)")
    ap.add_argument("--ladder", action="store_true", help="module rungs and cycles, on any tree")
    ap.add_argument("--seeds", metavar="M,..", help="with --ladder: what moving these modules drags along")
    ap.add_argument("--top", type=int, default=10, help="module cycles listed (default 10)")
    add_pin_args(ap)


def _scc(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        tree = measure(Path(root), pin_of(args, opts))
        if args.ladder or args.seeds:
            payload = probe_modules(tree, args.seeds, args.top)
            red = bool(args.strict and payload["cycles_total"])
            show = _print_modules
        else:
            payload = probe_packages(tree, _rungs(args.tiers), args.dist, args.top)
            red = bool(args.strict and (payload["dist_cycles"] or payload["module_ownership_clashes"]))
            show = _print_packages
    except CheckError as e:
        print("hs-code check scc: %s" % e)
        return 2
    payload["strict"] = bool(args.strict)
    if getattr(opts, "json", False):
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        show(payload)
        print("scc: probe (numbers, not a verdict)" + ("; --strict: RED" if red else ""))
    return 1 if red else 0


SCC = Command(add_args=_scc_args, run=_scc)
