#!/usr/bin/env python3
"""hook_runtime.py — one shared runtime for all harness hooks.

Ported from product-spec hook_runtime.py (crash audit, stdin/stdout skeleton,
config cache, telemetry wrapper) and generalized for the harness:

  * 3 hook classes instead of 2 hard-coded stem sets:
      - telemetry:  default ON,  fail-open, always {"continue": true}
      - nudge:      default OFF, advisory (stderr + exit 0)
      - compliance: default ON + BLOCKING, fail-CLOSED (exit 2 + reason)
    The class is a CODE CONSTANT in each hook file (`HOOK_CLASS = "..."`),
    never config data: a broken config file must not change what a
    hook is, only whether it is enabled and which mode it runs in.
  * config = harness-hooks.yaml (human-edited config is YAML).
    PyYAML is imported lazily INSIDE the config loader so telemetry/nudge
    paths stay importable without it; the compliance wrapper turns a missing
    dep into exit 2 + the install command.
  * resolve_actor(): every hook resolves identity independently —
    session file is an optional cache, never a prerequisite.

Telemetry/nudge public functions are fail-open: they never raise back into a
hook. The compliance wrapper is the one place that fails closed by design.
"""

import json
import os
import re
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

# --- shared Bash script matcher (single home for the Pre/Post:Bash pair) ------
# mark_bash_start (PreToolUse:Bash) and track_script_execution (PostToolUse:Bash)
# read ONE matcher from here so they can never drift out of lockstep. It matches
# a harness script (harness/scripts/<f>.py|sh or harness/e2e/<f>.py|sh) run in
# EXECUTION position — not merely referenced. A bare substring would count
# `grep ... scripts/check_fence.py`, `ls .../verify_install.py`, `cat ...` as
# runs and (via any read-back of these records) inflate the run signal with
# greps. Requiring a command boundary + optional interpreter is what keeps the
# signal real. group(1) = the harness-relative path (scripts/<f> | e2e/<f>).
#
# The path may carry an arbitrary leading dir prefix (`./`, an absolute path, or
# `"$CLAUDE_PROJECT_DIR"/...`): `(?:\S*/)?harness/` consumes it WITHOUT reopening
# the substring hole — it cannot bridge the space at an argument position, so a
# grep/ls/cat of the path (even an absolute one) still has no boundary in front
# and stays rejected.
#
# A bare `(` is NOT a boundary char: it opens a regex/code group as often as a
# subshell, so `python3 -c '... re.compile(r"(harness/scripts/x.py)")'` would
# false-count the capture group as a run. A genuine subshell that runs a harness
# script still presents a real boundary before the script (`(cd d && …`, `(…; …`)
# via the ; & | newline class, so dropping `(` loses no real execution signal.
SCRIPT_RE = re.compile(
    r"(?:^|[\n;|&])\s*"                                    # command boundary
    r"(?:[A-Za-z_]\w*=\S*\s+)*"                            # optional leading VAR=val env
    r"(?:(?:\S*/)?(?:python3?|bash|sh)(?:\s+-\S+)*\s+)?"   # optional interpreter (+ flags)
    r"(?:\S*/)?harness/((?:scripts|e2e)/[^\s]+\.(?:py|sh))"  # optional dir prefix (abs/$VAR/.)
)

# --- test-run recognition (the red-before-green floor) -----------------------
#
# SCRIPT_RE anchors on a `harness/scripts/...` path in EXECUTION position, which
# is exactly what a pytest invocation lacks: `python3 -m pytest <paths>` runs a
# MODULE and the paths that follow are arguments. So the one command class whose
# red/green transition carries the TDD claim was the one class the recorder could
# not see. These two helpers close that, and stay pure so a caller can pass
# records from any sink.
#
# `pytest:` prefixes the recorded target to keep it in a namespace of its own —
# a test FILE and a script PATH would otherwise collide in the same `script`
# field, and a lens counting script runs would silently absorb test runs.
_PYTEST_RE = re.compile(
    r"(?:^|[\n;|&])\s*"                          # command boundary
    r"(?:[A-Za-z_]\w*=\S*\s+)*"                  # optional leading VAR=val env
    r"(?:(?:\S*/)?python3?\s+-m\s+)?"            # optional `python3 -m`
    r"pytest\b(?P<args>[^\n;|&]*)"               # the runner, then its argv tail
)
# The repo's own runner: CLAUDE.md names it as the command CI runs, so a red/green
# pair recorded through it must count the same as a direct pytest pair.
_RUNNER_RE = re.compile(r"harness/scripts/run_local_tests\.py(?P<args>[^\n;|&]*)")


def test_run_target(command):
    """The test target this command runs, or None when it is not a test run.

    Returns a STRING, not a bool, because a bare yes/no cannot pair a red run
    with the green run OF THE SAME target — a red `test_a.py` followed by a green
    `test_b.py` would otherwise read as red->green. Falls back to the runner's own
    name when no path argument is present (a whole-suite run is still one target).
    """
    if not command:
        return None
    match = _PYTEST_RE.search(command) or _RUNNER_RE.search(command)
    if not match:
        return None
    for token in (match.group("args") or "").split():
        if token.startswith("-"):
            continue
        # a node id (`file.py::test_name`) narrows to its file: the red run often
        # names one test and the green run the whole file, and they are the same
        # target for this question.
        path = token.split("::", 1)[0]
        if path.endswith(".py") or "/" in path:
            return path
    return "run_local_tests.py" if _RUNNER_RE.search(command) else "pytest"


def had_red_before_green(records, target):
    """Did a FAILING run of `target` precede a PASSING one, in record order?

    False on an empty history by design: no evidence is not evidence of TDD. The
    caller distinguishes "did not happen" from "was not recorded" — this function
    only answers what the records show.
    """
    if not target:
        return False
    seen_red = False
    for rec in records or []:
        script = str(rec.get("script") or "")
        if not script.startswith("pytest:"):
            continue
        if target not in script[len("pytest:"):]:
            continue
        try:
            failed = int(rec.get("exit") or 0) != 0
        except (TypeError, ValueError):
            continue
        if failed:
            seen_red = True
        elif seen_red:
            return True
    return False


# --- crash audit (ported PS as-is, env names re-prefixed) --------------------

_LOG_NAME = "hook-crashes.log"
_LOG_MAX_BYTES = 256 * 1024  # coarse cap; over this we rotate to .1 then truncate


def yaml_load(text):
    """The one shared YAML read, delegated to yaml_io.yaml_load (ruamel.yaml,
    C-accelerated via ruamel.yaml.clib).

    ── WHY THIS DELEGATES INSTEAD OF PARSING HERE ────────────────────────────────
    Ruling (docs decisions, 2026-07-29, re-affirmed 2026-08-02): replace PyYAML
    with ruamel.yaml everywhere — a partial swap ("hooks stay on PyYAML, the rest
    moves") recreates the exact thing the ruling forbids, two YAML readers with
    two meanings for one file. yaml_io.py (harness/scripts/) is the SINGLE place
    that imports ruamel.yaml; this function is a thin call-through so every
    caller of hook_runtime.yaml_load keeps working unchanged.

    ── WHY THIS WAS PyYAML+CSafeLoader BEFORE, AND WHAT CHANGED ─────────────────
    The prior version resolved `getattr(yaml, "CSafeLoader", yaml.SafeLoader)`
    once and cached the LOADER CLASS. ruamel resolves its C parser
    (`ruamel.yaml.clib`) on the YAML() INSTANCE instead (`pure=False`, the
    default) — yaml_io._loader() caches that instance the same way this module
    used to cache the class. The one behavior change worth knowing: ruamel's
    safe loader raises DuplicateKeyError on a repeated mapping key where PyYAML
    silently kept the last value (see test_yaml_io.py).

    `import yaml_io` stays LAZY (module import happens on first call), matching
    this module's convention that telemetry/nudge paths must not explode if a
    dependency is missing; the compliance wrapper is what turns that into exit 2.

    Callers still catch yaml.YAMLError / ruamel's YAMLError — same exception
    family shape as before, pinned by test_yaml_fast_loader.py.
    """
    try:
        import yaml_io
    except ImportError:
        sys.path.append(str(_hooks_dir().parent / "scripts"))
        import yaml_io
    return yaml_io.yaml_load(text)


def _hooks_dir() -> Path:
    return Path(__file__).resolve().parent


def _log_dir() -> Path:
    # HARNESS_HOOK_LOG_DIR lets tests redirect the crash log to a tmp dir.
    raw = os.environ.get("HARNESS_HOOK_LOG_DIR")
    return Path(raw) if raw else _hooks_dir() / ".logs"


def _audit_disabled() -> bool:
    # Always-on by default; off via env, and silent under pytest so test runs
    # never write the real crash log.
    return bool(
        os.environ.get("HARNESS_HOOK_AUDIT_DISABLED")
        or os.environ.get("PYTEST_CURRENT_TEST")
    )


def log_hook_error(hook_name, exc) -> None:
    """Append ONE line (UTC ts, hook, exc type, message, traceback tail) to
    hook-crashes.log. Itself fail-open: any IO error is swallowed. Logs
    exception metadata ONLY, never the stdin payload (no PII leak)."""
    if _audit_disabled():
        return
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tb_tail = tb.strip().splitlines()[-1] if tb.strip() else ""
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "hook": str(hook_name),
                "type": type(exc).__name__,
                "msg": str(exc)[:500],
                "tb": tb_tail[:500],
            },
            ensure_ascii=False,
        )
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / _LOG_NAME
        try:
            if p.stat().st_size > _LOG_MAX_BYTES:
                p.replace(d / (_LOG_NAME + ".1"))
        except OSError:
            pass  # no file yet, or unstattable — nothing to rotate
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # fail-open: a crash logger must never crash a hook


# --- stdin / stdout skeleton (ported PS as-is) --------------------------------

def _parse(raw) -> dict:
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_stdin_json() -> dict:
    """Read stdin and parse it as a JSON object. Empty/malformed → {} (fail-open)."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    return _parse(raw)


def bash_command(data: dict) -> str:
    """The Bash tool's command string from a PreToolUse payload, coerced to
    str ("" when the payload shape is unexpected or the command is absent or
    non-string). Centralizes the boundary guard every PreToolUse(Bash) gate runs
    before stage detection, so the "what is a non-string command" answer lives
    in one place."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def emit_continue() -> None:
    """Emit the non-blocking contract: {"continue": true} on stdout."""
    try:
        sys.stdout.write(json.dumps({"continue": True}))
        sys.stdout.flush()
    except Exception:
        pass  # fail-open


def emit_system_message(text: str) -> None:
    """Emit the CC-spec-guaranteed user-visible field (`SyncHookJSONOutput.
    systemMessage` in the Claude Code hook spec) alongside
    `continue: true`. A HIGH-priority / security-and-ops nudge whose stderr-on-
    exit-0 advisory is otherwise invisible to a human uses this instead of
    `sys.stderr.write` (H2-resolved, INV-3 F-2). Fail-open like emit_continue.

    A hook's stdout is ONE JSON blob -- this writes it, so the CALLER must not
    also call emit_continue() in the same invocation (see each hook's own
    single-stdout-write guard, e.g. a per-module `_STDOUT_WRITTEN` flag)."""
    try:
        sys.stdout.write(json.dumps({"continue": True, "systemMessage": text}))
        sys.stdout.flush()
    except Exception:
        pass  # fail-open


def _emit_allow_with_updated_input(updated_input: dict, system_message: str = "") -> None:
    """Terminal write for an ALLOW verdict that also narrows the call:
    `hookSpecificOutput.updatedInput` (PreToolUse-only field CC reads to
    replace `tool_input`) plus an optional human-visible systemMessage.
    Fail-open like every other terminal writer here."""
    try:
        blob = {"continue": True,
                "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated_input}}
        if system_message:
            blob["systemMessage"] = system_message
        sys.stdout.write(json.dumps(blob))
        sys.stdout.flush()
    except Exception:
        pass  # fail-open


# Opt-in queue: a COMPLIANCE hook's core() may want a soft/advisory line to reach
# the spec-guaranteed systemMessage field, but core() must never write stdout
# itself -- run_compliance_hook owns the hook's ONE terminal JSON write (the
# fail-closed contract lives there). queue_system_message() lets core() mark that
# line; the wrapper drains it into the SAME stdout write it would have made
# anyway (no double-write, no change to any OTHER compliance hook that never
# calls this -- the queue is empty for them, so _drain_system_messages() is a
# silent no-op and behavior is byte-identical to before).
#
# Entries carry a GENERATION tag, not just text. hook_dispatch.py runs each
# core in its own daemon worker thread (run_core_isolated) observed with
# join(timeout); a core that times out is left running in the background (the
# timeout never injects into the worker). If that straggler later calls
# queue_system_message(), the dispatcher has by then already moved on to a
# LATER core and reset the queue for it -- without a generation check the
# straggler's message lands in that later core's drain and reads as if the
# later core said it. Each worker thread is tagged with the generation that
# was current when run_core_isolated started it (see that function); a message
# is only ever drained under the SAME generation it was queued under, so a
# stale straggler's message is silently dropped instead of misattributed. A
# core invoked directly (no thread -- the standalone hook wrappers) has no
# thread-local generation, so it falls back to whatever is currently the
# module's generation -- always "current" for that call, matching the old
# undifferentiated single-hook behavior exactly.
_pending_system_messages: list[tuple[object, str]] = []
# Sibling queue, same generation-tagging discipline: a compliance core() calls
# queue_updated_input() ALONGSIDE returning None (allow) to attach a PreToolUse
# `updatedInput` — the semantics-preserving "narrow" rung of a ladder gate
# (context_firewall.py is the one caller today). Every OTHER compliance core
# never calls this, so the queue is always empty for them and draining it is a
# no-op — byte-identical to before this existed.
_pending_updated_input: list[tuple[object, dict]] = []
_message_generation = 0
_generation_local = threading.local()


def _next_message_generation() -> int:
    """Advance and return the generation for a newly-starting core run. Test seam."""
    global _message_generation
    _message_generation += 1
    return _message_generation


def _current_generation():
    """The generation the CALLING thread's messages should be tagged with: the
    one captured for it at thread-start (run_core_isolated), or the module's
    current generation when there is none (direct/main-thread callers)."""
    return getattr(_generation_local, "generation", _message_generation)


def queue_system_message(text: str) -> None:
    """Opt-in: a compliance core() calls this (instead of stderr) to mark an
    advisory that should reach the human via systemMessage once the wrapper's
    single terminal stdout write happens. Never writes stdout itself. Tagged
    with the calling thread's generation (see module notes above) so a
    straggler from a timed-out core cannot leak into a later core's message."""
    _pending_system_messages.append((_current_generation(), str(text)))


def queue_updated_input(value: dict) -> None:
    """Opt-in: a compliance core() calls this ALONGSIDE returning None (allow)
    to attach a PreToolUse `updatedInput` — a rewrite that keeps the call's own
    semantics (same tool, same target, an added bound) rather than denying it.
    Never writes stdout itself; the wrapper (run_compliance_hook / the
    dispatcher's _run_group) drains it into the SAME terminal write a plain
    allow would have produced. Tagged with the calling thread's generation,
    same discipline as queue_system_message — a straggler from a timed-out
    core cannot land in a later core's drain. `updatedInput` is a PreToolUse-
    only concept in the Claude Code hook contract; a caller outside that event
    gets nothing back for it (there is no other event to attach it to)."""
    _pending_updated_input.append(
        (_current_generation(), dict(value) if isinstance(value, dict) else value))


def _drain_system_messages() -> str:
    """Pop + join every queued message TAGGED WITH THE CURRENT GENERATION
    (newline-joined), clearing the queue so a later invocation in the same
    process never sees a stale leftover. A message queued under a generation
    that is no longer current (a timed-out core's background thread, still
    running) is dropped, not returned -- see module notes above."""
    global _pending_system_messages
    msgs = [text for gen, text in _pending_system_messages if gen == _message_generation]
    _pending_system_messages = []
    return "\n".join(msgs) if msgs else ""


def _drain_updated_input():
    """Pop the updatedInput queued under THE CURRENT GENERATION, or None. Last
    value queued wins (mirrors a single-slot channel) — at most one core in a
    group is expected to call queue_updated_input in practice."""
    global _pending_updated_input
    vals = [v for gen, v in _pending_updated_input if gen == _message_generation]
    _pending_updated_input = []
    return vals[-1] if vals else None


def _reset_pending_system_messages() -> None:
    """Test seam + per-core clean slate: drop any queued-but-undrained message
    (and updatedInput) AND advance the generation (mirrors _reset_config_cache).
    Advancing the generation is what makes a timed-out core's still-running
    background thread unable to have a later queue_system_message() /
    queue_updated_input() call land in a subsequent core's drain -- its thread
    is tagged with the OLD generation, which no longer matches once this has
    run."""
    global _pending_system_messages, _pending_updated_input
    _pending_system_messages = []
    _pending_updated_input = []
    _next_message_generation()


# --- nudge visibility router (3 sinks, config-driven — INV-3 F-2) -------------
#
# HOOK_CLASS picks the FAILURE posture (fail-open vs fail-closed); it does NOT
# say who an advisory reaches. That is a separate, configurable axis: visibility
# is THREE INDEPENDENT booleans, not one sink enum --
#   model   -> record a `<name>_observation`; nudge_context_inject re-surfaces it
#              to the MODEL as additionalContext next turn.
#   user    -> shown to the HUMAN this turn (emit/queue_system_message).
#   stderr  -> `[advisory] ...` on stderr; exit-0 stderr reaches NOBODY (debug log
#              only), but a fail-closed hook's exit-2 stderr IS the block reason.
# The four legacy sink names survive only as back-compat sugar over those flags:
#   off = {model:false,user:false} · stderr = +{stderr:true} · relay = model-only
#   · systemMessage = model+user. No shipped config uses them.
#
# The map lives in harness/data/nudge-channels.yaml (human-edited); a repo owner
# MAY point HARNESS_NUDGE_CHANNELS at an override file (e.g. .harness-dev/ with
# `default: systemMessage` so a dev sees every nudge). Precedence, load-bearing:
#   per-name file entry > file-global `default:` (only when the file sets one)
#   > the caller's code-level default_channel > "stderr".
# Ship OMITS `default:`, so an unlisted security nudge keeps its code default
# (systemMessage) instead of being silenced. A broken/absent file fails OPEN to
# the caller default -- a visibility router must never crash or block a hook.

_NUDGE_CHANNELS_NAME = "nudge-channels.yaml"
_VALID_CHANNELS = ("relay", "systemMessage", "stderr", "off")
_nudge_channels_cache = None  # None = not yet loaded

# THREE independent output flags, not one tangled sink enum. A nudge can reach the
# model, the human-always, and/or the human-on-error — independently:
#   model -> record a `<name>_observation` (resurfaced to the MODEL as
#            additionalContext next UserPromptSubmit / folded into the /goal reinject).
#   user  -> queue a `systemMessage` the HUMAN sees THIS turn (unconditional).
#   stderr-> write an `[advisory]` line to stderr. Silent on exit 0 (a fail-open
#            nudge), BUT on exit 2 (a fail-closed hook that blocks) CC surfaces it to
#            the user AND feeds it to the model as the block reason. So stderr =
#            ERROR-ONLY visibility — DISTINCT from `off` (never visible). Do not
#            conflate the two: off drops the advisory entirely; stderr keeps it as the
#            block message a compliance hook shows only when it actually stops the op.
# The legacy 1-D sink NAMES are accepted as sugar and mapped here — a `systemMessage`
# that (post fix) ALSO reached the model is exactly the ambiguity this split removes.
#   relay = model-only · systemMessage = both · stderr = error-only · off = never
_AXES_FROM_CHANNEL = {
    "relay":         {"model": True,  "user": False, "stderr": False},
    "systemMessage": {"model": True,  "user": True,  "stderr": False},
    "stderr":        {"model": False, "user": False, "stderr": True},
    "off":           {"model": False, "user": False, "stderr": False},
}


def _axes_to_channel(ax) -> str:
    """Collapse resolved axes back to the closest legacy sink name (compat shim for
    nudge_channel / tests): both -> systemMessage, model-only -> relay, debug-only ->
    stderr, else off."""
    if ax.get("user") and ax.get("model"):
        return "systemMessage"
    if ax.get("model"):
        return "relay"
    if ax.get("stderr"):
        return "stderr"
    return "off"


def _norm_entry(v):
    """A YAML value → resolved axes {model,user,stderr}, or None to drop.
    Accepts BOTH the new 2-axis mapping `{model: bool, user: bool[, stderr: bool]}`
    AND a legacy sink string / bare `off` (YAML 1.1 folds a bare `off` → False)."""
    if v is False:  # bare `off`
        return dict(_AXES_FROM_CHANNEL["off"])
    if isinstance(v, str) and v in _AXES_FROM_CHANNEL:
        return dict(_AXES_FROM_CHANNEL[v])
    if isinstance(v, dict) and ("model" in v or "user" in v or "stderr" in v):
        return {"model": bool(v.get("model", False)),
                "user": bool(v.get("user", False)),
                "stderr": bool(v.get("stderr", False))}
    return None



def _nudge_channels_path() -> Path:
    """Through the one door; the layer stack is declared in config-layers.yaml."""
    sd = str(_hooks_dir().parent / "scripts")
    if sd not in sys.path:
        sys.path.insert(0, sd)
    import config_layers
    return config_layers.resolve_path(_NUDGE_CHANNELS_NAME)[0]


def _load_nudge_channels() -> dict:
    """Parse the channel map once per process. Malformed/unreadable/missing-PyYAML
    or a bogus env path ⇒ {default:None, channels:{}} (every nudge then falls to
    its caller default) + a crash-log line. Never raises."""
    global _nudge_channels_cache
    if _nudge_channels_cache is not None:
        return _nudge_channels_cache
    cfg: dict[str, Any] = {"default": None, "channels": {}}
    try:
        p = _nudge_channels_path()
        # An override pointing at a missing/unreadable file falls back to the tracked
        # ship file, so a stale HARNESS_NUDGE_CHANNELS never blanks the map — the SOFT
        # door's own shape, asked for the last candidate rather than spelled again here.
        if not p.is_file():
            import config_layers
            p = config_layers.candidates(_NUDGE_CHANNELS_NAME)[-1][1]
        if p.is_file():
            raw = yaml_load(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                d = _norm_entry(raw.get("default"))
                if d is not None:
                    cfg["default"] = d
                ch = raw.get("channels")
                if isinstance(ch, dict):
                    cfg["channels"] = {
                        str(k): _norm_entry(v) for k, v in ch.items()
                        if _norm_entry(v) is not None
                    }
    except Exception as e:  # noqa: BLE001 — a visibility router must not crash a hook
        log_hook_error("hook_runtime", e)
        cfg = {"default": None, "channels": {}}
    _nudge_channels_cache = cfg
    return cfg


def _reset_nudge_channels_cache() -> None:
    """Test seam: force a re-read of the channel map (mirrors _reset_config_cache)."""
    global _nudge_channels_cache
    _nudge_channels_cache = None


def nudge_axes(name, default_channel="stderr") -> dict:
    """Resolve the {model,user,stderr} axes for nudge `name`. Precedence: a per-name
    entry > the file-global `default:` (only when the file sets one) > the caller's
    code-level default_channel (a legacy sink name) > stderr. Fail-open to the caller
    default — a visibility resolver never crashes a hook."""
    try:
        cfg = _load_nudge_channels()
        ax = cfg["channels"].get(name)
        if isinstance(ax, dict):
            return ax
        d = cfg.get("default")
        if isinstance(d, dict):
            return d
    except Exception as e:  # noqa: BLE001 — fail-open to the caller default
        log_hook_error("hook_runtime", e)
    return dict(_AXES_FROM_CHANNEL.get(default_channel, _AXES_FROM_CHANNEL["stderr"]))


def nudge_channel(name, default_channel="stderr") -> str:
    """Back-compat shim: the legacy sink STRING for `name`, derived from its resolved
    axes. Prefer nudge_axes() in new code — the sink name loses the model/user split."""
    return _axes_to_channel(nudge_axes(name, default_channel))


def nudge_observation_names() -> list:
    """Names whose model axis is on (they record a `<name>_observation` — the MODEL
    bus): the legacy `relay` AND `systemMessage` sinks both qualify (systemMessage is
    an additive human layer over the model bus, not a replacement). The generic
    resurface pass uses THIS — every model-axis name, not a model-only subset — so a
    both-axes nudge still reaches the model, not just the human. Per-name entries
    only, not a file-global default."""
    try:
        cfg = _load_nudge_channels()
        return [n for n, ax in cfg.get("channels", {}).items()
                if isinstance(ax, dict) and ax.get("model")]
    except Exception as e:  # noqa: BLE001 — fail-open
        log_hook_error("hook_runtime", e)
        return []


def _record_nudge_observation(name, text, *, session, actor=None) -> None:
    """Append a `<name>_observation` for the relay sink (read by
    nudge_context_inject). Best-effort; append_event swallows its own errors."""
    import trace_log
    trace_log.append_event(hook=name, event="%s_observation" % name,
                           actor=actor, session=session,
                           status="observed", note=str(text))


def emit_nudge_and_continue(name, text, data=None, *,
                            default_channel="stderr") -> None:
    """For a hand-rolled nudge main() that owns its own stdout write: route `text`
    to its configured sink, then emit the hook's SINGLE terminal blob (a
    systemMessage if the sink queued one, else a plain continue). Drop-in for the
    old `sys.stderr.write(text); emit_continue()` pair. `data` supplies session_id
    for the relay sink."""
    session = data.get("session_id") if isinstance(data, dict) else None
    emit_nudge(name, text, session=session, default_channel=default_channel)
    queued = _drain_system_messages()
    if queued:
        emit_system_message(queued)
    else:
        emit_continue()


def route_relay_nudge(name, text, record_obs, *, default_channel="relay") -> None:
    """Router for a nudge that records its OWN bespoke `<family>_observation` (the
    caller passes `record_obs`). Dispatches on the resolved {model,user} axes WITHOUT
    writing terminal stdout:
      model -> call record_obs() (the observation is the model's channel — resurfaced
               at the next UserPromptSubmit OR folded into the /goal reinject reason);
      user  -> queue the advisory as a systemMessage (the hook's main() drains it).
    The two are INDEPENDENT: both on (legacy `systemMessage`) reaches both; model-only
    (legacy `relay`) reaches the model quietly; user-only reaches only the human; a
    bare `stderr`/neither writes a debug `[advisory]` line that reaches nobody on
    exit 0. Fail-open: any error is logged and swallowed."""
    if not text:
        return
    try:
        ax = nudge_axes(name, default_channel)
        if ax.get("model"):
            record_obs()
        if ax.get("user"):
            # leading newline: content drops below CC's "<Event> says:" label line
            queue_system_message("\n" + text)
        # legacy debug echo (relay/stderr sinks) — only when nothing reached the human.
        if not ax.get("user") and (ax.get("model") or ax.get("stderr")):
            sys.stderr.write("[advisory] %s\n" % text)
    except Exception as e:  # noqa: BLE001 — advisory routing must never raise
        log_hook_error(name, e)


def drain_or_continue() -> None:
    """Terminal write for a hand-rolled nudge main(): emit a queued systemMessage
    (from route_relay_nudge / emit_nudge) if any, else a plain continue. Replaces a
    bare emit_continue() in mains that now route through the systemMessage queue."""
    queued = _drain_system_messages()
    if queued:
        emit_system_message(queued)
    else:
        emit_continue()


def emit_nudge(name, text, *, kind=None, session=None, actor=None,
               default_channel="stderr") -> None:
    """Route a nudge advisory `text` on its resolved {model,user} axes WITHOUT writing
    the hook's terminal stdout blob:
      model -> record the `<name>_observation` (the model's channel; needs a `session`
               to record on — an anonymous model-only nudge degrades to a stderr line
               so it isn't silently lost);
      user  -> queue a systemMessage for the human.
    Both are independent (legacy `systemMessage` = both, `relay` = model-only,
    `stderr`/neither = a debug line reaching nobody on exit 0). The observation is the
    ONLY way a nudge reaches the model — a systemMessage never enters model context —
    so it fires whenever the model axis is on. Fail-open: errors are logged, swallowed."""
    if not text:
        return
    try:
        ax = nudge_axes(name, default_channel)
        if ax.get("model") and session:
            _record_nudge_observation(name, text, session=session, actor=actor)
        if ax.get("user"):
            # leading newline: content drops below CC's "<Event> says:" label line
            queue_system_message("\n" + text)
        # stderr leg: the legacy `stderr` sink, OR a model nudge with no session to
        # record on (degrade). Never when the human already saw it via systemMessage.
        if not ax.get("user") and (ax.get("stderr") or (ax.get("model") and not session)):
            sys.stderr.write("[advisory] %s\n" % text)
    except Exception as e:  # noqa: BLE001 — advisory routing must never raise
        log_hook_error(name, e)


# --- per-hook config (YAML, enabled/mode overrides ONLY) ----------------------

_CONFIG_NAME = "harness-hooks.yaml"

# Per-class defaults. The compliance row is the deliberate inversion of the
# source corpus: a gate that ships asleep protects nothing.
_CLASS_DEFAULTS = {
    "telemetry": {"enabled": True, "mode": "advisory"},
    "nudge": {"enabled": False, "mode": "advisory"},
    "compliance": {"enabled": True, "mode": "blocking"},
}

_config_cache = None  # module-level; None = not yet loaded


def _config_path() -> Path:
    # The BASE layer. HARNESS_HOOK_CONFIG (tests, dev tree) replaces it outright —
    # deliberately a replacement, because that is what a test seam is for; the
    # per-project layer below is the one that merges. Otherwise the shipped default
    # lives in harness/data/ (the config home, alongside stage-policy/guard-policy/…),
    # resolved off __file__ (durable, not CWD/env).
    raw = os.environ.get("HARNESS_HOOK_CONFIG")
    return Path(raw) if raw else _hooks_dir().parent / "data" / _CONFIG_NAME


def _overlay_path() -> Path:
    """The per-project layer: one file in THIS project's writeable data home.

    Under a shared-bin install the base is read-only and shared, so a project that
    wants one nudge on would otherwise have to edit the file every other project
    reads. This path is per-project by construction — `data_root()` is the project
    zone — and it is a LAYER, never a replacement (see `hook_config_document`).
    """
    raw = os.environ.get("HARNESS_HOOK_OVERLAY")
    if raw:
        return Path(raw)
    try:
        scripts = str(_hooks_dir().parent / "scripts")
        if scripts not in sys.path:
            sys.path.append(scripts)
        import harness_paths
        return harness_paths.data_root() / _CONFIG_NAME
    except Exception:  # noqa: BLE001 — an unresolvable project home means no layer
        return _hooks_dir().parent / "data" / ".no-project-overlay"


def _read_doc(path: Path) -> dict:
    """One YAML document, or {} for anything unreadable. Never raises."""
    try:
        if not path.is_file():
            return {}
        raw = yaml_load(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as e:  # noqa: BLE001 — malformed config must never crash a hook
        log_hook_error("hook_runtime", e)
        return {}


def _merge_docs(base: dict, over: dict) -> dict:
    """Layer `over` onto `base`: top-level keys replace, `hooks:` merges per NAME,
    and each named entry merges per KEY.

    Replacing instead of merging is the measured defect this function exists for —
    a bound override naming 12 hooks silently took nine shipped-enabled hooks off,
    because a name it did not mention fell through to its class default. A project
    saying "this one nudge, off" must not be read as "and nothing else applies".
    """
    merged = dict(base)
    for key, val in over.items():
        if key != "hooks":
            merged[key] = val
    over_hooks = over.get("hooks")
    if not isinstance(over_hooks, dict):
        return merged
    hooks = {}
    base_hooks = base.get("hooks")
    if isinstance(base_hooks, dict):
        hooks = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base_hooks.items()}
    for name, entry in over_hooks.items():
        cur = hooks.get(name)
        if isinstance(cur, dict) and isinstance(entry, dict):
            cur.update(entry)
        else:
            hooks[name] = dict(entry) if isinstance(entry, dict) else entry
    merged["hooks"] = hooks
    return merged


def hook_config_document() -> dict:
    """The WHOLE resolved config — `hooks:` plus the top-level knobs — after the
    per-project layer is applied. Read this rather than the file: a caller that
    opens `_config_path()` itself sees the shipped answer and misses the project's.
    """
    doc = _resolve_config()[0]
    return doc


def hook_config_overlay_names() -> list:
    """The hook names the project layer changed, sorted. An override nobody can see
    is how the measured defect stayed invisible; this is what lets a report say why
    a hook did not run instead of leaving the reader to guess."""
    names = _resolve_config()[1]
    return names


def force_required_names() -> frozenset:
    """Every hook name the dispatcher runs as fail-closed compliance, by `name` AND
    `module`. The project layer may only loosen one of these with a `force_reason`
    and its record (`_forced`).

    Read from the dispatch registry, not from a list kept here: guard_policy's own
    table spells the protected-branch fence two ways the running module does not,
    and omits the Bash-write floor, so a check keyed on it let both be switched off
    without --force. Parsed here rather than through hook_dispatch, which imports
    this module; a row whose fail_open is anything but `true` counts as fail-closed.
    Raises on an unreadable registry; callers choose the direction.
    """
    raw = yaml_load(dispatch_registry_path().read_text(encoding="utf-8")) or {}
    out: set[str] = set()
    for cores in (raw.get("groups") or {}).values():
        for c in cores or []:
            if (isinstance(c, dict) and c.get("module") and c.get("class") == "compliance"
                    and c.get("fail_open") is not True):
                out.update((str(c.get("name") or c["module"]), str(c["module"])))
    return frozenset(out)


def dispatch_registry_path() -> Path:
    """hook-dispatch.yaml — read here as well as by hook_dispatch, which imports this
    module; resolving the path in one place keeps both readers on one file."""
    raw = os.environ.get("HARNESS_HOOK_DISPATCH_CONFIG")
    return Path(raw) if raw else _hooks_dir().parent / "data" / "hook-dispatch.yaml"


def _loosens(entry) -> bool:
    return isinstance(entry, dict) and (entry.get("enabled") is False
                                        or entry.get("mode") == "advisory")


def _forced(entry) -> bool:
    """A loosening of a fail-closed gate is forced only on the record: a
    `force_reason` AND who, when and why (`off_record.RECORD_KEYS`)."""
    reason = entry.get("force_reason") if isinstance(entry, dict) else None
    if not (isinstance(reason, str) and reason.strip()):
        return False
    try:
        import off_record
    except ImportError:
        sys.path.append(str(_hooks_dir().parent / "scripts"))
        import off_record
    return not off_record.missing(entry)


def _screen_overlay(over: dict) -> list:
    """Strip the loosening keys from any overlay entry that turns a fail-closed gate
    off or advisory without a `force_reason` and its record, in place; return the
    refused names.

    The CLI door asks for --force, but the project file is hand-editable, so the
    rule has to hold where the file is read. An unreadable registry is logged and
    screens every compliance-looking loosening it cannot rule out — i.e. all of them.
    """
    hooks = over.get("hooks")
    if not isinstance(hooks, dict):
        return []
    loosening = [n for n, e in hooks.items() if _loosens(e) and not _forced(e)]
    if not loosening:
        return []
    try:
        guarded = force_required_names()
    except Exception as e:  # noqa: BLE001
        log_hook_error("hook_runtime", e)
        guarded = None
    refused = []
    for name in loosening:
        if guarded is not None and name not in guarded:
            continue
        entry = dict(hooks[name])
        if entry.get("enabled") is False:
            entry.pop("enabled")
        if entry.get("mode") == "advisory":
            entry.pop("mode")
        hooks[name] = entry
        refused.append(name)
    return sorted(refused)


def _resolve_config():
    """(merged document, overlay names, base hooks, screened overlay hooks, refused)
    — parsed once per process."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    base = _read_doc(_config_path())
    over = _read_doc(_overlay_path())
    names = sorted(over["hooks"]) if isinstance(over.get("hooks"), dict) else []
    refused = _screen_overlay(over) if names else []
    base_hooks = base.get("hooks") if isinstance(base.get("hooks"), dict) else {}
    over_hooks = over.get("hooks") if isinstance(over.get("hooks"), dict) else {}
    _config_cache = (_merge_docs(base, over), names, base_hooks, over_hooks, refused)
    return _config_cache


def hook_config_refused_names() -> list:
    """Overlay entries that tried to loosen a fail-closed gate with no force_reason or
    no record of who, when and why, and were ignored. Reported, never silently dropped."""
    return list(_resolve_config()[4])


def _load_config() -> dict:
    """The `hooks:` block of the resolved document. Malformed/unreadable/missing-PyYAML
    ⇒ {} (every hook then falls to its per-class default) + a crash-log line.
    The lazy yaml import keeps telemetry/nudge importable without PyYAML."""
    hooks = _resolve_config()[0].get("hooks")
    return hooks if isinstance(hooks, dict) else {}


def _reset_config_cache() -> None:
    """Test seam: drop the per-process config cache so a fresh file is re-read."""
    global _config_cache
    _config_cache = None


def _telemetry_globally_disabled() -> bool:
    return bool(os.environ.get("HARNESS_TELEMETRY_DISABLED"))


def _hook_entry(name: str) -> dict:
    entry = _load_config().get(name)
    return entry if isinstance(entry, dict) else {}


def _guard_policy_mode(name: str):
    """The unified posture for a REGISTERED guard ('off'|'warn'|'block'), or
    None if `name` is not a registered guard or the posture engine is
    unavailable. Lazy + defensive: guard_policy lives in ../scripts and imports
    trace_log (which imports this module), so importing it at call time avoids a
    cycle, and any failure degrades to None so the caller falls to its class
    default -- the posture bridge must never break a hook."""
    try:
        import guard_policy  # lazy: avoid an import cycle
    except Exception:  # noqa: BLE001
        try:
            sys.path.append(str(_hooks_dir().parent / "scripts"))
            import guard_policy
        except Exception:  # noqa: BLE001
            return None
    if name not in guard_policy.GUARD_REGISTRY:
        return None
    try:
        return guard_policy.resolve_mode(name)
    except Exception:  # noqa: BLE001 -- malformed policy must not crash a hook
        return None


def _class_defaults(hook_class: str, name: str = "?") -> dict:
    """The defaults for ``hook_class``, or raise on a class that is not one of
    the three.

    ── WHY THIS RAISES (this used to be a .get(..., _CLASS_DEFAULTS["nudge"])) ──
    An unrecognised class fell back to `nudge`, and nudge defaults to DISABLED.
    So a class name that was merely MISSPELT in the dispatch registry switched
    the hook off -- and because hook_dispatch skips a disabled hook by its
    REGISTRY class, the compliance-only `_skip_trace_once` audit line did not run
    either. The gate vanished with no verdict, no stderr and no trace.

    Measured on the live tree (2026-07-29, ship config, 15 compliance cores):
    a mistyped class disabled exactly 3 of them and left 12 standing. The
    survivors were not luckier -- each is named in guard-policy.yaml's
    GUARD_REGISTRY or in harness-hooks.yaml, so a second config plane answered
    the enabled question before the class default was ever consulted. The three
    that leant on the class default alone were disabled_skill_router,
    floor_bash_guard and protected_ref_guard: the skill cage, the Bash-write
    floor and the protected-branch fence.

    A caller passing a class this module does not know is a BUG in that caller,
    not a state to degrade through. Every hook passes its own HOOK_CLASS literal;
    the only config-derived callers (hook_dispatch, hs_cli) both funnel an
    exception into a fail-closed exit, and hook_dispatch.load_registry now
    rejects a bad class before it can reach here at all -- this is the backstop
    for the standalone path, not the primary check.
    """
    try:
        return _CLASS_DEFAULTS[hook_class]
    except (KeyError, TypeError):
        raise ValueError(
            "unknown hook class %r for hook %s -- must be one of %s."
            % (hook_class, name, ", ".join(sorted(_CLASS_DEFAULTS)))) from None


def hook_enabled(name: str, hook_class: str) -> bool:
    """Is hook ``name`` of ``hook_class`` enabled?

    Precedence: an explicit bool `enabled` in config wins (back-compat); else
    the unified guard policy when `name` is a registered guard (off => off);
    else the class default. The HARNESS_TELEMETRY_DISABLED kill-switch forces
    telemetry OFF and has no effect on nudge/compliance. `hook_class` comes from
    the hook's own HOOK_CLASS constant -- config cannot reclassify a hook.

    Raises ValueError on a class outside {telemetry, nudge, compliance}; see
    _class_defaults for why that is not degraded to a default.
    """
    return _enabled_decision(name, hook_class)[0]


def hook_enabled_source(name: str, hook_class: str) -> str:
    """Which source answered `hook_enabled` — the question "who turned this off".

    One of: env:HARNESS_TELEMETRY_DISABLED · project · shipped ·
    env:HARNESS_HOOK_CONFIG (the base was replaced) · guard-policy · class-default.
    Computed by the same walk as the answer, so the two cannot disagree.
    """
    return _enabled_decision(name, hook_class)[1]


def _enabled_decision(name: str, hook_class: str):
    defaults = _class_defaults(hook_class, name)
    if hook_class == "telemetry" and _telemetry_globally_disabled():
        return False, "env:HARNESS_TELEMETRY_DISABLED"
    _, _, base_hooks, over_hooks, _ = _resolve_config()
    over = over_hooks.get(name)
    if isinstance(over, dict) and isinstance(over.get("enabled"), bool):
        return over["enabled"], "project"
    base = base_hooks.get(name)
    if isinstance(base, dict) and isinstance(base.get("enabled"), bool):
        return base["enabled"], ("env:HARNESS_HOOK_CONFIG"
                                 if os.environ.get("HARNESS_HOOK_CONFIG") else "shipped")
    gmode = _guard_policy_mode(name)
    if gmode is not None:
        return gmode != "off", "guard-policy"
    return defaults["enabled"], "class-default"


def hook_mode(name: str, hook_class: str) -> str:
    """Enforcement mode for an ENABLED hook: 'blocking' | 'advisory'.

    telemetry/nudge: always advisory; config cannot escalate them to blocking.
    compliance precedence: an explicit `mode` of 'advisory'/'blocking' in config
    wins (back-compat); else the unified guard policy when `name` is registered
    (warn => advisory, block => blocking); else blocking -- the safe default for
    a gate is to gate.

    Raises ValueError on an unknown class, for the same reason hook_enabled does
    (see _class_defaults). The shape here was the sibling of that bug: the test
    below is `!= "compliance"`, so EVERY unrecognised class -- including a
    misspelt "compliance" -- was answered 'advisory', i.e. a gate that no longer
    gates. Validating first is what makes the two answers agree about which
    classes exist.
    """
    return _mode_decision(name, hook_class)[0]


def hook_mode_source(name: str, hook_class: str) -> str:
    """Which source answered `hook_mode` — the sibling of `hook_enabled_source`, so a
    gate made advisory names the layer that did it rather than the one that enabled it.

    One of: class · project · shipped · env:HARNESS_HOOK_CONFIG · guard-policy ·
    class-default. Computed by the same walk as the answer.
    """
    return _mode_decision(name, hook_class)[1]


def _mode_decision(name: str, hook_class: str):
    _class_defaults(hook_class, name)
    if hook_class != "compliance":
        return "advisory", "class"
    explicit = _hook_entry(name).get("mode")
    if explicit in ("advisory", "blocking"):
        over = _resolve_config()[3].get(name)
        if isinstance(over, dict) and "mode" in over:
            return explicit, "project"
        return explicit, ("env:HARNESS_HOOK_CONFIG"
                          if os.environ.get("HARNESS_HOOK_CONFIG") else "shipped")
    gmode = _guard_policy_mode(name)
    if gmode is not None:
        return ("advisory" if gmode == "warn" else "blocking"), "guard-policy"
    return "blocking", "class-default"


# --- actor resolution -------------------------------------------

# Sentinel rather than "" so an EMPTY answer counts as answered: "git has no email
# configured" is a real result, and re-spawning git on every call to rediscover it
# is most of the cost this removes (1.9-6.2 ms per spawn against 0.148 ms warm).
_IDENTITY_UNSET = object()
_git_email_cache = _IDENTITY_UNSET


def _reset_identity_cache() -> None:
    """Drop the memoised git identity. For tests — a hook process is short-lived
    and has no reason to call this."""
    global _git_email_cache
    _git_email_cache = _IDENTITY_UNSET


def _git_user_email() -> str:
    """The git-configured email, memoised for the life of the process.

    Only THIS lookup is cached, never the assembled actor string: the CI markers,
    HARNESS_USER, the per-session cache and the HARNESS_AGENT suffix are all
    re-read by resolve_actor on every call, and freezing the whole string would
    pin a value a caller is entitled to change mid-process (an agent suffix being
    the obvious one)."""
    global _git_email_cache
    if _git_email_cache is not _IDENTITY_UNSET:
        # Narrowed by the sentinel check above: the only two values this global
        # ever holds are _IDENTITY_UNSET (an `object()`) or a `str` written below.
        return cast(str, _git_email_cache)
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        value = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        value = ""
    _git_email_cache = value
    return value


def _state_dir() -> Path:
    # One door for hook-class state: harness_paths.hook_state_dir() —
    # HARNESS_STATE_DIR > the shared state_dir() tiers (so a bare self-host
    # hook lands in <host>/.harness/state like every other project state,
    # per the one-tree ruling) > the bin tree as fail-open
    # fallback. hook_runtime and hlog each hand-mirrored these tiers before
    # and drifted apart — the hook mirror never consulted HARNESS_DATA_ROOT
    # when self-hosted while hlog's did, so hooks and loggers wrote different
    # trees on the same day. Fail-open: any resolution failure falls back to
    # the bin-side dir next to this module rather than crashing the hook.
    try:
        scripts = str(_hooks_dir().parent / "scripts")
        if scripts not in sys.path:
            sys.path.append(scripts)
        import harness_paths
        return harness_paths.hook_state_dir()
    except Exception:
        return _hooks_dir().parent / "state"


def _safe_session_id(session_id: str) -> str:
    """Sanitize a session id for use as a single path component. A session id is
    normally a UUID from the host, but it is never trusted into a path: any char
    outside [A-Za-z0-9_-] is collapsed to '_' so it cannot traverse directories."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in (session_id or "_"))



safe_session_id = _safe_session_id  # public name for the shared sanitizer


def shard_discriminator(session) -> "str | None":
    """A per-session filename discriminator, or None when there is no real
    session. SSOT for the trace/telemetry shard name: N worktrees pointed at one
    host state store shard their writes by session so concurrent sessions
    stop contending on a single daily file. NO pid fallback (R2) — a sessionless
    writer keeps the shared legacy file, which is safe because a single JSONL
    line is < PIPE_BUF (4096B) so O_APPEND stays atomic; a pid shard would sprawl
    and starve rotation. Sanitized to [A-Za-z0-9_-], capped at 12 chars."""
    if not session:
        return None
    disc = _safe_session_id(session)[:12]
    # _safe_session_id collapses a garbage id to all-'_'; an all-'_' disc is not
    # a real session — keep it on the shared legacy file rather than a junk shard.
    if not disc.strip("_"):
        return None
    return disc


def project_dir(stdin_cwd=None):
    """Resolve the project root the hooks scan. CLAUDE_PROJECT_DIR (set by the
    host) wins; the Stop/PostToolUse stdin `cwd` is the fallback. Returns None
    when neither is usable (empty string is treated as absent)."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or stdin_cwd or None

def subagent_transcript(data):
    """A SubagentStop payload's own subagent transcript path, or None. The host's
    `agent_transcript_path` wins; else <main transcript minus extension>/subagents/
    agent-<agent_id>.jsonl. Never the main transcript."""
    direct = data.get("agent_transcript_path")
    if direct:
        return str(direct)
    agent_id = data.get("agent_id") or data.get("agentId") or data.get("subagent_id")
    main_tp = data.get("transcript_path")
    if agent_id and main_tp:
        return os.path.join(os.path.splitext(str(main_tp))[0], "subagents", "agent-%s.jsonl" % agent_id)
    return None

def resolve_actor(session_id=None) -> str:
    """Resolve the acting identity. Attribution, NOT authentication —
    env-derived, spoofable, never an authz signal.

    Order: CI marker → session file cache (optional — a hook must work when
    session_init never ran) → HARNESS_USER → git config user.email →
    $USER. Agent suffix from HARNESS_AGENT. Format: user:<u>[/agent:<a>] | ci.
    """
    if os.environ.get("CI") or os.environ.get("GITLAB_CI") or os.environ.get("GITHUB_ACTIONS"):
        return "ci"

    if session_id:
        try:
            p = _state_dir() / "sessions" / ("%s.json" % _safe_session_id(session_id))
            if p.is_file():
                cached = json.loads(p.read_text(encoding="utf-8")).get("actor")
                if cached:
                    return str(cached)
        except Exception:
            pass  # cache miss/corrupt → fall through to env chain

    user = (
        os.environ.get("HARNESS_USER")
        or _git_user_email()
        or os.environ.get("USER", "unknown")
    )
    actor = "user:%s" % user
    agent = os.environ.get("HARNESS_AGENT")
    if agent:
        actor += "/agent:%s" % agent
    return actor


# --- telemetry convenience wrapper (ported PS) ---------------------------------

_DEFAULT_CORE_TIMEOUT_S = 5.0


def run_core_isolated(core, data, timeout=None) -> dict:
    """Run ``core(data)`` in a daemon worker thread, observed from the main thread
    with ``join(timeout)``. Thin, posture-blind (the CALLER maps the outcome to a
    fail-open skip or a fail-closed block by the core's HOOK_CLASS). Returns:

      {"status": "ok", "value": <core return>}   core returned within the budget
      {"status": "error", "error": <exception>}  core raised
      {"status": "timeout"}                       core still running past the budget

    Portable by construction — a daemon thread + join, NOT signal.setitimer (which
    is Unix-only and would break the declared Windows target). Critically, a timeout
    does NOT inject an exception into the worker: the main thread simply observes the
    worker still alive, so a gate whose own body carries `except Exception: return
    None` cannot swallow the timeout signal (the F3 hole — VL-4/C1). The worker is a
    daemon, so a hung core dies with the process; its result box is per-call, never a
    shared global (VL-1 §9 — no cross-core leak).

    The worker is tagged with the generation current AT THE MOMENT this call
    starts it (see queue_system_message/_drain_system_messages module notes) —
    fixed once, at thread-start, never re-read live: if this core times out and
    keeps running, the main thread will advance the generation for whatever
    core runs next, but this worker's tag stays pinned to its OWN (by then
    stale) generation, so a queue_system_message() call it makes later is
    dropped rather than credited to that later core. Never raises."""
    box = {}
    gen = _current_generation()

    def _work():
        _generation_local.generation = gen
        try:
            box["value"] = core(data)
            box["status"] = "ok"
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as status=error
            box["error"] = e
            box["status"] = "error"

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(_DEFAULT_CORE_TIMEOUT_S if timeout is None else timeout)
    if t.is_alive():
        return {"status": "timeout"}
    return box if box.get("status") else {"status": "error",
                                          "error": RuntimeError("core produced no result")}


def run_telemetry_hook(name, core, raw=None) -> None:
    """Skeleton for telemetry hooks: read stdin JSON (or ``raw``), check
    enabled; if disabled, emit continue WITHOUT running core. Core runs inside
    a fail-open guard routing exceptions to the crash log. ALWAYS continues."""
    data = read_stdin_json() if raw is None else _parse(raw)
    try:
        if hook_enabled(name, "telemetry"):
            core(data)
    except Exception as e:  # noqa: BLE001 — telemetry must never break the op
        log_hook_error(name, e)
    emit_continue()


# --- nudge wrapper -------------------------------------------------------------

def run_nudge_hook(name, core, raw=None, default_channel="stderr") -> None:
    """Skeleton for nudge hooks (default OFF): core(data) may return a message
    string → routed to its configured sink (relay/systemMessage/stderr/off) via
    emit_nudge, NOT hard-wired to stderr any more (INV-3 F-2). Always exits 0 /
    continues. The systemMessage sink queues its line; the terminal write drains
    it into the hook's single stdout blob (no double-write)."""
    data = read_stdin_json() if raw is None else _parse(raw)
    try:
        if hook_enabled(name, "nudge"):
            msg = core(data)
            if msg:
                session = data.get("session_id") if isinstance(data, dict) else None
                emit_nudge(name, msg, session=session, default_channel=default_channel)
    except Exception as e:  # noqa: BLE001 — a nudge must never break the op
        log_hook_error(name, e)
    queued = _drain_system_messages()
    if queued:
        emit_system_message(queued)
    else:
        emit_continue()


# --- compliance wrapper — fail-CLOSED, its own top-level guard ----------

def run_compliance_hook(name, core, raw=None, data=None) -> None:
    """Top-level wrapper for compliance hooks. NOT built on the telemetry
    skeleton: that one is fail-open by contract, a gate must be fail-closed.

    core(data) contract: return None ⇒ pass; return a string ⇒ block reason.
    EVERY exception raised by core — including ImportError from a machine
    that skipped preflight and config trouble — lands in the except arm and
    blocks with exit 2 + an actionable reason. In `mode: advisory` (explicit
    opt-in) the reason is warned to stderr and the op continues. Disabled
    (explicit `enabled: false`) ⇒ skip core, exit 0.

    Deliberate fail-open edge: empty or unparseable STDIN becomes {} (see
    read_stdin_json), so core sees no command and passes. Blocking every
    Bash call whenever the transport hiccups would be a denial-of-service on
    the whole session; a payload that cannot be parsed also yields no
    command to gate. The gate therefore fails closed on ITS OWN errors, and
    open on absent input.

    This function never raises and never exits 0 on a broken enabled gate.
    """
    try:
        if data is None:
            data = read_stdin_json() if raw is None else _parse(raw)

        if not hook_enabled(name, "compliance"):
            emit_continue()
            sys.exit(0)

        reason = core(data)
        if reason:
            if hook_mode(name, "compliance") == "advisory":
                sys.stderr.write("[advisory] %s: %s\n" % (name, reason))
                emit_continue()
                sys.exit(0)
            sys.stderr.write("[%s] BLOCKED: %s\n" % (name, reason))
            sys.exit(2)

        # A soft/advisory core() may have QUEUED a systemMessage line (e.g.
        # gate_stage's soft-stage / receipt-gap advisories, H2-resolved) instead
        # of writing stderr directly -- drain it into this SAME terminal write so
        # the hook's stdout stays one JSON blob. Every other compliance hook
        # never calls queue_system_message(), so the queue is always empty for
        # them and this is byte-identical to the old plain emit_continue().
        queued = _drain_system_messages()
        updated_input = _drain_updated_input()
        if updated_input is not None:
            _emit_allow_with_updated_input(updated_input, queued)
        elif queued:
            emit_system_message(queued)
        else:
            emit_continue()
        sys.exit(0)
    except SystemExit:
        raise
    except ImportError as e:
        # Missing dependency — point at the fix, then fail closed.
        log_hook_error(name, e)
        sys.stderr.write(
            "[%s] BLOCKED: dependency missing (%s).\n"
            "Run: python3 harness/scripts/preflight_deps.py\n"
            "or:  pip install 'ruamel.yaml' 'ruamel.yaml.clib' pytest\n" % (name, e)
        )
        sys.exit(2)
    except Exception as e:  # noqa: BLE001 — fail CLOSED, with audit trail
        log_hook_error(name, e)
        sys.stderr.write(
            "[%s] BLOCKED: gate crashed (%s: %s). Fail-closed by policy; "
            "see hook-crashes.log. To bypass IN AN EMERGENCY set "
            "`enabled: false` for this hook in harness/data/harness-hooks.yaml — the "
            "change is tracked in git and traced.\n" % (name, type(e).__name__, e)
        )
        sys.exit(2)


# ── the closed vocabulary a fail-open hook may give for doing nothing ──────────
#
# A fail-open hook that runs, gives up and returns leaves no artifact, so "ran and
# could not proceed" and "never fired" are the same observation afterwards. These
# codes make the first one sayable.
#
# CLOSED on purpose. The trace is read by `hs-cli`, by whoever is debugging a hook
# that went quiet, and by anyone the trace file is shared with — so a reason may
# never carry a session id, a path, a branch name, or anything out of the payload.
# A caller that invents one lands on "other" (see normalize_skip_reason); the
# static half of that promise is test_a_fail_open_hook_never_exits_without_a_reason.
#
# NOT in here, deliberately: "this payload was not my subject". A PostToolUse nudge
# sees every tool call and declines nearly all of them; tracing that would bury the
# reasons above under its own volume — which is how a reader learns to skip the
# trace, and then it is unread on the run that mattered. Those exits declare
# themselves in the source with `# skip:not-applicable` instead.
SKIP_REASONS = frozenset({
    "disabled",             # the hook's own enable check said no
    "config-unreadable",    # its config exists but could not be loaded
    "root-unresolvable",    # no project/repo root could be established
    "identity-unresolvable",  # no session/agent id to key this run on
    "state-unreadable",     # its own state store could not be read
    "state-unwritable",     # ... or written
    "dependency-missing",   # an import, binary or sibling module was not there
    "subject-missing",      # the artifact it watches does not exist
    "subject-unchanged",    # it exists and is byte-identical to the last look
    "subject-unparseable",  # it exists and could not be read as what it claims
    "probe-failed",         # a checker/subprocess ran and returned nothing usable
    "already-reported",     # said or done once this session; the marker is set
    "backed-off",           # a breaker tripped: it failed too often to keep trying
    "other",                # a cause with no code yet — deliberately the last resort
})


def normalize_skip_reason(reason) -> str:
    """Map ``reason`` onto SKIP_REASONS, collapsing anything unlisted to "other".

    Collapsing rather than raising, because this runs on the fail-open path: a
    hook must not die over how it named its own silence. Collapsing rather than
    passing through, because an un-mapped string is exactly where a path or a
    session id would leak into a record other people read."""
    return reason if reason in SKIP_REASONS else "other"


def trace_skip(name, reason, *, session=None, actor=None) -> None:
    """Record ONCE per (session, hook, reason) that this fail-open hook ran and
    did nothing, and why.

    Once-per-reason, not once-per-call: a hook on a PostToolUse lane fires on
    every tool call, and the question this answers ("did it ever run, and what
    stopped it") is answered by the first record. A hook that later gives up for
    a DIFFERENT reason still gets its own line.

    Fail-open by class, like everything else here: any error is swallowed into
    the crash log rather than reaching the caller."""
    code = normalize_skip_reason(reason)
    try:
        if _skip_already_traced(session, "%s.%s" % (name, code)):
            return
        import trace_log
        trace_log.append_event(hook=name, event="%s_skip" % name, session=session,
                               actor=actor, status="skip", note=code)
        _mark_skip_traced(session, "%s.%s" % (name, code))
    except Exception as e:  # noqa: BLE001 — the skip trace is telemetry, never blocks
        log_hook_error(name, e)


def _skip_marker_path(session_id, name) -> Path:
    """Per-(session, hook) marker recording that this session already traced a skip
    for this gate."""
    return _state_dir() / "skip-marks" / ("%s.%s" % (_safe_session_id(session_id), name))


def _skip_already_traced(session_id, name) -> bool:
    """True if this (session, hook) already recorded a skip this session. Fail-open:
    no session id, or any stat error, yields False — degrade to recording again (per
    call) rather than suppressing the audit entirely, and never raise."""
    if not session_id:
        return False
    try:
        return _skip_marker_path(session_id, name).is_file()
    except OSError:
        return False


def _mark_skip_traced(session_id, name) -> None:
    """Record that this (session, hook) traced its skip. Best-effort; an error just
    means the next call re-records (degrade to per-call, never a raise)."""
    if not session_id:
        return
    try:
        p = _skip_marker_path(session_id, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    except OSError:
        pass


def compliance_skip_or_run(name, core, *, skip_event, skip_note=None) -> None:
    """Compliance entry shared by the enforcement guards: read stdin once, and if
    the gate is disabled record a VISIBLE `<skip_event>` trace (run_compliance_hook
    exits silently on disabled, but a skipped gate DECISION must be auditable) then
    continue; otherwise delegate to run_compliance_hook with the already-parsed
    payload (no parse->serialize->parse round-trip).

    The skip trace is written ONCE per (session, hook): a marker keyed by the session
    id and gate name suppresses the repeat lines a wide always-disabled matcher would
    otherwise emit on every tool call. One line per session (plus the config git diff)
    still proves the skip decision. The trace write and the marker are both fail-open —
    a broken trace or an unwritable marker degrades to re-recording, never blocks the
    skip, and posture is unchanged (continue + exit 0)."""
    raw = read_stdin_json()
    if not hook_enabled(name, "compliance"):
        sid = raw.get("session_id")
        if not _skip_already_traced(sid, name):
            try:
                import trace_log
                trace_log.append_event(
                    hook=name, event=skip_event, session=sid,
                    note=skip_note if skip_note is not None
                    else "disabled via %s" % _config_path())
            except Exception as e:  # noqa: BLE001 — trace is telemetry, never blocks
                log_hook_error(name, e)
            _mark_skip_traced(sid, name)
        emit_continue()
        sys.exit(0)
    run_compliance_hook(name, core, data=raw)
