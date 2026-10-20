#!/usr/bin/env python3
"""shell_unwrap.py — quote-aware promotion of shell-wrapper payloads.

command_views(command) returns the command PLUS every `sh -c '<payload>'` /
`bash -c "…"` / `eval '…'` payload that sits at a real command head, peeled
recursively. A gate that scans every view sees the inner command that a
quote-masking view of the raw string cannot.

WHY THIS IS A NEW MODULE AND NOT AN EDIT TO stage_detector.unwrapped()
----------------------------------------------------------------------
`unwrapped()` finds a wrapper with a boundary-anchored regex, and a regex has
no quote state, so it also peels a wrapper that lives INSIDE a quoted argument
of the outer command. Measured: the benign

    git commit -m "docs: warn about ; sh -c 'rm -rf /'"

unwraps to `rm -rf /`, because the `;` inside the message reads as a command
separator. That is harmless for `unwrapped()`'s only caller today
(protected_ref_guard, which uses it to READ a refspec out of a command already
matched by detect_stage) but fatal for a BLOCKING gate: three commit messages
measured in this repo become refusals. Fixing `unwrapped()` in place would push
protected_ref_guard's verdict toward MORE blocking — the direction that is
forbidden for it — so the two callers get two functions.

REACH IS IMPORTED, NOT COPIED
-----------------------------
`_KEYWORDS` and `_PREFIXES` come from stage_detector so the wrapper set, the
`do`/`then`/`else` command positions and the exec-style prefix list (sudo, env,
timeout, VAR=…) cannot drift apart. This module changes CORRECTNESS, not reach:
every command the old function peels and that is genuinely unquoted, this one
peels too. Coupling to those private names is deliberate — a rename there must
break the tests here rather than silently narrow this gate.

WHY A LIST OF VIEWS INSTEAD OF ONE PEELED STRING
------------------------------------------------
The original is always views[0], and payloads are only ever ADDED. So a caller
that blocks when ANY view trips can only find more reasons to block than it did
on the raw command — wiring this in cannot turn a BLOCK into an allow. That
one-directional property is the whole safety argument; a function returning
just the innermost string would REPLACE the caller's view and lose it. Do not
"simplify" this to a single return value.

WHAT STAYS OPEN, BY DESIGN
--------------------------
Anything that needs evaluation rather than parsing: `eval $CMD` and other
variable indirection, `$(…)`/backtick payloads, base64-decode pipelines. Those
are recorded as debt, not silently implied to be covered — see the residual
list pinned in test_shell_unwrap.py::TestTheDocumentedResidualHoles.
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import stage_detector as _sd  # noqa: E402

# Depth of nesting to peel, and a hard ceiling on how many views one command can
# produce. Both are cost guards: this runs on EVERY Bash tool call, so a
# pathological `sh -c "sh -c \"sh -c …\""` must not turn one hook into a fan-out.
# Reaching the ceiling degrades to "fewer views", i.e. the pre-existing behaviour
# of the raw command — never to an exception.
_MAX_DEPTH = 6
_MAX_VIEWS = 24

# Characters that end one command and open the next. Only counted when UNQUOTED —
# that single condition is the entire difference from the regex this replaces.
# Backtick and `(` and `{` open a command position too (substitution, subshell,
# brace group), so a wrapper right after one of them is a real head.
_SEPARATORS = frozenset(";&|\n(){}`")

# Cheap bail-out: no wrapper word anywhere means no possible promotion. Skipping
# the scan on the overwhelmingly common case keeps the added latency near zero.
# Deliberately an OVER-approximation on plain \b: `script.sh` and `sh-like` fire
# it even though neither is a wrapper invocation. Firing spuriously costs one
# quote-scan that finds nothing; failing to fire would silently disable the whole
# module. An earlier version excluded `/` from the preceding character to be
# tidier and thereby stopped matching `/bin/sh -c …` — a path-qualified wrapper,
# which is precisely a variant this gate must see. Keep the loose form.
_TRIGGER_RE = re.compile(r"\b(?:sh|bash|dash|zsh|ash|ksh|eval)\b")

# Anchored at a verified-unquoted head (no leading boundary group — the caller
# supplies the position, which is what makes this quote-aware). `-\w*c` accepts a
# bundled flag such as `-lc` / `-ec`.
_WRAP_AT_HEAD = re.compile(
    r"\s*" + _sd._KEYWORDS + _sd._PREFIXES +
    r"(?:[\w./+-]*/)?(?:sh|bash|dash|zsh|ash|ksh)(?:\s+--?[\w-]+)*?\s+-\w*c\s+",
    re.S)
_EVAL_AT_HEAD = re.compile(r"\s*" + _sd._KEYWORDS + _sd._PREFIXES + r"eval\s+", re.S)

# A bare (unquoted) payload runs to the end of its segment.
_BARE_ARG_RE = re.compile(r"[^;&|\n]+")

# `<<DELIM` / `<<-DELIM` / `<<'DELIM'` opens a heredoc whose BODY is data, never a
# command position — with or without a quoted delimiter. Matching only a
# shell-legal delimiter name avoids reading an arithmetic `<<` shift as a redirect.
#
# `<<<` (a herestring — one ordinary word) must stay out, and it needs the guard on
# BOTH sides: a trailing `(?!<)` alone still matched the second-and-third `<` of
# `<<<`, one offset over, and swallowed the rest of the line as a body.
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
# Marker for "inside a heredoc body". Any non-empty context means not-a-command;
# a distinct character just makes a debug dump readable.
_HEREDOC_CTX = "<"


def _quote_contexts(s):
    """Per-index quote context: "" unquoted, "'" inside single, '"' inside double.

    Same length as `s`, so an offset can be tested directly. Shell rules that
    matter here: a `'` inside "…" is literal and a `"` inside '…' is literal
    (otherwise every apostrophe in a commit message desynchronises the scan),
    and a backslash escapes the next character everywhere except inside '…'.
    An UNTERMINATED quote leaves the tail quoted, which is the right answer —
    such a command is a shell syntax error and never runs at all.
    """
    out = []
    quote = ""
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and quote != "'":
            out.append(quote)                 # the backslash
            if i + 1 < n:
                out.append(quote)             # the character it escapes
                i += 1
            i += 1
            continue
        if not quote and ch in ("'", '"'):
            out.append(ch)                    # the opening quote joins the span
            quote = ch
        elif quote and ch == quote:
            out.append(quote)                 # the closing quote joins the span
            quote = ""
        else:
            out.append(quote)
        i += 1
    return out


def _heredoc_spans(command, contexts):
    """[(start, end)] of each heredoc CONSTRUCT — from the `<<` through the end of
    its closing-delimiter line — for every heredoc opened at an unquoted offset.

    Marks each body in `contexts` as it goes, so a `<<` that appears INSIDE an
    already-located body is skipped rather than opening a second, nested span.

    Two spellings defeat a plain "closer right after a newline" regex, and both
    are legal: `<<-DELIM` permits TAB indentation before the closer, and a body
    may be unterminated. Getting these wrong is not a near miss — it silently
    changes which text a caller believes is data.
    """
    spans = []
    for m in _HEREDOC_RE.finditer(command):
        if contexts[m.start()]:
            continue                      # the `<<` is itself quoted or in a body
        newline = command.find("\n", m.end())
        if newline < 0:
            continue                      # no body follows on this command
        closer = re.compile(r"^[ \t]*%s[ \t]*$" % re.escape(m.group(2)), re.M)
        found = closer.search(command, newline + 1)
        # An unterminated body runs to the end of the string, which is what the
        # shell does too (it reads until EOF and errors).
        body_end = found.start() if found else len(command)
        for k in range(newline + 1, body_end):
            contexts[k] = _HEREDOC_CTX
        spans.append((m.start(), found.end() if found else len(command)))
    return spans


def without_heredocs(command, replacement=" "):
    """`command` with every heredoc construct replaced by `replacement`.

    For callers that scan a command for SHELL syntax: a heredoc body is data, so
    leaving it in makes the body's text look like commands. Uses the same locator
    as the quote scanner, which is the point — a caller with its own heredoc regex
    gets to be wrong in its own private way, and one that failed to strip
    `<<-DELIM` (tab-indented closer), an unterminated body, or a mismatched closer
    was measured refusing three commands that only wrote /tmp.

    Note what this does NOT do: removing the body also removes anything the body
    was going to EXECUTE. A caller that needs to see interpreter code fed on stdin
    (`python3 - <<PY … PY`) must inspect the original command as well — stripping
    is the right default for shell matchers and the wrong one for that check.
    """
    if not isinstance(command, str):
        return command
    contexts = _quote_contexts(command)
    out = command
    for start, end in reversed(_heredoc_spans(command, contexts)):
        out = out[:start] + replacement + out[end:]
    return out


# Interpreters that run a program read from STDIN when the command line gives them
# none, each paired with the flag that hands it the program INLINE instead — after
# which stdin is plain data, the same as for `jq`. The flag must end its word: a
# bundled `-Bc`/`-ne` counts, `-Mfeature=say` (no code flag) does not. Flags that
# take a separate argument (`python3 -W ignore -c …`) are not modelled, so such a
# spelling still reads as stdin-executing — the over-blocking direction.
# A script-path argument (`python3 parse.py`) is not exempted either: telling a
# script path from a flag's argument needs per-interpreter option tables.
_FLAG_END = r"(?=[\s_'\"]|$)"
_INLINE_PROGRAM_FLAG = {
    r"python(?:\d+(?:\.\d+)*)?": r"-[A-Za-z]*[cm]" + _FLAG_END,   # -c CODE, -m MODULE
    "perl": r"-[A-Za-z]*[eE]" + _FLAG_END,              # -e, -E, -ne, -lane
    "ruby": r"-[A-Za-z]*e" + _FLAG_END,
    "node": r"(?:-[A-Za-z]*[ep]" + _FLAG_END + r"|--(?:eval|print)\b)",
    "php": r"-[rR]" + _FLAG_END,
}
# Matches an interpreter word that WILL read its program from stdin: the name, not
# followed (after any other flags) by its inline-program flag. Exposed so a gate
# deciding "is this pipe stage a program runner" uses the same table.
STDIN_PROGRAM_INTERPRETER = "(?:%s)" % "|".join(
    r"%s(?![\w.-])(?!(?:\s+-[\w-]+)*?\s+%s)" % (name, flag)
    for name, flag in _INLINE_PROGRAM_FLAG.items())

# Anything on a heredoc's opening line that makes its body run: a shell or an
# interpreter reading stdin, eval/source/`.`/xargs, or a command substitution the
# heredoc sits inside (its output can be executed later). Matched on a quote-masked
# copy of the line, at a word that is not part of a file name (`install.sh` is not
# `sh`). Over-matching keeps a body scanned, which is the pre-existing behaviour.
_STDIN_RUNNER_RE = re.compile(
    r"(?<![\w.-])(?:[\w./+-]*/)?(?:(?:ba|da|z|k)?sh|ash|eval|source|xargs)(?![\w.-])"
    r"|(?<![\w.-])(?:[\w./+-]*/)?" + STDIN_PROGRAM_INTERPRETER +
    r"|(?:^|[\s;&|(])\.(?=\s)"
    r"|\$\(|`")


def _logical_line(command, start, end):
    """[line_start, line_end) of the shell line holding [start, end): widened over
    backslash-newline continuations on both sides, so a consumer split across a
    continuation still reads as part of the opener."""
    lo = command.rfind("\n", 0, start) + 1
    while lo >= 2 and command[lo - 2] == "\\":
        lo = command.rfind("\n", 0, lo - 1) + 1
    hi = command.find("\n", end)
    while hi > 0 and command[hi - 1] == "\\":
        nxt = command.find("\n", hi + 1)
        if nxt < 0:
            return lo, len(command)
        hi = nxt
    return lo, (hi if hi >= 0 else len(command))


def without_data_heredocs(command):
    """`command` with every DATA heredoc body blanked to spaces (newlines kept, so
    the result has the same length and line structure).

    A body is data only when BOTH hold:
      - the delimiter is quoted (`<<'EOF'` / `<<"EOF"`): no parameter expansion and
        no command substitution happen inside it, so no part of it runs as it is
        read. An unquoted body can run `$(…)`, so it is always kept;
      - nothing on the opener's logical line runs stdin as a program
        (`_STDIN_RUNNER_RE`): `cat >> notes.md <<'EOF'` writes text, while
        `bash <<'EOF'`, `cat <<'EOF' | sh` and `python3 - <<'EOF'` execute it.

    For a gate that blocks on what a command RUNS: a quoted note appended to a
    ledger that mentions a dangerous pipeline is not that pipeline. Unlike
    `without_heredocs`, which removes every body for matchers of shell write syntax,
    this keeps every body that could execute, so a caller loses a verdict only for
    text that never runs.
    """
    if not isinstance(command, str):
        return command
    contexts = _quote_contexts(command)
    chars = list(command)
    for m in _HEREDOC_RE.finditer(command):
        if contexts[m.start()] or not m.group(1):
            continue                      # quoted/in-body opener, or unquoted delimiter
        newline = command.find("\n", m.end())
        if newline < 0:
            continue
        lo, hi = _logical_line(command, m.start(), m.end())
        line = "".join("_" if contexts[k] else command[k] for k in range(lo, hi))
        closer = re.compile(r"^[ \t]*%s[ \t]*$" % re.escape(m.group(2)), re.M)
        found = closer.search(command, hi + 1)
        body_end = found.start() if found else len(command)
        if not _STDIN_RUNNER_RE.search(line):
            for k in range(hi + 1, body_end):
                if chars[k] != "\n":
                    chars[k] = " "
        for k in range(hi + 1, body_end):
            contexts[k] = _HEREDOC_CTX    # a `<<` inside this body opens nothing
    return "".join(chars)


def _mask_heredoc_bodies(command, contexts):
    """Mark every heredoc body as non-command context, in place.

    Found the hard way: the FIRST real use of this module blocked its own probe
    script, `python3 - <<'PY' … PY`, because the body mentioned `sh -c 'rm -rf /'`
    and nothing marked that text as data. A heredoc body is exactly the same class
    of false positive as a commit message that talks about a dangerous command —
    text that describes a command instead of running one. The artifact-forgery
    gate already strips heredoc bodies for the same reason; this applies that
    settled ruling here rather than inventing a new one.

    This CANNOT loosen a caller: masking only stops a payload being PROMOTED out
    of a body. views[0] is still the raw command, so anything the pattern battery
    caught in a heredoc body before, it still catches.
    """
    _heredoc_spans(command, contexts)     # marks the bodies as a side effect
    return contexts


def _head_offsets(command, contexts):
    """Offsets where a command can start: 0, plus just after each unquoted
    separator. Whitespace after the separator is consumed by the `\\s*` in the
    head patterns."""
    heads = [0]
    for i, ch in enumerate(command):
        if ch in _SEPARATORS and not contexts[i]:
            heads.append(i + 1)
    return heads


def _payload_at(command, i):
    """The wrapper's payload argument starting at offset `i`, or None.

    A quoted payload gives up one level of quoting — that is the case that
    matters, because quoting is exactly what hides the inner command from a
    caller's quote-masking view. A BARE payload is returned too, for
    completeness; it is already plainly visible in the raw command, so that
    branch is belt-and-braces rather than load-bearing.
    """
    if i >= len(command):
        return None
    quote = command[i]
    if quote in ("'", '"'):
        j = i + 1
        while j < len(command):
            if command[j] == "\\" and quote == '"' and j + 1 < len(command):
                j += 2
                continue
            if command[j] == quote:
                return command[i + 1:j]
            j += 1
        # Unterminated: hand back the rest. Over-promoting here can only add a
        # view, and the command itself cannot run.
        return command[i + 1:]
    m = _BARE_ARG_RE.match(command, i)
    return m.group(0).strip() if m else None


def _promotions(command):
    """Payloads of every wrapper standing at a genuine, unquoted command head."""
    if not _TRIGGER_RE.search(command):
        return []
    contexts = _mask_heredoc_bodies(command, _quote_contexts(command))
    found = []
    for head in _head_offsets(command, contexts):
        for pattern in (_WRAP_AT_HEAD, _EVAL_AT_HEAD):
            m = pattern.match(command, head)
            if not m:
                continue
            # The matched run must itself be unquoted end-to-end. A head derived
            # from an unquoted separator can still be followed by text that opens
            # a quote, and a wrapper name found inside it is not a command.
            if any(contexts[k] for k in range(m.start(), m.end())):
                continue
            payload = _payload_at(command, m.end())
            if payload:
                found.append(payload)
            break        # one wrapper per head; the payload is recursed instead
    return found


def top_level_segments(command):
    """`command` split into top-level pieces at each UNQUOTED separator in
    `_SEPARATORS` (`;`, `&`, `|`, a newline, and the open of a subshell/
    brace/backtick — the same set command_views() heads off of), with a
    heredoc body kept GLUED to its opening line as one segment: a `;` or
    newline inside a heredoc body is program TEXT, not a real separator,
    and splitting there is exactly what let two unrelated sub-commands read
    as one to gate_stage's artifact-forgery battery (a write inside the
    heredoc body, a mention in a later, unrelated line, merged into one
    refusal because both arms scanned the whole raw string).

    Reuses `_quote_contexts`/`_mask_heredoc_bodies`/`_head_offsets` — the
    same locators `command_views()` already trusts for "is this offset a
    real command position" — rather than a second, private notion of where
    a segment starts. Non-string input returns `[command]` unchanged
    (fail-open: a caller's battery still runs on the one segment it always
    had)."""
    if not isinstance(command, str):
        return [command]
    contexts = _quote_contexts(command)
    spans = _heredoc_spans(command, contexts)  # also masks the bodies, as a side effect
    heads = _head_offsets(command, contexts)
    # `_head_offsets` still treats the heredoc's OWN opening newline (right
    # after `<<DELIM`) as a real separator — only the BODY past it is marked
    # heredoc context. A head landing strictly inside the full construct
    # (after its `<<`, before its closer's end) is that newline or one from
    # the body's own punctuation, never a real command boundary.
    heads = [h for h in heads if not any(s < h < e for s, e in spans)]
    segs = []
    for i, start in enumerate(heads):
        end = heads[i + 1] - 1 if i + 1 < len(heads) else len(command)
        segs.append(command[start:end])
    return segs


def command_views(command):
    """`command` plus every wrapper payload at a real command head, peeled.

    Original first, deduplicated, order stable. Non-string input is passed
    through untouched so a caller can hand raw tool input straight in.
    """
    if not isinstance(command, str):
        return [command]
    views = [command]
    seen = {command}
    frontier = [(command, 0)]
    while frontier and len(views) < _MAX_VIEWS:
        current, depth = frontier.pop(0)
        if depth >= _MAX_DEPTH:
            continue
        for payload in _promotions(current):
            if payload in seen:
                continue
            seen.add(payload)
            views.append(payload)
            frontier.append((payload, depth + 1))
            if len(views) >= _MAX_VIEWS:
                break
    return views


# git commit's message flag — deliberately ONLY -m/--message: their argument
# is LITERAL text sitting right in the command, provably inert data. -F/
# --file reads the message from an external file/fd the gate cannot see
# into, so it is never added here — a forger could point -F at anything.
_COMMIT_MSG_FLAG_RE = re.compile(r"(?:-m|--message)(?:=|\s+)")
_GIT_COMMIT_RE = re.compile(r"\bgit\b[^;&|\n]*?\bcommit\b")
# echo/printf at a genuine command head — same keyword/exec-prefix grammar as
# _WRAP_AT_HEAD, so the wrapper set cannot drift between the two head
# detectors in this module.
_ECHO_PRINTF_HEAD_RE = re.compile(
    r"\s*" + _sd._KEYWORDS + _sd._PREFIXES + r"(?:echo|printf)\b\s*", re.S)


def _span_at(command, i):
    """The consumed span [start, end) of the argument _payload_at would read
    starting at `i` — quoted (including both quote characters) or bare (the
    unquoted run to the next separator). Mirrors _payload_at's own branches
    exactly, so the two can never disagree about where an argument ends."""
    if i >= len(command):
        return (i, i)
    quote = command[i]
    if quote in ("'", '"'):
        j = i + 1
        while j < len(command):
            if command[j] == "\\" and quote == '"' and j + 1 < len(command):
                j += 2
                continue
            if command[j] == quote:
                return (i, j + 1)
            j += 1
        return (i, len(command))          # unterminated: runs to the end
    m = _BARE_ARG_RE.match(command, i)
    return m.span() if m else (i, i)


def mask_literal_data_arguments(command, replacement=" "):
    """`command` with the LITERAL, provably-inert data argument of
    `git commit -m/--message` and `echo`/`printf` blanked out (same length,
    quote/separator skeleton preserved) — a shell-syntax scanner no longer
    reads text a forger could plant inside a commit message or an echo'd
    string as if it were the command's OWN syntax.

    Root cause this closes: gate_stage's coarse redirect/tee/dd matchers run
    on a command with every quote character stripped, with no notion of
    "this text sits inside a quoted DATA argument". A commit message merely
    MENTIONING a redirect/tee/dd shape in prose (`git commit -m "fix: dd
    of=plans/p/artifacts/x bs=1"`) was refused as forgery — the data was
    scanned as if it were the shell's own syntax, the exact false-positive
    class `without_heredocs` already exists to close for heredoc bodies.

    Deliberately narrow, in both directions:
      - `-F`/`--file` is never masked (see _COMMIT_MSG_FLAG_RE) — unlike -m
        its argument is not inline literal text, so it stays exactly as
        scanned as before this function existed.
      - echo/printf's mask stops at the first unquoted redirect/pipe/
        separator — `echo "…" > plans/p/artifacts/x` keeps its real
        redirect target fully visible to every downstream matcher.

    Reuses _quote_contexts/_payload_at's own branch logic (via _span_at);
    no new parser."""
    if not isinstance(command, str):
        return command
    contexts = _quote_contexts(command)
    chars = list(command)

    def _blank(a, b):
        for k in range(a, b):
            chars[k] = replacement

    for gm in _GIT_COMMIT_RE.finditer(command):
        if any(contexts[k] for k in range(gm.start(), gm.end())):
            continue
        seg_end = gm.end()
        while seg_end < len(command) and command[seg_end] not in _SEPARATORS:
            seg_end += 1
        pos = gm.end()
        while pos < seg_end:
            m = _COMMIT_MSG_FLAG_RE.search(command, pos, seg_end)
            if not m:
                break
            if any(contexts[k] for k in range(m.start(), m.end())):
                pos = m.end()
                continue
            start, end = _span_at(command, m.end())
            if end > start:
                _blank(start, end)
            pos = max(end, m.end())

    for hm in _ECHO_PRINTF_HEAD_RE.finditer(command):
        if any(contexts[k] for k in range(hm.start(), hm.end())):
            continue
        end = hm.end()
        while end < len(command) and not (
                not contexts[end] and command[end] in ";&|\n>"):
            end += 1
        if end > hm.end():
            _blank(hm.end(), end)

    return "".join(chars)
