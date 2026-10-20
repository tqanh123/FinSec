"""Which tree is the remote command about to run on — and is it the one you edited?

Measured 260906 (dev5, trace-lead): `hs-rops lane` refused with `lane busy` (rc 4), and the
`hs-rops lint` / `hs-rops typecheck` issued right after ran on the PREVIOUS tree and printed
`All checks passed` / `Success: 1253 files`. The lane still had the old HEAD and
`grep -c batch_detection` = 0. A refused sync and a successful sync produce
byte-identical output from the next verb.

Comparing HEADs is not enough: in that very case both HEADs were 1803fbf3 — the
difference was an uncommitted patch. So the fingerprint covers what `lane` carries:
the diff against HEAD (staged + unstaged, binary) and the content of every untracked,
non-ignored file. Same script on both sides, same git, same answer.

Every verb that runs a command prints one `tree:` line first and REFUSES on a
mismatch. `--stale-ok` runs on the old tree anyway — and says so in the line.
"""
from __future__ import annotations

import pathlib
import subprocess

from hs_family.codes import EXIT_REFUSED
from rops.ropslib.prog import PROG
from rops.ropslib.transport import sq

#: What `lane` does NOT carry across, so the fingerprint must not read it either:
#: the remote has its own `.venv`, `logs/` and `.rops.env` as untracked files, and the
#: local side has session state the lane never sees. `lane` imports this list; the two
#: stay one list on purpose — a path excluded from rsync but read by the fingerprint
#: would make every lane report "different" forever. `plans/` reaches the lane from the
#: host tree (F2), so it is outside the fingerprint too — but NOT through this list
#: (rsync must still carry it); it has its own carve-out next to `fp_script`, covering
#: both the ignored shape (document-intake) and the tracked one (sdlc-harness).
#: `.venv-ci/` is spelled out next to `.venv/`: the two are different strings and the
#: first does not match the second. Measured 260910 on sdlc-harness — every ROPS_CMD_*
#: runs through `$PWD/.venv-ci/bin`, that venv exists only on the lane, and `--delete`
#: called it a stray and removed it on every lane run until it was named here.
LANE_EXCLUDES = (
    ".git", ".venv/", ".venv-ci/", "/logs/", "__pycache__/", "*.py[cod]", ".mypy_cache/",
    ".pytest_cache/", ".ruff_cache/", "node_modules/", ".harness/", "/harness/",
    "harness/state/", "credentials.yaml", ".env", ".env.local", ".credential/",
    ".idea/", "*.egg-info/",
    # Session state and data corpora: never inputs of a test, and measured at 3.3 G
    # (`.claude/` on the main tree) and 1.3 G (golden). The golden tree reaches the
    # remote ONCE, at the route `golden_root()` tries last (~/Documents/document-intake-golden).
    "/.claude/", "/golden/", "/golden_data/", "/document-intake-golden/", "/Data_Invoice*/",
    "/data_invoice/", "/manual_test/", "/golden-replay/",
    # The CI runner's own working directory: builds/, per-job logs, the expanded
    # document. Measured 260910 -- one `hs-rops ci` run left it behind, the fingerprint
    # changed, and EVERY later verb refused the very tree it had just measured on.
    # Output of a run, never an input to one.
    ".gitlab-ci-local/",
    # Coverage reports a lane run writes at the tree root. Measured 260928: the next
    # sync's `--delete` replaced a fresh `coverage-harness.json` with the host's
    # five-day-old copy, and the group numbers read afterwards were the stale ones.
    "/coverage-*.json", "/coverage-*.xml", "/.coverage", "/.coverage.*",
)
#: Excludes that hold ONLY where the directory is install output rather than source.
#: `/harness/` came from document-intake, which tracks none of it and regenerates it on the
#: far side; sdlc-harness ships it as 3032 tracked files. Measured 260910: the lane carried
#: none of them, so every test under `harness/` measured HEAD while the working tree said
#: otherwise — and once the fingerprint filtered by the same list, the two sides agreed and
#: the gap stopped being visible at all. Tracked-ness is the discriminator git already
#: knows, so one rule serves both repos and neither carries the other's shape.
CONDITIONAL_EXCLUDES = ("/harness/",)


def lane_excludes_for(repo) -> tuple[str, ...]:
    """LANE_EXCLUDES minus any CONDITIONAL_EXCLUDES the repo actually tracks.

    A path git tracks is source: the lane must carry the working-tree version, exactly as
    it carries every other tracked file. A path git does not track is output the far side
    owns, and shipping it would overwrite the lane's own installed copy.
    """
    keep = []
    for e in CONDITIONAL_EXCLUDES:
        name = e.strip("/")
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", "-z", "--", name],
                           capture_output=True)
        if r.returncode != 0 or not r.stdout.strip():
            keep.append(e)          # untracked here -> the exclude stands
    return tuple(e for e in LANE_EXCLUDES
                 if e not in CONDITIONAL_EXCLUDES or e in keep)


#: Written by `hs-rops doctor` (with its `.bak`), excluded via `.git/info/exclude` locally,
#: never carried to a lane (`lane.ROPS_ENV_FILTER`) — never part of the tree.
FP_EXCLUDES = LANE_EXCLUDES + (".rops.env*",)

#: The `plans/` OVERLAY, measured 260929 (dev lane, sdlc-harness): `lane` step 4b rsyncs
#: the HOST checkout's untracked `plans/` content onto the lane, and only the far side
#: had it, so every `run --lane` refused at the same HEAD until the file reached a commit
#: both trees shared. The overlay is the host's, not the editing worktree's, so it sits
#: outside the fingerprint — but a TRACKED plans file edited in the worktree is a real
#: edit and must keep moving it. Hence `-x` only, never the diff pathspec: `/plans/`
#: (root-anchored — a bare `plans/` would also swallow an untracked fixture under some
#: `sub/plans/` that is not the host overlay) joins BOTH `ls-files` listings (the
#: untracked `-o` AND `-ci`, whose `comm` subtraction also drops a STAGED plans add from
#: the `--diff-filter=A` channel — measured: with `-x` on both, staged and untracked
#: copies of the same bytes read equal, no separate pathspec needed), while the main
#: diff keeps seeing tracked-plans edits. In a repo that IGNORES `plans/`
#: (document-intake) both channels already dropped it; nothing changes.
PLANS_OVERLAY = ("/plans/",)

#: HEAD (8 hex) and one hash over the working-tree delta, INDEPENDENT of the index: a
#: staged `git add` on one side and the same file untracked on the other must read equal
#: (measured 260906: a trial merge left 18 new files staged locally and untracked on the
#: lane — same bytes, different fingerprint, `run` refused a tree that WAS the local one).
#: So the diff carries only modified/deleted tracked files (`--diff-filter=a` drops
#: additions), and every new file — untracked OR added-in-index — is listed once, sorted,
#: as path + blob hash. `bash -c` on both sides: the remote login shell is zsh and the local
#: one may be anything; the script must mean the same thing in both places.
#: The excludes reach BOTH new-file lists. `-x` only filters `ls-files -o`; a staged copy
#: of an excluded path (`git add -A` on a lane stages its untracked `.rops.env`) came
#: back through `--diff-filter=A` and the fingerprint flipped (measured 260907, lead
#: lane: same bytes, `!= local`). `ls-files -ci` names the cached paths the same
#: patterns ignore; `comm -23` drops them from the staged list.
def _x_of(excludes) -> str:
    return " ".join(f"-x {sq(e)}" for e in excludes)


#: The SAME list again, as a pathspec — `-x` reaches `ls-files -o` and nothing else, so a
#: TRACKED file under an excluded dir stayed in `git diff` while rsync skipped it. Measured
#: 260910: one edit under `/harness/` (shipped source in this repo, install output in the
#: one these excludes were written for) made every `run` refuse forever, `lane` reporting
#: rc=0 with a fp that never moved. Trailing slashes go: a pathspec matches a directory by
#: its plain name, and `harness/state/` must keep its depth. Leading `/` means repo-root in
#: both dialects but git wants it spelled `:(top)`, which is the default here anyway.
def _pathspec_of(excludes) -> str:
    return " ".join(
        sq(f":(exclude,glob){e.strip('/')}") + " " + sq(f":(exclude,glob){e.strip('/')}/**")
        for e in excludes)


def fp_script(excludes=FP_EXCLUDES) -> str:
    """The fingerprint script for one exclude set.

    A FUNCTION, not a constant: the set now depends on what the repo tracks
    (`lane_excludes_for`), and both sides must be handed the SAME one. `remote()` takes the
    script the caller built from the LOCAL repo — asking the far side its own question would
    compare two different measurements and call the difference a stale tree.

    `--no-renames` on BOTH `git diff HEAD` calls: git's own rename detection (ON by
    default here, git 2.43) describes the identical bytes two different ways
    depending on how they arrived. A staged `git mv` plus an edit reads locally as
    one `R085 old->new` entry; the SAME final content, carried to a lane by rsync
    (which moves bytes, never the index), reads there as a plain deletion of the old
    path plus an UNTRACKED new one — measured, `fp` stayed `!=` between two trees
    that were byte-identical. `--no-renames` forces both sides to describe it the
    second way (delete + new blob), so they agree.

    `-z` on every listing feeding the `while read` loop below (`ls-files -o`,
    the `diff --name-only`/`ls-files -ci` pair `comm` subtracts): plain `--name-only`
    C-quotes any non-ASCII byte in a path under git's default `core.quotePath=true`,
    and the quoted string is not the real file — `git hash-object -- "$f"` against it
    fails silently (no `set -e`), so the loop's stdout ends up derived from the
    QUOTED FILENAME alone, never the file's actual bytes: editing a non-ASCII
    untracked file's content would then never move the fingerprint at all.

    `LC_ALL=C` for the whole script: `comm` needs both inputs in ITS order, and the two
    tools need not agree on what a locale means — measured on homelab, uutils `comm`
    compares bytes under `en_US.UTF-8` while `sort` collates there, so `comm` stopped at
    "not in sorted order" and a staged-but-ignored path leaked into the hash.
    """
    x, ps = _x_of(excludes + PLANS_OVERLAY), _pathspec_of(excludes)
    return (
        "export LC_ALL=C; git rev-parse --short=8 HEAD && "
        f"{{ git diff HEAD --no-renames --binary --full-index --diff-filter=a -- . {ps}; "
        f"{{ git ls-files -o --exclude-standard -z {x}; "
        "comm -z -23 <(git diff HEAD --no-renames --name-only -z --diff-filter=A | sort -z) "
        f"<(git ls-files -ci --exclude-standard -z {x} | sort -z); }} | sort -z -u | "
        "while IFS= read -r -d '' f; do "
        "printf '%s\\0' \"$f\"; git hash-object -- \"$f\"; done; } "
        "| git hash-object --stdin && "
        f"git ls-files -z -- . {ps} | git hash-object --stdin"
    )


FP_SCRIPT = fp_script()


def manifest_script(excludes=FP_EXCLUDES) -> str:
    """Per path, what the fingerprint folds in: `<blob> <path>` for an edited, added or
    untracked path, `D <path>` for a deleted one — NUL-separated. Same listings, same
    excludes as `fp_script`, so a fingerprint mismatch can name its paths."""
    x, ps = _x_of(excludes + PLANS_OVERLAY), _pathspec_of(excludes)
    return (
        "export LC_ALL=C; "
        f"git diff HEAD --no-renames --name-only -z --diff-filter=D -- . {ps} | "
        "while IFS= read -r -d '' f; do printf 'D %s\\0' \"$f\"; done; "
        f"{{ git diff HEAD --no-renames --name-only -z --diff-filter=d -- . {ps}; "
        f"git ls-files -o --exclude-standard -z {x}; }} | sort -z -u | "
        "while IFS= read -r -d '' f; do "
        "printf '%s %s\\0' \"$(git hash-object -- \"$f\" 2>/dev/null || echo '?')\" \"$f\"; done"
    )


def _manifest(text: str) -> dict:
    out = {}
    for rec in text.split("\0"):
        tag, _, path = rec.partition(" ")
        if path:
            out[path] = tag
    return out


def differing_paths(loc: dict, rem: dict, limit: int = 10,
                    tracked: "frozenset[str]" = frozenset()) -> list[str]:
    """`path (why)` for each path the two manifests disagree on, sorted, at most `limit`
    plus a count of the rest. A manifest lists only what differs from HEAD, so a path one
    side leaves out is at HEAD there when HEAD tracks it (`tracked`), else absent."""
    rows = []
    for path in sorted(set(loc) | set(rem)):
        a, b = loc.get(path), rem.get(path)
        if a == b:
            continue
        why = ("deleted here, still there" if a == "D"
               else "deleted there, still here" if b == "D"
               else ("edited there, at HEAD here" if path in tracked else
                     "only on the remote — stale there, `sync --delete` or `lane` removes it")
               if a is None else ("edited here, at HEAD there" if path in tracked else
                                  "untracked here, missing there") if b is None
               else "different content")
        rows.append(f"{path} ({why})")
    return rows[:limit] + ([f"... {len(rows) - limit} more"] if len(rows) > limit else [])

NO_GIT = "no-git"


def parse(text: str) -> tuple[str, ...] | None:
    """`HEAD\\nFP\\nIDX` -> (head, fp[:12], idx[:12]); a two-line answer -> (head, fp);
    anything else (empty, `no-git`) -> None. IDX hashes the tracked path set: the bytes
    fingerprint is index-independent, the gates that list the tree through git are not."""
    parts = text.split()
    if len(parts) not in (2, 3) or parts[0] == NO_GIT:
        return None
    return (parts[0],) + tuple(p[:12] for p in parts[1:])


def fp_excludes_for(repo) -> tuple[str, ...]:
    """What the fingerprint must skip for THIS repo: the lane's own list plus `.rops.env*`."""
    return lane_excludes_for(repo) + (".rops.env*",)


def local(repo: pathlib.Path, script: str | None = None) -> tuple[str, ...] | None:
    script = script or fp_script(fp_excludes_for(repo))
    r = subprocess.run(["bash", "-c", script], cwd=str(repo), text=True, capture_output=True)
    return parse(r.stdout) if r.returncode == 0 else None


def remote(host, remote_dir: str, script: str | None = None) -> tuple[str, ...] | None:
    cmd = (f"cd {remote_dir} 2>/dev/null && git rev-parse --is-inside-work-tree >/dev/null 2>&1 "
           f"&& bash -c {sq(script or FP_SCRIPT)} || echo {NO_GIT}")
    return parse(host.out(cmd, timeout=60))


def describe(where: str, loc, rem, *, stale_ok: bool,
             rebuilding: bool = False, synced: bool = False) -> tuple[str, bool]:
    """The `tree:` line and whether to refuse. `rebuilding`: the caller rebuilds on the
    refusal itself (`--lane`/`--sync`), so the mismatch is a note, not advice to run them."""
    if rem is None:
        return f"tree: {where}: no git there (a `{PROG} sync` dir, not a lane) — cannot compare", False
    if loc is None:
        return f"tree: {where}: HEAD {rem[0]} fp {rem[1]} (local is not a git tree — cannot compare)", False
    def _id(t):
        return f"HEAD {t[0]} fp {t[1]}" + (f" idx {t[2]}" if len(t) > 2 else "")
    if loc == rem:
        return f"tree: {where}: {_id(rem)} == local", False
    what = ("different HEAD" if loc[0] != rem[0] else
            "same HEAD, different uncommitted edits" if loc[1] != rem[1] else
            "same HEAD and bytes, different index — git tracks a different path set "
            "there, so a gate listing the tree through git reads other files")
    line = f"tree: {where}: {_id(rem)}  !=  local {_id(loc)} ({what})"
    if stale_ok:
        return line + " — running on the STALE tree (--stale-ok)", False
    if rebuilding:
        return line + " — rebuilding the lane first", True
    if synced:
        return "!! " + line + "\n!! the sync left the trees different — see the paths below", True
    return ("!! " + line + f"\n!! the remote tree is not the one you edited — run `{PROG} lane` "
            f"(or `{PROG} sync`) first, or add --sync to `run`/an alias to do both in one "
            f"command, or pass --stale-ok to measure the old tree on purpose"), True


def uncommitted_note(repo) -> str | None:
    """A note when the local tree carries uncommitted paths (the fingerprint's excludes
    left out): what a lane mirrors is then HEAD plus those edits, never HEAD alone, and
    a measurement meant as the base must not read it as one."""
    try:
        ps = _pathspec_of(fp_excludes_for(repo))
        r = subprocess.run(["bash", "-c", f"git status --porcelain -z -- . {ps}"],
                           cwd=str(repo), text=True, capture_output=True)
    except OSError:  # a note, never a reason the run stops
        return None
    n = len([x for x in r.stdout.split("\0") if x.strip()]) if r.returncode == 0 else 0
    if not n:
        return None
    return (f"note: the local tree has {n} uncommitted path(s) — the remote mirrors HEAD "
            f"plus them, not HEAD alone; for a base measurement of HEAD run "
            f"`{PROG} run --at HEAD -- <cmd>`")


def check(cfg, host, *, stale_ok: bool = False, rebuilding: bool = False,
          synced: bool = False) -> int:
    """Print the `tree:` line; return EXIT_REFUSED (3) on a mismatch unless stale_ok.

    A deliberate policy stop, not a broken read: the remote answered fine, the
    comparison ran fine, and rops declines to run on it ON PURPOSE — the family's
    3, not the 2 a `.rc` read that never captured a real code (exec.py's
    RC_UNREADABLE) or a bad input/config (UsageError) would mean.
    """
    repo = getattr(cfg, "repo", None)
    remote_dir = cfg.get("ROPS_REMOTE_DIR", "~")
    # ONE script for both sides, built from the local repo: the far side's own answer to
    # its own question is not comparable with ours.
    script = fp_script(fp_excludes_for(repo)) if repo else FP_SCRIPT
    loc = local(pathlib.Path(repo), script) if repo else None
    rem = remote(host, remote_dir, script)
    line, refuse = describe(f"{host.name}:{remote_dir}", loc, rem, stale_ok=stale_ok,
                           rebuilding=rebuilding, synced=synced)
    print(line)
    if repo and loc and rem and loc[0] == rem[0] and loc[1] != rem[1]:
        for row in _why_bytes_differ(pathlib.Path(repo), host, remote_dir):
            print(f"   differs: {row}")
    note = uncommitted_note(pathlib.Path(repo)) if repo and loc is not None else None
    if note and not refuse:
        print(note)
    return int(EXIT_REFUSED) if refuse else 0


def _why_bytes_differ(repo: pathlib.Path, host, remote_dir: str) -> list[str]:
    """The paths behind a same-HEAD fingerprint mismatch, asked of both sides with the
    LOCAL repo's script. Empty when either side cannot answer — a note, never a stop."""
    script = manifest_script(fp_excludes_for(repo))
    try:
        r = subprocess.run(["bash", "-c", script], cwd=str(repo), text=True, capture_output=True)
    except OSError:
        return []
    try:
        far = host.out(f"cd {remote_dir} 2>/dev/null && bash -c {sq(script)}", timeout=120)
    except Exception:  # noqa: BLE001 — a note about a mismatch must never be what stops a run
        return []
    if r.returncode or not far:
        return []
    loc, rem = _manifest(r.stdout), _manifest(far)
    ls = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--",
                         *sorted(set(loc) ^ set(rem))], cwd=str(repo), text=True,
                        capture_output=True)
    tracked = frozenset(p for p in ls.stdout.split("\0") if p)
    return differing_paths(loc, rem, tracked=tracked)
