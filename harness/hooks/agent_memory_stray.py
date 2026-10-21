#!/usr/bin/env python3
"""agent_memory_stray.py — SubagentStop telemetry hook: remove the empty
`.claude/agent-memory/<agent>/` dirs Claude Code leaves below the project root.

A `memory: project` agent gets its memory dir relative to the session's cwd, so a
session standing in `plans/<p>/reports/` leaves `plans/<p>/reports/.claude/
agent-memory/hs-red-teamer/` behind. From the cwd up to (not including) the project
root, an empty one is `rmdir`ed with whatever parents that leaves empty — `rmdir`
cannot remove content. One holding files is named and left alone. The project
root's own `.claude/` is never touched; a cwd outside the project does nothing.

The dir lands at the cwd the subagent was SPAWNED in, which the host's payload
`cwd` need not be (the session may have moved, and host versions differ on what
that field carries) — so the spawn cwd read from the subagent's own transcript is
swept too.
"""
import json
import os
import sys
from pathlib import Path

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import hook_runtime  # noqa: E402

HOOK_CLASS = "telemetry"
_NAME = Path(__file__).stem
_MEMORY_DIRS = ("agent-memory", "agent-memory-local")
_TRANSCRIPT_HEAD = 50


def _rmdir(d: Path) -> bool:
    try:
        d.rmdir()
        return True
    except OSError:
        return False


def _sweep(base: Path) -> list:
    """Remove empty agent dirs under `base/.claude/agent-memory*`; return the kept ones."""
    claude = base / ".claude"
    kept = []
    for name in _MEMORY_DIRS:
        mem = claude / name
        if mem.is_symlink() or not mem.is_dir():
            continue
        for agent in sorted(mem.iterdir()):
            if agent.is_dir() and not agent.is_symlink() and not _rmdir(agent):
                kept.append(agent)
        _rmdir(mem)
    if claude.is_dir() and not claude.is_symlink():
        _rmdir(claude)
    return kept


def _spawn_cwd(data: dict):
    """The `cwd` the subagent's transcript records, or None (missing/unreadable)."""
    tp = hook_runtime.subagent_transcript(data)
    if not tp:
        return None
    try:
        with open(tp, encoding="utf-8") as fh:
            for _, line in zip(range(_TRANSCRIPT_HEAD), fh):
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if cwd:
                    return str(cwd)
    except OSError:
        return None
    return None


def core(data: dict) -> None:
    root = hook_runtime.project_dir(None)
    starts = [c for c in (data.get("cwd"), _spawn_cwd(data)) if c]
    if not starts or not root:
        hook_runtime.trace_skip(_NAME, "root-unresolvable", session=data.get("session_id"))
        return
    root_r = Path(root).resolve()
    kept, seen = [], set()
    for start in starts:
        here = Path(start).resolve()
        if root_r not in here.parents:
            continue  # skip:not-applicable
        while here != root_r and here not in seen:
            seen.add(here)
            kept.extend(_sweep(here))
            here = here.parent
    if kept:
        hook_runtime.emit_nudge(
            _NAME,
            "[advisory] agent memory landed outside the project root (the session's cwd was "
            "below it when the agent started) and holds files, so it was left in place: %s "
            "— move what is worth keeping to %s/.claude/ and remove the rest"
            % (", ".join(str(k) for k in kept[:5]), root_r),
            session=data.get("session_id"), default_channel="systemMessage")


def main() -> int:
    hook_runtime.run_telemetry_hook(_NAME, core)
    return 0


if __name__ == "__main__":
    sys.exit(main())
