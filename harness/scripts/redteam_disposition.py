"""Is a red-team REVISE disposed of? One answer for both doors.

The red-team gate used to be a glob: a report file on disk cleared it, so a
report concluding REVISE with four HIGH findings let `plan next` call red-team
"ready" and move on. Judging a review's content is still not a CLI's job. What a
CLI CAN read is a field the reviewer declared, the same way the approval door
reads `decided_by` off the size receipt:

- the report's `verdict: PASS` or `verdict: REVISE` line (the key and the value
  stay English whatever language the prose is in);
- the report's findings table, the one whose first column is headed `id`;
- plan.md's `## Red-team disposition` tables, where every finding gets a row. A round
  per table is fine: the heading may carry a suffix (`## Red-team disposition — r2
  (security)`), and a table whose suffix names a report (its persona, stem or file
  name) covers that report's ids only; a table naming none covers every report.

A REVISE holds the plan until that table has no empty cell and a row for every
id of every REVISE report. A full table from an earlier round does not cover a
later round's ids. A report without the line, or a REVISE without an `id` table,
is named, never guessed at: two prose conclusions in two languages are two
parsers waiting to disagree. So is a heading that is almost the form (another level,
`Redteam`, `dispositions`): its line is quoted, never read as "no table".

What stays unchecked, and is said so: whether an accepted finding actually
reached the phase files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HEADING = "## Red-team disposition"

_VERDICT = re.compile(r"^verdict:\s*(PASS|REVISE)\s*$", re.IGNORECASE | re.MULTILINE)
_HEADING = re.compile(r"^##\s+red-team disposition\b(.*)$", re.IGNORECASE)
_NEAR = re.compile(r"^#{1,6}\s*red[\s_-]*team[\s_-]*dispositions?", re.IGNORECASE)
_PERSONA = re.compile(r"^from-code-reviewer-to-planner-red-team-(.+)-plan-review-report$")
_SEPARATOR = re.compile(r"^\|?[\s:|-]+\|?$")


def reports(plan_dir: Path) -> list:
    """The red-team reports on disk, empty files excluded (`hs_run_plan.
    _redteam_reports` carries the why). Lives here so the approval door finds the
    same files `plan next` does without reaching into another module's privates."""
    d = Path(plan_dir) / "reports"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("from-code-reviewer-to-planner-red-team-*-plan-review-report.md")):
        try:
            if p.stat().st_size > 0:
                out.append(p)
        except OSError:
            # Unreadable is not empty. A stat that fails says nothing about the
            # file, and dropping it would turn one I/O hiccup into "no review
            # happened" — the same false claim from the opposite direction.
            out.append(p)
    return out


def verdict_of(report: Path) -> "str | None":
    """`PASS`, `REVISE`, or None when the report declares neither."""
    try:
        text = Path(report).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _VERDICT.search(text)
    return m.group(1).upper() if m else None


def _norm(cell: str) -> str:
    return cell.strip().strip("*`_ ").strip()


def finding_ids(report: Path) -> "list | None":
    """First-column cells of every table headed `id`, or None when there is none."""
    try:
        lines = Path(report).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    ids: list = []
    found = False
    inside = False
    for line in lines:
        s = line.strip()
        if not s.startswith("|"):
            inside = False
            continue
        if not inside:
            inside = _norm(_cells(s)[0]).lower() == "id"
            found = found or inside
            continue
        if _SEPARATOR.match(s):
            continue
        cell = _norm(_cells(s)[0])
        if cell and cell not in ids:
            ids.append(cell)
    return ids if found else None


def _cells(line: str) -> list:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


@dataclass
class Section:
    line: int
    suffix: str
    rows: list
    read: list = field(default_factory=list)  # reference files its body came from


def sections(plan_md: Path) -> "tuple[list, list]":
    """(every disposition section, every near-miss heading as (line, text))."""
    try:
        lines = Path(plan_md).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], []
    found, near = [], []
    for i, l in enumerate(lines):
        m = _HEADING.match(l.strip())
        if m:
            body, read = _body(lines, i, Path(plan_md))
            found.append(Section(i + 1, m.group(1).strip(), _section_rows([l] + body, 0), read))
        elif _NEAR.match(l.strip()):
            near.append((i + 1, l.strip()))
    return found, near


def _body(lines: list, start: int, plan_md: Path) -> "tuple[list, list]":
    """(the section's lines up to the next heading, a pointer line read as the reference
    file it points to (`plan_refs`); each file read that way, with its pointer line)."""
    import plan_refs
    end = next((k for k in range(start + 1, len(lines)) if lines[k].strip().startswith("#")),
               len(lines))
    body: list = []
    read: list = []
    pins = plan_refs.pinned(plan_md.parent)
    for k in range(start + 1, end):
        target = plan_refs.pointer_target(lines[k], plan_md, plan_md.parent, pins)
        if target is None:
            body.append(lines[k])
            continue
        body.extend(plan_refs.body(target))
        read.append("%s (through the pointer at %s:%d)"
                    % (target.relative_to(plan_md.parent.resolve()).as_posix(),
                       plan_md.name, k + 1))
    return body, read


def disposition_rows(plan_md: Path) -> "list | None":
    """The data rows of every disposition table in plan.md, or None without a heading."""
    found, _near = sections(plan_md)
    return [r for s in found for r in s.rows] if found else None


def _section_rows(lines: list, start: int) -> list:
    """The section runs from the heading to the next heading of any level; inside it,
    the first table's header and separator lines are skipped."""
    rows: list = []
    header_seen = False
    for line in lines[start + 1:]:
        s = line.strip()
        if s.startswith("#"):
            break
        if not s.startswith("|"):
            if rows or header_seen:
                if not s:
                    continue
                break
            continue
        if not header_seen:
            header_seen = True
            continue
        if _SEPARATOR.match(s):
            continue
        rows.append(_cells(s))
    return rows


@dataclass
class Disposition:
    """`blocking` holds the plan; `notes` are said on the envelope and change nothing."""
    blocking: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def check(plan_dir: Path, reports: list) -> Disposition:
    out = Disposition()
    revise, silent, unlisted = [], [], []
    owed: dict = {}
    for r in reports:
        v = verdict_of(r)
        if v == "REVISE":
            revise.append(Path(r).name)
            ids = finding_ids(r)
            if ids is None:
                unlisted.append(Path(r).name)
            owed[Path(r).name] = ids or []
        elif v is None:
            silent.append(Path(r).name)
    if unlisted:
        out.notes.append(
            "no findings table with an `id` first column in %s — whether each of "
            "its findings has a disposition row is not checked" % ", ".join(unlisted))
    if silent:
        out.notes.append(
            "no `verdict: PASS|REVISE` line in %s — a REVISE there would not hold "
            "the plan; add the line, or read the report and dispose of it by hand"
            % ", ".join(silent))
    if not revise:
        return out
    plan_md = Path(plan_dir) / "plan.md"
    found, near = sections(plan_md)
    rows = [r for s in found for r in s.rows] if found else None
    said = "%s concludes REVISE" % ", ".join(revise)
    quoted = "; ".join("line %d «%s»" % n for n in near)
    if near and found:
        out.notes.append("heading(s) not in the `%s` form, so not read: %s" % (HEADING, quoted))
    if rows is None:
        out.blocking.append(
            "%s and %s has no `%s` table: add it with one row per finding "
            "(Accept: where the plan changed / Reject: the evidence)%s"
            % (said, plan_md, HEADING,
               "; heading(s) found but not in that form: %s" % quoted if near else ""))
    elif not rows:
        where = "; ".join(r for s in found for r in s.read) or str(plan_md)
        out.blocking.append("%s and the `%s` table in %s has no rows"
                            % (said, HEADING, where))
    else:
        open_rows = [_norm(r[0]) or "row %d" % (i + 1)
                     for i, r in enumerate(rows) if any(not c for c in r)]
        if open_rows:
            out.blocking.append("%s and the `%s` table still has empty cells in: %s"
                                % (said, HEADING, ", ".join(open_rows)))
        missing = []
        for name, ids in owed.items():
            covered = {_norm(r[0]) for s in found if _covers(s.suffix, name, owed)
                       for r in s.rows}
            missing += ["%s (%s)" % (i, name) for i in ids if i not in covered]
        if missing:
            out.blocking.append("%s and the `%s` table has no row for: %s"
                                % (said, HEADING, ", ".join(missing)))
    return out


def _names(report: str) -> list:
    stem = report[:-3] if report.endswith(".md") else report
    m = _PERSONA.match(stem)
    return [report, stem] + ([m.group(1)] if m else [])


def _covers(suffix: str, report: str, owed: dict) -> bool:
    """A table covers `report` when its heading names it, or names no REVISE report."""
    def named(r: str) -> bool:
        return any(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(n), suffix, re.IGNORECASE)
                   for n in _names(r))
    return named(report) or not any(named(r) for r in owed)


# A cite in a disposition cell: `path:N` or `path:N-M`, or `:N` for the path named
# before it in the same cell; a «quote» right after it is what line N holds.
_CITE = re.compile(r"`((?:[\w.-]+/)*[\w.-]+\.\w+)?:(\d+)(?:-(\d+))?`(\s*«(.*?)»)?", re.DOTALL)
_KINDS = ("matched", "moved", "not_found", "out_of_range", "missing_file", "range_only", "outside",
          "no_file", "foreign")
# `of <doc>` / `của <doc>` right after a cite says the line is another document's: the
# file the cell named before it is not the one meant, so the cite is neither checked
# nor moved, and a bare `:N` after it in the cell means that document too.
_FOREIGN = re.compile(r"\s*[(,]?\s*(?:của|of)\s+[^\s`]", re.IGNORECASE)
_ELSEWHERE = object()


def _resolve(plan_dir: Path, root: Path, rel: str) -> "tuple[Path, bool]":
    """(file, plan_local). Plan-local: `plan.md`, under the plan dir, or `plans/...`
    — the files a disposition row edits and this check can answer for."""
    if rel.startswith("plans/"):
        return Path(root) / rel, True
    here = Path(plan_dir) / rel
    return here, rel == "plan.md" or rel.startswith("phases/") or here.is_file()


def check_cites(plan_dir: Path, root) -> dict:
    """{"counts": {...}, "warnings": [...]} for every cite in the disposition table —
    advisory: the counts always, a warning per kind that did not hold."""
    import plan_handoff

    rows = disposition_rows(Path(plan_dir) / "plan.md") or []
    counts = dict.fromkeys(("cites", "quoted", *_KINDS), 0)
    bad: dict = {k: [] for k in ("moved", "not_found", "out_of_range", "missing_file", "no_file")}
    for row in rows:
        for cell in row:
            last: object = None
            for m in _CITE.finditer(cell):
                if _FOREIGN.match(cell, m.end()) or (m.group(1) is None and last is _ELSEWHERE):
                    counts["cites"] += 1
                    counts["foreign"] += 1
                    last = _ELSEWHERE
                    continue
                rel = m.group(1) or last
                if rel is None:
                    counts["cites"] += 1
                    counts["no_file"] += 1
                    bad["no_file"].append("`:%s`" % m.group(2))
                    continue
                assert isinstance(rel, str)
                last = rel
                start = int(m.group(2))
                end = int(m.group(3) or start)
                counts["cites"] += 1
                path, local = _resolve(Path(plan_dir), Path(root), rel)
                where = "`%s:%s`" % (rel, m.group(2) + ("-" + m.group(3) if m.group(3) else ""))
                if not local:
                    counts["outside"] += 1
                    continue
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    counts["missing_file"] += 1
                    bad["missing_file"].append(where)
                    continue
                quote = m.group(5)
                if quote is None:
                    if end > len(lines):
                        counts["out_of_range"] += 1
                        bad["out_of_range"].append("%s (file has %d lines)" % (where, len(lines)))
                    else:
                        counts["range_only"] += 1
                    continue
                counts["quoted"] += 1
                frags = [f for f in (plan_handoff._ws(p) for p in quote.split("…")) if f]
                if plan_handoff._holds(lines[start - 1:end], frags):
                    counts["matched"] += 1
                    continue
                size = end - start + 1
                at = [k + 1 for k in range(max(len(lines) - size + 1, 0))
                      if plan_handoff._holds(lines[k:k + size], frags)]
                kind = "moved" if at else "not_found"
                counts[kind] += 1
                bad[kind].append(where + (" now at %s" % ", ".join(":%d" % n for n in at[:3])
                                          if at else ""))
    why = {"moved": "the quoted text moved", "not_found": "the quoted text is not there",
           "out_of_range": "the line is past the end of the file",
           "missing_file": "the plan file is not there",
           "no_file": "no file named before it in the cell (another plan's file is "
                      "`plans/<plan>/...:N`)"}
    cmd = _command(plan_dir)
    remap = ("; if plan text was moved, `%s --remap-cites --from <git rev or old plan dir>` "
             "rewrites each to where its old lines are now (an approved plan is then "
             "re-approved)" % cmd)
    warnings = ["disposition cite(s) that no longer point where they say — %s: %s%s" % (
        why[k], ", ".join(v[:5]) + (" …" if len(v) > 5 else ""),
        remap if k in ("moved", "not_found", "out_of_range") else "")
        for k, v in bad.items() if v]
    if counts["foreign"]:
        warnings.append(
            "%d disposition cite(s) name another document (`of`/`của` after them) and are "
            "not checked; name its path, `plans/<plan>/<file>.md:N`, to have one checked"
            % counts["foreign"])
    if counts["range_only"]:
        warnings.append(
            "%d disposition cite(s) checked only for range — a line inserted above one moves it "
            "unseen; follow each with a «quote» of the line to have it checked by content "
            "(`%s --pin-cites` adds them from the current text; an approved plan is then "
            "re-approved)" % (counts["range_only"], cmd))
    return {"counts": counts, "warnings": warnings}


# --------------------------------------------------------------- cite rewriting ---

def _command(plan_dir) -> str:
    """The runnable rewrite command for this plan."""
    import shlex
    import runnable_command
    return "%s --plan %s" % (runnable_command.python_script("redteam_disposition.py"),
                             shlex.quote(str(plan_dir)))


_SKIP_DIRS = ("artifacts", "reports")


def _plan_texts(plan_dir: Path) -> dict:
    """{plan-relative path: lines} of every markdown file the plan's text lives in."""
    out = {}
    for f in sorted(Path(plan_dir).rglob("*.md")):
        rel = f.relative_to(plan_dir).as_posix()
        if rel.split("/", 1)[0] in _SKIP_DIRS or not f.is_file():
            continue
        out[rel] = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return out


def _quote_of(block: list) -> "str | None":
    """The block's first non-blank line as a «quote» a table cell can carry: its pieces
    between `«`, `»`, `|`, backticks and `…`, in order, joined by ` … ` (the matcher
    reads them as fragments in order), cut to about 80 characters."""
    import plan_handoff
    first = next((plan_handoff._ws(b) for b in block if b.strip()), "")
    pieces = [x.strip() for x in re.split(r"[«»|`…]", first) if x.strip()]
    out: list = []
    for p in pieces:
        if out and len(" … ".join(out + [p])) > 80:
            break
        out.append(p[:80])
    return " … ".join(out) or None


def _old_lines(source: str, plan_dir: Path, root: Path, rel: str) -> "list | None":
    """The lines `rel` held in `source`: an old copy of the plan dir, or a git rev."""
    import subprocess
    mine = "plans/%s/" % Path(plan_dir).name
    if Path(source).is_dir():
        if rel.startswith("plans/"):
            if not rel.startswith(mine):
                return None
            rel = rel[len(mine):]
        f = Path(source) / rel
        return (f.read_text(encoding="utf-8", errors="replace").splitlines()
                if f.is_file() else None)
    import process_group
    cwd = Path(root) if rel.startswith("plans/") else Path(plan_dir)
    try:
        r = process_group.run(["git", "-C", str(cwd), "show", "%s:./%s" % (source, rel)],
                              capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.decode("utf-8", "replace").splitlines() if r.returncode == 0 else None


def _locate(block: list, texts: dict) -> list:
    """[(rel, line)] where `block` now stands, whitespace aside."""
    import plan_handoff
    want = plan_handoff._ws(" ".join(block))
    size = len(block)
    hits = [(rel, k + 1) for rel, lines in texts.items()
            for k in range(max(len(lines) - size + 1, 0))
            if plan_handoff._ws(" ".join(lines[k:k + size])) == want]
    return hits


def _expanded(plan_dir: Path, rel: str, pins: set) -> list:
    """[(text, (file, line))] of `rel` now, a pointer line read as the reference file's
    body: where each of its lines stands after a body moved out to `refs/`."""
    import plan_refs
    doc = Path(plan_dir) / rel
    try:
        lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for k, line in enumerate(lines, 1):
        target = plan_refs.pointer_target(line, doc, Path(plan_dir), pins)
        if target is None:
            out.append((line, (rel, k)))
            continue
        ref = target.relative_to(Path(plan_dir).resolve()).as_posix()
        out.extend((t, (ref, n)) for n, t in plan_refs.numbered_body(target))
    return out


def _aligned(old: list, new: list, start: int, end: int) -> list:
    """[(file, line)] where old lines start..end stand in `new` (`_expanded`), by
    aligning the two; [] unless every cited line lines up, in one file, adjacent."""
    import difflib
    sm = difflib.SequenceMatcher(None, old, [t for t, _ in new], autojunk=False)
    spot = {a + i: new[b + i][1] for a, b, n in sm.get_matching_blocks() for i in range(n)}
    if any(i not in spot for i in range(start - 1, end)):
        return []
    spots = [spot[i] for i in range(start - 1, end)]
    files = {f for f, _ in spots}
    lines = [n for _, n in spots]
    if len(files) != 1 or lines != list(range(lines[0], lines[0] + len(lines))):
        return []
    return [spots[0]]


def _table_lines(plan_md: Path, pins: set) -> dict:
    """{file: {0-based line}} of every line a disposition table is read from — the same
    lines `sections` hands `_section_rows`, pointers followed."""
    import plan_refs
    plan_dir = plan_md.parent
    lines = plan_md.read_text(encoding="utf-8", errors="replace").splitlines()
    out: dict = {}
    for i, l in enumerate(lines):
        if not _HEADING.match(l.strip()):
            continue
        body: list = []
        for k in range(i + 1, len(lines)):
            if lines[k].strip().startswith("#"):
                break
            target = plan_refs.pointer_target(lines[k], plan_md, plan_dir, pins)
            if target is None:
                body.append((lines[k], plan_md, k))
            else:
                body.extend((t, target, n - 1) for n, t in plan_refs.numbered_body(target))
        started = False
        for text, f, k in body:
            t = text.strip()
            if t.startswith("#"):
                break
            if not t.startswith("|"):
                if started and t:
                    break
                continue
            started = True
            out.setdefault(f, set()).add(k)
    return out


def rewrite_cites(plan_dir: Path, root, source: "str | None" = None,
                  dry_run: bool = False) -> dict:
    """Give every plan-local disposition cite a «quote» (`source` None), or move each of
    this plan's own cites to where the lines it named in `source` stand now and quote
    them. A cite whose lines cannot be placed exactly once is left as it is and named;
    a bare `:N` after a cite that moved is written with the file it always meant."""
    import plan_refs

    plan_dir = Path(plan_dir)
    plan_md = plan_dir / "plan.md"
    if not plan_md.is_file():
        return {"ok": False, "error": "no plan.md in %s" % plan_dir}
    pins = plan_refs.pinned(plan_dir)
    texts = _plan_texts(plan_dir)
    mine = "plans/%s/" % plan_dir.name
    olds: dict = {}  # cited file -> its lines in `source`
    news: dict = {}  # cited file -> its lines now, through pointers
    result: dict = {"ok": True, "mode": "remap" if source else "pin", "rewritten": [],
                    "unresolved": [], "foreign": [], "files": []}

    def place(rel: str, start: int, end: int, where: str) -> "tuple[str, int, list] | None":
        if rel not in olds:
            olds[rel] = _old_lines(source or "", plan_dir, Path(root), rel)
        old = olds[rel]
        if old is None or end > len(old):
            result["unresolved"].append(where + " (no such lines in %s)" % source)
            return None
        block = old[start - 1:end]
        local = rel[len(mine):] if rel.startswith(mine) else rel
        if local not in news:
            news[local] = _expanded(plan_dir, local, pins)
        hits = _aligned(old, news[local], start, end)
        if not hits and any(b.strip() for b in block):
            hits = _locate(block, texts)
        if len(hits) == 1:
            return hits[0][0], hits[0][1], block
        result["unresolved"].append(where + (
            " (the cited line is blank and its place in the new layout was not found)"
            if not any(b.strip() for b in block) else
            " (its lines are now at %d places)" % len(hits) if hits else
            " (its lines are no longer in the plan)"))
        return None

    def cell_sub(cell: str) -> str:
        last: object = None     # the file a bare `:N` here means
        written = None  # the file the cell, as rewritten so far, makes a bare `:N` mean

        def one(m):
            nonlocal last, written
            if _FOREIGN.match(m.string, m.end()) or (m.group(1) is None and last is _ELSEWHERE):
                last = _ELSEWHERE
                result["foreign"].append(m.group(0).split("«")[0].strip())
                return m.group(0)
            rel = m.group(1) or last
            if rel is None:
                return m.group(0)
            assert isinstance(rel, str)
            last = rel
            start, end = int(m.group(2)), int(m.group(3) or m.group(2))
            span = m.group(2) + ("-" + m.group(3) if m.group(3) else "")
            where = "`%s:%s`" % (rel, span)
            path, local = _resolve(plan_dir, Path(root), rel)
            keep = m.group(0)
            if m.group(1) is None and written not in (None, rel):
                keep = "`%s:%s`%s" % (rel, span, m.group(4) or "")
            if not local or (source and rel.startswith("plans/") and not rel.startswith(mine)):
                written = rel
                return keep
            if source is None:
                written = rel
                if m.group(5) is not None:
                    return keep
                lines = texts.get(rel) or (path.read_text(encoding="utf-8", errors="replace")
                                           .splitlines() if path.is_file() else [])
                quote = _quote_of(lines[start - 1:end]) if end <= len(lines) else None
                if quote is None:
                    result["unresolved"].append(where + " (no text at that line to quote)")
                    return keep
                result["rewritten"].append(where)
                return keep + " «%s»" % quote
            spot = place(rel, start, end, where)
            if spot is None:
                written = rel
                return keep
            new_rel, at, block = spot
            written = new_rel
            new_span = "%d" % at if end == start else "%d-%d" % (at, at + end - start)
            quote = _quote_of(block)
            new = "`%s:%s`" % (new_rel, new_span) + (" «%s»" % quote if quote else "")
            if new != m.group(0):
                result["rewritten"].append("%s → `%s:%s`" % (where, new_rel, new_span))
            return new

        return _CITE.sub(one, cell)

    for f, idx in _table_lines(plan_md, pins).items():
        body = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        new = ["|".join(cell_sub(c) for c in l.split("|")) if k in idx else l
               for k, l in enumerate(body)]
        if new != body:
            result["files"].append(Path(f).resolve().relative_to(plan_dir.resolve()).as_posix())
            if not dry_run:
                f.write_text("".join(new), encoding="utf-8")
    result["ok"] = not result["unresolved"]
    return result


def main(argv=None) -> int:
    import argparse
    import json
    import harness_paths
    import plan_graph
    ap = argparse.ArgumentParser(
        description="Rewrite the cites in a plan's red-team disposition tables.")
    ap.add_argument("--plan", required=True,
                    help="the plan: its dir, or its name under the project's plans/")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pin-cites", action="store_true",
                      help="follow every unquoted plan-local cite with a «quote» of the "
                           "line it names now, so a later move is caught by content")
    mode.add_argument("--remap-cites", action="store_true",
                      help="move each of this plan's cites to where the lines it named in "
                           "--from stand now, quoted; a cite placed zero or several "
                           "times is left and named")
    ap.add_argument("--from", dest="source",
                    help="with --remap-cites: a git rev, or a dir holding the plan "
                         "before the move")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args(argv)
    if args.remap_cites and not args.source:
        ap.error("--remap-cites needs --from <git rev or old plan dir>")
    plan_dir = Path(args.plan)
    if not plan_dir.is_dir():
        plan_dir = harness_paths.project_root() / "plans" / args.plan
    plan_dir = plan_dir.resolve()
    out = rewrite_cites(plan_dir, plan_graph.root_for_plan_dir(plan_dir),
                        args.source if args.remap_cites else None, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
