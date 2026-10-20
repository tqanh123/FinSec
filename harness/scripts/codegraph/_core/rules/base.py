"""RulesEngine — architecture law is DATA in the descriptor, not code.

A rule is a dict: {id, kind, <params>}. Each `kind` is a strategy registered
in the kind registry; adding a kind NEVER edits this engine.

WHAT IT CANNOT SEE (inherited from every consumer of rule output)
    * A rule sees what its inputs see: snapshot edges are a LOWER BOUND
      (getattr / dict dispatch / callbacks never resolve), AST re-reads see
      no runtime behaviour. A green rule is "no violation AMONG THE VISIBLE",
      never "no violation".
    * The engine validates SHAPE (known kind, required params, unique id), not
      INTENT — a rule whose params are too loose to ever redden passes shape
      validation and proves nothing.
    * `severity: warn` rules report without failing; their violations are
      still machine-readable — a warn the caller never reads is a red that
      was renamed, not a softer red.

SEVERITY IS VALIDATED HERE, NOT JUST INTERPRETED
    Measured on the reference implementation this was ported from: `raw.get
    ("severity", "error")` accepted ANY string — `"nonsense"` included — and
    `RuleResult.is_red` (`severity == "error" and violations`) silently read
    it exactly like `"warn"` (never red), while the ported CLI's own violation
    COUNT summed `len(violations)` regardless of severity and still exited 1.
    A misspelled severity therefore failed a gate it could never see printed
    as passing. `{"error", "warn"}` is the whole vocabulary; anything else is
    refused HERE, at construction (exit 2, naming the value and both valid
    options), before any snapshot is ever scanned — never a silent warn, never
    a silent error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..graph.model import Graph

#: The whole severity vocabulary. See the module docstring's own section on
#: why this is enforced here rather than merely read.
SEVERITIES: frozenset[str] = frozenset({"error", "warn"})


class RulesError(Exception):
    """Honest cannot-run (maps to exit 2): unknown kind, bad params, dup id,
    or an unrecognized `severity`."""


@dataclass(frozen=True)
class Violation:
    """One machine-readable breach. `cause` names WHICH declared failure mode
    fired — gates prove every cause red at registration."""

    rule_id: str
    cause: str
    label: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    severity: str                 # "error" | "warn" — validated at RulesEngine construction
    violations: tuple[Violation, ...]
    notes: tuple[str, ...] = ()   # counted non-verdicts (UNDECIDED, blind floor…)
    #: Machine-readable counters behind the notes (edges / undecided / …).
    #: Same numbers the prose carries, addressable by key — a caller deciding
    #: "did this rule check ANYTHING" must not parse its own output.
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def is_red(self) -> bool:
        return self.severity == "error" and bool(self.violations)


@dataclass
class RuleContext:
    """What a rule may look at. Rule kinds declare which members they need;
    the engine does not fabricate missing ones — a kind finding None where it
    needs data raises RulesError (honest 2), never an empty green.

    `snapshots` is a SECOND door onto other snapshots' own `_index.json`
    payloads, keyed by snapshot NAME — for a kind whose own module SET to
    check lives in a DIFFERENT snapshot than the one `idx`/`graph` are built
    from (a snapshot is only ever compared against the tree it was taken
    of, so `idx` stays that tree's own; a kind needing a second tree's own
    module list, like `map`'s dist-law set, looks it up here by name rather
    than the caller smuggling it in through `idx` itself). Empty by
    default — nothing before this needed a second snapshot at all."""

    repo: Path
    graph: Graph | None = None
    idx: dict | None = None       # snapshot _index.json payload
    descriptor: Any = None
    snapshots: "Mapping[str, dict]" = field(default_factory=dict)


class RuleCheck(Protocol):
    """One kind strategy: validate params at build, check against a context."""

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None: ...

    def check(self, ctx: RuleContext) -> RuleResult: ...


#: kind name -> factory(rule_id, params) -> RuleCheck
_KINDS: dict[str, Callable[[str, dict[str, Any]], RuleCheck]] = {}


def register_kind(name: str) -> Callable:
    def deco(factory: Callable[[str, dict[str, Any]], RuleCheck]) -> Callable:
        if name in _KINDS:
            raise RulesError(f"rule kind {name!r} registered twice")
        _KINDS[name] = factory
        return factory
    return deco


def kind_names() -> list[str]:
    return sorted(_KINDS)


def _require(params: dict[str, Any], rule_id: str, *keys: str) -> None:
    for k in keys:
        if params.get(k) in (None, "", [], {}):
            raise RulesError(f"rule {rule_id!r}: kind requires param {k!r}")


def require_params(*keys: str) -> Callable:
    """Decorator for factories: declare required params once, validated the
    same way for every kind — a missing param is exit 2 BEFORE any scanning,
    never a silently-skipped rule."""
    def deco(factory):
        def wrapped(rule_id: str, params: dict[str, Any]) -> RuleCheck:
            _require(params, rule_id, *keys)
            return factory(rule_id, params)
        wrapped.__name__ = factory.__name__
        return wrapped
    return deco


class RulesEngine:
    """Resolve rule dicts into strategies and run them. The only door."""

    def __init__(self, rules: list[dict[str, Any]] | None = None) -> None:
        self._rules: list[tuple[str, RuleCheck, str]] = []
        seen: set[str] = set()
        for raw in rules or []:
            if not isinstance(raw, dict):
                raise RulesError(f"rule entry is not a mapping: {raw!r}")
            rid = raw.get("id")
            kind = raw.get("kind")
            if not rid or not kind:
                raise RulesError(f"rule needs non-empty `id` and `kind`: {raw!r}")
            if rid in seen:
                raise RulesError(f"rule id {rid!r} declared twice — one id, one job")
            seen.add(rid)
            factory = _KINDS.get(kind)
            if factory is None:
                raise RulesError(
                    f"rule {rid!r} has unknown kind {kind!r}; known: {', '.join(kind_names())}")
            severity = raw.get("severity", "error")
            if severity not in SEVERITIES:
                raise RulesError(
                    f"rule {rid!r}: severity {severity!r} is not valid — the whole "
                    f"vocabulary is {', '.join(sorted(SEVERITIES))}")
            params = {k: v for k, v in raw.items() if k not in ("id", "kind")}
            check = factory(rid, params)
            self._rules.append((rid, check, severity))

    def run(self, ctx: RuleContext) -> list[RuleResult]:
        out: list[RuleResult] = []
        for rid, check, sev in self._rules:
            raw = check.check(ctx)          # run ONCE: checks may be expensive
            out.append(RuleResult(rule_id=rid, severity=sev,
                                  violations=tuple(raw.violations),
                                  notes=tuple(raw.notes),
                                  metrics=dict(getattr(raw, "metrics", None) or ())))
        return out
