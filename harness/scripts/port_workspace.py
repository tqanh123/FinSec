#!/usr/bin/env python3
"""port_workspace.py — locate the `.port/` workspace the `port` domain reads.

`hs-run port next` used to read `<root>/.port/port.yaml`, where `root` is
hs-run's OWN resolved project root (`_resolve_project_root()` in `hs_run.py`
— `CLAUDE_PROJECT_DIR`, then `HARNESS_DATA_ROOT`'s parent, then a `plans/`/
`.git` walk-up, then CWD). That root answers "what project is this session
working on" — the right question for every other domain, and the wrong one
here: measured, `cd`ing into a fixture directory holding a valid
`.port/port.yaml` and running `hs-run port next` with `HARNESS_DATA_ROOT` set
to an unrelated tree answered `no_workspace` against the WRONG directory, even
though a valid workspace sat right there under the process CWD. Unsetting
`HARNESS_DATA_ROOT` "fixed" it only by accident — root fell back to plain
CWD, which happens to be right until the caller runs hs-run from anywhere
else (a subshell, a wrapper script, a different terminal tab with its own
env). Port is deliberately NOT scoped to hs-run's own project — it locates
the repo the operator is standing in RIGHT NOW, same as `git status` does,
never a project some other env var names.

This is the ONE thing this module answers. The root is the TARGET repo:
`--repo`, if given, wins outright; otherwise walk UP from the
process CWD to the first directory holding a `.port/` directory. Never
`packages/` and `plans/` — the old `ob-tools legacy.py
find_tool_root_from` required BOTH, and a flat repo (this domain's own test
fixtures, and plenty of real one-package repos) has neither.

The dist layout the port verbs write under is NOT read here: this module is in
hs-run's import closure, which never reaches `codegraph`. The codegraph-side
verbs read it through `codegraph._core.layout.target_descriptor`.
"""
from __future__ import annotations

from pathlib import Path

_PORT_DIR_NAME = ".port"
_CONFIG_REL = (_PORT_DIR_NAME, "port.yaml")


class PortRootNotFoundError(Exception):
    """No `.port/` directory found — at `--repo`, or anywhere from the start
    path up to the filesystem root.

    Two different searches raise this, and the message names which one ran:
    an explicit `--repo` that exists and IS a directory but does not hold
    `.port/` is "not found", never `BadRepoPathError` — the path itself was
    fine, there is simply nothing ported there yet, and that is a different
    problem from a typo'd/missing path (fixed by `/hs:port`, not by
    retyping `--repo`).
    """

    def __init__(self, searched: Path, *, repo_only: bool = False):
        self.searched = searched
        self.repo_only = repo_only
        if repo_only:
            message = (
                "no `.port/` directory under --repo %s — run /hs:port there "
                "to plan a port, or pass a different --repo" % searched)
        else:
            message = (
                "no `.port/` directory found at or above %s — run /hs:port "
                "to plan a port, or pass `--repo <dir>` naming a repo that "
                "already holds `.port/`" % searched)
        super().__init__(message)


class BadRepoPathError(Exception):
    """`--repo` named a path that does not exist, or exists but is not a
    directory. Distinct from `PortRootNotFoundError`: this is a bad
    ARGUMENT (fixed by retyping `--repo`), not an unported repo (fixed by
    running `/hs:port`)."""

    def __init__(self, repo: Path):
        self.repo = repo
        super().__init__(
            "--repo %s does not exist or is not a directory" % repo)


def find_port_root(start: Path, repo: Path | None = None) -> Path:
    """The directory holding `.port/`.

    `repo`, when given, wins outright and nothing is walked:
      * not an existing directory -> `BadRepoPathError`.
      * an existing directory with no `.port/` -> `PortRootNotFoundError`
        (`repo_only=True`) — a real path, just not one anything has been
        ported into yet.
      * an existing directory holding `.port/` -> returned as-is.

    Without `repo`, `start` is resolved FIRST and its existence checked
    before anything is walked — a `start` that does not exist raises
    `PortRootNotFoundError` naming it, rather than silently walking up from
    whichever ancestor happens to exist and returning an unrelated `.port/`
    found there. `start` (once confirmed to exist) and each of its parents,
    in order, are checked in turn for a `.port/` directory; the first hit
    wins. Reaching the filesystem root with no hit raises
    `PortRootNotFoundError`.

    Deliberately never checks for `packages/` or `plans/` — see the module
    docstring.
    """
    if repo is not None:
        repo_path = Path(repo).resolve()
        if not repo_path.is_dir():
            raise BadRepoPathError(repo_path)
        if (repo_path / _PORT_DIR_NAME).is_dir():
            return repo_path
        raise PortRootNotFoundError(repo_path, repo_only=True)

    start_path = Path(start).resolve()
    if not start_path.exists():
        raise PortRootNotFoundError(start_path)

    candidate = start_path if start_path.is_dir() else start_path.parent
    while True:
        if (candidate / _PORT_DIR_NAME).is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:  # filesystem root — nowhere further to go
            break
        candidate = parent

    raise PortRootNotFoundError(start_path)


def port_yaml_path(port_root: Path) -> Path:
    """`<port_root>/.port/port.yaml` — the one file this domain reads today.

    A single function rather than a module-level constant callers join by
    hand, so every reader builds the same path the same way; a second,
    independently-typed `.joinpath(".port", "port.yaml")` elsewhere is
    exactly the kind of copy that drifts when this one changes.
    """
    return Path(port_root).joinpath(*_CONFIG_REL)
