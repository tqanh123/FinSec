#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""advise_relay.py — the testable seam for the hs:advise relay mechanism.

A Claude Code subagent cannot call AskUserQuestion, so the advisor agent persists its working
state to a JSON file, emits a NEEDS_USER_INPUT marker carrying ONE question in the
AskUserQuestion schema, and ends its turn. The main orchestrator asks that question, then
re-spawns the advisor with the answer; the fresh advisor reads the state and resumes.

This module owns the deterministic, unit-testable half: atomic state I/O (machine-written
state is JSON, code-standards §3), the marker emit/parse, and the one-question schema check.
Mostly pure stdlib — importable by file path with no cross-tree dependency — except
`select_preview_route`, which lazily reaches into `harness/scripts/disabled_skills.py` (the
skill live/disabled/unknown resolver) to answer the NEEDS_PREVIEW routing question below.
"""
import json
import os
import re
import sys
from pathlib import Path

MARKER = "NEEDS_USER_INPUT"
_HEADER_MAX = 12

# harness/plugins/hs/skills/advise/scripts/advise_relay.py -> repo root is 6
# levels up (mirrors docs_gate.py's own walk from the same skill-tree depth).
_REPO_ROOT = Path(__file__).resolve().parents[6]


def read_state(path) -> dict:
    """Return the parsed relay state, or {} when the file is absent/unreadable (fresh run)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def write_state(path, state) -> None:
    """Atomically persist `state` as JSON: write a .tmp sibling, then os.replace so a re-spawned
    advisor never reads a half-written state file."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def validate_question(q) -> tuple:
    """Validate a single AskUserQuestion-shaped question. Returns (ok, reason).

    Enforces exactly the shape the orchestrator will pass verbatim to AskUserQuestion:
    a question string, a header <= 12 chars, a bool multiSelect, and 2-4 options each with a
    label. (The HARD-GATE-ONE-QUESTION rule lives in the skill; here we bound the schema.)"""
    if not isinstance(q, dict):
        return False, "not a dict"
    if not isinstance(q.get("question"), str) or not q["question"].strip():
        return False, "missing question text"
    header = q.get("header")
    if not isinstance(header, str) or not header.strip():
        return False, "missing header"
    if len(header) > _HEADER_MAX:
        return False, "header exceeds %d chars" % _HEADER_MAX
    if not isinstance(q.get("multiSelect"), bool):
        return False, "multiSelect must be a bool"
    options = q.get("options")
    if not isinstance(options, list) or not (2 <= len(options) <= 4):
        return False, "options must be a list of 2-4 entries"
    for opt in options:
        if not isinstance(opt, dict) or not str(opt.get("label", "")).strip():
            return False, "each option needs a label"
    return True, ""


def select_preview_route(root=".") -> str:
    """Return 'diagram' when `hs:diagram` reads live under `root`, else
    'mermaid' — the checkable branch a NEEDS_PREVIEW step must route on
    instead of a prose promise that assumes hs:diagram already shipped.
    Queries `disabled_skills.status`, the harness's own live/disabled/unknown
    resolver, rather than guessing from a cached skill list."""
    scripts_dir = _REPO_ROOT / "harness" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import disabled_skills
    sources = disabled_skills.default_sources(root)
    return "diagram" if disabled_skills.status("diagram", sources) == "live" else "mermaid"


def emit_needs_input(question) -> str:
    """Return the advisor's relay message: the marker on its own line, then one fenced json
    block holding the question. Raises ValueError if the question is out of schema — a courier
    must never emit a malformed question the orchestrator would pass on blindly."""
    ok, reason = validate_question(question)
    if not ok:
        raise ValueError("invalid relay question: %s" % reason)
    block = json.dumps(question, ensure_ascii=False, indent=2)
    return "%s\n```json\n%s\n```" % (MARKER, block)


def parse_needs_input(text) -> dict:
    """Extract the question dict from a NEEDS_USER_INPUT relay message. Returns {} when the text
    is not a relay message or carries no parseable json block."""
    idx = text.find(MARKER)
    if idx == -1:
        return {}
    # Anchor on the marker: parse the FIRST json block that follows it, so an
    # auxiliary ```json block earlier in the reply doesn't get picked instead.
    m = re.search(r"```json\s*(\{.*?\})\s*```", text[idx:], re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
