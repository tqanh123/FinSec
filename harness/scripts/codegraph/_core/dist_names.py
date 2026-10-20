"""Which installed distribution an import comes from.

One dotted import resolves, in order: a project's own `import_names` (its word is final);
the longest dotted prefix known to either the repo venv's installed files or the shipped
table, the venv winning a tie; else the top-level name itself. An answer is a tuple of
candidates — `cv2` is any of the opencv builds — and declaring any one satisfies it.

The venv is read with its own interpreter (`importlib.metadata` over each dist's RECORD),
so a namespace package splits by owner: `google.protobuf` is `protobuf`, `google.auth`
is `google-auth`. Prefixes stop at depth 3.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

import process_group

TABLE: Dict[str, Tuple[str, ...]] = {
    "yaml": ("pyyaml",), "dotenv": ("python-dotenv",), "PIL": ("pillow",), "jwt": ("PyJWT",),
    "dateutil": ("python-dateutil",), "bs4": ("beautifulsoup4",), "sklearn": ("scikit-learn",),
    "cv2": ("opencv-python", "opencv-python-headless", "opencv-contrib-python",
            "opencv-contrib-python-headless"),
    "google.protobuf": ("protobuf",), "psycopg_pool": ("psycopg-pool",),
    "opentelemetry": ("opentelemetry-api",), "opentelemetry.sdk": ("opentelemetry-sdk",),
    "mem0": ("mem0ai",),
}

PROBE = r"""
import json
from importlib.metadata import distributions
SUF = (".py", ".pyi", ".so", ".pyd")
out = {}
for d in distributions():
    name = d.metadata["Name"]
    if not name:
        continue
    mods = set()
    for f in d.files or ():
        parts = f.parts
        if (not parts or parts[0] == ".." or "__pycache__" in parts
                or any(p.endswith((".dist-info", ".egg-info", ".data")) for p in parts)
                or not parts[-1].endswith(SUF)):
            continue
        stem = parts[-1].split(".")[0]
        dotted = list(parts[:-1]) + ([] if stem == "__init__" else [stem])
        if dotted and all(p.isidentifier() for p in dotted):
            mods.update(".".join(dotted[:i]) for i in range(1, min(len(dotted), 3) + 1))
    if not d.files:
        mods.update(t for t in (d.read_text("top_level.txt") or "").split() if t.isidentifier())
    for m in mods:
        out.setdefault(m, set()).add(name)
print(json.dumps({k: sorted(v) for k, v in out.items()}))
"""

Installed = Mapping[str, Tuple[str, ...]]


def venv_python(root: Path) -> Path:
    win = os.name == "nt"
    return root / ".venv" / ("Scripts" if win else "bin") / ("python.exe" if win else "python")


def installed_modules(root: Path) -> Tuple[Optional[Installed], str]:
    """(module prefix -> owning dists, where the answer came from). None when the repo has
    no readable `.venv`; the second item then says why and that the table answers."""
    py = venv_python(root)
    if not py.is_file():
        return None, "shipped table (no .venv at the repo root)"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        # The group goes with it on a timeout: a venv interpreter can start its own children.
        r = process_group.run([str(py), "-c", PROBE], capture_output=True, text=True,
                              env=env, timeout=120, cwd=str(root))
        if r.returncode:
            why = (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1]
            return None, "shipped table (.venv could not answer: exit %d, %s)" % (r.returncode, why)
        found = {k: tuple(v) for k, v in json.loads(r.stdout).items()}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return None, "shipped table (.venv could not answer: %s)" % e
    return found, ".venv (%d import prefixes from %s)" % (len(found), py)


def candidates(target: str, extra: Optional[Mapping[str, str]] = None,
               installed: Optional[Installed] = None) -> Tuple[str, ...]:
    parts = target.split(".")
    prefixes = [".".join(parts[:i]) for i in range(len(parts), 0, -1)]
    for p in prefixes:
        if extra and p in extra:
            return (extra[p],)
    for p in prefixes:
        if installed and p in installed:
            return tuple(installed[p])
        if p in TABLE:
            return TABLE[p]
    return (parts[0],)
