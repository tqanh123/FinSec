"""hs-code's `port` domain — the `rehome` verb. A FLAT module under
`harness/scripts/`, sibling of `codegraph_port_ledger_verbs.py`/
`codegraph_port_judge_verbs.py` (same reason: the `hs-code-registry.d`
reachability channel matches a dot-free `module:` name).

`REHOME` is ported (algorithm-faithful) from `ob-tools@60075cb:
packages/portkit/src/portkit/ledger/rehome.py` (`load_table`/`check_table`/
`infer_moves`/`plan`/`build_events`/`promote_merges`/`run`): a batch that
reorganises its OWN already-ported tree — a dedupe, a test reorg, a wave
rewrite moving a symbol from one already-landed module to another — leaves
every OLD row's `to` pointing at that destination's FORMER home. The
ledger is append-only, so a rehome is one NEW line per changed row: same
key/status/reason/blockers, `to` re-pointed at the new home, plus
`rehomed_from=<old destinations>`.

THE OLD->NEW TABLE, AND WHY THIS IS NOT `module_map`: this harness already
has an old->new module table (`port_spec.py`'s own `module_map`), but it
answers a DIFFERENT question — the UPSTREAM package tree's layout onto
THIS repo's own layout, read by `add-module`/`build` while a batch is
still landing symbols for the first time. `rehome` runs AFTER symbols have
already landed: its old/new pair are both destinations INSIDE this repo's
own port tree (a reorg of code this repo already owns), which `module_map`
was never declared against and has no opinion on. Reusing it would
conflate two unrelated mappings under one name — `rehome` keeps its own
table door, `load_table`/`check_table` below, ported from upstream rather
than borrowed from `port_spec`.

WHAT DIFFERS FROM UPSTREAM:
  * `--ledger` upstream flag dropped: `port_ledger_reader.ledger_path` is
    already the one door every write verb in this tree resolves the ledger
    through (off `--repo`'s own port root); a caller-typed override would
    be a second one.
  * `--snap`/`--from-snap` take a bare NAME under `.port/snapshots/`
    (`port_batch_reader.snapshot_path`/`snapshot_commit`), never a raw
    directory path — the same "a name, not a path" discipline
    `carry-forward`'s own `--from-snapshot` already established (its own
    docstring: "any snapshot under `.port/snapshots/`, no batch of its own
    required"). No `--batch` at all: rehome's job is entirely inside the
    already-ported NEW tree, so it never needs a batch's `frozen_snapshot`
    (the OLD-tree pin every other write verb here anchors `src.sha`
    against) — see the `src` note below.
  * `src.sha`: upstream's ledger predates the `src` rule this harness's own
    write door adds (`port_ledger_writer.validate_src`) — every line THIS
    verb appends must carry one. There is no batch/pin to check `--snap`
    against (rehome moves code that is already inside the port tree, not a
    fresh checkout of an upstream source), so `src.sha` is `--snap`'s own
    commit stamp (`port_batch_reader.snapshot_commit`) — the evidence that
    every new destination this run writes actually exists in that tree —
    and `src.id` is the fixed literal `"rehome"` (`validate_src` only
    requires a non-empty string; this line's provenance is not any one
    `sources[]` entry, it is the rehome operation itself).
  * EXIT CODES follow the family table (`codegraph_port_ledger_verbs.py`'s
    own docstring), not upstream's blanket `die`: a malformed table entry,
    a table/snapshot that IS present but fails to read (unparseable, wrong
    schema), an unknown `--key`, or a broken ledger is exit 2 (already-
    broken state); an unknown `--snap`/`--from-snap` name (typed at the
    command line, no such name under `.port/snapshots/` at all) is exit 1
    — a finding, never broken state, via `port_batch_reader.read_snapshot`'s
    own `caller_named=True` rung; a target destination missing from the final
    snapshot, an `--to-key` that disagrees with `--snap`'s own anchor, both
    snapshots anchoring at the same commit, a duplicate destination in one
    planned row, a planned row a ledger law refuses, or the rehome creating
    a NEW L9 finding is exit 3 (`hs_family.RefusedError` — a well-formed
    request a rule declines); nothing left to rehome is exit 0 when
    something was ALREADY at its new home (`already`/`born_new_already`),
    exit 1 when the plan touched nothing at all — matching
    `judge-nodes`/`judge-removed`'s own "found nothing" convention, not
    upstream's blanket exit 1.
  * L9 IS CALLED, NOT COPIED: the before/after cross-ledger diff runs
    `port_ledger_laws.l9_cross_findings` (the SAME measurement
    `check-ledger`/the `ledger` rule kind already call) over the ledger
    state before and after the planned writes — never a second, drifted
    implementation of the same law.

GIT: none — every commit this verb needs comes from a named snapshot's own
`_index.json` stamp, never a live git resolve (mirrors `carry-forward`'s
own "GIT: none").
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Tuple

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hook_runtime import resolve_actor  # noqa: E402

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_ledger_laws  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_ledger_spec  # noqa: E402
import port_workspace  # noqa: E402
import yaml_io  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_ledger_verbs import _not_written, write_all  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_reader import Event  # noqa: E402
from port_ledger_spec import (  # noqa: E402
    NEW_PREFIX, PortingLedgerError, STATUSES_WITH_DEST, parse_key, trivial_1to1,
)
from port_ledger_writer import BrokenLedgerError, Ledger, now_ts  # noqa: E402

Body = Tuple[str, str]          # (module, name)

_HASH8 = re.compile(r"^[0-9a-f]{8}$")
_MOD = re.compile(r"^[A-Za-z_][\w.]*$")
_NAME = re.compile(r"^([\w.]+|\*)$")

#: The `src.id` every line this verb appends carries — see the module
#: docstring's `src.sha` note for why there is no `sources[]` id to use
#: instead.
_SRC_ID = "rehome"

#: How many changed rows a plain (non `--list`) run prints.
_SHOWN = 10


class TableError(ValueError):
    """The old->new table cannot be used honestly."""


# ── the old->new table: load + check (ported from `rehome.py`) ─────────────

def _parse_body(text: str, where: str) -> Body:
    parts = [p.strip() for p in str(text).strip().split("::")]
    if len(parts) == 3 and (parts[0].startswith(NEW_PREFIX) or _HASH8.match(parts[0]) or parts[0] == "new"):
        parts = parts[1:]           # a full node key: the anchor is irrelevant, the body is the identity
    if len(parts) != 2 or not _MOD.match(parts[0]) or not _NAME.match(parts[1]):
        raise TableError("%s: %r is not `<module>::<name>`" % (where, text))
    return parts[0], parts[1]


def load_table(path: Path) -> "list[tuple[Body, Body]]":
    """Entries in file order; a wildcard is `(mod, '*') -> (mod2, '*')`.
    YAML reads through `yaml_io.yaml_load` (the one shared YAML reader in
    this tree), never a second `import yaml` — upstream's own loader used
    PyYAML directly."""
    raw = path.read_text(encoding="utf-8")
    pairs: "list[tuple[str, str]]" = []
    if path.suffix.lower() == ".csv":
        for i, r in enumerate(csv.reader(raw.splitlines()), 1):
            if not r or not "".join(r).strip() or r[0].lstrip().startswith("#"):
                continue
            if len(r) != 2:
                raise TableError("%s:%d: want 2 columns `old,new`, got %d" % (path, i, len(r)))
            if i == 1 and "::" not in r[0]:
                continue                                    # header row
            pairs.append((r[0], r[1]))
    else:
        try:
            doc = yaml_io.yaml_load(raw)
        except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
            raise TableError("%s: not YAML: %s" % (path, exc)) from exc
        if isinstance(doc, dict) and set(doc) == {"moves"}:
            doc = doc["moves"]
        if isinstance(doc, dict):
            pairs = [(str(k), str(v)) for k, v in doc.items()]
        elif isinstance(doc, list):
            for i, entry in enumerate(doc, 1):
                if not (isinstance(entry, dict) and set(entry) == {"old", "new"}):
                    raise TableError(
                        "%s: item %d must be a mapping with exactly `old` and `new`" % (path, i))
                pairs.append((str(entry["old"]), str(entry["new"])))
        else:
            raise TableError(
                "%s: want a mapping `old: new` (or a list of {old, new}), got %s"
                % (path, type(doc).__name__))
    out = [(_parse_body(o, "%s old" % path), _parse_body(n, "%s new" % path)) for o, n in pairs]
    for o, n in out:
        if (o[1] == "*") != (n[1] == "*"):
            raise TableError(
                "%s::%s -> %s::%s: a wildcard must be on both sides" % (o[0], o[1], n[0], n[1]))
    return out


def check_table(entries: "list[tuple[Body, Body]]") -> "dict[Body, Body]":
    """Entries -> old->new map; raises `TableError` on identity /
    duplicate-with-conflict / chain."""
    m: "dict[Body, Body]" = {}
    for o, n in entries:
        if o == n:
            raise TableError("identity entry %s::%s -> itself" % o)
        if o in m and m[o] != n:
            raise TableError(
                "%s::%s has two targets: %s::%s and %s::%s" % (o[0], o[1], m[o][0], m[o][1], n[0], n[1]))
        m[o] = n
    sources = set(m)
    for o, n in m.items():
        if n in sources:
            raise TableError(
                "chain: %s::%s -> %s::%s -> ... — a target must not also be a source "
                "(one hop per run, state the final home)" % (o[0], o[1], n[0], n[1]))
    return m


# ── snapshot reading: one named snapshot's nodes, trimmed to what THIS ─────
# module's own functions read (`port_ledger_laws`'s own module docstring:
# every `port_*.py` module that needs a snapshot's own node set keeps its
# OWN traversal rather than importing another module's private one).

def _snapshot_node_info(idx: "dict[str, Any]", anchor: str) -> "dict[str, dict]":
    """Every node key of one already-read `_index.json` -> its own `{sha,
    parent, kind}`, at `anchor`. `plan`'s missing-target check only wants
    the key SET; `infer_moves` additionally wants `sha`/`parent`/`kind` —
    the last to follow a moved CLASS's own methods by name."""
    out: "dict[str, dict]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            for s in m.get("symbols") or ():
                for holder, parent in [(s, None)] + [(mm, s.get("name")) for mm in s.get("methods") or ()]:
                    key = "%s::%s::%s" % (anchor, m.get("module"), holder.get("name"))
                    out[key] = {"sha": holder.get("sha"), "parent": parent, "kind": holder.get("kind")}
    return out


def infer_moves(frm: "dict[str, dict]", final: "dict[str, dict]") -> "dict[Body, Body]":
    """Home moves the two new-tree snapshots prove: same name, same body
    sha, ONE old module, ONE new module. Ported (algorithm-faithful) from
    `ob-tools@60075cb:packages/portkit/src/portkit/ledger/rehome.py`'s own
    `infer_moves`, over `_snapshot_node_info`'s own `{sha, parent, kind}`
    shape rather than upstream's richer `snapshot_nodes` payload (a
    fingerprint sha + parent + kind is everything this function reads)."""
    def bodies(nodes: "dict[str, dict]") -> "dict[Body, dict]":
        out: "dict[Body, dict]" = {}
        for k, v in nodes.items():
            _anchor, mod, name = k.split("::")
            out[(mod, name)] = v
        return out

    a, b = bodies(frm), bodies(final)
    by_a: "dict[tuple[str, str], set[str]]" = {}
    by_b: "dict[tuple[str, str], set[str]]" = {}
    for (m, n), v in a.items():
        if (m, n) not in b and v["parent"] is None:
            by_a.setdefault((n, v["sha"]), set()).add(m)
    for (m, n), v in b.items():
        if (m, n) not in a and v["parent"] is None:
            by_b.setdefault((n, v["sha"]), set()).add(m)
    out: "dict[Body, Body]" = {}
    for key in by_a.keys() & by_b.keys():
        if len(by_a[key]) == 1 and len(by_b[key]) == 1:
            out[(next(iter(by_a[key])), key[0])] = (next(iter(by_b[key])), key[0])
    # methods follow their class, by name
    for (om, on), (nm, nn) in list(out.items()):
        if a[(om, on)].get("kind") != "class":
            continue
        for (m, n), v in a.items():
            if (m == om and v["parent"] == on and (nm, n) in b and b[(nm, n)]["parent"] == nn
                    and (om, n) not in b and (m, n) not in out):
                out[(m, n)] = (nm, n)
    return out


def _resolve(table: "dict[Body, Body]", mod: str, name: str) -> "Body | None":
    hit = table.get((mod, name)) or (table.get((mod, "*")) and (table[(mod, "*")][0], name))
    return hit or None


# ── the plan (pure classification, no I/O) ──────────────────────────────────

def plan(state: "dict[str, Event]", table: "dict[Body, Body]", keys: "set[str]",
        final_anchor: str, born_new: bool) -> dict:
    """Pure classification (no I/O). Returns the changes and every category
    count. Ported (algorithm-faithful) from `ob-tools@60075cb:...rehome.py`
    (`plan`)."""
    targets = {(n[0], n[1]) for n in table.values() if n[1] != "*"}
    wild_targets = {n[0] for n in table.values() if n[1] == "*"}
    changes: "list[dict]" = []
    already: "list[str]" = []
    used: "set[Body]" = set()
    for node, ev in sorted(state.items()):
        if node.split("::", 1)[0] not in keys or ev.status not in STATUSES_WITH_DEST:
            continue
        new_to, moved, hit = [], [], False
        for t in ev.extra.get("to") or ():
            try:
                tk = parse_key(t)
            except PortingLedgerError:
                new_to.append(t)
                continue
            body = (tk.module, tk.name)
            tgt = _resolve(table, *body) if tk.is_new else None
            if tgt is not None:
                nt = "%s::%s::%s" % (final_anchor, tgt[0], tgt[1])
                new_to.append(nt)
                moved.append(t)
                used.add(tgt)
                hit = True
            else:
                if tk.anchor == final_anchor and (body in targets or tk.module in wild_targets):
                    hit = True
                    used.add(body)
                new_to.append(t)
        if moved:
            changes.append({"node": node, "ev": ev, "to": tuple(new_to), "from": tuple(moved)})
        elif hit:
            already.append(node)
    bn: "list[dict]" = []
    bn_already: "list[dict]" = []
    if born_new:
        for node, ev in sorted(state.items()):
            if ev.status != "born-new" or not node.startswith(NEW_PREFIX):
                continue
            try:
                k = parse_key(node)
            except PortingLedgerError:
                continue
            tgt = _resolve(table, k.module, k.name)
            if tgt is None:
                continue
            nn = "%s::%s::%s" % (final_anchor, tgt[0], tgt[1])
            (bn_already if nn in state else bn).append({"node": nn, "from": node, "ev": ev})
            used.add(tgt)
    return {"changes": changes, "already": already, "born_new": bn, "born_new_already": bn_already,
            "used": used}


def _moved_to(c: dict) -> "list[str]":
    old = set(c["from"])
    orig = list(c["ev"].extra.get("to") or ())
    return [n for o, n in zip(orig, c["to"]) if o in old]


def _kind(c: dict) -> str:
    """`rename` when a destination's NAME changed, else `module` (same
    name, other home)."""
    for f, t in zip(c["from"], _moved_to(c)):
        if f.rsplit("::", 1)[1] != t.rsplit("::", 1)[1]:
            return "rename"
    return "module"


def build_events(p: dict, actor: str, stamp: str, src: dict) -> "list[Event]":
    """Ported (algorithm-faithful) from `...rehome.py`'s own `build_events`,
    with ONE house addition: every event's `extra["src"]` is stamped with
    `src` (upstream's own ledger carries no `src` rule at all — see the
    module docstring)."""
    evs: "list[Event]" = []
    for c in p["changes"]:
        ev: Event = c["ev"]
        extra = dict(ev.extra)
        extra["to"] = c["to"]
        extra["rehomed_from"] = c["from"]
        extra["src"] = dict(src)
        cand = Event(ts=stamp, node=c["node"], status=ev.status, extra=extra, by=ev.by, actor=actor,
                     note="rehome %d destination(s)" % len(c["from"]))
        if not extra.get("reason") and not trivial_1to1(cand):
            extra["reason"] = "destination moved home: " + "; ".join(
                "%s -> %s" % (f.split("::", 1)[1], t.split("::", 1)[1])
                for f, t in zip(c["from"], _moved_to(c)))
        evs.append(cand)
    for b in p["born_new"]:
        ev = b["ev"]
        extra = dict(ev.extra)
        extra["rehomed_from"] = (b["from"],)
        extra["src"] = dict(src)
        evs.append(Event(ts=stamp, node=b["node"], status="born-new", extra=extra, by=ev.by, actor=actor,
                         note="rehome born-new"))
    return evs


def promote_merges(evs: "list[Event]", state: "dict[str, Event]", keys: "set[str] | None" = None,
                   stamp: str = "", actor: str = "", src: "dict | None" = None
                   ) -> "tuple[list[Event], int]":
    """`ported` -> `merged` for changed rows whose single destination is now
    shared by another SOURCE BODY (dedupe collapses N clones onto one
    home). CO-OWNERS: a `ported` row this run does not change,
    keyed under `keys`, whose single destination the rehome just made
    shared, is promoted too — the L9 law calls the WHOLE group `merged`, so
    promoting only the movers would leave the group half-red and the run
    would refuse itself. Rows under keys the run may not touch are never
    promoted. Ported (algorithm-faithful) from `...rehome.py`'s own
    `promote_merges`."""
    after = dict(state)
    for e in evs:
        after[e.node] = e
    share: "dict[str, set[str]]" = {}
    for n, ev in after.items():
        for t in ev.extra.get("to") or ():
            share.setdefault(t, set()).add(n.split("::", 1)[-1])
    out: "list[Event]" = []
    n_promoted = 0
    for e in evs:
        to = tuple(e.extra.get("to") or ())
        if e.status == "ported" and len(to) == 1 and len(share.get(to[0], ())) > 1:
            extra = dict(e.extra)
            extra.setdefault("reason", "collapsed onto one home by dedupe")
            e = dataclasses.replace(e, status="merged", extra=extra)
            n_promoted += 1
        out.append(e)
    if keys:
        moved_to = {t for e in evs for t in (e.extra.get("to") or ())}
        changed = {e.node for e in evs}
        for n, ev in sorted(state.items()):
            to = tuple(ev.extra.get("to") or ())
            if (n in changed or ev.status != "ported" or len(to) != 1 or to[0] not in moved_to
                    or n.split("::", 1)[0] not in keys or len(share.get(to[0], ())) < 2):
                continue
            extra = dict(ev.extra)
            extra.setdefault("reason", "collapsed onto one home by dedupe")
            extra["src"] = dict(src or {})
            out.append(Event(ts=stamp, node=n, status="merged", extra=extra, by=ev.by, actor=actor,
                             note="rehome co-owner of a merged destination"))
            n_promoted += 1
    return out, n_promoted


# ── CLI ──────────────────────────────────────────────────────────────────

def _abs(base: Path, p: Path) -> Path:
    return p if p.is_absolute() else base / p


def _rehome_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--table", default=None, dest="table_path")
    ap.add_argument("--from-snap", default=None, dest="from_snap_name")
    ap.add_argument("--snap", required=True, dest="snap_name")
    ap.add_argument("--key", action="append", default=[], dest="key_args",
                    help="repeatable — an 8-hex old-tree anchor allowed to change")
    ap.add_argument("--to-key", default=None, dest="to_key_assert")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--born-new", action="store_true", dest="born_new")
    ap.add_argument("--list", action="store_true", dest="list_all")


def _check_rehome_args(args: argparse.Namespace) -> None:
    if not args.table_path and not args.from_snap_name:
        raise hs_family.UsageError(
            "port rehome: give --table and/or --from-snap — nothing states what moved")
    if not args.key_args:
        raise hs_family.UsageError("port rehome: --key is required (repeatable)")
    bad = sorted(k for k in args.key_args if not _HASH8.match(k[:8]) or len(k) != 8)
    if bad:
        raise hs_family.UsageError(
            "port rehome: --key must be an 8-hex old-tree anchor, got %s" % ", ".join(bad))


def _rehome(args: argparse.Namespace) -> int:
    """`hs-code port rehome --snap NAME (--table FILE | --from-snap NAME)
    --key HASH8 [--key HASH8 ...] [--to-key HASH8] [--merge] [--born-new]
    [--list] [--apply]` — bulk-update the `to` of ledger rows
    whose destination symbol moved home WITHIN the already-ported tree.
    `--snap` names the FINAL new-tree snapshot every target destination
    must exist in; `--table`/`--from-snap` state what moved (explicit /
    inferred); `--key` (repeatable) is which OLD-tree anchor(s) may
    change. Dry-run by default (`--apply` appends); all planned lines run
    the ledger laws AND the L9 cross-ledger law over the resulting state
    before the first append (see the module docstring's exit table)."""
    _check_rehome_args(args)
    root, opts = args.ctx.root, args.opts
    (table_path, from_snap_name, snap_name, key_args, to_key_assert,
     merge, born_new, list_all) = (
        args.table_path, args.from_snap_name, args.snap_name, args.key_args,
        args.to_key_assert, args.merge, args.born_new, args.list_all)

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    keys = {k[:8] for k in key_args}

    # `--snap` is always CALLER-typed (never a batch.yaml field): a name
    # that doesn't exist under `.port/snapshots/` is `SnapshotNotFoundError`
    # (exit 1, a finding), never caught here — its own `exit_code` reaches
    # `run_main` unchanged. A snapshot that DOES exist but fails to parse is
    # still `hs_family.UsageError` (exit 2, broken state), also left to
    # propagate: the family's own exit table is the one door for both.
    snap_commit, final_idx = port_batch_reader.read_snapshot(
        port_root, snap_name, caller_named=True)
    final_anchor = port_ledger_spec.new_anchor(snap_commit)
    final_nodes = _snapshot_node_info(final_idx, final_anchor)

    if to_key_assert and to_key_assert[:8] != final_anchor[len(NEW_PREFIX):]:
        raise hs_family.RefusedError(
            "port rehome: --to-key %r is not the anchor of snapshot %r (%s)"
            % (to_key_assert, snap_name, final_anchor[len(NEW_PREFIX):]))

    try:
        explicit = check_table(load_table(_abs(port_root, Path(table_path)))) if table_path else {}
    except TableError as e:
        return Verdict.broken(str(e)).exit

    inferred: "dict[Body, Body]" = {}
    explicit_all = explicit
    if from_snap_name:
        # `--from-snap` is likewise always CALLER-typed — same reasoning as
        # `--snap` above, same one-door read.
        from_commit, from_idx = port_batch_reader.read_snapshot(
            port_root, from_snap_name, caller_named=True)
        if from_commit == snap_commit:
            raise hs_family.RefusedError(
                "port rehome: --from-snap and --snap are the same tree (%s)" % final_anchor)
        from_anchor = port_ledger_spec.new_anchor(from_commit)
        from_nodes = _snapshot_node_info(from_idx, from_anchor)
        inferred = {o: n for o, n in infer_moves(from_nodes, final_nodes).items() if o not in explicit}
        try:
            explicit_all = check_table([*explicit.items(), *inferred.items()])
        except TableError as e:
            return Verdict.broken(str(e)).exit
    table = explicit_all

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(_not_written(e.findings)).exit
    state = ledger.replay()

    have = Counter(k.split("::", 1)[0] for k in state)
    missing_keys = sorted(k for k in keys if not have.get(k))
    if missing_keys:
        return Verdict.broken(
            "port rehome: the ledger holds NO row keyed %s (anchors present: %s) — wrong --key"
            % (", ".join(missing_keys),
               ", ".join("%s (%d)" % (a, n) for a, n in sorted(have.items())) or "none")).exit

    p = plan(state, table, keys, final_anchor, born_new)

    final_bodies = {(k.split("::")[1], k.split("::")[2]) for k in final_nodes}
    missing = sorted({t for c in p["changes"] for t in c["to"]
                      if t.startswith(final_anchor + "::")
                      and tuple(t.split("::")[1:]) not in final_bodies}
                     | {"%s::%s::%s" % (final_anchor, m, n) for (m, n) in p["used"] if (m, n) not in final_bodies})
    if missing:
        raise hs_family.RefusedError(
            "port rehome: %d target destination(s) do not exist in %r (%s); nothing written — "
            "the table (or --snap) is wrong: %s"
            % (len(missing), snap_name, final_anchor, "; ".join(missing[:20])))

    src = {"id": _SRC_ID, "sha": snap_commit}
    evs = build_events(p, resolve_actor(), now_ts(), src)
    promoted = 0
    if merge:
        evs, promoted = promote_merges(evs, state, keys, now_ts(), resolve_actor(), src)

    dup = [e.node for e in evs if len(set(e.extra.get("to") or ())) != len(e.extra.get("to") or ())]
    if dup:
        raise hs_family.RefusedError(
            "port rehome: %d row(s) would list one destination twice (e.g. %r); nothing written"
            % (len(dup), dup[0]))

    write_all(ledger, evs, "rehome", apply=False)

    after_state = dict(state)
    for ev in evs:
        after_state[ev.node] = ev
    # a finding is NEW when its (node, shape word) pair was not already red
    # before this run — ported verbatim from upstream's own comparison key.
    before_l9 = {(f.node, f.msg.split()[0]) for f in port_ledger_laws.l9_cross_findings(state)}
    new_l9 = [f for f in port_ledger_laws.l9_cross_findings(after_state)
              if (f.node, f.msg.split()[0]) not in before_l9]
    if new_l9:
        raise hs_family.RefusedError(
            "port rehome: the rehome creates %d L9 finding(s) (two symbols on one destination "
            "without `merged`, or a merged group half re-pointed); nothing written: %s"
            % (len(new_l9), "; ".join("%s %s: %s" % (f.law, f.node, f.msg) for f in new_l9[:20])))

    changes = p["changes"]
    by_status = Counter(e.status for e in evs if e.extra.get("rehomed_from") and e.status != "born-new")
    by_key = Counter(c["node"].split("::", 1)[0] for c in changes)
    by_kind = Counter(_kind(c) for c in changes)
    n_dest = sum(len(c["from"]) for c in changes)
    still_old = sorted({"%s::%s" % (m, n) for (m, n) in table if n != "*" and (m, n) in final_bodies})
    counts = {"rows-rehomed": len(changes), "destinations-changed": n_dest,
              "already-rehomed": len(p["already"]), "born-new-rekeyed": len(p["born_new"]),
              "born-new-already": len(p["born_new_already"]),
              "table-unreferenced": sum(1 for o, n in table.items() if o[1] != "*" and n not in p["used"]),
              "old-still-defined": len(still_old), "promoted-to-merged": promoted}

    apply = bool(getattr(opts, "apply", False))
    want_json = bool(getattr(opts, "json", False))
    data: "dict[str, Any]" = {
        "schema": "port.rehome/1", "final_anchor": final_anchor, "keys": sorted(keys),
        "counts": counts, "by_status": dict(by_status), "by_key": dict(by_key),
        "by_kind": dict(by_kind), "inferred": len(inferred), "dry_run": not apply,
    }

    if not want_json:
        print("ledger  : %s" % ledger.path)
        print("snap    : %s  anchor %s  (%d node(s))" % (snap_name, final_anchor, len(final_nodes)))
        print("table   : %d explicit + %d inferred entr(y/ies)  keys %s"
             % (len(explicit), len(inferred), ", ".join(sorted(keys))))
        print()
        print("rows rehomed (dest changes)    : %d   (%d destination(s))" % (len(changes), n_dest))
        for label, ctr in (("status", by_status), ("key", by_key), ("kind", by_kind)):
            for k, n in sorted(ctr.items()):
                print("  by %-7s%-14s: %d" % (label, k, n))
        if merge:
            print("  promoted ported -> merged    : %d" % promoted)
        print("already rehomed                : %d" % len(p["already"]))
        if born_new:
            print("born-new re-keyed              : %d   (already %d)"
                 % (len(p["born_new"]), len(p["born_new_already"])))
        print("table entries no row points at : %d" % counts["table-unreferenced"])
        print("old home STILL defined in snap : %d   (a copy, not a move? a re-export is invisible here)"
             % len(still_old))
        final_status = {e.node: e.status for e in evs}
        shown = changes if list_all else changes[:_SHOWN]
        for c in shown:
            for f, t in zip(c["from"], _moved_to(c)):
                print("  [%s] %s: %s -> %s"
                     % (final_status[c["node"]], c["node"], f.split("::", 1)[1], t.split("::", 1)[1]))
        if not list_all and len(changes) > _SHOWN:
            print("  ... %d more (--list)" % (len(changes) - _SHOWN))
        for s_ in still_old[:_SHOWN]:
            print("  [old-still-defined] %s" % s_)

    if not evs:
        touched = bool(p["already"] or p["born_new_already"])
        if not want_json:
            print("\nnothing to rehome" + (" — every row is already at its new home" if touched else ""))
        verdict = Verdict.ok(data) if touched else Verdict.red(data)
    elif not apply:
        if not want_json:
            print("\ndry-run: %d row(s) valid, nothing written (--apply appends)" % len(evs))
        verdict = Verdict.ok(data)
    else:
        write_all(ledger, evs, "rehome", apply=True)
        if not want_json:
            print("\nwritten: %d line(s) -> %s" % (len(evs), ledger.path))
        verdict = Verdict.ok(data)

    if want_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    return int(verdict.exit)


REHOME = Command(add_args=_rehome_args, run=_rehome)
