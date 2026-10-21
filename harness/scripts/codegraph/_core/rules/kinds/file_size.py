"""`file_size` — a file past the length threshold must carry a LENGTH VERDICT.

Length is REAL CODE LINES: total minus blank, comment, docstring and multi-line data
string lines (each line subtracted once). `loc` counts documentation too, and on a
well-documented tree that is a third to a half of every large file: counting it makes
the best-documented file write a verdict first, and makes deleting comments a path to
green.

    >= file_warn     needs a verdict — `split` or `measured-no-split`
    >= file_ceiling  needs `split`; `measured-no-split` does not save a file here: a
                     clever enough argument justifies any size, so above the ceiling
                     the argument is not allowed to win

A project may also declare `func_warn`/`func_ceiling` and `nest_warn`/`nest_ceiling`, each
pair whole. Every def is then its own reading unit, keyed `path::qualname` (`A.run`,
`outer.<locals>.run`, `f#2` for the second def at one qualname — `..qualname`):

    code lines   the def's span, `def` line included, minus prose and minus every
                 nested def or class — those are units of their own
    nesting      the blocks around the deepest line, as the eye reads the indentation:
                 an `elif` is one level, `except` aligns with its `try`, a nested def or
                 lambda starts again at 0

Past either warn a def needs a verdict; past either ceiling, `split`.

Line count is not a reason to split; it is a reason to ANSWER in writing. The answer is
a row of the project's verdict table (`verdicts:`, YAML):

    verdicts:
    - {path: app/big.py, verdict: measured-no-split, decision: DEC-..., reason: "..."}
    - {path: "app/big.py::Loader.run", verdict: split, decision: DEC-..., reason: "..."}

(`::` in a flow mapping needs the quotes; an unquoted one is a parse error, named.)

`decision` must be a DEC this harness reads (`decision_register.is_readable_id`); a row
whose id is not one (the retired `D-NN`) is no verdict, and is reported as such. A row
for a file or def no longer past a warn line is stale: the table only shrinks. The
thresholds are the project's (`thresholds: {file_warn, file_ceiling, ...}`), declared
whole — no number lives here.

By default every case REPORTS (named in the notes, the rule stays green); `block: true`
makes them violations.

WHAT IT CANNOT SEE
    * Above the ceiling it demands the word `split`, not a split: a verdict written and
      abandoned stays green.
    * Whether a verdict is RIGHT, or whether its DEC says what the row claims: it checks
      that the id is one the register reads, not that the ruling exists.
    * A multi-line string assigned to a name counts as data; a function that builds a
      string and `exec`s it hides its body the same way.
    * A def renamed or moved keeps no verdict: its row goes stale and the def reads as
      unruled, both named.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ...descriptor.model import ScanSpec, scan_py_counted
from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)
from ..qualname import Namer

_VERDICTS = ("split", "measured-no-split")
_ROW_KEYS = {"path", "verdict", "decision", "reason"}
_REPORTED = ("file-ceiling", "func-ceiling", "file-no-verdict", "func-no-verdict",
             "verdict-unreadable", "stale-verdict")
#: (warn, ceiling) key pairs; `file` is required, the others optional, each whole.
_PAIRS = {"file": ("file_warn", "file_ceiling"), "func": ("func_warn", "func_ceiling"),
          "nest": ("nest_warn", "nest_ceiling")}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_OWN_UNIT = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def code_lines(src: str) -> int:
    """Lines that are neither blank, comment, docstring nor multi-line data string."""
    lines = src.splitlines()
    return len(lines) - len(_prose(lines, ast.parse(src)))


def _prose(lines: list[str], tree: ast.AST) -> set[int]:
    prose: set[int] = {i for i, x in enumerate(lines, 1) if not x.strip() or x.strip().startswith("#")}
    for n in ast.walk(tree):
        span: ast.stmt | None = None
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body[0] if n.body else None
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str):
                span = b
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Constant) \
                and isinstance(n.value.value, str) and (n.end_lineno or n.lineno) > n.lineno:
            span = n
        if span is not None:
            prose.update(range(span.lineno, (span.end_lineno or span.lineno) + 1))
    return prose


def _span(n: ast.stmt) -> range:
    return range(n.lineno, (n.end_lineno or n.lineno) + 1)


def _is_elif(x: ast.If) -> bool:
    """`elif` and `else:` + `if` share one AST; the column tells them apart."""
    return len(x.orelse) == 1 and isinstance(x.orelse[0], ast.If) and x.orelse[0].col_offset == x.col_offset


def _blocks(x: ast.AST) -> list[list[Any]]:
    """The statement lists indented ONE level under `x`."""
    if isinstance(x, ast.If):
        return [x.body] if _is_elif(x) else [x.body, x.orelse]
    if isinstance(x, ast.Match):
        return [list(x.cases)]
    if isinstance(x, ast.match_case):
        return [x.body]
    if isinstance(x, ast.Try) or type(x).__name__ == "TryStar":
        return [x.body, x.orelse, x.finalbody]  # type: ignore[attr-defined]
    if isinstance(x, (ast.For, ast.AsyncFor, ast.While)):
        return [x.body, x.orelse]
    if isinstance(x, (ast.With, ast.AsyncWith, ast.ExceptHandler)):
        return [x.body]
    return []


def nesting(fn: ast.AST) -> int:
    """Blocks around the deepest line of `fn`'s own body."""
    def down(x: ast.AST, depth: int) -> int:
        if isinstance(x, _OWN_UNIT):
            return depth
        inner = _blocks(x)
        m = depth
        for block in inner:
            for c in block:
                m = max(m, down(c, depth + 1))
        seen = {id(c) for block in inner for c in block}
        for c in ast.iter_child_nodes(x):
            if id(c) not in seen:
                m = max(m, down(c, depth))
        return m
    return max((down(c, 0) for c in getattr(fn, "body", ())), default=0)


def def_units(src: str) -> list[tuple[str, int, int]]:
    """(qualname, code lines, nesting) for every def, in line order."""
    lines = src.splitlines()
    tree = ast.parse(src)
    prose = _prose(lines, tree)
    namer = Namer(tree)
    out: list[tuple[str, int, int]] = []
    for n in sorted((x for x in ast.walk(tree) if isinstance(x, _FUNCS)), key=lambda x: (x.lineno, x.col_offset)):
        own = set(_span(n)) - prose
        for c in ast.walk(n):
            if c is not n and isinstance(c, _OWN_UNIT[:3]):
                own -= set(_span(c))
        out.append((namer(n), len(own), nesting(n)))
    return out


def _thresholds(rule_id: str, raw: Any) -> dict[str, tuple[int, int]]:
    """{"file": (warn, ceiling)} plus "func" / "nest" when declared."""
    if not isinstance(raw, dict):
        raise RulesError(f"rule {rule_id!r}: `thresholds: {{file_warn, file_ceiling}}` is required — "
                         f"a length law's numbers are the project's, not the tool's")
    known = {k for pair in _PAIRS.values() for k in pair}
    out: dict[str, tuple[int, int]] = {}
    unknown = sorted(set(raw) - known)
    missing = sorted(k for name, pair in _PAIRS.items() for k in pair
                     if k not in raw and (name == "file" or set(pair) & set(raw)))
    if unknown or missing:
        raise RulesError(f"rule {rule_id!r}: `thresholds` unknown {unknown} / missing {missing} "
                         f"(keys: {', '.join(k for pair in _PAIRS.values() for k in pair)}; "
                         f"file_* required, each other pair whole or absent)")
    for name, (wk, ck) in _PAIRS.items():
        if wk not in raw:
            continue
        warn, ceiling = raw[wk], raw[ck]
        if not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in (warn, ceiling)) \
                or warn >= ceiling:
            raise RulesError(f"rule {rule_id!r}: `thresholds` needs 0 < {wk} < {ck} "
                             f"(got {warn!r}, {ceiling!r})")
        out[name] = (warn, ceiling)
    return out


def load_verdicts(p: Path, rule_id: str) -> list[dict[str, str]]:
    """The table's rows. No file = no verdicts; a malformed row is refused whole, never
    skipped — a skipped row reads exactly like a file nobody ruled on."""
    if not p.is_file():
        return []
    import yaml_io
    try:
        doc = yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
    except yaml_io.YAMLError as e:
        raise RulesError(f"rule {rule_id!r}: {p} does not parse ({' '.join(str(e).split())}) — quote a `path::name` "
                         f"key inside `{{...}}`") from e
    rows = doc.get("verdicts") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise RulesError(f"rule {rule_id!r}: {p}: `verdicts:` must be a list")
    out: list[dict[str, str]] = []
    for i, row in enumerate(rows, 1):
        tag = f"rule {rule_id!r}: {p.name} row {i}"
        if not isinstance(row, dict):
            raise RulesError(f"{tag}: not a mapping")
        if set(row) != _ROW_KEYS:
            raise RulesError(f"{tag}: keys must be exactly {sorted(_ROW_KEYS)} "
                             f"(unknown {sorted(set(row) - _ROW_KEYS)}, missing {sorted(_ROW_KEYS - set(row))})")
        if row["verdict"] not in _VERDICTS:
            raise RulesError(f"{tag}: `verdict` must be one of {list(_VERDICTS)}, got {row['verdict']!r}")
        if not str(row["reason"] or "").strip() or not str(row["path"] or "").strip():
            raise RulesError(f"{tag}: `path` and `reason` must be non-empty — a verdict without "
                             f"its measurement is a mention")
        out.append({k: str(row[k]) for k in _ROW_KEYS})
    return out


class FileSizeCheck:
    CAUSES = _REPORTED

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        self.roots: list[str] = list(params["roots"])
        self.limits = _thresholds(rule_id, params.get("thresholds"))
        self.warn, self.ceiling = self.limits["file"]
        self.scan = ScanSpec(include=tuple(params.get("include") or ("*.py",)),
                             exclude=tuple(params.get("exclude") or ()))
        self.verdicts = Path(params["verdicts"]) if params.get("verdicts") else None
        block = params.get("block", False)
        if not isinstance(block, bool):
            raise RulesError(f"rule {rule_id!r}: `block` must be true or false")
        self.block = block

    def _past_warn(self, lines: int, depth: int) -> tuple[bool, bool]:
        """(past a warn line, past a ceiling) for one def."""
        fn, ns = self.limits.get("func"), self.limits.get("nest")
        warn = bool(fn and lines >= fn[0]) or bool(ns and depth >= ns[0])
        ceiling = bool(fn and lines >= fn[1]) or bool(ns and depth >= ns[1])
        return warn, ceiling

    def _sizes(self, repo: Path) -> tuple[dict[str, int], dict[str, tuple[int, int, bool]], int]:
        """({file: code lines}, {file::def: (code lines, nesting, past a ceiling)}, files
        `exclude:` dropped) — defs only when past a warn line, and only when the project
        declared a def threshold. The dropped count is the caller's to print: a silent
        exclusion is a way for a tree to look cleaner than it is."""
        out: dict[str, int] = {}
        defs: dict[str, tuple[int, int, bool]] = {}
        dropped = 0
        measure_defs = "func" in self.limits or "nest" in self.limits
        for root in self.roots:
            rdir = repo / root
            if not rdir.is_dir():
                raise RulesError(f"rule {self.rule_id!r}: root {root!r} is not a directory under {repo}")
            paths, excluded = scan_py_counted(rdir, self.scan)
            dropped += excluded
            for path in paths:
                rel = path.relative_to(repo).as_posix()
                if "__pycache__" in rel:
                    continue
                src = path.read_text(encoding="utf-8", errors="replace")
                try:
                    out[rel] = code_lines(src)
                    for name, lines, depth in (def_units(src) if measure_defs else ()):
                        warn, ceiling = self._past_warn(lines, depth)
                        if warn:
                            defs[f"{rel}::{name}"] = (lines, depth, ceiling)
                except SyntaxError as e:
                    raise RulesError(f"rule {self.rule_id!r}: {rel} does not parse ({e}) — "
                                     f"an incomplete scan gives no verdict") from e
        return out, defs, dropped

    def check(self, ctx: RuleContext) -> RuleResult:
        import decision_register
        sizes, defs, dropped = self._sizes(ctx.repo)
        rows = []
        if self.verdicts is not None:
            rows = load_verdicts(self.verdicts if self.verdicts.is_absolute() else ctx.repo / self.verdicts,
                                 self.rule_id)
        cases: list[tuple[str, str, str]] = []
        ruled: dict[str, set[str]] = {}
        for r in rows:
            if not decision_register.is_readable_id(r["decision"]):
                cases.append(("verdict-unreadable", r["path"],
                              f"decision {r['decision']!r} is no DEC this harness reads — not a verdict"))
                continue
            if "::" in r["path"] and r["path"] not in defs:
                cases.append(("stale-verdict", r["path"],
                              f"{r['decision']} rules a def not past a function or nesting warn line "
                              f"— delete the row"))
                continue
            if "::" not in r["path"] and sizes.get(r["path"], 0) < self.warn:
                cases.append(("stale-verdict", r["path"],
                              f"{r['decision']} rules a file not past {self.warn} code lines — delete the row"))
                continue
            ruled.setdefault(r["path"], set()).add(r["verdict"])
        for rel, code in sorted(sizes.items()):
            kinds = ruled.get(rel, set())
            if code >= self.ceiling and "split" not in kinds:
                cases.append(("file-ceiling", rel, f"{code} code lines, past the ceiling {self.ceiling} "
                                                   f"with no `split` verdict (measured-no-split cannot save it)"))
            elif code >= self.warn and not kinds:
                cases.append(("file-no-verdict", rel, f"{code} code lines, past {self.warn} with no verdict"))
        for key, (lines, depth, ceiling) in sorted(defs.items()):
            kinds = ruled.get(key, set())
            if ceiling and "split" not in kinds:
                cases.append(("func-ceiling", key, f"{lines} code lines, nesting {depth}: past a function "
                                                   f"ceiling with no `split` verdict"))
            elif not kinds:
                cases.append(("func-no-verdict", key, f"{lines} code lines, nesting {depth}: past a "
                                                      f"function warn line with no verdict"))
        cases.sort(key=lambda c: (_REPORTED.index(c[0]), c[1]))
        v: list[Violation] = []
        shown = [f"file {self.warn}/{self.ceiling} code lines"]
        if "func" in self.limits:
            shown.append("function %d/%d code lines" % self.limits["func"])
        if "nest" in self.limits:
            shown.append("nesting %d/%d" % self.limits["nest"])
        notes: list[str] = ["thresholds: " + ", ".join(shown)]
        if dropped:
            notes.append(f"{dropped} file(s) excluded by `exclude:` — never silently a clean tree")
        for cause, path, msg in cases:
            if self.block:
                v.append(Violation(self.rule_id, cause, f"{path} — {msg}", {"path": path}))
            else:
                notes.append(f"{cause}  {path} — {msg}")
        if cases and not self.block:
            notes.insert(1, f"reporting only ({len(cases)} case(s)); `block: true` makes them fail")
        counts = {c: sum(1 for x in cases if x[0] == c) for c in _REPORTED}
        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes),
                          metrics={"files": len(sizes), "defs_past_warn": len(defs), "cases": counts,
                                   "files_excluded": dropped})


register_kind("file_size")(require_params("roots")(FileSizeCheck))
