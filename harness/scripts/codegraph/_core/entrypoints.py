"""Is a module a command-line entrypoint — one shared answer, not one per verb.

The default heuristic is a `main` symbol or an `argparse` import. The descriptor's
`entrypoints:` adds two declared overrides for its failure modes: `cli` for an
entrypoint it misses, `not_cli` for a library it mistakes for one.

WHAT IT CANNOT SEE
    The heuristic reads only `symbols` and `imports_external`: a CLI built another way
    (a `console_scripts` target with no local `main`, a `click` command) is invisible
    unless declared in `entrypoints.cli`.
"""
from __future__ import annotations

from .descriptor.model import DescriptorError, EntrypointsSpec


def is_cli(rec: dict, spec: "EntrypointsSpec | None" = None) -> bool:
    mod = rec.get("module")
    if spec is not None:
        if mod in spec.not_cli:
            return False
        if mod in spec.cli:
            return True
    return ("main" in {s.get("name") for s in rec.get("symbols", [])}
            or "argparse" in (rec.get("imports_external") or []))


def check_entrypoints_declared(spec: EntrypointsSpec, known: "set[str]") -> None:
    """A declared entrypoint the snapshot never measured is a typo or a moved module:
    refused by name, never read as "not declared"."""
    missing = sorted((set(spec.cli) | set(spec.not_cli)) - known)
    if missing:
        raise DescriptorError("entrypoints: %s declared but not found in the snapshot"
                              % ", ".join(missing))
