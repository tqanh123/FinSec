"""hs-code's `port` domain — `flow-trace`: the runs a batch declares, read off
the new tree's builder calls. A FLAT module under `harness/scripts/` (same
`hs-code-registry.d` reachability reason as every other
`codegraph_port_*_verbs.py`). The tracer is `port_flow_trace` — the same door
`scaffold slice-docs` builds its Flows section through.

`hs-code port flow-trace --batch B [--flow ID] [--snapshot NAME] [--edges]`

Adapted from `ob-tools@60075cb:packages/portkit/src/portkit/doc/flow_trace.py`
(`run`). The flows are the batch's own `flows:` (`{id, at, name?, builder?}`),
the builders the shipped table plus the batch's `flow_builders:`; the node homes
come from `--snapshot` (default: the batch's `new_snapshot`) and the tree is the
port root it measured. Upstream's positional REPO and `--plan`/`--ref` are
replaced by the batch and a snapshot NAME, like every other port verb.

Exit: 0 traced (the JSON carries every node, edge, home, stage and unresolved
spot; `--edges` prints the edges in text); 1 unknown batch; 2 no `flows:`, no
snapshot, a path the snapshot does not hold, or a file that does not parse —
zero flows reads exactly like "no flows", so it is never a green answer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_flow_trace  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--flow", default=None, metavar="ID")
    ap.add_argument("--snapshot", default=None, metavar="NAME")
    ap.add_argument("--edges", action="store_true")


def _print(t: dict, edges: bool) -> None:
    homes = t["homes"]
    print("── %s · %s    (%s)" % (t["id"], t["name"], t["at"]))
    print("   entry: %s   %d nodes · %d edges (%d branching)" % (
        t["entry"] or "— NOT DECLARED", len(t["nodes"]), len(t["edges"]),
        sum(1 for e in t["edges"] if e["kind"] == "branch")))
    print("   touches %d dist(s): %s" % (len(t["dists"]), ", ".join(t["dists"])))
    print("   %d stage(s) (%d looping), %d inter-stage edge(s)" % (
        len(t["stages"]), sum(1 for s in t["stages"] if s["loop"]), len(t["stage_edges"])))
    for i, st in enumerate(t["stages"]):
        rest = [n for n in st["nodes"] if n != st["entry"]]
        print("      [%d] %-22s %-14s%s" % (i, st["entry"], homes.get(st["entry"], "?"),
                                          "  ⟲ " + " · ".join(rest) if rest else ""))
    if edges:
        for e in t["edges"]:
            via = " via %s" % e["router"] if e["router"] else ""
            print("      %-8s %-24s -> %-24s [%s -> %s]%s" % (
                e["kind"], e["from"], e["to"], homes.get(e["from"], "?"),
                homes.get(e["to"], "?"), via))
    if t["unresolved"]:
        print("   ⚠ %d spot(s) NOT resolved:" % len(t["unresolved"]))
        for x in t["unresolved"]:
            print("      %s" % x)


def _flow_trace(args: argparse.Namespace) -> int:
    try:
        port_root = port_workspace.find_port_root(args.ctx.root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit
    try:
        batch = port_batch_reader.load_batch(port_root, args.batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit
    snap = args.snapshot or batch.new_snapshot
    if not snap:
        raise hs_family.UsageError("batch %r declares no `new_snapshot` — pass --snapshot NAME"
                                   % batch.name)
    _commit, idx = port_batch_reader.read_snapshot(port_root, snap, caller_named=True)
    try:
        traced = port_flow_trace.trace_all(
            [dict(f) for f in batch.flows], port_root, idx,
            port_flow_trace.load_builders(dict(batch.flow_builders or {})), args.flow)
    except port_flow_trace.Missing as e:
        raise hs_family.UsageError("port flow-trace: %s" % e) from e
    data: dict = {"batch": batch.name, "snapshot": snap, "flows": [t.as_dict() for t in traced]}
    if getattr(args.opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        for t in data["flows"]:
            _print(t, args.edges)
    return int(Verdict.ok(data).exit)


FLOW_TRACE = Command(add_args=_args, run=_flow_trace)
