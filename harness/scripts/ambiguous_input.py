"""One refusal for an input that names several things: exit 2, every match, never a guess.

Not found keeps its own rung. With the two merged, the candidates were the first thing lost:
`graph unit core` printed `null` as its JSON payload, `change impact ... run` printed `"trees": {}`.
Every lookup that can meet several matches builds its refusal here, so the shape is one.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Iterable

EXIT = 2


class Ambiguous(LookupError, ValueError):
    """`query` names several `kind`s; `candidates` holds each match, sorted. Also a
    `ValueError`, the type the unit resolver's callers already catch. With no `query`,
    `kind` is the whole head ("`.port/port.yaml` declares 2 source(s)").

    Flat, stdlib only: `port_batch_reader`, which hs-run reuses, imports it and must
    never pull in `codegraph`."""

    def __init__(self, kind: str, query: str, candidates: Iterable[str], hint: str) -> None:
        self.kind, self.query, self.hint = kind, query, hint
        self.candidates = sorted(set(candidates))
        head = "%s %r matches %d" % (kind, query, len(self.candidates)) if query else kind
        super().__init__("%s (%s) -- %s" % (head, ", ".join(self.candidates), hint))

    def __contains__(self, text: str) -> bool:
        """`"--source" in problem`: a refusal handed back as a problem reads like its text."""
        return text in str(self)

    @property
    def payload(self) -> dict:
        return {"error": str(self), "kind": self.kind, "query": self.query,
                "candidates": self.candidates}

    def refuse(self, opts: Any) -> int:
        """stderr always; the payload on stdout when the caller reads JSON (the envelope's `data`)."""
        print(self, file=sys.stderr)
        if getattr(opts, "json", False):
            print(json.dumps(self.payload, sort_keys=True, ensure_ascii=False))
        return EXIT


NOT_FOUND = 1


class NotFound(LookupError):
    """`query`, a name the caller gave, names no `kind`: exit 1, never 2 -- 2 is kept for
    a name that matches several, which carries its `candidates`. A config file whose own
    cross-reference names nothing is broken config, not this."""

    def __init__(self, message: str, kind: str, query: str) -> None:
        self.kind, self.query = kind, query
        super().__init__(message)

    @property
    def payload(self) -> dict:
        return {"error": str(self), "kind": self.kind, "query": self.query}

    def __contains__(self, text: str) -> bool:
        return text in str(self)

    def refuse(self, opts: Any) -> int:
        """stderr always; the payload on stdout when the caller reads JSON (the envelope's `data`)."""
        print(self, file=sys.stderr)
        if getattr(opts, "json", False):
            print(json.dumps(self.payload, sort_keys=True, ensure_ascii=False))
        return NOT_FOUND
