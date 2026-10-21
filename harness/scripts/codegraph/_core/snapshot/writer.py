"""The ONE writer orchestrator both snapshot-writing callers go through.

`codegraph.graph.verbs.SNAPSHOT` and `affected_tests.py`'s `_resolve_idx`
used to each run their own copy of scope -> dirty -> tool_stamp -> reuse_key
-> find_reusable -> take_snapshot -> prune_snapshots -> prune_dead_worktrees.
`write_current` is that sequence run once, in one place; each caller maps its
`WriteOutcome` to whatever shape it prints or returns.

Lives in its own module, not `store.py`: `write_current` needs `.pipeline.
take_snapshot` for the real-write path, and `.pipeline`'s own `take_snapshot`
needs `.store` (function-local, for its content-key-collision naming and its
mode-consistency check) -- putting the orchestrator IN `store.py` would make
`store` import `pipeline` at module level while `pipeline` already reaches
back into `store`, a two-file cycle `test_the_import_cycles_only_shrink.py`
counts as real (that gate walks every `Import` node at any depth, not just
the module preamble, so a lazy/function-local import on either side would
not have hidden it either). This module sits one layer above both and is
never imported back by either.
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import codegraph_snapshot_reader

from .freshness import working_tree_dirty
from .pipeline import take_snapshot
from .store import (find_reusable, load_dir, prune_dead_worktrees, prune_snapshots,
                    record_repo_identity, resolve_keep)
from .worktree_key import dirty_scope, head_tree_hash, reuse_key


@dataclass
class WriteOutcome:
    """What a `write_current` caller needs to print or act on — the ONE
    shape `codegraph.graph.verbs.SNAPSHOT` and `affected_tests.
    _resolve_idx` both now read, instead of each building its own dict from
    a duplicated scope -> dirty -> reuse_key -> find_reusable ->
    take_snapshot -> prune sequence."""
    dry_run: bool
    reused: bool
    dirty: bool
    exists: bool
    out_dir: Path
    commit: "str | None"
    #: the content identity of the tree this outcome's snapshot measured —
    #: `head_tree_hash(root)` on a clean write, `None` on a dirty one; a reuse
    #: carries the stored snapshot's own recorded `tree_hash`. The commit a
    #: reuse was stamped with survives a --no-ff merge of an unchanged tree, so
    #: readers judging "is this of the tree I stand in" must take THIS field.
    tree: "str | None"
    totals: dict
    roots_kept: list
    roots_declared: list
    notes: list
    elapsed: float
    #: write-time-only diagnostics (`take_snapshot`'s own `SnapshotResult`
    #: fields) — empty on a `reused=True` outcome, which runs no scan pass
    #: at all to produce either.
    banner: list
    overlap_warnings: list


def write_current(root: Path, base: Path, descriptor: Any, *, apply: bool,
                  mode: str = "port", sweep_dead_worktrees: bool = True) -> WriteOutcome:
    """scope -> dirty -> tool_stamp -> reuse_key -> find_reusable ->
    take_snapshot -> prune_snapshots -> prune_dead_worktrees, in ONE place.

    `apply=False` is the dry-run shape (`SNAPSHOT`'s default): measures
    into a throwaway scratch directory, nothing lands under `base`, never
    reuses (there is nothing of THIS run's own to reuse — the only question
    is whether a REAL directory of that name already `exists`), and never
    prunes. `apply=True` (`affected_tests.py`'s only mode) reuses on an
    exact content-key match, writes for real otherwise, then prunes both
    the keep-N window and, when `sweep_dead_worktrees`, worktrees `git` no
    longer lists — `SNAPSHOT` passes `sweep_dead_worktrees=False`
    itself only under an explicit `--out` (`_snapshot_base`'s own
    docstring: an `--out` target has no store-wide sibling-worktree
    structure to sweep; `base`'s parent there is an arbitrary caller
    directory, not `.../<project>/` shared by every worktree).

    Raises `SnapshotError` on a genuine measurement failure — the same
    shape `take_snapshot` itself raises, unwrapped, so every existing
    caller's own `except SnapshotError` keeps working unchanged.
    """
    tool_stamp = codegraph_snapshot_reader.compute_tool_stamp()
    scope = dirty_scope(root, descriptor)
    dirty = working_tree_dirty(root, scope)
    tree_hash = None if dirty else head_tree_hash(root)
    key = reuse_key(root, descriptor, dirty=dirty)
    desc_path = getattr(descriptor, "path", None)
    descriptor_sha = (codegraph_snapshot_reader.descriptor_file_sha(Path(desc_path).parent,
                                                                    Path(desc_path).name)
                      if desc_path else None)

    if apply:
        if sweep_dead_worktrees:
            # Stamp BEFORE the reuse check too: a reuse hit still confirms
            # this repo owns `base`, and a stale stamp from a since-removed
            # clone (basename collision under the shared <project> parent)
            # must never survive a real write here from THIS repo. Skipped
            # under an explicit `--out` (`sweep_dead_worktrees=False`): that
            # target has no store-wide sibling-worktree structure for the
            # sentinel to ever protect (`_snapshot_base`'s own docstring).
            record_repo_identity(base, root)
        reused_dir = find_reusable(base, key, tool_stamp, mode=mode)
        if reused_dir is not None:
            idx = load_dir(reused_dir)
            return WriteOutcome(
                dry_run=False, reused=True, dirty=dirty, exists=True, out_dir=reused_dir,
                commit=idx.get("commit"), tree=idx.get("tree_hash"), totals=idx.get("totals", {}),
                roots_kept=[r.get("id") for r in idx.get("roots") or []],
                roots_declared=idx.get("roots_declared") or [], notes=[], elapsed=0.0,
                banner=[], overlap_warnings=[])

    t0 = time.monotonic()
    if apply:
        res = take_snapshot(root, base, descriptor, mode=mode, force=True,
                            tool_stamp=tool_stamp, tree_hash=tree_hash, content_key=key,
                            descriptor_sha=descriptor_sha)
        elapsed = time.monotonic() - t0
        exists = False
        prune_snapshots(base, resolve_keep(descriptor), protect={res.out_dir})
        if sweep_dead_worktrees:
            prune_dead_worktrees(base.parent, root)
    else:
        with tempfile.TemporaryDirectory(prefix="hs-code-graph-snap-") as scratch:
            res = take_snapshot(root, Path(scratch), descriptor, mode=mode, force=True,
                                tool_stamp=tool_stamp, tree_hash=tree_hash, content_key=key,
                            descriptor_sha=descriptor_sha)
        elapsed = time.monotonic() - t0
        would = base / res.snapshot
        res = replace(res, out_dir=would)
        exists = would.exists()

    return WriteOutcome(
        dry_run=not apply, reused=False, dirty=dirty, exists=exists, out_dir=res.out_dir,
        commit=res.commit, tree=tree_hash, totals=res.totals, roots_kept=res.roots_kept,
        roots_declared=res.roots_declared, notes=res.notes, elapsed=elapsed,
        banner=res.banner, overlap_warnings=res.overlap_warnings)
