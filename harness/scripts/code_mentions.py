"""code_mentions — every match in the tracked tree, each classified by where it sits.

Flat (outside the `codegraph` package) because `hs_run_facts` reads it and that
family's import closure never reaches `codegraph`; `codegraph._core.query.
mentions` rebinds these names and adds the bounded user-regex door.

A string count is not a code count: a name in a docstring is not a call, a key
in YAML is not an import. Each match is one row, typed at the position it
starts:

    .py           tokens + AST -> `code` | `docstring` | `comment` | `string`,
                  with the innermost enclosing def/class (`module::Class.meth`)
    YAML/TOML/JSON -> `data`, `context` = the key path above the line
    Markdown       -> `doc`, `context` = heading path, `fenced` = inside ```
    other text     -> `text`

Reads the WORKING TREE of the files git tracks (`git ls-files`): an untracked
file is not the project's yet. Binary files (a NUL byte in the first 8 KiB) are
skipped; an SVG is XML text, so its labels match as `text`. A line longer than the
snippet shows a window around the match, not its head.

WHAT IT CANNOT SEE
    * The key path of YAML/TOML/JSON is read line by line (indent, `[table]`,
      brace depth), not by a full parse: a flow mapping on one line or a
      multi-line string names the nearest key, not the exact one.
    * A `.py` file that does not tokenize/parse degrades to `text` with a note.
    * f-string interpolations read as `string` on interpreters older than 3.12
      (one token there); 3.12+ tokenizes the expression as `code`.
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path
from typing import Callable, Iterable

import process_group
import secret_scan_before_ship as secret_scan


KINDS = ("code", "docstring", "comment", "string", "data", "doc", "text")
_DATA_SUFFIXES = {".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json"}
_DOC_SUFFIXES = {".md", ".markdown"}
_MAX_BYTES = 4 * 1024 * 1024


def tracked_files(repo: Path) -> "list[str]":
    try:
        proc = process_group.run(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), "ls-files", "-z"],
            capture_output=True, timeout=60, env=secret_scan.git_safe_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValueError("grep: git ls-files failed: %s" % type(e).__name__) from None
    if proc.returncode != 0:
        raise ValueError("grep: not a git checkout: %s" % repo)
    return sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})


def _read_text(path: Path) -> "str | None":
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def _line_starts(text: str) -> "list[int]":
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _pos(starts: "list[int]", offset: int) -> "tuple[int, int]":
    """(1-based line, 0-based col) of a character offset."""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, offset - starts[lo]


# ── .py ─────────────────────────────────────────────────────────────────────

class _PyFile:
    """Token spans and def/class extents of one parsed file."""

    def __init__(self, text: str, module: "str | None"):
        tree = ast.parse(text)
        starts = _line_starts(text)
        doc_starts: "set[tuple[int, int]]" = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    doc_starts.add((first.lineno, first.col_offset))
        self.spans: "list[tuple[int, int, str]]" = []   # (start, end, kind) char offsets
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                kind = "comment"
            elif tok.type == tokenize.STRING or tok.type in _FSTRING_TEXT:
                kind = "docstring" if (tok.start[0], tok.start[1]) in doc_starts else "string"
            else:
                continue
            a = starts[tok.start[0] - 1] + tok.start[1]
            b = starts[tok.end[0] - 1] + tok.end[1]
            self.spans.append((a, b, kind))
        self.scopes: "list[tuple[int, int, str]]" = []  # (first, last line, qualname)

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + child.name
                    self.scopes.append((child.lineno, child.end_lineno or child.lineno, name))
                    walk(child, name + ".")
                else:
                    walk(child, prefix)

        walk(tree, "")
        self.module = module

    def kind_at(self, offset: int) -> str:
        for a, b, kind in self.spans:
            if a <= offset < b:
                return kind
        return "code"

    def enclosing(self, line: int) -> "str | None":
        best = None
        for first, last, name in self.scopes:
            if first <= line <= last and (best is None or first >= best[0]):
                best = (first, name)
        if best is None:
            return None
        return "%s::%s" % (self.module, best[1]) if self.module else best[1]


_FSTRING_TEXT = {getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
                 if hasattr(tokenize, n)}


# ── structured data / docs: a context per line ──────────────────────────────

_YAML_KEY = re.compile(r"""^(\s*)(?:-\s+)?(?:"([^"]+)"|'([^']+)'|([^\s:#'"][^:#]*?))\s*:(?:\s|$)""")
_TOML_TABLE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*$")
_TOML_KEY = re.compile(r"""^\s*("[^"]+"|'[^']+'|[A-Za-z0-9_.\-]+)\s*=""")
_JSON_KEY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:')
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")


_YAML_BLOCK = re.compile(r":\s*[|>][+-]?\d*\s*(?:#.*)?$")  # value opens on the next line


def _yaml_contexts(lines: "list[str]") -> "list[str]":
    stack: "list[tuple[int, str]]" = []
    block = -1   # indent of the key whose `|`/`>` scalar is open; its lines are its value
    out = []
    for line in lines:
        indent_now = len(line) - len(line.lstrip())
        if block >= 0 and (not line.strip() or indent_now > block):
            out.append(".".join(k for _, k in stack))
            continue
        block = -1
        m = _YAML_KEY.match(line)
        if m and not line.lstrip().startswith("#"):
            indent = len(m.group(1)) + (2 if line.lstrip().startswith("-") else 0)
            key = (m.group(2) or m.group(3) or m.group(4)).strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            value = line[m.end():].split(" #", 1)[0].strip()
            if value and not value.startswith(("&", "!")) or _YAML_BLOCK.search(line):
                # A key holding a value on its own line has no children: every deeper
                # line is that value (`|`/`>`, a wrapped quoted or plain scalar).
                block = indent
        out.append(".".join(k for _, k in stack))
    return out


def _toml_contexts(lines: "list[str]") -> "list[str]":
    table = ""
    out = []
    for line in lines:
        t = _TOML_TABLE.match(line)
        if t:
            table = t.group(1).replace('"', "").replace("'", "")
            out.append(table)
            continue
        k = _TOML_KEY.match(line)
        key = k.group(1).strip("\"'") if k else ""
        out.append(".".join(x for x in (table, key) if x))
    return out


def _json_contexts(lines: "list[str]") -> "list[str]":
    stack: "list[str | None]" = []   # one entry per open {/[: its key, or None
    out = []
    for line in lines:
        keys = _JSON_KEY.findall(line)
        here = [k for k in stack if k] + keys[-1:]
        out.append(".".join(here))
        pending = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"':
                j = i + 1
                while j < len(line) and line[j] != '"':
                    j += 2 if line[j] == "\\" else 1
                rest = line[j + 1:].lstrip()
                if rest.startswith(":"):
                    pending = line[i + 1:j]
                i = j + 1
                continue
            if ch in "{[":
                stack.append(pending)
                pending = None
            elif ch in "}]":
                if stack:
                    stack.pop()
            i += 1
    return out


def _md_contexts(lines: "list[str]") -> "list[tuple[str, bool]]":
    heads: "list[tuple[int, str]]" = []
    fenced = False
    out = []
    for line in lines:
        if _MD_FENCE.match(line):
            out.append((" > ".join(h for _, h in heads), True))
            fenced = not fenced
            continue
        if not fenced:
            h = _MD_HEADING.match(line)
            if h:
                level = len(h.group(1))
                while heads and heads[-1][0] >= level:
                    heads.pop()
                heads.append((level, h.group(2)))
        out.append((" > ".join(x for _, x in heads), fenced))
    return out


# ── the scan ────────────────────────────────────────────────────────────────

def scan_with(repo: Path, find: "Callable[[str, str], list]", *, kinds: "Iterable[str] | None" = None,
              module_of: "Callable[[str], str | None]" = lambda p: None,
              files: "Iterable[str] | None" = None) -> "tuple[list[dict], list[str]]":
    """`(hits, notes)`: one row per match, file then position order. `find(rel, text)`
    returns the matches (objects with `.start()`/`.group(0)`) in one file -- the
    caller owns the pattern and its bounds. `module_of(path)` names a `.py` file's
    module so `enclosing` reads `module::Class.meth`. A tracked symlink is skipped
    and named in a note, so a `CLAUDE.md -> AGENTS.md` pair counts once."""
    want = set(kinds) if kinds else set(KINDS)
    unknown = want - set(KINDS)
    if unknown:
        raise ValueError("unknown kind(s) %s (have: %s)" % (sorted(unknown), ", ".join(KINDS)))
    repo = Path(repo)
    hits: "list[dict]" = []
    notes: "list[str]" = []
    links: "list[str]" = []
    for rel in (sorted(files) if files is not None else tracked_files(repo)):
        if (repo / rel).is_symlink():   # git holds a link, not a file: its target is read where it lives
            links.append(rel)
            continue
        text = _read_text(repo / rel)
        if text is None:
            continue
        matches = find(rel, text)
        if matches:
            hits.extend(h for h in _classify(rel, text, matches, module_of, notes) if h["kind"] in want)
    if links:
        notes.append("%d tracked symlink(s) skipped -- git holds the link, not a copy of its target: %s%s"
                     % (len(links), ", ".join(links[:5]), " ..." if len(links) > 5 else ""))
    return hits, notes


_SNIPPET = 200


def _snippet(line: str, col: int) -> str:
    """The line, stripped, when it fits in `_SNIPPET` characters; else a window of the
    line around `col`, `…` marking each cut -- a one-line SVG or minified JSON would
    otherwise show its first 200 characters and never the match."""
    if len(line.strip()) <= _SNIPPET:
        return line.strip()
    start = max(0, col - _SNIPPET // 3)
    end = start + _SNIPPET
    return (("…" if start else "") + line[start:end].strip()
            + ("…" if end < len(line.rstrip()) else ""))


def _classify(rel: str, text: str, matches: list, module_of, notes: "list[str]") -> "list[dict]":
    starts = _line_starts(text)
    lines = text.split("\n")
    suffix = Path(rel).suffix.lower()
    py = None
    if suffix == ".py":
        try:
            py = _PyFile(text, module_of(rel))
        except (SyntaxError, tokenize.TokenError, ValueError) as e:
            notes.append("%s: does not parse (%s) -- matches read as text" % (rel, type(e).__name__))
    data_ctx = None
    flavor = _DATA_SUFFIXES.get(suffix)
    if flavor is None and not suffix and text.lstrip()[:1] in ("{", "["):
        flavor = "json"   # a suffixless JSON file (`.test_durations`)
    if flavor is not None:
        data_ctx = {"yaml": _yaml_contexts, "toml": _toml_contexts, "json": _json_contexts}[flavor](lines)
    md_ctx = _md_contexts(lines) if suffix in _DOC_SUFFIXES else None
    out = []
    for m in matches:
        line, col = _pos(starts, m.start())
        row = {"file": rel, "line": line, "col": col, "match": m.group(0)[:200],
               "snippet": _snippet(lines[line - 1], col), "kind": "text",
               "context": None, "enclosing": None}
        if py is not None:
            row["kind"] = py.kind_at(m.start())
            row["enclosing"] = py.enclosing(line)
        elif data_ctx is not None:
            row["kind"], row["context"] = "data", data_ctx[line - 1] or None
        elif md_ctx is not None:
            ctx, fenced = md_ctx[line - 1]
            row["kind"], row["context"], row["fenced"] = "doc", ctx or None, fenced
        out.append(row)
    return out


