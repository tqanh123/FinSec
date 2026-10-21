#!/usr/bin/env python3
"""diagram_engine.py — probe available layout engines and pick one per archetype.

Routing table (measured, not assumed -- references/engine-selection.md):
architecture/dataflow prefer ELK (nested boundaries/stages need ports and
container-aware routing); workflow/lifecycle use graphviz (a single flow/state
axis, no nesting); sequence needs neither -- it is a fixed linear lifeline
layout. Neither external engine is assumed present: this module probes before
anything downstream builds on the result, and falls back to a pure-Python grid
layout when a diagram_type's whole preference chain is unavailable, never a
crash or a silently wrong picture.
"""
from __future__ import annotations

import shutil
import subprocess

#: Preference chain per diagram_type. Empty means "no external engine used".
PREFERRED_CHAIN = {
    "architecture": ("elk", "graphviz"),
    "dataflow": ("elk", "graphviz"),
    "workflow": ("graphviz",),
    "lifecycle": ("graphviz",),
    "sequence": (),
}

FALLBACK_ENGINE = "fallback-grid"
SEQUENCE_ENGINE = "sequence-linear"


def probe_graphviz() -> dict:
    path = shutil.which("dot")
    if not path:
        return {"available": False, "reason": "dot not found on PATH"}
    try:
        proc = subprocess.run([path, "-V"], capture_output=True, text=True, timeout=5)
    except OSError as exc:
        return {"available": False, "reason": "dot -V failed: %s" % exc}
    version = (proc.stdout or proc.stderr).strip()
    return {"available": True, "path": path, "version": version}


def probe_elk() -> dict:
    """ELK (Eclipse Layout Kernel) needs a JVM or the elkjs Node bundle; this
    harness vendors neither today (no elkjs package, no jar -- see
    references/engine-selection.md for the measured reason and the EPL-2.0
    licence check a future vendoring step would still need). The probe checks
    for real so a later install that adds one flips this on with no code
    change here."""
    node = shutil.which("node")
    if not node:
        return {"available": False, "reason": "node not found on PATH"}
    try:
        proc = subprocess.run(
            [node, "-e", "require.resolve('elkjs')"],
            capture_output=True, text=True, timeout=5,
        )
    except OSError as exc:
        return {"available": False, "reason": "node probe failed: %s" % exc}
    if proc.returncode != 0:
        return {"available": False, "reason": "elkjs is not installed for node"}
    return {"available": True, "path": node}


_PROBES = {"graphviz": probe_graphviz, "elk": probe_elk}


def select_engine(diagram_type: str, probes: dict | None = None) -> dict:
    """Pick the layout engine for `diagram_type`.

    Returns {"engine": <name>, "tried": [...]}: `tried` is the audit trail
    (every candidate probed, in order, with why it was or was not picked) --
    the shape a rendered diagram's metadata footer and the CLI's --explain-
    engine flag both read, so a "why did I get the ugly fallback" question is
    always answerable from the artifact itself.

    `probes` overrides the real probe callables — dependency injection for
    tests; real callers omit it and get the live PATH probes above.
    """
    if diagram_type == "sequence":
        return {"engine": SEQUENCE_ENGINE, "tried": []}
    probes = probes or _PROBES
    chain = PREFERRED_CHAIN.get(diagram_type, ("graphviz",))
    tried = []
    for name in chain:
        result = probes[name]()
        tried.append({"engine": name, **result})
        if result.get("available"):
            return {"engine": name, "tried": tried}
    tried.append({"engine": FALLBACK_ENGINE, "available": True,
                   "reason": "no preferred engine available"})
    return {"engine": FALLBACK_ENGINE, "tried": tried}
