"""`dup_symbol` — one name doing the same job in two groups is a copy, and
copies drift (the frozen check_dup_symbol doctrine, generalized).

A duplicate = same symbol NAME at module level, in two DIFFERENT groups, with
STRUCTURALLY EQUIVALENT bodies: `ast.dump` after stripping docstrings and
NORMALIZING identifiers. Verbatim copies (`sha` equal) are caught without
re-parsing; renamed-variable copies need the structural key.

BASELINE GROWS ONLY (two-way red)
    `baseline:` lists known pairs `"name@groupA|groupB"`. A pair outside the
    baseline is red; a baseline pair no longer detected is red TOO — the drift
    either got fixed (remove the line, in that commit) or changed shape
    (re-measure). Silence in both directions is how the 17 drifted copies in
    the old batch went unnoticed.

WHAT IT CANNOT SEE
    * Only MODULE-LEVEL symbols (functions/classes/constants); methods inside
      classes are not compared across groups.
    * Structural equivalence ignores names — two bodies that differ only in
      identifier spelling match. Two bodies that differ by ONE literal do not,
      and that is the point: drifted copies must not validate each other.
    * Imports of the SAME third-party name in many groups are symbols too;
      scope this rule with `groups:` or `only_names:` or it will happily
      report every `logger = logging.getLogger(__name__)`.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from typing import Any

from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind)


def _normalized_dump(node: ast.AST) -> str:
    """ast.dump with identifier names blanked and docstrings stripped."""
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (n.body and isinstance(n.body[0], ast.Expr)
                    and isinstance(n.body[0].value, ast.Constant)
                    and isinstance(n.body[0].value.value, str)):
                n.body = n.body[1:] or [ast.Pass()]
        if isinstance(n, ast.Name):
            n.id = "_"
        if isinstance(n, ast.arg):
            n.arg = "_"
        if isinstance(n, ast.Attribute):
            n.attr = "_"
    return ast.dump(node, include_attributes=False)


class DupSymbolCheck:
    CAUSES = ("dup-new", "baseline-gone")

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        self.groups: set[str] | None = set(params["groups"]) if params.get("groups") else None
        self.only_names: set[str] | None = set(params["only_names"]) if params.get("only_names") else None
        self.baseline: set[str] = set(params.get("baseline") or [])

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.idx is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a snapshot index")
        by_name: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        # name -> [(group, module, path)]
        for unit, mods in sorted(ctx.idx["units"].items()):
            for m in mods:
                grp = m.get("dist")
                if not grp:
                    continue
                if self.groups and grp not in self.groups:
                    continue
                for s in m["symbols"]:
                    if s["kind"] not in ("func", "class", "const"):
                        continue
                    if self.only_names and s["name"] not in self.only_names:
                        continue
                    by_name[s["name"]].append((grp, m["module"], str(m.get("path") or "")))
        violations: list[Violation] = []
        found: set[str] = set()
        for name, sites in sorted(by_name.items()):
            if len({g for g, _, _ in sites}) < 2:
                continue
            # structural key per site, then cross-group equal keys = duplicates
            keyed: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for grp, module, path in sites:
                try:
                    tree = ast.parse((ctx.repo / path).read_text(
                        encoding="utf-8", errors="replace"))
                except (OSError, SyntaxError):
                    continue
                key = None
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)) and node.name == name:
                        key = _normalized_dump(node)
                        break
                    if isinstance(node, ast.Assign):
                        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                        if name in targets:
                            key = _normalized_dump(node)
                            break
                if key:
                    keyed[key].append((grp, module))
            for key, owners in keyed.items():
                grps = sorted({g for g, _ in owners})
                if len(grps) < 2:
                    continue
                pair = f"{name}@" + "|".join(grps)
                found.add(pair)
                if pair not in self.baseline:
                    violations.append(Violation(
                        self.rule_id, "dup-new",
                        f"{name} structurally identical in {grps}: "
                        + ", ".join(m for _, m in sorted(owners)),
                        {"name": name, "groups": grps}))
        for gone in sorted(self.baseline - found):
            violations.append(Violation(
                self.rule_id, "baseline-gone",
                f"baseline pair no longer detected: {gone} — fixed (drop the "
                "line in that commit) or reshaped (re-measure); never silent",
                {"pair": gone}))
        return RuleResult(self.rule_id, "error", tuple(violations))


register_kind("dup_symbol")(DupSymbolCheck)
