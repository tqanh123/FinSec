"""hs-code's `port` domain — the three READ doors: `status`, `suggest`,
`trace`. A FLAT module under `harness/scripts/` (same reason
`codegraph_port_ledger_verbs.py` is one — the `hs-code-registry.d`
reachability channel matches a dot-free `module:` name).

`STATUS` is ported (algorithm-faithful) from `ob-tools@60075cb:packages/
portkit/src/portkit/port_verbs.py:153` (`run_status`) WITH `packages/
portkit/src/portkit/batch/port_todo.py`'s own `run_todo` folded in — there
is no separate `todo` verb here (a caller wanting the unblocked/ordered
listing gets it as part of `status`'s own report, never a second read of
the same ledger). The "doctor block" (upstream's own batch-context-plus-
twelve-laws aside, `port_verbs.py:175-193`) calls this tree's OWN law
collector, `port_ledger_laws.collect` — never a second implementation — and
upstream's bare `except (SystemExit, OSError, ValueError, Exception): pass`
does NOT cross: a doctor-block failure here always lands in the envelope's
own `warnings`, per this harness's no-silent-swallow rule; only the ABSENCE
of a resolvable batch (zero, or more than one with none named) stays quiet,
matching upstream's own designed default ("a ledger read from anywhere must
keep printing its counts even when no batch context resolves").

The tier ladder `run_todo`'s own ordering used to hard-code
(`batch/port_order.py:29-32`, six OpsBrain tier names) is GONE — see
`port_order.py`'s own module docstring for why and what replaces it.
Upstream's own windowing (`--top`/`--bottom`/`--from`/`--to`/`--after`/
`--budget`/`--format jsonl`) and the `--blockers` blocker-cluster view are
NOT ported: neither is read by anything `status`'s own required behaviour
needs, and adding either without a caller would be exactly the code this
tree's no-orphan rule forbids.

`SUGGEST` is ported (algorithm-faithful) from `port_todo.py`'s own
`run_suggest`: candidates for an OLD node by NORMALIZED-BODY sha, matched
against a NEW-tree snapshot. NEVER WRITES — the module docstring upstream
carries ("suggest is blind to CHANGED bodies... it never writes") is kept
word for word as this door's own contract, and no line of this function
ever touches `port_ledger_writer`. `--old-snap`/`--new-snap` take a
snapshot NAME under `.port/snapshots/`, resolved through
`port_batch_reader.read_snapshot` — the same door `carry-forward`'s own
`--from-snapshot` already uses — never a caller-typed directory path the
way upstream's own `--old-snap DIR`/`--new-snap DIR` did (this harness has
no bare-directory snapshot convention anywhere else).

`TRACE` is ported (algorithm-faithful) from `port_verbs.py:216`
(`run_why`), the verb NAME taken from `ledger/trace.py`'s own naming (this
harness's `trace` replaces upstream's `why`) — but not that module's own
symbol-wide "whole to-do list" report (callers, tier-direction, owned
tests): those eight items stand on machinery this tree does not build yet
(a per-symbol caller index, a `Ladder` class, `_tests.yaml` ownership
tables). `trace`'s own inputs are only `Ledger.transitions_of` and
`BlockerContext` — reusing the versions already ported for the ledger
write door, never re-copied. `trace`'s own history-plus-blocker-marks
report is `run_why` unchanged in substance.

EXIT CODES follow the family table (matching `codegraph_port_ledger_verbs.
py`'s own convention, not upstream's uniform rc-2-on-everything): a broken
ledger line, an unresolvable `--repo`, or an EXPLICITLY named `--batch`/
snapshot that does not exist is exit 2; a well-formed read that found
nothing (`status` after a pattern leaves no node, `suggest` with zero
candidates, `trace` on a node with no line at all) is exit 1; a normal
report is exit 0.

GIT: none of the three touches git at all — every read is off the ledger
and a named snapshot's already-written `_index.json`.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
for _p in (_SCRIPTS,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hs_family  # noqa: E402
import regex  # noqa: E402
import port_batch_reader  # noqa: E402
import port_ledger_laws  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_ledger_spec  # noqa: E402
import port_order  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core import regex_bound  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_spec import STATUS_ORDER  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger  # noqa: E402

_MARK = {True: "CLEARED", False: "still blocking", None: "cannot check"}


# ── status (+ todo folded in) ────────────────────────────────────────────────

def _status_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("pattern", nargs="?", default=None,
                    help="case-insensitive regex filtering node keys")
    ap.add_argument("--list", action="store_true",
                    help="print every matching node, not only counts")
    ap.add_argument("--batch", default=None, dest="batch_name",
                    help="resolve the tier ladder + doctor block against this batch "
                         "(falls back to the port root's sole batch when unambiguous)")
    ap.add_argument("--source", default=None, dest="source_id")


def _resolve_batch_ctx(port_root, batch, source_id):
    """`(batch_obj, source, pinned_sha, idx)` for one already-NAMED batch —
    raises whatever `port_batch_reader`/the pin door raises; the caller
    decides whether that is a hard refusal (an explicit `--batch` typo) or a
    warning (a batch that resolved but whose snapshot did not)."""
    source, problem = port_batch_reader.find_source(batch, source_id)
    if problem:
        raise hs_family.UsageError(str(problem))
    if not batch.frozen_snapshot:
        raise hs_family.UsageError(
            "batch %r has no `frozen_snapshot` declared" % batch.name)
    pinned, idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    return batch, source, pinned, idx


def _todo_groups(port_root, state):
    """The `todo` verb's own four buckets — ported (algorithm-faithful)
    from `port_todo.run_todo`'s `--unblocked` (default) path. Ordering is
    the CALLER's job (`_order_todo` below); this only classifies."""
    ctx = port_ledger_laws.BlockerContext(state, port_root)
    blocked = {k: ev for k, ev in state.items() if ev.status == "blocked"}
    ready: "list[str]" = []
    waiting_human: "list[tuple[str, list[str]]]" = []
    still: "list[tuple[str, list[str]]]" = []
    for k, ev in blocked.items():
        results = [(b, ctx.cleared(b)) for b in ev.extra.get("blocked_by") or ()]
        if any(r is False for _, r in results):
            still.append((k, [b for b, r in results if r is False]))
        elif any(r is None for _, r in results):
            waiting_human.append((k, [b for b, r in results if r is None]))
        else:
            ready.append(k)
    unported = [k for k, ev in state.items() if ev.status == "unported"]
    return {
        "unported": unported, "ready": ready,
        "waiting_human": [{"node": k, "blocked_by": bs} for k, bs in waiting_human],
        "still_blocked": [{"node": k, "blocked_by": bs} for k, bs in still],
    }


def _order_todo(todo, order):
    """Sort each bucket in place — by the target's own declared tier ladder
    (`order`) when one resolved, else by node key (never left in ledger
    replay order, which is `dict` insertion order and tells the reader
    nothing about the tree)."""
    key = order.of if order is not None else (lambda k: k)
    todo["unported"] = order.sort(todo["unported"]) if order else sorted(todo["unported"])
    todo["ready"] = order.sort(todo["ready"]) if order else sorted(todo["ready"])
    todo["waiting_human"].sort(key=lambda r: key(r["node"]))
    todo["still_blocked"].sort(key=lambda r: key(r["node"]))
    return todo


def _print_todo(todo) -> None:
    sections = (
        ("DO NOW (`unported`, no blocker)", todo["unported"], None),
        ("UNLOCKED (`blocked`, every blocker cleared)", todo["ready"], None),
        ("WAITING FOR A HUMAN (only uncheckable blockers left)", todo["waiting_human"], "blocked_by"),
        ("STILL BLOCKED", todo["still_blocked"], "blocked_by"),
    )
    for title, rows, blocker_key in sections:
        print("\n%s — %d node(s)" % (title, len(rows)))
        for row in rows:
            if blocker_key is None:
                print("  %s" % row)
            else:
                print("  %s   [%s]" % (row["node"], ", ".join(row[blocker_key])))


def _status(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    pattern, list_flag = args.pattern, args.list
    batch_name, source_id = args.batch_name, args.source_id

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(
            "port status: %s" % "; ".join(msg for _code, msg in e.findings)).exit

    state = ledger.replay()
    if pattern:
        try:
            pat = regex_bound.compile_bounded(pattern, regex.I)
            budget = regex_bound.ScanBudget()
            state = {k: v for k, v in state.items()
                     if regex_bound.bounded_search(pat, k, budget)}
        except regex.error as e:
            return Verdict.broken("port status: bad pattern %r: %s" % (pattern, e)).exit
        except (regex_bound.PatternBudgetExceeded, TimeoutError) as e:
            return Verdict.broken("port status: pattern %r: %s" % (pattern, e)).exit

    warnings: "list[str]" = []
    batch_ctx = None
    if batch_name is not None:
        try:
            batch = port_batch_reader.load_batch(port_root, batch_name)
        except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
            return Verdict.broken(str(e)).exit
        try:
            batch_ctx = _resolve_batch_ctx(port_root, batch, source_id)
        except (hs_family.UsageError, hs_family.RefusedError) as e:
            warnings.append("batch %r: %s" % (batch_name, e))
    else:
        known = port_batch_reader.list_batches(port_root)
        if len(known) == 1:
            try:
                batch = port_batch_reader.load_batch(port_root, known[0])
                batch_ctx = _resolve_batch_ctx(port_root, batch, None)
            except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError,
                    hs_family.UsageError, hs_family.RefusedError) as e:
                warnings.append("batch %r: %s" % (known[0], e))

    doctor = None
    if batch_ctx is not None:
        batch, source, _pinned, _idx = batch_ctx
        try:
            scan = port_ledger_laws.collect(ledger, port_root, batch, source)
            doctor = {"batch": batch.name, "findings": len(scan.findings),
                     "uncheckable": scan.uncheckable,
                     "pending_decisions": scan.pending_decisions}
        except Exception as exc:  # noqa: BLE001 — best-effort, but NEVER silent:
            warnings.append("doctor block failed: %s" % exc)  # this line is the fix.

    order = None
    if batch_ctx is not None:
        _batch, _source, pinned, idx = batch_ctx
        try:
            rank = port_order.tier_ranks(port_root)
        except port_order.LadderError as e:
            return Verdict.broken("port status: %s" % e).exit
        if not rank:
            warnings.append(
                "the target's codemap.yaml declares no tier ladder (a `layer_direction` "
                "or `same_tier_edge` rule with `tiers:`) — ordering by dist, then node key")
        order = port_order.Order(rank, idx, pinned[:8])
    else:
        warnings.append(
            "no ladder ordering available (pass --batch NAME with a "
            "frozen_snapshot) — falling back to node-key order")

    counts = Counter(ev.status for ev in state.values())
    counts_out: "list[dict[str, Any]]" = [
        {"status": s, "count": counts[s], "closed": True}
        for s in STATUS_ORDER if counts.get(s)]
    counts_out += [{"status": s, "count": counts[s], "closed": False}
                  for s in sorted(set(counts) - set(STATUS_ORDER))]

    list_out: "list[dict[str, Any]] | None" = None
    if list_flag:
        list_out = [
            {"node": k, "status": ev.status,
             "to": list(ev.extra.get("to") or ()),
             "blocked_by": list(ev.extra.get("blocked_by") or ())}
            for k, ev in sorted(state.items())]

    todo = _order_todo(_todo_groups(port_root, state), order)

    data: "dict[str, Any]" = {
        "ledger": str(ledger.path), "events": len(ledger.events),
        "broken": len(ledger.broken),
        "batch": batch_ctx[0].name if batch_ctx else None,
        "warnings": warnings, "doctor": doctor, "counts": counts_out, "todo": todo,
    }
    if list_out is not None:
        data["list"] = list_out

    if not state:
        msg = ("no node has a status line"
              + (" matching /%s/" % pattern if pattern else ""))
        print(msg, file=sys.stderr)
        if getattr(opts, "json", False):
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(Verdict.red(data).exit)

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(Verdict.ok(data).exit)

    print("ledger   : %s" % data["ledger"])
    print("events   : %d   nodes with lines: %d" % (data["events"], sum(counts.values())))
    if data["batch"]:
        print("batch    : %s" % data["batch"])
    for w in warnings:
        print("⚠ %s" % w)
    if doctor is not None:
        print("laws     : %d red finding(s)" % doctor["findings"])
    print()
    for row in counts_out:
        mark = "" if row["closed"] else "   ⚠ OUTSIDE THE CLOSED SET"
        print("  %6d  %s%s" % (row["count"], row["status"], mark))
    if list_out is not None:
        print()
        for row in list_out:
            dest = "  -> %s" % ", ".join(row["to"]) if row["to"] else ""
            blk = "  [blocked: %s]" % ", ".join(row["blocked_by"]) if row["blocked_by"] else ""
            print("  %-12s %s%s%s" % (row["status"], row["node"], dest, blk))
    _print_todo(todo)
    return int(Verdict.ok(data).exit)


STATUS = Command(add_args=_status_args, run=_status)


# ── suggest ──────────────────────────────────────────────────────────────────

def _suggest_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("node", nargs="?", default=None,
                    help="one OLD node key; omit for every node in --old-snap")
    ap.add_argument("--old-snap", required=True, dest="old_snap", metavar="NAME")
    ap.add_argument("--new-snap", required=True, dest="new_snap", metavar="NAME")


def _snapshot_bodies(idx, anchor):
    """Every node key of one already-read `_index.json` -> its own
    `{sha, path, lines}` — ported (algorithm-faithful, trimmed to the
    fields `suggest`'s own report reads) from `ob-tools@60075cb:packages/
    portkit/src/portkit/ledger/ledger_io.py`'s own `snapshot_nodes`."""
    out: "dict[str, dict]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            for s in m.get("symbols") or ():
                for holder in [s] + list(s.get("methods") or ()):
                    key = "%s::%s::%s" % (anchor, m.get("module"), holder.get("name"))
                    out[key] = {"sha": holder.get("sha"), "path": m.get("path"),
                               "lines": holder.get("lines")}
    return out


def _suggest(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    node, old_name, new_name = args.node, args.old_snap, args.new_snap

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(
            "port suggest: %s" % "; ".join(msg for _code, msg in e.findings)).exit

    try:
        old_commit, old_idx = port_batch_reader.read_snapshot(
            port_root, old_name, caller_named=True)
        new_commit, new_idx = port_batch_reader.read_snapshot(
            port_root, new_name, caller_named=True)
    except (port_batch_reader.SnapshotNotFoundError, hs_family.UsageError,
           hs_family.RefusedError) as e:
        return Verdict.broken(str(e)).exit

    old = _snapshot_bodies(old_idx, old_commit[:8])
    new = _snapshot_bodies(new_idx, "new@" + new_commit[:8])
    by_sha: "dict[str, list[str]]" = defaultdict(list)
    for k, v in new.items():
        by_sha[v["sha"]].append(k)

    state = ledger.replay()

    if node is not None and node not in old:
        print("no node %r in snapshot %r" % (node, old_name), file=sys.stderr)
        data = {"targets": 1, "hits": 0, "results": [], "misses": []}
        return int(Verdict.red(data).exit)

    targets = [node] if node is not None else sorted(old)
    results: "list[dict]" = []
    misses: "list[dict]" = []
    for k in targets:
        sha = old[k]["sha"]
        cands = by_sha.get(sha, [])
        if not cands:
            misses.append({"node": k, "sha": sha})
            continue
        cur = state.get(k)
        results.append({
            "node": k, "sha": sha, "path": old[k]["path"], "lines": old[k]["lines"],
            "current_status": cur.status if cur else None,
            "candidates": cands, "shared": len(cands) > 1,
            "suggested_command": (
                "hs-code port map %s --status ported --to %s --by suggest:%s"
                % (k, cands[0], sha[:8])),
        })

    data = {"targets": len(targets), "hits": len(results), "results": results,
           "misses": misses}
    verdict = Verdict.ok(data) if results else Verdict.red(data)

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(verdict.exit)

    for row in results:
        print(row["node"])
        line0 = (row["lines"] or [None])[0]
        print("  sha %s   %s:%s   current status: %s"
              % (row["sha"], row["path"], line0, row["current_status"] or "(no line yet)"))
        for c in row["candidates"]:
            warn = ("   ⚠ %d symbols share this sha — identical bodies, cannot pick which one"
                    % len(row["candidates"])) if row["shared"] else ""
            print("    candidate  %s%s" % (c, warn))
        print("    run:  %s" % row["suggested_command"])
        print()
    for row in misses:
        if node is not None:
            print("%s\n  sha %s — NO candidate in the new tree." % (row["node"], row["sha"]))
            print("  Meaning: the body changed, or it has not been carried over. "
                 "The tool cannot tell which.")
    print("%d/%d node(s) have body-sha candidates." % (len(results), len(targets)))
    print("NOTHING WRITTEN — a human reviews, then runs `map`.")
    return int(verdict.exit)


SUGGEST = Command(add_args=_suggest_args, run=_suggest)


# ── trace (formerly `why`) ───────────────────────────────────────────────────

def _trace_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("node")


def _trace(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    node = args.node

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(
            "port trace: %s" % "; ".join(msg for _code, msg in e.findings)).exit

    hist = ledger.transitions_of(node)
    if not hist:
        print("node %r has no line in the ledger" % node, file=sys.stderr)
        data: "dict[str, Any]" = {"node": node, "history": []}
        if getattr(opts, "json", False):
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(Verdict.red(data).exit)

    ctx = port_ledger_laws.BlockerContext(ledger.replay(), port_root)
    steps: "list[dict[str, Any]]" = []
    for i, (frm, ev) in enumerate(hist, 1):
        blockers = [{"blocker": b, "mark": _MARK[ctx.cleared(b)]}
                   for b in ev.extra.get("blocked_by") or ()]
        steps.append({
            "step": i, "from": frm, "status": ev.status, "ts": ev.ts,
            "by": ev.by, "actor": ev.actor or None,
            "destinations": list(ev.extra.get("to") or ()),
            "blockers": blockers,
            "reason": ev.extra.get("reason"), "note": ev.note,
        })
    trivial = port_ledger_spec.trivial_1to1(hist[-1][1])
    data = {"node": node, "history": steps, "trivial_last": trivial}

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(Verdict.ok(data).exit)

    print(node)
    print()
    for step in steps:
        print("  %d. %s -> %s      %s   (%s, %s)"
             % (step["step"], step["from"] or "∅", step["status"], step["ts"],
                step["by"], step["actor"] or "—"))
        for t in step["destinations"]:
            print("     dest   %s" % t)
        for b in step["blockers"]:
            print("     block  %s   [%s]" % (b["blocker"], b["mark"]))
        if step["reason"]:
            print("     why    %s" % step["reason"])
        if step["note"]:
            print("     note   %s" % step["note"])
        print()
    if trivial:
        print("  (last line is a trivial 1->1: `ported`, one destination, "
             "name unchanged — no reason required)")
    return int(Verdict.ok(data).exit)


TRACE = Command(add_args=_trace_args, run=_trace)
