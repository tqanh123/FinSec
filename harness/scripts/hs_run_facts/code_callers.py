"""hs_run_facts.code_callers — the `code.callers` fact provider.

`provide(ctx) -> {value, basis, warning, [not_in_snapshot], [unresolved]}`, the shape
`hs_run_facts.engine.resolve()` calls `entry(ctx)` for. `subjects` are
symbol names a caller names EXPLICITLY -- `module::name`, the Python
spelling `pkg.mod.name`/`pkg.mod.Class.meth`, or a bare name exactly one
definition carries; `value` is keyed by the `module::name` each resolved
to, and every re-spelling is a `warning` line: this
fact's `fact_providers` row declares `subjects: explicit`
(`hs_run_facts.engine._requires_explicit_subjects`), so unlike `code.
importers`/`code.tests_reached` it is never handed a computed default —
a caller list only means something for a symbol a human (or a planner
spawned as one, self-verifying a claim the way `plan/references/
verification-roles.md`'s Contract Verifier already does today by hand) has
actually named.

`value` = `{qualname: [{"caller": qualname, "file": path, "lines": [n,...]}]}`.
`caller`/`file` come straight off `codegraph_change_reader.callers_index`
(a snapshot-level edge — see that function's own docstring for the exact
bare-name ambiguity rule and the outside-tree/lower-bound disclosure it
already carries). `lines` is NOT the caller's own definition span
(`codegraph_change_reader.qualified_index`'s `lines` field) — a real
snapshot never carries a call-SITE line at all (measured live on a real
`hs-code graph snapshot`: `refs` on a symbol is a deduplicated SET of
resolved names, collected via `ast.walk` with no position kept). `lines`
here is found by a SECOND, narrow pass: a whole-word regex scan of the
target's bare name, restricted to the CALLER's own known definition line
range in its own file — bounded by the (small, explicitly named) subject
set. A caller can call the same target more than
once, so `lines` can hold more than one entry; a caller whose own text does
not confirm the call under this scan (an alias, a decorator-wrapped call, a
multi-line call the regex does not match) still appears with an EMPTY
`lines` list rather than being dropped — the snapshot's own `callers` index
already proved the edge exists, so an unmatched scan is a blind spot in the
LINE, never in the fact of the call itself.

For a METHOD subject, every `<receiver>.<name>` use across the snapshot's
measured files (strings and comments blanked first, a receiver on an earlier
line included) is read too: the snapshot never resolves a call through an
object whose type it does not know, so without this a method called only that
way read as having no caller. A use whose receiver's class the file itself
states, named through what the file imports or defines, is decided by that
class (`receiver_types`): one reaching the subject joins `value` under the def
holding it, with the `receivers` that showed it. The rest are `unresolved` =
`{qualname: [{"file", "uses", "lines", "receivers"[, "reaches"]}]}`: name matches,
undecided ones first, then one entry per file and class for a use whose receiver is
exactly another class (`Other()`) reaching its own method, carrying the class it
`reaches`. That is a label, never a deletion: a module elsewhere can rebind the
method. At most `_UNRESOLVED_FILES` entries are listed per subject, each with its
`uses` count and at most `_UNRESOLVED_LINES` lines, and the warning carries the full
counts. An import statement is no use.
"""
from __future__ import annotations

import bisect
import re
from pathlib import Path

import codegraph_change_reader
from hs_run_facts import receiver_types

_BASIS = (
    "one-hop caller-symbol edges from the snapshot's own `callers` index "
    "(`codegraph_change_reader.callers_index`) -- a lower bound: a call "
    "through an instance, getattr, dict dispatch and callbacks do not resolve "
    "into a ref (for a method, a `.name` use whose receiver's class the file "
    "states -- `Cls()`, `self`, `x: Cls`, `x = Cls()`, the class named through the "
    "file's own imports -- is decided by that class's MRO: reaching the subject it is "
    "listed with its `receivers`; every other `.name` use is listed under `unresolved`, "
    "one on a receiver that is exactly another class (`Other()`) labelled with the class "
    "it `reaches`), "
    "a call through a module loaded by path (`importlib.util."
    "spec_from_file_location` + `exec_module`) is no import edge and is never "
    "listed, "
    "and a bare target name shared by two definitions is credited to "
    "neither (ambiguous, never guessed). Call-SITE `lines` come from a "
    "second, narrow text scan restricted to each caller's own known "
    "definition range -- an alias or a decorator-wrapped call can still "
    "miss the scan, in which case that caller is listed with an empty "
    "`lines` list rather than dropped"
)

_WARNING = (
    "code.callers only resolves exact dotted-name refs the AST already "
    "captured (no runtime dispatch, see no-orphan-code.md), and `lines` is "
    "a best-effort text scan, not a second parse -- an empty `lines` list "
    "on a listed caller is still a real caller, just one the scan could "
    "not pin to an exact line"
)


def _strip_comment(line: str) -> str:
    """Code part of a line (a `#` inside a string literal is over-cut — the
    scan is a lower-bound hint, not a parser)."""
    return line.split("#", 1)[0]


def _read(root: Path, path: str) -> "str | None":
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _blanked(root: Path, path: str, cache: "dict | None" = None,
             text: "str | None" = None) -> "str | None":
    """`_code_only` of `path`'s text, tokenized once per call however many subjects
    and callers read it; None when the file cannot be read as UTF-8."""
    if cache is not None and path in cache:
        return cache[path]
    if text is None:
        text = _read(root, path)
    code = None if text is None else _code_only(text)
    if cache is not None:
        cache[path] = code
    return code


def _call_lines(root: Path, caller_path: str, def_lines, bare_name: str,
                cache: "dict | None" = None) -> "list[int]":
    """Whole-word occurrences of `bare_name` inside `caller_path`'s own
    `def_lines` (`[start, end]`, 1-indexed inclusive) — never the whole
    file, and never the target's own definition lines (those live in a
    DIFFERENT file by construction: `def_lines` here is always the
    CALLER's own span). A read failure (file moved/deleted since the
    snapshot was taken) degrades to an empty list — the same fail-open
    contract every other reader in this package holds to."""
    if not def_lines or len(def_lines) != 2:
        return []
    start, end = def_lines
    code = _blanked(root, caller_path, cache)
    if code is None:
        return []
    pat = re.compile(r"(?<![\w])" + re.escape(bare_name) + r"(?![\w])")
    hits: "list[int]" = []
    # The same blanked text and `\n`-only line count `_attribute_uses` reads, so a
    # line both scans name is the same line.
    for n, line in enumerate(code.split("\n"), 1):
        if n < start or n > end:
            continue
        if pat.search(line):
            hits.append(n)
    return hits


#: Per method subject, how many files `unresolved` lists; the warning counts them all.
_UNRESOLVED_FILES = 20
#: Per listed file, how many line numbers and receivers it carries; `uses` counts all.
_UNRESOLVED_LINES = 20
_UNRESOLVED_RECEIVERS = 10


def _blank_types() -> "frozenset[int]":
    import tokenize
    names = ("STRING", "COMMENT", "FSTRING_MIDDLE", "TSTRING_MIDDLE")
    return frozenset(getattr(tokenize, n) for n in names if hasattr(tokenize, n))


def _code_only(text: str) -> str:
    """`text` with string literals (an f-string's literal parts too), comments and
    whole import statements blanked, newlines kept where tokenize counts them (`\n`
    only, so a form feed inside a line moves no line number): a file name such as
    "cases.csv" is no use of `.csv`, nor is `from .csv import x`. A file that does
    not tokenize falls back to cutting comments only."""
    import io
    import tokenize
    lines = text.split("\n")
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text, newline="\n").readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return "\n".join(_strip_comment(l) for l in lines)
    blank = _blank_types()
    out = [list(l) for l in lines]
    spans = []
    # `import` is a hard keyword: wherever it stands, an import statement does. `from`
    # starts one only where a statement can start (line start, after `;`, after the
    # `:` of a one-line `if x: from a import b`), never in `raise E from e`.
    stmt_start, importing = True, None
    for t in toks:
        if t.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            if importing is not None:
                spans.append((importing, t.start))
            stmt_start, importing = True, None
            continue
        if t.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
            if t.type == tokenize.COMMENT:
                spans.append((t.start, t.end))
            continue
        if importing is None and t.type == tokenize.NAME and (
                t.string == "import" or (stmt_start and t.string == "from")):
            importing = t.start
        stmt_start = t.type == tokenize.OP and t.string in (";", ":")
        if t.type == tokenize.OP and t.string == ";" and importing is not None:
            spans.append((importing, t.start))
            importing = None
        if t.type in blank:
            spans.append((t.start, t.end))
    for (r0, c0), (r1, c1) in spans:
        for r in range(r0, r1 + 1):
            if r - 1 >= len(out):
                break
            row = out[r - 1]
            a = min(c0 if r == r0 else 0, len(row))
            b = min(c1 if r == r1 else len(row), len(row))
            if b > a:
                row[a:b] = " " * (b - a)
    return "\n".join("".join(r) for r in out)


def _measured_paths(idx: dict) -> "set[str]":
    """Every module file the snapshot measured, a module with no symbol of its own
    (a script, a `__main__.py`) and a test file no root covers included."""
    return {m["path"] for m in codegraph_change_reader._modules(idx) if m.get("path")}


def _receiver(code: str, end: int) -> str:
    """The receiver whose text ends just before `end`, a call or an index named by
    what it calls or indexes with its arguments dropped: `super()`, `stores[]`,
    `get()[]`, and `()` for a bare parenthesised expression. `code` is blanked of
    strings and comments, so every bracket counted is code."""
    i, suffix = end - 1, ""
    while i >= 0 and code[i] in ")]":
        close = code[i]
        opener, depth = "(" if close == ")" else "[", 0
        while i >= 0:
            if code[i] in ")]":
                depth += 1
            elif code[i] in "([":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < 0 or code[i] != opener:
            return close  # unbalanced: say what was seen, never guess
        suffix = opener + close + suffix
        i -= 1
        while i >= 0:  # a name stands on the same logical line as its bracket
            if code[i] in " \t":
                i -= 1
            elif code[i] == "\n" and i > 0 and code[i - 1] == "\\":
                i -= 2
            else:
                break
    name_end = i + 1
    while i >= 0 and (code[i].isalnum() or code[i] == "_"):
        i -= 1
    return code[i + 1:name_end] + suffix


def _attribute_uses(root: Path, paths, methods: "dict[str, str]",
                    cache: "dict | None" = None) -> "dict[str, list[dict]]":
    """For each method qualname -> its bare name: every `<receiver>.<name>` use in
    the measured files, `[{file, lines, receivers}]` per file, every line, `lines` being where
    the name itself stands (a receiver on the line before still counts). A call
    through an instance never resolves into the snapshot's callers index (the
    receiver's type is unknown), so these are candidates by name: a same-named
    method on another class matches too, which is why they never enter `value`."""
    gap = r"(?:\s|\\\n)*"  # whitespace, newlines, a backslash continuation
    pats = {q: (n, re.compile(r"([\w)\]]+)%s\.%s(%s)(?![\w])" % (gap, gap, re.escape(n))))
            for q, n in methods.items()}
    out: "dict[str, list[dict]]" = {q: [] for q in methods}
    for path in sorted(paths):
        text = _read(root, path)
        if text is None:
            continue
        wanted = [(q, pat) for q, (n, pat) in pats.items() if n in text]
        if not wanted:
            continue  # tokenize only a file that names the method at all
        code = _blanked(root, path, cache, text)
        if code is None:
            continue
        breaks = [i for i, ch in enumerate(code) if ch == "\n"]
        for q, pat in wanted:
            hits: "dict[int, None]" = {}
            receivers = set()
            for m in pat.finditer(code):
                hits[bisect.bisect_left(breaks, m.start(2)) + 1] = None
                receivers.add(_receiver(code, m.end(1)))
            if hits:
                out[q].append({"file": path, "lines": sorted(hits),
                               "receivers": sorted(receivers)[:_UNRESOLVED_RECEIVERS]})
    return out


def _credit(entries: list, path: str, line: int, found_at: list) -> None:
    """Add a use resolved by its receiver's class to `entries`, under the def holding it."""
    caller = found_at[0][3]
    receivers = sorted({r for _v, _c, r, _w in found_at})
    for e in entries:
        if e["caller"] == caller:
            e["lines"] = sorted(set(e["lines"]) | {line})
            e["receivers"] = sorted(set(e.get("receivers") or ()) | set(receivers))
            return
    entries.append({"caller": caller, "file": path, "lines": [line], "receivers": receivers})


def _qualify(subject: str, defined: dict, modules: "set[str]",
             by_bare: "dict[str, list[str]]") -> "tuple[str | None, list[str]]":
    """`(module::name, [])` for a subject spelled `module::name`, `pkg.mod.name`
    / `pkg.mod.Class.meth` (longest measured module prefix) or a bare name
    one definition carries; `(None, candidates)` for a bare name several
    carry; `(None, [])` for nothing at all."""
    if "::" in subject:
        return (subject if subject in defined else None), []
    parts = subject.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        if mod in modules:
            q = "%s::%s" % (mod, ".".join(parts[i:]))
            return (q if q in defined else None), []
    hits = by_bare.get(subject, [])
    return (hits[0], []) if len(hits) == 1 else (None, sorted(hits))


def _other_language(idx: dict, subjects: "list[str]") -> "dict[str, str]":
    """subject -> the language of its module, for a subject naming a module of a
    language other than Python (callers are read from Python references only)."""
    langs = {m["module"]: m["lang"] for ms in (idx.get("units") or {}).values() for m in ms
             if m.get("lang")}
    return {s: langs[s.partition("::")[0]] for s in subjects if s.partition("::")[0] in langs}


def provide(ctx: dict) -> dict:
    idx = ctx["snapshot"]
    root = Path(ctx["root"])
    asked = sorted(set(ctx.get("subjects") or ()))
    foreign = _other_language(idx, asked)
    if foreign and len(foreign) == len(asked):
        return {"unavailable": True,
                "reason": "language_unsupported: code.callers reads python only; %s"
                          % ", ".join("%s is %s" % kv for kv in sorted(foreign.items()))}
    subjects = [s for s in asked if s not in foreign]

    defined = codegraph_change_reader.qualified_index(idx)
    callers, _outside, _ambiguous = codegraph_change_reader.callers_index(idx)
    modules = {info["module"] for info in defined.values()}
    by_bare: "dict[str, list[str]]" = {}
    for qual in defined:
        by_bare.setdefault(qual.partition("::")[2], []).append(qual)

    value: "dict[str, list[dict]]" = {}
    cache: "dict[str, str | None]" = {}
    not_in_snapshot: "list[str]" = []
    notes: "list[str]" = []
    for subject in subjects:
        q, shared = _qualify(subject, defined, modules, by_bare)
        if q is None:
            not_in_snapshot.append(subject)
            if shared:
                notes.append("%s names %d definitions: %s -- pass one as module::name"
                             % (subject, len(shared), ", ".join(shared)))
            continue
        if q != subject:
            notes.append("%s -> %s" % (subject, q))
        bare = q.rpartition("::")[2].rpartition(".")[2]  # a method is called by its own name
        entries = []
        for caller_q in callers.get(q, ()):
            info = defined.get(caller_q)
            if info is None:
                continue
            lines = _call_lines(root, info["path"], info.get("lines"), bare, cache)
            entries.append({"caller": caller_q, "file": info["path"], "lines": lines})
        value[q] = entries

    methods = {q: q.rpartition(".")[2] for q in value if defined[q].get("parent")}
    unresolved: "dict[str, list[dict]]" = {}
    if methods:
        uses = _attribute_uses(root, _measured_paths(idx), methods, cache)
        table = receiver_types.ClassTable(idx, root)
        for q, found in uses.items():
            seen = {(e["file"], n) for e in value[q] for n in e["lines"]}
            kept: "list[dict]" = []
            elsewhere: "list[dict]" = []
            cut: "dict[int, int]" = {}  # id(entry) -> how many lines it had
            typed = 0
            other: "dict[str, int]" = {}
            for u in found:
                left = [n for n in u["lines"] if (u["file"], n) not in seen]
                if not left:
                    continue
                said = receiver_types.classify_file(table, u["file"], None, q)
                open_lines = []
                reach: "dict[str, list]" = {}
                for n in left:
                    found_at = said.get(n) or [(None, None, None, None)]
                    verdicts = {v for v, _c, _r, _w in found_at}
                    if verdicts == {"this"}:
                        typed += len(found_at)
                        _credit(value[q], u["file"], n, found_at)
                    elif verdicts == {"other"}:
                        for _v, cls, r, _w in found_at:
                            other[cls] = other.get(cls, 0) + 1
                            reach.setdefault(cls, []).append((n, r))
                    else:
                        open_lines.append(n)
                if open_lines:
                    kept.append(dict(u, uses=len(open_lines),
                                     lines=open_lines[:_UNRESOLVED_LINES]))
                    cut[id(kept[-1])] = len(open_lines)
                # A label, never a deletion: a module elsewhere can rebind the method.
                for cls, at in sorted(reach.items()):
                    lines = sorted({n for n, _r in at})
                    elsewhere.append({"file": u["file"], "uses": len(at), "reaches": cls,
                                      "lines": lines[:_UNRESOLVED_LINES],
                                      "receivers": sorted({r for _n, r in at})[
                                          :_UNRESOLVED_RECEIVERS]})
                    cut[id(elsewhere[-1])] = len(lines)
            unresolved[q] = (kept + elsewhere)[:_UNRESOLVED_FILES]
            n_uses = sum(u["uses"] for u in kept)
            bare = methods[q]
            listed = ("listed under `unresolved`" if len(kept) <= _UNRESOLVED_FILES else
                      "the first %d files listed under `unresolved`" % _UNRESOLVED_FILES)
            note = ("%s: %d resolved caller(s)" % (q, len(value[q]))
                    + (" (%d use(s) by a receiver of its class)" % typed if typed else ""))
            if other:
                note += ("; %d use(s) on a receiver that is exactly another class, listed "
                         "after them under `unresolved` with what each `reaches`: %s") % (
                    sum(other.values()), ", ".join(
                        "%s (%d)" % (c, k) for c, k in sorted(other.items(),
                                                              key=lambda kv: (-kv[1], kv[0]))))
            if n_uses:
                note += "; %d attribute use(s) of `.%s` in %d file(s), %s (receiver types " \
                        "not evident)" % (n_uses, bare, len(kept), listed)
            elif typed or other:
                note += "; no other `.%s` use in the measured files" % bare
            else:
                note += ", and no `.%s` use in the measured files either" % bare
            if other and len(kept) + len(elsewhere) > _UNRESOLVED_FILES:
                note += "; `unresolved` lists the first %d of %d entries, undecided ones " \
                        "first" % (_UNRESOLVED_FILES, len(kept) + len(elsewhere))
            short = [(len(u["lines"]), cut[id(u)]) for u in unresolved[q]
                     if cut[id(u)] > len(u["lines"])]
            if short:
                note += "; %d entries list %d of their %d lines" % (
                    len(short), sum(a for a, _b in short), sum(b for _a, b in short))
            notes.append(note)

    basis = "%s. Index: %d target(s), %d caller edge(s)" % (
        _BASIS, len(callers), sum(len(v) for v in callers.values()))
    if foreign:
        notes.append("language_unsupported (python only): %s"
                     % ", ".join("%s is %s" % kv for kv in sorted(foreign.items())))
    warning = _WARNING + ("; subjects: " + "; ".join(notes) if notes else "")
    out = {"value": value, "basis": basis, "warning": warning}
    if foreign:
        out["language_unsupported"] = sorted(foreign)
    if not_in_snapshot:
        out["not_in_snapshot"] = not_in_snapshot
    if unresolved:
        out["unresolved"] = unresolved
    return out
