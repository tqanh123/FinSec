"""What one TypeScript/JavaScript import says about package boundaries -- the record's
`import_flags`.

An import into ANOTHER workspace package is an edge like any other (it is in
`imports_*`); beside it the record says how the importer's package stands to it:

    to                  the edge target (`<path>::<name>`, `<path>`), or the package name
                        of an external import
    target_pkg          the package it lands in
    declared            own:<field> | ancestor:<pkg>:<field> | types_only | undeclared --
                        the nearest package.json listing it (`resolve_static.declared`)
    runtime_resolvable  yes | no | unknown -- would a package manager link it
    bypasses_exports    a tsconfig `paths` hit the package's `exports` would refuse
    inferred            reached through a built output mapped back to its source
    type_only           the import is erased at runtime
    external            (external imports) the package is outside the workspace

A flag is written for every cross-package edge, declared or not; an external import
only when undeclared. Reading the flags is a rule's (`package_boundary`), not this
module's: nothing here filters an edge.

WHAT IT CANNOT SEE
    * A RELATIVE import that climbs out of its package into another one
      (`../../shell/src/x`): it names no package, so no package.json is asked.
"""
from __future__ import annotations

from typing import Any


def flags_for(res: Any, edges: "list[tuple[str, bool]]", type_only: bool) -> "list[dict[str, Any]]":
    """The flags of one resolved import (`resolve_static.ResolveResult`) whose edges are
    `edges` ((target, type_only) per binding, as the record files them)."""
    if res.status == "external":
        if res.declared != "undeclared":
            return []
        return [{"to": res.target, "target_pkg": res.target_pkg or res.target, "external": True,
                 "declared": "undeclared", "runtime_resolvable": res.runtime_resolvable or "no",
                 "type_only": type_only}]
    if res.status != "internal" or res.declared in ("n/a", "self"):
        return []
    base = {"target_pkg": res.target_pkg, "declared": res.declared,
            "runtime_resolvable": res.runtime_resolvable or "unknown",
            "bypasses_exports": "bypasses_exports" in res.notes, "inferred": "out_to_src" in res.via}
    return [{"to": t, **base, "type_only": type_only or t_only} for t, t_only in edges]
