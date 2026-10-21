"""`stale_rename` — no live file names a path git recorded as renamed away.

The units are every rename on the repo's first-parent line (`codegraph.refactor.
rename_units`), so tomorrow's rename is checked tomorrow with no edit to the rule. A
file that names an old path ON PURPOSE (a fixture proving the old path is gone, a ledger
row recording the rename) says so where it stands: the `marker` and a reason, on the
same line or the line above.

    skip        globs whose old names are the record (default: the repo's own
                `domains.oldrefs.history`)
    also_under  prefixes a tree spells its paths under (`docs/`: `showcase/x` is
                `docs/showcase/x`); an old path living there is a name in use
    own_copies  `{moved_under, named_by}`: a rename `docs/X -> <moved_under>X` moved the
                repo's OWN copy of a file the product tells an installing project to
                keep; a file matching `named_by` naming `docs/X` names that project's
                file, not a dead one
    marker      default `old-path-on-purpose:`

    STALE       a live file names an old path, no marker

WHAT IT CANNOT SEE
    * Everything `rename_units` cannot: a rename inside a file's content, a rename made
      and undone inside one branch, a path built at runtime.
"""
from __future__ import annotations

import fnmatch
from typing import Any, List

from ..base import RuleContext, RuleResult, RulesError, Violation, register_kind

MARKER = "old-path-on-purpose:"


def marked(text: str, lines, marker: str = MARKER) -> bool:
    """Every one of `lines` (1-based) carries `marker`, or the line above it does."""
    rows = text.split("\n")
    return all(0 < n <= len(rows) and (marker in rows[n - 1] or (n > 1 and marker in rows[n - 2]))
               for n in lines)


class StaleRenameCheck:
    CAUSES = ("stale",)

    def __init__(self, rule_id: str, params: "dict[str, Any]") -> None:
        self.rule_id = rule_id
        for key in ("skip", "also_under"):
            if not isinstance(params.get(key, []), list):
                raise RulesError("rule %r: `%s` must be a list" % (rule_id, key))
        self.skip = params.get("skip")
        self.also_under = tuple(params.get("also_under") or ())
        own = params.get("own_copies") or {}
        if own and not (isinstance(own, dict) and isinstance(own.get("moved_under"), str)
                        and isinstance(own.get("named_by"), list)):
            raise RulesError("rule %r: `own_copies` is {moved_under: <prefix>, named_by: [globs]}"
                             % rule_id)
        self.own = own
        self.marker = str(params.get("marker") or MARKER)

    def _own_copy(self, p) -> bool:
        if not self.own or not p.old.startswith("docs/") or "/" in p.old[len("docs/"):]:
            return False
        return (p.new == self.own["moved_under"] + p.old[len("docs/"):]
                and any(fnmatch.fnmatchcase(p.path, g) for g in self.own["named_by"]))

    def stale(self, repo, *, texts=None, paths=None) -> List:
        from codegraph.refactor import oldrefs, rename_units as R
        skip = tuple(self.skip) if self.skip is not None else oldrefs.declared(repo)[0]
        units = R.rename_units(repo)
        out = []
        for p in R.stale_pairs(repo, units, skip=skip, also_under=self.also_under,
                               texts=texts, paths=paths):
            if self._own_copy(p):
                continue
            try:
                body = (texts or {}).get(p.path) or (repo / p.path).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            if not marked(body, p.lines, self.marker):
                out.append(p)
        return out

    def check(self, ctx: RuleContext) -> RuleResult:
        found = self.stale(ctx.repo)
        v = [Violation(self.rule_id, "stale", "%s:%s names %s (now %s)"
                       % (p.path, ",".join(map(str, p.lines)), p.old, p.new),
                       {"path": p.path, "lines": list(p.lines), "old": p.old, "new": p.new})
             for p in found]
        return RuleResult(self.rule_id, "error", tuple(v), (), {"stale": len(v)})


register_kind("stale_rename", languages="*")(StaleRenameCheck)
