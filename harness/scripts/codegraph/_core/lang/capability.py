"""Which languages a command reads, against which languages a tree is measured with.

A registry row declares `languages: [python]` when its code reads Python only. In a
tree whose `codemap.yaml` enables another pack -- a bin one under `languages:`, or the
project's own (an `adapters:` row naming a `resolver:`) -- the shell asks `refusal_for`
before the verb runs: an argument naming a file of a language outside the row's set is a refusal
(exit 3) by name -- never a run that skips it.

An argument is read as written and as each half of `--flag=value` and `OLD=NEW`, with a
`::symbol` tail dropped. It names another language's file when its basename matches
that pack's globs (existing or not: a move's destination), or when it is a directory
holding such a file. `--apply` writes the tree, so under it every target-like argument
(a path, or a dotted name) must be one the row's languages own -- a `.py` path, or a
dotted name, which only a Python module carries -- and an `--apply` naming no target
at all is refused too.

Stdlib, `yaml_io` and the walk's skip set only: the shell asks this before any
measuring code is imported.

WHAT IT CANNOT SEE
    * A target the verb derives itself (a default root, a glob it expands): the run
      goes ahead and the envelope carries `languages_not_considered`.
    * A dotted argument that is not a module: read as Python; the verb itself
      answers for a name the tree does not hold.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

import yaml_io

from ..walk import skipped

BOOK = Path(__file__).resolve().parents[4] / "data" / "hs-code-languages.yaml"
DEFAULT = "python"
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+")


def _globs() -> "dict[str, tuple[tuple[str, ...], tuple[str, ...]]]":
    doc = yaml_io.yaml_load(BOOK.read_text(encoding="utf-8")) or {}
    out = {}
    for row in doc.get("languages") or ():
        files = row.get("files") or {}
        out[str(row["id"])] = (tuple(files.get("include") or ()), tuple(files.get("exclude") or ()))
    return out


def _codemap(repo: Path) -> dict:
    try:
        doc = yaml_io.yaml_load((repo / "codemap.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _project_globs(repo: Path) -> "dict[str, tuple[tuple[str, ...], tuple[str, ...]]]":
    """The project's own languages: each `adapters:` row naming a `resolver:`."""
    out: "dict[str, tuple[tuple[str, ...], tuple[str, ...]]]" = {}
    for row in _codemap(repo).get("adapters") or ():
        if isinstance(row, dict) and row.get("resolver") and isinstance(row.get("id"), str):
            out[row["id"]] = (tuple("*" + str(e) for e in row.get("extensions") or ()), ())
    return out


def enabled(repo: Path) -> "list[str]":
    """The pack ids `repo`'s `codemap.yaml` measures with (absent there: Python), then
    the project's own languages."""
    langs = _codemap(repo).get("languages")
    bin_ids = sorted(langs) if isinstance(langs, dict) and langs else [DEFAULT]
    return bin_ids + sorted(set(_project_globs(repo)) - set(bin_ids))


def _owner(name: str, globs: "dict[str, tuple[tuple[str, ...], tuple[str, ...]]]") -> "str | None":
    base = PurePosixPath(name).name
    for lid, (inc, exc) in globs.items():
        if any(fnmatch.fnmatchcase(base, g) for g in inc) and not any(fnmatch.fnmatchcase(base, g)
                                                                       for g in exc):
            return lid
    return None


def languages_of_files(paths: "Iterable[str]") -> "dict[str, str | None]":
    """path -> the id of the pack whose globs own it (None: no pack does)."""
    globs = _globs()
    return {p: _owner(p, globs) for p in paths}


def tokens(args: "Iterable[str]") -> "list[str]":
    """Every argument a target may hide in: as written, each side of `--flag=value` and
    `OLD=NEW`, with a `::symbol` tail dropped; flag names themselves are not targets."""
    out: "list[str]" = []
    for a in args:
        parts = [a.split("=", 1)[1]] if a.startswith("-") and "=" in a else \
            ([] if a.startswith("-") else a.split("="))
        out.extend(p.split("::", 1)[0] for p in parts if p)
    return out


def _inside(repo: Path, d: Path) -> bool:
    """`d` is a directory of `repo` -- an argument naming one elsewhere is never walked."""
    try:
        d.resolve().relative_to(repo.resolve())
    except ValueError:
        return False
    return d.is_dir()


def _foreign_in(d: Path, foreign: "dict[str, tuple[tuple[str, ...], tuple[str, ...]]]") -> "str | None":
    for here, dirs, files in os.walk(d):
        rel = Path(here).relative_to(d).parts
        dirs[:] = [x for x in dirs if not skipped(rel + (x,))]
        for f in files:
            lid = _owner(f, foreign)
            if lid:
                return lid
    return None


def refusal_for(command: "Sequence[str]", languages: "Sequence[str] | None", args: "Sequence[str]",
                repo: Path, apply: bool = False) -> "str | None":
    """Why `command` (a row reading `languages`) must not run on `args` in `repo`, or None."""
    if not languages:
        return None
    globs = {**_globs(), **_project_globs(repo)}
    foreign = {lid: g for lid, g in globs.items() if lid in enabled(repo) and lid not in languages}
    if not foreign:
        return None
    cmd, reads = " ".join(command), ", ".join(languages)
    toks = tokens(args)
    for t in toks:
        lid = _owner(t, foreign)
        if lid is None and _inside(repo, repo / t):
            lid = _foreign_in(repo / t, foreign)
        if lid:
            return ("%s reads %s only; %s is %s (capability %s not in pack %s)"
                    % (cmd, reads, t, lid, command[-1], lid))
    if apply:
        targets = [t for t in toks if "/" in t or _DOTTED.fullmatch(t) or _owner(t, globs)]
        unproven = [t for t in targets if not (_owner(t, globs) in languages
                                               or ("/" not in t and _DOTTED.fullmatch(t)))]
        if not targets or unproven:
            return ("%s reads %s only and --apply writes the tree; in a tree measured with %s "
                    "every target must be shown to be %s, and %s"
                    % (cmd, reads, ", ".join(sorted(foreign)), reads,
                       "%s is not" % ", ".join(unproven) if unproven else "no target is named"))
    return None


def not_considered(languages: "Sequence[str] | None", repo: Path,
                   snapshot: "Callable[[], Path | None]") -> "dict[str, int | None] | None":
    """{language: modules} the row's `languages` leave out of `repo`'s current snapshot
    (`snapshot()`, asked only when something is left out; None per language when no
    snapshot says), or None when nothing is left out."""
    if not languages:
        return None
    left = [lid for lid in enabled(repo) if lid not in languages]
    if not left:
        return None
    counts: "dict[str, int]" = {}
    snapshot_dir = snapshot()
    if snapshot_dir is not None:
        try:
            meta = json.loads((snapshot_dir / "_meta.json").read_text(encoding="utf-8"))
            counts = {k: int(v.get("modules", 0)) for k, v in (meta.get("languages") or {}).items()}
        except (OSError, ValueError, AttributeError):
            counts = {}
    return {lid: counts.get(lid) for lid in left}
