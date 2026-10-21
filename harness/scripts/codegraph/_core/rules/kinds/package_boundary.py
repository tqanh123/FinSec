"""`package_boundary` — an import into another workspace package must be one the
importer's package.json declares, through the package's public `exports`.

Reads the record's `import_flags` (`lang.typescript.boundary`): every cross-package
edge carries one, declared or not, and the edge itself stays in the graph. A flag is a
violation by its cause:

    undeclared            the importer's package lists the target package nowhere
                          (`declared: undeclared`; `types_only` -- an `@types/` package for
                          a type-only import -- is declared)
    bypasses_exports      a tsconfig `paths` hit into a subpath the package's `exports`
                          would refuse
    external_undeclared   a package outside the workspace no package.json lists (off
                          unless named in `flags`)

Params: `flags` (default `[undeclared, bypasses_exports]`), `include_type_only` (default
true: an `import type` is coupling too), `allow: [{from, to, why}]` -- path globs on the
importer and on the flag's `to` (a package name for an external one), `why` required: an
allowed edge is a NOTE, never silent.

WHAT IT CANNOT SEE
    * What `boundary` cannot: a relative import that climbs into another package.
    * An import no resolver placed (`imports_unresolved`): no edge, no flag.
"""
from __future__ import annotations

from typing import Any

from ...descriptor.model import path_matches
from ..base import RuleContext, RuleResult, RulesError, Violation, register_kind

FLAGS = ("undeclared", "bypasses_exports", "external_undeclared")


def _causes(f: "dict[str, Any]") -> "list[str]":
    out = []
    if f.get("declared") == "undeclared":
        out.append("external_undeclared" if f.get("external") else "undeclared")
    if f.get("bypasses_exports"):
        out.append("bypasses_exports")
    return out


class PackageBoundaryCheck:
    CAUSES = FLAGS

    def __init__(self, rule_id: str, params: "dict[str, Any]") -> None:
        self.rule_id = rule_id
        flags = params.get("flags", ["undeclared", "bypasses_exports"])
        if not isinstance(flags, list) or not flags or any(f not in FLAGS for f in flags):
            raise RulesError(f"rule {rule_id!r}: `flags` must be a non-empty list of {', '.join(FLAGS)}")
        self.flags = frozenset(flags)
        self.type_only = params.get("include_type_only", True)
        if not isinstance(self.type_only, bool):
            raise RulesError(f"rule {rule_id!r}: `include_type_only` must be true or false")
        allow = params.get("allow", [])
        if not isinstance(allow, list) or any(
                not isinstance(a, dict) or set(a) != {"from", "to", "why"}
                or not all(isinstance(a[k], str) and a[k].strip() for k in a) for a in allow):
            raise RulesError(f"rule {rule_id!r}: `allow` entries are {{from, to, why}}, all non-empty "
                             f"-- an exception without its reason is a hidden one")
        self.allow = tuple(allow)

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.idx is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a snapshot index in the context")
        violations: "list[Violation]" = []
        notes: "list[str]" = []
        seen = 0
        for mods in ctx.idx.get("units", {}).values():
            for m in mods:
                for f in m.get("import_flags") or ():
                    seen += 1
                    if f.get("type_only") and not self.type_only:
                        continue
                    causes = [c for c in _causes(f) if c in self.flags]
                    if not causes:
                        continue
                    label = f"{m['path']} -> {f['to']}"
                    ok = next((a for a in self.allow
                               if path_matches(m["path"], a["from"]) and path_matches(str(f["to"]), a["to"])), None)
                    if ok is not None:
                        notes.append(f"allowed: {label} ({', '.join(causes)}): {ok['why']}")
                        continue
                    for c in causes:
                        violations.append(Violation(self.rule_id, c, label, {
                            "from": m["path"], "to": f["to"], "target_pkg": f.get("target_pkg"),
                            "declared": f.get("declared"), "runtime_resolvable": f.get("runtime_resolvable"),
                            "type_only": bool(f.get("type_only"))}))
        notes.insert(0, f"cross-package edges flagged: {seen}")
        return RuleResult(self.rule_id, "error", tuple(violations), tuple(notes), metrics={"flags": seen})


register_kind("package_boundary", languages=("typescript",))(PackageBoundaryCheck)
