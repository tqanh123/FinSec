#!/usr/bin/env python3
"""usage_nudge.py — advisory near-ceiling reminder before a heavy spawn (nudge class).

Fires on `PreToolUse` for `Agent|Task`/`Workflow`: reads the SAME local usage
calculator (`usage_meter.py`) the `hs-usage` CLI and the ccstatusline segment
read, and — only when a window's local estimate is at or above the
configured threshold — prints one advisory line before the spawn goes ahead.

Nudge posture: fail-open, never blocks. A crash, a missing/malformed
`usage-plans.yaml`, or no local transcripts at all all read the same as
"nothing to report" — this hook's only job is to say something when there IS
a real near-ceiling signal, never to guess one into existence. The binding
HOOK_CLASS lives here in code, never in config.
"""

import os
import sys
from pathlib import Path

# Diagnostic text carries Vietnamese; guard stderr encoding so a non-UTF-8
# locale degrades to replacement chars instead of raising mid-write (fail-open).
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001 — older streams / already-detached; never fatal
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import hook_runtime  # noqa: E402

HOOK_CLASS = "nudge"
_NAME = Path(__file__).stem


def core(data: dict):
    """The advisory string when any window is at/above the configured
    threshold, else `None`. Routing (which channel, whether the hook is even
    enabled) is the caller's job via `hook_runtime` — this only decides
    WHETHER there is something to say."""
    import usage_meter

    plans_doc = usage_meter.load_plans()
    rows = usage_meter.compute_all(plans_doc=plans_doc)
    return usage_meter.nudge_message(rows, usage_meter.nudge_threshold(plans_doc))


def main() -> int:
    if not hook_runtime.hook_enabled(_NAME, HOOK_CLASS):
        hook_runtime.emit_continue()
        return 0
    data = hook_runtime.read_stdin_json()
    d = data if isinstance(data, dict) else {}
    try:
        msg = core(d)
        if msg:
            hook_runtime.emit_nudge_and_continue(_NAME, msg, d)
            return 0
    except Exception as e:  # noqa: BLE001 — fail-open: a nudge never blocks the tool
        hook_runtime.log_hook_error(_NAME, e)
    hook_runtime.emit_continue()
    return 0


if __name__ == "__main__":
    sys.exit(main())
