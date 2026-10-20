"""One `git -C <repo> ...` runner for every domain (they cannot import each
other, so the duplicates lived in the kernel's shadow).

`run_git` returns raw stdout bytes (safe for `-z` output); `git_text` returns
stripped text. A failing command raises `GitError` unless `check=False`.

Every `repo` here is a caller-supplied path this process does not own (a
`--repo` flag, a target under `hs-code`) — git reads THAT repo's own config,
and some config keys name a program git then EXECUTES (`core.fsmonitor` is
the one measured to fire on `ls-files`: a scratch repo whose fsmonitor names
a script had it run twice through `source_files` before this hardening
landed). `_GIT_SAFE`/`_git_env` are `secret_scan_before_ship`'s
`GIT_SAFE_CONFIG`/`git_safe_env` — imported, never redefined, so this is the
same posture every other git caller in the tree carries, not a second copy
that can drift from it.

WHAT IT CANNOT SEE: no timeout, no stdin — callers that may hang (push) keep
their own `subprocess.run(..., timeout=)`; this is for local, fast queries.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import secret_scan_before_ship as secret_scan

_GIT_SAFE = secret_scan.GIT_SAFE_CONFIG
_git_env = secret_scan.git_safe_env


class GitError(RuntimeError):
    """A git command exited non-zero; the message carries git's stderr."""


def run_git(repo: Path | str, *args: str, check: bool = True) -> bytes:
    r = subprocess.run(["git", *_GIT_SAFE, "-C", str(repo), *args],
                       capture_output=True, env=_git_env())
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).decode(errors="replace").strip()
        raise GitError(f"git {' '.join(args[:2])} failed: {msg}")
    return r.stdout


def git_text(repo: Path | str, *args: str, check: bool = True) -> str:
    return run_git(repo, *args, check=check).decode(errors="replace").strip()


def source_files(repo: Path | str, *, tracked_only: bool = False) -> "list[str] | None":
    """Paths (relative to `repo`, which may be a subdirectory of the work tree)
    git counts as source: tracked plus untracked-not-ignored, sorted. None when
    `repo` is not inside a git work tree — the caller decides what "source"
    means there. A tracked file deleted from disk is still listed.

    `tracked_only=True` drops the untracked half (`--others --exclude-
    standard`) for a caller whose universe is deliberately "what git
    tracks" — moveflat's move-reference scan, check.gates' rule runner —
    so that caller reads through this ONE door instead of keeping its own
    private `ls-files -z` (no `--others`) call that could drift from it."""
    inside = run_git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.decode("utf-8", "replace").strip() != "true":
        return None
    args = ["ls-files", "-z", "--cached"]
    if not tracked_only:
        args += ["--others", "--exclude-standard"]
    raw = run_git(repo, *args)
    return sorted({p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p})


def changed_since(repo: Path | str, base_ref: str) -> "list[str]":
    """Files that differ between `merge-base(base_ref, HEAD)` and the
    CURRENT WORKING TREE, plus untracked files git does not ignore — the
    ONE file-granularity read every "what changed against a base ref" entry
    point shares. `base_ref...HEAD` alone (a plain `git diff --name-only`
    against the merge-base) misses every uncommitted edit AND every
    untracked file, answering "nothing changed" over a tree that
    demonstrably has.

    Every listing call is `-z`-terminated (`diff`/`ls-files`), so a
    `core.quotePath`-mangled non-ASCII filename comes back intact instead of
    C-quoted.

    Raises `GitError` when `base_ref` shares no history with HEAD, or on any
    other git failure — never returns an empty list to mean "the read
    itself failed": a caller selecting tests off this must see the
    failure, not a silent "safe, nothing changed"."""
    mb = git_text(repo, "merge-base", base_ref, "HEAD", check=False)
    if not mb:
        raise GitError(f"{base_ref!r} shares no history with HEAD")
    tracked = run_git(repo, "diff", "--name-only", "-z", mb).decode(errors="replace")
    untracked = run_git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode(errors="replace")
    files = {p for p in tracked.split("\0") if p} | {p for p in untracked.split("\0") if p}
    return sorted(files)


def resolve_ref(repo: Path | str, ref: str) -> str:
    """`ref` -> its full commit SHA, refusing anything git could mistake for
    an option instead of a revision. The ONE door every caller here that
    accepts a caller-supplied ref (a CLI `--base`, a stored snapshot's
    `commit` field) must go through before interpolating it into another git
    command's argv — measured hole: `--base "--output=<path>"` reached `git
    diff --name-only --output=<path>..HEAD` unvalidated, writing an arbitrary
    file and answering "nothing changed" with exit 0.

    Rejects a leading `-` outright (never even asks git) and otherwise
    resolves via `rev-parse --verify --end-of-options <ref>^{commit}` —
    `--end-of-options` (git >= 2.24) means the ref can never be re-parsed as
    a flag even if it read as one before validation, and `^{commit}` dereferences
    a tag to the commit it names rather than an object git would refuse to
    diff. The return value is always a 40-hex SHA, which cannot itself start
    with `-`, so every downstream use is safe by construction."""
    if not ref or ref.startswith("-"):
        raise GitError(f"not a valid git ref: {ref!r}")
    try:
        sha = git_text(repo, "rev-parse", "--verify", "--end-of-options",
                       f"{ref}^{{commit}}")
    except GitError as e:
        raise GitError(f"not a valid git ref: {ref!r} ({e})") from None
    if not sha:
        raise GitError(f"not a valid git ref: {ref!r}")
    return sha
