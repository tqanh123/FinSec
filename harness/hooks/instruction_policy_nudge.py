#!/usr/bin/env python3
"""instruction_policy_nudge.py — PreToolUse nudge: say what the project's adopted
instruction policy says about this edit or this landing.

Reads the active document only (`instruction_policy.active_document`): no document, a
broken one or a conflicting one answers nothing. Two of the three rule forms speak here:

  - forbid-edit on Write/Edit/MultiEdit: the target path matches the rule's pattern
  - require-check on a Bash `git commit` / `git push`: the rule's check is named as a
    reminder (whether it already ran is not something this hook can see)

The third form, prefer-command, rewrites the command in `instruction_policy_rewrite`.
Every answer names the rule id and the instruction line it was compiled from.

Posture: nudge, fail-open silently — a policy that cannot be read never stops a call.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(Path(_HOOKS_DIR).parents[1] / "harness" / "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)

HOOK_CLASS = "nudge"
_NAME = Path(__file__).stem
_EDIT_TOOLS = ("Write", "Edit", "MultiEdit")


def _project_root() -> Optional[Path]:
    import harness_root
    try:
        return Path(harness_root.resolve_project_root()[0])
    except Exception:  # noqa: BLE001 — no project, nothing to say
        return None


def core(data: Dict[str, Any]) -> Optional[str]:
    if not isinstance(data, dict):
        return None  # skip:not-applicable
    tool = data.get("tool_name")
    raw = data.get("tool_input")
    tin: dict = raw if isinstance(raw, dict) else {}
    if tool not in _EDIT_TOOLS and tool != "Bash":
        return None  # skip:not-applicable
    import hook_runtime
    root = _project_root()
    if root is None:
        hook_runtime.trace_skip(_NAME, "root-unresolvable")
        return None
    import instruction_policy as ip
    if not ip.policy_path(root).is_file():
        return None  # skip:not-applicable
    doc = ip.active_document(root)
    if doc is None:
        hook_runtime.trace_skip(_NAME, "config-unreadable")
        return None
    if tool in _EDIT_TOOLS:
        target = tin.get("file_path") or ""
        try:
            rel = Path(target).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = target
        ans = ip.check(doc, path=rel)
    else:
        ans = ip.check(doc, command=tin.get("command") or "")
        ans["decisions"] = [d for d in ans["decisions"] if d["action"] == "warn"]
    if not ans["decisions"]:
        return None  # skip:nothing-to-report
    lines = []
    for d in ans["decisions"]:
        what = ("run `%s` first" % d["ran"]) if d.get("ran") else "this path is one the project asks not to edit"
        lines.append("policy %s (%s): %s" % (d["id"], ip.where(d["provenance"]), what))
    return "\n".join(lines)
