#!/usr/bin/env python3
"""runnable_command.py — the one place a printed command names a harness program.

An envelope line like `python3 harness/scripts/plan_approval.py ...` runs only in a
repo that vendors the harness at ./harness, under whatever python3 is on PATH. Under
a global install the programs live in the shared bin and HARNESS_PYTHON names the
interpreter the launchers (`harness/bin/*`) use, so the line fails where the reader
stands. These helpers render the resolved absolute path and interpreter instead.

Interpreter: HARNESS_PYTHON when set — verbatim, because the launcher contract
word-splits it (`uv run --no-project --python 3.14 ... python`); otherwise the
interpreter running this process, which the launcher already chose and which
demonstrably runs harness code. A bare `python3` is neither.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import harness_paths  # noqa: E402


def interpreter() -> str:
    """The interpreter prefix, shell-ready."""
    raw = (os.environ.get("HARNESS_PYTHON") or "").strip()
    return raw or shlex.quote(sys.executable)


def python_script(name: str) -> str:
    """`<interpreter> <bin>/harness/scripts/<name>` — append the arguments."""
    path = harness_paths.bin_root() / "harness" / "scripts" / name
    return "%s %s" % (interpreter(), shlex.quote(str(path)))


def launcher(name: str) -> str:
    """`<bin>/harness/bin/<name>` — the on-PATH CLI by absolute path, which runs
    whether or not the bin is on PATH; it picks its own interpreter."""
    return shlex.quote(str(harness_paths.bin_root() / "harness" / "bin" / name))
