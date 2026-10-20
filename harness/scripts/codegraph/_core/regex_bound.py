"""One bounded-regex door for every config-sourced pattern inside codegraph.

Both `query.engine.QueryEngine.where` (a CLI-supplied search pattern, matched
against every symbol name) and `rules.kinds.threshold` (`verdict_above_warn`/
`verdict_above_ceiling` regexes declared in the TARGET repo's own
codemap.yaml — someone else's config, matched against someone else's file
text) compile an arbitrary pattern and run it many times over. The `regex`
module's own `timeout=` bounds ONE call, but a pattern that stays just under
that bound, run thousands of times, still hangs the whole scan — measured:
`graph where` with a pattern costing ~0.36s/name projected to ~1054s over
23639 names, each individual call legal under a 1s per-call timeout. `ScanBudget`
adds the missing TOTAL deadline across a whole scan; `bounded_search` is the
one call site both callers go through so neither regrows an unbounded second
copy.

`ScanBudget.now` is injectable so a test proves the total-deadline behavior by
advancing a FAKE clock, never by sleeping — a wall-clock assertion is flaky
under a loaded machine.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Callable

import regex

#: Per-call bound (the `regex` module's own `timeout=`).
PATTERN_TIMEOUT_SECONDS = 1.0

#: Total wall-clock budget for one whole scan (every `search()` call a single
#: verb/rule-check pass makes), regardless of how many individual calls each
#: stayed under the per-call bound.
TOTAL_SCAN_TIMEOUT_SECONDS = 5.0


def monotonic() -> float:
    """Indirection so a test can `monkeypatch.setattr(regex_bound, "monotonic",
    fake)` — `ScanBudget`'s default `now` looks this name up at CONSTRUCTION
    time (via a `default_factory` lambda), so the patch takes effect for every
    `ScanBudget()` built after it, with no sleep anywhere."""
    return _time.monotonic()


class PatternBudgetExceeded(ValueError):
    """A whole scan's total regex deadline ran out. Never raised mid-`regex`
    call — that is `TimeoutError`, the per-call bound; this is the
    one-level-up refusal a caller raises BEFORE issuing one more `search()`."""


@dataclass
class ScanBudget:
    """Tracks total elapsed time since construction across many bounded
    `search()` calls belonging to ONE scan (one `where` query, one
    `threshold` rule's check pass)."""

    total_seconds: float = TOTAL_SCAN_TIMEOUT_SECONDS
    now: Callable[[], float] = field(default_factory=lambda: monotonic)
    _start: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._start = self.now()

    def remaining(self) -> float:
        return self.total_seconds - (self.now() - self._start)

    def next_timeout(self, per_call: float = PATTERN_TIMEOUT_SECONDS) -> float:
        """The timeout to pass to the NEXT `search()` call — the smaller of
        the per-call bound and what is left of the total budget. Raises up
        front when the deadline is already spent, so a caller never issues
        one more call past it."""
        left = self.remaining()
        if left <= 0:
            raise PatternBudgetExceeded(
                "regex scan ran past its %.0fs total deadline — simplify the "
                "pattern(s) or narrow the query" % self.total_seconds)
        return min(per_call, left)


def compile_bounded(pattern: str, flags: int = 0):
    """Compile with the `regex` module — never bare `re` — so every match
    call downstream can carry a `timeout=`. Raises `regex.error` on a bad
    pattern, same as `re.compile` would."""
    return regex.compile(pattern, flags)


def bounded_search(pat, text: str, budget: ScanBudget, *,
                   per_call: float = PATTERN_TIMEOUT_SECONDS):
    """One `search()` bounded by BOTH the per-call timeout and the scan's
    remaining total deadline. Raises `PatternBudgetExceeded` when the total
    deadline is already gone, or lets `TimeoutError` (the `regex` module's
    own) propagate when this one call ran past its own slice."""
    timeout = budget.next_timeout(per_call)
    return pat.search(text, timeout=timeout)
