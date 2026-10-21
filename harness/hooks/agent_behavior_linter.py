#!/usr/bin/env python3
"""agent_behavior_linter.py — observes PostToolUse/PreCompact, reports at Stop.

Records a redacted event per PostToolUse/PreCompact call to a per-session
append-only journal, then at Stop re-folds the whole journal and runs the
detectors (`agent_behavior_linter_detectors.py`) over the result: a re-read
of an unchanged file, a deterministic error repeated with nothing changed, a
wide scan never narrowed, output that changed nothing, and a docs-only change
verified with the whole suite. Every finding links to the rule it comes from.

Deliberately NEVER registered on PreToolUse — the only event that can deny a
tool call — so observation cannot block anything as a matter of wiring, not
of promise. HOOK_CLASS = nudge: fail-open, advisory-only, default-ON switch
lives at `agent_behavior_linter` in harness/data/harness-hooks.yaml.

Registered as three `hook_dispatch.py` cores (see harness/data/hook-dispatch.yaml):
`handle_post_tool_use` on every PostToolUse group, `handle_pre_compact` on the
PreCompact group, `handle_stop` on Stop. Also runnable standalone (this
module's own main()) for the subprocess tests in this repo's suite.
"""
import json
import os
import sys
from pathlib import Path  # noqa: F401 — used in the `"Path | None"` return annotation

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(_HERE), "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hook_runtime  # noqa: E402
import agent_behavior_linter_events as events_mod  # noqa: E402
import agent_behavior_linter_store as store_mod  # noqa: E402
import agent_behavior_linter_detectors as detectors_mod  # noqa: E402
import agent_behavior_linter_report as report_mod  # noqa: E402

HOOK_CLASS = "nudge"
_HOOK = "agent_behavior_linter"
_JOURNAL_DIR = "agent-behavior-linter"
_CONFIG_NAME = "agent-behavior-linter.yaml"


def _thresholds() -> dict:
    try:
        import config_layers
        doc, _diag = config_layers.read_soft(_CONFIG_NAME)
    except Exception:  # noqa: BLE001 — a broken config layer must not crash the hook
        doc = {}
    out: dict = dict(detectors_mod.DEFAULT_THRESHOLDS)
    if isinstance(doc, dict):
        for key in detectors_mod.DEFAULT_THRESHOLDS:
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


def _append(path, record: dict, *, actor, session) -> None:
    """Append one redacted record. Best-effort: a store failure degrades to a
    thinner Stop report, never to a crash."""
    if path is None or record is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record, actor=actor, session=session,
                                ts=hook_runtime.datetime.now(hook_runtime.timezone.utc).isoformat()),
                           ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _read_records(path):
    if path is None or not path.is_file():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def handle_post_tool_use(data: dict):
    """Nudge core: append one redacted record. Never returns a message — this
    hook only ever speaks at Stop."""
    session = data.get("session_id") if isinstance(data, dict) else None
    record = events_mod.normalize_event(data)
    if record is not None:
        _append(_journal_path(session), record,
                actor=hook_runtime.resolve_actor(session_id=session), session=session)
    return None


def handle_pre_compact(data: dict):
    session = data.get("session_id") if isinstance(data, dict) else None
    record = events_mod.normalize_event({"hook_event_name": "PreCompact"})
    _append(_journal_path(session), record,
            actor=hook_runtime.resolve_actor(session_id=session), session=session)
    return None


def _finding_keys(finding: dict) -> "set[str]":
    """What a finding is ABOUT: its id plus each subject its evidence names
    (a file, a command, a search root) -- never a running count, which grows
    every turn and would make every Stop look new."""
    fid = str(finding.get("id"))
    subjects: "set[str]" = set()
    for value in (finding.get("evidence") or {}).values():
        for item in value if isinstance(value, list) else ():
            if isinstance(item, dict):
                subjects.update(str(item[k]) for k in ("path", "command_hash") if item.get(k))
    root = (finding.get("evidence") or {}).get("repeated_root")
    if root:
        subjects.add(str(root))
    return {"%s|%s" % (fid, s) for s in subjects} or {fid}


def handle_stop(data: dict):
    """Nudge core: fold this session's whole journal, run every detector, and
    return the rendered summary -- only when a finding names something not
    already reported this session (None otherwise). The journal is folded
    whole each Stop, so a finding that stays true would otherwise repeat on
    every turn for the rest of the session."""
    session = data.get("session_id") if isinstance(data, dict) else None
    path = _journal_path(session)
    records = _read_records(path)
    state = store_mod.fold(records)
    findings = detectors_mod.run_detectors(state, _thresholds())
    keys = set().union(*(_finding_keys(f) for f in findings)) if findings else set()
    reported = {k for r in records if r.get("kind") == "reported" for k in r.get("keys") or ()}
    if not keys - reported:
        return None  # skip:nothing-to-report -- every finding already reported this session
    _append(path, {"kind": "reported", "keys": sorted(keys)},
            actor=hook_runtime.resolve_actor(session_id=session), session=session)
    return report_mod.render_summary(state, findings) or None


def _dispatch(data: dict):
    event = data.get("hook_event_name") if isinstance(data, dict) else None
    if event == "Stop":
        return handle_stop(data)
    if event == "PreCompact":
        return handle_pre_compact(data)
    if event == "PostToolUse":
        return handle_post_tool_use(data)
    return None


def main() -> int:
    data = hook_runtime.read_stdin_json()
    msg = None
    try:
        if hook_runtime.hook_enabled(_HOOK, "nudge"):
            msg = _dispatch(data)
    except Exception as e:  # noqa: BLE001 — a nudge must never break the op
        try:
            hook_runtime.log_hook_error(_HOOK, e)
        except Exception:  # noqa: BLE001
            pass
        msg = None
    if msg:
        session = data.get("session_id") if isinstance(data, dict) else None
        hook_runtime.emit_nudge(_HOOK, msg, session=session)
    hook_runtime.drain_or_continue()
    return 0


if __name__ == "__main__":
    sys.exit(main())
