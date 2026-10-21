"""RefactorError -- the one exception every engine in this package raises for
a request that cannot be planned honestly (bad input, missing source, no git
repo). `move.py`'s CLI door turns it into `hs_family.UsageError` at the one
place every verb's own errors get mapped onto the family's exit table --
never a bare `SystemExit`, never surfaced past that one door."""
from __future__ import annotations


class RefactorError(Exception):
    """The request itself is malformed, or nothing was planned -- exit 2."""
