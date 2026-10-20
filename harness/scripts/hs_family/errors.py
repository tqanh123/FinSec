"""Error types `run_main` maps onto the family's exit table.

Raise one of these instead of a bare `SystemExit(<message>)`. The AST gate
(`harness/tests/test_hs_family_exit_gate.py`) forbids the bare string/f-string/
%-format shape across hs-cli's and hs-run's whole import closure — these are
the replacement, not a parallel option.

Deliberately plain `Exception` subclasses, NOT `SystemExit` subclasses: a verb
raises one, `run_main` catches it and RETURNS an int. Nothing upstream of
`run_main` should ever see the raise propagate as a process-terminating
`SystemExit` — that shape is exactly what let a caller not tell "declined on
purpose" from "crashed" apart in the first place.
"""
from .codes import EXIT_BROKEN, EXIT_REFUSED


class HsFamilyError(Exception):
    """Base: carries the exit code `run_main` must map to."""
    exit_code = EXIT_BROKEN

    def __init__(self, message: object):
        # a lookup refusal (`ambiguous_input`) is handed over as the object; kept as the
        # object, `__str__` returned it and every door that printed the error crashed
        self.message = str(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


class UsageError(HsFamilyError):
    """Bad input, unreadable/missing config, an unhandled crash surfaced on
    purpose — exit 2. Deliberately the SAME code an unclassified exception
    maps to: an operator cannot act differently on "config corrupt" vs "the
    CLI itself broke", and the table does not ask them to."""
    exit_code = EXIT_BROKEN


class RefusedError(HsFamilyError):
    """A deliberate policy stop — the CLI COULD proceed but declines on
    purpose (e.g. toggling a skill inside the shared bin tree). Exit 3."""
    exit_code = EXIT_REFUSED
