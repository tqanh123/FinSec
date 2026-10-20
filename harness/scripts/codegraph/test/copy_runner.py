"""codegraph.test.copy_runner -- the ONE shared copy+run engine `test prove`
and `test mutation` both use: copy a tree to a tempdir, run the tree's OWN
DECLARED `domains.test.command` (see `codegraph.test.declared_command`)
inside the copy, and confirm the REAL tree never moved.

WHY A COPY, ALWAYS
    Neither verb ever writes a source tree. `copied_tree` copies git's own
    view of the source (tracked + untracked, not ignored), so `.git`,
    `__pycache__` and every ignored artefact stay OUT (the copy runs no
    git; a stale cross-run `.pyc` reading
    a real "caught" mutation as "not-caught" is a measured upstream bug --
    `PYTHONDONTWRITEBYTECODE=1` in `run_declared`'s own env is the fix).
    `tree_digests`/`tree_changes` are the before/after guard every caller
    runs against the REAL tree -- a change is the one failure neither verb
    may paper over.

NO-GIT LIMIT
    The copy carries no `.git`. A declared command whose targets need git
    (a test marked against a commit, a fixture reading `git log`) goes red
    at the BASELINE inside the copy -- `test prove`/`test mutation` both read
    that as "baseline already red" / "copy's tests do not pass as-is" (exit
    2), never as a false "caught"/"survived".
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

#: pytest's own summary line ("1 failed in 0.08s") carries wall-clock
#: duration -- run the SAME command twice and only this token differs.
#: Flattened so two real runs of the same outcome report byte-identical
#: `tail` text (the `--write` byte-equality law every hs-code verb
#: keeps -- see test_hs_code_write_flag.py).
_DURATION = re.compile(r"\b\d+\.\d+s\b")

_SCRIPTS = Path(__file__).resolve().parents[2]  # harness/scripts/codegraph/test -> harness/scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import process_group  # noqa: E402 -- the timeout takes the runner's own children (xdist workers) too
import run_local_tests  # noqa: E402 -- ci_environment(): the SAME TMPDIR posture ci.sh runs under
from codegraph._core.git import source_files  # noqa: E402

from .declared_command import declared_test_command, expand_targets  # noqa: E402

DEFAULT_TIMEOUT = 300


_NEVER = {".git", "__pycache__"}


def _source_files(root: Path) -> "list[str]":
    """The tree both the copy and the digests are made of: git's own view
    (tracked + untracked, not ignored) inside a work tree, every file
    otherwise; `.git`/`__pycache__` never. What git ignores is what other
    processes write while a prove runs -- a coverage run creates and deletes
    `.coverage.<host>.<pid>` in the repo root mid-walk."""
    rels = source_files(root)
    if rels is None:
        rels = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    return [r for r in rels if not _NEVER & set(r.split("/"))]


def tree_digests(root: Path) -> "dict[str, str]":
    """Per-file sha256 over `_source_files` -- the before/after guard both
    verbs run against the REAL tree, per file so a refusal can name what
    moved. Generic on purpose: a declared command is not necessarily Python,
    so this is not `*.py`-scoped. A listed file gone from disk is `""`."""
    root = Path(root)
    out: "dict[str, str]" = {}
    for rel in _source_files(root):
        try:
            out[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except (FileNotFoundError, IsADirectoryError):
            out[rel] = ""
    return out


def tree_changes(before: "dict[str, str]", after: "dict[str, str]") -> "list[str]":
    """Paths added, removed or edited between two `tree_digests`."""
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def changed_tree_error(what: str, paths: "list[str]", limit: int = 20) -> RuntimeError:
    shown = ", ".join(paths[:limit]) + (f", ... (+{len(paths) - limit} more)"
                                        if len(paths) > limit else "")
    return RuntimeError(
        f"the SOURCE TREE CHANGED {what} -- this is the one thing this tool must "
        f"never do; investigate before trusting any result above. Changed "
        f"({len(paths)}): {shown}")


@contextmanager
def copied_tree(repo: Path) -> "Iterator[Path]":
    """A throwaway COPY of `repo`'s `_source_files` under a fresh tempdir,
    removed on exit whatever happens inside the `with` block."""
    repo = Path(repo)
    with tempfile.TemporaryDirectory(prefix="hs-code-test-") as tmp:
        work = Path(tmp) / "tree"
        work.mkdir()
        for rel in _source_files(repo):
            src = repo / rel
            if not src.is_file():
                continue
            dst = work / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        yield work


def run_declared(copy_root: Path, targets: "Sequence[str]", *,
                 timeout: int = DEFAULT_TIMEOUT) -> "tuple[int, str]":
    """Run `copy_root`'s own declared `domains.test.command`, expanded with
    `targets`, inside the copy. Returns `(exit_code, tail_of_output)`;
    `-1` is a timeout, `-2` an `OSError` launching the process (a runner
    named in the declaration that does not exist on this machine, say)."""
    template = declared_test_command(copy_root)
    argv = expand_targets(template, targets)
    env = run_local_tests.ci_environment()
    # A same-second stale .pyc from a PRIOR run (baseline, or a sibling
    # mutant) can make a real "caught"/"killed" mutation read as
    # "not-caught"/"survived" -- measured upstream. Never write one here.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        p = process_group.run(argv, cwd=copy_root, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except OSError as e:
        return -2, str(e)
    tail = "\n".join((p.stdout or p.stderr).strip().splitlines()[-8:])
    return p.returncode, _DURATION.sub("N.NNs", tail)
