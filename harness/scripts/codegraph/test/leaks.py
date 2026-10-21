"""hs-code test leaks -- DB-leak inventory from the run's own evidence.

Port of ob-tools' `testkit.analysis.leaks` (pinned commit `60075cb`). A DB
leak guard fails (or warns) AT the test that leaked state, with a fixed
message:

    DB state leaked by <nodeid>:
      [<check>] <message>

This verb reads that message out of a run (junit and/or a plain pytest log)
and reports it as an inventory: by check, by dist, by test.

    hs-code test leaks RUN.xml [more.xml ...] [--log pytest.log] --apply -o leaks.json
    hs-code test leaks RUN2.xml --against leaks.json          # burn-down / new leaks

`-o`/`--out` writes only under `--apply`. `--against` splits the previous
inventory into fixed (no longer leaking), still leaking and NEW.

Exit: 0 no NEW leak (and `--fail-on-leak` was not given, or nothing leaks) ·
1 a NEW leak against `--against`, or `--fail-on-leak` and something leaks ·
2 no input at all, or an input could not be read.

WHAT IT CANNOT SEE
    * Zero leak lines is NOT proof the guard ran: a dropped plugin, an env
      var turning the guard off, are silent. Pair with `pytest --markers`.
    * Opted-out tests never produce a line.
    * A leak in a session-scoped fixture is blamed on whichever test tears
      down first after it.
    * `dist` comes from the node id / classname path; anything else
      collapses under its first segment.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable

import hs_family
from test_result_readers import TestResultError, parse_xml

from codegraph.test.inputs import dist_namer, load_json
from hs_family.cli import Command

_HEAD = re.compile(r"DB state leaked by (\S+?):\s*$")
_CHECK = re.compile(r"^\s*\[([\w\-.]+)\]\s*(.*)$")


def parse_text(text: str) -> "dict[str, list[tuple[str, str]]]":
    """{nodeid: [(check, message)]} from any text carrying the guard's message."""
    out: "dict[str, list[tuple[str, str]]]" = defaultdict(list)
    node = None
    for line in text.splitlines():
        h = _HEAD.search(line)
        if h:
            node = h.group(1)
            continue
        if node is None:
            continue
        c = _CHECK.match(line)
        if c:
            out[node].append((c.group(1), c.group(2).strip()[:200]))
        elif line.strip():
            node = None                      # the block ended
    return out


def read_junit(path: Path) -> "dict[str, list[tuple[str, str]]]":
    root = parse_xml(path)
    chunks: "list[str]" = []
    for el in root.iter():
        if el.tag in ("failure", "error", "system-out", "system-err"):
            chunks += [el.get("message") or "", el.text or ""]
    return parse_text("\n".join(chunks))


def inventory(found: "dict[str, list[tuple[str, str]]]",
              dist_of: "Callable[[str], str]") -> dict:
    """Leaks counted by check and by dist; `dist_of` takes the node id's file
    path (`inputs.dist_namer`)."""
    by_check: "dict[str, set[str]]" = defaultdict(set)
    by_dist: "dict[str, set[str]]" = defaultdict(set)
    for node, items in found.items():
        by_dist[dist_of(node.split("::", 1)[0])].add(node)
        for chk, _msg in items:
            by_check[chk].add(node)
    return {"schema": "testkit.leaks/1", "tests": len(found),
            "by_check": {k: len(v) for k, v in sorted(by_check.items(), key=lambda kv: (-len(kv[1]), kv[0]))},
            "by_dist": {k: len(v) for k, v in sorted(by_dist.items(), key=lambda kv: (-len(kv[1]), kv[0]))},
            "leaks": {n: [{"check": c, "message": m} for c, m in dict.fromkeys(items)]
                      for n, items in sorted(found.items())}}


def diff(prev: dict, cur: dict) -> dict:
    p, c = set(prev.get("leaks", {})), set(cur["leaks"])
    return {"fixed": sorted(p - c), "still": sorted(p & c), "new": sorted(c - p)}


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("junit", type=Path, nargs="*", help="junit XML run(s) to read")
    sp.add_argument("--log", type=Path, action="append", default=[], metavar="FILE",
                    help="plain pytest log (the warn mode prints the message there); repeatable")
    sp.add_argument("-o", "--out", type=Path, default=None,
                    help="write the inventory JSON here (--apply writes it)")
    sp.add_argument("--against", type=Path, default=None, metavar="LEAKS.json",
                    help="previous inventory: report fixed / still / NEW")
    sp.add_argument("--fail-on-leak", action="store_true", help="exit 1 when any test leaks")
    sp.add_argument("--top", type=int, default=15, help="rows per section (default 15)")


def _leaks(args: argparse.Namespace) -> int:
    opts = args.opts
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    if not args.junit and not args.log:
        raise hs_family.UsageError("leaks: give at least one junit XML or --log")
    found: "dict[str, list[tuple[str, str]]]" = defaultdict(list)
    try:
        for j in args.junit:
            for n, items in read_junit(j).items():
                found[n] += items
        for lg in args.log:
            for n, items in parse_text(lg.read_text(encoding="utf-8", errors="replace")).items():
                found[n] += items
        prev = load_json(args.against) if args.against else None
        dist_of = dist_namer(args.ctx.root)
    except (OSError, TestResultError, ValueError) as exc:
        raise hs_family.UsageError(f"leaks: cannot read input: {exc}") from exc
    inv = inventory(found, dist_of)
    d = diff(prev, inv) if prev is not None else None
    if args.out and apply:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(inv, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.out:
        inv = {**inv, "out": {"path": str(args.out), "written": apply}}
    if d is not None:
        inv["against"] = d

    if as_json:
        print(json.dumps(inv, sort_keys=True, ensure_ascii=False))
    else:
        print(f"leaks: {inv['tests']} test(s) leaked DB state")
        for k, v in list(inv["by_check"].items())[:args.top]:
            print(f"  check {k:<28} {v}")
        for k, v in list(inv["by_dist"].items())[:args.top]:
            print(f"  dist  {k:<28} {v}")
        if not inv["tests"]:
            print("  0 leak lines — NOT proof the guard ran (the guard may be off, "
                  "dropped, or every test opted out); check `pytest --markers`")
        if d is not None:
            print(f"  against {args.against.name}: fixed {len(d['fixed'])} · "
                  f"still {len(d['still'])} · NEW {len(d['new'])}")
            for n in d["new"][:args.top]:
                print(f"    NEW {n}")
    red = bool(d and d["new"]) or (args.fail_on_leak and inv["tests"] > 0)
    return 1 if red else 0


LEAKS_VERB = Command(add_args=add_arguments, run=_leaks)
