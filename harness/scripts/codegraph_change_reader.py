"""codegraph_change_reader — the import-edge walk and the test-file
predicates a caller outside the `codegraph` package (hs-run's code facts
providers) needs, WITHOUT importing `codegraph`.

A flat module under `harness/scripts/`, next to `codegraph_snapshot_reader.py`
and built on the SAME reason that module exists: `import codegraph.change.
selection` (or `codegraph._core.testfiles`) executes `codegraph/__init__.py`
and every package `__init__.py` between it and the leaf module, which
transitively pulls in the whole measurement engine (`codegraph_snapshot_
reader.py`'s own docstring measured 133 modules for one such import). A
provider that only needs "which modules import this one, transitively" or
"is this path a test pytest would collect" should not pay that weight or
inherit that import surface.

MOVED here, not reimplemented beside the originals: `codegraph.change.
selection._modules`/`_module_by_path`/`_base_target`/`_reverse_import_edges`/
`import_channel`, and `codegraph._core.testfiles`'s bare predicates
(`is_test_dir`/`in_test_tree`/`is_test_file`/`python_files_patterns`/
`DEFAULT_TEST_PATTERNS`) are all PURE — dict/string operations over an
already-loaded snapshot index, or stdlib config parsing — with zero
reference to anything else in `codegraph`. Moving them costs nothing the
original design cared about keeping inside the package. Both origin modules
now REBIND their own names to these same function objects (`import_channel
is codegraph_change_reader.import_channel` holds), the identical pattern
`codegraph._core.snapshot.worktree_key` already uses for `worktree_key`.

NOT moved: `codegraph._core.testfiles.build_test_layout` (the DOOR, not the
data it builds). It honors a project's DECLARED `layout.tests_subdir` by
loading a `codemap.yaml` descriptor when the caller hands in none — `from
.descriptor import DescriptorError, load`, a real (if lazy) `codegraph`
import that the AST-level closure gate (`test_codegraph_snapshot_reader.py`'s
`test_the_hs_run_family_import_closure_never_touches_codegraph`) counts the
same as a module-level one, since it walks function bodies too. That
resolution step stays where its dependency already lives.

MOVED here too, though: the `FilesLayout` dataclass ITSELF. Its two methods
(`is_test_dir`/`in_test_tree`/`is_test_file`) are pure —
`fnmatch.fnmatch` plus `_is_test_segment`, exactly like the bare functions
above, with zero reference to a descriptor. A provider outside `codegraph`
that skips `FilesLayout` and reads only the UNDECLARED-convention bare
functions above would silently disagree with `hs-code change test select`
on any project that DECLARES `layout.tests_subdir` (this repo's own
`codemap.yaml` does: `layout.tests_subdir: tests`) — a real divergence, not
a hypothetical one, just not one this tree's current file set happens to
exercise. `codegraph._core.testfiles.FilesLayout` REBINDS to this class
(`FilesLayout is codegraph_change_reader.FilesLayout` holds); `build_test_
layout` stays in codegraph, untouched apart from that, and keeps
constructing it with a resolved `tests_subdir` when one is declared.

ALLOWED IMPORTS: the stdlib, nothing else — the same bar `codegraph_
snapshot_reader.py` holds itself to, for the same reason (no new weight on
hs-run's own import path).
"""
from __future__ import annotations

import ast
import configparser
import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# ── import-edge walk (moved from codegraph.change.selection) ────────────────
#
# `_modules`/`_module_by_path`/`_base_target`/`_reverse_import_edges`/
# `import_channel` operate on an already-loaded snapshot index dict alone —
# no reference to `codegraph._core.snapshot_bridge.bridge_records` or
# `codegraph._core.testfiles.FilesLayout`, the two names `selection.py`
# imports for its OTHER channels (`string`, and the bridged-test augment).
# Those stay in `selection.py`; only the pure walk moved.

def _modules(idx: dict) -> "list[dict]":
    """Every module record across every unit, plus the snapshot's
    `test_modules` (test-tree files no root measures — a workspace dist's
    `tests_<x>/` beside `src/`) — the flat iteration shape every channel
    below shares. A test importing a module is an edge whether or not a
    root happened to cover the test's directory."""
    out: "list[dict]" = []
    for mods in idx["units"].values():
        out.extend(mods)
    out.extend(idx.get("test_modules") or ())
    return out


def _module_by_path(idx: dict) -> "dict[str, dict]":
    return {m["path"]: m for m in _modules(idx)}


def _base_target(name: str) -> str:
    """`from pkg.mod import name` targets are stored `pkg.mod::name`
    (the snapshot's own convention, see `codegraph._core.query.engine.
    QueryEngine.importers`'s `base_module`); a bare `import pkg.mod` target
    carries no `::` at all. One door, matching that engine's own reading."""
    return name.split("::", 1)[0]


def _reverse_import_edges(idx: dict) -> "dict[str, set[str]]":
    """imported dotted module name -> the set of modules importing it
    DIRECTLY (module-level and function-local import edges both count, same
    as `QueryEngine.importers`). `import_channel` walks this transitively;
    this function only ever returns the one-hop edge."""
    reverse: "dict[str, set[str]]" = {}
    for m in _modules(idx):
        importer = m["module"]
        imported = ({_base_target(t) for t in m["imports_module_level"]}
                    | {_base_target(t) for t in m["imports_function_local"]})
        for tgt in imported:
            reverse.setdefault(tgt, set()).add(importer)
    return reverse


def fan_in(idx: dict, paths: "Iterable[str]") -> "dict[str, int]":
    """{path: number of DIRECT (one-hop) importers}, for every path in `paths`
    the snapshot actually indexes. A path absent from the snapshot is simply
    absent from the result -- never folded in as `0`, which would read as
    "measured, zero importers" rather than "not in this snapshot at all".

    Built on the exact same one-hop edge `hs_run_facts.code_importers` reads
    for its own per-subject IMPORTER LIST (`_reverse_import_edges`) -- this
    is the COUNT shape a caller that only needs "how many", not "which
    ones", reads directly instead of building the list and counting it by
    hand at each call site (`simplify_gate.py`'s fan-in nudge is the first
    of at least two callers with that exact need)."""
    by_path = _module_by_path(idx)
    reverse = _reverse_import_edges(idx)
    out: "dict[str, int]" = {}
    for p in paths:
        m = by_path.get(p)
        if m is None:
            continue
        out[p] = len(reverse.get(m["module"], set()))
    return out


def import_channel(idx: dict, changed_modules: "Iterable[str]") -> "set[str]":
    """Every module reached from `changed_modules` by walking the reverse
    import graph transitively (reverse BFS, unbounded depth) — the modules
    that import something changed, directly or through a chain of imports.

    WHAT IT CANNOT SEE
        * Exact dotted-name match only: `import pkg.mod as m` or a
          re-export surfaced under a different public name is invisible —
          the same blind spot `QueryEngine.importers` already carries.
        * A test reaching the change only via a conftest fixture, a
          subprocess call, or runtime dispatch (getattr/registry/string
          table) has no import edge to walk at all.
    """
    reverse = _reverse_import_edges(idx)
    seen: "set[str]" = set()
    stack = list(changed_modules)
    while stack:
        cur = stack.pop()
        for importer in reverse.get(cur, ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return seen


def import_channel_depths(idx: dict, changed_modules: "Iterable[str]") -> "dict[str, int]":
    """A SIBLING of `import_channel`, never a replacement — `import_channel`
    itself keeps its exact `set[str]` return type and its own DFS-by-stack
    body untouched, because `codegraph.change.selection` REBINDS the name
    (`selection.import_channel = codegraph_change_reader.import_channel`)
    and a test in `test_codegraph_change_reader.py` asserts that identity
    holds; changing what `import_channel` returns would break that rebind's
    caller without a single line here saying so.

    Same reverse-import graph, same reachability — but walked as a genuine
    level-by-level BFS (not `import_channel`'s DFS, which reaches the same
    SET but not necessarily each module's shortest hop count) so every
    reached module also gets its MINIMUM depth: a direct (one-hop) importer
    of `changed_modules` is depth 1, a module that imports one of those is
    depth 2, and so on. `hs_run_facts.code_tests_reached` reads this to
    order a fact's `value` by relevance (nearest tests first) instead of by
    path alone — a path-only order lets an alphabetically-early transitive
    hit outrank a direct importer once a downstream cap trims the list,
    which is backwards for `cook`'s own read of the fact."""
    reverse = _reverse_import_edges(idx)
    depth_of: "dict[str, int]" = {}
    frontier = [m for m in changed_modules]
    seen = set(frontier)
    depth = 0
    while frontier:
        depth += 1
        next_frontier: "list[str]" = []
        for cur in frontier:
            for importer in reverse.get(cur, ()):
                if importer not in seen:
                    seen.add(importer)
                    depth_of[importer] = depth
                    next_frontier.append(importer)
        frontier = next_frontier
    return depth_of


# ── test-file predicates (moved from codegraph._core.testfiles) ─────────────
#
# `is_test_dir`/`in_test_tree`/`is_test_file` here are the UNDECLARED-
# convention-only door (see this module's own docstring for why the
# DECLARED `tests_subdir` path, `FilesLayout`/`build_test_layout`, stayed
# behind) — the exact same bare functions `codegraph._core.testfiles`
# already kept beside its own `FilesLayout` for "a caller with no
# descriptor in hand at all" (that module's own docstring).

#: pytest's own default `python_files` glob list — used whenever no target
#: config declares its own.
DEFAULT_TEST_PATTERNS: "tuple[str, ...]" = ("test_*.py", "*_test.py")

#: The UNDECLARED-convention directory shape — consulted only when no
#: `layout.tests_subdir` names the one exact directory that counts.
_TEST_DIR_RE = re.compile(r"^tests?(_.+)?$")


def _is_test_segment(seg: str) -> bool:
    return bool(_TEST_DIR_RE.match(seg))


def is_test_dir(unit_or_dir: str) -> bool:
    """Any segment of a directory- or unit-shaped key (`<root-id>/<unit-
    name>`, or a bare directory path — carries NO filename) is shaped like
    a test tree. Every segment is checked; unlike `in_test_tree`, there is
    no trailing basename to exclude."""
    segs = [s for s in unit_or_dir.replace("\\", "/").split("/") if s]
    return any(_is_test_segment(s) for s in segs)


def in_test_tree(rel: str) -> bool:
    """Some ANCESTOR directory segment of the FILE path `rel` is shaped
    like a test tree — "is this test code", basename irrelevant. The
    file's own basename is excluded from the check (a leaf file is not its
    own ancestor)."""
    parent = Path(rel.replace("\\", "/")).parent.as_posix()
    if parent in ("", "."):
        return False
    return is_test_dir(parent)


def is_test_file(rel: str, patterns: "Iterable[str] | None" = None) -> bool:
    """`in_test_tree(rel)` AND the basename matches one of `patterns`
    (defaults to pytest's own `DEFAULT_TEST_PATTERNS`). A SUBSET of
    `in_test_tree` by construction."""
    if not in_test_tree(rel):
        return False
    pats = tuple(patterns) if patterns is not None else DEFAULT_TEST_PATTERNS
    name = Path(rel).name
    return any(fnmatch.fnmatch(name, pat) for pat in pats)


def _python_files_from_toml(pyproject: Path) -> "tuple[str, ...] | None":
    try:
        import tomllib
    except ImportError:  # pragma: no cover — no Python this old ships tomllib
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    section = (data.get("tool", {}) or {}).get("pytest", {}) or {}
    raw = (section.get("ini_options", {}) or {}).get("python_files")
    if not raw:
        return None
    if isinstance(raw, str):
        return tuple(raw.split())
    return tuple(str(x) for x in raw)


def _python_files_from_ini(path: Path, section: str) -> "tuple[str, ...] | None":
    if not path.is_file():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except configparser.Error:
        return None
    if not cp.has_option(section, "python_files"):
        return None
    raw = cp.get(section, "python_files").split()
    return tuple(raw) if raw else None


def python_files_patterns(root: Path) -> "tuple[str, ...]":
    """`root`'s own declared `python_files` glob list, pytest's own
    precedence (`pyproject.toml` -> `pytest.ini` -> `setup.cfg` ->
    `tox.ini`); undeclared everywhere -> `DEFAULT_TEST_PATTERNS`."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        pats = _python_files_from_toml(pyproject)
        if pats:
            return pats
    for fname, section in (("pytest.ini", "pytest"), ("setup.cfg", "tool:pytest"),
                           ("tox.ini", "pytest")):
        pats = _python_files_from_ini(root / fname, section)
        if pats:
            return pats
    return DEFAULT_TEST_PATTERNS


# ── symbol table + caller-symbol edges ───────────────────────────────────────
#
# `walk_symbols` MOVED from `codegraph._core.query.engine.walk_symbols`;
# `qualified_index`/`callers_index`/`CallersIndexMissing` MOVED from
# `codegraph.change.impact` — the same "MOVED here, REBOUND" law the
# import-edge walk above follows (`codegraph._core.query.engine.walk_symbols
# is codegraph_change_reader.walk_symbols` holds, same for the other three
# against `codegraph.change.impact`).
#
# `qualified_index`'s value shape is a SUPERSET of what either origin call
# site alone needed: `codegraph.change.impact.tier_warnings` reads `dist`
# (cross-group / no-upward-import detection — a real, tested feature, not
# decoration) and `run_impact` reads `path`; `hs_run_facts.code_callers`
# reads `path` and `lines` (a call-SITE line scan needs the definition span
# to bound it — see that module's own docstring). One shape carrying all
# four (`module`/`path`/`dist`/`parent`/`lines`) serves every reader; picking
# either origin's narrower shape would have silently dropped the OTHER
# reader's field — `dist` in particular: a rebind that lost it would not
# fail loudly, `tier_warnings`'s `if not t_dist: continue` just stops
# warning, which is exactly the kind of drift `no-orphan-code.md` calls a
# reachability check, not an in-degree one, catches.

def walk_symbols(idx: dict):
    """`(unit, module_dict, symbol_dict, parent_name|None)` for every symbol
    across every unit, methods included."""
    for unit, mods in idx["units"].items():
        for m in mods:
            for s in m.get("symbols", ()) or ():
                yield unit, m, s, None
                for mm in s.get("methods", []) or []:
                    yield unit, m, mm, s["name"]


def qualified_index(idx: dict) -> "dict[str, dict]":
    """`module::name` -> `{module, path, dist, parent, lines}` for every
    symbol/method. `dist` is the symbol's own group tag (falling back to its
    module's) that `tier_warnings` ranks by; `parent` is the owning class
    name for a method, `None` for a top-level symbol; `lines` is the
    symbol's own DEFINITION span (`[start, end]`, as written by the
    adapter) — never a call-site line; see `hs_run_facts.code_callers`'s own
    docstring for what that costs and how it is resolved there."""
    out: "dict[str, dict]" = {}
    for _unit, m, s, parent in walk_symbols(idx):
        q = "%s::%s" % (m["module"], s["name"])
        out.setdefault(q, {"module": m["module"], "path": m["path"],
                          "dist": s.get("dist") or m.get("dist"),
                          "parent": parent, "lines": s.get("lines")})
    return out


class CallersIndexMissing(LookupError):
    """The snapshot carries no `callers` index — refused rather than read as
    empty: an empty index is a real, different claim ("nobody calls
    anything"), never the same as "this snapshot never measured it"."""


def reexport_index(idx: dict) -> "dict[str, str]":
    """`M::n` -> `X::n` for every module-level `from X import n` in a measured
    module `M` -- the binding a caller reaches when it imports `n` from `M`
    rather than from where `n` is defined. An `as` alias binds a name the
    snapshot does not keep, so it is not here (a lower bound, like every
    other ref)."""
    out: "dict[str, str]" = {}
    for _unit, mods in idx.get("units", {}).items():
        for m in mods:
            for tgt in m.get("imports_module_level") or ():
                if "::" in tgt:
                    out.setdefault("%s::%s" % (m["module"], tgt.partition("::")[2]), tgt)
    return out


def follow_reexports(ref: str, defined: "dict[str, Any]", reexports: "dict[str, str]") -> "str | None":
    """The definition `ref` reaches through re-export bindings, or None (a
    cycle, or a chain ending at something the snapshot does not define)."""
    seen: "set[str]" = set()
    while ref not in defined:
        if ref in seen or ref not in reexports:
            return None
        seen.add(ref)
        ref = reexports[ref]
    return ref


def callers_index(idx: dict) -> "tuple[dict[str, list[str]], int, int]":
    """The snapshot's own `callers` index (ref string -> callers, written by
    `codegraph._core.snapshot.pipeline.callers_of`), keyed by the symbol
    each ref resolves to -> `(target -> sorted unique qualified callers,
    outside_tree_callers, ambiguous_callers)`.

    `codegraph.change.impact.callers_index` IS this function: a ref is
    qualified or a bare name; a qualified ref the snapshot does not define
    is followed through re-exports (`reexport_index`) to the definition it
    names; a bare name resolves only when exactly ONE definition carries it
    (two sharing it credits the caller to NEITHER — picking one would make
    the answer depend on set iteration order); a ref still unresolved is an
    outside-tree caller, counted, never silently dropped."""
    raw = idx.get("callers")
    if raw is None:
        raise CallersIndexMissing(
            "snapshot %s carries no callers index -- re-take it" % idx.get("snapshot", "?"))
    defined = qualified_index(idx)
    reexports = reexport_index(idx)
    by_bare: "dict[str, list[str]]" = {}
    for q in defined:
        by_bare.setdefault(q.partition("::")[2], []).append(q)
    callers: "dict[str, set[str]]" = {}
    outside = ambiguous = 0
    for ref, who in raw.items():
        if "::" in ref:
            hit = follow_reexports(ref, defined, reexports)
            hits = [hit] if hit else []
        else:
            hits = by_bare.get(ref, [])
        if len(hits) == 1:
            callers.setdefault(hits[0], set()).update(who)
        elif hits:
            ambiguous += len(who)
        else:
            outside += len(who)
    return {k: sorted(v) for k, v in sorted(callers.items())}, outside, ambiguous


@dataclass(frozen=True)
class FilesLayout:
    """The resolved answer to "what counts as a test tree/test file" for
    ONE (root, descriptor) pair — built once by `codegraph._core.testfiles.
    build_test_layout` (which stays in `codegraph`, since resolving a
    DECLARED `tests_subdir` needs its descriptor loader), its three methods
    the same shape as this module's own bare `is_test_dir`/`in_test_tree`/
    `is_test_file` functions (which stay as the UNDECLARED-convention-only
    door: a caller with no descriptor in hand at all, or a fixture-level
    unit test, still has a plain function to call)."""
    tests_subdir: "str | None"
    patterns: "tuple[str, ...]"

    def is_test_dir(self, unit_or_dir: str) -> bool:
        segs = [s for s in unit_or_dir.replace("\\", "/").split("/") if s]
        if self.tests_subdir is not None:
            return any(s == self.tests_subdir for s in segs)
        return any(_is_test_segment(s) for s in segs)

    def in_test_tree(self, rel: str) -> bool:
        parent = Path(rel.replace("\\", "/")).parent.as_posix()
        if parent in ("", "."):
            return False
        return self.is_test_dir(parent)

    def is_test_file(self, rel: str) -> bool:
        if not self.in_test_tree(rel):
            return False
        name = Path(rel).name
        return any(fnmatch.fnmatch(name, pat) for pat in self.patterns)


# ── the string channel (moved from codegraph.change.selection) ──────────────
#
# Three ways a test names what a change touched without importing it:
# `path_drivers` (the snapshot's own path/`-m` citations of a module), a
# `<stem>.py` text mention of a changed MODULE, and a string literal naming a
# repo-relative PATH -- a data file, a doc, a directory -- which is how a gate
# reading `docs/decisions.yaml` or scanning every file under `docs/` names it.

_GLOB_CHARS = frozenset("*?[")
_LITERAL_CACHE: "dict[tuple[str, int, int], frozenset[str]]" = {}


def _path_literal(value: str) -> "str | None":
    s = value.strip()
    while s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    if (not s or s == "." or len(s) > 300 or s.startswith("/") or "://" in s
            or any(c.isspace() for c in s) or ".." in s.split("/") or "{" in s):
        return None
    if _GLOB_CHARS & set(s):
        # A glob names paths only when anchored under a literal directory
        # (`docs/*.md`); a bare `*`, `*.yaml` or a regex class names everything.
        head, sep, _ = s.partition("/")
        if not sep or _GLOB_CHARS & set(head):
            return None
    return s


def _is_join_call(node: "ast.Call") -> bool:
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
    return name in ("join", "joinpath", "Path", "PurePath", "PurePosixPath")


def path_literals(text: str) -> "frozenset[str]":
    """Every repo-relative path a Python source spells as string literals: a
    single literal (`"docs/a.md"`, `"docs"`), a `/`-chain run of literals
    (`ROOT / "docs" / "decisions.yaml"` -> `docs/decisions.yaml`), or the
    literal arguments of a `join`/`joinpath`/`Path(...)` call. A literal that
    is part of a run counts only as the joined run, never alone: `ROOT /
    "docs" / "x.md"` names `docs/x.md`, not the whole `docs` directory.
    Unparseable source names nothing."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return frozenset()
    out: "set[str]" = set()
    consumed: "set[int]" = set()
    inner: "set[int]" = set()

    def flatten(node: ast.AST) -> "list[ast.AST]":
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            inner.add(id(node))
            return flatten(node.left) + flatten(node.right)
        return [node]

    def runs(parts: "list[ast.AST]") -> None:
        cur: "list[str]" = []
        for p in [*parts, None]:
            if isinstance(p, ast.Constant) and isinstance(p.value, str):
                consumed.add(id(p))
                cur.append(p.value.strip("/"))
                continue
            if cur:
                lit = _path_literal("/".join(cur))
                if lit:
                    out.add(lit)
                cur = []

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and id(node) not in inner:
            runs(flatten(node))
        elif isinstance(node, ast.Call) and len(node.args) >= 2 and _is_join_call(node):
            runs(list(node.args))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in consumed:
            lit = _path_literal(node.value)
            if lit:
                out.add(lit)
    return frozenset(out)


def names_path(literal: str, changed: str) -> bool:
    """`literal` names `changed`: the same path, a directory holding it, or a
    glob matching it."""
    if _GLOB_CHARS & set(literal):
        return (fnmatch.fnmatchcase(changed, literal)
                or fnmatch.fnmatchcase(changed, literal + "/*"))
    return changed == literal or changed.startswith(literal + "/")


def _literals_of(path: Path) -> "frozenset[str]":
    try:
        st = path.stat()
    except OSError:
        return frozenset()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _LITERAL_CACHE.get(key)
    if hit is None:
        try:
            hit = path_literals(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            hit = frozenset()
        _LITERAL_CACHE[key] = hit
    return hit


def string_channel(idx: dict, changed_paths: "Iterable[str]", root: Path,
                   layout: "FilesLayout") -> "tuple[set[str], dict[str, int]]":
    """Tests naming a changed path -> `(selected test paths, {"path_drivers": N,
    "text_fallback": N, "path_literals": N})`; the stats say which sub-channel
    earned each pick.

        path_drivers   the snapshot's own path/`-m` citations of a changed module.
        text_fallback  a changed `.py` file's `<stem>.py` anywhere in a test's text
                       (a bare filename in an f-string or a fixture ID).
        path_literals  a string literal of the test naming the changed path, a
                       directory holding it, or a glob matching it (`path_literals`).
    """
    changed_paths = list(changed_paths)
    by_path = _module_by_path(idx)
    selected: "set[str]" = set()
    via_drivers: "set[str]" = set()
    for cp in changed_paths:
        m = by_path.get(cp)
        if not m:
            continue
        for driver in m.get("path_drivers") or ():
            if layout.is_test_file(driver):
                selected.add(driver)
                via_drivers.add(driver)

    stems = {os.path.basename(cp) for cp in changed_paths if cp.endswith(".py")}
    via_fallback: "set[str]" = set()
    via_literals: "set[str]" = set()
    for m in _modules(idx):
        tp = m["path"]
        if not layout.is_test_file(tp) or tp in selected:
            continue
        if stems:
            try:
                text = (Path(root) / tp).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(stem in text for stem in stems):
                selected.add(tp)
                via_fallback.add(tp)
                continue
        if any(names_path(lit, cp) for lit in _literals_of(Path(root) / tp)
               for cp in changed_paths):
            selected.add(tp)
            via_literals.add(tp)
    return selected, {"path_drivers": len(via_drivers), "text_fallback": len(via_fallback),
                      "path_literals": len(via_literals)}


# ── the whole_tree channel's codemap rows (`test_select:`) ──────────────────

#: `codemap.yaml` `test_select:` -- a list of rows, each `{kind, test, why?}`.
#: `kind: whole_tree`: `test` (a repo-relative test file, or a glob of them) is
#: selected on every change, because it scans the tree rather than importing
#: what it checks. Parsed here, stdlib only, so hs-run's `code.tests_reached`
#: reads the rows the descriptor loader validates through the same function.
TEST_SELECT_KINDS: "tuple[str, ...]" = ("whole_tree",)
TEST_SELECT_KEYS: "frozenset[str]" = frozenset({"kind", "test", "why"})


def parse_test_select(raw: Any) -> "tuple[dict, ...]":
    """`test_select:` -> `({kind, test, why}, ...)`; ValueError names what is wrong."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("`test_select:` must be a list of rows")
    out: "list[dict]" = []
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ValueError(f"test_select[{i}] must be a mapping")
        unknown = sorted(set(row) - TEST_SELECT_KEYS)
        if unknown:
            raise ValueError(f"test_select[{i}]: unknown keys {', '.join(unknown)} "
                             f"(valid: {', '.join(sorted(TEST_SELECT_KEYS))})")
        if row.get("kind") not in TEST_SELECT_KINDS:
            raise ValueError(f"test_select[{i}]: kind {row.get('kind')!r} -- known: "
                             f"{', '.join(TEST_SELECT_KINDS)}")
        test = row.get("test")
        if (not isinstance(test, str) or not test or test.startswith("/")
                or ".." in test.split("/")):
            raise ValueError(f"test_select[{i}]: `test` must be a repo-relative path or glob")
        why = row.get("why")
        if why is not None and not isinstance(why, str):
            raise ValueError(f"test_select[{i}]: `why` must be a string")
        out.append({"kind": row["kind"], "test": test, "why": why})
    return tuple(out)


def whole_tree_tests(root: Path, entries: "Iterable[str]") -> "tuple[set[str], list[str]]":
    """`(test files, entries naming no file)` -- a glob expands over the tree
    (matching nothing selects nothing); a path stands for itself, and is also
    named in the second list when no such file exists, so a dead row is seen
    rather than silently run as a missing file."""
    root = Path(root)
    found: "set[str]" = set()
    dead: "list[str]" = []
    for e in sorted(set(entries)):
        if _GLOB_CHARS & set(e):
            hits = {p.relative_to(root).as_posix() for p in root.glob(e) if p.is_file()}
            found |= hits
        else:
            found.add(e)
            hits = {e} if (root / e).is_file() else set()
        if not hits:
            dead.append(e)
    return found, dead
