#!/usr/bin/env python3
"""secret_scan_before_ship.py — the secret scanner, as a LIBRARY.

Scans a diff its caller hands it (`scan_diff_text`; the push gate hands it the
exact range a push delivers) and reports likely secrets in added lines.
test/fixture/docs paths are excluded, so a scan does not fire on the project's
own fixtures — the common false positive.

It is not a hook and nothing dispatches it. A pre-push gate did ride the Bash
lane on this scanner and was deliberately removed (see the note beside
`protected_ref_guard` in harness/data/hook-dispatch.yaml): the audit is now a
skill the user invokes, not a gate that fires behind them. The callers are
`hs:security-scan`, `hs:review`, `hs:review-pr`, `hs:git` and the push gate,
which import it so there is one pattern table rather than five.

The name is kept because five modules and a decision record spell it. What it
no longer means is a posture — there is no HOOK_CLASS here, no fail-closed
wrapper and no break-glass toggle, because none of those govern a function
someone calls.
"""
import os
import re
import subprocess
import sys

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (_HOOKS_DIR, os.path.join(_HOOKS_DIR, "..", "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# Stages where committed content actually leaves the machine.
_SHIP_STAGES = ("push", "pr", "ship", "deploy")

# The secret-shape table lives in secret_patterns (the ONE copy
# the ship scanner, the secrets store and the handoff redactor share); this
# module reads it, never keeps a second.
import secret_patterns as _sp

_PATTERNS = _sp.PATTERNS

# Paths excluded from scanning: tests, fixtures, examples, docs, lockfiles. Excluding
# them is standard secret-scan practice and prevents the gate from blocking on its own
# fake-secret fixtures.
_EXCLUDE_RE = re.compile(
    r"(?:^|/)(?:tests?|spec|specs|fixtures?|examples?|samples?|mocks?|__tests__)(?:/|$)"
    # test-FILE names are excluded only for the .py (pytest) convention — a
    # `test_*.py` at any depth is conventionally a test holding FAKE secrets. A
    # file merely NAMED test_/_test in another language (lib/test_helpers.js, Go
    # src/server_test.go) is a real published module, so scan it.
    r"|(?:^|/)test_[^/]*\.py$|(?:^|/)[^/]*_test\.py$"
    r"|\.(?:md|lock|example|sample|template|dist)$"
    # Plan evidence records what a probe OBSERVED — the same role the `.md`
    # evidence beside it already plays, so it is excluded for the same reason: a
    # transcript of a secret-scanner measurement quotes the fake key it fed in.
    # Anchored on `plans/` because `.jsonl` is not inherently evidence — a runtime
    # log at harness/state/*.jsonl carries real values and is still scanned.
    r"|(?:^|/)plans/.*\.jsonl$",
    re.IGNORECASE)


def is_excluded(path: str) -> bool:
    return bool(_EXCLUDE_RE.search(path or ""))


# The detector's own SOURCE files: the table this module reads IS a list of secret
# shapes, so the commit that ADDS a shape carries an added line matching itself —
# the gate would block its own maintenance (measured: BL-328, the pgp-private-key
# entry had to be smuggled in with a `[ ]` in place of a space before this existed).
# Exact paths, not a pattern — a sibling file carrying the same string still fires.
#
# Kept SEPARATE from `is_excluded`/`_EXCLUDE_RE` on purpose: that predicate also
# decides which files' pytest IDs a `.test_durations` timing table gets stripped
# for (`_strip_excluded_test_id`), and these two are ordinary scanned production
# modules for that purpose — only their OWN diff content gets this carve-out.
_DETECTOR_SOURCE_PATHS = frozenset((
    "harness/scripts/secret_patterns.py",
    "harness/scripts/secret_scan_before_ship.py",
))


def _is_own_source(path: str) -> bool:
    return (path or "") in _DETECTOR_SOURCE_PATHS


# A db connection string whose HOST is local-dev is not a remotely-usable leak: the
# scheme://user:pass@host form only exposes a credential an attacker can actually USE
# when the host resolves OFF the machine. localhost / loopback / a dotless docker-
# compose service name / *.local resolve only inside the dev box or its compose
# network, so postgres://postgres:postgres@localhost or mongodb://admin:admin@mongo is
# skipped (the common docker-compose false positive). A dotted FQDN (db.internal,
# cluster0.x.mongodb.net) or a non-loopback IP still fires — that is a real leak.
_DB_LOCAL_HOST_RE = re.compile(
    r"(?i)^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?"
    r"|[a-z0-9_-]+"                       # single-label service name (no dot -> compose/dev)
    r"|[a-z0-9_-]+\.(?:local|localhost))$")


def _db_host_is_local(host: str) -> bool:
    return bool(_DB_LOCAL_HOST_RE.match((host or "").strip()))


# Documentation placeholders the generic key=value patterns must NOT flag (a secret
# gate that false-blocks `API_KEY=\'your-api-key-here\'` help text gets disabled). The
# high-confidence prefix patterns never match these, so the exemption is scoped to the
# families that match a LABEL plus whatever follows it.
_PLACEHOLDER_RE = re.compile(
    r"(?i)\b(?:your|example|sample|placeholder|changeme|dummy|redacted|todo)"
    r"|<[A-Za-z_]|\.\.\.|here['\"]?\s*$")

# Families keyed off a label (`password:`, `Bearer`, `user:pass@`) rather than a vendor
# prefix. Only these get the placeholder carve-out; extending it to `AKIA…` or `ghp_…`
# would let `# example: ghp_<real key>` walk straight through the gate.
_LABEL_FAMILIES = ("generic-secret", "bearer", "basic-auth-url")


def _placeholder_probe(m) -> str:
    """The span the placeholder carve-out is allowed to read: the credential alone when the
    pattern names one, else the whole match.

    Reading the whole match is wrong for any family that also captures a host, and it fails
    OPEN: `https://user:<real password>@example.com` carries the word `example` in the HOST,
    which tripped the carve-out and disarmed the pattern entirely — a live credential
    reported as documentation. The carve-out exists for a placeholder VALUE, so it gets the
    value and nothing else."""
    return m.groupdict().get("cred") or m.group(0)


def scan_text(text: str) -> list:
    """Return the names of secret patterns that match `text` (deduped, ordered). The
    label-keyed patterns skip an obvious documentation placeholder value; the
    prefix/var-anchored patterns are high-confidence and always count."""
    text = text or ""
    hits = []
    for name, rx in _PATTERNS:
        for m in rx.finditer(text):
            if name.startswith(_LABEL_FAMILIES) and _PLACEHOLDER_RE.search(_placeholder_probe(m)):
                continue  # a real secret elsewhere still fires; a lone placeholder does not
            # A `host` group IS the opt-in to the local-dev carve-out — no parallel name set
            # to keep in sync, so a new host-bearing pattern cannot silently miss it.
            host = m.groupdict().get("host")
            if host and _db_host_is_local(host):
                continue  # local-dev host — not remotely usable; a remote URI still fires
            if name not in hits:
                hits.append(name)
            break
    return hits


# A pytest ID CONTAINS its parameter, so a timing table (`.test_durations`) for a suite
# that tests this scanner carries the scanner's own fake keys verbatim in its KEYS.
# Measured 2026-09-02: a push was blocked on six pattern families, every hit an ID from
# `test_gemini_secret_scrub.py` — a file already excluded; the table citing it was not.
# The table is REGENERATED, so this blocked every push that refreshed it, which is the
# shape that gets a gate switched off.
#
# Scoped to the ID, never to the filename: a line is dropped only when the path before
# `::` is itself excluded. A line that is not an ID, or one naming a file this scanner
# DOES read, still gets scanned. `"path::name": 0.01` is the JSON shape; the leading
# quote and indentation are what the anchor allows for.
_TEST_ID_LINE_RE = re.compile(r'^\s*"([^"]+?\.py)::')


def _strip_excluded_test_id(line: str) -> str:
    """The line with an excluded file's test ID removed — and NOTHING else removed.

    Dropping the whole line was the first shape, and it is a hole: `"tests/test_x.py::n":
    "<a real key>"` would have gone with it. The ID is a NAME, the rest of the line is
    still content, so only the ID span is cut.
    """
    m = _TEST_ID_LINE_RE.match(line)
    if not m or not is_excluded(m.group(1)):
        return line
    end = line.find('"', m.end())
    return line if end < 0 else line[:m.start(1) - 1] + line[end:]


def scannable_added_lines(diff: str) -> str:
    """The added (`+`) content of non-excluded files in a unified diff, header-stripped."""
    out = []
    excluded = False
    prev_minus_header = False
    for line in (diff or "").splitlines():
        # A "+++ b/path" file header is ONLY a header when it directly follows
        # a "--- a/path" line (the unified-diff pair). A bare "+++ ..." with no
        # preceding "--- " is ADDED CONTENT whose own text starts with "++"
        # ("+" added-marker + "++..."); treating it as a header would drop that
        # content and let a secret on such a line evade the scan.
        if line.startswith("--- "):
            prev_minus_header = True
            continue
        if prev_minus_header and line.startswith("+++ "):
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
            excluded = is_excluded(path) or _is_own_source(path)
            prev_minus_header = False
            continue
        prev_minus_header = False
        if excluded:
            continue
        if line.startswith("+"):
            out.append(_strip_excluded_test_id(line[1:]))
    return "\n".join(out)



def scan_diff_text(diff: str):
    """The detection core shared by the in-session gate (gate_reason) and the
    transport backstop (push_gate._secret_reason): scan a unified diff's added
    lines for secrets, then re-scan with added-line breaks removed, to catch a
    secret wrapped across two added lines (e.g. an AKIA id split mid-token).
    Over-detection is fine for a secret gate; the distinctive prefixes keep
    cross-line false-matches rare. Returns the matched pattern names (possibly
    empty)."""
    added = scannable_added_lines(diff)
    hits = scan_text(added)
    if not hits:
        hits = scan_text(added.replace("\n", ""))
    return hits


# git READS the config of whatever directory it is pointed at, and several config
# values name a program git then EXECUTES. This module runs automatically before
# every ship and is pointed at directories it does not control by definition, so a
# git call inside one must never be able to run that directory's code.
#
# Measured against git 2.43.0 on the exact calls this module makes, not assumed:
# of 19 command-valued keys probed, only `core.fsmonitor` fires on `ls-files`.
# core.hooksPath, core.sshCommand, diff.external, core.pager, core.editor,
# core.gitProxy, credential.helper, uploadpack.packObjectsHook, filter.*.clean,
# filter.*.smudge, sequence.editor, core.askPass, ssh.variant, gpg.program,
# diff.*.textconv, trailer.*.command, core.alternateRefsCommand and
# protocol.ext.allow stayed silent. The blacklist is therefore SMALL and exact —
# but a blacklist ages badly, so the primary defence is structural: the emptiness
# probe below reads the FILESYSTEM and makes no git call inside an untrusted
# directory at all. This list guards the calls that remain (inside a directory
# whose identity as this repo's OWN worktree has already been established).
GIT_SAFE_CONFIG = ("-c", "core.fsmonitor=false")

# System-level git config is a machine-wide file this gate has no reason to obey
# while inspecting content; excluding it removes another channel through which a
# command-valued key could reach these calls.
def git_safe_env() -> dict:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def hardened_git_env() -> dict:
    """Env for a git call whose `-C` target is a caller-supplied path, not our
    own code: it must not be steerable by whatever the caller's process
    environment already carries. On top of `git_safe_env()` it drops GIT_DIR /
    GIT_WORK_TREE / GIT_COMMON_DIR / GIT_INDEX_FILE — an inherited value there
    would redirect git's repository resolution away from the `-C` root — and
    pins GIT_CONFIG_GLOBAL to the null device, the same command-execution
    channel as system config. Lives here, beside `git_safe_env`, because both
    the trust store and the path resolver need it and this module imports
    nothing of ours."""
    env = git_safe_env()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _dir_has_git_history(dot_git: str) -> bool:
    """True iff a `.git` DIRECTORY carries any history on disk — refs, packed-refs,
    loose objects or packfiles. Pure filesystem reads, no subprocess: this is what
    keeps a nested repo whose working tree is currently empty but whose HISTORY
    holds a secret on the fail-closed `content` path, without re-opening S1."""
    try:
        if os.path.exists(os.path.join(dot_git, "packed-refs")):
            return True
        heads = os.path.join(dot_git, "refs", "heads")
        for _root, _dirs, files in os.walk(heads):
            if files:
                return True
        objects = os.path.join(dot_git, "objects")
        for entry in os.scandir(objects):
            if entry.name in ("info", "pack"):
                continue
            return True
        pack = os.path.join(objects, "pack")
        if os.path.isdir(pack):
            for entry in os.scandir(pack):
                if entry.name.endswith((".pack", ".idx")):
                    return True
    except OSError:
        return True  # cannot rule history out -> must not be called empty
    return False


def _dir_is_empty_of_content(entry_path: str) -> bool:
    """True iff the directory holds NOTHING but its own `.git`.

    Replaces a `git ls-files --others --exclude-standard` run INSIDE the entry,
    which answered a different question: "is there anything git feels like
    mentioning". Ignore rules from the nested repo AND from the USER'S machine
    (`core.excludesFile`) both silenced it, so a nested repo whose only file was
    `.env.production` reported empty — and passed the gate — for any developer
    with `.env*` in their personal global ignore. `os.scandir` answers the real
    question, cannot be silenced by anyone's config, and needs no git subprocess
    (removing an S1 surface rather than adding one). Raises are the caller's to
    handle: unknown is never empty."""
    for entry in os.scandir(entry_path):
        if entry.name != ".git":
            return False
    return True


def _worktree_gitdir_target(entry_path: str):
    """The path a `.git` FILE's `gitdir:` line points at, resolved against the
    entry directory and realpath'd — or None when the file is unreadable, is not a
    gitfile at all, or names a path that does not exist.

    Resolved against `entry_path`, never CWD: git writes RELATIVE pointers for
    submodules (`gitdir: ../.git/modules/x`) and optionally for worktrees, so a
    CWD-relative resolution would misjudge identity in both directions."""
    try:
        with open(os.path.join(entry_path, ".git"), encoding="utf-8",
                  errors="replace") as fh:
            body = fh.read(4096)
    except OSError:
        return None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("gitdir:"):
            continue
        target = line[len("gitdir:"):].strip()
        if not target:
            return None
        if not os.path.isabs(target):
            target = os.path.join(entry_path, target)
        try:
            return os.path.realpath(target) if os.path.exists(target) else None
        except OSError:
            return None
    return None


def _is_own_worktree(entry_path: str, root: str) -> bool:
    """True iff `entry_path` is a linked worktree OF THE SCANNED REPO — identity,
    not presence.

    `os.path.isfile(entry/.git)` was presence: a FOREIGN repo's worktree, a
    `git clone --separate-git-dir` checkout, a checked-out submodule, a garbage
    `.git` file and a dangling pointer all wear the same shape, and all were
    waved through with a live key inside. Identity is the pointer's TARGET: git
    registers a worktree as a subdirectory of the owning repo's own
    `.git/worktrees/`, so only a target under THIS repo's `.git/worktrees/`
    is this repo's worktree. A submodule's target lands under `.git/modules/`
    and a separate-git-dir clone's lands wherever the operator chose — neither
    passes.

    `root` is passed in explicitly and never inferred from `entry_path`: the
    entry is the untrusted side of this question, so letting it name the repo it
    claims to belong to would be asking the suspect to vouch for itself."""
    target = _worktree_gitdir_target(entry_path)
    if target is None:
        return False
    try:
        # `.git` in the scanned root is itself a FILE when the scanned root is a
        # worktree; `--git-common-dir` is the owning repo's real `.git`, so a
        # worktree created FROM a worktree still resolves to one shared home.
        r = subprocess.run(
            ["git", *GIT_SAFE_CONFIG, "-C", root, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=15, env=git_safe_env())
        if r.returncode != 0 or not r.stdout.strip():
            return False
        common = r.stdout.strip()
        if not os.path.isabs(common):
            common = os.path.join(root, common)
        worktrees = os.path.realpath(os.path.join(common, "worktrees"))
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.commonpath([worktrees, target]) == worktrees if os.path.isdir(
        worktrees) else False


# A verified own worktree is SCANNED, so it needs a bound: a walk that never ends
# would hang the gate. The bound counts files git reports as untracked INSIDE the
# worktree, which on a normal checkout is a handful — this repo's own trunk lists
# 24. 5_000 is ~200x that: high enough that no ordinary worktree can reach it,
# low enough that a pathological tree fails closed in bounded time rather than
# grinding. Hitting it means the walk did NOT see everything, so the entry is
# reported unknown, never as a short clean list.
_WORKTREE_SCAN_MAX_FILES = 5_000


def scan_worktree_content(entry_path):
    """The scannable text of a VERIFIED own worktree's untracked files, or None
    when the content could not be fully determined (caller fails CLOSED).

    Only reachable once `_is_own_worktree` has passed. That distinction is the
    whole argument: this is a checkout of THIS repo's own history, so reading it
    is not the "descend into a foreign repo's tree" option the boundary
    classification rejects for `content`. Committed content on the worktree's
    branch is already covered
    by the unpushed-diff scan (`git log --branches` includes a worktree's branch);
    what is NOT covered, and what this reads, is untracked working-tree files —
    exactly where half-finished work parks a `.env.production`.

    Enumerated via git's own untracked listing inside the worktree, NOT a raw
    filesystem walk. Measured, not assumed: a raw walk of a worktree of this repo
    reads 16,853 files / 146 MB of ALREADY-COMMITTED content and produces 5
    secret-pattern hits from ordinary committed files (research logs, a workflow
    file), i.e. it would false-block every ship — re-introducing the over-blocking
    of `hs:worktree` that this boundary check exists to avoid. The listing answers
    the right question (what is here that history does not already cover) and
    honours the repo's own `.gitignore`.

    Guardrails: the user's machine-wide ignore list is neutralised (the same hole
    fixed on the outer listing), symlinks are NOT followed (neither file nor
    directory — a symlink is the way content walks out of the worktree, the exact
    class `content` refuses to risk), the per-file read cap is the module's
    existing one, and the file count is bounded by `_WORKTREE_SCAN_MAX_FILES`."""
    entry_path = str(entry_path)
    try:
        listed = subprocess.run(
            ["git", *GIT_SAFE_CONFIG, "-c", "core.excludesFile=" + os.devnull,
             "-C", entry_path, "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, text=True, timeout=30, env=git_safe_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None

    parts = []
    seen = 0
    for rel in listed.stdout.split("\x00"):
        if not rel:
            continue
        if rel.endswith("/"):
            return None  # a nested boundary INSIDE the worktree — unknown, fail closed
        seen += 1
        if seen > _WORKTREE_SCAN_MAX_FILES:
            return None  # bound hit: the walk did not see everything
        if is_excluded(rel) or _is_own_source(rel):
            continue
        path = os.path.join(entry_path, rel)
        # islink() on the entry itself, plus a realpath containment check, so
        # neither a file symlink nor a path THROUGH a directory symlink can read
        # content living outside the worktree.
        try:
            if os.path.islink(path):
                continue
            real = os.path.realpath(path)
            base = os.path.realpath(entry_path)
            if os.path.commonpath([base, real]) != base:
                continue
        except (OSError, ValueError):
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                chunk = fh.read(1_000_000)
        except (FileNotFoundError, IsADirectoryError):
            continue  # ordinary: no scannable text behind it — not an unknown
        except OSError:
            return None  # genuinely unreadable — unknown, not clean
        if "\x00" in chunk[:4096]:
            continue  # binary — no text secret to scan
        parts.append(chunk)
    return "\n".join(parts)


def classify_nested_boundary(entry_path, root) -> str:
    """Classify an untracked entry `git ls-files --others` collapsed to a single
    trailing-slash boundary (a nested git repo git refuses to descend into, so
    it is listed as one directory entry rather than file-by-file) into one
    of three genuinely different shapes, so the caller can treat each one
    according to what it actually is instead of treating every collapsed boundary
    identically:

      - "worktree": a linked worktree OF THE SCANNED REPO, established by
        resolving the `.git` file's `gitdir:` pointer and requiring it to land
        under this repo's own `.git/worktrees/` (see `_is_own_worktree`). The
        caller SCANS this one's untracked content rather than skipping it — it
        is a checkout of this project's own history, and `harness/plugins/hs/
        skills/worktree/scripts/worktree.cjs:372` places worktrees INSIDE the
        repo in monorepo mode, so blocking outright would wedge an ordinary,
        already-supported workflow.
      - "empty": a genuine nested repo (its own `.git` DIRECTORY) holding no
        files on disk outside `.git` AND carrying no history there either —
        there is no content to scan, so "content never scanned" would be a
        false claim. Both halves are read from the FILESYSTEM: an emptiness
        probe that asked git was silenced by the nested repo's own ignore
        rules and by the user's machine-wide `core.excludesFile`, so a nested
        repo whose only file was `.env.production` reported empty and passed.
      - "content": everything else — a real nested repo holding real files (the
        `git clone`-a-dependency-into-the-tree trigger this boundary check
        exists for), a foreign repo's worktree, a submodule checkout, a
        separate-git-dir clone, an unparseable or dangling `.git` file, OR the
        classification itself could not be completed — fails CLOSED, the same
        posture the rest of this module takes on a genuinely unknown entry.

    `root` (the scanned repo) is an explicit parameter, never inferred from
    `entry_path`: identity must be judged against the repo doing the scanning,
    not against whatever the untrusted entry points at."""
    entry_path = str(entry_path)
    if os.path.isfile(os.path.join(entry_path, ".git")):
        return "worktree" if _is_own_worktree(entry_path, str(root)) else "content"
    try:
        if _dir_is_empty_of_content(entry_path) and not _dir_has_git_history(
                os.path.join(entry_path, ".git")):
            return "empty"
    except OSError:
        return "content"  # unreadable — unknown, never "nothing to scan"
    return "content"


# The blocking half — `gate_reason` / `core` / `main`, the PreToolUse gate that refused a
# push whose unpushed diff carried a likely secret — was REMOVED. It shipped disabled from
# the day it was written and never blocked a real push; what survives here is the part the
# live callers actually use: `scan_text`, `scannable_added_lines`, `scan_diff_text`,
# `classify_nested_boundary`, `scan_worktree_content`, `is_excluded` and the git-safe env
# (`hs_run_security.py`, `hs_run_review_pr.py`, `hs_run_git.py`, `push_gate.py`), and
# `hs:security-scan` is where a real audit belongs — a skill the user invokes, not a gate
# that fires behind them.
#
# `gather_pack_surface` — this module's own tracked-diff + untracked-file combiner — went
# with it: it had no caller left (`hs_run_security._secret_grep_worktree` is the one
# working-tree combiner, built on the shared halves above), and where the two disagreed it
# was the weaker answer — measured: on a zero-commit repo holding an untracked secret it
# bailed to None before its untracked half ever ran (its `git diff HEAD` fails with no
# HEAD and the two reads were coupled), while the live combiner reports the leak; on an
# unreadable file it abandoned the whole scan at the first offender, while the live one
# keeps scanning and names every unreadable path.
#
# `HOOK_CLASS` went with it. The constant is what marks a file in this directory as a gate,
# and a module declaring itself a compliance gate while registering nowhere is exactly what
# `test_bug_class_invariants` refuses — correctly: from the outside that reads as a gate
# someone forgot to wire, not as a library that used to be one.
