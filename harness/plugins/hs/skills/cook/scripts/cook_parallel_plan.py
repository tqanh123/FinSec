#!/usr/bin/env python3
"""cook_parallel_plan.py — partition a plan's phases for opt-in parallel cook.

The safety core of `cook --parallel` (default OFF = today's sequential behavior).
Two responsibilities, both pure and testable:

1. `is_parallel_enabled(flag, env, config)` — deterministic opt-in resolution:
   explicit `--parallel` flag > `HARNESS_COOK_PARALLEL` env > `cook.parallel` config
   > default False. Nothing set -> sequential, so existing installs do not change.

2. `partition(phases)` — decide which phases may run concurrently. A phase is
   parallel ONLY when it is `parallel_safe` AND its file ownership is disjoint from
   every other parallel_safe phase. Any ownership overlap demotes BOTH phases to
   sequential and records the conflict — the fallback is always reported, never
   silent (orchestration-protocol: never parallel-edit the same file).

`expand_owns` resolves ownership globs against the real tree at the CLI edge so the
grouping core stays a pure set operation.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "scripts"))

import plan_graph
from scope_match import globs_overlap

ENV_VAR = "HARNESS_COOK_PARALLEL"
_TRUTHY = {"1", "true", "yes", "on"}


def is_parallel_enabled(flag=None, env=None, config=None) -> bool:
    """Resolve the opt-in. flag: bool|None (explicit CLI), env: str|None, config: bool|None."""
    if flag is not None:
        return bool(flag)
    if env is not None:
        return str(env).strip().lower() in _TRUTHY
    if config is not None:
        return bool(config)
    return False


def expand_owns(owns, root) -> set:
    """Expand ownership globs to repo-relative file paths. A pattern that matches
    nothing (e.g. a file a phase will CREATE) is kept literally — it is still owned."""
    root = Path(root)
    out = set()
    for pattern in owns or []:
        matches = [m for m in root.glob(pattern) if m.is_file()]
        if matches:
            out.update(m.relative_to(root).as_posix() for m in matches)
        else:
            out.add(Path(pattern).as_posix())
    return out


def _entries_overlap(a, b) -> bool:
    """True if two ownership entries (path or glob) can name a common file.

    Delegates to `scope_match.globs_overlap`, the token automaton that answers
    exactly this question — do two patterns share any path — rather than keeping
    a second, weaker answer to it here. What used to sit in this spot compared the
    fixed prefix of each pattern before its first wildcard, which is a proxy for
    the question, not the question: any two patterns rooted in the same directory
    matched, whatever they actually selected, so `data/*.yaml` and `data/*.json`
    read as overlapping while naming disjoint files.

    Measured on 171 pattern pairs against the real expanded file sets: the prefix
    rule called 26 pairs overlapping that share no file (15.2%), the automaton 3
    (1.8%). Both missed ZERO real overlaps -- the direction that can corrupt a tree
    was never the difference between them, so this is not a safety trade. The
    remaining conservatism is where it belongs: `expand_owns` keeps a pattern that
    matches nothing today (a file a phase will CREATE), so a phase still owns what
    it is about to write.
    """
    if a == b:
        return True
    # Spell each side the way `expand_owns` will (`Path(...).as_posix()`), so
    # `./src/new.py` and `src/new.py` -- the same file, and the same file the
    # expansion would produce -- are compared as one path rather than as two
    # strings. The matcher answers a question about paths; handing it a spelling
    # it has no reason to normalize is this caller's job to avoid.
    a, b = Path(a).as_posix(), Path(b).as_posix()
    if a == b:
        return True
    return globs_overlap(a, b)


def _shared_owns(owns_a, owns_b):
    """The overlapping ownership entries between two phases, by scope (not just by
    literal-string equality) — so two globs with overlapping scope are caught."""
    out = []
    for a in owns_a or []:
        for b in owns_b or []:
            if _entries_overlap(a, b):
                out.append(a if a == b else "%s ~ %s" % (a, b))
    return sorted(set(out))


def _import_edge_overlap(owns_a, owns_b, units) -> list:
    """Two phases can own no file in common and still be
    unsafe to run concurrently when one's file imports the other's. Reuses
    `plan_graph.file_import_edge` (the SAME hs-code-snapshot check
    `plan_graph.find_import_edge_conflicts` runs at plan-time) rather than a
    second detector — this module and `plan_graph` must never quietly disagree
    about what "safe in parallel" means. `units=None` (no snapshot resolved)
    degrades to `[]`: no import information, never a crash, never a false claim.
    """
    if not units:
        return []
    hits = []
    for a in sorted(owns_a or ()):
        for b in sorted(owns_b or ()):
            if plan_graph.file_import_edge(a, b, units):
                hits.append("%s ~ %s (import edge)" % (a, b))
    return hits


def partition(phases, units=None) -> dict:
    """Group phases into concurrent-safe vs sequential.

    phases: list of {"id": str, "parallel_safe": bool, "owns": [path, ...]}.
    `units`: an optional hs-code snapshot map (path -> record, see
    `plan_graph._snapshot_units`) — when given, a pair with NO shared file is
    still demoted if one's file imports the other's.
    Returns {"parallel": [id], "sequential": [id], "conflicts": [{a,b,shared}]}.
    Conservative by design: a single shared path (or import edge) demotes both
    phases to sequential.
    """
    safe = [p for p in phases if p.get("parallel_safe")]
    conflicts = []
    conflicted = set()
    for i in range(len(safe)):
        for j in range(i + 1, len(safe)):
            a, b = safe[i], safe[j]
            shared = _shared_owns(a.get("owns"), b.get("owns"))
            if not shared:
                shared = _import_edge_overlap(a.get("owns"), b.get("owns"), units)
            if shared:
                conflicts.append({"a": a["id"], "b": b["id"], "shared": shared})
                conflicted.add(a["id"])
                conflicted.add(b["id"])
    parallel = [p["id"] for p in safe if p["id"] not in conflicted]
    sequential = [p["id"] for p in phases
                  if not p.get("parallel_safe") or p["id"] in conflicted]
    return {"parallel": parallel, "sequential": sequential, "conflicts": conflicts}


def _resolve_config_default(root) -> bool:
    """Read cook.parallel from harness/data/cook.yaml (default False) — best-effort."""
    import config_layers
    cfg = config_layers.resolve_path("cook.yaml", root=root)[0]
    if not cfg.is_file():
        return False
    try:
        import yaml_io
        data = yaml_io.safe_load(cfg.read_text(encoding="utf-8")) or {}
        return bool(data.get("parallel", False))
    except Exception:
        return False


def _load_units(units_json, root) -> "dict | None":
    """`--units-json`'s file when given (an EXPLICIT override, for a fixture
    or a snapshot other than the project's own auto-selected HEAD one), in
    whichever of its two accepted shapes: an already-flattened `path ->
    record` map, or a real hs-code snapshot's raw `_index.json` (top-level
    `units` key grouping records by category) — the same shape `plan_graph.
    _snapshot_units` flattens for `plan_graph`'s own `find_import_edge_
    conflicts`, reused here rather than a second flattener (plan_graph and
    cook_parallel_plan must never disagree about what a snapshot record
    means).

    Omitted (the normal case): auto-selects `root`'s own store snapshot of
    HEAD via `plan_graph.resolve_snapshot_units` — the SAME selection
    `plan_graph.find_import_edge_conflicts` itself now uses at plan-time, so
    the two channels can never disagree about which snapshot answered."""
    if not units_json:
        units, _warning = plan_graph.resolve_snapshot_units(root)
        return units
    data = json.loads(Path(units_json).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "units" in data:
        return plan_graph._snapshot_units(data)
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phases-json", help="JSON file: list of {id,parallel_safe,owns}")
    ap.add_argument("--parallel", dest="flag", action="store_true", default=None,
                    help="force parallel ON (overrides env/config)")
    ap.add_argument("--root", default=".", help="repo root (default cwd)")
    ap.add_argument("--expand", action="store_true",
                    help="expand each phase's owns globs against --root before partitioning")
    ap.add_argument("--units-json",
                    help="EXPLICIT override for the snapshot units (raw "
                         "_index.json, or an already-flattened path->record "
                         "map) that demote a disjoint-file pair joined by a "
                         "real import edge. Omitted: auto-selects --root's own "
                         "store snapshot of HEAD (plan_graph.resolve_snapshot_"
                         "units) — no need to name a snapshot directory.")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    enabled = is_parallel_enabled(
        flag=args.flag,
        env=os.environ.get(ENV_VAR),
        config=_resolve_config_default(root),
    )
    if not args.phases_json:
        print(json.dumps({"parallel_enabled": enabled}))
        return 0

    phases = json.loads(Path(args.phases_json).read_text(encoding="utf-8"))
    if args.expand:
        for p in phases:
            p["owns"] = sorted(expand_owns(p.get("owns"), root))
    out = partition(phases, units=_load_units(args.units_json, root))
    out["parallel_enabled"] = enabled
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
