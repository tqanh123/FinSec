#!/usr/bin/env python3
"""instruction_policy_rewrite.py — PreToolUse(Bash): run the command the project's
adopted policy prefers (`prefer-command`), in place of the one written.

Only the program word is replaced, and only by one program word (`check_schema` refuses a
replacement carrying anything else), so the rewrite keeps the call's meaning: `npm test`
becomes `pnpm test`. The answer is the dispatcher's third answer, `{"updatedInput": ...}`.

It sits LAST in its dispatch group: every gate before it has already allowed the command
as written. A command carrying `${secret:REF}` is left to `secrets_inject`, the other hook
that rewrites Bash input — two rewrites of one call would race.

Posture: compliance for the rewrite channel, registered fail_open — it never returns a
reason, so it can never block, and an unreadable, broken or conflicting policy answers
nothing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(Path(_HOOKS_DIR).parents[1] / "harness" / "scripts"))

HOOK_CLASS = "compliance"


def core(data: Dict[str, Any]) -> Optional[dict]:
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None  # skip:not-applicable
    tin = data.get("tool_input")
    command = tin.get("command") if isinstance(tin, dict) else None
    if not isinstance(command, str) or not command.strip() or "${secret:" in command:
        return None  # skip:not-applicable
    import harness_root
    import instruction_policy as ip
    try:
        root = Path(harness_root.resolve_project_root()[0])
    except Exception:  # noqa: BLE001 — no project, nothing to rewrite
        return None  # skip:not-applicable
    doc = ip.active_document(root)
    if doc is None:
        return None  # skip:not-applicable
    new = ip.rewrite_command(doc, command)
    if new is None:
        return None  # skip:nothing-to-report
    return {"updatedInput": dict(tin if isinstance(tin, dict) else {}, command=new)}
