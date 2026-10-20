"""Seal the scratch repos an e2e runner seeds from the MACHINE's git config.

The runners are plain scripts, so the suite's `pytest_configure` seal never reaches
them. A machine-wide `core.hooksPath` (git-secrets on the remote) then scans the seed
commit of a fixture tree and refuses it. `seal()` applies the same env the suite uses
(`harness/tests/_git_isolation.py`); call it at module level, before any git runs.
"""
import os
import sys
from pathlib import Path

_TESTS = str(Path(__file__).resolve().parents[1] / "tests")


def seal() -> None:
    if _TESTS not in sys.path:
        sys.path.insert(0, _TESTS)
    from _git_isolation import git_isolation_env
    os.environ.update(git_isolation_env())
