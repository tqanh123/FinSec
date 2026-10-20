"""`hs-code change structural` — does a diff change this tree's STRUCTURE?

Structural means one of two things, both read off the import graph:
  * a module of this tree is added or removed (a rename is both);
  * a changed file's runtime imports between this tree's modules differ
    (module-level and function-local; `mod::name` counts as `mod`; an import
    under `TYPE_CHECKING` and a third-party import are no edge of this tree —
    the same edge set `codegraph.check.tree.module_graph` reads off a snapshot).
A body-only edit is not structural, wherever the file sits. Test CODE — a
file under the declared test tree (`layout.tests_subdir`, `in_test_tree`) — is
never structural: the architecture a review judges is the product's component
map, and a new test module is no component of it. Those files come back under
`tests`, so the count is not silently smaller.

Both revisions of a file are resolved in ONE `ImportContext`, built from the
tree on disk now: an import the old text made of a module this diff deleted
does not resolve and so drops out of the comparison, and the deletion itself
is the reason reported instead.

USAGE
    hs-code change structural [--base REF] FILE...

    Old text = `git show REF:FILE` (absent at REF: the file is new); new text =
    the file on disk (absent: deleted). REF defaults to HEAD — the review's
    own diff source (`git status`), staged, unstaged and untracked alike.

WHAT IT CANNOT READ, AND SAYS SO
    A file no declared root owns, a file no adapter parses (not Python), and a
    revision that does not parse come back under `unread` with why — never as
    "not structural". The caller decides what an unread file counts as
    (`structural_change.judge` applies the declared `structural_globs`).

Exit 0 with the answer; 2 when there is no descriptor to read or REF is not a
revision.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from codegraph._core.verdict import Verdict
from hs_family.cli import Command


@dataclass(frozen=True)
class Change:
    path: str
    old: Optional[str]
    new: Optional[str]


@dataclass(frozen=True)
class Unread:
    path: str
    why: str

    def as_dict(self) -> "dict[str, str]":
        return {"path": self.path, "why": self.why}


def structural_changes(ctx: Any, changes: "Iterable[Change]",
                       is_test: "Callable[[str], bool]" = lambda _p: False,
                       ) -> "tuple[list[str], list[Unread], list[str]]":
    """(reasons, unread, tests) for `changes`, every revision resolved in `ctx`."""
    from codegraph._core.snapshot.pipeline import module_imports

    reasons: "list[str]" = []
    unread: "list[Unread]" = []
    tests: "list[str]" = []
    for c in changes:
        if c.old is None and c.new is None:
            continue
        if is_test(c.path):
            tests.append(c.path)
            continue
        module = ctx.module_at(c.path)
        if module is None or not c.path.endswith(".py"):
            unread.append(Unread(c.path, "no declared root owns it" if c.path.endswith(".py")
                                 else "not a module the import graph reads"))
            continue
        if c.old is None:
            reasons.append("adds module %s (%s)" % (module, c.path))
            continue
        if c.new is None:
            reasons.append("removes module %s (%s)" % (module, c.path))
            continue
        try:
            before = module_imports(ctx, c.path, c.old, module)
            after = module_imports(ctx, c.path, c.new, module)
        except SyntaxError as e:
            unread.append(Unread(c.path, "does not parse: %s" % e.msg))
            continue
        if before is None or after is None:
            unread.append(Unread(c.path, "not a module the import graph reads"))
            continue
        if before != after:
            delta = ["+" + m for m in sorted(after - before)] + ["−" + m for m in sorted(before - after)]
            reasons.append("%s imports %s (%s)" % (module, " ".join(delta), c.path))
    return reasons, unread, tests


def _git_text(repo: Path, ref: str, path: str) -> Optional[str]:
    from codegraph._core.git import GitError, run_git
    try:
        return run_git(repo, "show", "%s:%s" % (ref, path)).decode("utf-8", "replace")
    except GitError:
        return None                                   # absent at REF: the file is new


def worktree_changes(repo: Path, base: str, files: "Iterable[str]") -> "list[Change]":
    out = []
    for rel in files:
        p = repo / rel
        out.append(Change(rel, _git_text(repo, base, rel),
                          p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None))
    return out


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--base", default="HEAD", metavar="REF")
    sp.add_argument("files", nargs="+", metavar="FILE")


def run(args: argparse.Namespace, repo: Path, opts: Any) -> Verdict:
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph._core.snapshot.pipeline import import_context
    from codegraph._core.testfiles import build_test_layout

    try:
        descriptor = load(repo / "codemap.yaml", repo).descriptor
    except (OSError, DescriptorError) as e:
        return Verdict.broken("structural: %s" % e)
    from codegraph._core.git import GitError, run_git
    try:
        run_git(repo, "rev-parse", "--verify", "--quiet", "%s^{commit}" % args.base)
    except GitError:
        return Verdict.broken("structural: %r is not a revision of %s" % (args.base, repo))
    ctx = import_context(repo, descriptor)
    reasons, unread, tests = structural_changes(ctx, worktree_changes(repo, args.base, args.files),
                                                build_test_layout(repo, descriptor).in_test_tree)
    data = {"base": args.base, "files": len(args.files), "reasons": reasons,
            "unread": [u.as_dict() for u in unread], "tests": tests}
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("structural: %d reason(s), %d unread, %d test file(s), %d file(s) vs %s"
              % (len(reasons), len(unread), len(tests), len(args.files), args.base))
        for r in reasons:
            print("  " + r)
        for u in unread:
            print("  unread %s — %s" % (u.path, u.why))
    return Verdict.ok(data)


def _structural(args: argparse.Namespace) -> int:
    return run(args, args.ctx.root, args.opts).exit


STRUCTURAL = Command(add_args=add_arguments, run=_structural)
