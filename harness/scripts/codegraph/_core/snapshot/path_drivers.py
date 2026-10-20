"""path_drivers — who invokes a module by PATH rather than by import.

The import graph answers "which module names does this file bind". In a tree
whose entry points are scripts, that question misses the call structure: the
caller is a line in a SKILL.md, a hook config or a shell script that spells
`scripts/foo.py`, and no adapter sees it. Measured on a real harness: 119 CLI
modules under one flat root, 79 of them with no production importer, 105 named
by path outside any test. Those are the callers.

The stage lives in the kernel because an edge is what it produces, and an edge
the kernel does not measure does not exist for any consumer downstream — the
same failure the pipeline docstring names for cross-root edges: not red, not
yellow, NONEXISTENT. A move plan built without these moves a file and leaves
every driver pointing at nothing.

TWO SPELLINGS, both resolved here:

    path    `lib/runner.py`, or any longer suffix of the module's own path.
            Trees write the short form; matching only the full repo-relative
            path finds almost nothing. A suffix carried by TWO modules is
            recorded in `ambiguous` and produces NO edge, the rule
            `ambiguous_imports` already sets — guessing invents a caller for
            one module and hides one from the other.

    dotted  `python -m lib.runner`. The only spelling that survives the move to
            a package, so a tree part-way through one has both.

    bare    `- validate_artifacts` in a registry, or `runner` in backticks.
            No directory, no extension. Measured on a real harness: four CLI
            modules were reachable ONLY this way, so a path-only scan called
            them unreferenced. Bare names collide far more than paths do, so
            they are read only where the writer marked them as identifiers --
            a YAML VALUE (the document is parsed, so flow maps and sequences
            are values like any block form), a list item or scalar value in
            the other config kinds, backticks in prose -- and never in Python
            source, where an import already says it and a local variable
            would otherwise count as a reference. A YAML document that does
            not parse falls back to the line rules and the fallback is
            REPORTED (`yaml_fallback`), never silent.

TWO ANSWERS, because they are two questions. `drivers` is every reference: it
is what a move must fix, since a stale citation rots exactly like a stale
command. `invoked` is the subset whose LINE carries an execution context --
an interpreter, a runner, a subprocess call, a command field. Measured on a
real harness: 1433 reference lines, 248 of them invocations. Partitioning a
script tree on the unseparated signal files a module under whoever happened to
document it.

A REFERENCE IS NOT PROOF OF A CALL, and this stage does not claim otherwise.
`python3 scripts/foo.py` and a decision record citing `scripts/foo.py` are the
same token to a scanner, and measured on a real harness the ledger accounted for
247 of 1208 modules that way. For the question this exists to answer -- what
breaks when a file moves -- that is the right set: a stale citation rots exactly
like a stale command. For "is anything still running this", it is an UPPER
bound, and separating the two needs an execution context that the short form
(`scripts/foo.py` alone on a step line) does not carry.

WHAT IT CANNOT SEE
    * A path built at runtime -- a variable holding the WHOLE script name, a
      directory joined from config, interpolation mid-string. A variable
      PREFIX (`$VAR/`, `${VAR}/`, `"$VAR"/`, `$(…)/` and the `../` chain it
      leaves behind) IS seen and stripped: the suffix after it names the module.
    * A caller that invokes through a wrapper (`hs-run <verb>`, a Makefile
      target, a systemd unit outside the repo) names the wrapper, not the
      module; the edge stops at the wrapper.
    * A rename of the FILE is invisible to callers that spell the old path, and
      equally invisible here: after the rename nothing matches, which reads the
      same as "nobody calls it". This stage tells you the edge is gone, never
      that it moved.
    * Files matching `DriverSpec.include` are read by basename pattern, plus
      extension-less launchers detected by their SHEBANG (a binary or a
      shebang-less file without an extension stays unread), and a generated
      inventory is skipped by declaration. All are reported, because a file
      that was never read and a file with no match read identically.
    * Green means no driver was FOUND. It is not "nothing invokes this".
    * A Python comment or docstring naming a module is a citation, never an
      invocation — the tokenizer marks it, so `python3 x.py` written in a
      docstring example stops reading as a call. A STRING LITERAL that
      BUILDS a human-facing suggestion (a print/log message spelling out a
      command for a person to type) is not a docstring and is not caught by
      this: it sits at a normal argument position, so a nearby exec word
      still reads it as a real invocation.
"""
from __future__ import annotations

import io
import re
import subprocess
import tokenize
from pathlib import Path

import yaml_io

from ..descriptor.model import DescriptorError, DriverSpec
from ..git import run_git

#: Printed by consumers that report this stage; the docstring above is the home.
BLIND: tuple[str, ...] = (
    "a path built at runtime beyond a LEADING variable prefix (a variable "
    "holding the whole script name, interpolation mid-string) is not literal "
    "text and is not seen; `$VAR/`, `${VAR}/`, `\"$VAR\"/`, `$(…)/` and the "
    "`../` chain after them ARE stripped and the remainder is resolved",
    "a caller that goes through a wrapper names the wrapper, not the module",
    "a citation and an invocation are the same token here: this is what a move "
    "must fix, and an UPPER bound on what still runs",
    "after a rename nothing matches, which reads identically to 'nobody calls "
    "it' -- this stage never says an edge MOVED",
    "declared file kinds are read by basename pattern, extension-less "
    "launchers by shebang; a shebang-less or binary extension-less file is "
    "not read, and declared generated inventories are skipped -- all "
    "reported rather than assumed",
    "`invoked` is a LOWER bound: a call behind a wrapper, a variable or a "
    "Makefile target carries no execution context on the line, so an empty "
    "`invoked` never means nothing runs this",
)

#: A path-shaped token. Deliberately greedy on directories so the longest
#: spelling in the text is what gets resolved.
_PATH_RE = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+\.py")
_DASH_M_RE = re.compile(r"-m\s+([\w]+(?:\.[\w]+)+)")

#: A variable prefix a shell splices a directory in front of: `$VAR/`,
#: `${VAR}/` (either optionally quoted) and `$(…)/`. The `$` is not a path
#: character, so in the `$VAR/` form the variable's NAME is glued INTO the
#: path token (`$ENGINE/pkg/x.py` reads as `ENGINE/pkg/x.py` to _PATH_RE) —
#: measured on a real launcher, the reference then resolved to nothing.
_VAR_PREFIX_RE = re.compile(
    r"""(?:\$\([^()]*\)|["']?\$\{[^}]+\}["']?|["']?\$[A-Za-z_]\w*["']?)/""")

#: The `../` (and `./`) chains a spliced prefix leaves behind:
#: `$(dirname "$0")/../pkg/x.py` names pkg/x.py after the splice. `..` and
#: `.` are navigation, not directory names anybody owns — stripped only
#: behind a boundary so a name CONTAINING dots stays whole.
_DOTDOT_RE = re.compile(r'(^|[\s"\'`=(])(?:\.\./|\./)+')


def strip_var_prefix(line: str) -> str:
    """One line with the variable prefixes removed from its path tokens.

    Shared with the owners classifier, so the stage that FINDS a reference
    and the law that reads its LINE agree on what the text names — two
    private copies of one spelling rule is how a caller and a gate come to
    disagree about the same line.

    Called once per line of every scanned file, so the two substring guards
    below matter: `_VAR_PREFIX_RE` can only ever match text containing a
    literal `$`, and `_DOTDOT_RE` only text containing `./` (`../` carries
    it too, at its second character) — on a line with neither, both `.sub`
    calls are a guaranteed no-op, and skipping the regex engine entirely for
    that (the common case) is strictly faster than asking it to confirm
    there is nothing to do.
    """
    if "$" in line:
        line = _VAR_PREFIX_RE.sub("", line)
    if "./" in line:
        line = _DOTDOT_RE.sub(r"\1", line)
    return line


def py_prose_lines(text: str) -> "set[int] | None":
    """Comment and docstring line numbers of a Python file, or None when it
    does not tokenize — then the caller falls back to `#` handling only.

    Shared with the owners classifier, for the same reason `strip_var_prefix`
    is: two private copies of "what counts as prose" is how the stage that
    FINDS an invocation and the law that reads its LINE come to disagree.

    A docstring is a STRING token that STARTS a logical line — the previous
    significant token is NEWLINE (a logical boundary), INDENT/DEDENT, or
    nothing. A string literal inside code is NOT prose: the measured false
    positive was `[sys.executable, "release/sbom.py"]`'s docstring cousin —
    a STRING after `)` reads as an argument until NEWLINE is counted as the
    boundary it is.
    """
    prose: set[int] = set()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, ValueError):
        return None
    prev = None
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            prose.add(tok.start[0])
        elif tok.type == tokenize.STRING and prev in (
                None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            for ln in range(tok.start[0], tok.end[0] + 1):
                prose.add(ln)
        # NL (a newline inside brackets) is NOT a logical boundary; NEWLINE is.
        if tok.type not in (tokenize.COMMENT, tokenize.NL,
                            tokenize.INDENT, tokenize.DEDENT):
            prev = tok.type
    return prose

#: File kinds that use `#` for a comment and carry no other prose/code split
#: at this stage: YAML, TOML, cfg/ini, a shell script, and a bare .txt list
#: (`requirements.txt`/`constraints.txt`) or an extension-less launcher.
#: Shared with the owners classifier, for the same reason `strip_var_prefix`
#: and `py_prose_lines` are.
HASH_COMMENT_EXT = (".yaml", ".yml", ".toml", ".cfg", ".ini", ".txt", ".sh", "")


def hash_comment_start(line: str) -> "int | None":
    """Column of the `#` that starts a comment, or None if the line has
    none. A `#` inside a single- or double-quoted string is not a comment
    start -- a quoted value may legitimately carry one. Nor is a `#` GLUED
    to the character before it: a comment marker is preceded by
    whitespace or sits at the start of the line, the same rule that keeps
    a URL's `#fragment` out of a shell or YAML comment.
    """
    if "#" not in line:
        return None
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return i
    return None


def strip_hash_comment(line: str) -> str:
    """`line` with a hash comment (see hash_comment_start) removed. A
    comment-only line strips to nothing, so a reference living ONLY inside
    one is never found; real content before the `#` is left untouched."""
    i = hash_comment_start(line)
    return line if i is None else line[:i]


#: A bare name where a config writer marks one: a list item, or a scalar value.
#: Quoted forms included; anything with a space, a slash or a brace is not a name.
_CFG_BARE_RE = re.compile(r"""^\s*(?:-\s*|[\w.\-]+\s*:\s*)["']?([A-Za-z_]\w*)["']?\s*,?\s*$""")
#: A bare name in prose, marked by the writer with backticks.
_MD_BARE_RE = re.compile(r"`([A-Za-z_]\w*)(?:\.py)?`")

_CFG_EXT = (".yaml", ".yml", ".json", ".toml", ".cfg", ".ini")
_PROSE_EXT = (".md", ".txt")

#: What makes a line an INVOCATION rather than a mention. Read from the text
#: BEFORE the reference: `python3 x.py` counts, `see x.py` does not. The set is
#: CLOSED on purpose — a rule like "any verb" reads an English sentence as a
#: call, which is the failure this field exists to stop.
_EXEC_WORDS = frozenset(
    "bash sh zsh exec source uv pipx poetry run cmd command entrypoint script "
    "args argv popen check_output check_call".split())
#: Delimiters a shell or a config puts between the runner and the path. The
#: quotes matter: `subprocess.run(["python3", "x.py"])` has three of them.
_DELIM_RE = re.compile(r"""[\s"'`\[\](),:=]+""")
#: A flag is stepped OVER, not treated as the runner: `bash -c 'x.py'`.
_FLAG_RE = re.compile(r"^-{1,2}[\w][\w.=-]*$")
#: How far back to look. An execution context sits next to what it runs; a
#: longer reach starts finding verbs in the middle of a sentence.
_EXEC_LOOKBACK = 4


def _is_invocation(prefix: str) -> bool:
    """Does the text before a reference put it in a command position?"""
    toks = [t for t in _DELIM_RE.split(prefix) if t]
    for tok in reversed(toks[-_EXEC_LOOKBACK:]):
        low = tok.lower()
        if (low in _EXEC_WORDS or low.startswith("python")
                or low.startswith("subprocess.") or low.endswith("/")
                or low == "sys.executable"):
            return True
        if _FLAG_RE.match(tok):
            continue          # a flag belongs to the runner, keep walking back
        return False
    return False


def _tracked(repo: Path) -> list[str]:
    """Repo-relative files, from git when it is a checkout, else a walk.

    Untracked files are not callers of anything that ships, and a walk would
    drag in build output; when git is unavailable the walk is the honest
    fallback rather than an empty result.

    `-z` (NUL-separated): plain `ls-files` C-quotes a non-ASCII path under git's
    default `core.quotePath=true`, AND a whitespace `.split()` tore a path
    carrying a space into two bogus fragments -- either way the caller
    (`find_path_drivers`) silently never scanned that file's own text, since
    `(repo / rel).read_text()` on a fragment or a quoted string just raises,
    caught, skipped.
    """
    try:
        raw = run_git(repo, "ls-files", "-z", check=False)
        if raw:
            return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        pass
    return [p.relative_to(repo).as_posix() for p in sorted(repo.rglob("*"))
            if p.is_file() and ".git/" not in p.relative_to(repo).as_posix()]


def _has_shebang(p: Path) -> bool:
    try:
        with open(p, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _wanted(rel: str, include: tuple[str, ...], repo: "Path | None" = None) -> bool:
    """Is this file read? Declared kinds by basename pattern, plus one
    GENERAL addition: a file with no extension whose first line is a shebang
    is a launcher whatever directory it lives in — detected by the `#!`
    itself, never by a `bin/` convention, and a binary or a shebang-less
    file stays unread."""
    name = rel.rsplit("/", 1)[-1]
    if any(Path(name).match(pat) for pat in include):
        return True
    if repo is None or Path(name).suffix:
        return False
    return _has_shebang(repo / rel)


def _bare_index(modules: dict[str, str]) -> dict[str, list[str]]:
    """Module stem -> the modules carrying it. `build` in two places is two."""
    by: dict[str, list[str]] = {}
    for mod, rel in modules.items():
        by.setdefault(rel.rsplit("/", 1)[-1][:-3], []).append(mod)
    return by


#: One bare identifier token, and one whole-path token — the two single-token
#: shapes a YAML VALUE may carry. Anything with a space in it is prose.
_BARE_TOKEN_RE = re.compile(r"^[A-Za-z_]\w*$")
_PATH_TOKEN_RE = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)+\.py$")


#: A `key:` at the start of a config line, block form only — the run-key
#: check for the line-shaped fallback (a document that does not parse, or a
#: non-YAML config kind). A `- value` list item has no key and is never run
#: by this rule alone.
_CFG_KEY_RE = re.compile(r"""^\s*([\w.\-]+)\s*:""")


def _cfg_line_names(text: str, run_keys: frozenset = frozenset()
                    ) -> "tuple[set[str], set[str]]":
    """The line-shaped bare-name rule: `- x` and `key: x`, one value a line.
    Kept as the FALLBACK for a YAML document that does not parse, and as the
    only rule for the other config kinds. `run_keys` promotes a `key: x`
    line (never a bare `- x`, which carries no key) to a run reference the
    same way the parsed path does."""
    names: set[str] = set()
    run: set[str] = set()
    for line in text.splitlines():
        line = strip_hash_comment(line)
        m = _CFG_BARE_RE.match(line)
        if not m:
            continue
        names.add(m.group(1))
        km = _CFG_KEY_RE.match(line)
        if km and km.group(1) in run_keys:
            run.add(m.group(1))
    return names, run


def yaml_value_lines(text: str, run_keys: frozenset = frozenset()
                     ) -> "tuple[dict[str, list[int]], set[str], str | None]":
    """Every scalar at a VALUE position of a YAML document -> its 1-based
    line number(s), plus the subset sitting directly under a declared RUN
    key (a mapping key whose value is an execution context BY DECLARATION,
    not by a nearby exec word).

    The document is PARSED (compose), so flow maps `- {module: x}` and flow
    sequences `[a, x]` are values like any block form — the line-shaped rule
    cannot see into either, and measured on a real dispatcher that silence
    cost fourteen hooks their owner, and a SECOND reader of the same file
    (the owners depended-on law, hunting a line NUMBER) went right on
    missing them even after the first fix, because it still matched only
    the block shape. One parse serves both.

    A run key's value need not be a single token: `command: $HARNESS_PY
    $HARNESS_ROOT/harness/hooks/x.py` is a whole command line, and the path
    inside it is pulled out the same way a raw line is — variable prefix
    stripped, then the path token found — because the KEY already declares
    the value a run context; no exec-word test is needed for it.

    Keys are skipped as values (a key names a field, not a module), comments
    never survive the parser, and a plain (non-run-key) scalar carrying more
    than one token is prose. Aliases are not followed. Returns ({}, set(),
    reason) when the document does not parse.
    """
    try:
        docs = list(yaml_io.compose_all(text))
    except yaml_io.YAMLError as e:
        first = str(e).strip().splitlines()
        why = first[0] if first else "unparsable"
        return {}, set(), f"does not parse — line rules used: {why}"
    lines: dict[str, list[int]] = {}
    run: set[str] = set()

    def add(name: str, ln: int, is_run: bool) -> None:
        lines.setdefault(name, []).append(ln)
        if is_run:
            run.add(name)

    def walk(node, is_value: bool, key: "str | None") -> None:
        if isinstance(node, yaml_io.ScalarNode):
            if not is_value:
                return
            raw = strip_var_prefix(str(node.value)).strip()
            ln = node.start_mark.line + 1
            is_run = key is not None and key in run_keys
            if _BARE_TOKEN_RE.match(raw) or _PATH_TOKEN_RE.match(raw):
                add(raw, ln, is_run)
            elif is_run:
                for m in _PATH_RE.finditer(raw):
                    add(m.group(0), ln, True)
        elif isinstance(node, yaml_io.MappingNode):
            for k, v in node.value:
                kname = k.value if isinstance(k, yaml_io.ScalarNode) else None
                walk(v, True, kname)
        elif isinstance(node, yaml_io.SequenceNode):
            for it in node.value:
                walk(it, True, key)

    for doc in docs:
        walk(doc, True, None)
    for name in lines:
        lines[name] = sorted(set(lines[name]))
    return lines, run, None


def _bare_refs(rel: str, text: str, run_keys: frozenset = frozenset()
              ) -> "tuple[set[str], set[str], str | None]":
    """(names, run-names, fallback-reason) this file marks as identifiers.
    YAML is read as YAML — every VALUE position, flow forms included; a
    document that does not parse falls back to the line rules AND says so,
    because a silent fallback is the same silence this function exists to
    end."""
    if rel.endswith((".yaml", ".yml")):
        lines, run, why = yaml_value_lines(text, run_keys)
        if why is None:
            return set(lines), run, None
        names, run = _cfg_line_names(text, run_keys)
        return names, run, why
    if rel.endswith(_CFG_EXT):
        names, run = _cfg_line_names(text, run_keys)
        return names, run, None
    if rel.endswith(_PROSE_EXT):
        return set(_MD_BARE_RE.findall(text)), set(), None
    return set(), set(), None


def _suffix_index(modules: dict[str, str]) -> dict[str, list[str]]:
    """Every path suffix of every module, mapped to the modules carrying it.

    Suffixes start at two segments: a bare `foo.py` is too weak a claim to call
    an edge, and in a flat tree it would match half the repo's prose.
    """
    by: dict[str, list[str]] = {}
    for mod, rel in modules.items():
        parts = rel.split("/")
        for i in range(len(parts) - 2, -1, -1):
            by.setdefault("/".join(parts[i:]), []).append(mod)
    return by


def find_path_drivers(repo: Path | str, modules: dict[str, str],
                      drivers: DriverSpec | None = None) -> dict:
    """Which files name each module by path or by `-m`, and what was not asked.

    `modules` maps module name -> repo-relative path. Returns `drivers`
    (module -> sorted files), `ambiguous` (a suffix two modules carry, with the
    candidates), `excluded` (declared generated files that exist) and
    `stale_exclusions` (declared ones that do not).
    """
    repo = Path(repo).resolve()
    spec = drivers or DriverSpec()

    unreasoned = [g.path for g in spec.generated if not (g.why or "").strip()]
    if unreasoned:
        raise DescriptorError(
            "`drivers.generated` entries state no reason: %s — a filename on a "
            "list cannot be told apart from one somebody pasted, and the next "
            "reader has no way to know whether it still holds"
            % ", ".join(sorted(unreasoned)))

    skip = {g.path for g in spec.generated}
    own = {rel: mod for mod, rel in modules.items()}
    by_suffix = _suffix_index(modules)
    by_stem = _bare_index(modules)
    run_keys = frozenset(spec.run_keys)

    files = [f for f in _tracked(repo) if _wanted(f, spec.include, repo)]
    present = set(files)

    hits: dict[str, set[str]] = {}
    runs: dict[str, set[str]] = {}
    ambiguous: dict[tuple[str, str], set[str]] = {}
    yaml_fallback: list[dict] = []
    scanned = 0
    for rel in files:
        if rel in skip:
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        # Config values name modules too — YAML read as YAML (flow forms
        # included); a document that does not parse falls back and SAYS so.
        # `run_names` is the subset a declared `drivers.run_keys` promotes to
        # an execution context BY DECLARATION, whatever the nearby words say.
        names, run_names, why = _bare_refs(rel, text, run_keys)
        if why is not None:
            yaml_fallback.append({"path": rel, "why": why})
        # Per LINE: it is the only place an execution context lives, and it is
        # where a variable prefix is stripped — the token `ENGINE/pkg/x.py`
        # the regex sees on `$ENGINE/pkg/x.py` names a directory nobody owns.
        ran: set[str] = {n for n in run_names if n.endswith(".py")}
        refs: set[str] = {n for n in names if n.endswith(".py")}
        dashm_ran: set[str] = set()
        dashm_refs: set[str] = set()
        # A comment or docstring names a module without running it. Python
        # keeps the citation (it is documentation embedded in running code)
        # and drops only the invocation -- the one kind this stage can
        # tokenize, via py_prose_lines. Every other hash-comment kind has no
        # such split here: the text after an unquoted `#` is not a reference
        # at all, citation or invocation -- a comment naming a module in a
        # YAML/txt/sh/toml/cfg/ini file is not documentation someone browses
        # alongside code, it is prose about a file this stage cannot tell
        # apart from a real one otherwise. A string BUILDING a suggestion for
        # a person to type is not prose in either sense and stays invoked,
        # undocumented here on purpose — see WHAT IT CANNOT SEE.
        prose = py_prose_lines(text) if rel.endswith(".py") else None
        hash_fmt = Path(rel).suffix in HASH_COMMENT_EXT
        for lineno, line in enumerate(text.splitlines(), 1):
            line = strip_var_prefix(line)
            if hash_fmt:
                line = strip_hash_comment(line)
            is_prose = prose is not None and lineno in prose
            for m in _PATH_RE.finditer(line):
                refs.add(m.group(0))
                if not is_prose and _is_invocation(line[:m.start()]):
                    ran.add(m.group(0))
            for m in _DASH_M_RE.finditer(line):
                dashm_refs.add(m.group(1))
                if not is_prose:
                    dashm_ran.add(m.group(1))
        for ref in refs:
            carriers = by_suffix.get(ref)
            if not carriers:
                continue
            if len(carriers) > 1:
                ambiguous.setdefault(("path", ref), set()).update(carriers)
                continue
            mod = carriers[0]
            # A module's own usage line is documentation, not a call site.
            if own.get(rel) == mod:
                continue
            hits.setdefault(mod, set()).add(rel)
            if ref in ran:
                runs.setdefault(mod, set()).add(rel)
        for dotted in dashm_refs:
            if dotted in modules and own.get(rel) != dotted:
                hits.setdefault(dotted, set()).add(rel)
                if dotted in dashm_ran:
                    runs.setdefault(dotted, set()).add(rel)
        for stem in (n for n in names if not n.endswith(".py")):
            carriers = by_stem.get(stem)
            if not carriers:
                continue
            if len(carriers) > 1:
                ambiguous.setdefault(("bare", stem), set()).update(carriers)
                continue
            if own.get(rel) != carriers[0]:
                hits.setdefault(carriers[0], set()).add(rel)
                if stem in run_names:
                    runs.setdefault(carriers[0], set()).add(rel)

    return {
        "drivers": {m: sorted(v) for m, v in sorted(hits.items())},
        "invoked": {m: sorted(v) for m, v in sorted(runs.items())},
        "ambiguous": [{"ref": r, "kind": k, "candidates": sorted(c)}
                      for (k, r), c in sorted(ambiguous.items())],
        "yaml_fallback": yaml_fallback,
        "excluded": [{"path": g.path, "why": g.why}
                     for g in sorted(spec.generated, key=lambda g: g.path)
                     if g.path in present],
        "stale_exclusions": sorted(g.path for g in spec.generated
                                   if g.path not in present),
        "files_scanned": scanned,
        "blind": list(BLIND),
    }
