"""`single_door` — exactly one door for a capability; every other caller is red.

Port of the frozen check_conn_door doctrine, generalized: which call pattern
is THE door, which files may hold it, is all rule data.

BASELINE GROWS ONLY (two-way red, the paid-for trap)
    With `baseline:` (list of labels): a NEW bypass the baseline does not carry
    is red, AND a baseline entry whose violation vanished is red TOO — a
    baseline shrinking while nobody fixed anything is a call site that learned
    to hide (getattr, indirection), not a win. Removing a baseline line is a
    human act with a reason, done in the same commit as the fix.

WHAT IT CANNOT SEE
    * Calls through variables (`h = psycopg.connect; h(...)`) — the dotted
      name must appear at the call site for AST to see it.
    * getattr/dispatch-dict acquisition is invisible; that is exactly the
      hole the two-way baseline exists to smell.
    * It judges CALL SITES, not connection lifetime (pool vs single) —
      whether a bypass is functionally equivalent is not this rule's question.
"""
from __future__ import annotations

import ast
from typing import Any, Iterator

from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)


def _dotted_call(node: ast.Call) -> str | None:
    f = node.func
    parts: list[str] = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
        return ".".join(reversed(parts))
    return None


def _iter_calls(path) -> Iterator[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted_call(node)
            if name:
                yield node.lineno, name


class SingleDoorCheck:
    CAUSES = ("door-bypass", "baseline-new", "baseline-gone")

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        import fnmatch
        self._fn = fnmatch
        self.bypass_patterns: list[str] = list(params["bypass_patterns"])
        self.door_patterns: list[str] = list(params.get("door_patterns") or [])
        self.allowed_files: list[str] = list(params.get("allowed_files") or [])
        self.baseline: set[str] = set(params.get("baseline") or [])

    def _match_any(self, name: str, patterns: list[str]) -> bool:
        return any(self._fn.fnmatch(name, p) for p in patterns)

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.idx is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a snapshot index")
        violations: list[Violation] = []
        labels: set[str] = set()
        for unit, mods in sorted(ctx.idx["units"].items()):
            for m in mods:
                rel = str(m.get("path") or "")
                if not rel or any(self._fn.fnmatch(rel, a) for a in self.allowed_files):
                    continue
                f = ctx.repo / rel
                for lineno, name in _iter_calls(f):
                    if self._match_any(name, self.bypass_patterns):
                        label = f"{m['module']}:{lineno}  {name}"
                        labels.add(label)
                        if label not in self.baseline:
                            violations.append(Violation(
                                self.rule_id, "door-bypass", label,
                                {"module": m["module"], "line": lineno, "call": name}))
        if self.baseline:
            for gone in sorted(self.baseline - labels):
                violations.append(Violation(
                    self.rule_id, "baseline-gone",
                    f"baseline entry no longer fires: {gone} — a bypass that "
                    "vanished without a fix learned to hide; update the "
                    "baseline in the same commit as the real change",
                    {"label": gone}))
        notes = (f"door patterns: {self.door_patterns or '(none declared)'}",)
        return RuleResult(self.rule_id, "error", tuple(violations), notes)


register_kind("single_door", languages=("python",))(
    require_params("bypass_patterns")(SingleDoorCheck))
