"""Guard of the external data root (`finsec_alpharadar.data_root`, plan 261022-1147 phase 1, R1.1).

Every test passes an explicit environment mapping: none reads `os.environ` (the dev machine sets FINSEC_DATA_ROOT).
The git-backed tests run `git init` in `tmp_path`; no network, no clock, no sleep.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from finsec_alpharadar.data_root import (
    DATA_ROOT_ENV,
    DataRootError,
    DataRootInsideWorkTreeError,
    find_work_tree,
    resolve_data_root,
    resolve_external_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed with exit {result.returncode}: {result.stderr.strip()}")


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A fresh git work tree whose `.gitignore` ignores `/capture-data/` (written with LF: no CRLF blank-line pattern)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_bytes(b"/capture-data/\n")
    return repo.resolve()


def _no_work_tree(path: Path) -> None:
    return None


def _check_ignore_must_not_run(work_tree: Path, query: str) -> tuple[int, str]:
    raise AssertionError("git check-ignore must not run for a root outside any work tree")


def test_unset_or_empty_root_is_refused() -> None:
    for env in ({}, {DATA_ROOT_ENV: ""}, {DATA_ROOT_ENV: "   "}):
        with pytest.raises(DataRootError, match=f"{DATA_ROOT_ENV} is not set"):
            resolve_data_root(env)


def test_relative_root_is_refused() -> None:
    for value in ("capture-root", "./capture-root", "../FinSec_data"):
        with pytest.raises(DataRootError, match="absolute") as caught:
            resolve_data_root({DATA_ROOT_ENV: value})
        assert DATA_ROOT_ENV in str(caught.value)


def test_root_outside_any_work_tree_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "finsec-data"
    resolved = resolve_external_dir(
        {DATA_ROOT_ENV: str(root)}, DATA_ROOT_ENV, work_tree_of=_no_work_tree, check_ignore=_check_ignore_must_not_run
    )
    assert resolved == root.resolve()
    assert not root.exists(), "the guard must not create the directory"


def test_find_work_tree_walks_up_to_a_dot_git_dir_or_file(tmp_path: Path) -> None:
    with_dir = tmp_path / "with-dir"
    (with_dir / ".git").mkdir(parents=True)
    with_file = tmp_path / "with-file"
    with_file.mkdir()
    (with_file / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a linked worktree's .git is a file
    plain = tmp_path / "plain"
    plain.mkdir()

    assert find_work_tree(with_dir / "a" / "b") == with_dir  # the start path need not exist
    assert find_work_tree(with_dir) == with_dir
    assert find_work_tree(with_file / "raw") == with_file
    found = find_work_tree(plain / "x")
    assert found is None or not found.is_relative_to(tmp_path), f"no .git under tmp_path above plain/, got {found}"


def test_unignored_root_inside_a_work_tree_is_refused(git_repo: Path) -> None:
    with pytest.raises(DataRootInsideWorkTreeError) as caught:
        resolve_data_root({DATA_ROOT_ENV: str(git_repo / "capture-root")})
    message = str(caught.value)
    assert str(git_repo) in message, message
    assert DATA_ROOT_ENV in message and "outside the clone" in message and "/data/" in message, message
    assert not (git_repo / "capture-root").exists()


def test_ignored_root_inside_a_work_tree_is_accepted(git_repo: Path) -> None:
    root = git_repo / "capture-data"
    assert resolve_data_root({DATA_ROOT_ENV: str(root)}) == root
    assert not root.exists(), "the guard must not create the directory"


def test_git_failure_is_an_error_not_an_acceptance(tmp_path: Path) -> None:
    def failing_check_ignore(work_tree: Path, query: str) -> tuple[int, str]:
        return 128, "fatal: not a git repository"

    with pytest.raises(DataRootError) as caught:
        resolve_external_dir(
            {DATA_ROOT_ENV: str(tmp_path / "root")},
            DATA_ROOT_ENV,
            work_tree_of=lambda path: tmp_path.resolve(),
            check_ignore=failing_check_ignore,
        )
    assert not isinstance(caught.value, DataRootInsideWorkTreeError)
    message = str(caught.value)
    assert "128" in message and "fatal: not a git repository" in message, message


def test_env_example_declares_an_empty_data_root() -> None:
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert f"{DATA_ROOT_ENV}=" in lines, f".env.example must declare an empty {DATA_ROOT_ENV}= line"


def test_junction_into_a_work_tree_is_resolved_before_the_walk(git_repo: Path, tmp_path: Path) -> None:
    # A link outside the clone that points into it: a lexical parent walk from the link finds no .git (red-team r1 LT7).
    target = git_repo / "inside"
    target.mkdir()
    link = tmp_path / "outside-link"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False
        )
        assert made.returncode == 0, f"mklink /J failed with exit {made.returncode}: {made.stdout} {made.stderr}"
    else:
        os.symlink(target, link, target_is_directory=True)
    try:
        assert find_work_tree(link / "root") != git_repo, "precondition: the lexical walk from the link misses the repo"
        with pytest.raises(DataRootInsideWorkTreeError) as caught:
            resolve_data_root({DATA_ROOT_ENV: str(link / "root")})
        assert str(git_repo) in str(caught.value)
    finally:
        if sys.platform == "win32":
            os.rmdir(link)  # removes the junction only, never the target's content
        else:
            os.unlink(link)


def test_backup_dir_variable_uses_the_same_guard(git_repo: Path) -> None:
    name = "FINSEC_BACKUP_DIR"
    cases = ({}, {name: "relative/backup"}, {name: str(git_repo / "backup")})
    for env in cases:
        with pytest.raises(DataRootError) as caught:
            resolve_external_dir(env, name)
        message = str(caught.value)
        assert name in message and DATA_ROOT_ENV not in message, message
    with pytest.raises(DataRootInsideWorkTreeError):
        resolve_external_dir({name: str(git_repo / "backup")}, name)
