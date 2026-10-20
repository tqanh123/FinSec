#!/usr/bin/env python3
"""_errors.py — the install package's one error type (extracted from
install.py so submodules can raise it without importing the orchestrator)."""


class InstallError(Exception):
    """A deployer-actionable failure (bad input) — the message names the file
    and the fix so the installer reports it plainly instead of dumping a raw
    traceback at onboarding time."""


class UndeclaredRuntimeError(ValueError):
    """`--runtime <name>` named a runtime the capability table does not
    declare — a DELIBERATE refusal (exit 3), never a broken-data crash (exit
    2). Subclasses ValueError so a caller that still catches the bare type
    (e.g. `_runtime_wire.unwire`'s unrelated JSON-decode guard) keeps working
    unchanged; `install.py`'s `main()` catches this class before the generic
    `ValueError` arm so the two paths cannot collapse into one exit code
    again — that collapse is what let a missing agent frontmatter or a
    corrupt JSON (`json.JSONDecodeError` is itself a `ValueError` subclass)
    read as a refusal instead of the broken-data crash it actually is."""
