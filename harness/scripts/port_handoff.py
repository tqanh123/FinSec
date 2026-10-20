"""port_handoff.py — `.port/batches/<b>/handoff.json`: what a mechanical
port step left for a PERSON to route through `plan.md`, as one FLAT module
under `harness/scripts/` (sibling of `port_batch_reader.py`/`port_runs.py` —
same reachability reason: `harness/scripts/hs_run_port.py`'s own `next`
compares this file's own bytes against a batch's declared
`handoff_sha256` (`port_batch_reader.Batch.handoff_sha256`), so this module
must stay reachable from `hs_run_port.py` WITHOUT dragging in the
`codegraph` measurement engine — no `codegraph` import here, at any depth,
matching every other flat `port_*.py` sibling that reader import-closure
gate holds to (`port_ledger_reader.py`'s own docstring states the same
rule). `git` is not read here either — `target_head` arrives as a
CALLER-supplied value (`harness/scripts/hs_code.py`'s own `--step` writer
resolves it, since that shell may import `codegraph` freely); this module
never resolves it itself.

LAYOUT — one handoff document PER BATCH (mirrors `port_runs.py`'s own
per-batch `runs.jsonl`, never `.port/ledger.jsonl`'s one-per-repo shape):
`<port_root>/.port/batches/<name>/handoff.json`. MACHINE-WRITTEN, OVERWRITTEN
whole on every write (never append-only — a handoff item's own STANDING can
go stale the moment the step that raised it runs clean again, so the right
shape is "the current truth", not "every truth that ever held").

WRITE DOOR — `write_step_items`: replaces the (step, dist) key's OWN items
with what THAT run reported (an empty list clears it), leaving every OTHER
step's items untouched. Whole-document atomic write
(`artifact_io.atomic_write_text`) — a reader (`hs-run port next`, `/hs:port`
pass 2) never observes a half-written file. Each item gets a STABLE id
(`h-<8 hex>` over `(step, dist, kind, path, why)`, sha256-derived) so a
re-run reporting the identical finding does not mint a fresh id every time —
a plan phase written against `h-0001` keeps naming the same finding across
reruns of the step that raised it. `done_when` is the literal `hs-code
--step ...` invocation the CALLER already ran (not re-derived here): the
criterion for an item being done is running that exact command again and
getting exit 0, which `hs-run port next` checks by re-rendering the SAME
command the run ledger already renders for that step/dist — see
`hs_run_port.py`'s own module docstring.

READ DOOR — `read_handoff`/`sha256_bytes`: `None` when no file exists yet (a
batch that never ran a step producing findings is a normal, unplanned-
nothing state). A file that exists but does not parse to the declared shape
raises `HandoffError` — never silently repaired or treated as empty; `hs-run
port next` reports that as a broken state naming the file, the same
"append-only-adjacent, never auto-rewritten" discipline every sibling
`.port/` document holds to even though THIS one is whole-document
overwritten (the INVARIANT that never changes is "a reader never guesses
past malformed content").
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import port_batch_reader  # noqa: E402
import register_store  # noqa: E402
from artifact_io import atomic_write_text  # noqa: E402

_SCHEMA_VERSION = 1

_TOP_KEYS = frozenset({"version", "batch", "source", "target_head", "mechanical_done", "items"})
_ITEM_KEYS = frozenset({"id", "kind", "path", "why", "step", "dist", "done_when"})


class HandoffError(ValueError):
    """`.port/batches/<b>/handoff.json` exists but is not well-formed — the
    caller (`hs-run port next`, `write_step_items`) refuses rather than
    silently treating it as empty or repairing it in place."""


def handoff_path(port_root: "str | Path", batch: str) -> Path:
    return port_batch_reader.batches_dir(port_root) / batch / "handoff.json"


def item_id(step: str, dist: "str | None", kind: str, path: str, why: str) -> str:
    """`h-<8 hex>` over `(step, dist, kind, path, why)` — stable across
    reruns: the SAME finding always mints the SAME id, so a plan phase
    written against one id keeps naming the same finding after a re-run
    reports it again unchanged. `\\x1f` (unit separator) joins the fields so
    a value carrying the literal string used elsewhere as a join cannot
    collide two different tuples onto the same digest."""
    digest = hashlib.sha256(
        "\x1f".join((step, dist or "", kind, path, why)).encode("utf-8")).hexdigest()
    return "h-%s" % digest[:8]


@dataclass(frozen=True)
class HandoffDoc:
    version: int
    batch: str
    source: "dict[str, Any]"
    target_head: "str | None"
    mechanical_done: "list[str]"
    items: "list[dict[str, Any]]"

    def as_dict(self) -> dict:
        return {
            "version": self.version, "batch": self.batch, "source": dict(self.source),
            "target_head": self.target_head, "mechanical_done": list(self.mechanical_done),
            "items": [dict(i) for i in self.items],
        }


def _validate(doc: Any, path: Path) -> None:
    if not isinstance(doc, dict):
        raise HandoffError("%s does not parse to a mapping (got %s)"
                          % (path, type(doc).__name__))
    missing = _TOP_KEYS - set(doc)
    extra = set(doc) - _TOP_KEYS
    if missing or extra:
        problems = []
        if missing:
            problems.append("missing key(s) %s" % sorted(missing))
        if extra:
            problems.append("unknown key(s) %s" % sorted(extra))
        raise HandoffError("%s: %s" % (path, "; ".join(problems)))
    if doc["version"] != _SCHEMA_VERSION:
        raise HandoffError("%s: unsupported version %r (known: %d)"
                          % (path, doc["version"], _SCHEMA_VERSION))
    if not isinstance(doc["batch"], str) or not doc["batch"]:
        raise HandoffError("%s: `batch` must be a non-empty string" % path)
    if not isinstance(doc["source"], dict):
        raise HandoffError("%s: `source` must be a mapping" % path)
    if doc["target_head"] is not None and not isinstance(doc["target_head"], str):
        raise HandoffError("%s: `target_head` must be a string or null" % path)
    if not isinstance(doc["mechanical_done"], list):
        raise HandoffError("%s: `mechanical_done` must be a list" % path)
    if not isinstance(doc["items"], list):
        raise HandoffError("%s: `items` must be a list" % path)
    for i, item in enumerate(doc["items"]):
        if not isinstance(item, dict):
            raise HandoffError("%s: items[%d] is not a mapping" % (path, i))
        item_missing = {"id", "kind", "path", "why", "step", "dist", "done_when"} - set(item)
        if item_missing:
            raise HandoffError("%s: items[%d] missing key(s) %s"
                              % (path, i, sorted(item_missing)))


def read_handoff(port_root: "str | Path", batch: str) -> "HandoffDoc | None":
    """`None` when no `handoff.json` exists yet for `batch` — a batch whose
    steps never reported a finding is a normal state, not an error. Raises
    `HandoffError` for a file that exists but does not parse to the
    declared shape."""
    path = handoff_path(port_root, batch)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HandoffError("%s does not parse as JSON (%s)" % (path, exc)) from exc
    _validate(doc, path)
    return HandoffDoc(
        version=doc["version"], batch=doc["batch"], source=dict(doc["source"]),
        target_head=doc["target_head"], mechanical_done=list(doc["mechanical_done"]),
        items=[dict(i) for i in doc["items"]])


def sha256_bytes(port_root: "str | Path", batch: str) -> "str | None":
    """sha256 of the RAW file bytes — the same hash `hs-run port next`
    compares against a batch's own declared `handoff_sha256`, and the same
    bytes `/hs:port` pass 2 hashes when it records that field. `None` when
    no `handoff.json` exists."""
    path = handoff_path(port_root, batch)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_step_items(port_root: "str | Path", batch: str, *, source: Mapping[str, Any],
                     target_head: "str | None", mechanical_done: Sequence[str],
                     step: str, dist: "str | None", done_when_cmd: str,
                     items: "Sequence[Mapping[str, Any]]") -> HandoffDoc:
    """Replace THIS `(step, dist)`'s own items with `items` (possibly empty
    — a clean re-run of a step that previously reported findings clears
    them), leaving every OTHER step's items exactly as they were. Each raw
    item is `{kind, path, why}` (an optional `node` passes through
    unchanged); this door adds `id` (`item_id`), `step`, `dist` and
    `done_when: {cmd: done_when_cmd, exit: 0}`.

    `source`/`target_head`/`mechanical_done` are written FRESH on every call
    (never merged with the prior document's own) — they describe the
    BATCH's current state, not any one step's, so the caller (which always
    knows the batch's current source/head/green-lane) supplies the truth
    each time. Raises `HandoffError` when an EXISTING `handoff.json` does
    not parse — never silently discarded and replaced with a smaller
    document that drops another step's items."""
    path = handoff_path(port_root, batch)
    path.parent.mkdir(parents=True, exist_ok=True)
    with register_store.register_lock(path.with_name(path.name + ".lock")):
        return _write_step_items_locked(
            port_root, batch, path, source=source, target_head=target_head,
            mechanical_done=mechanical_done, step=step, dist=dist,
            done_when_cmd=done_when_cmd, items=items)


def _write_step_items_locked(port_root, batch, path, *, source, target_head,
                             mechanical_done, step, dist, done_when_cmd, items) -> HandoffDoc:
    """`write_step_items`'s read-replace-write, under the batch's handoff lock:
    two `--step` runs of different dists must not drop each other's items."""
    existing = read_handoff(port_root, batch)
    kept = [it for it in (existing.items if existing else [])
           if not (it.get("step") == step and it.get("dist") == dist)]
    stamped: "list[dict[str, Any]]" = []
    for raw in items:
        kind, path_, why = raw["kind"], raw["path"], raw["why"]
        entry: "dict[str, Any]" = {
            "id": item_id(step, dist, kind, path_, why), "kind": kind, "path": path_,
            "why": why, "step": step, "dist": dist,
            "done_when": {"cmd": done_when_cmd, "exit": 0},
        }
        if raw.get("node") is not None:
            entry["node"] = raw["node"]
        stamped.append(entry)
    doc = HandoffDoc(
        version=_SCHEMA_VERSION, batch=batch, source=dict(source), target_head=target_head,
        mechanical_done=list(mechanical_done), items=kept + stamped)
    atomic_write_text(path, json.dumps(doc.as_dict(), ensure_ascii=False, indent=2,
                                       sort_keys=True) + "\n")
    return doc
