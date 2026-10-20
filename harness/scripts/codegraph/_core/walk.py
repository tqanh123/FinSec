"""The directories no tree walk of the graph library enters: VCS, virtualenvs,
tool caches, and a port's source checkouts (`.port/sources/<id>-<sha8>/`,
another repository's code at a pinned commit — `hs-code port source`).

`.port/` as a whole is NOT skipped: its batch specs name the target's modules,
which a rename must still see. Checked by the path's own parts, never resolved.
"""
from __future__ import annotations

from typing import Iterable, Sequence

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
