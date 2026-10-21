"""structural_change — is a diff STRUCTURAL? The one answer the review briefing
(`hs_run_review`), the rule-scan it records (`rule_view.build_rule_scan`) and
the pr/ship gate (`artifact_check._architecture_review_consistency`) share.

Structural = the diff adds or removes a module of this tree, or changes an
import edge between two of its modules. That is read off the import graph by
`hs-code change structural` (`codegraph/change/structural.py`), asked through
the CLI rather than imported: this module sits in hs-run's import closure,
which never reaches `codegraph` (`test_codegraph_snapshot_reader.py`).

A file the graph cannot read — no `codemap.yaml`, no declared root owns it,
not Python, does not parse — is judged by the declared `structural_globs`
(`standards.yaml drift:`), so a policy file or a shell script under a
structural tree still counts. With no graph at all every file is judged that
way, which is what the glob gate did before the graph existed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

_HS_CODE = Path(__file__).resolve().parent / "hs_code.py"

# Generic cross-language fallback: a diff intersecting one of these is structural.
# Mirrors the shipped harness/data/standards.yaml drift.structural_globs; used only
# when neither the env override nor a readable standards.yaml carries the key.
DEFAULT_STRUCTURAL_GLOBS = (
    "src/**", "lib/**", "packages/**", "internal/**", "services/**",
    "modules/**", "**/schemas/*.json", "**/migrations/**",
)

UNREAD = "the import graph cannot read it"


def read_drift_section(root):
    """The drift: mapping from the highest-precedence readable source, or None.
    Precedence: $HARNESS_STANDARDS_CONFIG file -> <root>/harness/data/standards.yaml
    -> the shipped standards.yaml beside this module. Fail-open: an unreadable or
    drift-less source falls through; never raises into the gate."""
    # env > the named tree > beside this module, declared once in
    # config-layers.yaml. read_soft never raises and names any layer it fell through.
    import config_layers
    for candidate_root in (root, None):
        data, _diag = config_layers.read_soft("standards.yaml", root=candidate_root)
        if isinstance(data, dict) and isinstance(data.get("drift"), dict):
            return data["drift"]
    return None


def load_structural_globs(root) -> "list[str]":
    drift = read_drift_section(root)
    if drift:
        globs = drift.get("structural_globs")
        if isinstance(globs, list) and globs:
            picked = [g for g in globs if isinstance(g, str) and g.strip()]
            if picked:
                return picked
    return list(DEFAULT_STRUCTURAL_GLOBS)


@dataclass(frozen=True)
class Structural:
    """`via` names who answered — `graph` (globs only for what it could not
    read) or `globs` (no graph to ask); `globs` are the ones that caught a file."""
    via: str
    reasons: "tuple[str, ...]" = ()
    globs: "tuple[str, ...]" = ()

    @property
    def structural(self) -> bool:
        return bool(self.reasons)

    def as_dict(self) -> "dict[str, Any]":
        return {"via": self.via, "reasons": list(self.reasons), "globs": list(self.globs)}

    @classmethod
    def from_dict(cls, d: Any) -> "Optional[Structural]":
        """A recorded verdict, or None when the record is not one."""
        if not (isinstance(d, dict) and d.get("via") in ("graph", "globs")
                and isinstance(d.get("reasons"), list)):
            return None
        return cls(d["via"], tuple(str(r) for r in d["reasons"]),
                   tuple(str(g) for g in d.get("globs") or ()))


def _ask_graph(root: Path, base: str, changed: "list[str]") -> "Optional[dict]":
    if not (root / "codemap.yaml").is_file():
        return None
    import process_group
    try:
        # through the group helper: the child is hs-code, which spawns git of its own
        r = process_group.run(
            [sys.executable, str(_HS_CODE), "--repo", str(root), "change", "structural",
             "--base", base, *changed],
            capture_output=True, text=True, timeout=300, env=dict(os.environ))
        data = json.loads(r.stdout)["data"] if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(data, dict) and isinstance(data.get("reasons"), list)
            and isinstance(data.get("unread"), list)):
        return None
    return data


def _first_glob(globs: "list[str]", path: str) -> "Optional[str]":
    import scope_match
    return next((g for g in globs if scope_match.scope_matches([g], [path])), None)


def verdict_of(changed: "list[str]", globs: "list[str]", graph: "Optional[dict]") -> Structural:
    """The verdict from the graph's answer (None: there was no graph to ask)
    plus the globs for every file it could not read."""
    if graph is None:
        via, reasons, blind = "globs", [], list(changed)
    else:
        via, reasons = "graph", [str(r) for r in graph["reasons"]]
        blind = [str(u.get("path")) for u in graph["unread"] if isinstance(u, dict)]
    hit: "list[str]" = []
    for path in blind:
        g = _first_glob(globs, path)
        if g is None:
            continue
        reasons.append("%s matches %s" % (path, g) + (" (%s)" % UNREAD if via == "graph" else ""))
        if g not in hit:
            hit.append(g)
    return Structural(via, tuple(reasons), tuple(g for g in globs if g in hit))


def judge(root, changed: "Iterable[str]", *, base: str = "HEAD") -> Structural:
    root = Path(root)
    changed = [c for c in changed if c]
    if not changed:
        return Structural("graph" if (root / "codemap.yaml").is_file() else "globs")
    return verdict_of(changed, load_structural_globs(str(root)), _ask_graph(root, base, changed))
