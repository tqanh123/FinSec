"""codegraph.change.selection — which tests a change can break, read off a
measured snapshot rather than re-parsed from source.

REWRITTEN here, not ported: the reference tool's own `testkit.analysis.select`
(pinned `60075cb`, `packages/testkit/src/testkit/analysis/select.py`) pairs
with `testmap.py`'s `scan_units(src_root, pkg)`, which hard-codes a
`<src_root>/<pkg>/` layout — this repo (and a target repo this engine runs
against) is not that shape, and a snapshot already carries the import graph
this engine needs, measured once by `codegraph.graph snapshot` rather than
re-walked here. What survives the port is the OUTPUT shape obt's own tool
settled on: a selection plus an explicit BLIND SPOT (the tests NOT selected),
because a selector that prints only the selection hides exactly the fact a
reviewer needs.

FOUR CHANNELS, each independently switchable (`channels=`) so a caller (and a
test) can drop one and watch what it alone was catching:

    self        a changed file that is itself a test selects itself.
    import      reverse BFS over the snapshot's own import edges
                (`imports_module_level` + `imports_function_local`, exact
                dotted-name match — same blind spot every import edge in this
                kernel carries: an alias or a re-export is invisible) from
                every changed module to every test module reachable from it,
                however many hops away.
    whole_tree  every test the target repo's OWN gate book declares
                `whole_tree`, and every `codemap.yaml` `test_select:` row of
                `kind: whole_tree` (a path or a glob) — a change ANYWHERE can
                redden one of these, so a narrow run always adds them (a repo
                carrying neither contributes none).
    string      a test that names a changed module by PATH or `-m` — the
                snapshot's own `path_drivers` field (a citation, not proof of
                a call; see `codegraph._core.snapshot.path_drivers`'s own
                docstring for what that stage cannot see) — PLUS a `<stem>.py`
                text-mention fallback for a test `path_drivers` did not catch
                (a bare filename dropped into an f-string or a fixture ID),
                PLUS a string literal of the test naming the changed PATH, a
                directory holding it, or a glob matching it (`ROOT / "docs" /
                "decisions.yaml"`, `"docs"`) — the one sub-channel a change
                to a data file or a doc reaches a test through.

WHAT IT CANNOT SEE
    * Every blind spot `import_channel`'s own edges carry (see this module's
      `import_channel` docstring) is inherited whole — a test reaching the
      change ONLY through a conftest fixture, a subprocess call, or runtime
      dispatch (getattr/registry/string table) is invisible to every channel
      here, which is why `blind_spot` is a first-class field of `Selection`
      and not an afterthought: it is what the caller must still run through
      a broader tier, not a claim that nothing else could be affected.
    * `idx=None` (no snapshot to read — measuring one failed, or none was
      ever taken) degrades to the `self` channel alone: `import`/`string`
      need the measured import graph and per-module `path_drivers`, and
      `whole_tree` is the one channel that does NOT need a snapshot (it reads
      the target repo's gate book, not this engine's own measurement) so it
      still contributes.
    * The string channel's text-mention fallback is a literal substring
      search (`stem in text`) — a `<stem>.py` occurring in an unrelated
      identifier or a comment about a DIFFERENT file of the same name reads
      as a hit; this is the same over-selective failure mode
      `affected_tests.py`'s own stem-based import resolution already
      accepted as the safe direction (a spurious inclusion, never a
      spurious exclusion).
    * A test outside every declared root reaches every channel here only
      through `codegraph._core.snapshot_bridge` (see `_augmented_idx`); its
      own WHAT IT CANNOT SEE (weaker import classification than a real
      snapshot record, unparseable files skipped) applies on top of every
      blind spot listed here — a test that bridge never returns still reads
      as untouched, same as any other module absent from `idx`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import codegraph_change_reader
from codegraph._core.snapshot_bridge import bridge_records
from codegraph._core.testfiles import FilesLayout, build_test_layout

#: The channel names this engine knows. A `channels=` argument naming
#: anything else is a caller error (typo'd flag, stale test) — refused
#: loudly rather than silently ignored, the same law `V2_KEYS` and friends
#: apply to a descriptor's own top-level keys.
CHANNELS: "tuple[str, ...]" = ("self", "import", "whole_tree", "string")

# `_modules`/`_module_by_path`/`_base_target`/`_reverse_import_edges`/
# `import_channel` MOVED to `codegraph_change_reader` (a flat module outside
# this package, so a caller like hs-run's facts locator can walk the
# reverse-import graph without importing the whole `codegraph` measurement
# engine — see that module's own docstring). Rebound to the SAME function
# objects, not reimplemented beside them: the identical pattern `codegraph.
# _core.snapshot.worktree_key.worktree_key` already uses for its own move.
# `import_channel is codegraph_change_reader.import_channel` holds — one
# definition.
_modules = codegraph_change_reader._modules
_module_by_path = codegraph_change_reader._module_by_path
_base_target = codegraph_change_reader._base_target
_reverse_import_edges = codegraph_change_reader._reverse_import_edges
import_channel = codegraph_change_reader.import_channel


def string_channel(idx: dict, changed_paths: "Iterable[str]", root: Path,
                   layout: "FilesLayout | None" = None
                   ) -> "tuple[set[str], dict[str, int]]":
    """`codegraph_change_reader.string_channel` (its docstring names the three
    sub-channels and their stats); `layout=None` self-builds via
    `build_test_layout(root)` -- a direct caller with no layout of its own yet;
    `select_tests` passes its own through, so one call loads `codemap.yaml` once."""
    if layout is None:
        layout = build_test_layout(root)
    return codegraph_change_reader.string_channel(idx, changed_paths, root, layout)


#: Synthetic unit key bridged records are filed under — never a real root
#: id (every real root's own `id` law forbids a leading `.`; a leading `.`
#: here means this key can never collide with one, see `check_roots`).
_BRIDGE_UNIT = ".bridge"


def _augmented_idx(idx: "dict | None", root: Path, layout: "FilesLayout | None" = None
                   ) -> "dict | None":
    """`idx` with `snapshot_bridge.bridge_records(idx, root, layout)` filed
    under one extra pseudo-unit, `_BRIDGE_UNIT` — a LOCAL copy; the caller's
    own `idx` (and its `"units"` dict) is never mutated, so a caller reading
    `eng.idx.get("units")` afterward for something else (unit inventories, a
    query verb's own `all_units`) never sees the pseudo-unit leak in.

    Filing bridged records into `idx["units"]` this way, rather than a
    parallel list every channel below would need its own merge step for, is
    what lets `_modules`/`_reverse_import_edges`/`string_channel` pick them
    up with ZERO changes: each already walks `idx["units"].values()`."""
    if idx is None:
        return None
    bridged = bridge_records(idx, root, layout)
    if not bridged:
        return idx
    idx2 = dict(idx)
    units2 = dict(idx.get("units", {}))
    units2[_BRIDGE_UNIT] = bridged
    idx2["units"] = units2
    return idx2


@dataclass
class Selection:
    """One selector answer: what to run, per-channel evidence for it, and
    the blind spot — tests neither selected nor known to be safe to skip."""

    selected: "list[str]" = field(default_factory=list)
    channels: "dict[str, list[str]]" = field(default_factory=dict)
    blind_spot: "list[str]" = field(default_factory=list)
    notes: "list[str]" = field(default_factory=list)

    def to_dict(self) -> "dict[str, Any]":
        return {"selected": self.selected, "channels": self.channels,
                "blind_spot": self.blind_spot, "notes": self.notes}


def receipt(sel: "Selection", *, source_ref: "str | None" = None,
           snapshot_commit: "str | None" = None,
           whole_tree_files: "Iterable[str]" = ()) -> "dict[str, Any]":
    """The impact-selection receipt: WHAT drove the pick (a fingerprint — the
    source ref/diff, the snapshot's own commit as the manifest, and the
    whole_tree POLICY row count the gate book contributed), WHICH channel(s)
    picked up each selected test, and — for every blind-spot test the caller
    still owes the full suite — an explicit UNKNOWN skip cost rather than an
    assumed zero: this engine measures WHICH tests a change reaches, never
    HOW LONG a skipped one would have taken, so reporting "0" would state a
    fact nobody measured. A missing fingerprint field stays `None`, never a
    fabricated stand-in."""
    by_test: "dict[str, list[str]]" = {}
    for channel, tests in sel.channels.items():
        for t in tests:
            by_test.setdefault(t, []).append(channel)
    return {
        "fingerprint": {
            "source_ref": source_ref,
            "manifest": snapshot_commit,
            "policy_whole_tree_rows": len(list(whole_tree_files)),
        },
        "selected": [{"test": t, "channels": sorted(by_test.get(t, []))}
                    for t in sel.selected],
        "skipped": [{"test": t, "reason": "matched no selection channel "
                    "(self/import/whole_tree/string)", "cost": "unknown"}
                   for t in sel.blind_spot],
        "notes": list(sel.notes),
    }


def whole_tree_files(root: Path, descriptor: Any = None) -> "set[str]":
    """The tests every change selects: the target's gate book rows marked
    `whole_tree` (`GateBook.whole_tree_files`) plus every
    `codemap.yaml` `test_select:` row of `kind: whole_tree` (a path or a glob;
    `select_tests` expands it). The one read both entry points (`hs-code change
    test select`, the test skill's `affected_tests.py`) make. A book that does
    not load raises `gate_registry.GateRegistryError`, a malformed codemap
    `DescriptorError`: without them the whole-tree channel cannot answer, so no
    superset selection exists to hand out. `descriptor=None` loads the codemap
    when one exists."""
    from hs_family import gate_registry
    out = set(gate_registry.load(root).whole_tree_files())
    if descriptor is None and (Path(root) / "codemap.yaml").is_file():
        from codegraph._core.descriptor import load
        descriptor = load(Path(root) / "codemap.yaml", Path(root)).descriptor
    out |= {r["test"] for r in getattr(descriptor, "test_select", ()) or ()
            if r["kind"] == "whole_tree"}
    return out


def select_tests(idx: "dict | None", changed_paths: "Iterable[str]", *, root: Path,
                 whole_tree_files: "Iterable[str]" = (),
                 channels: "Iterable[str]" = CHANNELS) -> Selection:
    """The one door every caller (the `hs-code change test select` verb,
    `affected_tests.py`'s rewritten inside, and `s1_recall.py`'s new-selector
    scoring mode) goes through.

    `idx` is a loaded snapshot index (`codegraph._core.snapshot.store.
    load_dir`'s own shape) or `None` when no measurement is available — see
    this module's own WHAT IT CANNOT SEE for what `None` degrades to.
    `changed_paths` are repo-relative `.py` paths; an empty iterable is a
    real, distinct answer ("nothing changed"), not an error — same law
    `codegraph.change.impact.run_impact` applies to `changed_targets == []`.

    `root` doubles as the tree `codegraph._core.snapshot_bridge.
    bridge_records` scans for a test file `idx` never measured (outside
    every declared root) — merged into a LOCAL copy of `idx` before any
    channel below reads it, so a bridged test joins `all_tests`, the import
    channel, and the string channel exactly like a real snapshot record,
    with no separate merge step per channel (see `_augmented_idx`).
    """
    enabled = set(channels)
    unknown = enabled - set(CHANNELS)
    if unknown:
        raise ValueError(
            "selection: unknown channel(s) %s — known: %s"
            % (", ".join(sorted(unknown)), ", ".join(CHANNELS)))

    layout = build_test_layout(root)
    idx = _augmented_idx(idx, root, layout)
    changed = sorted(set(changed_paths))
    all_tests = (sorted({m["path"] for m in _modules(idx) if layout.is_test_file(m["path"])})
                if idx is not None else sorted(p for p in changed if layout.is_test_file(p)))

    if not changed:
        return Selection(selected=[], channels={}, blind_spot=all_tests,
                         notes=["no changed files — nothing selected"])

    per: "dict[str, set[str]]" = {}
    notes: "list[str]" = []

    if "self" in enabled:
        per["self"] = {p for p in changed if layout.is_test_file(p)}

    if idx is None:
        notes.append(
            "no snapshot available — only the self-select channel ran; "
            "import/string need a measured import graph")
    else:
        if "import" in enabled:
            by_path = _module_by_path(idx)
            changed_modules = {by_path[p]["module"] for p in changed if p in by_path}
            reached = import_channel(idx, changed_modules)
            per["import"] = {m["path"] for m in _modules(idx)
                             if m["module"] in reached and layout.is_test_file(m["path"])}
        if "string" in enabled:
            sel, stats = string_channel(idx, changed, root, layout)
            per["string"] = sel
            if any(stats.values()):
                notes.append(
                    "string channel: %d test(s) via path_drivers, %d via a "
                    "<stem>.py text-mention fallback, %d via a string literal naming "
                    "the changed path or a directory holding it"
                    % (stats["path_drivers"], stats["text_fallback"], stats["path_literals"]))

    if "whole_tree" in enabled:
        # Independent of `idx`: the gate book and `codemap.yaml` `test_select:`
        # are the target repo's own declarations, not this engine's measurement
        # — a change anywhere can redden one of these whether or not a snapshot
        # exists. A glob expands over the tree; an entry naming no file is a
        # dead row, said so rather than silently contributing nothing.
        found, dead = codegraph_change_reader.whole_tree_tests(root, whole_tree_files)
        per["whole_tree"] = found
        for e in dead:
            notes.append("whole_tree: %r names no test file in this tree" % e)

    selected = sorted(set().union(*per.values())) if per else []
    blind = sorted(set(all_tests) - set(selected))
    return Selection(selected=selected, channels={k: sorted(v) for k, v in per.items()},
                     blind_spot=blind, notes=notes)
