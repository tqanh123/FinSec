"""bounded_cache_dir.py — the ONE on-disk cache-directory shape shared by
every content-keyed cache under `harness_paths.tool_data_root()/cache/`:
resolve the subdirectory (`None` when the data root cannot be resolved, or
`harness_paths` itself is not importable — the caller then reparses on every
call rather than erroring), and prune it down to its newest N entries by
mtime.

`codegraph._core.snapshot_bridge` and `hs_family.tree_scanning_tests` each
kept their own copy of both halves (`_cache_dir`/`_prune_cache` and
`_cache_dir`/`_prune`, respectively) — same computation, two places that
could drift out of sync by hand. This module is that computation, once;
each caller still resolves its OWN subdirectory name and its OWN keep
count (their bound sizes differ on purpose — a per-content-hash cache
holding many small entries needs a much larger bound than a per-root
cache holding one record per repo).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def cache_subdir(name: str) -> "Optional[Path]":
    """`harness_paths.tool_data_root()/cache/<name>`, or `None` when that
    root cannot be resolved (`harness_paths.data_root_unresolved`) or
    `harness_paths` itself cannot even be imported (a caller — e.g.
    `snapshot_bridge` — designed to run standalone outside this harness).
    `None` means "no cache" to every caller here, never an error: caching
    is an optimization, not a requirement to answer correctly."""
    try:
        import harness_paths
    except ImportError:
        return None
    base = harness_paths.tool_data_root()
    if harness_paths.data_root_unresolved(base):
        return None
    return base / "cache" / name


def prune(cache_dir: "Path", *, keep: int) -> None:
    """Delete every `*.json` entry under `cache_dir` beyond the `keep`
    newest by mtime. A read failure (missing directory, no permission) is
    a no-op, and a single entry that cannot be removed is skipped rather
    than aborting the rest — pruning is housekeeping, never a requirement
    the caller's own correct answer depends on."""
    try:
        entries = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    excess = len(entries) - keep
    if excess <= 0:
        return
    for p in entries[:excess]:
        try:
            p.unlink()
        except OSError:
            pass
