#!/usr/bin/env python3
"""Hand-off anchors: a plan telling another plan's reader to edit line `:N`.

The shape read is the one plans already write, in any prose language:

    **EDIT** in `plans/<plan>/phases/phase-9.md`:
    - `:80` holds «quoted text … elided tail» ⇒ REPLACE "..."
    - `:329-333` holds «first line» … «last line» ⇒ ...

The target is the last backticked `plans/...` path on a non-item line since the
last heading. An item is a `- `:N`` or `- `:N-M`` bullet plus its indented
continuation lines; its quotes are every «…» before the first `⇒` (what comes
after is the replacement). `…` inside a quote elides. An item with no quote
(`- After `:250` add ...`) names a place, not content, and is not checked. An
`applied: <note>` line under a target marks that target's anchors as pasted: they
are no longer checked, while the target's approval still is.

`check` answers per anchor: the quoted fragments sit in order inside lines N..M
(match), inside a same-sized window elsewhere (moved), or nowhere (not found).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_ITEM = re.compile(r"^(\s*)-\s+`:(\d+)(?:-(\d+))?`(.*)$")
_TARGET = re.compile(r"`(plans/[^`\s]+)`")
_QUOTE = re.compile(r"«(.*?)»", re.DOTALL)
_ARROW = "⇒"
_APPLIED = re.compile(r"^\s*applied:\s*\S", re.IGNORECASE)
# The whole marker line, for plan_approval: bookkeeping written after approval, so
# it sits outside the approval hash the way the frontmatter does.
APPLIED_LINE = re.compile(r"^[ \t]*applied:[ \t]*\S[^\n]*\n?", re.IGNORECASE | re.MULTILINE)
_ELIDE = "…"


@dataclass
class Anchor:
    source: str
    line: int
    target: str
    start: int
    end: int
    fragments: list = field(default_factory=list)
    applied: bool = False
    block: int = 0  # line of the target line that opened this item's block


def _ws(text: str) -> str:
    return " ".join(text.split())


def _head(item_text: str) -> str:
    """The item up to its first `⇒` that sits outside a «quote»."""
    depth = 0
    for k, ch in enumerate(item_text):
        if ch == "«":
            depth += 1
        elif ch == "»":
            depth = max(depth - 1, 0)
        elif ch == _ARROW and depth == 0:
            return item_text[:k]
    return item_text


def _fragments(item_text: str) -> list:
    head = _head(item_text)
    out: list = []
    for q in _QUOTE.findall(head):
        out.extend(f for f in (_ws(p) for p in q.split(_ELIDE)) if f)
    return out


@dataclass
class Unchecked:
    source: str
    line: int
    reason: str  # "no-target" | "no-quote"


def scan_text(text: str, source: str) -> tuple:
    """(anchors, unchecked): every `- `:N`` item is one or the other — an item with
    no `plans/...` target line above it in its section, or with no «quote» before
    the arrow, cannot be checked and is said so rather than dropped."""
    lines = text.splitlines()
    out: list = []
    skipped: list = []
    target = None
    block = 0
    applied = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("#"):
            target, applied = None, False
            i += 1
            continue
        m = _ITEM.match(line)
        if not m:
            if _APPLIED.match(line):
                applied = True
            found = _TARGET.findall(line)
            if found and not line.lstrip().startswith("-"):
                target, block, applied = found[-1], i + 1, False
            i += 1
            continue
        indent = len(m.group(1))
        body = [m.group(4)]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip() or nxt.lstrip().startswith(("#", "```")):
                break
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
            j += 1
        frags = _fragments("\n".join(body))
        if not target:
            if frags:  # quoted like an anchor, but its target line was not recognised
                skipped.append(Unchecked(source, i + 1, "no-target"))
        elif not frags:
            if not applied:
                skipped.append(Unchecked(source, i + 1, "no-quote"))
        else:
            start = int(m.group(2))
            out.append(Anchor(source=source, line=i + 1, target=target, start=start,
                              end=int(m.group(3) or start), fragments=frags, applied=applied,
                              block=block))
        i = j
    return out, skipped


def parse_text(text: str, source: str) -> list:
    return scan_text(text, source)[0]


def scan(plan_dir) -> tuple:
    plan_dir = Path(plan_dir)
    docs = [plan_dir / "plan.md", *sorted((plan_dir / "phases").glob("*.md"))]
    out: list = []
    skipped: list = []
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        a, u = scan_text(text, doc.relative_to(plan_dir).as_posix())
        out.extend(a)
        skipped.extend(u)
    return out, skipped


def parse(plan_dir) -> list:
    return scan(plan_dir)[0]


def _holds(window: list, fragments: list) -> bool:
    text = _ws(" ".join(window))
    pos = 0
    for f in fragments:
        k = text.find(f, pos)
        if k < 0:
            return False
        pos = k + len(f)
    return True


def check(anchor: Anchor, root) -> dict:
    """{status: match|moved|not_found|no_target, at: [line, ...]}."""
    path = Path(root) / anchor.target
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"status": "no_target", "at": []}
    size = anchor.end - anchor.start + 1
    if _holds(lines[anchor.start - 1:anchor.end], anchor.fragments):
        return {"status": "match", "at": [anchor.start]}
    at = [k + 1 for k in range(0, max(len(lines) - size + 1, 0))
          if _holds(lines[k:k + size], anchor.fragments)]
    return {"status": "moved", "at": at} if at else {"status": "not_found", "at": []}


def plan_of(target: str, root) -> "Path | None":
    """The nearest dir holding plan.md above `target`, stopping at plans/ — a plan
    may sit under a group dir (`plans/<group>/<plan>/...`)."""
    plans = (Path(root) / "plans").resolve()
    d = (Path(root) / target).parent
    while True:
        r = d.resolve()
        if r == plans or plans not in r.parents:
            return None
        if (d / "plan.md").is_file():
            return d
        d = d.parent


def target_plans(anchors: list, root, own) -> list:
    """Plan dirs the anchors point into, other than `own`, in first-seen order."""
    out: list = []
    own = Path(own).resolve()
    for a in anchors:
        d = plan_of(a.target, root)
        if d is not None and d.resolve() != own and d not in out:
            out.append(d)
    return out


def _span(a: Anchor) -> str:
    return ":%d" % a.start if a.start == a.end else ":%d-%d" % (a.start, a.end)


# A callable a block quotes: an identifier written as a call or a definition (`name(`)
# inside a quote's code span. That is what a signature change touches; a type or a
# constant quoted for context is not — listing those buried the one call site that
# mattered under 150 lines of vocabulary on a real plan.
_CODE_SPAN = re.compile(r"`([^`]+)`")
_CALLED = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SYMBOL_SHAPE = re.compile(r"[a-z0-9]_[a-z0-9]|[a-z][A-Z]|^_[A-Za-z]")


def _quoted_callables(fragment: str) -> set:
    out: set = set()
    for m in _CODE_SPAN.finditer(fragment):
        span = m.group(1)
        if fragment[m.end():m.end() + 1] == "(":  # `name`(args)
            span += "("
        out.update(c for c in _CALLED.findall(span) if _SYMBOL_SHAPE.search(c))
    return out


# Named on more lines than this across the target plan, a symbol is vocabulary, not an
# edit site: listing it would bury the one call site that matters.
SYMBOL_LINES_MAX = 15
# One warning line per symbol, this many per target plan; the rest are named on one.
SYMBOL_LINES_SHOWN = 8


def _plan_docs(plan: Path) -> list:
    return [plan / "plan.md", *sorted((plan / "phases").glob("*.md"))]


def unanchored_mentions(anchors: list, root) -> tuple:
    """({plan dir: {symbol: ["file:line", ...]}}, too_common) — lines of a target
    plan naming a symbol its hand-off blocks quote, on no anchored line. A block
    that changes a signature reaches every place the plan names it, not only the
    lines it quoted."""
    by_plan: dict = {}
    for a in anchors:
        if a.applied:
            continue
        plan = plan_of(a.target, root)
        if plan is None:
            continue
        syms, covered = by_plan.setdefault(plan, (set(), set()))
        for f in a.fragments:
            syms.update(_quoted_callables(f))
        for n in range(a.start, a.end + 1):
            covered.add(((Path(root) / a.target).resolve(), n))
    out: dict = {}
    too_common = 0
    for plan, (syms, covered) in by_plan.items():
        hits: dict = {s: [] for s in sorted(syms, key=str.lower)}
        pats = {s: re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(s)) for s in hits}
        for doc in _plan_docs(plan):
            try:
                lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = doc.relative_to(plan).as_posix()
            for n, line in enumerate(lines, 1):
                for s, pat in pats.items():
                    if pat.search(line):
                        hits[s].append((rel, n, (doc.resolve(), n) in covered))
        for s, found in hits.items():
            if len(found) > SYMBOL_LINES_MAX:
                too_common += 1
                continue
            loose = ["%s:%d" % (rel, n) for rel, n, cov in found if not cov]
            if loose:
                out.setdefault(plan, {})[s] = loose
    return out, too_common


def report(plan_dir, root, project_state) -> dict:
    """{"counts": {...}, "warnings": [...]} for `plan next`. The counts are always
    there, zeros included, so an empty warning list reads as "N checked, all held"
    or "nothing was checked" — never ambiguously. `project_state` maps a plan dir
    to the state its `plan next` would report."""
    anchors, skipped = scan(plan_dir)
    counts = {"checked": 0, "matched": 0, "moved": 0, "not_found": 0, "no_target_file": 0,
              "applied": 0, "unchecked": len(skipped),
              "blocks": len({(a.source, a.block) for a in anchors}),
              "targets": len({a.target for a in anchors})}
    mentions, counts["symbols_too_common"] = unanchored_mentions(anchors, root)
    counts["unanchored_mentions"] = sum(len(v) for m in mentions.values() for v in m.values())
    out = []
    for a in anchors:
        if a.applied:
            counts["applied"] += 1
            continue
        r = check(a, root)
        counts["checked"] += 1
        counts[{"match": "matched", "no_target": "no_target_file"}.get(r["status"], r["status"])] += 1
        if r["status"] == "match":
            continue
        where = "%s:%d `%s` in %s" % (a.source, a.line, _span(a), a.target)
        if r["status"] == "moved":
            out.append("hand-off anchor %s: the quoted text is now at %s — the line "
                       "number drifted" % (where, ", ".join(":%d" % n for n in r["at"][:3])))
        elif r["status"] == "not_found":
            out.append("hand-off anchor %s: the quoted text is not found in the target — "
                       "it was already edited or the quote is wrong" % where)
        else:
            out.append("hand-off anchor %s: the target file does not exist" % where)
    for reason, why in (("no-target", "no `plans/...` target line above it in its section"),
                        ("no-quote", "no «quote» of the line before ⇒")):
        hit = [u for u in skipped if u.reason == reason]
        if hit:
            out.append("hand-off items not checked (%d) — %s: %s" % (
                len(hit), why, ", ".join("%s:%d" % (u.source, u.line) for u in hit[:5])))
    for plan, syms in mentions.items():
        items = list(syms.items())
        for s, locs in items[:SYMBOL_LINES_SHOWN]:
            out.append("hand-off target %s: `%s` also named at %s — no anchor covers it; a "
                       "changed name or signature reaches it too" % (
                           plan.name, s, ", ".join(locs[:5]) + (" …" if len(locs) > 5 else "")))
        if len(items) > SYMBOL_LINES_SHOWN:
            out.append("hand-off target %s: %d more quoted callable(s) also named on lines no "
                       "anchor covers: %s" % (plan.name, len(items) - SYMBOL_LINES_SHOWN,
                                               ", ".join("`%s`" % s for s, _ in items[SYMBOL_LINES_SHOWN:])))
    for t in dict.fromkeys(a.target for a in anchors):
        if plan_of(t, root) is None and (Path(root) / t).is_file():
            out.append("hand-off target %s sits in no plan (no plan.md above it under "
                       "plans/) — whether an edit there drifted an approval is not checked" % t)
    for d in target_plans(anchors, root, plan_dir):
        state = project_state(d)
        if state in ("approval_drifted", "approval_invalid", "state_conflict"):
            out.append("hand-off target %s is %s — an edit landed after its approval; it "
                       "needs re-review before cook: hs-run plan next --plan %s"
                       % (d.name, state, d))
    return {"counts": counts, "warnings": out}


def warnings(plan_dir, root, project_state) -> list:
    return report(plan_dir, root, project_state)["warnings"]
