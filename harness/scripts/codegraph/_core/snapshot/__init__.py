"""Snapshot pipeline, formats, and the read-side store.

`freshness` is not re-exported: its callers import
`codegraph._core.snapshot.freshness` directly.
"""
from .format import KernelFormat, SnapshotFormat, format_for, register_format
from .pipeline import (PayloadProvider, SnapshotError, SnapshotResult, SourceRef,
                       commit_stamp, discover, edge_targets, take_snapshot)
from .store import (DESCRIPTOR_NAMES, SNAP_RE, SnapshotMissing, snapshot_mode,
                    load_dir, prune_snapshots, resolve_snapshot, snapshot_dirs)

__all__ = [
    "KernelFormat", "SnapshotFormat", "format_for", "register_format",
    "PayloadProvider", "SnapshotError", "SnapshotResult", "SourceRef", "commit_stamp",
    "discover", "edge_targets", "take_snapshot",
    "DESCRIPTOR_NAMES", "SNAP_RE", "SnapshotMissing", "snapshot_mode", "load_dir",
    "prune_snapshots", "resolve_snapshot", "snapshot_dirs",
]
