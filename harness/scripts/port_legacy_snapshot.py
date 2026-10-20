"""port_legacy_snapshot.py — the port domain's legacy (pre-`schema_version`)
snapshot format, workspace payload, and descriptor codec, as one FLAT module
under `harness/scripts/` (matches the `port_*.py` glob, coverage group
`port-port`).

Ported (algorithm-faithful) from `ob-tools@60075cb`,
`packages/portkit/src/portkit/legacy.py` — `read_tier` (:46),
`WorkspacePayloadProvider` (:60), `LegacyPortingFormat` (:73),
`PortingLegacyDescriptorCodec` (:200), `register_all` (:252). Cross-checked
against `packages/codemap/tests/parity/porting_legacy.py` (:176, :225) — the
module the upstream kernel's OWN test fixture (`legacy_registered`) actually
registers, which is the module the two ported tests in
`harness/tests/codegraph/test_descriptor.py` assert against (the error KIND
and the offending key/field name, never the literal sentence — see below).

DEVIATIONS FROM THE OLD TOOL'S OWN BYTES, BY DESIGN:
  * `unit_yaml`'s generated-file header used to cite the OLD tool's own
    generator paths (`tools/porting/gen_map.py`, `assign.py`) — neither
    exists in this tree, and citing them was false provenance: a human (or
    a future reader of this repo's history) tracing "what wrote this file"
    would land on a path that has never existed here. The header now names
    the REAL producer, `hs-code port snapshot`, in English. Everything else
    in `unit_yaml`/`index_json` follows the old tool's layout unchanged
    (field names, ordering, ALL of it snapshot FILE BYTES rather than
    narrative prose this repo authors) — EXCEPT the two other in-body
    comments the old tool wrote in Vietnamese (the `tier`-absent note, the
    `dup_module_assign` note): those are English now too, same reason as
    `DescriptorError` below — a comment emitted into a generated file is
    still a string literal this module writes, not upstream's own bytes
    once translated, and this tree's standard refuses hard-coded Vietnamese
    prose outside a message table wherever it appears.
  * `DescriptorError` messages in `PortingLegacyDescriptorCodec.to_descriptor`
    are English, not the upstream parity module's Vietnamese — hard-coded
    Vietnamese error prose outside a message table is refused by this
    tree's own standard. The two ported tests assert the error KIND (e.g.
    "dead key") and the offending key/field name, never the literal
    sentence, so they hold under either language.

NOT PORTED: `BatchContext`, `find_batch`, `find_tool_root_from` — this
tree's own `.port/` batch layout (`port_batch_reader.py`) replaced them.

`register_legacy_codec`/`legacy_codecs`/`LegacyDescriptorCodec` are NOT
ported here either — they live in the kernel seam this module registers
against (`codegraph._core.descriptor.store`), re-opened together with this
module's one real registrant, `register_all()` below.

THE ONE REAL CALLER: `codegraph_port_snapshot_verbs.SNAPSHOT` calls
`register_all()` before it loads a source descriptor, so a pre-
`schema_version` source (an old porting `index.yaml`-shaped tree) is
snapshotted with THIS format instead of refusing outright.

`register_all()` is called EXPLICITLY, never as an import side effect: a
module that registers global state on import makes every other test in the
process depend on whether this one was imported — the kernel's own test
(`test_missing_schema_version_rejected`) asserts that an unclaimed legacy
document is rejected when no codec is registered, and an import-time
registration would silently break it for the rest of the process. A test
that DOES need the codec registered must also restore the registries
afterward (`monkeypatch.setattr` on the two module-level lists this module
and the kernel seam hold) — this module itself carries no such fixture
(that belongs to the test file, not the production module).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codegraph._core.descriptor import store as _store
from codegraph._core.descriptor.model import ProjectDescriptor, Root, ScanSpec, check_roots
from codegraph._core.descriptor.store import DescriptorError, check_python
from codegraph._core.snapshot.format import register_format, yaml_str


# ── legacy snapshot format (byte-exact with the frozen upstream writer) ─────

def read_tier(pyproject: Path) -> "str | None":
    """A dist's `tier` read from `pyproject.toml`. Never guessed from a name.

    Regex, not a TOML parser: the field sits at one fixed spot, a wrong regex
    returns `None` (visible as an empty `tier` column), while a parser
    choking on one odd file would fail the whole snapshot pass.
    """
    if not pyproject.is_file():
        return None
    m = re.search(r'^\s*tier\s*=\s*["\']([^"\']+)["\']',
                  pyproject.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


class WorkspacePayloadProvider:
    """dist/tier for workspace roots: dist IS the subdirectory name (a fact
    read off the path), tier comes from that subdirectory's own pyproject."""

    name = "porting-workspace"

    def source_payload(self, repo: Path, root: Root, unit_name: str) -> "dict[str, Any]":
        if root.kind != "workspace":
            return {}
        return {"dist": unit_name,
                "tier": read_tier(root.dir(repo) / unit_name / "pyproject.toml")}


class LegacyPortingFormat:
    """Port of the frozen writer in the old tool's `core/snapshot.py` (the
    block from the per-unit YAML header to `_index.json`) — NOT byte-exact:
    the generated-file header names the real producer (`hs-code port
    snapshot`) instead of the old tool's own generator paths, which do not
    exist in this tree (see the module docstring's DEVIATIONS section).

    Field names, ordering, and every OTHER emitted string are the old
    tool's own layout, unchanged — EXCEPT the header (above) and the two
    other in-body comments this module used to write in Vietnamese (the
    `tier`-absent note, the `dup_module_assign` note), now English (see the
    module docstring's DEVIATIONS section) — a comment this module writes
    into the generated file is still this module's own string literal, not
    upstream's byte-exact output, once translated. Everything a human reads
    on stderr (`DescriptorError`, below) is English too.
    """

    name = "porting-legacy"

    def unit_yaml(self, ctx: "dict[str, Any]") -> str:
        unit = ctx["unit"]
        mods: "list[dict]" = sorted(ctx["mods"], key=lambda m: m["path"])
        commit: str = ctx["commit"]
        payload: dict = ctx["unit_payload"] or {}

        def dist_of(m: dict) -> "str | None":
            return (m.get("payload") or {}).get("dist")

        L: "list[str]" = []
        L.append(f"# Inventory of unit `{unit}` at {commit[:8]}, generated by "
                 f"`hs-code port snapshot` — do not hand-edit.")
        L.append("# `dist` blank: this symbol's dist has not been decided yet. No `dst`."
                 if not mods[0].get("dist") and not dist_of(mods[0]) else
                 "# `dist` READ from the path (workspace root) — this snapshot does not "
                 "overwrite it, it RECONCILES assignment rules against where the symbol "
                 "actually lives.")
        L.append("unit: " + yaml_str(unit))
        L.append("root: " + yaml_str(mods[0]["root"]))
        L.append("frozen_at: " + commit)
        L.append(f"modules: {len(mods)}")
        L.append("symbols: %d" % sum(
            len(m["symbols"]) + sum(len(s.get("methods", [])) for s in m["symbols"])
            for m in mods))
        known = dist_of(mods[0])
        tier = payload.get("tier")
        if known:
            L.append(f"dist: {yaml_str(known)}")
            L.append(f"tier: {yaml_str(tier or '')}"
                     + ("" if tier else "   # pyproject.toml declares no `tier`"))
        else:
            L.append("dist: null")
        L.append("files:")
        for m in mods:
            mdist = dist_of(m)
            L.append(f"  - path: {yaml_str(m['path'])}")
            L.append(f"    module: {m['module']}")
            L.append(f"    loc: {m['loc']}")
            L.append(f"    bytes: {m['bytes']}")
            L.append(f"    file_sha: {m['file_sha']}")
            if m["dup_module_assign"]:
                L.append(f"    dup_module_assign: [{', '.join(m['dup_module_assign'])}]"
                         "   # name assigned several times at module level; the map "
                         "keeps the FIRST")
            for key in ("imports_module_level", "imports_function_local", "imports_external"):
                v = m[key]
                L.append(f"    {key}: [{', '.join(v)}]" if v else f"    {key}: []")
            L.append(f"    dist: {yaml_str(mdist)}" if mdist else "    dist: null")
            if not m["symbols"]:
                L.append("    symbols: []")
                continue
            L.append("    symbols:")
            for s in m["symbols"]:
                L.append(f"      - name: {yaml_str(s['name'])}")
                L.append(f"        kind: {s['kind']}")
                L.append(f"        lines: [{s['lines'][0]}, {s['lines'][1]}]")
                L.append(f"        sha: {s['sha']}")
                L.append(f"        dist: {yaml_str(mdist)}" if mdist else "        dist: null")
                if s.get("refs"):
                    L.append(f"        refs: [{', '.join(s['refs'])}]")
                if s.get("methods"):
                    L.append("        methods:")
                    for mm in s["methods"]:
                        L.append(f"          - name: {yaml_str(mm['name'])}")
                        L.append(f"            lines: [{mm['lines'][0]}, {mm['lines'][1]}]")
                        L.append(f"            sha: {mm['sha']}")
                        L.append(f"            dist: {yaml_str(mdist)}" if mdist
                                else "            dist: null")
                        if mm.get("refs"):
                            L.append(f"            refs: [{', '.join(mm['refs'])}]")
        return "\n".join(L) + "\n"

    def index_json(self, ctx: "dict[str, Any]") -> str:
        import json

        def norm_mod(m: dict) -> dict:
            mdist = (m.get("payload") or {}).get("dist")
            out = {k: v for k, v in m.items() if k != "payload"}
            out["dist"] = mdist
            syms = []
            for s in m["symbols"]:
                s2 = dict(s)
                s2["dist"] = mdist
                if s.get("kind") == "class":
                    # the frozen tool always writes `methods` on a class — even []
                    s2["methods"] = [{**mm, "dist": mdist} for mm in s.get("methods", [])]
                syms.append(s2)
            out["symbols"] = syms
            return out

        mod_payload = ctx["mod_payload"]
        unit_payloads = ctx["unit_payloads"]
        index = {
            "callers": ctx["callers"],
            "snapshot": ctx["snapshot"],
            "mode": ctx["mode"],
            "commit": ctx["commit"],
            "package": ctx["package"],
            "roots": [{"id": r.id, "path": r.path, "kind": r.kind} for r in ctx["roots"]],
            "roots_declared": ctx["roots_declared"],
            "mod_dist": dict(sorted((m, p.get("dist")) for m, p in mod_payload.items()
                                    if p.get("dist"))),
            "tiers": {u: p.get("tier") for u, p in sorted(unit_payloads.items())
                      if p.get("tier")},
            "totals": ctx["totals"],
            "units": {u: [norm_mod(m) for m in mods]
                      for u, mods in sorted(ctx["units"].items())},
        }
        return json.dumps(index, ensure_ascii=False, sort_keys=True, indent=1)


#: The porting batch's legacy key set — port of upstream's `core/index.py`
#: VALID_KEYS.
LEGACY_VALID_KEYS: "frozenset[str]" = frozenset({
    "repo", "package", "namespace_root", "frozen_commit",
    "cluster", "owner", "thresholds", "conn_doors", "conn_door_files",
    "python", "layout", "scan", "lib_labels", "variant", "flows",
    "tiers", "dists", "slices", "root", "snapshots",
    "branch", "frozen_at", "workspace", "requires_python", "measured",
})


class PortingLegacyDescriptorCodec:
    """Claims the old porting `index.yaml` (no `schema_version`) and maps
    the GENERIC subset (layout/scan/thresholds/python/package) into a v1
    kernel descriptor. Everything else stays passthrough for the porting
    domain."""

    name = "porting-legacy"

    def claims(self, doc: "dict[str, Any]") -> bool:
        layout = doc.get("layout")
        return (isinstance(layout, dict) and isinstance(layout.get("roots"), list)
                and bool(doc.get("package")))

    def claimed_keys(self) -> "frozenset[str]":
        return LEGACY_VALID_KEYS

    def to_descriptor(self, doc: "dict[str, Any]", path: Path) -> ProjectDescriptor:
        check_python(doc.get("python"), path)
        lay = doc.get("layout") or {}
        if "kind" in lay or "workspace_dir" in lay:
            raise DescriptorError(
                f"{path}: `layout.kind` / `layout.workspace_dir` are dead keys.\n"
                f"  Use `layout.roots:` instead — one root per line, `path` relative "
                f"to the repo root.")
        raw = lay.get("roots") or []
        if not raw:
            raise DescriptorError(f"{path}: missing `layout.roots:` — nowhere to scan from.")
        roots: "list[Root]" = []
        for i, r in enumerate(raw, 1):
            if not isinstance(r, dict):
                raise DescriptorError(f"{path}: `layout.roots[{i}]` is not a mapping")
            miss = [k for k in ("id", "path", "kind") if not r.get(k)]
            if miss:
                raise DescriptorError(f"{path}: `layout.roots[{i}]` is missing {', '.join(miss)}")
            roots.append(Root(id=str(r["id"]), path=str(r["path"]).strip("/"), kind=str(r["kind"])))
        check_roots(roots)
        scan_doc = doc.get("scan") or {}
        return ProjectDescriptor(
            schema_version=1,
            project=str(doc.get("package") or "porting"),
            roots=tuple(roots),
            scan=ScanSpec(include=tuple(scan_doc.get("include") or ("*.py",)),
                          exclude=tuple(scan_doc.get("exclude") or ())),
            thresholds=dict(doc.get("thresholds") or {}),
            domains={},
            snapshot_format=self.name,
            path=path,
        )


_registered = False


def register_all() -> None:
    """Register the legacy format and descriptor codec (idempotent).

    Called EXPLICITLY, never as an import side effect — see this module's
    own docstring.
    """
    global _registered
    if _registered:
        return
    register_format(LegacyPortingFormat())
    _store.register_legacy_codec(PortingLegacyDescriptorCodec())
    _registered = True
