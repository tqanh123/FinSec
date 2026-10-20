"""Shared "resolve ONE snapshot, checked for freshness" step, factored out of
`codegraph/graph/query.py`'s own `_run_query` so `codegraph/graph/rules.py`'s
`rules` verb does not carry a second copy of the same law
(`drift` never uses it: measuring a stale snapshot is its whole job): the PRIMARY snapshot a verb reads is refused when its commit differs
from HEAD over the descriptor's declared roots (`--allow-stale` turns the
refusal into a warning: stderr in text mode, the caller's `warnings` list in JSON mode); `--fresh` measures
the on-disk tree instead of trusting the store's "latest" — for a store-reading verb
once per content, filed in this worktree's own base and reused (`fresh_filed`); for a
`check` verb into a scratch directory, which the caller MUST delete once done (even on
a later red/crash) — every `--fresh` caller's own `finally: shutil.rmtree`.

`--fresh` + an explicit "which snapshot" flag (`--snap` for the 9 query
leaves, `--snapshot` for `rules`) is a usage error in EVERY caller — both
name what to measure — but the two flags differ in SHAPE (`--snap REF`
resolves inside the snapshot store by id/prefix/hash; `--snapshot DIR` is a
bare directory path, the ported tool's own `rules` shape) so which-directory
resolution stays each caller's own job; only the freshness LAW moved here.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ambiguous_input import Ambiguous, NotFound
from codegraph._core.descriptor import root_paths
from codegraph._core.snapshot.freshness import check_freshness
from codegraph._core.snapshot.pipeline import SnapshotError, take_snapshot
from codegraph._core.snapshot.store import (SnapshotMissing, load_dir, resolve_current,
                                            resolve_snapshot)
from codegraph.graph.verbs import SnapshotBaseUnresolved, _snapshot_base


class SnapshotResolveError(Exception):
    """Honest cannot-run for the resolve step — callers convert with
    `Verdict.broken(str(e)).exit`, the same shape `DescriptorError`/
    `SnapshotError`/`SnapshotMissing` already use elsewhere in this package."""


class SnapshotRefNotFound(NotFound, SnapshotResolveError):
    """`--snap REF` naming no snapshot: a `SnapshotResolveError` to every door, and a
    `NotFound` to the doors that answer it on rung 1."""


class SnapshotRefAmbiguous(Ambiguous, SnapshotResolveError):
    """`--snap REF` naming several snapshots: a `SnapshotResolveError` to every door,
    and an `Ambiguous` to the doors that print the envelope."""


def checked_idx(root: Path, opts: Any, snap_dir: Path, descriptor: Any,
               verb: str, warnings: "list[str] | None" = None, *,
               store: "Path | None" = None) -> dict:
    """Load `snap_dir`'s index and refuse it when stale (unless
    `--allow-stale`); a warning (stale-allowed, non-git tree, dirty tree, or
    a commit mismatch outside the declared roots) prints to stderr in text
    mode, same as every existing freshness caller -- and lands in `warnings`
    (the envelope's) when the caller passes one. `store` is where this caller's
    `--fresh` would file, named in the refusal; None when it would file nothing."""
    idx = load_dir(snap_dir)
    snap_commit = str(idx.get("commit") or "")
    error, warning = check_freshness(
        root, snap_commit, allow_stale=getattr(opts, "allow_stale", False),
        label=f"snapshot {snap_dir.name}", roots=root_paths(descriptor), store=store)
    if error is not None:
        raise SnapshotResolveError(f"{verb}: {error}")
    _warn(opts, warning, warnings)
    return idx


def _warn(opts: Any, message: "str | None", warnings: "list[str] | None") -> None:
    if message is None:
        return
    if not getattr(opts, "json", False):
        print(f"⚠ {message}", file=sys.stderr)
    elif warnings is not None:
        warnings.append(message)


def fresh_scratch(root: Path, descriptor: Any, verb: str) -> "tuple[Path, dict, Path]":
    """Measure the tree on disk into a scratch dir (the `--fresh` shape) -> (snap_dir,
    idx, scratch_dir). Caller deletes `scratch_dir` when done, even on a
    later exception (mirrors every existing `--fresh` caller's `finally`)."""
    scratch = Path(tempfile.mkdtemp(prefix=f"hs-code-graph-{verb}-fresh-"))
    try:
        res = take_snapshot(root, scratch, descriptor, mode="daily", force=True)
        return res.out_dir, load_dir(res.out_dir), scratch
    except SnapshotError as e:
        shutil.rmtree(scratch, ignore_errors=True)
        raise SnapshotResolveError(f"{verb} --fresh: {e}") from e
    except BaseException:      # interrupted inside the long step: the caller never got it
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def fresh_filed(root: Path, descriptor: Any, out: "str | None", verb: str,
                need_store: bool = True) -> "tuple[Path | None, Path, dict, Path | None]":
    """`--fresh` through the store's content-keyed reuse: the tree on disk now,
    uncommitted files included, measured once per content and filed in THIS
    worktree's own base (`write_current`, the `graph snapshot --apply` path). A
    later `--fresh` over identical content reads that measurement back instead of
    re-measuring. Only for a caller that reads the store itself (`need_store`, the
    graph queries): a `check` verb's unpinned measure, and every in-process
    `measure()` over a scratch repo, stays in a scratch dir and files nothing."""
    if not need_store:
        d, idx, scratch = fresh_scratch(root, descriptor, verb)
        return None, d, idx, scratch
    base = _snapshot_base(descriptor.project, out, root)
    import codegraph_snapshot_reader
    from codegraph._core.snapshot.freshness import measured_row
    from codegraph._core.snapshot.writer import write_current
    try:
        outcome = write_current(root, base, descriptor, apply=True, mode="port",
                                sweep_dead_worktrees=False)
    except SnapshotError as e:
        raise SnapshotResolveError(f"{verb} --fresh: {e}") from e
    idx = load_dir(outcome.out_dir)
    if outcome.reused and codegraph_snapshot_reader.recording_active():
        codegraph_snapshot_reader.record_measured(
            outcome.out_dir, measured_row(root, list(root_paths(descriptor)) or None))
    return base, outcome.out_dir, idx, None


def _named(verb: str, msg: str) -> str:
    return msg if msg.startswith(verb) else f"{verb}: {msg}"


def resolve_idx(root: Path, descriptor: Any, opts: Any, snap: "str | None", out: "str | None",
                verb: str, fresh: bool, need_store: bool = True,
                snap_dir: "str | Path | None" = None,
                warnings: "list[str] | None" = None) -> "tuple[Path | None, Path, dict, Path | None]":
    """The ONE resolve every door reads a snapshot through -> (store base, snap_dir, idx,
    scratch). Exactly one source: `fresh` measures the on-disk tree (`fresh_filed`; `scratch`
    is set only on its no-store fallback, and the caller deletes it); `snap_dir` pins an explicit directory; else `snap` (None = the
    latest) under the store base. A stale snapshot is refused unless `--allow-stale`.
    Every cannot-run is one `SnapshotResolveError` naming `verb` once, so no door lets
    one escape as a traceback. `need_store=False` keeps `fresh` in a scratch dir (the
    caller never reads the store itself); the base comes back None then, and for a
    pinned `snap_dir`. `warnings`, when
    given, collects what JSON mode would otherwise not print."""
    pinned = snap_dir is not None and str(snap_dir) != ""
    if fresh and (snap or pinned):
        flag = "--snapshot" if pinned else "--snap"
        raise SnapshotResolveError(
            f"{verb}: --fresh and {flag} are mutually exclusive — --fresh measures the "
            f"tree on disk as it is now, {flag} pins an explicit one")
    try:
        if pinned:
            d = Path(snap_dir)  # type: ignore[arg-type]
            if not d.is_dir():
                raise SnapshotResolveError(f"{verb}: no snapshot dir {d}")
            return None, d, checked_idx(root, opts, d, descriptor, verb, warnings), None
        if fresh:
            return fresh_filed(root, descriptor, out, verb, need_store)
        base = _snapshot_base(descriptor.project, out, root)
        assert base is not None
        if snap is None:
            # "latest" -- resolved by CURRENT tree content, not by which
            # sibling was written last (a reuse round-trip writes nothing,
            # so "last written" can be a state the tree already reverted
            # away from; see `store.resolve_current`'s own docstring).
            d, note = resolve_current(base, root, descriptor)
            _warn(opts, note, warnings)
        else:
            d = resolve_snapshot(base, snap)
        return base, d, checked_idx(root, opts, d, descriptor, verb, warnings,
                                    store=base if need_store else None), None
    except Ambiguous as e:
        raise SnapshotRefAmbiguous(e.kind, e.query, e.candidates, e.hint) from e
    except NotFound as e:
        raise SnapshotRefNotFound(_named(verb, str(e)), e.kind, e.query) from e
    except (SnapshotBaseUnresolved, SnapshotMissing) as e:
        raise SnapshotResolveError(_named(verb, str(e))) from e
