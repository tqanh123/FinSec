"""port_ledger_laws.py — the port ledger's twelve LEDGER-LEVEL laws (L0-L11)
and the `collect()` measurement that runs all of them, as one FLAT module
under `harness/scripts/` (sibling of `port_ledger_reader.py`/`port_ledger_
spec.py`/`port_ledger_writer.py`, matching the `port_*.py` glob, coverage
group `port-port`).

Ported (algorithm-faithful) from `ob-tools@60075cb`:
  * `packages/codemap/src/codemap/ledger/laws.py` (170 lines) — the kernel
    law-engine SHAPES (event law / ledger scan / `Finding` pair). Not
    reused as a second engine: `port_ledger_spec.py`'s own docstring
    explains the porting domain runs `PORTING_EVENT_LAWS` INSTEAD of the
    kernel's generic laws (two engines firing on one condition would file a
    finding under two codes) — only the CONCEPT crosses here.
  * the `collect` half of `packages/portkit/src/portkit/ledger/
    check_ledger.py` (551 lines) — the porting domain's OWN twelve laws.
    `run`'s printing/argparse half is not ported (see `THE ONE REAL
    CALLER` below).

THE TWELVE LAWS (upstream's own names, `check_ledger.py:64-77`)
    L0   a ledger line is BROKEN (bad JSON, missing field)
    L1   an OLD node has no status line at all
    L2   status / blocker prefix / `by` OUTSIDE the closed set
    L3   an ILLEGAL transition under the table
    L4   `blocked` with empty `blocked_by`, or the reverse
    L5   `node:`/`gate:`/`decision:` pointing at something that DOES NOT EXIST
    L6   a blocker CYCLE over `node:`
    L7   a NEW-tree node neither a destination nor `born-new`
    L8   a MISSING REASON where one is required
    L9   mapping SHAPE not matching the status
    L10  a destination that has DRIFTED in the new tree since the line was written
    L11  a `born-new` declaration whose node exists in NO new-tree snapshot

L0 IS A SEPARATE TIER, NOT A `collect()` FINDING
    Every other law is a soft finding — `collect()` returns it, a caller
    decides what to do. L0 is the one law `Ledger.load_checked` already
    enforces as a hard refusal (`BrokenLedgerError`, matching `append_event`'s
    own `broken_ledger_refusal`): a ledger carrying ANY broken line never
    reaches `collect()` at all. This mirrors the plan's own "L0 is a
    separate never-off tier" ruling — L0 is not merely un-turn-off-able
    among equals, it is not even the same KIND of check as L1-L11.

L2/L4/L7(origin)/L8/L9(per-event shape) ALREADY RUN — `validate_event`
(`port_ledger_spec.py`) is the exact per-event law engine `append_event`
runs on write; `collect()` below calls it over EVERY event on READ and
folds the pairs in as `Finding`s, never a second per-event implementation
that could drift from what the write door already enforces.

WHAT DIFFERS FROM UPSTREAM, BY DESIGN (recorded, not silently dropped):
  * `--key` selection: upstream lets a caller pick an arbitrary OLD anchor.
    This harness's ledger is per-REPO (shared across every batch, see
    `port_ledger_reader.py`'s own docstring) and a BATCH's own
    `frozen_snapshot` is the one anchor that means anything to it — the OLD
    key is always `port_batch_reader.pinned_frozen_commit(...)[:8]`, never
    a caller-typed override. `collect()` takes a `Batch` (+ its resolved
    source), not a bag of paths.
  * `--newmap-root`: upstream scans a directory of dated new-tree snapshots
    by commit hash for L10/L11. This harness keeps EVERY named snapshot
    under `.port/snapshots/*` (a batch's own `new_snapshot` just names
    ONE) — `port_batch_reader.snapshots_dir(port_root)` IS the newmap-root
    equivalent; no second directory convention is needed.
  * `gate:` blockers: upstream checks existence against a `--probes` dir.
    This harness's `.port/` domain has no probes-directory concept (git
    grep, 0 hits anywhere under `harness/scripts/port_*.py`) — `gate:`
    reads as UNCHECKABLE (`None`), the same honest "tool is blind here"
    `BlockerContext.cleared` already returns for `test:`/`external:`, never
    a silent `False`.
  * `DECISION_ID_RE` is NOT ported — `decision_register.is_readable_id`
    (this harness's own DEC grammar, both the current and the retired
    spelling) replaces it; a `decision:` blocker's id is well-formed when
    THAT function says so. Existence is checked against
    `decision_register.parse_decisions` (this harness ALWAYS has a
    register — unlike upstream's optional `--decisions` dir, `decision:`
    is always checkable here, never `None`).
  * Upstream's own verdict-provenance counting, `--top`/`--list` printing,
    `run()`'s argparse/CLI surface: not ported — `collect()` is the ONE
    real half this tree needs (the `ledger` rule kind and `check-ledger`
    verb both call it directly), matching the brief's own scope.

THE ONE REAL CALLER: `codegraph_port_check_ledger_verbs.CHECK_LEDGER`
and the `ledger` rule kind (`codegraph/_core/rules/kinds/ledger.py`) both
call `collect()` — the SAME measurement, never re-derived by either door.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import codegraph_snapshot_reader
import decision_register
import port_batch_reader
from port_ledger_reader import Event
from port_ledger_spec import (
    BLOCK_PREFIXES, NEW_PREFIX, PortingLedgerError, SPEC, STATUSES_DONE,
    new_anchor, parse_key, validate_event,
)
from port_ledger_writer import Ledger

#: Upstream's own law-code -> one-line intent table, ported verbatim
#: (`check_ledger.py:64-77`) — the same names the batch's own baselines and
#: `hs-code check` reports would speak.
LAWS: "dict[str, str]" = {
    "L0": "broken ledger line",
    "L1": "old node with no line",
    "L2": "status / prefix / by outside the closed set",
    "L3": "illegal transition",
    "L4": "blocked <-> blocked_by mismatch",
    "L5": "blocker points at something that does not exist",
    "L6": "blocker cycle",
    "L7": "new-tree node not declared",
    "L8": "missing reason where required",
    "L9": "mapping shape does not match status",
    "L10": "destination drifted in the new tree",
    "L11": "born-new declared, absent from every new-tree snapshot",
}

#: The one law that can never be turned off, under ANY reason — checked
#: independently of `rules.off`'s own four-field completeness law
#: (`port_config._REQUIRED_OFF_FIELDS`), the same "never" `port_config.py`
#: already enforces for `port.L0` (a distinct tier from `core: true`,
#: which CAN be forced off with a reason — `hs_family/gate_registry.py`).
NEVER_OFF_LAW = "L0"


@dataclass(frozen=True)
class Finding:
    """One law's own verdict: which law, which node, why. Upstream's own
    `check_ledger.Finding` shape (`law`, `node`, `msg`) — a 3-field record,
    not the 2-field `(code, message)` pair `port_ledger_spec.Finding`
    already names for a SINGLE event's own per-event laws (that type has no
    room for `node` because the caller already knows which event it asked
    about; a whole-ledger scan does not)."""

    law: str
    node: str
    msg: str


@dataclass
class LedgerScan:
    """What `collect()` measured, before any caller decides what to do with
    it — mirrors upstream's own split (`check_ledger.py`'s `LedgerScan`):
    the `ledger` rule kind and `check-ledger` verb both read the SAME
    measurement, neither re-derives it from the other's printed text."""

    findings: "list[Finding]" = field(default_factory=list)
    n_events: int = 0
    old_key: "str | None" = None
    old_key_rows: int = 0
    new_key: "str | None" = None
    new_ran: bool = False
    uncheckable: int = 0
    pending_decisions: "dict[str, int]" = field(default_factory=dict)
    drift_lost: int = 0


# ── blocker resolution: node:/decision: checkable here, gate:/test:/external: not ──

class BlockerContext:
    """Where a `blocked_by` entry resolves. Ported (algorithm-faithful,
    trimmed to what this tree can actually check — see the module
    docstring's WHAT DIFFERS section) from `ob-tools@60075cb:packages/
    portkit/src/portkit/ledger/check_ledger.py:256` /
    `ledger_spec.py`'s own `BlockerContext`.

    `node:` needs only the replayed state; `decision:` needs the decision
    register (`decisions_root`, defaulting to `port_root` — the port
    domain has no separate decisions-directory convention of its own);
    `gate:`/`test:`/`external:` are uncheckable in this tree — `None`,
    never a silent `False`."""

    def __init__(self, state: "Mapping[str, Event]", port_root: Path,
                decisions_root: "Path | None" = None) -> None:
        self.state = state
        self.port_root = Path(port_root)
        self.decisions_root = Path(decisions_root) if decisions_root else self.port_root
        self._decision_ids: "set[str] | None" = None

    def _known_decision_ids(self) -> "set[str]":
        if self._decision_ids is None:
            self._decision_ids = {
                str(r.get("id")) for r in decision_register.parse_decisions(self.decisions_root)}
        return self._decision_ids

    def cleared(self, b: str) -> "bool | None":
        """Is this blocker cleared? `None` = CANNOT CHECK — never read as
        'no'."""
        kind, _, body = b.partition(":")
        body = body.strip()
        if kind == "node":
            ev = self.state.get(body)
            return ev is not None and ev.status in STATUSES_DONE
        if kind == "decision":
            return body in self._known_decision_ids()
        return None        # gate: / test: / external: — uncheckable here


# ── snapshot reading: one named snapshot's nodes, keyed like the ledger ─────
#
# `codegraph_port_ledger_verbs._snapshot_node_keys` (the key SET, for
# `init`'s bulk declare) and `codegraph_port_judge_verbs._snapshot_nodes` /
# `_snapshot_node_shas` (the richer per-node payload, for `judge-nodes`)
# are the established precedent: every `port_*.py` module that needs a
# snapshot's own node set keeps its OWN traversal, trimmed to the fields
# ITS laws read, citing the same upstream function rather than importing
# each other's private helpers (each is `_`-prefixed — no caller outside
# its own module). L1/L7/L9/L10/L11 below only ever compare a `sha` or a
# bare key, never `path`/`lines`/`parent` — so this module's own trim
# matches `_snapshot_node_shas`'s shape, not the richer one.

def _snapshot_node_shas(idx: "dict[str, Any]", anchor: str) -> "dict[str, str]":
    """Every node key of one already-read `_index.json` -> its own symbol
    `sha`, keyed exactly like a ledger node (`<anchor>::module::name`) —
    ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/
    src/portkit/ledger/ledger_io.py:81-95` (`snapshot_nodes`), trimmed to
    the one field this module's laws read (mirrors
    `codegraph_port_judge_verbs._snapshot_node_shas`'s own trim of the
    same upstream function). Takes an already-parsed index + a
    caller-resolved anchor rather than reading the directory and deriving
    the anchor itself — `port_batch_reader.snapshot_commit`/
    `new_batch_anchor` already own anchor resolution, one door, not two."""
    out: "dict[str, str]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            for s in m.get("symbols") or ():
                for holder in [s] + list(s.get("methods") or ()):
                    key = "%s::%s::%s" % (anchor, m.get("module"), holder.get("name"))
                    out[key] = holder.get("sha")
    return out


def _body_of(key: str) -> str:
    """`module::name` of a node key — the part TWO nodes at different
    anchors (a row carried forward under `carry-forward`, or the same
    symbol before/after a rebuild) share. Never the full key, which always
    differs by anchor."""
    return key.split("::", 1)[1] if "::" in key else key


def _anchor_of(key: str) -> str:
    return key.split("::", 1)[0]


# ── L9 (cross-ledger): mapping shape vs status, across the WHOLE ledger ────

def l9_cross_findings(state: "Mapping[str, Event]") -> "list[Finding]":
    """L9's whole-ledger half — ported (algorithm-faithful) from
    `ob-tools@60075cb:packages/portkit/src/portkit/ledger/
    check_ledger.py:193`'s own `l9_findings`. Sources are counted by
    `module::name` BODY, not by full key: a row carried onto the next
    freeze (`port carry-forward`) is the SAME symbol under another anchor
    and must not read as a second node sharing its own destination.
    Distinct from `destination_shape_law`'s own per-event L9 (shape of ONE
    line) — this is the cross-line check upstream keeps as a separate
    function for the same reason."""
    dest_index: "dict[str, list[str]]" = {}
    for node, ev in state.items():
        for t in ev.extra.get("to") or ():
            dest_index.setdefault(t, []).append(node)
    src_bodies = {t: {_body_of(n) for n in nodes} for t, nodes in dest_index.items()}
    out: "list[Finding]" = []
    for k, ev in sorted(state.items()):
        to = tuple(ev.extra.get("to") or ())
        n_src = len(src_bodies.get(to[0], ())) if len(to) == 1 else 0
        if ev.status == "ported" and len(to) == 1 and n_src > 1:
            out.append(Finding("L9", k, "`ported` but destination %s is shared by "
                               "%d other node(s) — that is `merged`" % (to[0], n_src - 1)))
        if ev.status == "merged" and len(to) == 1 and n_src < 2:
            out.append(Finding("L9", k, "`merged` but only this node points at "
                               "%s — that is `ported`" % to[0]))
    return out


# ── L6: blocker cycles over node: ───────────────────────────────────────────

def find_block_cycles(state: "Mapping[str, Event]") -> "list[list[str]]":
    """Cycles over `node:` edges, three-colour DFS, each cycle reported
    once. Ported (algorithm-faithful) from `ob-tools@60075cb:packages/
    portkit/src/portkit/ledger/check_ledger.py:124`. Edges into bodies that
    are NOT ledger nodes are skipped — a blocker pointing at a missing key
    is L5's finding, not a phantom cycle member."""
    edges: "dict[str, list[str]]" = {}
    for k, ev in state.items():
        blockers = ev.extra.get("blocked_by") or ()
        edges[k] = [b.split(":", 1)[1].strip() for b in blockers if b.startswith("node:")]
    WHITE, GREY, BLACK = 0, 1, 2
    color: "dict[str, int]" = defaultdict(int)
    stack: "list[str]" = []
    cycles: "list[list[str]]" = []
    seen: "set[frozenset]" = set()

    def dfs(u: str) -> None:
        color[u] = GREY
        stack.append(u)
        for v in edges.get(u, ()):
            if v not in state:
                continue
            if color[v] == GREY:
                cyc = stack[stack.index(v):] + [v]
                sig = frozenset(cyc)
                if sig not in seen:
                    seen.add(sig)
                    cycles.append(cyc)
            elif color[v] == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    for k in sorted(state):
        if color[k] == WHITE:
            dfs(k)
    return cycles


# ── the measurement ──────────────────────────────────────────────────────────

def collect(ledger: Ledger, port_root: Path, batch: "port_batch_reader.Batch",
           source: "Mapping[str, Any]", *,
           decisions_root: "Path | None" = None) -> LedgerScan:
    """Run L1-L11 against `ledger` (already loaded through `Ledger.
    load_checked` by the caller — L0 is that call's own refusal, never a
    `collect()` finding, see the module docstring). Raises
    `hs_family.UsageError` when `batch` has no `frozen_snapshot` (the OLD
    anchor every one of L1/L7/L9-cross/L10/L11 needs) or the snapshot
    itself cannot be read; `batch.new_snapshot` is OPTIONAL like upstream's
    own `--new` — its absence means L7/L10/L11 do not run, recorded via
    `LedgerScan.new_ran`, never folded into a silent green."""
    import hs_family

    state = ledger.replay()
    findings: "list[Finding]" = []

    # ── per-event laws (L2/L4/L7-origin/L8/L9-shape) — the SAME engine
    # append_event already runs on write, called here on READ.
    for node, ev in sorted(state.items()):
        for code, msg in validate_event(ev):
            findings.append(Finding(code, node, msg))

    # ── L3 — illegal transitions over the FULL history ──────────────────
    for node in sorted({ev.node for ev in ledger.events}):
        prev_status: "str | None" = None
        for prev, ev in ledger.transitions_of(node):
            if prev_status is not None and ev.status != prev_status \
                    and not SPEC.may_transition(prev_status, ev.status):
                findings.append(Finding(
                    "L3", node, "%s: %r -> %r is not in the transition table"
                    % (ev.ts, prev_status, ev.status)))
            prev_status = ev.status

    if not batch.frozen_snapshot:
        raise hs_family.UsageError(
            "port check-ledger: batch %r has no `frozen_snapshot` declared "
            "— L1/L7/L9/L10/L11 need the OLD-tree anchor it names" % batch.name)
    old_commit, old_idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    old_key = old_commit[:8]
    old_nodes = _snapshot_node_shas(old_idx, old_key)

    kstate = {k: ev for k, ev in state.items() if _anchor_of(k) == old_key}

    # ── L1 — an OLD node with no status line at all ──────────────────────
    for k in sorted(set(old_nodes) - set(kstate)):
        findings.append(Finding("L1", k, "old node has no status line at all"))

    # ── L5 — blockers pointing at something that does not exist ─────────
    ctx = BlockerContext(state, port_root, decisions_root)
    uncheckable = 0
    pending_decisions: "dict[str, int]" = {}
    for k, ev in sorted(state.items()):
        for b in ev.extra.get("blocked_by") or ():
            cleared = ctx.cleared(b)
            if cleared is None:
                uncheckable += 1
                continue
            kind, _, body = b.partition(":")
            body = body.strip()
            if kind == "node" and body not in state and body not in old_nodes:
                findings.append(Finding("L5", k, "`node:%s` — no node carries that key" % body))
            elif kind == "decision":
                if not decision_register.is_readable_id(body):
                    findings.append(Finding(
                        "L5", k, "`decision:%s` — bad id shape; such an id never "
                        "matches a decision record" % body))
                elif cleared is False:
                    pending_decisions[body] = pending_decisions.get(body, 0) + 1

    # ── L6 — blocker cycles ──────────────────────────────────────────────
    for cyc in find_block_cycles(state):
        findings.append(Finding("L6", cyc[0], "blocker cycle: " + " -> ".join(cyc)))

    # ── L9 (cross-ledger half) ────────────────────────────────────────────
    findings.extend(l9_cross_findings(state))

    new_key: "str | None" = None
    new_ran = False
    drift_lost = 0
    if batch.new_snapshot:
        new_ran = True
        new_commit, new_idx = port_batch_reader.read_snapshot(port_root, batch.new_snapshot)
        new_key = new_anchor(new_commit)
        new_nodes = _snapshot_node_shas(new_idx, new_key)
        new_bodies = {_body_of(k) for k in new_nodes}

        # ── L7 — new-tree nodes neither claimed nor declared ─────────────
        kdest_index: "dict[str, list[str]]" = {}
        for node, ev in kstate.items():
            for t in ev.extra.get("to") or ():
                kdest_index.setdefault(t, []).append(node)
        claimed = {_body_of(k) for k in kdest_index}
        declared = {_body_of(k) for k, ev in state.items() if ev.status == "born-new"}
        for k in sorted(new_nodes):
            if _body_of(k) not in claimed and _body_of(k) not in declared:
                findings.append(Finding(
                    "L7", k, "NEW-tree node: no old node's destination, and no "
                    "`born-new` line"))
        for k, srcs in sorted(kdest_index.items()):
            if _body_of(k) not in new_bodies:
                findings.append(Finding(
                    "L7", k, "destination absent from the new tree; %d old "
                    "node(s) point at it" % len(srcs)))

        # ── L10 — destination DRIFT since the line was written ───────────
        # `snapshots_dir(port_root)` is the newmap-root equivalent (every
        # named snapshot this port root has ever kept, not only the
        # batch's OWN two names) — see the module docstring.
        by_hash: "dict[str, Path] | None" = None
        for k, ev in sorted(kstate.items()):
            for t in ev.extra.get("to") or ():
                try:
                    tk = parse_key(t)
                except PortingLedgerError:
                    continue                       # L2 already has the bad shape
                if not tk.is_new:
                    continue
                anc = tk.anchor[len(NEW_PREFIX):] if tk.anchor.startswith(NEW_PREFIX) else None
                if anc is None or anc == new_key[len(NEW_PREFIX):]:
                    continue                        # legacy key, or written at current
                if by_hash is None:
                    by_hash = _index_snapshots_by_hash(port_root)
                dest_dir = by_hash.get(anc)
                if dest_dir is None:
                    drift_lost += 1
                    continue
                # `read_snapshot_dir`, not `read_snapshot`: `dest_dir` came from
                # `_index_snapshots_by_hash`'s hash lookup, never a caller- or
                # batch-declared NAME, so there is no name to resolve — only a
                # directory to read in the format the registry records for it.
                then_idx = port_batch_reader.read_snapshot_dir(port_root, dest_dir)
                then_shas = _snapshot_node_shas(then_idx, "new@" + anc)
                then_sha = then_shas.get(t)
                now_key = next((kk for kk in new_nodes if _body_of(kk) == _body_of(t)), None)
                now_sha = new_nodes.get(now_key) if now_key else None
                if then_sha and now_sha and then_sha != now_sha:
                    findings.append(Finding(
                        "L10", k, "destination %s DRIFTED: written at `%s` (sha %s), "
                        "the new tree is now at `%s` (sha %s)"
                        % (_body_of(t), anc, then_sha,
                           new_key[len(NEW_PREFIX):], now_sha)))

        # ── L11 — born-new declared, absent from every new-tree snapshot ──
        for k, ev in sorted(state.items()):
            if ev.status != "born-new" or not k.startswith(NEW_PREFIX):
                continue
            body = _body_of(k)
            if body in new_bodies:
                continue
            findings.append(Finding(
                "L11", k, "born-new but `%s` exists in NO new-tree snapshot under "
                "%s — a declaration no tree honours; fix the module or drop the "
                "line (with a reason)" % (body, port_batch_reader.snapshots_dir(port_root))))

    return LedgerScan(
        findings=findings, n_events=len(ledger.events), old_key=old_key,
        old_key_rows=len(kstate), new_key=new_key, new_ran=new_ran,
        uncheckable=uncheckable, pending_decisions=pending_decisions,
        drift_lost=drift_lost)


def _index_snapshots_by_hash(port_root: Path) -> "dict[str, Path]":
    """Every named snapshot under `.port/snapshots/*`, indexed by its OWN
    `_index.json` commit (first 8 hex) — the newmap-root equivalent (see
    the module docstring). A dir whose `_index.json` cannot be read is
    skipped, never raised on: L10's own `drift_lost` bucket already exists
    for exactly this "evidence gone" case. Deliberately NOT routed through
    `port_batch_reader.read_snapshot`: that door raises on an unreadable
    index (one bad snapshot would abort the whole L10 pass), while this
    scan's whole point is to walk EVERY name once and quietly drop the
    ones that don't parse — a second, separate reader by design, not a
    second copy of the door's own error mapping."""
    out: "dict[str, Path]" = {}
    d = port_batch_reader.snapshots_dir(port_root)
    if not d.is_dir():
        return out
    for sub in sorted(p for p in d.iterdir() if p.is_dir()):
        try:
            idx = port_batch_reader.read_snapshot_dir(port_root, sub)
        except codegraph_snapshot_reader.SnapshotIndexError:
            continue
        commit = idx.get("commit")
        if isinstance(commit, str) and len(commit) >= 8:
            out.setdefault(commit[:8], sub)
    return out
