"""Which checkout a directory is: a linked worktree's main checkout, through
`git rev-parse --git-common-dir`. A leaf: `config` and `detect` both read it."""
from __future__ import annotations

import pathlib
import subprocess


def _common_dir(repo: pathlib.Path) -> pathlib.Path | None:
    """`git rev-parse --git-common-dir` resolved against `repo`, or None."""
    if not (repo / ".git").exists():
        return None
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=str(repo),
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return (repo / common).resolve() if common else None


def main_checkout(repo: pathlib.Path) -> pathlib.Path | None:
    """The main checkout a LINKED worktree belongs to; None for the main checkout
    itself or a directory that is not a worktree."""
    if not (repo / ".git").is_file():
        return None
    common = _common_dir(repo)
    if common is None:
        return None
    main = common.parent
    return None if main == repo.resolve() else main
