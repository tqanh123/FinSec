"""The one verb result type: an exit code plus an optional machine payload.

The `graph snapshot` verb (`codegraph/graph/verbs.py`) builds one internally
and converts it to a plain int only at its own return, matching the family
core's `run(ns) -> int` command contract.

Exit contract: 0 ran and clean · 1 red for a structural reason · 2 could not
run honestly (bad input, missing tree). 2 is never read as 0.

WHAT IT CANNOT SEE: it does not validate that `data` is JSON-serialisable; a
caller serializing it into the envelope's `data` fails loudly on a bad payload, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ambiguous_input import Ambiguous, NotFound


class _ClassOnly:
    """A classmethod that refuses to be read off an INSTANCE.

    `Verdict.ok(data)` builds a clean verdict. `v.ok` looks like it reports
    one — every sibling result type in this tree spells the question that way
    — but it is a bound method, and a bound method is truthy. `assert v.ok`
    was therefore green on `Verdict(exit=2)`. The constructor name is load
    bearing at every call site, so the instance read is made loud instead.
    """

    def __init__(self, fn):
        self._fn = fn
        self.__doc__ = fn.__doc__

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, owner=None):
        if obj is not None:
            raise AttributeError(
                f"{owner.__name__}.{self._name}() is a CONSTRUCTOR, not a state: "
                f"reading it off an instance yields a truthy bound method, so "
                f"`assert v.{self._name}` passes whatever the verb did. "
                f"Ask `v.exit == 0` / `v.exit` instead.")
        return self._fn.__get__(owner, owner)


@dataclass(frozen=True)
class Verdict:
    exit: int
    data: Any = None

    @_ClassOnly
    @classmethod
    def ok(cls, data: Any = None) -> "Verdict":
        return cls(0, data)

    @classmethod
    def red(cls, data: Any = None) -> "Verdict":
        return cls(1, data)

    @classmethod
    def broken(cls, error: "str | Ambiguous", **extra: Any) -> "Verdict":
        """The exit-2 refusal: `error` goes to stderr AND into the payload.

        Printed here, at the one place every refusal passes through, rather
        than at each call site — a refusal that only ever wrote its reason
        into the envelope's `data` is invisible at a plain terminal.
        """
        import sys
        if isinstance(error, Ambiguous):   # several matches: the payload carries each one
            amb: Ambiguous = error
            error, extra = str(amb), {**amb.payload, **extra}
        print(error, file=sys.stderr)
        return cls(2, {"error": error, **extra})

    @classmethod
    def refused(cls, reason: str, **extra: Any) -> "Verdict":
        """The exit-3 stop: the verb could proceed and declines on purpose.
        `reason` goes to stderr AND into the payload, like `broken`."""
        import sys
        print(reason, file=sys.stderr)
        return cls(3, {"refused": reason, **extra})

    @classmethod
    def unresolved(cls, problem: "str | Ambiguous | NotFound | Exception") -> "Verdict":
        """A lookup of a name the caller gave that did not come back as one thing:
        not found -> 1, several -> 2 with every candidate, anything else -> 2."""
        if isinstance(problem, NotFound):
            nf: NotFound = problem
            import sys
            print(nf, file=sys.stderr)
            return cls(1, nf.payload)
        return cls.broken(problem if isinstance(problem, (str, Ambiguous)) else str(problem))

    @classmethod
    def of(cls, ok: bool, data: Any = None) -> "Verdict":
        return cls(0 if ok else 1, data)
