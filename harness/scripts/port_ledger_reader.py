"""port_ledger_reader — the port domain's compatible streaming ledger reader
AND its event envelope, as one flat module under `harness/scripts/`, a
sibling of `codegraph_snapshot_reader.py` (same reason: a caller that only
wants to read `.port/ledger.jsonl` back off disk should not have to import
the whole `codegraph` measurement engine to do it).

Formerly two modules under `codegraph/`: `codegraph.port.ledger_io` (the
reader) delegating to `codegraph._core.ledger.model` (the event envelope,
`Event`/`ENVELOPE_KEYS`/`BrokenLine`). `hs_run_port.py` was the only real
caller of either, and `import codegraph.port.ledger_io` alone put
`codegraph` in `sys.modules` for a verb that never touches the measurement
engine — exactly the import weight `codegraph_snapshot_reader.py` exists to
keep off hs-run's own path, just paid a second time by a different reader.
Folded into one module here; `codegraph/port/` and `codegraph/_core/ledger/`
no longer exist.

LAYOUT — NOT the upstream tool's `output/porting/<batch>/port-ledger.jsonl`
walk-up: one ledger per REPO, fixed at `<port_root>/.port/ledger.jsonl`.
`port_workspace.find_port_root` already resolves `<port_root>`; this module
never re-derives it.

COMPATIBLE READ — every line the OpsBrain-era tool ever wrote reads
unchanged. Measured against the real ledger (21 MB, 65,728 lines):
0 broken, 25,608 distinct nodes, none carrying `src`.

Streaming, never `path.read_text()` whole: the upstream tool this reader is
algorithm-faithful to loads a ledger by reading the whole file into memory
before splitting on `\\n`; on a 21 MB ledger that is one 21 MB string held
for the load's whole duration, on top of the parsed events. `read_ledger`
below walks the file handle line by line instead — same broken-line /
partial-tail semantics, no single `read_text()` call sized to the ledger.

NO WRITE DOOR HERE. `read_ledger` never inspects `src` at all — it reports
counts and broken lines, nothing about which of the read lines do or do not
carry one. Appending (the `src` rule: a line this harness writes must carry
a source id + 40-hex commit sha) comes with the first writer verb, a later
slice — this module makes no promise about it either way.

WHAT THE EVENT ENVELOPE CANNOT SEE:
  * `ts` is the writing machine's clock. Two skewed machines make file
    order (append order) the only real order — never sort by `ts`.
  * `by` records PROVENANCE ("human" / "suggest:<sha>" / whatever the
    domain declares), never identity — the same caveat `actor` itself
    carries: attribution, not proof.
  * Nothing here validates `extra`; shape laws live with the writer this
    module does not yet have (the `src` rule above is this tree's first
    one, and it lands at the append door, not the read door).

ALLOWED IMPORTS: the stdlib only. Nothing under `codegraph`, and nothing
else new on hs-run's own import path — a caller that only wants to stream a
JSONL ledger back off disk should not inherit anything heavier than that.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# ── the event envelope: fields the kernel owns, fields the domain owns ──────
#
# Ported (algorithm-faithful) from
# `ob-tools@60075cb:packages/codemap/src/codemap/ledger/model.py`. Kernel-owned
# here: the event envelope (`Event`, `ENVELOPE_KEYS`) and the `BrokenLine`
# shape a tolerant reader reports an unusable line as. Appending and
# attribution live in `port_ledger_writer` (`Ledger`, `append_event`), the law
# engine and the domain state table in `port_ledger_spec`/`port_ledger_laws`.

#: Envelope keys are reserved: `extra` keys colliding with them cannot
#: round-trip and would silently shadow a machine-owned field.
ENVELOPE_KEYS: "frozenset[str]" = frozenset(
    {"ts", "node", "status", "by", "actor", "note"})

#: (line number, raw text, error) — kept verbatim; swallowing a broken line
#: would turn "the tool is blind here" into "the ledger is clean".
BrokenLine = tuple


@dataclass(frozen=True)
class Event:
    """One ledger line: the full state of one node at one moment.

    Full-state, not a delta — replaying deltas requires every line to be
    present and in order; replaying full states makes "last line wins" a
    fold that cannot be wrong, and one broken line damages exactly one node.
    """

    ts: str
    node: str
    status: str
    by: str = "human"
    actor: str = ""
    note: "str | None" = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clash = set(self.extra) & ENVELOPE_KEYS
        if clash:
            raise ValueError(
                "extra keys collide with the kernel envelope: %s"
                % sorted(clash))

    def as_dict(self) -> dict:
        """Stable serialization order: envelope core, extra (sorted),
        envelope tail."""
        d: dict = {"ts": self.ts, "node": self.node, "status": self.status}
        for k in sorted(self.extra):
            v = self.extra[k]
            d[k] = list(v) if isinstance(v, tuple) else v
        d["by"] = self.by
        if self.actor:
            d["actor"] = self.actor
        if self.note:
            d["note"] = self.note
        return d

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Event":
        extra = {k: (tuple(v) if isinstance(v, list) else v)
                 for k, v in d.items() if k not in ENVELOPE_KEYS}
        return cls(
            ts=d["ts"], node=d["node"], status=d["status"],
            by=d.get("by", "human"),
            actor=d.get("actor", ""),
            note=d.get("note"),
            extra=extra,
        )


# ── the reader: which repo's ledger, and streaming it into events ──────────

#: `<port_root>/.port/ledger.jsonl` — the one ledger this repo keeps.
_LEDGER_REL = (".port", "ledger.jsonl")

#: The law code a broken ledger line speaks under, upstream's own name for
#: it (`ob-tools@60075cb:packages/portkit/src/portkit/ledger/
#: check_ledger.py:64`, `"L0": "broken ledger line"`). Used by
#: `hs_run_port.py` for a line `read_ledger` could not parse at all (bad
#: JSON, a missing envelope field, a partial tail). The first writer verb
#: is expected to reuse the same code for its own `src` rule — same
#: definition of "unusable", just caught at the write door instead of the
#: read door — but that door does not exist in this slice.
BROKEN_LINE_LAW = "L0"


def ledger_path(port_root: Path) -> Path:
    """`<port_root>/.port/ledger.jsonl`."""
    return Path(port_root).joinpath(*_LEDGER_REL)


def checkpoints_path(ledger: Path) -> Path:
    """The append-only sidecar of `ledger`'s checkpoints: one JSON line
    `{lines, sha256, why, actor, ts}` per `port_ledger_writer.write_checkpoint`."""
    return Path(ledger).with_name("ledger.checkpoints.jsonl")


def _read_checkpoints(ledger: Path) -> "tuple[list[dict], list[BrokenLine]]":
    """Checkpoints of `ledger`, sorted by line count. A line that is not one is
    a broken ledger (fail closed: a check that silently skips reads as passed)."""
    cp_path = checkpoints_path(ledger)
    if not cp_path.is_file():
        return [], []
    out: "list[dict]" = []
    bad: "list[BrokenLine]" = []
    for n, raw in enumerate(cp_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            cp = json.loads(raw)
            if not (isinstance(cp, dict) and isinstance(cp.get("lines"), int)
                    and cp["lines"] > 0 and isinstance(cp.get("sha256"), str)):
                raise ValueError("needs integer `lines` > 0 and string `sha256`")
        except ValueError as exc:
            bad.append((0, raw, "unreadable checkpoint (%s line %d): %s" % (cp_path.name, n, exc)))
            continue
        out.append(cp)
    return sorted(out, key=lambda c: c["lines"]), bad


def broken_line_findings(broken: "list[BrokenLine]") -> "list[dict]":
    """`[{line, law, message}, ...]` — every caller that refuses to proceed
    on a broken ledger line (`hs-run port next`'s own `l0_findings`, and any
    write-door verb that refuses to append onto a ledger already carrying
    one) formats the finding the SAME way, through this one function,
    rather than each hand-rolling its own dict literal and drifting apart on
    field names."""
    return [{"line": lineno, "law": BROKEN_LINE_LAW, "message": why}
            for lineno, _raw, why in broken]


@dataclass
class LedgerReadResult:
    """One streaming read of a ledger file. `events`/`broken` carry the
    exact shapes `Event`/`BrokenLine` above already define — this is not a
    second event/broken-line model, only a second (streaming) way to fill
    the same two lists."""

    path: Path
    events: list = field(default_factory=list)
    broken: "list[BrokenLine]" = field(default_factory=list)
    #: Physical content lines actually handed to `json.loads`, success OR
    #: failure (the same count the known-answer probe uses) — blank lines and
    #: `#`-comment lines are never counted (they are never parsed at all).
    #: A bad-JSON line counts here AND in `broken`; the two answer
    #: different questions ("how much was there" vs "how much was usable").
    n_lines: int = 0
    elapsed_s: float = 0.0

    @property
    def n_broken(self) -> int:
        return len(self.broken)

    @property
    def n_nodes(self) -> int:
        return len({ev.node for ev in self.events})

    def as_stats(self) -> dict:
        return {"lines": self.n_lines, "broken": self.n_broken, "nodes": self.n_nodes}


def read_ledger(path: Path) -> LedgerReadResult:
    """Stream `path` line by line into a `LedgerReadResult`.

    A missing file reads as an EMPTY ledger (0/0/0), matching the kernel's
    own `Ledger.load` — "no ledger yet" and "no line yet" are the same fact
    to a reader.

    Broken-line detection matches the kernel's whole-text loader exactly: a
    line that is not valid JSON, is valid JSON but not an object (`null`,
    a list, a number, a string), or is an object but not a valid `Event`
    (missing `ts`/`node`/`status`), is kept VERBATIM in `.broken` rather
    than silently dropped — a swallowed line is how a genuinely broken
    ledger reads as "clean". A file whose final byte is not a newline (an
    interrupted write) is reported the same way, as a `partial tail` entry
    covering only that last, unterminated chunk — never parsed as JSON at
    all, same as the kernel loader.
    """
    result = LedgerReadResult(path=path)
    if not path.is_file():
        return result

    checkpoints, bad_checkpoints = _read_checkpoints(path)
    result.broken.extend(bad_checkpoints)
    pending = list(checkpoints)
    prefix = hashlib.sha256()

    def _changed(cp: dict, how: str) -> None:
        result.broken.append((cp["lines"], "", (
            "history changed before a checkpoint: lines 1-%d %s since %s by %s (%s) — "
            "append-only: supersede with a new line, never edit, delete or cut one"
            % (cp["lines"], how, cp.get("ts", "?"), cp.get("actor", "?"), cp.get("why", "?")))))

    start = time.monotonic()
    # `newline=""` — no universal-newline translation; the file is split on
    # "\n" only, exactly like the kernel's `text.split("\n")`.
    with path.open("r", encoding="utf-8", newline="") as fh:
        lineno = 0
        for raw in fh:
            lineno += 1
            terminated = raw.endswith("\n")
            if terminated:
                prefix.update(raw.encode("utf-8"))
                while pending and pending[0]["lines"] == lineno:
                    cp = pending.pop(0)
                    if prefix.hexdigest() != cp["sha256"]:
                        _changed(cp, "no longer hash to what was recorded")
            if not terminated:
                # The final physical chunk in a file with no trailing
                # newline — iteration cannot yield this on any earlier
                # line, so this is always the LAST one processed.
                if raw:
                    result.broken.append((
                        lineno, raw,
                        "partial tail: file does not end with a newline "
                        "(interrupted write?)"))
                break
            content = raw[:-1].strip()
            if not content or content.startswith("#"):
                continue
            result.n_lines += 1
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                result.broken.append((lineno, content, "bad JSON: %s" % exc))
                continue
            if not isinstance(parsed, dict):
                result.broken.append((
                    lineno, content,
                    "bad event: not a JSON object (got %s)"
                    % type(parsed).__name__))
                continue
            try:
                result.events.append(Event.from_dict(parsed))
            except (KeyError, ValueError, TypeError) as exc:
                result.broken.append((lineno, content, "bad event: %s" % exc))
    for cp in pending:
        _changed(cp, "are no longer all there (the file holds fewer lines)")
    result.elapsed_s = time.monotonic() - start
    return result
