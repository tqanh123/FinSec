"""codegraph.test.declared_command -- read a target repo's OWN declared test
runner (`domains.test.command` in its `codemap.yaml`) and expand it against a
list of explicit targets. `test prove`/`test mutation` share this reader
rather than guessing a runner or a PYTHONPATH the way the upstream tool did
(see those two modules' own docstrings for the measured bug this replaces).

CONTRACT (binding -- `domains.test.command`)
    A list of strings, containing EXACTLY ONE item that IS EXACTLY the
    literal `"{targets}"` -- never absent, never embedded inside a longer
    string (`"--x={targets}"`), never repeated. That one item expands to N
    separate argv items, one per target, never a joined string. Anything
    else is refused (`DeclaredCommandError`, mapped to exit 2 by every
    caller) with a message naming `domains.test.command`, so a caller lands
    on the exact key to fix.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

_PLACEHOLDER = "{targets}"


class DeclaredCommandError(ValueError):
    """`domains.test.command` is missing, malformed, or codemap.yaml itself
    could not be loaded -- every caller maps this to exit 2."""


def declared_test_command(repo: Path) -> "tuple[str, ...]":
    """`repo`'s own `domains.test.command` template, still carrying the
    literal `"{targets}"` placeholder -- `expand_targets` substitutes it.
    Raises `DeclaredCommandError` on anything that does not satisfy this
    module's own CONTRACT above."""
    repo = Path(repo)
    doc_path = repo / "codemap.yaml"
    if not doc_path.is_file():
        import codemap_adoption
        raise DeclaredCommandError(
            f"domains.test.command: no codemap.yaml at {doc_path} -- declare "
            f"domains.test.command: [<argv...>, \"{_PLACEHOLDER}\"] there before "
            f"proving or probing a mutation (no map yet: {codemap_adoption.REMEDY})")
    from codegraph._core.descriptor import DescriptorError, load
    try:
        descriptor = load(doc_path, repo).descriptor
    except DescriptorError as e:
        raise DeclaredCommandError(str(e)) from None
    block = (descriptor.domains or {}).get("test") or {}
    command = block.get("command")
    if command is None:
        raise DeclaredCommandError(
            "domains.test.command: not declared in codemap.yaml -- add "
            f"domains.test.command: [<argv...>, \"{_PLACEHOLDER}\"] naming this "
            f"repo's own test runner")
    import test_runners
    try:
        return test_runners.check_argv(command, "domains.test.command")
    except test_runners.RunnerError as e:
        raise DeclaredCommandError(str(e)) from None


def expand_targets(template: "tuple[str, ...]", targets: "Sequence[str]") -> "list[str]":
    """`template`'s own `"{targets}"` item replaced by `targets` -- N separate
    argv items, one per target, never a joined string. `targets` must be
    non-empty: a caller with nothing to name passes `["."]` (the whole
    declared scope) rather than this function inventing that default."""
    if not targets:
        raise DeclaredCommandError("expand_targets: at least one target is required")
    out: "list[str]" = []
    for item in template:
        if item == _PLACEHOLDER:
            out.extend(targets)
        else:
            out.append(item)
    return out
