"""`code_shape` — four code shapes, each reporting until a project makes it block.

Pure stdlib `ast` over the files under the rule's `roots` (the descriptor's own
`scan_py_counted`, so include/exclude mean what they mean everywhere else, and a
file `exclude:` drops is a counted note, never a silent absence).

THE FOUR SHAPES
    TYPE-RETURN-PUBLIC    a public function declares its return type
    TIME-TZ-AWARE         no bare `datetime.now()` / any `utcnow()`
    EXCEPT-SWALLOW        no `except ...: pass`
    DATACLASS-FROZEN      a `@dataclass` says `frozen=True`

    Mined from two codebases: each shape was measured in both, and kept only
    when the two numbers differed. A shape both share the same way
    distinguishes nothing.

REPORT BY DEFAULT, BLOCK BY DECLARATION
    Every shape runs and every case it finds is NAMED in the notes, while the
    verdict stays green: a green still says what it saw. A project raises a
    shape to blocking by listing it under `block:`; from then on a case the
    baseline does not hold is a violation. There is no key that lowers a
    shape — the old `warn:` spelling is refused, not read.

BASELINE — TWO-WAY, SHRINK-ONLY (blocking shapes)
    A case not in the baseline            -> violation
    a baseline line that no longer fires  -> violation (`baseline-stale`;
    delete it in the SAME commit that fixed it). One file holds every shape's
    debt; the key is (shape, file, name) with no line number for the two
    shapes keyed by a name, so moving code inside a file is not noise.

WHAT IT CANNOT SEE
    * A PRESENT return annotation is not a CORRECT one: `-> Any` counts.
    * `datetime.now()` through a variable, `getattr`, or a wrapper under
      another name is invisible; only a call rooted at `datetime`/`dt` counts.
    * `except ...: pass` is the NARROWEST swallow: `except: return None`,
      `except: continue` or a bare log-then-carry-on are not counted. A floor.
    * `frozen=True` does not measure immutability: a frozen dataclass holding
      a list is still mutable.
    * Test files are outside the denominator: an `except: pass` in a test is
      the test's tool, not a product shape.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ...descriptor.model import ScanSpec, scan_py_counted
from ...testfiles import build_test_layout, in_test_tree
from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)
from ..qualname import Namer

#: clock names that return "now" WITHOUT timezone information.
NAIVE_CLOCK = frozenset({"now", "utcnow"})

SHAPES: tuple[str, ...] = ("TYPE-RETURN-PUBLIC", "TIME-TZ-AWARE",
                           "EXCEPT-SWALLOW", "DATACLASS-FROZEN")

#: named cases printed per run; the rest are counted.
_NAMED = 20


def is_test(rel: str, layout: Any = None) -> bool:
    """Under a test-tree ancestor directory: DECLARED `layout.tests_subdir`
    (via `layout`, when given) wins outright, else the UNDECLARED
    convention `codegraph._core.testfiles.in_test_tree`'s own rule
    (`tests?(_.+)?`) — the tree pytest collects. A NAME that starts with
    `test` is no evidence here: on this repo every `test_*.py`/`*_test.py`
    outside a test-tree dir is a product module (`test_policy.py`,
    `hs_run_test.py`), and this repo's own `skills/test/` is a PRODUCT
    directory despite matching the undeclared convention — see `codemap.
    yaml`'s own `layout.tests_subdir: tests` declaration, which `_found`
    below resolves once per call via `build_test_layout` and is what
    actually excludes it (`layout=None`, the bare-function default, can
    only ever answer the undeclared convention — it has no `root` to load
    a descriptor from)."""
    return layout.in_test_tree(rel) if layout is not None else in_test_tree(rel)


def _decorator_name(d: ast.expr) -> str:
    if isinstance(d, ast.Call):
        d = d.func
    if isinstance(d, ast.Attribute):
        return d.attr
    if isinstance(d, ast.Name):
        return d.id
    return ""


def scan_tree(tree: ast.AST, rel: str) -> list[tuple[str, str, str]]:
    """(shape, file, name) for every case in one parsed file. A def is named by its
    qualname; the second definition at one qualname is `name#2`, and so on in line order."""
    namer = Namer(tree)

    def named(shape: str, n: ast.AST) -> str:
        return namer(n, shape)

    out: list[tuple[str, str, str]] = []
    nodes = sorted(ast.walk(tree), key=lambda x: (getattr(x, "lineno", 0), getattr(x, "col_offset", 0)))
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not n.name.startswith("_") and n.returns is None:
                out.append(("TYPE-RETURN-PUBLIC", rel, named("TYPE-RETURN-PUBLIC", n)))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in NAIVE_CLOCK:
            # `x.now(tz)` / `x.now(tz=...)` is correct; `utcnow()` has no fix
            naive = n.func.attr == "utcnow" or (not n.args and not n.keywords)
            base = n.func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            root_name = base.id if isinstance(base, ast.Name) else ""
            # a repo's own `Clock.now()` is a door, not a naive clock
            if naive and root_name in ("datetime", "dt"):
                out.append(("TIME-TZ-AWARE", rel, f"{root_name}.…{n.func.attr}():{n.lineno}"))
        if isinstance(n, ast.ExceptHandler):
            if len(n.body) == 1 and isinstance(n.body[0], ast.Pass):
                out.append(("EXCEPT-SWALLOW", rel, f"except:{n.lineno}"))
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list:
                if _decorator_name(d) != "dataclass":
                    continue
                frozen = isinstance(d, ast.Call) and any(
                    k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value
                    for k in d.keywords)
                if not frozen:
                    out.append(("DATACLASS-FROZEN", rel, named("DATACLASS-FROZEN", n)))
    return out


def load_baseline(p: Path) -> set[tuple[str, str, str]]:
    """`- shape | file | name` lines. No file = an empty set: an absent
    baseline must not read as a clean tree."""
    if not p.is_file():
        return set()
    out: set[tuple[str, str, str]] = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln.startswith("- "):
            continue
        parts = [x.strip() for x in ln[2:].split("|")]
        if len(parts) != 3:
            raise RulesError(f"{p}: line `{ln[:60]}` does not have the three parts "
                             f"`shape | file | name`")
        out.add((parts[0], parts[1], parts[2]))
    return out


class CodeShapeCheck:
    CAUSES = SHAPES + ("baseline-stale",)

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        if "warn" in params:
            raise RulesError(
                f"rule {rule_id!r}: `warn:` is not a key — every shape already reports "
                f"without failing; list the shapes that must fail under `block:`")
        block = list(params.get("block") or ())
        unknown = sorted(set(block) - set(SHAPES))
        if unknown:
            raise RulesError(f"rule {rule_id!r}: `block:` names no such shape "
                             f"{', '.join(unknown)}; the shapes are {', '.join(SHAPES)}")
        self.rule_id = rule_id
        self.roots: list[str] = list(params["roots"])
        self.scan = ScanSpec(include=tuple(params.get("include") or ("*.py",)),
                             exclude=tuple(params.get("exclude") or ()))
        self.baseline_path = Path(params["baseline"]) if params.get("baseline") else None
        self.block: set[str] = set(block)

    def _found(self, repo: Path) -> tuple[set[tuple[str, str, str]], int, int]:
        layout = build_test_layout(repo)
        found: set[tuple[str, str, str]] = set()
        files = 0
        dropped = 0
        bad: list[str] = []
        for root in self.roots:
            rdir = repo / root
            if not rdir.is_dir():
                raise RulesError(f"rule {self.rule_id!r}: root {root!r} is not a "
                                 f"directory under {repo}")
            paths, excluded = scan_py_counted(rdir, self.scan)
            dropped += excluded
            for path in paths:
                rel = path.relative_to(repo).as_posix()
                if "__pycache__" in rel or is_test(rel, layout):
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError:
                    bad.append(rel)
                    continue
                files += 1
                found.update(scan_tree(tree, rel))
        if bad:
            raise RulesError(f"rule {self.rule_id!r}: {len(bad)} file(s) did not parse "
                             f"(first {bad[0]}) — an incomplete scan gives no verdict")
        return found, files, dropped

    def check(self, ctx: RuleContext) -> RuleResult:
        found, files, dropped = self._found(ctx.repo)
        bl = None
        if self.baseline_path is not None:
            bl = (self.baseline_path if self.baseline_path.is_absolute()
                  else ctx.repo / self.baseline_path)
        prior = load_baseline(bl) if bl else set()
        v: list[Violation] = []
        reported: list[tuple[str, str, str]] = []
        for s, f, name in sorted(found - prior):
            if s not in self.block:
                reported.append((s, f, name))
                continue
            v.append(Violation(self.rule_id, s, f"{s}  {f}  {name}  — a case the "
                               f"baseline does not hold", {"shape": s, "file": f, "name": name}))
        notes: list[str] = []
        if dropped:
            notes.append(f"{dropped} file(s) excluded by `exclude:` — never silently a clean tree")
        for s, f, name in sorted(prior - found):
            if s not in self.block:
                notes.append(f"{s}  {f}  {name} — baseline line no longer reproduces "
                             f"(delete it in the SAME commit)")
                continue
            v.append(Violation(self.rule_id, "baseline-stale",
                               f"{s}  {f}  {name}  — baseline line no longer reproduces; "
                               f"delete it in the SAME commit that fixed it",
                               {"shape": s, "file": f, "name": name}))
        soft = [s for s in SHAPES if s not in self.block]
        if soft:
            notes.insert(0, f"reporting only (list under `block:` to fail): {', '.join(soft)}")
        for s, f, name in reported[:_NAMED]:
            notes.append(f"{s}  {f}  {name}")
        if len(reported) > _NAMED:
            notes.append(f"… {len(reported) - _NAMED} more reported case(s)")
        if self.block and self.baseline_path is None:
            notes.append("no `baseline` — every case of a blocking shape is new")
        counts = {s: sum(1 for c in found if c[0] == s) for s in SHAPES}
        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes),
                          metrics={"files": files, "cases": counts, "files_excluded": dropped})


register_kind("code_shape")(require_params("roots")(CodeShapeCheck))
