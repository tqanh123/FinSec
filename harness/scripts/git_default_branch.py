"""The repo's integration branch by convention: origin/HEAD's target, then a local
main/master. One resolver for every reader that diffs a branch against it.

A leaf on purpose: the snapshot readers (`hs_run_facts.engine`) and the review range
(`review_recall`) both ask this question, and a reader that borrowed `review_recall`'s
copy pulled the risk rubric and the change class into its own import closure -- a cycle
as soon as the change class read the snapshot back."""
from __future__ import annotations

import subprocess
from typing import Optional

import secret_scan_before_ship as secret_scan


def _git(root, *args) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root), *args],
                          capture_output=True, text=True, env=secret_scan.git_safe_env())


def main_branch(root) -> Optional[str]:
    """None when the repo has no such branch (a brand-new feature branch)."""
    head = _git(root, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if head.returncode == 0:
        name = head.stdout.strip().rsplit("/", 1)[-1]
        if name:
            return name
    for b in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "-q", b).returncode == 0:
            return b
    return None
