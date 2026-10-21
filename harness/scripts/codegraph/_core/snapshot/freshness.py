"""Is a snapshot's stamped commit still the repo's HEAD.

Promoted out of `obt rules`, the first verb that needed it (private,
module-level helpers there) — measured bug: the store's "latest" snapshot
was yesterday's commit while HEAD had moved on, and the verb ran green over
the old tree with no line saying so. A second verb wanting the same refusal
reads this module instead of writing its own commit comparison.

SCOPED TO THE MEASURED ROOTS
    Measured bug: a commit that only touches `plans/*.md` (outside every
    declared root) got refused the same as a real source change, because the
    comparison covered the whole repo. `roots` (a caller's
    `descriptor.roots` paths) narrows both the commit-vs-commit diff and the
    working-tree dirt check to those paths — HEAD moving is not the same
    fact as the MEASURED TREE moving. `roots=None` (or empty — nothing
    statically declared, e.g. a pure auto-discover project) keeps the old
    whole-repo comparison: without a known scope there is nothing safe to
    narrow to.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..git import GitError, git_text, resolve_ref, run_git

def fresh_hint(store: "Path | None" = None) -> str:
    """What `--fresh` does, in the words every freshness hint uses: it measures the
    files on disk (`take_snapshot` walks the filesystem, so a modified or untracked
    module is in the answer), never the HEAD commit, and never into a tracked file.
    `store` is where this caller's `--fresh` files its measurement for reuse; None
    when it files nothing there (a pinned snapshot dir, a check that measures into
    a scratch dir), so no path is promised."""
    hint = ("--fresh measures the tree as it is on disk now (uncommitted files "
            "included), never into a tracked file")
    if store is None:
        return hint
    return hint + (f"; it files the measurement in {store} and reuses it for the "
                   "same content")


def head_of(repo: Path) -> str | None:
    """HEAD sha, or None when the tree is not a git checkout at all."""
    try:
        if git_text(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            return None
        return git_text(repo, "rev-parse", "HEAD", check=False) or None
    except (GitError, OSError):
        return None


def commits_match(a: str, b: str) -> bool:
    """Prefix compare either way: a snapshot may carry only a short sha
    while HEAD is always full — both must read as "the same commit", never
    as a silent mismatch."""
    a, b = a.lower(), b.lower()
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _changed_paths(repo: Path, a: str, b: str, roots: Sequence[str]) -> list[str] | None:
    """Paths that differ between commits `a` and `b`, restricted to `roots` —
    `None` when the comparison itself could not run (bad ref, shallow clone),
    never confused with an empty, successful diff.

    `a` (a snapshot's stamped `commit`, read back from a snapshot file — not
    necessarily produced by THIS process) is resolved via `git.resolve_ref`
    before it reaches `diff` argv, the same door `incremental.changed_files`
    uses for its own caller-supplied `--base` — a value shaped like a git
    option must never be interpolated unvalidated. `b` is always this
    process's own `git rev-parse HEAD` output, already a bare SHA."""
    try:
        a_sha = resolve_ref(repo, a)
        # `-z` (NUL-separated): plain `--name-only` C-quotes a non-ASCII path under
        # git's default `core.quotePath=true`; the caller must see the real path.
        raw = run_git(repo, "diff", "--name-only", "-z", "--end-of-options",
                      a_sha, b, "--", *roots)
    except (GitError, OSError):
        return None
    out = raw.decode("utf-8", "surrogateescape")
    return [p for p in out.split("\0") if p]


def dirty_paths(repo: Path, roots: "Sequence[str] | None" = None) -> "list[str]":
    """Every path with uncommitted state inside `roots` (the whole repo when
    `roots` is `None`/empty) — modified, staged, deleted, or untracked-not-
    ignored — repo-relative, in `git status` order; `[]` off a git checkout.
    `-z`: git's default `core.quotePath=true` would C-quote a non-ASCII path.
    Inside a checkout a `git status` that fails (a pathspec outside the work tree,
    a broken index) raises `GitError`: an unanswered question is no clean tree."""
    argv = ("status", "--porcelain", "-z", "--", *roots) if roots else ("status", "--porcelain", "-z")
    try:
        raw = run_git(repo, *argv)
    except GitError:
        try:
            run_git(repo, "rev-parse", "--is-inside-work-tree")
        except (GitError, OSError):
            return []
        raise
    except OSError:
        return []
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    out: "list[str]" = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        out.append(entry[3:])
        if entry[0] in "RC":
            i += 1  # a rename/copy entry is followed by its origin path
    return out


def working_tree_dirty(repo: Path, roots: "Sequence[str] | None" = None) -> bool:
    """True when `repo` has uncommitted changes inside `roots` (the whole
    repo when `roots` is `None`/empty). The ONE door both `check_freshness`'s
    own "uncommitted changes" warning below and any reuse-on-clean-HEAD
    decision (a snapshot pass deciding whether it may hand back an existing
    snapshot instead of rebuilding) ask through -- so the two can never
    silently define "dirty" two different ways."""
    return bool(dirty_paths(repo, roots))


def snapshot_scope(roots: "Sequence[str] | None", idx: "dict | None") -> "list[str]":
    """The paths whose change changes what a snapshot says: its roots plus the config
    inputs it recorded (`config_inputs`, a tsconfig outside the roots). No roots is
    the whole tree, which already holds them."""
    roots = list(roots or ())
    if not roots or "." in roots:
        return roots
    return roots + [str(x) for x in (idx or {}).get("config_inputs") or () if str(x) not in roots]


def check_freshness(repo: Path, snap_commit: str, *, allow_stale: bool,
                    label: str, roots: "Sequence[str] | None" = None,
                    store: "Path | None" = None
                    ) -> tuple[str | None, str | None]:
    """Compare `snap_commit` against `repo`'s current HEAD.

    Returns `(error, warning)`; at most one is set. `error` means the caller
    should refuse (exit non-zero) — a green run over an old tree checks
    nothing about the current one. `warning` is a line still worth printing
    on an otherwise-green run: stale-but-allowed, a non-git tree (the
    comparison could not run at all), a commit mismatch that turned out to
    touch nothing in scope, or a clean commit match with in-scope
    uncommitted changes on top (the snapshot does not see them).

    `store` is the snapshot store read, named as where `--fresh` files.

    `label` names the snapshot in the message (e.g. "snapshot 2026...-abcd"
    or a dir name) — the caller's own vocabulary for what it is comparing.

    `roots` narrows the comparison to those repo-relative paths (a
    descriptor's declared `layout.roots`) — HEAD moving only matters here
    when something INSIDE the measured tree moved with it. Falls back to
    comparing the whole repo when `roots` is `None`/empty: an unscoped
    caller, or a project with nothing statically declared (auto-discover),
    has no known boundary to narrow to.
    """
    head = head_of(repo)
    if head is None:
        return None, (f"{repo} is not a git checkout — snapshot-vs-HEAD "
                      f"comparison skipped, freshness not verified")
    if not commits_match(head, snap_commit):
        if roots:
            changed = _changed_paths(repo, snap_commit, head, roots)
            if changed is not None and not changed:
                return None, (
                    f"{label} is at commit {snap_commit} but HEAD is {head} — "
                    f"outside the measured roots ({', '.join(roots)}), nothing "
                    f"in scope changed")
        msg = (f"{label} is at commit {snap_commit} but HEAD is {head} — "
              f"a green run over an old tree checks nothing about the "
              f"current one ({fresh_hint(store)}; "
              f"`hs-code graph snapshot --apply` files a new default "
              f"snapshot; --allow-stale runs anyway)")
        if allow_stale:
            return None, f"stale snapshot allowed: {msg}"
        return msg, None
    if working_tree_dirty(repo, roots):
        return None, ("HEAD matches the snapshot but the tree has "
                      "uncommitted changes — the snapshot does not see them "
                      f"({fresh_hint(store)})")
    return None, None


def _dirty_or_unknown(repo: Path, roots: "Sequence[str] | None") -> "bool | None":
    """`working_tree_dirty`, or None when git cannot answer: a provenance row says
    unknown rather than failing the envelope that carries it."""
    try:
        return working_tree_dirty(repo, roots)
    except GitError:
        return None


def measured_row(repo: Path, roots: "Sequence[str] | None" = None) -> dict:
    """A fresh measurement of `repo` as one provenance row: the tree on disk, so no
    snapshot id or commit; `dirty` = uncommitted changes inside `roots`
    (`working_tree_dirty`, this module's one definition). `head`/`dirty` are None off
    a git checkout — unknown, never a guessed False."""
    head = head_of(repo)
    return {"repo": str(Path(repo).resolve()), "head": head,
            "dirty": None if head is None else _dirty_or_unknown(repo, roots),
            "stale": None if head is None else False,
            "mode": "fresh", "snapshot": None, "snapshot_commit": None}


def provenance_rows(events: "Sequence[dict]", repo: Path) -> "list[dict]":
    """`codegraph_snapshot_reader.recording()` events -> provenance rows, in read
    order, one per distinct (mode, snapshot, head). `dirty` is git's word on every
    row — uncommitted changes in the measured roots; `stale` says a stored snapshot
    is of another commit than HEAD (always False on a fresh row). A read of a
    directory a fresh measure in the same call filed is that measure, not a second
    tree."""
    measured_dirs = {e["dir"] for e in events if e["kind"] == "measure"}
    head: "str | None" = None
    head_read = False
    rows: "list[dict]" = []
    seen: set = set()
    for e in events:
        if e["kind"] == "measure":
            row = e["row"]
        elif e["dir"] in measured_dirs:
            continue
        else:
            if not head_read:
                head, head_read = head_of(repo), True
            commit = e["snapshot_commit"]
            row = {"repo": str(Path(repo).resolve()), "head": head,
                   "dirty": None if head is None
                   else _dirty_or_unknown(repo, e.get("roots") or None),
                   "stale": None if head is None or commit is None
                   else not commits_match(head, commit),
                   "mode": "snapshot", "snapshot": e["snapshot"], "snapshot_commit": commit}
        key = (row["mode"], row["snapshot"], row["head"])
        if key not in seen:
            seen.add(key)
            rows.append(row)
    return rows
