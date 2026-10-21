"""bench — repeated measurement: quiet machine, clean slate each run, spread reported.

Three behaviors converge here, and none of them is style:

6. Reset the ground before EACH repetition. A `kill` does not return postgres
   connections; measuring on a dirty server produced three consecutive wrong
   root-cause attributions. rops does not know what YOUR ground is, so it runs
   whatever `ROPS_BENCH_PREP` names — `rops db reset`, `redis-cli flushall`,
   `rm -rf .cache`, or nothing.

7. Run >=2 repetitions and print the spread. The observed failure shape is "first run
   fast, later runs slow", so a single run proves nothing. Two is the FLOOR, not a
   target: a 50%-flaky test needs >=10 to show its base rate.

9. Refuse to measure on a busy machine (ropslib/quiet.py).
"""
from __future__ import annotations

import argparse
import subprocess

from hs_family import RefusedError, UsageError
from rops.ropslib import hosts, quiet
from rops.ropslib.exec import log_path, run_remote
from rops.ropslib.prog import PROG
from rops.ropslib.transport import sq
from rops.verbs.run import command_line


NOISE_THRESHOLD = 0.10


def tree_note(local: str, remote: str) -> str | None:
    """A one-line warning when the tree being measured is not the tree being edited.

    Reports, never refuses: benching an older revision on purpose is a bisect, and a tool
    that blocks it teaches people to pass --force by reflex. Silent when either side cannot
    answer — a destination without git would otherwise warn on every single run.
    """
    # Prefix, not equality: `--short` picks the shortest unambiguous abbreviation for EACH
    # repository, so one commit reads `074d64e2` here and `074d64e24` on the remote.
    if not local or not remote or local.startswith(remote) or remote.startswith(local):
        return None
    return (f"!! the remote is at {remote}, this tree is at {local} — measuring code you "
            f"did not just edit. Run `{PROG} sync` first, or ignore this if the older "
            f"revision is the point.")


def spread_of(walls: list[float]) -> float | None:
    """Relative spread, or None when the fastest run is at the timer's floor.

    `/usr/bin/time` reports 0.00 below its resolution, and `(hi - lo) / lo` on that is a
    ZeroDivisionError raised AFTER every measurement is already in hand — the numbers were
    taken and then thrown away with a traceback. Hit on `bench -- 'cd <dir> && true'`.
    """
    lo, hi = min(walls), max(walls)
    return None if lo <= 0 else (hi - lo) / lo


def timed_line(command: str, base: str) -> str:
    """The remote line for one repetition, exiting with the COMMAND's status.

    It used to end on `tail -1`, so every repetition reported `tail`'s 0. Measured: runs
    with 3115 errors, with 14 failures, and with no test run at all each came back
    rc=0 and each wall went into the spread."""
    return (f"/usr/bin/time -f 'WALL=%e CPU=%P' -o {base}.time "
            f"sh -c {sq(command)} > {base}.out 2>&1; __b=$?; "
            f"cat {base}.time; tail -1 {base}.out; exit $__b")


def _rev(cfg, host) -> str:
    # `host.out` does NOT carry the cd-into-the-destination prefix that `run_remote` does:
    # measured, it answers from the login directory, where `git rev-parse` returns nothing
    # and the whole check silently no-ops. The cd has to be in the command itself.
    where = cfg.get("ROPS_REMOTE_DIR", "~")
    try:
        return host.out(f"cd {where} 2>/dev/null && git rev-parse --short HEAD "
                        f"2>/dev/null || true").strip()
    except OSError:
        return ""


#: `contract.parse`: the command is taken only after `--`.
after_dashdash = "command"


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--label", required=True)
    p.add_argument("--repeat", type=int, default=None)
    p.add_argument("--timeout", type=int, default=3000)
    p.add_argument("--no-prep", action="store_true", help="skip ROPS_BENCH_PREP")
    p.add_argument("--allow-busy", action="store_true",
                   help="measure anyway (the number will be wrong and will not look it)")
    p.add_argument("command", nargs=argparse.REMAINDER)


def run(args: argparse.Namespace) -> int:
    cfg = args.cfg
    if getattr(args, "hosts", None):
        raise RefusedError(
            f"{PROG} bench: --hosts compares MACHINES, not configurations — two different "
            "boxes have two different noise floors, so the spread would be meaningless. "
            "Bench each host separately.")
    host = hosts.gate(args)[0]
    command = command_line(args.command).strip()
    if not command:
        raise UsageError(f"{PROG} bench: nothing to measure")

    repeat = args.repeat or int(cfg.get("ROPS_BENCH_REPEAT", "2"))
    if repeat < 2:
        raise UsageError(
            f"{PROG} bench: --repeat must be >= 2. One run cannot tell a result from the "
            "machine having a bad moment (behavior 7).")

    try:
        local_rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
    except OSError:
        local_rev = ""
    note = tree_note(local_rev, _rev(cfg, host))
    if note:
        print(note)

    prep = cfg.get("ROPS_BENCH_PREP")
    if not prep and not args.no_prep:
        print("!! ROPS_BENCH_PREP is not set — nothing resets the ground between runs.\n"
              "   A leaked connection or warm cache from run 1 lands inside run 2's number.\n"
              "   Declare it, or pass --no-prep to say this workload genuinely needs no reset.\n"
              "   It runs ON THE REMOTE, so name the underlying command, not a rops verb:\n"
              "     ROPS_BENCH_PREP=docker compose -p <proj> restart <svc>")
        return 2

    walls: list[float] = []
    for i in range(1, repeat + 1):
        if not args.allow_busy:
            quiet.assert_quiet(cfg, host)
        if prep and not args.no_prep:
            print(f"--- prep: {prep}")
            pr = run_remote(cfg, host, prep, timeout_s=600, tail=4, label=f"prep-{args.label}-{i}")
            if not pr.ok:
                print(pr.tail, end="")
                print(f"!! prep failed (rc={pr.rc}) — refusing to measure on an unknown ground")
                return pr.rc

        base = log_path(cfg, f"bench-{args.label}-{i}").removesuffix(".log")
        # The user command goes through `sh -c` because it is a STRING, not a program.
        # In `time prog a; b > out 2> timing` the redirect binds to the LAST clause, so
        # /usr/bin/time writes to the shell's own stderr and `timing` stays empty —
        # measured. The wall figure still reaches the wrapper's log, so the run reports
        # `wall=TIMEOUT` while its number sits one file away: misfiled, not lost.
        # The whole cpu pool, whatever the command's own width: a suite let in beside
        # the measured run lands in its wall time.
        res = run_remote(cfg.override("ROPS_CORES", "all", "bench"), host, timed_line(command, base),
                         timeout_s=args.timeout, tail=6, label=f"bench-{args.label}-{i}")
        w = host.out(f"grep -o 'WALL=[0-9.]*' {base}.time | cut -d= -f2")
        print(f"--- run {i}: rc={res.rc} wall={w or 'TIMEOUT'}  log={res.log}")
        if w and res.rc != 0:
            print(f"!! run {i} failed (rc={res.rc}) — not a measurement. It timed the part "
                  f"that ran before it broke; see {base}.out")
        elif w:
            walls.append(float(w))

    if len(walls) < 2:
        print(f"\n!! only {len(walls)} run(s) produced a number — nothing to conclude (behavior 7)")
        return 2

    lo, hi = min(walls), max(walls)
    spread = spread_of(walls)
    if spread is None:
        print(f"\n== {args.label}: {len(walls)} runs, {lo:.2f}-{hi:.2f}s — at the timer's "
              f"resolution floor, so there is no spread to report and nothing to compare ==")
        return 0
    print(f"\n== {args.label}: {len(walls)} runs, {lo:.2f}-{hi:.2f}s, "
          f"spread {hi - lo:.2f}s ({spread * 100:.1f}%) ==")
    if spread > NOISE_THRESHOLD:
        # Not "weakens" — INVALIDATES. A comparison below the noise floor is not a
        # weak result, it is not a result.
        print("!! spread >10% — any comparison finer than this is noise, not a finding")
    return 0
