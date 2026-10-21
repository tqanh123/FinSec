"""The `resolve.alias` table of a Vite or webpack config, read without running it.

A package directory's `vite.config.{ts,mts,js,mjs,cjs}` and
`webpack.config.{js,cjs,mjs,ts}` are parsed with the tree-sitter grammar and the alias
value is evaluated by a CLOSED set of forms: a string, a template whose only
substitutions are `__dirname`/`import.meta.dirname`, `+` of those, `path.resolve`/
`path.join` (the binding must be imported from `path`/`node:path`, by import or
`require`), `fileURLToPath(new URL(lit, import.meta.url))` and `new URL(...).pathname`,
`__dirname`, `import.meta.dirname`, `process.cwd()` (read as the config's directory, the
entry flagged `assumed_cwd`). The config object is found under `export default`/
`module.exports`, through `defineConfig(...)`, `satisfies`/`as`, an arrow returning an
object, or one top-level `const` naming it.

What the evaluator cannot read is `opaque`, never guessed: a known key whose value is
not in the set is an `opaque` entry (`opaque_alias_target` for what it matches); a key
it cannot read -- a spread, a computed key, a variable `find`, a RegExp outside the safe
subset -- or a config whose shape it cannot follow makes the whole table
`opaque_scope` (an undeclared bare specifier there may be an alias: never called
`undeclared`).

Matching follows the tool: Vite/rollup `spec == key` or `spec.startswith(key + "/")`, a
RegExp `find` by search; webpack the same for a plain key, `key$` exact, one `*`
wildcard, a `false` value ignored, an array value a fallback list. Entries are tried in
declaration order. A RegExp runs only through `regex_bound` with the snapshot's scan
budget; a pattern with a quantified group that itself quantifies or alternates
(`(a+)+`, `(a|a)*`) is opaque at translation, before anything runs.

A replacement string is read as the tool reads it: an absolute path the evaluator
computed (or any absolute literal under webpack) is a filesystem path, kept when it is
inside the repo; a literal `/x` under Vite is the config root's `x`; `./x`/`../x` is
used as written, relative to the importing file; anything else is a bare specifier.

A table applies only to the files of the package whose directory holds the config: a
root config's aliases do not reach a sub-package (which config a bundler runs for a
file is not knowable statically).
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import regex

from ... import regex_bound

VITE = ("vite.config.ts", "vite.config.mts", "vite.config.js", "vite.config.mjs", "vite.config.cjs")
WEBPACK = ("webpack.config.js", "webpack.config.cjs", "webpack.config.mjs", "webpack.config.ts")
_PATH_MODULES = ("path", "node:path")
_URL_MODULES = ("url", "node:url")
_QUANT = "*+?{"
_REPEAT = "*+{"


@dataclass(frozen=True)
class Value:
    text: str
    computed: bool = False      # an absolute path the evaluator computed


@dataclass(frozen=True)
class Entry:
    find: str
    how: str                    # rollup | prefix | exact | wildcard | regex
    values: "tuple[Value, ...]" = ()
    state: str = "ok"           # ok | opaque | ignored
    flags: "tuple[str, ...]" = ()
    pattern: Any = None


@dataclass(frozen=True)
class AliasTable:
    path: str                   # the config file, repo-relative
    tool: str                   # vite | webpack
    scope: str                  # the package directory it applies to
    entries: "tuple[Entry, ...]" = ()
    opaque_scope: bool = False
    notes: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class Match:
    entry: Entry
    state: str                  # ok | opaque | ignored | timeout
    rewrites: "tuple[str, ...]" = ()


class _Opaque(Exception):
    pass


# ------------------------------------------------------------------ RegExp

def _nested_quantifier(src: str) -> bool:
    """A group that holds a quantifier or an alternation, itself repeated."""
    stack: "list[bool]" = []
    i, n, in_class = 0, len(src), False
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            stack.append(False)
        elif c == ")":
            risky = stack.pop() if stack else False
            if i + 1 < n and src[i + 1] in _REPEAT and risky:
                return True
            if stack:
                stack[-1] = stack[-1] or risky
        elif c in _QUANT or c == "|":
            if stack:
                stack[-1] = True
        i += 1
    return False


def translate_regex(source: str, flags: str) -> Any:
    """A JavaScript RegExp literal as a bounded `regex` pattern; raises `_Opaque` outside
    the subset both engines read alike."""
    if set(flags) - set("igus") or _nested_quantifier(source):
        raise _Opaque
    if any(tok in source for tok in ("(?<", "\\k", "\\p", "\\P", "\\u{", "\\c", "[^]")) or \
            regex.search(r"\\[1-9]", source):
        raise _Opaque
    f = (regex.IGNORECASE if "i" in flags else 0) | (regex.DOTALL if "s" in flags else 0)
    try:
        return regex_bound.compile_bounded(source, f)
    except regex.error:
        raise _Opaque from None


# ------------------------------------------------------------------ the syntax tree

class IRNode:
    """A node of the bridge's `alias_ir` answer, read like a tree-sitter node: the same
    `type` names and fields, for the subset the evaluator below reads."""

    def __init__(self, d: "dict[str, Any]", named: bool = True):
        self.type = str(d.get("t", "other"))
        self.text = str(d.get("x", "")).encode("utf-8")
        self.is_named = named
        self._fields = {k: IRNode(v) for k, v in (d.get("f") or {}).items() if v}
        self.named_children = [IRNode(c) for c in d.get("c") or [] if c]
        self.children = [IRNode({"t": t, "x": t}, False) for t in d.get("k") or []] + self.named_children

    def child_by_field_name(self, name: str) -> "IRNode | None":
        return self._fields.get(name)


def _tree_sitter_root(rel: str, text: str) -> Any:
    from .treesitter.reader import grammar_for, parser_for
    return parser_for(grammar_for(rel)).parse(text.encode("utf-8")).root_node


# ------------------------------------------------------------------ evaluator

def _text(n: Any) -> str:
    return n.text.decode("utf-8", "replace")


def _str(n: Any) -> str:
    out = []
    for c in n.children:
        if c.type == "string_fragment":
            out.append(_text(c))
        elif c.type == "escape_sequence":
            t = _text(c)
            out.append(t[1:] if len(t) == 2 else t)
    return "".join(out)


def _unwrap(n: Any) -> Any:
    while n is not None and n.type in ("parenthesized_expression", "satisfies_expression", "as_expression",
                                       "non_null_expression"):
        n = n.named_children[0] if n.named_children else None
    return n


def _key(n: Any) -> str:
    if n.type in ("property_identifier", "identifier", "number"):
        return _text(n)
    if n.type == "string":
        return _str(n)
    raise _Opaque


class _Eval:
    def __init__(self, root: Any, config_dir_abs: str):
        self.dir = config_dir_abs
        self.flags: "set[str]" = set()
        self.path_mod: "set[str]" = set()          # names bound to the path module
        self.path_fn: "dict[str, str]" = {}         # local -> resolve | join
        self.url_fn: "set[str]" = set()             # names bound to fileURLToPath
        self.url_mod: "set[str]" = set()
        self.consts: "dict[str, Any]" = {}
        self._bindings(root)

    def _bindings(self, root: Any) -> None:
        for st in root.named_children:
            if st.type == "import_statement":
                src = st.child_by_field_name("source")
                mod = _str(src) if src is not None else ""
                clause = next((c for c in st.named_children if c.type == "import_clause"), None)
                for c in clause.named_children if clause is not None else ():
                    if c.type in ("identifier", "namespace_import"):
                        self._module(mod, _text(c) if c.type == "identifier" else _text(c.named_children[-1]))
                    elif c.type == "named_imports":
                        for s in c.named_children:
                            imported = _text(s.child_by_field_name("name"))
                            alias = s.child_by_field_name("alias")
                            local = _text(alias) if alias is not None else imported
                            if mod in _PATH_MODULES and imported in ("resolve", "join"):
                                self.path_fn[local] = imported
                            elif mod in _URL_MODULES and imported == "fileURLToPath":
                                self.url_fn.add(local)
            elif st.type in ("lexical_declaration", "variable_declaration"):
                for d in (c for c in st.named_children if c.type == "variable_declarator"):
                    name, value = d.child_by_field_name("name"), _unwrap(d.child_by_field_name("value"))
                    if name is None or value is None:
                        continue
                    req = self._required(value)
                    if req is not None and name.type == "identifier":
                        self._module(req, _text(name))
                    elif req is not None and name.type == "object_pattern":
                        for p in name.named_children:
                            local = _text(p) if p.type == "shorthand_property_identifier_pattern" else ""
                            if local and req in _PATH_MODULES and local in ("resolve", "join"):
                                self.path_fn[local] = local
                            elif local and req in _URL_MODULES and local == "fileURLToPath":
                                self.url_fn.add(local)
                    elif name.type == "identifier":
                        self.consts[_text(name)] = value

    def _module(self, mod: str, local: str) -> None:
        if mod in _PATH_MODULES:
            self.path_mod.add(local)
        elif mod in _URL_MODULES:
            self.url_mod.add(local)

    @staticmethod
    def _required(n: Any) -> "str | None":
        if n.type == "call_expression" and _text(n.child_by_field_name("function")) == "require":
            args = [a for a in n.child_by_field_name("arguments").named_children if a.type != "comment"]
            if len(args) == 1 and args[0].type == "string":
                return _str(args[0])
        return None

    # ---- values
    def value(self, n: Any) -> "tuple[str, bool]":
        """(string, computed-absolute) of an expression in the closed set."""
        n = _unwrap(n)
        if n is None:
            raise _Opaque
        t = n.type
        if t == "string":
            return _str(n), False
        if t == "template_string":
            out, computed = [], False
            for c in n.children:
                if c.type == "string_fragment":
                    out.append(_text(c))
                elif c.type == "escape_sequence":
                    out.append(_text(c)[1:])
                elif c.type == "template_substitution":
                    v, comp = self.value(c.named_children[0]) if c.named_children else ("", False)
                    out.append(v)
                    computed = computed or comp
            return "".join(out), computed
        if t == "binary_expression" and _text(n.child_by_field_name("operator")) == "+":
            (a, ca), (b, _cb) = self.value(n.child_by_field_name("left")), self.value(n.child_by_field_name("right"))
            return a + b, ca
        if t == "identifier" and _text(n) == "__dirname":
            return self.dir, True
        if t == "member_expression":
            obj, prop = n.child_by_field_name("object"), _text(n.child_by_field_name("property"))
            if obj.type == "meta_property" and prop == "dirname":
                return self.dir, True
            if obj.type == "new_expression" and prop == "pathname":
                return self._url(obj), True
            raise _Opaque
        if t == "call_expression":
            return self._call(n)
        raise _Opaque

    def _url(self, new: Any) -> str:
        ctor = new.child_by_field_name("constructor")
        args = [a for a in (new.child_by_field_name("arguments").named_children
                            if new.child_by_field_name("arguments") is not None else ()) if a.type != "comment"]
        if ctor is None or _text(ctor) != "URL" or len(args) != 2 or _text(args[1]) != "import.meta.url":
            raise _Opaque
        lit, comp = self.value(args[0])
        if comp:
            raise _Opaque
        return posixpath.normpath(posixpath.join(self.dir, lit))

    def _call(self, n: Any) -> "tuple[str, bool]":
        fn = n.child_by_field_name("function")
        args = [a for a in n.child_by_field_name("arguments").named_children if a.type != "comment"]
        op = None
        if fn.type == "member_expression":
            obj, prop = fn.child_by_field_name("object"), _text(fn.child_by_field_name("property"))
            if obj.type == "identifier" and _text(obj) in self.path_mod and prop in ("resolve", "join"):
                op = prop
            elif obj.type == "identifier" and _text(obj) in self.url_mod and prop == "fileURLToPath":
                op = "url"
            elif _text(fn) == "process.cwd" and not args:
                self.flags.add("assumed_cwd")
                return self.dir, True
        elif fn.type == "identifier":
            op = self.path_fn.get(_text(fn)) or ("url" if _text(fn) in self.url_fn else None)
        if op == "url":
            if len(args) != 1 or _unwrap(args[0]).type != "new_expression":
                raise _Opaque
            return self._url(_unwrap(args[0])), True
        if op is None:
            raise _Opaque
        parts = [self.value(a)[0] for a in args]
        if op == "join":
            joined = posixpath.normpath(posixpath.join(*parts)) if parts else "."
            if not posixpath.isabs(joined):
                self.flags.add("assumed_cwd")
                joined = posixpath.normpath(posixpath.join(self.dir, joined))
            return joined, True
        out = ""
        for p in parts:
            out = p if posixpath.isabs(p) else posixpath.join(out, p)
        if not posixpath.isabs(out):
            self.flags.add("assumed_cwd")
            out = posixpath.join(self.dir, out)
        return posixpath.normpath(out), True

    # ---- the config object
    def config_object(self, root: Any) -> Any:
        for st in root.named_children:
            if st.type == "export_statement" and any(not c.is_named and _text(c) == "default"
                                                     for c in st.children):
                return self._object(st.child_by_field_name("value") or st.named_children[-1], 0)
            if st.type == "expression_statement" and st.named_children and \
                    st.named_children[0].type == "assignment_expression":
                a = st.named_children[0]
                if _text(a.child_by_field_name("left")) == "module.exports":
                    return self._object(a.child_by_field_name("right"), 0)
        return None

    def _object(self, n: Any, depth: int) -> Any:
        n = _unwrap(n)
        if n is None or depth > 4:
            raise _Opaque
        if n.type == "object":
            return n
        if n.type == "call_expression" and _text(n.child_by_field_name("function")) == "defineConfig":
            args = [a for a in n.child_by_field_name("arguments").named_children if a.type != "comment"]
            if len(args) == 1:
                return self._object(args[0], depth + 1)
        if n.type in ("arrow_function", "function_expression"):
            body = n.child_by_field_name("body")
            if body is not None and body.type != "statement_block":
                return self._object(body, depth + 1)
        if n.type == "identifier" and _text(n) in self.consts:
            return self._object(self.consts[_text(n)], depth + 1)
        raise _Opaque


def _prop(obj: Any, name: str) -> Any:
    """The value of key `name` in an object literal; `_Opaque` when a spread or a
    computed key before it could have set it."""
    found = None
    for c in obj.named_children:
        if c.type in ("spread_element",) or (c.type == "pair" and c.child_by_field_name("key").type
                                             == "computed_property_name"):
            found = _Opaque
        elif c.type == "pair" and _key(c.child_by_field_name("key")) == name:
            found = c.child_by_field_name("value")
        elif c.type == "shorthand_property_identifier" and _text(c) == name:
            found = _Opaque
    if found is _Opaque:
        raise _Opaque
    return found


def _webpack_how(key: str) -> "tuple[str, str]":
    if key.endswith("$"):
        return key[:-1], "exact"
    if key.count("*") == 1:
        return key, "wildcard"
    return key, "prefix"


def _entries(ev: _Eval, alias: Any, tool: str) -> "tuple[list[Entry], bool]":
    alias = _unwrap(alias)
    out: "list[Entry]" = []
    opaque_scope = False

    def one(find: str, how: str, val: Any, pattern: Any = None) -> None:
        ev.flags.clear()
        v = _unwrap(val)
        try:
            if tool == "webpack" and v is not None and v.type == "false":
                out.append(Entry(find, how, (), "ignored", pattern=pattern))
                return
            if tool == "webpack" and v is not None and v.type == "array":
                vals = tuple(Value(*ev.value(x)) for x in v.named_children)
            else:
                vals = (Value(*ev.value(v)),)
            out.append(Entry(find, how, vals, "ok", tuple(sorted(ev.flags)), pattern))
        except _Opaque:
            out.append(Entry(find, how, (), "opaque", pattern=pattern))

    if alias is not None and alias.type == "object":
        for c in alias.named_children:
            if c.type != "pair" or c.child_by_field_name("key").type == "computed_property_name":
                opaque_scope = True
                continue
            key = _key(c.child_by_field_name("key"))
            find, how = _webpack_how(key) if tool == "webpack" else (key, "rollup")
            one(find, how, c.child_by_field_name("value"))
    elif alias is not None and alias.type == "array":
        # vite/rollup: [{find, replacement}]; webpack 5: [{name, alias, onlyModule}]
        keys = ("find", "replacement") if tool == "vite" else ("name", "alias")
        for el in alias.named_children:
            el = _unwrap(el)
            if el is None or el.type != "object":
                opaque_scope = True
                continue
            try:
                f, r = _unwrap(_prop(el, keys[0])), _prop(el, keys[1])
                if f is None or r is None:
                    raise _Opaque
                if f.type == "regex" and tool == "vite":
                    flags = f.child_by_field_name("flags")
                    pat = translate_regex(_text(f.child_by_field_name("pattern")),
                                          _text(flags) if flags is not None else "")
                    one(_text(f), "regex", r, pat)
                elif tool == "vite":
                    one(ev.value(f)[0], "rollup", r)
                else:
                    only = _prop(el, "onlyModule")
                    name = ev.value(f)[0]
                    one(name, "exact" if only is not None and _unwrap(only).type == "true"
                        else _webpack_how(name)[1], r)
            except _Opaque:
                opaque_scope = True
    else:
        opaque_scope = True
    return out, opaque_scope


def read_table(repo: Path, rel: str, parse: "Callable[[str, str], Any] | None" = None) -> AliasTable:
    """The alias table of the config file `rel` (repo-relative); `parse(rel, text)` gives
    its syntax tree (the tree-sitter grammar's, or the bridge's `IRNode`)."""
    tool = "vite" if posixpath.basename(rel).startswith("vite.") else "webpack"
    scope = posixpath.dirname(rel) or "."
    try:
        text = (repo / rel).read_text(encoding="utf-8")
    except OSError as e:
        return AliasTable(rel, tool, scope, opaque_scope=True, notes=("alias_config_unreadable: %s (%s)" % (rel, e),))
    root = (parse or _tree_sitter_root)(rel, text)
    ev = _Eval(root, posixpath.normpath(posixpath.join(Path(repo).resolve().as_posix(), scope)))
    try:
        cfg = ev.config_object(root)
        if cfg is None:
            raise _Opaque
        res = _prop(cfg, "resolve")
        res = _unwrap(res) if res is not None else None
        if res is None:
            return AliasTable(rel, tool, scope)
        if res.type != "object":
            raise _Opaque
        alias = _prop(res, "alias")
    except _Opaque:
        return AliasTable(rel, tool, scope, opaque_scope=True)
    if alias is None:
        return AliasTable(rel, tool, scope)
    entries, opaque = _entries(ev, alias, tool)
    return AliasTable(rel, tool, scope, tuple(entries), opaque)


def config_files(files: "Iterable[str] | frozenset[str]", package_dirs: "Iterable[str]") -> "list[str]":
    """The bundler configs present in each package directory (repo-relative)."""
    have = files if isinstance(files, (set, frozenset)) else set(files)
    out = []
    for d in dict.fromkeys(package_dirs):
        for name in VITE + WEBPACK:
            rel = posixpath.normpath(posixpath.join(d, name))
            if rel in have:
                out.append(rel)
    return out


def load(repo: Path, files: "frozenset[str]", package_dirs: "Iterable[str]",
         parse: "Callable[[str, str], Any] | None" = None) -> "dict[str, tuple[AliasTable, ...]]":
    """{package dir: its tables}."""
    out: "dict[str, list[AliasTable]]" = {}
    for rel in config_files(files, package_dirs):
        t = read_table(repo, rel, parse)
        out.setdefault(t.scope, []).append(t)
    return {k: tuple(v) for k, v in out.items()}


# ------------------------------------------------------------------ matching

def _expand(template: str, m: Any) -> str:
    out, i = [], 0
    while i < len(template):
        c = template[i]
        if c == "$" and i + 1 < len(template):
            nxt = template[i + 1]
            if nxt == "$":
                out.append("$")
                i += 2
                continue
            if nxt == "&":
                out.append(m.group(0))
                i += 2
                continue
            if nxt.isdigit():
                j = i + 1
                while j < len(template) and template[j].isdigit() and int(template[i + 1:j + 1]) <= len(m.groups()):
                    j += 1
                if j > i + 1:
                    out.append(m.group(int(template[i + 1:j])) or "")
                    i = j
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def lookup(table: AliasTable, spec: str, budget: "regex_bound.ScanBudget") -> "Match | None":
    """The first entry of `table` that `spec` matches, with the specifier(s) it becomes."""
    for e in table.entries:
        if e.how == "regex":
            try:
                m = regex_bound.bounded_search(e.pattern, spec, budget)
            except (TimeoutError, regex_bound.PatternBudgetExceeded):
                return Match(e, "timeout")
            if m is None:
                continue
            if e.state != "ok":
                return Match(e, e.state)
            return Match(e, "ok", tuple(spec[:m.start()] + _expand(v.text, m) + spec[m.end():] for v in e.values))
        if e.how == "exact":
            hit, rest = spec == e.find, ""
        elif e.how == "wildcard":
            pre, post = e.find.split("*", 1)
            hit = spec.startswith(pre) and spec.endswith(post) and len(spec) >= len(pre) + len(post)
            if hit and e.state == "ok":
                star = spec[len(pre):len(spec) - len(post)]
                return Match(e, "ok", tuple(v.text.replace("*", star, 1) for v in e.values))
            rest = ""
        else:
            hit = spec == e.find or spec.startswith(e.find + "/")
            rest = spec[len(e.find):]
        if not hit:
            continue
        if e.state != "ok":
            return Match(e, e.state)
        return Match(e, "ok", tuple(v.text + rest for v in e.values))
    return None


def classify(table: AliasTable, entry: Entry, rewritten: str, repo_abs: str) -> "tuple[str, str]":
    """What a rewritten request is: ("path", repo-relative) | ("relative", spec) |
    ("bare", spec) | ("outside", text)."""
    computed = any(v.computed for v in entry.values)
    if rewritten.startswith("/"):
        if computed or table.tool == "webpack":
            rel = os.path.relpath(rewritten, repo_abs)
            rel = Path(rel).as_posix()
            return ("outside", rewritten) if rel == ".." or rel.startswith("../") else ("path", rel)
        return "path", posixpath.normpath(posixpath.join(table.scope, rewritten.lstrip("/")))
    if rewritten.startswith(("./", "../")) or rewritten in (".", ".."):
        return "relative", rewritten
    return "bare", rewritten


__all__ = ["AliasTable", "Entry", "Match", "Value", "VITE", "WEBPACK", "classify", "config_files", "load",
           "lookup", "read_table", "translate_regex"]
