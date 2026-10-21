#!/usr/bin/env python3
"""trace_chain_merge.py — reconcile the two trace/ continuous-chain branches
into ONE recomputed chain (the one-tree ruling, user-ruled option C).

The state-tree consolidation (state_tree_migrate.py) correctly refuses to
blind-move a `trace-YYYYMMDD.jsonl` continuous-chain file: verify_trace_chain
walks every plain-named file in a directory as ONE chain linked by cross-day
checkpoints, so dropping a divergent branch's day into the target directory
corrupts the walk even with no filename collision. This tool is the human
call that resolution requires — not a silent move, not "pick one branch",
but a genuine merge-and-recompute of BOTH branches' full history:

  - every continuous-chain record from BOTH `harness/state/trace` (legacy)
    and `.harness/state/trace` (new/dot) trees is read, tagged by tree of
    origin, ordered by `ts` (a stable sort with legacy-then-dot pre-ordering,
    so an exact `ts` tie sorts legacy before dot, and original file order is
    preserved within the same tree+file);
  - a byte-identical record present in both trees (the common ancestor
    before the branches diverged) collapses to ONE copy;
  - `chain_hash` is recomputed from genesis using the REAL trace_log
    `_chain_hash` function (imported, never re-implemented);
  - daily checkpoints are regenerated in the exact format trace_log itself
    writes (`trace_log._finalize_checkpoint`, reused, not hand-rolled);
  - ONE transition record is appended, carrying each old chain's final head
    hash + record count, and pointing at the premerge archive manifest;
  - both original trees' source files are preserved byte-for-byte under
    `.harness/state/trace-premerge/{legacy,dot}/`, made read-only — the
    merge is auditable and reversible, never a silent destructive rewrite;
  - per-session SHARD files (`trace-YYYYMMDD-<disc>.jsonl`, each its own
    independent chain from genesis) are moved exactly as before, never
    swept into this recompute. A same-name shard colliding across the two
    trees with DIFFERING content (two branches writing the same session's
    shard in parallel) is not a genuine fork the way a continuous-chain
    collision is — each shard already starts its own chain from genesis, so
    there is no cross-file chain state a rename could invalidate. The
    legacy copy is renamed to a free `trace-YYYYMMDD-<disc>-legacyN.jsonl`
    slot and moved in — both copies survive as independent, verifiable
    chains. Only a collision that exhausts every rename slot is reported
    as a genuine conflict (both copies then stay in place, untouched).

Idempotent: once the legacy tree's trace/ directory holds no more
continuous-chain or shard files, a second `apply()` is a pure no-op — it
never re-scans the new tree's own merged output as if it were fresh input.
"""
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import harness_paths  # noqa: E402
import hook_runtime  # noqa: E402
import trace_log  # noqa: E402

_CONTINUOUS_RE = re.compile(r"^trace-(\d{8})\.jsonl$")
_SHARD_RE = re.compile(r"^trace-\d{8}-.+\.jsonl$")
_CHECKPOINT_RE = re.compile(r"^trace-checkpoint-\d{8}\.json$")

# Escalation bound for _shard_rename_target — the shard namespace a real
# collision could exhaust is effectively unbounded (12-char discriminators),
# so this is a defensive cap, never expected to bind in practice; a genuine
# repo has never produced more than one same-name collision on one shard.
_MAX_SHARD_RENAME_ATTEMPTS = 50


def _shard_rename_target(dest_dir: Path, name: str):
    """A free filename for a legacy-side shard whose name collides with a
    DIFFERENT dot-side shard's content: `<stem>-legacy.jsonl`, escalating to
    `-legacy2`, `-legacy3`, ... if that candidate is somehow ALSO already
    taken. None once every candidate up to _MAX_SHARD_RENAME_ATTEMPTS is
    taken — the genuinely-indeterminate case the caller reports as a
    conflict instead of guessing further."""
    stem = name[:-len(".jsonl")]
    for n in range(1, _MAX_SHARD_RENAME_ATTEMPTS + 1):
        suffix = "-legacy" if n == 1 else "-legacy%d" % n
        candidate = dest_dir / (stem + suffix + ".jsonl")
        if not candidate.exists():
            return candidate
    return None


def _legacy_trace_dir(root):
    return Path(root) / "harness" / "state" / "trace"


def _new_trace_dir(root):
    return Path(root) / ".harness" / "state" / "trace"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_records(path: Path, tree_tag: str):
    """[(tree_tag, record_dict), ...] in file line order."""
    out: list[tuple[str, dict]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append((tree_tag, json.loads(line)))
        except (ValueError, TypeError):
            continue
    return out


def _canonical_key(rec: dict) -> str:
    """Content identity excluding chain_hash — two records from divergent
    branches with the SAME underlying event collapse to one copy."""
    no_chain = {k: v for k, v in rec.items() if k != "chain_hash"}
    return json.dumps(no_chain, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)


def _date_str(ts: str) -> str:
    return ts[:10].replace("-", "") if len(ts) >= 10 else "00000000"


def _archive(files_by_tag, root, dry_run):
    """Copy every original continuous-chain file verbatim into
    trace-premerge/<tag>/, chmod read-only, write a sha256 manifest."""
    premerge_dir = _new_trace_dir(root).parent / "trace-premerge"
    manifest = {"legacy": {}, "dot": {}}
    for tag, p in files_by_tag:
        dest = premerge_dir / tag / p.name
        sha = _sha256(p)
        manifest[tag][p.name] = sha
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
            mode = dest.stat().st_mode
            dest.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    if not dry_run and (manifest["legacy"] or manifest["dot"]):
        premerge_dir.mkdir(parents=True, exist_ok=True)
        (premerge_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False),
            encoding="utf-8")


def plan(root):
    """Gather every continuous-chain file from both trees. Returns
    (records, files_by_tag, shard_moves, shard_dupes, shard_renames,
    shard_conflicts) — pure/read-only. `dest.exists()` checks inside this
    function only ever read the filesystem (stat), never write."""
    legacy_dir = _legacy_trace_dir(root)
    new_dir = _new_trace_dir(root)
    records = []       # [(tag, record_dict), ...] in pre-sort order
    files_by_tag = []  # [(tag, Path), ...] continuous files, for archiving
    shard_moves = []   # [(src_path, dest_path), ...]
    shard_dupes = []   # [src_path, ...] legacy-side, byte-identical to dest
    shard_renames = []    # [(src_path, dest_path), ...] legacy renamed to a free slot
    shard_conflicts = []  # [(src_path, dest_path), ...] every rename slot taken

    for tag, d in (("legacy", legacy_dir), ("dot", new_dir)):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("trace-*.jsonl")):
            if _CONTINUOUS_RE.match(p.name):
                files_by_tag.append((tag, p))
                records.extend(_read_records(p, tag))
            elif _SHARD_RE.match(p.name) and tag == "legacy":
                # Only the LEGACY side needs moving; a shard already in the
                # new tree is already home.
                dest = new_dir / p.name
                if not dest.exists():
                    shard_moves.append((p, dest))
                elif p.read_bytes() == dest.read_bytes():
                    # A per-session shard concurrently written into BOTH
                    # trees under the same name — byte-identical, so the
                    # legacy copy is a pure redundant duplicate.
                    shard_dupes.append(p)
                else:
                    # Same name, DIFFERING content — two branches writing the
                    # same session's shard in parallel. Not a chain fork the
                    # way a continuous-chain collision is (each shard already
                    # starts its own chain from genesis), so it is safely
                    # resolved by renaming the legacy copy into a free slot
                    # rather than reported as a conflict (see module
                    # docstring). Only exhausting every rename slot is a
                    # genuine, human-call conflict.
                    renamed = _shard_rename_target(new_dir, p.name)
                    if renamed is not None:
                        shard_renames.append((p, renamed))
                    else:
                        shard_conflicts.append((p, dest))

    return records, files_by_tag, shard_moves, shard_dupes, shard_renames, shard_conflicts


def apply(root, dry_run=True):
    """Execute the merge. Returns a report dict:
    {total_records_before, total_records_after, duplicates_dropped,
     transition_record, shards_moved, conflicts}.

    dry_run=True (default): every read happens, nothing is written/removed.
    dry_run=False: writes the recomputed chain into the new tree, archives
    every original continuous file (read-only, verbatim) under
    trace-premerge/, removes the now-redundant originals from BOTH trees'
    trace/ directories, and moves any legacy-only shard as before."""
    root = Path(root)
    (records, files_by_tag, shard_moves, shard_dupes, shard_renames,
     shard_conflicts) = plan(root)
    # Whether the LEGACY tree contributed any continuous-chain content this
    # call — `records` always also includes the DOT tree's own (possibly
    # prior-merged) content, so this, not `bool(records)`, is what decides
    # whether there is genuinely fresh input to merge.
    legacy_has_fresh_records = any(tag == "legacy" for tag, _ in records)
    report = {
        "total_records_before": len(records) if legacy_has_fresh_records else 0,
        "total_records_after": 0,
        "duplicates_dropped": 0,
        "transition_record": None,
        "shards_moved": [],
        "shards_renamed": [],
        "conflicts": [],
    }

    # Shard housekeeping is independent of the continuous-chain merge below,
    # and safe/cheap to repeat every call — run it unconditionally.
    for src, dest in shard_moves:
        report["shards_moved"].append(src.name)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            src.unlink()

    for src in shard_dupes:
        if not dry_run:
            src.unlink()

    for src, dest in shard_renames:
        report["shards_renamed"].append({"from": src.name, "to": dest.name})
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            src.unlink()

    for src, dest in shard_conflicts:
        report["conflicts"].append({
            "path": src.name,
            "reason": ("a same-name per-session shard exists in both trees "
                       "with differing content and every trace-<date>-<disc>"
                       "-legacyN.jsonl rename slot up to %d is already taken "
                       "— genuinely indeterminate, not something this tool "
                       "may resolve by guessing further; needs a human "
                       "call" % _MAX_SHARD_RENAME_ATTEMPTS),
        })

    if not legacy_has_fresh_records:
        # Idempotent for the continuous chain: nothing NEW from the legacy
        # tree to merge (already merged by a prior run, or never had any).
        # A shard_conflict, by design, NEVER resolves itself — it stays in
        # plan()'s output on EVERY future call, which used to defeat
        # idempotency entirely (a coarse "legacy tree still has ANY file"
        # pre-check treated that permanent leftover as fresh work,
        # re-scanning and re-merging the dot tree's own prior OUTPUT as if
        # it were input, then crashing trying to re-archive an
        # already-archived, now read-only file). The conflict is still
        # reported above, every run — it must never go silent.
        return report

    # Per-tree head (last ORIGINAL chain_hash seen, in file order) + record
    # count, computed from the pre-sort (per-tree, per-file, in-order) list —
    # this is what the transition record cites, independent of the merge sort.
    per_tree_last = {"legacy": None, "dot": None}
    per_tree_count = {"legacy": 0, "dot": 0}
    for tag, rec in records:
        per_tree_count[tag] += 1
        h = rec.get("chain_hash")
        if h:
            per_tree_last[tag] = h

    seen = set()
    deduped = []
    for tag, rec in records:
        key = _canonical_key(rec)
        if key in seen:
            report["duplicates_dropped"] += 1
            continue
        seen.add(key)
        deduped.append(rec)
    deduped.sort(key=lambda r: r.get("ts") or "")

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    transition = {
        "ts": now.isoformat(),
        "actor": hook_runtime.resolve_actor(),
        "hook": "trace_chain_merge",
        "event": "state_tree_merge",
        "reason": "state-tree merge",
        "legacy_head": per_tree_last["legacy"],
        "legacy_records": per_tree_count["legacy"],
        "dot_head": per_tree_last["dot"],
        "dot_records": per_tree_count["dot"],
        "premerge_manifest": "trace-premerge/manifest.json",
    }
    deduped.append(transition)
    report["transition_record"] = transition
    report["total_records_after"] = len(deduped)

    hashing_started = False
    prev = ""
    out_records = []
    for rec in deduped:
        had_hash = bool(rec.get("chain_hash")) or rec is transition
        if not hashing_started and had_hash:
            hashing_started = True
        rec = dict(rec)
        if hashing_started:
            rec.pop("chain_hash", None)
            h = trace_log._chain_hash(prev, rec)
            rec["chain_hash"] = h
            prev = h
        else:
            rec.pop("chain_hash", None)
        out_records.append(rec)

    if dry_run:
        return report

    # Archive + delete originals BEFORE writing merged output — deleting a
    # source path can never clobber a not-yet-written destination this way.
    _archive(files_by_tag, root, dry_run=False)
    for _tag, _p in files_by_tag:
        try:
            _p.unlink()
        except OSError:
            pass
    legacy_dir = _legacy_trace_dir(root)
    for _p in (sorted(legacy_dir.glob("trace-checkpoint-*.json"))
              if legacy_dir.is_dir() else []):
        try:
            _p.unlink()
        except OSError:
            pass

    by_date = {}
    for rec in out_records:
        by_date.setdefault(_date_str(rec.get("ts") or ""), []).append(rec)

    new_dir = _new_trace_dir(root)
    new_dir.mkdir(parents=True, exist_ok=True)
    cutover_written = False
    cutover_ts = None
    for rec in out_records:
        if rec.get("chain_hash"):
            cutover_ts = rec.get("ts")
            break
    for date_str in sorted(by_date):
        day_records = by_date[date_str]
        fpath = new_dir / ("trace-%s.jsonl" % date_str)
        fpath.write_text(
            "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                     for r in day_records) + "\n",
            encoding="utf-8")
        cutover_arg = None
        if not cutover_written and any(r.get("chain_hash") for r in day_records):
            cutover_arg = cutover_ts
            cutover_written = True
        trace_log._finalize_checkpoint(new_dir, date_str, fpath, chain_cutover_ts=cutover_arg)

    return report


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    report = apply(args.root, dry_run=not args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
