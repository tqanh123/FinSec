"""hs_family — the shared exit-code table + error types for the hs-* CLI
family (hs-cli, hs-run, and the upcoming hs-code).

See `codes.py` for the table, `errors.py` for what a verb raises instead of a
bare `SystemExit(<str>)`, and `main_wrapper.py` for the one table mapping an
exception onto an exit code (`exit_code_for`, read by the CLI core and by
`run_main`). `registry.py` is the third primitive: the generic
base+fragments `{domain: {verb: spec}}` loader hs-run's own registry
pipeline was extracted from — a second CLI reuses it by constructing its own
`RegistryEnv`, never by forking the merge algorithm. `common.py` is the
fourth: the two flags (`--version`, `--list`) shared by every hs-* CLI.
`base_drift.py` is the fifth: the shared "local base vs. `origin/<base>`"
warning a verb that takes a base branch calls. `gate_registry.py` is the
sixth: the one book of gates `hs-cli gates` reads, one page per kind under
`harness/data/gate-registry.d/`; `tree_scanning_tests.py` is its test-gate
criterion. This package is a
deliberately separate primitive from `grid/` (harness/scripts/grid/) — not
related, just laid out the same way: `__init__.py` + relative intra-package
imports, reached as `hs_family` with harness/scripts on sys.path (the route
every hs-* entry point uses; `registry.py` imports its sibling scripts by bare
name, so no other route works).

`registry` is NOT imported here: it needs attrs and ruamel, and callers that
only want the exit table or `base_drift` stay stdlib-only. Import it as
`hs_family.registry` where it is used.
"""
from .codes import EXIT_DONE, EXIT_FINDINGS, EXIT_BROKEN, EXIT_REFUSED, ExitCode
from .errors import HsFamilyError, UsageError, RefusedError
from .main_wrapper import run_main
from . import common
from . import base_drift

__all__ = [
    "ExitCode",
    "EXIT_DONE",
    "EXIT_FINDINGS",
    "EXIT_BROKEN",
    "EXIT_REFUSED",
    "HsFamilyError",
    "UsageError",
    "RefusedError",
    "run_main",
    "common",
    "base_drift",
]
