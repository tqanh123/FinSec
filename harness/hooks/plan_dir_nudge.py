#!/usr/bin/env python3
"""plan_dir_nudge.py — PreToolUse(Write/Edit/MultiEdit) nudge: a write under a
`plans/<dir>/` that has no valid `plan.md` will never anchor a hard stage.

`resolve_active_plan` and the artifact gate only ever recognize a plan through
`plans/<dir>/plan.md` carrying a parseable frontmatter `status:` — a dir with no
`plan.md`, or one whose frontmatter is missing/unparseable, is invisible to
pr/merge/ship/deploy no matter how much work lands under it. That gap is silent
until the moment a stage tries to anchor to it; this hook surfaces it at write
time instead, the first time something is written into such a dir this session.

Scope: only a write that resolves under `<project root>/plans/<dir>/` (never
`plans/reports/`, the gitignored scratch tree, and never the dir's OWN
`plan.md` — creating that file IS the fix, so a write to it must not warn about
its own absence). Validity reuses `artifact_check._frontmatter_status` — the
one shared frontmatter-status reader `resolve_active_plan` itself uses — rather
than a second parser that could disagree with it about the same file.

Nudge class: fail-open, silent on its own errors, config-gated in
harness-hooks.yaml — ships `enabled: true` there today, per the current
default posture; the nudge CLASS default without an entry would be OFF.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import harness_paths  # noqa: E402
import hook_runtime  # noqa: E402
import nudge_dedupe  # noqa: E402
import write_guard  # noqa: E402 — reuse rel_target (resolve-then-relative_to), not a second resolver

HOOK_CLASS = "nudge"
_NAME = Path(__file__).stem
_DEDUPE_KIND = "plan_dir"

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

_MESSAGE = (
    "[nudge] plan_dir: `plans/%s/` has no valid plan.md (frontmatter status) — "
    "pr/merge/ship/deploy cannot anchor to this plan dir (the artifact gate "
    "resolves plans only through plans/*/plan.md carrying a frontmatter status). "
    "Run hs:plan to scaffold one. Advisory; it does not block cook.\n"
)


def _root(raw: str) -> Path:
    """The tree the WRITE TARGET `raw` itself lives in — host repo, or
    whichever linked worktree the target sits inside — resolved by walking
    UP FROM THE TARGET PATH (`harness_paths.walk_up_for_project_root`), never
    from cwd.

    `harness_paths.project_root()` (the prior resolver here) answers "which
    project is the CALLER standing in" — it redirects to a linked worktree's
    own root only when CWD stands inside one. That fixed a write landing
    under the worktree while cwd stood there too, but it is still the wrong
    question for this hook: a write can target either side of a host/worktree
    split independently of where the invoking shell's cwd happens to be. cwd
    inside a worktree, writing into the HOST's own invalid plan dir, or the
    reverse (cwd at the host, writing into a worktree's invalid plan dir) —
    both used to resolve `root` to the wrong tree, `rel_target` then found
    the target outside it, and the hook went silent about a target that was
    never under whichever tree cwd happened to answer for. Walking up from
    the target itself instead of cwd answers correctly for both.

    Falls back to `harness_paths.project_root()` only when the target's own
    ancestry carries no root marker at all (`plans/` nor `.git`) — never
    None, matching this function's prior contract."""
    found = harness_paths.walk_up_for_project_root(Path(raw))
    return found if found is not None else harness_paths.project_root()


def _target_raw(data: Dict[str, Any]):
    ti = data.get("tool_input") or {}
    return ti.get("file_path") or ti.get("path")


def _plan_dir_name(rel: str) -> Optional[str]:
    """The immediate dir name under `plans/` that `rel` (root-relative POSIX
    path) lives INSIDE, or None when `rel` is not under a `plans/<dir>/` at
    all, or is directly under the gitignored `plans/reports/` scratch tree.

    Requires >= 3 parts (`plans`, `<dir>`, `<something>`): a target with
    exactly 2 parts is a FILE directly under `plans/` (e.g. `plans/README.md`)
    — `parts[1]` there is a filename, not a plan dir, and must not be read as
    one."""
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "plans":
        return None
    name = parts[1]
    if name == "reports":
        return None
    return name


def _has_valid_plan_md(plan_md: Path) -> bool:
    """True iff `plan_md` exists and its frontmatter parses with a `status:`
    key — the same test `resolve_active_plan` applies. Any read/parse error
    reads as invalid (fail toward nudging, never toward hiding a real gap)."""
    if not plan_md.is_file():
        return False
    try:
        text = plan_md.read_text(encoding="utf-8")
    except OSError:
        return False
    import artifact_check as ac
    return ac._frontmatter_status(text) is not None


def core(data: Dict[str, Any]) -> Optional[str]:
    """Return the advisory for the first write this session into a plans/<dir>/
    with no valid plan.md, or None."""
    if not isinstance(data, dict) or data.get("tool_name") not in _WRITE_TOOLS:
        return None  # skip:not-applicable
    raw = _target_raw(data)
    if not raw:
        return None  # skip:not-applicable

    root = _root(raw)
    rel = write_guard.rel_target(raw, root)
    if rel is None:
        return None  # skip:not-applicable (outside the project root)
    dirname = _plan_dir_name(rel)
    if dirname is None:
        return None  # skip:not-applicable (not under plans/<dir>/, or plans/reports/)
    if rel == "plans/%s/plan.md" % dirname:
        return None  # skip:not-applicable — writing plan.md IS the fix

    plan_md = root / "plans" / dirname / "plan.md"
    if _has_valid_plan_md(plan_md):
        return None  # skip:nothing-to-report

    session_id = data.get("session_id") or os.environ.get("HARNESS_SESSION_ID") or ""
    if nudge_dedupe.already_nudged(session_id, _DEDUPE_KIND, dirname):
        hook_runtime.trace_skip(_NAME, "already-reported", session=data.get("session_id"))
        return None
    nudge_dedupe.mark_nudged(session_id, _DEDUPE_KIND, dirname)
    return _MESSAGE % dirname


def main() -> int:
    data = hook_runtime.read_stdin_json()
    d = data if isinstance(data, dict) else {}
    try:
        if hook_runtime.hook_enabled(_NAME, HOOK_CLASS):
            msg = core(d)
            if msg:
                hook_runtime.emit_nudge_and_continue(_NAME, msg, d)
                return 0
    except Exception as e:  # noqa: BLE001 — a nudge must never break the tool
        try:
            hook_runtime.log_hook_error(_NAME, e)
        except Exception:  # noqa: BLE001 — logging itself must never be fatal
            pass
    hook_runtime.emit_continue()
    return 0


if __name__ == "__main__":
    sys.exit(main())
