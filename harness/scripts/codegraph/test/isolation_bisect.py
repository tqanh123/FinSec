"""hs-code test isolation-bisect -- a test red in the full suite, green alone:
find the polluter with a non-random binary search over the run's own prefix.

Port of ob-tools' `testkit.analysis.isolation_bisect` (pinned commit
`60075cb`).

    hs-code test isolation-bisect --victim 'tests/test_x.py::test_y' \\
        [--pytest-arg=-m --pytest-arg='not live'] [--candidates ids.txt]

Algorithm (each probe = one pytest run over an explicit ordered id list):
  1. victim red with the FULL prefix?      no  -> not reproducible (exit 1)
  2. victim red ALONE?                     yes -> not an isolation problem (2)
  3. smallest prefix length k that turns the victim red — binary search,
     valid because a polluter anywhere in a prefix keeps every longer prefix
     red; the last element of that prefix is the TRIGGER.
  4. [trigger, victim] red?                yes -> single polluter: the trigger.
  5. else the trigger needs a HELPER earlier in the prefix: binary search the
     smallest j with `prefix[:j] + [trigger, victim]` red; helper = prefix[j-1];
     confirm with [helper, trigger, victim].
  6. still not red -> a chain longer than two: reported UNRESOLVED with the
     minimal trigger prefix so a human can continue from there.
Probes are cached by exact order, so a repeated question costs nothing.

This verb never writes to disk — it only runs pytest probes and reports.

Exit: 0 a single polluter or a helper+trigger pair was found · 1 unresolved
(a chain longer than two, or the victim was not reproducible serially) · 2
the victim id is not among the collected ids, or a probe could not run
honestly (a hang, a pytest crash).

WHAT IT CANNOT SEE
    * Order is the COLLECTION order under `-p no:randomly`; a suite whose
      pollution depends on xdist worker assignment, on wall-clock, on
      threads/time races, or on state outside the process is not reproduced
      by replaying ids serially — the verb then says "not reproducible",
      which is NOT "no polluter".
    * Explicit-id runs skip conftest/fixture work that the FULL run triggers
      only through other files' scopes.
    * `-o addopts=` is forced so the repo's own addopts cannot corrupt the
      parse — anything they contributed must be re-passed via `--pytest-arg`.
    * Two independent polluters are found one at a time.
    * A victim that hangs is not red until `--run-timeout` kills the probe.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

import hs_family
import process_group
from hs_family.cli import Command

#: is the LAST id of this ordered list red? (ids = [..., victim])
RedProbe = Callable[[Sequence[str]], bool]


def bisect_polluter(cands: Sequence[str], victim: str, probe: RedProbe) -> dict:
    """Pure search over `probe`. Returns a result dict; `status` is one of
    found | found-pair | unresolved-chain | not-reproducible | red-alone."""
    cache: "dict[tuple[str, ...], bool]" = {}

    def red(ids: Sequence[str]) -> bool:
        key = tuple(ids)
        if key not in cache:
            cache[key] = probe(list(ids))
        return cache[key]

    def res(status: str, **kw: Any) -> dict:
        return {"status": status, "victim": victim, "candidates": len(cands),
                "probes": len(cache), **kw}

    if not cands or not red([*cands, victim]):
        return res("not-reproducible")
    if red([victim]):
        return res("red-alone")

    lo, hi = 1, len(cands)                      # hi is known red
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if red([*cands[:mid], victim]) else (mid + 1, hi)
    k = lo
    trigger = cands[k - 1]
    if red([trigger, victim]):
        return res("found", culprit=trigger, index=k - 1)

    prefix = list(cands[:k - 1])                # trigger needs a helper in here
    lo, hi = 1, len(prefix)                     # prefix+[trigger] is red (step 3)
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if red([*prefix[:mid], trigger, victim]) else (mid + 1, hi)
    helper = prefix[lo - 1] if prefix else None
    if helper is not None and red([helper, trigger, victim]):
        return res("found-pair", culprits=[helper, trigger])
    return res("unresolved-chain", trigger=trigger, trigger_index=k - 1,
               minimal_prefix_len=k)


# ── real pytest probe ────────────────────────────────────────────────────────

class ProbeError(RuntimeError):
    """The probe could not run honestly -- never a verdict."""


def _base(extra: Sequence[str]) -> list:
    return [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:randomly", *extra]


def collect_ids(victim: str, extra: Sequence[str], cwd: "Path | None",
                run_timeout: float) -> "list[str]":
    try:
        proc = process_group.run([*_base(extra), "--collect-only", "-q"], cwd=cwd,
                                 capture_output=True, text=True, timeout=run_timeout)
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"collect exceeded {run_timeout:g}s — a hang is not a verdict") from e
    ids = [ln.strip() for ln in proc.stdout.splitlines() if "::" in ln
           and " " not in ln.split("::", 1)[0]]
    if victim not in ids:
        raise ProbeError(f"victim {victim!r} not among {len(ids)} collected id(s) "
                         f"(collect rc={proc.returncode})")
    return ids[:ids.index(victim)]


def pytest_probe(victim: str, extra: Sequence[str], cwd: "Path | None",
                 run_timeout: float) -> RedProbe:
    def probe(ids: Sequence[str]) -> bool:
        with tempfile.TemporaryDirectory(prefix="hs-code-isobisect-") as tmp:
            argsfile = Path(tmp) / "ids.txt"
            argsfile.write_text("\n".join(ids) + "\n", encoding="utf-8")
            try:
                proc = process_group.run(
                    [*_base(extra), "-q", "--tb=no", "-rfE", f"@{argsfile}"],
                    cwd=cwd, capture_output=True, text=True, timeout=run_timeout)
            except subprocess.TimeoutExpired as e:
                raise ProbeError(f"probe of {len(ids)} id(s) exceeded "
                                 f"{run_timeout:g}s — a hang is not a verdict") from e
        if proc.returncode not in (0, 1):
            raise ProbeError(f"pytest rc={proc.returncode} on {len(ids)} id(s): "
                             f"{proc.stdout.strip().splitlines()[-1:]}")
        return any(ln.split(" - ", 1)[0].split(" ", 1)[-1].strip() == victim
                   for ln in proc.stdout.splitlines()
                   if ln.startswith(("FAILED ", "ERROR ")))
    return probe


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--victim", required=True,
                    help="pytest node id that is red in the suite, green alone")
    sp.add_argument("--candidates", type=Path, default=None, metavar="FILE",
                    help="ordered ids (one per line) instead of collecting")
    sp.add_argument("--pytest-arg", action="append", default=[], metavar="ARG",
                    help="extra pytest arg for collect AND probes (repeatable; "
                         "`addopts` is forced empty — re-pass what you need)")
    sp.add_argument("--run-timeout", type=float, default=1800.0,
                    help="seconds per probe before it counts as unusable (1800)")


def _render(r: dict) -> str:
    s = r["status"]
    tail = f"({r['candidates']} candidate(s), {r['probes']} probe(s))"
    if s == "found":
        return f"isolation-bisect: POLLUTER {r['culprit']}  [index {r['index']}] {tail}"
    if s == "found-pair":
        lines = [f"isolation-bisect: POLLUTER PAIR (helper then trigger) {tail}"]
        lines += [f"  {c}" for c in r["culprits"]]
        return "\n".join(lines)
    if s == "unresolved-chain":
        return (f"isolation-bisect: UNRESOLVED chain > 2 — minimal prefix "
                f"{r['minimal_prefix_len']} ends at trigger {r['trigger']} {tail}")
    if s == "not-reproducible":
        return (f"isolation-bisect: victim is GREEN with the full prefix — not "
                f"reproducible serially (NOT the same as no polluter) {tail}")
    return f"isolation-bisect: victim is red ALONE — not an isolation problem, fix it as a plain failure {tail}"


def _isolation_bisect(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    extra = list(args.pytest_arg)
    try:
        cands = ([ln.strip() for ln in args.candidates.read_text(encoding="utf-8").splitlines()
                  if ln.strip()] if args.candidates
                 else collect_ids(args.victim, extra, root, args.run_timeout))
        r = bisect_polluter(cands, args.victim,
                            pytest_probe(args.victim, extra, root, args.run_timeout))
    except (ProbeError, OSError) as e:
        raise hs_family.UsageError(f"isolation-bisect: {e}") from e
    if getattr(opts, "json", False):
        print(json.dumps(r, sort_keys=True, ensure_ascii=False))
    else:
        print(_render(r))
    return {"found": 0, "found-pair": 0, "red-alone": 2}.get(r["status"], 1)


ISOLATION_BISECT = Command(add_args=add_arguments, run=_isolation_bisect)
