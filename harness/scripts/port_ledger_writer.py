"""port_ledger_writer.py — the port ledger's ONE write door, as one FLAT
module under `harness/scripts/` (sibling of `port_ledger_reader.py`/
`port_ledger_spec.py`).

`Ledger` is ported (algorithm-faithful) from
`ob-tools@60075cb:packages/codemap/src/codemap/ledger/ledger.py`, trimmed to
exactly what the `map` write path calls (no-orphan, function level): `load`,
`_append_line`, `replay`. `history`/`transitions_of`/`reverse_index`/`commit` have
no caller reachable from this slice's verb and are left behind rather than
copied unused.

`load` does NOT re-implement the streaming/tolerant read upstream's own
`Ledger.load` inlines — it delegates to `port_ledger_reader.read_ledger`,
the one compatible reader this tree already has, so there is never a second
place that decides what a broken ledger line looks like.

`append_event` is ported (algorithm-faithful) from `ledger_io.py`'s
`write_event` (`ob-tools@60075cb:packages/portkit/src/portkit/ledger/
ledger_io.py:110-152`) — validate-then-append, the ONE door every ledger
writer shares — with ONE addition this tree's ledger did not carry before:
the `src` rule (a line this harness writes must carry `src: {id, sha}`,
`sha` a 40-hex commit, or the whole line is refused under the SAME law code
(`L0`) a broken line on disk already speaks — `port_ledger_reader.
BROKEN_LINE_LAW`). Upstream's own transition-table check and partial-tail
refusal are unchanged; `actor`/`ts` are the CALLER's job (`resolve_actor()`/
`now_ts()`), never re-derived here.

A LOADED ledger already carrying a broken line refuses OUTRIGHT, before any
law even runs: upstream has several call sites that parse a ledger and
silently skip a bad line (`except ValueError: continue`); this door never
repeats that shape — a ledger this door is about to APPEND to is read
through the exact same tolerant reader `hs-run port next` uses, and a
ledger holding ANY broken line is refused with the same `L0` policy that
verb already reports, not written past.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import port_ledger_reader
import register_store
from port_ledger_reader import BrokenLine, Event
from port_ledger_spec import SPEC, Finding, LedgerError, is_sha40, validate_event

#: How many broken line numbers a refusal names before summarising the rest.
_BROKEN_LINES_SHOWN = 10


def now_ts() -> str:
    """Local-wall-clock ISO stamp — ported verbatim from
    `ob-tools@60075cb:packages/codemap/src/codemap/ledger/model.py`'s own
    `now_ts`. Local on purpose: the reader is a human skimming the file; the
    ORDER authority is append order (file position), never this stamp — two
    skewed writing machines make `ts` untrustworthy for ordering, which is
    exactly why `Ledger.replay`/`append_event`'s transition check both read
    file order, never sort by this field."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class BrokenLedgerError(LedgerError):
    """`Ledger.load_checked` met a broken line; `findings` is the `L0` refusal."""

    def __init__(self, findings: "list[Finding]") -> None:
        self.findings = findings
        super().__init__("; ".join(msg for _code, msg in findings))


@dataclass
class Ledger:
    """One ledger file, loaded tolerant, written append-only."""

    path: Path
    events: "list[Event]" = field(default_factory=list)
    broken: "list[BrokenLine]" = field(default_factory=list)
    #: Trailing partial line found at load — appending after one would weld
    #: two events into a single unparseable line; `_append_line` refuses instead.
    partial_tail: bool = False
    #: `history`'s per-node index, and the events list (object + length) it was
    #: built from: events only grow or are replaced, so either change rebuilds it.
    _by_node: "dict[str, list[Event]]" = field(default_factory=dict, repr=False, compare=False)
    _by_node_of: "tuple[object, int]" = field(default=(None, -1), repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        """Read `path` through `port_ledger_reader.read_ledger` — the ONE
        streaming, compatible reader this tree has — never a second,
        whole-file loader duplicating its broken-line/partial-tail rules."""
        result = port_ledger_reader.read_ledger(path)
        partial_tail = any(
            msg.startswith("partial tail:") for _lineno, _raw, msg in result.broken)
        return cls(path=Path(path), events=list(result.events),
                   broken=list(result.broken), partial_tail=partial_tail)

    @classmethod
    def load_checked(cls, path: Path) -> "Ledger":
        """The door for a read that DECIDES something: `load`, refused with
        `BrokenLedgerError` (line numbers in the message) when any line is
        broken. `load` stays tolerant for readers that report `.broken`."""
        ledger = cls.load(path)
        findings = broken_ledger_refusal(ledger)
        if findings:
            raise BrokenLedgerError(findings)
        return ledger

    def _append_line(self, *evs: Event) -> None:
        """Append one line per event, in order, through one open. No edits, no
        deletes — supersede by appending. Private: `append_events` below is the
        only caller."""
        if self.partial_tail:
            raise LedgerError(
                "%s ends with a partial line; appending would corrupt it.\n"
                "  Inspect the tail, fix or remove the fragment BY HAND (it "
                "is evidence), then re-run." % self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write("".join(ev.as_json() + "\n" for ev in evs))
        self.events.extend(evs)

    def replay(self) -> "dict[str, Event]":
        """Current state: per node, the LAST event in file order."""
        state: "dict[str, Event]" = {}
        for ev in self.events:
            state[ev.node] = ev
        return state

    def history(self, node: str) -> "list[Event]":
        """Every line ever written for `node`, in file (append) order —
        ported (algorithm-faithful) from `ob-tools@60075cb:packages/codemap/
        src/codemap/ledger/ledger.py:113`. The ledger-laws scan (L3, the
        full-history illegal-transition check) is its one real caller in
        this tree; `replay()` above answers "state NOW", this answers
        "everything that was ever said about this node". One pass builds the
        index for every node; asking each node in turn is not one pass each."""
        src, n = self._by_node_of
        if src is not self.events or n != len(self.events):
            idx: "dict[str, list[Event]]" = {}
            for ev in self.events:
                idx.setdefault(ev.node, []).append(ev)
            self._by_node, self._by_node_of = idx, (self.events, len(self.events))
        return list(self._by_node.get(node, ()))

    def transitions_of(self, node: str) -> "list[tuple[str | None, Event]]":
        """`(previous status, event)` per step of `node`'s own history. The
        first step's previous is `None` — a first event has no 'from',
        which is not the same fact as the empty string. Ported
        (algorithm-faithful) from `ob-tools@60075cb:packages/codemap/src/
        codemap/ledger/ledger.py:118`."""
        out: "list[tuple[str | None, Event]]" = []
        prev: "str | None" = None
        for ev in self.history(node):
            out.append((prev, ev))
            prev = ev.status
        return out


def validate_src(ev: Event) -> "list[Finding]":
    """This harness's OWN addition to the write door — upstream's ledger
    carries no such rule (a line this tree read from the real OpsBrain
    ledger, `port_ledger_reader.py`'s own docstring, never had `src` at
    all). Every line this harness APPENDS from here on must carry
    `src: {id, sha}`, `sha` a full 40-hex commit — refused under the same
    law code a broken-on-disk line already speaks (`L0`,
    `port_ledger_reader.BROKEN_LINE_LAW`), never a second law namespace for
    "this write door's own opinion"."""
    src = ev.extra.get("src")
    law = port_ledger_reader.BROKEN_LINE_LAW
    if not isinstance(src, dict):
        return [(law, "missing `src` — every line this harness writes must "
                  "carry `src: {id, sha}`")]
    src_id = src.get("id")
    sha = src.get("sha")
    problems: "list[Finding]" = []
    if not isinstance(src_id, str) or not src_id.strip():
        problems.append((law, "`src.id` is missing or not a non-empty string"))
    if not is_sha40(sha):
        problems.append((law, "`src.sha` must be a 40-hex commit SHA, not %r" % sha))
    return problems


def broken_ledger_refusal(ledger: Ledger) -> "list[Finding]":
    """A loaded ledger already carrying a broken line refuses ANY append
    outright, before any per-event law even runs — the same `L0` policy
    `hs-run port next` already reports for the exact same ledger. Empty when
    the ledger is clean."""
    if not ledger.broken:
        return []
    law = port_ledger_reader.BROKEN_LINE_LAW
    shown = [lineno for lineno, _raw, _msg in ledger.broken[:_BROKEN_LINES_SHOWN]]
    more = len(ledger.broken) - len(shown)
    where = ", ".join("line %d" % n for n in shown) + (", +%d more" % more if more else "")
    return [(law, "%d broken line(s) in %s (%s) — fix by hand first (append-only: "
             "never edit a line in place, supersede it with a new one)"
             % (len(ledger.broken), ledger.path, where))]


def _event_findings(state: "dict[str, Event]", ev: Event) -> "list[Finding]":
    """The per-event laws, judged against `state` (node -> its last event)."""
    errs: "list[Finding]" = list(validate_event(ev))
    errs.extend(validate_src(ev))
    prev = state.get(ev.node)
    if prev is not None and prev.status != ev.status:
        if not SPEC.may_transition(prev.status, ev.status):
            allowed = sorted(SPEC.transitions.get(prev.status, frozenset()))
            errs.append(("L9", "illegal transition %r -> %r. From %r only: %s"
                         % (prev.status, ev.status, prev.status,
                            ", ".join(allowed) or "(nowhere)")))
    return errs


def _validate(ledger: Ledger, evs: "list[Event]") -> "tuple[Event | None, list[Finding]]":
    """Every check a write runs, against whatever `ledger` holds. The batch
    is judged IN ORDER on one replay that each accepted event joins, so a
    later event is judged against an earlier one of the same batch (a
    `ported` then an `unported` of one node is the illegal transition it would
    be across two writes). One replay per batch, not per event. Factored out
    so the lock-free dry-run path and the locked apply path run the exact
    same law engine against two different ledger snapshots."""
    # A ledger already carrying ANY broken line (partial tail included — it
    # is recorded in `ledger.broken` too, see `Ledger.load`) refuses before
    # a single per-event law runs; `Ledger._append_line` repeats the
    # partial-tail refusal at its own layer as a second belt, never the
    # only one.
    broken = broken_ledger_refusal(ledger)
    if broken:
        return evs[0], broken
    state = ledger.replay()
    for ev in evs:
        errs = _event_findings(state, ev)
        if errs:
            return ev, errs
        state[ev.node] = ev
    return None, []


def append_events(ledger: Ledger, evs: "list[Event]", *,
                  apply: bool = True) -> "tuple[Event | None, list[Finding]]":
    """Validate-then-append a whole batch, all-or-nothing — the ONE write
    door every port-ledger writer in this harness shares (matching upstream's
    own `write_event`, plus the `src` rule and the broken-ledger refusal this
    tree adds). Returns `(the first refused event, its findings)`, or
    `(None, [])` when every event was accepted (and, on `apply`, written).

    `apply=False` runs every law and appends nothing (dry-run): it validates
    against `ledger` exactly as the caller loaded it (typically
    `load_checked`, moments earlier) and never touches the lock or the disk
    — a dry-run report stays free, so it keeps serving even while another
    writer holds the lock.

    `apply=True` is the critical section: it takes `register_store.
    register_lock` on `<ledger path>.lock`, RE-READS the ledger fresh off
    disk under that lock ONCE, and validates + appends the batch against
    THAT read — never against the possibly-stale state the caller loaded
    earlier. Two writers racing `Ledger.load` on the same file can no longer
    both see the ledger as it stood before either of them wrote: the second
    one to reach the lock re-reads the first one's lines and is judged
    against them. An empty batch touches neither the lock nor the file.
    """
    evs = list(evs)
    if not evs:
        return None, []
    if not apply:
        return _validate(ledger, evs)

    lock_path = ledger.path.with_name(ledger.path.name + ".lock")
    with register_store.register_lock(lock_path):
        current = Ledger.load(ledger.path)
        refused = _validate(current, evs)
        if refused[0] is not None:
            return refused
        current._append_line(*evs)
    ledger.events = current.events
    ledger.broken = current.broken
    ledger.partial_tail = current.partial_tail
    return None, []


def append_event(ledger: Ledger, ev: Event, *, apply: bool = True) -> "list[Finding]":
    """`append_events` for one event: the findings that blocked the write
    (empty = accepted). A caller checks `bool(findings)` to know whether
    anything was written; the findings themselves are the refusal's own
    reasons, never re-derived from a message string. A caller with more than
    one event hands the whole batch to `append_events` — a loop of this
    re-reads and replays the ledger once per event."""
    return append_events(ledger, [ev], apply=apply)[1]


def write_checkpoint(path: Path, *, why: str, actor: "str | None" = None,
                     ts: "str | None" = None) -> dict:
    """Record the ledger's current length and the sha256 of its bytes in
    `port_ledger_reader.checkpoints_path` (append-only). From then on every
    `read_ledger` re-hashes that prefix: an edited, deleted or cut line before
    it is a broken ledger. Refused (`BrokenLedgerError`) on a ledger that is
    already broken — a checkpoint must never bless a bad history. Returns the
    record written."""
    import hashlib
    import json

    from hook_runtime import resolve_actor

    ledger = Ledger.load_checked(path)
    data = ledger.path.read_bytes() if ledger.path.is_file() else b""
    record = {"lines": data.count(b"\n"), "sha256": hashlib.sha256(data).hexdigest(),
              "why": why, "actor": actor or resolve_actor(), "ts": ts or now_ts()}
    if record["lines"] == 0:
        return record
    cp_path = port_ledger_reader.checkpoints_path(ledger.path)
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    with cp_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return record
