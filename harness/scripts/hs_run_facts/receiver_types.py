"""hs_run_facts.receiver_types — the class a method use's receiver evidently has.

`code.callers` lists every `<receiver>.<name>` use of a method subject by name. Some
receivers state their class in the file itself: `Cls()`, `Cls` (the class object),
`self`/`cls` in a method of `Cls`, a name annotated `Cls` (`Optional[Cls]`,
`Cls | None`, `"Cls"`) or bound `x = Cls(...)` and in no other way in its scope. A
class is named only through what the file binds, read in the scope Python reads it
in: a class the module defines, a name imported from a module the snapshot holds
(re-exports followed), `mod.Cls` through an imported module. A bare name matching
some class elsewhere names nothing. The use then resolves through the class's MRO
(C3, over the snapshot's classes, each base named through its own module):

  this  — it reaches the subject.
  other — the receiver is exactly that class (`Cls()`, `Cls`), it reaches another
          class's method, and nothing about the class can change what `Cls()` builds:
          every class on its MRO is in the snapshot, none defines `__new__` or names
          a metaclass.
  None  — anything else. A declared type (`self`, `x: Cls`) may hold any subclass,
          and ruling every subclass out needs the whole program (a class built in a
          factory, under an `if`, behind an alias reads like none), so it is a caller
          or undecided, never `other`.

`other` leaves only a count behind, so every doubt lands on None. Not a type checker:
a class attribute assigned rather than defined (`create = staticmethod(f)`) is not
seen, nor a class built at runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

import codegraph_change_reader

#: Bases that define no method a subject could be shadowed by, when the snapshot
#: holds no class for the base to resolve to.
_TRANSPARENT = frozenset({"object", "Protocol", "Generic", "ABC"})
_SELF_NAMES = frozenset({"self", "cls"})
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPES = _FUNCS + (ast.Lambda, ast.ClassDef) + _COMPS
_OTHER = ("other",)
_NOTHING = (None, None, False)


def _tail(text: str) -> str:
    return text.split("[", 1)[0].rpartition(".")[2].strip()


def _unique(hows: list) -> list:
    """`hows` without repeats; a binding naming a syntax node is the same only as itself."""
    seen: dict = {}
    for h in hows:
        seen.setdefault(tuple(id(x) if isinstance(x, ast.AST) else x for x in h), h)
    return list(seen.values())


def _walk(roots):
    """Each root and its descendants, a nested scope node yielded but not entered."""
    todo = list(reversed(roots))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, _SCOPES):
            todo.extend(reversed(list(ast.iter_child_nodes(n))))


def _outer(n) -> list:
    """The parts of a scope node evaluated in the scope around it."""
    if isinstance(n, _COMPS):
        return [n.generators[0].iter]
    out = list(getattr(n, "decorator_list", ()))
    if isinstance(n, ast.ClassDef):
        return out + list(n.bases) + [k.value for k in n.keywords]
    args = n.args
    out += [d for d in args.defaults + args.kw_defaults if d is not None]
    if isinstance(n, _FUNCS):
        out += [a.annotation for a in _params(n) if a.annotation is not None]
        if n.returns is not None:
            out.append(n.returns)
    return out


def _inner(n) -> list:
    if isinstance(n, ast.Module):
        return list(n.body)
    if isinstance(n, ast.Lambda):
        return [n.body]
    if isinstance(n, _COMPS):
        first = n.generators[0]
        heads = [n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt]
        return heads + [first.target] + list(first.ifs) + list(n.generators[1:])
    return list(n.body)


def _params(fn) -> list:
    a = fn.args
    return a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]


def _own(roots):
    """Every node a scope evaluates: its own body, and the outer parts of the scopes
    nested in it."""
    for n in _walk(roots):
        yield n
        if isinstance(n, _SCOPES):
            yield from _own(_outer(n))


class ClassTable:
    """The snapshot's top-level classes, and the parsed files that name them."""

    def __init__(self, idx: dict, root):
        self.root = Path(root)
        self.module_of_path: "dict[str, str]" = {}
        self.path_of_module: "dict[str, str]" = {}
        for m in codegraph_change_reader._modules(idx):
            if m.get("path") and m.get("module"):
                self.module_of_path[m["path"]] = m["module"]
                self.path_of_module[m["module"]] = m["path"]
        self.bases: "dict[str, list[str]]" = {}
        self.methods: "dict[str, set[str]]" = {}
        for _unit, m, s, parent in codegraph_change_reader.walk_symbols(idx):
            if parent is None and s.get("kind") == "class":
                q = "%s::%s" % (m["module"], s["name"])
                self.bases[q] = [str(b) for b in s.get("bases") or ()]
                self.methods[q] = {str(mm.get("name", "")).rpartition(".")[2]
                                   for mm in s.get("methods") or ()}
        self._files: "dict[str, Optional[FileScope]]" = {}
        self._member: "dict[tuple, object]" = {}
        self._asking: "set[tuple]" = set()
        self._mro: "dict[str, Optional[list]]" = {}
        self._plain: "dict[tuple, bool]" = {}

    def file(self, path: str, text: "str | None" = None) -> "Optional[FileScope]":
        if path not in self._files:
            self._files[path] = None
            if text is None:
                try:
                    text = (self.root / path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return None
            try:
                self._files[path] = FileScope(self, path, ast.parse(text))
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                pass
        return self._files[path]

    def module_scope(self, module: str) -> "Optional[FileScope]":
        path = self.path_of_module.get(module)
        return self.file(path) if path else None

    def member(self, module: str, name: str):
        """What `module.name` is: ("class", q), ("module", m) or None."""
        key = (module, name)
        if key in self._member:
            return self._member[key]
        if key in self._asking:
            return None  # a re-export that leads back to itself names nothing
        scope = self.module_scope(module)
        sub = "%s.%s" % key
        if scope is not None and name in scope.raw(scope.tree):
            self._asking.add(key)
            try:
                hit = scope.lookup_ref(name, scope.tree)  # what the module binds, once
            finally:
                self._asking.discard(key)
        elif scope is None and "%s::%s" % key in self.bases:
            hit = ("class", "%s::%s" % key)  # a file that does not parse: the snapshot's word
        else:
            hit = ("module", sub) if sub in self.path_of_module else None
        self._member[key] = hit
        return hit

    def mro(self, q: str) -> Optional[list]:
        """C3 linearization; a base the snapshot does not hold is an opaque `?<expr>`
        entry. None when it cannot be built (a cycle, an inconsistent order)."""
        if q not in self._mro:
            self._mro[q] = None  # an inheritance cycle reads as no answer
            self._mro[q] = self._linearize(q)
        return self._mro[q]

    def _linearize(self, q: str) -> Optional[list]:
        scope = self.module_scope(q.partition("::")[0])
        seqs = []
        for text in self.bases.get(q, ()):
            try:
                expr = ast.parse(text, mode="eval").body
                while isinstance(expr, ast.Subscript):  # Generic[T], Base[int]
                    expr = expr.value
            except (SyntaxError, ValueError):
                expr = None
            hit = scope.resolve(expr, scope.tree) if scope is not None and expr is not None \
                else None
            if hit and hit[0] == "class" and hit[1] != q:
                sub = self.mro(hit[1])
                if sub is None:
                    return None
                seqs.append(list(sub))
            elif not hit and _tail(text) in _TRANSPARENT:
                continue
            else:
                seqs.append(["?" + text])
        seqs.append([s[0] for s in seqs])
        out = [q]
        while any(seqs):
            seqs = [s for s in seqs if s]
            for s in seqs:
                head = s[0]
                if not any(head in t[1:] for t in seqs):
                    break
            else:
                return None
            out.append(head)
            seqs = [t[1:] if t[0] == head else t for t in seqs]
        return out

    def target(self, q: str, name: str) -> Optional[str]:
        """The method `name` an instance of `q` reaches; None when an opaque base
        comes first, no class defines it, or the MRO cannot be built."""
        for c in self.mro(q) or ():
            if c.startswith("?"):
                return None
            if name in self.methods.get(c, ()):
                return "%s.%s" % (c, name)
        return None

    def plain(self, q: str, name: str) -> bool:
        """Whether `q().<name>` is the `def` its MRO shows, as far as the files of
        those classes can tell: every class is in the snapshot, none is decorated,
        defines `__new__` or `__getattribute__`, names a metaclass or assigns its
        `__class__`, and no file of theirs assigns `<name>` in a class body or as an
        attribute (`self.<name> = ...`, `Cls.<name> = ...`). A file elsewhere can
        still rebind it, which is why a use this decides is labelled, never dropped."""
        if (q, name) not in self._plain:
            self._plain[(q, name)] = self._plain_now(q, name)
        return self._plain[(q, name)]

    def _plain_now(self, q: str, name: str) -> bool:
        for c in self.mro(q) or ["?"]:
            if c.startswith("?") or self.methods.get(c, set()) & {"__new__", "__getattribute__"}:
                return False
            module, _sep, cname = c.partition("::")
            scope = self.module_scope(module)
            node = scope.classes.get(cname) if scope is not None else None
            if scope is None or node is None or node.decorator_list \
                    or any(k.arg == "metaclass" for k in node.keywords) \
                    or name in scope.assigned_attrs or "__class__" in scope.assigned_attrs \
                    or name in scope.class_body_names(node):
                return False
        return True


def verdict(table: ClassTable, subject: str, cls: str, exact: bool) -> Optional[str]:
    """`this`/`other`/None for a use of `subject`'s name on a receiver of class `cls`."""
    name = subject.rpartition(".")[2]
    reached = table.target(cls, name)
    if reached is None or (exact and not table.plain(cls, name)):
        return None
    if reached == subject:
        return "this"
    return "other" if exact else None


class FileScope:
    """One parsed file: each scope's bindings, read only when asked."""

    def __init__(self, table: ClassTable, path: str, tree: ast.Module):
        self.table, self.path, self.tree = table, path, tree
        self.module = table.module_of_path.get(path, "")
        self.package = (self.module if path.endswith("__init__.py")
                        else self.module.rpartition(".")[0])
        self.shared = {name for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))
                       for name in n.names}
        self.classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        self.top_body = {id(n) for n in tree.body}
        self.assigned_attrs = {t.attr for n in ast.walk(tree)
                               if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                               for target in (n.targets if isinstance(n, ast.Assign) else [n.target])
                               for t in ast.walk(target) if isinstance(t, ast.Attribute)}
        self.assigned_attrs |= {n.args[1].value for n in ast.walk(tree)
                                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                and n.func.id == "setattr" and len(n.args) > 1
                                and isinstance(n.args[1], ast.Constant)
                                and isinstance(n.args[1].value, str)}
        self.parent: "dict[int, object]" = {id(tree): None}
        todo: list = [tree]
        while todo:
            scope = todo.pop()
            for n in _own(_inner(scope)):
                if isinstance(n, _SCOPES) and id(n) not in self.parent:
                    self.parent[id(n)] = scope
                    todo.append(n)
        self._raw: "dict[int, dict]" = {}
        self._bound: "dict[tuple, list | None]" = {}

    def _from(self, module: "str | None", level: int) -> str:
        if not level:
            return module or ""
        base = self.package
        for _ in range(level - 1):
            base = base.rpartition(".")[0]
        return "%s.%s" % (base, module) if module and base else (module or base)

    def raw(self, scope) -> "dict[str, list]":
        """name -> every way `scope` binds it, as written: ("from", module, name),
        ("mod", dotted), ("cls", q), ("param", arg), ("ann", node), ("call", node),
        ("other",)."""
        if id(scope) in self._raw:
            return self._raw[id(scope)]
        out: "dict[str, list]" = {}
        self._raw[id(scope)] = out

        def bind(name, how=_OTHER):
            out.setdefault(name, []).append(how)

        if isinstance(scope, _FUNCS + (ast.Lambda,)):
            for arg in _params(scope):
                bind(arg.arg, ("param", arg))
        handled: "set[int]" = set()
        at_top = isinstance(scope, ast.Module)
        for n in _own(_inner(scope)):
            if isinstance(n, ast.ClassDef):
                q = "%s::%s" % (self.module, n.name)
                top = at_top and id(n) in self.top_body and q in self.table.bases
                bind(n.name, ("cls", q) if top else _OTHER)
            elif isinstance(n, _FUNCS):
                bind(n.name)
            elif isinstance(n, _COMPS) and not isinstance(scope, _COMPS):
                for x in ast.walk(n):  # a walrus in a comprehension binds out here
                    if isinstance(x, ast.NamedExpr):
                        bind(x.target.id)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                handled.add(id(n.target))
                bind(n.target.id, ("ann", n.annotation))
            elif isinstance(n, ast.Assign):
                single = len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                for t in n.targets:
                    for leaf in ast.walk(t):
                        if isinstance(leaf, ast.Name):
                            handled.add(id(leaf))
                            if isinstance(leaf.ctx, ast.Store):  # `self.x = 1` keeps `self`
                                bind(leaf.id, ("call", n.value) if single else _OTHER)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) \
                    and id(n) not in handled:
                bind(n.id)  # for/with/except/walrus/comprehension targets, augmented, del
            elif isinstance(n, ast.ImportFrom):
                base = self._from(n.module, n.level)
                for alias in n.names:
                    if alias.name != "*":
                        bind(alias.asname or alias.name,
                             ("from", base, alias.name) if base else _OTHER)
            elif isinstance(n, ast.Import):
                for alias in n.names:
                    if alias.asname:
                        bind(alias.asname, ("mod", alias.name))
                    else:
                        head = alias.name.partition(".")[0]
                        bind(head, ("mod", head))
            elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
                bind(n.name)
            elif isinstance(n, ast.MatchMapping) and n.rest:
                bind(n.rest)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bind(n.name)
        return out

    def _chain(self, scope):
        """The scopes a name read in `scope` looks through, innermost first: a class
        body only for what stands directly in it."""
        first = True
        while scope is not None:
            if first or not isinstance(scope, ast.ClassDef):
                yield scope
            scope, first = self.parent.get(id(scope)), False

    def lookup_ref(self, name: str, scope):
        """The class or module `name` names where `scope` reads it, when one thing."""
        if name in self.shared:
            return None
        for s in self._chain(scope):
            hows = self.raw(s).get(name)
            if hows is None:
                continue
            hows = _unique(hows)
            if len(hows) != 1:
                return None
            how = hows[0]
            if how[0] == "from":
                return self.table.member(how[1], how[2])
            if how[0] == "mod":
                return ("module", how[1])
            if how[0] == "cls":
                return ("class", how[1])
            return None
        return None

    def resolve(self, expr, scope):
        if isinstance(expr, ast.Name):
            return self.lookup_ref(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            head = self.resolve(expr.value, scope)
            if head and head[0] == "module":
                return self.table.member(head[1], expr.attr)
        return None

    def annotation(self, node, scope) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(node, ast.Subscript):
            outer = node.value
            tail = outer.attr if isinstance(outer, ast.Attribute) else getattr(outer, "id", "")
            return self.annotation(node.slice, scope) if tail == "Optional" else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = [s for s in (node.left, node.right)
                     if not (isinstance(s, ast.Constant) and s.value is None)]
            return self.annotation(sides[0], scope) if len(sides) == 1 else None
        hit = self.resolve(node, scope)
        return hit[1] if hit and hit[0] == "class" else None

    def call(self, node, scope) -> Optional[str]:
        if isinstance(node, ast.Call):
            hit = self.resolve(node.func, scope)
            return hit[1] if hit and hit[0] == "class" else None
        return None

    def bound(self, scope, name: str) -> "list | None":
        """How `scope` binds `name`, each binding read: ("ref", hit), ("inst", class,
        exact), other; None when it does not. Read only for a name asked about, and
        no further than the first binding that already decides nothing."""
        key = (id(scope), name)
        if key not in self._bound:
            hows = self.raw(scope).get(name)
            read: "list | None" = None
            if hows is not None:
                read = []
                for how in _unique(hows):
                    kind = how[0]
                    got: tuple
                    if kind == "param":
                        ann = how[1].annotation
                        cls = self.annotation(ann, self.parent.get(id(scope))) \
                            if ann is not None else None
                        got = ("inst", cls, False) if cls else _OTHER
                    elif kind == "ann":
                        cls = self.annotation(how[1], scope)
                        got = ("inst", cls, False) if cls else _OTHER
                    elif kind == "call":
                        cls = self.call(how[1], scope)
                        got = ("inst", cls, True) if cls else _OTHER
                    elif kind in ("from", "mod", "cls"):
                        hit = (self.table.member(how[1], how[2]) if kind == "from" else
                               ("module", how[1]) if kind == "mod" else ("class", how[1]))
                        got = ("ref", hit) if hit else _OTHER
                    else:
                        got = _OTHER
                    read.append(got)
                    if got is _OTHER:
                        break
            self._bound[key] = read
        return self._bound[key]

    def class_body_names(self, node) -> "set[str]":
        """Names a class body assigns (not its `def`s)."""
        return {t.id for n in _own(_inner(node))
                if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                for target in (n.targets if isinstance(n, ast.Assign) else [n.target])
                for t in ast.walk(target) if isinstance(t, ast.Name)}

    def lookup_var(self, name: str, scope) -> tuple:
        """("class", q, True) for a name naming a class, ("inst", q, exact) for an
        instance of one, else _NOTHING."""
        if name in self.shared:
            return _NOTHING
        for s in self._chain(scope):
            hows = self.bound(s, name)
            if hows is None:
                continue
            owner = self.parent.get(id(s))
            if name in _SELF_NAMES and isinstance(s, _FUNCS) \
                    and len(_unique(self.raw(s)[name])) == 1 \
                    and isinstance(owner, ast.ClassDef) and id(owner) in self.top_body \
                    and _params(s)[:1] and _params(s)[0].arg == name \
                    and not any(_tail(ast.unparse(d)) == "staticmethod" for d in s.decorator_list):
                q = "%s::%s" % (self.module, owner.name)
                return ("inst", q, False) if q in self.table.bases else _NOTHING
            if len(hows) == 1 and hows[0][0] == "ref":
                hit = hows[0][1]
                return ("class", hit[1], True) if hit and hit[0] == "class" else _NOTHING
            classes = {h[1] if h[0] == "inst" else None for h in hows}
            if len(classes) != 1 or None in classes:
                return _NOTHING
            return "inst", classes.pop(), all(h[2] for h in hows)
        return _NOTHING


def classify_file(table: ClassTable, path: str, text: "str | None",
                  subject: str) -> "dict[int, list[tuple]]":
    """line -> [(verdict, class, receiver, caller)] for every `.<name>` attribute use
    of `subject` in the file; `caller` is the top-level def holding the use
    (`module::f`, `module::Cls.meth`), the top-level class for a use in its body,
    else `module::<module>`. Empty when the file cannot be parsed."""
    scope = table.file(path, text)
    if scope is None:
        return {}
    name = subject.rpartition(".")[2]
    module = scope.module
    out: "dict[int, list[tuple]]" = {}

    patched = name in scope.assigned_attrs  # `obj.create = ...` / setattr here: any receiver

    def use(recv, where_scope, caller: str) -> tuple:
        where = "%s::%s" % (module, caller or "<module>")
        kind, cls, exact = _NOTHING
        shown = type(recv).__name__
        if isinstance(recv, ast.Call):
            head = recv.func
            shown = (getattr(head, "id", None) or getattr(head, "attr", None) or shown) + "()"
            cls = scope.call(recv, where_scope)
            exact = cls is not None
        elif isinstance(recv, ast.Name):
            kind, cls, exact = scope.lookup_var(recv.id, where_scope)
            shown = recv.id
        elif isinstance(recv, ast.Attribute):
            shown = recv.attr
            hit = scope.resolve(recv, where_scope)
            if hit and hit[0] == "class":
                cls, exact = hit[1], True
        decided = verdict(table, subject, cls, exact) if cls and not patched else None
        return decided, cls, shown, where

    def visit(roots: list, here, caller: str) -> None:
        for n in _walk(roots):
            if isinstance(n, ast.Attribute) and n.attr == name:
                out.setdefault(n.end_lineno or n.lineno, []).append(use(n.value, here, caller))
            if not isinstance(n, _SCOPES):
                continue
            visit(_outer(n), here, caller)
            top = here is scope.tree
            if isinstance(n, ast.ClassDef):
                inner = n.name if top else caller
            elif isinstance(n, _FUNCS):
                in_top_class = isinstance(here, ast.ClassDef) and \
                    scope.parent.get(id(here)) is scope.tree
                inner = n.name if top else "%s.%s" % (caller, n.name) if in_top_class else caller
            else:
                inner = caller
            visit(_inner(n), n, inner)

    try:
        visit(_inner(scope.tree), scope.tree, "")
    except RecursionError:
        return {}
    return out
