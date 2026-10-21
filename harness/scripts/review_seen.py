#!/usr/bin/env python3
"""What a code review read, named by content rather than by commit id.

A review counts for the work it saw. A commit id is the wrong key for that: an amend,
a rebase, a cherry-pick or a squash gives the same change a new id, and which ids are
"in history" depends on the branch checked out and the tree a shell sits in. A
patch-id (`git patch-id --stable`) names a commit's change by its content, so the same
change reads the same wherever it lands, and a stored one needs no git to compare.

Only a change a review reads carries one: a commit touching nothing but docs markdown
(`workflow_weight`'s own `docs_md` kind — cook's finalize step writes those after the
review) carries none, and neither does a merge commit.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret_scan_before_ship  # noqa: E402
import workflow_weight  # noqa: E402

_MARK = "\x01"


def _git(repo: Path, *args: str, stdin: "str | None" = None) -> "str | None":
    try:
        r = subprocess.run(["git", *secret_scan_before_ship.GIT_SAFE_CONFIG, "-C", str(repo),
                            *args], input=stdin, capture_output=True, text=True,
                           env=secret_scan_before_ship.git_safe_env())
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def _changed_paths(repo: Path, shas: list) -> "dict[str, list]":
    """sha -> the paths it changes, for each sha git knows; merges are left out."""
    out = _git(repo, "log", "--no-walk=unsorted", "--no-merges", "--name-only",
               "--format=" + _MARK + "%H", *shas)
    if out is None:
        if len(shas) == 1:
            return {}
        found: dict = {}
        for sha in shas:
            found.update(_changed_paths(repo, [sha]))
        return found
    paths: dict = {}
    sha = None
    for line in out.splitlines():
        if line.startswith(_MARK):
            sha = line[1:].strip()
            paths[sha] = []
        elif line.strip() and sha is not None:
            paths[sha].append(line.strip())
    return paths


def _reader():
    """paths -> whether a review reads any of them: anything but docs markdown."""
    try:
        cfg = workflow_weight._compile(workflow_weight.load_policy())
    except Exception:  # noqa: BLE001 — no policy: every change is one a review reads
        return bool
    return lambda paths: any(workflow_weight._classify(p, cfg) != "docs_md" for p in paths)


def patch_ids(repo, shas) -> "dict[str, str]":
    """sha -> patch-id for each of `shas` whose change a review reads. A sha git does
    not know here is left out, as is a docs-only commit and a merge."""
    repo = Path(repo)
    shas = [str(s) for s in dict.fromkeys(shas or []) if s]
    if not shas:
        return {}
    reads = _reader()
    read = [s for s, p in _changed_paths(repo, shas).items() if reads(p)]
    if not read:
        return {}
    diff = _git(repo, "log", "--no-walk=unsorted", "-p", "--no-color",
                "--format=commit %H", *read)
    ids = _git(repo, "patch-id", "--stable", stdin=diff) if diff is not None else None
    out: dict = {}
    for line in (ids or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def range_shas(repo, base: str, head: str, *, limit: int) -> list:
    """The newest `limit` non-merge commits in base..head; [] when git cannot say."""
    out = _git(Path(repo), "rev-list", "--no-merges", "--max-count=%d" % limit,
               "%s..%s" % (base, head))
    return out.split() if out else []
