"""port_drift_merge.py — carry an upstream file's drift into the target file(s)
that already hold it, by three-way merge. A FLAT module under `harness/scripts/`.

Ported from `ob-tools@60075cb:packages/portkit/src/portkit/drift/{merge,twins,
targets}.py`:

    upstream@from --rewrite--> OLD
    upstream@to   --rewrite--> NEW     git merge-file  TWIN OLD NEW  -> merged
    TWIN (the target file)             conflicts: red (nothing written) or
                                       reject (the twin's side kept, upstream's to `<twin>.rej`)

A file the port SPLIT over several twins goes hunk by hunk (`hunks`/`apply_all`:
each hunk lands in the ONE twin holding its exact context, `fuzz` context lines
dropped at most; none/several is a reject, never a guess), or symbol by symbol
(`virtual_text`: only the changed top-level blocks, merged into the module the
ledger sends that symbol to).

WHERE A FILE LIVES comes from the ledger (`port_refs.Index`), never from a
definition lookup as upstream `targets.derive` did: a name defined in two target
files is ambiguous to a lookup, while the ledger row that ported it names one.
"""
from __future__ import annotations

import ast
import difflib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_DEF = re.compile(r"^(?P<ind>[ \t]*)(?:async\s+def|def|class)\s+(?P<name>\w+)")


# ── symbol blocks (targets.py) ───────────────────────────────────────────────

def top_level_names(text: str) -> "list[str]":
    """Top-level def/class/assign names of one module, in file order."""
    out: "list[str]" = []
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            out += [t.id for x in tg for t in ast.walk(x) if isinstance(t, ast.Name)]
    return out


def virtual_text(text: str, names: "set[str]") -> str:
    """The module reduced to its imports + the top-level blocks named in `names`
    (a comment glued above a block travels with it). Merging OLD/NEW virtual texts
    into a target file lands only those blocks' delta."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    out: "list[str]" = []
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            keep = True
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            keep = n.name in names
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            keep = any(t.id in names for x in tg for t in ast.walk(x) if isinstance(t, ast.Name))
        else:
            keep = False
        if not keep:
            continue
        start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        out.append("".join(lines[start - 1:n.end_lineno]).rstrip("\n") + "\n")
    return "\n".join(out)


def changed_names(old: str, new: str) -> "list[str]":
    """Top-level names whose block differs between two revisions (added/removed/edited)."""
    def blocks(t: str) -> "dict[str, str]":
        return {nm: virtual_text(t, {nm}) for nm in top_level_names(t)}
    o, n = blocks(old), blocks(new)
    return [nm for nm in dict.fromkeys([*o, *n]) if o.get(nm) != n.get(nm)]


# ── three-way (merge.py) ─────────────────────────────────────────────────────

def conflict_regions(merged: str) -> "list[dict]":
    """Each `<<<<<<<` .. `>>>>>>>` block of merge-file output, 1-based lines."""
    out: "list[dict]" = []
    start = mid = sep = None
    for i, ln in enumerate(merged.splitlines(), 1):
        if ln.startswith("<<<<<<< "):
            start, mid, sep = i, None, None
        elif ln.startswith("||||||| ") and start:
            mid = i
        elif ln.startswith("=======") and start:
            sep = i
        elif ln.startswith(">>>>>>> ") and start and sep:
            out.append({"start": start, "end": i, "port_lines": (mid or sep) - start - 1,
                        "upstream_lines": i - sep - 1})
            start = None
    return out


def regions_text(marked: str) -> str:
    lines = marked.splitlines(keepends=True)
    return "".join("".join(lines[g["start"] - 1:g["end"]]) + "\n" for g in conflict_regions(marked))


def _merge_file(ours: str, base: str, theirs: str, *extra: str) -> "tuple[int, str, str]":
    with tempfile.TemporaryDirectory() as td:
        a, o, n = Path(td, "port"), Path(td, "old"), Path(td, "new")
        a.write_text(ours, encoding="utf-8")
        o.write_text(base, encoding="utf-8")
        n.write_text(theirs, encoding="utf-8")
        r = subprocess.run(["git", "merge-file", "-p", *extra, "-L", "port", "-L", "upstream-old",
                            "-L", "upstream-new", str(a), str(o), str(n)],
                           capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


@dataclass
class Merged:
    """One target file's outcome. `text` is what `--apply` writes (the twin's own
    side of each conflict under reject); `rej` is what goes to `<twin>.rej`."""
    file: str
    port: str
    text: str
    changed: bool
    conflicts: int = 0
    regions: "list[dict]" = field(default_factory=list)
    rej: str = ""
    problems: "list[str]" = field(default_factory=list)
    symbols: "list[str]" = field(default_factory=list)
    hunks_placed: "list[dict]" = field(default_factory=list)
    hunks_already: "list[str]" = field(default_factory=list)
    hunks_duplicate: "list[dict]" = field(default_factory=list)
    rejects: int = 0

    def report(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k not in ("text", "rej")}
        out["owes_rej"] = bool(self.rej)
        return out


def three_way(rel: str, port: Path, port_rel: str, old: str, new: str) -> Merged:
    current = port.read_text(encoding="utf-8")
    rc, out, err = _merge_file(current, old, new)
    m = Merged(file=rel, port=port_rel, text=out, changed=out != current)
    if rc < 0 or rc > 127:
        m.problems.append("git merge-file failed: %s" % err[:200])
        return m
    if rc:
        m.conflicts, m.regions = rc, conflict_regions(out)
        m.rej = regions_text(out)
        m.text = _merge_file(current, old, new, "--ours")[1]
        m.changed = m.text != current
    return m


# ── several twins, hunk by hunk (twins.py) ───────────────────────────────────

@dataclass
class Hunk:
    before: "list[str]"
    after: "list[str]"
    start: int
    lead: int = 0
    tail: int = 0

    @property
    def title(self) -> str:
        first = next((ln for ln in self.after + self.before if ln.strip()), "").strip()
        return "@@ old:%d @@ %s" % (self.start, first[:60])


def hunks(old: str, new: str, context: int = 3) -> "list[Hunk]":
    """One hunk per contiguous change, each with up to `context` equal lines
    around it; two nearby changes stay two hunks (a split file may hold them in
    different twins, and one fused hunk would fit none)."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out: "list[Hunk]" = []
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        lead = min(context, ops[k - 1][2] - ops[k - 1][1]) if k and ops[k - 1][0] == "equal" else 0
        tail = (min(context, ops[k + 1][2] - ops[k + 1][1])
                if k + 1 < len(ops) and ops[k + 1][0] == "equal" else 0)
        out.append(Hunk(a[i1 - lead:i2 + tail], b[j1 - lead:j2 + tail], i1 - lead + 1, lead, tail))
    return out


def _find(lines: "list[str]", block: "list[str]") -> "list[int]":
    if not block:
        return []
    n, first = len(block), block[0]
    return [i for i in range(len(lines) - n + 1) if lines[i] == first and lines[i:i + n] == block]


def _trim(h: Hunk, k: int) -> "tuple[list[str], list[str]] | None":
    dl, dt = min(k, h.lead), min(k, h.tail)
    before = h.before[dl:len(h.before) - dt]
    after = h.after[dl:len(h.after) - dt]
    return (before, after) if before else None


def added_symbols(h: Hunk) -> "list[tuple[str, str]]":
    """(indent, name) of every def/class a PURE-INSERTION hunk adds."""
    if len(h.before) != h.lead + h.tail:
        return []
    body = h.after[h.lead:len(h.after) - h.tail]
    return [(m["ind"], m["name"]) for ln in body if (m := _DEF.match(ln))]


def defined_in(texts: "dict[str, list[str]]", ind: str, name: str) -> "list[str]":
    pat = re.compile(r"^%s(?:async\s+def|def|class)\s+%s\b" % (re.escape(ind), re.escape(name)))
    return sorted(t for t, lines in texts.items() if any(pat.match(ln) for ln in lines))


def place(h: Hunk, texts: "dict[str, list[str]]", fuzz: int = 0):
    """-> ('placed', (twin, at, k, before, after)) | ('already', None) |
    ('ambiguous', [twin, ...]) | ('unplaced', None)."""
    for k in range(fuzz + 1):
        t = _trim(h, k)
        if t is None:
            break
        before, after = t
        hits = [(name, i) for name, lines in texts.items() for i in _find(lines, before)]
        if len(hits) == 1:
            name, i = hits[0]
            return "placed", (name, i, k, before, after)
        if hits:
            return "ambiguous", sorted({n for n, _ in hits})
        if after != before and any(_find(lines, after) for lines in texts.values()):
            return "already", None
    return "unplaced", None


def apply_all(hs: "list[Hunk]", texts: "dict[str, str]", fuzz: int = 0) -> dict:
    """Place every hunk against the CURRENT text of the twins (re-searched after
    each landing, so two hunks in one twin never fight over offsets)."""
    cur = {n: t.splitlines(keepends=True) for n, t in texts.items()}
    placed: "list[dict]" = []
    already: "list[str]" = []
    duplicate: "list[dict]" = []
    unplaced: "list[Hunk]" = []
    ambiguous: "list[tuple[Hunk, list[str]]]" = []
    for h in hs:
        kind, res = place(h, cur, fuzz)
        if kind in ("placed", "unplaced"):
            syms = added_symbols(h)
            have = {n: defined_in(cur, i, n) for i, n in syms}
            if syms and all(have.values()):
                duplicate.append({"hunk": h.title, "symbols": sorted(have),
                                  "twins": sorted({t for v in have.values() for t in v})})
                continue
            if any(have.values()):
                unplaced.append(h)
                continue
        if kind == "placed":
            name, at, k, before, after = res
            cur[name][at:at + len(before)] = after
            placed.append({"hunk": h.title, "twin": name, "line": at + 1, "fuzz": k})
        elif kind == "already":
            already.append(h.title)
        elif kind == "ambiguous":
            ambiguous.append((h, res))
        else:
            unplaced.append(h)
    return {"placed": placed, "already": already, "duplicate": duplicate, "unplaced": unplaced,
            "ambiguous": ambiguous, "text": {n: "".join(ls) for n, ls in cur.items()}}


def reject_text(rel: str, unplaced: "list[Hunk]", ambiguous: "list[tuple[Hunk, list[str]]]") -> str:
    """Patch-shaped `.rej` body for the hunks no twin took."""
    def body(h: Hunk) -> "list[str]":
        return ([("-%s" % ln) if ln.endswith("\n") else "-%s\n" % ln for ln in h.before]
                + [("+%s" % ln) if ln.endswith("\n") else "+%s\n" % ln for ln in h.after])
    out = ["--- upstream %s (hunks no twin took)\n" % rel]
    for h in unplaced:
        out += ["%s   [no twin holds this context]\n" % h.title] + body(h)
    for h, names in ambiguous:
        out += ["%s   [ambiguous: %s]\n" % (h.title, ", ".join(names))] + body(h)
    return "".join(out)


def merge_twins(rel: str, twins: "dict[str, Path]", old: str, new: str, fuzz: int) -> "list[Merged]":
    """One upstream file into the several twins it was split over, hunk by hunk."""
    texts = {r: p.read_text(encoding="utf-8") for r, p in twins.items()}
    res = apply_all(hunks(old, new), texts, fuzz)
    rej = (reject_text(rel, res["unplaced"], res["ambiguous"])
           if res["unplaced"] or res["ambiguous"] else "")
    out: "list[Merged]" = []
    for n, (r, _p) in enumerate(twins.items()):
        m = Merged(file=rel, port=r, text=res["text"][r], changed=res["text"][r] != texts[r],
                   hunks_placed=[h for h in res["placed"] if h["twin"] == r])
        if n == 0:
            m.hunks_already, m.hunks_duplicate = res["already"], res["duplicate"]
            m.rejects = len(res["unplaced"]) + len(res["ambiguous"])
            m.rej = rej
        out.append(m)
    return out
