"""Where the store lives: one home per user, self-created on first use.

`~/.harness/secrets/` by default, `HARNESS_SECRETS_HOME` to override (tests,
or a second store). The first command that touches the store creates the home
and the three empty ledgers — there is no manual init step, and no command
before it can report a half-existing store.
"""
from __future__ import annotations

import os
from pathlib import Path

LEDGERS = ("index.jsonl", "grants.jsonl", "access.jsonl", "trusted.jsonl")
ENV_HOME = "HARNESS_SECRETS_HOME"


def secrets_home(env=None) -> Path:
    """The store's home directory (not necessarily existing yet)."""
    env = os.environ if env is None else env
    return Path(env.get(ENV_HOME) or Path.home() / ".harness" / "secrets")


def ensure_home(home: Path) -> Path:
    """Create the home and its three ledgers if absent; return the home.

    Idempotent — every command calls this first, so the store bootstraps
    itself on first use and stays intact on every later one. 0700 on the
    directory: it holds the encrypted file backend's key.
    """
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass  # a read-only mount or a foreign owner: the backend reports its own errors
    for name in LEDGERS:
        (home / name).touch(exist_ok=True)
    return home
