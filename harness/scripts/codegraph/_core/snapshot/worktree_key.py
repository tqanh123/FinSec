"""The snapshot-base path COMPONENT keyed by the WORKTREE, not just the
project name.

`harness_paths.data_root()` deliberately redirects every linked git
worktree's STATE to the HOST repo's `.harness/` (its own module docstring,
"F2") -- correct for runtime state, which is one thing shared across every
worktree of a repo. A snapshot is a MEASUREMENT of a tree, not shared state:
two worktrees of one repo can carry different code (a feature branch vs
main), so redirecting their snapshots to one shared base the same way lets
one worktree's numbers silently read as, or get overwritten by, another's.
This module supplies the extra path segment `_snapshot_base` nests under, so
two worktrees land in two bases and neither reads the other's.

Superseded design: the earlier ruling that the measuring tool stays OUTSIDE
the tree it measures answered a narrower question -- whether porting the tool
INTO this repo would move the instrument along with the tree it measures.
That risk is real for the TOOL's own code (guarded instead by the `tool`
stamp, `codegraph_snapshot_reader.compute_tool_stamp`); this module answers
the separate question of which TREE a snapshot is OF, once the tool already
lives inside every tree it might measure -- a linked worktree, in
particular.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

import codegraph_snapshot_reader
import tree_content_key

from ..descriptor.model import root_paths
from ..git import GitError, git_text

# `_digest` / `worktree_toplevel` / `worktree_key` MOVED to
# `codegraph_snapshot_reader` (a flat module outside this package, so a
# caller like hs-run's facts locator can resolve a snapshot's own path
# layout without importing the whole `codegraph` measurement engine — see
# that module's own docstring). Rebound to the SAME function objects, not
# reimplemented beside them: `codegraph_snapshot_reader.descriptor.model.
# DESCRIPTOR_SCHEMA_VERSION` already re-exports its constant this exact way, so
# this is the second use of one pattern, not a new one. `worktree_key is
# codegraph_snapshot_reader.worktree_key` holds — one definition.
_digest = codegraph_snapshot_reader._digest
worktree_toplevel = codegraph_snapshot_reader.worktree_toplevel
worktree_key = codegraph_snapshot_reader.worktree_key


def repo_identity_digest(repo: Path) -> Optional[str]:
    """A digest of `git rev-parse --git-common-dir`, resolved to an
    absolute path -- the identity every worktree of ONE repository shares
    (a linked worktree's own `.git` is a FILE pointing at this same
    directory), and that two SEPARATE clones -- or two unrelated repos
    whose directory basenames happen to collide -- never do.

    Used to stamp a project's snapshot-base subdirectory with WHICH
    repository wrote it (`store.record_repo_identity`), so `store.
    prune_dead_worktrees` can tell "a worktree of THIS repo git no longer
    lists" (safe to remove) apart from "a directory that merely lives
    under the same `<project>` parent because a different clone's basename
    collided" (never remove — that function's own docstring). `None`
    outside a git checkout or on any other git failure: the caller must
    treat "cannot determine" as "do not trust", never as "same repo"."""
    try:
        raw = git_text(repo, "rev-parse", "--git-common-dir", check=False)
    except (GitError, OSError):
        return None
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path(repo) / p
    return _digest(str(p.resolve()))


def head_tree_hash(repo: Path) -> Optional[str]:
    """`git rev-parse HEAD^{tree}` -- the CONTENT identity of the commit
    HEAD currently points at. Distinct from the commit sha itself (an amend
    that only rewrites the message shares its predecessor's tree), which is
    exactly the identity a snapshot pass cares about: the pipeline measures
    FILES, not commit metadata. `None` outside a git checkout, or when HEAD
    does not resolve yet (a freshly `git init`ed repo with no commit)."""
    try:
        h = git_text(repo, "rev-parse", "HEAD^{tree}", check=False)
    except (GitError, OSError):
        return None
    return h or None


# ── content-keyed reuse: what makes two measurements INTERCHANGEABLE ────────
#
# `worktree_key` (above) answers "which snapshot BASE" — this section answers
# "which snapshot WITHIN that base, if any, may be handed back instead of
# re-measured". A snapshot pass on a dirty tree used to overwrite whatever
# else lived at its commit's name (`take_snapshot`'s own clean-sweep law);
# that is right for "one snapshot lives at this commit" but wrong for a
# caller (`affected_tests.py`) that wants to come BACK to a prior dirty edit
# without paying to re-measure it. `reuse_key` is the identity two builds are
# compared BY: an exact match is safe to reuse, anything else is not — never
# a "close enough" heuristic.


def _descriptor_file_rel(repo: Path, descriptor: Any) -> "tuple[str, ...]":
    """The descriptor's own file, repo-relative — `()` when the descriptor
    was never loaded from one (`descriptor.path is None`, the in-memory
    `auto_discover` shape `affected_tests.py` falls back to when a target
    carries no `codemap.yaml`). MEASURED HOLE this closes: `codemap.yaml`
    commonly sits at the repo root while `layout.roots` names subdirectories
    under it, so a root-only dirty/content scope reads an uncommitted edit
    to the descriptor itself as a clean tree — `graph snapshot --apply`
    then hands back a snapshot measured under the OLD descriptor with no
    warning at all."""
    path = getattr(descriptor, "path", None)
    if path is None:
        return ()
    try:
        rel = Path(path).resolve().relative_to(repo.resolve())
    except (OSError, ValueError):
        return ()
    return (rel.as_posix(),)


def dirty_scope(repo: Path, descriptor: Any,
                config_inputs: "Optional[Sequence[str]]" = None) -> "tuple[str, ...]":
    """`root_paths(descriptor)` PLUS the descriptor's own file (see
    `_descriptor_file_rel`) PLUS each enabled language pack's config inputs
    (a workspace's `package.json`s, its tsconfigs: outside a narrow root, yet
    they change what the snapshot says) — the full path set whose uncommitted state
    decides both "is this tree dirty" (the caller's own `working_tree_
    dirty(repo, dirty_scope(...))` call) and the dirty content key below;
    the two must never be computed over two different scopes, or a build
    could stamp `content_key` as clean while `dirty` itself said otherwise."""
    from ..lang import registry as lang_registry
    own = tuple(root_paths(descriptor)) + _descriptor_file_rel(repo, descriptor)
    cfg = lang_registry.config_inputs(repo, descriptor) if config_inputs is None else config_inputs
    return own + tuple(x for x in cfg if x not in own)


def content_key(repo: Path, descriptor: Any, *, dirty: bool,
                scope: "Optional[tuple[str, ...]]" = None) -> "Optional[str]":
    """The tree's own content identity for reuse matching. Clean ->
    `head_tree_hash(repo)`, same as before. Dirty -> delegates to
    `tree_content_key.content_key(repo, dirty_scope(repo, descriptor))` — the
    ONE tree-fingerprint algorithm every reader shares (a snapshot's reuse key
    and a suite run's junit key alike), rather than a second copy of the same
    sha256-over-(tree, diff, untracked) assembly kept in sync by hand. Two
    DIFFERENT uncommitted edits at the SAME commit hash still hash to two
    DIFFERENT keys, and returning to a prior edit hashes back to the SAME key
    — that invariant now lives in `tree_content_key`'s own docstring and
    tests, not here. `dirty` is the CALLER's own fact (one
    `working_tree_dirty` call shared with everything else that needs it, same
    law `SNAPSHOT` already followed for `tree_hash`) — never recomputed
    here. `None` off a non-git tree or an unresolved HEAD, or when the git
    calls this needs fail. `scope` is the caller's own `dirty_scope` when it has one:
    the same scope its `dirty` was measured over, computed once.

    A snapshot stored under the OLD local assembly may miss ONE reuse round
    after this change (the two algorithms may not byte-assemble identically
    in every edge case — see `tree_content_key`'s own module docstring); a
    fresh dirty measurement replaces it and the drift never repeats."""
    if not dirty:
        return head_tree_hash(repo)
    return tree_content_key.content_key(repo, dirty_scope(repo, descriptor) if scope is None else scope)


def descriptor_digest(descriptor: Any) -> str:
    """sha256 (short) of the descriptor's own resolved identity — `roots`,
    `auto_discover`, `scan`, everything `dataclasses.asdict` walks, `path`
    excluded (a load-time artifact, not tree content). Two passes of the
    IDENTICAL tree content under two different descriptors (auto-discover
    vs an explicit `codemap.yaml`, or `codemap.yaml` itself edited) are NOT
    interchangeable measurements — this is folded into `reuse_key` so a
    caller can never forget to check it separately."""
    payload = asdict(descriptor)
    payload.pop("path", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def reuse_key(repo: Path, descriptor: Any, *, dirty: bool,
              scope: "Optional[tuple[str, ...]]" = None) -> "Optional[str]":
    """The FULL identity a build is reused against — `content_key` folded
    with `descriptor_digest`. `None` when `content_key` itself is `None`
    (non-git tree, unresolved HEAD, or a git failure): nothing safe to key
    reuse on."""
    key = content_key(repo, descriptor, dirty=dirty, scope=scope)
    if key is None:
        return None
    return hashlib.sha256(f"{key}|{descriptor_digest(descriptor)}".encode("utf-8")).hexdigest()
