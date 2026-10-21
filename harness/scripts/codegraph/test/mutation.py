"""hs-code test mutation -- do the tests actually NOTICE when the code is
wrong? A survivor RATE, never a gate: a surviving mutant means the test
suite does not pin that behaviour down, not that a bug exists.

Port of ob-tools' `testkit.analysis.mutation` (pinned commit `60075cb`): the
operator/AST logic below is the SAME shape upstream shipped. Rewired parts:

  * every run happens through `codegraph.test.copy_runner.run_declared` --
    the tree's own DECLARED `domains.test.command` -- never a bare
    `sys.executable -m pytest` with a `<workdir>/src` PYTHONPATH guess (wrong
    on a flat tree, and the exact bug `codegraph.test.prove`'s own docstring
    measures);
  * `find_functions` scans an arbitrary repo-relative directory, not a fixed
    `<src_root>/<pkg>` layout -- this harness has no such convention;
  * which tests each mutant RUNS is picked by `codegraph.change.selection.
    select_tests` -- the SAME selector `hs-code change test select` already
    calls (see `_select_tests`) -- never a second, hand-rolled touch map.

WHY A COPY, ALWAYS
    Neither verb ever writes a source tree; see `codegraph.test.copy_runner`.

WHY IT OFTEN CANNOT RUN
    Running someone else's test suite needs their environment. When the
    copy's baseline does not pass, this returns exit 2 with the reason --
    never a rate it did not measure.

NO-GIT LIMIT
    The copy this verb runs in carries no `.git` (see `codegraph.test.
    copy_runner`) -- a selected test that needs git goes red at the
    BASELINE, which is exit 2 "the copy's tests do not pass as-is", never a
    false "survived".

WHAT IT CANNOT SEE
    * Only the operators in `OPERATORS`, one per function, first site of
      each kind.
    * A mutant killed by a test that FAILS FOR AN UNRELATED REASON still
      counts as killed. The rate is therefore an upper bound on the truth.
    * `_select_tests` degrades to `idx=None` (self + whole_tree channels
      only -- see `codegraph.change.selection.select_tests`'s own docstring)
      when the real tree cannot be freshly measured (no git, no descriptor,
      a measurement failure); a mutant the selector then routes to nothing
      falls back to the declared command's whole scope (`.`), so it still
      runs against SOMETHING rather than being silently skipped -- but that
      fallback is not a targeted kill and reads no differently from a
      genuine survivor.
    * Timeouts are treated as killed. Syntax-invalid mutants are discarded,
      not counted as killed.

USAGE
    hs-code test mutation TARGET_DIR [--limit 8] [--operators compare_swap,...]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hs_family

from codegraph.test.copy_runner import (changed_tree_error, copied_tree, run_declared,
                                        tree_changes, tree_digests)
from codegraph.test.declared_command import DeclaredCommandError
from hs_family.cli import Command

DEFAULT_LIMIT = 8
RUN_TIMEOUT_S = 300

OPERATORS: "tuple[str, ...]" = (
    "compare_swap",      # < -> <=, == -> !=
    "arith_swap",        # + -> -, * -> /
    "const_bump",        # 0 -> 1, n -> n+1
    "return_none",       # drop a `return <expr>` value
)

_TEST_NAME = re.compile(r"^(test_.*|.*_test)\.py$")


@dataclass(frozen=True)
class Mutant:
    """One candidate edit: which function, which operator, where."""

    function: str            # "<relpath>::<name>"
    relpath: str
    lineno: int
    operator: str
    description: str

    @property
    def key(self) -> str:
        return f"{self.function}:{self.lineno}:{self.operator}"


@dataclass
class Outcome:
    mutant: Mutant
    status: str              # killed | survived | invalid | error
    detail: str = ""


@dataclass
class ProbeReport:
    outcomes: "list[Outcome]" = field(default_factory=list)
    ran: bool = False
    reason: str = ""         # why it did not run, when ran is False
    candidates: int = 0

    def killed(self) -> "list[Outcome]":
        return [o for o in self.outcomes if o.status == "killed"]

    def survived(self) -> "list[Outcome]":
        return [o for o in self.outcomes if o.status == "survived"]

    def score(self) -> "float | None":
        """killed / (killed + survived). None when nothing was measured."""
        k, s = len(self.killed()), len(self.survived())
        return (k / (k + s)) if (k + s) else None


# ── picking targets ──────────────────────────────────────────────────────────

def find_functions(root: Path, target: Path, *, limit: int = DEFAULT_LIMIT
                   ) -> "list[tuple[str, Any]]":
    """Top-level functions under `target` (repo-relative to `root`), richest
    first (most statements) -- the cheapest honest proxy for "a mutation can
    meaningfully change this" available without a call graph. A test file
    (pytest's own `test_*.py`/`*_test.py` convention) is never a candidate:
    on a flat tree `target` can be the whole repo, source and tests side by
    side, and mutating a test proves nothing about the code under test."""
    if not target.is_dir():
        raise FileNotFoundError(f"target dir not found: {target}")
    out: "list[tuple[str, Any]]" = []
    for path in sorted(target.rglob("*.py")):
        if "__pycache__" in path.parts or _TEST_NAME.match(path.name):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((rel, node))
    out.sort(key=lambda item: (-len(item[1].body), item[0], item[1].lineno))
    return out[:limit]


# ── generating mutants ───────────────────────────────────────────────────────

_CMP = {ast.Lt: (ast.LtE, "< -> <="), ast.LtE: (ast.Lt, "<= -> <"),
        ast.Gt: (ast.GtE, "> -> >="), ast.GtE: (ast.Gt, ">= -> >"),
        ast.Eq: (ast.NotEq, "== -> !="), ast.NotEq: (ast.Eq, "!= -> ==")}
_ARITH = {ast.Add: (ast.Sub, "+ -> -"), ast.Sub: (ast.Add, "- -> +"),
          ast.Mult: (ast.Div, "* -> /"), ast.Div: (ast.Mult, "/ -> *")}


class _Collector(ast.NodeVisitor):
    """Every site in one function where an operator could be applied."""

    def __init__(self) -> None:
        self.sites: "list[tuple[str, int, str]]" = []

    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802
        for op in node.ops:
            if type(op) in _CMP:
                self.sites.append(("compare_swap", node.lineno, _CMP[type(op)][1]))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:  # noqa: N802
        if type(node.op) in _ARITH:
            self.sites.append(("arith_swap", node.lineno, _ARITH[type(node.op)][1]))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            self.sites.append(("const_bump", node.lineno,
                               f"{node.value} -> {node.value + 1}"))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        if node.value is not None:
            self.sites.append(("return_none", node.lineno, "return <expr> -> None"))
        self.generic_visit(node)


def candidates_for(relpath: str, fn: Any, *, operators: "tuple[str, ...]" = OPERATORS
                   ) -> "list[Mutant]":
    """One mutant per operator per function -- first site of each kind."""
    col = _Collector()
    for stmt in fn.body:
        col.visit(stmt)
    seen: "set[str]" = set()
    out: "list[Mutant]" = []
    for op, lineno, desc in sorted(col.sites, key=lambda s: (s[1], s[0])):
        if op not in operators or op in seen:
            continue
        seen.add(op)
        out.append(Mutant(f"{relpath}::{fn.name}", relpath, lineno, op, desc))
    return out


# ── applying a mutant (to the copy) ──────────────────────────────────────────

class _Mutator(ast.NodeTransformer):
    """Applies ONE operator at ONE line inside ONE named function."""

    def __init__(self, target: str, lineno: int, operator: str) -> None:
        self.target = target
        self.lineno = lineno
        self.operator = operator
        self.applied = False
        self.depth = 0

    def visit_FunctionDef(self, node):  # noqa: N802
        if self.depth or node.name != self.target:
            return node
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Compare(self, node: ast.Compare):  # noqa: N802
        if self.operator == "compare_swap" and not self.applied \
                and node.lineno == self.lineno and self.depth:
            for i, op in enumerate(node.ops):
                if type(op) in _CMP:
                    node.ops[i] = _CMP[type(op)][0]()
                    self.applied = True
                    break
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp):  # noqa: N802
        if self.operator == "arith_swap" and not self.applied \
                and node.lineno == self.lineno and self.depth:
            if type(node.op) in _ARITH:
                node.op = _ARITH[type(node.op)][0]()
                self.applied = True
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):  # noqa: N802
        if self.operator == "const_bump" and not self.applied \
                and node.lineno == self.lineno and self.depth:
            if isinstance(node.value, (int, float)) \
                    and not isinstance(node.value, bool):
                node.value = node.value + 1
                self.applied = True
        return self.generic_visit(node)

    def visit_Return(self, node: ast.Return):  # noqa: N802
        if self.operator == "return_none" and not self.applied \
                and node.lineno == self.lineno and self.depth:
            if node.value is not None:
                node.value = ast.Constant(value=None)
                self.applied = True
        return self.generic_visit(node)


def mutate_source(text: str, mutant: Mutant) -> "str | None":
    """Source with the mutant applied, or None when it could not be applied.
    `ast.unparse` is used here and nowhere else: this text is never written
    back to a real tree, so losing comments and formatting costs nothing."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    name = mutant.function.split("::", 1)[1]
    mut = _Mutator(name, mutant.lineno, mutant.operator)
    new = mut.visit(tree)
    if not mut.applied:
        return None
    ast.fix_missing_locations(new)
    try:
        return ast.unparse(new)
    except Exception:                                    # noqa: BLE001
        return None


# ── test selection: the SAME selector `change test select` already calls ────

class _NullOpts:
    """The minimal `opts` shape `resolve_idx` reads (`.json`) -- `True`
    suppresses its one informational stderr print, which is noise here."""
    json = True


def _selection_index(root: Path):
    """The index `codegraph.change.selection.select_tests` is seeded with: a
    FRESH measurement of the untouched REAL tree (never the copy: the copy
    has no `.git`, and a fresh measurement needs HEAD). Taken ONCE per probe,
    since the real tree does not move while one runs. `None` (self +
    whole_tree channels only) when the real tree cannot be freshly measured --
    a mutation probe must still run rather than hard-refuse over a repo this
    engine cannot snapshot."""
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph_graph_freshness import SnapshotResolveError, resolve_idx

    # A fresh measurement shells out to `git`, which prints its own "not a
    # git repository" straight to the inherited stderr fd -- skipped for a
    # non-git target, which falls back anyway.
    if not (root / ".git").is_dir():
        return None
    scratch = None
    try:
        descriptor = load(root / "codemap.yaml", root).descriptor
        _base, _snap_dir, idx, scratch = resolve_idx(
            root, descriptor, _NullOpts(), None, None, "test mutation", fresh=True,
            need_store=False)
        return idx
    except (DescriptorError, SnapshotResolveError, OSError):
        return None
    finally:
        if scratch is not None:
            import shutil as _shutil
            _shutil.rmtree(scratch, ignore_errors=True)


def _select_tests(root: Path, relpath: str, idx) -> "list[str]":
    """Test targets for a mutant touching `relpath`, via `codegraph.change.
    selection.select_tests` -- the SAME selector `hs-code change test
    select` already calls."""
    from codegraph.change import selection
    return selection.select_tests(idx, [relpath], root=root).selected


# ── running ──────────────────────────────────────────────────────────────────

def probe(root: Path, target: Path, *, limit: int = DEFAULT_LIMIT,
          timeout: int = RUN_TIMEOUT_S, operators: "tuple[str, ...]" = OPERATORS
         ) -> ProbeReport:
    """Mutate a COPY, run its own declared test command, report the
    survivors. The real tree is hashed before and after (in a `finally`, so
    every return path is covered); a difference raises rather than being
    reported as a result."""
    rep = ProbeReport()
    before = tree_digests(root)
    try:
        try:
            funcs = find_functions(root, target, limit=limit)
        except FileNotFoundError as e:
            rep.reason = str(e)
            return rep

        mutants: "list[Mutant]" = []
        for rel, fn in funcs:
            mutants.extend(candidates_for(rel, fn, operators=operators))
        rep.candidates = len(mutants)
        if not mutants:
            rep.reason = (f"no mutation site found in {len(funcs)} function(s) -- "
                          f"nothing to measure")
            return rep

        with copied_tree(root) as work:
            try:
                code, tail = run_declared(work, ["."], timeout=timeout)
            except DeclaredCommandError as e:
                rep.reason = str(e)
                return rep
            if code != 0:
                rep.reason = ("the copy's tests do not pass as-is (exit "
                              f"{code}) -- a mutant's kill cannot be told apart "
                              f"from the pre-existing failure. Last lines: {tail}")
                return rep
            rep.ran = True
            idx = _selection_index(root)

            for mutant in mutants:
                src_file = work / mutant.relpath
                try:
                    original = src_file.read_text(encoding="utf-8")
                except OSError as e:
                    rep.outcomes.append(Outcome(mutant, "error", str(e)))
                    continue
                mutated = mutate_source(original, mutant)
                if mutated is None:
                    rep.outcomes.append(Outcome(mutant, "invalid",
                                                "operator did not apply at that line"))
                    continue
                targets = _select_tests(root, mutant.relpath, idx) or ["."]
                src_file.write_text(mutated, encoding="utf-8")
                code, tail = run_declared(work, targets, timeout=timeout)
                src_file.write_text(original, encoding="utf-8")
                if code == 0:
                    rep.outcomes.append(Outcome(mutant, "survived", "tests stayed green"))
                elif code in (-1, -2):
                    rep.outcomes.append(Outcome(mutant, "killed",
                                                f"suite did not complete: {tail}"))
                else:
                    rep.outcomes.append(Outcome(mutant, "killed", tail))
        return rep
    finally:
        changed = tree_changes(before, tree_digests(root))
        if changed:
            raise changed_tree_error("during the mutation probe", changed)


def summary(rep: ProbeReport) -> dict:
    score = rep.score()
    return {
        "ran": rep.ran,
        "reason": rep.reason,
        "candidates": rep.candidates,
        "killed": len(rep.killed()),
        "survived": len(rep.survived()),
        "invalid": sum(1 for o in rep.outcomes if o.status == "invalid"),
        "errors": sum(1 for o in rep.outcomes if o.status == "error"),
        "score": None if score is None else round(score, 3),
        "operators": list(OPERATORS),
    }


def render(rep: ProbeReport, top: int = 20) -> str:
    s = summary(rep)
    if not rep.ran:
        return "\n".join([
            "mutation probe: DID NOT RUN -- this is exit 2, not a score of 0.",
            f"  reason: {rep.reason or 'unknown'}",
            f"  candidates found: {rep.candidates}",
            "  A rate that was never measured must never read as a rate.",
        ])
    L = [
        (f"mutation probe: {s['killed']} killed / "
         f"{s['killed'] + s['survived']} measured = {s['score']:.0%}")
        if s["score"] is not None else "mutation probe: nothing measured",
        f"  candidates {s['candidates']} · invalid {s['invalid']} "
        f"· errors {s['errors']}",
        "",
        "  This is a FLOOR, not a grade: a survivor means no selected test",
        "  pinned that behaviour -- sometimes fine, sometimes a real hole.",
        "",
    ]
    surv = rep.survived()[:top]
    if surv:
        L.append(f"  SURVIVORS ({len(rep.survived())}) -- the actual finding:")
        for o in surv:
            L.append(f"    {o.mutant.function}  line {o.mutant.lineno}")
            L.append(f"        {o.mutant.operator}: {o.mutant.description}")
    if not rep.survived():
        L.append("  no survivors among the mutants tried.")
        L.append("  Not a clean bill: operators are one-at-a-time, only the "
                 "richest functions were tried, and selection is approximate.")
    return "\n".join(L)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("target", help="directory (repo-relative) to mutate")
    sp.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"how many functions to mutate (default {DEFAULT_LIMIT})")
    sp.add_argument("--timeout", type=int, default=RUN_TIMEOUT_S,
                    help=f"per-run wall-clock ceiling in seconds "
                         f"(default {RUN_TIMEOUT_S})")
    sp.add_argument("--operators", default=",".join(OPERATORS),
                    help=f"comma-separated subset of {','.join(OPERATORS)}")
    sp.add_argument("--top", type=int, default=20)


def _mutation_args(ap: argparse.ArgumentParser) -> None:
    ap.epilog = ('A survivor RATE, never a gate. Runs the repo\'s OWN declared domains.test.command inside a COPY with no .git -- a selected test that needs git goes red at the BASELINE (exit 2), never a false "survived".')
    add_arguments(ap)


def _mutation(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    target = root / args.target
    as_json = bool(getattr(opts, "json", False))

    ops = tuple(o.strip() for o in args.operators.split(",") if o.strip())
    unknown = [o for o in ops if o not in OPERATORS]
    if unknown:
        raise hs_family.UsageError(
            f"test mutation: unknown operator(s) {unknown}; known: {list(OPERATORS)}")

    try:
        rep = probe(root, target, limit=args.limit, timeout=args.timeout, operators=ops)
    except (OSError, RuntimeError) as e:
        raise hs_family.UsageError(f"test mutation: {e}") from e

    if as_json:
        print(json.dumps({
            "summary": summary(rep),
            "outcomes": [{"function": o.mutant.function,
                          "line": o.mutant.lineno,
                          "operator": o.mutant.operator,
                          "status": o.status, "detail": o.detail}
                         for o in rep.outcomes],
        }, sort_keys=True, ensure_ascii=False))
    else:
        print(render(rep, top=args.top))

    return 2 if not rep.ran else 0


MUTATION = Command(add_args=_mutation_args, run=_mutation)
