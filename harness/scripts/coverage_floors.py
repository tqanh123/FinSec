"""Per-group line-coverage floors over any runner's report.

A single global percentage is the wrong instrument: a line-weighted mean lets thousands of
well-covered lines bury the few hundred that matter most (this harness measured 89% overall
with its guard hooks at 60%). So floors are declared per GROUP and judged per group.

    read_lcov / read_istanbul_summary / read_coveragepy
                every report becomes the same per-file `FileCov` keyed by the repo-relative
                path. LCOV `SF:` is relative to where its run started -- vitest's root, jest's
                rootDir -- so the reader joins it to `base`; absolute keys (json-summary,
                coverage.py) go through `rel_key`. Two runs over one file merge line by line
    evaluate    each group's found/hit, judged against its floor; a group with no floor is
                REPORTED, never judged
    codemap_groups / declared_floors
                the code map's `groups:` (first match wins, `dir/**` takes the subtree) and its
                `coverage:` block (`floors: {<group>: {line: N}}`, `metric: lines`)

Two refusals, never a guess:
    * a report that is missing, unparseable or measures nothing raises `Unmeasurable` --
      never 0%, never a pass: a false 0% lets a regression through looking like a number;
    * a group nothing in the report falls in, or whose files on disk are absent from the
      report, is UNMEASURABLE: absent files are uncovered, so leaving them out only raises
      the number (vitest 4+ reports only the files a test loaded unless `coverage.include`
      names them; jest likewise without `collectCoverageFrom`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional

METRICS = ("lines",)
_COVERAGE_KEYS = frozenset({"metric", "runner", "provider", "floors"})


class Unmeasurable(ValueError):
    """A report that did not read: a failure, never 0%."""


class CoverageSpecError(ValueError):
    """A `coverage:` block that does not read."""


@dataclass
class FileCov:
    found: int
    hit: int
    #: line -> hit, when the report carries per-line data (LCOV `DA:`): merged by line
    lines: "dict[int, bool]" = field(default_factory=dict)


# ---------------------------------------------------------------- reading

def rel_key(path: str, root: Path) -> "str | None":
    """A report key as a repo-relative posix path, or None when it names no file of ours.

    Lexical on purpose: resolving would follow symlinks, and a root reached through a link
    would then disagree with a report written through the real path. A path outside `root`
    is None -- its group goes UNMEASURABLE rather than scoring a file of another tree."""
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not (p.startswith("/") or (len(p) > 1 and p[1] == ":")):
        return p
    base = PurePosixPath(str(root).replace("\\", "/"))
    try:
        return PurePosixPath(p).relative_to(base).as_posix()
    except ValueError:
        return None


def _joined(sf: str, root: Path, base: Path) -> "str | None":
    p = sf.replace("\\", "/")
    if not (p.startswith("/") or (len(p) > 1 and p[1] == ":")):
        p = (PurePosixPath(str(base).replace("\\", "/")) / p).as_posix()
    rel = rel_key(p, root)
    if rel is None:
        return None
    parts: "list[str]" = []
    for seg in rel.split("/"):
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/".join(parts)


def read_lcov(text: str, root: Path, *, base: Path) -> "dict[str, FileCov]":
    """LCOV records by repo-relative path; `SF:` relative to `base`, the directory the run
    started from. Lines come from `DA:`; `LF`/`LH` only when a record has no `DA:`."""
    out: "dict[str, FileCov]" = {}
    cur: "Optional[str]" = None
    das: "dict[int, bool]" = {}
    lf = lh = None
    records = 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("TN:"):
            continue
        tag, _, val = line.partition(":")
        if line == "end_of_record":
            if cur is None:
                raise Unmeasurable("lcov line %d: end_of_record with no SF" % n)
            records += 1
            rel = _joined(cur, root, base)
            if rel is not None:
                fc = (FileCov(len(das), sum(das.values()), dict(das)) if das
                      else FileCov(int(lf or 0), int(lh or 0)))
                out = merge(out, {rel: fc})
            cur, das, lf, lh = None, {}, None, None
        elif tag == "SF":
            if cur is not None:
                raise Unmeasurable("lcov line %d: SF before the previous end_of_record" % n)
            cur = val
        elif cur is None:
            raise Unmeasurable("lcov line %d: %r outside a record" % (n, line[:40]))
        elif tag == "DA":
            try:
                ln, count = val.split(",")[:2]
                das[int(ln)] = das.get(int(ln), False) or int(count) > 0
            except ValueError:
                raise Unmeasurable("lcov line %d: DA is not `line,count`: %r" % (n, val)) from None
        elif tag in ("LF", "LH"):
            try:
                lf, lh = (int(val), lh) if tag == "LF" else (lf, int(val))
            except ValueError:
                raise Unmeasurable("lcov line %d: %s is not a number" % (n, tag)) from None
    if cur is not None:
        raise Unmeasurable("lcov ends inside the record of %s (no end_of_record)" % cur)
    if not records:
        raise Unmeasurable("lcov holds no record")
    return out


def read_istanbul_summary(doc: Any, root: Path) -> "dict[str, FileCov]":
    """`coverage-summary.json`: absolute keys beside `total`, `lines: {total, covered}`."""
    if not isinstance(doc, dict) or "total" not in doc:
        raise Unmeasurable("json-summary has no `total`: not an istanbul summary")
    out: "dict[str, FileCov]" = {}
    for key, entry in doc.items():
        if key == "total":
            continue
        try:
            lines = entry["lines"]
            fc = FileCov(int(lines["total"]), int(lines["covered"]))
        except (KeyError, TypeError, ValueError):
            raise Unmeasurable("json-summary entry %s has no lines.total/covered" % key) from None
        rel = rel_key(key, root)
        if rel is not None:
            out = merge(out, {rel: fc})
    return out


def read_coveragepy(doc: Any, root: Path) -> "dict[str, FileCov]":
    """coverage.py JSON: `files[<path>].summary.{num_statements, missing_lines}`."""
    if not isinstance(doc, dict) or "files" not in doc:
        raise Unmeasurable("carries no `files` key, so nothing was measured")
    out: "dict[str, FileCov]" = {}
    for key, entry in (doc.get("files") or {}).items():
        rel = rel_key(key, root)
        if rel is None:
            continue
        s = (entry or {}).get("summary") or {}
        found = int(s.get("num_statements", 0))
        out[rel] = FileCov(found, found - int(s.get("missing_lines", 0)))
    return out


def merge(*maps: "Mapping[str, FileCov]") -> "dict[str, FileCov]":
    """One file reported by more than one run: per-line data unions (a line any run hit is
    hit); a total-only record keeps the larger hit."""
    out: "dict[str, FileCov]" = {}
    for m in maps:
        for rel, fc in m.items():
            have = out.get(rel)
            if have is None:
                out[rel] = FileCov(fc.found, fc.hit, dict(fc.lines))
            elif have.lines and fc.lines:
                lines = dict(have.lines)
                for ln, h in fc.lines.items():
                    lines[ln] = lines.get(ln, False) or h
                out[rel] = FileCov(len(lines), sum(lines.values()), lines)
            elif fc.hit > have.hit:
                out[rel] = FileCov(fc.found, fc.hit, dict(fc.lines))
    return out


def total(per_file: "Mapping[str, FileCov]") -> "tuple[int, int]":
    return sum(f.found for f in per_file.values()), sum(f.hit for f in per_file.values())


# ---------------------------------------------------------------- groups

@dataclass(frozen=True)
class Group:
    name: str
    contains: Callable[[str], bool]
    floor: "Optional[float]" = None
    why: str = ""
    #: how the group was declared (its globs), for a message
    label: str = ""


@dataclass
class GroupResult:
    name: str
    status: str            # PASS | FAIL | REPORTED | UNMEASURABLE
    found: int = 0
    hit: int = 0
    floor: "Optional[float]" = None
    matched: int = 0
    absent: "list[str]" = field(default_factory=list)
    reason: str = ""

    @property
    def pct(self) -> "Optional[float]":
        return 100.0 * self.hit / self.found if self.found else None


def codemap_groups(decl: "Iterable[Mapping[str, Any]]",
                   floors: "Mapping[str, float]") -> "list[Group]":
    """The code map's `groups:` -- first match wins, by the code map's own glob rule
    (`graph.model.path_glob`) -- each with its floor, if declared."""
    from codegraph._core.graph.model import path_glob
    order = [(str(g["name"]), tuple(g["match"])) for g in decl]

    def first(rel: str) -> "Optional[str]":
        return next((n for n, pats in order if any(path_glob(rel, p) for p in pats)), None)

    def member(name: str) -> "Callable[[str], bool]":
        return lambda rel: first(rel) == name

    return [Group(name, member(name), floors.get(name), label=str(list(pats)))
            for name, pats in order]


def declared_floors(block: Any, groups: "Iterable[Mapping[str, Any]]") -> "dict[str, float]":
    """`coverage:` → {group: line floor}. `CoverageSpecError` names what does not read."""
    if block in (None, {}):
        return {}
    if not isinstance(block, dict):
        raise CoverageSpecError("`coverage:` must be a mapping")
    unknown = sorted(set(block) - _COVERAGE_KEYS)
    if unknown:
        raise CoverageSpecError("`coverage:` has unknown key(s) %s (known: %s)"
                                % (", ".join(unknown), ", ".join(sorted(_COVERAGE_KEYS))))
    if block.get("metric", "lines") not in METRICS:
        raise CoverageSpecError("`coverage.metric` %r: only %s is measured the same way by "
                                "every runner" % (block.get("metric"), ", ".join(METRICS)))
    for k in ("runner", "provider"):
        if k in block and not (isinstance(block[k], str) and block[k]):
            raise CoverageSpecError("`coverage.%s` must be a non-empty string" % k)
    names = {str(g.get("name")) for g in groups}
    floors = block.get("floors") or {}
    if not isinstance(floors, dict):
        raise CoverageSpecError("`coverage.floors` must map a group name to {line: N}")
    out: "dict[str, float]" = {}
    for name, spec in floors.items():
        if name not in names:
            raise CoverageSpecError("`coverage.floors.%s` names no group in `groups:` (%s)"
                                    % (name, ", ".join(sorted(names)) or "none declared"))
        if not isinstance(spec, dict) or set(spec) != {"line"}:
            raise CoverageSpecError("`coverage.floors.%s` must be {line: N}" % name)
        line = spec["line"]
        if isinstance(line, bool) or not isinstance(line, (int, float)) or not 0 <= line <= 100:
            raise CoverageSpecError("`coverage.floors.%s.line` must be a percentage, got %r" % (name, line))
        out[str(name)] = line
    return out


def evaluate(per_file: "Mapping[str, FileCov]", groups: "Iterable[Group]",
             on_disk: "Callable[[Group], set[str]]") -> "list[GroupResult]":
    """Each group's numbers and verdict. `on_disk(group)` is the group's files as the tree
    holds them -- an empty set means the report came from a tree we cannot see, which is no
    evidence of an omission."""
    out: "list[GroupResult]" = []
    for g in groups:
        mine = {rel: fc for rel, fc in per_file.items() if g.contains(rel)}
        r = GroupResult(g.name, "REPORTED", floor=g.floor, matched=len(mine))
        r.found, r.hit = total(mine)
        if not mine or r.found == 0:
            r.status = "UNMEASURABLE"
            r.reason = ("%d file(s) matched %s in the report. An empty group averages to "
                        "nothing, and nothing reads as clean; check the globs before trusting "
                        "this." % (len(mine), g.label))
        else:
            disk = on_disk(g)
            r.absent = sorted(disk - set(mine))
            if r.absent:
                r.status = "UNMEASURABLE"
                r.reason = ("%d of %d file(s) on disk are absent from the report (%s%s). Absent "
                            "files are uncovered, so leaving them out can only raise the number."
                            % (len(r.absent), len(disk), ", ".join(r.absent[:3]),
                               ", …" if len(r.absent) > 3 else ""))
            elif g.floor is not None:
                r.status = "FAIL" if (r.pct or 0.0) + 1e-9 < g.floor else "PASS"
        out.append(r)
    return out


def breach_lines(results: "Iterable[GroupResult]", groups: "Iterable[Group]",
                 unit: str = "lines") -> "list[str]":
    """The one-line refusals, in group-name order: UNMEASURABLE, or below the floor."""
    why = {g.name: g.why for g in groups}
    out = []
    for r in sorted(results, key=lambda r: r.name):
        if r.status == "UNMEASURABLE":
            out.append("%s: UNMEASURABLE — %s" % (r.name, r.reason))
        elif r.status == "FAIL":
            out.append("%s: %.1f%% is below the floor of %s (%d/%d %s missed). %s"
                       % (r.name, r.pct or 0.0, r.floor, r.found - r.hit, r.found, unit,
                          why.get(r.name, "")))
    return out
