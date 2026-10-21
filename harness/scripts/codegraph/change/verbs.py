"""hs-code's `change` domain — verbs that answer a SECOND question over an
already-measured snapshot (or a pair of them). The verb shim; the engine
lives in the `codegraph.change` package (`codegraph/change/impact.py`,
`codegraph/change/selection.py`).

Today: `impact` (blast radius / fan-in), ported from ob-tools' `devkit dev
impact` (pinned commit `60075cb`); and `test select` (`TEST`, dispatching
to its one sub-verb) -- pick the tests a change can break, over the same
selection engine `affected_tests.py` calls (`codegraph.change.selection.
select_tests`). Both keep their `cmd_*` verb shim HERE rather than colocated
with their engine module the way the OTHER four `change` verbs do (`api-diff`/
`snapshot-diff`/`hotspots`/`report` each keep `cmd_*` inside their own package
module) -- a historical remnant (`change.yaml`'s own header: this flat module
predates the package-path convention), tracked in the backlog rather than
moved inline here. `IMPACT`/`TEST`: each verb is a `Command` constant (`add_args`/`run`) named by its registry row's
`entry:`; the family core parses the declared argv and hands `run` a namespace
carrying `ctx.root` (the target repo) and `opts` (`hs_code.ShellOptions`, the
shell's own flags).

THREE ARGV SHAPES:
    impact --base REF [SYMBOL] [--changed FILE] [--depth N] [--labels FILE]
           [--scan-root DIR] [--no-text-scan]
        Warns when the local `REF` branch has drifted from `origin/REF`
        (`hs_family.base_drift`, never blocks), measures `REF`'s own tree and
        the current tree, and — with no `SYMBOL`/`--changed` given —
        defaults its target set to every symbol defined, in EITHER tree, by
        the files that differ from
        `merge-base(REF, HEAD)` in the WORKING tree, untracked files included, (`--base` names
        files, obt's `--changed` names symbols; `targets_from_files` is the
        bridge). Resolved with `run_impact(strict=False)`: the everyday diff
        adds a symbol (absent from the old tree) or removes one (absent from
        the new tree) — an absence from ONE tree is the expected shape here,
        reported per-tree, never a run-ending error by itself.

    impact --data-key KEY [--depth N] [--labels FILE] [--no-text-scan]
        A data key (or enum value, or a JSON pointer to one) has no symbol: the
        working tree is measured once, the targets are the symbols that read KEY
        by name (`codegraph.change.data_key`), and the answer names where KEY is
        declared too. A key no tracked file mentions is exit 1.

    impact OLD_SNAP NEW_SNAP [SYMBOL] [--changed FILE] [--depth N]
           [--labels FILE] [--scan-root DIR] [--no-text-scan]
        The ported obt CLI's own shape: two already-taken snapshot
        directories. No default target set exists here (obt's own CLI had
        none either) — a bare `SYMBOL` or `--changed FILE` is required.

`--changed FILE` in EITHER shape is obt's own format unchanged: the JSON a
`graph changed` envelope `data`-shaped payload ({"changed": [[name, file], ...],
"added": [...]}) or a bare JSON list of symbol names.

WHAT IT CANNOT SEE
    * `--base`'s old-tree measurement uses THIS repo's CURRENT `codemap.yaml`
      for BOTH trees (obt's own two-snapshot CLI never had this problem — it
      took two already-measured snapshots). A `layout.roots` rename between
      `REF` and HEAD is invisible here: the old tree is measured with the
      NEW layout, which finds nothing under a moved root rather than
      erroring. Use the explicit snapshot-pair shape across a layout rename.
    * `REF`'s tree is materialized via `git worktree add --detach` (never a
      bare `git archive` extraction): `take_snapshot`'s own `commit_stamp`
      runs `git log` against the directory it measures, which a plain
      tar-extracted tree (no `.git`) cannot answer. The worktree shares this
      SAME repo's object store, so the snapshot's commit date is the real
      one.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

_SCRIPTS = Path(__file__).resolve().parents[2]   # codegraph/<pkg>/x.py -> scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import hs_family  # noqa: E402
import hs_family.base_drift as base_drift  # noqa: E402
import process_group  # noqa: E402
import secret_scan_before_ship as secret_scan  # noqa: E402
from hs_family import gate_registry  # noqa: E402

from ambiguous_input import Ambiguous, NotFound  # noqa: E402
from codegraph._core.descriptor import DescriptorError, load  # noqa: E402
from codegraph._core.git import GitError  # noqa: E402
from codegraph._core.git import changed_since as _changed_since  # noqa: E402
from codegraph._core.snapshot.pipeline import SnapshotError  # noqa: E402
from codegraph._core.snapshot.store import SnapshotMissing, load_dir  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph.change import data_key, selection  # noqa: E402
from codegraph.change.impact import (CallersIndexMissing, ImpactResult, run_impact,  # noqa: E402
                                     targets_from_files)

from codegraph_graph_freshness import SnapshotResolveError, fresh_scratch, resolve_idx  # noqa: E402
from codegraph.graph.query import _other_engine  # noqa: E402
from codegraph.graph.verbs import SnapshotBaseUnresolved, _snapshot_base  # noqa: E402
from hs_family.cli import Command  # noqa: E402


class _ImpactUsageError(Exception):
    """A cannot-run this verb itself surfaces — never a raw exception, always
    turned into `Verdict.broken(str(e)).exit` at the one place `_impact`
    returns."""


def _load_targets(path: str) -> "list[str]":
    """Symbol list from `graph changed`'s envelope `data` or a bare JSON list —
    obt's own `--changed` shape, unchanged."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(x) for x in data]
    rows: "list[str]" = []
    for key in ("changed", "added"):      # removed symbols have no radius left
        for name, _file in data.get(key, ()):
            rows.append(str(name))
    return rows


def _load_labels(path: "str | None") -> "dict[str, int]":
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in raw.items()}


def _diff_name_only(repo: Path, base_ref: str) -> "list[str]":
    """Files that differ between `merge-base(base_ref, HEAD)` and the WORKING
    TREE, plus untracked files git does not ignore — the file-granularity
    source `--base`'s DEFAULT target set reads when no symbol/`--changed` is
    given. Delegates to `codegraph._core.git.changed_since`, the ONE shared
    home for this read (`affected_tests._changed_from_base` calls the same
    function), translating its `GitError` into this module's own
    `_ImpactUsageError` so every existing caller here keeps seeing the verb
    contract it already handles."""
    try:
        return _changed_since(repo, base_ref)
    except GitError as e:
        raise _ImpactUsageError(f"impact: {e}") from e


@contextlib.contextmanager
def _ref_worktree(repo: Path, ref: str) -> "Iterator[Path]":
    """`ref`'s tree as a throwaway `git worktree` for the body of the `with`
    (see this module's own WHAT IT CANNOT SEE for why not a bare `git
    archive` extraction), removed on EVERY way out — including an interrupt
    or SIGTERM while the checkout itself is running. That is the slow step,
    and the cleanup used to begin only after it returned: an interrupted run
    left both the registration and the directory under /tmp."""
    parent = Path(tempfile.mkdtemp(prefix="hs-code-change-impact-old-wt-"))
    wt = parent / "tree"
    with process_group.raise_on_termination():
        try:
            # Its own process group: `worktree add` runs the checkout in a child
            # git, and killing only the parent left that child writing files
            # into the tree after it had been removed (measured on SIGTERM).
            proc = process_group.run(
                ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "worktree", "add", "--detach",
                 str(wt), ref],
                capture_output=True, text=True, timeout=60, env=secret_scan.git_safe_env())
            if proc.returncode != 0:
                raise _ImpactUsageError(
                    f"impact: git worktree add {ref!r} failed: {proc.stderr.strip()}")
            yield wt
        except (OSError, subprocess.TimeoutExpired) as e:
            raise _ImpactUsageError(f"impact: git worktree add {ref!r} failed: {e}") from e
        finally:
            _remove_worktree(repo, wt)
            shutil.rmtree(parent, ignore_errors=True)


def _remove_worktree(repo: Path, wt: Path) -> None:
    """Unregister and delete OUR OWN throwaway worktree. `git worktree remove
    --force --force` does both in one step; it is skipped when git never registered
    `wt` (the add failed or was cut short before it did).

    On FAILURE this does not fall back to a raw `shutil.rmtree(wt)` alone:
    deleting the directory without unregistering it leaves `git worktree
    list` showing a broken entry, so it reports the failure on stderr instead,
    naming the path. It never runs `git worktree prune`: `prune` sweeps EVERY
    worktree on this repo whose directory has gone missing — another
    session's, on an unmounted disk or a `/tmp` a reboot already cleared — not
    just this one, so it is not this function's call to make on the user's
    behalf.
    """
    listed = subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "worktree", "list",
                             "--porcelain", "-z"],
                            capture_output=True, text=True, timeout=30, env=secret_scan.git_safe_env())
    if f"worktree {wt}" not in listed.stdout.split("\0"):
        return
    # Twice: `worktree add` locks the tree ("initializing") until it is done,
    # and an add cut short leaves that lock, which one `--force` refuses. The
    # tree is ours alone, so overriding its lock touches nothing else.
    res = subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "worktree", "remove",
                          "--force", "--force", str(wt)],
                         capture_output=True, text=True, timeout=30, env=secret_scan.git_safe_env())
    if res.returncode != 0:
        print(f"impact: could not remove the throwaway worktree at {wt} — left in "
              f"place; clean it up by hand (`git -C {repo} worktree remove --force "
              f"{wt}`): {res.stderr.strip()}", file=sys.stderr)


def _targets(changed_file: "str | None", symbol: "str | None") -> "dict[str, Any] | None":
    if symbol:
        return {"symbol": symbol}
    if changed_file:
        return {"changed_targets": _load_targets(changed_file)}
    return None


def _base_mode(argv: "list[str]", base_ref: str, root: Path, *,
               changed_file: "str | None", run: Callable[..., ImpactResult]) -> ImpactResult:
    """The `--base REF` shape. `run(idxs, scan_roots, tree_names, **targets)`
    is called while `REF`'s worktree still exists: the old snapshot's text
    scan reads the OLD tree's own files, which are gone once it is removed."""
    if len(argv) > 1:
        raise _ImpactUsageError(
            f"impact --base takes at most one positional SYMBOL, got {argv!r}")
    symbol = argv[0] if argv else None

    base_drift.warn_if_base_drifted(root, base_ref, stderr=sys.stderr)

    try:
        descriptor = load(root / "codemap.yaml", root).descriptor
    except DescriptorError as e:
        raise _ImpactUsageError(str(e)) from e

    with process_group.raise_on_termination():     # the new tree's scratch is ours to remove too
        return _measure_both(argv, base_ref, root, descriptor, changed_file=changed_file,
                             symbol=symbol, run=run)


def _measure_both(argv: "list[str]", base_ref: str, root: Path, descriptor: Any, *,
                  changed_file: "str | None", symbol: "str | None",
                  run: Callable[..., ImpactResult]) -> ImpactResult:
    _snap, new_idx, new_scratch = fresh_scratch(root, descriptor, "impact")
    try:
        with _ref_worktree(root, base_ref) as old_root:
            _snap, old_idx, old_scratch = fresh_scratch(old_root, descriptor, "impact")
            try:
                kwargs = _targets(changed_file, symbol)
                if kwargs is None:
                    # Union of BOTH trees' symbols in the changed files: a
                    # symbol the diff ADDS matches only in `new_idx`, one it
                    # REMOVES matches only in `old_idx` — matching one side
                    # alone would silently drop every deleted symbol from the
                    # target set. `strict=False` is what makes this safe: a
                    # target this union produces is expected to be absent
                    # from ONE of the two trees (that is the whole point of
                    # a diff), so a bare "not found" there must not blank
                    # that tree's report or turn the run red by itself.
                    changed_files = _diff_name_only(root, base_ref)
                    kwargs = {"changed_targets": sorted(
                        set(targets_from_files(old_idx, changed_files))
                        | set(targets_from_files(new_idx, changed_files))),
                        "strict": False}
                return run([old_idx, new_idx], [old_root, None],
                           [f"base {base_ref}", "working tree"], **kwargs)
            finally:
                shutil.rmtree(old_scratch, ignore_errors=True)
    finally:
        shutil.rmtree(new_scratch, ignore_errors=True)


def _snapshot_idx(root: Path, ref: str) -> dict:
    """A snapshot directory, or a name as `graph` prints it, looked up in this project's
    store through the door `graph trace SYM SNAP_B` uses (`_other_engine`)."""
    if Path(ref).is_dir():
        return load_dir(Path(ref))
    try:
        base = _snapshot_base(load(root / "codemap.yaml", root).descriptor.project, None, root)
    except (DescriptorError, SnapshotBaseUnresolved) as e:
        raise SnapshotMissing(f"{ref!r} is not a directory, and no store to look it up in: {e}") from e
    return _other_engine(base, ref).idx


def _snapshot_pair_mode(argv: "list[str]", root: Path, *, changed_file: "str | None",
                        run: Callable[..., ImpactResult]) -> ImpactResult:
    """The `OLD_SNAP NEW_SNAP [SYMBOL]` shape — the ported obt CLI's own. No
    default target set: a caller giving neither `SYMBOL` nor `--changed` is a
    usage error, same as upstream. A snapshot is a directory or a name `graph` printed."""
    if len(argv) not in (2, 3):
        raise _ImpactUsageError(
            f"impact: give --base REF, or OLD_SNAP NEW_SNAP [SYMBOL]; got {argv!r}")
    idxs = [_snapshot_idx(root, argv[0]), _snapshot_idx(root, argv[1])]
    kwargs = _targets(changed_file, argv[2] if len(argv) == 3 else None)
    if kwargs is None:
        raise _ImpactUsageError("impact: give a symbol or --changed FILE")
    return run(idxs, [None, None], None, **kwargs)


def _data_key_mode(argv: "list[str]", key_arg: str, root: Path, *, base_ref: "str | None",
                   changed_file: "str | None",
                   run: Callable[..., ImpactResult]) -> ImpactResult:
    """`--data-key KEY`: the working tree measured once; the targets are the symbols that
    read KEY by name (`data_key.find`). A key no tracked file mentions is exit 1."""
    if argv or base_ref is not None or changed_file is not None:
        raise _ImpactUsageError(
            "impact --data-key takes no SYMBOL, --base or --changed: the key's readers "
            "are the targets, measured on the working tree")
    try:
        key, pointer_note = data_key.key_of(key_arg)
        descriptor = load(root / "codemap.yaml", root).descriptor
    except (ValueError, DescriptorError) as e:
        raise _ImpactUsageError(f"impact: {e}") from e
    with process_group.raise_on_termination():
        _snap, idx, scratch = fresh_scratch(root, descriptor, "impact")
        try:
            found = data_key.find(root, idx, key)
            if pointer_note:
                found["notes"].insert(0, pointer_note)
            res = run([idx], [None], ["working tree"],
                      changed_targets=list(found["readers"]), strict=False)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    code = res.code
    if not (found["declared_in"] or found["readers"] or found["unplaced"]):
        code = 1
        found["notes"].append(f"no tracked file mentions {key!r} as a data key or a "
                              f"string/name in code")
    elif not found["readers"]:
        found["notes"].append(f"{key!r} is declared but no symbol reads it by name -- a key "
                              f"built at runtime is invisible here")
    data = {**(res.data or {}), "data_key": found}
    return ImpactResult(code, "\n".join([*data_key.render(found), res.text]), data)


def _impact_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--base", default=None, metavar="REF")
    ap.add_argument("--data-key", default=None, metavar="KEY",
                    help="a data key or enum value (or a JSON pointer to one): where it is "
                         "declared, the symbols that read it by name, and their radius")
    ap.add_argument("--changed", default=None, metavar="FILE")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--labels", default=None, metavar="FILE")
    ap.add_argument("--scan-root", default=None, metavar="DIR",
                    help="where the NEW tree's files are read for the text scan "
                         "(default: --repo); under --base the old tree is always "
                         "scanned in its own worktree")
    ap.add_argument("--no-text-scan", action="store_true")
    ap.add_argument("rest", nargs="*", metavar="ARGS",
                    help="--base REF: at most one SYMBOL; otherwise OLD_SNAP NEW_SNAP [SYMBOL]")


def _impact(args: argparse.Namespace) -> int:
    """`impact (--base REF | OLD_SNAP NEW_SNAP) [SYMBOL] [--changed FILE]
    [--depth N] [--labels FILE] [--scan-root DIR] [--no-text-scan]` — see
    this module's own docstring for the argv shapes. `--scan-root` names
    where the NEW tree's files are read for the text scan (default: `root`);
    under `--base` the old tree is always scanned in its own worktree."""
    root, opts = args.ctx.root, args.opts
    base_ref, changed_file, depth = args.base, args.changed, args.depth
    labels_file, scan_root_arg, no_text_scan = args.labels, args.scan_root, args.no_text_scan
    argv = args.rest

    try:
        labels = _load_labels(labels_file)
    except (OSError, ValueError, TypeError) as e:
        return Verdict.broken(f"impact: cannot read --labels {labels_file!r}: {e}").exit

    default_root = Path(scan_root_arg) if scan_root_arg else root

    def run(idxs: list, scan_roots: list, tree_names: "list[str] | None",
            **targets: Any) -> ImpactResult:
        return run_impact(idxs, depth=depth, labels=labels,
                          scan_roots=[r or default_root for r in scan_roots],
                          no_text_scan=no_text_scan, tree_names=tree_names, **targets)

    try:
        if args.data_key is not None:
            result = _data_key_mode(argv, args.data_key, root, base_ref=base_ref,
                                    changed_file=changed_file, run=run)
        elif base_ref is not None:
            result = _base_mode(argv, base_ref, root, changed_file=changed_file, run=run)
        else:
            result = _snapshot_pair_mode(argv, root, changed_file=changed_file, run=run)
    except (Ambiguous, NotFound) as e:
        return e.refuse(opts)
    except _ImpactUsageError as e:
        return Verdict.broken(str(e)).exit
    except (SnapshotMissing, SnapshotError, SnapshotResolveError, CallersIndexMissing) as e:
        return Verdict.broken(f"impact: {e}").exit
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return Verdict.broken(f"impact: cannot read --changed {changed_file!r}: {e}").exit

    if getattr(opts, "json", False):
        print(json.dumps(result.data, sort_keys=True, ensure_ascii=False))
    else:
        print(result.text)
    return result.code


IMPACT = Command(add_args=_impact_args, run=_impact)


# ─── test select — the `change` domain's SECOND verb ───────────────────────
#
# `hs-code change test select` — a 3-word command living behind ONE registry
# row (`test`): the (domain, verb) registry is flat, so `test`'s own entry
# owns a sub-verb of its own (`select`, today's only one) rather than the
# registry growing a third tier. An unknown sub-verb is a usage error (exit
# 2), the same "never silently do nothing" law every other verb here follows.

def _change_test_args(ap: argparse.ArgumentParser) -> None:
    """`test <sub-verb> [args...]` — dispatches to this verb's own sub-verb
    (today: `select`). An unknown sub-verb, or none at all, is `hs_family.
    UsageError` (exit 2) — `add_subparsers(required=True)` refuses both,
    never a silent no-op."""
    sub = ap.add_subparsers(dest="sub", required=True)
    select = sub.add_parser("select", help="pick the tests a change can break")
    select.add_argument("--base", default=None, metavar="REF")
    select.add_argument("--changed", nargs="*", default=None, metavar="FILE",
                        help="repeatable — symbol/test source files known to have changed")


def _change_test(args: argparse.Namespace) -> int:
    if args.sub == "select":
        return _test_select(args)
    raise hs_family.UsageError("change test: unknown sub-verb %r (known: select)" % args.sub)


TEST = Command(add_args=_change_test_args, run=_change_test)


def _test_select(args: argparse.Namespace) -> int:
    """`test select (--base REF | --changed FILE...)` — pick the tests a
    change can break: reverse-BFS over the snapshot's own import graph, every
    `whole_tree` gate the target repo's gate book declares, and a
    `path_drivers`/text-mention string channel — see `codegraph.change.
    selection`'s own docstring for the four channels and what each cannot
    see.

    FRESHNESS: the exact law `graph importers` uses (`codegraph_graph_
    freshness`) — the store's latest snapshot is refused when its commit
    differs from HEAD over the descriptor's declared roots, unless
    `--allow-stale` (shell flag) turns the refusal into a warning;
    `--fresh` (shell flag) measures the on-disk tree instead, once per content in this
    worktree's own base (`fresh_filed`).
    """
    root, opts = args.ctx.root, args.opts
    base_ref, changed_explicit = args.base, list(args.changed or [])
    if not base_ref and not changed_explicit:
        return Verdict.broken(
            "change test select: give --base REF or --changed FILE...").exit

    descriptor_path = root / "codemap.yaml"
    try:
        descriptor = load(descriptor_path, root).descriptor
    except DescriptorError as e:
        return Verdict.broken(str(e)).exit

    changed = list(changed_explicit)
    if base_ref:
        base_drift.warn_if_base_drifted(root, base_ref, stderr=sys.stderr)
        try:
            changed += _diff_name_only(root, base_ref)
        except _ImpactUsageError as e:
            return Verdict.broken(str(e)).exit
    changed = sorted(set(changed))

    try:
        whole_tree_files = selection.whole_tree_files(root, descriptor)
    except gate_registry.GateRegistryError as e:
        return Verdict.broken("change test select: %s" % e).exit

    scratch: "Path | None" = None
    try:
        _base, _snap_dir, idx, scratch = resolve_idx(
            root, descriptor, opts, None, None, "change test select",
            bool(getattr(opts, "fresh", False)))
        sel = selection.select_tests(idx, changed, root=root,
                                     whole_tree_files=whole_tree_files)
    except SnapshotResolveError as e:
        return Verdict.broken(str(e)).exit
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    rc = selection.receipt(sel, source_ref=base_ref, snapshot_commit=(idx or {}).get("commit"),
                           whole_tree_files=whole_tree_files)
    if getattr(opts, "json", False):
        print(json.dumps({**sel.to_dict(), "receipt": rc}, sort_keys=True, ensure_ascii=False))
    else:
        print("change test select: %d changed file(s) -> %d test(s) selected, "
             "%d in the blind spot" % (len(changed), len(sel.selected), len(sel.blind_spot)))
        for t in sel.selected:
            print("  " + t)
        for t in sel.stories:
            print("  story (not run): " + t)
        if sel.blind_spot:
            print("  (skip cost for %d blind-spot test(s): unknown, not 0 — run the full "
                 "suite before merge)" % len(sel.blind_spot))
        for n in sel.notes:
            print("⚠ %s" % n, file=sys.stderr)
    return 0 if sel.selected or not changed else 1
