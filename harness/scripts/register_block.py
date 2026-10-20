#!/usr/bin/env python3
"""register_block.py — SSOT builder for the resolved-register context block.

Both context hooks — inject_prompt_context (SessionStart) and subagent_init
(SubagentStart) — call build_register so the two surfaces cannot drift. The block
carries the three knobs that shape what gets WRITTEN: code_style, the output
language, and the reasoning language. Each is a one-line essence plus a pointer at
the full profile, so a reader that needs the MANDATORY/Boundaries body slices it on
demand through body_for rather than paying for it every turn.

It used to carry a conversational half as well — a harshness ladder, a persona
form, interview-rigor knobs, a universal-harm floor, an authority pointer at a
voice rule — and an audience register on top of that. All of it shaped how the
assistant TALKED rather than what it produced, and all of it was injected on every
SessionStart and every subagent spawn. The cut is why `surface` no longer changes
what is emitted: with the conversational half gone, the session block and the
subagent block are the same block.

Pure library, never raises: body_for reads a profile file inside try/except and
degrades to "" on any failure — it never blocks a session or a subagent (the callers
are telemetry-class, fail-open).
"""

from pathlib import Path

# Resolve the profile-body dir off this file (scripts/ -> ../data/output-styles/),
# never CWD — the hook runs with an arbitrary working directory.
_STYLES_DIR = Path(__file__).resolve().parent.parent / "data" / "output-styles"


def body_for(kind: str, level) -> str:
    """Return the MANDATORY/Boundaries directive slice of a profile body, or "".

    Default install resolves code_style to None — the common path, not an edge —
    so short-circuit on None BEFORE building any path or touching the filesystem
    (no `level-None.md` ever gets constructed or logged).

    Reads harness/data/output-styles/{kind}-level-{level}.md and keeps only the
    `## MANDATORY...` / `## Boundaries...` sections: the H1, preamble, `---` rules,
    and any embedded `## Scope Fence` section are dropped — the register block
    carries its own scope-fence once, so re-emitting the file's copy would triple
    it. Any read failure → "" (fail-open, never raises)."""
    if level is None:
        return ""
    try:
        path = _STYLES_DIR / ("%s-level-%s.md" % (kind, level))
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:  # noqa: BLE001 - missing/unreadable profile → essence-only
        return ""
    return _slice_directives(text)


# `FORBIDDEN` is the older name of `Boundaries`; a deployer profile may still use it.
_DIRECTIVE_HEADS = ("MANDATORY", "BOUNDARIES", "FORBIDDEN")


def _slice_directives(text: str) -> str:
    """Keep only the `## MANDATORY*` / `## Boundaries*` sections of a profile body.

    Sections are delimited by level-2 headings (`## `); `### ` subheadings inside a
    section are NOT split points (they do not start with `## `). Content before the
    first `## ` (H1 + preamble) and any other section (e.g. `## Scope Fence`) is
    dropped."""
    sections = []
    current = None  # (heading_text, [lines]) once inside a kept section
    keep = False
    for line in text.splitlines():
        if line.startswith("## "):
            # Flush a finished kept section, then decide on the new heading.
            if keep and current is not None:
                sections.append("\n".join(current).rstrip())
            head = line[3:].strip().upper()
            keep = head.startswith(_DIRECTIVE_HEADS)
            current = [line] if keep else None
        elif keep and current is not None:
            current.append(line)
    if keep and current is not None:
        sections.append("\n".join(current).rstrip())
    return "\n\n".join(s for s in sections if s)


def build_register(prefs: dict, surface: str = "session") -> str:
    """The additionalContext register block for a session or a subagent.

    `prefs` is the flat dict from output_config.resolve_all(). Read straight off
    the dict — one source, no re-load, never raise (the resolver is fail-open by
    construction).

    `surface` is accepted and ignored. It selected between a session block and a
    reduced subagent block back when the difference was the conversational half;
    now both surfaces get the same three knobs, and the parameter stays so the two
    hooks keep one call shape.
    """
    import output_config  # same scripts dir; lazy to keep this module import-light

    # One fence line, emitted before the knobs rather than after. Each knob below
    # also states its own evidence carve-out, but a subagent writing a report reads
    # this block as the whole contract, so the invariant is stated once in the open
    # instead of only as a clause attached to whichever knob happens to be set.
    lines = [
        "Scope-fence: these knobs shape wording and code shape only. They never "
        "change evidence (file:line / IDs / SHAs / numbers / quotes), a gate "
        "decision, or an artifact's own designed voice.",
    ]

    prof = output_config.code_style_profile(prefs.get("code_style"))
    if prof:
        lines.append("[Code style - adapts the DELIVERABLE]")
        # Summary + pointer on one line: reading the profile in full cost more than
        # the steer it carried, paid once per session whether or not it applied.
        lines.append(
            "code_style=%d/5 (%s): shape generated CODE only (comment density, "
            "verbosity, examples) - %s. Details when needed: %s."
            % (prof["level"], prof["name"], prof["essence"],
               "harness/data/output-styles/code-style-level-%d.md" % prof["level"]))

    # State the OUTPUT language on its own line. It used to be mentioned only inside
    # the thinking-language directive below, which fires solely when the two differ —
    # so on the common config (both `vi`) the block named no language at all. The main
    # session recovers that from CLAUDE.md; a subagent has no such source and defaults
    # to English. Evidence tokens are exempt, same as everywhere else.
    tlang = prefs.get("thinking_language")
    olang = prefs.get("language")
    if olang:
        lines.append("[Output language]")
        lines.append(
            "language=%s: write chat prose, reports, and generated docs in %s. "
            "Evidence tokens (file:line / IDs / SHAs / numbers / quotes) and "
            "instruction files (skills/rules/agents) are never translated."
            % (olang, olang))
    # Emit ONLY when thinking_language differs from the output language — no point
    # saying "think in en, write in en".
    if tlang and tlang != olang:
        lines.append(
            "[Reasoning language - internal thinking only, NOT the written output]")
        lines.append(
            "thinking_language=%s: reason internally in %s for logic/precision; the written "
            "output stays in %s. Shapes the model's reasoning, never any evidence token "
            "(file:line / IDs / SHAs / numbers / quotes) or gate decision."
            % (tlang, tlang, olang))
    return "\n".join(lines)
