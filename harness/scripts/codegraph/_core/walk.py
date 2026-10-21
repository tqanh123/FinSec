"""The directories no tree walk of the graph library enters: VCS, virtualenvs,
tool caches, and a port's source checkouts (`.port/sources/<id>-<sha8>/`,
another repository's code at a pinned commit — `hs-code port source`).

`.port/` as a whole is NOT skipped: its batch specs name the target's modules,
which a rename must still see. Checked by the path's own parts, never resolved.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

import process_group
import secret_scan_before_ship as secret_scan

SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", "site-packages",
                       ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".harness"})
#: consecutive parts that open a tree of someone else's code
FOREIGN_TREES = ((".port", "sources"),)


def skipped(parts: "Sequence[str] | Iterable[str]") -> bool:
    """True when a path with these parts lies under a directory no walk enters."""
    parts = tuple(parts)
    if SKIP_DIRS.intersection(parts):
        return True
    return any(parts[i:i + len(t)] == t for t in FOREIGN_TREES for i in range(len(parts)))


def walk_files(base: "Path", include: "Sequence[str]", exclude: "Sequence[str]" = ()) -> "list[Path]":
    """Files under `base` whose basename matches an `include` glob and no `exclude`
    one, sorted. A directory no walk enters is cut from the walk itself (`os.walk`
    pruned in place), never visited and filtered after: a `node_modules/` holds more
    files than the tree it sits in."""
    out: "list[Path]" = []
    if not base.is_dir():
        return out
    for here, dirs, files in os.walk(base):
        rel = Path(here).relative_to(base).parts
        dirs[:] = sorted(d for d in dirs if not skipped(rel + (d,)))
        for f in files:
            if any(fnmatch.fnmatchcase(f, g) for g in include) \
                    and not any(fnmatch.fnmatchcase(f, g) for g in exclude):
                out.append(Path(here) / f)
    return sorted(out)


def git_listed(repo: Path) -> "list[str] | None":
    """Repo-relative files git tracks or would track (untracked, not ignored);
    None outside a git checkout. The ignore rules are what keep a `.venv` or
    a build dir out without a `scan.exclude` line per repo."""
    try:
        proc = process_group.run(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60, env=secret_scan.git_safe_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})
