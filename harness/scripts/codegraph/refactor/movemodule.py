"""codegraph.refactor.movemodule -- move modules inside one dotted package
and rewrite every reference DIRECTLY (no shim, no re-export).

Port of ob-tools' `devkit.codemod.movemodule` (pinned commit `60075cb`,
`packages/devkit/src/devkit/codemod/movemodule.py`). Adapted for this repo:

    * `find_package` (upstream: a hard-coded glob search over
      `packages/*/src/{pkg}`, `src/{pkg}`, `{pkg}`) is REPLACED by
      `_layout.find_package_dir`, which reads the target repo's own
      `codemap.yaml` `import_roots` -- the layout schema's own declared roots --
      instead of guessing a directory convention. This is the one piece of
      this door that is genuinely layout-specific, so it is the one piece
      that had to stop being a guess.
    * `--map`/YAML move-map loading (`load_map`, `flatten_map`, `groups:`,
      `--dist`, `--drop-group-prefix`) is not ported -- this engine takes
      an explicit `{old: new}` dotted mapping only; `--tests`'s own
      `tests:` group override (a YAML-declared `{group: [stems]}` map,
      upstream `build_moves`'s `tests` parameter) is not ported either --
      this door always derives the test pairing from `mapping` itself
      (`"." in new`), never a separate declared list.
    * `--tests` IS ported (`build_test_moves`) -- moves `test_<leaf>.py`
      alongside a module gaining a subpackage. Upstream located the tests
      directory by comparing `pkgdir.parent.name == "src"` (a hard-coded
      convention-name literal); this port reads the same layout schema
      `find_package_dir` already reads (`descriptor.src_subdir`/
      `tests_subdir`) instead. Naming the test FILE template reads
      `codegraph._core.testfiles.python_files_patterns` (the same reader
      `build_test_layout` and every other caller in the package share) for
      the target's own pytest `python_files` declaration, falling back to
      pytest's own `test_*.py` default when nothing is declared or the
      declared shape is not a single glob.
    * `_depth_edits` IS ported (`with_name("data")` / `parents[N]`
      arithmetic after a depth change) -- schema-agnostic by measurement:
      it reduces a literal already present in the file's own AST, reading
      no layout convention at all.
    * `_blind` IS ported (advisory notes for `__slots__`/CLI-flag/
      monkeypatch/dynamic-import strings spelling a moved module's bare
      name) -- same measurement: it reads only the dotted-name bookkeeping
      this door already builds (`leaves`/`rel_names`), never a layout
      convention. The one shape it drops versus upstream is the
      FSTRING_MIDDLE branch (an f-string interpolating a dotted name) --
      see WHAT IT CANNOT SEE, unchanged from before this round.
    * `DYNAMIC_ATTR_CALLS`/`_MONKEY_CALLS` came from a sibling upstream
      module (`devkit.codemod.rename`) this port does not carry as an import;
      they are copied in as this module's own constants instead. The directory
      skip upstream copied alongside them is `codegraph._core.walk`, the one
      set every walk of this library shares.
      Upstream's own hard-coded history-directory skip is NOT copied in the
      same way -- `_scan_files` instead reads the target repo's own
      declared history (`codemap.yaml` `domains.oldrefs.history`, via
      `codegraph.refactor.oldrefs.declared`/`.in_history`), the same
      declaration `refactor move`'s own `old_mentions` report already
      treats as "the record, not a stale reference" (see `move.py`'s
      `_old_mentions`). A hard-coded directory-name set would both miss a
      history FILE outside any named directory (`CHANGELOG.md`, `docs/
      decisions.yaml`) and diverge from what the report already promises.

WHAT IT DOES
    * moves `<pkg>/<old>.py` -> `<pkg>/<new>.py` (dotted, relative to the
      package; a new subpackage gets a stub `__init__.py`);
    * rewrites, on tokens/AST: dotted names in code (`import a.b as c`,
      `from a.b import x`, `a.b.f()`), `from a import b [as c]` (the local
      name is kept), and relative imports (`from . import x`, `from .old
      import y`) recomputed from the importer's NEW place -- also inside a
      moved file's own imports of modules that did not move;
    * rewrites complete dotted module names inside strings and comments,
      bounded by non-identifier characters.

ROLE, versus `import_rewrite` (measured, not assumed -- see that sibling
module's own docstring): this engine moves a file ALREADY TRACKED in the
target repo (`git mv`) and rewrites every reference to it across the WHOLE
repo, including a bare dotted expression outside any import line
(`result = pkg.mod.VALUE`, which this module's own tokenize pass DOES
rewrite -- `import_rewrite.rewrite_imports` never touches it, by design,
since it is line-based). `import_rewrite` instead lands bytes read from a
separate, read-only SOURCE repo that were never committed into the target at
all, rewriting only the landed file's own import lines through a
`module_map` (a many-prefix mapping, including whole-package prefixes this
module's own `build_moves` refuses outright as "not a module"). Moving a
file already in this repo -> this module; landing a new file by a prefix
map -> `import_rewrite`.

WHAT IT CANNOT SEE
    * an f-string (tokenizes as FSTRING_START/MIDDLE/END on this
      interpreter, not STRING) that spells a dotted name -- the token loop
      below only ever matches COMMENT/STRING, and `_blind`'s own
      FSTRING_MIDDLE advisory branch is the one upstream shape this port
      drops (see the adaptation list above);
    * a non-literal `.parents[...]` index or any OTHER `.parent`/`dirname`
      chain on `__file__` that `_depth_edits` cannot reduce to a constant --
      LISTED in `Plan.manual` for a human, never guessed at;
    * a moved module's bare leaf name built at runtime rather than spelled
      as a literal string (an f-string, a concatenation, a variable) --
      `_blind` only ever matches an `ast.Constant` string;
    * uncommitted work: files are read from disk at plan time, not the index.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import attrs

from codegraph._core.git import GitError, run_git, source_files
from codegraph._core.layout import test_home_of
from codegraph._core.testfiles import python_files_patterns

from codegraph._core import walk

from . import oldrefs
from ._layout import find_import_root, find_package_dir, load_descriptor
from .errors import RefactorError

TEXT_SUFFIXES = frozenset({".py", ".md", ".yaml", ".yml", ".toml", ".cfg", ".ini",
                          ".txt", ".sh"})
_STR_TOKENS = frozenset({tokenize.COMMENT, tokenize.STRING})
#: from ob-tools' sibling `devkit.codemod.rename` -- not ported as a module
#: (this door does not do renames), so these two constants are copied in
#: verbatim rather than pulled from an import this repo does not carry.
DYNAMIC_ATTR_CALLS = frozenset({"getattr", "setattr", "delattr", "hasattr"})
_MONKEY_CALLS = frozenset({"setattr", "delattr", "patch", "setitem", "delitem",
                          "import_module", "__import__"})


@attrs.define(frozen=True)
class Note:
    """One spot the rewrite could not fix (a depth change it cannot reduce
    to a literal) or could not see at all (a blind-spot advisory) -- read by
    a human, never acted on by the move itself."""
    kind: str
    path: str
    line: int
    detail: str


@attrs.define
class FileMove:
    src: str                  # repo-relative posix path
    dst: str
    old: str = ""             # full dotted (modules only)
    new: str = ""
    status: str = "pending"   # pending | done | refused
    reason: str = ""
    kind: str = "module"      # "module" | "test" -- a --tests companion move

    @property
    def depth(self) -> int:
        """How many directory levels deeper (positive) or shallower
        (negative) `dst` sits versus `src` -- the arithmetic `_depth_edits`
        below applies to a moved file's own `__file__`-relative literals."""
        return len(self.dst.split("/")) - len(self.src.split("/"))


@attrs.define
class Plan:
    repo: Path
    pkg: str
    moves: List[FileMove] = attrs.field(factory=list)
    rel_pkg: str = ""                                      # the package (or root) directory
    texts: Dict[str, str] = attrs.field(factory=dict)      # rel(original) -> new text
    changes: List[Tuple[str, int, str, str]] = attrs.field(factory=list)
    new_inits: List[str] = attrs.field(factory=list)
    #: depth-arithmetic spots `_depth_edits` could not reduce to a literal --
    #: read by hand, never acted on.
    manual: List[Note] = attrs.field(factory=list)
    #: spots the rewrite cannot see at all -- a moved module's bare leaf
    #: name spelled as a string (monkeypatch/CLI-flag/getattr/dynamic
    #: import) -- read by hand, never acted on.
    blind: List[Note] = attrs.field(factory=list)

    @property
    def refused(self) -> List[FileMove]:
        return [m for m in self.moves if m.status == "refused"]

    @property
    def pending(self) -> List[FileMove]:
        return [m for m in self.moves if m.status == "pending"]


def _join(pkg: str, name: str) -> str:
    """`pkg.name`; just `name` at an import root, whose modules carry no prefix."""
    return "%s.%s" % (pkg, name) if pkg else name


def _strip(pkg: str, dotted: str) -> str:
    return dotted[len(pkg) + 1:] if pkg else dotted


def build_moves(repo: Path, pkgdir: Path, pkg: str, mapping: Dict[str, str]) -> List[FileMove]:
    rel_pkg = pkgdir.relative_to(repo).as_posix()
    moves: List[FileMove] = []
    seen_new: Dict[str, str] = {}
    for old, new in mapping.items():
        if old == new:
            raise RefactorError("%s: new path equals old path" % old)
        src = "%s/%s.py" % (rel_pkg, old.replace(".", "/"))
        dst = "%s/%s.py" % (rel_pkg, new.replace(".", "/"))
        m = FileMove(src, dst, _join(pkg, old), _join(pkg, new))
        if new in seen_new:
            m.status, m.reason = "refused", "target %s also wanted by %s" % (new, seen_new[new])
        seen_new[new] = old
        sp, dp = repo / src, repo / dst
        if m.status != "refused":
            if (pkgdir / old.replace(".", "/")).is_dir():
                m.status, m.reason = "refused", "%s is a package, not a module" % old
            elif sp.is_file() and dp.exists():
                m.status, m.reason = "refused", "target exists: %s" % dst
            elif not sp.is_file() and dp.is_file():
                m.status = "done"
            elif not sp.is_file():
                m.status, m.reason = "refused", "source missing: %s" % src
            else:
                for i in range(1, len(new.split("."))):
                    shadow = ".".join(new.split(".")[:i])
                    if shadow not in mapping and (pkgdir / (shadow.replace(".", "/") + ".py")).is_file():
                        m.status, m.reason = "refused", (
                            "new subpackage %s would shadow a module" % shadow)
        moves.append(m)
    return moves


# ------------------------------------------------------------- --tests pairing

def _tests_dir(repo: Path, descriptor, pkgdir: Path) -> str:
    """Where a module's paired test file lives -- calls `layout.test_home_of`, the ONE
    place this decision is made, instead of a second copy of it. That shared reader
    pulls from the SAME descriptor the layout schema already ships
    (`packages_dir`/`src_subdir`/`tests_subdir`), never a hard-coded `src`/`packages`
    string literal -- the declared `src_subdir` name (default absent) is what upstream
    instead spelled as a literal `"src"` comparison. A package sitting directly under a
    declared `src_subdir` that ALSO sits under a `packages_dir` (a `packages/<dist>/
    src/<pkg>` tree) pairs with that dist's own `<tests_subdir or "tests">`; every
    other tree -- a flat `src/<pkg>` at the repo root, or a package with no
    `src_subdir` convention declared at all -- pairs with the bare top-level `tests/`
    `test_home_of` returns when no `dist` names one. `dist_root == repo` tells the two
    apart: a `packages/<dist>/src/<pkg>` layout's dist root is a subdirectory
    (`packages/<dist>`) two levels up from `pkgdir`; a bare `src/<pkg>` layout's is the
    repo root itself, which never gets a `dist` name to nest under."""
    src_name = getattr(descriptor, "src_subdir", None)
    dist = None
    if src_name and pkgdir.parent.name == src_name:
        dist_root = pkgdir.parents[1]
        if dist_root != repo:
            dist = dist_root.name
    return test_home_of(descriptor, dist=dist).rstrip("/")


def _pytest_test_template(repo: Path) -> str:
    """The target repo's own pytest `python_files` naming, turned into a
    `"...%s...py"` template -- pytest's own default `test_*.py` when nothing
    is declared. Reads through `codegraph._core.testfiles.python_files_
    patterns`, the same reader `build_test_layout` and every other caller
    in the package now share, rather than a second copy of the
    `pyproject.toml`/`pytest.ini`/`setup.cfg`/`tox.ini` precedence walk;
    only a single-glob pattern (one `*`, ending `.py`) becomes a template --
    several declared globs need a human pick, not a guess, so that shape
    falls back to the plain default rather than silently choosing one."""

    def _as_template(pats: "Tuple[str, ...]") -> "Optional[str]":
        for p in pats:
            if p.count("*") == 1 and p.endswith(".py"):
                return p.replace("*", "%s")
        return None

    return _as_template(python_files_patterns(repo)) or "test_%s.py"


def build_test_moves(repo: Path, descriptor, pkgdir: Path,
                     mapping: Dict[str, str]) -> List[FileMove]:
    """The `test_<leaf>.py` pair for every mapping entry whose NEW dotted
    name gained a subpackage (`"." in new` -- a bare rename carries its test
    along without a directory change, so only a genuine regroup needs one);
    a module with no such test file is silently skipped (not every module
    has one), a collision with an existing target is refused like any other
    move. Port of upstream `movemodule.build_moves`'s own `with_tests` branch
    (`packages/devkit/src/devkit/codemod/movemodule.py:262-273` at
    `frozen-260924`), generalised off `_tests_dir` instead of its literal
    `pkgdir.parent.name == "src"` check."""
    tdir = _tests_dir(repo, descriptor, pkgdir)
    tmpl = _pytest_test_template(repo)
    moves: List[FileMove] = []
    for old, new in mapping.items():
        if "." not in new:
            continue
        leaf = old.rsplit(".", 1)[-1]
        group = "/".join(new.split(".")[:-1])
        src = "%s/%s" % (tdir, tmpl % leaf)
        dst = "%s/%s/%s" % (tdir, group, tmpl % leaf)
        sp, dp = repo / src, repo / dst
        if not sp.is_file() and dp.is_file():
            moves.append(FileMove(src, dst, status="done", kind="test"))
        elif not sp.is_file():
            continue                                        # module without a test: not an error
        elif dp.exists():
            moves.append(FileMove(src, dst, status="refused",
                                  reason="target exists: %s" % dst, kind="test"))
        else:
            moves.append(FileMove(src, dst, kind="test"))
    return moves


# ---------------------------------------------------------------- rewriting

def _line_offsets(text: str) -> List[int]:
    offs, pos = [0], 0
    for ln in text.splitlines(keepends=True):
        pos += len(ln)
        offs.append(pos)
    return offs


def _dotted_rx(keys) -> "Optional[re.Pattern[str]]":
    ks = sorted(keys, key=len, reverse=True)
    if not ks:
        return None
    # never inside a path: at an import root the bare stem is also a file name
    # (`scripts/flat.py`), which `old_mentions` lists rather than this rewrites
    return re.compile(r"(?<![\w./])(" + "|".join(re.escape(k) for k in ks) + r")(?!\w)(?!/\d)(?!\.py\b)")


@attrs.define
class _Ctx:
    rel: str
    mv_all: Dict[str, str]           # old dotted -> new dotted (pending + done)
    mv_pending: Dict[str, str]
    rx: "Optional[re.Pattern[str]]"
    old_dotted: Optional[str]        # this file's dotted name before the move (in-package only)
    new_dotted: Optional[str]
    is_init: bool
    pkg: str
    depth: int = 0                   # this file's OWN depth change, 0 unless it moves
    is_test: bool = False
    leaves: Dict[str, str] = attrs.field(factory=dict)      # old leaf -> old dotted (blind scan)
    rel_names: Dict[str, str] = attrs.field(factory=dict)   # old pkg-relative -> new pkg-relative


def _split_pkg(dotted: str, is_init: bool) -> List[str]:
    parts = dotted.split(".")
    return parts if is_init else parts[:-1]


def _relative(pkg_parts: List[str], base: str) -> str:
    b = base.split(".")
    common = 0
    while common < min(len(pkg_parts), len(b)) and pkg_parts[common] == b[common]:
        common += 1
    return "." * (len(pkg_parts) - common + 1) + ".".join(b[common:])


def _fmt_alias(name: str, asname: Optional[str]) -> str:
    return name + (" as %s" % asname if asname else "")


def _from_edits(tree: ast.AST, lines: List[str], offs: List[int], ctx: _Ctx) -> list:
    edits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.ImportFrom):
            continue
        relative = n.level > 0
        if relative:
            if ctx.old_dotted is None:
                continue
            base_parts = _split_pkg(ctx.old_dotted, ctx.is_init)
            up = n.level - 1
            if up > len(base_parts) - 1:
                continue
            base = ".".join(base_parts[:len(base_parts) - up] + (n.module.split(".") if n.module else []))
        else:
            base = n.module or ""
        items, changed = [], False
        for a in n.names:
            full = "%s.%s" % (base, a.name) if base else a.name
            mapping = ctx.mv_all if not relative else ctx.mv_pending
            if a.name != "*" and full in mapping:
                nb, nn = mapping[full].rsplit(".", 1)
                items.append((nb, nn, a.asname or (a.name if nn != a.name else None)))
                changed = True
            else:
                nb = ctx.mv_pending.get(base, base) if relative else base
                changed = changed or nb != base
                items.append((nb, a.name, a.asname))
        moved_importer = relative and ctx.new_dotted != ctx.old_dotted
        if not changed and not moved_importer:
            continue
        importer_pkg = _split_pkg(ctx.new_dotted or ctx.old_dotted or "", ctx.is_init) if relative else []
        groups: Dict[str, list] = {}
        for nb, nn, al in items:
            groups.setdefault(nb, []).append((nn, al))
        row0 = lines[n.lineno - 1]
        c0 = len(row0.encode()[:n.col_offset].decode())
        indent = row0[:c0] if not row0[:c0].strip() else ""
        stmts = []
        for nb, names in groups.items():
            mod = _relative(importer_pkg, nb) if relative else nb
            stmts.append("from %s import %s" % (mod, ", ".join(_fmt_alias(a, b) for a, b in names)))
        sep = "\n" + indent if indent or c0 == 0 else "; "
        s = offs[n.lineno - 1] + c0
        end_lineno = n.end_lineno or n.lineno
        end_col = n.end_col_offset or 0
        erow = lines[end_lineno - 1]
        e = offs[end_lineno - 1] + len(erow.encode()[:end_col].decode())
        edits.append((s, e, sep.join(stmts)))
    return edits


def _has_file(node: ast.AST) -> bool:
    return any(isinstance(x, ast.Name) and x.id == "__file__" for x in ast.walk(node))


def _has_parent_hop(node: ast.AST) -> bool:
    return any(isinstance(x, ast.Attribute) and x.attr in ("parent", "parents")
               for x in ast.walk(node))


def _seg(node: ast.AST, lines: List[str], offs: List[int]) -> Tuple[int, int]:
    def pos(r: int, c: int) -> int:
        return offs[r - 1] + len(lines[r - 1].encode()[:c].decode())
    end_lineno = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
    end_col = getattr(node, "end_col_offset", None) or 0
    return pos(node.lineno, node.col_offset), pos(end_lineno, end_col)  # type: ignore[attr-defined]


def _depth_edits(tree: ast.AST, text: str, lines: List[str], offs: List[int],
                 ctx: _Ctx, notes: List[Note]) -> List[Tuple[int, int, str]]:
    """After a move changes a file's own directory depth by `ctx.depth`
    levels: `Path(__file__).with_name("data")` -> `.parents[d] / "data"` and
    a literal `.parents[N]` -> `.parents[N+d]` (data dirs stay pinned at the
    package root, so the arithmetic that reaches them must move WITH the
    file). Anything it cannot reduce to a literal (a non-constant `parents[
    ...]` index, any OTHER `.parent`/`dirname` chain on `__file__`) is
    LISTED for a human, never guessed at."""
    d = ctx.depth
    edits: List[Tuple[int, int, str]] = []
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    handled: set = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "with_name" and _has_file(n.func.value)):
            handled.add(id(n.func))
            if (len(n.args) == 1 and isinstance(n.args[0], ast.Constant)
                    and n.args[0].value == "data" and not _has_parent_hop(n.func.value)):
                vs, ve = _seg(n.func.value, lines, offs)
                new = "%s.parents[%d] / \"data\"" % (text[vs:ve], d)
                up = parent.get(n)
                if isinstance(up, (ast.Attribute, ast.Subscript)) or (
                        isinstance(up, ast.Call) and up.func is n):
                    new = "(%s)" % new
                s, e = _seg(n, lines, offs)
                edits.append((s, e, new))
            else:
                notes.append(Note("file-depth", ctx.rel, n.lineno,
                                  "with_name(...) on __file__ after a depth change of "
                                  "%+d: check by hand" % d))
        elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute)
              and n.value.attr == "parents" and _has_file(n.value.value)):
            handled.add(id(n.value))
            sl = n.slice
            if (isinstance(sl, ast.Constant) and isinstance(sl.value, int)
                    and not _has_parent_hop(n.value.value)):
                s, e = _seg(sl, lines, offs)
                edits.append((s, e, str(sl.value + d)))
            else:
                notes.append(Note("file-depth", ctx.rel, n.lineno,
                                  "parents[...] not a literal after a depth change of "
                                  "%+d: check by hand" % d))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and n.attr == "parent" and _has_file(n.value)
                and not _has_parent_hop(n.value) and ctx.depth > 0):
            up = parent.get(n)
            if (isinstance(up, ast.BinOp) and isinstance(up.op, ast.Div) and up.left is n
                    and isinstance(up.right, ast.Constant) and up.right.value == "data"):
                handled.add(id(n))
                vs, ve = _seg(n.value, lines, offs)
                s, e = _seg(n, lines, offs)
                edits.append((s, e, "%s.parents[%d]" % (text[vs:ve], d)))
    listed: set = set()
    for n in ast.walk(tree):
        if id(n) in handled:
            continue
        lineno = getattr(n, "lineno", None)
        if lineno is None:
            continue
        hit = False
        if isinstance(n, ast.Attribute) and n.attr == "parent" and _has_file(n.value):
            hit = True
        elif isinstance(n, ast.Call) and _has_file(n):
            fn = n.func
            if ((isinstance(fn, ast.Attribute) and fn.attr == "dirname")
                    or (isinstance(fn, ast.Name) and fn.id == "dirname")):
                hit = True
        if hit and lineno not in listed:
            listed.add(lineno)
            notes.append(Note("file-depth", ctx.rel, lineno,
                              "`.parent`/dirname of __file__ after a depth change of "
                              "%+d: same directory arithmetic now lands %d level(s) "
                              "%s -- check by hand"
                              % (d, abs(d), "below" if d > 0 else "above")))
    return edits


def _blind(tree: ast.AST, ctx: _Ctx, out: List[Note]) -> None:
    """Spots the rewrite cannot see at all -- every one of them is a moved
    module's BARE LEAF name spelled as a plain string, which the tokenizer's
    dotted-name substitution (built to match a full dotted chain bounded by
    non-identifier characters) has no reason to touch: a full `mypkg.a.f`
    needs no flag here since that pass already reaches it. Port of upstream
    `movemodule._blind` (`packages/devkit/src/devkit/codemod/movemodule.py:
    463-509` at `frozen-260924`), unchanged in shape -- this scan reads only
    `ctx.leaves`/`ctx.rel_names` (both dotted-name bookkeeping, not a layout
    convention), so there was no hard-coded path to replace."""
    leaves = ctx.leaves
    flags = {"--" + k.replace("_", "-"): k for k in leaves}

    def hit(s: str) -> Optional[str]:
        if s in leaves:
            return leaves[s]
        if s in flags:
            return leaves[flags[s]]
        return None

    docstrings = {id(body[0].value) for body in
                 (getattr(x, "body", None) for x in ast.walk(tree))
                 if isinstance(body, list) and body and isinstance(body[0], ast.Expr)}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docstrings and n.value in ctx.rel_names):
            out.append(Note("bare-module-name", ctx.rel, n.lineno,
                            "string %r is a moved module's old package-relative name "
                            "(now %r): not rewritten" % (n.value, ctx.rel_names[n.value])))
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__slots__" for t in n.targets):
            for c in ast.walk(n.value):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and hit(c.value):
                    out.append(Note("slots-string", ctx.rel, c.lineno,
                                    "__slots__ entry %r equals a moved module's old name"
                                    % c.value))
        elif isinstance(n, ast.Call):
            fn_node = n.func
            fn = (fn_node.attr if isinstance(fn_node, ast.Attribute)
                 else fn_node.id if isinstance(fn_node, ast.Name) else "")
            strs: List[Tuple[ast.Constant, str]] = []
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    strs.append((a, a.value))
            if fn == "add_argument":
                for node, val in strs:
                    if hit(val) and val.startswith("--"):
                        out.append(Note("cli-flag-string", ctx.rel, node.lineno,
                                        "flag %r spells a moved module's old name" % val))
            if fn in DYNAMIC_ATTR_CALLS:
                for node, val in strs[:1]:
                    if hit(val):
                        out.append(Note("attr-call-string", ctx.rel, node.lineno,
                                        "%s(..., %r) names a moved module as an attribute"
                                        % (fn, val)))
            if fn in _MONKEY_CALLS or (fn == "object" and isinstance(fn_node, ast.Attribute)):
                if (fn in ("import_module", "__import__") and n.args
                        and not isinstance(n.args[0], ast.Constant)):
                    out.append(Note("dynamic-import", ctx.rel, n.lineno,
                                    "%s(<computed>) -- the module name is built at run time" % fn))
                for node, val in strs[:1]:
                    root = val.split(".", 1)[0].split(":", 1)[0]
                    if (root in leaves and "." in val
                            and not val.startswith(ctx.pkg + ".")):
                        out.append(Note("monkeypatch-target", ctx.rel, node.lineno,
                                        "target %r names a moved module without its package"
                                        % val))


def _rewrite_py(text: str, ctx: _Ctx, manual: List[Note], blind: List[Note]) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    lines = text.split("\n")
    offs = _line_offsets(text)
    edits: List[Tuple[int, int, str]] = []
    ast_edits = _from_edits(tree, lines, offs, ctx)
    if ctx.mv_all:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))

        def pos(p):
            return offs[p[0] - 1] + p[1]

        i = 0
        while i < len(toks):
            t = toks[i]
            prev = toks[i - 1] if i else None
            if t.type == tokenize.NAME and not (prev and prev.type == tokenize.OP and prev.string == "."):
                chain, j = [t], i
                while (j + 2 < len(toks) and toks[j + 1].type == tokenize.OP and toks[j + 1].string == "."
                       and toks[j + 1].start == toks[j].end and toks[j + 2].type == tokenize.NAME
                       and toks[j + 2].start == toks[j + 1].end):
                    chain.append(toks[j + 2])
                    j += 2
                for k in range(len(chain), 0, -1):
                    name = ".".join(c.string for c in chain[:k])
                    if name in ctx.mv_all:
                        edits.append((pos(chain[0].start), pos(chain[k - 1].end), ctx.mv_all[name]))
                        break
                i = j + 1
                continue
            if t.type in _STR_TOKENS and ctx.rx is not None:
                new = ctx.rx.sub(lambda m: ctx.mv_all[m.group(1)], t.string)
                if new != t.string:
                    edits.append((pos(t.start), pos(t.end), new))
            i += 1
    edits = [e for e in edits if not any(s <= e[0] and e[1] <= end for s, end, _ in ast_edits)]
    edits += ast_edits
    if ctx.depth != 0:
        edits += _depth_edits(tree, text, lines, offs, ctx, manual)
    _blind(tree, ctx, blind)
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return text


def plan_module_move(repo: Path, pkg: str, mapping: Dict[str, str],
                     with_tests: bool = False, root: Optional[str] = None) -> Plan:
    """`pkg` names the dotted package the modules live in; `root` (then `pkg` is "")
    names a declared import root instead, whose modules move by their bare names --
    `flat=pkg.flat` takes a top-level script into a package."""
    repo = repo.resolve()
    descriptor = load_descriptor(repo)
    if root is not None:
        if pkg:
            raise RefactorError("--module takes --package or --root, not both")
        if with_tests:
            raise RefactorError("--tests pairs a test with a module inside a --package")
        pkgdir = find_import_root(repo, descriptor, root)
    else:
        pkgdir = find_package_dir(repo, descriptor, pkg)
    plan = Plan(repo, pkg)
    plan.moves = build_moves(repo, pkgdir, pkg, mapping)
    if with_tests:
        plan.moves += build_test_moves(repo, descriptor, pkgdir, mapping)
    if plan.refused:
        return plan
    rel_pkg = pkgdir.relative_to(repo).as_posix()
    mv_all = {m.old: m.new for m in plan.moves if m.old}
    mv_pending = {m.old: m.new for m in plan.moves if m.status == "pending" and m.old}
    by_src = {m.src: m for m in plan.pending}
    rx = _dotted_rx(mv_all)
    leaves = {m.old.rsplit(".", 1)[-1]: m.old for m in plan.pending if m.kind == "module"}
    rel_names = {_strip(pkg, o): _strip(pkg, n) for o, n in mv_pending.items()}
    history, _relative_roots = oldrefs.declared(repo)
    rels = source_files(repo)
    if rels is None:
        rels = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file())
    for rel in rels:
        parts = rel.split("/")
        if (oldrefs.in_history(rel, history) or walk.skipped(parts)
                or Path(rel).suffix not in TEXT_SUFFIXES or not (repo / rel).is_file()):
            continue
        text = (repo / rel).read_text(encoding="utf-8", errors="surrogateescape")
        mv = by_src.get(rel)
        in_pkg = rel.startswith(rel_pkg + "/") and rel.endswith(".py")
        old_dotted = None
        if in_pkg:
            old_dotted = ".".join([p for p in [pkg] if p] + rel[len(rel_pkg) + 1:-3].split("/"))
            if old_dotted.endswith(".__init__"):
                old_dotted = old_dotted[:-9]
        ctx = _Ctx(rel, mv_all, mv_pending, rx, old_dotted,
                   mv.new if (mv and mv.old) else old_dotted, rel.endswith("__init__.py"), pkg,
                   depth=mv.depth if mv else 0, is_test=("/tests/" in "/" + rel),
                   leaves=leaves, rel_names=rel_names)
        if rel.endswith(".py"):
            new = _rewrite_py(text, ctx, plan.manual, plan.blind)
        elif rx is not None:
            new = rx.sub(lambda m: mv_all[m.group(1)], text)
        else:
            new = text
        if new != text:
            plan.texts[rel] = new
            for n, (a, b) in enumerate(zip(text.split("\n"), new.split("\n")), 1):
                if a != b:
                    plan.changes.append((rel, n, a.strip(), b.strip()))
    plan.rel_pkg = rel_pkg
    for m in plan.pending:
        parts = _strip(pkg, m.new).split(".")[:-1]
        for i in range(1, len(parts) + 1):
            init = "%s/%s/__init__.py" % (rel_pkg, "/".join(parts[:i]))
            if not (repo / init).exists() and init not in plan.new_inits:
                plan.new_inits.append(init)
    return plan


def apply_module_plan(plan: Plan) -> List[str]:
    repo = plan.repo
    written: List[str] = []
    dst_of = {m.src: m.dst for m in plan.pending}
    for m in plan.pending:
        (repo / m.dst).parent.mkdir(parents=True, exist_ok=True)
        try:
            run_git(repo, "mv", m.src, m.dst)
        except GitError as e:
            raise RefactorError("git mv %s %s failed: %s" % (m.src, m.dst, e)) from None
        written.append(m.dst)
    for init in plan.new_inits:
        p = repo / init
        if not p.exists():
            dotted = _join(plan.pkg, ".".join(Path(init).parent.relative_to(plan.rel_pkg).parts))
            p.write_text('"""%s -- subpackage created by `hs-code refactor move --module`."""\n' % dotted,
                         encoding="utf-8")
            run_git(repo, "add", init, check=False)
            written.append(init)
    for rel, text in plan.texts.items():
        (repo / dst_of.get(rel, rel)).write_text(text, encoding="utf-8", errors="surrogateescape")
        written.append(dst_of.get(rel, rel))
    return written
