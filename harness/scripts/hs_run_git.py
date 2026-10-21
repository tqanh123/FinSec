#!/usr/bin/env python3
"""hs_run_git.py — the `git` domain's verb set for hs-run.

Promoted from an earlier draft.
That draft's "no precedent for importing harness/hooks/ from harness/scripts/"
claim was wrong — `guard_config.py` (same directory, same shape: a CLI script
that needs a hooks-module for its own logic) already does exactly this:

    _HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(_HERE))
    sys.path.insert(0, str(_HERE.parent / "hooks"))

Matched verbatim below (not `lens_gate.py`'s narrower hooks-only variant,
because this module — like guard_config.py — is also runnable standalone via
`__main__`, so the belt-and-suspenders self-path insert earns its keep here).

`commit-plan` REUSES `secret_scan_before_ship.scan_diff_text` — the same
scanner the pre-ship hook and push_gate's transport backstop already run —
rather than a second regex set. The only new code is `_staged_diff`: the
existing scanner reads unpushed COMMITS or the working-tree pack surface;
commit-plan needs the STAGED diff (`git diff --cached`), a third, distinct
diff this repo had no reader for yet.

Exit-tier deviation from the draft (bug found while wiring this in): the
draft returned EXIT_STEP_REMAINING(11) for "diff is clean, here's the split
plan" — but hs_run.py's dispatcher enforces that an 11 promises a runnable
`hs-run ...` command in `next_action` (`_looks_runnable`), and the split-plan
sentence is prose, not a command. Left as 11 it would have been silently
forced to EXIT_BROKEN(2) by the dispatcher on every clean run, which is a
worse contract than what the draft intended. `ready` is EXIT_NEEDS_MODEL(10)
instead: deciding how to actually stage/commit per the plan is exactly a
"spawn something, it can't run deterministically here" step, which is what
10 means. `commit-plan` never calls `git commit`/`git push`/anything that
mutates the index or HEAD — it only reads `git diff --cached`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "hooks"))

import hs_run_next_command  # noqa: E402
import hs_run_plan  # noqa: E402 — `_resolve_plan_arg`, the SAME permissive --plan
                     # resolution the plan domain itself uses (dir / file-inside-it /
                     # bare name under plans/); restating it here would drift from it
import plan_graph  # noqa: E402 — the plan-graph sidecar reader: phase file
                    # ownership + the plan's own `merger_owned:` list
import scope_match  # noqa: E402 — the one path-glob matcher; a declared entry is a glob
import secret_scan_before_ship as secret_scan  # noqa: E402

# Hardening for every git call below, IMPORTED from the scanner this module already
# depends on rather than restated. git reads the inspected repository's own config,
# and keys such as `core.fsmonitor` name a program git then EXECUTES; this module
# runs git against a repo it does not own, on the path that decides whether a staged
# secret ships. Two modules hardening differently means the weaker one is the way in.
_GIT_SAFE = secret_scan.GIT_SAFE_CONFIG
_git_env = secret_scan.git_safe_env

# The six-rung ladder (D10), IMPORTED rather than re-declared.
# Ten modules used to hand-copy these four constants and had already drifted:
# rung 11 carried two different names across the tree and two modules declared
# the same constant twice. `hs_run` owns the ladder as a closed `Exit` IntEnum;
# both historical spellings of rung 11 are bound to the same member there, so
# this import changes no name any caller in this file uses.
import hs_run  # noqa: E402 — the verb declaration lives here
from hs_family.errors import UsageError  # noqa: E402
from hs_run import (EXIT_BROKEN, EXIT_DONE, EXIT_FINDINGS, EXIT_NEEDS_MODEL,  # noqa: E402
                     EXIT_STEP_REMAINING)

# Arbitrary, documented thresholds for the split-plan advisory (not a second
# scanner, not domain logic — just formatting `git diff --cached --numstat`'s
# own numbers into a human-actionable split recommendation).
_SPLIT_FILE_CEILING = 3
_SPLIT_LINE_CEILING = 50

def _git_dir_present(root: Path) -> bool:
    """True iff a `.git` entry (a directory for a normal repo, OR a FILE for
    a worktree/submodule gitlink) exists anywhere from `root` up to the
    filesystem root. This walks the same parent chain `git rev-parse` itself
    walks to locate a repository, but reads the filesystem directly instead
    of trusting git's own verdict — the discriminator DEFECT 3 needs, because
    git's stderr text is NOT reliable here: a corrupt `.git/HEAD` makes
    `git rev-parse --is-inside-work-tree` print the exact same
    `fatal: not a git repository (or any of the parent directories): .git`
    message a directory with no `.git` at all produces (confirmed by hand,
    see the phase report) — so the message can't tell "absent" from "broken"
    apart, only presence-on-disk can."""
    cur = root.resolve()
    while True:
        try:
            if (cur / ".git").exists():
                return True
        except OSError:
            pass  # unreadable entry — treat as "not found here", keep walking up
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent


def _git_repo_status(root: Path):
    """Distinguishes the THREE outcomes `_in_git_repo` used to collapse into
    two. Returns `(status, stderr)`:

    - `"repo"`   — git confirms `root` is inside a work tree; proceed.
    - `"absent"` — no `.git` anywhere up the tree; genuinely never a repo
      (fresh checkout target, plain scratch dir) — the existing benign
      fail-open path, unchanged.
    - `"broken"` — `git rev-parse` failed to answer AND a `.git` entry
      exists somewhere in the chain: git is PRESENT but its plumbing is
      refusing to work (corrupt HEAD, bad repositoryformatversion, an
      unparseable `.git/config`, ...). This is DEFECT 3 — it used to read
      identically to `"absent"` and reported `nothing_staged` EXIT 0 while a
      staged secret sat in the index.

    Discriminator: on-disk presence of `.git`, not `rev-parse`'s exit code
    or stderr text (both are unreliable for telling "absent" from "broken" —
    see `_git_dir_present`). Known weak spot: an orphaned `.git` entry that
    was never actually initialized by git (e.g. a stray empty directory or
    file someone manually named `.git`) reads as `"broken"` here instead of
    `"absent"`, over-blocking a directory that was never really a repo. That
    false-positive is judged the safer failure mode for a secret-scan gate —
    a spurious STOP costs a manual check, a spurious PASS costs a leaked
    credential.
    """
    r = subprocess.run(
        ["git", *_GIT_SAFE, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=30, env=_git_env())
    if r.returncode == 0 and r.stdout.strip() == "true":
        return "repo", ""
    if _git_dir_present(root):
        return "broken", (r.stderr.strip()
                           or "git rev-parse --is-inside-work-tree exited %d" % r.returncode)
    return "absent", ""


def _staged_diff(root: Path):
    """`git diff --cached` — the patch about to become a commit RIGHT NOW.
    Distinct from secret_scan_before_ship's own two diff-gatherers (unpushed
    COMMITS, or the working-tree pack surface): this is a third shape, staged
    but not yet committed.

    Returns `(diff_text, error)`. `error` is `None` on a clean git run
    (`diff_text` may legitimately be "" — nothing staged). `error` is git's
    own stderr when git itself failed on a REAL repo (corrupted object
    store, detached plumbing, etc.) — that case must never be silently
    read as "" by the caller (that was the bug: an empty string meant BOTH
    "nothing staged" and "git broke", so a broken repo skipped the secret
    gate and reported EXIT_DONE). Callers must check `_git_repo_status` first
    — this function only distinguishes success/failure of the diff itself,
    not "no repo at all" (which fails open by design, see
    `_git_repo_status`)."""
    r = subprocess.run(
        ["git", *_GIT_SAFE, "-C", str(root), "diff", "--cached", "--no-color"],
        capture_output=True, text=True, timeout=30, env=_git_env())
    if r.returncode == 0:
        return r.stdout, None
    return "", (r.stderr.strip() or "git diff --cached exited %d" % r.returncode)


def _sequencer_in_progress(root: Path) -> str:
    """`"rebase"`, `"merge"`, `"cherry-pick"`, `"revert"`, or `""` when git is
    not mid-operation.

    The paths are asked of GIT (`rev-parse --git-path`) rather than joined onto
    a guessed `.git/` — a worktree, a submodule and a `--separate-git-dir` clone
    each put them somewhere else, and a check that silently finds nothing in
    those layouts reports a clean tree for the one case it exists to catch.

    Returns the name in the spelling the reader will type: the message built
    from it says `git <name> --continue`, which is a real command for all four.
    """
    def has(rel) -> bool:
        try:
            r = subprocess.run(
                ["git", *_GIT_SAFE, "-C", str(root), "rev-parse", "--git-path", rel],
                capture_output=True, text=True, timeout=15, env=_git_env())
        except (OSError, subprocess.SubprocessError):
            return False
        if r.returncode != 0:
            return False
        p = (r.stdout or "").strip()
        if not p:
            return False
        q = Path(p)
        if not q.is_absolute():
            q = Path(root) / q
        return q.exists()

    # rebase first: a conflicted rebase ALSO leaves MERGE_MSG and an unmerged
    # index, so checking merge first would name the wrong command.
    for rel, name in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                      ("CHERRY_PICK_HEAD", "cherry-pick"),
                      ("REVERT_HEAD", "revert"), ("MERGE_HEAD", "merge")):
        if has(rel):
            return name
    return ""


# What `git commit` actually DOES mid-operation, measured on real repos rather
# than reasoned about — the first version of this message reasoned, and was
# wrong about three of the four:
#
#   merge / cherry-pick / revert   HEAD stays on the branch. `git commit`
#       CONCLUDES the operation: it exits 0, the operation state is gone, and
#       `git <op> --abort` afterwards fails with `fatal: There is no <op> to
#       abort` (rc 128) with the commit intact. So committing here is the
#       CORRECT move, and a verb that says STOP is refusing at the moment the
#       user is right.
#
#   rebase   HEAD is detached, and a commit finishes only the CURRENT step —
#       measured on a 3-commit rebase, `rebase-merge/` is still present after
#       the commit. `git rebase --abort` from there really does discard it.
#
# One sentence covering both would have to be true of neither, so this splits.
_SEQUENCER_CONCLUDED_BY_COMMIT = ("merge", "cherry-pick", "revert")


def _sequencer_next_action(op: str) -> str:
    if op in _SEQUENCER_CONCLUDED_BY_COMMIT:
        return ("a %s is in progress. Committing the staged resolution is what "
                "CONCLUDES it — `git %s --continue` is the same move under "
                "another name, and `git %s --abort` throws the resolution away. "
                "Check the staged diff is the resolution you meant, then commit."
                % (op, op, op))
    return ("STOP — a rebase is in progress and HEAD is detached. A commit here "
            "finishes only the CURRENT step, not the rebase, and `git rebase "
            "--abort` then discards it along with the rest. Run `git rebase "
            "--continue` until the rebase reports it is done.")


def _conflicted_paths(root: Path):
    """Unresolved paths via `git diff --cached --name-only --diff-filter=U`
    — catches an unmerged index left by a merge, rebase, OR cherry-pick
    without guessing at `.git/MERGE_HEAD` vs `REBASE_HEAD` vs
    `CHERRY_PICK_HEAD` (three paths for the same underlying git state; the
    diff-filter reads the index directly and needs none of them). On an
    unmerged path `git diff --cached` itself still exits 0 and prints only a
    "* Unmerged path <file>" marker line — no real diff content — which is
    exactly why DEFECT 2 slipped past the secret scanner and reported
    `ready`.

    Returns `(paths, error)`, same shape as `_staged_diff`: `error` is
    `None` on a clean git run (`paths` may legitimately be `[]` — no
    conflicts). `error` is git's own stderr when the invocation itself
    failed — a residue of the DEFECT-1/3 class this function used to
    collapse a subprocess failure into an empty list, indistinguishable
    from "no conflicts detected". Callers must check `error`, not just
    truthiness of `paths`."""
    # `-z`: plain `--name-only` C-quotes any non-ASCII byte in a path under git's
    # default `core.quotePath=true` -- the caller surfaces this list straight to the
    # user (`next_action`, `warnings`) as the files to go resolve, so it must be the
    # real path, never a quoted stand-in.
    r = subprocess.run(
        ["git", *_GIT_SAFE, "-C", str(root), "diff", "--cached", "--name-only",
         "--diff-filter=U", "-z"],
        capture_output=True, timeout=30, env=_git_env())
    if r.returncode != 0:
        return [], (r.stderr.decode("utf-8", "replace").strip()
                     or "git diff --cached --diff-filter=U exited %d" % r.returncode)
    out = r.stdout.decode("utf-8", "surrogateescape")
    return [p for p in out.split("\0") if p.strip()], None


def _staged_numstat_records(root: Path):
    """(records, error) from ONE `git diff --cached --numstat -z --no-renames`
    call — the single reader `_staged_stats` and `_staged_paths` both parse,
    so the two can never again disagree on how many files a staged change
    touches. Each record is `(added_or_None, deleted_or_None, path)`; `added`/
    `deleted` are `None` for a binary file (numstat's own `-` column).

    `--no-renames` is the same fix `_staged_paths` already carried on its own
    `--name-status` call: left on, git folds a staged `git mv old.py new.py`
    into ONE `R100 old.py => new.py` record, so a reader counting records
    counts a rename as one file where two real paths changed. `-z` NUL-
    delimits records instead of C-quoting a non-ASCII path under git's
    default `core.quotePath=true` (same reasoning as `_staged_paths`'s own
    docstring) — each record is 3 tab-separated fields ending at the NUL,
    never a newline-ambiguous line.

    `error` is `None` on a clean git run (an empty `records` may legitimately
    mean nothing staged); `error` is git's own stderr when the invocation
    itself failed — never silently collapsed to an empty list, the same
    indistinguishable-from-empty bug DEFECT 1 fixed for `_staged_diff`."""
    r = subprocess.run(
        ["git", *_GIT_SAFE, "-C", str(root), "diff", "--cached", "--numstat",
         "-z", "--no-renames"],
        capture_output=True, timeout=30, env=_git_env())
    if r.returncode != 0:
        return [], (r.stderr.decode("utf-8", "replace").strip()
                     or "git diff --cached --numstat exited %d" % r.returncode)
    out = r.stdout.decode("utf-8", "surrogateescape")
    records = []
    for rec in out.split("\0"):
        if not rec:
            continue
        parts = rec.split("\t", 2)
        if len(parts) < 3:
            continue
        added_raw, deleted_raw, path = parts
        added = int(added_raw) if added_raw != "-" else None
        deleted = int(deleted_raw) if deleted_raw != "-" else None
        records.append((added, deleted, path))
    return records, None


def _staged_stats(root: Path):
    """(file_count, total_added_lines, error) from `_staged_numstat_records`.
    A binary file (added `None`) counts toward `file_count`, not the line
    total — same rule the single-command version used to apply per numstat's
    `-` column."""
    records, error = _staged_numstat_records(root)
    if error is not None:
        return 0, 0, error
    files = len(records)
    added = sum(a for a, _d, _p in records if a is not None)
    return files, added, None


def commit_plan(root: Path, *, plan=None, phase=None) -> dict:
    return hs_run_next_command.finalize(_cmd_commit_plan_impl(root, plan=plan, phase=phase))


@_attrs.define(frozen=True, kw_only=True)
class CommitSummary:
    """The one artifact `commit-plan` hands a driver: what it is about to commit.

    Declared rather than typed inline so `files`/`added_lines` are ints by
    construction — a count that arrives as a string renders fine and compares
    wrong."""

    plan = _attrs.field()
    files: int = _attrs.field(converter=int)
    added_lines: int = _attrs.field(converter=int)

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


@_attrs.define(frozen=True, kw_only=True)
class PushSummary:
    """The artifact every push-question state hands a driver.

    All four push states describe the same subject — this branch against its
    upstream — and were four inline dicts carrying between one and three keys.
    Declared once, a reader can ask for `commits` at any of them and get a list
    rather than a KeyError at three of the four."""

    upstream = _attrs.field(default=None)
    commits: list = _attrs.field(factory=list)
    gate_reason = _attrs.field(default=None)

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


@_attrs.define(frozen=True, kw_only=True)
class DeclaredScopeSummary:
    """What `--plan`/`--phase` found among the staged paths — attached only when
    both flags were given (see `_resolve_declared_scope`).

    `outside` is empty everywhere except `staged_outside_declared`, where it
    names exactly what blocked. `merger_owned_staged`/`plan_dir_staged` are the
    two EXEMPT categories — staged only because the plan's own `merger_owned:`
    list claims the path, or because the path lives inside the plan directory
    itself — and they stay visible on `ready` too: an exemption applied with
    nothing printed about it is indistinguishable from a check that never ran."""

    outside: list = _attrs.field(factory=list)
    merger_owned_staged: list = _attrs.field(factory=list)
    plan_dir_staged: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


def _rel_to_root(root: Path, p: Path) -> str:
    """`p` as a POSIX path relative to `root` — the same shape `git diff
    --cached --name-only` reports staged paths in, so the two sides of the
    comparison speak the same vocabulary. Falls back to the resolved absolute
    path when `p` is not under `root` at all (a `--plan` pointed outside the
    repo): that value can never match a staged path, which is the correct,
    safe answer for a directory the diff has no way to name."""
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(p).resolve().as_posix()


def _matches_declared(entries, path: str) -> bool:
    """True when `path` is covered by one of `entries`, each a path glob in
    `scope_match` semantics; an entry ending in `/` owns everything under it
    — `scope_match.normalize_dir_prefix`, the one shared rule every reader of
    a `files_to_*`-style entry agrees on, not a private re-typed copy of it."""
    return scope_match.scope_matches(
        [scope_match.normalize_dir_prefix(e) for e in entries], [path])


def _resolve_declared_scope(root: Path, plan_arg: str, phase_arg: str):
    """`(declared, merger_owned, plan_dir_rel, error)` for `--plan`/`--phase`.

    `error` names a `--plan` that will not parse, or a `--phase` absent from
    the plan's own graph — a usage mistake, not a repo condition, so the
    caller turns it straight into a usage error rather than any commit-plan
    state. `None` on success."""
    plan_dir = hs_run_plan._resolve_plan_arg(root, plan_arg)
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return None, None, None, "--plan %s: %s" % (plan_arg, graph["error"])
    subtasks = graph.get("subtasks", {}) or {}
    if phase_arg not in subtasks:
        return None, None, None, ("--phase %s: not a phase in %s/plan-graph.yaml"
                                   % (phase_arg, plan_dir))
    declared = plan_graph._files(graph, phase_arg,
                                  "files_to_create", "files_to_modify", "files_to_delete")
    merger_owned = {v for v in (graph.get("merger_owned") or ()) if isinstance(v, str)}
    return declared, merger_owned, _rel_to_root(root, plan_dir), None


def _staged_paths(root: Path):
    """(paths, error) — every repo-relative path `git diff --cached` touches,
    no filter. Reads `_staged_numstat_records` (see its own docstring for the
    `--no-renames`/`-z` reasoning: a `git mv undeclared.py declared.py` must
    surface BOTH the old and new path, and a non-ASCII path must come back
    un-quoted) instead of running its own `--name-status` command — the two
    used to disagree on how many files a staged rename touches (`_staged_stats`
    left rename detection ON and counted git's own single collapsed record as
    one file); one shared reader makes that impossible by construction.

    Same failure contract as `_conflicted_paths`: `error` is `None` on a
    clean git run; a subprocess failure is never collapsed into an empty
    list."""
    records, error = _staged_numstat_records(root)
    if error is not None:
        return [], error
    return [path for _a, _d, path in records], None


def _classify_staged(staged, declared, merger_owned, plan_dir_rel):
    """Splits `staged` into (outside, merger_owned_hits, plan_dir_hits) against
    the phase's declared set + the two exempt categories, in that priority
    order — a path matching more than one category is reported once, under
    the first one it satisfies."""
    outside, merger_hits, plan_hits = [], [], []
    for p in staged:
        if _matches_declared(declared, p):
            continue
        if _matches_declared(merger_owned, p):
            merger_hits.append(p)
            continue
        if plan_dir_rel and _matches_declared([plan_dir_rel + "/"], p):
            plan_hits.append(p)
            continue
        outside.append(p)
    return outside, merger_hits, plan_hits


def _cmd_commit_plan_impl(root: Path, *, plan=None, phase=None) -> dict:
    """`hs-run git next` — reports whether the staged diff is safe to
    commit and how to split it. Never runs `git commit`/`git push`, never
    touches the index: this verb PLANS a commit, the caller performs it.

    States: `nothing_staged` (EXIT_DONE — genuinely no repo, or a repo with
    nothing staged), `git_diff_failed` (EXIT_BROKEN — reused for every git
    invocation this verb makes that can fail on a REAL repo: DEFECT 1
    (`_staged_diff`'s `git diff --cached` itself failed, e.g. a corrupted
    object store), DEFECT 3 (`_git_repo_status` reports `"broken"` before a
    diff is ever attempted, e.g. a bad `repositoryformatversion` or a
    corrupt `.git/HEAD`/`.git/config`), and the same residue class in
    `_conflicted_paths` (`git diff --cached --diff-filter=U`) and
    `_staged_stats` (`git diff --cached --numstat`) — all are the same
    "git is present but its plumbing refuses to answer, fail CLOSED"
    situation, reusing the state means no new consumer wiring is needed in
    the state-vocabulary owner), `unresolved_conflict` (EXIT_FINDINGS
    — DEFECT 2: an unmerged path is staged, so `git diff --cached` yields
    only a "* Unmerged path" marker with no real content for the secret
    scanner to see, and `git commit` itself refuses to run in this state),
    `secret_found` (EXIT_FINDINGS — a high-confidence secret pattern hit the
    staged diff, mirrors the pre-ship hook's fail-closed posture, just moved
    earlier to commit time — still checked FIRST when `plan`/`phase` are
    given too: a staged secret stops the run before the declared-scope
    question is even asked), `staged_outside_declared` (EXIT_FINDINGS —
    only reachable with both `plan`/`phase` given: the comparison RAN and
    found a staged path that is neither declared by the phase nor exempt,
    the same finding-shape as `secret_found`/`unresolved_conflict` above),
    `ready` (EXIT_NEEDS_MODEL — clear to commit; a model decides how to act
    on the split plan).

    `plan`/`phase` come from `NEXT`'s own `--plan`/`--phase` (validated
    there — both or neither); resolving them here, once, keeps this the only
    place that reads `merger_owned:`/`files_to_*` for the comparison. Absent
    (the default), the comparison is skipped and `ready` carries a warning
    that staged paths were not checked — never a silent pass.

    `next_command` is `null` on EVERY path — an explicit
    claim, never an absent key. This verb reaches exit 0/1/2/10 only (measured:
    zero exit-11 assignment sites), and none of those tiers names a step a
    driver may run unattended: `ready` is exit 10 precisely because choosing
    HOW to stage and word the commit is the model's judgment, and every
    EXIT_FINDINGS/EXIT_BROKEN path here says stop (an unmerged index or a
    staged secret; a corrupt repo). Emitting a `git commit` string on any of them would invite
    a driver to run exactly the command the state exists to prevent."""
    # Resolved BEFORE any git call — a bad `--plan`/`--phase` is the caller's
    # mistake, not a fact about this repo, and the earlier `staged_secret_...`
    # style precedent above is what this follows: refuse rather than guess.
    # `NEXT` already enforced both-or-neither; a caller through this
    # function directly (tests do) gets the same rule here, once.
    declared = merger_owned = plan_dir_rel = None
    scope_warning = []
    if plan is not None or phase is not None:
        if (plan is None) != (phase is None):
            raise UsageError(
                "hs-run git next: --plan and --phase must both be given, or "
                "neither (got %s alone)" % ("--plan" if plan is not None else "--phase"))
        declared, merger_owned, plan_dir_rel, scope_err = _resolve_declared_scope(
            root, plan, phase)
        if scope_err:
            raise UsageError("hs-run git next: %s" % scope_err)
    else:
        scope_warning = [
            "no --plan/--phase given — staged paths were NOT checked against "
            "the phase's declared file set"]

    repo_status, repo_err = _git_repo_status(root)
    if repo_status == "broken":
        return hs_run.StepResult(
            state="git_diff_failed",
            next_action="STOP — git could not confirm %s is a repo (%s) — "
                            "the repo may be corrupted, fix git itself before "
                            "committing" % (root, repo_err),
            artifacts=[],
            warnings=[repo_err],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    if repo_status == "absent":
        return hs_run.StepResult(
            state="nothing_staged",
            next_action="nothing staged — `git add` first",
            artifacts=[],
            warnings=[],
            exit_code=EXIT_DONE,
        ).as_dict()

    diff, err = _staged_diff(root)
    if err is not None:
        return hs_run.StepResult(
            state="git_diff_failed",
            next_action="STOP — `git diff --cached` failed (%s) — the repo may "
                            "be corrupted, fix git itself before committing" % err,
            artifacts=[],
            warnings=[err],
            exit_code=EXIT_BROKEN,
        ).as_dict()

    conflicted, conflict_err = _conflicted_paths(root)
    if conflict_err is not None:
        return hs_run.StepResult(
            state="git_diff_failed",
            next_action="STOP — `git diff --cached --diff-filter=U` failed (%s) — "
                            "the repo may be corrupted, fix git itself before "
                            "committing" % conflict_err,
            artifacts=[],
            warnings=[conflict_err],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    if conflicted:
        # The diff scan RAN and found a real, actionable problem — a
        # finding, not a broken CLI. `git_diff_failed` above (the scan itself
        # could not run) stays EXIT_BROKEN; this is the scan succeeding and
        # reporting what it found.
        return hs_run.StepResult(
            state="unresolved_conflict",
            next_action="STOP — unresolved merge conflict(s) in %s — resolve and "
                            "re-stage before committing" % ", ".join(conflicted),
            artifacts=[],
            warnings=conflicted,
            exit_code=EXIT_FINDINGS,
        ).as_dict()

    # A resolved conflict is not a finished operation. `--diff-filter=U` reports
    # only UNRESOLVED paths, so the moment the reader runs `git add` the check
    # above goes quiet — while git is still mid-sequencer. Measured on that exact
    # tree: this verb answered `ready` and offered commit advice that named
    # neither the operation nor what committing would do to it.
    #
    # Read through git's own sequencer question rather than by guessing at
    # `.git/rebase-merge` vs `rebase-apply` vs `MERGE_HEAD` vs `CHERRY_PICK_HEAD`
    # — the same reasoning `_conflicted_paths` gives for using a diff-filter
    # instead of those four paths.
    #
    # The RUNG is not the same for all four (see `_sequencer_next_action` for the
    # measurements). Mid-merge/cherry-pick/revert, committing is the correct next
    # move — so this is rung 10, a step the model performs, and rung 2 would be
    # the verb calling the user's own repair "broken". Mid-rebase it really is
    # rung 2: HEAD is detached, the commit finishes one step of several, and the
    # `--abort` the reader reaches for next discards it.
    in_progress = _sequencer_in_progress(root)
    if in_progress:
        return hs_run.StepResult(
            state="sequencer_in_progress",
            next_action=_sequencer_next_action(in_progress),
            artifacts=[],
            warnings=["%s in progress" % in_progress],
            exit_code=(EXIT_NEEDS_MODEL
                       if in_progress in _SEQUENCER_CONCLUDED_BY_COMMIT
                       else EXIT_BROKEN),
        ).as_dict()

    if not diff.strip():
        return hs_run.StepResult(
            state="nothing_staged",
            next_action="nothing staged — `git add` first",
            artifacts=[],
            warnings=[],
            exit_code=EXIT_DONE,
        ).as_dict()

    hits = secret_scan.scan_diff_text(diff)
    if hits:
        # A finding, not BROKEN — the scan ran successfully and found a
        # real secret the caller must act on (unstage + rotate).
        return hs_run.StepResult(
            state="secret_found",
            next_action="STOP — staged diff matches secret pattern(s): %s — "
                            "unstage and rotate before committing" % ", ".join(hits),
            artifacts=[],
            warnings=hits,
            exit_code=EXIT_FINDINGS,
        ).as_dict()

    # Checked AFTER secret_found (a staged secret stops the run before this
    # question is even asked) and BEFORE `ready` (the model needs to know
    # about an out-of-scope file before it is told the diff is clear to
    # commit). Only runs when `declared` was resolved above — absent
    # `--plan`/`--phase`, this whole block is skipped and `ready` carries
    # `scope_warning` instead.
    scope_outside, scope_merger_hits, scope_plan_hits = [], [], []
    if declared is not None:
        staged_paths, staged_paths_err = _staged_paths(root)
        if staged_paths_err is not None:
            return hs_run.StepResult(
                state="git_diff_failed",
                next_action="STOP — `git diff --cached --numstat` failed (%s) — "
                                "the repo may be corrupted, fix git itself before "
                                "committing" % staged_paths_err,
                artifacts=[],
                warnings=[staged_paths_err],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        scope_outside, scope_merger_hits, scope_plan_hits = _classify_staged(
            staged_paths, declared, merger_owned, plan_dir_rel)
        if scope_outside:
            # A finding, not a step owed to the model: the scan RAN and found a
            # real violation, the same rung `secret_found`/`unresolved_conflict`
            # answer above — 10 stays reserved for `ready`, where committing is
            # a judgment call rather than something already found wrong.
            return hs_run.StepResult(
                state="staged_outside_declared",
                next_action="STOP — staged path(s) outside %s's declared file set: "
                                "%s — unstage them or declare them in "
                                "plan-graph.yaml before committing"
                                % (phase, ", ".join(scope_outside)),
                artifacts=[DeclaredScopeSummary(
                    outside=scope_outside, merger_owned_staged=scope_merger_hits,
                    plan_dir_staged=scope_plan_hits).as_dict()],
                warnings=scope_outside,
                exit_code=EXIT_FINDINGS,
            ).as_dict()

    files, added, stats_err = _staged_stats(root)
    if stats_err is not None:
        return hs_run.StepResult(
            state="git_diff_failed",
            next_action="STOP — `git diff --cached --numstat` failed (%s) — the "
                            "repo may be corrupted, fix git itself before "
                            "committing" % stats_err,
            artifacts=[],
            warnings=[stats_err],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    split_plan = ("single" if (files <= _SPLIT_FILE_CEILING and added <= _SPLIT_LINE_CEILING)
                  else "grouped")
    ready_artifacts = [CommitSummary(plan=split_plan, files=files,
                                      added_lines=added).as_dict()]
    if scope_merger_hits or scope_plan_hits:
        ready_artifacts.append(DeclaredScopeSummary(
            merger_owned_staged=scope_merger_hits,
            plan_dir_staged=scope_plan_hits).as_dict())
    return hs_run.StepResult(
        state="ready",
        next_action="commit as a %s change (%d file(s), %d added line(s))"
                        % (split_plan, files, added),
        artifacts=ready_artifacts,
        warnings=scope_warning,
        exit_code=EXIT_NEEDS_MODEL,
    ).as_dict()


def _git_read(root: Path, args: list):
    """(stdout, error) for a read-only git call, hardened the same way every other
    call in this module is. `error` is git's own stderr on a non-zero exit — never
    collapsed into an empty stdout, which is the ambiguity `_staged_diff`'s docstring
    records as a real bug: "" meant both "nothing there" and "git broke"."""
    r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *args],
                       capture_output=True, text=True, timeout=30, env=_git_env())
    if r.returncode != 0:
        return "", (r.stderr.strip() or "git %s exited %d" % (args[0], r.returncode))
    return r.stdout.strip(), None


def push_plan(root: Path) -> dict:
    """What a push from here would do, read before it is attempted.

    `commit-plan` stops at the commit. The step after it — is there an upstream, is
    anything ahead of it, would the pre-push gate refuse — was three git questions the
    push drawer described in prose and an agent had to remember to ask. Each is one
    read, and each names a different next move, so each is a state.

    Read-only, exactly like `commit-plan`: it never runs `git push`. The gate answer
    comes from `artifact_check.check_stage("push", root)`, the same function the
    pre-push hook calls, so this reports the hook's own verdict rather than a second
    opinion that could disagree with it.
    """
    status, detail = _git_repo_status(root)
    if status == "broken":
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="git_diff_failed", exit_code=EXIT_BROKEN,
            next_action="STOP — git could not confirm %s is a repo (%s) — "
                        "the repo may be corrupted, fix git itself before "
                        "pushing" % (root, detail),
            warnings=[detail] if detail else [],
        ).as_dict())
    if status == "absent":
        # Mirrors `commit-plan`'s own fail-open contract for a directory that was
        # never a git repo at all. `_git_repo_status` already tells `absent` (no
        # `.git` anywhere up the tree) apart from `broken` (a `.git` present but
        # git's plumbing refuses to answer) — before this branch existed, both
        # collapsed into the same `status != "repo"` arm below, so a plain scratch
        # directory (never a repo) answered `git_diff_failed`/EXIT_BROKEN through
        # `NEXT`'s fallthrough, while `commit-plan` alone answers the benign
        # `nothing_staged`/EXIT_DONE for the EXACT same tree. Measured directly
        # against a directory with no `.git` at all before this fix landed.
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="nothing_staged", exit_code=EXIT_DONE,
            next_action="nothing staged — `git add` first",
        ).as_dict())

    upstream, err = _git_read(root, ["rev-parse", "--abbrev-ref", "@{u}"])
    if err or not upstream:
        # Not a failure: a branch nobody has pushed yet is the normal first push, and
        # the flag that fixes it is different from the flag a normal push uses.
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="no_upstream", exit_code=EXIT_NEEDS_MODEL,
            next_action="this branch tracks nothing — the first push needs "
                        "`git push -u origin HEAD`, not a plain push",
            artifacts=[PushSummary().as_dict()],
        ).as_dict())

    ahead, err = _git_read(root, ["log", "@{u}..HEAD", "--oneline"])
    if err:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="git_diff_failed", exit_code=EXIT_BROKEN,
            next_action="git could not compare HEAD against %s — %s" % (upstream, err),
        ).as_dict())
    commits = [c for c in ahead.splitlines() if c.strip()]
    if not commits:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="nothing_to_push", exit_code=EXIT_DONE,
            next_action="HEAD is not ahead of %s — nothing to push" % upstream,
            artifacts=[PushSummary(upstream=upstream).as_dict()],
        ).as_dict())

    import artifact_check
    # The stage is MEASURED, not assumed. On a protected branch the pre-push gate
    # runs `merge`, which requires review-decision and plan-approval on top of
    # verification — so a hard-coded "push" reports a clear gate for a tree whose
    # real gate is about to refuse over two receipts it never read.
    import push_gate
    stage = push_gate.stage_for_checkout(root)
    blocked = artifact_check.check_stage(stage, root)
    if blocked:
        # A receipt gap the LOCAL hook only warns about. `push_gate.py` calls this same
        # check and then writes "[pre-push warn] … remote receipts-gate will enforce"
        # before returning None — measured on one tree, the push this state described as
        # refused went through with rc=0. So it is a model decision (rung 10), not a
        # broken repo (rung 2), and the sentence says what will actually happen.
        #
        # One exception, and it is not a receipt gap at all: with no plan resolved
        # there is nowhere for a receipt to live, so nothing the reader writes into
        # `plans/` clears this until a plan exists. `ship` and `fix` both call that
        # condition rung 2 for exactly that reason. Measured on one tree, this verb
        # answered rung 10 while `ship` answered rung 2 on the same repo — a driver's
        # next move then depended on which verb it happened to ask.
        #
        # The split is re-derived from disk — the same `resolve_active_plan` call
        # `ship` makes at its own fork — rather than by matching the reason's
        # wording: a reason string is written for a human and gets reworded, and two
        # domains keyed on its prose would drift apart the day someone improves a
        # sentence.
        #
        # `allow_completed` is READ from the same stage policy `check_stage` reads,
        # never defaulted. A stage that sets `allow_completed_plan: true` resolves a
        # `completed` plan where a bare call resolves None — and this branch would
        # then answer "no plan resolved" in an envelope whose next_action prints the
        # plan's own path. The first draft of this branch did exactly that: it
        # created the divergence it was written to close, and the domain's 31 tests
        # stayed green because no fixture uses a completed plan.
        #
        # Keyed on the MEASURED stage, not the literal "push". Two lines above,
        # `check_stage` is asked about `stage`, which is `merge` on a protected
        # branch — so reading `push`'s policy here asked one question of the gate
        # and a different one of the resolver. The two stages agree on
        # `allow_completed_plan` today (both true, measured), which is why nothing
        # had gone wrong; that is a coincidence of the config, not a guarantee, and
        # the day they diverge this branch would refuse a tree the gate accepted.
        _policy = artifact_check.load_policy()["stages"].get(stage) or {}
        plan_dir = artifact_check.resolve_active_plan(
            str(root),
            allow_completed=bool(_policy.get("allow_completed_plan", False)))
        if plan_dir is None:
            return hs_run_next_command.finalize(hs_run.StepResult(
                state="no_plan_resolved", exit_code=EXIT_BROKEN,
                next_action=blocked,
                artifacts=[PushSummary(upstream=upstream, commits=commits,
                                       gate_reason=blocked).as_dict()],
            ).as_dict())
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="push_gate_blocked", exit_code=EXIT_NEEDS_MODEL,
            next_action="the push would go through locally, but CI's receipts-gate "
                        "will refuse it — %s" % blocked,
            artifacts=[PushSummary(upstream=upstream, commits=commits,
                                   gate_reason=blocked).as_dict()],
        ).as_dict())

    return hs_run_next_command.finalize(hs_run.StepResult(
        state="ready_to_push", exit_code=EXIT_NEEDS_MODEL,
        next_action="%d commit(s) ahead of %s and the push gate passes"
                    % (len(commits), upstream),
        artifacts=[PushSummary(upstream=upstream, commits=commits).as_dict()],
    ).as_dict())


def _next(ns) -> dict:
    """The one door. Which step applies is a disk question, not a choice.

    Anything staged means the commit question is still open; nothing staged means the
    commit already happened (or has not been prepared) and the push question is what
    is left. Answering that by hand puts a decision table back in the skill body,
    which is exactly what routing replaced.

    Order is the projection: the commit comes before the push, so `commit-plan` is
    asked first and only a `nothing_staged` answer falls through.

    `--plan <dir> --phase <id>` (options, not a new verb — this stays the only
    row `git.yaml` registers): both or neither. Given, the commit step compares
    the staged diff against that phase's declared file set (see
    `_resolve_declared_scope`/`_classify_staged` in `_cmd_commit_plan_impl`).
    Neither is the default and behaves exactly as before, plus a warning that
    the comparison did not run. `--plan`/`--phase` are never forwarded to
    `push_plan`: the push question has no declared-scope concept of its own.

    The two internal steps take no argv: this is the one door that parses one,
    so a malformed argv is refused under the one public name.
    """
    root = ns.ctx.root
    if (ns.plan is None) != (ns.phase is None):
        raise UsageError(
            "hs-run git next: --plan and --phase must both be given, or neither "
            "(got %s alone)" % ("--plan" if ns.plan is not None else "--phase"))
    staged = commit_plan(root, plan=ns.plan, phase=ns.phase)
    if staged.get("state") != "nothing_staged":
        return _rerun_hint(staged)
    return _rerun_hint(push_plan(root))


NEXT = hs_run.verb(_next, options=("plan", "phase"), helps={
    "plan": "plan dir whose phase declares the files this commit may stage",
    "phase": "that phase's id; with --plan, the staged diff is compared against the "
             "phase's declared file set (both or neither; omit both to skip the check)"})


def _rerun_hint(out: dict) -> dict:
    """Point the reader back at `next`, never at whichever verb answered.

    An envelope naming the inner verb teaches the reader to pick verbs by hand again.
    Rungs 0 and 2 are left alone — nothing follows a finished or a broken step, and a
    re-run line there reads as "try again", which is not the next move. On rung 11 the
    hint goes in the SENTENCE only: `next_command` stays the bare runnable string.
    """
    if out.get("exit_code") in (EXIT_NEEDS_MODEL, EXIT_STEP_REMAINING):
        out["next_action"] = "%s — then: hs-run git next" % out.get("next_action")
    return out


if __name__ == "__main__":  # pragma: no cover - refusal path, asserted by subprocess
    # Not a command. On this ladder `0` is a CLAIM — "nothing left to do" — so a
    # module that runs here, does nothing and exits 0 tells an automated caller
    # the work is finished. Refuse loudly on the broken rung instead.
    import sys as _sys
    _sys.stderr.write(
        "%s is a hs-run domain module, not a command.\n"
        "Verbs are dispatched through the CLI, which resolves the project root, "
        "loads the registry and owns the exit ladder — none of which happens when "
        "this file is run on its own.\n"
        "Run it through the dispatcher instead:  hs-run %s <verb>\n"
        % (Path(__file__).name, 'git'))
    raise SystemExit(EXIT_BROKEN)
