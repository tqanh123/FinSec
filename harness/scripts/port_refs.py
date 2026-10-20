"""port_refs.py — the reference-rewrite engine `hs-code port tests-land`
calls to repoint a landed test file's old-package imports/strings to their
new-tree homes, as one FLAT module under `harness/scripts/` (sibling of
`port_tests_plan.py`).

Port of ob-tools' `portkit.rewrite.refs` (pinned commit `60075cb`,
`packages/portkit/src/portkit/rewrite/refs.py`, 598 lines) — ONLY the part
upstream `tests_land.py:72` imports (`Report, Resolver, load_index,
rewrite_python`) plus their own callees (no-orphan, judged per function):
`Index`, `_split_target`, every `Resolver` method (`dominant_package`/
`module`/`_door`/`binds`/`symbol`/`dotted`/`file_of`/`path` — each one
reachable from `rewrite_python` through `rewrite_text`), `_import_edits`,
`_docstring_starts`, `rewrite_text`, `_common_prefix` — and `load_born`/
`text_prose`, which `hs-code refactor rewrite-refs` calls. LEFT BEHIND on
purpose from THIS module: `run`/`add_arguments`/`_iter_files` (refs.py's
own CLI) live in the verb that owns them, `hs-code refactor rewrite-refs`
(`codegraph/refactor/rewrite_refs.py`), which also calls `load_born` and
`text_prose` here.

NO HARD-CODED PACKAGE OR NAMESPACE: upstream's `OLD = "gsm_agent"` module
constant (and the three module-level regexes built from it) is replaced
here by a `package: str` PARAMETER every function/regex-builder takes,
read off `.port/port.yaml sources[].package` by the caller — same rule
`port_tests_plan.rewrite_patch`'s own docstring states. `Resolver.module`'s
own `twin = "ob." + old[...]` fallback (upstream hard-codes the NEW port's
namespace root as a second literal, `"ob"`) and `file_of`'s `packages/*/src`
glob are replaced by the target's own `codemap.yaml` descriptor, a
constructor parameter: `namespace_root` for the twin, and the dist layout
door (`codegraph._core.layout.dist_import_root`) for where a new-tree module
lives (`module_files`).

`load_index` READS THROUGH THE LEDGER DOOR: upstream's own `load_index`
opens the ledger file itself and swallows a broken line (`except
ValueError: continue`, refs.py:96-103) — silently building an `Index`
missing whatever that line said. This port's `load_index` takes a `Ledger`
already loaded through `port_ledger_writer.Ledger.load_checked` (raises
`BrokenLedgerError`, naming every broken line, BEFORE anything here runs)
— never a second, more forgiving parse of the same file. It keeps
upstream's own "last line per node wins" fold (a `dict` overwritten in
file order), just over `ledger.events` instead of a freshly re-opened file.

WHAT IT CANNOT SEE: unchanged from upstream (see each function's own
docstring below) — a bare `import <old>.x` with no alias, `getattr`
dispatch, `monkeypatch.setattr` strings, and a symbol the ledger never saw
are still invisible; only the door reading the ledger, and the two
literals, changed.
"""
from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from codegraph._core import layout as _layout
from port_ledger_writer import Ledger


@dataclass
class Index:
    """The measured old -> new table, built from the ledger."""

    sym: "dict[tuple[str, str], set[tuple[str, str]]]" = field(default_factory=lambda: defaultdict(set))
    dropped: "dict[tuple[str, str], str]" = field(default_factory=dict)
    home: "dict[str, Counter]" = field(default_factory=lambda: defaultdict(Counter))
    #: `home` plus the destinations of METHOD rows: every target module a
    #: symbol of the old module landed in — where its FILE's code now lives
    #: (a class whose methods the port split out is homed in each of them).
    #: `home` stays class-level: it answers what an import can name.
    places: "dict[str, Counter]" = field(default_factory=lambda: defaultdict(Counter))
    old_modules: "set[str]" = field(default_factory=set)

    def module_home(self, old: str) -> "tuple[str | None, list[str]]":
        c = self.home.get(old)
        if not c:
            return None, []
        homes = sorted(c)
        return (homes[0] if len(homes) == 1 else None), homes

    def module_places(self, old: str) -> "list[str]":
        return sorted(self.places.get(old) or ())

    def is_package(self, old: str) -> bool:
        return any(m.startswith(old + ".") for m in self.old_modules)


def _split_target(t: str) -> "tuple[str, str] | None":
    """`new@ab12::ob.x.y::Name` / `ob.x.y::Name` -> (module, name)."""
    parts = t.split("::")
    if len(parts) < 2:
        return None
    mod, name = parts[-2], parts[-1]
    return mod, name.split(".")[0]


def load_index(ledger: Ledger, package: str, keys: "list[str] | None" = None) -> Index:
    """The measured old -> new table, from an ALREADY-loaded, ALREADY-
    checked `Ledger` (`Ledger.load_checked` — see the module docstring for
    why this never re-opens or re-parses the file). `last[node] = ev`
    matches upstream's own "last line per node wins" fold, just replayed
    over `ledger.events` (file order) instead of a second read of the
    path."""
    last: "dict[str, Any]" = {}
    for ev in ledger.events:
        node = ev.node
        if keys and node.split("::", 1)[0] not in keys:
            continue
        last[node] = ev
    ix = Index()
    for node, ev in last.items():
        parts = node.split("::")
        if len(parts) != 3 or not parts[1].startswith(package):
            continue
        old_mod, name = parts[1], parts[2]
        ix.old_modules.add(old_mod)
        method = "." in name                  # the class row carries the import home
        if ev.status == "dropped" and not method:
            ix.dropped[(old_mod, name)] = ev.extra.get("reason") or ""
            continue
        if ev.status != "ported":
            continue
        for t in ev.extra.get("to") or []:
            r = _split_target(t)
            if r:
                ix.places[old_mod][r[0]] += 1
                if not method:
                    ix.sym[(old_mod, name)].add(r)
                    ix.home[old_mod][r[0]] += 1
    return ix


def load_born(ix: Index, born: Path, package: str) -> "list[str]":
    """Modules the port BORE (no ledger row): `{old module: new module}` JSON, or one `old new`
    pair per line. Each becomes a whole-module home, so `from old import X` resolves to `new::X`;
    a key `old.module::Name` routes ONE symbol (a dedupe move) to `new.module` or
    `new.module::NewName`. Returns the problems (a malformed line / an old name outside
    `package`). Ported from `refs.py:load_born`, the package a parameter."""
    text = born.read_text(encoding="utf-8")
    try:
        pairs = json.loads(text)
        if not isinstance(pairs, dict):
            raise ValueError("not an object")
    except ValueError:
        pairs = {}
        for i, ln in enumerate(text.splitlines(), 1):
            ln = ln.split("#", 1)[0].strip()
            if not ln:
                continue
            parts = ln.replace("=", " ").replace("->", " ").split()
            if len(parts) != 2:
                return ["%s:%d: want `old.module new.module`, got %r" % (born, i, ln)]
            pairs[parts[0]] = parts[1]
    problems = []
    for old, new in pairs.items():
        mod, sep, name = old.partition("::")
        if not mod.startswith(package + ".") or not isinstance(new, str) or not new \
                or new.startswith(package) or (sep and not name.isidentifier()):
            problems.append("%s: bad pair %r -> %r (old must be `%s.x` or `%s.x::Name`, new the "
                            "port module or `module::Name`)" % (born, old, new, package, package))
            continue
        ix.old_modules.add(mod)
        if sep:                                    # one symbol a dedupe moved: a ledger-like row
            nmod, _, nname = new.partition("::")
            ix.sym[(mod, name)].add((nmod, nname or name))
        else:
            ix.home[mod][new] += 1
    return problems


def module_files(repo: Path, descriptor: Any, mod: str) -> "tuple[list[Path], list[Path]]":
    """`(<mod>.py hits, <mod>/__init__.py hits)` under every dist's import root
    the target's layout declares (`<packages_dir>/<dist>[/<src_subdir>]`),
    each sorted, absolute."""
    rel = mod.replace(".", "/")
    packages = repo / descriptor.packages_dir
    dists = sorted(p.name for p in packages.iterdir() if p.is_dir()) if packages.is_dir() else []
    roots = [repo / _layout.dist_import_root(descriptor, d) for d in dists]
    return ([f for r in roots for f in [r / f"{rel}.py"] if f.is_file()],
            [f for r in roots for f in [r / rel / "__init__.py"] if f.is_file()])


class Resolver:
    def __init__(self, ix: Index, repo: Path, package: str, descriptor: Any):
        self.ix = ix
        self.repo = repo
        self.package = package
        self.descriptor = descriptor
        self.namespace_root = descriptor.namespace_root
        self._files: "dict[str, Path | None]" = {}
        self._text: "dict[str, str]" = {}
        self.unknown: Counter = Counter()      # old module the ledger never saw -> refs

    # -- module / symbol ------------------------------------------------------------------
    def dominant_package(self, old: str) -> "str | None":
        """The new package that holds most ported symbols of the old package `old` (prose refs only)."""
        c: Counter = Counter()
        for m, homes in self.ix.home.items():
            if m == old or m.startswith(old + "."):
                for h, n in homes.items():
                    c[h.rsplit(".", 1)[0] if "." in h else h] += n
        c = Counter({k: v for k, v in c.items() if "." in k})
        return c.most_common(1)[0][0] if c else None

    def module(self, old: str, loose: bool = False) -> "tuple[str | None, str]":
        """(new module, why-not). `loose` (prose / directory refs, never code) accepts the dominant
        home of a package the port spread over several dists."""
        new, homes = self.ix.module_home(old)
        if new:
            return new, ""
        if loose and self.ix.is_package(old):
            dom = self.dominant_package(old)
            if dom and self.file_of(dom):
                return dom, ""
        if not homes:
            if self.ix.is_package(old):
                sub = {h for m in self.ix.old_modules if m.startswith(old + ".")
                      for h in (self.ix.module_home(m)[0],) if h is not None}
                pref = _common_prefix(sorted(sub))
                if pref:
                    return pref, ""
            twin = (self.namespace_root + "." + old[len(self.package) + 1:]
                    if old.startswith(self.package + ".") else "")
            if twin and self.file_of(twin):          # a module the ledger never saw, same path in the new tree
                return twin, ""
            self.unknown[old] += 1
            return None, "unknown old module"
        door = self._door(old, homes)
        if door:
            return door, ""
        return None, "several homes: " + ", ".join(homes)

    def _door(self, old: str, homes: "list[str]") -> "str | None":
        """A module the port split over several homes keeps ONE door: the home named like the old
        module (`<package>.planner` -> `<namespace_root>.planner.planner`'s package
        `<namespace_root>.planner`), else the package every home shares when that package is
        itself a module. Measured on the tree."""
        leaf = old.rsplit(".", 1)[-1]
        same = [h for h in homes if h.rsplit(".", 1)[-1] == leaf]
        if len(same) == 1:
            return same[0] if self.file_of(same[0]) else same[0].rsplit(".", 1)[0]
        pre = _common_prefix(homes)
        if pre and self.file_of(pre) and pre.rsplit(".", 1)[-1] == leaf:
            return pre
        counts = self.ix.home[old].most_common()
        total = sum(n for _, n in counts)
        if len(counts) > 1 and counts[0][1] >= 0.9 * total and total - counts[0][1] <= 2:
            return counts[0][0]                       # one module holds all but a stray symbol or two
        return None

    def binds(self, mod: str, name: str) -> bool:
        """Does the port module's file mention `name`? Unknown file = cannot say = True."""
        f = self.file_of(mod)
        if f is None:
            return True
        key = str(f)
        if key not in self._text:
            self._text[key] = (self.repo / f).read_text(encoding="utf-8", errors="replace")
        return re.search(rf"\b{re.escape(name)}\b", self._text[key]) is not None

    def symbol(self, old_mod: str, name: str) -> "tuple[tuple[str, str] | None, str]":
        homes = self.ix.sym.get((old_mod, name))
        if homes and len(homes) == 1:
            return next(iter(homes)), ""
        if homes:
            return None, f"{name} split over " + ", ".join(sorted({h[0] for h in homes}))
        if (old_mod, name) in self.ix.dropped:
            return None, "dropped"
        new, why = self.module(old_mod)
        if new:
            if not self.binds(new, name):          # the home the module resolves to never heard of the name
                return None, f"{name} is not defined in {new} (moved? hand it in with --born {old_mod}::{name})"
            return (new, name), ""
        return None, why

    def dotted(self, dotted: str) -> "tuple[str | None, str]":
        """`<package>.a.b.C.m` -> `<namespace_root>.x.b.C.m` (module + symbol + attribute tail)."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            mod = ".".join(parts[:i])
            if mod not in self.ix.old_modules and not self.ix.is_package(mod):
                continue
            rest = parts[i:]
            if not rest:
                new, why = self.module(mod)
                return (new, "") if new else (None, why)
            # a module (attribute path) whose next part is a submodule?
            if f"{mod}.{rest[0]}" in self.ix.old_modules or self.ix.is_package(f"{mod}.{rest[0]}"):
                continue
            if mod in self.ix.old_modules:
                sym, why = self.symbol(mod, rest[0])
                if sym is None:
                    return None, why
                nmod, nname = sym
                tail = ".".join([nname] + rest[1:])
                return f"{nmod}.{tail}", ""
            new, why = self.module(mod)
            if new:
                return ".".join([new] + rest), ""
            return None, why
        self.unknown[".".join(parts[:2])] += 1
        return None, "unknown old module"

    # -- paths ----------------------------------------------------------------------------
    def file_of(self, mod: str) -> "Path | None":
        if mod in self._files:
            return self._files[mod]
        py, init = module_files(self.repo, self.descriptor, mod)
        hits = py or init
        out = hits[0].relative_to(self.repo) if len(hits) == 1 else None
        self._files[mod] = out
        return out

    def path(self, rest: str) -> "tuple[str | None, str]":
        """`ops_loop/cli.py` / `ops_loop/` / `` (the root) -> repo-relative new path"""
        rest = rest.strip("/")
        if not rest:
            return None, "the bare package root has no single home"
        is_dir = False
        p = rest
        if p.endswith(".py"):
            p = p[:-3]
        else:
            is_dir = True
        old = self.package + "." + p.replace("/", ".")
        if p.endswith("/__init__"):
            old = old[: -len(".__init__")]
        new, why = self.module(old, loose=is_dir)
        if not new:
            return None, why
        f = self.file_of(new)
        if f is None:
            return None, f"no file for {new}"
        if is_dir:
            return str(f.parent) + "/", ""
        return str(f), ""


def _common_prefix(mods: "list[str]") -> "str | None":
    if not mods:
        return None
    pre = mods[0].split(".")
    for m in mods[1:]:
        cur = m.split(".")
        n = 0
        while n < min(len(pre), len(cur)) and pre[n] == cur[n]:
            n += 1
        pre = pre[:n]
    return ".".join(pre) if len(pre) >= 2 else None


# -------------------------------------------------------------------------------------------
@dataclass
class Report:
    counts: Counter = field(default_factory=Counter)
    unresolved: "list[dict]" = field(default_factory=list)
    dropped: "list[dict]" = field(default_factory=list)

    def miss(self, f: str, line: int, kind: str, text: str, why: str) -> None:
        d = {"file": f, "line": line, "kind": kind, "text": text.strip()[:140], "why": why}
        (self.dropped if why == "dropped" else self.unresolved).append(d)


@dataclass(frozen=True)
class _Regexes:
    dotted: "re.Pattern"
    path: "re.Pattern"
    bare: "re.Pattern"


@lru_cache(maxsize=None)
def _compiled(package: str) -> _Regexes:
    """The three module-level regexes upstream builds once from its literal
    `OLD`, built here once per `package` string instead (cached: `rewrite_text`
    is called per string/comment token, never re-compiling per call)."""
    esc = re.escape(package)
    dotted = re.compile(rf"(?<![\w./-]){esc}((?:\.\w+)+)(?P<attr>:\w+)?")
    path = re.compile(rf"(?<![\w.-])(?P<src>(?:[\w./-]*?/)?src/)?{esc}/(?P<rest>(?:[\w.-]+/)*[\w.-]*)")
    bare = re.compile(rf"(?:(?<![\w./-])|(?<=src/)){esc}(?![\w/:]|\.\w)")
    return _Regexes(dotted, path, bare)


def rewrite_text(text: str, rs: Resolver, rep: Report, fname: str, package: str, line0: int = 1,
                 hist: "str | None" = None, prose=lambda line: False) -> str:
    """Substitute dotted refs and paths in free text (a string token, a comment, a text file).
    `hist` + `prose(line)`: a ref that resolves nowhere, on a PROSE line, is a statement about the
    old world; it is kept but its package name becomes `hist` (never in code, never guessed)."""
    regexes = _compiled(package)

    def dotted(m: "re.Match") -> str:
        whole = package + m.group(1)
        new, why = rs.dotted(whole)
        line = line0 + text.count("\n", 0, m.start())
        if new is None:
            if hist and prose(line):
                rep.counts["historical"] += 1
                return m.group(0).replace(package, hist, 1)
            rep.miss(fname, line, "dotted", m.group(0), why)
            return m.group(0)
        rep.counts["dotted"] += 1
        return new + (m.group("attr") or "")

    def path(m: "re.Match") -> str:
        new, why = rs.path(m.group("rest"))
        line = line0 + text.count("\n", 0, m.start())
        if new is None:
            if hist and prose(line):
                rep.counts["historical"] += 1
                return m.group(0).replace(package, hist, 1)
            rep.miss(fname, line, "path", m.group(0), why)
            return m.group(0)
        rep.counts["path"] += 1
        return new

    out = regexes.path.sub(path, text)
    out = regexes.dotted.sub(dotted, out)

    def bare(m: "re.Match") -> str:
        line = line0 + out.count("\n", 0, m.start())
        if hist and prose(line):
            rep.counts["historical"] += 1
            return hist
        rep.miss(fname, line, "bare", m.group(0), "bare package name")
        return m.group(0)

    out = regexes.bare.sub(bare, out)
    return out


# -- python ----------------------------------------------------------------------------------
def _import_edits(tree: "ast.Module", src_lines: "list[str]", rs: Resolver, rep: Report, fname: str,
                  package: str) -> "list[tuple[int, int, str]]":
    edits: "list[tuple[int, int, str]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and \
                (node.module == package or node.module.startswith(package + ".")):
            indent = " " * node.col_offset
            groups: "dict[str, list[str]]" = {}
            keep: "list[ast.alias]" = []
            extra: "list[str]" = []
            for a in node.names:
                if a.name == "*":
                    rep.miss(fname, node.lineno, "star", ast.get_source_segment("\n".join(src_lines), node) or "", "star import")
                    keep.append(a)
                    continue
                as_mod = f"{node.module}.{a.name}"
                is_sub = as_mod in rs.ix.old_modules or rs.ix.is_package(as_mod)
                has_row = (node.module, a.name) in rs.ix.sym or (node.module, a.name) in rs.ix.dropped
                sym, why = rs.symbol(node.module, a.name) if node.module != package and not (is_sub and not has_row) \
                    else (None, "")
                if sym is None and is_sub:
                    new, why2 = rs.module(as_mod)
                    if new:
                        extra.append(f"{indent}import {new} as {a.asname or a.name}")
                        rep.counts["import-module"] += 1
                        continue
                    why = why2
                if sym is None:
                    rep.miss(fname, node.lineno, "import", f"from {node.module} import {a.name}", why)
                    keep.append(a)
                    continue
                nmod, nname = sym
                local = a.asname or a.name
                spelled = local if nname == local else f"{nname} as {local}"
                groups.setdefault(nmod, []).append(spelled)
                rep.counts["import"] += 1
            new_lines = [f"{indent}from {m} import {', '.join(n)}" for m, n in sorted(groups.items())]
            new_lines += extra
            if keep:
                new_lines.append(f"{indent}from {node.module} import "
                                 + ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in keep))
            if len(new_lines) == 0:
                continue
            edits.append((node.lineno, node.end_lineno or node.lineno, "\n".join(new_lines)))
        elif isinstance(node, ast.Import):
            hit = [a for a in node.names if a.name == package or a.name.startswith(package + ".")]
            if not hit:
                continue
            indent = " " * node.col_offset
            out_names: "list[str]" = []
            for a in node.names:
                if a in hit and a.asname:
                    new, why = rs.dotted(a.name)
                    if new is None:
                        rep.miss(fname, node.lineno, "import", f"import {a.name} as {a.asname}", why)
                        out_names.append(f"{a.name} as {a.asname}")
                    else:
                        rep.counts["import"] += 1
                        out_names.append(f"{new} as {a.asname}")
                else:
                    if a in hit:
                        rep.miss(fname, node.lineno, "code-name", f"import {a.name}", "dotted name used unaliased")
                    out_names.append(a.name + (f" as {a.asname}" if a.asname else ""))
            edits.append((node.lineno, node.end_lineno or node.lineno, f"{indent}import {', '.join(out_names)}"))
    return edits


def _docstring_starts(tree: "ast.Module") -> "set[tuple[int, int]]":
    """(row, col) of every docstring literal: prose by construction, unlike any other string."""
    out: "set[tuple[int, int]]" = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            b = n.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str):
                out.add((b.value.lineno, b.value.col_offset))
    return out


_PROSE_SUFFIXES = {".md", ".rst", ".txt", ".example"}


def text_prose(suffix: str, text: str):
    """Which lines of a non-Python file are prose: all of a document, only the `#` comments
    of data (`refs.py:_text_prose`)."""
    if suffix in _PROSE_SUFFIXES:
        return lambda line: True
    lines = text.split("\n")
    return lambda line: 0 < line <= len(lines) and lines[line - 1].lstrip().startswith("#")


def rewrite_python(src: str, rs: Resolver, rep: Report, fname: str, package: str,
                   hist: "str | None") -> str:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        rep.miss(fname, 0, "syntax", "", "file does not parse")
        return src
    lines = src.split("\n")
    for s, e, new in sorted(_import_edits(tree, lines, rs, rep, fname, package), reverse=True):
        lines[s - 1:e] = new.split("\n")
    src = "\n".join(lines)
    # strings / comments / f-string middles, then leftovers
    edits: "list[tuple[tuple[int, int], tuple[int, int], str]]" = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError):
        return src
    docstrings = _docstring_starts(tree)
    for t in toks:
        if t.type in (tokenize.STRING, tokenize.COMMENT, getattr(tokenize, "FSTRING_MIDDLE", -1)) and package in t.string:
            is_prose = t.type == tokenize.COMMENT or (t.type == tokenize.STRING and t.start in docstrings)
            new = rewrite_text(t.string, rs, rep, fname, package, t.start[0], hist, lambda _l, p=is_prose: p)
            if new != t.string:
                edits.append((t.start, t.end, new))
        elif t.type == tokenize.NAME and t.string == package:
            rep.miss(fname, t.start[0], "code-name", t.line, "NAME token in code")
    if not edits:
        return src
    ls = src.split("\n")
    for (sr, sc), (er, ec), new in sorted(edits, reverse=True):
        head, tail = ls[sr - 1][:sc], ls[er - 1][ec:]
        ls[sr - 1:er] = (head + new + tail).split("\n")
    return "\n".join(ls)
