"""hs-code's `port` domain — `born-new`: declare the symbols born on the new
tree between two of its snapshots. A FLAT module under `harness/scripts/`
(same `hs-code-registry.d` reachability reason as every other
`codegraph_port_*_verbs.py`).

`hs-code port born-new --batch B --old SNAP [--new SNAP] [--source ID] [--apply]`

Adapted from `ob-tools@60075cb:packages/devkit/src/devkit/analysis/bornnew.py`
(`diff_symbols`, `record_new`). Upstream kept a ledger of its own
(`born-new:<module>::<name>`, one self-looping status); here the port ledger
already owns the status and the law it answers — L7 holds every new-tree node
to "some old node's destination, or declared `born-new`" — so the lines land
there, keyed `new@<hash8>::<module>::<name>` at the NEW snapshot's commit,
through the batch write door (`write_all`: validated in order, all-or-nothing),
`src` the batch source's pin like every other line.
`--new` defaults to the batch's `new_snapshot`.

WHAT IS DECLARED: a body (`module::name`) present in `--new` and not in
`--old`, that is not a move (same name and sha, exactly ONE old home and ONE
new home — `logger` in every module pairs with nothing), not already the
destination of any ledger row (`claimed`: a port put it there, it was not
born), and not already declared `born-new` under any anchor (`already`: a
rerun appends nothing). A renamed symbol reads as removed + added and is
declared: the name is the only link the snapshots carry.

Upstream's `attach` (an event-bus reminder after each snapshot) has no bus to
hang on here; the reminder is L7 itself — `check-ledger` names every new-tree
node nobody declared.

Exit: 0 (dry-run plans, `--apply` writes); 2 broken ledger / unreadable
snapshot / no new snapshot; 3 a planned line the ledger laws refuse.
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

from hook_runtime import resolve_actor  # noqa: E402

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_ledger_verbs import write_all  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_spec import new_anchor  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger, now_ts  # noqa: E402


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--old", required=True, dest="old_snapshot")
    ap.add_argument("--new", default=None, dest="new_snapshot")
    ap.add_argument("--source", default=None, dest="source_id")


def diff_bodies(old: "dict[str, str]", new: "dict[str, str]") -> "dict[str, list[str]]":
    """`module::name -> sha` twice -> added / removed / changed / moved,
    mutually exclusive. A move pairs one (name, sha) with exactly one old and
    one new module; any other count stays removed + added."""
    def by_key(f: "dict[str, str]") -> "dict[tuple[str, str], set[str]]":
        out: "dict[tuple[str, str], set[str]]" = {}
        for body, sha in f.items():
            mod, _, name = body.partition("::")
            out.setdefault((name, sha), set()).add(mod)
        return out

    ka, kb = by_key(old), by_key(new)
    moved = sorted((key[0], next(iter(ka[key])), next(iter(kb[key])))
                   for key in ka.keys() & kb.keys()
                   if len(ka[key]) == len(kb[key]) == 1 and ka[key] != kb[key])
    moved_old = {"%s::%s" % (a, n) for n, a, _b in moved}
    moved_new = {"%s::%s" % (b, n) for n, _a, b in moved}
    return {"added": sorted(set(new) - set(old) - moved_new),
            "removed": sorted(set(old) - set(new) - moved_old),
            "changed": sorted(b for b in set(old) & set(new) if old[b] != new[b]),
            "moved": ["%s: %s -> %s" % m for m in moved]}


def _bodies(idx: dict) -> "dict[str, str]":
    """`module::name -> sha` of every symbol and method one `_index.json`
    carries — upstream's `snapshot_nodes` (`ob-tools@60075cb:packages/portkit/
    src/portkit/ledger/ledger_io.py:81-95`) trimmed to the sha, anchor-free:
    the two snapshots compare by body. Its own traversal, like every port
    module's (`port_ledger_laws` keeps the same trim for L7)."""
    out: "dict[str, str]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            for s in m.get("symbols") or ():
                for holder in [s] + list(s.get("methods") or ()):
                    out["%s::%s" % (m.get("module"), holder.get("name"))] = holder.get("sha")
    return out


def _body_of(key: str) -> str:
    return key.split("::", 1)[1] if "::" in key else key


def _born_new(args: argparse.Namespace) -> int:
    try:
        port_root = port_workspace.find_port_root(args.ctx.root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit
    try:
        batch = port_batch_reader.load_batch(port_root, args.batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit
    new_name = args.new_snapshot or batch.new_snapshot
    if not new_name:
        raise hs_family.UsageError("batch %r declares no `new_snapshot` — pass --new"
                                   % batch.name)
    source, problem = port_batch_reader.find_source(batch, args.source_id)
    if problem:
        return Verdict.broken(problem).exit
    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        raise hs_family.UsageError("the ledger is broken: %s" % e) from e
    _c, old_idx = port_batch_reader.read_snapshot(port_root, args.old_snapshot, caller_named=True)
    new_commit, new_idx = port_batch_reader.read_snapshot(port_root, new_name, caller_named=True)
    diff = diff_bodies(_bodies(old_idx), _bodies(new_idx))
    state = ledger.replay()
    claimed = {_body_of(t) for ev in state.values()
               for t in ev.extra.get("to") or ()}
    declared = {_body_of(k) for k, ev in state.items() if ev.status == "born-new"}
    anchor = new_anchor(new_commit)
    fresh = [b for b in diff["added"] if b not in claimed and b not in declared]
    actor, stamp = resolve_actor(), now_ts()
    events = [port_ledger_reader.Event(
        ts=stamp, node="%s::%s" % (anchor, b), status="born-new", by="human",
        actor=actor, extra={"to": (), "reason": "born between snapshots %s and %s"
                            % (args.old_snapshot, new_name),
                            "src": {"id": source.get("id"), "sha": source.get("pinned_sha")}})
              for b in fresh]
    apply = bool(getattr(args.opts, "apply", False))
    write_all(ledger, events, "born-new", apply=apply)
    data = {"batch": batch.name, "old": args.old_snapshot, "new": new_name, "apply": apply,
            **diff, "claimed": sorted(b for b in diff["added"] if b in claimed),
            "already": sorted(b for b in diff["added"] if b in declared and b not in claimed),
            "declare": [e.node for e in events], "written": len(events) if apply else 0}
    if getattr(args.opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("born-new %s -> %s: +%d -%d ~%d moved %d" % (
            args.old_snapshot, new_name, len(diff["added"]), len(diff["removed"]),
            len(diff["changed"]), len(diff["moved"])))
        for node in data["declare"]:
            print("  + %s" % node)
        print("declare %d (claimed %d, already %d)%s" % (
            len(events), len(data["claimed"]), len(data["already"]),
            "" if apply else " — dry-run: --apply writes"))
    return int(Verdict.ok(data).exit)


BORN_NEW = Command(add_args=_args, run=_born_new)
