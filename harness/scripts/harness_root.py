#!/usr/bin/env python3
"""harness_root.py — the ONE place `HARNESS_BIN_ROOT` / project-root resolution
lives. Shared by hs-run and hs-cli (siblings, neither imports the other — see
hs_run.py's own module docstring for why that boundary matters) so both CLIs
apply the SAME rules instead of hand-rolling their own `CLAUDE_PROJECT_DIR or
_ROOT` fallback per call site.

Two DIFFERENT questions live here, and they resolve to two DIFFERENT
directories under a global install — conflating them is exactly the bug this
module exists to close:

  `resolve_project_root()` answers "where does THIS PROJECT'S data live" —
  `.harness/`, `plans/`. Under a global install that is never the shared bin
  tree (the final guard below refuses that outright); under self-host/
  per-project install it is wherever `CLAUDE_PROJECT_DIR` (or a `.git`/`plans/`
  walk-up) lands.

  `bin_root_env()` answers "where does the shared harness INSTALLATION live" —
  scripts, skills, `harness/data/*.yaml`. Under a global install that is
  `HARNESS_BIN_ROOT`; under self-host/per-project install there is no separate
  bin tree, so callers fall back to their own `__file__`-derived root (the
  physical location of the running script IS the installation, in both of
  those modes).

A caller reading its OWN installation's catalog (hs-cli's `skills`/
`components`, which read `harness/plugins/hs/skills/`,
`harness/data/components.yaml`) wants `bin_root_env()`, never
`resolve_project_root()` — `CLAUDE_PROJECT_DIR` names the project currently
being worked on, which is a completely different directory from the shared
catalog under a global install, and reading it as if it held the catalog was
a real, reproduced bug: `hs-cli skills` silently printed 0 lines,
`hs-cli components` threw a raw traceback, and `--enable` claimed an
already-present skill needed re-fetching — all three because the CLI read
the wrong directory, not because anything was actually missing.

── vs. `harness_paths.py` — two modules, two DIFFERENT questions, NOT a merge
target ──────────────────────────────────────────────────────────────────────
`harness_paths.project_root()`/`data_root()` and THIS module's
`resolve_project_root()` both answer "what project am I working on", and both
read `CLAUDE_PROJECT_DIR`/`HARNESS_DATA_ROOT` — so on the surface they look
like the same function twice. They are not, and delegating one to the other
would be wrong, not just redundant:

  - `harness_paths` serves HOOKS: every guard/hook call runs inside a live
    Claude Code turn, where `CLAUDE_PROJECT_DIR` is ALWAYS set, and a wrong
    answer there can widen a write lane. Its contract is FAIL-CLOSED — an
    unresolvable project under a global install returns a sentinel
    (`_UNRESOLVED`) the caller MUST treat as "block", never CWD.
    `harness_paths.project_root()`'s global-install branch also falls back to
    the SHARED BIN root as a last resort — safe for a hook (which only reads
    shipped config from it), wrong for a CLI verb that would then read/write
    PROJECT state into shared binary infrastructure.

  - THIS module serves the two standalone CLIs (hs-run, hs-cli), invoked
    directly by a human or an agent's Bash tool — a context where
    `CLAUDE_PROJECT_DIR` is frequently UNSET (reproduced: a bare
    Bash-tool subprocess carries no hook env at all). Precedence here is
    CLAUDE_PROJECT_DIR-first specifically so a live session's project always
    wins when present; PLAIN CWD is the honest last resort (there is a real,
    just-typed command to answer, not a guard decision to make), and
    `bin_root_env()`'s refusal (the final guard below) is the one borrowed
    safety property: never resolve to the shared bin tree.

Net effect: given `HARNESS_DATA_ROOT=/a/.harness` and `CLAUDE_PROJECT_DIR=/b`
BOTH set, `harness_paths.data_root()` answers `/a/.harness` (HARNESS_DATA_ROOT
wins there) while THIS module's `resolve_project_root()` answers `/b`
(CLAUDE_PROJECT_DIR wins here) — a real, observable disagreement, not a bug to
converge: a hook writing state and a CLI verb resolving `plans/` in the SAME
process tree can legitimately be answering to two different `HARNESS_DATA_ROOT`
overrides for two different purposes (a hook's own state store vs. the plans/
tree of the project a human is actively typing commands in). Do not "fix" this
by making one call the other; if a caller ever needs BOTH answers to agree, it
must resolve via `harness_paths` explicitly (never assume this module's answer
already matches it).

`ProjectRootError` deliberately does NOT subclass `hs_run.RegistryError`, even
though it did before this module was extracted (hs_run.py used to define
`class ProjectRootError(RegistryError)` purely so `main()`'s existing
`except RegistryError` handled it for free). This module is now a SHARED
sibling of hs-run AND hs-cli — hs-cli has no `RegistryError` concept at all —
so subclassing hs_run's error here would be a layering violation (and a
circular import: hs_run.py imports THIS module). `hs_run.main()` instead
catches `ProjectRootError` with its own explicit `except` clause at the one
call site that can raise it — verified: both raise sites in this module are
caught explicitly by their callers, so the lost inheritance is harmless in
practice, just no longer "free."
"""
from __future__ import annotations

import os
from pathlib import Path


class ProjectRootError(Exception):
    """A stale/misconfigured project root (bad `CLAUDE_PROJECT_DIR`, a
    filesystem-root `HARNESS_DATA_ROOT`, a `HARNESS_DATA_ROOT` that is not a
    data home, or a resolved root that IS the shared bin root under a global
    install) — never silently degrade to CWD or the
    bin tree; name the problem instead."""


#: `resolve_project_root()` source values that mean "this answer came from a
#: walk-up the caller never chose": tier 3's CWD walk-up and the verified
#: own-worktree redirect. The `env:*` sources are deliberate ABSENT entries —
#: a caller who pinned a project (CLAUDE_PROJECT_DIR / HARNESS_DATA_ROOT)
#: already chose the answer, even when it differs from cwd.
_WALKUP_SOURCES = ("walked-up-to:", "cwd-worktree-root")


def walkup_disclosure(root, source, cwd=None):
    """The warnings[] line disclosing that `root` was resolved by walking UP
    from the caller's cwd to a DIFFERENT directory — or None when there is
    nothing to disclose (root == cwd, or the root came from an explicit pin).

    Tier 3 of `resolve_project_root()` (no CLAUDE_PROJECT_DIR, no
    HARNESS_DATA_ROOT) walks up from CWD to the nearest `plans/`-or-`.git`
    ancestor and answers for it — correctly, and silently: an agent standing
    in a scratch subdirectory gets an envelope for the project above it
    without one line saying so. This closes the silence, not the behavior:
    the resolution result is contracted elsewhere and unchanged, and this
    function is PURE over its arguments (reads no env, decides only on the
    (root, source) the resolver already returned plus cwd) so a disclosure
    can never change the answer it describes.

    One shared definition for every CLI that consumes `resolve_project_
    root()` and carries a `warnings` list on its envelope — hs-run's step
    envelope today; the message names the pin knobs that exist per CLI
    (hs-run has no --root flag; hs-code's is --repo)."""
    s = str(source or "")
    if not s.startswith(_WALKUP_SOURCES):
        return None
    try:
        cur = (Path(cwd) if cwd is not None else Path.cwd()).resolve()
    except OSError:
        return None
    if Path(root) == cur:
        return None
    if s == "cwd-worktree-root":
        why = "cwd sits inside a linked worktree of that project"
    elif s.endswith("plans-dir"):
        why = "it is the nearest ancestor carrying a plans/ directory"
    else:
        why = "it is the nearest ancestor carrying a .git entry"
    return (
        "root resolved by walking up: answering for %s while standing in %s "
        "(%s). If that is not the project you meant, run the command from "
        "inside it, or pin it with HARNESS_DATA_ROOT=<project>/.harness "
        "(or CLAUDE_PROJECT_DIR=<project>; hs-code also accepts --repo). "
        "Under a global install HARNESS_BIN_ROOT names the shared binary — "
        "set it alongside the project pin." % (root, cur, why))


def _walk_up_for_root(start: Path):
    """The nearest ancestor of `start` carrying a root marker, or None —
    `harness_paths.walk_up_for_project_root`, one definition for both
    resolvers."""
    import harness_paths
    return harness_paths.walk_up_for_project_root(start)


def _data_root_misplaced(data_dir: Path):
    """Why `data_dir` cannot be a data home, or None. Borrowed from
    `harness_paths.data_root_misplaced()` like `_walk_up_for_root`: one
    definition of "data home" for the hook resolver and the CLI resolver. Not
    fail-open — `harness_paths` ships beside this module, and a missing copy
    must not let a project directory pass as its own data home."""
    import harness_paths
    return harness_paths.data_root_misplaced(data_dir)


def _cwd_worktree_root_if_host_is(cwd: Path, expected_host: Path):
    """cwd's own linked-worktree root, but ONLY when that worktree's actual
    HOST equals `expected_host` — the project a tier ALREADY resolved by some
    other means (`CLAUDE_PROJECT_DIR`'s own walk-up, or `HARNESS_DATA_ROOT`'s
    parent). ONE shared gate for both tiers of `resolve_project_root()`, and
    the same answer `harness_paths.project_root()` gives hooks: it lives in
    `harness_paths.worktree_root_if_host_is`, the lower module, so the two
    never import each other in a cycle."""
    import harness_paths
    return harness_paths.worktree_root_if_host_is(cwd, expected_host)


def bin_root_env():
    """`HARNESS_BIN_ROOT`, resolved, ONLY when it names a real directory —
    else None. Read directly from the env (never inferred from `__file__` or
    CWD): a caller that wants "wherever THIS running script physically lives"
    already has that in its own `__file__`-derived constant and should fall
    back to it itself when this returns None (self-host / per-project install
    have no separate bin tree, so there is nothing further this function
    could usefully guess)."""
    raw = os.environ.get("HARNESS_BIN_ROOT")
    if not raw:
        return None
    try:
        p = Path(raw)
        return p.resolve() if p.is_dir() else None
    except OSError:
        return None


def resolve_project_root():
    """Returns (root: Path, source: str) — the CURRENT PROJECT's data root,
    never the shared bin tree. Precedence:

    1. `CLAUDE_PROJECT_DIR`, when it names an EXISTING directory — the working
       project of a live Claude Code session. A value that does NOT exist
       (stale/rotated/typo'd) raises `ProjectRootError` instead of silently
       falling through to CWD. The directory is then walked UP for a root
       marker (`plans/` or `.git`): `CLAUDE_PROJECT_DIR` pointing at a real,
       existing SUBdirectory of the actual project (`<proj>/src/deep`) must
       not be trusted as-is — that used to build a second `plans/` tree there
       instead of finding the real one one level up. Only when no ancestor
       carries a marker does the value stand as given (a genuinely fresh,
       non-git project — nothing higher to prefer).

       A live Claude Code session in a git worktree typically sets
       `CLAUDE_PROJECT_DIR` to the HOST checkout while the process's actual
       cwd is the worktree — so once this tier's own answer R is settled,
       cwd is checked one more time: if it sits inside a linked worktree
       whose real HOST (verified, never assumed) IS R, `plans/` resolves
       from the worktree instead (`_cwd_worktree_root_if_host_is`, shared
       with tier 2's identical check below). A worktree of some OTHER,
       unrelated project leaves this tier's answer untouched.
    2. Otherwise, `HARNESS_DATA_ROOT` — the project's own `.harness/` data
       home under a global install, when there is no live Claude Code session
       to supply `CLAUDE_PROJECT_DIR`. Names the project directly (its
       parent — mirrors `harness_paths.data_root()`'s own `<project>/.harness`
       shape) without needing to guess from CWD, which under this exact shape
       can land on the shared bin root itself (a git checkout, so it carries
       a `.git` marker) and silently write project data into shared
       infrastructure.
    3. Otherwise, walk up from CWD looking for a `plans/` dir or a `.git`
       entry.
    4. If no marker is found anywhere up to the filesystem root, CWD itself
       is the answer — there is nothing higher to prefer.

    FINAL guard, applied to whichever tier above won: under a global install
    (`HARNESS_BIN_ROOT` set to a real directory), a resolved root that turns
    out to BE the shared bin root is refused outright (`ProjectRootError`)
    rather than silently accepted — the bin tree is read-only shared
    infrastructure (docs/harness/global-install-guide.md's two-zone model);
    project data never belongs there. Self-host (`HARNESS_BIN_ROOT` unset,
    bin == project) never trips this — `bin_root_env()` returns None there.
    """
    bin_root = bin_root_env()

    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        cand = Path(proj)
        if not cand.is_dir():
            raise ProjectRootError(
                "CLAUDE_PROJECT_DIR=%r does not exist (or is not a directory) — "
                "refusing to silently fall back to the current directory; fix or "
                "unset CLAUDE_PROJECT_DIR before re-running" % proj)
        resolved = cand.resolve()
        found = _walk_up_for_root(resolved)
        if found is not None:
            marker = "plans-dir" if (found / "plans").is_dir() else "git"
            root, source = found, "env:CLAUDE_PROJECT_DIR-walked-up-to:%s" % marker
        else:
            root, source = resolved, "env:CLAUDE_PROJECT_DIR"
        wt_root = _cwd_worktree_root_if_host_is(Path.cwd(), root)
        if wt_root is not None:
            root, source = wt_root, "cwd-worktree-root"
    else:
        data_root_raw = os.environ.get("HARNESS_DATA_ROOT")
        if data_root_raw:
            try:
                dr = Path(data_root_raw).resolve()
            except OSError:
                dr = Path(data_root_raw)
            parent = dr.parent
            if parent == Path(parent.anchor):
                raise ProjectRootError(
                    "HARNESS_DATA_ROOT=%r resolves to a project directory at the "
                    "filesystem root (%s) — refusing to treat the filesystem root "
                    "as a project" % (data_root_raw, parent))
            misplaced = _data_root_misplaced(dr)
            if misplaced:
                raise ProjectRootError(
                    "HARNESS_DATA_ROOT=%r is not a harness data home: %s"
                    % (data_root_raw, misplaced))
            # cwd standing inside a LINKED worktree owns its OWN `plans/`
            # tree, which must win over HARNESS_DATA_ROOT — that env may
            # legitimately name the shared HOST's `.harness/state` instead
            # (the F2 redirect `harness_paths.py` keeps for STATE). But only
            # when the worktree's OWN host actually IS the project
            # HARNESS_DATA_ROOT names: a worktree of some unrelated repo must
            # not steal a run configured for a DIFFERENT project (a real
            # global-install shape — an explicit per-project data root
            # pointed elsewhere than whatever repo the shell happens to be
            # sitting inside). Same shared gate tier 1 uses above.
            wt_root = _cwd_worktree_root_if_host_is(Path.cwd(), parent)
            if wt_root is not None:
                root, source = wt_root, "cwd-worktree-root"
            else:
                root, source = parent, "env:HARNESS_DATA_ROOT"
        else:
            cwd = Path.cwd()
            found = _walk_up_for_root(cwd)
            if found is not None:
                marker = "plans-dir" if (found / "plans").is_dir() else "git"
                root, source = found, "walked-up-to:%s" % marker
            else:
                root, source = cwd.resolve(), "cwd-no-markers-found"

    if bin_root is not None and root == bin_root:
        raise ProjectRootError(
            "resolved project root %s is the SHARED bin root (HARNESS_BIN_ROOT=%s) "
            "under a global install — refusing to read/write project data into "
            "shared binary infrastructure (docs/harness/global-install-guide.md's "
            "two-zone model: the bin tree is read-only shared infrastructure, never "
            "one project's plans/ home); set CLAUDE_PROJECT_DIR or HARNESS_DATA_ROOT "
            "to the actual project before re-running, or run from inside it"
            % (root, bin_root))

    return root, source


if __name__ == "__main__":
    root, source = resolve_project_root()
    print("root=%s source=%s" % (root, source))
