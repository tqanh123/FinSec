"""Orchestration: gate, run, judge -- in that order, and never any other.

Order IS the design. A capability refusal that arrives after a 200-second pipeline
has printed its own green summary is a refusal nobody reads; the gate therefore runs
before anything starts, and the tests assert that mechanically with a spy runner.

Four exit codes, deliberately distinct. A refusal and a failure call for opposite
next actions -- one means "take this to a lane that can run it", the other means "the
code is broken" -- and `hs-rops ci run || ...` cannot tell them apart if they share a
number.
"""
from __future__ import annotations

import sys
from typing import Callable

from rops.ropslib.gitlab import capability as cap
from rops.ropslib.gitlab import lane as gl
from rops.ropslib.gitlab import reader

RC_OK = 0
RC_BROKEN = 2        # the question could not be established: nothing ran
RC_FAILED = 1        # the pipeline ran and did not pass
RC_UNSUPPORTED = 3   # this lane cannot honestly execute it

#: (command) -> (rc, log). A seam, so gate ORDER is testable without a machine.
Runner = Callable[[str], "tuple[int, str]"]


def execute(cfg, *, doc: dict, listing: list, lane: cap.Lane,
            runner: Runner, jobs: list[str] | None = None,
            privileged: bool = False, concurrency: int = 0,
            file: str = "", variables: list[str] | None = None) -> int:
    # WHICH JOBS RUN comes first, and the capability gate then judges only those.
    #
    # Measured 260910 on document-intake: the document declares retry on 20 of its 29
    # jobs, and in a local context every one of them is ruled out by `rules:` wanting
    # merge-request variables. Judging the document as WRITTEN refused the pipeline
    # naming 20 retry jobs, while the true answer was that nothing runs here at all.
    # Both readings are `not ok`; only one of them is a reason a person can act on.
    try:
        # Narrowed the same way the gate will be: `--job a` expects one job, not the
        # whole file's worth, or every selective run reads as a shortfall.
        wanted = listing
        if jobs:
            # EXACT cell names first, base names only for a selection that named no
            # cell. `--job 'test: [1/6]'` runs one cell, so the bar is one; measured,
            # matching on the shared base name made a single-shard run report
            # `only 1 of 6 job(s) ran`. `--job test` still means all six, which is
            # what keeps a 1-of-6 run from passing as the whole job.
            exact = {str(j) for j in jobs}
            wanted = [e for e in listing
                      if isinstance(e, dict) and str(e.get("name", "")) in exact]
            if not wanted:
                names = {cap._base_name(j) for j in jobs}
                wanted = [e for e in listing
                          if isinstance(e, dict)
                          and cap._base_name(str(e.get("name", ""))) in names]
        expected = reader.expected_count(wanted)
        live = sorted({cap._base_name(str(e.get("name", ""))) for e in wanted
                       if isinstance(e, dict)
                       and e.get("when") not in ("never", "manual")})
    except reader.ReadError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return RC_BROKEN

    verdict = cap.verdict(doc, lane, jobs=live)
    if not verdict.ok:
        print(verdict.reason, file=sys.stderr)
        return RC_UNSUPPORTED

    command = gl.local_command(cfg, jobs=jobs, file=file, privileged=privileged,
                               concurrency=concurrency, variables=variables)
    rc, log = runner(command)
    outcome = gl.classify(rc=rc, log=log, expected=expected)
    print(f"pipeline: {outcome.reason}")
    return RC_OK if outcome.ok else RC_FAILED
