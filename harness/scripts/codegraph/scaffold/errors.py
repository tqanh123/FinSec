"""ScaffoldError -- the one exception every engine in this package raises for
a request that cannot be planned honestly (missing template key, wrong
template kind, unknown placeholder, no codemap.yaml). A CLI door turns it
into `hs_family.UsageError` (exit 2) -- never a bare `SystemExit`.

A collision found by the no-clobber check (`engine.write_plan`) is also this
type: the request COULD be planned, but writing it would overwrite a file
someone wrote, so the whole plan is refused before anything is touched --
the caller distinguishes "cannot plan" from "refused to write" by whether it
already has a rendered plan in hand, not by a second exception type.
"""
from __future__ import annotations


class ScaffoldError(Exception):
    """Cannot plan, or refused to write -- see module docstring."""
