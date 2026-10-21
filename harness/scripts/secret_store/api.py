"""What every hs-secrets verb does first: open the store, read a value safely,
fold the index. Kept here so each verb stays one parser + one action."""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

from secret_store import backends, home as home_mod, ledgers

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class Refusal(Exception):
    """A deliberate no — the verb maps this to the CLI's refusal exit (3) or a
    broken one (2) by the `code` it carries."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def check_ref(ref: str) -> str:
    if not _REF.match(ref or ""):
        raise Refusal("a ref is one word of letters, digits, . _ / - (got %r)" % (ref or ""))
    return ref


def open_store(backend_choice: "str | None"):
    """(home, backend, live refs folded from the index) — the three things a
    verb needs; the home bootstraps itself here on first use."""
    home = home_mod.ensure_home(home_mod.secrets_home())
    backend = backends.select(home, choice=backend_choice)
    return home, backend, ledgers.fold_index(ledgers.read(home / "index.jsonl"))


def read_value(flag_value: "str | None", ref: str) -> str:
    """The value, never from argv: `--value -` reads stdin, no flag in a tty
    prompts (echo off), no flag and no tty refuses."""
    if flag_value == "-":
        data = sys.stdin.read()
    elif flag_value is None:
        if not sys.stdin.isatty():
            raise Refusal("no value: pass --value - (stdin), or run in a tty to be prompted")
        data = getpass.getpass("value for %s: " % ref)
        confirm = getpass.getpass("again: ")
        if data != confirm:
            raise Refusal("the two entries differ", code=2)
    else:
        data = flag_value
    if data.endswith("\n"):
        data = data[:-1]
    if not data:
        raise Refusal("an empty value is almost certainly a mistake; pass a real one")
    return data
