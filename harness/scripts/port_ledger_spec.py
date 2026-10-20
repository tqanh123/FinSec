"""port_ledger_spec.py — the port domain's ledger state table + the kernel
shapes it is declared against, as one FLAT module under `harness/scripts/`
(sibling of `port_ledger_reader.py`, same reason: the write door this table
feeds is a bare, dot-free `hs-code-registry.d` module name).

Ported (algorithm-faithful, not written from memory) from
`ob-tools@60075cb`:
  * `packages/codemap/src/codemap/ledger/spec.py` — `LedgerSpec`/
    `BlockerRules`/`LedgerError`: the KERNEL shapes any ledger family
    declares its own state table against.
  * `packages/portkit/src/portkit/ledger/ledger_spec.py` — the porting
    DOMAIN's instance: eight statuses, the transition table, the event
    laws, and the node-key grammar.

WHAT IS NOT HERE, ON PURPOSE (no-orphan discipline, judged at function
level): only what the single-node `map` write path actually calls made the
crossing. Left behind because nothing on that path reaches them:
  * `BlockerContext` (upstream `ledger_spec.py`'s own end) — resolves
    `decision:`/`gate:` blockers against a DEC/probe directory; the write
    path here never clears a blocker, only records it. `hs-code port
    check-ledger` and the `ledger` rule kind (`port_ledger_laws.
    BlockerContext`) own that resolution now, against `decision_register`
    — see that module's own docstring for what changed crossing over.
  * `new_anchor` / `NEW_ANCHOR_LEGACY`'s sibling `_anchor_batch` machinery,
    `test_owner_of` — batch anchoring and the test-owner reader are
    `init`/batch-verb concerns, none of them called by `append_event` or
    anything it calls (`STATUS_ORDER` DID cross, once `hs-code port status`
    became this module's second real caller — see below). `DECISION_ID_RE`
    stays un-ported for good, not merely un-crossed-yet: `check-ledger`'s
    own `decision:` shape check goes through `decision_register.
    is_readable_id` instead (the house DEC grammar, current and retired
    spelling both), never a private copy of upstream's regex.
  * The kernel's own `laws.py` engine (`run_event_laws`, `transition_
    findings`, the `L-STATUS`/`L-*` codes) — the porting domain runs its
    OWN `PORTING_EVENT_LAWS` INSTEAD of the kernel's generic ones (upstream
    says so directly: two engines firing on the same condition would file
    one finding under two codes), so the kernel law engine has no caller on
    this path at all. Only the `Finding` TYPE crosses (a 1-line alias
    below), never the engine.

WHAT IT CANNOT SEE (inherited from upstream, unchanged):
  * The ledger does not know the tree: `ported` naming a destination that
    does not exist in the new tree is a snapshot-vs-ledger question, not
    this table's.
  * `node_pattern` validates SHAPE only. Two different well-shaped keys are
    still two different nodes; identity is the caller's string.
  * Deletion or an in-place edit is invisible to these laws. It becomes visible
    only BEFORE a checkpoint (`port_ledger_writer.write_checkpoint`, re-hashed by
    `port_ledger_reader.read_ledger`): history after the last checkpoint is
    still unguarded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Tuple, TypeGuard

from port_ledger_reader import Event

# ── kernel shapes (`codemap/ledger/spec.py`) ────────────────────────────────


class LedgerError(Exception):
    """The ledger cannot be used honestly: bad spec, bad line, or bad write."""


@dataclass(frozen=True)
class BlockerRules:
    """Blocking semantics for ledgers that use them (the port ledger is one).

    `blocked_statuses`   statuses that REQUIRE a non-empty blocker list
    `prefixes`           closed set of blocker prefixes (`node:`, `decision:` ...)
    `reference_prefixes` subset whose body IS another node key
    """

    blocked_statuses: "frozenset[str]"
    prefixes: "frozenset[str]"
    reference_prefixes: "frozenset[str]" = frozenset()

    def __post_init__(self) -> None:
        if not self.blocked_statuses:
            raise LedgerError("BlockerRules with no blocked statuses: declare None instead")
        bad = self.reference_prefixes - self.prefixes
        if bad:
            raise LedgerError(
                "reference prefixes not in the closed prefix set: %s" % sorted(bad))


@dataclass(frozen=True)
class LedgerSpec:
    """The state table of ONE ledger family, declared by its owning domain.
    The kernel enforces whatever closed set it is handed; the domain
    decides what the sets ARE."""

    statuses: "frozenset[str]"
    transitions: "Mapping[str, frozenset[str]]"
    node_pattern: "re.Pattern | None" = None
    blockers: "BlockerRules | None" = None
    name: str = ""

    def __post_init__(self) -> None:
        if not self.statuses:
            raise LedgerError("a ledger spec with no statuses is not a state machine")
        unknown_from = set(self.transitions) - set(self.statuses)
        if unknown_from:
            raise LedgerError(
                "transitions declare source statuses outside the closed set: %s"
                % sorted(unknown_from))
        for src, dsts in self.transitions.items():
            unknown_to = set(dsts) - set(self.statuses)
            if unknown_to:
                raise LedgerError(
                    "transitions from %r reach statuses outside the closed set: %s"
                    % (src, sorted(unknown_to)))
        if self.blockers is not None:
            unblocked = set(self.blockers.blocked_statuses) - set(self.statuses)
            if unblocked:
                raise LedgerError(
                    "blocked statuses outside the closed set: %s" % sorted(unblocked))

    def may_transition(self, prev: str, nxt: str) -> bool:
        """Is `prev -> nxt` legal? A first event has no `prev` and is always free."""
        return nxt in self.transitions.get(prev, frozenset())


#: (law code, message). A 1-line local alias — the write path never runs the
#: kernel's `laws.py` engine (see the module docstring), so only the TYPE
#: upstream `Finding` names crosses, never the engine that produces it.
Finding = Tuple[str, str]


# ── the porting domain's own table (`portkit/ledger/ledger_spec.py`) ───────

STATUSES: "frozenset[str]" = frozenset({
    "unported",     # known to exist, nobody has touched it
    "in-progress",  # someone is on it; carries NO destination
    "ported",       # 1 -> 1
    "split",        # 1 -> many
    "merged",       # many -> 1 (this line is one of the "many")
    "dropped",      # deliberately not carried over; reason ALWAYS required
    "born-new",     # node of the NEW tree, from no old node
    "blocked",      # impeded; blocked_by must be non-empty
})

BLOCK_PREFIXES: "frozenset[str]" = frozenset({
    "node:", "decision:", "gate:", "test:", "external:",
})

#: Statuses allowed to carry destinations (the "FIVE MAPPING SHAPES" table).
STATUSES_WITH_DEST: "frozenset[str]" = frozenset({"ported", "split", "merged"})

#: A FROM row settled enough to judge/carry against — ported verbatim from
#: `ob-tools@60075cb:packages/portkit/src/portkit/ledger/ledger_spec.
#: py:62` (`STATUSES_DONE`). `judge-nodes`'s "changed" cause refuses to
#: promote a row still `unported`/`in-progress`/`blocked` — that is not a
#: judged destination, only a plan.
STATUSES_DONE: "frozenset[str]" = frozenset({"ported", "split", "merged", "dropped"})

#: Legal transitions. A missing edge is a RED gate, never a widened one.
TRANSITIONS: "dict[str, frozenset[str]]" = {
    "unported":    frozenset({"in-progress", "blocked", "ported", "split", "merged", "dropped"}),
    "in-progress": frozenset({"unported", "blocked", "ported", "split", "merged", "dropped"}),
    "blocked":     frozenset({"unported", "in-progress", "dropped"}),
    "ported":      frozenset({"in-progress", "blocked", "split", "merged", "dropped"}),
    "split":       frozenset({"in-progress", "blocked", "ported", "merged", "dropped"}),
    "merged":      frozenset({"in-progress", "blocked", "ported", "split", "dropped"}),
    "dropped":     frozenset({"unported", "in-progress"}),
    "born-new":    frozenset({"ported", "dropped"}),
}

NEW_PREFIX = "new@"


def is_sha40(value: object) -> "TypeGuard[str]":
    """A full 40-hex commit SHA and nothing else (a trailing newline included)."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None

#: The new-tree anchor of the FIRST batch: bare `new`, no commit. Readable
#: forever, writable never.
NEW_ANCHOR_LEGACY = "new"

#: The test-owner dist column: on a test node's row, the dist whose tests
#: dir owns the test's landing. Rows written before the column exist read
#: as EMPTY — never as "no owner decided".
TEST_OWNER_KEY = "test_owner"

#: Print order for `hs-code port status` — not alphabetical. It is a story:
#: how much is left, how much is happening, how much is done, and how much
#: was deliberately dropped. Ported verbatim from `ob-tools@60075cb:
#: packages/portkit/src/portkit/ledger/ledger_spec.py:103`.
STATUS_ORDER: "list[str]" = [
    "unported", "blocked", "in-progress", "ported", "split",
    "merged", "dropped", "born-new",
]

KEY_RE = re.compile(
    r"^(?P<anchor>[0-9a-f]{8}|new|new@[0-9a-f]{8})::"
    r"(?P<module>[A-Za-z_][\w.]*)::(?P<name>[\w.]+)$")


class PortingLedgerError(Exception):
    """The port ledger cannot be used honestly (bad key, bad spec use)."""


class Key:
    __slots__ = ("anchor", "module", "name")

    def __init__(self, anchor: str, module: str, name: str):
        self.anchor, self.module, self.name = anchor, module, name

    @property
    def is_new(self) -> bool:
        """Belongs to the NEW tree. Accepts BOTH key forms (legacy `new`,
        current `new@<hash8>`) — the ledger is append-only, old lines stay."""
        return self.anchor == NEW_ANCHOR_LEGACY or self.anchor.startswith(NEW_PREFIX)


def parse_key(s: str) -> Key:
    m = KEY_RE.match(s)
    if not m:
        raise PortingLedgerError(
            "bad node key: %r\n"
            "  must be <anchor>::<module>::<name>. The anchor is:\n"
            "    <hash8>          OLD tree at the frozen hash\n"
            "    new@<hash8>      NEW tree at a commit\n"
            "    'new'            NEW tree, LEGACY form — readable, never written again"
            % s)
    return Key(m["anchor"], m["module"], m["name"])


def new_anchor(commit: str) -> str:
    """New-tree anchor at a commit: `new@<hash8>`. Ported (algorithm-faithful)
    from `ob-tools@60075cb:packages/portkit/src/portkit/ledger/ledger_spec.
    py:146-155`.

    The `new@` prefix stays (not the bare hash): old-tree and new-tree
    hashes share ONE key space in one ledger, and both trees can sit at the
    same commit (one repo, `src/` and `packages/` side by side).
    """
    if not re.fullmatch(r"[0-9a-f]{8,40}", commit):
        raise PortingLedgerError("new-tree anchor needs a commit hash, not %r" % commit)
    return NEW_PREFIX + commit[:8]


# ── the spec instance handed to the kernel shapes above ─────────────────────
SPEC = LedgerSpec(
    name="porting",
    statuses=STATUSES,
    transitions=TRANSITIONS,
    node_pattern=KEY_RE,
    blockers=BlockerRules(
        blocked_statuses=frozenset({"blocked"}),
        prefixes=BLOCK_PREFIXES,
        reference_prefixes=frozenset({"node:"}),
    ),
)


# ── event laws (porting codes L2/L4/L7/L8/L9, per single event) ────────────

def trivial_1to1(ev: Event) -> bool:
    """Trivial 1->1: `ported`, exactly one destination, symbol name UNCHANGED."""
    to: "tuple" = tuple(ev.extra.get("to") or ())
    if ev.status != "ported" or len(to) != 1:
        return False
    try:
        return parse_key(ev.node).name == parse_key(to[0]).name
    except PortingLedgerError:
        return False


def _to(ev: Event) -> "tuple[str, ...]":
    return tuple(ev.extra.get("to") or ())


def _blocked_by(ev: Event) -> "tuple[str, ...]":
    return tuple(ev.extra.get("blocked_by") or ())


def key_shape_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    try:
        parse_key(ev.node)
    except PortingLedgerError as e:
        return [("L2", str(e))]
    return []


def closed_set_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    if ev.status not in STATUSES:
        return [("L2", "status %r outside the closed set (%s)"
                  % (ev.status, ", ".join(sorted(STATUSES))))]
    return []


def destination_shape_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    to = _to(ev)
    err: "list[Finding]" = []
    if to and ev.status not in STATUSES_WITH_DEST:
        err.append(("L9", "status %r must not carry destinations but has %d. A "
                     "destination is declared when it LANDS, not when it is "
                     "intended." % (ev.status, len(to))))
    if ev.status in STATUSES_WITH_DEST and not to:
        err.append(("L9", "status %r needs at least one destination" % ev.status))
    if ev.status == "split" and len(to) < 2:
        err.append(("L9", "`split` means 1->many but only %d destination(s) — "
                     "one destination is `ported`" % len(to)))
    if ev.status in ("ported", "merged") and len(to) > 1:
        err.append(("L9", "`%s` needs exactly one destination but has %d — "
                     "many destinations is `split`" % (ev.status, len(to))))
    for t in to:
        try:
            tk = parse_key(t)
        except PortingLedgerError as e:
            err.append(("L2", "destination: %s" % e))
            continue
        if not tk.is_new:
            err.append(("L7", "destination %r carries anchor %r; a destination "
                         "must live in the NEW tree (anchor `%s<hash8>`)"
                         % (t, tk.anchor, NEW_PREFIX)))
    return err


def origin_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    try:
        key = parse_key(ev.node)
    except PortingLedgerError:
        return []          # L2 already reported the shape
    if ev.status == "born-new" and not key.is_new:
        return [("L7", "`born-new` is for NEW-tree nodes only, but the key "
                  "carries anchor %r" % key.anchor)]
    if key.is_new and ev.status not in ("born-new", "ported", "dropped"):
        return [("L7", "new-tree nodes accept `born-new` / `ported` / `dropped` "
                  "only, not %r" % ev.status)]
    return []


def blocker_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    err: "list[Finding]" = []
    blockers = _blocked_by(ev)
    if ev.status == "blocked" and not blockers:
        err.append(("L4", "`blocked` with empty `blocked_by` — an unnamed "
                     "blocker can never be cleared by anyone"))
    if blockers and ev.status != "blocked":
        err.append(("L4", "`blocked_by` present but status is %r, not `blocked`"
                     % ev.status))
    for b in blockers:
        if not any(b.startswith(p) for p in BLOCK_PREFIXES):
            err.append(("L2", "blocker %r carries no prefix from the closed set "
                         "(%s)" % (b, ", ".join(sorted(BLOCK_PREFIXES)))))
        elif len(b.split(":", 1)[1].strip()) == 0:
            err.append(("L2", "blocker %r has a prefix but no body" % b))
    return err


def reason_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    if not ev.extra.get("reason") and not trivial_1to1(ev) and ev.status != "unported":
        why = ("`dropped` ALWAYS needs a reason" if ev.status == "dropped"
               else "a line that is not a trivial 1->1 (status %r) needs a reason"
               % ev.status)
        return [("L8", "missing `reason`: %s" % why)]
    return []


def provenance_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    if ev.by != "human" and not re.fullmatch(r"suggest:[0-9a-f]{8,16}", ev.by):
        return [("L2", "`by` must be 'human' or 'suggest:<sha>', not %r" % ev.by)]
    return []


def test_owner_law(ev: Event, spec: LedgerSpec) -> "list[Finding]":  # noqa: ARG001
    """The test-owner column's shape law: present means a non-empty string.
    Absent is LAWFUL — every row written before the column exists reads as
    empty, which is the migration, not a violation."""
    owner = ev.extra.get(TEST_OWNER_KEY)
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        return [("L2", "`%s` must be a non-empty dist name when present, not %r"
                  % (TEST_OWNER_KEY, owner))]
    return []


#: The porting event laws, stable order. Run INSTEAD of the kernel builtin
#: laws — both firing on the same condition would file one finding under
#: two codes.
PORTING_EVENT_LAWS = (
    key_shape_law,
    closed_set_law,
    destination_shape_law,
    origin_law,
    blocker_law,
    reason_law,
    provenance_law,
    test_owner_law,
)


def validate_event(ev: Event) -> "list[Finding]":
    """All findings for ONE event, under porting law codes.

    An unknown status SHORT-CIRCUITS to L2 alone: with the status outside
    the closed set, the reason/destination/origin laws would pile noise
    onto a line that is already unparseable policy.
    """
    head = closed_set_law(ev, SPEC)
    if head:
        return head
    out: "list[Finding]" = []
    for law in PORTING_EVENT_LAWS:
        if law is closed_set_law:
            continue
        out.extend(law(ev, SPEC))
    return out
