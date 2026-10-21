#!/usr/bin/env python3
"""yaml_comment_cut_nudge.py — PostToolUse:{Write|Edit|MultiEdit} nudge (nudge-class).

After a write to a `.yaml`/`.yml` file, names each value that write left cut short
by a ` #` (`scripts/yaml_comment_cut.py`): YAML reads
`kept here (see issue #67)` as `kept here (see issue` without a word, and a page
rendered from the file keeps the cut. The moment of the write is the one place
the author still knows what the value was meant to say. A line with a ` #` after
a value that the scanner could not read is named too: a cut there goes unchecked.

Scoped to what the call changed: Claude Code's Edit and Write results carry the
file's prior text (`tool_response.originalFile`, measured), and only cuts and
unread lines it did not already hold are named — so one already there is not
blamed on a later edit, nor is a cut in a line that was unread before it, and one
an edit makes by removing quotes or a line is. A new file, or a payload without
the prior text, counts every one as new.

Keyed on `tool_input.file_path`: a heredoc or `sed -i` write carries none and
is not seen here; `yaml_comment_cut.py FILE` answers for those. A file over 1 MB is
skipped: that is a generated lockfile or dump, not a hand-written value.

🛑 It does not block. PostToolUse runs after the write; exit 2 cannot un-write it.
"""
from __future__ import annotations

import os
import shlex
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001 — older streams; never fatal
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import hook_runtime  # noqa: E402
import yaml_comment_cut  # noqa: E402

HOOK_CLASS = "nudge"
_NAME = Path(__file__).stem
_SUFFIXES = (".yaml", ".yml")
_MAX_BYTES = 1_000_000


def _written_path(data: dict) -> Path | None:
    tool_input = data.get("tool_input")
    raw = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(raw, str) or not raw.lower().endswith(_SUFFIXES):
        return None
    p = Path(raw)
    if not p.is_absolute() and isinstance(data.get("cwd"), str):
        p = Path(data["cwd"]) / p
    return p


def _row(lines: list, number: int) -> str:
    return lines[number - 1].strip() if 0 < number <= len(lines) else ""


def _new_findings(data: dict, text: str) -> tuple:
    """(cuts, unread, stopped) this call made: those in the file now, less the ones its
    prior text already held. A cut is matched by value and dropped text, so one that
    only moved is not new and one copied once more names every copy; a cut read in a
    line that was unread before and is no longer is not new either. An unread line is
    matched by its text, and one copied once more names every copy too. With no prior
    text — a new file, or a runtime whose payload does not carry it — every one counts
    as new."""
    after = yaml_comment_cut.check(text)
    response = data.get("tool_response")
    before_text = response.get("originalFile") if isinstance(response, dict) else None
    if not isinstance(before_text, str) or not (after.cuts or after.unread or after.stopped):
        return after.cuts, after.unread, after.stopped
    before = yaml_comment_cut.check(before_text)
    was, now = yaml_comment_cut.rows(before_text), yaml_comment_cut.rows(text)
    was_unread = Counter(_row(was, n) for n, _why in before.unread)
    now_unread = Counter(_row(now, n) for n, _why in after.unread)
    freed = was_unread - now_unread
    held = Counter((c.kept, c.dropped) for c in before.cuts)
    count = Counter((c.kept, c.dropped) for c in after.cuts)
    cuts = []
    for c in after.cuts:
        if count[(c.kept, c.dropped)] <= held[(c.kept, c.dropped)]:
            continue
        if freed[_row(now, c.line)] > 0:
            freed[_row(now, c.line)] -= 1
            continue
        cuts.append(c)
    unread = [(n, why) for n, why in after.unread
              if now_unread[_row(now, n)] > was_unread[_row(now, n)]]
    return cuts, unread, None if before.stopped else after.stopped


def core(data: dict) -> None:
    session = data.get("session_id")
    path = _written_path(data)
    if path is None:
        return  # skip:not-applicable (not a .yaml/.yml write)
    try:
        if path.stat().st_size > _MAX_BYTES:
            return  # skip:not-applicable (over 1 MB: generated, not hand-written)
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        hook_runtime.trace_skip(_NAME, "subject-missing", session=session)
        return
    cuts, unread, stopped = _new_findings(data, text)
    if not cuts and not unread and not stopped:
        return  # skip:nothing-to-report
    lines = ["[yaml-comment-cut] YAML reads ` #` as the start of a comment, even mid-value:"]
    lines += [f"  · {yaml_comment_cut.describe(str(path), c)}" for c in cuts]
    lines += [f"  · {path}:{n}: not read: {why}; a ` #` after a value there went unchecked"
              for n, why in unread]
    if stopped:
        lines.append("  · %s:%d: not read from here on: %s; a cut below went unchecked"
                     % (path, *stopped))
    if cuts:
        lines.append("Fix: quote the value (`title: \"… (see issue #67)\"`), or, if a comment "
                     "was meant, put a space after the `#`.")
    checker = Path(yaml_comment_cut.__file__).resolve()
    lines.append(f"Re-run: python3 {shlex.quote(str(checker))} {shlex.quote(str(path))}")
    hook_runtime.emit_nudge(_NAME, "\n".join(lines), session=session,
                            default_channel="systemMessage")


def main(raw: str | None = None) -> None:
    data = hook_runtime.read_stdin_json() if raw is None else hook_runtime._parse(raw)
    try:
        if hook_runtime.hook_enabled(_NAME, HOOK_CLASS):
            core(data)
    except Exception as e:  # noqa: BLE001 — visibility never breaks a tool call
        hook_runtime.log_hook_error(_NAME, e)
    hook_runtime.drain_or_continue()


if __name__ == "__main__":
    main()
