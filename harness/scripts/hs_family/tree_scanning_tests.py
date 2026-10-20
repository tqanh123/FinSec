"""hs_family.tree_scanning_tests — which test files enumerate the repo tree.

A test file counts when it enumerates files under the tree it lives in: an
`rglob`, a `glob` with `**`, an `os.walk`, or a `git ls-files`, whose target is
reached from the file's own `__file__` — directly, through assignments, loop
targets, comprehensions, `with` targets, parameter defaults, or an argument
passed to a function defined in the same file. Reading the manifest's inventory
(`<root> / ".../manifest.json"`) enumerates the tree too: it lists every file.

A call into another module counts when it passes a `__file__`-derived argument
to a function that enumerates a tree reached from its own parameters, directly
or through a sibling in its module. The module is found the way the test imports
it: under the directories the test file and the conftests above it put on
`sys.path` (evaluated from their `__file__`), or the test's own directory — a
helper under tests/ and a product module under release/ are read alike.

Every suite root `harness/data/test-strategy.yaml` declares is read, subdirectories
included, so a scan in `release/tests` counts the same as one in `harness/tests`.

A scan of a tmp dir, a scratch repo, or an installed copy is not counted: its
target never reaches `__file__`. An `ls-files` whose every pathspec names one
file is a lookup, not a scan.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import artifact_io
import bounded_cache_dir

from .registry import RegistryError


_INVENTORY = "manifest.json"
_CACHE_KEEP = 16


def _names(node) -> Set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _direct(scope) -> list:
    """The nodes of one scope — a module or a function body — without the bodies of
    the functions nested in it (their defaults and decorators stay here)."""
    out: list = []
    stack = list(scope.body)
    while stack:
        n = stack.pop()
        out.append(n)
        if isinstance(n, _FUNCS):
            stack += n.args.defaults + [d for d in n.args.kw_defaults if d] + n.decorator_list
            continue
        stack += list(ast.iter_child_nodes(n))
    return out


def _params(fn) -> List[str]:
    return [a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs]


def _bindings(nodes, funcs) -> Tuple[list, list, Set[str]]:
    """`(pairs, calls, bound)` of one scope: `(target, value)` per binding, `(function,
    param, value)` per argument or default that binds a parameter of a function in
    this module, and every name the scope binds."""
    pairs: list = []
    calls: list = []
    bound: Set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Assign):
            pairs += [(t, n.value) for t in n.targets]
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None:
            pairs.append((n.target, n.value))
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs.append((n.target, n.iter))
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            pairs.append((n.optional_vars, n.context_expr))
        elif isinstance(n, ast.NamedExpr):
            pairs.append((n.target, n.value))
        elif isinstance(n, _FUNCS):
            bound.add(n.name)
            pos = n.args.args[len(n.args.args) - len(n.args.defaults):]
            calls += [(n, a.arg, d) for a, d in zip(pos, n.args.defaults)]
            calls += [(n, a.arg, d) for a, d in zip(n.args.kwonlyargs, n.args.kw_defaults)
                      if d is not None]
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            for fn in funcs.get(n.func.id, ()):
                params = [a.arg for a in fn.args.args]
                calls += [(fn, p, a) for p, a in zip(params, n.args)]
                calls += [(fn, k.arg, k.value) for k in n.keywords if k.arg in params]
    for target, _ in pairs:
        bound |= _names(target)
    return pairs, calls, bound


class _Scopes:
    """Which names reach `__file__` in each scope of one module. A name is read in
    the scope that binds it — an assignment in one function never taints the same
    name in another — and a name a scope does not bind is read from the one around it.
    `seed` maps a scope to names taken as reached (a function's own parameters)."""

    def __init__(self, tree, seed=None):
        self.tree = tree
        self.parent: Dict[object, object] = {tree: None}
        self.nodes: Dict[object, list] = {}
        todo = [tree]
        while todo:
            sc = todo.pop()
            self.nodes[sc] = _direct(sc)
            for n in self.nodes[sc]:
                if isinstance(n, _FUNCS):
                    self.parent[n] = sc
                    todo.append(n)
        funcs: Dict[str, list] = {}
        for sc in self.parent:
            if isinstance(sc, _FUNCS):
                funcs.setdefault(sc.name, []).append(sc)
        self.pairs: Dict[object, list] = {}
        self.calls: Dict[object, list] = {}
        self.bound: Dict[object, Set[str]] = {}
        for sc, nodes in self.nodes.items():
            pairs, calls, bound = _bindings(nodes, funcs)
            if isinstance(sc, _FUNCS):
                bound |= set(_params(sc))
            self.pairs[sc] = [(_names(t), _names(v)) for t, v in pairs]
            self.calls[sc] = [(fn, param, _names(v)) for fn, param, v in calls]
            self.bound[sc] = bound
        self.taint = {sc: set((seed or {}).get(sc, ())) for sc in self.parent}
        changed = True
        while changed:
            changed = False
            for sc in self.parent:
                eff = self.tainted(sc) | {"__file__"}
                for targets, sources in self.pairs[sc]:
                    new = targets - self.taint[sc]
                    if new and sources & eff:
                        self.taint[sc] |= new
                        changed = True
                for fn, param, sources in self.calls[sc]:
                    if param not in self.taint[fn] and sources & eff:
                        self.taint[fn].add(param)
                        changed = True
        self.eff = {sc: self.tainted(sc) | {"__file__"} for sc in self.parent}

    def tainted(self, sc) -> Set[str]:
        out: Set[str] = set()
        shadow: Set[str] = set()
        while sc is not None:
            out |= self.taint[sc] - shadow
            shadow |= self.bound[sc]
            sc = self.parent[sc]
        return out

    def reaches(self, node, sc) -> bool:
        return bool(_names(node) & self.eff[sc])

    def top(self, sc):
        """The module-level function `sc` sits in, or the module."""
        while self.parent[sc] is not None and self.parent[sc] is not self.tree:
            sc = self.parent[sc]
        return sc


def _callee(f):
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return "%s.%s" % (f.value.id, f.attr)
    return None


def _flat_args(call) -> list:
    out = []
    for a in call.args:
        out += list(a.elts) if isinstance(a, (ast.List, ast.Tuple)) else [a]
    return out


def _names_one_file(spec: str) -> bool:
    """A pathspec naming one file (`docs/x.md`, `a/.gitkeep`), not a directory
    (`harness/`, `.claude/`) or a glob."""
    name = Path(spec).name
    return (not spec.endswith("/") and not any(ch in spec for ch in "*?[")
            and bool(Path(spec).suffix or name.startswith(".")))


def _ls_files_target(call):
    """The expression an `ls-files` call runs against: `cwd=`, else its non-constant
    arguments (`-C <dir>`, a helper's repo argument), else `__file__` — the process
    cwd, which is the repo under pytest. None for a one-file lookup."""
    args = _flat_args(call)
    consts = [a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    if "ls-files" not in consts:
        return None
    after = consts[consts.index("ls-files") + 1:]
    specs = [c for c in after if not c.startswith("-")]
    if specs and all(_names_one_file(c) for c in specs):
        return None
    cwd = [k.value for k in call.keywords if k.arg == "cwd"]
    if cwd:
        return cwd[0]
    dynamic = [a for a in args if not isinstance(a, ast.Constant)]
    return ast.Tuple(elts=dynamic, ctx=ast.Load()) if dynamic else ast.Name(id="__file__")


def _consts(tree) -> Dict[str, str]:
    """Module-level `NAME = "string"` bindings."""
    return {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
            for t in n.targets if isinstance(t, ast.Name)}


def _tail(node, consts: Dict[str, str]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return consts.get(node.id, "") if isinstance(node, ast.Name) else ""


def _enum_targets(c, scanners: FrozenSet[str], consts: Dict[str, str]):
    """What one node enumerates — the tree it walks, or the arguments of a call into
    a scanner — or None when it enumerates nothing."""
    if isinstance(c, ast.BinOp) and isinstance(c.op, ast.Div):
        return [c.left] if _tail(c.right, consts).endswith(_INVENTORY) else None
    if not isinstance(c, ast.Call):
        return None
    f = c.func
    if isinstance(f, ast.Attribute) and f.attr == "rglob":
        return [f.value]
    if (isinstance(f, ast.Attribute) and f.attr == "glob" and c.args
            and isinstance(c.args[0], ast.Constant) and "**" in str(c.args[0].value)):
        return [f.value]
    if _callee(f) == "os.walk":
        return c.args[:1]
    if _callee(f) in scanners:
        return list(c.args) + [k.value for k in c.keywords]
    t = _ls_files_target(c)
    return None if t is None else [t]


def _hits(tree, scanners: FrozenSet[str] = frozenset(), seed=None, scopes=None) -> list:
    """`(line, module-level function or module, scanner or None)` per enumeration
    whose target reaches `__file__`; the scanner is the callee it went through."""
    consts = _consts(tree)
    sc = scopes or _Scopes(tree, seed)
    out = []
    for scope, nodes in sc.nodes.items():
        for c in nodes:
            targets = _enum_targets(c, scanners, consts)
            if targets and any(sc.reaches(t, scope) for t in targets):
                via = _callee(c.func) if isinstance(c, ast.Call) else None
                out.append((c.lineno, sc.top(scope), via if via in scanners else None))
    return out


def _read(path: Path, reads: Optional[Dict[Path, str]] = None) -> str:
    """The one read site every scanner routes through — records `path`'s content
    hash into `reads` (when tracking) so a warm cache check can revalidate by
    rehashing, never by re-parsing."""
    raw = path.read_bytes()
    if reads is not None:
        reads[path] = hashlib.sha256(raw).hexdigest()
    return raw.decode("utf-8")


def _enumerators(path: Path, cache: Dict[Path, FrozenSet[str]],
                  reads: Optional[Dict[Path, str]] = None) -> FrozenSet[str]:
    """The module-level functions of the module at `path` that enumerate a tree
    reached from one of their parameters, directly or by passing one to a sibling
    that does."""
    if path not in cache:
        tree = ast.parse(_read(path, reads))
        funcs = {n.name: n for n in tree.body if isinstance(n, _FUNCS)}
        found: Set[str] = set()
        edges: Dict[str, Set[str]] = {}
        for _, top, via in _hits(tree, frozenset(funcs),
                                 seed={fn: set(_params(fn)) for fn in funcs.values()}):
            if isinstance(top, _FUNCS):
                if via is None:
                    found.add(top.name)
                else:
                    edges.setdefault(top.name, set()).add(via)
        changed = True
        while changed:
            changed = False
            for name, to in edges.items():
                if name not in found and to & found:
                    found.add(name)
                    changed = True
        cache[path] = frozenset(found)
    return cache[path]


_SAME = frozenset({"Path", "pathlib.Path", "str", "os.fspath", "os.path.abspath",
                   "os.path.realpath"})


def _eval_path(node, env: Dict[str, Path], file: Path) -> Optional[Path]:
    """The path an expression built from `__file__` names, or None."""
    if isinstance(node, ast.Name):
        return file if node.id == "__file__" else env.get(node.id)
    if isinstance(node, ast.Call):
        name, args = _callee(node.func), node.args
        if name in _SAME and len(args) == 1:
            return _eval_path(args[0], env, file)
        if name == "os.path.dirname" and len(args) == 1:
            p = _eval_path(args[0], env, file)
            return p.parent if p else None
        if (isinstance(node.func, ast.Attribute) and node.func.attr in ("resolve", "absolute")
                and not args):
            return _eval_path(node.func.value, env, file)
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        p = _eval_path(node.value, env, file)
        return p.parent if p else None
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)):
        p = _eval_path(node.value.value, env, file)
        return p.parents[node.slice.value] if p and node.slice.value < len(p.parents) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        p = _eval_path(node.left, env, file)
        r = node.right
        return p / r.value if p and isinstance(r, ast.Constant) and isinstance(r.value, str) else None
    return None


def _path_inserts(tree, file: Path) -> List[Path]:
    """The directories a module puts on `sys.path`, evaluated from its `__file__`."""
    env: Dict[str, Path] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            p = _eval_path(n.value, env, file)
            if p is not None:
                env[n.targets[0].id] = p
    out = []
    for c in ast.walk(tree):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr in ("insert", "append") and _callee(c.func.value) == "sys.path"
                and c.args):
            p = _eval_path(c.args[-1], env, file)
            if p is not None:
                out.append(p)
    return out


def _module_file(dotted: str, roots: List[Path], seen: Dict,
                  probes: Optional[Dict[Path, bool]] = None) -> Optional[Path]:
    parts = dotted.split(".")
    for r in roots:
        key = (r, dotted)
        if key not in seen:
            hit = None
            for f in (r.joinpath(*parts[:-1], parts[-1] + ".py"),
                      r.joinpath(*parts, "__init__.py")):
                exists = f.is_file()
                if probes is not None:
                    probes[f] = exists
                if exists:
                    hit = f
                    break
            seen[key] = hit
        if seen[key] is not None:
            return seen[key]
    return None


def _imports(nodes, wanted: Set[str], roots: List[Path], seen,
             probes: Optional[Dict[Path, bool]] = None) -> Dict[str, Tuple[Path, Optional[str]]]:
    """`{local name: (module file, attribute or None)}` for each import binding a name
    in `wanted` that resolves under `roots`."""
    out: Dict[str, Tuple[Path, Optional[str]]] = {}
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                dotted = a.name if a.asname else a.name.split(".")[0]
                local = a.asname or dotted
                f = _module_file(dotted, roots, seen, probes) if local in wanted else None
                if f is not None:
                    out[local] = (f, None)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            for a in n.names:
                local = a.asname or a.name
                if local not in wanted:
                    continue
                sub = _module_file("%s.%s" % (n.module, a.name), roots, seen, probes)
                f = sub or _module_file(n.module, roots, seen, probes)
                if f is not None:
                    out[local] = (f, None if sub else a.name)
    return out


def _scanners(scopes: _Scopes, roots: List[Path], cache, seen,
              reads: Optional[Dict[Path, str]] = None,
              probes: Optional[Dict[Path, bool]] = None) -> FrozenSet[str]:
    """The callee spellings in this file that reach an enumerating function elsewhere,
    read only for the calls that pass a `__file__`-reached argument."""
    wanted: Set[str] = set()
    for scope, nodes in scopes.nodes.items():
        for c in nodes:
            name = _callee(c.func) if isinstance(c, ast.Call) else None
            if name and any(scopes.reaches(a, scope)
                            for a in list(c.args) + [k.value for k in c.keywords]):
                wanted.add(name.split(".")[0])
    out: Set[str] = set()
    if not wanted:
        return frozenset()
    nodes = [n for ns in scopes.nodes.values() for n in ns]
    for local, (f, attr) in _imports(nodes, wanted, roots, seen, probes).items():
        fns = _enumerators(f, cache, reads)
        if attr is None:
            out |= {"%s.%s" % (local, fn) for fn in fns}
        elif attr in fns:
            out.add(local)
    return frozenset(out)


def scanning_test_files_in(tests_dir, *, reads: Optional[Dict[Path, str]] = None,
                            probes: Optional[Dict[Path, bool]] = None) -> Dict[Path, List[int]]:
    """`{test file: [line of each enumeration]}` for every `test_*.py` under
    `tests_dir` that enumerates the tree it lives in. `reads`/`probes`, when given,
    collect the content hash of every file read and the hit/miss of every import
    candidate probed — a cache's content key, not part of the public contract."""
    tests_dir = Path(tests_dir).resolve()
    if not tests_dir.is_dir():
        return {}
    cache: Dict[Path, FrozenSet[str]] = {}
    seen: Dict = {}
    conftest: Dict[Path, List[Path]] = {}
    for c in tests_dir.rglob("conftest.py"):
        conftest[c.parent] = [c.parent] + _path_inserts(ast.parse(_read(c, reads)), c)
    out: Dict[Path, List[int]] = {}
    for path in sorted(tests_dir.rglob("test_*.py")):
        tree = ast.parse(_read(path, reads))
        roots = _path_inserts(tree, path) + [path.parent]
        for d in [path.parent, *path.parent.parents]:
            roots += conftest.get(d, [])
            if d == tests_dir:
                break
        scopes = _Scopes(tree)
        scanners = _scanners(scopes, roots, cache, seen, reads, probes)
        lines = sorted({h[0] for h in _hits(tree, scanners, scopes=scopes)})
        if lines:
            out[path] = lines
    return out


def test_roots(root) -> List[Path]:
    """Every suite root `harness/data/test-strategy.yaml` declares, relative to `root`,
    a root nested in another folded into it. A tree that ships no strategy file ships
    no suites."""
    import config_layers
    import run_local_tests
    strategy = config_layers.resolve_path("test-strategy.yaml", root=root)[0]
    if not strategy.is_file():
        return []
    out: List[Path] = []
    for p in sorted({Path(c["path"]) for c in run_local_tests.load_strategy(strategy).values()}):
        if not any(p.is_relative_to(q) for q in out):
            out.append(p)
    return out


def _tool_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _listing(root: Path, suites: List[Path]) -> List[str]:
    """Every `test_*.py`/`conftest.py` under a declared suite — a bare directory
    listing, no read, so a new or removed file is caught without the cost of a scan."""
    out: Set[str] = set()
    for suite in suites:
        d = root / suite
        if d.is_dir():
            out |= {p.as_posix() for p in d.rglob("test_*.py")}
            out |= {p.as_posix() for p in d.rglob("conftest.py")}
    return sorted(out)


def _cache_dir() -> Optional[Path]:
    return bounded_cache_dir.cache_subdir("tree-scanning-gates")


def _cache_path(cache_dir: Path, root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return cache_dir / (digest + ".json")


def _note(msg: str) -> None:
    print("note: %s" % msg, file=sys.stderr)


def _load_cache(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _note("tree-scanning-gates cache %s unreadable (%s), recomputing" % (path, e))
        return None
    shape_ok = (isinstance(data, dict) and isinstance(data.get("tool_digest"), str)
                and isinstance(data.get("suites"), list) and isinstance(data.get("listing"), list)
                and isinstance(data.get("reads"), dict) and isinstance(data.get("probes"), dict)
                and isinstance(data.get("result"), dict))
    if not shape_ok:
        _note("tree-scanning-gates cache %s has an unexpected shape, recomputing" % path)
        return None
    return data


def _fresh(stored: dict, root: Path, suites: List[Path]) -> bool:
    """Every key component still matches: tool source, suite list, directory
    listing, content of every file the last scan read, and every import-probe
    hit/miss it made — a mismatch on any one invalidates."""
    try:
        if stored["tool_digest"] != _tool_digest():
            return False
        if [s.as_posix() for s in suites] != stored["suites"]:
            return False
        if _listing(root, suites) != stored["listing"]:
            return False
        for p, want in stored["reads"].items():
            fp = Path(p)
            if not fp.is_file() or hashlib.sha256(fp.read_bytes()).hexdigest() != want:
                return False
        for p, want in stored["probes"].items():
            if Path(p).is_file() != want:
                return False
    except (OSError, TypeError, KeyError):
        return False
    return True


def _prune(cache_dir: Path, keep: int = _CACHE_KEEP) -> None:
    bounded_cache_dir.prune(cache_dir, keep=keep)


def _write_cache(path: Path, root: Path, suites: List[Path], reads: Dict[Path, str],
                  probes: Dict[Path, bool], result: Dict[str, dict]) -> None:
    record = {
        "tool_digest": _tool_digest(),
        "suites": [s.as_posix() for s in suites],
        "listing": _listing(root, suites),
        "reads": {str(p): h for p, h in reads.items()},
        "probes": {str(p): v for p, v in probes.items()},
        "result": result,
    }
    body = json.dumps(record, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        artifact_io.atomic_write_text(path, body)
    except (OSError, artifact_io.CrossVolumeError) as e:
        _note("tree-scanning-gates cache write to %s failed (%s)" % (path, e))
        return
    _prune(path.parent)


def _scan(root: Path):
    """The uncached computation, with the content it read/probed collected alongside
    the result — the cache's content key."""
    reads: Dict[Path, str] = {}
    probes: Dict[Path, bool] = {}
    out: Dict[str, dict] = {}
    suites = test_roots(root)
    for suite in suites:
        for path, lines in scanning_test_files_in(root / suite, reads=reads, probes=probes).items():
            rel = path.relative_to(root).as_posix()
            if path.stem in out:
                raise RegistryError("test-gate %s is two files: %s and %s"
                                    % (path.stem, out[path.stem]["file"], rel))
            out[path.stem] = {"file": rel, "lines": lines, "whole_tree": True}
    return out, suites, reads, probes


def tree_scanning_gates(root) -> Dict[str, dict]:
    """Gate-registry source: one row per test file, in any declared suite, that
    enumerates the tree — whole-tree by the criterion that found it. Cached on disk,
    content-keyed on the tool's own source, the suite list, the file listing, and
    every file read/probed doing the scan — a warm hit rehashes that set (no AST
    parse) rather than trusting a timestamp. An unresolved data root computes
    without a cache and writes nothing."""
    root = Path(root).resolve()
    cache_dir = _cache_dir()
    if cache_dir is None:
        out, _, _, _ = _scan(root)
        return out
    cache_path = _cache_path(cache_dir, root)
    stored = _load_cache(cache_path)
    if stored is not None:
        suites = test_roots(root)
        if _fresh(stored, root, suites):
            return stored["result"]
    out, suites, reads, probes = _scan(root)
    _write_cache(cache_path, root, suites, reads, probes, out)
    return out
