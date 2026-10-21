"""hs_run_facts.code_tests_reached — the `code.tests_reached` fact provider.

`provide(ctx) -> {value, basis, warning, [not_in_snapshot]}`, the shape
`hs_run_facts.engine.resolve()` calls `entry(ctx)` for and folds through
`_apply_cap`. `ctx = {root, domain, state, subjects, snapshot}`; `subjects`
is always a resolved list by the time this runs (the engine's own
`_default_subjects` already ran if the verb passed none) and `snapshot` is
the already-loaded, already-freshness-checked index dict.

`value` = subjects that are THEMSELVES test files (self-select — a path
check, no snapshot lookup needed), UNION the transitive reverse-import walk
(`codegraph_change_reader.import_channel_depths`, a depth-tracking sibling
of `import_channel` — see that function's own docstring for why
`import_channel` itself stays untouched; both moved out of `codegraph.
change.selection` into a flat stdlib-only module so this package stays
codegraph-free) from every subject THE SNAPSHOT KNOWS ABOUT,
filtered through `codegraph_change_reader.FilesLayout` — built here with a
DECLARED `codemap.yaml` `layout.tests_subdir` when one exists, read directly
via `yaml_io` the same way `hs_run_facts.engine.project_name` reads
`project:` (never through codegraph's own descriptor loader — this package
stays codegraph-free, same law `codegraph_change_reader.py` holds itself
to).

ORDER: `value` is sorted by DEPTH first (self-selected subjects at depth 0,
a direct importer at depth 1, a two-hop importer at depth 2, ...), path only
breaking a tie within one depth — never plain alphabetical order. This is
what a downstream cap (`hs_run_facts.engine._apply_cap`) or the aggregate
byte ceiling relies on to cut the FARTHEST tests first: a cap over a
purely path-sorted list can keep three alphabetically-early transitive
hits and drop the one direct importer, which is the wrong priority for
`cook` reading this fact.

THE SAME CHANNELS AS `hs-code change test select`: beside self-select and
the import walk, `value` carries the `string` channel
(`codegraph_change_reader.string_channel`: `path_drivers`, a `<stem>.py`
mention, and a string literal naming a subject's path or a directory holding
it -- how a change to a data file or a doc reaches the gate that reads it)
at depth 1, and the `whole_tree` channel (the gate book's whole-tree rows
plus `codemap.yaml` `test_select:` rows of `kind: whole_tree`, parsed by the
same `codegraph_change_reader.parse_test_select` the descriptor loader
calls) after every measured hop. One gap stays: the selection engine also
files a test no declared root measures (`codegraph._core.snapshot_bridge`)
before its channels run; this package stays codegraph-free, so such a test
is reached here only when the snapshot itself carries it -- the gap `basis`
and `warning` name.

STORIES: a record a language pack named `role: story` (`*.stories.tsx`) is never a
test: one the walk reaches, or a subject that is one, is listed under `stories`
beside `value` (absent when there is none).

A subject ABSENT from the snapshot (a file too new to have been measured)
is never folded into `value` as a silent 0 — it is named in
`not_in_snapshot` instead, so a caller can tell "measured, nothing reaches
it" apart from "never measured at all".
"""
from __future__ import annotations

import shlex
from pathlib import Path

import codegraph_change_reader
import yaml_io

_BASIS = (
    "the snapshot's import, string and whole_tree channels "
    "(`codegraph_change_reader`), the three `hs-code change test select` reads; "
    "a test file no declared root measures is not in the snapshot, so an empty "
    "value means no MEASURED test reaches this subject; a test loading the subject "
    "by path (`importlib.util.spec_from_file_location`) is reached through a "
    "literal path string, never when the path is built at runtime"
)

_WARNING = (
    "code.tests_reached reads the tests the snapshot measured -- a test file "
    "no declared root covers is reached by hs-code change test select alone"
)


def _warning(root, subjects) -> str:
    """The standing disclosure, plus the command that also files the tests no
    declared root measures: the bin's own hs-code by absolute path (it runs
    whether or not the bin is on PATH), scoped to this repo and these
    subjects. No subjects, no command — `--changed` with nothing after it is
    not a runnable line."""
    if not subjects:
        return _WARNING
    import runnable_command
    return "%s; to file those too, run: %s %s" % (
        _WARNING, runnable_command.launcher("hs-code"),
        shlex.join(["--repo", str(root), "change", "test", "select",
                    "--changed", *subjects]))


def _layout_for(root: "Path") -> "codegraph_change_reader.FilesLayout":
    """A `FilesLayout` honoring a DECLARED `codemap.yaml` `layout.
    tests_subdir` when one exists — mirrors `codegraph._core.testfiles.
    build_test_layout`'s own resolution (declared wins outright over the
    UNDECLARED `^tests?(_.+)?$` convention) WITHOUT that function's
    descriptor loader: read straight off the YAML via `yaml_io`, the same
    door `hs_run_facts.engine.project_name` already reads `project:`
    through. An unreadable/absent/malformed `codemap.yaml`, or a `layout:`
    block with no `tests_subdir`, degrades to `tests_subdir=None` — the
    UNDECLARED convention `FilesLayout` already falls back to on its own.

    A `tests_subdir` that is present but the WRONG SHAPE (not a non-empty
    string — `descriptor/store.py`'s own `_load_v2` refuses exactly this,
    `layout.tests_subdir:` must be a non-empty string) is malformed the
    identical way, and must degrade the identical way: `str(42)` reading
    as a credited `"42"` would accept, silently, a value the real
    descriptor loader (`build_test_layout` -> `descriptor.load`) refuses
    outright — the exact divergence the parity tests in
    `test_hs_run_facts_code_tests_reached.py` pin against
    `build_test_layout` itself."""
    root = Path(root)
    tests_subdir = None
    cm = root / "codemap.yaml"
    if cm.is_file():
        try:
            doc = yaml_io.safe_load(cm.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 -- unreadable descriptor = UNDECLARED convention
            doc = {}
        if isinstance(doc, dict):
            layout_blk = doc.get("layout")
            if isinstance(layout_blk, dict):
                ts = layout_blk.get("tests_subdir")
                if isinstance(ts, str) and ts:
                    tests_subdir = ts
    patterns = codegraph_change_reader.python_files_patterns(root)
    return codegraph_change_reader.FilesLayout(tests_subdir=tests_subdir, patterns=patterns)


def _whole_tree_entries(root: "Path") -> "set[str]":
    """What `codegraph.change.selection.whole_tree_files` reads, without its
    codegraph loader: the gate book's whole-tree rows plus `codemap.yaml`
    `test_select:` rows of `kind: whole_tree`, parsed by the same
    `codegraph_change_reader.parse_test_select` the descriptor loader calls. A
    book or a codemap that does not load contributes nothing here; `hs-code
    change test select` refuses on the same tree and names why."""
    from hs_family import gate_registry
    out: "set[str]" = set()
    try:
        out |= set(gate_registry.load(Path(root)).whole_tree_files())
    except gate_registry.GateRegistryError:
        pass
    cm = Path(root) / "codemap.yaml"
    if cm.is_file():
        try:
            doc = yaml_io.safe_load(cm.read_text(encoding="utf-8")) or {}
            rows = codegraph_change_reader.parse_test_select(
                doc.get("test_select") if isinstance(doc, dict) else None)
        except Exception:  # noqa: BLE001 -- the loader refuses it loudly elsewhere
            rows = ()
        out |= {r["test"] for r in rows if r["kind"] == "whole_tree"}
    return out


def provide(ctx: dict) -> dict:
    idx = ctx["snapshot"]
    root = ctx["root"]
    subjects = sorted(set(ctx.get("subjects") or ()))
    layout = _layout_for(root).with_roles(idx)

    by_path = codegraph_change_reader._module_by_path(idx)
    self_selected = {p for p in subjects if layout.is_test_file(p)}
    not_in_snapshot = sorted(p for p in subjects if p not in by_path)

    changed_modules = {by_path[p]["module"] for p in subjects if p in by_path}
    depths = codegraph_change_reader.import_channel_depths(idx, changed_modules)
    reached_depth_by_path = {
        m["path"]: depths[m["module"]] for m in codegraph_change_reader._modules(idx)
        if m["module"] in depths and layout.is_test_file(m["path"])
    }
    stories = sorted({m["path"] for m in codegraph_change_reader._modules(idx)
                      if m["module"] in depths and layout.is_story_file(m["path"])}
                     | {p for p in subjects if layout.is_story_file(p)})

    # depth 0 = the subject IS a test (self-select); depth N = an importer
    # N hops away. A self-selected path always wins its own depth-0 slot
    # even if the same path also shows up in the reverse walk at some N>0.
    depth_by_path = dict(reached_depth_by_path)
    depth_by_path.update({p: 0 for p in self_selected})

    # The two channels `hs-code change test select` reads beside the import
    # graph. A test naming a subject (its path, a directory holding it) sits at
    # depth 1, beside a direct importer; a whole-tree test is reached by every
    # subject but names none of them, so it sorts after every measured hop.
    named, _stats = codegraph_change_reader.string_channel(idx, subjects, Path(root), layout)
    for p in named:
        depth_by_path.setdefault(p, 1)
    whole, _dead = codegraph_change_reader.whole_tree_tests(Path(root), _whole_tree_entries(root))
    last = max(depth_by_path.values(), default=0) + 1
    for p in whole:
        depth_by_path.setdefault(p, last)

    # ordered NEAREST first (self-tests, then direct importers, then two
    # hops out, ...), path breaking ties within one depth -- never plain
    # alphabetical order. This is what lets a downstream cap (`_apply_cap`)
    # or the aggregate byte ceiling cut the FARTHEST tests first instead of
    # whichever happen to sort earliest by path.
    ordered_value = [p for p, _d in sorted(depth_by_path.items(), key=lambda kv: (kv[1], kv[0]))]

    out = {"value": ordered_value, "basis": _BASIS, "warning": _warning(root, subjects)}
    if stories:
        out["stories"] = stories
    if not_in_snapshot:
        out["not_in_snapshot"] = not_in_snapshot
    return out
