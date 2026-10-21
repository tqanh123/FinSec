"""hs-code test clone-scan -- a test that copies production logic. Port of ob-tools' `testkit.analysis.clone_scan` (pinned
commit `60075cb`), raised from "which dist copied which" to "which TEST
copied what".

WHY IT IS NOT THE PRODUCTION SCAN
    `hs-code refactor clone-dedupe` (`codegraph.refactor.clones.buckets_for`)
    answers a different question: it groups PRODUCTION defs by NAME (module-
    level functions, `Class.method` pairs, module-level constants only) and
    asks whether two homes carry the same name with the same or drifted
    body. A test copying production keeps a DIFFERENT name on purpose
    (`test_compute_ratio` copying `compute_ratio`) -- name-keyed grouping
    would never even pair them. This scan compares every test function
    body (module-level or nested, any name) against every source function
    body, keyed by STRUCTURE alone -- a genuinely different
    algorithm for a genuinely different question, not a second copy of
    `buckets_for`'s. Production copying production is that verb's finding,
    never this one's.

    A test copied from production is not testing anything -- it
    re-implements the algorithm and asserts the re-implementation agrees
    with itself. Two tests of one shape are not reported: they are the
    normal state of a suite, and under upstream's rules they buried the
    test-to-source pairs.

    So the two verdicts are NOT symmetric in cost:
      * IDENTICAL -- a copy that still matches. Debt: it will drift eventually.
      * DRIFTED   -- a copy that ALREADY diverged from its source. The
                    dangerous one: the test is checking the old behaviour,
                    or checking itself. This is the finding, not the
                    identical pair.

    Origin is decided by STRUCTURE, not text: bodies are compared after
    names, constants and strings are erased, so `f(a, b)` and `g(x, y)`
    match. IDENTICAL is an equal erased-node sequence. DRIFTED is an
    unequal one whose ORDERED match ratio (`difflib`, 2*matched/total) is
    at or above DRIFT_THRESHOLD -- order counts, so a test holding a source
    function's statements in another order is not a copy of it. A heuristic
    with a dial, not a proof of copying.

    Upstream compared bags of node types by Jaccard over every pair. Order
    is now part of the measure, and the pairing is still not all-pairs: a
    ratio of t needs a bag Jaccard of at least t/(2-t), so an AllPairs
    prefix filter at that bound picks candidates, the bag Dice bound drops
    most of them, and only the rest pay for the sequence match.

DIRECTORIES, GENERALISED
    Upstream took a caller-typed `<tests_dir> <src_dir>` pair -- fine for a
    monorepo with exactly one of each. This repo's OWN `codemap.yaml`
    declares SIX flat `layout.roots` (harness, harness/scripts/rops,
    release, scripts, docs, showcase) and no single repo-root `tests/` at
    all (test trees sit at `harness/tests/`, `release/tests/`, ...) -- a
    hard-coded `tests/` would scan nothing. With BOTH positionals omitted,
    `scan_repo` pools every declared `layout.roots` directory
    (`codegraph._core.descriptor.root_paths`, the same door
    `test.classify`'s dist lookup and `test.check_home`'s scope both build
    on) and splits each file tests-vs-src by `codegraph._core.testfiles.
    build_test_layout(...).in_test_tree` -- the SAME test-tree-membership
    predicate `test.classify`/`test.check_home` consult through the
    snapshot's own test map, so a file two verbs disagree about being "test
    code" cannot happen. Passing BOTH keeps upstream's literal two-tree
    scan. ONE positional is a usage error: upstream scanned a directory
    against itself, which is only test against test.

WHAT IT CANNOT SEE
    * Structural similarity is not provenance: two people writing the
      obvious three-line loop produce the same fingerprint. `MIN_NODES` and
      `COMMON` cut the noise floor but cannot remove it -- a DRIFTED verdict
      means "worth a look", never "was copied".
    * Bodies under `MIN_NODES` are skipped entirely: a git wrapper or a
      read-and-parse helper copied five times is invisible here.
    * Tests copying tests, and source copying source, are never reported.
    * Code generated at import time, `exec`, string-built functions: no AST
      node, no body, nothing to compare.
    * Decorators are stripped before comparing -- two tests differing only
      in `@pytest.mark.parametrize` read as IDENTICAL.
    * Type annotations and default values are erased with the names; a copy
      that diverged only in a signature is reported IDENTICAL.
    * Only `def` bodies. Module-level copy-paste (constants, fixture
      tables) is not compared.
    * Two production functions copying each other: not reported here
      (`hs-code refactor clone-dedupe`).
    * The baseline is a list of names, so a clone that changes BOTH
      endpoints looks like one clone disappearing and another appearing --
      the count is right, the identity is not.

USAGE
    hs-code test clone-scan
    hs-code test clone-scan TESTS_DIR SRC_DIR [--baseline FILE --check]
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from collections import Counter
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import hs_family
from hs_family.cli import Command

#: Bodies smaller than this are not compared. Chosen by reading sampled pairs
#: by hand: below it nearly all were git wrappers, path joins, read-and-parse
#: -- the helpers every tree writes for itself; at and above it nearly all
#: were real copies (the samples and the tree they came from: the clone-scan
#: row of docs/STANDARDIZE.md). Raising it makes the probe blind to more.
MIN_NODES = 40

#: Structural similarity at or above which two non-identical bodies are
#: called DRIFTED. Dial, not law: 1.0 = only exact matches, 0.0 = everything
#: is a clone.
DRIFT_THRESHOLD = 0.80

#: Names so common that a match says nothing about copying. Same list idea
#: as `codegraph.refactor.clones.COMMON`, extended with the test-shaped
#: conventions.
COMMON: "frozenset[str]" = frozenset({
    "__init__", "__repr__", "__str__", "__eq__", "__hash__", "__len__",
    "setUp", "tearDown", "setup_method", "teardown_method",
})


@dataclass(frozen=True)
class Function:
    """One comparable body: a test function or a production function."""

    id: str                      # "<kind>:<relpath>::<Class.outer.name>[#n]"
    kind: str                    # "tests" | "src"
    path: str                    # path relative to the scanned root
    name: str
    line: int
    nodes: int                   # AST node count of the body
    fingerprint: str             # structure with names/constants/strings erased
    bag: Counter                 # node-type histogram (candidate filter and bound)


@dataclass(frozen=True)
class ClonePair:
    """Two functions with the same shape."""

    a: Function
    b: Function
    verdict: str                 # "identical" | "drifted"
    similarity: float

    @property
    def key(self) -> str:
        """Order-independent identity — the baseline stores this."""
        lo, hi = sorted((self.a.id, self.b.id))
        return f"{lo} <-> {hi}"

    @property
    def crosses(self) -> str:
        """Which kind of copy this is: tests<->src, or tests<->tests.

        Both sides are always spelled out. Collapsing same-kind pairs to a
        bare "tests" would make the test-copies-test count silently zero.
        """
        a, b = sorted((self.a.kind, self.b.kind))
        return f"{a}->{b}"


@dataclass
class ScanReport:
    functions: "list[Function]" = field(default_factory=list)
    pairs: "list[ClonePair]" = field(default_factory=list)
    skipped_small: int = 0
    skipped_common: int = 0
    #: files WALKED (post `__pycache__`/dedup filtering), one count per kind
    #: -- the denominator that tells "0 clones because nothing was scanned"
    #: apart from "0 clones because every body was under `MIN_NODES`".
    files_tests: int = 0
    files_src: int = 0

    def drifted(self) -> "list[ClonePair]":
        return [p for p in self.pairs if p.verdict == "drifted"]

    def identical(self) -> "list[ClonePair]":
        return [p for p in self.pairs if p.verdict == "identical"]


# ── structural comparison ────────────────────────────────────────────────────

class _Eraser(ast.NodeVisitor):
    """Rewrite a body into a shape string: names, constants and strings gone.

    `f(a, b) -> g(x, y)` and `self.call(1, 2) -> self.other(3, 4)` erase to
    the same string, which is the point: copying changes the words, not the
    shape.
    """

    def __init__(self) -> None:
        self.parts: "list[str]" = []
        self.bag: Counter = Counter()

    def _emit(self, tag: str) -> None:
        self.parts.append(tag)
        self.bag[tag] += 1

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 (ast API)
        self._emit("Name")

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        self._emit(f"Const:{type(node.value).__name__}")

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        self._emit("Attribute")
        self.visit(node.value)

    def visit_arg(self, node: ast.arg) -> None:  # noqa: N802
        self._emit("arg")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        # Nested defs keep their structure but not their name.
        self._emit("FunctionDef")
        for child in node.body:
            self.visit(child)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._emit("AsyncFunctionDef")
        for child in node.body:
            self.visit(child)

    def generic_visit(self, node: ast.AST) -> None:
        self._emit(type(node).__name__)
        super().generic_visit(node)


def _body_of(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> "list[ast.stmt]":
    """The statements to compare: docstring dropped, decorators ignored."""
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body


def _fingerprint(body: "list[ast.stmt]") -> "tuple[str, Counter, int]":
    er = _Eraser()
    for stmt in body:
        er.visit(stmt)
    return "|".join(er.parts), er.bag, len(er.parts)


def _sequence_similarity(a: "Function", b: "Function") -> float:
    """Matching ratio of the two ORDERED erased-node sequences (2*M/T,
    `difflib`, no junk heuristic). A bag of node types forgets order: a test
    holding every node type of a source function in another order scored 1.0
    there and was reported as a drifted copy of something it never copied."""
    return SequenceMatcher(None, a.fingerprint.split("|"), b.fingerprint.split("|"),
                           autojunk=False).ratio()


def _bag_ratio_bound(a: Counter, b: Counter) -> float:
    """2*|a & b| / (|a| + |b|): an upper bound on `_sequence_similarity`, since
    every matched element of the sequences is one element of the multiset
    intersection. Cheap enough to discard a candidate before the O(n*m)
    match."""
    total = sum(a.values()) + sum(b.values())
    return 2 * sum((a & b).values()) / total if total else 0.0


def _functions_in_file(path: Path, rel: str, kind: str) -> "list[Function]":
    """Every comparable function/method AST node in one file, already
    filtered to `>= MIN_NODES`. Shared by `load_functions` (one root) and
    `load_pool` (many roots, repo-relative `rel`) -- the ONE place that
    parses a file and turns its defs into `Function` rows.

    A parse error returns `[]` rather than raising: one unparseable file
    must not hide every clone in the tree."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: "list[Function]" = []
    taken: Counter = Counter()

    def walk(parent: ast.AST, scope: str) -> None:
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, ast.ClassDef):
                walk(node, f"{scope}{node.name}.")
                continue
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(node, scope)
                continue
            qual = f"{scope}{node.name}"
            walk(node, f"{qual}.")
            fp, bag, n = _fingerprint(_body_of(node))
            if n < MIN_NODES:
                continue
            # A qualified name still repeats when one scope defines a name
            # twice; the id must not, or a function pairs with itself.
            taken[qual] += 1
            ident = qual if taken[qual] == 1 else f"{qual}#{taken[qual]}"
            out.append(Function(id=f"{kind}:{rel}::{ident}", kind=kind,
                                path=rel, name=node.name, line=node.lineno,
                                nodes=n, fingerprint=fp, bag=bag))

    walk(tree, "")
    return out


def load_functions(root: Path, kind: str, pattern: str = "*.py") -> "tuple[list[Function], int]":
    """(functions, files walked) under `root`, sorted by path then line. The
    file count is the scan's own denominator: a caller can tell "found
    nothing because MIN_NODES ate every body" apart from "found nothing
    because there was nothing to walk"."""
    out: "list[Function]" = []
    files = 0
    for path in sorted(root.rglob(pattern)):
        if "__pycache__" in path.parts:
            continue
        files += 1
        rel = str(path.relative_to(root))
        out.extend(_functions_in_file(path, rel, kind))
    return sorted(out, key=lambda f: (f.path, f.line)), files


def load_pool(repo_root: Path, kind: str, scan_roots: "list[Path]",
             predicate: "Callable[[str], bool]", pattern: str = "*.py") -> "tuple[list[Function], int]":
    """The multi-root generalisation `load_functions` cannot express: every
    comparable function under any of `scan_roots` whose path RELATIVE TO
    `repo_root` (never to the individual root that found it) satisfies
    `predicate` -- so a function's `id` stays stable regardless of which of
    this repo's several declared `layout.roots` it sits under, and a file
    reachable from two overlapping roots is counted once."""
    out: "list[Function]" = []
    files = 0
    seen: "set[str]" = set()
    for base in scan_roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(pattern)):
            if "__pycache__" in path.parts:
                continue
            try:
                rel = str(path.relative_to(repo_root))
            except ValueError:
                continue
            if rel in seen or not predicate(rel):
                continue
            seen.add(rel)
            files += 1
            out.extend(_functions_in_file(path, rel, kind))
    return sorted(out, key=lambda f: (f.path, f.line)), files


# ── pairing ──────────────────────────────────────────────────────────────────

def pair_functions(tests: "list[Function]", src: "list[Function]", *,
                   drift_threshold: float = DRIFT_THRESHOLD) -> ScanReport:
    """The comparison core of `scan_trees`/`scan_repo`, over already-LOADED
    function lists -- so both a two-tree and a pooled multi-root scan share
    exactly one pairing algorithm.

    Only a test against a source function is ever a pair. Two tests of one
    shape are the normal state of a suite, not a finding; production
    copying production is `refactor clone-dedupe`'s question.
    """
    report = ScanReport()
    report.functions.extend(tests)
    report.functions.extend(src)
    pool = list(tests) + list(src)

    def crosses(a: Function, b: Function) -> bool:
        return a.kind != b.kind

    by_fp: "dict[str, list[Function]]" = {}
    for f in pool:
        by_fp.setdefault(f.fingerprint, []).append(f)

    seen: "set[str]" = set()
    for group in by_fp.values():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if not crosses(a, b):
                    continue
                if a.name in COMMON or b.name in COMMON:
                    report.skipped_common += 1
                    continue
                pair = ClonePair(a, b, "identical", 1.0)
                if pair.key in seen:
                    continue
                seen.add(pair.key)
                report.pairs.append(pair)

    # A sequence ratio of t needs a multiset overlap of at least t*(|a|+|b|)/2,
    # which is a bag Jaccard of at least t/(2-t): the candidate filter runs at
    # that bound, so it can only over-select.
    bound = drift_threshold / (2 - drift_threshold) if drift_threshold < 2 else 1.0
    for a, b in _near_miss_candidates([f for f in pool if f.name not in COMMON], bound):
        if not crosses(a, b) or a.fingerprint == b.fingerprint:
            continue
        if _bag_ratio_bound(a.bag, b.bag) < drift_threshold:
            continue
        sim = _sequence_similarity(a, b)
        if sim < drift_threshold:
            continue
        pair = ClonePair(a, b, "drifted", sim)
        if pair.key in seen:
            continue
        seen.add(pair.key)
        report.pairs.append(pair)

    report.pairs.sort(key=lambda p: (-p.similarity, p.key))
    return report


def _near_miss_candidates(pool: "list[Function]", threshold: float
                          ) -> "Iterator[tuple[Function, Function]]":
    """Every pair whose similarity CAN reach `threshold`, and far fewer than
    all pairs, which grow with the square of the pool.

    Exact, not sampled — the prefix filter of a set-similarity join. A bag is
    the set of tokens (node type, k) for k = 1..count, so its size is `nodes`
    and the multiset Jaccard is the plain Jaccard of those sets. With tokens
    ranked rarest-first across the pool, J(x, y) >= t forces an overlap of at
    least ceil(t*|x|) for the larger x, so the two must share a token within
    x's first |x| - ceil(t*|x|) + 1. Records are indexed in size order, and
    a smaller y below t*|x| cannot reach t at all (J <= |y|/|x|)."""
    eps = 1e-9
    tokens: "list[list[tuple[str, int]]]" = []
    df: Counter = Counter()
    for f in pool:
        toks = [(tag, k) for tag, count in f.bag.items() for k in range(1, count + 1)]
        tokens.append(toks)
        df.update(toks)
    rank = {tok: i for i, tok in enumerate(sorted(df, key=lambda tok: (df[tok], tok)))}

    index: "dict[int, list[int]]" = {}
    for i in sorted(range(len(pool)), key=lambda i: (pool[i].nodes, i)):
        n = pool[i].nodes
        prefix = sorted(rank[tok] for tok in tokens[i])[:max(1, n - math.ceil(threshold * n - eps) + 1)]
        floor = threshold * n - eps
        found: "set[int]" = set()
        for r in prefix:
            for j in index.get(r, ()):
                if pool[j].nodes >= floor:
                    found.add(j)
        for j in sorted(found):
            yield pool[j], pool[i]
        for r in prefix:
            index.setdefault(r, []).append(i)


def scan_trees(tests_root: Path, src_root: Path,
               *, drift_threshold: float = DRIFT_THRESHOLD) -> ScanReport:
    """Find tests under `tests_root` copying functions under `src_root`."""
    tests, tests_files = load_functions(tests_root, "tests")
    src, src_files = load_functions(src_root, "src")
    report = pair_functions(tests, src, drift_threshold=drift_threshold)
    report.files_tests = tests_files
    report.files_src = src_files
    return report


def default_scan_roots(repo_root: Path, descriptor: Any) -> "list[Path]":
    """This repo's declared `layout.roots` as directories (`root_paths`,
    the SAME door `test.classify`/`test.check_home` build their own scope
    on) — never a bare `repo_root.rglob(...)`, which this repo's own
    `codemap.yaml` measures at 54% git-ignored noise (`ak-src`, `.claude`'s
    install-output slot, ...). No declared roots (a pure auto-discover
    project) falls back to the repo root itself."""
    from codegraph._core.descriptor import root_paths
    paths = root_paths(descriptor)
    return [repo_root / p for p in paths] if paths else [repo_root]


def scan_repo(repo_root: Path, descriptor: Any, *,
             drift_threshold: float = DRIFT_THRESHOLD) -> ScanReport:
    """The zero-positional default: pool every declared `layout.roots`
    directory, split tests-vs-src by `codegraph._core.testfiles.
    build_test_layout(...).in_test_tree` -- the exact test-tree-membership
    predicate `test.classify`'s own snapshot-backed test map is built from,
    so this scan and that one never disagree about what counts as a test
    file."""
    from codegraph._core.testfiles import build_test_layout
    layout = build_test_layout(repo_root, descriptor)
    roots = default_scan_roots(repo_root, descriptor)
    tests, tests_files = load_pool(repo_root, "tests", roots, layout.in_test_tree)
    src, src_files = load_pool(repo_root, "src", roots, lambda rel: not layout.in_test_tree(rel))
    report = pair_functions(tests, src, drift_threshold=drift_threshold)
    report.files_tests = tests_files
    report.files_src = src_files
    return report


# ── baseline: two-way, shrink-only ───────────────────────────────────────────

def load_baseline(path: Path) -> "set[str]":
    """Baseline is JSON: {"clones": ["<a> <-> <b>", ...]}."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    return set(doc.get("clones") or [])


def compare_baseline(report: ScanReport, baseline: "set[str]") -> dict:
    """Red on NEW clones; also red when a baselined clone VANISHED.

    A clone that disappears without anyone editing either side means the
    probe stopped seeing it (a rename, a body edit, a moved file) — the
    same "learned to hide" reading a shrink-only baseline exists to catch.
    Shrinking a baseline is a deliberate act, never a side effect of a
    refactor.
    """
    now = {p.key for p in report.pairs}
    return {
        "new": sorted(now - baseline),
        "gone": sorted(baseline - now),
        "kept": sorted(now & baseline),
    }


def summary(report: ScanReport) -> dict:
    drifted = report.drifted()
    return {
        "functions": len(report.functions),
        "functions_tests": sum(1 for f in report.functions if f.kind == "tests"),
        "functions_src": sum(1 for f in report.functions if f.kind == "src"),
        "files_tests": report.files_tests,
        "files_src": report.files_src,
        "pairs": len(report.pairs),
        "identical": len(report.identical()),
        "drifted": len(drifted),
        "test_copies_src": sum(1 for p in report.pairs if p.crosses == "src->tests"),
        "min_nodes": MIN_NODES,
        "drift_threshold": DRIFT_THRESHOLD,
    }


# ── report ───────────────────────────────────────────────────────────────────

def render(report: ScanReport, top: int = 20) -> str:
    s = summary(report)
    L = [
        f"clone scan: {s['files_tests']} test file(s) -> {s['functions_tests']} "
        f"test func(s) >= {MIN_NODES} nodes",
        f"            {s['files_src']} source file(s) -> {s['functions_src']} "
        f"source func(s) >= {MIN_NODES} nodes",
        f"  pairs {s['pairs']} = identical {s['identical']} + drifted {s['drifted']}",
        f"  every pair is a test against a source function ({s['test_copies_src']})",
        "",
        "  DRIFTED is the finding: the copy already disagrees with its source.",
        f"  Similarity >= {DRIFT_THRESHOLD} but not byte-equal. Heuristic, not proof.",
        "",
    ]
    drifted = report.drifted()[:top]
    if drifted:
        L.append(f"  drifted (top {len(drifted)}):")
        for p in drifted:
            L.append(f"    {p.similarity:.2f}  {p.a.id}")
            L.append(f"          {p.b.id}")
    identical = report.identical()[:top]
    if identical:
        L.append("")
        L.append(f"  identical (top {len(identical)}) — debt, not danger:")
        for p in identical:
            L.append(f"    {p.a.id}")
            L.append(f"          {p.b.id}")
    if not report.pairs:
        L.append("  no clone found above the floor.")
        L.append("  This is NOT a clean bill: bodies under the floor, dynamic")
        L.append("  code and module-level copy-paste are all invisible here.")
    return "\n".join(L)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    """Single source of this verb's flags."""
    sp.add_argument("tests_dir", nargs="?", default=None,
                    help="tests root to scan, repo-relative (default: this repo's "
                         "declared layout.roots, pooled and split tests-vs-src)")
    sp.add_argument("src_dir", nargs="?", default=None,
                    help="source root, repo-relative; with tests_dir given, omit to "
                         "scan tests against themselves; omit BOTH for the pooled default")
    sp.add_argument("--baseline", type=Path, default=None,
                    help="baseline JSON; with --check, red on new AND on vanished")
    sp.add_argument("--write-baseline", type=Path, default=None,
                    help="record the current clones as the baseline (--apply writes it)")
    sp.add_argument("--check", action="store_true",
                    help="compare against --baseline, exit 1 if it moved")
    sp.add_argument("--top", type=int, default=20, help="rows per section")


def _resolve_dir(root: Path, given: str, argname: str) -> Path:
    p = Path(given)
    p = p if p.is_absolute() else root / p
    if not p.is_dir():
        raise hs_family.UsageError(f"clone-scan: not a directory: {p} ({argname})")
    return p


def _clone_scan(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    if args.tests_dir is None and args.src_dir is None:
        from codegraph.check.tree import workspace
        descriptor, _packages = workspace(root)
        report = scan_repo(root, descriptor)
        scope = "repo-pooled (declared layout.roots)"
    elif args.src_dir is None:
        raise hs_family.UsageError(
            "clone-scan: TESTS_DIR needs a SRC_DIR -- tests are compared with source "
            "only (a test resembling another test is not a finding); omit both for "
            "the pooled default")
    else:
        tests_root = _resolve_dir(root, args.tests_dir, "tests_dir")
        src_root = _resolve_dir(root, args.src_dir, "src_dir")
        report = scan_trees(tests_root, src_root)
        scope = f"{args.tests_dir} vs {args.src_dir}"

    if args.write_baseline:
        if apply:
            args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
            args.write_baseline.write_text(
                json.dumps({"clones": sorted(p.key for p in report.pairs)},
                          ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                encoding="utf-8")
        if not as_json:
            dest = "written" if apply else "[dry-run: --apply writes]"
            print(f"clone-scan: baseline {dest} — {len(report.pairs)} clone(s) "
                  f"-> {args.write_baseline}", file=sys.stderr)
            print("  the number is DEBT, not a score. Shrinking it is an edit, "
                  "never a side effect.", file=sys.stderr)

    if args.check:
        if not args.baseline:
            raise hs_family.UsageError("clone-scan: --check needs --baseline")
        if not args.baseline.is_file():
            raise hs_family.UsageError(f"clone-scan: no baseline at {args.baseline}")
        diff = compare_baseline(report, load_baseline(args.baseline))
        if as_json:
            print(json.dumps({"summary": summary(report), "scope": scope, **diff},
                             ensure_ascii=False, sort_keys=True))
        else:
            for k in ("new", "gone"):
                for line in diff[k][:args.top]:
                    print(f"  {k.upper():4} {line}")
            print(f"clone-scan: new {len(diff['new'])} · gone {len(diff['gone'])} "
                  f"· kept {len(diff['kept'])}", file=sys.stderr)
        if diff["new"] or diff["gone"]:
            if not as_json:
                print("RED: the clone baseline moved. New copies are debt; "
                      "vanished ones mean the probe stopped seeing them — "
                      "check which before shrinking the baseline.")
            return 1
        if not as_json:
            print(f"GREEN: baseline matches ({len(diff['kept'])} clone(s)). "
                  "That number is DEBT, not a score.")
        return 0

    if as_json:
        print(json.dumps({"summary": summary(report), "scope": scope,
                          "pairs": [{"a": p.a.id, "b": p.b.id,
                                     "verdict": p.verdict,
                                     "similarity": round(p.similarity, 3),
                                     "crosses": p.crosses}
                                    for p in report.pairs]},
                         ensure_ascii=False, sort_keys=True))
    else:
        print(f"scope: {scope}")
        print(render(report, top=args.top))
    return 0


CLONE_SCAN = Command(add_args=add_arguments, run=_clone_scan)
