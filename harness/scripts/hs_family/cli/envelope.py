"""The family's one output shape: ONE JSON line on stdout for every exit path — a result,
an error, `--help`, `--list`, `--version`, a crash. `--text` renders the same envelope for a
human through `render_text`, the one text printer (`hs-cli render` uses it too, and hs-run's
step envelopes print through it)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

SCHEMA = "hs-envelope/1"
RESERVED = ("schema", "cli", "command", "exit_code", "state", "warnings", "error", "output", "data")


def build(*, cli: str, command: Iterable[str], exit_code: int, state: str,
          warnings: Iterable[str] = (), data: Optional[Mapping[str, Any]] = None,
          error: Optional[str] = None, output: str = "",
          extra: Optional[Mapping[str, Any]] = None) -> dict:
    env: dict = {"schema": SCHEMA, "cli": cli, "command": list(command),
                 "exit_code": int(exit_code), "state": state, "warnings": list(warnings)}
    if error is not None:
        env["error"] = error
    if output:
        env["output"] = output
    if data is not None:
        env["data"] = dict(data)
    for key, value in (extra or {}).items():
        if key in RESERVED:
            raise ValueError("envelope key %r is reserved for the family shape" % key)
        env[key] = value
    return env


def _default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if hasattr(obj, "__attrs_attrs__"):
        import attrs
        return attrs.asdict(obj)
    return str(obj)


def dumps(env: Mapping[str, Any]) -> str:
    return json.dumps(env, ensure_ascii=False, default=_default)


_HELP_WIDTH = 64


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _HELP_WIDTH else text[: _HELP_WIDTH - 1] + "…"


def _command_lines(commands: Iterable[Mapping[str, Any]], clip: bool = True) -> list:
    rows = [(c["name"], (_clip if clip else str)(c.get("help") or ""), c.get("label") or "")
            for c in commands]
    w = max((len(n) for n, _h, _l in rows), default=0)
    hw = max((len(h) for _n, h, _l in rows), default=0)
    return [("  %-*s  %-*s  %s" % (w, n, hw, h, lab)).rstrip() for n, h, lab in rows]


def _value(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=_default)


# A skipped row used to render identically to a passed one: the mark reads
# `done`, and a gate that never applied is marked done because there was nothing
# left to do. So the one channel a person actually looks at reported "the grid
# leg passed" for a plan that never ran a grid.
#
# Three marks, because there are three states and a reader given two will map the
# third onto whichever it resembles.
_MARK_BY_KIND = {"flag-waived": "~", "not-applicable": "-"}
_NOTE_BY_KIND = {
    "flag-waived": "  (waived by flag)",
    "not-applicable": "  (n/a — gate does not apply to this plan)",
}


def checklist_mark(item: Mapping[str, Any]) -> str:
    """`x` ran and passed · `~` somebody waived it · `-` it never applied."""
    kind = item.get("skip_kind")
    if kind:
        return _MARK_BY_KIND.get(kind, "?")
    return "x" if item.get("done") else " "


def checklist_note(item: Mapping[str, Any]) -> str:
    """Why the row did not run, in words. The mark alone is a legend nobody has."""
    kind = item.get("skip_kind")
    return _NOTE_BY_KIND.get(kind, "") if kind else ""


def render_step_text(envelope: Mapping[str, Any]) -> str:
    """An hs-run step envelope for a human, byte-stable so a script piping it can
    parse it unchanged: `hs-run --text`, `hs-cli render` and its plain fallback when
    stdout is not a tty or rich is missing all print these lines, so there is ONE
    plain-text format to keep correct rather than several that drift apart."""
    lines = []
    lines.append("state: %s" % envelope.get("state", ""))
    lines.append("next_action: %s" % envelope.get("next_action", ""))

    numbers = envelope.get("summary_numbers")
    if numbers:
        lines.append("summary_numbers: %s" % numbers)

    # Rule (d): the step-routing fields come BEFORE artifacts, because they are what
    # the reader is meant to act on first. Shown only when non-empty -- unlike
    # `warnings`, an empty list here is the normal case for a step the table does not
    # route, and a "(none)" line on every call would be noise the JSON already
    # self-declares. Which fields belong here is not this module's decision: it is
    # declared by `hs_run.Envelope`'s `render` metadata and enforced against every
    # human render path by test_hs_run_envelope_contract.py.
    for key in ("rules", "references"):
        values = envelope.get(key) or []
        if values:
            lines.append("%s (%d):" % (key, len(values)))
            for value in values:
                lines.append("  - %s" % value)

    advisory = envelope.get("advisory")
    if advisory:
        lines.append("advisory: %s" % advisory)

    # The shared "a person must decide" block. Rendered as its three named parts
    # when the envelope carries the real shape; a non-dict value (only ever seen
    # in a contract-conformance test's sentinel fixture) falls back to a bare
    # line rather than crashing the renderer on a shape it does not recognise.
    ask_user = envelope.get("ask_user")
    if ask_user:
        if isinstance(ask_user, dict):
            lines.append("ask_user:")
            lines.append("  question: %s" % ask_user.get("question", ""))
            facts = ask_user.get("facts") or []
            if facts:
                lines.append("  facts:")
                for fact in facts:
                    lines.append("    - %s" % fact)
            options = ask_user.get("options") or []
            if options:
                lines.append("  options:")
                for option in options:
                    lines.append("    - %s" % option)
        else:
            lines.append("ask_user: %s" % ask_user)

    # The road ahead. Shown only when non-empty, like the routing fields above: an
    # empty `pending` on a finished plan is the normal case, and the reader is being
    # told what remains, not reassured that nothing does.
    pending = envelope.get("pending") or []
    if pending:
        lines.append("pending (%d): %s" % (len(pending), ", ".join(str(p) for p in pending)))

    artifacts = envelope.get("artifacts") or []
    lines.append("artifacts: (none)" if not artifacts else "artifacts:")
    for artifact in artifacts:
        lines.append("  - %s" % artifact)

    checklist = envelope.get("checklist")
    if checklist:
        lines.append("checklist:")
        for item in checklist:
            lines.append("  [%s] %s%s" % (checklist_mark(item), item.get("step", "?"),
                                          checklist_note(item)))
            evidence = item.get("evidence")
            if evidence:
                lines.append("      evidence: %s" % evidence)
            for reason in item.get("reasons") or []:
                lines.append("      reason: %s" % reason)

    skipped = envelope.get("skipped")
    if skipped:
        lines.append("skipped: %s" % ", ".join(skipped))

    next_command = envelope.get("next_command")
    if next_command:
        lines.append("next_command: %s" % next_command)

    # `_apply_facts`'s own fill. Dict shape (the real one) renders per fact id;
    # a non-dict value (only ever a contract-conformance sentinel) falls back to
    # a bare line, same shape `ask_user` already uses for the same reason.
    facts = envelope.get("facts")
    if facts:
        if isinstance(facts, dict):
            lines.append("facts:")
            for fact_id, fact in facts.items():
                lines.append("  %s: %s" % (fact_id, fact))
        else:
            lines.append("facts: %s" % facts)

    # Rule (c): every warning, verbatim, unfolded. No `[:N]` slice, no
    # summary-only fallback, no "show more" gate. The count line states the
    # true total so a reader can tell nothing was silently dropped.
    warnings = envelope.get("warnings") or []
    lines.append("warnings: (none)" if not warnings else "warnings (%d):" % len(warnings))
    for warning in warnings:
        lines.append("  - %s" % warning)

    root = envelope.get("root")
    if root:
        lines.append("root: %s (%s)" % (root, envelope.get("root_source", "?")))

    return "\n".join(lines)


def render_text(env: Mapping[str, Any]) -> str:
    """The family's one text printer. An hs-run step envelope (it carries
    `next_action`) prints as a step; every other envelope by its family shape."""
    if "next_action" in env:
        return render_step_text(env)
    data = env.get("data") or {}
    lines: list = []
    if env.get("error") is not None:
        lines.append("%s: %s" % (env.get("cli") or "error", env["error"]))
    elif env.get("state") == "help":
        if data.get("usage"):
            lines.append(str(data["usage"]).rstrip())
        if data.get("commands"):
            lines += ["", "commands:"] + _command_lines(data["commands"])
        if data.get("shell_flags"):
            lines += ["", "shell flags:"] + _command_lines(data["shell_flags"], clip=False)
        if data.get("footer"):
            lines += [""] + list(data["footer"])
    elif env.get("state") == "list":
        lines += [str(c) for c in data.get("commands", ())]
    elif env.get("state") == "version":
        lines.append(str(data.get("version", "")))
    else:
        if env.get("output"):
            lines.append(str(env["output"]).rstrip("\n"))
        lines += ["%s: %s" % (k, _value(v)) for k, v in data.items()]
    lines += ["warning: %s" % w for w in env.get("warnings") or ()]
    return "\n".join(lines)


__all__ = ["RESERVED", "SCHEMA", "build", "checklist_mark", "checklist_note", "dumps",
           "render_step_text", "render_text"]
