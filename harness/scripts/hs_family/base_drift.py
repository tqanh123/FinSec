"""hs_family.base_drift — warn when a LOCAL base branch has drifted from its
`origin/<base>` counterpart.

A verb that takes a base branch (a diff/port-style command comparing HEAD
against `--base main`, say) reads the LOCAL `<base>` ref. If that local copy
is stale against `origin/<base>`, the comparison silently uses a different
merge base than a fresh checkout would — worth a warning, never worth
blocking on: the caller asked to compare against a NAMED branch, and a
missing/absent upstream is a completely ordinary shape (no remote named
`origin`, a base branch that was never pushed, a repo with no remote
configured at all) rather than a misconfiguration. So this module WARNS on a
measurable divergence and stays SILENT when there is nothing to compare
against — never raises, because a comparison helper that can crash the verb
it decorates is worse than the drift it is meant to catch.

`affected_tests.py --base` calls it today. Every verb that accepts a `base`
argument MUST call
`warn_if_base_drifted` before using that ref for anything — see this
module's own test file for the pinned contract it must honor.
"""
from __future__ import annotations

import subprocess
import sys


def _rev(repo, ref) -> str | None:
    """The commit `ref` resolves to in `repo`, or None if it does not exist.

    Never raises: an absent ref (no such local branch, no such remote-
    tracking branch, `repo` not a git worktree at all) is the ordinary case
    every caller here treats as "nothing to compare" — not a crash.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha or None


def warn_if_base_drifted(repo, base: str, *, stderr=None) -> str | None:
    """Compare local branch `<base>` against its `origin/<base>` counterpart.

    Returns (and prints to `stderr`) a warning when the two resolve to
    different commits. Returns None — silently — when `base` is not a plain
    local branch with an `origin/` counterpart, or when the two already agree.
    `stderr` defaults to `sys.stderr`; a caller passes its own stream to
    capture the message instead of printing it.

    Both sides resolve by FULL ref path (`refs/heads/<base>`,
    `refs/remotes/origin/<base>`), never the bare spelling: a bare
    `origin/<base>` also parses when `<base>` is a revision expression
    (`origin/HEAD~1` is valid whenever `refs/remotes/origin/HEAD` exists, as it
    does in every normal clone), which would compare two unrelated revisions.
    A base already spelled `origin/...` is skipped outright. Every caller gets
    this for free instead of filtering the shape itself.
    """
    if base.startswith("origin/"):
        return None
    local = _rev(repo, "refs/heads/%s" % base)
    upstream = _rev(repo, "refs/remotes/origin/%s" % base)
    if local is None or upstream is None or local == upstream:
        return None
    msg = ("warning: local %r (%s) has drifted from origin/%s (%s) — this "
           "run compares against the LOCAL copy" % (base, local[:12], base, upstream[:12]))
    print(msg, file=stderr if stderr is not None else sys.stderr)
    return msg
