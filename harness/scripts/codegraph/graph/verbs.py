"""hs-code's `graph` domain — verbs over the ported codegraph measurement
library (`codegraph/`); it imports `codegraph._core...` like any other caller.

Each verb is a `Command` constant (`add_args`/`run`) named by its registry row's
`entry:`; the family core parses the declared argv and hands `run` a namespace
carrying `ctx.root` (the target repo) and `opts` (`hs_code.ShellOptions`, the
shell's own flags).
`run` returns a plain int — hs-code's own exit ladder (hs_family.codes), not the
ported `Verdict`'s own dataclass; `Verdict` is used INTERNALLY here (matching
the upstream architecture, and keeping its own tautology-guard tests
meaningful) and converted to an int only at the return.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]   # codegraph/<pkg>/x.py -> scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import codegraph_snapshot_reader  # noqa: E402
import harness_paths  # noqa: E402

from codegraph._core.descriptor import DescriptorError, load  # noqa: E402
from codegraph._core.snapshot.pipeline import SnapshotError, SnapshotRefused  # noqa: E402
from codegraph._core.snapshot.writer import write_current  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command, Outcome, state_of  # noqa: E402

#: What a snapshot pass without `--apply` says, in the envelope's `warnings` and on
#: stderr under `--text`: `state: done` alone reads as "filed", and it filed nothing.
DRY_RUN_WARNING = ("DRY RUN — nothing written; add --apply to file this snapshot "
                   "(the store's default snapshot is unchanged)")


class SnapshotBaseUnresolved(Exception):
    """The snapshot store's data home could not be resolved — every caller
    converts this to `Verdict.broken(str(e)).exit`, the same honest-cannot-run
    shape `DescriptorError`/`SnapshotError` already use in this file."""


def _snapshot_base(project: str, out_override: str | None, root: Path) -> Path:
    """Where this project's snapshots live.

    A snapshot is a measurement OF a tree, so it NEVER lands beside that
    tree the way the ported tool's old default did (that default put repo
    A's numbers inside repo B, and refused outright anywhere with no tool
    root of its own — the common case for a verb whose job is measuring
    OTHER people's trees). `--out` wins outright; otherwise the snapshot
    lands under this harness's own data root, keyed by project name AND by
    `root`'s own WORKTREE (`codegraph._core.snapshot.worktree_key`) —
    `harness_paths.data_root()` is a per-project home regardless of which
    tree `--repo` points at, but it deliberately redirects every linked git
    worktree's STATE to the host repo (F2, `harness_paths.py`). A snapshot
    is a measurement, not shared state: two worktrees of one repo can carry
    different code, so without the worktree-key segment below one
    worktree's snapshot would read as, or get silently overwritten by,
    another's (see `codegraph._core.snapshot.worktree_key`'s own docstring).

    Uses `harness_paths.tool_data_root()` — the TOOL-context resolver (this
    runs as a Bash-tool invocation of `hs-code`, not a hook) — never the bare
    `data_root()`: under a global install with `HARNESS_BIN_ROOT` set but no
    `HARNESS_DATA_ROOT`/`CLAUDE_PROJECT_DIR` to anchor a project, the bare
    resolver fails closed to the `/__harness_data_root_unresolved__` marker
    PATH — measured: used as a real directory, it raised a bare
    `PermissionError` traceback from deep inside the snapshot pipeline
    instead of refusing here with an actionable message.

    The `codegraph/snapshots/<project>/<worktree_key(root)>` SUFFIX itself is
    `codegraph_snapshot_reader.snapshot_base` — this function's own job is
    only resolving WHICH data root to hand it (the tool-context tier, the
    `--out` escape hatch), never re-joining those path segments a second
    time beside the reader's copy.
    """
    if out_override:
        return Path(out_override)
    base, why = harness_paths.tool_data_root_and_reason()
    if harness_paths.data_root_unresolved(base):
        raise SnapshotBaseUnresolved(
            "cannot resolve the snapshot store's data home, so `graph snapshot` "
            "has nowhere to write: %s" % why)
    return codegraph_snapshot_reader.snapshot_base(base, project, root)


def _snapshot_args(ap: argparse.ArgumentParser) -> None:
    """The pipeline's own `mode`, `root` and `commit` knobs have no flag: no
    caller measures anything but HEAD of the whole tree in `port` mode.
    `--keep` has none either: how many snapshots a base holds is the shared
    store layer's own policy (`codegraph._core.snapshot.store.
    resolve_keep`/`DEFAULT_KEEP`), applied the SAME way to every writer —
    not a per-command dial."""
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="write here instead of this project's own snapshot store")


def _snapshot(args: argparse.Namespace) -> "int | Outcome":
    """Run the snapshot pipeline over `root` (hs-code's `--repo`, default the
    resolved project root) and its `<root>/codemap.yaml` descriptor.

    Dry-run by default (measures into a scratch directory, writes nothing) —
    `--apply` (hs-code's own shared flag, not re-declared here) writes for
    real. Mirrors the ported tool's own writer-defaults-to-dry-run law.
    """
    root, opts = args.ctx.root, args.opts
    out_override = args.out

    descriptor_path = root / "codemap.yaml"
    try:
        loaded = load(descriptor_path, root)
    except DescriptorError as e:
        return Verdict.broken(str(e)).exit
    descriptor = loaded.descriptor

    try:
        base = _snapshot_base(descriptor.project, out_override, root)
    except SnapshotBaseUnresolved as e:
        return Verdict.broken(str(e)).exit
    apply = bool(getattr(opts, "apply", False))

    # ONE orchestrator, shared with `affected_tests._resolve_idx`: scope ->
    # dirty -> tool_stamp -> reuse_key -> find_reusable -> take_snapshot ->
    # prune_snapshots -> prune_dead_worktrees, in `writer.write_current`'s
    # own docstring. `sweep_dead_worktrees=out_override is None`: an
    # explicit `--out` target has no store-wide sibling-worktree structure
    # to sweep (`base`'s parent there is an arbitrary caller directory, not
    # the `.../<project>/` every worktree of this repo shares).
    try:
        outcome = write_current(root, base, descriptor, apply=apply, mode="port",
                                sweep_dead_worktrees=out_override is None)
    except SnapshotRefused as e:
        return Verdict.refused(str(e)).exit
    except SnapshotError as e:
        return Verdict.broken(str(e)).exit

    data = {"dry_run": outcome.dry_run, "exists": outcome.exists, "reused": outcome.reused,
            "snapshot": outcome.out_dir.name, "out_dir": str(outcome.out_dir),
            "commit": outcome.commit, "tree": outcome.tree, "totals": outcome.totals,
            "roots_kept": outcome.roots_kept, "roots_declared": outcome.roots_declared,
            "notes": outcome.notes}
    code = Verdict.ok(data).exit
    if getattr(opts, "json", False):
        # The envelope carries `data`; every note and the dry-run line are its
        # `warnings`, the same place a query verb's notes land.
        warnings = (*outcome.overlap_warnings, *outcome.notes,
                    *(() if outcome.reused or apply else (DRY_RUN_WARNING,)))
        return Outcome(exit_code=code, state=state_of(code), data=data,
                       warnings=tuple(w for w in warnings if w.strip()))
    if outcome.reused:
        print(f"snapshot {outcome.out_dir.name}  ({outcome.out_dir})  "
              f"[reused: unchanged since the last apply]")
        for k in sorted(data["totals"]):
            print(f"  {k:16} {data['totals'][k]}")
        return code

    for ln in outcome.banner:
        print(ln)
    for ln in outcome.overlap_warnings:
        print(ln, file=sys.stderr)
    for ln in outcome.notes:
        print(f"⚠ {ln}", file=sys.stderr)
    suffix = ""
    if not apply:
        print(f"⚠ {DRY_RUN_WARNING}", file=sys.stderr)
        suffix = "  [DRY RUN: nothing written — --apply writes"
        if outcome.exists:
            suffix += "; already exists"
        suffix += "]"
    print(f"snapshot {outcome.out_dir.name}  ({outcome.out_dir}){suffix}")
    for k in sorted(outcome.totals):
        print(f"  {k:16} {outcome.totals[k]}")
    return code


SNAPSHOT = Command(add_args=_snapshot_args, run=_snapshot)
