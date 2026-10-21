"""Snapshot formats — how an in-memory snapshot becomes bytes on disk.

The kernel owns ONE format (v2, English headers, `schema_version` stamped).
Any other byte format is a REGISTERED format implementing the same
protocol — the seam that makes byte parity possible without the kernel
knowing what a domain-specific payload field means: domain fields ride
through `payload` and are rendered verbatim by the format that understands
them.

WHAT IT CANNOT SEE (applies to every format, inherited by readers):
    * A format renders what the pipeline measured — every measurement blind
      spot (see `codegraph._core.adapter`) lands in the bytes unchanged.
    * `yaml_str` quotes only when needed; a string that HAPPENS to look like a
      plain scalar is written unquoted. Round-tripping through a YAML parser
      is equivalent, but byte-diffing against hand-written YAML is not.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from ..descriptor.model import KERNEL_SCHEMA_VERSION


#: A scalar made only of these, not starting with a digit, and not one of the
#: words a loader resolves to another type, is safe to write PLAIN. Everything
#: else is quoted. This is a WHITELIST on purpose: a list of dangerous things
#: is always one unseen value away from being wrong — `::` slipped past an
#: earlier character-blacklist version into every flow sequence, and
#: `yes`/`007`/`~` would have been read back as True/7/None.
_PLAIN_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./+")

#: Plain words a YAML loader resolves to a non-string. YAML 1.1 folds the
#: on/off and yes/no pairs too, which is the same fold that bites GitHub
#: workflow files.
_RETYPED = frozenset("""
    y Y yes Yes YES n N no No NO true True TRUE false False FALSE
    on On ON off Off OFF null Null NULL ~ .nan .NaN .NAN .inf .Inf .INF
""".split())


def yaml_str(s: str) -> str:
    """Emit one scalar. Plain only when it cannot be read back as anything else.

    ONE DOOR: every value the snapshot writes goes through here, including the
    elements of a flow sequence. That is structural rather than a rule about
    characters, so a field added later is covered without editing anything —
    and test_snapshot_yaml_is_portable.py holds the door shut.
    """
    if s == "":
        return '""'
    if s in _RETYPED or s[0].isdigit() or s[0] == "-":
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if not set(s) <= _PLAIN_SAFE:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


@runtime_checkable
class SnapshotFormat(Protocol):
    """Renders one snapshot directory. `name` is the registry key a descriptor
    selects via `snapshot.format`."""

    name: str

    def unit_yaml(self, ctx: "dict[str, Any]") -> str:
        """Full text (including the trailing newline) of one unit file.

        `ctx` carries: unit, root, commit, mods (the unit's file records,
        already path-sorted), unit_payload (first-source payload, setdefault
        semantics), schema_version.
        """
        ...

    def index_json(self, ctx: "dict[str, Any]") -> str:
        """Full text of `_index.json` — deterministic bytes for the same tree.
        `ctx` additionally carries: snapshot (dir name), mode, package, roots,
        roots_declared, mod_payload (module -> payload dict), unit_payloads,
        totals, units, callers, tool (the writer's provenance stamp, or
        `None` when the caller supplied none — omitted from the bytes then,
        never written as a null), tree_hash (the measured tree's content
        identity, same omit-when-`None` rule as `tool`), content_key (the
        caller's own reuse identity, same omit-when-`None` rule), measured_at
        (wall-clock write time, ALWAYS written — the tie-break `store.
        resolve_snapshot` reads when several snapshots share one commit).
        """
        ...


class KernelFormat:
    """The kernel's own v2 format: English headers, `schema_version` stamped,
    domain payload rendered as an opaque mapping at the unit level only."""

    name = "codemap"

    def unit_yaml(self, ctx: dict[str, Any]) -> str:
        unit = ctx["unit"]
        mods: list[dict] = ctx["mods"]
        commit: str = ctx["commit"]
        payload: dict = ctx["unit_payload"] or {}
        L: list[str] = []
        L.append(f"# codemap snapshot unit `{unit}` at {commit[:8]}. "
                 f"GENERATED — do not edit by hand.")
        L.append(f"schema_version: {KERNEL_SCHEMA_VERSION}")
        L.append("unit: " + yaml_str(unit))
        L.append("root: " + yaml_str(ctx["root"]))
        L.append("frozen_at: " + commit)
        L.append(f"modules: {len(mods)}")
        L.append(f"symbols: {sum(len(m['symbols']) + sum(len(s.get('methods', [])) for s in m['symbols']) for m in mods)}")
        if payload:
            L.append("payload:")
            for k in sorted(payload):
                L.append("  " + yaml_str(k) + ": " + yaml_str(str(payload[k])))
        L.append("files:")
        for m in mods:
            L.append(f"  - path: {yaml_str(m['path'])}")
            L.append("    module: " + yaml_str(m['module']))
            L.append(f"    loc: {m['loc']}")
            L.append(f"    bytes: {m['bytes']}")
            L.append("    file_sha: " + yaml_str(m['file_sha']))
            if m.get("invoked_by"):
                L.append("    invoked_by: ["
                         + ", ".join(yaml_str(x) for x in m["invoked_by"]) + "]"
                         "   # of those, the lines that RUN it (lower bound)")
            if m.get("path_drivers"):
                L.append("    path_drivers: ["
                         + ", ".join(yaml_str(x) for x in m["path_drivers"]) + "]"
                         "   # files naming this module by path or -m, never importing it")
            if m["dup_module_assign"]:
                L.append(f"    dup_module_assign: [{', '.join(yaml_str(x) for x in m['dup_module_assign'])}]"
                         "   # names assigned multiple times at module level; first kept")
            for key in ("imports_module_level", "imports_function_local", "imports_external"):
                v = m[key]
                L.append("    " + yaml_str(key) + ": ["
                         + ", ".join(yaml_str(x) for x in v) + "]"
                         if v else "    " + yaml_str(key) + ": []")
            if m.get("imports_type_only"):
                L.append("    imports_type_only: [" + ", ".join(yaml_str(x) for x in m["imports_type_only"]) + "]"
                         "   # under `if TYPE_CHECKING:`: never loaded, still coupling")
            if not m["symbols"]:
                L.append("    symbols: []")
                continue
            L.append("    symbols:")
            for s in m["symbols"]:
                L.append(f"      - name: {yaml_str(s['name'])}")
                L.append("        kind: " + yaml_str(s['kind']))
                L.append(f"        lines: [{s['lines'][0]}, {s['lines'][1]}]")
                L.append("        sha: " + yaml_str(s['sha']))
                if s.get("refs"):
                    L.append(f"        refs: [{', '.join(yaml_str(x) for x in s['refs'])}]")
                if s.get("bases"):
                    L.append(f"        bases: [{', '.join(yaml_str(x) for x in s['bases'])}]")
                if s.get("methods"):
                    L.append("        methods:")
                    for mm in s["methods"]:
                        L.append("          - name: " + yaml_str(mm['name']))
                        L.append(f"            lines: [{mm['lines'][0]}, {mm['lines'][1]}]")
                        L.append("            sha: " + yaml_str(mm['sha']))
                        if mm.get("refs"):
                            L.append(f"            refs: [{', '.join(yaml_str(x) for x in mm['refs'])}]")
        return "\n".join(L) + "\n"

    def index_json(self, ctx: dict[str, Any]) -> str:
        index = {
            "schema_version": KERNEL_SCHEMA_VERSION,
            "callers": ctx["callers"],
            "snapshot": ctx["snapshot"],
            "mode": ctx["mode"],
            "commit": ctx["commit"],
            "package": ctx["package"],
            "roots": [{"id": r.id, "path": r.path, "kind": r.kind} for r in ctx["roots"]],
            "roots_declared": ctx["roots_declared"],
            "mod_unit": dict(sorted(ctx["mod_unit"].items())),
            "totals": ctx["totals"],
            "units": ctx["units"],
            # The path-driver stage: edges a caller spells rather than imports,
            # plus what the scan refused to read. Carried at the top level
            # because `ambiguous` and `excluded` are properties of the PASS, not
            # of any one module — and a reader who never sees them would take
            # an empty driver list for "nothing invokes this".
            "ambiguous_path_refs": ctx["ambiguous_path_refs"],
            "driver_scan": ctx["driver_scan"],
        }
        # Omitted (never a null) when the caller supplies none: a portable
        # caller outside this harness has no version/hash to stamp, and the
        # frozen-upstream byte-match fixture was captured before this key
        # existed -- both must stay silent rather than write `"tool": null`.
        # Test-tree files no root measures (`pipeline.unrooted_tests`): graph
        # nodes for a reverse walk, never a unit's code. Omitted when empty, so
        # a tree whose roots already hold every test keeps its bytes.
        if ctx.get("descriptor_sha") is not None:
            index["descriptor_sha"] = ctx["descriptor_sha"]
        if ctx.get("test_modules"):
            index["test_modules"] = ctx["test_modules"]
        if ctx.get("test_modules_unparsed"):
            index["test_modules_unparsed"] = ctx["test_modules_unparsed"]
        if ctx.get("tool") is not None:
            index["tool"] = ctx["tool"]
        # Same omit-when-absent law as `tool`, for the same reason: the
        # frozen-upstream fixture predates `tree_hash` too.
        if ctx.get("tree_hash") is not None:
            index["tree_hash"] = ctx["tree_hash"]
        # Same law again: a caller with no reuse identity to stamp (every
        # existing frozen-upstream fixture, every scratch/daily pass) writes
        # no `content_key` key at all.
        if ctx.get("content_key") is not None:
            index["content_key"] = ctx["content_key"]
        # Same omit-when-absent law, and for the SAME reason `content_key`
        # needs it: `measured_at` (wall-clock write time) is what breaks a
        # `resolve_snapshot` tie among several content-keyed siblings of one
        # commit — a caller not using content-keyed naming has no tie to
        # break, and writing a clock reading unconditionally would make
        # `test_deterministic_bytes_two_runs`'s own "two runs, byte-identical
        # output" guarantee false for every caller, not just reuse-aware ones.
        if ctx.get("measured_at") is not None:
            index["measured_at"] = ctx["measured_at"]
        return json.dumps(index, ensure_ascii=False, sort_keys=True, indent=1)


_FORMATS: dict[str, SnapshotFormat] = {}


def register_format(fmt: SnapshotFormat) -> None:
    """Register a snapshot format (domains do this at plugin load)."""
    _FORMATS[fmt.name] = fmt


def format_for(name: str | None) -> SnapshotFormat:
    """The named format, or the kernel's own. Unknown name is an ERROR — a
    snapshot written by a typo'd format name would read as a schema nobody
    chose."""
    if name is None:
        return KernelFormat()
    try:
        return _FORMATS[name]
    except KeyError:
        raise KeyError(f"snapshot format {name!r} is not registered; available: "
                       f"{', '.join(sorted(_FORMATS)) or 'only the kernel default'}") from None
