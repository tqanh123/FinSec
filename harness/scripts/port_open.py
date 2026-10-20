"""port_open.py — the port domain's "open items" store: the machine STOPS
at a fork it cannot resolve alone, the model PROPOSES a choice, a human
APPROVES or REJECTS it. One FLAT module under `harness/scripts/` (sibling of
`port_runs.py`/`port_ledger_writer.py` — same reasons: `hs_run_port.py`
reads this back to decide whether a batch has pending items, and `hs-code
port propose`/`approve` write through it; neither caller should inherit the
`codegraph` measurement engine to do either).

LAYOUT — one open-items ledger PER BATCH (mirrors `port_runs.py`, never
`.port/ledger.jsonl`'s one-per-REPO shape): `<port_root>/.port/batches/
<batch>/open.jsonl`. A batch's own unresolved forks belong with the rest of
its own declared state, not mixed into a different batch's, and not mixed
into the PORT ledger either — an open item is a record that a DECISION is
still pending, the port ledger is a record of a node's CURRENT status; they
answer different questions and a broken line in one must never block a
reader of the other.

ONE RECORD PER EVENT — append-only, never a read-modify-write (CLAUDE.md's
own store discipline). Every line carries `id` (`o-0001`, ...), `event` (one
of `raised`/`proposed`/`approved`/`rejected`), `by`, `actor`, `ts`:

  * `raised`   (`by: hs-code`) — a real caller hit a fork it cannot resolve
    alone: `kind` (e.g. `two-homes`), `subject` (a STABLE string key —
    `raise_item` is idempotent on `(kind, subject)`, never on `id`),
    `candidates` (>=2 strings), `evidence` (strings), optional `node` (a
    port-ledger node key the item's resolution will write a `ported` line
    for) and `step` (the verb name that raised it).
  * `proposed` (`by: model`) — `choice` (must already be one of the raised
    item's own `candidates`), `why` (non-empty), `scope` (`item` or
    `architecture`); `architecture` REQUIRES `decision` (a DEC id, shape
    checked with `decision_register.is_readable_id` — existence is a
    DIFFERENT question, `port_ledger_laws.BlockerContext.cleared` answers
    it, at approval time, not here).
  * `approved` (`by: human`) — `choice` (must be one of the candidates).
  * `rejected` (`by: human`) — `why` (non-empty).

ACTOR IS ATTRIBUTION, NOT AUTHENTICATION (same rule `resolve_actor()`'s own
docstring states) — `by` is a free-text ROLE label a caller can spell
however it likes; only ONE thing here is a hard rule, and it exists to
resist DRIFT, not to resist forgery: **an `approved`/`rejected` line only
CLOSES an item when its own `by` reads literally `"human"` AND its own
`actor` does not look machine-shaped** (`"ci"`, or an actor carrying an
`/agent:` suffix — the exact shape `resolve_actor()` hands back for CI and
for an agent session). A line that fails either check is still a
WELL-FORMED line — it is read, it is not broken, it is simply never treated
as the closing event; the item stays exactly as pending as it was before
that line existed. This is the reader's own defense-in-depth: the `approve`/
`reject` verbs already refuse (exit 3) BEFORE writing when the calling
actor is machine-shaped, so a line failing this check on a REAL run only
ever arrives by hand (or by a bug) — which is exactly the case this check
exists to catch.

MALFORMED IS BROKEN, NOT SILENTLY DROPPED — bad JSON, a non-object value, a
missing/extra/mistyped key for the line's own `event`, an `event`/`proposed`/
`approved` line naming an `id` with no `raised` line ahead of it (file
order), a duplicate `raised` for the same `id`, or a `choice`/`proposed`
`decision` that does not validate — every one of these is kept VERBATIM in
`.broken` (line number + raw text + reason), the same "never silently
repaired" contract `port_runs.read_runs`/`port_ledger_reader.read_ledger`
already hold their own files to. A reader that met ANY broken line refuses
outright (`OpenLedgerError`) before answering ANY question about item
state — a half-readable open-items file is not a source of truth for
"safe to write" either.

ONE VALIDATION DOOR — `_apply_event` is BOTH the reader's own fold (called
once per line already on disk) AND the writer's own pre-append check
(called once against the NEW candidate line before ANY of `raise_item`/
`propose_item`/`approve_item`/`reject_item` appends it) — the same
"validate against the exact same engine, on read AND on write" discipline
`port_ledger_writer.append_event`/`_validate` already hold the sibling port
ledger to. There is no second, looser check either exposed for a
convenience-write.

LOCKING — every write door takes `register_store.register_lock` on
`<path>.lock`, RE-READS the file fresh under that lock, and validates +
appends against THAT read — the same race discipline `port_ledger_writer.
append_event` already holds `.port/ledger.jsonl` to.

WHAT THIS MODULE DOES NOT DECIDE — whether the calling actor is ALLOWED to
approve/reject (that refusal, and the `decision:` EXISTENCE check for a
`scope: architecture` proposal, are `hs-code port approve`'s own job,
`codegraph_port_open_verbs.py`) or whether an approved item's `node` should
also get a `ported` line written to the PORT ledger (also that verb's own
job — this module never imports `port_ledger_writer`/`port_ledger_spec` at
all, so an open item can be read/written with no port-ledger dependency).
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import decision_register  # noqa: E402
import register_store  # noqa: E402
import port_batch_reader  # noqa: E402
from hook_runtime import resolve_actor  # noqa: E402
from port_ledger_writer import now_ts  # noqa: E402

#: (line number, raw text, reason) — kept verbatim, never swallowed.
BrokenOpenLine = tuple

_BASE_KEYS: "frozenset[str]" = frozenset({"id", "ts", "actor", "event", "by"})
_EVENTS: "frozenset[str]" = frozenset({"raised", "proposed", "approved", "rejected"})
_PENDING_STATES: "frozenset[str]" = frozenset({"open", "proposed"})

_RAISED_REQUIRED: "frozenset[str]" = frozenset({"kind", "subject", "candidates", "evidence"})
_RAISED_OPTIONAL: "frozenset[str]" = frozenset({"node", "step"})
_PROPOSED_REQUIRED: "frozenset[str]" = frozenset({"choice", "why", "scope"})
_PROPOSED_OPTIONAL: "frozenset[str]" = frozenset({"decision"})
_APPROVED_REQUIRED: "frozenset[str]" = frozenset({"choice"})
_REJECTED_REQUIRED: "frozenset[str]" = frozenset({"why"})

_SCOPES: "frozenset[str]" = frozenset({"item", "architecture"})


class OpenLedgerError(Exception):
    """`open.jsonl` carries at least one line this reader cannot use.
    Append-only: the fix is a line by hand (or a superseding one), never an
    automatic rewrite. `broken` is the same `[(line, raw, reason), ...]`
    shape every sibling reader in this tree already carries."""

    def __init__(self, broken: "list") -> None:
        self.broken = list(broken)
        super().__init__("; ".join("line %d: %s" % (n, m) for n, _r, m in self.broken))


class ItemNotFoundError(Exception):
    """No `raised` line names this `id` under this batch."""

    def __init__(self, item_id: str) -> None:
        self.item_id = item_id
        super().__init__("no open item %r" % item_id)


class ItemNotPendingError(Exception):
    """The item exists but is already `approved`/`rejected` — not a fork
    waiting on anyone any more."""

    def __init__(self, item_id: str, state: str) -> None:
        self.item_id, self.state = item_id, state
        super().__init__("item %r is not pending (state=%s)" % (item_id, state))


@dataclass(frozen=True)
class Item:
    """One open item's replayed state. `proposal`/`resolution` are `None`
    until a `proposed`/closing `approved`/`rejected` line lands."""

    id: str
    kind: str
    subject: str
    candidates: "tuple[str, ...]"
    evidence: "tuple[str, ...]"
    node: "str | None"
    step: "str | None"
    state: str  # open | proposed | approved | rejected
    proposal: "Mapping[str, Any] | None" = None
    resolution: "Mapping[str, Any] | None" = None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "subject": self.subject,
            "candidates": list(self.candidates), "evidence": list(self.evidence),
            "node": self.node, "step": self.step, "state": self.state,
            "proposal": dict(self.proposal) if self.proposal else None,
            "resolution": dict(self.resolution) if self.resolution else None,
        }


@dataclass
class OpenReadResult:
    path: Path
    items: "dict[str, Item]" = field(default_factory=dict)
    broken: "list" = field(default_factory=list)


def open_path(port_root: "str | Path", batch: str) -> Path:
    """`<port_root>/.port/batches/<batch>/open.jsonl` — never a caller-typed
    path; `batch` names a directory a real batch already owns."""
    return port_batch_reader.batches_dir(port_root) / batch / "open.jsonl"


def is_machine_actor(actor: str) -> bool:
    """`"ci"`, or an actor carrying an `/agent:` suffix — the exact shapes
    `hook_runtime.resolve_actor()` hands back for CI and for an agent
    session. Never a stronger identity check: attribution, not
    authentication (`resolve_actor`'s own docstring)."""
    return actor == "ci" or "/agent:" in actor


def _closes(by: str, actor: str) -> bool:
    """An `approved`/`rejected` line closes its item only when BOTH hold:
    `by` reads literally `"human"`, and `actor` does not look
    machine-shaped. Either miss and the line is read, not broken, simply
    never authoritative."""
    return by == "human" and not is_machine_actor(actor)


def _shape_problem(rec: "dict", required: "frozenset[str]",
                   optional: "frozenset[str]" = frozenset()) -> "str | None":
    have = set(rec) - _BASE_KEYS
    missing = required - have
    extra = have - required - optional
    if missing or extra:
        bits = []
        if missing:
            bits.append("missing key(s) %s" % sorted(missing))
        if extra:
            bits.append("unknown key(s) %s" % sorted(extra))
        return "; ".join(bits)
    return None


def _apply_event(accum: "dict[str, dict]", rec: "dict") -> "str | None":
    """Fold ONE candidate record into `accum` (mutated in place on success).
    Returns a problem string (nothing mutated) or `None` (accepted). The
    ONE validation door every read and every write goes through — see the
    module docstring."""
    missing = _BASE_KEYS - set(rec)
    if missing:
        return "missing key(s) %s" % sorted(missing)
    rid, ts, actor, event, by = (rec["id"], rec["ts"], rec["actor"], rec["event"], rec["by"])
    for name, val in (("id", rid), ("ts", ts), ("actor", actor), ("event", event), ("by", by)):
        if not isinstance(val, str) or not val.strip():
            return "`%s` must be a non-empty string" % name
    if event not in _EVENTS:
        return "unknown event %r (expected one of %s)" % (event, sorted(_EVENTS))

    if event == "raised":
        if rid in accum:
            return "id %r already has a `raised` line" % rid
        problem = _shape_problem(rec, _RAISED_REQUIRED, _RAISED_OPTIONAL)
        if problem:
            return problem
        kind, subject = rec["kind"], rec["subject"]
        candidates, evidence = rec["candidates"], rec["evidence"]
        if not isinstance(kind, str) or not kind.strip():
            return "`kind` must be a non-empty string"
        if not isinstance(subject, str) or not subject.strip():
            return "`subject` must be a non-empty string"
        if (not isinstance(candidates, list) or len(candidates) < 2
                or not all(isinstance(c, str) and c.strip() for c in candidates)):
            return "`candidates` must be a list of >=2 non-empty strings"
        if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
            return "`evidence` must be a list of strings"
        node, step = rec.get("node"), rec.get("step")
        if node is not None and (not isinstance(node, str) or not node.strip()):
            return "`node` must be a non-empty string when present"
        if step is not None and (not isinstance(step, str) or not step.strip()):
            return "`step` must be a non-empty string when present"
        accum[rid] = {
            "id": rid, "kind": kind, "subject": subject,
            "candidates": tuple(candidates), "evidence": tuple(evidence),
            "node": node, "step": step, "state": "open",
            "proposal": None, "resolution": None,
        }
        return None

    acc = accum.get(rid)
    if acc is None:
        return "%s event for unknown id %r (no `raised` line ahead of it)" % (event, rid)

    if event == "proposed":
        problem = _shape_problem(rec, _PROPOSED_REQUIRED, _PROPOSED_OPTIONAL)
        if problem:
            return problem
        choice, why, scope = rec["choice"], rec["why"], rec["scope"]
        decision = rec.get("decision")
        if not isinstance(choice, str) or choice not in acc["candidates"]:
            return ("`choice` %r is not one of item %r's candidates %s"
                    % (choice, rid, list(acc["candidates"])))
        if not isinstance(why, str) or not why.strip():
            return "`why` must be a non-empty string"
        if scope not in _SCOPES:
            return "`scope` must be one of %s, not %r" % (sorted(_SCOPES), scope)
        if scope == "architecture":
            if not isinstance(decision, str) or not decision.strip():
                return "scope `architecture` needs a non-empty `decision` id"
            if not decision_register.is_readable_id(decision):
                return "`decision` %r is not a readable DEC id" % decision
        elif decision is not None:
            return "`decision` only applies to scope `architecture`"
        acc["proposal"] = {"choice": choice, "why": why, "scope": scope,
                           "decision": decision, "by": by, "actor": actor, "ts": ts}
        if acc["state"] == "open":
            acc["state"] = "proposed"
        return None

    if event == "approved":
        problem = _shape_problem(rec, _APPROVED_REQUIRED)
        if problem:
            return problem
        choice = rec["choice"]
        if not isinstance(choice, str) or choice not in acc["candidates"]:
            return ("`choice` %r is not one of item %r's candidates %s"
                    % (choice, rid, list(acc["candidates"])))
        payload = {"event": "approved", "choice": choice, "by": by, "actor": actor, "ts": ts}
    else:  # rejected
        problem = _shape_problem(rec, _REJECTED_REQUIRED)
        if problem:
            return problem
        why = rec["why"]
        if not isinstance(why, str) or not why.strip():
            return "`why` must be a non-empty string"
        payload = {"event": "rejected", "why": why, "by": by, "actor": actor, "ts": ts}

    if acc["state"] not in ("approved", "rejected") and _closes(by, actor):
        acc["state"] = event
        acc["resolution"] = payload
    return None


def _to_item(acc: "dict") -> Item:
    return Item(id=acc["id"], kind=acc["kind"], subject=acc["subject"],
               candidates=acc["candidates"], evidence=acc["evidence"],
               node=acc["node"], step=acc["step"], state=acc["state"],
               proposal=acc["proposal"], resolution=acc["resolution"])


def _fold(path: Path) -> "tuple[dict[str, dict], list]":
    accum: "dict[str, dict]" = {}
    broken: "list" = []
    if not path.is_file():
        return accum, broken
    with path.open("r", encoding="utf-8", newline="") as fh:
        lineno = 0
        for raw in fh:
            lineno += 1
            if not raw.endswith("\n"):
                if raw:
                    broken.append((lineno, raw,
                                  "partial tail: file does not end with a newline "
                                  "(interrupted write?)"))
                break
            content = raw[:-1].strip()
            if not content:
                continue
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                broken.append((lineno, content, "bad JSON: %s" % exc))
                continue
            if not isinstance(parsed, dict):
                broken.append((lineno, content,
                              "bad record: not a JSON object (got %s)" % type(parsed).__name__))
                continue
            problem = _apply_event(accum, parsed)
            if problem:
                broken.append((lineno, content, problem))
    return accum, broken


def broken_line_findings(broken: "list") -> "list[dict]":
    return [{"line": lineno, "message": why} for lineno, _raw, why in broken]


def read_open(port_root: "str | Path", batch: str) -> OpenReadResult:
    """Stream `<port_root>/.port/batches/<batch>/open.jsonl`. A missing file
    reads as empty (no items, nothing broken) — a batch with no fork yet is
    a normal state. Tolerant: `.broken` names every line this fold could not
    use; callers that need to WRITE or DECIDE something refuse outright on
    `.broken` (see `pending_items`/`approved_choice` and every write door
    below) — a caller that only wants to REPORT reads `.broken` itself."""
    path = open_path(port_root, batch)
    accum, broken = _fold(path)
    items = {iid: _to_item(acc) for iid, acc in accum.items()}
    return OpenReadResult(path=path, items=items, broken=broken)


def pending_items(port_root: "str | Path", batch: str) -> "list[Item]":
    """Every item still `open`/`proposed`, sorted by id. Raises
    `OpenLedgerError` when the file itself is unreadable — a broken
    open-items file can never honestly answer "nothing pending"."""
    result = read_open(port_root, batch)
    if result.broken:
        raise OpenLedgerError(result.broken)
    return sorted((it for it in result.items.values() if it.state in _PENDING_STATES),
                 key=lambda it: it.id)


def approved_choice(port_root: "str | Path", batch: str, kind: str, subject: str) -> "str | None":
    """The approved `choice` for the item raised under `(kind, subject)`, or
    `None` when no such item is approved yet (never raised, still pending,
    or rejected). Raises `OpenLedgerError` on a broken file, same reason as
    `pending_items`."""
    result = read_open(port_root, batch)
    if result.broken:
        raise OpenLedgerError(result.broken)
    for it in result.items.values():
        if it.kind == kind and it.subject == subject and it.state == "approved":
            return (it.resolution or {}).get("choice")
    return None


def _next_id(accum: "dict[str, dict]") -> str:
    nums = [int(m.group(1)) for m in
           (re.fullmatch(r"o-(\d+)", iid) for iid in accum) if m]
    return "o-%04d" % ((max(nums) + 1) if nums else 1)


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=False) + "\n")


def raise_item(port_root: "str | Path", batch: str, *, kind: str, subject: str,
              candidates: "list[str]", evidence: "list[str]",
              node: "str | None" = None, step: "str | None" = None) -> Item:
    """Raise a fork the caller cannot resolve alone. IDEMPOTENT on
    `(kind, subject)`: a PENDING (open/proposed) or an ALREADY-APPROVED item
    with that same key is returned as-is, nothing written — a re-run of the
    raising step (`add-module`/`judge-nodes`) must never pile up a second
    item for the same fork, and once a human has approved one, re-raising
    would ask the same question twice. A REJECTED item does not block a
    fresh raise: rejection is a dead end for that item, not an answer the
    caller should keep re-reading.

    Always writes for real — the CALLER decides whether to call this at all
    behind its OWN `--apply` flag; this function carries no dry-run mode of
    its own (the module docstring's own door discipline)."""
    path = open_path(port_root, batch)
    lock_path = path.with_name(path.name + ".lock")
    with register_store.register_lock(lock_path):
        accum, broken = _fold(path)
        if broken:
            raise OpenLedgerError(broken)
        for acc in accum.values():
            if (acc["kind"] == kind and acc["subject"] == subject
                    and acc["state"] in ("open", "proposed", "approved")):
                return _to_item(acc)
        rec = {"id": _next_id(accum), "ts": now_ts(), "actor": resolve_actor(),
              "event": "raised", "by": "hs-code", "kind": kind, "subject": subject,
              "candidates": list(candidates), "evidence": list(evidence)}
        if node is not None:
            rec["node"] = node
        if step is not None:
            rec["step"] = step
        problem = _apply_event(accum, rec)
        if problem:
            raise ValueError("port_open.raise_item: %s" % problem)
        _append(path, rec)
        return _to_item(accum[rec["id"]])


def propose_item(port_root: "str | Path", batch: str, item_id: str, *, choice: str,
                 why: str, scope: str, decision: "str | None" = None) -> Item:
    """Write a `proposed` line for `item_id`. Raises `OpenLedgerError` (a
    broken file), `ItemNotFoundError`, `ItemNotPendingError`, or
    `ValueError` (the SAME shape problem `_apply_event` would report for a
    hand-written line — an invalid `choice`/`scope`/`decision` refuses here
    too, never only on read)."""
    path = open_path(port_root, batch)
    lock_path = path.with_name(path.name + ".lock")
    with register_store.register_lock(lock_path):
        accum, broken = _fold(path)
        if broken:
            raise OpenLedgerError(broken)
        acc = accum.get(item_id)
        if acc is None:
            raise ItemNotFoundError(item_id)
        if acc["state"] not in _PENDING_STATES:
            raise ItemNotPendingError(item_id, acc["state"])
        rec = {"id": item_id, "ts": now_ts(), "actor": resolve_actor(), "event": "proposed",
              "by": "model", "choice": choice, "why": why, "scope": scope}
        if decision is not None:
            rec["decision"] = decision
        problem = _apply_event(accum, rec)
        if problem:
            raise ValueError("port_open.propose_item: %s" % problem)
        _append(path, rec)
        return _to_item(accum[item_id])


def _resolve(port_root: "str | Path", batch: str, item_id: str, *,
            event: str, extra: dict) -> Item:
    path = open_path(port_root, batch)
    lock_path = path.with_name(path.name + ".lock")
    with register_store.register_lock(lock_path):
        accum, broken = _fold(path)
        if broken:
            raise OpenLedgerError(broken)
        if item_id not in accum:
            raise ItemNotFoundError(item_id)
        rec = {"id": item_id, "ts": now_ts(), "actor": resolve_actor(),
              "event": event, "by": "human", **extra}
        problem = _apply_event(accum, rec)
        if problem:
            raise ValueError("port_open.%s: %s" % (event, problem))
        _append(path, rec)
        return _to_item(accum[item_id])


def approve_item(port_root: "str | Path", batch: str, item_id: str, *, choice: str) -> Item:
    """Write an `approved` line, `by: human`, the caller's own real
    `resolve_actor()`. Whether that actor IS human-shaped, and whether a
    `scope: architecture` proposal's `decision` blocker has cleared, are the
    `approve` VERB's own checks (`codegraph_port_open_verbs.py`) — this
    function writes what it is told; the reader-side `_closes` gate is the
    defense that still holds even if a caller skipped that check."""
    return _resolve(port_root, batch, item_id, event="approved", extra={"choice": choice})


def reject_item(port_root: "str | Path", batch: str, item_id: str, *, why: str) -> Item:
    """Write a `rejected` line, `by: human`. See `approve_item`'s own note."""
    return _resolve(port_root, batch, item_id, event="rejected", extra={"why": why})
