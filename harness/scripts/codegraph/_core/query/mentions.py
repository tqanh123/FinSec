"""`graph grep`'s door: a user regex, bounded, over `code_mentions`' classifier.

The classifier itself is flat (`code_mentions`) so `hs_run_facts` can read it
without reaching this package; this module adds what only a typed-in pattern
needs -- a per-file and a whole-scan time bound (`regex_bound`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

import code_mentions

from .. import regex_bound

KINDS = code_mentions.KINDS
tracked_files = code_mentions.tracked_files

_GREP_TOTAL_SECONDS = 60.0
_PER_FILE_SECONDS = 2.0


def scan(repo: Path, pattern: str, *, kinds: "Iterable[str] | None" = None,
         ignore_case: bool = False, module_of: "Callable[[str], str | None]" = lambda p: None,
         files: "Iterable[str] | None" = None) -> "tuple[list[dict], list[str]]":
    """`code_mentions.scan_with` for a typed-in regex: one bound per file, one
    for the whole scan, so a catastrophic pattern ends as a refusal."""
    try:
        pat = regex_bound.compile_bounded(pattern, regex_bound.regex.I if ignore_case else 0)
    except regex_bound.regex.error as e:
        raise ValueError("not a regex: %r (%s)" % (pattern, e)) from None
    budget = regex_bound.ScanBudget(total_seconds=_GREP_TOTAL_SECONDS)

    def find(rel: str, text: str) -> list:
        try:
            return list(pat.finditer(text, timeout=budget.next_timeout(_PER_FILE_SECONDS)))
        except TimeoutError:
            raise ValueError("pattern %r ran past %.0fs on %s -- simplify it"
                             % (pattern, _PER_FILE_SECONDS, rel)) from None
        except regex_bound.PatternBudgetExceeded as e:
            raise ValueError(str(e)) from None

    return code_mentions.scan_with(repo, find, kinds=kinds, module_of=module_of, files=files)
