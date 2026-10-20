"""hs_family.versioning — the one INT schema/format-version membership check,
shared by every reader that compares a document's declared version against a
known set.

Measured before this module existed: `codegraph_snapshot_reader.read_index`
(`got not in KNOWN_SCHEMA_VERSIONS`) and `codegraph/_core/descriptor/store.py`
(`version != KERNEL_SCHEMA_VERSION`) each did the comparison with a bare `in`/
`!=`, and Python's numeric-tower equality makes BOTH shapes accept a value
that is not really the version the reader thinks it read: `2.0 == 2` is
True, so `2.0 in frozenset({2})` is True, and `2.0 != 2` is False — a
`schema_version: 2.0` document reads as `schema_version: 2` at either call
site. `True == 1` is True too (`bool` is an `int` subclass), so `True in
frozenset({1})` is also True wherever `1` happens to be a known version.

`int_version_problem` closes both call sites with one function rather than
one `type(...) is int` guard hand-added at each: a second reader added later
(a third snapshot/descriptor format, say) gets the same check by calling
this, not by remembering to re-derive it.

Pure and side-effect-free (`-> str | None`, never raises) rather than an
`hs_family` error type: the two current call sites wrap the same problem in
TWO DIFFERENT exception classes with two different message shapes
(`SnapshotIndexError` naming the file and the known-version list one way,
`DescriptorError` naming the kernel's own single supported version another)
— a shared function that raised would force one of those shapes on both
callers, or a parametrized error class neither currently needs. Returning
the problem as text lets each caller keep its own wording and exception
type; only the JUDGMENT (is this int, and known) is shared.
"""
from __future__ import annotations

from typing import Any, Collection


def int_version_problem(value: Any, known: "Collection[int]") -> "str | None":
    """`None` when `value` is a genuine `int` member of `known`; otherwise a
    short, caller-agnostic description of what is wrong — fold it into
    whatever exception/message shape the caller already uses.

    `type(value) is int`, never `isinstance`: `isinstance(True, int)` is
    True (bool IS an int subclass) and a `bool`/`float`/numeric `str` all
    compare equal to the int they spell under `==`/`in`, which is exactly
    what let `2.0`, `True`, and (via a laxer caller) `"2"` slip past a bare
    membership check. `type(...) is int` rejects all three outright, without
    three separate isinstance calls.
    """
    if type(value) is not int:
        return "must be an int, got %s %r" % (type(value).__name__, value)
    if value not in known:
        known_s = ", ".join(str(v) for v in sorted(known))
        return "unknown value %r (known: %s)" % (value, known_s)
    return None
