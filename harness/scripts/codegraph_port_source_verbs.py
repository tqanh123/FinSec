"""hs-code's `port` domain — `source`: the read-only checkout of a batch's source
at its pinned commit. A FLAT module under `harness/scripts/` (same
`hs-code-registry.d` reachability reason as every other `codegraph_port_*_verbs.py`).

`hs-code port source --batch B [--source ID] [--remove] [--apply]`

The checkout lives at `.port/sources/<id>-<sha8>/` (`port_batch_reader.
source_checkout`), built by `materialize_pin` — the clone + `checkout --detach
<pin>` `port snapshot` builds from, so nothing is written under the source repo;
here `--local` rather than `--shared`, so the checkout keeps its pin even after the
source rewrites history and prunes. Every file and directory outside `.git` loses
its write bit (an edit or a new file is refused by the OS, not by good manners;
advisory against root and against the owner chmodding back), and `.port/sources/.gitignore` keeps
it out of the target's history; tree walks never enter it (`codegraph._core.walk`).
The steps that need the upstream tree read it (`{upstream}` of `route-parity`) —
the source repo's own working tree may sit at any commit.

States: `absent` (dry-run names the path; `--apply` builds it), `ready` (HEAD is
the pin, the tree is clean and still locked — reused), `edited` (a file differs
from the pin, HEAD moved, or a write bit came back — exit 3: a read-only checkout must not carry work; `--remove --apply`
drops it, the next `--apply` rebuilds it). A pin git cannot check out is exit 2.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_drift  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.git import GitError, git_text, run_git  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_snapshot_verbs import materialize_pin  # noqa: E402
from hs_family.cli import Command  # noqa: E402

_GITIGNORE = "*\n!.gitignore\n"
_WRITE = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--remove", action="store_true")


def _worktree_paths(co: Path, *, top_down: bool = True):
    """Every file and directory of the checkout outside its `.git`, the root
    included; symlinks are skipped (a chmod follows them out of the tree)."""
    if top_down:
        yield co
    for dp, dn, fn in os.walk(co, topdown=top_down):
        if top_down:
            dn[:] = [d for d in dn if not (Path(dp) == co and d == ".git")]
        elif Path(dp) == co / ".git" or co / ".git" in Path(dp).parents:
            continue
        for name in (dn + fn if top_down else fn + dn):
            p = Path(dp) / name
            if not p.is_symlink() and p != co / ".git":
                yield p
    if not top_down:
        yield co


def _set_writable(co: Path, writable: bool) -> None:
    """Unlock top-down (a directory must be writable before its children can be
    changed), lock bottom-up (children first, then the directory holding them)."""
    for p in _worktree_paths(co, top_down=writable):
        mode = p.stat().st_mode
        p.chmod(mode | stat.S_IWUSR if writable else mode & ~_WRITE)


def _unlocked(co: Path) -> "list[str]":
    return ["writable: %s" % (p.relative_to(co).as_posix() or ".")
            for p in _worktree_paths(co) if p.stat().st_mode & _WRITE]


def _state(co: Path, sha: str) -> "tuple[str, list[str]]":
    if not co.exists():
        return "absent", []
    try:
        head = git_text(co, "rev-parse", "HEAD")
        dirty = run_git(co, "status", "--porcelain", "--untracked-files=all").decode(
            "utf-8", "replace")
    except GitError as e:
        return "edited", ["not a readable checkout (%s)" % e]
    changed = [ln[3:] for ln in dirty.splitlines() if ln.strip()] + _unlocked(co)
    if head != sha:
        changed.insert(0, "HEAD %s is not the pin %s" % (head[:8], sha[:8]))
    return ("edited" if changed else "ready"), changed


def _source(args: argparse.Namespace) -> int:
    try:
        port_root = port_workspace.find_port_root(args.ctx.root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit
    try:
        batch = port_batch_reader.load_batch(port_root, args.batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit
    source, problem = port_batch_reader.find_source(batch, args.source_id)
    if problem:
        return Verdict.unresolved(problem).exit
    sid, sha = str(source.get("id")), str(source.get("pinned_sha"))
    repo, _package = port_drift.source_entry(port_root, sid)
    co = port_batch_reader.source_checkout(port_root, sid, sha)
    apply = bool(getattr(args.opts, "apply", False))
    state, changed = _state(co, sha)
    data = {"batch": batch.name, "source": sid, "sha": sha,
            "path": co.relative_to(port_root).as_posix(), "state": state, "changed": changed,
            "apply": apply}
    if args.remove:
        if apply and co.exists():
            _set_writable(co, True)
            shutil.rmtree(co)
            data["state"] = "removed"
        return _report(args, data, "" if apply else "dry-run: --apply removes it")
    if state == "edited":
        raise hs_family.RefusedError(
            "port source: %s no longer matches %s@%s (%s) — a read-only checkout must not "
            "carry work; `--remove --apply` drops it" % (data["path"], sid, sha[:8],
                                                         "; ".join(changed[:5])))
    if state == "absent" and apply:
        co.parent.mkdir(parents=True, exist_ok=True)
        gi = co.parent / ".gitignore"
        if not gi.is_file():
            gi.write_text(_GITIGNORE, encoding="utf-8")
        try:
            materialize_pin(repo, sha, co, shared=False)
        except hs_family.UsageError as e:
            if co.exists():
                shutil.rmtree(co)
            return Verdict.broken("port source: %s" % e).exit
        _set_writable(co, False)
        data["state"] = "ready"
    return _report(args, data, "" if apply or state == "ready" else
                   "dry-run: --apply checks it out")


def _report(args: argparse.Namespace, data: dict, closing: str) -> int:
    if getattr(args.opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("source %s@%s -> %s: %s" % (data["source"], data["sha"][:8], data["path"],
                                         data["state"]))
        if closing:
            print(closing)
    return int(Verdict.ok(data).exit)


SOURCE = Command(add_args=_args, run=_source)
