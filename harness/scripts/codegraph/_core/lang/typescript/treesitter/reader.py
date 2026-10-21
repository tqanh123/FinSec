"""The tree-sitter reader of the TypeScript/JavaScript pack: one file -> `ModuleSymbols`.

The grammar follows the extension: `.ts .mts .cts` (and `.d.ts`) with the typescript
grammar, where `<T>x` is a cast; `.tsx .js .jsx .mjs .cjs` with tsx (measured: 0/228
JavaScript files with an error, against 18/228 under the javascript grammar). The
queries under `queries/` locate imports and symbols; what a query cannot say -- the
`type`/`default`/`*` tokens, a magic comment before `import()`'s argument, an
`import()` in a type position -- the host code here decides.

A file always reads: a syntax error is a `problems` row with its line (the region it
covers may hide symbols), an unexpected exception is one `EXCEPTION` row. The text is
parsed from the decoded `str` re-encoded as UTF-8, never the file's raw bytes (a
`#match?` predicate raises on invalid UTF-8), and no `progress_callback` is set (it
crashes 0.26 on Python 3.12).

WHAT IT CANNOT SEE
    * A specifier that is not a string literal (`import(x)`, `require(name)`): kept as
      an expression, never resolved.
    * `f<typeof import('m')>()` -- the grammar parses it as a comparison plus an ERROR
      (tree-sitter-typescript #367); the `typeof` heuristic still reads it type-only.
    * `import.meta.glob(...)`, `/// <reference ...>`, a bundler's virtual module: no
      import record.
    * Names bound by destructuring (`const {a, b} = require("x")`): the import is read,
      the names are not symbols.
    * A CommonJS export built any other way than `module.exports = {...}`/`= v`,
      `exports.x = v` (`Object.assign(module.exports, ...)`, `exports[k] = v`).
    * A quote-style or comment-only edit does not change a body hash (`hashing`).
"""
from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path
from typing import Any, ClassVar

import tree_sitter as ts
import tree_sitter_typescript as tst

from ....adapter.base import ModuleSymbols, SymbolInfo
from ..facts import Refs, TsImport
from .hashing import symbol_sha

_QUERY_DIR = Path(__file__).resolve().parent / "queries"
_TS_SUFFIXES = (".ts", ".mts", ".cts")
_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
GRAMMAR_VERSION = importlib.metadata.version("tree-sitter-typescript")

_FUNCTIONS = frozenset({"function_declaration", "function_expression", "arrow_function",
                        "method_definition", "generator_function_declaration",
                        "generator_function", "class_static_block"})
_TYPE_CONTEXTS = frozenset({"type_annotation", "type_query", "type_arguments", "generic_type",
                            "type_alias_declaration", "lookup_type", "union_type",
                            "intersection_type", "function_type", "object_type", "array_type",
                            "tuple_type", "conditional_type", "type_parameter", "constraint",
                            "extends_type_clause", "implements_clause", "index_type_query"})
_TS_ONLY = frozenset({"type_annotation", "interface_declaration", "type_alias_declaration",
                      "enum_declaration", "abstract_class_declaration", "as_expression",
                      "satisfies_expression", "non_null_expression", "internal_module",
                      "ambient_declaration", "implements_clause", "accessibility_modifier"})
_KIND = {"function": "func", "overload": "func", "class": "class", "variable": "const",
         "interface": "interface", "type": "type", "enum": "enum", "namespace": "namespace"}
_MAX_PROBLEMS = 50


def grammar_for(path: "Path | str") -> str:
    name = Path(path).name.lower()
    return "typescript" if name.endswith(_TS_SUFFIXES) else "tsx"


_cache: "dict[str, Any]" = {}


def _lang(grammar: str) -> Any:
    key = "lang:" + grammar
    if key not in _cache:
        fn = tst.language_typescript if grammar == "typescript" else tst.language_tsx
        _cache[key] = ts.Language(fn())
    return _cache[key]


def parser_for(grammar: str) -> Any:
    """The one parser of `grammar` this process holds."""
    key = "parser:" + grammar
    if key not in _cache:
        _cache[key] = ts.Parser(_lang(grammar))
    return _cache[key]


def _query(grammar: str, kind: str) -> Any:
    key = "q:%s:%s" % (grammar, kind)
    if key not in _cache:
        text = "\n".join((_QUERY_DIR / ("%s.%s.scm" % (kind, part))).read_text(encoding="utf-8")
                         for part in ("common", "ts"))
        _cache[key] = ts.Query(_lang(grammar), text)
    return _cache[key]


def _matches(grammar: str, kind: str, root: Any) -> "list[dict[str, list[Any]]]":
    return [caps for _i, caps in ts.QueryCursor(_query(grammar, kind)).matches(root)]


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "replace")


def _str_value(node: Any) -> str:
    out = []
    for c in node.children:
        if c.type == "string_fragment":
            out.append(_text(c))
        elif c.type == "escape_sequence":
            t = _text(c)
            out.append(t[1:] if len(t) == 2 else t)
    return "".join(out)


def _has_token(node: Any, token: str) -> bool:
    return any(not c.is_named and _text(c) == token for c in node.children)


def _ancestors(node: Any):
    n = node.parent
    while n is not None:
        yield n
        n = n.parent


def _in_function(node: Any) -> bool:
    return any(a.type in _FUNCTIONS for a in _ancestors(node))


def _type_position(call: Any) -> bool:
    if call.parent is not None and call.parent.type == "unary_expression" \
            and _has_token(call.parent, "typeof"):
        return True
    for a in _ancestors(call):
        if a.type in _TYPE_CONTEXTS:
            return True
        if a.type in ("program", "statement_block") or a.type.endswith("_statement"):
            return False
    return False


def _first_arg(args: Any) -> Any:
    return next((c for c in args.named_children if c.type != "comment"), None)


def _spec_of(arg: Any) -> "tuple[str | None, dict | None]":
    if arg is None:
        return None, {"kind": "none", "text": ""}
    if arg.type == "string":
        return _str_value(arg), None
    return None, {"kind": arg.type, "text": _text(arg)}


def _name(node: Any) -> str:
    return _str_value(node) if node.type == "string" else _text(node)


def _import_bindings(stmt: Any) -> "tuple[tuple[str, str, bool], ...]":
    clause = next((c for c in stmt.named_children if c.type == "import_clause"), None)
    if clause is None:
        return ()
    stmt_type = _has_token(stmt, "type")
    out = []
    for c in clause.named_children:
        if c.type == "identifier":
            out.append(("default", _text(c), stmt_type))
        elif c.type == "namespace_import":
            ident = next((x for x in c.named_children if x.type == "identifier"), None)
            out.append(("*", _text(ident) if ident else "*", stmt_type))
        elif c.type == "named_imports":
            for spec in (s for s in c.named_children if s.type == "import_specifier"):
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                imported = _name(name) if name is not None else (
                    "default" if _has_token(spec, "default") else "")
                out.append((imported, _name(alias) if alias is not None else imported,
                            stmt_type or _has_token(spec, "type")))
    return tuple(out)


def _export_bindings(stmt: Any) -> "tuple[tuple[str, str, bool], ...]":
    stmt_type = _has_token(stmt, "type")
    out = []
    for c in stmt.named_children:
        if c.type == "export_clause":
            for spec in (s for s in c.named_children if s.type == "export_specifier"):
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                imported = _name(name) if name is not None else "default"
                out.append((imported, _name(alias) if alias is not None else imported,
                            stmt_type or _has_token(spec, "type")))
        elif c.type == "namespace_export":
            ident = next((x for x in c.named_children if x.type in ("identifier", "string")), None)
            out.append(("*", _name(ident) if ident else "*", stmt_type))
    return tuple(out)


def _imports(root: Any, grammar: str) -> "tuple[TsImport, ...]":
    found: "list[tuple[int, TsImport]]" = []
    for caps in _matches(grammar, "imports", root):
        if "import.stmt" in caps:
            stmt, src = caps["import.stmt"][0], caps["import.source"][0]
            bindings = _import_bindings(stmt)
            kind = "static" if any(c.type == "import_clause" for c in stmt.named_children) \
                else "side_effect"
            imp = TsImport(kind, _str_value(src), bindings, _has_token(stmt, "type"),
                           stmt.start_point[0] + 1, _in_function(stmt))
        elif "reexport.stmt" in caps:
            stmt, src = caps["reexport.stmt"][0], caps["reexport.source"][0]
            kind = "export_from" if any(c.type == "export_clause" for c in stmt.named_children) \
                else "export_all"
            imp = TsImport(kind, _str_value(src), _export_bindings(stmt), _has_token(stmt, "type"),
                           stmt.start_point[0] + 1, _in_function(stmt))
        elif "dynamic.call" in caps:
            call = caps["dynamic.call"][0]
            spec, expr = _spec_of(_first_arg(call.child_by_field_name("arguments")))
            imp = TsImport("dynamic", spec, (), _type_position(call), call.start_point[0] + 1,
                           _in_function(call), expr)
        elif "require.call" in caps:
            call = caps["require.call"][0]
            spec, expr = _spec_of(_first_arg(call.child_by_field_name("arguments")))
            imp = TsImport("require", spec, (), False, call.start_point[0] + 1,
                           _in_function(call), expr)
        elif "import_eq.clause" in caps:
            clause, src = caps["import_eq.clause"][0], caps["import_eq.source"][0]
            ident = next((c for c in clause.named_children if c.type == "identifier"), None)
            stmt = clause.parent
            imp = TsImport("import_equals", _str_value(src),
                           (("*", _text(ident), False),) if ident is not None else (),
                           stmt is not None and _has_token(stmt, "type"),
                           clause.start_point[0] + 1, _in_function(clause))
        else:
            continue
        node = next(iter(caps.values()))[0]
        found.append((node.start_byte, imp))
    return tuple(imp for _b, imp in sorted(found, key=lambda x: x[0]))


def _lines(node: Any) -> "tuple[int, int]":
    return node.start_point[0] + 1, node.end_point[0] + 1


def _sha(nodes: "list[Any]", grammar: str, lead: "tuple[str, ...]" = ()) -> str:
    return symbol_sha(nodes, grammar, GRAMMAR_VERSION, lead)


def _methods(cls: Any, grammar: str) -> "tuple[SymbolInfo, ...]":
    body = cls.child_by_field_name("body")
    cname = cls.child_by_field_name("name")
    parent = _text(cname) if cname is not None else None
    out = []
    for c in (body.named_children if body is not None else []):
        if c.type in ("method_definition", "abstract_method_signature"):
            name = c.child_by_field_name("name")
            if name is not None:
                out.append(SymbolInfo(name=_text(name), kind="method", lines=_lines(c),
                                      sha=_sha([c], grammar), parent=parent))
    return tuple(out)


def _bases(cls: Any) -> "tuple[str, ...]":
    out = []
    for h in (c for c in cls.named_children if c.type == "class_heritage"):
        for e in (c for c in h.named_children if c.type == "extends_clause"):
            v = e.child_by_field_name("value")
            if v is not None:
                out.append(_text(v))
    return tuple(out)


def _value_kind(value: Any) -> str:
    return ("func" if value.type in ("function_expression", "arrow_function", "generator_function")
            else "class" if value.type == "class" else "const")


def _cjs(stmt: Any, target: Any, value: Any, grammar: str) -> "list[tuple[int, SymbolInfo]]":
    """`module.exports = {a, b: f}` names each key; `module.exports = v` is `default`;
    `exports.x = v` / `module.exports.x = v` names `x`."""
    path = _text(target).replace(" ", "")
    if path == "module.exports":
        if value.type != "object":
            return [(stmt.start_byte, SymbolInfo("default", _value_kind(value), _lines(stmt), _sha([value], grammar)))]
        out = []
        for c in value.named_children:
            if c.type == "pair" and c.child_by_field_name("key").type in ("property_identifier", "string"):
                v = c.child_by_field_name("value")
                out.append((c.start_byte, SymbolInfo(_name(c.child_by_field_name("key")), _value_kind(v),
                                                     _lines(c), _sha([v], grammar))))
            elif c.type == "shorthand_property_identifier":
                out.append((c.start_byte, SymbolInfo(_text(c), "const", _lines(c), _sha([c], grammar))))
            elif c.type == "method_definition" and c.child_by_field_name("name") is not None:
                out.append((c.start_byte, SymbolInfo(_text(c.child_by_field_name("name")), "func", _lines(c),
                                                     _sha([c], grammar))))
        return out
    for head in ("module.exports.", "exports."):
        name = path[len(head):] if path.startswith(head) else ""
        if name.replace("$", "_").isidentifier():
            return [(stmt.start_byte, SymbolInfo(name, _value_kind(value), _lines(stmt), _sha([value], grammar)))]
    return []


def _symbols(root: Any, grammar: str) -> "tuple[tuple[SymbolInfo, ...], tuple[str, ...]]":
    found: "list[tuple[int, SymbolInfo]]" = []
    for caps in _matches(grammar, "symbols", root):
        defs = [(k.split(".", 1)[1], v[0]) for k, v in caps.items() if k.startswith("def.")]
        if defs and "name" in caps:
            what, node = defs[0]
            name = _name(caps["name"][0])
            kind = _KIND[what]
            if what == "variable":
                decl = node.parent
                keyword = _text(decl.children[0]) if decl is not None and decl.children else "const"
                found.append((node.start_byte, SymbolInfo(name, kind, _lines(node),
                                                          _sha([node], grammar, (keyword,)))))
            elif what == "class":
                found.append((node.start_byte, SymbolInfo(name, kind, _lines(node), _sha([node], grammar),
                                                          methods=_methods(node, grammar),
                                                          bases=_bases(node))))
            else:
                found.append((node.start_byte, SymbolInfo(name, kind, _lines(node), _sha([node], grammar))))
        elif "default.stmt" in caps:
            stmt, value = caps["default.stmt"][0], caps["default.value"][0]
            found.append((stmt.start_byte, SymbolInfo("default", _value_kind(value), _lines(stmt),
                                                      _sha([value], grammar))))
        elif "cjs.stmt" in caps:
            found += _cjs(caps["cjs.stmt"][0], caps["cjs.target"][0], caps["cjs.value"][0], grammar)
    seen: "set[str]" = set()
    dups: "list[str]" = []
    syms: "list[SymbolInfo]" = []
    for _b, s in sorted(found, key=lambda x: x[0]):
        if s.name in seen:
            if s.name not in dups:
                dups.append(s.name)
            continue
        seen.add(s.name)
        syms.append(s)
    return tuple(syms), tuple(dups)


def _problems(root: Any, js: bool) -> "tuple[dict, ...]":
    out: "list[dict]" = []
    ts_hit: "dict | None" = None
    stack = [root]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            out.append({"kind": "ERROR" if n.is_error else "MISSING",
                        "line": n.start_point[0] + 1, "col": n.start_point[1]})
            if n.is_error:
                continue
        if js and ts_hit is None and n.type in _TS_ONLY:
            ts_hit = {"kind": "ts_syntax_in_js", "line": n.start_point[0] + 1, "col": n.start_point[1]}
        if n.has_error or js:
            stack.extend(reversed(n.children))
    out.sort(key=lambda p: (p["line"], p["col"]))
    if ts_hit is not None:
        out.append(ts_hit)
    return tuple(out[:_MAX_PROBLEMS])


class TreeSitterTsReader:
    """`typescript` pack, engine `tree-sitter`."""

    ext: ClassVar[frozenset[str]] = frozenset(_TS_SUFFIXES + (".tsx",) + _JS_SUFFIXES)

    def parse(self, path: Path, text: str, container: "tuple[str, ...]", module: str) -> ModuleSymbols:
        data = text.encode("utf-8")
        grammar = grammar_for(path)
        try:
            root = parser_for(grammar).parse(data).root_node
            imports = _imports(root, grammar)
            symbols, dups = _symbols(root, grammar)
            problems = _problems(root, Path(path).name.lower().endswith(_JS_SUFFIXES))
        except Exception as e:  # noqa: BLE001 -- one file never sinks the snapshot
            imports, symbols, dups = (), (), ()
            problems = ({"kind": "EXCEPTION", "line": 0, "col": 0, "detail": type(e).__name__},)
        return ModuleSymbols(path=path, module=module, container=container, loc=text.count("\n"),
                             nbytes=len(data), file_sha=hashlib.sha256(data).hexdigest()[:16],
                             text=text, imports=imports, symbols=symbols, dup_module_assign=dups,
                             problems=problems)


_PLAIN = frozenset({"identifier", "type_identifier", "shorthand_property_identifier"})
_MEMBER = {"member_expression": ("object", "property"), "nested_type_identifier": ("module", "name")}


def _require_bindings(name: Any) -> "tuple[tuple[str, str], ...] | None":
    if name.type == "identifier":
        return (("*", _text(name)),)
    if name.type != "object_pattern":
        return None
    out = []
    for c in name.named_children:
        if c.type == "shorthand_property_identifier_pattern":
            out.append((_text(c), _text(c)))
        elif c.type == "pair_pattern":
            k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
            if k is not None and v is not None and v.type == "identifier":
                out.append((_name(k), _text(v)))
    return tuple(out)


def refs(path: "Path | str", text: str) -> Refs:
    """`facts.Refs` of one file, read off its syntax tree."""
    root = parser_for(grammar_for(path)).parse(text.encode("utf-8")).root_node
    plain: "list[tuple[int, str]]" = []
    member: "list[tuple[int, str, str]]" = []
    requires: "list[tuple[int, str, tuple[tuple[str, str], ...]]]" = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "import_statement" or (n.type == "export_statement"
                                            and n.child_by_field_name("source") is not None):
            continue
        if n.type in _PLAIN:
            plain.append((n.start_point[0] + 1, _text(n)))
        elif n.type in _MEMBER:
            a, b = (n.child_by_field_name(f) for f in _MEMBER[n.type])
            if a is not None and b is not None and a.type == "identifier":
                member.append((a.start_point[0] + 1, _text(a), _text(b)))
        elif n.type == "variable_declarator":
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if name is not None and value is not None and value.type == "call_expression":
                fn, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
                arg = _first_arg(args) if args is not None else None
                bound = _require_bindings(name)
                if fn is not None and _text(fn) == "require" and arg is not None and arg.type == "string" \
                        and bound is not None:
                    requires.append((value.start_point[0] + 1, _str_value(arg), bound))
        stack.extend(reversed(n.children))
    return Refs(tuple(sorted(plain)), tuple(sorted(member)), tuple(sorted(requires)))
