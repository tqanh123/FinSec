"""How plans under one `plans/` dir relate: a split parent and its children.

The human's keep/split answer is recorded on the parent (`artifacts/scope-sizing.json`).
A child names its parent with plan.md frontmatter `parent_plan: <parent dir name>`; the
parent finds its children by reading its siblings. One place holds each link, so a child
written after the split needs no edit to the parent — and no parent-side list can drift
from the children on disk.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frontmatter_parser  # noqa: E402

SCOPE_RECEIPT = "scope-sizing.json"
SIZE_DECISIONS = ("keep", "split")
PARENT_KEY = "parent_plan"


def size_decision(plan_dir) -> "tuple[str | None, str | None]":
    """(decision, None) when the human's keep/split answer is on disk, else
    (None, why not). A receipt not attributed to `user` is refused: the machine
    never decides a plan's size."""
    receipt = Path(plan_dir) / "artifacts" / SCOPE_RECEIPT
    if not receipt.is_file():
        return None, "no scope-sizing decision recorded at %s" % receipt
    try:
        rec = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        rec = {}
    rec = rec if isinstance(rec, dict) else {}
    if str(rec.get("decided_by") or "").strip().lower() != "user":
        return None, ("%s's decided_by is %r, not \"user\" — a machine-attributed "
                      "size decision is refused" % (receipt, rec.get("decided_by")))
    decision = str(rec.get("decision") or "").strip().lower()
    if decision not in SIZE_DECISIONS:
        return None, ("%s's decision is %r, expected one of %s"
                      % (receipt, rec.get("decision"), SIZE_DECISIONS))
    return decision, None


def _frontmatter(plan_dir) -> dict:
    fm = frontmatter_parser.parse_file(Path(plan_dir) / "plan.md").get("frontmatter")
    return fm if isinstance(fm, dict) else {}


def declared_parent(plan_dir) -> "tuple[str | None, str | None]":
    """(parent dir name, None), (None, None) when the plan names no parent, or
    (None, problem) when `parent_plan:` is present but not a plan name."""
    fm = _frontmatter(plan_dir)
    if PARENT_KEY not in fm:
        return None, None
    raw = fm[PARENT_KEY]
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None, ("plan.md `%s:` must be the parent plan's directory name, got %r"
                      % (PARENT_KEY, raw))
    return name, None


def own_phase_files(plan_dir) -> list:
    """Phase files the plan itself still holds — under phases/ and flat, the two
    layouts the approval hash reads."""
    d = Path(plan_dir)
    return sorted(set(d.glob("phases/phase-*.md")) | set(d.glob("phase-*.md")))


def children(plan_dir) -> list:
    """Sibling plan dirs whose plan.md names this one as `parent_plan`, by name."""
    plan_dir = Path(plan_dir)
    try:
        siblings = sorted(p for p in plan_dir.parent.iterdir()
                          if p.is_dir() and p.name != plan_dir.name and (p / "plan.md").is_file())
    except OSError:
        return []
    return [p for p in siblings if declared_parent(p)[0] == plan_dir.name]


def split_view(plan_dir) -> "dict | None":
    """None unless the human decided `split`; else the children found and the
    phase files the parent still holds. `done` once at least one child names the
    parent and the parent holds no phase: its work lives in the children."""
    decision, _ = size_decision(plan_dir)
    if decision != "split":
        return None
    kids = children(plan_dir)
    own = own_phase_files(plan_dir)
    return {"children": kids, "own_phases": own, "done": bool(kids) and not own}


def child_problems(plan_dir) -> list:
    """What is wrong with this plan's `parent_plan:`, each a sentence: a parent the
    plans dir does not hold, or one the human did not split, cannot see this child."""
    name, problem = declared_parent(plan_dir)
    if problem:
        return [problem]
    if name is None:
        return []
    parent = Path(plan_dir).parent / name
    if not (parent / "plan.md").is_file():
        return ["plan.md `%s: %s` names no plan under %s — the parent cannot see this "
                "child" % (PARENT_KEY, name, Path(plan_dir).parent)]
    decision, why = size_decision(parent)
    if decision != "split":
        return ["plan.md `%s: %s` names a plan whose size decision is not split (%s) — "
                "it reads as one plan to cook, not as this child's parent"
                % (PARENT_KEY, name, decision or why)]
    return []


def split_sentence(view: dict, plan_dir) -> str:
    """What the parent still owes its split, or '' when nothing: the one wording the
    plan projection and the cook refusal both print."""
    parts = []
    if not view["children"]:
        parts.append("no plan names this one as its parent yet: give each child plan's "
                     "plan.md frontmatter `%s: %s`" % (PARENT_KEY, Path(plan_dir).name))
    if view["own_phases"]:
        parts.append("%d phase file(s) still here (%s) — move each into the child plan "
                     "that cooks it" % (len(view["own_phases"]), ", ".join(
                         p.name for p in view["own_phases"])))
    return "; ".join(parts)


def never_cooked(plan_dir) -> "str | None":
    """Why `plan_dir` is never opened or closed itself, or None: the human split it.
    The one sentence every lifecycle writer (open, close, finalize) refuses with."""
    view = split_view(Path(plan_dir))
    if view is None:
        return None
    return "%s was split; cook its child plans instead%s" % (
        Path(plan_dir).name,
        ": " + ", ".join(Path(c).name for c in view["children"]) if view["children"] else "")
