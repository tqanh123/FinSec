"""lane — a REAL git worktree of this tree on the remote: bare repo + push + rsync.

Why this exists (measured 260906, three lanes independently): `hs-rops sync` copies the
working tree but not `.git/` — and for a linked worktree `.git` is a FILE pointing at a
gitdir that does not exist over there. The remote then answers `git ls-files` with
rc=128, so every repo gate that enumerates the tree through git went red (60/60 on the
main tree, 61/61 on obs-int, 28 on docs-rev) while the same tree was 0 red locally.
Worse than "no git": gates that `skipif` on the existence of `.git` saw the pointer
file, did not skip, and ran red.

What `lane` does, in order:
  1. `git init --bare` ROPS_BARE on the remote once (default `~/rops/di.git`).
  2. `git push --force` the local HEAD to `refs/heads/lane/<name>` in that bare repo.
  3. Make `~/rops/<name>` a worktree of the bare repo at that commit (`worktree add`
     the first time; `reset --hard` afterwards). A pre-existing plain directory is
     moved aside and everything in LANE_KEEP (the venvs, `logs/`) carried over, so
     nothing is rebuilt.
  4. rsync the working tree OVER the checkout with `--delete` — uncommitted edits and
     gitignored inputs (`plans/`, which the repo gates read) arrive; `.git`, the venvs,
     `logs/` and caches are excluded, and excluded paths are never deleted. `.rops.env*`
     is hidden from the sender only, so it never travels and a stale copy is deleted.
  4b. the HOST checkout's untracked/ignored `plans/` (a linked worktree has none).
  4c. the lane's index is made to mirror the LOCAL index (staged adds/moves/deletes).
  5. `uv sync` there (ROPS_LANE_SYNC_CMD, default `uv sync --frozen --group dev
     --all-extras`) — skipped, saying why, where the repo owns no `uv.lock` and no
     command was set.
  6. ROPS_LANE_SETUP, when declared: the repo's own idempotent setup (a database the
     lane's ROPS_ENV names, a fixture corpus), run in the lane with ROPS_ENV exported.

The remote tree ends up as: tracked files at HEAD + your local modifications on top
+ your local untracked files, with a real index — exactly what the gates measure here.
"""
from __future__ import annotations

import argparse
import base64
import pathlib
import posixpath
import re
import subprocess
import sys
import time

from rops.ropslib import detect, hosts
from rops.ropslib.exec import report, run_remote, wait_for_dir
from rops.ropslib.transport import sq
from rops.ropslib.tree import lane_excludes_for, uncommitted_note


#: Remote-only directories, in the three layers `ROPS_RSYNC_EXCLUDES` already uses:
#: a floor that is always kept, whatever the repo's own commands name (`venvs_in`), and
#: ROPS_LANE_KEEP for what detection cannot reach (a model cache, a fixture corpus).
#: Nothing repo-specific is baked in — `.venv-ci` is THIS repo's spelling and reaches
#: rops through its `.rops.env`, never through this file.
KEEP_FLOOR = (".venv", "logs")


def keep_names(cfg) -> list[str]:
    """Floor + detected + declared, de-duplicated, order stable."""
    out = list(KEEP_FLOOR)
    for n in detect.venvs_in(cfg) + cfg.csv("ROPS_LANE_KEEP"):
        if n and n not in out:
            out.append(n)
    return out


def lane_excludes(cfg) -> list[str]:
    """The rsync excludes for a lane: the shared list, plus every kept name.

    DERIVED from `keep_names`, not maintained beside it. Measured 260910: the keep list
    and the exclude list were two hand-written lists, `.venv-ci` was missing from both,
    and each miss destroyed the same 457 M venv by a different route — `rm -rf` of the
    moved-aside tree on a rebuild, `--delete` as a stray on every run after.
    """
    out = list(lane_excludes_for(cfg.repo))
    for n in keep_names(cfg):
        if not any(e.rstrip("/") == n for e in out):
            out.append(n + "/")
    return out


#: `.rops.env` (and the `.bak` doctor keeps beside it) is local config, never part of
#: the tree. `-s` hides it from the SENDER only: a plain `--exclude` would also PROTECT
#: the receiver's copy from `--delete`, so a copy an older lane run carried over would
#: sit there for good as `?? .rops.env` — one dirty path on every lane report.
ROPS_ENV_FILTER = ("--filter", "-s /.rops.env*")


def tree_rsync_args(cfg) -> list[str]:
    """Step 4's filter/exclude flags, in rsync order (first match wins)."""
    out = list(ROPS_ENV_FILTER)
    for e in lane_excludes(cfg):
        out += ["--exclude", e]
    return out


def _staged(repo) -> tuple[list[str], list[str]]:
    """(paths to add, paths to drop): the local index against HEAD, a rename split into
    delete + add (`--no-renames`), `.rops.env*` never included."""
    r = _local(repo, "diff", "--cached", "--no-renames", "--name-status", "-z", "HEAD")
    if r.returncode != 0:
        return [], []
    parts = r.stdout.split("\0")
    add: list[str] = []
    drop: list[str] = []
    for status, path in zip(parts[0::2], parts[1::2]):
        if not path or posixpath.basename(path).startswith(".rops.env"):
            continue
        (drop if status.startswith("D") else add).append(path)
    return add, drop


def _b64_nul(paths: list[str]) -> str:
    return base64.b64encode("".join(p + "\0" for p in paths).encode()).decode()


def index_mirror_cmd(repo, lane_dir: str) -> str | None:
    """The remote command that makes the lane's index say what the LOCAL index says,
    or None when nothing is staged here.

    Step 4 moves bytes, never the index, so the lane's index stays at HEAD: a staged
    `git mv`/`git add`/`git rm` here is a path `git ls-files` lists here and not there,
    and every gate that enumerates the tree through git went red over there only.
    Mirroring the local index (not `git add -A`) keeps an UNSTAGED new file untracked on
    both sides: the same path set either side, nothing more. A staged path the lane does
    not have (rsync-excluded) is skipped rather than aborting the mirror. The fingerprint
    (ropslib/tree.py) is index-independent by design, and the dirty count reads
    `git status`, where a staged path still differs from HEAD — both keep their meaning.
    Paths travel base64-encoded and NUL-separated: no quoting of any byte in a name.
    """
    add, drop = _staged(repo)
    if not add and not drop:
        return None
    script = f"""set -e
cd {lane_dir}
A=$(mktemp) D=$(mktemp)
trap 'rm -f "$A" "$D"' EXIT
printf %s {sq(_b64_nul(add))} | base64 -d | while IFS= read -r -d '' f; do
  if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\\0' "$f"; fi
done > "$A"
printf %s {sq(_b64_nul(drop))} | base64 -d > "$D"
if [ -s "$A" ]; then git add -f --pathspec-from-file="$A" --pathspec-file-nul; fi
if [ -s "$D" ]; then git rm -q --cached --ignore-unmatch --pathspec-from-file="$D" --pathspec-file-nul; fi
"""
    return f"bash -c {sq(script)}"


def _expand_home(path: str, home: str) -> str:
    p = path.strip()
    if home:
        if p == "~" or p.startswith("~/"):
            p = home + p[1:]
        elif p == "$HOME" or p.startswith("$HOME/"):
            p = home + p[len("$HOME"):]
    return posixpath.normpath(p) if p else p


def lane_note(cfg, name: str, lane: str, home: str) -> str | None:
    """The closing advice, or None when run/test already land in this lane.

    Compared after expanding `~`/`$HOME` against the REMOTE home: `/home/u/rops/x` and
    `~/rops/x` are one directory. The advice never carries a `~` path — pasted unquoted,
    it expands on THIS machine, to this machine's home.
    """
    current = cfg.get("ROPS_REMOTE_DIR")
    if current and _expand_home(current, home) == _expand_home(lane, home):
        return None
    if cfg.get("ROPS_LANE") == name:
        return (f"note: ROPS_LANE is already {name!r}, but ROPS_REMOTE_DIR={current!r} "
                f"[{cfg.source('ROPS_REMOTE_DIR')}] outranks it — unset ROPS_REMOTE_DIR "
                f"so run/test land in the lane")
    return (f"note: run/test read ROPS_REMOTE_DIR={current or 'unset'!r}, not this lane — "
            f"set ROPS_LANE={name} so they land in it")


def keep_fragment(names) -> str:
    """The `mv $OLD/<x> $L/<x>` lines, generated from the list so a name cannot be added
    to one place and forgotten in the other."""
    return "\n".join(
        f'[ -d "$OLD/{n}" ] && mv "$OLD/{n}" "$L/{n}" && echo "kept {n}"' for n in names)




def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", default=None,
                   help="lane name → remote ~/rops/<name> and branch lane/<name> "
                        "(default: ROPS_LANE, else the local directory name)")
    p.add_argument("--no-sync", action="store_true", help="skip the uv sync step")
    p.add_argument("--force", action="store_true",
                   help="rebuild even while processes are running inside the lane (they will read a tree that no longer exists)")


def _local(repo, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *argv], text=True, capture_output=True)


def plans_tracked_excludes(repo: pathlib.Path) -> list[str]:
    """rsync `--exclude` patterns for every path tracked, in `repo`, under `plans/`.

    Step 4 already rsynced the local worktree (tracked HEAD + local edits) over the
    lane, so its `plans/` already matches. Step 4b's job is only what a linked
    worktree's own `plans/` cannot carry -- untracked or ignored content that lives
    solely in the HOST checkout. Measured 260925: without this, step 4b re-copied the
    host's own version of every tracked `plans/` file over what step 4 had just
    gotten right, so the lane went dirty and its fingerprint stopped matching local
    even though nothing about the local tree was wrong.

    `git ls-files` lists index paths regardless of whether they still exist on disk,
    which is exactly what is wanted here: a path tracked at HEAD is step 4's to carry,
    present or deleted, never step 4b's to reintroduce.
    """
    r = _local(repo, "ls-files", "-z", "--", "plans")
    if r.returncode != 0 or not r.stdout:
        return []
    out = []
    for raw in r.stdout.split("\0"):
        if not raw:
            continue
        rel = raw[len("plans/"):] if raw.startswith("plans/") else raw
        if rel:
            out.append(rel)
    return out


#: The default lane sync. `--frozen` makes uv refuse anywhere the repo carries no
#: `uv.lock` — that refusal is a fact about the repo, decidable here, so it is decided
#: here rather than discovered by the remote dying after every earlier step was right.
DEFAULT_SYNC_CMD = "uv sync --frozen --group dev --all-extras"


def sync_plan(cfg, local_root: pathlib.Path) -> tuple[str | None, str]:
    """The sync command this lane should run, or None with the reason it should not.

    An operator-set ROPS_LANE_SYNC_CMD always wins — it is the "force one" answer to
    both skips below. Otherwise: a lock is syncable as frozen; a `pyproject.toml`
    without a lock is the measured sdlc-harness case (`--frozen` refuses on the
    remote and the lane returned rc!=0); neither file means uv has nothing to sync
    at all, which the old code still tried to run.
    """
    custom = cfg.get("ROPS_LANE_SYNC_CMD")
    if custom:
        return custom, "operator-set sync cmd"
    if (local_root / "uv.lock").is_file():
        return DEFAULT_SYNC_CMD, "locked"
    if (local_root / "pyproject.toml").is_file():
        return (None, "sync: skipped — no uv.lock here, --frozen would refuse; "
                      "set ROPS_LANE_SYNC_CMD to force one")
    return (None, "sync: skipped — no pyproject.toml and no uv.lock here, uv has "
                  "nothing to sync; set ROPS_LANE_SYNC_CMD to force one")


def package_count_cmd(cmd: str) -> str:
    """Which command counts installed packages after `cmd` synced the venv.

    `uv pip list` only answers for uv's OWN venv. A declared, non-uv
    `ROPS_LANE_SYNC_CMD` (the operator's answer for a repo with no `uv.lock` —
    R3/R4, a plain `pip install`) populates whatever interpreter `python3`
    resolves to under the same PATH; `uv pip list` there reports on a DIFFERENT
    venv than the one just synced (measured: `venv: 1 packages` after a real
    install of dozens). Keep `uv pip list` only where the sync itself IS uv's.
    """
    first = cmd.split()[0] if cmd.split() else ""
    return ("uv pip list 2>/dev/null | tail -n +3 | wc -l" if first == "uv"
            else "python3 -m pip list --format=freeze 2>/dev/null | wc -l")


#: Prints, one per line, every installed dist that is editable but has no working
#: import hook: no `.pth` in its RECORD, or a `.pth` whose every path line names a
#: dir that does not exist. A dist whose source dir holds no code (a meta dist: a
#: pyproject and docs, tests alone, or nothing yet) has nothing to hook and is not
#: printed; a source dir that does not exist still is. The walk follows symlinked dirs
#: (each dir once, by device and inode) and does not enter a subdir that is another
#: dist: no __init__.py, and a setup.py, a setup.cfg with [metadata], or a pyproject
#: with [project], [tool.poetry] or [build-system] (one holding only tool config is
#: none), so a workspace root's members do not lend it code. Dirs are visited
#: cheapest first: a dir costs its parent's cost, plus one, plus its rank among its
#: sibling dirs by name, so neither a wide nor a deep data dir next to the code dir
#: is walked through before it. A dir past DIR_ENTRIES entries is cut short; the walk
#: reads at most BUDGET dirs and ENTRY_BUDGET entries, and the whole script stops
#: walking DEADLINE_S seconds after it starts, well inside the probe's timeout. A walk
#: that ends without finding code but did not see everything (a budget or the
#: deadline ran out, a dir was cut short, or a dir or entry could not be read) answers
#: neither: it prints `walk-incomplete NAME`, so a large data tree can neither time
#: the probe out into silence nor raise a false alarm. A broken or looping symlink is
#: skipped. Run with the lane venv's interpreter.
EDITABLE_CHECK = """\
import errno, heapq, json, os, time
from importlib.metadata import distributions
from itertools import islice
from urllib.parse import unquote, urlparse
CODE = (".py", ".pyx", ".so", ".pyd")
SKIP = {"tests", "test", "__pycache__", "node_modules"}
BUDGET = 5000
ENTRY_BUDGET = 200000
DIR_ENTRIES = 5000
DEADLINE_S = 60
END = time.monotonic() + DEADLINE_S
GONE = (errno.ENOENT, errno.ELOOP)
def has_code(url):
    src = unquote(urlparse(url or "").path)
    if not src or not os.path.isdir(src):
        return True
    seen, heap, pushed, whole = set(), [(0, 0, src)], 0, True
    dirs_left, entries_left = BUDGET, ENTRY_BUDGET
    while heap:
        cost, _, top = heapq.heappop(heap)
        try:
            st = os.stat(top)
        except OSError as e:
            whole = whole and e.errno in GONE
            continue
        if (st.st_dev, st.st_ino) in seen or (top != src and is_dist(top)):
            continue
        seen.add((st.st_dev, st.st_ino))
        if dirs_left == 0 or entries_left == 0 or time.monotonic() > END:
            return None
        dirs_left -= 1
        cap = min(entries_left, DIR_ENTRIES)
        try:
            with os.scandir(top) as it:
                entries = list(islice(it, cap + 1))
        except OSError:
            whole = False
            continue
        if any(e.name.endswith(CODE) and kind(e, "file") for e in entries):
            return True
        if len(entries) > cap:
            whole = False
        entries_left -= min(len(entries), cap)
        subs = []
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP:
                continue
            d = kind(e, "dir")
            if d is None:
                whole = False
            elif d:
                subs.append(e)
        for rank, e in enumerate(sorted(subs, key=lambda e: e.name)):
            pushed += 1
            heapq.heappush(heap, (cost + 1 + rank, pushed, e.path))
    return False if whole else None
def kind(entry, what):
    try:
        return entry.is_file() if what == "file" else entry.is_dir()
    except OSError as e:
        return False if e.errno in GONE else None
def is_dist(path):
    if os.path.isfile(os.path.join(path, "__init__.py")):
        return False
    if os.path.isfile(os.path.join(path, "setup.py")):
        return True
    for name, marks in (("setup.cfg", ("[metadata]",)),
                        ("pyproject.toml", ("[project]", "[tool.poetry]", "[build-system]"))):
        try:
            with open(os.path.join(path, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if any(m in text for m in marks):
            return True
    return False
for d in distributions():
    try:
        du = json.loads(d.read_text("direct_url.json") or "{}")
    except ValueError:
        continue
    if not (du.get("dir_info") or {}).get("editable"):
        continue
    hooks = [f for f in (d.files or []) if str(f).endswith(".pth")]
    live = False
    for f in hooks:
        try:
            lines = open(d.locate_file(f), encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if ln and not ln.startswith("#") and (ln.startswith("import") or os.path.isdir(ln)):
                live = True
    if live:
        continue
    code = has_code(du.get("url"))
    if code is None:
        print("walk-incomplete", d.metadata["Name"])
    elif code:
        print("no-import-hook", d.metadata["Name"])
"""


_DIST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def unhooked(probe_out: str, tag_wanted: str = "no-import-hook") -> list[str]:
    """The dist names EDITABLE_CHECK printed under `tag_wanted`; any other line on
    stdout is not one."""
    names = []
    for line in (probe_out or "").splitlines():
        tag, _, name = line.strip().partition(" ")
        if tag == tag_wanted and _DIST_NAME.fullmatch(name):
            names.append(name)
    return names


def repair_editables(cfg, host, lane: str, path: str, cmd: str) -> int:
    """Reinstall, by name, every editable dist the sync left without an import hook.

    uv builds a workspace member's editable once. Built while the member's code dir
    was still empty, it carries no hook, and a later `uv sync` sees the member
    installed and leaves it: `No module named ...` with the dist-info present. Only a
    uv sync is checked — `--reinstall-package` is uv's flag.
    """
    if not cmd.split()[:2] == ["uv", "sync"]:
        return 0
    probe = (f"cd {lane} && export PATH={path}:$PATH && "
             f"uv run --no-sync python -c {sq(EDITABLE_CHECK)} 2>/dev/null")
    first = host.out(probe, timeout=120)
    unknown = unhooked(first, "walk-incomplete")
    if unknown:
        print(f"editable: {', '.join(unknown)} — no hook, and the walk of its dir did not "
              f"finish (too many files, or a dir it cannot read), so whether there is "
              f"code to hook is unknown; not reinstalled")
    bad = unhooked(first)
    if not bad:
        return 0
    print(f"editable: {', '.join(bad)} installed with no import hook "
          f"(built before its code existed) — reinstalling")
    again = " ".join(f"--reinstall-package {b}" for b in bad)
    rc = report(run_remote(cfg, host, f"cd {lane} && export PATH={path}:$PATH && {cmd} {again}",
                           timeout_s=900, tail=3, label="lane-editable-reinstall"))
    if rc:
        return rc
    still = unhooked(host.out(probe, timeout=120))
    if still:
        print(f"!! editable: still no import hook for {', '.join(still)} — "
              f"its imports will fail on this lane")
    return 0


def run(args: argparse.Namespace) -> int:
    cfg = args.cfg
    host = hosts.gate(args, need_dir=False)[0]
    name = args.name or cfg.get("ROPS_LANE") or cfg.repo.name
    return build(cfg, host, name, no_sync=args.no_sync, force=args.force)


def run_setup(cfg, host, lane: str, name: str) -> int:
    """Step 6: `ROPS_LANE_SETUP` in the lane, after its sync. The repo declares it and
    owns its idempotence; rops only runs it, through the same runner as the sync
    (ROPS_ENV exported, the lane's locks held), and says how it ended."""
    cmd = cfg.get("ROPS_LANE_SETUP")
    if not cmd:
        return 0
    path = cfg.get("ROPS_PATH_PREPEND", "$HOME/.local/bin")
    rc = report(run_remote(cfg, host, f"cd {lane} && export PATH={path}:$PATH && {cmd}",
                           timeout_s=900, tail=5, label=f"lane-{name}-setup"))
    if rc:
        print(f"!! setup failed rc={rc} (ROPS_LANE_SETUP: {cmd}) — the lane is synced but "
              f"what the setup provides is not there")
    else:
        print(f"setup: rc=0 (ROPS_LANE_SETUP: {cmd})")
    return rc


def build(cfg, host, name: str, *, no_sync: bool = False, force: bool = False) -> int:
    """The steps `lane` runs from the CLI, as a callable other verbs can drive
    directly — `run --lane NAME` (R5) rebuilds a lane inline through this SAME
    sequence rather than re-implementing it, so the two never drift apart.
    """
    bare = cfg.get("ROPS_BARE", "~/rops/di.git")
    lane = f"~/rops/{name}"
    branch = f"lane/{name}"
    rc = wait_for_dir(cfg, host, lane)
    if rc:
        return rc

    head = _local(cfg.repo, "rev-parse", "HEAD").stdout.strip()
    if not head:
        print(f"!! {cfg.repo} is not a git tree; lane needs a HEAD to push", file=sys.stderr)
        return 2

    # 1. bare repo, once. `command -v git` first: "not a git tree" and "no git binary"
    #    are different diagnoses and the second one is a one-line apt away.
    r = host.run(f"command -v git >/dev/null || {{ echo no-git; exit 3; }}; "
                 f"test -d {bare} || git init -q --bare {bare}; echo bare-ok", timeout=60)
    if r.returncode:
        print(f"!! remote: {r.stdout.strip() or r.stderr.strip()}", file=sys.stderr)
        return r.returncode

    # 2. push HEAD. The ssh alias carries port/key from ~/.ssh/config, so the url is
    #    scp-like and relative to the remote home. `--no-verify`: the pre-push hook
    #    (`scripts/local_ci_gates.py`) is the gate for the REAL remote; here it ran the
    #    whole local CI before a single object moved (measured: 325 s, 12K transferred)
    #    and the lane exists precisely so those gates run over there instead.
    url = f"{host.name}:{bare.removeprefix('~/')}"
    t = time.perf_counter()
    r = _local(cfg.repo, "push", "-q", "--force", "--no-verify", url, f"HEAD:refs/heads/{branch}")
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
        return r.returncode
    print(f"push {head[:8]} → {url} {branch} {time.perf_counter() - t:.1f}s")

    # 3. worktree at that commit. Detached on purpose: `reset --hard` later moves HEAD
    #    without caring which branch the checkout thinks it is on.
    # Busy check FIRST. Both paths below mutate the tree under a running process —
    # `mv` aside on the first build, `reset --hard` + rsync --delete afterwards — and
    # pytest does not die from that: it keeps reading through old inodes and prints a
    # summary for a tree that no longer exists, indistinguishable from a valid run
    # (dev5, 260906: a full suite at 30 % inside the lane while `lane` was re-run).
    keep = keep_fragment(keep_names(cfg))
    script = f"""
set -e
L={lane}; B={bare}
LP=$(cd "$L" 2>/dev/null && pwd -P || true)
if [ -n "$LP" ]; then
  BUSY=$(for p in /proc/[0-9]*; do c=$(readlink "$p/cwd" 2>/dev/null) || continue; case "$c" in "$LP"|"$LP"/*) echo "${{p#/proc/}} $(tr '\\0' ' ' < "$p/cmdline" | cut -c1-80)";; esac; done)
  if [ -n "$BUSY" ] && [ "{int(bool(force))}" != "1" ]; then
    echo "!! lane busy — processes with cwd inside $L (pass --force to rebuild anyway):"; echo "$BUSY" | head -8; exit 4
  fi
fi
if [ -e "$L/.git" ] && git -C "$L" rev-parse --is-inside-work-tree >/dev/null 2>&1 \\
   && [ "$(git -C "$L" rev-parse --git-common-dir 2>/dev/null | xargs -I{{}} sh -c 'cd {{}} && pwd -P')" = "$(cd $B && pwd -P)" ]; then
  git -C "$L" reset -q --hard {head}
  echo "worktree-reset $L"
else
  if [ -e "$L" ]; then OLD="$L.pre-lane-$(date +%s)"; mv "$L" "$OLD"; echo "moved-aside $OLD"; fi
  git -C "$B" worktree prune
  git -C "$B" worktree add -q --detach "$L" {head}
  if [ -n "$OLD" ]; then
{keep}
    rm -rf "$OLD"
  fi
  echo "worktree-add $L"
fi
git -C "$L" rev-parse --short HEAD
"""
    r = host.run(f"bash -c {sq(script)}", timeout=300)
    print(r.stdout.strip())
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
        return r.returncode

    # 4. rsync over the checkout. --delete so files removed locally vanish there; the
    #    excludes are protected (rsync never deletes an excluded path without
    #    --delete-excluded), so the pointer `.git`, the venv and the logs survive.
    dst = f"{host.name}:{lane.removeprefix('~/')}/"
    t = time.perf_counter()
    r = subprocess.run(["rsync", "-az", "--delete", "--stats", *tree_rsync_args(cfg),
                        str(cfg.repo).rstrip("/") + "/", dst],
                       text=True, capture_output=True)
    for line in r.stdout.splitlines():
        if line.startswith(("Number of files:", "Total transferred")):
            print(line)
    print(f"rsync {time.perf_counter() - t:.2f}s rc={r.returncode}")
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
        return r.returncode

    # 4b. `plans/` lives ONLY in the host tree: a linked worktree has none (the harness
    #     fences plans/ to the host, F2), so rsync from the worktree carries nothing and
    #     the gates that read plan files SKIP — which reads exactly like green (dev5,
    #     260906: 10/10 worktrees plans=0, only the host tree has it). Take it from the host.
    #     Whatever the LOCAL worktree itself tracks under plans/, step 4 already carried
    #     (HEAD + local edits) -- never let the host's own copy of a tracked path land
    #     back on top of it (measured 260925: it did, and the lane went dirty).
    common = _local(cfg.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    host_root = pathlib.Path(common).parent if common else cfg.repo
    plans_src = host_root / "plans"
    if plans_src.is_dir():
        ex = ["--exclude", "__pycache__/", "--exclude", "*.py[cod]"]
        for p in plans_tracked_excludes(cfg.repo):
            ex += ["--exclude", p]
        r = subprocess.run(["rsync", "-az", *ex, str(plans_src) + "/", dst + "plans/"],
                           text=True, capture_output=True)
        if r.returncode:
            print(r.stderr.strip(), file=sys.stderr)
            return r.returncode
    n_plans = host.out(f"ls {lane}/plans 2>/dev/null | wc -l", timeout=60)
    print(f"plans/: {n_plans} entries on the lane (untracked/ignored plans copied from the "
          f"host checkout {plans_src}: a linked worktree has none, and the gates read them)")

    # 4c. the index. Step 4 moved bytes; the lane's index still says HEAD.
    mirror = index_mirror_cmd(cfg.repo, lane)
    if mirror:
        add, drop = _staged(cfg.repo)
        r = host.run(mirror, timeout=120)
        if r.returncode:
            print(f"!! index mirror failed: {(r.stderr or r.stdout or '').strip()}", file=sys.stderr)
            return r.returncode
        print(f"index: {len(add)} staged add/modify, {len(drop)} staged delete mirrored from local")

    # 5. the remote index now describes the same tree the gates would see here.
    dirty = host.out(f"cd {lane} && git status --porcelain | wc -l && git ls-files | wc -l", timeout=120)
    n_dirty, n_tracked = (dirty.split() + ["?", "?"])[:2]
    print(f"remote {lane}: HEAD {head[:8]}, {n_tracked} tracked, {n_dirty} dirty path(s)")
    note = uncommitted_note(cfg.repo)
    if note:
        print(note)

    if not no_sync:
        # `--all-extras` by default: a bare `uv sync` PRUNES what the extras carry
        # (langfuse, and with it urllib3/wrapt), and every `importorskip` on it turns
        # into a SKIP — 22 obs tests went quiet and the run read as "7282 passed, better
        # than baseline" while measuring less (dev5, 260906). The venv is echoed as a
        # count so a narrow and a full sync no longer print the same thing.
        cmd, why = sync_plan(cfg, cfg.repo)
        if cmd is None:
            # Nothing was sent, so there is no venv to count either: a package count
            # over an unsynced venv is a number that reads like evidence.
            print(why)
        else:
            path = cfg.get("ROPS_PATH_PREPEND", "$HOME/.local/bin")
            rc = report(run_remote(cfg, host, f"cd {lane} && export PATH={path}:$PATH && {cmd}",
                                   timeout_s=900, tail=3, label=f"lane-{name}-sync"))
            if rc:
                return rc
            rc = repair_editables(cfg, host, lane, path, cmd)
            if rc:
                return rc
            n_pkg = host.out(f"cd {lane} && export PATH={path}:$PATH && {package_count_cmd(cmd)}",
                             timeout=120)
            print(f"venv: {n_pkg} packages (sync cmd: {cmd})")

    rc = run_setup(cfg, host, lane, name)
    if rc:
        return rc

    if cfg.get("ROPS_REMOTE_DIR") != lane:
        note = lane_note(cfg, name, lane, host.out('printf %s "$HOME"', timeout=30))
        if note:
            print(note)
    return 0
