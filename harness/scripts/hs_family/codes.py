"""The hs-* family's closed exit-code table:

    0        done
    1        findings
    2        broken (crash / bad config / usage)
    3        deliberate refusal
    10/11    NOT on this table — hs-run extends it with its own "needs a
             model turn" / "needs a deterministic step" rungs in its own
             `Exit` IntEnum (hs_run.py), kept separate from this package.
    124/125/126  hs-rops only (ropslib/exec.py) — a different CLI, not touched
             here: RC_TIMEOUT=124 (started, overran --timeout), RC_NOT_STARTED=125
             (never started — tmux lane only), RC_VANISHED=126 (started, its
             process group is gone, and no rc line was ever written —
             ropslib/tmux.py's own timeout-vs-vanished branch). A rc line that
             never appeared at all (no "=" in it) is RC_UNREADABLE, a BROKEN
             read sourced from THIS table's own EXIT_BROKEN, not a fourth
             hs-rops-only code.

0/1 are plain `return` values a verb's own dispatch produces — they are never
raised. 2/3 are the pair `errors.py` + `main_wrapper.run_main` exist for: a
verb raises `UsageError`/`RefusedError` instead of a bare `SystemExit(<str>)`,
and `run_main` maps it to the matching code exactly once.
"""
import enum


class ExitCode(enum.IntEnum):
    """Closed by construction. Deliberately does NOT include hs-run's 10/11 —
    those are a per-CLI extension of this table, not a family-wide code."""
    DONE = 0
    FINDINGS = 1
    BROKEN = 2
    REFUSED = 3


EXIT_DONE = ExitCode.DONE
EXIT_FINDINGS = ExitCode.FINDINGS
EXIT_BROKEN = ExitCode.BROKEN
EXIT_REFUSED = ExitCode.REFUSED
