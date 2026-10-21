#!/usr/bin/env python3
"""codemap_areas.py — gate-registry source for the `code` page: the repo's
`codemap.yaml` `rules:` as `{rule id: {kind}}` (declared in
harness/data/gate-registry.d/code.yaml). A repo without codemap.yaml has none.

The module used to also define the "area" half of the codemap —
`load_roots`/`area_root`/`debts_path_for`, the DEBTS.md ledger's path
resolution — which its own docstring already called a test-only surface
(codemap.yaml is `obt`'s dev-repo input and every caller sat under
harness/tests/). Those three moved to harness/tests/_codemap_debts.py when
the function-orphan sweep required every ship def to be reachable from a
real entry point; `rule_gates` stays, reachable through its registry entry.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import yaml_io  # noqa: E402


def rule_gates(root) -> dict:
    """Gate-registry source for the `code` page: `{rule id: {kind}}` from the repo's
    `codemap.yaml` `rules:`; a repo without codemap.yaml has none."""
    path = Path(root) / "codemap.yaml"
    if not path.is_file():
        return {}
    data = yaml_io.yaml_load(path.read_text(encoding="utf-8")) or {}
    return {r["id"]: {"kind": r.get("kind")} for r in (data.get("rules") or []) if r.get("id")}
