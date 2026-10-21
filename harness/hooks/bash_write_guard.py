#!/usr/bin/env python3
"""bash_write_guard.py — PostToolUse:Bash advisory (telemetry-class, fail-open).

write_guard (PreToolUse:Write|Edit|MultiEdit) blocks the agent's FILE tools from
editing gate config, but it is structurally blind to a write spelled through the
shell: a redirect (`> file`, `>> file`), `tee`, `sed -i`, or a `python -c
"open(path,'w')"`. This hook closes the visibility gap, not the write: it runs
PostToolUse — AFTER the command already ran — so it CANNOT block. It does the one
honest thing left: detect that a guarded path was written through Bash, record it
to the telemetry ledger, and nudge. The edited file is tracked, so the change
also surfaces as a git diff. Defense-in-depth surfacing (tamper-EVIDENT), exactly
the floor write_guard documents — never an airtight gate.

Deliberately NOT flagged: `cp`/`mv` into a guarded path. That is the SANCTIONED
workaround for editing a guarded file (write to /tmp, then cp/mv into place); it
is explicit and visible, so nagging on it would punish the blessed path. Reading
a guarded file and redirecting ELSEWHERE (`cat guarded > /tmp/x`) is not a write
to it and stays silent — only the WRITE TARGET is matched, never a source arg.

Posture: telemetry-class (default ON, fail-open, always continues). The detection
is a heuristic over the command string, in lockstep with write_guard's GUARD_LIST
and root resolution (one guarded set, two observers).
"""

import os
import posixpath
import re
import sys
from pathlib import Path
from typing import Optional

# Diagnostic text may carry non-ASCII; guard stderr encoding so a non-UTF-8
# locale degrades to replacement chars instead of raising mid-write (fail-open).
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — older streams / already detached; never fatal
    pass

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HOOKS_DIR, "..", "scripts"))
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)
import hook_runtime  # noqa: E402
import shell_reader  # noqa: E402 — the shell's own reading: commands, words, cwds
import write_guard  # noqa: E402 — reuse GUARD_LIST + root + path resolution (DRY)

HOOK_CLASS = "telemetry"
_NAME = Path(__file__).stem

# HIGH priority (H2-resolved, INV-3 F-2): a write_guard bypass through the shell is
# security-relevant and must reach the human, not just stderr-on-exit-0 (spec-invisible).
# core() routes it through the shared systemMessage queue (emit_nudge); main() drains
# that into the hook's ONE terminal JSON blob, so the dispatcher can call core() in
# process without a double write.

# A line `shell_reader` cannot tokenize still splits into segments on these
# connectors, so a write target in one segment is never paired with a verb in
# another. Shared shape with bash_safety_guard, kept local to avoid a hook→hook import.
_CONNECTOR_RE = re.compile(r"[;&|\n]+")
_COMMENT_RE = re.compile(r"(?:^|\s)#[^\n]*")

# Redirect target: an optional fd digit, `>`/`>>`, an optional clobber-force
# `|`, then the path. `>&2` / `2>&1` carry a `&` right after the operator → the
# negative class rejects them, so an fd-dup is never mistaken for a file write.
_REDIR_RE = re.compile(r"\d*>>?\|?\s*([^\s|;&<>()]+)")
# dd if=… of=PATH — a block-copy write to a file path (raw-device targets are
# bash_safety_guard's job; here we only care about a guarded config path).
_DD_RE = re.compile(r"\bdd\b[^|;&\n]*\bof=([^\s|;&<>()]+)")
# truncate [-s N] FILE — empties/sizes a file (its last bare arg is the target)
_TRUNCATE_RE = re.compile(r"\btruncate\b(?:\s+-\S+|\s+\d+)*\s+([^\s|;&<>()]+)")
# Quoted spans: masked to same-length underscores BEFORE redirect/tee/sed
# matching so a `>` living inside a quoted literal is never read as a real
# operator (the underscores carry no `>` and cannot fnmatch a guarded path).
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# tee [opts] FILE...  — group(1) eats option flags, group(2) the file args.
_TEE_RE = re.compile(r"\btee\b((?:\s+-\S+)*)((?:\s+[^\s|;&<>()]+)*)")
# sed in-place: -i / -i.bak / --in-place. Presence flips the whole segment into
# "scan every token" mode (the edited files are bare args; fnmatch filters out
# the script + flags, only a real guarded path can survive).
_SED_INPLACE_RE = re.compile(r"\bsed\b[^|;&\n]*?(?:\s-\w*i\w*\b|\s--in-place\b)")
# python -c "... open('FILE', 'w'|'a'|'x' ...) ..." — write-mode open only.
# The mode check requires the SECOND argument's quoted value to be, in full,
# a realistic write-capable file mode (w/a/x, optionally +/b/t) — not any
# quoted substring elsewhere containing a w/a/x character. A bare
# `[^)]*['"][^'"]*[wax]` search would match on ANY later kwarg value sharing
# that letter (`errors='ascii'`) or even the PATH string itself if a THIRD
# argument's quote boundary were crossed, over-flagging a plain read.
_PY_C_RE = re.compile(r"\bpython3?\b[^|;&\n]*?\s-c\b")
_PY_MODE_LITERAL = r"""['"](?:[wax][bt]?\+?|[wax]\+[bt]?)['"]"""
_PY_OPEN_RE = re.compile(
    r"open\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*(?:\w+\s*=\s*)?" + _PY_MODE_LITERAL)
# pathlib: Path('PATH').write_text(...) / .write_bytes(...) — a literal-path write
_PY_WRITE_RE = re.compile(
    r"\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*write_(?:text|bytes)")
# A redirect/dd target may be QUOTED; the mask underscored it, so recover from raw.
_QUOTED_TARGET_RE = re.compile(r"(['\"])([^'\"]*)\1")
_BARE_TARGET_RE = re.compile(r"[^\s|;&<>()]+")


def _redir_target_raw(raw, start):
    """Redirect target from the RAW segment at `start`. Masking is equal-length so
    positions align; a quoted target (`> 'plans/p/artifacts/x.json'`) was masked to
    underscores and would otherwise be a forgery-gate blind spot — read it back."""
    s = raw[start:]
    mq = _QUOTED_TARGET_RE.match(s)
    if mq:
        return mq.group(2)
    mb = _BARE_TARGET_RE.match(s)
    return mb.group(0) if mb else ""


def _root() -> Path:
    return write_guard._root()


def _guard_patterns() -> tuple:
    """GUARD_LIST plus any write-guard.yaml extra_guarded — the SAME set
    write_guard enforces, so the two observers never drift apart."""
    try:
        return write_guard.GUARD_LIST + write_guard._extra_guarded(
            write_guard._switch_config())
    except Exception:  # noqa: BLE001 — fall back to the constant; never crash
        return write_guard.GUARD_LIST


def _unquote(s: str) -> str:
    """Drop quote chars, keeping content — recovers a quoted target for the
    branches that scan whitespace tokens (sed -i, cp/mv dest)."""
    return s.replace("'", "").replace('"', "")


def _mask(cmd: str) -> str:
    """Mask quoted spans to equal-length underscores FIRST, then blank a
    trailing `# comment` — in that order, so a `#` living inside a quoted
    string (`echo 'a #b' > f`) is already underscored and cannot be misread
    as a comment start eating the rest of the line. Both substitutions are
    length-preserving (spaces/underscores match the consumed span exactly),
    so offsets taken on the result still align with the RAW command. A `>`
    inside a quoted string is then NOT seen as a redirect operator (kills
    false positives like `echo "a > path"`), while an unquoted redirect to a
    quoted target is rare and acceptably missed for an advisory."""
    no_quotes = _QUOTED_RE.sub(lambda m: "_" * len(m.group(0)), cmd)
    return _COMMENT_RE.sub(lambda m: " " * len(m.group(0)), no_quotes)


def _candidate_targets(seg_masked: str, seg_raw: str) -> list:
    """WRITE targets in one segment — the paths a write verb lands ON, never a
    source/arg path. Redirect/tee/dd give precise targets; sed -i scans all
    tokens (its targets are bare args); python open() yields its literal path
    arg (read from the RAW segment, which still carries the quotes)."""
    targets = []
    for m in _REDIR_RE.finditer(seg_masked):
        targets.append(_redir_target_raw(seg_raw, m.start(1)))
    for m in _DD_RE.finditer(seg_masked):
        targets.append(m.group(1))
    for m in _TRUNCATE_RE.finditer(seg_masked):
        targets.append(m.group(1))
    tee = _TEE_RE.search(seg_masked)
    if tee:
        targets.extend(t for t in tee.group(2).split() if not t.startswith("-"))
    seg_unq = _unquote(seg_raw)
    if _SED_INPLACE_RE.search(seg_unq):
        targets.extend(seg_unq.split())
    if _PY_C_RE.search(seg_raw):
        targets.extend(m.group(1) for m in _PY_OPEN_RE.finditer(seg_raw))
        targets.extend(m.group(1) for m in _PY_WRITE_RE.finditer(seg_raw))
    return targets


# cp/mv/ln/install/rsync DEST — the LAST operand (SRC args are reads), or the
# directory `-t`/`--target-directory` names. Kept behind the include_copy_move flag:
# the config observer EXCLUDES it (cp/mv into a guarded config is the blessed
# write-to-/tmp-then-move workaround), the artifact gate INCLUDES it (an artifact
# has no sanctioned shell path).
# Recognised only as a command's own name (past `VAR=x` and the wrappers that run it):
# `grep -rn install a b` names the word, it runs no copy.
_COPIERS = frozenset({"cp", "mv", "ln", "install", "rsync"})
# A redirection word: `2>&1`, `>/dev/null`, `>>log`, `<in` -- or a bare operator
# (`>`, `2>`) whose target is the next word. Neither is an operand.
_REDIR_WORD_RE = re.compile(r"^\d*(?:&>>?|>>?&?|<)(.*)$")


#: per copier, the short options that take an argument: in a bundle (`-Dm644`) the rest of
#: the word is it, alone (`-m`) the next word is -- never an operand either way. rsync's
#: `-t` is --times and `-m`/`-o`/`-g`/`-S` take nothing; only GNU's copiers have `-t DIR`.
_SHORT_ARGS = {"cp": "St", "mv": "St", "ln": "St", "install": "Smogt", "rsync": "efBTM@"}
_TDIR_COPIERS = frozenset({"cp", "mv", "ln", "install"})


def _is_tdir_long(name: str) -> bool:
    """getopt_long reads any unambiguous prefix of a long option: `--t`, `--targ`."""
    return len(name) >= 1 and "target-directory".startswith(name)


def _copy_operands(words: list, cmd: str = "cp") -> "list[tuple[str, list, bool]]":
    """(dest, sources, dest is a directory) for one copy's argument words, read with the
    copier's own option grammar (`_SHORT_ARGS`). A long option written without `=` may or
    may not take the next word, and a wrong guess hides the target either way, so both
    readings are kept: every target either names is returned."""
    out: list = []
    for long_takes_arg in (False, True):
        for hit in _copy_reading(words, cmd, long_takes_arg):
            if hit not in out:
                out.append(hit)
    return out


def _copy_reading(words: list, cmd: str, long_takes_arg: bool) -> list:
    short_args = _SHORT_ARGS.get(cmd, "")
    tdir_ok = cmd in _TDIR_COPIERS
    ops: list = []
    tdir = None
    i, opts = 0, True
    while i < len(words):
        w = words[i]
        m = _REDIR_WORD_RE.match(w)
        if m:
            i += 1 if m.group(1) else 2
            continue
        nxt = words[i + 1] if i + 1 < len(words) else None
        if opts and w == "--":
            opts = False
        elif opts and w.startswith("--"):
            name, eq, val = w[2:].partition("=")
            if tdir_ok and _is_tdir_long(name):
                tdir = val if eq else nxt
                i += 0 if eq else 1
            elif not eq and long_takes_arg:
                i += 1
        elif opts and w.startswith("-") and len(w) > 1:
            rest = w[1:]
            for j, ch in enumerate(rest):
                if ch in short_args:
                    arg = rest[j + 1:]
                    if not arg:
                        arg = nxt
                        i += 1
                    if ch == "t" and tdir_ok:
                        tdir = arg
                    break
        else:
            ops.append(w)
        i += 1
    if tdir:
        return [(tdir, ops, True)] if ops else []
    if len(ops) < 2:  # need >=1 src + a dest
        return []
    return [(ops[-1], ops[:-1], ops[-1].endswith("/"))]


def _copy_move_targets(words: list) -> "list[tuple[str, list, bool]]":
    """(dest, sources, dest is a directory) for one command's words, its own name first."""
    cmd = posixpath.basename(words[0]) if words else ""
    return _copy_operands(words[1:], cmd) if cmd in _COPIERS else []


#: commands whose operands are files they remove
_REMOVERS = frozenset({"rm", "unlink", "shred", "srm"})


def _plain_operands(words: list) -> list:
    out, opts = [], True
    for w in words:
        if _REDIR_WORD_RE.match(w):
            continue
        if opts and w == "--":
            opts = False
        elif not (opts and w.startswith("-") and len(w) > 1):
            out.append(w)
    return out


#: git's global options that take the next word (`git -C <dir> rm`, `git -c k=v rm`)
_GIT_ARG_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                           "--config-env", "--attr-source", "--exec-path"})
#: rsync's long options that take the next word when written without `=`
_RSYNC_ARG_LONGS = frozenset({
    "exclude", "include", "exclude-from", "include-from", "files-from", "filter", "rsh", "rsync-path",
    "backup-dir", "suffix", "temp-dir", "compare-dest", "copy-dest", "link-dest", "partial-dir",
    "log-file", "log-file-format", "out-format", "password-file", "chmod", "chown", "usermap",
    "groupmap", "timeout", "contimeout", "bwlimit", "max-size", "min-size", "max-delete",
    "modify-window", "sockopts", "port", "address", "info", "debug", "skip-compress", "iconv",
    "checksum-choice", "compress-choice", "compress-level", "outbuf", "stop-after", "stop-at",
    "write-batch", "read-batch", "only-write-batch", "protocol", "block-size", "max-alloc",
    "remote-option", "early-input", "copy-as"})


def _git_command(rest: list) -> "tuple[str, str, list, bool]":
    """(the -C directory it runs in, its subcommand, the subcommand's words, whether a
    `-c alias.*` makes the subcommand unknowable), past git's global options -- the value
    of `-C`/`-c`/`--git-dir` is no subcommand."""
    j, cdir, aliased = 0, "", False
    while j < len(rest) and rest[j].startswith("-"):
        if rest[j] in _GIT_ARG_OPTS:
            if rest[j] == "-C" and j + 1 < len(rest):
                cdir = posixpath.join(cdir, rest[j + 1])
            aliased |= rest[j] == "-c" and j + 1 < len(rest) and rest[j + 1].startswith("alias.")
            j += 2
        else:
            j += 1
    return (cdir, rest[j], rest[j + 1:], aliased) if j < len(rest) else (cdir, "", [], aliased)


def _rsync_operands(words: list) -> list:
    """rsync's operands: the words no option took (`--exclude <pat>`, `-e <cmd>` take one)."""
    out, i, opts = [], 0, True
    while i < len(words):
        w = words[i]
        m = _REDIR_WORD_RE.match(w)
        if m:
            i += 1 if m.group(1) else 2
            continue
        if opts and w == "--":
            opts = False
        elif opts and w.startswith("--"):
            name, eq, _ = w[2:].partition("=")
            i += 1 if (not eq and name in _RSYNC_ARG_LONGS) else 0
        elif opts and w.startswith("-") and len(w) > 1:
            for j, ch in enumerate(w[1:]):
                if ch in _SHORT_ARGS["rsync"]:
                    i += 0 if w[j + 2:] else 1
                    break
        elif w:
            out.append(w)
        i += 1
    return out


def _removal_operands(words: list) -> list:
    """The files one command removes, its own name first: rm/unlink/shred's operands,
    `git rm`'s (never `--cached`: the file stays), `git clean`'s pathspecs, what `mv` moves
    away (its sources), and what `rsync` deletes (`--del`/`--delete*`: its destination;
    `--remove-source-files`/`--remove-sent-files`: its sources)."""
    if not words:
        return []
    cmd, rest = posixpath.basename(words[0]), words[1:]
    if cmd == "git":
        cdir, sub, args, aliased = _git_command(rest)
        if sub == "clean":
            ops = _plain_operands(args) or ["."]  # no pathspec: the directory it runs in
        elif (sub == "rm" or aliased) and "--cached" not in args:
            ops = _plain_operands(args)
        else:
            return []
        return [posixpath.join(cdir, o) for o in ops] if cdir else ops
    if cmd == "mv":
        return [s for _d, srcs, _isdir in _copy_operands(rest, "mv") for s in srcs]
    if cmd == "rsync":
        ops = _rsync_operands(rest)
        gone = ops[-1:] if any(w == "--del" or w.startswith("--delete") for w in rest) else []
        sent = any(w in ("--remove-source-files", "--remove-sent-files") for w in rest)
        return gone + (ops[:-1] if sent else [])
    if cmd not in _REMOVERS:
        return []
    return _plain_operands(rest)


#: xargs's options that take the next word (written apart; `-I{}`, `-n1` carry it)
_XARGS_ARG_OPTS = frozenset({"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--arg-file",
                             "--delimiter", "--max-args", "--max-procs", "--max-chars",
                             "--max-lines", "--replace", "--eof", "--process-slot-var"})


def xargs_command(argv: list) -> "Optional[list]":
    """The words of the command `xargs` runs (its own name first, past the wrappers that
    run it), from xargs's own words; None when `argv` is not an xargs; [] when it runs
    the default `echo`."""
    if not argv or posixpath.basename(argv[0].text) != "xargs":
        return None
    i = 1
    while i < len(argv) and argv[i].text.startswith("-") and argv[i].text != "-":
        t = argv[i].text
        i += 1
        if t == "--":
            break
        if t in _XARGS_ARG_OPTS:
            i += 1
    inner = argv[i:]
    head, _exact, _own = shell_reader.strip_prefix(inner)
    return inner[head:]


# Sentinel: `cwd` was never PASSED at all, vs. passed explicitly as None (a
# real caller with a payload that happens to carry no `cwd` field). The two
# get different fallbacks — see `_cwd_anchor`'s docstring.
_CWD_OMITTED = object()


def _cwd_anchor(cwd) -> Path:
    """The real working directory a relative shell token anchors at when the
    command itself never `cd`s.

    Two callers, two honest anchors — NOT a compromise, because they answer
    different questions:
      - `cwd` OMITTED entirely (bash_write_guard's own config-lane callers,
        `bypass_targets`/`core()`, and every existing unit test that calls
        the parser directly with no execution context at all): anchors at
        `write_guard._root()`, UNCHANGED from before this fix — a relative
        GUARD_LIST pattern is written root-relative by definition, and this
        lane has no real shell cwd to reason about in the first place (a
        telemetry heuristic over command TEXT, not a claim about which file
        actually got written).
      - `cwd` PASSED (even as None — a real caller whose payload happens to
        carry no `cwd` field): anchors at that value if given, else this
        process's OWN `Path.cwd()` — the REAL shell working directory. This
        is gate_stage's artifact-forgery arm: a relative destination must
        resolve exactly the way the shell that ran the command would have
        resolved it, symlinks included, which anchoring at a protected-tree
        ROOT instead of the real cwd got wrong (`cp f lnk/x.json` named a
        different file than the one the shell actually wrote)."""
    if cwd is _CWD_OMITTED:
        return _root()
    if cwd:
        try:
            return Path(cwd).resolve()
        except (OSError, ValueError):
            pass
    try:
        return Path.cwd().resolve()
    except OSError:
        return Path(".")


def _commands(command, cwd) -> list:
    """(the words a command runs, the directories it may run in) for each simple command;
    an `xargs` contributes the command it runs too, its operands written out. A line
    `shell_reader` cannot tokenize reads segment by segment from `cwd`, each directory
    loose: no reading vouches for where the shell stands in it."""
    default_cwd = _cwd_anchor(cwd)
    cmds = shell_reader.read(command, default_cwd)
    if cmds is None:
        out: list = []
        pos = 0
        masked = _mask(command)
        cuts = [(m.start(), m.end()) for m in _CONNECTOR_RE.finditer(masked)] + [(len(command),) * 2]
        for a, b in cuts:
            seg = command[pos:a]
            pos = b
            words = [shell_reader.Word(t, t, False, False) for t in _unquote(seg).split()]
            head, _exact, _own = shell_reader.strip_prefix(words)
            out.append((seg, [w.text for w in words[head:]], [shell_reader.Loose(str(default_cwd))], []))
        return out
    out = []
    for c in cmds:
        argv = [w.text for w in c.argv]
        files = [w.text for op, w in c.redirects
                 if op in (">", ">>", "&>", "&>>", ">|", "<>") or (op == ">&" and not
                                                                   re.fullmatch(r"\d+-?|-", w.text))]
        out.append((c.text, argv, c.cwds, files))
        inner = xargs_command(c.argv)
        if inner:
            out.append(("", [w.text for w in inner], c.cwds, []))
    return out


def shell_targets(command, include_copy_move: bool = False, cwd=_CWD_OMITTED,
                  removals: bool = False) -> "list[tuple[Path, bool]]":
    """(path, vouched) for every filesystem WRITE target named in a Bash command (with
    `removals`, every path it REMOVES instead: rm, unlink, git rm, what mv moves away),
    resolved to an ABSOLUTE, symlink-followed path. A relative token anchors at each
    directory `shell_reader` says the command may run in -- its `cd`s read the way the
    shell runs them -- else at `_cwd_anchor(cwd)`. Under a directory no reading vouches
    for (a `shell_reader.Loose`: `cd "$X"`, a `cd` that may have failed) the token joins
    the literal text the line names it by, and the pair says `vouched` False; what that
    means is each caller's own. Pure + fail-open: junk input → []."""
    if not isinstance(command, str) or not command.strip():
        return []
    out: list = []
    # Clobber-force `>|` is semantically `>`; the reader reads it as one, the redirect
    # regex in `_candidate_targets` needs it spelled `>`.
    command = command.replace(">|", "> ")
    for text, argv, cwds, files in _commands(command, cwd):
        if argv and posixpath.basename(argv[0]) in ("cd", "pushd", "popd"):
            continue
        if removals:
            toks = [t for t in _removal_operands(argv) if t]
        else:
            toks = files + [t for t in _candidate_targets(_mask(text), text) if t]
        pairs = [(shell_reader.join(a, tok), a) for tok in toks for a in cwds]
        if include_copy_move and not removals:
            for dest, srcs, is_dir in _copy_move_targets(argv):
                for a in cwds:
                    base = shell_reader.join(a, dest)
                    try:
                        into = is_dir or (not isinstance(base, shell_reader.Loose) and base.is_dir())
                    except OSError:
                        into = is_dir
                    # into a directory, each source lands under its own name
                    pairs += ([(shell_reader.join(base, Path(x.rstrip("/")).name), a)
                               for x in srcs if x.rstrip("/")] if into else [(base, a)])
        for target, a in pairs:
            vouched = not isinstance(target, shell_reader.Loose)  # an absolute token needs no cwd
            try:
                p = Path(target).resolve(strict=False)
            except (OSError, ValueError):  # noqa: BLE001 — a bad token never breaks detection
                continue
            if (p, vouched) not in out:
                out.append((p, vouched))
    return out


def shell_write_absolute_targets(command, include_copy_move: bool = False,
                                 cwd=_CWD_OMITTED, removals: bool = False) -> list:
    """The paths of `shell_targets`, vouched or not: the ONE resolution step every
    root-relative check shares (`shell_write_targets` below, gate_stage's artifact arm,
    the floor guard). A token under a directory no reading vouches for keeps the literal
    text the line names it by (`$PWD/harness/hooks/x`), so a check that reads a dynamic
    prefix still sees one. Pure + fail-open: junk input → []."""
    out = []
    for p, _vouched in shell_targets(command, include_copy_move, cwd=cwd, removals=removals):
        if p not in out:
            out.append(p)
    return out


def shell_write_targets(command, include_copy_move: bool = False, roots=None,
                        cwd=_CWD_OMITTED, removals: bool = False) -> list:
    """Every filesystem WRITE target named in a Bash command, as POSIX paths
    relative to EVERY root in `roots` it resolves under (deduped, in order —
    never just the first root that matches). The shared parser behind two
    observers: bash_write_guard (config — copy/move EXCLUDED, `roots` omitted
    → defaults to `(_root(),)`, the bin-lane anchor GUARD_LIST's config paths
    live under) and gate_stage's artifact-forgery arm (plan artifacts —
    copy/move INCLUDED, `roots` passed explicitly as
    `protected_trees.protected_working_trees()` — the project artifact
    union). A NESTED worktree (the normal `.claude/worktrees/` layout) sits
    under BOTH the host root and its own worktree root, so a target inside it
    resolves under both — stopping at the first root that matched (the host,
    always listed first) used to hide the match against the target's OWN
    worktree root, the one whose relative form actually looks like a guarded
    artifact path. Every root is tried now; a root that does not contain the
    resolved target simply contributes nothing. Pure + fail-open: junk input
    → []."""
    if not isinstance(command, str) or not command.strip():
        return []
    roots = tuple(roots) if roots else (_root(),)
    out, seen = [], set()
    for resolved in shell_write_absolute_targets(command, include_copy_move, cwd=cwd,
                                                 removals=removals):
        for root in roots:
            try:
                rel = write_guard.rel_target(str(resolved), root)
            except Exception:  # noqa: BLE001 — a bad root/token never breaks detection
                rel = None
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def bypass_targets(command) -> list:
    """List of (rel_path, matched_pattern) for every guarded CONFIG path written
    through the shell (copy/move excluded — the blessed workaround). Pure +
    fail-open: junk input → []."""
    import fnmatch
    patterns = _guard_patterns()
    hits = []
    for rel in shell_write_targets(command, include_copy_move=False):
        pat = next((p for p in patterns if fnmatch.fnmatch(rel.lower(), p.lower())), None)
        if pat:
            hits.append((rel, pat))
    return hits


def core(data: dict) -> None:
    """Telemetry core: on a guarded-path bypass, record it to the usage ledger
    and write ONE advisory line. stderr only — PostToolUse, so it never blocks;
    the command already ran. Records the TARGET + matched pattern only, never the
    raw command (which could carry secrets from an `echo ... > file`)."""
    if data.get("tool_name") != "Bash":
        return  # skip:not-applicable
    cmd = hook_runtime.bash_command(data)
    hits = bypass_targets(cmd)
    if not hits:
        return  # skip:nothing-to-report
    session = data.get("session_id") or os.environ.get("HARNESS_SESSION_ID") or ""
    try:
        from telemetry_paths import append_event
        for rel, pat in hits:
            append_event("hook-telemetry.jsonl", {
                "source": "hook:bash",
                "event": "write_guard_bypass",
                "target": rel,
                "matched": pat,
                "session": session,
            })
    except Exception:  # noqa: BLE001 — telemetry must never break the op
        pass
    targets = ", ".join(rel for rel, _ in hits)
    writers = sorted({write_guard._SANCTIONED_WRITER[pat] for _, pat in hits
                      if pat in write_guard._SANCTIONED_WRITER})
    route = ("Its sanctioned writer: %s." % "; ".join("`%s`" % w for w in writers)
             if writers else "Edit it with a normal editor outside the session.")
    # Default sink is systemMessage (a SECURITY advisory — an escaped write to gate
    # config the human must see); reroutable via nudge-channels.yaml. Route to the
    # SHARED queue (no terminal write here) so both the standalone main() and the
    # dispatcher own the single stdout blob — makes core() a stdout-free in-process
    # callable (no double write).
    hook_runtime.emit_nudge(
        _NAME,
        "[advisory] bash_write_guard: a guarded path was written through the "
        "shell (%s), bypassing write_guard. PostToolUse cannot block — the change "
        "already ran. A tracked path shows as a git diff; the graph snapshot store "
        "is untracked and leaves none. Verify it was intended. %s" % (targets, route),
        session=session, default_channel="systemMessage")


def main(raw=None) -> None:
    """Read stdin, check enabled, run core inside a fail-open guard, then drain a
    queued systemMessage (the escaped-write advisory) into the single terminal blob,
    else a plain continue — mirrors the hand-rolled nudge mains, and lets the
    dispatcher call core() in-process without a double stdout write."""
    data = hook_runtime.read_stdin_json() if raw is None else hook_runtime._parse(raw)
    try:
        if hook_runtime.hook_enabled(_NAME, "telemetry"):
            core(data)
    except Exception as e:  # noqa: BLE001 — telemetry must never break the op
        hook_runtime.log_hook_error(_NAME, e)
    hook_runtime.drain_or_continue()


if __name__ == "__main__":
    main()
