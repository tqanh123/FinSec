"""Configuration: env > .rops.env (repo root) > .rops.env (main checkout) > detected >
default, with provenance.

Every value carries where it came from. A detected value that reads exactly like a
declared one is how a wrong destination gets a `--delete` run against it.

In a LINKED worktree `.git` is a file, so `find_repo` stops at the worktree and its
own `.rops.env` is usually absent; the main checkout's file (the parent of `git
rev-parse --git-common-dir`) is then read as its own, weaker layer, printed as
`.rops.env (main checkout)`, and `doctor` does not write a second file over it.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass

from hs_family import UsageError
from rops.ropslib.prog import PROG

ENV_FILENAME = ".rops.env"
PREFIX = "ROPS_"

# Source ranks, weakest first. `doctor` prints these verbatim.
SRC_DEFAULT, SRC_DETECTED, SRC_FILE, SRC_ENV = "default", "detected", ".rops.env", "env"
SRC_MAIN_FILE = ".rops.env (main checkout)"
#: `ROPS_REMOTE_DIR` derived from `ROPS_LANE` (config.py::_apply_lane) rather than
#: read from any layer directly — its own tag, so `doctor` never prints this as if
#: someone had typed `ROPS_REMOTE_DIR=` themselves.
SRC_LANE = "ROPS_LANE"


@dataclass(frozen=True)
class Value:
    value: str
    source: str

    def __bool__(self) -> bool:
        return bool(self.value)


class Config:
    """Resolved keys plus the provenance of each. Never a bare dict — a bare dict is
    exactly what makes a detected value indistinguishable from a declared one."""

    def __init__(self, repo: pathlib.Path, values: dict[str, Value]):
        self.repo = repo
        self._v = values

    def get(self, key: str, default: str = "") -> str:
        v = self._v.get(key)
        return v.value if v else default

    def source(self, key: str) -> str:
        v = self._v.get(key)
        return v.source if v else SRC_DEFAULT

    def is_detected(self, key: str) -> bool:
        return self.source(key) == SRC_DETECTED

    def csv(self, key: str) -> list[str]:
        return [p.strip() for p in self.get(key).split(",") if p.strip()]

    def keys(self) -> list[str]:
        return sorted(self._v)

    def items(self):
        return [(k, self._v[k]) for k in self.keys()]

    def require(self, key: str, why: str) -> str:
        val = self.get(key)
        if not val:
            raise UsageError(
                f"rops: {key} is not set — {why}\n"
                f"      declare it in {self.repo / ENV_FILENAME} or export {key}=..."
            )
        return val

    def override(self, key: str, value: str, source: str) -> "Config":
        """A NEW Config with `key` forced to `value`, tagged `source`.

        Used by `run --lane NAME` (and every `ROPS_CMD_*` alias, which shares its
        `add_tree_args`/`execute` path) to set the lane for ONE call — never mutates
        what THIS call resolved from the environment/file layers, so a second verb
        reading the same `args.cfg` still sees its own answer.
        """
        values = dict(self._v)
        values[key] = Value(value, source)
        return Config(self.repo, values)

    def with_lane(self, name: str, source: str) -> "Config":
        """A NEW Config in lane `name` for ONE call (`run --lane NAME`): the lane, its
        remote dir, and its log dir together — the three keys a lane decides, set by
        the one rule `load` applies, so a per-call lane cannot forget one of them."""
        values = dict(self._v)
        lane = Value(name, source)
        values["ROPS_LANE"] = lane
        values["ROPS_REMOTE_DIR"] = Value(f"~/rops/{name}", source)
        _apply_lane_log_dir(values, lane)
        return Config(self.repo, values)


def find_repo(start: pathlib.Path | None = None) -> pathlib.Path:
    """Nearest ancestor holding a .git entry; else the starting directory."""
    cur = (start or pathlib.Path.cwd()).resolve()
    for cand in [cur, *cur.parents]:
        if (cand / ".git").exists():
            return cand
    return cur


#: The shell escape a single-quoted value uses for an embedded quote. The writer
#: emits it; the parser reverses exactly it — one convention, both ends.
_SQ = "'\\''"


def shell_quote(value: str) -> str:
    """One value as a single-quoted shell word. Everything literal: `$`, `&&`,
    `#` survive verbatim, which is what the ROPS_CMD_* strings need (they expand
    on the REMOTE, not here) and what makes `source .rops.env` assign instead
    of execute."""
    return "'" + value.replace("'", _SQ) + "'"


def _maybe_unquote(v: str) -> str:
    """Reverse `shell_quote` for a whole-value quote pair; a bare value passes
    through untouched (legacy unquoted files keep their exact old meaning)."""
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace(_SQ, "'")
    return v


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """KEY=VALUE lines. Two value shapes:

    * single-quoted (what the writer emits): taken verbatim to the closing
      quote — ` #` inside is content, and the `$_SQ` escape unescapes;
    * bare (legacy files, hand edits): a trailing ` #` comment is stripped;
      a bare value containing ` #` would be truncated, which is why the
      writer never emits that shape anymore.
    """
    if not path.is_file():
        return {}
    return _parse_lines(path.read_text(encoding="utf-8"))


def _parse_lines(text: str) -> dict[str, str]:
    """`parse_env_file` over a string — the migration path checks its own
    output without writing it first."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if v.startswith("'"):
            out[k.strip()] = _maybe_unquote(v)
        else:
            out[k.strip()] = v.split(" #", 1)[0].strip()
    return out


def _common_dir(repo: pathlib.Path) -> pathlib.Path | None:
    """`git rev-parse --git-common-dir` resolved against `repo`, or None."""
    if not (repo / ".git").exists():
        return None
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=str(repo),
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return (repo / common).resolve() if common else None


def main_checkout(repo: pathlib.Path) -> pathlib.Path | None:
    """The main checkout a LINKED worktree belongs to; None for the main checkout
    itself or a directory that is not a worktree."""
    if not (repo / ".git").is_file():
        return None
    common = _common_dir(repo)
    if common is None:
        return None
    main = common.parent
    return None if main == repo.resolve() else main


def main_env_file(repo: pathlib.Path) -> pathlib.Path | None:
    """The main checkout's `.rops.env` when `repo` is a linked worktree and it exists."""
    main = main_checkout(repo)
    path = main / ENV_FILENAME if main else None
    return path if path and path.is_file() else None


def load(repo: pathlib.Path | None = None, *, detect: bool = True) -> Config:
    from rops.ropslib import detect as detect_mod

    repo = repo or find_repo()
    values: dict[str, Value] = {}

    if detect:
        for k, v in detect_mod.detect(repo).items():
            if v:
                values[k] = Value(v, SRC_DETECTED)
    main_file = main_env_file(repo)
    for k, v in (parse_env_file(main_file) if main_file else {}).items():
        if k.startswith(PREFIX):
            values[k] = Value(v, SRC_MAIN_FILE)
    for k, v in parse_env_file(repo / ENV_FILENAME).items():
        if k.startswith(PREFIX):
            values[k] = Value(v, SRC_FILE)
    for k, v in os.environ.items():
        if k.startswith(PREFIX):
            values[k] = Value(v, SRC_ENV)
    _apply_lane(values)
    return Config(repo, values)


def _apply_lane(values: dict[str, "Value"]) -> None:
    """`ROPS_LANE`, from ANY layer, names `~/rops/<lane>` as `ROPS_REMOTE_DIR` —
    unless a REAL environment variable already set `ROPS_REMOTE_DIR` (the operator
    typing it for this one call outranks a lane name declared anywhere else).

    Before this, seven verbs read `ROPS_REMOTE_DIR` directly and exactly one
    (`lane.py`) read `ROPS_LANE` — so `lane.py`'s own hint ("or ROPS_LANE=<name>")
    was false: setting `ROPS_LANE` moved nothing `run`/`test`/... read. Resolving it
    HERE, once, means every reader of `ROPS_REMOTE_DIR` sees the lane without a
    seven-site edit, and `doctor` (which already prints `ROPS_REMOTE_DIR`'s source)
    shows exactly where the value came from.
    """
    lane = values.get("ROPS_LANE")
    if not lane or not lane.value:
        return
    remote_dir = values.get("ROPS_REMOTE_DIR")
    if remote_dir is None or remote_dir.source != SRC_ENV:
        # a real ROPS_REMOTE_DIR env var wins outright
        values["ROPS_REMOTE_DIR"] = Value(f"~/rops/{lane.value}", SRC_LANE)
    _apply_lane_log_dir(values, lane)


def _apply_lane_log_dir(values: dict[str, "Value"], lane: "Value") -> None:
    """Logs follow the lane, or two lanes' logs mix in one dir. A declared log dir stands
    only when its layer is at least as strong as the one that named the lane: declared
    side by side in one `.rops.env` it is deliberate; under a lane exported for ONE call
    it was written for a different lane."""
    log_dir = values.get("ROPS_LOG_DIR")
    if log_dir is None or not log_dir.value or _rank(log_dir.source) < _rank(lane.source):
        values["ROPS_LOG_DIR"] = Value(f"{DEFAULT_LOG_ROOT}/{lane.value}", SRC_LANE)


#: Where remote logs go with no lane and no ROPS_LOG_DIR; a lane gets a subdir.
DEFAULT_LOG_ROOT = "/tmp/rops"

_RANKS = {SRC_DEFAULT: 0, SRC_DETECTED: 1, SRC_MAIN_FILE: 2, SRC_FILE: 3, SRC_ENV: 4}


def _rank(source: str) -> int:
    """Layer strength; a per-call source (`--lane`) outranks every layer."""
    return _RANKS.get(source, len(_RANKS))


def maybe_migrate_env_file(path: pathlib.Path) -> "tuple[bool, list[str]]":
    """(migrated, refused_keys) — upgrade a legacy unquoted file to the quoted
    shape, ONLY when the upgrade is lossless.

    A legacy file is the dangerous one (sourcing it executes the unquoted
    command tails) and `write_env_file` never touches an existing file, so
    `doctor` calls this instead. Comments and blank lines are preserved
    verbatim; a bare `KEY=VALUE` line becomes `KEY='VALUE'` with VALUE the
    legacy parse of that same line. When the legacy parse TRUNCATED the line
    (the bare value contained ` #`), re-quoting the truncated form would
    silently bless the truncation as the declared value — such a key refuses,
    nothing is written, and the file stays byte-for-byte. The whole rewritten
    text is re-parsed and compared before any write, so a migrate that would
    change ANY key's meaning never lands. Never creates a file."""
    if not path.is_file():
        return False, []
    text = path.read_text(encoding="utf-8")
    old = _parse_lines(text)
    out_lines: list[str] = []
    refused: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            out_lines.append(raw)
            continue
        k, _, v = line.partition("=")
        key, v = k.strip(), v.strip()
        if v.startswith("'"):
            out_lines.append(raw)          # already the quoted shape
            continue
        if old.get(key) != v:
            refused.append(key)            # legacy parse truncated this line
            out_lines.append(raw)
            continue
        out_lines.append("%s=%s" % (key, shell_quote(old[key])))
    if refused:
        return False, refused
    new_text = "\n".join(out_lines) + "\n"
    if new_text == text:
        return False, []                   # already fully quoted — nothing to do
    if _parse_lines(new_text) != old:
        return False, sorted(old)          # anything shifted: touch nothing
    # The user's own file is rewritten: keep its exact bytes first (`env_backup_path`),
    # so a migration that reads right but is not wanted is one `mv` from undone.
    shutil.copyfile(path, env_backup_path(path))
    path.write_text(new_text, encoding="utf-8")
    return True, []


def env_backup_path(path: pathlib.Path) -> pathlib.Path:
    """Where `maybe_migrate_env_file` keeps the pre-migration bytes: `<name>.bak`
    beside the file. Overwritten by a later migration, whose input already holds
    everything the earlier one kept, in its quoted form."""
    return path.with_name(path.name + ".bak")


def write_env_file(cfg: Config, path: pathlib.Path) -> list[str]:
    """Write detected values out so the next run reads a declared file instead of
    re-detecting. Returns the lines written. Never overwrites an existing file.

    Every value is single-quoted (`shell_quote`): the ROPS_CMD_* strings carry
    `&&`/`$` for the REMOTE shell, and an unquoted file made `source .rops.env`
    execute that tail locally (measured 260929). Quoted, sourcing assigns the
    exact string and runs nothing, and the parser reads it back verbatim."""
    if path.exists():
        return []
    lines = [
        f"# .rops.env — written by `{PROG} doctor` from what it detected.",
        "# Read it. A wrong value here sends commands to the wrong place.",
        "# Real environment variables of the same name still win over this file.",
        "# Safe to source: values are single-quoted, so sourcing only assigns them.",
        "",
        "# ROPS_HOST is never detected — guessing a host means running commands on",
        "# someone else's machine. Declare it.",
        f"ROPS_HOST={shell_quote(cfg.get('ROPS_HOST'))}",
        "",
    ]
    for key, val in cfg.items():
        if key == "ROPS_HOST" or val.source != SRC_DETECTED:
            continue
        lines.append(f"{key}={shell_quote(val.value)}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return lines


def git_exclude(repo: pathlib.Path, entry: str) -> bool:
    """Add `entry` to .git/info/exclude — the LOCAL ignore list. Never .gitignore:
    that file belongs to the repo and may not be ours to change.

    Resolved through `git rev-parse --git-common-dir`, not a bare `repo / ".git"`:
    in a LINKED WORKTREE `.git` is a FILE (`gitdir: .../.git/worktrees/<name>`)
    pointing at a private per-worktree directory that has no `info/` of its own —
    `info/exclude` lives once, at the COMMON dir every worktree of the same repo
    shares. `repo / ".git" / "info"` there silently created a new, never-read
    `info/exclude` inside the file's own parent (`.git` as a path component of a
    FILE, not a directory, so `.mkdir()` raised `NotADirectoryError` before this
    fix's own regression, or missed writing anywhere doctor's caller ever checks) —
    `.rops.env` kept showing up untracked and one `git add -A` away from being
    committed with a `ROPS_HOST` value inside it. This repo works mainly out of
    `.claude/worktrees/*`, so that gap was not a corner case.
    """
    common = _common_dir(repo)
    if common is None:
        return False
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    ex = info / "exclude"
    body = ex.read_text(encoding="utf-8") if ex.is_file() else ""
    if any(line.strip() == entry for line in body.splitlines()):
        return False
    sep = "" if body.endswith("\n") or not body else "\n"
    ex.write_text(f"{body}{sep}{entry}\n", encoding="utf-8")
    return True
