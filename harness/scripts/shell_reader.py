#!/usr/bin/env python3
"""shell_reader.py — the shell's own reading of a command line, as facts.

`read(command, cwd)` returns every simple command the line runs -- at the top level, inside
`( )`, `{ }`, an `if`/`while`/`until`/`for`/`case` body, a function body, a `$( )`, a
backtick, a `<( )` or the `-c` line of a shell (`bash -c '…'`, run where the shell runs)
-- each with:

- its words after quote removal, as the shell hands them to the program: `r''m` is `rm`,
  `"-delete"` is `-delete`, `\\rm` is `rm`; an expansion the hook cannot evaluate stays as
  written and marks the word `dynamic`;
- its head past the prefixes that run it: `VAR=x`, `timeout 5`, `nice -n 5`, `env -i`,
  `sudo -u root`, `stdbuf -oL`, `busybox`, with `!` and `time` read as the pipeline's;
- its redirections;
- the directories it may run in.

The directories are tracked the way the shell runs the line. An and-or list keeps where the
shell may stand after the last pipeline succeeded and after it failed, so `cd X && a` runs
`a` in X only, `cd X || a` runs `a` where it was, and `cd X || cd Y; b` runs `b` in X or Y.
A `cd` changes nothing past `&` or `( )` or a substitution (a subshell); in a pipeline only
the last element may (zsh runs it in the shell itself); in a compound body or a function it
may or may not have run. A `cd` is sure to succeed only into a static directory that exists
now with nothing earlier in the line able to remove, move or replace it. Any other target
-- one that does not exist yet, one an earlier command could replace, one the shell expands
(`$X`, `~`, `-`) -- is a `Loose` directory: the literal text the line names it by, which no
reading vouches for.

Facts only. What a caller holds a command to (the artifact cage, the harness floor) is the
caller's own; a reader that carried one caller's policy changed every other caller's answer.
A line this reader cannot tokenize (an unclosed quote, group or substitution) reads as None:
what that means is the caller's call too.
"""

import os
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import shell_unwrap  # noqa: E402


class Loose(str):
    """A directory no reading vouches for: the literal path text the line names it by."""


Anchor = Union[Path, Loose]
#: where a function body or a loop's later pass may stand: anywhere
ANYWHERE = Loose("?")


@dataclass(frozen=True)
class Word:
    text: str           # after quote removal; an expansion kept as written (`$X`)
    raw: str
    dynamic: bool       # holds an expansion, glob or brace the hook cannot evaluate
    quoted: bool        # some part was quoted (a reserved word counts only unquoted)
    subs: Tuple[str, ...] = ()   # the command lines its substitutions run


@dataclass
class Command:
    words: List[Word]               # the simple command, prefixes included, redirections not
    head: int                       # index of the command's own name in `words`; len() = none
    exact: bool                     # every prefix before the head was read whole
    redirects: List[Tuple[str, Word]]
    text: str                       # the source text of the command
    cwds: List[Anchor]              # where it may run
    own_dir: Optional[Word] = None  # `env -C DIR` / `sudo -D DIR`: the command's own cwd
    pipeline: int = 0
    position: int = 0
    nested: bool = False
    #: every pipeline it is part of, innermost last, each as (pipeline, position): a command
    #: inside `( )` or `{ }` feeds the pipe the group stands in
    pipes: Tuple[Tuple[int, int], ...] = ()
    #: `simple`, or `list`: the words a `for`/`select` loops over (no program runs them)
    kind: str = "simple"

    def position_in(self, pipeline: int) -> Optional[int]:
        return next((pos for pid, pos in self.pipes if pid == pipeline), None)

    @property
    def argv(self) -> List[Word]:
        return self.words[self.head:]

    @property
    def name(self) -> str:
        return posixpath.basename(self.words[self.head].text) if self.head < len(self.words) else ""


class Unreadable(ValueError):
    pass


# ---------------------------------------------------------------------------- tokens

_OPS = sorted(("&&", "||", ";;&", ";;", ";&", "|&", "&>>", "&>", ">>", "<<<", "<<-", "<<", "<>",
               "<&", ">&", ">|", "((", ";", "&", "|", "(", ")", "<", ">"), key=len, reverse=True)
_REDIRS = frozenset({"<", ">", ">>", "&>", "&>>", "<>", ">|", "<&", ">&", "<<<", "<<", "<<-"})
_META = " \t\n;&|()<>"
_BRACE_RE = re.compile(r"\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}")
_ARRAY_RE = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")


def _match_paren(s: str, k: int) -> int:
    """Index of the `)` closing a `(` opened just before `k`."""
    depth, i, n = 1, k, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise Unreadable("unclosed '")
            i = j + 1
            continue
        if c == '"':
            i = _dquote(s, i + 1)[0]
            continue
        if c == "`":
            i = _backtick_end(s, i + 1) + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Unreadable("unclosed (")


def _backtick_end(s: str, k: int) -> int:
    i = k
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    raise Unreadable("unclosed `")


def _brace_end(s: str, k: int) -> int:
    depth, i = 1, k
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = (j + 1) if j >= 0 else len(s)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Unreadable("unclosed ${")


def _dollar(s: str, i: int) -> "tuple[int, str, list]":
    """An expansion at `s[i] == '$'`: (end, its text, the command lines it runs)."""
    if s.startswith("$((", i):
        depth, j = 2, i + 3
        while j < len(s) and depth:
            depth += {"(": 1, ")": -1}.get(s[j], 0)
            j += 1
        if depth:
            raise Unreadable("unclosed $((")
        return j, s[i:j], []
    if s.startswith("$(", i):
        j = _match_paren(s, i + 2)
        return j + 1, s[i:j + 1], [s[i + 2:j]]
    if s.startswith("${", i):
        j = _brace_end(s, i + 2)
        inner, subs = s[i + 2:j], []
        for sm in re.finditer(r"\$\(", inner):
            if not inner.startswith("$((", sm.start()):
                subs.append(inner[sm.end():_match_paren(inner, sm.end())])
        return j + 1, s[i:j + 1], subs
    m = re.match(r"\$(?:[A-Za-z_]\w*|[0-9@*#?$!-])", s[i:])
    if m:
        return i + len(m.group(0)), m.group(0), []
    return i + 1, "$", []


def _dquote(s: str, i: int) -> "tuple[int, str, bool, list]":
    """A double-quoted span opened before `i`: (end past its `"`, text, dynamic, subs)."""
    out: "list[str]" = []
    subs: "list[str]" = []
    dyn = False
    while i < len(s):
        c = s[i]
        if c == '"':
            return i + 1, "".join(out), dyn, subs
        if c == "\\" and i + 1 < len(s) and s[i + 1] in '$`"\\\n':
            if s[i + 1] != "\n":
                out.append(s[i + 1])
            i += 2
            continue
        if c == "$":
            i, part, sub = _dollar(s, i)
            out.append(part)
            dyn |= part != "$"
            subs += sub
            continue
        if c == "`":
            j = _backtick_end(s, i + 1)
            subs.append(s[i + 1:j])
            out.append(s[i:j + 1])
            dyn = True
            i = j + 1
            continue
        out.append(c)
        i += 1
    raise Unreadable('unclosed "')


_ANSI = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
         "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(s: str, i: int) -> "tuple[int, str]":
    """A `$'…'` span whose body starts at `i`: (end past its `'`, decoded text)."""
    out: "list[str]" = []
    while i < len(s):
        c = s[i]
        if c == "'":
            return i + 1, "".join(out)
        if c == "\\" and i + 1 < len(s):
            e = s[i + 1]
            m = re.match(r"x([0-9A-Fa-f]{1,2})|([0-7]{1,3})", s[i + 1:])
            if m:
                out.append(chr(int(m.group(1), 16) if m.group(1) else int(m.group(2), 8)))
                i += 1 + len(m.group(0))
                continue
            out.append(_ANSI.get(e, "\\" + e))
            i += 2
            continue
        out.append(c)
        i += 1
    raise Unreadable("unclosed $'")


def _word(s: str, i: int) -> "tuple[Word, int]":
    out: "list[str]" = []
    subs: "list[str]" = []
    start, dyn, quoted = i, False, False
    while i < len(s):
        c = s[i]
        if c in _META:
            if c == "(" and not quoted and _ARRAY_RE.fullmatch("".join(out)):
                j = _match_paren(s, i + 1)  # `name=(a b)`: an array, one word
                for tok in _tokens(s[i + 1:j]):
                    if tok[0] == "W":
                        subs += tok[1].subs
                out.append(s[i:j + 1])
                dyn, i = True, j + 1
                continue
            if c in "<>" and s.startswith("(", i + 1) and i > start:
                j = _match_paren(s, i + 2)  # a process substitution glued to the word
                subs.append(s[i + 2:j])
                out.append(s[i:j + 1])
                dyn, i = True, j + 1
                continue
            break
        if c == "\\":
            if s.startswith("\\\n", i):
                i += 2
                continue
            if i + 1 < len(s):
                out.append(s[i + 1])
                quoted = True
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise Unreadable("unclosed '")
            out.append(s[i + 1:j])
            quoted, i = True, j + 1
            continue
        if s.startswith("$'", i):
            i, part = _ansi_c(s, i + 2)
            out.append(part)
            quoted = True
            continue
        if s.startswith('$"', i):
            i += 1
            continue
        if c == '"':
            i, part, d, sub = _dquote(s, i + 1)
            out.append(part)
            quoted, dyn, subs = True, dyn or d, subs + sub
            continue
        if c == "$":
            i, part, sub = _dollar(s, i)
            out.append(part)
            dyn |= part != "$"
            subs += sub
            continue
        if c == "`":
            j = _backtick_end(s, i + 1)
            subs.append(s[i + 1:j])
            out.append(s[i:j + 1])
            dyn, i = True, j + 1
            continue
        if c in "*?[" or (c == "~" and i == start) or (c == "{" and _BRACE_RE.match(s, i)):
            dyn = True
        out.append(c)
        i += 1
    return Word("".join(out), s[start:i], dyn, quoted, tuple(subs)), i


def _tokens(s: str) -> list:
    """[(kind, value, start, end)]: kind W (a Word), OP, or NL."""
    out: list = []
    i = 0
    while i < len(s):
        c = s[i]
        if c in " \t":
            i += 1
        elif s.startswith("\\\n", i):
            i += 2
        elif c == "\n":
            out.append(("NL", "\n", i, i + 1))
            i += 1
        elif c == "#":
            j = s.find("\n", i)
            i = len(s) if j < 0 else j
        elif s.startswith(("<(", ">("), i):
            j = _match_paren(s, i + 2)
            w, k = _word(s, j + 1)
            out.append(("W", Word(s[i:j + 1] + w.text, s[i:k], True, w.quoted,
                                  (s[i + 2:j],) + w.subs), i, k))
            i = k
        else:
            m = re.match(r"\d+(?=[<>])", s[i:])
            if m:
                i += len(m.group(0))
                continue
            op = next((o for o in _OPS if s.startswith(o, i)), None)
            if op:
                out.append(("OP", op, i, i + len(op)))
                i += len(op)
            else:
                w, k = _word(s, i)
                if k == i:  # nothing a word could take: never loop
                    raise Unreadable("stuck at %r" % s[i:i + 10])
                out.append(("W", w, i, k))
                i = k
    return out


# ---------------------------------------------------------------------------- tree

@dataclass
class _Simple:
    words: list
    redirects: list
    text: str
    kind: str = "simple"      # simple | list (the words a `for`/`select` loops over)


@dataclass
class _Group:
    kind: str                 # subshell | group | if | loop | case | function
    parts: list               # each a sequence
    redirects: list = field(default_factory=list)
    text: str = ""


@dataclass
class _Pipeline:
    nodes: list
    negated: bool = False


@dataclass
class _AndOr:
    pipes: list               # [(pipeline, the connector before it)]
    background: bool = False


def _reserved(tok, *names) -> bool:
    return bool(tok) and tok[0] == "W" and not tok[1].quoted and tok[1].text in names


class _Parser:
    def __init__(self, s: str, toks: list):
        self.s, self.t, self.i = s, toks, 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def take(self):
        tok = self.peek()
        if tok is None:
            raise Unreadable("unexpected end")
        self.i += 1
        return tok

    def expect(self, name):
        if not _reserved(self.peek(), name):
            raise Unreadable("expected %s" % name)
        self.i += 1

    def newlines(self):
        while self.peek() and self.peek()[0] == "NL":
            self.i += 1

    def seq(self, closers=frozenset(), ops=frozenset()) -> list:
        items: list = []
        while True:
            self.newlines()
            tok = self.peek()
            if tok is None:
                if closers or ops:
                    raise Unreadable("unclosed group")
                return items
            if (tok[0] == "OP" and tok[1] in ops) or _reserved(tok, *closers):
                return items
            if tok[0] == "OP" and tok[1] == ";":
                self.i += 1
                continue
            ao = self.and_or(closers)
            tok = self.peek()
            if tok and tok[0] == "OP" and tok[1] in (";", "&"):
                ao.background = tok[1] == "&"
                self.i += 1
            elif tok and tok[0] == "NL":
                self.i += 1
            elif tok and not ((tok[0] == "OP" and tok[1] in ops) or _reserved(tok, *closers)
                              or (tok[0] == "OP" and tok[1] in (";;", ";&", ";;&"))):
                raise Unreadable("unexpected %r" % (tok[1],))
            items.append(ao)
            if tok and tok[0] == "OP" and tok[1] in (";;", ";&", ";;&") and ";;" in ops:
                return items

    def and_or(self, closers) -> _AndOr:
        pipes = [(self.pipeline(closers), None)]
        while self.peek() and self.peek()[0] == "OP" and self.peek()[1] in ("&&", "||"):
            conn = self.take()[1]
            self.newlines()
            pipes.append((self.pipeline(closers), conn))
        return _AndOr(pipes)

    def pipeline(self, closers) -> _Pipeline:
        neg = False
        while True:
            if _reserved(self.peek(), "!"):
                neg = not neg
                self.i += 1
            elif _reserved(self.peek(), "time"):
                self.i += 1
                while self.peek() and self.peek()[0] == "W" and self.peek()[1].text.startswith("-"):
                    self.i += 1
            else:
                break
        nodes = [self.command(closers)]
        while self.peek() and self.peek()[0] == "OP" and self.peek()[1] in ("|", "|&"):
            self.i += 1
            self.newlines()
            nodes.append(self.command(closers))
        return _Pipeline(nodes, neg)

    def redirects(self) -> list:
        out = []
        while self.peek() and self.peek()[0] == "OP" and self.peek()[1] in _REDIRS:
            op = self.take()[1]
            tok = self.take()
            if tok[0] != "W":
                raise Unreadable("a redirection without a target")
            out.append((op, tok[1]))
        return out

    def group(self, kind, parts, start) -> _Group:
        g = _Group(kind, parts)
        g.redirects = self.redirects()
        g.text = self.s[start:self.t[self.i - 1][3]] if self.i else ""
        return g

    def command(self, closers):
        tok = self.peek()
        if tok is None:
            raise Unreadable("a command was expected")
        start = tok[2]
        if tok[0] == "OP" and tok[1] == "(":
            self.i += 1
            body = self.seq(ops=frozenset({")"}))
            self.take()
            return self.group("subshell", [body], start)
        if tok[0] == "OP" and tok[1] == "((":
            self.i += 1
            while not (self.peek() and self.peek()[0] == "OP" and self.peek()[1] == ")"
                       and self.peek(1) and self.peek(1)[0] == "OP" and self.peek(1)[1] == ")"):
                self.take()
            self.i += 2
            return _Simple([], self.redirects(), self.s[start:self.t[self.i - 1][3]])
        if _reserved(tok, "{"):
            self.i += 1
            body = self.seq(frozenset({"}"}))
            self.expect("}")
            return self.group("group", [body], start)
        if _reserved(tok, "if"):
            self.i += 1
            parts = [self.seq(frozenset({"then"}))]
            self.expect("then")
            parts.append(self.seq(frozenset({"elif", "else", "fi"})))
            while _reserved(self.peek(), "elif"):
                self.i += 1
                parts.append(self.seq(frozenset({"then"})))
                self.expect("then")
                parts.append(self.seq(frozenset({"elif", "else", "fi"})))
            if _reserved(self.peek(), "else"):
                self.i += 1
                parts.append(self.seq(frozenset({"fi"})))
            self.expect("fi")
            return self.group("if", parts, start)
        if _reserved(tok, "while", "until"):
            self.i += 1
            parts = [self.seq(frozenset({"do"}))]
            self.expect("do")
            parts.append(self.seq(frozenset({"done"})))
            self.expect("done")
            return self.group("loop", parts, start)
        if _reserved(tok, "for", "select"):
            self.i += 1
            head = []
            if self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "((":
                while not (self.peek() and self.peek()[0] == "OP" and self.peek()[1] == ")"
                           and self.peek(1) and self.peek(1)[0] == "OP" and self.peek(1)[1] == ")"):
                    self.take()
                self.i += 2
            else:
                self.take()                         # the loop variable
                self.newlines()
                if _reserved(self.peek(), "in"):
                    self.i += 1
                    while self.peek() and self.peek()[0] == "W":
                        head.append(self.take()[1])
            while self.peek() and (self.peek()[0] == "NL" or self.peek()[1] == ";"):
                self.i += 1
            parts = [[_AndOr([(_Pipeline([_Simple(head, [], "", "list")]), None)])]] if head else []
            if _reserved(self.peek(), "{"):
                self.i += 1
                parts.append(self.seq(frozenset({"}"})))
                self.expect("}")
            else:
                self.expect("do")
                parts.append(self.seq(frozenset({"done"})))
                self.expect("done")
            return self.group("loop", parts, start)
        if _reserved(tok, "case"):
            self.i += 1
            subject = self.take()
            self.newlines()
            self.expect("in")
            parts = [[_AndOr([(_Pipeline([_Simple([subject[1]], [], "")]), None)])]]
            while True:
                self.newlines()
                if _reserved(self.peek(), "esac"):
                    self.i += 1
                    break
                if self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "(":
                    self.i += 1
                while not (self.peek() and self.peek()[0] == "OP" and self.peek()[1] == ")"):
                    self.take()
                self.i += 1
                parts.append(self.seq(frozenset({"esac"}), frozenset({";;"})))
                if self.peek() and self.peek()[0] == "OP" and self.peek()[1] in (";;", ";&", ";;&"):
                    self.i += 1
            return self.group("case", parts, start)
        if _reserved(tok, "function"):
            self.i += 1
            self.take()
            if self.peek() and self.peek()[1] == "(" and self.peek(1) and self.peek(1)[1] == ")":
                self.i += 2
            self.newlines()
            return _Group("function", [[_AndOr([(_Pipeline([self.command(closers)]), None)])]])
        if (tok[0] == "W" and not tok[1].quoted and self.peek(1) and self.peek(1)[0] == "OP"
                and self.peek(1)[1] == "(" and self.peek(2) and self.peek(2)[1] == ")"):
            self.i += 3
            self.newlines()
            return _Group("function", [[_AndOr([(_Pipeline([self.command(closers)]), None)])]])
        return self.simple(closers)

    def simple(self, closers) -> _Simple:
        words, redirects = [], []
        start = self.peek()[2]
        end = start
        cond = _reserved(self.peek(), "[[")
        while True:
            tok = self.peek()
            if tok is None or tok[0] == "NL":
                break
            if cond and tok[0] == "OP" and tok[1] not in (";", "&", ";;"):
                words.append(Word(tok[1], tok[1], False, False))
            elif tok[0] == "OP" and tok[1] in _REDIRS and not cond:
                redirects += self.redirects()
                end = self.t[self.i - 1][3]
                continue
            elif tok[0] == "OP":
                break
            elif not words and _reserved(tok, *closers):
                break
            else:
                words.append(tok[1])
                if cond and _reserved(tok, "]]"):
                    cond = False
            end = tok[3]
            self.i += 1
        if not words and not redirects:
            raise Unreadable("an empty command")
        return _Simple(words, redirects, self.s[start:end])


# ---------------------------------------------------------------------------- prefixes

#: per wrapper: (its short options that take the next word, its long ones, the positional
#: words it reads before the command it runs)
_WRAPPERS = {
    "sudo": ("ugpChDrtUTR", {"user", "group", "host", "prompt", "close-from", "chdir", "role",
                             "type", "other-user", "command-timeout", "chroot"}, 0),
    "doas": ("uC", set(), 0),
    "env": ("uCSP", {"unset", "chdir", "split-string"}, 0),
    "nice": ("n", {"adjustment"}, 0),
    "nohup": ("", set(), 0),
    "timeout": ("sk", {"signal", "kill-after"}, 1),
    "stdbuf": ("ioe", {"input", "output", "error"}, 0),
    "ionice": ("cnt", {"class", "classdata"}, 0),
    "chrt": ("", set(), 1),
    "setsid": ("", set(), 0),
    "unbuffer": ("", set(), 0),
    "command": ("", set(), 0),
    "builtin": ("", set(), 0),
    "exec": ("a", set(), 0),
    "busybox": ("", set(), 0),
    "chronic": ("", set(), 0),
    "ifne": ("", set(), 0),
    "time": ("fo", {"format", "output"}, 0),
}
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*\+?=")
#: a wrapper's option that names the command's own working directory
_CHDIR_OPTS = {("env", "C"), ("env", "chdir"), ("sudo", "D"), ("sudo", "chdir")}


def strip_prefix(words: list) -> "tuple[int, bool, Optional[Word]]":
    """(index of the command's own name, whether every prefix was read whole, the directory
    a wrapper runs it in) past `VAR=x` assignments and the wrappers that run their words."""
    i, exact, own_dir = 0, True, None
    while i < len(words):
        w = words[i]
        if _ASSIGN_RE.match(w.text) and not w.text.startswith("="):
            i += 1
            continue
        name = posixpath.basename(w.text)
        spec = _WRAPPERS.get(name)
        if spec is None:
            break
        if name == "command" and i + 1 < len(words) and words[i + 1].text in ("-v", "-V"):
            return len(words), exact, own_dir  # prints what a name is; runs nothing
        shorts, longs, positional = spec
        i += 1
        while i < len(words) and words[i].text.startswith("-") and words[i].text != "-":
            t = words[i].text
            i += 1
            if t == "--":
                break
            if t.startswith("--"):
                opt, eq, val = t[2:].partition("=")
                if opt in longs and not eq and i < len(words):
                    if (name, opt) in _CHDIR_OPTS:
                        own_dir = words[i]
                    i += 1
                elif (name, opt) in _CHDIR_OPTS and eq:
                    own_dir = Word(val, val, words[i - 1].dynamic, words[i - 1].quoted)
                exact &= name != "env" or opt != "split-string"
                continue
            for j, ch in enumerate(t[1:]):
                if ch in shorts:
                    rest = t[j + 2:]
                    if (name, ch) in _CHDIR_OPTS:
                        own_dir = (Word(rest, rest, words[i - 1].dynamic, words[i - 1].quoted)
                                   if rest else (words[i] if i < len(words) else None))
                    if not rest:
                        i += 1
                    exact &= not (name == "env" and ch == "S")
                    break
                if name == "nice" and ch.isdigit():
                    break
        if name == "env":
            while i < len(words) and _ASSIGN_RE.match(words[i].text):
                i += 1
        if name == "chrt" and i < len(words) and words[i].text.startswith("-"):
            exact = False
        i += positional
    return i, exact, own_dir


# ---------------------------------------------------------------------------- directories

#: a command that can remove, move or replace a directory a later `cd` names
_MUTATORS = frozenset({"rm", "rmdir", "mv", "ln", "unlink", "shred", "chmod", "chown", "rsync",
                       "find", "xargs", "tar", "unzip", "cp", "install", "mount", "umount"})
_GIT_MUTATORS = frozenset({"clean", "rm", "mv", "checkout", "switch", "reset", "restore", "stash",
                           "worktree", "submodule", "pull", "merge", "rebase", "am", "apply"})


def join(anchor: Anchor, part: str) -> Anchor:
    """`part` under `anchor`; a Loose anchor keeps the result loose."""
    if part.startswith("/"):
        return Path(part)
    if isinstance(anchor, Loose):
        return Loose(posixpath.join(anchor, part))
    return anchor / part


def _union(*sets) -> list:
    out: list = []
    for s in sets:
        for x in s:
            if x not in out:
                out.append(x)
    return out


class _Tracker:
    def __init__(self):
        self.out: "dict[int, Command]" = {}
        self.mutated = False
        self.pipes = 0
        self.ctx: "list[tuple[int, int]]" = []
        self.cdpath = bool(os.environ.get("CDPATH"))

    def seq(self, seq, S, nested):
        for ao in seq:
            S = self.and_or(ao, S, nested)
        return S

    def and_or(self, ao, S, nested):
        ok = bad = S
        for k, (pipe, conn) in enumerate(ao.pipes):
            if k == 0:
                run, keep = S, []
            elif conn == "&&":
                run, keep = ok, bad
            else:
                run, keep = bad, ok
            s, f = self.pipeline(pipe, run or S, nested)
            if not run:
                s, f = [], []
            if k == 0:
                ok, bad = s, f
            elif conn == "&&":
                ok, bad = s, _union(f, keep)
            else:
                ok, bad = _union(keep, s), f
        return S if ao.background else _union(ok, bad)

    def pipeline(self, p, R, nested):
        self.pipes += 1
        pid = self.pipes
        res = []
        for k, n in enumerate(p.nodes):
            self.ctx.append((pid, k))
            try:
                res.append(self.node(n, R, nested, pid, k))
            finally:
                self.ctx.pop()
        if len(p.nodes) == 1:
            s, f = res[0]
        else:
            s = f = _union(R, *res[-1])  # bash runs each element in a subshell; zsh the last here
        return (f, s) if p.negated else (s, f)

    def node(self, n, R, nested, pid, pos):
        if isinstance(n, _Simple):
            return self.simple(n, R, nested, pid, pos)
        for _op, w in n.redirects:
            self.subs(w, R)
        if n.redirects:
            self.record(_Simple([], n.redirects, " ".join(op + " " + w.raw for op, w in n.redirects)),
                        R, nested, pid, pos)
        if n.kind == "subshell":
            self.seq(n.parts[0], R, True)
            return R, R
        if n.kind == "group":
            out = self.seq(n.parts[0], R, nested)
            return out, out
        if n.kind == "function":
            before = len(self.out)
            self.seq(n.parts[0], _union(R, [ANYWHERE]), True)
            moves = any(c.name in ("cd", "pushd", "popd") for c in list(self.out.values())[before:])
            X = _union(R, [ANYWHERE]) if moves else R
            return X, X
        X = list(R)
        for _ in range(2 if n.kind == "loop" else 1):
            for part in n.parts:
                X = _union(X, self.seq(part, X, True))
        if n.kind == "loop" and len(X) > len(R):
            X = _union(X, [ANYWHERE])
        return X, X

    def subs(self, w, R):
        for line in w.subs:
            self.seq(_parse(line), R, True)

    def record(self, n, R, nested, pid, pos) -> Command:
        head, exact, own_dir = strip_prefix(n.words)
        if n.kind == "list":
            head, own_dir = len(n.words), None
        cwds = list(R)
        if own_dir is not None:
            cwds = [self.cd_into(a, own_dir)[0] for a in R]
        prior = self.out.get(id(n))
        if prior:
            prior.cwds = _union(prior.cwds, cwds)
            return prior
        c = Command(list(n.words), head, exact, list(n.redirects), n.text, cwds, own_dir,
                    pid, pos, nested, tuple(self.ctx), n.kind)
        self.out[id(n)] = c
        return c

    def simple(self, n, R, nested, pid, pos):
        for w in n.words:
            self.subs(w, R)
        for _op, w in n.redirects:
            self.subs(w, R)
        c = self.record(n, R, nested, pid, pos)
        name = c.name
        argv = c.argv
        if name in ("cd", "pushd"):
            args = argv[1:]
            while args and args[0].text.startswith("-") and args[0].text != "-":
                if args[0].text == "--":
                    args = args[1:]
                    break
                args = args[1:]
            target = args[0] if args else None
            new = [self.cd_into(a, target) for a in R]
            moved = [p for p, _sure in new]
            sure = all(s for _p, s in new)
            return moved, ([] if sure else R)
        if name == "popd":
            return [ANYWHERE], R
        body = _shell_body(argv) if name in _SHELLS else None
        if body is not None:
            self.seq(_parse(body.text), c.cwds, True)
        if name in _MUTATORS or not c.exact or (
                name == "git" and any(w.text in _GIT_MUTATORS for w in argv[1:])) or (
                argv and argv[0].dynamic):
            self.mutated = True
        return R, R

    def cd_into(self, anchor: Anchor, target: Optional[Word]) -> "tuple[Anchor, bool]":
        """Where `cd target` from `anchor` lands, and whether it is sure to get there."""
        if target is None:
            return Loose("~"), False
        d = target.text
        if target.dynamic or d == "-" or d.startswith(("~", "+")):
            return Loose(posixpath.join(str(anchor), d)), False
        q = join(anchor, d)
        if isinstance(q, Loose):
            return q, False
        if self.cdpath and not d.startswith(("/", "./", "../")) and d not in (".", ".."):
            return Loose(str(q)), False
        if self.mutated:
            return Loose(str(q)), False
        try:
            if q.is_dir():
                return q.resolve(), True
        except OSError:
            pass
        return Loose(str(q)), False


#: the shells whose `-c` body is a command line of its own, run where the shell runs
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "yash"})
#: a shell's options that take the next word
_SHELL_ARG_OPTS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


def _shell_body(argv: list) -> Optional[Word]:
    """The command line a shell runs from `-c` (`bash -c`, `sh -ec`, `bash -o pipefail
    -c`): the first word past its options, when one of them carries `c`; else None."""
    i, has_c = 1, False
    while i < len(argv):
        t = argv[i].text
        if t in _SHELL_ARG_OPTS:
            i += 2
            continue
        if t == "--":
            i += 1
            break
        if t.startswith("--"):
            i += 1
            continue
        if t[:1] in "-+" and len(t) > 1:
            has_c = has_c or (t[0] == "-" and "c" in t[1:])
            i += 1
            continue
        break
    return argv[i] if has_c and i < len(argv) else None


def _parse(s: str) -> list:
    return _Parser(s, _tokens(s)).seq()


def read(command, cwd) -> Optional[List[Command]]:
    """Every simple command `command` runs, with the directories it may run in from `cwd`
    (a Path, or a Loose one); None when the line cannot be tokenized."""
    if not isinstance(command, str):
        return None
    text = shell_unwrap.without_heredocs(command)
    try:
        tree = _parse(text)
        t = _Tracker()
        t.seq(tree, [cwd], False)
    except (Unreadable, RecursionError):
        return None
    return list(t.out.values())
