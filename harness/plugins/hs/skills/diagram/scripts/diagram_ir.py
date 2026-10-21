#!/usr/bin/env python3
"""diagram_ir.py — load and validate hs:diagram typed JSON IR.

Validation is hand-written, not delegated to the OPTIONAL `jsonschema` package:
this module's correctness must not depend on a library the harness only warns
about when absent (harness/requirements.txt). The five `schemas/*.schema.json`
files (plus the shared `common.schema.json`) are the machine-readable CONTRACT
external tools and editors can point at; `test_diagram_ir_validate.py` cross-
checks that the two never drift (same enums, same required fields).

Every failure accumulates instead of raising on the first one, and each error
names WHERE it happened (a JSON-pointer-shaped path) and WHAT to do about it
(a one-line hint) — a plain `raise ValueError(msg)` mid-document tells an author
about error #1 and hides #2 through #40.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
SCHEMAS_DIR = _HERE.parent / "schemas"

DIAGRAM_TYPES = ("architecture", "dataflow", "lifecycle", "sequence", "workflow")
VISUAL_PRESETS = ("classic", "blueprint")
THEMES = ("light", "dark")

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MAX_TEXT = 512


@dataclass(frozen=True)
class IRError:
    path: str
    message: str
    hint: str

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message, "hint": self.hint}


# --------------------------------------------------------------------- schema

def load_schema(diagram_type: str) -> dict:
    """diagram_type's schema with common.schema.json's $defs merged in, so a
    `#/$defs/...` $ref resolves without an external resolver library."""
    if diagram_type not in DIAGRAM_TYPES:
        raise ValueError("unknown diagram_type %r" % (diagram_type,))
    common = json.loads((SCHEMAS_DIR / "common.schema.json").read_text(encoding="utf-8"))
    own = json.loads((SCHEMAS_DIR / f"{diagram_type}.schema.json").read_text(encoding="utf-8"))
    merged = dict(common["$defs"])
    merged.update(own.get("$defs", {}))
    own["$defs"] = merged
    return own


def load_ir(path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ------------------------------------------------------------------- helpers

def _err(errors: list, path: str, message: str, hint: str) -> None:
    errors.append(IRError(path=path, message=message, hint=hint))


def _check_text(errors: list, value: Any, path: str, required: bool = True) -> None:
    if value is None:
        if required:
            _err(errors, path, "missing required text field", "add a non-empty string")
        return
    if not isinstance(value, str) or not value.strip():
        _err(errors, path, "must be a non-empty string", "provide a non-empty string value")
        return
    if len(value) > _MAX_TEXT:
        _err(errors, path, "exceeds %d characters" % _MAX_TEXT,
             "shorten to at most %d characters" % _MAX_TEXT)


def _check_id(errors: list, value: Any, path: str, seen: dict) -> None:
    if not isinstance(value, str) or not value:
        _err(errors, path, "id must be a non-empty string", 'add a short id, e.g. "svc-1"')
        return
    if not _ID_RE.match(value):
        _err(errors, path, '"%s" does not match the id pattern' % value,
             "ids match ^[a-zA-Z0-9_-]{1,64}$ (letters, digits, - and _, 1-64 chars)")
        return
    if value in seen:
        _err(errors, path, 'duplicate id "%s" (first declared at %s)' % (value, seen[value]),
             "give each entity in this document a unique id")
        return
    seen[value] = path


def _check_enum(errors: list, value: Any, path: str, allowed, field: str) -> None:
    if value is None:
        return
    if value not in allowed:
        _err(errors, path, '"%s" is not a valid %s' % (value, field),
             "use one of: %s" % ", ".join(allowed))


def _check_ref(errors: list, value: Any, path: str, known: dict, field: str) -> None:
    if value is None:
        _err(errors, path, "missing required %s endpoint" % field,
             "point it at an id declared earlier in this document")
        return
    if not isinstance(value, str) or not value:
        _err(errors, path, "%s endpoint must be a non-empty string" % field,
             "point it at an id declared earlier in this document")
        return
    if value not in known:
        _err(errors, path, '"%s" does not name a declared %s' % (value, field),
             "point it at an id declared earlier in this document")


def _check_int_range(errors: list, value: Any, path: str, lo: int, hi: int, field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        _err(errors, path, "%s must be an integer between %d and %d" % (field, lo, hi),
             "set %s to an int in [%d, %d] or omit it" % (field, lo, hi))


def _check_array(errors: list, value: Any, path: str, *, required: bool, max_items: int,
                  label: str) -> list:
    if value is None:
        if required:
            _err(errors, path, "%s requires a non-empty array" % label,
                 "add at least one entry to %s" % path)
        return []
    if not isinstance(value, list):
        _err(errors, path, "%s must be an array" % label, "wrap %s in a list" % path)
        return []
    if required and not value:
        _err(errors, path, "%s requires a non-empty array" % label,
             "add at least one entry to %s" % path)
    if len(value) > max_items:
        _err(errors, path, "%s exceeds the limit of %d" % (label, max_items),
             "split the diagram or drop unused entries")
    return value


def _check_meta(errors: list, meta: Any) -> None:
    if not isinstance(meta, dict):
        _err(errors, "$.meta", "meta must be an object", 'add "meta": {"title": "..."}')
        return
    _check_text(errors, meta.get("title"), "$.meta.title")
    if meta.get("subtitle") is not None:
        _check_text(errors, meta.get("subtitle"), "$.meta.subtitle", required=False)
    if meta.get("description") is not None:
        _check_text(errors, meta.get("description"), "$.meta.description", required=False)
    _check_enum(errors, meta.get("visual_preset"), "$.meta.visual_preset", VISUAL_PRESETS,
                "visual_preset")
    _check_enum(errors, meta.get("theme"), "$.meta.theme", THEMES, "theme")


# ----------------------------------------------------------------- per-type

_ARCH_ROLES = ("frontend", "backend", "database", "cache", "queue", "storage",
               "gateway", "auth", "external", "worker", "person", "system")
_BOUNDARY_ROLES = ("cloud", "vpc", "cluster", "trust-boundary", "private-network",
                    "external-zone")
_CONN_KINDS = ("sync", "async", "stream", "fallback", "bi-directional")


def _validate_architecture(errors: list, data: dict) -> None:
    known: dict = {}
    components = _check_array(errors, data.get("components"), "$.components",
                               required=True, max_items=250, label="architecture components")
    for idx, c in enumerate(components):
        base = "$.components[%d]" % idx
        if not isinstance(c, dict):
            _err(errors, base, "each component must be an object",
                 'wrap it in {"id": ..., "label": ...}')
            continue
        _check_id(errors, c.get("id"), base + ".id", known)
        _check_text(errors, c.get("label"), base + ".label")
        _check_enum(errors, c.get("role"), base + ".role", _ARCH_ROLES, "role")
        _check_int_range(errors, c.get("layer"), base + ".layer", 0, 10, "layer")
        _check_text(errors, c.get("technology"), base + ".technology", required=False)
        _check_text(errors, c.get("description"), base + ".description", required=False)

    boundaries = _check_array(errors, data.get("boundaries"), "$.boundaries",
                               required=False, max_items=250, label="boundaries")
    for idx, b in enumerate(boundaries):
        base = "$.boundaries[%d]" % idx
        if not isinstance(b, dict):
            _err(errors, base, "each boundary must be an object",
                 'wrap it in {"id": ..., "label": ..., "components": [...]}')
            continue
        _check_id(errors, b.get("id"), base + ".id", known)
        _check_text(errors, b.get("label"), base + ".label")
        _check_enum(errors, b.get("role"), base + ".role", _BOUNDARY_ROLES, "role")
        members = b.get("components")
        if members is not None and not isinstance(members, list):
            _err(errors, base + ".components", "components must be an array of ids",
                 "wrap the member ids in a list")
        elif isinstance(members, list):
            for midx, member in enumerate(members):
                _check_ref(errors, member, "%s.components[%d]" % (base, midx), known,
                           "component")

    connections = _check_array(errors, data.get("connections"), "$.connections",
                                required=False, max_items=1000, label="connections")
    for idx, conn in enumerate(connections):
        base = "$.connections[%d]" % idx
        if not isinstance(conn, dict):
            _err(errors, base, "each connection must be an object",
                 'wrap it in {"from": ..., "to": ...}')
            continue
        _check_text(errors, conn.get("label"), base + ".label", required=False)
        _check_enum(errors, conn.get("kind"), base + ".kind", _CONN_KINDS, "kind")
        _check_ref(errors, conn.get("from"), base + ".from", known, "component")
        _check_ref(errors, conn.get("to"), base + ".to", known, "component")


_LANE_ROLES = ("user", "frontend", "orchestrator", "worker", "approver", "system")
_STEP_KINDS = ("start", "action", "decision", "wait", "subprocess",
               "terminal-success", "terminal-failure")
_TRANSITION_KINDS = ("normal", "branch-true", "branch-false", "retry", "exception")


def _validate_workflow(errors: list, data: dict) -> None:
    lane_ids: dict = {}
    lanes = _check_array(errors, data.get("lanes"), "$.lanes",
                          required=False, max_items=250, label="lanes")
    for idx, lane in enumerate(lanes):
        base = "$.lanes[%d]" % idx
        if not isinstance(lane, dict):
            _err(errors, base, "each lane must be an object",
                 'wrap it in {"id": ..., "label": ...}')
            continue
        _check_id(errors, lane.get("id"), base + ".id", lane_ids)
        _check_text(errors, lane.get("label"), base + ".label")
        _check_enum(errors, lane.get("role"), base + ".role", _LANE_ROLES, "role")

    step_ids: dict = {}
    steps = _check_array(errors, data.get("steps"), "$.steps",
                          required=True, max_items=250, label="workflow steps")
    for idx, s in enumerate(steps):
        base = "$.steps[%d]" % idx
        if not isinstance(s, dict):
            _err(errors, base, "each step must be an object",
                 'wrap it in {"id": ..., "label": ...}')
            continue
        _check_id(errors, s.get("id"), base + ".id", step_ids)
        _check_text(errors, s.get("label"), base + ".label")
        _check_enum(errors, s.get("kind"), base + ".kind", _STEP_KINDS, "kind")
        if s.get("lane") is not None:
            _check_ref(errors, s.get("lane"), base + ".lane", lane_ids, "lane")

    transitions = _check_array(errors, data.get("transitions"), "$.transitions",
                                required=False, max_items=1000, label="transitions")
    for idx, t in enumerate(transitions):
        base = "$.transitions[%d]" % idx
        if not isinstance(t, dict):
            _err(errors, base, "each transition must be an object",
                 'wrap it in {"from": ..., "to": ...}')
            continue
        _check_text(errors, t.get("label"), base + ".label", required=False)
        _check_text(errors, t.get("condition"), base + ".condition", required=False)
        _check_enum(errors, t.get("kind"), base + ".kind", _TRANSITION_KINDS, "kind")
        _check_ref(errors, t.get("from"), base + ".from", step_ids, "step")
        _check_ref(errors, t.get("to"), base + ".to", step_ids, "step")


_PARTICIPANT_ROLES = ("client", "service", "database", "gateway", "external", "queue")
_MESSAGE_KINDS = ("sync-call", "async-signal", "return", "self-call", "error")


def _validate_sequence(errors: list, data: dict) -> None:
    participant_ids: dict = {}
    participants = _check_array(errors, data.get("participants"), "$.participants",
                                 required=True, max_items=50, label="sequence participants")
    if isinstance(data.get("participants"), list) and 0 < len(data["participants"]) < 2:
        _err(errors, "$.participants", "sequence requires at least 2 participants",
             "add a second participant")
    for idx, p in enumerate(participants):
        base = "$.participants[%d]" % idx
        if not isinstance(p, dict):
            _err(errors, base, "each participant must be an object",
                 'wrap it in {"id": ..., "label": ...}')
            continue
        _check_id(errors, p.get("id"), base + ".id", participant_ids)
        _check_text(errors, p.get("label"), base + ".label")
        _check_enum(errors, p.get("role"), base + ".role", _PARTICIPANT_ROLES, "role")

    messages = _check_array(errors, data.get("messages"), "$.messages",
                             required=False, max_items=500, label="messages")
    for idx, m in enumerate(messages):
        base = "$.messages[%d]" % idx
        if not isinstance(m, dict):
            _err(errors, base, "each message must be an object",
                 'wrap it in {"from": ..., "to": ..., "label": ...}')
            continue
        _check_text(errors, m.get("label"), base + ".label")
        _check_enum(errors, m.get("kind"), base + ".kind", _MESSAGE_KINDS, "kind")
        _check_ref(errors, m.get("from"), base + ".from", participant_ids, "participant")
        _check_ref(errors, m.get("to"), base + ".to", participant_ids, "participant")


_DATAFLOW_ROLES = ("source", "transform", "store", "sink", "consumer", "filter", "governance")
_CLASSIFICATIONS = ("public", "internal", "confidential", "pii", "restricted")


def _validate_dataflow(errors: list, data: dict) -> None:
    stage_ids: dict = {}
    stages = _check_array(errors, data.get("stages"), "$.stages",
                           required=False, max_items=250, label="stages")
    for idx, st in enumerate(stages):
        base = "$.stages[%d]" % idx
        if not isinstance(st, dict):
            _err(errors, base, "each stage must be an object",
                 'wrap it in {"id": ..., "label": ..., "order": 0}')
            continue
        _check_id(errors, st.get("id"), base + ".id", stage_ids)
        _check_text(errors, st.get("label"), base + ".label")
        order = st.get("order")
        if order is None:
            _err(errors, base + ".order", "missing required integer order",
                 "set order to an integer starting at 0")
        elif isinstance(order, bool) or not isinstance(order, int) or order < 0:
            _err(errors, base + ".order", "order must be a non-negative integer",
                 "set order to an integer >= 0")

    node_ids: dict = {}
    nodes = _check_array(errors, data.get("nodes"), "$.nodes",
                          required=True, max_items=250, label="dataflow nodes")
    for idx, n in enumerate(nodes):
        base = "$.nodes[%d]" % idx
        if not isinstance(n, dict):
            _err(errors, base, "each node must be an object",
                 'wrap it in {"id": ..., "label": ...}')
            continue
        _check_id(errors, n.get("id"), base + ".id", node_ids)
        _check_text(errors, n.get("label"), base + ".label")
        _check_enum(errors, n.get("role"), base + ".role", _DATAFLOW_ROLES, "role")
        _check_enum(errors, n.get("classification"), base + ".classification",
                    _CLASSIFICATIONS, "classification")
        if n.get("stage") is not None:
            _check_ref(errors, n.get("stage"), base + ".stage", stage_ids, "stage")

    flows = _check_array(errors, data.get("flows"), "$.flows",
                          required=False, max_items=1000, label="flows")
    for idx, f in enumerate(flows):
        base = "$.flows[%d]" % idx
        if not isinstance(f, dict):
            _err(errors, base, "each flow must be an object",
                 'wrap it in {"from": ..., "to": ...}')
            continue
        _check_text(errors, f.get("label"), base + ".label", required=False)
        _check_ref(errors, f.get("from"), base + ".from", node_ids, "node")
        _check_ref(errors, f.get("to"), base + ".to", node_ids, "node")


_STATE_KINDS = ("initial", "active", "waiting", "failure-recoverable", "failure-fatal",
                "terminal-success", "terminal-cancelled")
_LIFECYCLE_TRANSITION_KINDS = ("normal", "retry", "timeout", "cancel", "fail")


def _validate_lifecycle(errors: list, data: dict) -> None:
    state_ids: dict = {}
    states = _check_array(errors, data.get("states"), "$.states",
                           required=True, max_items=250, label="lifecycle states")
    for idx, s in enumerate(states):
        base = "$.states[%d]" % idx
        if not isinstance(s, dict):
            _err(errors, base, "each state must be an object",
                 'wrap it in {"id": ..., "label": ...}')
            continue
        _check_id(errors, s.get("id"), base + ".id", state_ids)
        _check_text(errors, s.get("label"), base + ".label")
        _check_enum(errors, s.get("kind"), base + ".kind", _STATE_KINDS, "kind")

    transitions = _check_array(errors, data.get("transitions"), "$.transitions",
                                required=False, max_items=1000, label="transitions")
    for idx, t in enumerate(transitions):
        base = "$.transitions[%d]" % idx
        if not isinstance(t, dict):
            _err(errors, base, "each transition must be an object",
                 'wrap it in {"from": ..., "to": ...}')
            continue
        _check_text(errors, t.get("event"), base + ".event", required=False)
        _check_text(errors, t.get("action"), base + ".action", required=False)
        _check_enum(errors, t.get("kind"), base + ".kind", _LIFECYCLE_TRANSITION_KINDS, "kind")
        _check_ref(errors, t.get("from"), base + ".from", state_ids, "state")
        _check_ref(errors, t.get("to"), base + ".to", state_ids, "state")


_VALIDATORS = {
    "architecture": _validate_architecture,
    "workflow": _validate_workflow,
    "sequence": _validate_sequence,
    "dataflow": _validate_dataflow,
    "lifecycle": _validate_lifecycle,
}


def validate_ir(data: Any) -> list:
    """Validate one IR document. Returns [] when it is valid, else IRError list."""
    errors: list = []
    if not isinstance(data, dict):
        _err(errors, "$", "the document root must be an object", "wrap the IR in a JSON object")
        return errors

    schema_version = data.get("schema_version")
    if schema_version != 1:
        _err(errors, "$.schema_version", "schema_version must be 1",
             'set "schema_version": 1')

    diagram_type = data.get("diagram_type")
    if diagram_type not in DIAGRAM_TYPES:
        _err(errors, "$.diagram_type", "unknown diagram_type %r" % (diagram_type,),
             "use one of: %s" % ", ".join(DIAGRAM_TYPES))
        return errors  # nothing further can be checked without a known type

    _check_meta(errors, data.get("meta"))
    _VALIDATORS[diagram_type](errors, data)
    return errors


# ------------------------------------------------------------------------ CLI

def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate an hs:diagram IR document")
    parser.add_argument("input")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    data = load_ir(args.input)
    errors = validate_ir(data)
    if args.json:
        print(json.dumps({"valid": not errors, "errors": [e.to_dict() for e in errors]}))
    else:
        print("valid" if not errors else "%d error(s)" % len(errors))
        for e in errors:
            print("%s: %s -- %s" % (e.path, e.message, e.hint))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(_main())
