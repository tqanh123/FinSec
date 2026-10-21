"""S2: a TS/JS symbol's body hash over its normalized syntax tree.

A pre-order walk emits the tree's node types, field names and leaf text, after:

    1. dropping `comment`, every extra node and `empty_statement`;
    2. unwrapping a parenthesized expression/type with exactly one named child;
    3. flattening nested `union_type`/`intersection_type` (a leading `|`/`&` gone);
    4. a `string` as its content with the quote style folded (`'a'` == `"a"`, and an
       escaped quote is the quote); a `template_string` kept as written;
    5. a `number` without `_` separators;
    6. `jsx_text` as its non-empty stripped lines joined by one space; a
       `jsx_expression` holding only a comment dropped;
    7. a one-parameter arrow function with a bare identifier parameter in one shape
       (`a => b` == `(a) => b`);
    8. `<hole>` for an array slot a `,` leaves empty (`[a, , b]` != `[a, b]`);
    9. leaf `;` and `,` dropped; a MISSING node as `<MISSING>`.

Field names go in, so `for (a;;)` and `for (;a;)` differ. Measured on 1248 symbols
from two real repos: stable 1245/1248 under Prettier's defaults and 1237/1248 under
an aggressive config; one changed token changed 1194/1194.

The hash names what made it: `tree-sitter:<grammar version>+s2:<hex16>`, the
grammar's name inside the hashed input. A grammar upgrade or another grammar for an
extension moves every hash -- and the prefix makes that a named engine change.

WHAT IT CANNOT SEE
    * A quote style changed on purpose, and a comment-only edit: both invisible, as
      for the Python reader's `ast.unparse` hash.
    * HTML Prettier reformats inside a template literal: the string's content really
      changed, so the hash does too.
"""
from __future__ import annotations

import hashlib
from typing import Any

ALGO = "s2"
_DROP = frozenset({"comment", "empty_statement"})
_PARENS = frozenset({"parenthesized_expression", "parenthesized_type"})
_FLAT = frozenset({"union_type", "intersection_type"})
_ARRAYS = frozenset({"array", "array_pattern"})
_SEP = "\x1f"


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "replace")


def _dropped(node: Any) -> bool:
    return node.type in _DROP or node.is_extra


def _string(node: Any) -> str:
    parts = []
    for c in node.children:
        if c.type == "string_fragment":
            parts.append(_text(c))
        elif c.type == "escape_sequence":
            t = _text(c)
            parts.append(t[1:] if t in ("\\'", '\\"', "\\`") else t)
    return 's"' + "".join(parts) + '"'


def _jsx_text(node: Any) -> "str | None":
    lines = [ln.strip() for ln in _text(node).splitlines()]
    joined = " ".join(ln for ln in lines if ln)
    return joined or None


def _flat_members(node: Any) -> "list[Any]":
    out, stack = [], list(reversed(node.named_children))
    while stack:
        n = stack.pop()
        if _dropped(n):
            continue
        if n.type == node.type or (n.type == "parenthesized_type" and _single(n) is not None
                                   and _single(n).type == node.type):
            inner = n if n.type == node.type else _single(n)
            stack.extend(reversed(inner.named_children))
        else:
            out.append(n)
    return out


def _single(node: Any) -> Any:
    kids = [c for c in node.named_children if not _dropped(c)]
    return kids[0] if len(kids) == 1 else None


def _arrow1(node: Any) -> "tuple[Any, Any] | None":
    """(param, body) of an arrow function taking ONE bare identifier, else None."""
    if node.child_by_field_name("type_parameters") or node.child_by_field_name("return_type"):
        return None
    body = node.child_by_field_name("body")
    p = node.child_by_field_name("parameter")
    if p is not None and p.type == "identifier":
        return p, body
    ps = node.child_by_field_name("parameters")
    if ps is None:
        return None
    params = [c for c in ps.named_children if not _dropped(c)]
    if len(params) != 1:
        return None
    only = params[0]
    if only.type == "identifier":
        return only, body
    if only.type == "required_parameter" and only.named_child_count == 1:
        pat = only.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            return pat, body
    return None


def tokens(root: Any) -> "list[str]":
    """The S2 token stream of the subtree at `root`."""
    out: "list[str]" = []
    stack: "list[tuple[str, Any]]" = [("node", root)]
    while stack:
        what, item = stack.pop()
        if what == "emit":
            out.append(item)
            continue
        node = item
        if _dropped(node):
            continue
        t = node.type
        if t in _PARENS and _single(node) is not None:
            stack.append(("node", _single(node)))
            continue
        if node.is_missing:
            out.append("<MISSING>")
            continue
        if t == "string":
            out.append(_string(node))
            continue
        if t == "number":
            out.append(_text(node).replace("_", ""))
            continue
        if t == "jsx_text":
            jt = _jsx_text(node)
            if jt:
                out.append(jt)
            continue
        if t == "jsx_expression" and all(_dropped(c) for c in node.named_children):
            continue
        if t == "arrow_function":
            a1 = _arrow1(node)
            if a1 is not None:
                out.append("(arrow1")
                stack.append(("emit", ")"))
                stack.append(("node", a1[1]))
                stack.append(("node", a1[0]))
                continue
        if t in _FLAT:
            out.append("(" + t)
            stack.append(("emit", ")"))
            for m in reversed(_flat_members(node)):
                stack.append(("node", m))
            continue
        if node.child_count == 0:
            text = _text(node)
            if node.is_named or text not in (";", ","):
                out.append(text)
            continue
        out.append("(" + t)
        todo: "list[tuple[str, Any]]" = []
        prev = None
        for i, c in enumerate(node.children):
            if t in _ARRAYS and not c.is_named and _text(c) == "," and prev in ("[", ","):
                todo.append(("emit", "<hole>"))
            if not c.is_named:
                prev = _text(c)
            elif not _dropped(c):
                prev = None
            if _dropped(c):
                continue
            field = node.field_name_for_child(i)
            if field:
                todo.append(("emit", field + ":"))
            todo.append(("node", c))
        todo.append(("emit", ")"))
        stack.extend(reversed(todo))
    return out


def symbol_sha(nodes: "list[Any]", grammar: str, grammar_version: str,
               lead: "tuple[str, ...]" = ()) -> str:
    """`tree-sitter:<grammar_version>+s2:<hex16>` over `lead` then `nodes` read in order
    (a variable's `lead` is its declaration keyword: `let x` -> `const x` moves it)."""
    toks = [grammar, *lead] + [tok for n in nodes for tok in tokens(n)]
    digest = hashlib.sha256((ALGO + ":" + _SEP.join(toks)).encode("utf-8")).hexdigest()[:16]
    return "tree-sitter:%s+%s:%s" % (grammar_version, ALGO, digest)
