#!/usr/bin/env python3
"""zsh_split_nudge.py — PreToolUse(Bash) nudge: an unquoted multi-word parameter
expansion is NOT word-split under zsh.

`cmd $V` / `for p in $P; do ... done` / `cmd ${ARR[$k]}` passes ONE argument in
zsh, where bash would split $V on whitespace into several. When the Bash tool
runs under a zsh login shell this silently breaks any command written the bash
way — three real breakages measured 2026-09-24 (a `for` loop over `${NODES[$k]}`
that ran once instead of nine times, an empty probe loop from `$cfg`, and a
`no tests ran` pytest arm from `$opt`). The fix on the agent's side is small
(`${=VAR}`, `xargs`, or run the command through `bash -c`), but nothing surfaces
the problem until the command has already misbehaved — this is that surface.

NARROWED RULE (measured 2026-09-25 at c723b1e95, 13861 distinct real Bash commands
from this machine's session transcripts):
a "fire on every unquoted $NAME" rule hit 11.8% of that corpus (1635/13861),
almost all single-path vars ($S, $SP, $f, ...) that zsh handles perfectly well
— a nudge that noisy gets switched off. Firing now needs the command to show
its own evidence that the value IS multi-word:

  Fires on:
  - `$NAME` / `${NAME}` / `${NAME<modifier>}` where `NAME` is bound, in THIS
    SAME command, to a literal value containing whitespace — a command-head
    assignment (`NAME="a b"`, `NAME='a b'`, optionally under
    export/local/declare/typeset) or a `for NAME in <all-literal list>` whose
    list has a whitespace-containing item (`for opt in "" "-o x=1"`).
  - `${NAME[key]}` whose `key` is not a bare integer, `@`, or `*` — an
    array/assoc lookup (`${NODES[$k]}`), as opposed to a scalar-safe index
    (`${pipestatus[1]}`, `${a[@]}`).

  Does NOT fire on (unchanged from before, plus two new ones marked *):
  - `"$V"` (double-quoted — quoting suppresses splitting in both shells)
  - `${=V}` (zsh's own explicit-split syntax — the thing this nudge suggests)
  - `$V` as the value of a simple assignment (`X=$V`) — POSIX/zsh/bash all
    skip field-splitting for an assignment's value, regardless of shell; the
    command-head check also recognises `do`/`then`/`else` as heads, so
    `do n=${v#q }` inside a loop body is exempt too
  - `$?` `$#` `$$` `$!` and any digit, `$(...)`  command substitution, and
    `$((...))` arithmetic — none of these match the identifier-only pattern
    this scan looks for, so they are excluded by construction, not by a
    special case
  - * a bare `$NAME` never bound multiword in this same command (the old,
    too-broad default — `$S` in `chmod +x $S/x.sh` no longer fires on its own)
  - * literally flanked by a `"` on both sides (`cmd[s-1]=='"' and cmd[e]=='"'`)
    — covers `"$(grep "$v" f)"`, where the NESTED inner quotes make
    shell_unwrap._quote_contexts misread `$v` as unquoted (a known, accepted
    blind spot of that scanner rather than something this module patches)

Detection stays quote-context-aware (reuses shell_unwrap._quote_contexts, the
same per-index "is this offset inside a quote" oracle stage_detector/gate_stage/
bash_safety_guard already trust) rather than a second, private notion of
quoting. Anything already inside `'...'` or `"..."` reads as quoted for free —
including a `bash -c '...'` payload, which is exactly why no separate masking
step exists for it: the payload sits inside real (outer-shell) single quotes,
so the quote-context scan already excludes it.

More false-positive shapes closed, all measured against the same corpus.
`[[ $V == x ]]`, a `case $V in` switch word, and a heredoc body never
field-split their expansion, quoted or not — those spans are now masked the
same way a quoted string already is. A declaration keyword
(`export`/`local`/`declare`/`typeset X=$V`) no longer defeats the plain
assignment-value exemption. And a PREFIX assignment (`NAME=value cmd ...`)
no longer counts as proof `$NAME` is multi-word in that SAME simple command —
prefix assignments bind only the invoked command's environment, never the
shell doing the expanding, so `$NAME` right after the prefix still reads
whatever NAME held BEFORE it; only a standalone assignment (one followed by a
command boundary — possibly after more assignment words, or a trailing
comment, `A="x y" B=1;` / `A="x y"  # note` — never another non-assignment
word) counts.

Nudge class: fail-open, silent on its own errors, config-gated in
harness-hooks.yaml — ships `enabled: true` there today, per the current
default posture; the nudge CLASS default without an entry would be OFF.
"""

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import hook_runtime  # noqa: E402
import nudge_dedupe  # noqa: E402
import shell_unwrap  # noqa: E402

HOOK_CLASS = "nudge"
_NAME = Path(__file__).stem
_DEDUPE_KIND = "zsh_split"

# A command HEAD: string start, right after an unquoted `;` `&` `|` newline or
# `(`, or right after the keyword `do`/`then`/`else` (with its one space).
# Shared by the multiword-assignment scan and the assignment-value exemption
# so "what counts as a command boundary" cannot drift between the two.
_HEAD = r"(?:^|[;&|\n(]|\b(?:do|then|else)\s)\s*"
_HEAD_AT_END_RE = re.compile(_HEAD + r"$")

_FOR_RE = re.compile(r"\bfor\s+([A-Za-z_]\w*)\s+in\s+(.*?)(?:;|\n|\bdo\b)", re.S)
# Group 1 (optional) is the declaration keyword itself — captured so a
# standalone declaration (`export NAME="a b"`, always its own statement) can
# be told apart from a bare `NAME="a b"`, which can ALSO be a PREFIX
# assignment to a following command word in the same simple command.
_ASSIGN_MULTIWORD_RE = re.compile(
    _HEAD + r"(export\s+|local\s+|declare\s+|typeset\s+)?"
    r"([A-Za-z_]\w*)=(\"[^\"$`]*\"|'[^']*')")
# What must follow a bare (undeclared) assignment's literal for it to be a
# real, standalone shell assignment rather than a per-command environment
# prefix: a command boundary, or the end of the string (a trailing `#...`
# comment counts too — it still ends at end-of-line/end-of-string, it just
# reads as a comment rather than nothing). `NAME="a b" cmd ...` (a non-
# assignment WORD starts right away, no boundary in between) is a prefix
# assignment instead: it sets NAME only in cmd's environment, never in the
# shell doing the expanding, so it must NOT count as proof of anything about
# how `$NAME` expands elsewhere in that same simple command.
_ASSIGN_BOUNDARY_AFTER_RE = re.compile(
    r"\s*(?:#[^\n]*)?(?:$|[;&|\n)]|\bdo\b|\bthen\b|\belse\b)")
# One more `NAME=value` word immediately following (whitespace-separated) —
# the shape a MULTI-assignment statement (`A="x y" B="c d";`) is made of.
# NAME and value are captured (not just skipped past): a word after the
# first in a standalone multi-assignment statement is itself a standalone
# binding too, so ITS OWN quoted value must be checked for whitespace the
# same way the first word's is, not merely used to find the boundary.
_NEXT_ASSIGN_WORD_RE = re.compile(
    r"\s+([A-Za-z_]\w*)=(\"[^\"$`]*\"|'[^']*'|[^\s;&|()]*)")
# A declaration keyword directly before `NAME=` — `export X=$V` is still a
# plain assignment-VALUE context, exactly like a bare `X=$V`; the keyword
# must not defeat that exemption (see _is_assignment_value).
_DECL_KEYWORD_AT_END_RE = re.compile(r"\b(?:export|local|declare|typeset)\s+$")
# `[[ ... ]]` extended-test bodies and a `case` statement's own switch WORD
# never field-split their expansion, quoted or not — POSIX/zsh both exempt
# them the same way an assignment's RHS is exempt.
_TEST_OPEN_RE = re.compile(r"\[\[(?=\s)")
_TEST_CLOSE_RE = re.compile(r"\]\]")
_CASE_WORD_RE = re.compile(r"\bcase\s+(\S+)\s+in\b")
_NO_SPLIT_CTX = "#"

# `${NAME[key]<modifier...>}` or bare `$NAME`. A digit/special char right after
# `$` (positional param, `$?`, `$(`, `$((`) matches neither alternative, so
# those never reach the scan at all.
_EXPANSION_RE = re.compile(r"\$\{([A-Za-z_]\w*)(\[[^\]]*\])?[^}]*\}|\$([A-Za-z_]\w*)")
_SUBSCRIPT_SAFE_RE = re.compile(r"\[(-?\d+|@|\*)\]")

_MESSAGE = (
    "[nudge] zsh_split: `%s` is an unquoted expansion whose value this command "
    "shows to be multi-word (or a non-scalar subscript) — under zsh (this Bash "
    "tool's $SHELL) it passes as ONE argument instead of splitting on "
    "whitespace the way bash would. If the split is intentional, use "
    "`${=VAR}` or pipe through `xargs`; otherwise quote it (`\"%s\"`) or run "
    "the command via `bash -c '...'`. Advisory, non-blocking.\n"
)


def _is_zsh(env=None) -> bool:
    env = env if env is not None else os.environ
    shell = env.get("SHELL") or ""
    return bool(shell) and os.path.basename(shell) == "zsh"


def _bound_multiword_names(command: str) -> set:
    """Names bound, IN THIS SAME command, to a literal value containing
    whitespace: a command-head assignment (`NAME="a b"`) or a `for NAME in
    <list>` whose list is entirely literal (no `$`, no backtick) and has a
    whitespace-containing item. A `$`/backtick anywhere in a `for` list means
    the actual per-iteration value cannot be read off the command text, so
    that loop var is never added — silence over a guess.

    A bare (undeclared) `NAME="a b"` counts only when it is a STANDALONE
    assignment (immediately followed by a command boundary or end of
    string) — one that is instead immediately followed by another word
    (`NAME="a b" cmd ...`) is a per-command PREFIX assignment: it sets NAME
    only in cmd's environment, never in the shell doing the expanding, so it
    proves nothing about how `$NAME` expands in that same simple command. A
    DECLARED assignment (`export`/`local`/`declare`/`typeset NAME=...`) has
    no such ambiguity — it can only ever be its own statement — so it always
    counts.

    A word AFTER the first in the SAME standalone statement (`A="x y"
    B="c d"`) is itself a standalone binding too — its own quoted value is
    walked and checked for whitespace the same way the first word's is,
    never merely skipped past to find the boundary. Prefix-vs-standalone is
    still decided ONCE for the whole run: `A="x y" B="c d" cmd ...` binds
    neither name to the shell doing the expanding, so the run counts only
    when the boundary check passes (or the run is DECLARED, which is always
    standalone)."""
    names = set()
    for m in _FOR_RE.finditer(command):
        lst = m.group(2)
        if "$" in lst or "`" in lst:
            continue
        try:
            items = shlex.split(lst)
        except ValueError:
            continue
        if any(re.search(r"\s", it) for it in items):
            names.add(m.group(1))
    for m in _ASSIGN_MULTIWORD_RE.finditer(command):
        candidates = [m.group(2)] if re.search(r"\s", m.group(3)[1:-1]) else []
        pos = m.end()
        while True:
            nm = _NEXT_ASSIGN_WORD_RE.match(command, pos)
            if not nm:
                break
            value = nm.group(2)
            if (len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]
                    and re.search(r"\s", value[1:-1])):
                candidates.append(nm.group(1))
            pos = nm.end()
        if not candidates:
            continue
        if not m.group(1) and not _ASSIGN_BOUNDARY_AFTER_RE.match(command, pos):
            continue  # a prefix assignment run to a following command, not standalone
        names.update(candidates)
    return names


def _is_assignment_value(command: str, pos: int) -> bool:
    """True when the expansion starting at `pos` is the value of a simple
    assignment word (`NAME=$VAR...`, optionally `export`/`local`/`declare`/
    `typeset NAME=$VAR...`) that itself opens a command (or sits right after
    `do`/`then`/`else`) — exempt from splitting in every POSIX-family shell,
    zsh included, regardless of what the value is. The declaration keyword,
    if present, is stripped before the head check so `export X=$V` reads
    exactly like a bare `X=$V`."""
    k = pos - 1
    if k < 0 or command[k] != "=":
        return False
    j = k
    while j > 0 and (command[j - 1].isalnum() or command[j - 1] == "_"):
        j -= 1
    prefix = command[:j]
    decl = _DECL_KEYWORD_AT_END_RE.search(prefix)
    if decl:
        prefix = prefix[:decl.start()]
    return _HEAD_AT_END_RE.search(prefix) is not None


def _mask_no_split_zones(command: str, contexts: list) -> None:
    """Mark, in `contexts` (the same per-index array `_quote_contexts`
    returns), every offset where an unquoted expansion still never gets
    word-split: inside a `[[ ... ]]` extended test, and the single WORD a
    `case` statement switches on (`case $V in`). POSIX/zsh exempt both from
    field-splitting regardless of quoting, unlike an ordinary command
    argument — the same reason an assignment's RHS is exempt."""
    for m in _TEST_OPEN_RE.finditer(command):
        if contexts[m.start()]:
            continue
        close = _TEST_CLOSE_RE.search(command, m.end())
        end = close.end() if close else len(command)
        for k in range(m.start(), end):
            if not contexts[k]:
                contexts[k] = _NO_SPLIT_CTX
    for m in _CASE_WORD_RE.finditer(command):
        s, e = m.start(1), m.end(1)
        if contexts[s]:
            continue
        for k in range(s, e):
            if not contexts[k]:
                contexts[k] = _NO_SPLIT_CTX


def _find_first_hit(command: str) -> Optional[Tuple[str, str]]:
    """The (token_text, reason) of the first qualifying expansion in
    `command`, or None. `reason` is "subscript" or "bound-multiword" — both
    render through the same advisory. Scans left to right and returns on the
    first hit: an advisory trigger, not a census of every offending token."""
    contexts = shell_unwrap._quote_contexts(command)
    shell_unwrap._heredoc_spans(command, contexts)  # marks bodies as data, in place
    _mask_no_split_zones(command, contexts)
    multiword = _bound_multiword_names(command)
    for m in _EXPANSION_RE.finditer(command):
        s, e = m.start(), m.end()
        if contexts[s] or m.group(0).startswith("${="):
            continue
        if s > 0 and e < len(command) and command[s - 1] == '"' and command[e] == '"':
            continue  # flanked by a literal quote pair (nested "$(...)" case)
        if _is_assignment_value(command, s):
            continue
        name = m.group(1) or m.group(3)
        sub = m.group(2)
        if sub and not _SUBSCRIPT_SAFE_RE.fullmatch(sub):
            return m.group(0), "subscript"
        if name in multiword:
            return m.group(0), "bound-multiword"
    return None


def core(data: Dict[str, Any]) -> Optional[str]:
    """Return the advisory for the first qualifying expansion in a Bash
    command run under zsh, or None. Any shell other than zsh (including an
    absent/unreadable $SHELL) is silent — bash DOES word-split, so there is
    nothing to warn about there."""
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None  # skip:not-applicable
    if not _is_zsh():
        return None  # skip:not-applicable
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None  # skip:not-applicable

    hit = _find_first_hit(command)
    if hit is None:
        return None  # skip:nothing-to-report
    token, _reason = hit

    session_id = data.get("session_id") or os.environ.get("HARNESS_SESSION_ID") or ""
    if nudge_dedupe.already_nudged(session_id, _DEDUPE_KIND, command):
        hook_runtime.trace_skip(_NAME, "already-reported", session=data.get("session_id"))
        return None
    nudge_dedupe.mark_nudged(session_id, _DEDUPE_KIND, command)
    return _MESSAGE % (token, token)


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
