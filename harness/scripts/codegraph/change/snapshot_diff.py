"""`hs-code change snapshot-diff` — two snapshots as one Markdown MR artifact.

Ported from ob-tools' `devkit.clones.snapshotdiff` (pinned commit `60075cb`);
the engine is unchanged, `SNAPSHOT_DIFF` is the hs-code adapter.

The artifact a reviewer reads next to a big change: what the code-tree
measurement says moved between two snapshot passes — symbols added, removed,
changed, and MOVED (same name, same body-sha, new file — mechanical identity,
not a rename guess). Same input, same bytes, every time: the file is meant to
be diffed in review, so nondeterminism here would fake a revert.

USAGE
    hs-code change snapshot-diff [OLD_SNAP NEW_SNAP] [--md out.md] [--top N]

    Without OLD_SNAP/NEW_SNAP this project's own store (the one `hs-code
    graph snapshot --apply` writes to) supplies both: NEW is the snapshot of
    the tree as it stands (matched by content key, with a warning when
    nothing stored matches), OLD the newest snapshot of any OTHER commit. A
    second measurement of NEW's own commit -- a dirty state, possibly one
    since reverted -- is never the old side. Nothing to pair is a
    cannot-run, never a guess. Without `--md` the Markdown
    goes to stdout; with it, only the summary prints and the file is written
    under `--apply` (parents created) — a dry run names the file it would
    write.

WHAT IT CANNOT SEE
    * Inherits the snapshot contract: comment-only edits are invisible (`sha`
      strips comments), dynamic symbols do not exist, callers are a floor.
    * A RENAME shows as removed+added. `moved` pairs name+sha across paths —
      that is identity, not inference; anything looser would guess.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from codegraph._core.query.engine import walk_symbols
from codegraph._core.snapshot.store import SnapshotMissing, load_dir, resolve_current, snapshot_dirs
from codegraph._core.verdict import Verdict
from hs_family.cli import Command


def diff_snapshot_pairs(a: dict, b: dict) -> "dict[str, Any]":
    """Added/removed/changed/moved between two indexes, keyed (name, path) —
    the four categories are MUTUALLY EXCLUSIVE (a mover is not also added and
    removed; that double read is exactly what an MR reviewer does not need).

    `moved` is (name, old_path, new_path) for entries whose body-sha is
    IDENTICAL — moving a file without touching the body is not a change, and
    identity is mechanical, not a guess. A rename WITH edits (sha differs)
    stays removed+added.

    UNAMBIGUOUS PAIRING ONLY: a (name, sha) that appears on MORE than one old
    or more than one new path is ambiguous — `logger = getLogger(__name__)` is
    byte-identical in hundreds of files, and pairing every old copy with every
    new copy is a cross-product, not a move (measured on the ported tool's own
    tree: 15 864 rows for 4 939 names, `logger` alone accounting for 10 342).
    Such keys are left as removed+added and counted in `moved_ambiguous`; only
    a key with exactly one old and one new path becomes a `moved` row.
    """
    def flat(ix: dict) -> "dict[tuple[str, str], str]":
        return {(s["name"], m["path"]): s["sha"] for _u, m, s, _p in walk_symbols(ix)}

    A, B = flat(a), flat(b)

    def by_key(F: "dict[tuple[str, str], str]") -> "dict[tuple[str, str], set[str]]":
        out: dict[tuple[str, str], set[str]] = {}
        for (n, p), sha in F.items():
            out.setdefault((n, sha), set()).add(p)
        return out

    KA, KB = by_key(A), by_key(B)
    moved: list[tuple[str, str, str]] = []
    ambiguous = 0
    for key in KA.keys() & KB.keys():
        old_paths, new_paths = KA[key], KB[key]
        if len(old_paths) == 1 and len(new_paths) == 1:
            pa, pb = next(iter(old_paths)), next(iter(new_paths))
            if pa != pb:
                moved.append((key[0], pa, pb))
        elif old_paths != new_paths:
            ambiguous += 1        # same (name, sha), several homes — not a move
    moved.sort()
    moved_old = {(n, pa) for n, pa, _pb in moved}
    moved_new = {(n, pb) for n, _pa, pb in moved}
    return {
        "added":   sorted(set(B) - set(A) - moved_new),
        "removed": sorted(set(A) - set(B) - moved_old),
        "changed": sorted((n, p) for n, p in set(A) & set(B) if A[(n, p)] != B[(n, p)]),
        "moved":   moved,
        "moved_ambiguous": ambiguous,
    }


def markdown_diff(a: dict, b: dict, top: int = 25) -> str:
    """Deterministic Markdown artifact. Sorted inputs, fixed section order,
    truncated long sections with an explicit count — same input, same bytes."""
    d = diff_snapshot_pairs(a, b)
    L = [f"# Snapshot diff — {a.get('snapshot', '?')} → {b.get('snapshot', '?')}",
         "",
         "| | count |",
         "|---|---|",
         f"| added | {len(d['added'])} |",
         f"| removed | {len(d['removed'])} |",
         f"| changed | {len(d['changed'])} |",
         f"| moved (same name+sha, unambiguous) | {len(d['moved'])} |",
         "",
         f"- base commit `{a.get('commit', '?')}` · head commit `{b.get('commit', '?')}`",
         "- a RENAME appears as removed+added; `moved` is identity (name+sha), not a guess",
         f"- {d['moved_ambiguous']} (name, sha) key(s) with several homes (e.g. `logger`)"
         " stayed removed+added — cross-product pairing is not a move",
         ""]
    sections = (("Added", d["added"], "path"),
                ("Removed", d["removed"], "path"),
                ("Changed", d["changed"], "path"),
                ("Moved", [(n, f"{pa} → {pb}") for n, pa, pb in d["moved"]], "to"))
    for title, rows, col in sections:
        L.append(f"## {title} ({len(rows)})")
        if not rows:
            L.append("")
            L.append("(none)")
            L.append("")
            continue
        L.append("")
        L.append("| symbol | " + col + " |")
        L.append("|---|---|")
        for name, where in rows[:top]:
            L.append(f"| `{name}` | `{where}` |")
        if len(rows) > top:
            L.append(f"| … | {len(rows) - top} more (--top) |")
        L.append("")
    return "\n".join(L)


def default_snaps(root: Path) -> "tuple[Path | None, Path | None, str | None]":
    """`root`'s own project store -> (old, new, note); (None, None, why) when
    nothing pairs, and the caller says so in its own words.

    NEW is `store.resolve_current`'s pick, never the newest by name: a
    dirty measurement of HEAD sorts after HEAD's clean one, so "the two
    newest" compared HEAD with an edit that may since have been reverted.
    OLD is the newest snapshot whose commit differs from NEW's. Only
    directories a snapshot pass finished (an `_index.json` inside) count —
    one mid-write is not a snapshot yet."""
    from codegraph._core.descriptor import load
    from codegraph.graph.verbs import _snapshot_base

    descriptor = load(root / "codemap.yaml", root).descriptor
    base = _snapshot_base(descriptor.project, None, root)
    dirs = [d for d in snapshot_dirs(base) if (d / "_index.json").is_file()]
    if len(dirs) < 2:
        return None, None, "fewer than two snapshots in this project's store"
    new, note = resolve_current(base, root, descriptor)
    head = load_dir(new, record=False).get("commit")
    older = [d for d in dirs if load_dir(d, record=False).get("commit") != head]
    if not older:
        return None, None, ("every snapshot in this project's store measures commit "
                            f"{str(head)[:8]}; there is no other commit to compare against")
    return older[-1], new, note


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("old_snap", nargs="?", default=None,
                    help="baseline snapshot directory (default: the project's newest of another commit)")
    sp.add_argument("new_snap", nargs="?", default=None,
                    help="newer snapshot directory (default: the project's snapshot of the current tree)")
    sp.add_argument("--md", metavar="FILE", default=None,
                    help="write the Markdown artifact here under --apply (default: stdout)")
    sp.add_argument("--top", type=int, default=25,
                    help="rows per section before truncating (default 25)")


def run(args: argparse.Namespace, root: Path, *, as_json: bool, apply: bool) -> Verdict:
    if args.old_snap is None or args.new_snap is None:
        from codegraph._core.descriptor import DescriptorError
        from codegraph.graph.verbs import SnapshotBaseUnresolved
        try:
            old, new, note = default_snaps(root)
        except (DescriptorError, SnapshotBaseUnresolved, SnapshotMissing) as e:
            return Verdict.broken(f"snapshot-diff: {e}")
        if old is None or new is None:
            return Verdict.broken(f"snapshot-diff: {note} — pass OLD_SNAP NEW_SNAP")
        if note:
            print(f"⚠ {note}", file=sys.stderr)
        args.old_snap = args.old_snap or str(old)
        args.new_snap = args.new_snap or str(new)
        if not as_json:
            print(f"snapshot-diff: defaults picked {Path(args.old_snap).name} -> "
                  f"{Path(args.new_snap).name}", file=sys.stderr)
    try:
        a_idx = load_dir(Path(args.old_snap))
        b_idx = load_dir(Path(args.new_snap))
    except SnapshotMissing as e:
        return Verdict.broken(f"snapshot-diff: {e}")

    md = markdown_diff(a_idx, b_idx, top=args.top)
    d = diff_snapshot_pairs(a_idx, b_idx)
    out = Path(args.md) if args.md else None
    if out is not None and apply:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md + "\n", encoding="utf-8")
    payload = {
        "a": a_idx.get("snapshot"), "b": b_idx.get("snapshot"),
        "added": [[n, p] for n, p in d["added"]],
        "removed": [[n, p] for n, p in d["removed"]],
        "changed": [[n, p] for n, p in d["changed"]],
        "moved": [[n, pa, pb] for n, pa, pb in d["moved"]],
        "moved_ambiguous": d["moved_ambiguous"],
        "md": str(out) if out is not None else None,
        "dry_run": out is not None and not apply}
    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    elif out is None:
        print(md)
    else:
        counts = (f"(+{len(d['added'])} -{len(d['removed'])} "
                  f"~{len(d['changed'])} moved {len(d['moved'])})")
        if apply:
            print(f"snapshot-diff: wrote {out} {counts}")
        else:
            print(f"snapshot-diff: [dry-run: nothing written — --apply writes {out}] {counts}")
    return Verdict(0, payload)


def _snapshot_diff(args: argparse.Namespace) -> int:
    """`snapshot-diff [OLD_SNAP NEW_SNAP] [--md FILE] [--top N]` — the hs-code
    verb. `--md` writes only under `--apply`."""
    return run(args, args.ctx.root, as_json=args.opts.json, apply=args.opts.apply).exit


SNAPSHOT_DIFF = Command(add_args=add_arguments, run=_snapshot_diff)
