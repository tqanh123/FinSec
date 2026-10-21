"""Run one command on several hosts at once, and report a table.

Fan-out always uses the plain-ssh lane: nobody watches four panes on four machines, and
the tmux lane's per-pane serialization would defeat the point. Hosts must be distinct —
two lanes on one machine share its CPU and its services, which silently poisons any
number either one produces.
"""
from __future__ import annotations

import concurrent.futures
import sys

from rops.ropslib import hosts as hosts_mod
from rops.ropslib.exec import Result, reaps_on_stop, run_remote


def execute(args, command: str, *, label: str = "run") -> int:
    cfg = args.cfg
    targets = hosts_mod.resolve(args)

    problems = [(h, hosts_mod.check_transport(cfg, h)) for h in targets]
    bad = [(h, p) for h, p in problems if p]
    if bad:
        for _, p in bad:
            print(f"!! {p}", file=sys.stderr)
        return 2

    results: dict[str, Result] = {}
    # each host runs on a worker, where no signal lands: the stop handler is held here
    with reaps_on_stop(), concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(run_remote, cfg, h, command,
                        timeout_s=args.timeout,
                        tail=getattr(args, "tail", None),
                        label=f"{label}-{h.name}"): h
            for h in targets
        }
        for fut in concurrent.futures.as_completed(futures):
            host = futures[fut]
            try:
                results[host.name] = fut.result()
            except Exception as exc:  # one host dying must not hide the others
                print(f"!! {host.name}: {exc}", file=sys.stderr)
                results[host.name] = Result(rc=2, log="", lines=0, tail=str(exc),
                                            host=host.name, wall=0.0)

    width = max(len(n) for n in results)
    print()
    for name in sorted(results):
        r = results[name]
        mark = "ok  " if r.ok else "FAIL"
        print(f"[{mark}] {name:<{width}}  rc={r.rc:<4} {r.wall:7.1f}s  {r.log or '(no log)'}")

    failed = [n for n, r in results.items() if not r.ok]
    if failed:
        # Print the tail of every failure: the point of fan-out is comparing hosts, and
        # a summary line that says only "FAIL" makes you re-run to learn anything.
        for name in sorted(failed):
            print(f"\n--- {name} tail ---\n{results[name].tail}", end="")
        return max(results[n].rc for n in failed)
    return 0
