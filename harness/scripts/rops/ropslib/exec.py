"""Running a command remotely and getting its REAL exit code back.

Three behaviors, each one a measurement this project already lost:

3. `pipefail` around the user command. `{ CMD; echo $?; } | tee` returns TEE's rc —
   pytest red, reported green.
4. The full log stays remote; only a tail comes back. `| tail -N` on the run you need
   to analyze destroys the evidence. Hit five times, once losing all 62 FAILED lines.
5. Kill by PGID, never by pid: `kill -9 <pid>` does not take down a `uv run` tree.

And one measured constraint that shapes the plain-ssh lane:

  A background job the command spawns (`... &`) inherits the write end of the pipe,
  so `tee` never sees EOF and ssh NEVER RETURNS — measured, exit 124 under timeout,
  reproduced 1/1 with tee and 0/3 without. The plain lane therefore redirects to a
  file (`> log 2>&1 < /dev/null`) instead of piping through tee. The tmux lane keeps
  tee because the pane itself has to see the output; there, the same jam surfaces as
  "pane busy forever", which is a diagnosable symptom rather than a hang.
"""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import pathlib
import posixpath
import re
import shlex
import signal
import sys
import threading
import time
from dataclasses import dataclass

from hs_family import UsageError
from hs_family.codes import EXIT_BROKEN
from rops.ropslib.config import DEFAULT_LOG_ROOT
from rops.ropslib.transport import Host, prelude, sq

RC_TIMEOUT = 124        # started, overran --timeout
RC_NOT_STARTED = 125    # never started (tmux lane only — see verbs/../tmux)
#: The log carried no `__RC=` line at all -- the remote read never captured a real
#: exit code (a jam, a kill mid-write, a log the wrapper never got to write). A
#: BROKEN read, not a refusal: nothing here declined on purpose. Sourced from
#: hs_family.codes rather than a new literal so this and the family's own
#: `UsageError`/an unclassified crash all mean the same "2" for the same reason.
RC_UNREADABLE = int(EXIT_BROKEN)
RC_VANISHED = 126       # started, its process group is gone, and no rc line was ever written

RC_SENTINEL = "__RC"    # the one name both lanes echo and both readers grep for

_BRE_SPECIAL = set(r".[]*^$\\")


def rc_marker_shell() -> str:
    """What the wrapper prints after the command: `__RC=$?` on a line of its own.

    The leading newline is unconditional: output that ends without one (`... | tr
    "\\n" " "`) put the marker at the end of the last line, `^__RC=` matched nothing,
    and a green suite read as 2. The plain lane could ask the log for its last byte;
    the tmux lane cannot (tee may not have written it yet), and one marker for both
    keeps the two logs one shape. The cost: a log that did end its line carries one
    blank line before the marker, which takes a line of every tail."""
    return f"printf '\\n%s\\n' \"{RC_SENTINEL}=$?\""


def rc_marker_grep() -> str:
    """What every reader greps for: `^__RC=`, BRE-escaped so the sentinel may change
    to anything without silently breaking the watcher (a `[` in the name would)."""
    esc = "".join("\\" + c if c in _BRE_SPECIAL else c for c in RC_SENTINEL)
    return f"^{esc}="



@dataclass
class Result:
    rc: int
    log: str            # remote path — the full evidence
    lines: int          # line count of that log
    tail: str
    host: str
    wall: float

    @property
    def ok(self) -> bool:
        return self.rc == 0


def report(res: "Result", *, prefix: str = "") -> int:
    """Print what a run produced and hand back its rc.

    Verbs return `run_remote(...).rc`, and an earlier version of the db verb did
    exactly that — which threw the tail away and left a failing compose call printing
    NOTHING at all. Returning through here makes dropping the evidence the harder path.
    """
    print(f"{prefix}[full log: {res.host}:{res.log} ({res.lines} lines)]")
    if res.tail:
        print(res.tail, end="" if res.tail.endswith("\n") else "\n")
    return res.rc


def log_dir(cfg) -> str:
    """The remote log dir: ROPS_LOG_DIR as resolved (per lane — config._apply_lane), else
    the shared default."""
    return cfg.get("ROPS_LOG_DIR") or DEFAULT_LOG_ROOT


def log_path(cfg, label: str) -> str:
    """The run's log, named after its label with every character a shell would read
    replaced: the path is spliced bare into each far-side command (`> out`, `tail`,
    `grep`, the pgid sidecar), so `--label "full suite"` split it and the run died on a
    parse error. The label as typed still reaches the queue, quoted (`lock_prelude`)."""
    stamp = int(time.time() * 1000)
    slug = re.sub(r"[^A-Za-z0-9._@+-]", "_", label)
    suffix = f"-{slug}" if label else ""
    return f"{log_dir(cfg)}/{stamp}{suffix}.log"


def pgid_path(out: str) -> str:
    """Sidecar next to the log: the process group the wrapped command runs in."""
    return out + ".pgid"


def pgid_stamp(out: str) -> str:
    """Shell fragment the wrapper runs FIRST: record its own process group.

    `/proc/self/stat` of the `cut` child — field 5 is pgrp, and the child inherits the
    group of the job it runs in. No `$$` (that is the interactive shell, not the job)
    and no `pgrep -f` (matches the ssh session issuing it)."""
    return f'cut -d" " -f5 /proc/self/stat > {pgid_path(out)}'


def alive_in_pgid(host: Host, pgid: str) -> int:
    """How many processes on the far side still belong to `pgid`, read from
    /proc/*/stat field 5 (after the `(comm)` — comm may hold spaces or parens).

    This is the second half of the watch loop: the sentinel says "finished and
    reported"; this says "nobody left to report". A log that will never get its rc
    line (kill -9, OOM, a reboot) reads as 124 forever without it."""
    if not pgid.strip().isdigit():
        return -1
    # awk, not `set -- $line`: the remote login shell may be zsh, where an unquoted
    # expansion does NOT word-split and `$3` is empty — measured on homelab: a live
    # `sleep` in the stamped group counted as 0 and a running job read as "vanished".
    # `cat` first so a stat file that disappears mid-scan cannot abort awk (mawk exits 2).
    cmd = ("cat /proc/[0-9]*/stat 2>/dev/null | awk -v pg=" + pgid.strip()
           + " '{ sub(/.*\\) /, \"\"); if ($3 == pg) n++ } END { print n + 0 }'")
    outp = host.out(cmd)
    return int(outp) if outp.strip().isdigit() else -1


def shell_command(cfg, command: str, label: str = "") -> str:
    """`prelude(cfg) && command`, handed to a DECLARED shell (`ROPS_SHELL`, default
    `bash`) as a single-quoted `-c` argument — never spliced into whatever shell
    ssh happened to start. Shared by BOTH lanes (`wrap` here, `tmux.wrap_tmux`): a
    command written in bash syntax means something else, or nothing, under the
    remote login shell (measured on homelab: zsh, `$BASH_VERSION` empty) — an
    unquoted multi-path variable does not word-split the way a bash caller wrote
    it, and `${PIPESTATUS[0]}` reads empty (no such array in zsh). A gate list ran
    ZERO tests and still reported rc 0. `sq()` carries the text through the login
    shell UNCHANGED, so it reaches a real declared shell regardless of what shell
    ssh started — one function, so a fix here is a fix in both lanes, not one lane
    fixed and the other left with the splice this replaces.

    `pipefail` is set HERE, inside the declared shell: an option set in the outer
    subshell does not cross into a fresh `bash -c`, and `pytest ... | tail -40`
    reported `__RC=0` while pytest was red. Probed in a subshell first — an unknown
    `set -o` is a special-builtin error that ENDS a non-interactive POSIX shell (a
    `ROPS_SHELL=sh` without the option must still run the command).
    """
    shell = cfg.get("ROPS_SHELL") or "bash"
    # Grouped, on lines of its own: a `&`, `||` or trailing `#` in the command then ends
    # inside the group instead of reaching the lock/cd chain (`cmd & grep x path`
    # backgrounded the `cd` too, and the grep ran in $HOME).
    body = f"{PIPEFAIL} {lock_prelude(cfg, label=label)} && {prelude(cfg)} && {{\n{command}\n}}"
    return f"{shell} -c {sq(body)}"


#: How long a run waits for a seat in a pool another run holds, then gives up (75).
LOCK_WAIT_S = 1800
RC_LOCK_BUSY = 75
#: `ROPS_CORES`: a run's seats in the machine's `cpu` pool; this value takes the pool.
ALL_CORES = "all"

#: The far side's queue (`pool_agent.py`), sent with every locked command. Without
#: python3 there is no queue, and a run never goes ahead unqueued.
_POOL_SRC = pathlib.Path(__file__).with_name("pool_agent.py").read_text(encoding="utf-8")
_POOL_FN = r"""__rops_pool() {
  command -v python3 >/dev/null 2>&1 || { echo "[rops] python3 is not on this host: rops queues every run with it -- install python3; nothing run"; return 2; }
  python3 -c "$__ROPS_POOL" "$@"
}"""


def locked_env(cfg) -> "list[str]":
    """The `ROPS_ENV` entries `ROPS_LOCK_ENV` names as exclusive (a DB URL): runs that
    export the same value take turns. Undeclared, an env is no lock — a shared
    `PYTHONDONTWRITEBYTECODE` would otherwise serialize every lane."""
    names = set(cfg.csv("ROPS_LOCK_ENV"))
    return sorted(e for e in cfg.csv("ROPS_ENV") if e.split("=", 1)[0] in names)


def lock_names(cfg) -> "list[str]":
    """`ROPS_LOCK` (and `run --lock`): names a run queues on machine-wide, for what
    no lane dir or env names. Sorted: every run takes them in one order, so `--lock P
    --lock Q` and `--lock Q --lock P` cannot each hold one and wait on the other."""
    return sorted(set(cfg.csv("ROPS_LOCK")))


def cores(cfg) -> str:
    """`ROPS_CORES` checked: a positive integer, `all`, or "" (no cpu seat)."""
    v = (cfg.get("ROPS_CORES") or "").strip()
    if v and v != ALL_CORES and not (v.isascii() and v.isdigit() and int(v) > 0):
        raise UsageError(f"rops: ROPS_CORES/--cores must be a positive integer or "
                         f"`{ALL_CORES}`, got {v!r}")
    return v


def _segments(command: str) -> "list[list[str]]":
    """`command`'s simple commands: tokens split at every shell operator, also one
    glued to a word (`cd w;`)."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    out: "list[list[str]]" = [[]]
    for tok in lex:
        if tok and set(tok) <= set("|&;()<>"):
            out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def _runs_pytest(seg: "list[str]") -> bool:
    return any(posixpath.basename(t) in ("pytest", "py.test") for t in seg)  # `-m pytest` too


#: programs that run as one light process: a command made only of these takes one seat
_ONE_SEAT = frozenset({"find", "head", "tail", "cat", "less", "ls", "grep", "egrep", "fgrep", "rg",
                       "wc", "sed", "awk", "cut", "sort", "uniq", "tr", "echo", "printf", "stat",
                       "du", "df", "test", "[", "true", "false", "jq", "yq", "git", "readlink",
                       "realpath", "basename", "dirname", "diff", "cmp", "file", "sha256sum",
                       "md5sum", "date", "uptime", "pwd", "cd", "export", "sleep", "which", "ps",
                       "pgrep", "free", "nproc", "tee", "mkdir", "rm", "cp", "mv", "touch", "ln",
                       "tar", "zcat", "id", "hostname", "uname"})


def _one_process(seg: "list[str]") -> bool:
    """One light process: its program (past `VAR=x`) is in `_ONE_SEAT`, or a python
    running an inline `-c` line."""
    words = [t for t in seg if not ("=" in t and t.split("=", 1)[0].isidentifier())]
    if not words:
        return True
    prog = posixpath.basename(words[0])
    if prog in _ONE_SEAT:
        return True
    return prog.startswith("python") and "-c" in words[1:2]


def width(command: str) -> str:
    """The cpu seats a suite command asks for: the widest pytest-xdist `-n` /
    `--numprocesses` among its simple commands that run pytest (`0`: one process); the
    whole pool when one of them names no number (`auto`, `logical`, or none -- a suite
    of unknown width is a full one). With no pytest, one seat when every simple command
    is one light process (`find | head`, `python3 -c ...`), else the whole pool (a build
    or a runner may run wide). Another program's `-n` (`tail -n 200`) is no width."""
    try:
        every = _segments(command)
    except ValueError:
        return ALL_CORES
    segments = [seg for seg in every if _runs_pytest(seg)]
    if not segments:
        return "1" if every and all(_one_process(seg) for seg in every) else ALL_CORES
    widths = [_n_of(seg) for seg in segments]
    named = [w for w in widths if w is not None]
    if not widths or len(named) < len(widths):  # no pytest, or one naming no width
        return ALL_CORES
    return str(max(named))


def _n_of(seg: "list[str]") -> "int | None":
    """The process count one pytest command names with `-n`; None when it names none
    or a word (`auto`, `logical`) -- the whole pool."""
    for i, t in enumerate(seg):
        v = None
        if t in ("-n", "--numprocesses") and i + 1 < len(seg):
            v = seg[i + 1]
        elif t.startswith("--numprocesses="):
            v = t.split("=", 1)[1]
        elif t.startswith("-n") and len(t) > 2 and not t.startswith("--"):
            v = t[2:].lstrip("=")
        if v is not None:
            return max(int(v), 1) if v.isascii() and v.isdigit() else None
    return None


#: said once while a run queues for the whole pool only because its pytest names no width
NO_WIDTH_HINT = ("this pytest names no -n, so it holds the whole cpu pool (its own config may "
                 "run it wide); a serial run can pass --cores 1, a short one --skip-queue \"why\"")
UNKNOWN_WIDTH_HINT = ("this command names no width (no pytest -n, a program that may run wide), so "
                      "it holds the whole cpu pool; a one-process run can pass --cores 1, a short "
                      "one --skip-queue \"why\"")


def seated(cfg, command: str, *, why: str = "the command's width"):
    """`cfg` holding the cpu seats `command` runs at: its declared `ROPS_CORES` when it
    has one, else `width(command)` -- `run` and `ci run`; an alias sets its width as
    `--cores` first, and `bench` holds the whole pool. A pytest naming no width carries
    `NO_WIDTH_HINT` (`ROPS_CORES_HINT`) for the queue to say."""
    if (cfg.get("ROPS_CORES") or "").strip():
        return cfg
    w = width(command)
    cfg = cfg.override("ROPS_CORES", w, why)
    if w == ALL_CORES:
        cfg = cfg.override("ROPS_CORES_HINT",
                           NO_WIDTH_HINT if _names_no_width(command) else UNKNOWN_WIDTH_HINT, why)
    return cfg


def _names_no_width(command: str) -> bool:
    """A pytest among `command`'s simple commands names no `-n` (the whole pool by width)."""
    try:
        return any(_n_of(seg) is None for seg in _segments(command) if _runs_pytest(seg))
    except ValueError:
        return False


def lock_prelude(cfg, wait_s: int | None = None, *, label: str = "") -> str:
    """Shell that takes, for the command's whole life, a seat in each pool the run
    needs (`pool_agent.py`): the remote dir (resolved, so `~/x` and `/home/u/x` are
    one), each `ROPS_LOCK` name, the `ROPS_LOCK_ENV` values (keyed by a hash, so no
    value reaches the script or the ledger), all one seat each; then, last, its
    `ROPS_CORES` seats in the machine's `cpu` pool -- or, with `ROPS_QUEUE_SKIP`, a
    record that it runs outside that queue and why. The seats are the shell's (`$$`):
    they free when the command ends, however it ends."""
    wait = int(wait_s if wait_s is not None else (cfg.get("ROPS_LOCK_WAIT") or LOCK_WAIT_S))
    remote = cfg.get("ROPS_REMOTE_DIR", "~")
    label = sq(label or cfg.get("ROPS_LANE") or "run")
    base = f'--dir "$__L" --owner $$ --label {label}'

    def take(pool: str, extra: str = "") -> str:
        return f'__rops_pool acquire {base} --pool {pool} --wait {wait}{extra} || exit $?'

    parts = [f"__ROPS_POOL={sq(_POOL_SRC)}", _POOL_FN,
             '__L="${TMPDIR:-/tmp}/rops-locks-$(id -u)"',
             f'__D=$(cd {remote} 2>/dev/null && pwd -P || echo {sq(remote)})',
             take('"dir $__D"')]
    parts += [take(sq(f"lock {name}")) for name in lock_names(cfg)]
    env = locked_env(cfg)
    if env:
        key = hashlib.sha1(",".join(env).encode()).hexdigest()[:16]
        names = ", ".join(e.split("=", 1)[0] for e in env)
        parts.append(take(sq(f"ROPS_ENV {names} #{key}")))
    n = cores(cfg)
    skip = (cfg.get("ROPS_QUEUE_SKIP") or "").strip()
    if n and skip:
        parts.append(f"__rops_pool skip {base} --pool cpu --reason {sq(skip)} || exit $?")
    elif n:
        cap = cfg.get("ROPS_CPU_CAP").strip()
        if cap and not (cap.isascii() and cap.isdigit() and int(cap) > 0):
            raise UsageError(f"rops: ROPS_CPU_CAP must be a positive integer, got {cap!r}")
        brake = (cfg.get("ROPS_CPU_BRAKE") or "on").strip().lower()
        if brake not in ("on", "off"):
            raise UsageError(f"rops: ROPS_CPU_BRAKE must be on or off, got {brake!r}")
        hint = (cfg.get("ROPS_CORES_HINT") or "").strip()
        parts.append(take("cpu", f" --n {100000 if n == ALL_CORES else int(n)}"
                          + (f" --cap {int(cap)}" if cap else "")
                          + (" --no-brake" if brake == "off" else "")
                          + (f" --hint {sq(hint)}" if hint else "")))
    return parts[0] + "\n" + parts[1] + "\n" + " && ".join(parts[2:])


def wait_for_dir(cfg, host, remote_dir: str) -> int:
    """Block until no run holds `remote_dir` (bounded as a run is): a lane rebuild
    rewrites the tree a running suite reads."""
    at = cfg.override("ROPS_REMOTE_DIR", remote_dir, "lane")
    wait = int(cfg.get("ROPS_LOCK_WAIT") or LOCK_WAIT_S)
    r = host.run(f"bash -c {sq(lock_prelude(at, wait) + ' && true')}", timeout=wait + 60)
    if r.returncode:
        print((r.stdout or "").strip() or f"!! lane dir {remote_dir}: lock wait rc {r.returncode}")
    elif "[rops] queued:" in (r.stdout or ""):
        print((r.stdout or "").strip())
    return r.returncode


#: Turn pipefail on only where the shell has it; see `shell_command`.
PIPEFAIL = "(set -o pipefail) 2>/dev/null && set -o pipefail;"


def wrap(cfg, command: str, out: str, label: str = "") -> str:
    """The remote one-liner. `setopt local_options pipefail` is zsh; the `2>/dev/null`
    lets a bash/sh login shell fall through instead of dying on an unknown setopt —
    measured on this host: the login shell IS zsh, so pipefail is live. It covers the
    OUTER shell only; the user command's own pipes get it from `shell_command`.
    """
    return ("{ ( setopt local_options pipefail 2>/dev/null; " + pgid_stamp(out) + "; "
            + shell_command(cfg, command, label)
            + ' ) ; ' + rc_marker_shell() + ' ; } > ' + out + " 2>&1 < /dev/null")


def read_result(host: Host, out: str, tail_n: int, started: float) -> Result:
    lines = host.out(f"wc -l < {out} 2>/dev/null") or "0"
    try:
        tail = host.run(f"tail -n {tail_n} {out}").stdout
    except UnicodeDecodeError:  # a binary byte in the tail is evidence, not a reason to lose the rc
        tail = host.run(f"tail -n {tail_n} {out}", errors="replace").stdout
    rc_line = host.out(f"grep '{rc_marker_grep()}' {out} 2>/dev/null | tail -1")
    value = rc_line.partition("=")[2]
    # ASCII: `str.isdigit` accepts `²`, which `int` refuses.
    rc = int(value) if value.isascii() and value.isdigit() else RC_UNREADABLE
    return Result(rc=rc, log=out, lines=int(lines.strip() or 0), tail=tail,
                  host=host.name, wall=time.perf_counter() - started)


def run_remote(cfg, host: Host, command: str, *, timeout_s: int,
               tail: int | None = None, label: str = "") -> Result:
    """Plain-ssh lane. Blocks until the command finishes or ssh is cut at timeout."""
    tail_n = tail if tail is not None else int(cfg.get("ROPS_TAIL", "60"))
    out = log_path(cfg, label)
    host.run(f"mkdir -p {log_dir(cfg)}")
    started = time.perf_counter()
    with reaped_when_stopped(host, out):
        proc = host.run(wrap(cfg, command, out, label), capture=False, timeout=timeout_s)
    if proc.returncode == RC_TIMEOUT:
        res = read_result(host, out, tail_n, started)
        # A log that never got its RC_SENTINEL line means the command was still running when
        # ssh was cut — the work may well still be running over there. Say which.
        pg = host.out(f"cat {pgid_path(out)} 2>/dev/null")
        left = alive_in_pgid(host, pg)
        res.tail += timeout_line(host, out, pg, left)
        res.rc = RC_TIMEOUT
        return res
    return read_result(host, out, tail_n, started)


#: What a supervisor sends a client it stops: SIGTERM, then (measured, ~1.4 s later)
#: SIGKILL; SIGHUP when its terminal goes; SIGINT for Ctrl-C.
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


#: the far runs this client holds now, each `(host, out)`: a fan-out holds one per worker
_HELD: "dict[int, tuple[Host, str]]" = {}
_HELD_LOCK = threading.Lock()
_KEYS = itertools.count()
_HANDLING: "list[bool]" = []


@contextlib.contextmanager
def reaps_on_stop():
    """While held, a stop signal reaps every far run the client holds (`_HELD`), then the
    client exits as the signal asks. Signals land on the main thread only, so this
    installs there -- a fan-out holds it around its workers -- and is a no-op anywhere
    else or inside another one. A signal the client was handed ignored (`nohup`) stays
    ignored: the caller said a closed terminal must not stop the run."""
    if threading.current_thread() is not threading.main_thread() or _HANDLING:
        yield
        return
    reaped: "list[int]" = []

    def _stop(signum, _frame):
        if not reaped:
            reaped.append(signum)
            with _HELD_LOCK:
                held = list(_HELD.values())
            for host, out in held:
                host.spawn(f"bash -c {sq(reap_script(out))} >> {out} 2>&1")
                print(f"[rops] stopped by signal {signum}: taking down the far run whose group "
                      f"{host.name}:{pgid_path(out)} names (its seats free with it); "
                      f"the reaper writes to {out}", file=sys.stderr)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    saved = {s: signal.signal(s, _stop) for s in _STOP_SIGNALS
             if signal.getsignal(s) is not signal.SIG_IGN}
    _HANDLING.append(True)
    try:
        yield
    finally:
        _HANDLING.pop()
        for s, h in saved.items():
            signal.signal(s, h)


@contextlib.contextmanager
def reaped_when_stopped(host: Host, out: str):
    """While the far run is the client's, a client told to stop takes it down too.

    `ssh` runs in a session of its own (`process_group.run`), so a signal to the
    client's group never reaches it, and a far run without a terminal outlives a dropped
    connection anyway: measured, a stopped client left the far group running with its
    seats held. On the first stop signal the client starts the reaper (`reap_script`,
    its output appended to the run's log) in a session of its own -- the SIGKILL that
    follows cannot reach it -- then exits as the signal asks (`reaps_on_stop`). A run
    that overruns `--timeout` is not stopped: it goes on detached, as `timeout_line` says."""
    with _HELD_LOCK:
        key = next(_KEYS)
        _HELD[key] = (host, out)
    try:
        with reaps_on_stop():
            yield
    finally:
        with _HELD_LOCK:
            _HELD.pop(key, None)


def reap_script(out: str) -> str:
    """The far half of a stopped client: `kill` the group the wrapper stamped in `out`'s
    sidecar, with `kill <PGID>`'s own refusals (never this script's group or an
    ancestor's). No sidecar yet, or one that names no single group (`0`, `1`, empty):
    nothing is signalled -- never a group guessed at."""
    return (_SELF_PGIDS
            + f"TARGETS=$(cat {pgid_path(out)} 2>/dev/null | tr -cd 0-9)\n"
            + 'case "$TARGETS" in ""|0|1) echo "[rops] client stopped: nothing to reap, '
              'no group stamped"; exit 1;; esac\n'
            + 'echo "[rops] client stopped: taking down its run"\n'
            + _KILL_GROUPS)


def timeout_line(host: Host, out: str, pg: str, left: int) -> str:
    """The deadline passed before the rc line: how many are still running and, when
    some are, that the run goes on detached and how to wait for its own rc."""
    line = f"\n[rops] timeout: pgid={pg or '?'} still-running={left}\n"
    if left > 0:
        line += (f"[rops] detached: it goes on over there and writes its rc to {host.name}:{out}"
                 f" — wait for it: hs-rops fetch --wait {out}\n")
    return line


#: The PGIDs of the shell running a kill script and of every ancestor (login shell, sshd):
#: a pattern is in this shell's own argv, so `pgrep -f` always finds it — measured, the
#: kill took down its own group (ssh rc 255) and left the target running. Field 3 of
#: `/proc/<pid>/stat` after `(comm)` is the pgrp, field 2 the ppid.
_SELF_PGIDS = r"""SELF=" "
p=$$
while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null; do
  set -- $(sed 's/.*) //' /proc/$p/stat 2>/dev/null)
  [ -n "$3" ] || break
  SELF="$SELF$3 "
  p=$2
done
pgid_of() { set -- $(sed 's/.*) //' /proc/$1/stat 2>/dev/null); echo "$3"; }
"""

#: TERM every group in $TARGETS, give them 5 s, KILL what is left, then count with
#: `pgrep -g`. A group that is this script's own (or an ancestor's) refuses outright.
_KILL_GROUPS = r"""LIVE=
for PG in $TARGETS; do
  case "$SELF" in *" $PG "*) echo "!! refusing: pgid $PG is this kill's own group"; exit 3;; esac
  if [ "$(pgrep -g "$PG" | wc -l)" -eq 0 ]; then echo "no process in pgid $PG"; else LIVE="$LIVE $PG"; fi
done
[ -n "$LIVE" ] || exit 1
left() { n=0; for g in $LIVE; do n=$((n + $(pgrep -g "$g" | wc -l))); done; echo "$n"; }
for PG in $LIVE; do echo "pgid=$PG"; kill -TERM -- -"$PG" 2>/dev/null; done
i=0; while [ $i -lt 50 ] && [ "$(left)" -gt 0 ]; do sleep 0.1; i=$((i+1)); done
if [ "$(left)" -gt 0 ]; then
  for PG in $LIVE; do kill -KILL -- -"$PG" 2>/dev/null; done; sleep 1; echo "escalated to KILL"
fi
LEFT=$(left)
echo "remaining=$LEFT"
[ "$LEFT" -eq 0 ]
"""


def kill_script(target: str) -> str:
    """The remote bash script for `kill <target>`: all digits is a PGID; anything else
    is a `pgrep -f` pattern, resolved to the PGID of EVERY match that is not this script's
    own group or an ancestor's — a backgrounded job leaves its wrapper shell and the job
    itself carrying the same text in two groups (measured), and killing only the first
    left the job running behind `remaining=0`. PGID 0 and 1 refuse: `kill -- -1` signals
    every process the user owns. Nothing signalled exits 1, as `kill`/`pkill` do: a
    wrong number (`$!` of a wrapper shell, not the group) must not read as done."""
    if target.isdigit():
        if int(target) <= 1:
            return f"echo '!! refusing: pgid {int(target)} would signal far more than one group'; exit 3"
        return _SELF_PGIDS + f"TARGETS={int(target)}\n" + _KILL_GROUPS
    find = (f"TARGETS=\nfor pid in $(pgrep -f -- {sq(target)}); do\n"
            '  g=$(pgid_of "$pid"); [ -n "$g" ] || continue\n'
            '  case "$SELF" in *" $g "*) continue;; esac\n'
            '  case " $TARGETS " in *" $g "*) ;; *) TARGETS="$TARGETS $g";; esac\n'
            "done\n"
            'if [ -z "$TARGETS" ]; then echo "no match"; exit 1; fi\n')
    return _SELF_PGIDS + find + _KILL_GROUPS


def kill_tree(host: Host, target: str) -> tuple[int, str]:
    """Behavior 5 — kill whole process groups: `target` all digits is that PGID,
    anything else names the groups of every process `pgrep -f` matches.

    Never `pkill -f <pattern>`: the pattern appears in the far side's own command line,
    so it matches the very session issuing it and kills the connection (exit 255).
    Resolve to PGIDs first, excluding this script's own ancestry, then signal them.
    """
    r = host.run(f"bash -c {sq(kill_script(target))}", timeout=60)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
