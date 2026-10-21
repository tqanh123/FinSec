"""Who is asking: the agent binary ABOVE this process, hashed.

A grant does not name a string; it names a sha256 of an executable file. The
path is only how that file was found — a process saying "claude" in its path
grants nothing, and an upgrade that swaps the binary changes the hash and so
changes the identity (which is exactly what `trust --repin` exists to admit).

The parent chain is walked through /proc (pid -> ppid via /proc/<pid>/stat,
executable via /proc/<pid>/exe). The walk stops SOFTLY: the outermost
ancestors on a desktop are the terminal and the display manager, and
"permission denied" reading them is the normal edge of the walk, never an
error. macOS/Windows walk their own ways when their tiers land; the hint
table below is what has been MEASURED on this box (a versioned install
directory is the strongest signal — the agent binaries here live under
`versions/<semver>`).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

#: a versioned install directory (`.../versions/2.1.283[/...]`) — the shape
#: every measured agent runtime installs itself under on this box
_VERSIONED = re.compile(r"/versions/[0-9][0-9A-Za-z.\-]*(?:/|$)")
#: bare binary names that are agents even without a versioned path. Hints,
#: not authority: they pick WHICH chain entry to hash, never grant anything.
_NAMES = frozenset({"claude", "codex", "cursor-agent", "gemini"})


def looks_like_agent(exe: "str | None") -> bool:
    """A heuristic hint, deliberately weak: versioned path or known name."""
    if not exe:
        return False
    if _VERSIONED.search(exe):
        return True
    return os.path.basename(exe) in _NAMES


def fingerprint_of(path: "str | Path") -> Optional[str]:
    """sha256 of the executable file — the identity a grant carries."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _ppid_of(pid: int) -> int:
    try:
        stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
    except OSError:
        return 0
    # the comm field can contain spaces and parens — everything after the
    # LAST ')' is fixed-width: state, ppid, ...
    tail = stat.rpartition(")")[2].split()
    return int(tail[1]) if len(tail) > 1 else 0


def _exe_of(pid: int) -> Optional[str]:
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return None


def parent_chain(start: "int | None" = None, limit: int = 32) -> List[Tuple[int, Optional[str]]]:
    """(pid, exe) from THIS process (or `start`) up through its parents,
    nearest first. Stops softly at pid 1, a loop, an unreadable stat, or
    `limit` steps — the outer edge of a desktop session is unreadable by
    design and the chain that matters is well inside it."""
    out: List[Tuple[int, Optional[str]]] = []
    pid = int(start or os.getpid())
    seen = set()
    while pid and pid not in seen and len(out) < limit:
        seen.add(pid)
        exe = _exe_of(pid)
        ppid = _ppid_of(pid)
        if ppid == pid:
            break
        out.append((pid, exe))
        pid = ppid
    return out


def agent_above(chain: "List[Tuple[int, Optional[str]]] | None" = None,
                fingerprint=None) -> "Optional[Tuple[str, str]]":
    """(exe, sha256) of the nearest agent-looking binary in the parent chain,
    nearest first — or None when no ancestor looks like one.

    `fingerprint` is injectable for tests; the default hashes the real file.
    A binary that LOOKS like an agent but cannot be read hashes to None and
    is skipped: an unhashable identity must not become a grant key."""
    chain = parent_chain() if chain is None else chain
    hash_of = fingerprint_of if fingerprint is None else fingerprint
    for _pid, exe in chain:
        if not exe or not looks_like_agent(exe):
            continue
        sha = hash_of(exe)
        if sha:
            return str(exe), sha
    return None
