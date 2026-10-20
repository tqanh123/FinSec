"""hs_run_facts.code_importers — the `code.importers` fact provider.

`provide(ctx) -> {value, basis, warning, [not_in_snapshot]}`. `value` =
`{subject_path: [importer_path, ...]}`, DIRECT (one-hop) importers only —
`codegraph_change_reader._reverse_import_edges`'s own edge, never the
transitive walk `code.tests_reached` uses. A subject ABSENT from the
snapshot is never folded into `value` as an empty list (which would read as
"measured, zero importers") — it is named in `not_in_snapshot` instead.

BLIND SPOT, on every fill: `_reverse_import_edges` is an EXACT dotted-name
import-graph edge. A module invoked only through subprocess, a prose
reference, or runtime dispatch (getattr/registry/string table) carries no
import edge and will never appear as an importer here — `no-orphan-code.md`
names the same caveat for reachability generally. `basis` states this on
every call, and `warning` carries the identical disclosure for the
envelope's top-level `warnings` — unconditional on a successful fill, not
only when something was actually excluded, so a caller who checks only the
envelope's `warnings` (never opening each fact's own `basis`) still sees
the disclosure.
"""
from __future__ import annotations

import codegraph_change_reader

_BASIS = (
    "one-hop reverse import edges only (`codegraph_change_reader."
    "_reverse_import_edges`) -- a module with zero importers here may "
    "still be invoked via subprocess, prose, or dynamic dispatch (see "
    "no-orphan-code.md); zero importers is not proof nothing uses it"
)

_WARNING = (
    "code.importers only counts direct (one-hop) import edges -- a module "
    "reached only via subprocess/prose/dynamic dispatch will not appear "
    "here, so an empty importer list is not proof nothing uses it"
)


def provide(ctx: dict) -> dict:
    idx = ctx["snapshot"]
    subjects = sorted(set(ctx.get("subjects") or ()))

    by_path = codegraph_change_reader._module_by_path(idx)
    reverse = codegraph_change_reader._reverse_import_edges(idx)
    paths_by_module: "dict[str, list[str]]" = {}
    for m in codegraph_change_reader._modules(idx):
        paths_by_module.setdefault(m["module"], []).append(m["path"])

    value: "dict[str, list[str]]" = {}
    not_in_snapshot = []
    for p in subjects:
        m = by_path.get(p)
        if m is None:
            not_in_snapshot.append(p)
            continue
        importer_mods = reverse.get(m["module"], set())
        importer_paths = sorted(
            path for mod in importer_mods for path in paths_by_module.get(mod, ()))
        value[p] = importer_paths

    out = {"value": value, "basis": _BASIS, "warning": _WARNING}
    if not_in_snapshot:
        out["not_in_snapshot"] = not_in_snapshot
    return out
