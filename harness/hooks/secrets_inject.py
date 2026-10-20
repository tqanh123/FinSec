#!/usr/bin/env python3
"""secrets_inject.py — PreToolUse compliance hook: substitute ${secret:NAME}
in memory, or block the call.

Scans ONLY the fields the config declares per tool (harness/data/
secrets-inject.yaml — Bash.command, WebFetch.url, ...); a ${secret:REF} in
an undeclared tool or field passes through untouched, because that is what
"only the declared fields" means. Each REF resolves through the hs-secrets
CLI's `resolve` verb — the same grant-gated door the agent itself would hit,
running with THIS hook's process as the caller, so the agent binary above
Claude Code is the identity the grant checks.

Substitution answers on stdout as one JSON object with
hookSpecificOutput.updatedInput (measured applying, Claude Code 2.1.284:
the model sent `echo AAA`, the command ran `BBB`); the value reaches the
tool call in memory and is never written to a log or a file by this hook.

Posture: compliance, fail-CLOSED on the substitution's own errors — a REF
that does not resolve (missing, no grant, backend failure, unreadable
config) blocks the call with exit 2 and a reason that names the REF, never
the value. Absent/unparseable stdin has nothing to scan and passes, the
same fail-open edge bash_safety_guard carries.

It runs inside the in-process dispatcher (its own `PreToolUse:Bash|WebFetch`
group in harness/data/hook-dispatch.yaml): `core` returns a dict carrying
`updatedInput`, the dispatcher's third answer after allow and deny, and the
dispatcher writes it as the one terminal stdout line. The standalone
`__main__` stays for tests and debugging.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = Path(_HOOKS_DIR).parents[1]
sys.path.insert(0, str(_ROOT / "harness" / "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)

HOOK_CLASS = "compliance"
_NAME = Path(__file__).stem

REF_PATTERN = re.compile(r"\$\{secret:([A-Za-z0-9][A-Za-z0-9._/-]*)\}")
CONFIG_PATH = _ROOT / "harness" / "data" / "secrets-inject.yaml"


class _Block(Exception):
    """A refusal the wrapper maps to exit 2 — the message never carries a value."""


def _fields_for(tool: str) -> list:
    import yaml_io
    try:
        cfg = yaml_io.yaml_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — an unreadable scan list must name itself
        raise _Block("secrets-inject config unreadable: %s" % e)
    if not isinstance(cfg, dict):
        raise _Block("secrets-inject config is not a mapping")
    tools = cfg.get("tools") or {}
    fields = tools.get(tool) or []
    if not isinstance(fields, list):
        raise _Block("secrets-inject config: tools.%s must be a list of fields" % tool)
    return [str(f) for f in fields]


def _resolve(ref: str):
    """The ref's value through the hs-secrets CLI, or (None, reason).

    Runs `resolve` as a subprocess of THIS hook, so the grant check sees the
    agent binary above the hook — exactly the identity that will use the
    value. The answer is read out of the family envelope (one JSON line);
    the value itself rides that pipe and nothing else."""
    argv = [sys.executable, str(_ROOT / "harness" / "scripts" / "hs_secrets.py"),
            "resolve", ref]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "resolve did not answer (%s)" % e
    first = proc.stdout.splitlines()[0] if proc.stdout.strip() else ""
    if first.startswith("{"):
        try:
            doc = json.loads(first)
        except ValueError:
            doc = {}
        if proc.returncode == 0:
            return str(doc.get("output") or "").rstrip("\n"), None
        reason = str(doc.get("error") or doc.get("output") or
                     "resolve refused %s" % ref).strip()
        return None, reason
    return None, (proc.stderr.strip() or "resolve failed for %s" % ref)


def _substitute(text: str, cache: dict):
    """One field's text with every ${secret:REF} replaced (or raises _Block)."""
    def repl(m):
        ref = m.group(1)
        if ref not in cache:
            cache[ref] = _resolve(ref)
        value, why = cache[ref]
        if value is None:
            raise _Block("${secret:%s} did not resolve: %s" % (ref, why))
        return value
    return REF_PATTERN.sub(repl, text)


def core(data: dict):
    """None ⇒ nothing to do; dict {"updatedInput": ...} ⇒ the rewritten input;
    str ⇒ a block reason. Only declared (tool, field) pairs are ever rewritten.

    The cheap placeholder scan runs FIRST, over every string value, so a call
    with no ${secret:...} anywhere never touches the config (or the store) —
    and the fail-closed posture engages exactly when there is something to
    scan, never as a tax on every tool call."""
    if not isinstance(data, dict):
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if not any(isinstance(v, str) and REF_PATTERN.search(v)
               for v in tool_input.values()):
        return None
    tool = str(data.get("tool_name") or "")
    try:
        fields = _fields_for(tool)
    except _Block as e:
        return str(e)
    if not fields:
        return None  # a placeholder in an undeclared tool passes untouched
    interesting = {f: tool_input[f] for f in fields
                   if isinstance(tool_input.get(f), str) and REF_PATTERN.search(tool_input[f])}
    if not interesting:
        return None  # the placeholder sits in a field this tool does not declare
    rewritten = dict(tool_input)
    cache: dict = {}
    try:
        for field, text in interesting.items():
            rewritten[field] = _substitute(text, cache)
    except _Block as e:
        return str(e)
    return {"updatedInput": rewritten}


def _emit_update(new_input: dict) -> bool:
    try:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": new_input,
            }}, ensure_ascii=False))
        sys.stdout.flush()
        return True
    except Exception:  # noqa: BLE001 — a failed write must not fake success
        return False


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        data = json.loads(raw)
    except ValueError:
        return 0  # nothing parseable to scan — same edge as the Bash gate
    if not isinstance(data, dict):
        return 0  # a JSON scalar is not a tool call; there is nothing to scan
    try:
        out = core(data)
    except Exception as e:  # noqa: BLE001 — fail CLOSED on our own errors
        print("BLOCKED: secrets_inject failed: %s" % e, file=sys.stderr)
        return 2
    if out is None:
        return 0
    if isinstance(out, dict):
        if not _emit_update(out["updatedInput"]):
            print("BLOCKED: secrets_inject could not deliver the rewritten input",
                  file=sys.stderr)
            return 2
        return 0
    print("BLOCKED: %s" % out, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
