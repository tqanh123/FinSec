"""The files a plan's approval hash covers: plan.md, its phase files, the phase-DAG
sidecar, and every file under its reference dirs. A leaf: what reads a plan's text
(the approval door, a section reader following a pointer) asks here.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frontmatter_parser  # noqa: E402

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?^(?:---|\.\.\.)\s*$\n?",
                             re.DOTALL | re.MULTILINE)


def _plan_files(plan_dir: Path):
    plan_dir = Path(plan_dir)
    files = [plan_dir / "plan.md"]
    files += sorted(plan_dir.glob("phase-*.md"))
    # Phase files at the plan-dir root (flat) AND under phases/ (scaffold layout);
    # both fold into the hash keyed by basename so a phases/-layout plan cannot slip
    # phase edits past approval, and a flat->phases/ migration of identical content
    # does not spuriously trip drift.
    files += sorted(plan_dir.glob("phases/phase-*.md"))
    # Machine-readable phase-DAG sidecar: hardcode the name so a mutable
    # frontmatter `phase_graph:` marker cannot redirect which file the hash covers.
    side = plan_dir / "plan-graph.yaml"
    if side.is_file():
        files.append(side)
    files = [f for f in files if f.is_file()]
    return files + [f for f in reference_files(plan_dir) if f not in files]


# Where a plan keeps material its phases tell the executor to follow. Everything
# under these dirs is approved content, linked or not; a plan names its own with
# plan.md frontmatter `reference_dirs:` (pinned — dropping a dir after approval
# is drift).
DEFAULT_REFERENCE_DIRS = ("refs",)


def _reference_dir_entries(plan_dir) -> tuple:
    """(declared entries, problems). Absent key → the default; a key present but
    unreadable is a problem, never a silent fallback to the default."""
    pm = Path(plan_dir) / "plan.md"
    try:
        m = _FRONTMATTER_RE.match(pm.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return list(DEFAULT_REFERENCE_DIRS), []
    parsed = frontmatter_parser.parse_text(m.group(0), "<frontmatter>") if m else {}
    fm = parsed.get("frontmatter") if parsed.get("ok") else None
    if not isinstance(fm, dict) or "reference_dirs" not in fm:
        return list(DEFAULT_REFERENCE_DIRS), []
    raw = fm["reference_dirs"]
    raw = [raw] if isinstance(raw, str) else raw
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        return [], ["plan.md `reference_dirs:` must be a list of plan-dir-relative "
                    "directory names, got %r" % (fm["reference_dirs"],)]
    base = Path(plan_dir).resolve()
    entries, problems = [], []
    for x in raw:
        d = (base / x.strip()).resolve()
        try:
            rel = d.relative_to(base)
        except ValueError:
            problems.append("reference dir %r leaves the plan dir" % x)
            continue
        if not rel.parts or Path(x.strip()).is_absolute():
            problems.append("reference dir %r must name a subdirectory of the plan "
                            "dir" % x)
        elif rel.parts[0] == "artifacts":
            problems.append("reference dir %r is under artifacts/, which holds "
                            "machine-written state that moves after approval" % x)
        else:
            entries.append(rel.as_posix())
    return entries, problems


def _walk_reference_dirs(plan_dir) -> tuple:
    """(files, escapes) under the plan's reference dirs, following links — a linked
    dir's files are reference material too — each dir walked once. `escapes` names
    what resolves outside the plan dir: such a file is still hashed (by what it
    points at), a dir is not descended."""
    plan_dir = Path(plan_dir)
    base = plan_dir.resolve()
    files, escapes, seen = set(), [], set()
    for rel in _reference_dir_entries(plan_dir)[0]:
        for dirpath, dirnames, filenames in os.walk(plan_dir / rel, followlinks=True):
            here = Path(dirpath)
            real = here.resolve()
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
            if real != base and base not in real.parents:
                escapes.append(here.relative_to(plan_dir).as_posix() + "/")
                dirnames[:] = []
                continue
            for name in filenames:
                f = here / name
                if not f.is_file():
                    continue
                files.add(f)
                real_f = f.resolve()
                if base not in real_f.parents:
                    escapes.append(f.relative_to(plan_dir).as_posix())
    return (sorted(files, key=lambda f: f.relative_to(plan_dir).as_posix()),
            sorted(set(escapes)))


def reference_dir_problems(plan_dir) -> list:
    entries, problems = _reference_dir_entries(plan_dir)
    return problems + ["reference %s resolves outside the plan dir — copy it in, or "
                       "it can change with no drift" % x
                       for x in _walk_reference_dirs(plan_dir)[1]]


def reference_files(plan_dir) -> list:
    """Every file under the plan's reference dirs, sorted by plan-relative path."""
    return _walk_reference_dirs(plan_dir)[0]
