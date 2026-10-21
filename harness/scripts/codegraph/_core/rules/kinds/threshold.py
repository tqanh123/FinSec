"""`threshold` — size ceilings with a mandatory verdict above the warn line.

Port of the frozen check_file_size doctrine, generalized to any metric the
snapshot carries per module (`metric` param; `loc` today):

    >= warn    -> RED unless the file carries a verdict matching
                  `verdict_above_warn` (regex list; e.g. "SPLIT" / "MEASURED,
                  NOT SPLIT")
    >= ceiling -> RED unless the verdict matches `verdict_above_ceiling`
                  (default: the FIRST pattern of the warn list — a clever
                  argument defends any size, so above the ceiling the
                  argument is not allowed to win; only "SPLIT" is).

NOT THE LENGTH LAW
    `loc` and `bytes` count comments and docstrings, so the best-documented file hits
    the line first and deleting comments turns it green; a verdict here is a regex over
    the file, so a leftover one still defends. File and function length is `file_size`
    (real code lines, def/nesting thresholds, a verdict table keyed by path). This kind
    stays for any other per-module metric; on a length metric its result says so.

WHY A VERDICT AND NOT JUST A NUMBER
    A threshold only forces a MEASUREMENT; the verdict is the answer the
    measurement demands. The ledger-coupled half of the old gate ("the split
    must then HAPPEN") is a porting-domain concern, layered on top.

WHAT IT CANNOT SEE
    * `loc` counts newlines as the adapter recorded them for the module —
      whatever a comment/blank-stripping the adapter itself does or does not
      do, this rule inherits verbatim.
    * Verdicts are matched as REGEX over the file text (comments included):
      a verdict in a DIFFERENT file does not defend this one, but a stale
      verdict left behind after a refactor still defends it — this rule
      cannot tell a fresh verdict from a leftover.
"""
from __future__ import annotations

from typing import Any

from ... import regex_bound
from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)


#: per-module snapshot metrics that measure length — `file_size` is the law for those.
_LENGTH_METRICS = ("loc", "bytes")


def _file_text(ctx: RuleContext, path: str) -> str:
    f = ctx.repo / path
    try:
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise RulesError(f"cannot read {path}: {e}") from e


class ThresholdCheck:
    CAUSES = ("over-warn-without-verdict", "over-ceiling")

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        self.metric: str = params.get("metric", "loc")
        self.warn: int = int(params["warn"])
        self.ceiling: int | None = (
            int(params["ceiling"]) if params.get("ceiling") is not None else None)
        pats = params.get("verdict_above_warn") or []
        if not pats:
            raise RulesError(
                f"rule {rule_id!r}: `verdict_above_warn` must list at least one "
                "regex — a threshold nobody can satisfy by writing a verdict "
                "is a wall, not a measurement")
        self.warn_pats = [regex_bound.compile_bounded(p) for p in pats]
        if self.ceiling is not None and not params.get("verdict_above_ceiling"):
            raise RulesError(
                f"rule {rule_id!r}: `ceiling` set without `verdict_above_ceiling` "
                "— reusing a warn pattern silently would let a soft verdict "
                "(e.g. 'NOT SPLIT' matching the substring SPLIT) defend the "
                "ceiling, and the ceiling exists precisely so no argument but "
                "a split wins")
        self.ceil_pats = [regex_bound.compile_bounded(p)
                          for p in (params.get("verdict_above_ceiling") or [])]

    def _any_verdict_matches(self, pats: list, text: str,
                             budget: "regex_bound.ScanBudget") -> bool:
        """`any(p.search(text) for p in pats)`, bounded on both axes
        (`regex_bound`): these patterns come straight from the TARGET repo's
        own codemap.yaml — someone else's config, matched against someone
        else's file text — so an unbounded `re.search` here is exactly the
        ReDoS shape `where` closes for the CLI-supplied pattern; refuses with
        a `RulesError` (this module's own honest-cannot-run) rather than
        hanging the whole `graph rules` pass."""
        for p in pats:
            try:
                if regex_bound.bounded_search(p, text, budget):
                    return True
            except regex_bound.PatternBudgetExceeded as e:
                raise RulesError(f"rule {self.rule_id!r}: {e}") from None
            except TimeoutError:
                raise RulesError(
                    f"rule {self.rule_id!r}: a verdict pattern ran past "
                    f"{regex_bound.PATTERN_TIMEOUT_SECONDS:.0f}s — simplify it"
                ) from None
        return False

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.idx is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a snapshot index")
        violations: list[Violation] = []
        budget = regex_bound.ScanBudget()
        for unit, mods in sorted(ctx.idx["units"].items()):
            for m in mods:
                value = m.get(self.metric)
                if value is None:
                    continue
                if self.warn is not None and value >= self.warn:
                    rel = str(m.get("path") or m["module"])
                    text = _file_text(ctx, rel)
                    if not self._any_verdict_matches(self.warn_pats, text, budget):
                        violations.append(Violation(
                            self.rule_id, "over-warn-without-verdict",
                            f"{m['module']}: {value} {self.metric} >= warn "
                            f"{self.warn} and no verdict found",
                            {"module": m["module"], "metric": self.metric,
                             "value": value, "line": self.warn}))
                    if (self.ceiling is not None and value >= self.ceiling
                            and not self._any_verdict_matches(self.ceil_pats, text, budget)):
                        violations.append(Violation(
                            self.rule_id, "over-ceiling",
                            f"{m['module']}: {value} {self.metric} >= ceiling "
                            f"{self.ceiling} — only a split verdict defends this",
                            {"module": m["module"], "metric": self.metric,
                             "value": value, "line": self.ceiling}))
        notes: tuple[str, ...] = ()
        if self.metric in _LENGTH_METRICS:
            notes = (f"`{self.metric}` counts documentation too — the length law is kind "
                     f"`file_size` (code lines, verdict table)",)
        return RuleResult(self.rule_id, "error", tuple(violations), notes)


register_kind("threshold")(
    require_params("warn")(ThresholdCheck))
