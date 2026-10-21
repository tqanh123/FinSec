"""The tmux lane: type into a pane on the remote machine.

Slower than plain ssh (4-8 round trips instead of 1) and worth it for exactly two
things: watching a long run on the remote machine's own screen, and a job that has to
outlive this session. Two behaviors exist ONLY here, because their causes exist only
here:

1. Wait for the pane to be idle, judged by its shell's CHILD COUNT (`pgrep -cP
   <pane_pid>`). A busy pane feeds keystrokes to the running process's STDIN, not the
   shell — the command "ran" and changed nothing (hit twice). `pane_current_command`
   cannot judge this: it names the wrapper's shell, not whether the shell is the
   prompt or a running job — tmux reported `zsh` mid-pytest while `pgrep -cP` said 2.

8. Clear the line buffer (C-c) before typing, and confirm the command STARTED after
   Enter. A leftover partial line puts zsh in PS2 — Enter only adds a newline, the
   command never runs, `__RC=` never appears, and waiting the full timeout reports 124
   as if the work had run and overrun. Two different diagnoses wearing one exit code,
   which is why 125 ("never started") is a distinct code here.

This lane KEEPS `tee`: the pane itself has to see the output. That means it also keeps
the same jam measured above — a surviving background job holds the write end of the pipe, tee
never sees EOF, and the pane reads "busy forever". Here that is a SYMPTOM you can see,
not a hang: it is the only reason 56 orphaned processes were ever found.
"""
from __future__ import annotations

import sys
import time

from hs_family import RefusedError
from rops.ropslib.prog import PROG
from rops.ropslib.exec import (RC_NOT_STARTED, RC_TIMEOUT, RC_VANISHED, Result, alive_in_pgid,
                          log_dir, log_path, pgid_path, pgid_stamp, rc_marker_grep, rc_marker_shell,
                          read_result, reaped_when_stopped, shell_command, timeout_line)
from rops.ropslib.transport import Host, sq


#: `^` and `{top-left}` arrived in tmux 2.1; `doctor` names an older remote tmux.
TMUX_MIN = (2, 1)


def session(cfg) -> str:
    return cfg.get("ROPS_TMUX_SESSION", "live")


def pane(cfg) -> str:
    """The session's lowest window, its top-left pane, quoted for the remote shell.
    `:0.0` names an index a `base-index 1` tmux.conf never creates: send-keys failed
    with "can't find window: 0" while display-message quietly picked another pane.
    Quoted: under zsh `extendedglob`, a bare `^` is a glob operator."""
    return sq(f"{session(cfg)}:^.{{top-left}}")


def ensure_session(cfg, host: Host) -> bool:
    """True when this call created the session."""
    s = sq(session(cfg))
    if host.ok(f"tmux has-session -t {s} 2>/dev/null"):
        return False
    # window-size latest: the pane sizes to whichever client is attached, rather than
    # to the smallest one.
    host.run(f"tmux new-session -d -s {s} && tmux set-option -t {s} window-size latest")
    return True


def is_idle(cfg, host: Host) -> bool:
    pane_pid = host.out(f"tmux display-message -p -t {pane(cfg)} '#{{pane_pid}}'")
    if not pane_pid.isdigit():
        return False
    return host.out(f"pgrep -cP {pane_pid}") in ("", "0")


def wait_idle(cfg, host: Host, timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_idle(cfg, host):
            return
        time.sleep(2)
    # A pane that never goes idle is usually the same jam: a background job from an
    # earlier run still holds the pipe. `hs-rops kill --stray` is the way out.
    raise RefusedError(
        f"rops: tmux session {session(cfg)} on {host.name} still busy after {timeout_s}s — "
        f"not sending (look: `tmux attach -t {session(cfg)}`). "
        f"A pane that never idles usually means a background job from an earlier run "
        f"still holds the log pipe; try `{PROG} kill --stray`.")


def clear_line(cfg, host: Host) -> None:
    """Behavior 8, first half. `pane_current_command == zsh` cannot tell a clean prompt
    from a continuation prompt — both are zsh waiting for input. C-c is the only thing
    that returns the pane to a blank line."""
    host.run(f"tmux send-keys -t {pane(cfg)} C-c")
    time.sleep(1)


def wrap_tmux(cfg, command: str, out: str, label: str = "") -> str:
    """Same contract as exec.wrap but through tee, so the pane sees the output too.

    Runs the command through `exec.shell_command` (the SAME helper `wrap` uses),
    not a second splice of `prelude(cfg) && command` into whatever shell the pane's
    login shell happens to be — a fix to the shell-declaration behavior in one lane
    was previously not a fix in this one. `run_remote` writes it to a script `sh`
    runs; the pane's own shell never parses it, so no `setopt` for it here.
    """
    return ("{ ( " + pgid_stamp(out) + "; "
            + shell_command(cfg, command, label)
            + ' ) ; ' + rc_marker_shell() + ' ; } 2>&1 | tee ' + out)


def run_remote(cfg, host: Host, command: str, *, timeout_s: int,
               tail: int | None = None, label: str = "") -> Result:
    tail_n = tail if tail is not None else int(cfg.get("ROPS_TAIL", "60"))
    out = log_path(cfg, label)
    host.run(f"mkdir -p {log_dir(cfg)}")
    created = ensure_session(cfg, host)
    wait_idle(cfg, host)
    # A new pane holds no partial line, and its shell may not have set up SIGINT yet:
    # measured on homelab, C-c sent right after new-session killed the pane 20/20 (the
    # server exits with it); one second later, 0/20.
    if not created:
        clear_line(cfg, host)

    started = time.perf_counter()
    # The pane's line editor sees only `sh <script>`: typed key by key, the wrapper's
    # brackets and quotes reach whatever widgets the remote shell binds to them (an
    # autopair plugin closed every `{` and the run never started). The script holds
    # ROPS_ENV values: mktemp makes it 0600 under a name nobody can pre-place, and it
    # removes itself as it starts. Write, type and Enter in ONE ssh round trip: separate
    # calls leave a window for something else to land between them.
    body = 'rm -f -- "$0"; ' + wrap_tmux(cfg, command, out, label)
    with reaped_when_stopped(host, out):
        return _start_and_watch(cfg, host, body, out, timeout_s, tail_n, started)


def _start_and_watch(cfg, host: Host, body: str, out: str, timeout_s: int, tail_n: int,
                     started: float) -> Result:
    r = host.run('__f=$(umask 077 && mktemp "${TMPDIR:-/tmp}/rops-XXXXXX") && '
                 f"printf '%s\\n' {sq(body)} > \"$__f\" && "
                 f'tmux send-keys -t {pane(cfg)} -l -- "sh $__f" && '
                 f"tmux send-keys -t {pane(cfg)} Enter")
    if r.returncode:
        print((r.stderr or "").strip(), file=sys.stderr)
        return Result(rc=r.returncode, log=out, lines=0, tail="", host=host.name,
                      wall=time.perf_counter() - started)

    # Behavior 8, second half: tee creates the log the moment the command starts. Not
    # seeing it within seconds means Enter did NOT execute — a different failure from
    # "ran but overran", so do not burn the whole timeout to report it as one.
    for _ in range(10):
        time.sleep(1.5)
        if host.ok(f"test -f {out}"):
            break
    else:
        capture = host.out(f"tmux capture-pane -p -t {pane(cfg)} | tail -5")
        print(f"!! command did NOT start (no {out} after 15s) — the pane may hold a "
              f"partial line.\n   pane now:\n{capture}", file=sys.stderr)
        return Result(rc=RC_NOT_STARTED, log=out, lines=0, tail=capture,
                      host=host.name, wall=time.perf_counter() - started)

    return watch(host, out, timeout_s, tail_n, started)


def watch(host: Host, out: str, timeout_s: float, tail_n: int, started: float,
          poll_s: float = 3.0) -> Result:
    """Two questions, asked in this order every poll:
    1. has the rc line landed? -> finished and reported.
    2. is anyone left in the command's process group? none, and still no rc line ->
       it died without reporting (RC_VANISHED), not "still running" (RC_TIMEOUT).
    The pgid sidecar is written by the wrapper itself; until it exists only question 1
    is asked. `tee` is inside the group, so when the group is empty the log is final:
    re-ask question 1 once before calling it vanished."""
    marker = f"grep -q '{rc_marker_grep()}' {out} 2>/dev/null"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if host.ok(marker):
            return read_result(host, out, tail_n, started)
        pg = host.out(f"cat {pgid_path(out)} 2>/dev/null")
        if pg and alive_in_pgid(host, pg) == 0:
            if host.ok(marker):
                return read_result(host, out, tail_n, started)
            res = read_result(host, out, tail_n, started)
            res.tail += f"\n[rops] pgid {pg} is empty and no rc line was written — died without reporting\n"
            res.rc = RC_VANISHED
            return res
        time.sleep(poll_s)

    res = read_result(host, out, tail_n, started)
    pg = host.out(f"cat {pgid_path(out)} 2>/dev/null")
    res.tail += timeout_line(host, out, pg, alive_in_pgid(host, pg))
    res.rc = RC_TIMEOUT
    return res
