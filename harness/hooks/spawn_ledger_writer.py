#!/usr/bin/env python3
"""spawn_ledger_writer.py — append each Agent/Task spawn to the provenance ledger
(telemetry, fail-open).

The ledger (spawn-provenance/spawns.jsonl) is the disk fact the cook
attestation reads through `spawn_provenance.has_spawn()`: "did a spawn
attributable to THIS call actually happen, and was it the role the call asked
for?". Its writers used to be the two spawn-count hooks, removed for
over-blocking a sliding window; the reader stayed wired and nothing wrote the
ledger afterwards, so on a fresh install the attestation could never
legitimately pass.

The writer returns deliberately as TELEMETRY: it records the spawn and never
judges it, so nothing that can block rides back onto this lane — the counting
gates stay gone. Fails open on every error (a broken counter must never wedge
a spawn), honors the enabled gate like every telemetry hook.
"""
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))
import hook_runtime  # noqa: E402

HOOK_CLASS = "telemetry"
_STEM = "spawn_ledger_writer"


def core(data: dict) -> None:
    ti = data.get("tool_input")
    subagent_type = ti.get("subagent_type") if isinstance(ti, dict) else None
    import spawn_provenance
    spawn_provenance.record_spawn(
        data.get("session_id"),
        tool_use_id=data.get("tool_use_id"),
        # the dispatcher resolves the session's latest `hs-run` call once per group
        run_id=data.get("hs_run_id"),
        subagent_type=subagent_type if isinstance(subagent_type, str) and subagent_type else None,
    )


def main(raw=None) -> None:
    hook_runtime.run_telemetry_hook(_STEM, core, raw=raw)


if __name__ == "__main__":
    main()
