"""Behavior 9 — is this machine quiet enough to believe a number?

Only `bench` and `doctor --measure` call this. Running work on a busy machine is
legitimate; MEASURING on one is not.

Measured on this project's remote host: 56 orphaned CPU burners on a 20-core box,
oldest alive 108 minutes, load average 58 — poisoning every measurement in that
window while every other check stayed green. Background jobs a command spawns
(`... &`) are ADOPTED BY INIT when the tree is killed by PGID, so they do not die
with it.
"""
from __future__ import annotations

import time

from hs_family import RefusedError
from rops.ropslib.prog import PROG
from rops.ropslib.transport import Host

DEFAULT_MAX_LOAD = 4.0  # the fallback when the remote cannot report its cores
LOAD_PER_CORE = 0.2     # 20 cores -> 4.0, i.e. today's behavior on this host

# How long a hot host is given to COOL before the gate calls it busy. A `bench
# --repeat N` measures run 2 seconds after run 1's workers exit, and the 1-minute
# load average is still carrying the run that just ENDED -- measured here: 12.94 ->
# 8.99 -> 5.46 over five minutes with nothing of ours alive. Reading loadavg once and
# refusing made every xdist workload a suite `--repeat 2` could not finish. The budget
# is bounded on purpose: a host that stays hot for the whole of it IS busy, and still
# refuses. Raising it does not widen what passes, only how long we wait to find out.
DEFAULT_COOLDOWN_TIMEOUT = 300.0
COOLDOWN_POLL_S = 10.0


def load_average(host: Host) -> float:
    try:
        return float(host.out("cut -d' ' -f1 /proc/loadavg"))
    except ValueError:
        return -1.0


def free_kb(host: Host, path: str) -> int:
    """Free KiB on the filesystem holding `path`, or -1 if it cannot be read."""
    try:
        return int(host.out("df -Pk " + path + " | awk 'NR==2{print $4}'"))
    except (ValueError, OSError):
        return -1


SCRATCH_WARN_PCT = 85


def used_pct(host: Host, path: str) -> int:
    """Percent used on the filesystem holding `path`, or -1 when it cannot be read."""
    try:
        raw = host.out("df -Pk " + path + " | awk 'NR==2{print $5}'")
        return int(raw.strip().rstrip("%"))
    except (ValueError, OSError):
        return -1


def warn_pct(cfg) -> int:
    """The declared scratch threshold, or the default. Declarable so it can be exercised
    against a real machine without editing this file — see ci.sh's PARALLEL for what an
    unvariable knob costs."""
    try:
        return int(cfg.get("ROPS_SCRATCH_WARN_PCT", SCRATCH_WARN_PCT))
    except (TypeError, ValueError):
        return SCRATCH_WARN_PCT


def scratch_note(pct: int, free: int, path: str = "/dev/shm",
                 threshold: int | None = None) -> str | None:
    """A one-line warning when the scratch filesystem is close to full.

    `bench` clears the ground between repetitions via ROPS_BENCH_PREP; `run` does not, and
    `run` is what drives a suite by hand. Measured: three full-suite `run` passes left 19 GB
    in /dev/shm, and the NEXT run died with `OSError: [Errno 122] Disk quota exceeded` —
    surfacing as `git init` exit 128, a red test rather than a disk message. The cost lands
    on the following run, so warning at the end of this one arrives in time.

    Warns; never deletes. `run` executes what it is handed, and a verb that removes files
    on its own eventually removes someone's data.
    """
    limit = SCRATCH_WARN_PCT if threshold is None else threshold
    if pct < 0 or pct < limit:
        return None
    return (f"!! {path} is {pct}% full ({free // 1024} MiB free) after this run. Scratch "
            f"trees from a previous run are not cleared by `run` the way ROPS_BENCH_PREP "
            f"clears them for `bench` — the next run may die with 'Disk quota exceeded', "
            f"which surfaces as a failing test, not as a disk error.")


def scratch_paths(cfg, host: Host) -> list[str]:
    """Every filesystem the free-space floor is checked on: the declared scratch path,
    and the remote's default temp dir, which is where a command that does not set TMPDIR
    writes. Measured: the floor watched /dev/shm (2.8 MiB used) while /tmp filled with
    7.6 GiB of leftovers and a run died with `Errno 28` in 3115 tests."""
    declared = cfg.get("ROPS_SCRATCH_PATH", "/tmp")
    default = (host.out('printf %s "${TMPDIR:-/tmp}"') or "/tmp").strip() or "/tmp"
    return [declared] if default == declared else [declared, default]


def stray_pids(cfg, host: Host) -> list[str]:
    """Orphaned shells spinning CPU: PPID 1, state R, and the program is the login
    shell or `sh` (the tmux lane's wrapper runs as `sh <script>`). This shape is never
    legitimate — an orphaned shell in the running state is not doing work anybody
    asked for. Matched as the program's name, so `ssh` and `zshdb` are not shells."""
    name = cfg.get("ROPS_LOGIN_SHELL", "zsh").rsplit("/", 1)[-1]
    shell = "".join(c for c in name if c.isalnum() or c in "_-") or "zsh"
    out = host.out("ps -eo pid,ppid,stat,args | awk '$2==1 && $3 ~ /R/ && $4 ~ /(^|[-\\/])("
                   + shell + "|sh)$/ {print $1}'")
    return out.split()


def cores(host: Host) -> int:
    """The remote's core count, or -1 when the box cannot say. Never raises: a missing
    `nproc` is a reason to fall back, not to refuse a measurement."""
    try:
        return int(host.out("nproc 2>/dev/null || getconf _NPROCESSORS_ONLN"))
    except (ValueError, OSError):
        return -1


def ceiling(cfg, host: Host) -> float:
    """The load above which a measurement is not worth believing.

    Scaled by the REMOTE's core count, because a load average is a run-queue length and
    only means something against the number of cores that can drain it. The old absolute
    4.0 was 20% utilisation on the 20-core box this tool is used against — it refused on a
    quiet machine, costing roughly ten minutes of cooldown waits in one session — and 200%
    on a 2-core container, where it would have measured on an oversubscribed box without
    saying so. `LOAD_PER_CORE` is set so that 20-core host keeps exactly its 4.0.

    An explicit `ROPS_MAX_LOAD` is used as typed. Scaling a number an operator wrote would
    make it mean something other than what they wrote.
    """
    declared = cfg.get("ROPS_MAX_LOAD")
    if declared:
        try:
            return float(declared)
        except ValueError:
            pass
    n = cores(host)
    return DEFAULT_MAX_LOAD if n < 1 else n * LOAD_PER_CORE


def load_line(cfg, load: float, cap: float, n: int) -> str:
    """`22.39 on 20 cores = 1.12/core (ceiling 4.00 = 0.20/core)`. The bare
    `22.39 (ceiling 4.00)` read as an absolute ceiling on a 20-core box; showing the
    per-core figures says the ceiling already scales, and how far over it the box is."""
    where = f"{load:.2f} on {n} cores = {load / n:.2f}/core" if n > 0 else f"{load:.2f} (cores unknown)"
    if cfg.get("ROPS_MAX_LOAD"):
        return f"{where} (ceiling {cap:.2f}, ROPS_MAX_LOAD)"
    per_core = f" = {cap / n:.2f}/core" if n > 0 else ""
    return f"{where} (ceiling {cap:.2f}{per_core})"


def cooldown_timeout(cfg) -> float:
    try:
        return float(cfg.get("ROPS_COOLDOWN_TIMEOUT", str(DEFAULT_COOLDOWN_TIMEOUT)))
    except ValueError:
        return DEFAULT_COOLDOWN_TIMEOUT


def report(cfg, host: Host) -> int:
    """Print both rows; return how many are bad."""
    bad = 0
    strays = stray_pids(cfg, host)
    print(f"[{'ok  ' if not strays else 'FAIL'}] {'orphans':<13} "
          + ("none spinning CPU" if not strays
             else f"{len(strays)} process(es) — run `{PROG} kill --stray`"))
    bad += 0 if not strays else 1
    load, cap = load_average(host), ceiling(cfg, host)
    print(f"[{'ok  ' if load <= cap else 'FAIL'}] {'load':<13} {load_line(cfg, load, cap, cores(host))}")
    bad += 0 if load <= cap else 1
    # Same question the gate refuses on, so `doctor --measure` never says all-clear
    # about a host `bench` will turn away. Silent when no floor is declared.
    floor = cfg.get("ROPS_MIN_FREE_KB")
    if floor:
        try:
            want = int(floor)
        except ValueError:
            want = 0
        for where in scratch_paths(cfg, host):
            have = free_kb(host, where)
            ok = have < 0 or have >= want
            shown = "unreadable" if have < 0 else f"{have // 1024} MiB"
            print(f"[{'ok  ' if ok else 'FAIL'}] {'scratch':<13} {shown} free on {where} "
                  f"(floor {want // 1024} MiB)")
            bad += 0 if ok else 1
    return bad


def assert_quiet(cfg, host: Host, *, reap: bool = True) -> None:
    """Refuse to measure on a dirty machine. Without this a poisoned run reads exactly
    like a clean one.

    A hot reading is not by itself a busy host -- it can be the decaying tail of the
    measurement that just finished. So the gate WAITS for the load to fall, bounded by
    ROPS_COOLDOWN_TIMEOUT, and refuses only if it never does. What passes is unchanged;
    only how long we wait before deciding."""
    strays = stray_pids(cfg, host)
    if strays and reap:
        print(f"rops: reaping {len(strays)} orphaned process(es) before measuring")
        host.run("kill -9 " + " ".join(strays))
        time.sleep(3)

    # "Fit to measure on" has two halves, and load was the only one rops asked about.
    # Measured on a 20-core host: one run of the suite under test wrote 6.7 GiB of temp
    # trees to /dev/shm and pytest keeps the last three, so a `bench --repeat N`
    # accumulates until the filesystem fills. It does not surface as "out of space" --
    # pytest died with INTERNALERROR on a worker whose test passes alone, and the run
    # before that returned a NUMBER, 6s off its siblings, from a filesystem already 63%
    # full of its own leftovers. A wrong number that does not look wrong is what this
    # gate exists to stop.
    #
    # OFF unless ROPS_MIN_FREE_KB is declared: rops cannot know what your workload
    # writes, and guessing a floor would refuse healthy hosts. No floor, no round trip.
    floor = cfg.get("ROPS_MIN_FREE_KB")
    if floor:
        try:
            want = int(floor)
        except ValueError:
            want = 0
        for where in scratch_paths(cfg, host):
            have = free_kb(host, where)
            if not 0 <= have < want:
                continue
            raise RefusedError(
                f"rops: {have // 1024} MiB free on {where} at {host.name}, below the "
                f"declared floor of {want // 1024} MiB — NOT measuring. A run that "
                f"fills its scratch filesystem does not fail as 'out of space': it "
                f"returns a plausible number, or dies somewhere unrelated. Clear the "
                f"leftovers (a previous run's temp trees) or lower ROPS_MIN_FREE_KB "
                f"only if you know this workload writes less.")

    cap, budget = ceiling(cfg, host), cooldown_timeout(cfg)
    load = load_average(host)
    waited = 0.0
    while load > cap and waited < budget:
        print(f"rops: load {load:.2f} > ceiling {cap:.2f} on {host.name} — waiting for it "
              f"to fall ({waited:.0f}/{budget:.0f}s). A run that just ended is still in "
              f"the 1-minute average.")
        time.sleep(COOLDOWN_POLL_S)
        waited += COOLDOWN_POLL_S
        load = load_average(host)

    if load > cap:
        raise RefusedError(
            f"rops: load {load:.2f} > ceiling {cap:.2f} on {host.name} after {budget:.0f}s "
            f"of cooldown — NOT measuring. The machine is busy with something else; a "
            f"number taken now would be wrong and would read exactly like a right one. "
            f"Raise ROPS_MAX_LOAD only if this host genuinely idles higher — never to get "
            f"past a red gate.")
