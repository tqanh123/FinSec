#!/usr/bin/env python3
"""skill_quality_gate.py — write-time SKILL.md content gate (compliance, fail-open on crash).

PostToolUse(Write|Edit|MultiEdit): block a SKILL.md write whose body carries a
HARD content finding, so a bad skill cannot land silently and wait for CI. The
decision this executes ordered exactly that split: HARD findings (a dangling
local reference, a machine-provenance birth-marker) block HERE at write time;
size stays with `--strict` CI on changed skills, and shape/orphan/PII findings
stay advisory in check_skill_structure's report. The brain is
check_skill_structure.write_gate_reason — this hook only routes a tool event
to it and turns a reason into a block.

Fail-open on its own errors (the dispatch row carries fail_open: a crashing
content heuristic must not wedge every SKILL.md write) and on absent input; a
returned finding still blocks. Honors the enabled gate like every compliance hook.
Inert for every file that is not a SKILL.md.
"""
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))
import hook_runtime  # noqa: E402

HOOK_CLASS = "compliance"
_NAME = "skill_quality_gate"


def core(data: dict):
    """None ⇒ allow; str ⇒ block reason. PostToolUse fires AFTER the write, so
    the block is an instruction to fix the file, not a prevention — the reason
    must stay actionable (write_gate_reason names the path and the rule)."""
    ti = data.get("tool_input")
    fp = ti.get("file_path") if isinstance(ti, dict) else None
    if not isinstance(fp, str) or not fp.endswith("SKILL.md"):
        return None
    import check_skill_structure
    return check_skill_structure.write_gate_reason(fp)


def main() -> int:
    hook_runtime.run_compliance_hook(_NAME, core)
    return 0


if __name__ == "__main__":
    sys.exit(main())
