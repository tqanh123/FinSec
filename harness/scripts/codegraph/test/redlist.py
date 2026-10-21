"""hs-code test redlist -- junit -> red inventory grouped by FAMILY, diffable
between runs.

Port of ob-tools' `testkit.analysis.redlist` (pinned commit `60075cb`). A red
suite of hundreds is not hundreds of problems: this verb groups red tests
into a FAMILY (kind, dist, normalised message) so a wave's burn-down is a
number, not a re-grep.

    hs-code test redlist RUN.xml --apply -o red.json                 # inventory
    hs-code test redlist RUN2.xml --against red.json --apply -o red2.json  # burn-down

`-o`/`--out` and `--write-upstream-red` write only under `--apply` — a dry
run still computes and prints the same inventory/diff/sidecar preview.

UPSTREAM-RED. A port must not "fix" what the upstream freeze itself fails —
that drifts the twin from upstream. Run the SAME lane on the upstream tree,
then

    hs-code test redlist PORT.xml --upstream-junit UP.xml \\
        --write-upstream-red upstream-red.yaml --evidence "..." --apply
    hs-code test redlist PORT.xml --upstream-red upstream-red.yaml  # every later run

the tests red in BOTH runs are the sidecar (keyed `<module leaf>::<test
name>`, with the evidence text). A run given `--upstream-red` reports them
apart: `red N = actionable A + upstream-red U`. They are still red (never
hidden, never counted fixed); only the actionable count is the burn-down
target.

Exit: 0 no NEW red (a pure burn-down, or a clean first inventory) · 1 at
least one NEW red id · 2 an input could not be read, or `--write-upstream-red`
was given without `--evidence`/`--upstream-junit`.

WHAT IT CANNOT SEE
    * Upstream-red is only as true as the upstream run: same interpreter,
      same sibling data, or the set is an environment artefact.
    * The key is the module LEAF + test name: two test files of the same
      basename in different directories collapse into one key.
    * Families are as good as the message normaliser: two causes wearing the
      same first line merge; one cause with a varying quoted name splits.
    * Only the FIRST message line is used.
    * Test ids are `junit.parse_junit`'s: a testcase with no `classname` is
      `::<name>`, one with no `name` is skipped with a warning.
    * Skips and xfails are not red. A test that vanished from the run is
      reported as vanished, never as fixed.
    * `dist` comes from the junit classname; a repo whose classnames do not
      encode the package collapses everything under the first path segment.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Callable, cast

import hs_family
import yaml_io
from test_result_readers import TestResultError

from codegraph.test.evidence import stamp_fields
from codegraph.test.inputs import dist_namer, load_json, load_yaml
from codegraph.test.junit import parse_junit
from hs_family.cli import Command

_PATH = re.compile(r"(?:/[\w.\-]+){2,}")
_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS = re.compile(r"\s+")


def normalise(message: str) -> str:
    """First line, volatile parts flattened, whitespace collapsed, capped."""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    for rx, sub in ((_ADDR, "0xADDR"), (_PATH, "<path>"), (_NUM, "N")):
        first = rx.sub(sub, first)
    return _WS.sub(" ", first).strip()[:110]


def read_run(path: "Path | str") -> "tuple[dict[str, tuple[str, str]], set[str]]":
    """One junit -> ({red_id: (kind, message)}, every_seen_id), ids as
    `junit.parse_junit` builds them for every other reader of a run."""
    rep = parse_junit(path)
    for w in rep.warnings:
        print(f"redlist: {w}", file=sys.stderr)
    red = {r.test_id: (r.outcome, r.message) for r in rep.records if r.outcome}
    return red, {r.test_id for r in rep.records}


def inventory(red: "dict[str, tuple[str, str]]", total_seen: int,
              dist_of: "Callable[[str], str]") -> dict:
    """Red tests grouped into families by (kind, dist, normalised message).
    `dist_of` takes a repo-relative path (`inputs.dist_namer`); a junit
    classname is dotted, so it is turned back into one first."""
    fams: "dict[tuple[str, str, str], list[str]]" = defaultdict(list)
    for tid, (kind, msg) in red.items():
        classname = tid.split("::", 1)[0]
        fams[(kind, dist_of(classname.replace(".", "/")), normalise(msg))].append(tid)
    rows = [{"family": f"{k}|{d}|{m}", "kind": k, "dist": d, "message": m,
             "count": len(ids), "sample": sorted(ids)[:3], "ids": sorted(ids)}
            for (k, d, m), ids in fams.items()]
    rows.sort(key=lambda r: (-cast(int, r["count"]), r["family"]))
    return {"total_red": len(red), "total_seen": total_seen, "families": rows}


def diff(prev: dict, cur_inv: dict, cur_red: "set[str]", cur_seen: "set[str]") -> dict:
    prev_ids = {i for f in prev.get("families", []) for i in f["ids"]}
    prev_count = {f["family"]: f["count"] for f in prev.get("families", [])}
    cur_count = {f["family"]: f["count"] for f in cur_inv["families"]}
    fixed = sorted(i for i in prev_ids if i in cur_seen and i not in cur_red)
    vanished = sorted(i for i in prev_ids if i not in cur_seen)
    still = sorted(prev_ids & cur_red)
    new = sorted(cur_red - prev_ids)
    deltas = sorted(
        ({"family": f, "from": prev_count.get(f, 0), "to": cur_count.get(f, 0),
          "delta": cur_count.get(f, 0) - prev_count.get(f, 0)}
         for f in set(prev_count) | set(cur_count)
         if cur_count.get(f, 0) != prev_count.get(f, 0)),
        key=lambda d: (d["delta"], d["family"]))
    return {"prev_red": len(prev_ids), "cur_red": len(cur_red), "fixed": fixed,
            "vanished": vanished, "still_red": len(still), "new_red": new,
            "family_deltas": deltas}


def test_key(tid: str) -> str:
    """`classname::name` -> `<module leaf>::<name>`: the join key between two
    trees' layouts."""
    cls, _, name = tid.partition("::")
    return f"{cls.rsplit('.', 1)[-1]}::{name}"


def shared_red(port_red: "set[str]", up_red: "set[str]") -> "list[str]":
    """Port ids whose (module leaf, test name) is red in the upstream run too."""
    up_keys = {test_key(i) for i in up_red}
    return sorted(i for i in port_red if test_key(i) in up_keys)


def split_upstream(red_ids: "set[str]", sidecar: dict) -> dict:
    keys = set(sidecar.get("tests", []))
    hit = sorted(i for i in red_ids if test_key(i) in keys)
    stale = sorted(keys - {test_key(i) for i in red_ids})
    return {"upstream_red": hit, "actionable": len(red_ids) - len(hit), "stale_entries": stale,
            "evidence": sidecar.get("evidence", ""), "freeze": sidecar.get("freeze", ""),
            "source_sha": sidecar.get("source_sha", ""), "date": sidecar.get("date", "")}


def _print_upstream(u: dict) -> None:
    n = len(u["upstream_red"])
    print(f"upstream-red: red {n + u['actionable']} = actionable {u['actionable']} + "
          f"upstream-red {n} (freeze {u['freeze'] or '?'})"
          + (f" · {len(u['stale_entries'])} sidecar entr(y/ies) no longer red in this run"
             if u["stale_entries"] else ""))


def _print_inventory(inv: dict, top: int) -> None:
    print(f"redlist: {inv['total_red']} red of {inv['total_seen']} seen, "
          f"{len(inv['families'])} family(ies)")
    for r in inv["families"][:top]:
        print(f"  {r['count']:>5}  {r['kind']:<7} {r['dist']:<22} {r['message']}")
    if len(inv["families"]) > top:
        print(f"  ... {len(inv['families']) - top} more family(ies) (see -o)")


def _print_diff(d: dict, top: int) -> None:
    print(f"burn-down: red {d['prev_red']} -> {d['cur_red']} "
          f"({d['cur_red'] - d['prev_red']:+d}) · fixed {len(d['fixed'])} · "
          f"new {len(d['new_red'])} · still {d['still_red']} · "
          f"vanished(BLIND) {len(d['vanished'])}")
    for row in d["family_deltas"][:top]:
        print(f"  {row['delta']:>+5}  {row['from']:>4} -> {row['to']:<4} {row['family']}")
    if d["new_red"]:
        print("NEW RED (regressions):")
        for i in d["new_red"][:top]:
            print(f"  {i}")


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("junit", type=Path, help="junit XML of the run to inventory")
    sp.add_argument("-o", "--out", type=Path, default=None,
                    help="write the inventory JSON here (--apply writes it; the next --against)")
    sp.add_argument("--against", type=Path, default=None, metavar="PREV.json",
                    help="a previous inventory: report burn-down + regressions")
    sp.add_argument("--top", type=int, default=15, help="rows to print (15)")
    sp.add_argument("--upstream-junit", type=Path, default=None, metavar="UP.xml",
                    help="junit of the same lane run on the upstream freeze")
    sp.add_argument("--upstream-red", type=Path, default=None, metavar="SIDECAR.yaml",
                    help="committed sidecar of tests red upstream too: reported apart from actionable red")
    sp.add_argument("--write-upstream-red", type=Path, default=None, metavar="SIDECAR.yaml",
                    help="with --upstream-junit: write the shared-red sidecar (needs --evidence; "
                         "--apply writes it)")
    sp.add_argument("--evidence", default="", help="with --write-upstream-red: how the upstream run "
                    "was made (environment, sibling data revisions, artifact paths)")
    sp.add_argument("--freeze", default="", help="with --write-upstream-red: the upstream freeze hash")


def _redlist(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    try:
        red, seen = read_run(args.junit)
        prev = load_json(args.against) if args.against else None
        dist_of = dist_namer(root)
    except (OSError, TestResultError, ValueError) as e:
        raise hs_family.UsageError(f"redlist: {e}") from e
    inv = inventory(red, len(seen), dist_of)
    result: dict = {"inventory": inv}
    if args.out:
        if apply:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(inv, indent=1, sort_keys=True), encoding="utf-8")
        result["out"] = {"path": str(args.out), "written": apply}
    d = diff(prev, inv, set(red), seen) if prev is not None else None
    if d is not None:
        result["diff"] = d
    up = args.upstream_junit
    wr = args.write_upstream_red
    sidecar = None
    try:
        if up is not None:
            up_red, _ = read_run(up)
            shared = shared_red(set(red), set(up_red))
            result["shared_with_upstream"] = {"count": len(shared), "upstream_red_total": len(up_red)}
            if wr is not None:
                if not args.evidence:
                    raise hs_family.UsageError(
                        "redlist: --write-upstream-red needs --evidence (how the upstream run was made)")
                sidecar = {"schema": "testkit.upstream_red/1", "freeze": args.freeze,
                           "evidence": args.evidence, **stamp_fields(root),
                           "tests": sorted({test_key(i) for i in shared})}
                result["sidecar"] = {"path": str(wr), "tests": len(sidecar["tests"]), "written": apply}
                if apply:
                    wr.parent.mkdir(parents=True, exist_ok=True)
                    wr.write_text(yaml_io.safe_dump(sidecar, allow_unicode=True, sort_keys=False,
                                                 width=100), encoding="utf-8")
        elif wr is not None:
            raise hs_family.UsageError("redlist: --write-upstream-red needs --upstream-junit")
        side_in = args.upstream_red
        if side_in is not None:
            sidecar = load_yaml(side_in)
        if sidecar is not None:
            result["upstream"] = split_upstream(set(red), sidecar)
    except (OSError, TestResultError, ValueError) as e:
        raise hs_family.UsageError(f"redlist: {e}") from e

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        _print_inventory(inv, args.top)
        if "shared_with_upstream" in result:
            sh = result["shared_with_upstream"]
            print(f"shared with upstream: {sh['count']} of {sh['upstream_red_total']} upstream red")
        if "sidecar" in result:
            sc = result["sidecar"]
            print(f"sidecar {sc['path']}: {sc['tests']} test(s) "
                  + ("written" if sc["written"] else "(dry-run; --apply writes)"))
        if "upstream" in result:
            _print_upstream(result["upstream"])
        if d is not None:
            _print_diff(d, args.top)
        if args.out and not apply:
            print(f"[dry-run: nothing written — --apply writes {args.out}]", file=sys.stderr)
    return 1 if (d is not None and d["new_red"]) else 0


REDLIST = Command(add_args=add_arguments, run=_redlist)
