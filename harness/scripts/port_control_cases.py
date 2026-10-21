#!/usr/bin/env python3
"""port_control_cases.py — the port tool's seeded positive-control cases.

The port gate page's `control` row replays a handful of miniature port
workspaces — each carrying ONE seeded defect the tool must flag — through
the SAME `kind: "ledger"` engine door `hs-code check gates` uses for a real
batch (user decision: "a few known cases, a few seconds, run on the PORTING
machine before each batch; red when the tool fails to catch a seeded
case"). Upstream's own step ran the port TOOL's pytest suite
(`close_batch.py` `_step_positive_control`, ob-tools@60075cb); a harness
install ships no test tree to a porting machine, so the cases ship as DATA
instead — a port root in miniature under `harness/data/port-control/
cases/<id>/port/` plus an `expected.json` naming the law a healthy tool
flags it with.

`port`, not `.port`, on purpose: a directory named `.port` inside the bin
tree would be mistaken for a real workspace by the very walk-up
(`port_workspace.find_port_root`) this tool runs — the runner renames it
in the TEMP COPY it executes, so the shipped tree never carries a live
`.port/` and the bin tree stays read-only-safe (nothing is ever written
under `harness/data/`).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

_CASES_REL = Path("harness") / "data" / "port-control" / "cases"


def cases_root(bin_root: Path) -> Path:
    """Where the shipped seed cases live under `bin_root`."""
    return Path(bin_root) / _CASES_REL


def _expected_of(case_dir: Path) -> str:
    return str(json.loads((case_dir / "expected.json").read_text(
        encoding="utf-8"))["law"])


def run_case(engine_door, case_dir: Path, work: Path) -> Dict[str, Any]:
    """One seed, one outcome row. `engine_door(port_root)` is supplied by
    the caller so this module never imports the rules engine itself (the
    mutation tests point THAT import at a copied, deliberately broken
    tree; this one must keep resolving against the real one)."""
    import port_workspace  # the same walk-up every port verb trusts

    expected = _expected_of(case_dir)
    staged = work / case_dir.name
    shutil.copytree(case_dir, staged)
    (staged / ".port").mkdir(exist_ok=True)  # rename port/ -> .port in the copy
    for entry in (staged / "port").iterdir():
        shutil.move(str(entry), str(staged / ".port" / entry.name))
    (staged / "port").rmdir()
    try:
        port_root = port_workspace.find_port_root(staged)
    except (port_workspace.PortRootNotFoundError,
            port_workspace.BadRepoPathError) as e:
        return {"case": case_dir.name, "expected": expected, "caught": False,
                "detail": "fixture is not a port workspace: %s" % e}
    t0 = time.monotonic()
    try:
        res = engine_door(port_root)
    except Exception as e:  # noqa: BLE001 — the L0 seed's shape IS a refusal
        caught = expected == "L0"
        detail = ("ledger refused (%s)" if caught else "engine error (%s)") % e
        return {"case": case_dir.name, "expected": expected, "caught": caught,
                "detail": detail, "seconds": round(time.monotonic() - t0, 3)}
    laws = [v.cause for v in res.violations]
    return {"case": case_dir.name, "expected": expected,
            "caught": expected in laws,
            "detail": "flagged %s" % (laws or "nothing"),
            "seconds": round(time.monotonic() - t0, 3)}


def run_cases(bin_root: Path) -> List[Dict[str, Any]]:
    """Every shipped seed, against the tool as installed under `bin_root`."""
    from codegraph._core.rules import RuleContext, RulesEngine

    def door(port_root: Path):
        engine = RulesEngine([{"id": "control", "kind": "ledger",
                               "batch": "case"}])
        return engine.run(RuleContext(repo=port_root))[0]

    out: List[Dict[str, Any]] = []
    root = cases_root(bin_root)
    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        with tempfile.TemporaryDirectory(prefix="port-control-") as tmp:
            out.append(run_case(door, case_dir, Path(tmp)))
    return out


def run_control_row(target: Path) -> List[Dict[str, Any]]:
    """The `control` gate row's runner entry: the shipped seeds, run
    against the BIN's own tool (never `target`'s tree — the row measures
    the detector, not the ported repo)."""
    import harness_paths

    return run_cases(harness_paths.bin_root())
