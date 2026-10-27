"""Guard for directories that must live outside the git clone: the external data root and the backup directory.

Licensed vendor data (vnstock), portal bodies that name real people and spike output must never land in the public
repository. `resolve_data_root` reads `FINSEC_DATA_ROOT`; Phase 2 calls `resolve_external_dir` for `FINSEC_BACKUP_DIR`.
A directory inside a git work tree is accepted only when git ignores it. The guard never creates a directory.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping

DATA_ROOT_ENV = "FINSEC_DATA_ROOT"

# The file asked about inside the directory. A file, not the directory itself: with core.autocrlf=true a "<dir>/" query
# matches the empty pattern git makes of a CRLF blank line in .gitignore (AGENTS.md "Verified Facts"). A .jsonl name,
# because raw captures are JSONL and `*.jsonl` is not ignored globally (only under /data/).
PROBE_FILE = "raw/probe.jsonl"

# Variables that would point git at another repository than the work tree we found (a git hook that runs pytest sets them).
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

WorkTreeFinder = Callable[[Path], Path | None]
CheckIgnore = Callable[[Path, str], tuple[int, str]]


class DataRootError(ValueError):
    """The directory variable is unset, not absolute, or git could not answer whether the directory is ignored."""


class DataRootInsideWorkTreeError(DataRootError):
    """The resolved directory lies inside a git work tree and git does not ignore it."""


def find_work_tree(path: Path) -> Path | None:
    """The nearest of `path` and its parents that holds a `.git` directory or file, or None. Filesystem only, no git.

    `path` need not exist. The walk is lexical: resolve the path first (`resolve_external_dir` does) so that a junction
    or symlink into a clone cannot hide the clone.
    """
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():  # a directory in a clone, a file in a linked worktree
            return candidate
    return None


def git_check_ignore(work_tree: Path, query: str) -> tuple[int, str]:
    """Run `git check-ignore -q <query>` in `work_tree`; return git's exit code (0 ignored, 1 not ignored) and stderr."""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARS}
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", query],
            cwd=work_tree,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as err:  # git is not installed or not on PATH
        return -1, f"could not run git: {err}"
    return result.returncode, result.stderr.strip()


def resolve_external_dir(
    env: Mapping[str, str],
    var_name: str,
    *,
    work_tree_of: WorkTreeFinder = find_work_tree,
    check_ignore: CheckIgnore = git_check_ignore,
) -> Path:
    """The resolved absolute directory named by `env[var_name]`, refused when it would put data into a git clone.

    Raises `DataRootError` when the variable is unset, empty or not absolute, or when git fails; raises
    `DataRootInsideWorkTreeError` when the resolved path is inside a git work tree that does not ignore it.
    Messages name only the variable and the paths, never another environment value.
    """
    raw = env.get(var_name, "")
    if not raw.strip():
        raise DataRootError(f"{var_name} is not set: set it to an absolute directory outside the git clone")
    path = Path(raw)
    if not path.is_absolute():
        raise DataRootError(f"{var_name} must be an absolute path, got {raw!r}")
    # Resolve BEFORE the parent walk: a junction or symlink outside the clone can point into it (red-team r1 LT7).
    resolved = path.resolve()
    work_tree = work_tree_of(resolved)
    if work_tree is None:
        return resolved
    query = (resolved / PROBE_FILE).relative_to(work_tree).as_posix()
    exit_code, stderr = check_ignore(work_tree, query)
    if exit_code == 0:
        return resolved
    if exit_code == 1:
        raise DataRootInsideWorkTreeError(
            f"{var_name} resolves to {resolved}, inside the git work tree {work_tree}, which does not ignore it; "
            "use a path outside the clone, or under /data/"
        )
    raise DataRootError(
        f"{var_name} resolves to {resolved}, inside the git work tree {work_tree}; "
        f"git check-ignore failed with exit {exit_code}: {stderr}"
    )


def resolve_data_root(env: Mapping[str, str]) -> Path:
    """The external data root from `FINSEC_DATA_ROOT` in `env` (pass `os.environ` at an entry point)."""
    return resolve_external_dir(env, DATA_ROOT_ENV)
