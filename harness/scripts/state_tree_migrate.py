#!/usr/bin/env python3
"""state_tree_migrate.py — one-time consolidation of the legacy `harness/state`
tree into the single project-state home, `.harness/state` (the one-tree
ruling).

Every relative path under the legacy tree gets ONE classification:

  bin_scope   — a literal name harness_paths.bin_state_dir() itself writes
                (install-state.json, ...). Describes the INSTALL, not a
                project. Stays in harness/state/ untouched.
  move        — exists only in the legacy tree. Relocated verbatim — EXCEPT
                a `trace/` continuous-chain file (`trace-YYYYMMDD.jsonl` /
                `trace-checkpoint-YYYYMMDD.json`, no shard discriminator),
                which is `conflict`-classed even with no name collision (see
                below); a per-session shard (`trace-YYYYMMDD-<disc>.jsonl`)
                is still a safe move.
  identical   — exists in both trees, byte-identical. The legacy copy is
                redundant; dropped on apply, new-tree copy is untouched.
  merge_ts    — exists in both trees as `.jsonl`, every line on both sides
                parses as JSON and carries a `ts` key, and it is NOT under a
                `trace/` directory (hash-chained files are never ts-merged —
                see below). Lines are unioned, exact-duplicate lines dropped,
                sorted by `ts`, written to the new-tree path.
  conflict    — a `trace/` file present in both trees under the SAME name
                with differing content — a hash-chained record's chain_hash
                is computed against the PREV record in file WRITE order, so
                two divergent continuations of the same chain cannot be
                reordered/concatenated by timestamp without invalidating
                chain_hash on one branch. That is a genuine fork, not a
                merge conflict this tool may resolve by guessing — it is
                reported, named, untouched, and left for a human call
                (trace_chain_merge.py's recompute, or a human pick of one
                branch as canonical). Neither copy is deleted or
                overwritten. This is the ONLY action apply() leaves both
                copies of.
  cache_mtime_resolved — exists in both trees, non-`trace/`, with differing
                content that is not safely ts-mergeable (not JSONL-with-ts
                on both sides) — a single-blob cache/state file (a JSON
                object, a bare scalar, ...) independently updated on both
                branches. Not a genuine fork: resolved deterministically by
                filesystem mtime — whichever copy was written LAST wins and
                becomes (or stays) the new-tree content; the OTHER copy is
                never silently discarded, it is archived byte-verbatim,
                read-only, under `.harness/state/state-premerge/<tag>/<rel>`
                (`tag` = "legacy" or "dot", naming which tree the archived
                loser came from) before the legacy copy is removed.

`plan()` is pure (read-only). `apply()` executes a plan: `dry_run=True` (the
default) performs every read and reports the SAME classification without
writing or deleting anything. `dry_run=False` writes/merges into the new
tree and removes the legacy copy for every non-conflict, non-bin-scope item
— never for a conflict (a trace/ fork), which apply() always leaves both
copies of untouched.

Idempotent by construction: after a real apply, every migrated relpath is
absent from the legacy tree, so a second plan() finds nothing there to
migrate (only conflicts persist — correctly, since they were never
resolved).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

# A `trace-YYYYMMDD.jsonl` or `trace-checkpoint-YYYYMMDD.json` (8-digit date,
# no shard discriminator) is part of verify_trace_chain's CONTINUOUS chain —
# it walks every such plain-named file in one directory as ONE chain linked
# by cross-day checkpoints (harness/scripts/verify_trace_chain.py
# _partition_files/_walk_files). A `trace-YYYYMMDD-<disc>.jsonl` shard is
# each its own independent chain and is NOT this class.
_CONTINUOUS_TRACE_RE = re.compile(r"^trace-(?:checkpoint-)?\d{8}\.(?:jsonl|json)$")

# Literal names harness_paths.bin_state_dir() writers use — install/cleanup
# tooling's own metadata, not project state. Kept exact (not a prefix match)
# so a project-state file that happens to share a WORD never gets skipped.
BIN_SCOPE_NAMES = frozenset({
    "install-state.json",
    "install-omitted-skills.json",
    "cleanup-backup",
    "cleanup-prev-manifest.json",
})


def _tracked_relpaths(root: Path) -> frozenset:
    """Relpaths (relative to `<root>/harness/state`) that are git-tracked —
    a real file like `.gitkeep`, kept so the directory survives with
    nothing else in it, is not state and must never move, merge or be
    removed by this tool, whatever classify() would otherwise call it.
    Fail-open (empty set) when `root` is not a git checkout: this tool
    also runs against a plain synthetic tree in tests."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false",
             "ls-files", "-z", "--", "harness/state"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return frozenset()
    prefix = "harness/state/"
    return frozenset(
        Path(p[len(prefix):]) for p in out.stdout.split("\0")
        if p.strip() and p.startswith(prefix))


def _iter_relpaths(root: Path):
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p.relative_to(root)


def _top_name(rel: Path) -> str:
    return rel.parts[0]


def _read_bytes(p: Path):
    try:
        return p.read_bytes()
    except OSError:
        return None


def _jsonl_lines_with_ts(data: bytes):
    """Parsed (line_text, ts) pairs if EVERY non-blank line is valid JSON
    carrying a `ts` key, else None (not mergeable this way)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict) or "ts" not in obj:
            return None
        out.append((line, obj["ts"]))
    return out


def classify(rel: Path, legacy_root: Path, new_root: Path,
             tracked_relpaths: frozenset = frozenset()) -> dict:
    """One classification dict for a single legacy-tree relpath. Never
    touches disk beyond reading."""
    if rel in tracked_relpaths:
        return {"rel": rel, "action": "git_tracked"}
    if _top_name(rel) in BIN_SCOPE_NAMES:
        return {"rel": rel, "action": "bin_scope"}

    legacy_path = legacy_root / rel
    new_path = new_root / rel
    legacy_bytes = _read_bytes(legacy_path)

    if not new_path.is_file():
        if _top_name(rel) == "trace" and _CONTINUOUS_TRACE_RE.match(rel.name):
            return {
                "rel": rel, "action": "conflict",
                "reason": ("a continuous-chain trace file exists only in the "
                          "legacy tree — moving it into the new tree's "
                          "trace/ directory would join it to a cross-day "
                          "checkpoint sequence it was never actually "
                          "chained to (verify_trace_chain walks every "
                          "plain-named file as one chain); needs a human "
                          "call, not a silent move"),
            }
        return {"rel": rel, "action": "move"}

    new_bytes = _read_bytes(new_path)
    if legacy_bytes == new_bytes:
        return {"rel": rel, "action": "identical"}

    if rel.suffix == ".jsonl" and _top_name(rel) != "trace":
        legacy_lines = _jsonl_lines_with_ts(legacy_bytes) if legacy_bytes else []
        new_lines = _jsonl_lines_with_ts(new_bytes) if new_bytes else []
        if legacy_lines is not None and new_lines is not None:
            merged = _merge_ts_lines(new_lines, legacy_lines)
            return {"rel": rel, "action": "merge_ts", "merged_text": merged}

    if _top_name(rel) == "trace":
        return {
            "rel": rel, "action": "conflict",
            "reason": ("both trees hold a trace/ hash-chained file of the same "
                       "name — a chain fork, not a mergeable duplicate"),
        }

    try:
        legacy_mtime = legacy_path.stat().st_mtime
    except OSError:
        legacy_mtime = 0.0
    try:
        new_mtime = new_path.stat().st_mtime
    except OSError:
        new_mtime = 0.0
    # A tie keeps the dot tree's existing content in place (nothing to write).
    winner = "legacy" if legacy_mtime > new_mtime else "new"
    return {"rel": rel, "action": "cache_mtime_resolved", "winner": winner}


def _merge_ts_lines(a, b) -> str:
    """Union of two (line_text, ts) lists: exact-duplicate LINES collapse to
    one, the rest sorted by `ts` (stable, so same-ts lines keep their
    relative arrival order). Returns the merged JSONL text, trailing
    newline included when there is any content."""
    seen = {}
    order = []
    for line, ts in a + b:
        if line not in seen:
            seen[line] = ts
            order.append(line)
    order.sort(key=lambda ln: seen[ln])
    return ("\n".join(order) + "\n") if order else ""


def plan(root, tracked_relpaths=None) -> list:
    """[classification, ...] for every relpath under `<root>/harness/state`,
    sorted by relpath. Pure/read-only. `tracked_relpaths=None` (the
    production default) auto-detects via git; pass an explicit frozenset
    to pin it (tests, or a caller that already has the list)."""
    root = Path(root)
    legacy_root = root / "harness" / "state"
    new_root = root / ".harness" / "state"
    if tracked_relpaths is None:
        tracked_relpaths = _tracked_relpaths(root)
    return [classify(rel, legacy_root, new_root, tracked_relpaths)
            for rel in _iter_relpaths(legacy_root)]


def _prune_emptied_dirs(legacy_root: Path, touched_dirs: set) -> list:
    """Remove a directory under `legacy_root` that THIS run's own removals
    left with nothing in it — never `legacy_root` itself, and never a
    directory this run did not touch (a pre-existing empty dir, or one
    still holding a git_tracked/bin_scope/conflict file survives). Walks
    upward: emptying a leaf can empty its now-childless parent too."""
    pruned = []
    frontier = list(touched_dirs)
    while frontier:
        d = frontier.pop()
        if d == legacy_root or legacy_root not in d.parents:
            continue
        if not d.is_dir() or any(d.iterdir()):
            continue
        d.rmdir()
        pruned.append(d)
        frontier.append(d.parent)
    return sorted(str(p.relative_to(legacy_root)) for p in pruned)


def apply(root, dry_run=True, tracked_relpaths=None) -> dict:
    """Execute plan(root). Returns {"moved": [...], "identical": [...],
    "merged": [...], "conflicts": [...], "bin_scope": [...],
    "cache_resolved": [...], "git_tracked": [...], "pruned_dirs": [...]} —
    each a list of relpath strings (conflicts carry their reason;
    cache_resolved carries winner + archived path).

    dry_run=True (default): read-only, identical report, no writes/deletes.
    dry_run=False: writes/merges into the new tree and removes the migrated
    legacy copy for every non-conflict, non-bin-scope, non-git_tracked item,
    then prunes any legacy subdirectory THIS run's own removals left empty.
    A conflict or a git-tracked file is NEVER written to or deleted from
    either side."""
    root = Path(root)
    legacy_root = root / "harness" / "state"
    new_root = root / ".harness" / "state"
    if tracked_relpaths is None:
        tracked_relpaths = _tracked_relpaths(root)
    items = plan(root, tracked_relpaths)
    report: dict[str, list] = {"moved": [], "identical": [], "merged": [], "conflicts": [],
                                "bin_scope": [], "cache_resolved": [], "git_tracked": [],
                                "pruned_dirs": []}
    touched_dirs = set()
    for item in items:
        rel = item["rel"]
        action = item["action"]
        if action == "git_tracked":
            report["git_tracked"].append(str(rel))
            continue
        if action == "bin_scope":
            report["bin_scope"].append(str(rel))
            continue
        if action == "conflict":
            report["conflicts"].append({"path": str(rel), "reason": item["reason"]})
            continue
        legacy_path = legacy_root / rel
        new_path = new_root / rel
        if action == "move":
            report["moved"].append(str(rel))
            if not dry_run:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                data = legacy_path.read_bytes()
                new_path.write_bytes(data)
                legacy_path.unlink()
                touched_dirs.add(legacy_path.parent)
        elif action == "identical":
            report["identical"].append(str(rel))
            if not dry_run:
                legacy_path.unlink()
                touched_dirs.add(legacy_path.parent)
        elif action == "merge_ts":
            report["merged"].append(str(rel))
            if not dry_run:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.write_text(item["merged_text"], encoding="utf-8")
                legacy_path.unlink()
                touched_dirs.add(legacy_path.parent)
        elif action == "cache_mtime_resolved":
            winner = item["winner"]
            loser_tag = "legacy" if winner == "new" else "new"
            archive_tag = "legacy" if loser_tag == "legacy" else "dot"
            archive_rel = "state-premerge/%s/%s" % (archive_tag, rel.as_posix())
            report["cache_resolved"].append(
                {"path": str(rel), "winner": winner, "archived": archive_rel})
            if not dry_run:
                loser_path = legacy_path if loser_tag == "legacy" else new_path
                archive_path = new_root.parent / "state-premerge" / archive_tag / rel
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_bytes(loser_path.read_bytes())
                archive_path.chmod(archive_path.stat().st_mode & ~0o222)
                if winner == "legacy":
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    new_path.write_bytes(legacy_path.read_bytes())
                legacy_path.unlink()
                touched_dirs.add(legacy_path.parent)
    if not dry_run and touched_dirs:
        report["pruned_dirs"] = _prune_emptied_dirs(legacy_root, touched_dirs)
    return report


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Consolidate the legacy harness/state tree into "
                    ".harness/state (the one-tree ruling). Dry-run by "
                    "default — prints the same report --apply would act on, "
                    "without writing or deleting anything.")
    ap.add_argument("--root", required=True,
                    help="repo root (the directory holding harness/ and .harness/)")
    ap.add_argument("--apply", action="store_true",
                    help="write/merge into .harness/state and remove the "
                         "migrated legacy copies; omit for a dry run")
    args = ap.parse_args(argv)
    report = apply(args.root, dry_run=not args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
