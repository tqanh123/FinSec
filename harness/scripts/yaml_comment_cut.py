#!/usr/bin/env python3
"""yaml_comment_cut.py — find a ` #` that YAML read as a comment inside a value.

YAML starts a comment at any `#` preceded by whitespace outside quotes, even
mid-sentence. `title: kept here (see issue #67)` loads as `kept here (see issue`:
no error, and anything rendered from the file carries the cut value. In a
multi-line plain scalar the same `#` mid-line ends the value early.

A plain (unquoted) scalar followed on its line by a comment is flagged when
  - the comment closes a bracket the kept value opened (`(see # 67)`), or
  - the `#` is glued to its text and both the kept value and the comment run
    to two words or more: prose broken mid-sentence (`see issue #85 for it`).

Measured on 91486 YAML files on one machine, vendored configs included: a glued
`#` alone flagged 19 of the comments after a plain scalar, 18 of them a one-token
note after a complete value (`Adam #Momentum`, `deep orange #black`). These rules,
reading past the lines ruamel rejects, flag 1 of the 4924 comments after a plain
scalar there, the one real cut. The same corpus shaped the two-word test,
so that is a fit, not an independent check. A comment on a line of its own between
a value and its next line ends the value too; it reads exactly like a comment
written under the value, the corpus holds none, and it is not flagged.

Read off the scanner's tokens, not the composed tree, so a file whose structure no
longer parses is still read and an alias is one token rather than a revisit of its
anchor. A character YAML does not allow is read as a space before scanning. Where
the scanner rejects a line (ruamel refuses `[mat:loader]`, which YAML 1.2 allows),
the lines below it indented deeper are skipped with it, since they can only go on
with what it opened, and scanning resumes after them. The scanner holds back a
token that may start a key, so the skip starts on the line of the first token it
had not handed out; its depth is taken from a flow collection still open above,
if any. A plain key whose `:` never came is marked on the next line, which it did
not open, so scanning resumes there; a quoted or flow key spanning lines is marked
inside what it opened, and that line is skipped. A sibling of a compact `- key:`
reads as deeper than its dash and is skipped too. Only a ` #` after something else
on its line can cut a value, so a skipped line with one is reported unread and the
rest are not; past the scanner's 10000th stop, nothing more is read. Lines are
counted as an editor counts them (`\\n`, `\\r\\n`, a lone `\\r`), not as the
scanner does.

  python3 yaml_comment_cut.py FILE...   # exit 0 clean, 1 cut found, 2 unreadable,
                                        # or a line with a ` #` unread, or a stop
"""
from __future__ import annotations

import bisect
import re
import sys
from dataclasses import dataclass, field

import yaml_io

_CLOSES = {"(": ")", "[": "]", "{": "}"}
_TRAILING_COMMENT = re.compile(r"[ \t]+(#[^\r\n\x85\u2028\u2029]*)")
_HASH = re.compile(r"[ \t]#")
_BREAK = re.compile(r"\r\n|\r|\n")
_FLOW_OPEN = (yaml_io.FlowSequenceStartToken, yaml_io.FlowMappingStartToken)
_FLOW_CLOSE = (yaml_io.FlowSequenceEndToken, yaml_io.FlowMappingEndToken)
_READ_TO_END = re.compile(r"[ \t]*(?:#.*)?")
_PROPERTIES = re.compile(r"(?:[&!][^ \t\r\n]*[ \t]+)*")
# A budget, not a loop guard: each resume starts past the last. 91486 files of one
# machine needed at most 959 (generated snapshots full of `[typing::Literal]`),
# at about 0.2 ms each.
_MAX_RESUMES = 10_000


@dataclass(frozen=True)
class Cut:
    line: int       # 1-based line holding the `#`
    kept: str       # the value YAML kept
    dropped: str    # from the `#` to the end of its line


@dataclass
class Checked:
    cuts: list
    unread: list = field(default_factory=list)     # [(line, why)] skipped lines with a ` #`
    replaced: list = field(default_factory=list)   # lines a disallowed character became a space in
    stopped: tuple | None = None                   # (line, why): nothing from here on was read


def _closes_kept_bracket(kept: str, comment: str) -> bool:
    stack: list[tuple[str, bool]] = []
    for text, from_kept in ((kept, True), (comment, False)):
        for ch in text:
            if ch in _CLOSES:
                stack.append((_CLOSES[ch], from_kept))
            elif stack and ch == stack[-1][0]:
                if stack.pop()[1] and not from_kept:
                    return True
    return False


def _is_cut(kept: str, comment: str) -> bool:
    if _closes_kept_bracket(kept, comment):
        return True
    glued = comment[1:2] and not comment[1].isspace()
    return bool(glued) and len(kept.split()) >= 2 and len(comment[1:].split()) >= 2


class _Lines:
    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [m.end() for m in _BREAK.finditer(text)]

    def number(self, index: int) -> int:
        return bisect.bisect_right(self.starts, index)

    def row(self, number: int) -> str:
        start = self.starts[number - 1]
        m = _BREAK.search(self.text, start)
        return self.text[start:m.start() if m else len(self.text)]

    def after(self, number: int) -> int:
        """Index where the line after `number` starts, or the end of the text."""
        return self.starts[number] if number < len(self.starts) else len(self.text)


def rows(text: str) -> list:
    """`text`'s lines as `check` numbers them."""
    return _BREAK.split(text[1:] if text.startswith("\ufeff") else text)


def _start(text: str, base: int) -> int:
    """Where the slice handed to the scanner starts: past a byte-order mark, which the
    C reader drops and the pure one counts."""
    return base + 1 if text.startswith("\ufeff", base) else base


def _indent(row: str) -> int:
    return len(row) - len(row.lstrip(" \t"))


def _plain_at(text: str, index: int) -> bool:
    """Whether the node at `index`, past its anchor and tag, is a plain scalar."""
    end = _PROPERTIES.match(text, index).end()  # type: ignore[union-attr]
    return text[end:end + 1] not in ("", '"', "'", "[", "{")


def _trailing_hash(row: str) -> bool:
    """A ` #` after something else on the line: only that can cut a value there."""
    m = _HASH.search(row)
    return m is not None and bool(row[:m.start()].strip())


def _scan(text: str, base: int, found: dict, at: dict) -> None:
    """Every cut in `text[base:]`, into `found` as it goes, so the cuts read before a
    scanner error stay. `at` keeps where the last token handed out ends (`seen`) and
    where each flow collection still open starts (`flows`)."""
    shift = _start(text, base)
    for tok in yaml_io.scan(text[shift:]):
        at["seen"] = shift + tok.end_mark.index
        if isinstance(tok, _FLOW_OPEN):
            at["flows"].append(shift + tok.start_mark.index)
        elif isinstance(tok, _FLOW_CLOSE) and at["flows"]:
            at["flows"].pop()
        elif isinstance(tok, yaml_io.ScalarToken) and tok.plain and tok.value:
            end = shift + tok.end_mark.index
            m = _TRAILING_COMMENT.match(text, end)
            if m and _is_cut(tok.value, m.group(1)):
                found[end] = (tok.value, m.group(1).rstrip())


def _problem(err) -> str:
    return " ".join(str(getattr(err, "problem", None) or err).split())


def check(text: str) -> Checked:
    """Every value of `text` that a ` #` cut short, in line order, with the lines it
    could not read and the ones where a disallowed character was read as a space."""
    if text.startswith("\ufeff"):
        text = text[1:]
    rows = _Lines(text)
    got = Checked([])
    # Replaced up front, one for one: the C reader reports a refused character's
    # position in UTF-8 bytes, so it cannot be found again from the error.
    got.replaced = sorted({rows.number(m.start())
                           for m in yaml_io.NON_PRINTABLE.finditer(text)})
    if got.replaced:
        text = yaml_io.NON_PRINTABLE.sub(" ", text)
    found: dict = {}
    base = 0
    for _ in range(_MAX_RESUMES):
        at: dict = {"seen": None, "flows": []}
        try:
            _scan(text, base, found, at)
            break
        except yaml_io.YAMLError as err:
            mark = getattr(err, "problem_mark", None)
            if mark is None:
                got.stopped = (rows.number(base), _problem(err))
                break
            shift = _start(text, base)
            stop = rows.number(shift + mark.index)
            context = getattr(err, "context_mark", None)
            first = min(rows.number(shift + context.index) if context is not None else stop, stop)
            # The scanner holds back a token that may start a key, so a line is read
            # only up to the last token it handed out; the rest of that line is not.
            seen = shift if at["seen"] is None else at["seen"]
            line = rows.number(seen)
            rest = text[seen:rows.after(line)].rstrip("\r\n")
            first = min(first, line + 1 if _READ_TO_END.fullmatch(rest) else line)
            top = min(first, rows.number(at["flows"][0])) if at["flows"] else first
            # A deeper line below can only go on with what the rejected line, or the
            # flow collection it is in, opened (a quoted or block scalar, a flow
            # collection): read alone it would be taken for plain scalars.
            opener = _indent(rows.row(top))
            last = stop
            if (stop > first and "simple key" in str(getattr(err, "context", ""))
                    and context is not None and _plain_at(text, shift + context.index)
                    and _indent(rows.row(stop)) <= opener):
                last = stop - 1  # a plain key's `:` looked for on a line it did not open
            while last < len(rows.starts) and (not rows.row(last + 1).strip()
                                               or _indent(rows.row(last + 1)) > opener):
                last += 1
            for line in range(first, last + 1):
                if _trailing_hash(rows.row(line)):
                    got.unread.append((line, _problem(err)))
            base = rows.after(last)
    else:
        if base < len(text):
            got.stopped = (rows.number(base), "the scanner stopped %d times" % _MAX_RESUMES)
    got.cuts = sorted((Cut(rows.number(end), kept, dropped)
                       for end, (kept, dropped) in found.items()), key=lambda c: c.line)
    return got


def describe(path: str, cut: Cut) -> str:
    return f"{path}:{cut.line}: value ends at `{cut.kept[-60:]}`; YAML dropped `{cut.dropped}`"


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: yaml_comment_cut.py FILE...\n")
        return 2
    rc = 0
    for path in argv:
        try:
            with open(path, encoding="utf-8") as fh:
                got = check(fh.read())
        except (OSError, UnicodeDecodeError) as exc:
            sys.stderr.write(f"{path}: unreadable: {exc}\n")
            rc = 2
            continue
        except Exception as exc:  # noqa: BLE001 — a crash is not a cut; say which file
            sys.stderr.write(f"{path}: could not check: {type(exc).__name__}: {exc}\n")
            rc = 2
            continue
        for cut in got.cuts:
            print(describe(path, cut))
        for line in got.replaced:
            sys.stderr.write(f"{path}:{line}: read a character YAML does not allow as a space\n")
        for line, why in got.unread:
            sys.stderr.write(f"{path}:{line}: not read: {why}\n")
        if got.stopped:
            sys.stderr.write("%s:%d: not read from here on: %s\n" % (path, *got.stopped))
        if got.unread or got.stopped:
            rc = 2
        elif got.cuts and rc == 0:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
