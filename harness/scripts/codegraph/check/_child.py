"""Shared child-interpreter machinery for `hs-code check` doors that run code in the
TARGET's own interpreter, never this tool's: `smoke` imports modules directly, while
`route-parity`/`live-smoke` run a short script that prints ONE marked JSON line. All
three doors share the same two decisions — which interpreter, and how to tell a
subprocess that could not be executed at all from one that ran and disagreed — so this
is the ONE place either question is answered; a copy in a second file drifts the
moment one door's `--python` handling changes and the other's does not.

Runs through `process_group.run` (never a bare `subprocess.run`): a child that starts
children of its own (an ASGI app's background tasks, a lifespan's retry loop) leaves
them reparented to init on a bare timeout — see that module's own docstring for the
measured case this avoided.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import process_group
from codegraph.check.tree import CheckError


def interpreter(python: Any) -> str:
    """The literal file `python` names, `python` resolved on PATH, or this tool's own
    interpreter when `python` is falsy. Raises `CheckError` naming `python` when a
    non-empty value resolves to nothing runnable — never silently falls back."""
    if not python:
        return sys.executable
    if Path(python).is_file():
        return str(python)
    found = shutil.which(python)
    if not found:
        raise CheckError("--python not found: %s" % python)
    return found


def run_marked_child(argv: List[str], *, cwd: Path, env: Dict[str, str], timeout: float,
                     mark: str) -> Tuple[Optional[str], "subprocess.CompletedProcess"]:
    """Run `argv`, returning `(payload, proc)`: `payload` is the LAST line of stdout
    starting with `mark` (the mark itself stripped), or `None` when no such line ever
    printed — the caller decides what that means (a crash before the print, a wrong
    marker, ...) using `proc.stderr`/`proc.returncode`. Raises `CheckError` only for a
    timeout or an interpreter that could not even be executed — every OTHER way the
    child can fail (it ran, disagreed, or died after printing nothing) is a normal
    return, because those are findings the caller reports, not a reason this call
    itself failed."""
    try:
        proc = process_group.run(argv, cwd=str(cwd), env=env, timeout=timeout,
                                 capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        raise CheckError("the child did not finish in %gs (--timeout)" % timeout)
    except OSError as e:
        raise CheckError("cannot execute %s: %s" % (argv[0], e))
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith(mark):
            payload = line[len(mark):]
    return payload, proc
