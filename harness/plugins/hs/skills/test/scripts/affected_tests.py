#!/usr/bin/env python3
"""affected_tests.py — pick the test files a change can break, read off a
measured snapshot rather than a hand-parsed import graph.

    affected_tests.py --changed src/foo.py src/bar.py
    affected_tests.py --base main                     # changed = merge-base(main, HEAD) vs the
                                                       # working tree, untracked files included
    affected_tests.py --base HEAD~1 --pytest          # emit a ready-to-run pytest command

Selects through the four channels `codegraph.change.selection.select_tests`
implements — see that module's own docstring for what each sees and cannot
see: `self` (a changed test selects itself), `import` (reverse BFS over the
snapshot's own import graph, module-level and function-local edges, however
many hops away), `whole_tree` (every test the target repo's OWN gate book
declares `whole_tree`, plus its `codemap.yaml` `test_select:` rows of
`kind: whole_tree` — absent both, absent contribution), `string`
(a test naming a changed module by path/`-m`, via the snapshot's
`path_drivers`, a `<stem>.py` text-mention fallback, and a string literal
naming a changed path or a directory holding it — how a doc or data-file
change reaches the gate that reads it). This is a
SUPERSET selector — when in doubt it includes a test; it never claims a
focused run replaces the full suite before merge.

SNAPSHOT: measured into the SAME per-worktree base `hs-code graph snapshot
--apply` writes (`codegraph.graph.verbs._snapshot_base`, `mode="port"`) —
no longer a namespace dedicated to this script. Sharing the base means a
snapshot `graph snapshot --apply` just took, still content-keyed to the
CURRENT tree, is reused here with no rebuild at all — the reuse mechanism
(`codegraph._core.snapshot.worktree_key.reuse_key` + `codegraph._core.
snapshot.store.find_reusable`) matches on tree content (clean: the commit's
own tree hash; dirty: a hash over the uncommitted diff and untracked files)
folded with a digest of the resolved descriptor, so a dirty tree NEVER
silently reuses a clean (or a differently-dirty) measurement — see
`worktree_key.reuse_key`'s own docstring. The store lives under
`harness_paths.tool_data_root()` — resolved the SAME way regardless of
`--root`; a target repo carrying no `codemap.yaml` (a plain project this
script runs against without ever having opted into `hs-code`) gets an
in-memory `auto_discover` descriptor instead — nothing is ever written INTO
the measured repo to get one. Measured cost of a fresh pass over this
harness's own 1717-module tree (`hs-code graph snapshot --json --apply`'s
own `totals.modules`, commit `31bbfe5c1`): ~80-95s; a cache hit (the common case —
most invocations follow one that already warmed it, from this script OR
from `graph snapshot --apply`) is a sub-second disk read. `SnapshotBase
Unresolved` (no `HARNESS_DATA_ROOT` and no initialized project to anchor
one) degrades the same way an unmeasurable tree does — one note on stderr,
`self`-select channel only, never a crash.

LIMITS (run the full suite before merge): every blind spot `selection`'s
own channels carry is inherited whole — a test reaching the change ONLY via
a conftest fixture or a subprocess call (no import, no path mention) is not
selected by ANY channel here. A snapshot needs a real commit to stamp its own
name (`commit_stamp`, the same requirement `graph snapshot`/`change impact`
already carry) — in a directory with no `.git` at all, measuring one fails
and this script degrades to the `self`-select channel alone (still exit 0,
never a crash): a `--changed`-only invocation with no commit anywhere in the
tree gets the weakest answer this script can give, not a wrong one.

Exit: 0 (prints selected tests, possibly none); 2 = usage error (incl. a
--base git can't diff).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# harness/plugins/hs/skills/test/scripts/ -> harness/ is 5 levels up (mirrors
# harness/plugins/hs/skills/bakeoff/scripts/bakeoff_rank.py's own relative walk —
# same depth, same route to the sibling `scripts/` tree). Works identically under
# a per-project AND a global install: a plugin script and harness/scripts/ ship as
# ONE physical tree either way (this is the same reason hs_run.py/hs_cli.py/
# hs_code.py resolve their own root from `__file__` rather than an env var), so a
# path built from `__file__` is never wrong about which tree it is standing in.
_HARNESS_SCRIPTS = Path(__file__).resolve().parents[5] / "scripts"
if str(_HARNESS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_HARNESS_SCRIPTS))
import hs_family.base_drift as base_drift  # noqa: E402
from hs_family import gate_registry  # noqa: E402

from codegraph._core.descriptor import DescriptorError, load  # noqa: E402
from codegraph._core.descriptor.model import DESCRIPTOR_SCHEMA_VERSION, ProjectDescriptor  # noqa: E402
from codegraph._core.git import GitError, changed_since  # noqa: E402
from codegraph._core.snapshot.pipeline import SnapshotError  # noqa: E402
from codegraph._core.snapshot.store import load_dir  # noqa: E402
from codegraph._core.snapshot.writer import write_current  # noqa: E402
from codegraph.change import selection  # noqa: E402
from codegraph.graph.verbs import SnapshotBaseUnresolved, _snapshot_base  # noqa: E402


def _changed_from_base(base: str, root: str):
    """Changed paths since `base` (any file: a data file reaches a test through the
    string channel), or None if git cannot answer (bad ref
    / not a repo). Calls `codegraph._core.git.changed_since` — the SAME read
    `hs-code change test select --base` uses (`codegraph.change.verbs.
    _diff_name_only`), so an uncommitted edit or an untracked file selects
    identically through either entry point. A None MUST surface as a loud
    error — never a silent 'no tests affected', which a caller would
    misread as 'safe, run nothing'."""
    try:
        return list(changed_since(root, base))
    except GitError:
        return None


def _load_descriptor(root: Path) -> ProjectDescriptor:
    """`<root>/codemap.yaml` when declared; else an in-memory `auto_discover`
    descriptor — nothing is ever written INTO the target repo to get one.
    `project` is used only to key this script's own dedicated snapshot
    namespace, so `root.name` (never validated against anything) is fine."""
    descriptor_path = root / "codemap.yaml"
    if descriptor_path.is_file():
        return load(descriptor_path, root).descriptor
    return ProjectDescriptor(schema_version=DESCRIPTOR_SCHEMA_VERSION,
                             project=root.name, auto_discover=True)


def _resolve_idx(root: Path, descriptor: ProjectDescriptor) -> "tuple[dict | None, str | None]":
    """-> (idx, note). `idx` is None only when even a fresh measurement
    could not run (e.g. no `.py` files at all under the discovered roots, or
    the snapshot store's data home cannot be resolved) — the caller still
    answers, degraded to `selection`'s own `self`-only fallback for
    `idx=None` (see that module's docstring).

    Goes through the SAME content-keyed reuse mechanism `hs-code graph
    snapshot --apply` uses (see the module docstring's SNAPSHOT section):
    a snapshot that command just wrote for the CURRENT tree content is
    reused here with no rebuild at all, and a dirty tree here NEVER falls
    back to a clean (or a differently-dirty) measurement — reuse is an
    EXACT content-key match, never "close enough". `writer.write_current`
    is the ONE orchestrator this and `codegraph.graph.verbs.SNAPSHOT`
    both call, instead of each duplicating scope -> dirty -> tool_stamp ->
    reuse_key -> find_reusable -> take_snapshot -> prune sequence; `base`
    is always the resolved store here (`_snapshot_base(..., None, ...)`
    above, never a caller `--out`), so the dead-worktree sweep always runs,
    unlike `SNAPSHOT`'s own call, which skips it under an explicit
    `--out`."""
    try:
        base = _snapshot_base(descriptor.project, None, root)
    except SnapshotBaseUnresolved as e:
        return None, ("cannot resolve the snapshot store (%s) — falling back "
                      "to the self-select channel only" % e)

    try:
        outcome = write_current(root, base, descriptor, apply=True, mode="port")
    except (SnapshotError, GitError) as e:
        return None, ("could not measure a snapshot (%s) — falling back to the "
                      "self-select channel only" % e)
    if outcome.reused:
        return load_dir(outcome.out_dir), "reused a cached snapshot (%s)" % outcome.out_dir.name
    label = "measured a fresh %s snapshot into the store (%.1fs)" % (
        "dirty" if outcome.dirty else "clean", outcome.elapsed)
    return load_dir(outcome.out_dir), label


def parse_args(argv):
    root = "."
    base = None
    changed = []
    as_pytest = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--root":
            if i + 1 >= len(argv):
                raise ValueError("--root needs a path")
            root = argv[i + 1]
            i += 2
        elif a == "--base":
            if i + 1 >= len(argv):
                raise ValueError("--base needs a git ref")
            base = argv[i + 1]
            i += 2
        elif a == "--changed":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                changed.append(argv[i])
                i += 1
        elif a == "--pytest":
            as_pytest = True
            i += 1
        else:
            raise ValueError("unknown argument %r" % a)
    if not base and not changed:
        raise ValueError("provide --changed <files...> or --base <ref>")
    return root, base, changed, as_pytest


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        root, base, changed, as_pytest = parse_args(argv)
    except ValueError as e:
        print("usage: affected_tests.py [--root <dir>] (--changed <files...> | --base <ref>) "
              "[--pytest]", file=sys.stderr)
        print("error: %s" % e, file=sys.stderr)
        return 2

    root = os.path.abspath(root)
    if base:
        # Warn-only, never fatal, never changes `diffed`/the exit code below.
        # The helper itself stays silent on every `base` that is not a plain
        # local branch with an `origin/` counterpart (a SHA, `HEAD~1`, a tag,
        # an `origin/...` spelling).
        base_drift.warn_if_base_drifted(root, base, stderr=sys.stderr)
        diffed = _changed_from_base(base, root)
        if diffed is None:
            print("error: could not `git diff` against %r (bad ref, or not a git repo). "
                  "Pass --changed <files...> instead." % base, file=sys.stderr)
            return 2
        changed += diffed
    # normalise to root-relative; every kind of file (a doc, a data file) can select
    norm = []
    for c in changed:
        abs_c = os.path.abspath(c) if os.path.isabs(c) else os.path.join(root, c)
        rel = os.path.relpath(abs_c, root)
        if rel.startswith(".."):
            print("warning: path escapes repo root, skipping: %s" % c, file=sys.stderr)
            continue
        norm.append(rel)
    changed = sorted(set(norm))

    if not changed:
        # an empty result must not read as "safe, run nothing": this is a SUPERSET
        # selector — a test reaching the change only via a conftest fixture or a
        # subprocess call (no import, no path mention) is not captured.
        print("(no affected test files found — this is a superset selector over the "
              "import graph; run the full suite before merge.)", file=sys.stderr)
        return 0

    root_path = Path(root)
    descriptor = _load_descriptor(root_path)
    idx, note = _resolve_idx(root_path, descriptor)
    if note:
        print("note: %s" % note, file=sys.stderr)

    try:
        whole_tree_files = selection.whole_tree_files(root_path, descriptor)
    except gate_registry.GateRegistryError as e:
        print("error: the gate registry did not load, so the whole-tree channel cannot "
              "answer: %s — fix it, or run the full suite." % e, file=sys.stderr)
        return 2
    sel = selection.select_tests(idx, changed, root=root_path,
                                 whole_tree_files=whole_tree_files)
    tests = sel.selected

    if not tests:
        print("(no affected test files found — this is a superset selector over the "
              "import graph; run the full suite before merge.)", file=sys.stderr)

    if as_pytest and tests:
        import test_runners
        commands, unclaimed = test_runners.commands(root_path, tests)
        for cmd in commands:
            print(cmd)
        if unclaimed:
            print("(no language pack runner claims: %s)" % " ".join(unclaimed), file=sys.stderr)
    else:
        for t in tests:
            print(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
