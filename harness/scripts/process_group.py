"""process_group.py -- `subprocess.run` for a child that may start children of
its own: the child runs in its own session, and on a timeout (or any exception
while waiting, a SIGTERM turned into one included) the WHOLE process group is
taken down before this returns.

`subprocess.run(..., timeout=)` kills only the direct child. Measured: a
`pytest -n` run under a timed-out declared test command left six xdist workers
reparented to init, still running 53 minutes later in a temp tree that had
already been deleted under them.

Shutdown: SIGTERM to the group, wait up to `grace` seconds for the group to
empty, then SIGKILL and wait again. The direct child is reaped as part of the
wait, so the group is gone (not merely signalled) when this returns.

WHAT IT CANNOT SEE
    * A descendant that calls setsid()/setpgid() leaves the group and is not
      reached: a daemon started on purpose survives, by design and by accident
      alike.
    * Off POSIX there are no process groups; only the direct child is killed.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Optional, Sequence, Union

_POLL_S = 0.05


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group(proc: "subprocess.Popen", pgid: int, grace: float) -> bool:
    deadline = time.monotonic() + grace
    while True:
        proc.poll()
        if not _group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


def kill_group(proc: "subprocess.Popen", grace: float = 2.0) -> None:
    """TERM the child's process group, KILL what is left after `grace`, and
    reap the child. A no-op for a child that already exited alone."""
    if not hasattr(os, "killpg"):
        proc.kill()
        proc.wait()
        return
    pgid = proc.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        if _wait_group(proc, pgid, grace):
            break
    proc.wait()


def run(args: "Union[str, Sequence[str]]", *, timeout: Optional[float],
        input: Any = None, check: bool = False, capture_output: bool = False,
        grace: float = 2.0, **kwargs: Any) -> "subprocess.CompletedProcess":
    """Drop-in for `subprocess.run` with the group shutdown above. Raises
    `subprocess.TimeoutExpired` after the group is gone, and
    `subprocess.CalledProcessError` under `check=True`."""
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    with subprocess.Popen(args, start_new_session=True, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_group(proc, grace)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(proc.args, exc.timeout, output=stdout,
                                            stderr=stderr) from None
        except BaseException:
            kill_group(proc, grace)
            raise
        retcode = proc.wait()
    if check and retcode:
        raise subprocess.CalledProcessError(retcode, proc.args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(proc.args, retcode, stdout, stderr)
