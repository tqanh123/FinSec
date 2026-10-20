#!/usr/bin/env python3
"""plan_evidence_paths.py — a path cited as EVIDENCE must resolve to a real file.

Measured on this repo: a plan's Validation Log carried an `[OBSERVED]` row
whose source column named `artifacts/cli-owners.json`. That file had never
existed under any name. The row read as a measurement anyone could go and
re-derive, and nobody could, because the pointer went nowhere. Nothing caught
it — the plan parsed, the gates were green, and the number in the row was
right, which is what made the dead pointer invisible.

SCOPE, measured rather than guessed. Over every markdown under `plans/`, 40
backticked repo paths are cited and 6 do not resolve: a file deleted on purpose
and named in the record of its own deletion, three paths written relative to
some other base, and one runtime state file that never enters git. All six are
correct prose. A checker reading every cited path would be wrong six times out
of six on its first run, which is how a check gets switched off instead of
fixed.

On an EVIDENCE-marked line the same dangling pointer is never prose. The line
says the reader can go and check; that is the whole claim, and a pointer to
nothing makes it false. So that is the only line this reads.

Read-only. Never edits a plan, an artifact, or any gate config.

WHAT IT CANNOT SEE
    * Whether the file it found says what the row claims. Presence is not
      agreement — this closes the "pointer goes nowhere" hole and no other.
    * A path built at runtime, spelled without backticks, or written relative
      to a base other than the repo root, the plan dir or a sibling plan's dir.
    * A row that cites no path at all. Most do not: an evidence row naming a
      command rather than a file is untouched, and stays unproven by this.
    * A missing row. A claim nobody wrote down is not a dangling pointer, and
      nothing here asks whether the Validation Log is complete.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOOK_CLASS = None  # a library + CLI, not a dispatched hook

#: The markers that turn a line into a claim the reader can check. Spelled as a
#: set rather than a single pattern so adding one is a one-line edit that the
#: marker test immediately covers.
EVIDENCE_MARKERS = ("OBSERVED", "MEASURED", "VERIFIED")

_MARK = re.compile(r"\[(?:%s)\]" % "|".join(EVIDENCE_MARKERS))

#: A backticked token shaped like a repo path: at least one `/`, and a short
#: extension. The extension is what keeps a bare directory (`plans/`) and a
#: dotted module name out; the `/` is what keeps a bare filename out, since a
#: filename with no directory cannot be resolved against anything.
#: A leading `../` run is a path relative to the plan dir (another plan's file).
_PATH = re.compile(
    r"`((?:\.\./)*[A-Za-z0-9_][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,6})`")

#: A citation carrying any of these is a shape, not a path — a glob, an
#: ellipsis, or a placeholder. Reading one as a filename produces a finding
#: nobody can act on.
_NOT_A_PATH = re.compile(r"[*?{}<>]|\.\.\.")


def _repo_of(plan_dir: Path) -> Path:
    """The repo root a `plans/<plan>` dir sits in.

    Walks UP to the parent of `plans/` rather than assuming a depth, because a
    plan dir is nested one level under `plans/` here and the wave layout nests
    its phase dirs one deeper. Falls back to the plan's own parent when there
    is no `plans/` on the path at all, which keeps a caller passing an
    arbitrary directory from silently resolving against `/`.
    """
    for p in plan_dir.resolve().parents:
        if p.name == "plans":
            return p.parent
    return plan_dir.resolve().parent


def _resolves(root: Path, plan_dir: Path, cited: str) -> bool:
    """True when `cited` names a file from the repo root or the plan dir, and that
    file is inside the repo: a `../` run is followed, never out of it."""
    top = root.resolve()
    for base in (root, plan_dir):
        target = (base / cited).resolve()
        if (target == top or top in target.parents) and target.exists():
            return True
    return False


def _siblings_holding(root: Path, plan_dir: Path, cited: str) -> list:
    """The other plans under `plans/` that hold `cited` relative to their own dir."""
    if cited.startswith("../"):
        return []
    plans = root / "plans"
    try:
        dirs = sorted(d for d in plans.iterdir() if d.is_dir())
    except OSError:
        return []
    own = plan_dir.resolve()
    return [d.name for d in dirs if d.resolve() != own and (d / cited).is_file()]


def _scan(plan_dir, repo=None) -> "tuple[list, list]":
    """(dangling, sibling): findings for evidence-cited paths that resolve nowhere,
    and warnings for those that resolve only in a sibling plan's dir."""
    plan_dir = Path(plan_dir)
    if not plan_dir.is_dir():
        return [], []
    root = Path(repo) if repo is not None else _repo_of(plan_dir)
    dangling: list = []
    sibling: list = []
    try:
        docs = sorted(plan_dir.rglob("*.md"))
    except OSError:
        return [], []
    for md in docs:
        try:
            lines = md.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if not _MARK.search(line):
                continue
            for cited in _PATH.findall(line):
                if _NOT_A_PATH.search(cited) or _resolves(root, plan_dir, cited):
                    continue
                try:
                    where = md.relative_to(plan_dir)
                except ValueError:
                    where = md
                holders = _siblings_holding(root, plan_dir, cited)
                if holders:
                    sibling.append(
                        "%s:%d cites `%s` as evidence; it is not in this plan but in "
                        "sibling plan(s) %s — cite it as %s so a reader lands on it"
                        % (where, n, cited, ", ".join(holders),
                           " or ".join("`../%s/%s`" % (h, cited) for h in holders)))
                    continue
                dangling.append(
                    "%s:%d cites `%s` as evidence, but no such file exists "
                    "(tried %s and the plan dir) — write the artifact, or say "
                    "in words what was measured" % (where, n, cited, root))
    return dangling, sibling


def dangling_evidence_paths(plan_dir, repo=None) -> list:
    """Findings, one per evidence-cited path that resolves to no file.

    A path is tried against the repo root first and the plan dir second — both,
    because plans cite both ways and a citation that resolves either way is a
    pointer a reader can follow; a `../` citation is followed from the plan dir and
    must stay inside the repo. A path that only a sibling plan holds is not here:
    see `sibling_evidence_paths`. Never raises: an unreadable plan dir yields no
    finding, since this is a check on what a plan SAYS and an absent plan says
    nothing.
    """
    return _scan(plan_dir, repo)[0]


def sibling_evidence_paths(plan_dir, repo=None) -> list:
    """Warnings, one per evidence-cited path found only in a sibling plan's dir —
    a pointer a reader can follow once told which plan, so not a false claim."""
    return _scan(plan_dir, repo)[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="a path cited as evidence in a plan must resolve to a real file")
    ap.add_argument("plan", help="the plan directory to read")
    ap.add_argument("--repo", default=None,
                    help="repo root cited paths resolve against (default: the "
                         "parent of plans/ above the plan dir)")
    args = ap.parse_args(argv)
    findings, sibling = _scan(args.plan, repo=args.repo)
    for f in findings:
        print("  " + f)
    for w in sibling:
        print("  warning: " + w)
    print("plan-evidence-paths: %d dangling citation(s), %d in a sibling plan"
          % (len(findings), len(sibling)))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
