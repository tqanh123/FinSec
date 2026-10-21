#!/usr/bin/env python3
"""context_firewall.py — PreToolUse gate on a tool call's blast radius.

Sizes a Read/Glob/Grep/Bash call from deterministic signals (file bytes, glob
match count, presence of an offset/limit/head_limit window, shell output
shape) and answers on a ladder:

    allow  -> nothing to say
    warn   -> exit 0, advisory reaches the human via a queued systemMessage
              (the same soft-compliance-core channel gate_stage's own
              soft-stage advisories use — see hook_runtime.queue_system_message)
    narrow -> exit 0, `hookSpecificOutput.updatedInput` bounds the SAME call
              (see context_firewall_policy.NARROW_REWRITES for which signals
              carry a rewrite and why the rest do not) — queued via
              hook_runtime.queue_updated_input alongside an allow (return None)
    block  -> exit 2, the reason names a concrete bounded alternative

Never duplicates the privacy guard (`privacy_read_guard.py`, secret-path
gating) or a scout guard — this only ever sizes CONTENT volume, never path
SENSITIVITY, and it never inspects WHAT a call returns, only what it COULD.

Runs as a `hook_dispatch.py` core (see harness/data/hook-dispatch.yaml groups
`PreToolUse:Bash`, `PreToolUse:Read|NotebookRead`, `PreToolUse:Grep|Glob`) —
every registered (event, matcher) pair in this repo must resolve to a
hook-dispatch.yaml group (`test_hook_dispatch_group_presence.py`,
`verify_install.dispatch_group_problems`), so a warn/narrow/block ladder has
to speak the dispatcher's existing per-core contract rather than invent a
parallel standalone one. `core(data)` therefore returns None for allow/warn/
narrow (the queued side-channels above carry the guidance) and a STRING only
for a genuine block — the one shape `_run_group` already turns into exit 2.
`main()` below wraps the same `core` in `run_compliance_hook` so the hook is
also directly runnable/testable as a standalone script.

HOOK_CLASS = compliance (it can genuinely block), with the same documented
deviation `disabled_skill_router.py`/`model_guard.py` already carry: an
INTERNAL error fails OPEN (this hook is a heuristic on call size, not a
security control — a crash here must not brick every Bash/Read/Grep/Glob call
in the session). Only a real `block` verdict exits 2.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hook_runtime  # noqa: E402
import context_firewall_signals as signals_mod  # noqa: E402
import context_firewall_policy as policy_mod  # noqa: E402

HOOK_CLASS = "compliance"
_HOOK = "context_firewall"
_CONFIG_NAME = "context-firewall.yaml"

_DEFAULT_THRESHOLDS = {
    "read_warn_bytes": 256 * 1024,
    "read_block_bytes": 2 * 1024 * 1024,
    "shell_dump_warn_bytes": 128 * 1024,
    "shell_dump_block_bytes": 512 * 1024,
    "search_head_limit": 100,
    "narrow_search_head_limit": 100,
    "glob_breadth_warn_count": 200,
    "glob_breadth_block_count": 2000,
    "repeat_block_count": 3,
    "narrow_read_limit_lines": 200,
}

_JOURNAL_DIR = "context-firewall"


def _thresholds() -> dict:
    """Shipped defaults merged with the layered config — a malformed value never
    disables a limit, it just keeps the default for that one key."""
    try:
        import config_layers
        doc, _diag = config_layers.read_soft(_CONFIG_NAME)
    except Exception:  # noqa: BLE001 — a broken config layer must not crash the gate
        doc = {}
    out: dict = dict(_DEFAULT_THRESHOLDS)
    if isinstance(doc, dict):
        for key in _DEFAULT_THRESHOLDS:
            val = doc.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                out[key] = val
    return out


def _journal_path(session_id) -> "Path | None":
    if not session_id:
        return None
    try:
        base = hook_runtime._state_dir() / _JOURNAL_DIR
        return base / ("%s.jsonl" % hook_runtime.safe_session_id(session_id))
    except Exception:  # noqa: BLE001
        return None


def _fingerprint(kind: str, signal_id: str, target: str) -> str:
    normalized = "%s|%s|%s" % (kind or "", signal_id or "", " ".join((target or "").split()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _repeat_count(path, fingerprint: str) -> int:
    """How many prior lines in this session's journal already carry `fingerprint`.
    A read, never a rewrite — the journal is append-only."""
    if path is None or not path.is_file():
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("fingerprint") == fingerprint:
                    count += 1
    except OSError:
        return 0
    return count


def _record(path, *, actor, session, fingerprint, tool, signal_id, action) -> None:
    """Append one intervention line. Best-effort: a store failure degrades to no
    repeat-count memory for the rest of the session, never to a crash."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": hook_runtime.datetime.now(hook_runtime.timezone.utc).isoformat(),
            "actor": actor, "session": session, "fingerprint": fingerprint,
            "tool": tool, "signal": signal_id, "action": action,
        }, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def evaluate(data: dict, thresholds: dict) -> tuple:
    """Pure decision step: (verdict dict, kind, facts) for one PreToolUse
    payload. Split out from core() so tests can drive it without stdin/stdout,
    and records the intervention as a side effect (same as core() would)."""
    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return {"action": "allow"}, "other", {}
    raw_cwd = data.get("cwd")
    cwd: str = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else os.getcwd()
    kind, sigs, facts = signals_mod.compute_signals(tool_name, tool_input, cwd, thresholds)
    if not sigs:
        return {"action": "allow"}, kind, facts

    session = data.get("session_id")
    target = facts.get("file_path") or facts.get("pattern") or facts.get("command") or ""
    dominant = policy_mod.dominant_signal(sigs)
    fingerprint = _fingerprint(kind, dominant["id"] if dominant else "", target)
    path = _journal_path(session)
    repeats = _repeat_count(path, fingerprint)

    verdict = policy_mod.decide(sigs, facts, thresholds, repeat_count=repeats)
    if verdict["action"] != "allow":
        _record(path, actor=hook_runtime.resolve_actor(session_id=session), session=session,
                 fingerprint=fingerprint, tool=tool_name, signal_id=verdict.get("signal_id"),
                 action=verdict["action"])
    return verdict, kind, facts


def core(data: dict):
    """Compliance core (run_compliance_hook / hook_dispatch contract): None for
    allow/warn/narrow (guidance travels via the queued side-channels below); a
    STRING block reason only for a real `block` verdict."""
    verdict, _kind, _facts = evaluate(data, _thresholds())
    action = verdict.get("action", "allow")
    if action == "allow":
        return None

    message = "Context firewall: %s\n%s" % (verdict.get("reason", ""), verdict.get("strategy", ""))
    if action == "block":
        return message
    if action == "narrow":
        # updatedInput replaces the whole tool_input, so the bound rides on the full input
        hook_runtime.queue_updated_input(dict(data.get("tool_input") or {}, **(verdict.get("updated_input") or {})))
    hook_runtime.queue_system_message(message)  # warn AND narrow both reach the human
    return None


def main() -> int:
    """Standalone entry (also used by hook_dispatch.py via `core` directly).
    Deliberately NOT `run_compliance_hook`/`compliance_skip_or_run` — both are
    fail-CLOSED by design, and this gate's own deviation (matching
    `disabled_skill_router.py`/`model_guard.py`: a heuristic on call size, not
    a security control) is that an INTERNAL error fails OPEN. Only a genuine
    `block` verdict from `core()` exits 2, and that exit sits outside the
    fail-open try so a broad `except` can never swallow it. The dispatched
    path carries the same posture via `fail_open: true` on this hook's rows in
    hook-dispatch.yaml."""
    try:
        if not hook_runtime.hook_enabled(_HOOK, "compliance"):
            hook_runtime.emit_continue()
            return 0
    except Exception as e:  # noqa: BLE001 — a broken enabled read must not wedge a call
        hook_runtime.log_hook_error(_HOOK, e)
        hook_runtime.emit_continue()
        return 0

    data = hook_runtime.read_stdin_json()
    try:
        reason = core(data)
    except Exception as e:  # noqa: BLE001 — FAIL-OPEN: a heuristic crash must never brick a call
        hook_runtime.log_hook_error(_HOOK, e)
        hook_runtime._reset_pending_system_messages()
        reason = None

    if reason:
        sys.stderr.write("[%s] BLOCKED: %s\n" % (_HOOK, reason))
        return 2

    queued = hook_runtime._drain_system_messages()
    updated_input = hook_runtime._drain_updated_input()
    if updated_input is not None:
        hook_runtime._emit_allow_with_updated_input(updated_input, queued)
    elif queued:
        hook_runtime.emit_system_message(queued)
    else:
        hook_runtime.emit_continue()
    return 0


if __name__ == "__main__":
    sys.exit(main())
