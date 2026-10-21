#!/usr/bin/env python3
"""harness_paths.py — single home for root + state-dir resolution.

Two roots, one shared binary serving many projects:

  bin_root()   — where the programs live (hooks, scripts, skills, rules, data
                 templates). Read-only at runtime for every actor. A global
                 install points every project at ONE bin.
  data_root()  — a project's private data home `.harness/`. All per-project
                 state, telemetry, trace, writeable config land here.

`root()` is kept as a back-compat alias for `bin_root()` so the shipped-binary
readers are untouched by the split. Resolution is PURE (no mkdir): writers own
their own mkdir, readers never create what they inspect.

Precedence (exact):
  bin_root():   HARNESS_BIN_ROOT > HARNESS_ROOT (legacy alias) > __file__-relative
                > upward walk(harness/manifest.json | harness/hooks/) > CWD
  data_root():  HARNESS_DATA_ROOT > CLAUDE_PROJECT_DIR/.harness
                > (self-host, ONLY when HARNESS_BIN_ROOT UNSET) bin_root()/.harness
                > FAIL-CLOSED marker (caller-visible) — never a silent CWD
  state_dir():  HARNESS_STATE_DIR > data_root()/state
  tool_data_root(): data_root() strict tiers first; only under a global layout
                (HARNESS_BIN_ROOT set) with a still-unresolved result does a
                Bash-tool script walk up from CWD to its own project's
                `.harness/` — still unresolved -> the same FAIL-CLOSED marker.
  tool_state_dir(): HARNESS_STATE_DIR > tool_data_root()/state
  bin_state_dir(): bin_root()/harness/state   # install metadata, never project-side

FAIL-CLOSED (red-team F2/F5/F7): under a global layout (HARNESS_BIN_ROOT set) a
data_root() that cannot resolve a project returns the unresolved marker instead
of decaying to CWD — guards MUST treat it as "no project root" and BLOCK. Self-host
is detected by HARNESS_BIN_ROOT being UNSET (a global bin is itself a git checkout,
so a `.git` walk-up would wrongly collapse it). A data root whose parent is the
filesystem root (e.g. HARNESS_DATA_ROOT=/data) is not a real project (F7).

data_root()/state_dir() are STRICT-ONLY — no CWD walk, ever; a guard/hook always
calls these bare. The CWD walk-up lives ONLY behind the named tool_data_root()/
tool_state_dir() functions, for a tool invoked via the Bash tool (whose env
carries no CLAUDE_PROJECT_DIR): a NAMED function keeps this trust boundary
grep-enforceable (an absence-guard on harness/hooks/ + fs_guard.py bans the
`tool_data_root`/`tool_state_dir` tokens outright) — a boolean opt-in on the
strict functions would not be lintable and nothing would stop a future guard
from passing True and widening its own fail-closed lane.
"""

import os
from pathlib import Path
from typing import Optional

# Fail-closed sentinel: a global-layout data_root() that cannot resolve a
# project returns this. Absolute + non-existent so an accidental write under it
# fails loudly rather than silently landing in CWD/home. Callers/guards detect
# it via data_root_unresolved() and BLOCK.
_UNRESOLVED = Path("/__harness_data_root_unresolved__")

# The per-project data home dir name under a project root.
DATA_HOME_NAME = ".harness"

# --- linked-worktree host redirect (F2) ------------------------------------- #
# A git worktree carries only tracked files, so it would otherwise open its own
# state store and fragment/lose runtime data when removed. worktree_host_root()
# detects a linked worktree by its `.git` FILE (`gitdir: <host>/.git/worktrees/
# <name>`) + the `commondir` beside it, and returns the HOST repo root so both
# state flavours point back there. Reads a FILE (never spawns git — hot path),
# memoized per start dir, and fail-open ABSOLUTE: any surprise layout returns
# None and the caller keeps its legacy behaviour.
_WORKTREE_CACHE: dict = {}


def _reset_worktree_cache() -> None:
    """Test hook: drop `worktree_host_root()`'s memoized results."""
    _WORKTREE_CACHE.clear()


def _nearest_git_marker(start: Path):
    """Walk up from `start` to the nearest dir holding a `.git` entry. Returns
    (dir, ".git" Path, is_file) — the tree root itself, plus whether its
    marker is a FILE (linked worktree) or a DIR (main checkout) — or None if
    no `.git` is found at all. Pure filesystem check, no git subprocess."""
    try:
        cur = Path(start).resolve()
    except (OSError, RuntimeError):
        return None
    for cand in (cur, *cur.parents):
        g = cand / ".git"
        try:
            if g.is_file():
                return (cand, g, True)
            if g.is_dir():
                return (cand, g, False)
        except OSError:
            continue
    return None


def current_working_tree_root(start=None) -> Optional[Path]:
    """The root of the git working tree `start` (default CWD) is ACTUALLY
    inside right now — a linked worktree's own root, NEVER redirected to its
    host. protected_trees.protected_working_trees() is keyed by the REPO
    (_resolve_repo_anchor()), not by cwd — but its self-host tier still calls
    THIS function (as its last fallback, after CLAUDE_PROJECT_DIR and
    HARNESS_ROOT) for exactly that "which repo is this process physically
    standing in" question. Do NOT reach for
    project_root() here: for a "which PROJECT's plans/docs/state do
    I read" decision it does the OPPOSITE of worktree_host_root() — it
    redirects the HOST answer OUT to a linked worktree the caller physically
    stands in (worktree_root_if_host_is below), never consolidates the
    worktree back to the host. Only worktree_host_root() consolidates to the
    host (module docstring "F2"), and only for STATE (`.harness/`) — a
    different, intentional concern from plans/docs.

    Precedence: CLAUDE_PROJECT_DIR (always set in a real hook invocation,
    already worktree-local — never redirected) > nearest `.git` walk-up from
    `start`. Returns None only when neither resolves (fail-open: the caller
    must then judge nothing rather than block/allow on a guess)."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        cand = Path(proj)
        if cand.is_dir():
            resolved = cand.resolve()
            if resolved != Path(resolved.anchor):
                return resolved
    marker = _nearest_git_marker(Path(start) if start is not None else Path.cwd())
    if marker is None:
        return None
    return marker[0]


def _probe_worktree_host_root(start: Path) -> Optional[Path]:
    """Uncached detection. Walk up from `start` to the nearest dir holding a
    `.git` entry: a `.git` DIR is the main worktree (no redirect -> None); a
    `.git` FILE is a linked worktree -> resolve its `commondir` to `<host>/.git`
    and return `<host>`. Any read/parse failure -> None (fail-open)."""
    marker = _nearest_git_marker(start)
    if marker is None:
        return None
    _cand, git_file, is_file = marker
    if not is_file:
        return None  # main worktree — never redirect
    try:
        content = git_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    gitdir_raw = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            gitdir_raw = line[len("gitdir:"):].strip()
            break
    if not gitdir_raw:
        return None
    gitdir = Path(gitdir_raw)
    try:
        if not gitdir.is_absolute():
            gitdir = (git_file.parent / gitdir).resolve()
        else:
            gitdir = gitdir.resolve()
        common_raw = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = Path(common_raw)
        if not common.is_absolute():
            common = (gitdir / common).resolve()
        else:
            common = common.resolve()
    except (OSError, RuntimeError):
        return None
    # commondir resolves to the shared `<host>/.git`; the host root is its parent.
    if common.name != ".git" or not (common.parent / ".git").is_dir():
        return None
    return common.parent


def worktree_host_root(start: Path) -> Optional[Path]:
    """Host repo root when `start` sits inside a linked git worktree, else None.
    Memoized per raw start dir; fail-open. The caller supplies its own anchor —
    CLAUDE_PROJECT_DIR-or-bin_root() for both flavours since the one-tree
    ruling: hook_runtime._state_dir() now delegates to the SAME
    data_root()/state_dir() tiers the script flavour uses, rather than probing
    its own _hooks_dir()-anchored redirect. _hooks_dir() survives only as the
    fail-open fallback when that delegation itself raises."""
    key = str(start)
    if key in _WORKTREE_CACHE:
        return _WORKTREE_CACHE[key]
    result = _probe_worktree_host_root(start)
    _WORKTREE_CACHE[key] = result
    return result


def root_has_markers(path: Path) -> bool:
    """A directory is a plausible project root when it already carries a
    `plans/` dir (an already-scaffolded project) or a `.git` entry (a fresh,
    not-yet-scaffolded checkout — a DIR for a normal clone, a FILE for a
    linked worktree; `exists()` covers both)."""
    return (path / "plans").is_dir() or (path / ".git").exists()


def walk_up_for_project_root(start: Path) -> Optional[Path]:
    """The nearest ancestor of `start` (start included) carrying a root marker,
    or None if none of start/its parents qualify."""
    try:
        cur = start.resolve()
    except OSError:
        return None
    for candidate in (cur, *cur.parents):
        if root_has_markers(candidate):
            return candidate
    return None


def linked_worktree_root(cwd: Path) -> Optional[Path]:
    """The nearest ancestor of `cwd` that is itself a LINKED git worktree's own
    root — a `.git` FILE (`gitdir: ...`), never a plain checkout's `.git` DIR.
    None for a plain checkout or when no root marker is found at all.

    A linked worktree carries its OWN `plans/` (a git-tracked tree), checked
    out where the caller is physically standing — a different directory from
    the shared host `.harness/state` lives under (the F2 redirect above).
    Requiring the FILE form keeps this from over-firing on a shell sitting at a
    plain checkout."""
    found = walk_up_for_project_root(cwd)
    if found is None:
        return None
    return found if (found / ".git").is_file() else None


def worktree_root_if_host_is(cwd: Path, expected_host: Path) -> Optional[Path]:
    """`cwd`'s own linked-worktree root, but ONLY when that worktree's real
    HOST (`worktree_host_root`, never assumed) equals `expected_host` — the
    project a resolver already chose by other means. None otherwise: cwd is not
    a worktree, its host cannot be resolved (fail-open), or cwd is a worktree
    of an UNRELATED project.

    The one answer both resolvers use — `project_root()` below for hooks and
    `harness_root.resolve_project_root()` for the hs-* CLIs — so a hook and
    hs-run name the same `plans/` from inside a linked worktree."""
    wt_root = linked_worktree_root(cwd)
    if wt_root is None:
        return None
    try:
        host = worktree_host_root(cwd)
    except OSError:
        return None
    if host is None or host != expected_host:
        return None
    return wt_root


def _bin_root_from_file() -> Optional[Path]:
    """The bin root inferred from this file's own location: harness_paths.py
    lives at <bin>/harness/scripts/harness_paths.py. Returns the candidate only
    when it actually carries the harness markers (so a relocated copy falls
    through to the CWD walk-up)."""
    here = Path(__file__).resolve()
    cand = here.parent.parent.parent  # scripts -> harness -> <bin>
    if (cand / "harness" / "manifest.json").is_file() or (cand / "harness" / "hooks").is_dir():
        return cand
    return None


def bin_root() -> Path:
    raw = os.environ.get("HARNESS_BIN_ROOT") or os.environ.get("HARNESS_ROOT")
    if raw:
        return Path(raw).resolve()
    from_file = _bin_root_from_file()
    if from_file is not None:
        return from_file
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "harness" / "manifest.json").is_file():
            return candidate
        if (candidate / "harness" / "hooks").is_dir():
            return candidate
    return cur


def root() -> Path:
    """Back-compat alias for bin_root() — the shipped-binary readers keep using
    root() and transparently see the bin root."""
    return bin_root()


def _valid_project_dir(data_dir: Path) -> bool:
    """A data root is a real project only when its enclosing project dir is not
    the filesystem root (F7): HARNESS_DATA_ROOT=/data → project "/" is rejected."""
    parent = data_dir.parent
    return parent != Path(parent.anchor)


# Entries that make a directory a project or bin root. A data home carries none:
# it holds state, never the tree it serves.
_ROOT_MARKERS = (".git", "plans", DATA_HOME_NAME, "harness/manifest.json")


def data_root_misplaced(data_dir) -> Optional[str]:
    """Why `data_dir` cannot be a data home, or None when it can.

    Refused: a directory carrying a project/bin root marker (the value names the
    project, not its `.harness/`), or a path nested inside another data home.
    A path that does not exist yet passes — the skeleton is seeded on first run."""
    p = Path(data_dir).absolute()
    for anc in p.parents:
        if anc.name == DATA_HOME_NAME:
            return ("%s sits inside the data home %s — set HARNESS_DATA_ROOT=%s"
                    % (p, anc, anc))
    for m in _ROOT_MARKERS:
        if (p / m).exists():
            return ("%s carries %s, so it is a project or bin root, not a data home "
                    "— set HARNESS_DATA_ROOT=%s" % (p, m, p / DATA_HOME_NAME))
    return None


def data_root_env_problem() -> Optional[str]:
    """Why HARNESS_DATA_ROOT cannot name a data home, or None (unset or fine).
    data_root() fails closed on any reason given here; a CLI prints it."""
    raw = os.environ.get("HARNESS_DATA_ROOT")
    if not raw:
        return None
    cand = Path(raw).resolve()
    if not _valid_project_dir(cand):
        return ("HARNESS_DATA_ROOT=%s puts the project at the filesystem root — "
                "set it to <project>/%s" % (raw, DATA_HOME_NAME))
    return data_root_misplaced(cand)


def _project_from_cwd() -> Optional[Path]:
    """Tool-context project resolver: walk up from CWD to the nearest dir that
    already carries a `.harness/` data home, return `<dir>/.harness`. Keyed on an
    existing `.harness/` (NOT a `.git` walk-up, per F2) so a global bin — itself a
    git checkout with NO per-project `.harness/` — is never mistaken for a project.
    Returns None when no initialized project encloses CWD; the caller then fails
    closed. This is for a TOOL invoked via the Bash tool (whose env carries no
    CLAUDE_PROJECT_DIR) — never for a guard, which runs in a hook env that always
    has CLAUDE_PROJECT_DIR and stays fail-closed."""
    try:
        cur = Path.cwd().resolve()
    except OSError:
        return None
    for cand in (cur, *cur.parents):
        dh = cand / DATA_HOME_NAME
        if dh.is_dir() and _valid_project_dir(dh):
            return dh
    return None


def data_root() -> Path:
    """The per-project data home `.harness/`. STRICT ONLY — never walks CWD; see
    module docstring for the exact precedence and the fail-closed contract. A
    guard/hook always calls this bare. A tool that needs the CWD walk-up uses
    the named tool_data_root() instead (never a boolean opt-in here — see
    module docstring for why)."""
    raw = os.environ.get("HARNESS_DATA_ROOT")
    if raw:
        return _UNRESOLVED if data_root_env_problem() else Path(raw).resolve()
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        anchor = Path(proj).resolve()
        host = worktree_host_root(anchor)  # linked worktree -> host (F2)
        cand = (host if host is not None else anchor) / DATA_HOME_NAME
        return cand if _valid_project_dir(cand) else _UNRESOLVED
    # Self-host (dogfood): ONLY when HARNESS_BIN_ROOT is UNSET. A global bin is
    # itself a git checkout, so this must NOT key off a `.git` walk-up (F2).
    if not os.environ.get("HARNESS_BIN_ROOT"):
        anchor = bin_root()
        host = worktree_host_root(anchor)  # self-host in a worktree -> host (R4/T8)
        return (host if host is not None else anchor) / DATA_HOME_NAME
    # Global layout with no resolvable project → fail-closed, never CWD.
    return _UNRESOLVED


def data_root_for(root) -> Path:
    """The data home of a CALLER-SUPPLIED project root (a tool's `--repo`), not of this
    process's own project. HARNESS_DATA_ROOT answers only when it IS that root's data
    home (its project dir is the root or the root's worktree host); otherwise the root's
    own `.harness/` (its worktree host's, F2). Reading the session's home for another
    repo is the defect this exists for: the answer was right for the wrong project."""
    anchor = Path(root).resolve()
    host = worktree_host_root(anchor)
    project = host if host is not None else anchor
    raw = os.environ.get("HARNESS_DATA_ROOT")
    if raw and Path(raw).resolve().parent in (anchor, project):
        return _UNRESOLVED if data_root_env_problem() else Path(raw).resolve()
    return project / DATA_HOME_NAME


def tool_data_root() -> Path:
    """TOOL-context resolver: the strict tiers first (DRY — calls data_root(),
    never re-inlines them); only when that comes back unresolved AND under a
    global layout (HARNESS_BIN_ROOT set) does a Bash-tool script walk up from
    CWD to its own project's `.harness/` instead of failing closed. It runs
    with cwd == the project and only ever resolves that project's own state,
    so it cannot widen a guard lane. A guard/hook must NEVER call this — it
    always calls data_root() bare and stays fail-closed (F2); this is exactly
    the boundary the absence-guard on harness/hooks/ + fs_guard.py enforces."""
    dr = data_root()
    if not data_root_unresolved(dr):
        return dr
    if os.environ.get("HARNESS_BIN_ROOT"):
        found = _project_from_cwd()
        if found is not None:
            return found
    return dr  # still unresolved — the fail-closed marker, never CWD


def data_root_unresolved(p: Path) -> bool:
    """True when `p` is the fail-closed marker — guards must BLOCK on this."""
    return Path(p) == _UNRESOLVED


def _cwd_worktree_plans_redirect(expected_host: Path) -> Optional[Path]:
    """`worktree_root_if_host_is` for the process cwd, fail-open: a hook must
    never crash because a filesystem probe raised."""
    try:
        return worktree_root_if_host_is(Path.cwd(), expected_host)
    except OSError:
        return None


def project_root() -> Path:
    """The per-project root for gate/scan operations that must target the
    project being worked on — NOT the shared binary. Precedence:
    CLAUDE_PROJECT_DIR (the working project) > the data-home-derived project dir
    (data_root().parent) > bin root (self-host, where bin == project). Always
    concrete — a scan/gate always has a real tree to point at.

    Every high-value caller runs in a HOOK context, where CLAUDE_PROJECT_DIR is
    always set and tier 1 wins outright. Tier 2 (data_root().parent) is reached
    only from a SCRIPT context (no CLAUDE_PROJECT_DIR, e.g. a Bash-tool
    subprocess).

    Both tiers apply ONE further check before returning: is cwd itself sitting
    inside a linked git worktree whose real HOST (verified via
    `_cwd_worktree_plans_redirect`, never assumed) is the root this tier just
    named? If so, the WORKTREE wins instead — `plans/` and every other
    git-tracked read this function feeds (gate_stage, inject_prompt_context,
    stamp_artifact, ...) come from the tree the caller is physically standing
    in, never redirected to wherever `.harness/state` legitimately stays
    shared with the host (`data_root()`/`state_dir()`'s own F2 redirect,
    UNCHANGED by this — state and plans/ are allowed to name two different
    trees on purpose). A worktree of some OTHER, unrelated project leaves the
    tier's own answer untouched.

    Use root()/bin_root() for BIN-global config; use this only for project-scoped
    file operations (git diff of the project, plans/ receipts, source scans).
    MEASURED, self-host with cwd inside a linked worktree: these do NOT
    coincide — project_root() redirects to the worktree (this function's own
    4th step, just above), while bin_root() has no worktree awareness at all
    and keeps naming the host. A caller that reads root()/bin_root() where it
    meant project_root() silently reads the wrong tree the moment cwd stands
    in a worktree, self-host included — this was the root cause of a write
    into a worktree's own plan-approval.json passing a guard anchored on
    bin_root() (see protected_trees.protected_working_trees() for the guard-specific
    fix)."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        cand = Path(proj)
        if cand.is_dir():
            resolved = cand.resolve()
            # F7 fail-closed: never hand back the filesystem root as a project to
            # WRITE into (inject would stamp "/plans/reports/..."). data_root()
            # rejects this same case; mirror it here and fall through to the next
            # tier instead of returning "/".
            if resolved != Path(resolved.anchor):
                wt_root = _cwd_worktree_plans_redirect(resolved)
                return wt_root if wt_root is not None else resolved
    dr = data_root()
    if not data_root_unresolved(dr):
        parent = dr.parent
        wt_root = _cwd_worktree_plans_redirect(parent)
        return wt_root if wt_root is not None else parent
    return bin_root()


def state_dir() -> Path:
    """STRICT ONLY — see data_root(). A tool needing the CWD walk-up uses
    tool_state_dir() instead."""
    raw = os.environ.get("HARNESS_STATE_DIR")
    if raw:
        return Path(raw)
    return data_root() / "state"


def writable_state_dir():
    """`state_dir()` for WRITERS, or None when no project resolves.

    Readers may take the unresolved path and simply find nothing. A writer cannot:
    `state_dir()` yields `<_UNRESOLVED>/state` on an unresolved root, so a
    `mkdir(parents=True)` materialises the fail-closed marker at the FILESYSTEM ROOT
    — the very path `bootstrap.ensure_skeleton` refuses with ValueError. That
    directory then outlives the process and leaks into unrelated runs (it broke
    `test_bootstrap`'s marker check after an `-n auto` suite run).

    Returns None rather than raising because every current caller is telemetry- or
    nudge-class: dropping one record is the cheap failure, writing to `/` is not.
    """
    sd = state_dir()
    return None if data_root_unresolved(sd.parent) else sd


def tool_state_dir() -> Path:
    """TOOL-context state dir over tool_data_root() — see its docstring. A
    guard/hook must NEVER call this; it always calls state_dir() bare."""
    raw = os.environ.get("HARNESS_STATE_DIR")
    if raw:
        return Path(raw)
    return tool_data_root() / "state"


def state_dir_for(root) -> Path:
    """State home for a CALLER-SUPPLIED project root — the data home of THAT
    root, not of this process's own env-project. Tiers: HARNESS_STATE_DIR >
    HARNESS_DATA_ROOT/state > `<root>/.harness/state`.

    Exists because callers that take an explicit root (decision_reconcile's
    --root, the plugin CLIs) used to mirror these tiers by hand, and every
    mirror drifted on its own: the no-env fallback hardcoded the LEGACY
    `<root>/harness/state` tree, so one register kept two markers and the two
    state trees disagreed. Per the one-tree ruling, project state lives
    in the root's data home, and this is the one door that answers for a root
    the env does not name."""
    raw = os.environ.get("HARNESS_STATE_DIR")
    if raw:
        return Path(raw)
    data = os.environ.get("HARNESS_DATA_ROOT")
    if data:
        return Path(data) / "state"
    return Path(root) / DATA_HOME_NAME / "state"


def hook_state_dir() -> Path:
    """State home for HOOK-class runtime state (trace/telemetry/diag/sessions/
    skip-marks): HARNESS_STATE_DIR > the shared state_dir() tiers > the bin
    tree as fail-open fallback.

    hook_runtime and hlog each hand-mirrored these tiers and drifted apart —
    the hook mirror never consulted HARNESS_DATA_ROOT when self-hosted while
    hlog's did, so hooks and loggers wrote DIFFERENT trees on the same day.
    Per the one-tree ruling, a bare self-host hook lands in
    `<host>/.harness/state` like every other project state (host-anchored via
    state_dir()'s F2 redirect, never keyed off CLAUDE_PROJECT_DIR — R4). On a
    global install with no resolvable project, state_dir() comes back
    unresolved and this fails OPEN onto bin_state_dir() — the old hand-rolled
    fallback's target — rather than dropping records on the filesystem root."""
    raw = os.environ.get("HARNESS_STATE_DIR")
    if raw:
        return Path(raw)
    sd = state_dir()
    if not data_root_unresolved(sd.parent):
        return sd
    return bin_state_dir()


def bin_state_dir() -> Path:
    """Bin-side state home for install metadata (install-omitted-skills.json,
    install-state.json) — describes the INSTALL, not a project, so it never
    rides data_root()."""
    return bin_root() / "harness" / "state"


def engine_home() -> Path:
    """The courier's stable engine home: where `harness setup` copies the engine
    out of the plugin cache. `<XDG_DATA_HOME|~/.local/share>/harness`. PURE — no
    mkdir (writers own their own mkdir). This is orthogonal to bin_root()/
    data_root(): it names the shared install location a global-mode bin_root()
    then points at, and does NOT touch self-host detection (a self-host repo
    leaves HARNESS_BIN_ROOT UNSET and never consults this)."""
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    # A relative XDG_DATA_HOME must NOT anchor the SHARED engine home to CWD (which
    # `.resolve()` on a relative path would do — giving every project its own engine
    # under <cwd>/…). The XDG spec says a relative value is invalid and should be
    # ignored, so fall back to the ~/.local/share default.
    if base != "~/.local/share" and not os.path.isabs(os.path.expanduser(base)):
        base = "~/.local/share"
    p = Path(base).expanduser() / "harness"
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        # a symlink loop / ELOOP on XDG_DATA_HOME must not crash the diagnostic verbs
        # (doctor/version) that call engine_home() before any other guard; fall back to
        # the un-resolved absolute path so downstream .is_file() checks simply report the
        # engine home as absent instead of raising RuntimeError('Symlink loop').
        return p.absolute()


def engine_current() -> Path:
    """The `current` pointer under engine_home() — the default engine a repo's
    HARNESS_BIN_ROOT resolves to when it is not pinned to a version dir."""
    return engine_home() / "current"


def trace_dir() -> Path:
    return state_dir() / "trace"


def telemetry_dir() -> Path:
    return state_dir() / "telemetry"
