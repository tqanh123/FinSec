"""A run of a project's own tool (its test runner, its compiler) may write into the tree.
`guarded(repo)` copies every uncommitted file aside before the run and, after it, puts
back any the run changed -- copying the post-run version aside first, so no version is
lost whoever made the change -- then refuses the run (exit 3) naming each file and both
copies. A run that changed nothing leaves no trace.

Guarded: tracked files with uncommitted changes, untracked files git does not ignore
(`git status --porcelain -z --untracked-files=all`), and assume-unchanged or
skip-worktree files (`git ls-files -v`). Not guarded: an untracked file under the run's
declared `outputs` (a coverage dir) -- a tracked one is guarded wherever it lies --,
ignored files, the harness's own data home.

One guarded run per repo (a lock under the data home); a second waits `wait_s`, then
is refused. A run killed outright (SIGKILL) leaves its run dir without a `done` mark:
the next run is refused until `restore_interrupted` or `discard_interrupted` answers it.
SIGTERM/SIGHUP restore like any other way out (`process_group.raise_on_termination`).

Copies live in `<data home>/runs/<ts>-<pid>-<rand>/{before,after}/` with a manifest of
hashes. The backup is capped at `guard_backup_max_mb` (the language book, default 64):
over it, the run is refused before it starts.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import harness_paths
import process_group

BOOK = Path(__file__).resolve().parents[1] / "data" / "hs-code-languages.yaml"
DEFAULT_MAX_MB = 64
DONE = "done"
_FLAGS = "--restore-interrupted %s / --discard-interrupted %s on `hs-code test run` or `check run`"


def _refuse(msg: str) -> "Exception":
    import hs_family
    return hs_family.RefusedError(msg)


def _git(top: Path, *argv: str) -> bytes:
    import secret_scan_before_ship as secret_scan
    return subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(top), *argv], check=True,
                          capture_output=True, env=secret_scan.git_safe_env()).stdout


def _toplevel(repo: Path) -> Path:
    return Path(_git(Path(repo), "rev-parse", "--show-toplevel").decode().strip())


def _runs_dir(repo: Path) -> Path:
    return harness_paths.data_root_for(repo) / "runs"


def max_mb() -> float:
    try:
        import yaml_io
        doc = yaml_io.safe_load(BOOK.read_text(encoding="utf-8")) or {}
        return float(doc.get("guard_backup_max_mb", DEFAULT_MAX_MB))
    except (OSError, ValueError, TypeError):
        return float(DEFAULT_MAX_MB)


def _under(rel: str, prefixes: Sequence[str]) -> bool:
    return any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in prefixes)


def uncommitted(top: Path, outputs: Sequence[str] = (), skip: Sequence[str] = ()) -> "list[str]":
    """Toplevel-relative paths of every file the guard copies (see the module doc)."""
    out: "set[str]" = set()
    new: "set[str]" = set()   # only an untracked file can be a run's output
    fields = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    i = 0
    while i < len(fields):
        entry = fields[i].decode("utf-8", "surrogateescape")
        i += 1
        if len(entry) < 4:
            continue
        out.add(entry[3:])
        if entry.startswith("??"):
            new.add(entry[3:])
        if entry[0] in "RC":
            i += 1   # the rename's source path follows
    for line in _git(top, "ls-files", "-v", "-z").split(b"\0"):
        tag, _, rel = line.decode("utf-8", "surrogateescape").partition(" ")
        if rel and (tag.islower() or tag == "S"):
            out.add(rel)
    return sorted(r for r in out if not _under(r, list(skip))
                  and not (r in new and _under(r, list(outputs))))


def _digest(p: Path) -> "Optional[str]":
    if p.is_symlink():
        return "link:" + os.readlink(p)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(os.readlink(src))
    else:
        shutil.copy2(src, dst)


@contextlib.contextmanager
def _lock(runs: Path, wait_s: float) -> Iterator[None]:
    runs.mkdir(parents=True, exist_ok=True)
    fd = os.open(runs / "guard.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _refuse("another guarded run holds %s (waited %gs) -- nothing run"
                                  % (runs / "guard.lock", wait_s)) from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def interrupted(repo: Path) -> "list[str]":
    runs = _runs_dir(Path(repo))
    if not runs.is_dir():
        return []
    return sorted(d.name for d in runs.iterdir()
                  if d.is_dir() and (d / "manifest.json").is_file() and not (d / DONE).exists())


def _settle(top: Path, run: Path, files: "dict[str, Optional[str]]") -> "list[str]":
    """Put back every file whose content moved off its `before` copy, the moved version
    copied to `after/` first. Returns what was put back."""
    moved = []
    for rel, was in files.items():
        cur = top / rel
        if _digest(cur) == was:
            continue
        moved.append(rel)
        if cur.is_symlink() or cur.exists():
            _copy(cur, run / "after" / rel)
            cur.unlink()
        if was is not None:
            _copy(run / "before" / rel, cur)
    return moved


def _finish(run: Path, moved: "list[str]") -> None:
    if moved:
        (run / DONE).write_text("\n".join(moved) + "\n", encoding="utf-8")
    else:
        shutil.rmtree(run, ignore_errors=True)


@contextlib.contextmanager
def guarded(repo: Path, outputs: Sequence[str] = (), *, wait_s: float = 600.0,
            max_mb: "Optional[float]" = None) -> Iterator[Path]:
    """The body runs with the tree's uncommitted files copied aside; see the module doc.
    `outputs` are toplevel-relative paths the run writes on purpose."""
    top = _toplevel(Path(repo))
    runs = _runs_dir(Path(repo))
    cap = globals()["max_mb"]() if max_mb is None else max_mb
    with _lock(runs, wait_s):
        left = interrupted(Path(repo))
        if left:
            raise _refuse("an earlier guarded run did not finish (%s): the tree may still hold "
                          "what it wrote -- answer it first: %s"
                          % (runs / left[0], _FLAGS % (left[0], left[0])))
        skip = []
        try:
            skip.append(runs.parent.resolve().relative_to(top.resolve()).as_posix())
        except ValueError:
            pass
        rels = uncommitted(top, outputs, skip)
        size = sum((top / r).stat().st_size for r in rels
                   if (top / r).is_file() and not (top / r).is_symlink())
        if size > cap * 1024 * 1024:
            raise _refuse("the uncommitted files to copy aside before the run hold %.1f MB, over "
                          "guard_backup_max_mb (%g) -- commit or move some first; nothing run"
                          % (size / 1048576.0, cap))
        run = runs / ("%s-%d-%s" % (time.strftime("%Y%m%dT%H%M%S"), os.getpid(), secrets.token_hex(3)))
        files = {}
        for r in rels:
            files[r] = _digest(top / r)
            if files[r] is not None:
                _copy(top / r, run / "before" / r)
        run.mkdir(parents=True, exist_ok=True)
        (run / "manifest.json").write_text(json.dumps({"top": str(top), "files": files},
                                                      sort_keys=True), encoding="utf-8")
        with process_group.raise_on_termination():
            try:
                yield run
            finally:
                moved = _settle(top, run, files)
                _finish(run, moved)
        if moved:
            raise _refuse("the run changed uncommitted files: %s -- each put back as it was "
                          "before (copies: %s/before), the run's versions kept in %s/after; "
                          "the run's result is not trusted" % (", ".join(moved), run, run))


def restore_interrupted(repo: Path, run_id: str) -> "list[str]":
    run = _runs_dir(Path(repo)) / run_id
    doc = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    moved = _settle(Path(doc["top"]), run, doc["files"])
    (run / DONE).write_text("\n".join(moved) + "\n", encoding="utf-8")
    return moved


def discard_interrupted(repo: Path, run_id: str) -> None:
    run = _runs_dir(Path(repo)) / run_id
    if not (run / "manifest.json").is_file():
        raise _refuse("no guarded run %s under %s" % (run_id, run.parent))
    shutil.rmtree(run)


def add_interrupted_flags(group: Any) -> None:
    """The two answers to a killed run, on every verb that runs under `guarded` (`_FLAGS`)."""
    group.add_argument("--restore-interrupted", metavar="ID",
                       help="put back what a killed run left changed, then stop")
    group.add_argument("--discard-interrupted", metavar="ID",
                       help="forget a killed run's copies (the tree stays as it is), then stop")


def answer_interrupted(repo: Path, restore: Optional[str], discard: Optional[str]) -> str:
    """Do what `add_interrupted_flags` asked; the line to print."""
    if restore:
        moved = restore_interrupted(repo, restore)
        return "put back %d file(s): %s" % (len(moved), ", ".join(moved) or "none changed")
    discard_interrupted(repo, discard or "")
    return "discarded the copies of %s" % discard
