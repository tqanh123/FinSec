"""Evidence anchors: the {source_sha, date} pair an artefact carries inside
itself (a redlist upstream-red sidecar, a port judge packet header) -- so the
reader of that artefact can tell WHICH tree it was measured against and WHEN,
rather than trusting a file that could have been generated anywhere.

Port of ob-tools' `codemap.evidence` (pinned commit `60075cb`), `stamp_fields`
only: an anchor rides inside an artefact someone reads, never in a stamp file
of its own.

WHAT IT CANNOT SEE
    * A stamp asserts WHERE it claims to have been measured; nothing here
      verifies the claim against the real tree.
    * `tree` not a git repo, or an unborn branch, leaves `source_sha` out —
      the date alone is for humans reading the file, not a fence.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from codegraph._core.git import GitError, git_text


def stamp_fields(tree: "Path | None" = None) -> dict:
    """{source_sha, date} for an artefact about to be written."""
    out: dict = {"date": _dt.date.today().isoformat()}
    if tree is not None:
        try:
            sha = git_text(tree, "rev-parse", "HEAD", check=False)
        except (GitError, OSError):
            sha = ""
        # an unborn branch answers "HEAD" (exit 0) — that is not a commit
        if re.fullmatch(r"[0-9a-f]{7,40}", sha):
            out["source_sha"] = sha
    return out
