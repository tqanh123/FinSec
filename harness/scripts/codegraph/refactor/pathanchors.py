"""codegraph.refactor.pathanchors -- the sys.path anchors and `__file__`-based
paths a file carries.

Port of ob-tools' `devkit.codemod.pathanchors` (pinned commit `60075cb`,
`packages/devkit/src/devkit/codemod/pathanchors.py`), ported nearly verbatim:
this is a pure static-AST evaluator over one file's own `__file__`-derived
expressions plus a git listing -- it reads no layout schema and carries no
namespace assumption to replace (measured: zero mentions of a project name,
a `packages/` convention, or any hard-coded directory anywhere in the
upstream file).

A flat tree bootstraps itself: a file reaches its siblings by putting its own
directory (or an ancestor) on `sys.path`, spelled from `__file__`. Move the
file and every one of those spellings points somewhere else. Measured on one
real harness, a flat move produced 108 collection errors for exactly this
reason, and the anchors were spelled through helper functions often enough
(`_hooks_dir()` alone, 16 call sites) that a tool which cannot see through a
helper is a tool that hands a third of the tree back by hand -- and through a
chain of plain variables often enough (`_HOOKS = ...; TARGET = _HOOKS /
"leaf.py"`) that a tool checking only an assignment's own AST for the literal
name `__file__` misses every second hop and reads the join as an ordinary
string, silently.

Three answers per anchor, and no fourth:

  * `computed`      -- the expression evaluates, statically, to this directory.
  * `foreign-root`  -- the expression depends on a RUNTIME PARAMETER (an install
                      target, a pytest `tmp_path`, a `--repo` argv): a different
                      tree, or a tree chosen at run time. A move must not touch
                      it, and it is never this file's own bootstrap.
  * `unresolved`    -- the expression names something static evaluation cannot
                      reduce (an `IfExp` with disagreeing branches, a name the
                      module does not define). Carries the reason; the goal on a
                      real tree is zero of these, and a nonzero count is a
                      question for a reader, not a silent hand-wave.

The same evaluator feeds `path_exprs_in_file`, which adds the OTHER
`__file__`-based paths a moved file carries -- the data file read from beside
the module, `argv` spellings -- because a move has to rewrite those to keep
pointing where they pointed, not only the sys.path lines.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Optional, TypeGuard

import attrs

from codegraph._core.git import GitError, source_files

KIND_COMPUTED = "computed"
KIND_FOREIGN = "foreign-root"
KIND_UNRESOLVED = "unresolved"

STYLE_PATHLIB = "pathlib"
STYLE_OSPATH = "os.path"


class _Unk(Exception):
    """Static evaluation stopped. `kind` is 'param' (a runtime parameter -- the
    expression is about a FOREIGN tree) or 'unresolved' (cannot reduce)."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@attrs.define(frozen=True)
class Anchor:
    """One `sys.path.insert/append` call site, or a plain `__file__`-based
    assignment (a data path). `is_anchor` says which."""
    path: str
    line: int
    expr: str
    dir_abs: Optional[str]
    style: str
    kind: str
    reason: str = ""
    is_anchor: bool = True
    col: int = 0
    end_line: int = 0
    end_col: int = 0


#: The non-anchor spelling, for readers who want the distinction in the type.
PathExpr = Anchor


def _norm(p: str) -> str:
    return os.path.normpath(p)


def _style_of(expr_src: str) -> str:
    if "os.path" in expr_src or "dirname(" in expr_src or "abspath(" in expr_src:
        return STYLE_OSPATH
    return STYLE_PATHLIB


class _FileScan:
    """Static evaluation of one file's path expressions."""

    def __init__(self, root: Path, rel: str, abs_override: Optional[str] = None):
        self.root = Path(root).resolve()
        self.rel = rel
        self.file = self.root / rel
        #: `abs_override` evaluates the file's expressions AS IF the file sat
        #: there -- the before/after differential a move needs.
        self.abs = abs_override or str(self.file)
        self.tree: Optional[ast.Module]
        try:
            self.tree = ast.parse(self.file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            self.tree = None
        #: parameter names of the enclosing function, for foreign-root reasons
        self._params: set = set()
        self._helpers: dict = {}
        self._imports: set = set()
        #: the source expression each module-level/handled name was assigned,
        #: so a pathlib-vs-os.path judgement can see through the variable
        self._assign_src: dict = {}
        #: node -> parent node, built once, so a join expression anywhere in
        #: the file (a call argument, not just an assignment RHS) can tell
        #: whether it sits INSIDE a wider join -- only the outermost one is a
        #: real path to retarget; a nested piece is just its parent's part.
        self._parent: dict = {}
        if self.tree is not None:
            for node in ast.walk(self.tree):
                for child in ast.iter_child_nodes(node):
                    self._parent[id(child)] = node
        if self.tree is not None:
            for st in self.tree.body:
                if isinstance(st, (ast.Import, ast.ImportFrom)):
                    for a in st.names:
                        self._imports.add(a.asname or a.name.split(".")[0])
                if (isinstance(st, ast.FunctionDef) and not st.args.args
                        and not st.args.posonlyargs and not st.args.kwonlyargs):
                    self._helpers[st.name] = st

    # ------------------------------------------------------------- evaluation

    def ev(self, node, env: dict) -> str:
        """Evaluate `node` to a normalized absolute-ish path string, or raise _Unk."""
        if node is None:
            raise _Unk("unresolved", "no expression")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _norm(node.value)
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return self.abs
            if node.id in env:
                v = env[node.id]
                if isinstance(v, _Unk):
                    raise v
                return v
            if node.id in self._params:
                raise _Unk("param", "runtime param %r" % node.id)
            if node.id in self._helpers:
                return self._call_helper(self._helpers[node.id], {})
            if node.id in self._imports:
                raise _Unk("unresolved", "imported name %r" % node.id)
            # A bare name the module never defines: a value that arrives at run
            # time (an argparse namespace, a caller's object). Never a bootstrap.
            raise _Unk("param", "runtime name %r" % node.id)
        if isinstance(node, ast.Call):
            return self._call(node, env)
        if isinstance(node, ast.Attribute):
            if node.attr in ("parent", "dirname"):
                return os.path.dirname(self.ev(node.value, env))
            if node.attr in ("resolve", "absolute"):
                return _norm(self.ev(node.value, env))
            try:
                base = self.ev(node.value, env)
            except _Unk:
                # An attribute of something evaluation could not reduce is an
                # attribute of a RUNTIME object (an argparse namespace, a
                # caller's value). It can never be a `__file__` bootstrap --
                # those reduce -- so it is foreign-root, not unresolved.
                raise _Unk("param", "runtime attribute .%s" % node.attr) from None
            raise _Unk("unresolved", "attribute .%s (of %r)" % (node.attr, base))
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"):
            base = self.ev(node.value.value, env)
            sl = node.slice
            k = sl.value if isinstance(sl, ast.Constant) else getattr(sl, "value", None)
            if not isinstance(k, int):
                raise _Unk("unresolved", "parents[<not a literal>]")
            p = base
            for _ in range(k + 1):
                p = os.path.dirname(p)
            return p
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return _norm(os.path.join(self.ev(node.left, env),
                                      self.ev(node.right, env)))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # String concatenation builds paths too (`env_root + "/harness"`).
            return _norm(os.path.join(self.ev(node.left, env),
                                      self.ev(node.right, env)))
        if isinstance(node, ast.IfExp):
            left = right = None
            unk: Optional[_Unk] = None
            try:
                left = self.ev(node.body, env)
            except _Unk as e:
                unk = e
            try:
                right = self.ev(node.orelse, env)
            except _Unk as e:
                unk = unk or e
            if left is not None and right is not None:
                if left == right:
                    return left
                raise _Unk("unresolved",
                           "IfExp branches disagree: %r vs %r" % (left, right))
            raise unk or _Unk("unresolved", "IfExp")
        raise _Unk("unresolved", type(node).__name__)

    def _call_helper(self, fn, env: dict) -> str:
        """Evaluate a parameterless helper by running its straight-line body:
        sequential assignments, then the return. Anything else raises as usual."""
        local: dict = {}
        for st in fn.body:
            if (isinstance(st, ast.Assign) and len(st.targets) == 1
                    and isinstance(st.targets[0], ast.Name)):
                try:
                    local[st.targets[0].id] = self.ev(st.value, local)
                except _Unk as e:
                    local[st.targets[0].id] = e
            elif isinstance(st, ast.Return):
                return self.ev(st.value, local)
        raise _Unk("unresolved", "helper %r has no return" % fn.name)

    def _call(self, node: ast.Call, env: dict) -> str:
        f = node.func
        fn = ast.unparse(f)
        args = node.args
        if fn in ("os.path.dirname", "dirname"):
            return os.path.dirname(self.ev(args[0], env))
        if fn in ("os.path.abspath", "abspath", "os.path.realpath", "realpath",
                  "os.path.normpath", "normpath"):
            return _norm(self.ev(args[0], env))
        if fn in ("os.path.join", "join"):
            return _norm(os.path.join(*[self.ev(a, env) for a in args]))
        if fn in ("str", "os.fspath", "Path", "pathlib.Path"):
            return _norm(os.path.join(*[self.ev(a, env) for a in args])) if args else "."
        if fn in ("os.environ.get", "getenv", "os.getenv", "os.getcwd", "getcwd"):
            raise _Unk("param", "runtime environment (%s)" % fn)
        if fn in self._helpers and not args:
            return self._call_helper(self._helpers[fn], {})
        if (isinstance(f, ast.Attribute) and f.attr in ("resolve", "absolute")
                and not args):
            return self.ev(f.value, env)
        if fn == "getattr" and args:
            # getattr(obj, "literal") -- evaluate obj; a param obj is a param anchor
            return self.ev(args[0], env)
        raise _Unk("unresolved", "call %s" % fn)

    # -------------------------------------------------------------- collection

    def _style_src(self, expr: ast.AST) -> str:
        """Unparse an expression, following helper calls and variable
        assignments, so a pathlib-or-os.path judgement sees through `_base()`
        and `_HERE` to what they spell."""
        src = ast.unparse(expr)
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
                and expr.func.id in self._helpers:
            fn = self._helpers[expr.func.id]
            for st in fn.body:
                if isinstance(st, ast.Return) and st.value is not None:
                    return src + " ; " + self._style_src(st.value)
        if isinstance(expr, ast.Name) and expr.id in self._assign_src:
            return src + " ; " + self._assign_src[expr.id]
        if isinstance(expr, ast.BinOp):
            return self._style_src(expr.left) + " / " + self._style_src(expr.right)
        return src

    def _is_outermost_join(self, node) -> bool:
        """True unless an ANCESTOR is itself a `__file__`-derived join -- a
        nested `Path(__file__)...  / "a"` inside a wider `... / "a" / "b"` is
        part of the outer expression, not a second path of its own; editing
        both would splice two overlapping spans."""
        p = self._parent.get(id(node))
        while p is not None:
            if _is_join(p) and _mentions_transitively(p, self._assign_src):
                return False
            p = self._parent.get(id(p))
        return True

    def _classify(self, expr: ast.expr, env: dict, line: int, is_anchor: bool):
        src = ast.unparse(expr)
        try:
            val = self.ev(expr, env)
            kind, reason, out = KIND_COMPUTED, "", val
        except _Unk as e:
            out = None
            if e.kind == "param":
                kind, reason = KIND_FOREIGN, (
                    "depends on a runtime %s -- a tree chosen at run time, never "
                    "this file's own bootstrap" % e.detail)
            else:
                kind, reason = KIND_UNRESOLVED, e.detail
        return Anchor(self.rel, line, src, out, _style_of(self._style_src(expr)),
                     kind, reason,
                     is_anchor=is_anchor, col=getattr(expr, "col_offset", 0),
                     end_line=getattr(expr, "end_lineno", line) or line,
                     end_col=getattr(expr, "end_col_offset", 0))

    def scan(self) -> list:
        """Every sys.path anchor and plain `__file__`-based assignment, in order."""
        out: list = []
        if self.tree is None:
            return out
        seen_lines: set = set()

        def visit(body, env: dict, params: set) -> None:
            saved = self._params
            self._params = params
            local = dict(env)
            for st in body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 \
                        and isinstance(st.targets[0], ast.Name):
                    v: object
                    try:
                        v = self.ev(st.value, local)
                        local[st.targets[0].id] = v
                    except _Unk as e:
                        v = e
                        local[st.targets[0].id] = e
                    if _mentions_transitively(st.value, self._assign_src):
                        self._assign_src[st.targets[0].id] = ast.unparse(st.value)
                        # A Call that never reduces (`.read_text()`, `.exists()`)
                        # is not itself a path -- claiming this line here would
                        # block the fallback walk below from ever recording the
                        # real join it wraps (`(_ROOT / "a" / "b.py").read_text()`),
                        # which DOES reduce and IS the one to retarget.
                        if not (isinstance(st.value, ast.Call) and isinstance(v, _Unk)):
                            pe = self._classify(st.value, local, st.lineno, False)
                            if pe.line not in seen_lines:
                                seen_lines.add(pe.line)
                                out.append(pe)
                if isinstance(st, ast.For) and isinstance(st.target, ast.Name) \
                        and isinstance(st.iter, (ast.Tuple, ast.List)):
                    for elt in st.iter.elts:
                        try:
                            local[st.target.id] = self.ev(elt, local)
                        except _Unk as e:
                            local[st.target.id] = e
                        visit(st.body, local, params)
                    continue
                if isinstance(st, ast.FunctionDef):
                    fn_params = params | {a.arg for a in st.args.args}
                    visit(st.body, local, fn_params)
                    continue
                # Blocks first: an anchor INSIDE a try/if body must be classified
                # with that block's own assignments in scope -- ast.walk(st) would
                # otherwise see it with the OUTER env and misjudge it.
                for b in ("body", "orelse", "finalbody"):
                    sub = getattr(st, b, None)
                    if sub and isinstance(st, (ast.If, ast.Try, ast.With)):
                        visit(sub, local, params)
                for h in getattr(st, "handlers", []) or []:
                    visit(h.body, local, params)
                for n in ast.walk(st):
                    if isinstance(n, ast.Call):
                        fn = ast.unparse(n.func)
                        if fn in ("sys.path.insert", "sys.path.append") and n.args:
                            a = self._classify(n.args[-1], local, n.lineno, True)
                            if a.line not in seen_lines:
                                seen_lines.add(a.line)
                                out.append(a)
                    elif _is_join(n) and _mentions_transitively(n, self._assign_src) \
                            and self._is_outermost_join(n):
                        # A path joined INLINE at its use site -- a call
                        # argument, not an assignment -- is still a
                        # `__file__`-based path a move must keep pointing
                        # where it pointed; only the OUTERMOST join per
                        # branch is recorded, so a nested piece already
                        # covered by its parent is not edited twice.
                        pe = self._classify(n, local, n.lineno, False)
                        if pe.line not in seen_lines:
                            seen_lines.add(pe.line)
                            out.append(pe)
            self._params = saved

        visit(self.tree.body, {}, set())
        return out


def _is_join(n: ast.AST) -> "TypeGuard[ast.BinOp]":
    return isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.Add))


def _mentions_transitively(node, assign_src: dict) -> bool:
    """True if `node` mentions `__file__` directly, OR through a name that
    itself was assigned from an expression that does -- any depth. `HOOK_PATH =
    _HOOKS / "leaf.py"` never spells `__file__`; it spells `_HOOKS`, which
    does, so the join is a `__file__`-based path exactly as much as its base."""
    return any(isinstance(n, ast.Name) and (n.id == "__file__" or n.id in assign_src)
               for n in ast.walk(node))


def anchors_in_file(root: Path, rel: str) -> list:
    """The `sys.path.insert/append` anchors one file spells."""
    return [a for a in _FileScan(root, rel).scan() if a.is_anchor]


def path_exprs_in_file(root: Path, rel: str) -> list:
    """Every `__file__`-based path the file spells: anchors and data paths."""
    return _FileScan(root, rel).scan()


# ------------------------------------------------------ effective directories

def _pyproject_pythonpath(root: Path) -> list:
    """`[tool.pytest.ini_options] pythonpath` entries, repo-relative."""
    import tomllib
    pp = Path(root) / "pyproject.toml"
    if not pp.is_file():
        return []
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    entries = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
    return [Path(root) / e for e in entries.get("pythonpath", []) if isinstance(e, str)]


def _conftests_upward(root: Path, rel: str) -> list:
    """conftest.py files from `rel`'s directory up to the repo root, nearest first."""
    out = []
    d = (Path(root) / rel).parent
    root_p = Path(root)
    while True:
        cand = d / "conftest.py"
        try:
            out.append(cand.relative_to(root_p).as_posix())
        except ValueError:
            break
        if d == root_p:
            break
        d = d.parent
    return out


def effective_dirs(root: Path, rel: str) -> set:
    """The directories through which `rel` can import bare names: its own
    directory (script mode and pytest's default prepend import mode), its own
    computed sys.path anchors, every ancestor conftest's directory and anchors,
    and the pyproject-declared pythonpath."""
    root = Path(root)
    dirs: set = set()
    f = root / rel
    dirs.add(f.parent)
    for cf in _conftests_upward(root, rel):
        c = root / cf
        if c.is_file():
            dirs.add(c.parent)
            for a in anchors_in_file(root, cf):
                if a.kind == KIND_COMPUTED:
                    dirs.add(Path(a.dir_abs))
    for a in anchors_in_file(root, rel):
        if a.kind == KIND_COMPUTED:
            dirs.add(Path(a.dir_abs))
    dirs |= set(_pyproject_pythonpath(root))
    return dirs


# --------------------------------------------------------- the move differential

@attrs.define(frozen=True)
class BrokenImport:
    importer: str
    module: str
    line: int


def _bare_imports(root: Path, rel: str) -> list:
    """Bare top-level module names the file imports, statically."""
    f = Path(root) / rel
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    out: list = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.append((a.name.split(".")[0], n.lineno))
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.append((n.module.split(".")[0], n.lineno))
        elif (isinstance(n, ast.Call)
              and ast.unparse(n.func) == "importlib.import_module"
              and n.args and isinstance(n.args[0], ast.Constant)
              and isinstance(n.args[0].value, str)):
            out.append((n.args[0].value.split(".")[0], n.lineno))
    return out


def broken_imports_after(root: Path, moved: dict) -> list:
    """Imports the MOVE breaks: names that resolved through the importer's
    effective directories BEFORE and stop resolving AFTER (the module now sits in
    a directory those same grants do not reach).

    Differential ONLY. An import that never resolved statically -- it runs on a
    runtime sys.path some other module adds -- is not a new break and is not
    reported: judging it would be the guess this tool refuses to make."""
    root = Path(root)
    moved_files = {Path(s).stem: (s, d) for s, d in moved.items()}
    out: list = []
    # The tracked files, through the one listing door (`-z` underneath, so a
    # non-ASCII path is not C-quoted) -- the universe `moveflat.tracked` scans
    # for references, so an importer this check judges is one the move rewrites.
    listed = source_files(root, tracked_only=True)
    if listed is None:
        raise GitError("not a git work tree: %s" % root)
    files = [p for p in listed if p.endswith(".py")]
    for rel in files:
        if rel in moved:
            continue  # moved files are part (a)'s problem, not an importer's
        dirs = effective_dirs(root, rel)
        for mod, ln in _bare_imports(root, rel):
            if mod not in moved_files:
                continue
            src, dst = moved_files[mod]
            # Run on the PRE-move tree: the module file sits at `src`.
            if (root / src).parent not in dirs or not (root / src).is_file():
                continue  # never resolved statically -- not this move's break
            if (root / dst).parent not in dirs:
                out.append(BrokenImport(rel, mod, ln))
    return out
