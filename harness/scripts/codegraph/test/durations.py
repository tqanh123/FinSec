"""hs-code test durations -- junit durations: top expensive, delta, slowdown
trend, history cuts; `--store` builds a `.test_durations` shard file + LPT
shard preview.

Port of ob-tools' `testkit.durations.durations` (pinned commit `60075cb`),
minus `--rops` and `--run`. This verb READS what a run left: running a suite,
here or on another machine, is `hs-rops`' job.

    hs-code test durations RUN.xml [RUN2.xml ...] [--top N]
    hs-code test durations RUN.xml -o durations.jsonl --apply
    hs-code test durations --store .test_durations --apply RUN.xml [...]

`--apply` is the only way anything lands on disk (`-o`/`--out`'s jsonl dump,
`--store`'s shard file) — a dry run
still computes and prints everything, it just does not write.

Exit: 0 report/store computed cleanly · 2 no junit file given, or one could
not be parsed.

WHAT IT CANNOT SEE
    * Tests that never ran in CI (local-only, deselected) are absent from the
      junit file — an empty cell here is BLIND, not "no test".
    * `time` missing on a testcase is kept as None and counted as a blind
      record; it is never silently zero.
    * Outcome is ignored on purpose (durations only).
    * Suite-level wall time (fixtures, session setup) is not attributed to
      any test; sum-of-times < suite time does not mean missing tests.
    * Non-pytest junit (jest & friends) is best-effort.
    * Duplicate test_id within one run is reported as a warning, not merged.
    * `--store`: censoring is a threshold guess; the shard preview is TEST
      TIME ONLY (no per-job startup cost).
    * `--store`: lane identity is the FILENAME convention `<lane>-rep<N>.xml`;
      renaming files merges lanes wrongly, silently.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import hs_family

from codegraph.test.junit import ParseReport, dump_jsonl, parse_junit, render_report
from codegraph.test.junit import report as build_report
from codegraph.test.shard_store import (DEFAULT_TIMEOUT_S, censored_ids, lane_medians,
                                        lpt_preview, merge_lanes)
from hs_family.cli import Command


def _store(args: argparse.Namespace, runs: "list[ParseReport]", root: Path,
          apply: bool, as_json: bool) -> int:
    merged = merge_lanes(lane_medians(runs))
    lanes = lane_medians(runs)
    censored = censored_ids(merged, args.timeout)
    ns = sorted(int(x) for x in str(args.shards).split(","))
    shards = {n: lpt_preview(merged, n) for n in ns}
    wrote = None
    if apply:
        args.store.parent.mkdir(parents=True, exist_ok=True)
        args.store.write_text(json.dumps(merged, indent=0, sort_keys=True), encoding="utf-8")
        wrote = str(args.store)

    if not as_json:
        serial = sum(merged.values())
        print(f"durations: {len(merged)} tests ("
              + ", ".join(f"{k}={len(v)}" for k, v in sorted(lanes.items()))
              + f"), serial {serial:.0f}s -> "
              + (wrote or f"[dry-run: --apply writes {args.store}]"))
        if censored:
            print(f"durations: {len(censored)} CENSORED @>={args.timeout:g}s "
                  "(hung-then-killed upper bounds, kept in the file):")
            for t in censored[:10]:
                print(f"  {t}")
        print("shard preview (TEST TIME ONLY — add your own per-job startup):")
        for n, pv in shards.items():
            print(f"  N={n}: max={pv['max']}s min={pv['min']}s spread={pv['spread']}s")
    else:
        print(json.dumps({
            "verb": "test durations",
            "tests": len(merged), "serial_seconds": round(sum(merged.values()), 1),
            "censored": censored, "shards": shards, "wrote": wrote,
            "dry_run": not apply},
            sort_keys=True, ensure_ascii=False))
    return 0


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("junit", nargs="*",
                    help="junit XML file(s), one per run (lane files follow "
                         "'<lane>-rep<N>.xml' for --store)")
    sp.add_argument("--top", type=int, default=10, help="top-N rows (default 10)")
    sp.add_argument("-o", "--out", type=Path, default=None,
                    help="also dump durations.jsonl to this path (--apply writes it)")
    sp.add_argument("--dump-only", action="store_true",
                    help="only write the jsonl dump, no report")
    sp.add_argument("--store", type=Path, default=None, metavar="PATH",
                    help="write a `.test_durations` JSON (median per lane) "
                         "+ LPT shard preview + censored list (--apply writes it)")
    sp.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="censor cap seconds: a time at/over it minus 0.5s is a "
                         "hung-then-killed upper bound (180)")
    sp.add_argument("--shards", default="2,4,6,8",
                    help="comma list of shard counts for the LPT preview")


def _durations(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    store = args.store
    if not args.junit:
        raise hs_family.UsageError("durations: need at least one junit file")

    runs: "list[ParseReport]" = []
    for path in args.junit:
        try:
            runs.append(parse_junit(path))
        except (OSError, ValueError) as e:
            raise hs_family.UsageError(f"durations: {e}") from e

    if store is not None:
        return _store(args, runs, root, apply, as_json)

    if args.out and apply:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as fh:
            dump_jsonl(runs, fh)
    if args.dump_only:
        return 0

    data = build_report(runs, top=args.top)
    if args.out:
        data["out"] = str(args.out)
        data["dry_run"] = not apply
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        render_report(data)
        if args.out and not apply:
            print(f"[dry-run: nothing written — --apply writes {args.out}]")
    return 0


DURATIONS = Command(add_args=add_arguments, run=_durations)
