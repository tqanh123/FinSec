"""Reading an .env file without executing it.

KEY=VALUE lines: bare values, single- or double-quoted values (the quoting a
safe-to-source writer emits), a trailing ` # comment` on a BARE value only —
inside quotes a `#` is content. Comments and blank lines are skipped; a line
without `=` is skipped rather than guessed at. Nothing here ever touches a
shell: the whole point is that the file's values go into the store, not into
a command.
"""
from __future__ import annotations


def parse(text: str) -> dict:
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if value[:1] in ("'", '"') and len(value) >= 2:
            end = value.find(value[0], 1)
            if end != -1:
                # the quoted span is the value; anything after it is comment
                out[key] = value[1:end]
                continue
        out[key] = value.split(" #", 1)[0].rstrip()
    return out
