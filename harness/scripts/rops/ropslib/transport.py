"""ssh plumbing shared by both lanes.

Behavior 2 lives here: `ssh host tmux send-keys -l -- "$LINE"` does NOT preserve argv
quoting — the remote shell re-splits and send-keys rejoins the pieces WITHOUT spaces,
giving `parse error near '}'`. Every string handed to a remote shell goes through sq().
"""
from __future__ import annotations

import pathlib
import shlex
import subprocess
import sys

# `process_group` is the shared door at `harness/scripts/process_group.py`, a
# sibling of this package's own root (`harness/scripts/rops/`) -- reached the same
# way `hs_family` already is (`from hs_family import UsageError` elsewhere in this
# tree): `harness/scripts` is on `sys.path` for any real rops entry point
# (`hs_rops.py` puts it there), so this needs no path surgery of its own outside a
# bare test run, which is the one caller that has not already done it.
_SCRIPTS = pathlib.Path(__file__).resolve().parents[2]  # ropslib -> rops -> scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import process_group  # noqa: E402

DEFAULT_TIMEOUT = 30


def sq(text: str) -> str:
    """Single-quote wrap for a POSIX shell. Behavior 2."""
    return "'" + text.replace("'", "'\\''") + "'"


class Host:
    """One ssh destination. Holds no global state: fan-out builds N of these."""

    def __init__(self, name: str, ssh_opts: str = ""):
        self.name = name
        self.opts = shlex.split(ssh_opts)

    def __repr__(self) -> str:
        return f"Host({self.name})"

    def _argv(self) -> list[str]:
        return ["ssh", *self.opts, self.name]

    def run(self, remote_cmd: str, *, capture: bool = True,
            timeout: int | None = DEFAULT_TIMEOUT,
            errors: str = "strict") -> subprocess.CompletedProcess:
        """One short command. `remote_cmd` reaches the remote login shell verbatim.

        stdin is closed: an interactive prompt on the far side would otherwise hang
        a command nobody is watching. `errors` is how undecodable output bytes are
        read (`"replace"` for a reader of arbitrary files).

        Routed through `process_group.run` rather than a bare `subprocess.run`:
        the LOCAL child is `ssh` itself, and a destination whose `~/.ssh/config`
        sets a `ProxyCommand` makes `ssh` fork a child of its own to reach it —
        one `subprocess.run(timeout=)` cannot see and does not kill, so it
        outlives a timed-out run. `process_group.run` starts `ssh` in its own
        session and kills the whole group, which reaches that child too.
        """
        try:
            return process_group.run(
                [*self._argv(), remote_cmd], text=True, errors=errors,
                capture_output=capture, stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(args=[], returncode=124, stdout="", stderr="timeout")

    def out(self, remote_cmd: str, *, timeout: int | None = DEFAULT_TIMEOUT) -> str:
        return self.run(remote_cmd, timeout=timeout).stdout.strip()

    def ok(self, remote_cmd: str, *, timeout: int | None = DEFAULT_TIMEOUT) -> bool:
        return self.run(remote_cmd, timeout=timeout).returncode == 0


def prelude(cfg) -> str:
    """cd + PATH + exports, one prefix for every command.

    A non-interactive ssh shell often lacks ~/.local/bin, so `uv` is simply missing —
    which reads as "uv is not installed", not as "PATH is wrong".
    """
    parts = [f"cd {cfg.get('ROPS_REMOTE_DIR', '~')}"]
    if p := cfg.get("ROPS_PATH_PREPEND"):
        parts.append(f"export PATH={p}:$PATH")
    for pair in cfg.csv("ROPS_ENV"):
        parts.append(f"export {pair}")
    return " && ".join(parts)
