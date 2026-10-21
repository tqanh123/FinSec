#!/usr/bin/env python3
"""hs_cli_render.py — the DISPLAY layer for one `hs-run` result.

Renders ONE already-parsed hs-run envelope (the dict decoded from its JSON
stdout line) and stops. This is deliberate, not an omission — see the harness
decision register's own section B.MINUS-6 before adding anything that looks
like a "run next step" button or a driver loop.

hs-run's exit ladder is 0 done / 10 a MODEL must judge / 11 one more
deterministic step / 2 broken. A UI convenience button that runs "the next
step" for both 10 and 11 collapses the whole judgment layer into a click —
10 specifically means a human/model call is required, and the `record`
verbs (the ones that persist a decision) are themselves exit-11 while still
carrying judgment, so "only automate 11" is not a safe simplification
either. Display-only sidesteps the hazard by construction rather than by
policy: this module never executes anything, so it structurally cannot
violate either rule.

Three rules, enforced (not merely documented) by test_hs_cli_render.py:
  (a) never auto-advance on exit code 10
  (b) never auto-run a `record` verb, at any exit code
  (c) every element of `warnings` renders verbatim and unfolded — never
      truncated, summarised, or collapsed behind "show more"

The plain text is the family's one printer, `hs_family.cli.envelope.render_text`
— the same lines `hs-run --text` prints; this module adds the rich tty view.

(a) and (b) hold structurally: this module imports no shell-out or dynamic-
dispatch mechanism (subprocess / os.system / os.popen / importlib
dispatch — nothing that could re-invoke hs-run to "advance"), and it
imports no hs_run domain module directly either (nothing that could call a
`record` verb's entry function in-process, bypassing the shell-out route
entirely). (c) is behavioural: the warnings loops (here and in the family
printer) have no `[:N]` slice, no truncation branch, no summary-only fallback.

Callers pass an ALREADY-PARSED dict — this module never shells out to
hs_run.py itself, which is what keeps it trivially testable without a
subprocess in the loop and is what makes it "display-only" rather than a
thin wrapper that still drives execution.

rich renders the tty path; every code path below has a plain-text fallback
that a script piping this output can parse byte-stably — that fallback runs
when stdout is not a terminal OR rich is not installed, never a hard
failure either way. rich is imported LAZILY inside `_rich_bundle()`, never
at module top level: measured cold-import cost is ~35.8ms in this
environment, and hs-run's own hot path (`plan next` / `cook next`) never
touches this file, so nothing pays that cost unless a human is actually
looking at a terminal.

`hs-cli render` is its entry point; this module is the display library behind
it, not a command of its own.
"""
from __future__ import annotations

import sys

from hs_family.cli.envelope import checklist_mark, checklist_note, render_text


# ------------------------------------------------------------------ tty gate


def _stream_is_tty(stream) -> bool:
    """False on any doubt (no `isatty` attribute, or `isatty()` itself
    raising) — the safe default is always the byte-stable plain-text path,
    never a rich render nobody asked for."""
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _rich_bundle():
    """The ONLY place this module imports rich, and only ever called after
    the tty gate above has already passed. A separate function (rather than
    inlining the import) so tests can monkeypatch this one seam to simulate
    rich being entirely absent, without needing to uninstall the real
    package."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    return Console, Panel, Table, Text


# --------------------------------------------------------------------- rich


def _render_rich(envelope: dict, stream, bundle) -> None:
    """tty rendering. Every value that came from the envelope (not a literal
    label this module wrote itself) is wrapped in `Text(...)`, never handed
    to Console/Panel/Table as a raw str — `Text()` never interprets rich
    markup (unlike `Text.from_markup`/a bare str under `console.markup`),
    so a warning or next_action containing a literal `[...]` cannot be
    parsed as a style tag and silently eaten. `no_color=True` +
    `highlight=False` keep the render deterministic (no ANSI escapes
    interleaved into the middle of a warning's text); `soft_wrap=True`
    makes 'unfolded' literal — no word-wrap insertion, no width-based
    cropping — which is what rule (c) actually requires for the warnings
    section below."""
    Console, Panel, Table, Text = bundle
    console = Console(file=stream, no_color=True, highlight=False, soft_wrap=True)

    state = str(envelope.get("state", ""))
    body = Text(str(envelope.get("next_action", "")))
    numbers = envelope.get("summary_numbers")
    if numbers:
        body.append("\nsummary_numbers: %s" % numbers)
    console.print(Panel(body, title="state: %s" % state, expand=False))

    # Same ordering and same non-empty rule as the plain path -- see the comment
    # there. Every envelope-sourced value goes through `Text()` for the reason in
    # this function's docstring: a rule path or advisory containing `[...]` must not
    # be eaten as a rich style tag.
    for key in ("rules", "references"):
        values = envelope.get(key) or []
        if values:
            table = Table(title=key)
            table.add_column(key.rstrip("s"))
            for value in values:
                table.add_row(Text(str(value)))
            console.print(table)

    advisory = envelope.get("advisory")
    if advisory:
        console.print(Text("advisory: %s" % advisory))

    # Same three-part rendering as the plain path, through `Text()` for the same
    # reason every envelope-sourced value there is: a question/fact/option
    # containing a literal `[...]` must not be parsed as a rich style tag.
    ask_user = envelope.get("ask_user")
    if ask_user:
        if isinstance(ask_user, dict):
            table = Table(title="ask_user")
            table.add_column("question")
            table.add_row(Text(str(ask_user.get("question", ""))))
            console.print(table)
            facts = ask_user.get("facts") or []
            if facts:
                console.print(Text("facts: " + "; ".join(str(f) for f in facts)))
            options = ask_user.get("options") or []
            if options:
                console.print(Text("options: " + "; ".join(str(o) for o in options)))
        else:
            console.print(Text("ask_user: %s" % ask_user))

    pending = envelope.get("pending") or []
    if pending:
        console.print(Text("pending (%d): %s"
                           % (len(pending), ", ".join(str(p) for p in pending))))

    artifacts = envelope.get("artifacts") or []
    if artifacts:
        table = Table(title="artifacts")
        table.add_column("path")
        for artifact in artifacts:
            table.add_row(Text(str(artifact)))
        console.print(table)
    else:
        console.print("artifacts: (none)")

    checklist = envelope.get("checklist")
    if checklist:
        table = Table(title="checklist")
        table.add_column("done")
        table.add_column("step")
        table.add_column("evidence")
        for item in checklist:
            table.add_row(
                checklist_mark(item),
                str(item.get("step", "?")) + checklist_note(item),
                Text(str(item.get("evidence") or "")),
            )
        console.print(table)

    skipped = envelope.get("skipped")
    if skipped:
        console.print("skipped: %s" % ", ".join(skipped))

    next_command = envelope.get("next_command")
    if next_command:
        console.print(Text("next_command: %s" % next_command))

    facts = envelope.get("facts")
    if facts:
        if isinstance(facts, dict):
            table = Table(title="facts")
            table.add_column("fact")
            table.add_column("value")
            for fact_id, fact in facts.items():
                table.add_row(Text(str(fact_id)), Text(str(fact)))
            console.print(table)
        else:
            console.print(Text("facts: %s" % facts))

    # Rule (c) — see the module and function docstrings; this loop is the
    # one place that rule actually lives. Do not add a `[:N]`/summary path
    # here without also deleting the test that guards it.
    warnings = envelope.get("warnings") or []
    console.print("warnings: (none)" if not warnings else "warnings (%d):" % len(warnings))
    for warning in warnings:
        console.print(Text(str(warning)))

    root = envelope.get("root")
    if root:
        console.print(Text("root: %s (%s)" % (root, envelope.get("root_source", "?"))))


# ---------------------------------------------------------------- entry point


def render_envelope(envelope: dict, stream=None) -> None:
    """Render ONE hs-run envelope to `stream` (default sys.stdout) and
    return. No loop, no return code, nothing to chain onto the next call —
    see the module docstring for why that is exactly the point."""
    stream = stream if stream is not None else sys.stdout
    # The rich view frames an hs-run STEP; any other family envelope has no step in
    # it and prints by its own shape through the one printer.
    if "next_action" in envelope and _stream_is_tty(stream):
        try:
            bundle = _rich_bundle()
        except ImportError:
            pass
        else:
            _render_rich(envelope, stream, bundle)
            if hasattr(stream, "flush"):
                stream.flush()
            return
    stream.write(render_text(envelope) + "\n")
    if hasattr(stream, "flush"):
        stream.flush()


if __name__ == "__main__":
    # Manual, read-only smoke entry — pipes a real hs-run envelope in via
    # stdin, e.g.:
    #   python3 harness/scripts/hs_run.py plan status 2>/dev/null | head -1 \
    #     | python3 harness/scripts/hs_cli_render.py
    import json as _json

    render_envelope(_json.loads(sys.stdin.readline()))
