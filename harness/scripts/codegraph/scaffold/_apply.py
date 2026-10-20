"""The write half `new-module` and `new-dist` share: plan by default, write only
under hs-code's shared `--apply`.

`--target DIR` names where a write lands (default: the repo root, which is what
every repo-relative template path assumes); it never turns a plan into a write.
Every answer says which of the two it was in `dry_run`, the field `graph
snapshot` already carries.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .engine import Content, Link, collisions_under, write_plan


def apply_plan(repo: Path, files: "Sequence[tuple[str, Content]]", *, target: "str | None",
               apply: bool) -> "dict":
    """`{dry_run, target, written, refused}` -- `files` written under `target`
    (default `repo`) only when `apply`; a collision refuses the whole plan."""
    dest = Path(target) if target else Path(repo)
    written: "list[str]" = []
    refused: "list[str]" = []
    if apply:
        written, refused = write_plan(dest, files)
    else:
        refused = collisions_under(dest, files)
    return {"dry_run": not apply, "target": str(dest), "written": written, "refused": refused}


def file_rows(files: "Sequence[tuple[str, Content]]") -> "list[dict]":
    """`data.files`: `{path}` per planned file, plus `link_to` for a symlink."""
    return [{"path": p, **({"link_to": c.target} if isinstance(c, Link) else {})}
            for p, c in files]


def print_report(verb: str, data: dict, files: "Sequence[tuple[str, Content]]") -> None:
    for w in data["warnings"]:
        print("  warning: %s" % w)
    if data["refused"]:
        print("hs-code scaffold %s: %s -- %d file(s) already exist under %s:"
              % (verb, "WOULD REFUSE" if data["dry_run"] else "REFUSED",
                 len(data["refused"]), data["target"]))
        for p in data["refused"]:
            print("  %s" % p)
        print("  nothing written")
        return
    if data["dry_run"]:
        print("hs-code scaffold %s: DRY RUN -- planned %d file(s) for %r under %s "
              "(nothing written; --apply writes)" % (verb, len(files), data["name"], data["target"]))
        for row in file_rows(files):
            print("  %s%s" % (row["path"], " -> " + row["link_to"] if "link_to" in row else ""))
        return
    print("hs-code scaffold %s: wrote %d file(s) under %s" % (verb, len(data["written"]), data["target"]))
    for p in data["written"]:
        print("  %s" % p)
