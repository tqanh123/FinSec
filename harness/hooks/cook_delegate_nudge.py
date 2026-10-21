#!/usr/bin/env python3
"""cook_delegate_nudge.py — advisory backstop for cook's per-phase posture.

Cook runs IN PLACE by default now; a subagent is the workflow-weight
reader's call (`workflow_weight.posture`) over the phase's declared files —
in_place unless a forcing flag or the declared-file count says otherwise.
That posture is prose on the skill side, not a gate — nothing blocks an
inline run. This hook is the soft reminder in the direction that still
needs one: when the MAIN agent writes non-test SOURCE that a phase whose
posture says SUBAGENT declares, it fires ONCE (per plan/session) to say
"hand this write to a `@developer` subagent, or split the phase". A phase
under the threshold answers in place and never hears from this hook —
inline is the default, not the exception to police.

The discriminator is agent IDENTITY, not stateful phase-window counting: a write that
carries an `agent_type`/`subagent_type` attribution IS the delegation (a subagent is
doing it) → stay silent. A write with attribution ABSENT is the main thread → worth
flagging, but only when the covering phase's declared files pushed the posture to
subagent (the threshold, not plan `mode:`, decides — a `mode: fast` plan with a
nine-file phase still gets the nudge).

Nudge posture: default-ON, advisory, fail-open — writes a reminder to its configured
sink and ALWAYS continues (never exit 2). Deduped once per (session, plan) so a phase
full of writes nudges once, not per-file. The binding HOOK_CLASS lives here in code.

Scope note: this is deliberately cook-only (plan-bound). The reliable predicate —
in_progress plan + main-agent source write a large phase declares — only exists where
a plan is being cooked phase-by-phase; a generic "you have subagents but ran inline"
nudge across every skill would be a false-positive machine (small fixes and advisory
skills all run inline legitimately, which is now also cook's own default).
"""


import fnmatch
import os
import sys
from pathlib import Path

# Diagnostic text carries Vietnamese; guard stderr encoding so a non-UTF-8 locale
# degrades to replacement chars instead of raising mid-write (fail-open).
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — older streams / already-detached; never fatal
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import harness_paths  # noqa: E402
import plan_graph  # noqa: E402
import scope_match  # noqa: E402
import workflow_weight  # noqa: E402
import hook_runtime  # noqa: E402
import nudge_dedupe  # noqa: E402

HOOK_CLASS = "nudge"
_NAME = Path(__file__).stem
_DEDUPE_KIND = "cook_delegate"

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Only a code-source write is a 3.I implement. A doc/config/data write, a test file,
# an artifact — none are the implement step, so none should nudge. An explicit code
# allowlist keeps false positives near zero (config/docs never trip it).
_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift",
    ".scala", ".sh", ".sql",
}

def _root() -> Path:
    """The project root whose plans/ we resolve the active plan under —
    `harness_paths.project_root()`, the ONE shared choke point every
    plans-resolving hook reads `root` from: it already redirects to a linked
    worktree's own `plans/` when cwd stands inside one (`_cwd_worktree_plans_
    redirect`), which a raw `hook_runtime.project_dir()` read (CLAUDE_PROJECT_DIR
    or stdin cwd only) does not — that used to read the HOST's active plan
    from a session standing in a worktree."""
    return harness_paths.project_root()


def _has_agent_attribution(data: dict) -> bool:
    """True when the write carries a non-empty subagent attribution — i.e. a subagent
    (not the main thread) is doing it, which IS the delegation. Read straight off the
    top-level payload, not via resolve_actor (session_id is shared)."""
    for key in ("agent_type", "subagent_type"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return True
    return False


def _target_rel(data: dict, root: Path):
    """The write target as a path relative to root (best-effort). Returns a POSIX-ish
    relative string, or None when there is no path."""
    ti = data.get("tool_input") or {}
    raw = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
    if not raw:
        return None
    p = Path(str(raw))
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        # Outside root or unresolvable — fall back to the raw string; classification
        # below still works off basename/suffix.
        return p.as_posix()


def _is_implement_source(rel: str) -> bool:
    """True when rel is a code-source implement target: a known code ext, NOT a test
    file, NOT under a docs/plans tree."""
    low = rel.lower()
    parts = low.split("/")
    base = parts[-1]
    if Path(low).suffix not in _CODE_EXTS:
        return False
    if base.startswith("test_") or base.endswith("_test.py") or ".test." in base or ".spec." in base:
        return False
    if any(seg in ("tests", "test", "__tests__") for seg in parts[:-1]):
        return False
    if parts[0] in ("docs", "plans"):
        return False
    return True


def _prompt_flags(plan_dir: Path, node: str) -> tuple:
    """The forcing flags `cook prompt developer` persisted for THIS phase
    (`kind: posture_flags` in the append-only phase-commits log, last-wins).
    The hook has no channel to the CLI, so that record is the only way the
    posture here can agree with the posture the caller actually asked for
   . Fail-open: no log, no record, any error → no flags."""
    try:
        import json
        path = plan_dir / "artifacts" / "phase-commits.jsonl"
        flags = None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "posture_flags" and rec.get("phase") == node:
                flags = [str(f) for f in (rec.get("flags") or []) if str(f)]
        return tuple(flags or [])
    except Exception:  # noqa: BLE001 — flags are an input, never a crash
        return ()


def _covers(rel: str, entry: str) -> bool:
    """One declared entry vs one written path. A DIRECTORY entry (trailing
    `/`) covers by prefix — and only by prefix-with-slash, so `feat2/x.py`
    does not match `feat/` (the counter-control). A glob entry matches
    by scope_match; anything else matches literally."""
    e = str(entry)
    if e.endswith("/"):
        return rel.startswith(e)
    if any(c in e for c in "*?["):
        return scope_match.glob_to_regex(e).match(rel) is not None
    return e == rel


def _covering_posture(plan_dir: Path, rel: str, root=None):
    """(node, posture) of the phase whose declared files cover `rel`, else (None, None).

    A declaration covers `rel` when a `files_to_*` entry matches it literally,
    by glob (scope_match — the one matcher plan_graph itself uses), or — for a
    DIRECTORY entry — by prefix. The posture is then the reader's
    answer over THAT phase's full declared set, directory entries expanded to
    what git tracks at the time asked, the forcing flags `prompt` persisted for
    the phase, and the plan's own `posture:`. Fail-open: any read/parse error or
    contradicting forces → (None, None) (a broken graph never nudges)."""
    try:
        graph = plan_graph.parse_phase_graph(plan_dir)
        for node in sorted((graph.get("subtasks") or {})):
            files = plan_graph.declared_files(graph, node)
            if any(_covers(rel, f) for f in files):
                expanded = plan_graph.expand_declared(root, files) if root else files
                return node, workflow_weight.posture(
                    sorted(expanded), flags=_prompt_flags(plan_dir, node),
                    declared=workflow_weight.declared_posture(plan_dir))
    except Exception:  # noqa: BLE001 — fail-open: a nudge never breaks on a broken graph
        pass
    return None, None


def core(data: dict, root: Path):
    """Return (advisory, plan_name) iff a MAIN-agent write hits source a phase
    whose posture (declared files + prompt-persisted flags) answers SUBAGENT;
    else None. Routing + dedupe are the caller's job — this never blocks."""
    if data.get("tool_name") not in _WRITE_TOOLS:
        return None
    if _has_agent_attribution(data):
        return None  # a subagent is writing — this IS the delegation
    rel = _target_rel(data, root)
    if not rel or not _is_implement_source(rel):
        return None

    from artifact_check import resolve_active_plan
    plan_dir = resolve_active_plan(root)
    if plan_dir is None:
        return None
    node, posture = _covering_posture(plan_dir, rel, root=root)
    if posture is None or posture.mode != "subagent":
        return None  # in place is the default; only a large/forced phase is worth a word

    msg = (
        "[nudge] cook_delegate: the workflow-weight posture says phase %s runs as a "
        "subagent (%s). Hand this write to a `@developer` subagent (main keeps verify "
        "+ the paired commit), or split the phase if it should stay in place. "
        "Advisory, non-blocking.\n" % (node, posture.reason)
    )
    return msg, plan_dir.name


def core_dispatch(data: dict):
    """In-process dispatcher entry: a single-arg core(data) that resolves the project
    root, applies the once-per-(session, plan) dedupe, and RETURNS the advisory string
    (or None). The dispatcher runs this instead of main() and emits the return value
    unconditionally, so the dedupe MUST live here — otherwise the nudge would fire on
    every source write in the phase instead of once. Fail-open: any error → None."""
    if not isinstance(data, dict):
        return None  # skip:not-applicable
    try:
        hit = core(data, _root())
    except Exception as e:  # noqa: BLE001 — a nudge never blocks the tool
        hook_runtime.log_hook_error(_NAME, e)
        return None
    if not hit:
        return None  # skip:nothing-to-report
    msg, subject = hit
    session = data.get("session_id") or os.environ.get("HARNESS_SESSION_ID") or ""
    if nudge_dedupe.already_nudged(session, _DEDUPE_KIND, subject):
        hook_runtime.trace_skip(_NAME, "already-reported", session=data.get("session_id"))
        return None
    nudge_dedupe.mark_nudged(session, _DEDUPE_KIND, subject)
    return msg


def main() -> int:
    # Nudge structure mirrors cook_isolation_nudge: honor the config gate, run the
    # detector fail-open, dedupe once per (session, plan), then route via
    # emit_nudge_and_continue (config sink) + ALWAYS continue.
    if not hook_runtime.hook_enabled(_NAME, HOOK_CLASS):
        hook_runtime.emit_continue()
        return 0
    data = hook_runtime.read_stdin_json()
    d = data if isinstance(data, dict) else {}
    try:
        hit = core(d, _root())
        if hit:
            msg, subject = hit
            session = d.get("session_id") or os.environ.get("HARNESS_SESSION_ID") or ""
            if not nudge_dedupe.already_nudged(session, _DEDUPE_KIND, subject):
                nudge_dedupe.mark_nudged(session, _DEDUPE_KIND, subject)
                hook_runtime.emit_nudge_and_continue(_NAME, msg, d)
                return 0
    except Exception as e:  # noqa: BLE001 — fail-open: a nudge never blocks the tool
        hook_runtime.log_hook_error(_NAME, e)
    hook_runtime.emit_continue()
    return 0


if __name__ == "__main__":
    sys.exit(main())
