"""protected_trees.py — the tree set an anti-forgery guard protects.

The host checkout plus every worktree `git worktree list` registers for the same repo,
keyed by the repo (never by cwd or install mode). Kept out of `harness_paths` because
this module spawns git and imports a sibling for the hardened git env; `harness_paths`
is a foundation module and resolves paths only.

Anchor tiers (`_resolve_repo_anchor`):
  global (HARNESS_BIN_ROOT set) — CLAUDE_PROJECT_DIR, then data_root().parent; else None;
  self-host — CLAUDE_PROJECT_DIR, then HARNESS_ROOT, then the cwd's git root, then bin_root().
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import harness_paths


def _reset_cache() -> None:
    """Test hook: drop the memoized `git worktree list` results."""
    _REPO_WORKTREES_CACHE.clear()


def _legacy_root_env() -> Optional[Path]:
    """HARNESS_ROOT when set, else None — never bin_root()'s fallback chain,
    which always names SOME tree (the running process's own repo)."""
    raw = os.environ.get("HARNESS_ROOT")
    return Path(raw).resolve() if raw else None


def _resolve_repo_anchor() -> Optional[Path]:
    """The repo whose worktree family is protected; tiers in the module docstring.

    Under a global layout the bin is never the project, so an unnamed project is None
    and the guard takes its project-unresolved fail-closed arm. The anchor only picks
    which member asks `git worktree list`; any member names the whole family."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        cand = Path(proj)
        if cand.is_dir():
            resolved = cand.resolve()
            if resolved != Path(resolved.anchor):
                return resolved
    if os.environ.get("HARNESS_BIN_ROOT"):
        # Global layout: the bin is never the project, and neither is a cwd walk.
        # Nothing names the project → None, so a guard takes its own
        # project-unresolved fail-closed arm instead of judging against the bin.
        dr = harness_paths.data_root()
        if not harness_paths.data_root_unresolved(dr):
            parent = dr.parent
            if parent != Path(parent.anchor):
                return parent
        return None
    explicit_bin = _legacy_root_env()
    if explicit_bin is not None:
        return explicit_bin
    cwd_root = harness_paths.current_working_tree_root()
    if cwd_root is not None:
        return cwd_root
    return harness_paths.bin_root()


_REPO_WORKTREES_CACHE: dict = {}
# Below the dispatcher's own per-core budget (hook_dispatch._core_timeout_s(),
# default 5.0s): a probe stuck longer than that is killed by the DISPATCHER
# instead, which fails closed but orphans the git child (measured: it keeps
# running past the hook's own exit). This timeout existing at all is only
# useful if it can fire first.
_GIT_WORKTREE_TIMEOUT_S = 3

#: How git ends "not a git repository" when its upward walk found nothing: at the
#: filesystem root, or at a mount point it will not cross (a tmpfs TMPDIR such as
#: /dev/shm, a separately mounted disk -- git 2.43 then names the mount).
_NON_REPO_WALK_PHRASES = ("(or any of the parent directories)",
                          "(or any parent up to mount point")


class WorktreeListError(Exception):
    """`git worktree list` was expected to work and did not — anything other
    than the legitimate "not a git repository" case (a plain, non-repo
    directory, which is not an error: `_probe_repo_worktrees` folds it into
    `(anchor,)` and never raises for it). Raised for a missing/broken `git`
    binary, a timeout, a permissions failure, or any other non-zero exit
    whose stderr does NOT say "not a git repository" — i.e. every case where
    the caller cannot tell whether other worktrees exist. A caller that is
    about to protect a compliance artifact (write_guard, gate_stage) must
    catch this and fail CLOSED rather than silently assuming no other
    worktree exists; a caller with nothing artifact-shaped at stake may
    ignore it."""


def _probe_repo_worktrees(anchor: Path) -> tuple:
    """Every worktree `anchor`'s own repo family has, per `git worktree list
    --porcelain` run FROM `anchor` — ALWAYS the full family (main checkout +
    every linked worktree), regardless of which member `anchor` itself
    happens to be; git answers the same full list from any of them. Hardened
    env (reuses `secret_scan_before_ship.hardened_git_env()` — the same hardening
    `trust_store.is_trusted()` applies for a caller-supplied `-C` target, lazy
    import so a missing/broken sibling script never bricks a guard).

    Two DIFFERENT failure shapes, handled differently on purpose:
      - `anchor` is not a git repository at all (a plain directory — MEASURED:
        git exits 128 with "fatal: not a git repository (or any of the parent
        directories): .git" on stderr, or "(or any parent up to mount point
        /dev)" when the walk stops at a mount point). Legitimate, not an error:
        there is nothing to union, so this returns `(anchor,)` same as always.
        Accepted ONLY when stderr carries one of those walk phrases (never a bare
        "not a git repository", which a FAKE `.git` file pointing at a broken
        gitdir also produces) AND walking up from `anchor`
        (`harness_paths._nearest_git_marker`) finds no `.git` at all — a real
        `.git` marker means this IS a repo whose worktree list genuinely
        failed, so that case falls through to the ambiguous arm below instead
        of silently shrinking the union to `(anchor,)`.
      - `git` itself failed — missing/broken binary (MEASURED:
        FileNotFoundError), a timeout, a permissions failure, a broken/fake
        gitdir, or any other non-zero exit that is NOT the legitimate
        non-repo case just described. This is AMBIGUOUS: other worktrees may
        exist and this call simply could not see them, so it raises
        `WorktreeListError` instead of guessing `(anchor,)` — a caller
        protecting a compliance artifact must fail closed on this, not
        silently drop worktree protection."""
    try:
        import subprocess
        import secret_scan_before_ship as _secret_scan
        # C locale: the non-repo case below is recognized by git's English stderr.
        env = dict(_secret_scan.hardened_git_env(), LC_ALL="C")
        env.pop("LANGUAGE", None)
        r = subprocess.run(
            # -z: NUL-terminated fields (never newline-split, never stripped
            # below) so a worktree path carrying a trailing space or an
            # embedded newline is read back byte-for-byte, not truncated or
            # split into a fake extra "worktree" entry.
            ["git", *_secret_scan.GIT_SAFE_CONFIG, "-C", str(anchor),
             "worktree", "list", "--porcelain", "-z"],
            capture_output=True, text=True, timeout=_GIT_WORKTREE_TIMEOUT_S,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001 — git itself could not be run: ambiguous, fail closed
        raise WorktreeListError("could not run git worktree list (%s: %s)"
                                 % (type(exc).__name__, exc)) from exc
    if r.returncode != 0:
        stderr_low = (r.stderr or "").lower()
        if ("not a git repository" in stderr_low
                and any(p in stderr_low for p in _NON_REPO_WALK_PHRASES)
                and harness_paths._nearest_git_marker(anchor) is None):
            return (anchor,)  # legitimate: a plain, non-repo directory
        raise WorktreeListError(
            "git worktree list exited %d: %s"
            % (r.returncode, (r.stderr or "").strip() or "(no stderr)"))
    out, seen = [], set()
    for field in r.stdout.split("\0"):
        if not field.startswith("worktree "):
            continue
        wt = field[len("worktree "):]  # no .strip(): a trailing space is part of the path
        if not wt:
            continue
        try:
            p = Path(wt).resolve()
        except OSError:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    if anchor not in seen:
        out.append(anchor)
    return tuple(out) if out else (anchor,)


def _repo_worktrees(anchor: Path) -> tuple:
    """Memoized wrapper over `_probe_repo_worktrees` — one real `git`
    subprocess per anchor per process, not per call. A `WorktreeListError`
    propagates uncached: it is not written into `_REPO_WORKTREES_CACHE`, so
    the next call for the same anchor (in this same process) probes again
    rather than replaying a stale failure."""
    key = str(anchor)
    if key in _REPO_WORKTREES_CACHE:
        return _REPO_WORKTREES_CACHE[key]
    result = _probe_repo_worktrees(anchor)
    _REPO_WORKTREES_CACHE[key] = result
    return result


def protected_working_trees(anchor: Optional[Path] = None) -> tuple:
    """Host plus every linked worktree of the anchor's repo — a UNION, not
    project_root()'s single redirected tree: an artifact a guard lets through stays on
    disk for a later invocation standing in any member, so every member is protected
    whatever cwd this call has. A worktree of an unrelated repo never joins.

    () when no anchor resolves (global layout, project unnamed). Raises
    `WorktreeListError` when git itself failed (see `_probe_repo_worktrees`); a guard
    must fail closed on it, never read it as "no other worktrees"."""
    if anchor is None:
        anchor = _resolve_repo_anchor()
    if anchor is None:
        return ()
    return _with_monorepo_prefix(anchor, _repo_worktrees(anchor))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _with_monorepo_prefix(anchor: Path, worktrees: tuple) -> tuple:
    """`anchor` may be a SUBDIRECTORY of its own worktree's toplevel — the
    project lives inside a monorepo (`CLAUDE_PROJECT_DIR=<toplevel>/sub`).
    `worktrees` (from `_repo_worktrees`) names each family member's TOPLEVEL
    only, so a sibling's copy of that SAME subdirectory was never in the
    union — a write to `<sibling>/sub/plans/.../plan-approval.json` matched
    no protected root even though `<toplevel>/sub/plans/...` did.

    Finds which member `anchor` sits under and, when one does, adds
    `<member>/<anchor's own relative path>` for every OTHER member too. The
    bare toplevels stay in the union unchanged — every existing caller
    already relies on them being there."""
    own_toplevel = next((w for w in worktrees if _is_under(anchor, w)), None)
    if own_toplevel is None or own_toplevel == anchor:
        return worktrees
    rel = anchor.relative_to(own_toplevel)
    out, seen = [], set()
    for w in worktrees:
        if w not in seen:
            seen.add(w)
            out.append(w)
        sub = w / rel
        if sub not in seen:
            seen.add(sub)
            out.append(sub)
    return tuple(out)
