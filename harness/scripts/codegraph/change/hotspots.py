"""`hs-code change hotspots` — churn × size, and a canary for the merge request.

Ported from ob-tools' `devkit.analysis.hotspots` (pinned commit `60075cb`);
the engine is unchanged except where noted, `HOTSPOTS` is the hs-code
adapter.

A file that changes constantly AND is big is where the next defect lives. This
module joins git history (how often, how much) with the graph measurement (how
big) and produces a ranked list plus a canary that a merge request can run:
"does this MR touch a top-N hotspot?".

USAGE
    hs-code change hotspots [--snapshot DIR] [--rev REV] [--since WHEN]
                            [--top N] [--changed-from FILE] [--out PATH] [--strict]

    History is read from `--repo`'s tree; the size measurement is `--snapshot`,
    else the latest in this project's own store, refused when stale (the
    shell's `--allow-stale` overrides, `--fresh` measures the on-disk tree instead). The
    report is filed beside the snapshot it was measured from as
    `change-hotspots.json` — `--out` names a directory, or a `.json` path for
    the file itself — and only under `--apply`; a dry run names the file it
    would write. A `--fresh` measurement is this call's answer, not a snapshot
    anyone chose to keep, so nothing is filed beside it: there `--apply` needs
    `--out`. `change-hotspots.json`
    is an audit trail for a human or a later manual read, the same family
    convention `report.py`'s own `report.json`/`report.html` follow -- the
    REAL signal a CI gate acts on is this command's own exit code (`--strict`
    turns a canary hit into exit 1), not a re-read of the file. No automated
    reader parses it back today; wire one if a real consumer shows up.

CHURN IS READ FROM GIT, NOT GUESSED
    `git log --numstat` gives added/deleted lines per file per commit. A commit
    that rewrites a whole file counts every line; that is churn, and it is not
    the same as "the file is bad" — the ranking says where attention is spent,
    never that a file is wrong. The log runs with `--no-renames` and
    `core.quotepath=off` so every path comes out as the literal path the
    snapshot records, whatever the machine's own git config says (a rename
    otherwise prints as `pkg/{a.py => b.py}` and a non-ASCII name as a quoted
    octal escape — neither matches any snapshot path).

THE CANARY IS ADVISORY, AND SAYS SO
    Touching a hotspot is a prompt to look, not a red gate: a legitimate change
    to a busy file must not be blocked, or the gate becomes the reason people
    route around it. `--strict` makes it exit 1 for teams that want that; the
    default prints the warning and exits 0.

WHAT IT CANNOT SEE
    * Rewritten history: rebase, squash and force-push erase churn, so a file
      that was churned before a rewrite reads as quiet. The numbers describe the
      history that SURVIVED in this clone.
    * A rename splits a file's history: `--no-renames` counts the old path's
      churn under the old path (reported as not in the snapshot) and the new
      path's from the rename on.
    * Line counts are not effort: a generated file with a thousand added lines
      outranks a hand-edited one with ten, every time.
    * Only files the snapshot knows about are ranked — a hotspot outside the
      measured roots is invisible here (it is visible to `git log` alone, which
      is why the two are joined rather than merged).
    * Deleted files are counted in the churn window but cannot be ranked (no
      current size); they are reported separately, not dropped silently.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from codegraph._core.query.engine import walk_symbols
from codegraph._core.verdict import Verdict
from hs_family.cli import Command

REPORT_NAME = "change-hotspots.json"


class HotspotError(RuntimeError):
    """Cannot measure honestly (no git, bad revision) — exit 2."""


# ── churn ────────────────────────────────────────────────────────────────────

def churn(repo: Path, *, since: "str | None" = None,
          rev: str = "HEAD") -> "dict[str, dict]":
    """Added+deleted line counts and touch counts per path. Read-only git."""
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=off", "log", "--numstat",
           "--no-renames", "--format=__C__%H", rev]
    if since:
        cmd.append(f"--since={since}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise HotspotError(f"git log failed in {repo}: {e}") from e

    stats: dict[str, dict] = defaultdict(lambda: {"commits": 0, "added": 0,
                                                  "deleted": 0})
    for line in out.splitlines():
        if line.startswith("__C__"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        # Binary files report "-" for counts; the touch still counts.
        row = stats[path]
        row["commits"] += 1
        row["added"] += int(a) if a.isdigit() else 0
        row["deleted"] += int(d) if d.isdigit() else 0
    return dict(stats)


def hotspot_score(row: dict, size: int) -> float:
    """Churn lines × measured size. Both factors matter; either alone lies.

    A tiny file edited daily is noise; a huge file never touched is scenery.
    Multiplying is what puts "big AND busy" on top, which is the actual claim.
    """
    return float(row["added"] + row["deleted"]) * float(size)


def rank(idx: dict, stats: "dict[str, dict]", *, top: int = 20) -> dict:
    """Join churn with measured size and rank. Deterministic ordering."""
    size_by_path: dict[str, int] = {}
    for _unit, m, _s, _p in walk_symbols(idx):
        size_by_path[m["path"]] = size_by_path.get(m["path"], 0) + int(
            m.get("loc") or 0)

    ranked = []
    for path, row in stats.items():
        if path not in size_by_path:
            continue
        ranked.append({"path": path, "loc": size_by_path[path],
                       "commits": row["commits"],
                       "added": row["added"], "deleted": row["deleted"],
                       "score": hotspot_score(row, size_by_path[path])})
    # Sort by score desc, then path — a stable tie-break, so two runs over the
    # same history produce the same list (a ranking that reshuffles is unusable
    # as a baseline).
    ranked.sort(key=lambda r: (-r["score"], r["path"]))
    gone = sorted(p for p in stats if p not in size_by_path)
    return {"snapshot": idx.get("snapshot"), "commit": idx.get("commit"),
            "top": ranked[:top], "ranked_total": len(ranked),
            "not_in_snapshot": gone}


# ── canary ───────────────────────────────────────────────────────────────────

def canary(idx: dict, stats: "dict[str, dict]", changed: "list[str]",
           *, top: int = 10) -> dict:
    """Which of the changed paths sit in the top-N hotspots."""
    report = rank(idx, stats, top=top)
    hot = {r["path"]: i + 1 for i, r in enumerate(report["top"])}
    hits = [{"path": p, "rank": hot[p]} for p in sorted(set(changed))
            if p in hot]
    return {"touched": len(changed), "hits": hits,
            "advisory": ("touching a hotspot is a prompt to look, not a "
                         "verdict — a legitimate change to a busy file must "
                         "not be blocked")}


# ── CLI ──────────────────────────────────────────────────────────────────────

def report_path(out: "str | None", snapshot_dir: Path) -> Path:
    """The file the report is filed as: beside the snapshot by default, in
    `out` when it names a directory, or `out` itself when it ends `.json`."""
    if out is None:
        return snapshot_dir / REPORT_NAME
    dest = Path(out)
    return dest if dest.suffix == ".json" else dest / REPORT_NAME


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--snapshot", metavar="DIR", default=None,
                    help="snapshot directory (default: the latest in this project's store)")
    sp.add_argument("--rev", default="HEAD", help="revision to read (default HEAD)")
    sp.add_argument("--since", default=None,
                    help="git --since window, e.g. '6 months ago'")
    sp.add_argument("--top", type=int, default=20, help="rank depth (default 20)")
    sp.add_argument("--changed-from", metavar="FILE", default=None,
                    help="file listing changed paths -> also run the canary")
    sp.add_argument("--out", metavar="PATH", default=None,
                    help=f"where --apply files the report — a directory, or a path ending "
                         f"in .json (default: {REPORT_NAME} beside the snapshot)")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when the canary hits (opt-in; default advisory)")


def run(args: argparse.Namespace, repo: Path, opts: Any) -> Verdict:
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph_graph_freshness import SnapshotResolveError, resolve_idx

    as_json = bool(getattr(opts, "json", False))
    apply = bool(getattr(opts, "apply", False))
    if getattr(opts, "fresh", False) and apply and args.out is None:
        return Verdict.broken("hotspots: --fresh files no report beside the measurement "
                           "it read — --apply needs --out to file the report")
    try:
        changed = None
        if args.changed_from:
            changed = [p for p in Path(args.changed_from).read_text(
                encoding="utf-8").splitlines() if p.strip()]
        descriptor = load(repo / "codemap.yaml", repo).descriptor
        _, snap_dir, idx, scratch = resolve_idx(repo, descriptor, opts, None, None, "hotspots",
                                                fresh=bool(getattr(opts, "fresh", False)),
                                                snap_dir=args.snapshot)
    except SnapshotResolveError as e:
        return Verdict.broken(str(e))
    except (OSError, DescriptorError) as e:
        return Verdict.broken(f"hotspots: {e}")
    try:
        try:
            stats = churn(repo, since=args.since, rev=args.rev)
        except HotspotError as e:
            return Verdict.broken(f"hotspots: {e}")
        report = rank(idx, stats, top=args.top)
        if changed is not None:
            report["canary"] = canary(idx, stats, changed, top=args.top)
        # a `--fresh` measurement is this call's answer: nothing is filed beside it
        dest = None if getattr(opts, "fresh", False) and args.out is None \
            else report_path(args.out, snap_dir)
        if apply and dest is not None:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                           indent=1), encoding="utf-8")
            except OSError as e:
                return Verdict.broken(f"hotspots: cannot write {dest}: {e}")
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    hit = bool(args.strict and report.get("canary", {}).get("hits"))
    if as_json:
        print(json.dumps({**report, "out": None if dest is None else str(dest),
                          "dry_run": not apply},
                         sort_keys=True, ensure_ascii=False))
        return Verdict.of(not hit, report)

    print(f"hotspots: {report['ranked_total']} ranked file(s) "
          f"(snapshot {report['snapshot']})")
    for i, r in enumerate(report["top"], 1):
        print(f"  {i:>2}. {r['score']:>10.0f}  {r['path']}  "
              f"({r['loc']} loc, {r['commits']} commits, "
              f"+{r['added']}/-{r['deleted']})")
    if report["not_in_snapshot"]:
        print(f"  {len(report['not_in_snapshot'])} churned file(s) not in the "
              f"snapshot (deleted or outside the measured roots)")
    if "canary" in report:
        c = report["canary"]
        print(f"\ncanary: {len(c['hits'])}/{c['touched']} changed file(s) are "
              f"hotspots")
        for h in c["hits"]:
            print(f"  #{h['rank']:<3} {h['path']}")
        print(f"  ({c['advisory']})")
    if dest is None:
        print("[dry-run: nothing written — --fresh files nothing beside its measurement; "
              "--apply needs --out]")
    else:
        print(f"wrote {dest}" if apply else f"[dry-run: nothing written — --apply writes {dest}]")
    return Verdict.of(not hit, report)


def _hotspots(args: argparse.Namespace) -> int:
    """`hotspots [--snapshot DIR] [--rev REV] [--since WHEN] [--top N]
    [--changed-from FILE] [--out PATH] [--strict]` — the hs-code verb."""
    return run(args, args.ctx.root, args.opts).exit


HOTSPOTS = Command(add_args=add_arguments, run=_hotspots)
