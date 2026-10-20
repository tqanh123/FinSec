"""The family's one exception -> exit-code table (`exit_code_for`), and
`run_main`, the `main()` wrapper for a plain script that is not built on the
CLI core. The core (`hs_family.cli.CliBase.main`) classifies through the same
table and renders an envelope instead of printing.

Never raises: every branch returns an `int` (an `ExitCode` member, which IS an
`int`), so a caller can always do `sys.exit(run_main(...))` or, inside a CLI's
own `main(argv) -> int`, `return run_main(...)` directly.
"""
import sys
import traceback

from .codes import EXIT_DONE, EXIT_BROKEN
from .errors import HsFamilyError


def exit_code_for(exc: BaseException) -> int:
    """`HsFamilyError` -> its own code; `SystemExit` -> an int code unchanged,
    `None` -> EXIT_DONE, anything else (a leftover message) -> EXIT_BROKEN;
    any other exception -> EXIT_BROKEN."""
    if isinstance(exc, HsFamilyError):
        return int(exc.exit_code)
    if isinstance(exc, SystemExit):
        if exc.code is None:
            return int(EXIT_DONE)
        return exc.code if isinstance(exc.code, int) else int(EXIT_BROKEN)
    return int(EXIT_BROKEN)


def run_main(dispatch, *, stderr=None) -> int:
    """Call `dispatch()` (a zero-arg callable) and return the exit code the
    caller should use.

    - `dispatch()` returns an int -> passed through unchanged.
    - `dispatch()` returns `None` -> EXIT_DONE (mirrors a bare `sys.exit()`).
    - raises `HsFamilyError` (`UsageError`/`RefusedError`) -> its own
      `exit_code`, message printed to stderr.
    - raises `SystemExit` -> `.code` passed through unchanged when it is
      already an int (the correct, already-migrated shape — e.g. argparse's
      own `.error()`); a bare string/`None` code is a leftover the AST gate
      did not (yet) catch, mapped to EXIT_BROKEN rather than Python's own
      default of exit 1 (`None` -> EXIT_DONE, matching a bare `sys.exit()`).
    - any other exception -> EXIT_BROKEN, full traceback on stderr (an
      unclassified crash is exactly what EXIT_BROKEN means).
    """
    err = stderr if stderr is not None else sys.stderr
    try:
        rc = dispatch()
    except HsFamilyError as exc:
        print(str(exc), file=err)
        return exit_code_for(exc)
    except SystemExit as exc:
        if exc.code is not None and not isinstance(exc.code, int):
            print(str(exc.code), file=err)
        return exit_code_for(exc)
    except Exception as exc:
        traceback.print_exc(file=err)
        return exit_code_for(exc)
    return int(EXIT_DONE) if rc is None else int(rc)
