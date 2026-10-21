"""SnapshotStore — locating and reading existing snapshots.

One door for reads (same principle as the descriptor store): every consumer
loads `_index.json` through here, so snapshot-role checks (`mode`), naming
rules, and "which snapshot is the frozen one" live in exactly one place.

Snapshot directory names are `<yyyymmdd>-<hhmmss>-<hash8>` — stamp from COMMIT
time, never run time, so the same tree state always maps to the same name. A
directory named `current` would lie the moment a second snapshot exists.

WHAT IT CANNOT SEE
    * Name matching is by full id (which always wins: it names one
    directory), then prefix or hash suffix. Two snapshots sharing a short
    hash is REAL on long batches — a partial ref matching more than one is
    an ERROR, never "take the first": picking wrong silently shifts every
    later number.
    * `_tests.yaml`: a legacy sidecar name a pre-layering pass used to write;
    no generator for it exists in this tree (`_tests.yaml` is only excluded
    from `pipeline.py`'s legacy-file sweep, never read here — see
    `codegraph._core.testmap`, which computes test/unit touches straight
    from the snapshot's own index instead).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import codegraph_snapshot_reader

from ambiguous_input import Ambiguous, NotFound
from ..git import GitError, run_git
from .freshness import fresh_hint, working_tree_dirty
from .worktree_key import dirty_scope, repo_identity_digest, reuse_key, worktree_key

#: Name of the sentinel `record_repo_identity` stamps into a project's
#: snapshot-base subdirectory (one per worktree) — `prune_dead_worktrees`'s
#: own proof that a directory belongs to the SAME repository as the one
#: currently running it, never a different clone whose basename collided
#: under the shared `<project>` parent.
_IDENTITY_FILE = ".repo-identity"

#: `<stamp>-<commit8>`, same as ever, with an OPTIONAL trailing content-key
#: fragment (`find_reusable`'s own identity, see that function) -- backward
#: compatible with every snapshot directory written before content-keyed
#: naming existed. The fragment lets several dirty measurements of ONE
#: commit coexist as distinct directories instead of overwriting each other.
#: MOVED to `codegraph_snapshot_reader.SNAPSHOT_DIR_NAME_RE` (same re-export
#: shape as `worktree_key`/`snapshot_base` below) -- a reader outside this
#: package (hs-run's facts locator) needs the identical naming contract
#: without importing the whole package to get it.
SNAP_RE = codegraph_snapshot_reader.SNAPSHOT_DIR_NAME_RE

#: How many snapshots one base may hold before `prune_snapshots` removes the
#: oldest. Content-keyed dirty builds can now coexist at one commit (the
#: reuse law this base exists for), which is exactly the growth a fixed
#: keep-newest cap exists to bound; 8 gives a few live working states of
#: back-and-forth room without letting a base grow without limit.
DEFAULT_KEEP = 8

#: Descriptor file name a caller's root-detection walks up for: `codemap.yaml`,
#: the kernel schema. Used to list `"index.yaml"` too (a legacy porting-batch
#: shape) — measured 0 readers anywhere in the tree beyond this declaration
#: and this package's own re-export, and the legacy-codec seam that name paired
#: with (`descriptor/store.py`'s `register_legacy_codec`/`legacy_codecs`/
#: `LegacyDescriptorCodec`) has 0 in-tree callers either, so it was removed.
#: Dropped rather than kept as a declared-but-unread claim: the name and the
#: codec come back together, and only with a real caller.
DESCRIPTOR_NAMES: tuple[str, ...] = ("codemap.yaml",)


class SnapshotMissing(Exception):
    """Honest cannot-run (maps to exit 2)."""


class SnapshotNotFound(NotFound, SnapshotMissing):
    """A ref naming no snapshot in a store that has some: exit 1 at the doors that read
    it, still a `SnapshotMissing` for every other door."""


class SnapshotAmbiguous(Ambiguous, SnapshotMissing):
    """A ref naming several snapshots: still a `SnapshotMissing` for every door that
    reports one, and an `Ambiguous` for the doors that print the envelope."""


def snapshot_dirs(base: Path) -> list[Path]:
    """Snapshot directories under `base`, sorted by name (= commit time)."""
    if not base.is_dir():
        return []
    return sorted((x for x in base.iterdir() if x.is_dir() and SNAP_RE.fullmatch(x.name)),
                  key=lambda x: x.name)


def _measured_at_or_zero(p: Path) -> float:
    try:
        return float(load_dir(p, record=False).get("measured_at") or 0)
    except SnapshotMissing:
        return 0.0


def _oldest_first(cands: "list[Path]") -> "list[Path]":
    """`cands`, oldest first — sorted by `measured_at` (each candidate's
    OWN wall-clock write time) with the name as a tiebreak, not by name
    alone. Name-sort alone is chronological ONLY when every candidate's
    stamp (commit time, second granularity) differs; several content-keyed
    dirty siblings of one commit — or several real commits landing in the
    same wall-clock second, a fast scripted sequence's own everyday case —
    share a name prefix that carries no time information at all, and a
    candidate written with no `content_key` (so no `measured_at` either)
    sorts as if oldest — right for a legacy pre-reuse directory, the only
    shape that omits it."""
    return sorted(cands, key=lambda p: (_measured_at_or_zero(p), p.name))


def prune_snapshots(base: Path, keep: int | None, *,
                    protect: "set[Path] | frozenset[Path]" = frozenset()) -> list[str]:
    """Delete all but the `keep` NEWEST snapshots under `base`, oldest first
    (`_oldest_first`'s own chronological order, not a bare name sort).

    `keep=None` (the declared-absence default) prunes nothing — the behavior
    before this function existed. A candidate must be something a snapshot
    pass itself wrote: `snapshot_dirs`' own name pattern, AND an
    `_index.json` inside — a directory that merely LOOKS like a snapshot
    name (or one mid-write with no index yet) is never a target.

    The `keep` cut runs over EVERY real candidate first (protected included)
    so a `protect`ed path that already sits within the newest `keep` changes
    nothing — the normal case, where it is the snapshot this very command
    just wrote. `protect` is only consulted after, to spare a path the raw
    cut would otherwise take (an explicitly pinned older snapshot) — the
    store then holds one more than `keep` for exactly that one run, rather
    than deleting the thing the caller named unsafe to lose.
    """
    if keep is None:
        return []
    protected = {p.resolve() for p in protect}
    cands = _oldest_first([d for d in snapshot_dirs(base) if (d / "_index.json").is_file()])
    victims = [v for v in cands[:max(0, len(cands) - keep)]
              if v.resolve() not in protected]
    pruned = []
    for v in victims:
        shutil.rmtree(v, ignore_errors=True)
        pruned.append(v.name)
    return pruned


def record_repo_identity(worktree_dir: Path, repo: Path) -> None:
    """Stamp `worktree_dir` (a project's snapshot-base subdirectory for one
    worktree) with `repo`'s own `repo_identity_digest` — called on every
    real write through `write_current`, so a directory always carries the
    identity of whichever repository most recently wrote into it. A `None`
    digest (git failure) leaves any prior stamp untouched rather than
    erasing proof that used to be trustworthy."""
    digest = repo_identity_digest(repo)
    if digest is None:
        return
    worktree_dir.mkdir(parents=True, exist_ok=True)
    (worktree_dir / _IDENTITY_FILE).write_text(digest, encoding="utf-8")


def _recorded_repo_identity(worktree_dir: Path) -> "str | None":
    try:
        text = (worktree_dir / _IDENTITY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def live_worktree_keys(repo: Path) -> "set[str] | None":
    """Every worktree `git` currently lists against `repo`'s own
    repository, each turned into the SAME key string `worktree_key.
    worktree_key` stamps for it — the set `prune_dead_worktrees` diffs a
    project's snapshot-base subdirectories against. `None` on any git
    failure (not a checkout, git missing, ...): "cannot determine" must
    never read as "nothing is live", or a git hiccup would wipe every
    worktree's stored snapshots on the next `--apply`.
    """
    try:
        raw = run_git(repo, "worktree", "list", "--porcelain")
    except (GitError, OSError):
        return None
    keys: set[str] = set()
    for line in raw.decode("utf-8", "replace").split("\n"):
        if line.startswith("worktree "):
            top = line[len("worktree "):].strip()
            if top:
                keys.add(worktree_key(Path(top)))
    return keys or None


def prune_dead_worktrees(project_base: Path, repo: Path) -> list[str]:
    """Remove a project's snapshot-base subdirectory for a worktree `git`
    no longer lists (removed via `git worktree remove`, or the plain
    `rm -rf` of a linked worktree that leaves it `git worktree list`-blind
    the same way). `worktree_key.worktree_key` is a one-way digest of the
    worktree's own absolute path, so the only way to tell a stored key is
    dead is to recompute that same function for every worktree git
    currently knows about and diff the sets — nothing is ever removed when
    that comparison itself cannot be trusted (`live_worktree_keys` returned
    `None` on a git failure, or `project_base` holds nothing yet).

    `project_base` (`.../codegraph/snapshots/<project>/`) is keyed by
    PROJECT NAME, shared by every clone of a repo with that basename, or by
    two entirely unrelated repos whose basenames collide — a subdirectory
    that is simply "not among `repo`'s own live worktrees" is not proof it
    is a DEAD worktree of `repo`; it may belong to a different repository
    entirely. A directory is only ever removed when its recorded identity
    (`record_repo_identity`, stamped on every real write) POSITIVELY
    matches `repo`'s own `repo_identity_digest` — an unstamped directory
    (the pre-this-fix layout, or one this repo never actually wrote into)
    or one stamped for a different repo is left alone, the same
    fail-closed law `live_worktree_keys() is None` already follows.
    """
    if not project_base.is_dir():
        return []
    live = live_worktree_keys(repo)
    if live is None:
        return []
    this_identity = repo_identity_digest(repo)
    if this_identity is None:
        return []
    removed = []
    for d in sorted(project_base.iterdir()):
        if not d.is_dir() or d.name in live:
            continue
        if _recorded_repo_identity(d) != this_identity:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


_stamp_commit_prefix = codegraph_snapshot_reader.stamp_commit_prefix


def _pick_latest(snaps: "list[Path]") -> Path:
    """The newest of `snaps` by `codegraph_snapshot_reader.chronological` —
    the ONE rule the hs-run facts engine picks by too: name order across
    commits, each candidate's own `measured_at` inside a tie (content-keyed
    siblings of one commit). The common case (no tie) reads no index."""
    return codegraph_snapshot_reader.chronological(snaps, _measured_at_or_zero)[-1]


def missing_message(base: Path) -> str:
    """No snapshot under `base`: the two ways to get one."""
    return (f"no snapshots under {base} — {fresh_hint(base)}; "
            f"`hs-code graph snapshot --apply` files a default snapshot")


def resolve_snapshot(base: Path, ref: str | None = None) -> Path:
    """One snapshot directory under `base`.

    None = the LATEST by name (`_pick_latest`'s own tie-break for several
    content-keyed siblings of one commit). A ref that is one directory's full
    name picks it, even when it also prefixes that commit's content-keyed
    siblings; otherwise it matches a date prefix or hash suffix. Zero matches and ambiguous matches are both
    errors — a snapshot that does not exist must not fall back to "latest"
    (that reads a different denominator while every printed number still
    looks perfectly normal).
    """
    snaps = snapshot_dirs(base)
    if not snaps:
        raise SnapshotMissing(missing_message(base))
    if ref is None:
        return _pick_latest(snaps)
    exact = [s for s in snaps if s.name == ref]
    if exact:
        return exact[0]
    hits = [s for s in snaps if s.name.startswith(ref) or s.name.endswith(ref)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SnapshotAmbiguous("snapshot", ref, (s.name for s in hits), "pass more of the name")
    have = ", ".join(s.name for s in snaps)
    raise SnapshotNotFound(f"{ref!r} matches 0 snapshots; available: {have}", "snapshot", ref)


def resolve_current(base: Path, root: Path, descriptor: Any) -> "tuple[Path, str | None]":
    """The snapshot under `base` that matches `root`'s CURRENT tree content
    -> `(snap_dir, note)`.

    MEASURED BUG this fixes: `resolve_snapshot(base, None)` (`_pick_latest`)
    breaks a tie among content-keyed siblings of one commit by `measured_at`
    -- the LAST WRITE. A reuse round-trip writes nothing (`find_reusable`
    hands back an existing directory unchanged), so after clean -> dirty ->
    revert-to-clean, "most recently written" is the DIRTY sibling, not the
    tree as it now stands -- a reader (`graph stats`, say) then reads stale
    content with no warning at all.

    Computed through the SAME `worktree_key.reuse_key` every writer already
    calls to decide reuse (never a second copy of that hash): an exact
    `content_key` match wins outright, regardless of which write put it
    there or when. No match (the tree moved to a state nothing under `base`
    has measured yet) falls back to `_pick_latest` and returns a note
    naming the snapshot being read and that it does not match the tree's
    current state -- the caller prints it, never swallows it silently.
    """
    snaps = snapshot_dirs(base)
    if not snaps:
        raise SnapshotMissing(missing_message(base))
    scope = dirty_scope(root, descriptor)
    dirty = working_tree_dirty(root, scope)
    key = reuse_key(root, descriptor, dirty=dirty, scope=scope)
    if key is not None:
        for d in snaps:
            try:
                idx = load_dir(d, record=False)
            except SnapshotMissing:
                continue
            if idx.get("content_key") == key:
                return d, None
    fallback = _pick_latest(snaps)
    note = (f"reading {fallback.name} — no stored snapshot matches the tree's current "
           f"state; showing the most recently measured one instead ({fresh_hint(base)})")
    return fallback, note


def find_reusable(base: Path, key: "str | None", tool_stamp: Any,
                  mode: str = "port") -> "Path | None":
    """The snapshot under `base` whose own `content_key`/`tool`/`mode` match
    `key`/`tool_stamp`/`mode` EXACTLY — handed back instead of rebuilt.
    Scans every candidate, not just "latest": a caller building dirty state
    `B` on a commit that already carries dirty state `A` must not match
    `A`'s key, and a caller returning to `A` after `B` must still find it,
    wherever it sits among the base's other content-keyed siblings. A
    STRICT reading (approved): a dirty key never matches a clean snapshot's
    key or a different dirty snapshot's key, by construction — `content_key`
    already makes those distinct hashes, so no extra branch is needed here
    to keep a dirty build from falling back to a clean one.

    `key=None` (no content identity to reuse on — non-git tree, unresolved
    HEAD) always misses. `None` on no match, a `mode` mismatch (a `daily`
    scratch pass never satisfies a `port` reuse check, or the reverse), or a
    broken/unreadable candidate (a directory mid-write) — the caller then
    builds normally.
    """
    if key is None:
        return None
    for d in snapshot_dirs(base):
        try:
            idx = load_dir(d, record=False)
        except SnapshotMissing:
            continue
        if idx.get("mode", "port") != mode:
            continue
        if idx.get("content_key") != key:
            continue
        if not codegraph_snapshot_reader.same_tool(idx.get("tool"), tool_stamp):
            continue
        return d
    return None


def resolve_keep(descriptor: Any) -> int:
    """How many snapshots one base may hold — the project's own declared
    `snapshot.keep` (`ProjectDescriptor.snapshot_keep`) when set, else
    `DEFAULT_KEEP`. Duck-typed (`getattr`) so this module never needs to
    import the descriptor package back."""
    n = getattr(descriptor, "snapshot_keep", None)
    return n if n is not None else DEFAULT_KEEP


def load_dir(d: Path, *, record: bool = True) -> dict:
    """Read `_index.json` of one snapshot directory (the machine door).

    Human-readable unit YAML exists too; both are generated from the same
    single AST pass, so they cannot disagree.

    Delegates to `codegraph_snapshot_reader.read_index` — the SAME flat
    module a caller outside this package (hs-run, say) reads through, so a
    `schema_version` this reader does not know is rejected identically from
    either door. Re-raised as `SnapshotMissing` (this package's own "honest
    cannot-run") rather than propagating `SnapshotIndexError` directly, so
    every existing caller in this package that already catches
    `SnapshotMissing` keeps working unchanged.

    `record=False` for a read that only CHOOSES a snapshot (see
    `codegraph_snapshot_reader.recording`): it is not a tree the answer came from.
    """
    try:
        return codegraph_snapshot_reader.read_index(d, record=record)
    except codegraph_snapshot_reader.SnapshotIndexError as exc:
        raise SnapshotMissing(str(exc)) from exc


def snapshot_mode(index: dict) -> str:
    """A snapshot's role: `port` = frozen-at-milestone, `daily` = rolling HEAD.
    Snapshots predating the `mode` field count as `port` — correct, none other
    existed yet. The pipeline's one-base-one-role law reads roles through here."""
    return index.get("mode") or "port"
