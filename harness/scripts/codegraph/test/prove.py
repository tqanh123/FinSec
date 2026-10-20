"""hs-code test prove -- mutation-proof a gate ON DEMAND: apply ONE declared
mutation to a COPY of the tree, run the tree's own DECLARED
`domains.test.command` against declared test target(s), assert they redden,
then discard the copy.

Port of ob-tools' `testkit.analysis.prove` (pinned commit `60075cb`), rewired
onto this harness's own `codegraph.test.copy_runner` / `codegraph.test.
declared_command` instead of a bare `sys.executable -m pytest` and a guessed
PYTHONPATH -- the upstream tool's own guess (a single-package `<repo>/src` or
a uv workspace's `<repo>/packages/*/src`) is WRONG on a flat tree: the test
could then import the ORIGINAL tree instead of the copy, and a real hole
would read as "caught" with no sign anything was off. `--pythonpath` is
dropped entirely here -- the DECLARED command replaces it.

DISTINCT FROM `codegraph.test.mutation`
    That probe AUTO-SELECTS mutation sites and reports a survivor RATE -- a
    measurement, never a gate. This verb takes a DECLARED mutation (file,
    old string, new string) and DECLARED test(s), and answers one yes/no
    question: does THIS change turn THOSE tests red.

USAGE
    hs-code test prove --file src/x.py --old "a < b" --new "a <= b" \\
        --test tests/test_x.py::test_boundary
    hs-code test prove --spec prove.yaml    # batch: a YAML list of
                                             # {file, old, new, tests}

NO-GIT LIMIT
    The copy this verb runs in carries no `.git` (see `codegraph.test.
    copy_runner`) -- a named `--test` that needs git goes red at the
    BASELINE, which reads as exit 2 "baseline already red", never a false
    "caught".

WHAT IT CANNOT SEE
    * `old` must occur EXACTLY once in the file; an ambiguous string is
      refused (exit 2) rather than mutating "the first match" silently.
    * The baseline (unmutated copy) must pass the named test(s) first, or a
      "caught" verdict after mutating cannot be told apart from a
      pre-existing failure -- that case is exit 2, not a verdict either way.
    * A mutated run that times out or cannot start is exit 2, never
      "caught": the named tests did not go red, the run did not finish.
      Mutating the declared runner itself (`run_local_tests.py` here) is
      the usual way to get there -- the proof then runs a different command.
    * One mutation per spec entry: a caller wanting several distinct edits
      proved together writes two spec entries, one per edit.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import hs_family
import yaml_io

from codegraph.test.copy_runner import (changed_tree_error, copied_tree, run_declared,
                                        tree_changes, tree_digests)
from codegraph.test.declared_command import DeclaredCommandError
from hs_family.cli import Command

DEFAULT_TIMEOUT = 300


@dataclass(frozen=True)
class ProveSpec:
    file: str
    old: str
    new: str
    tests: "tuple[str, ...]"


@dataclass(frozen=True)
class ProveOutcome:
    spec: ProveSpec
    status: str            # caught | not-caught | error
    detail: str = ""


def prove_one(repo: Path, spec: ProveSpec, *, timeout: int = DEFAULT_TIMEOUT) -> ProveOutcome:
    """Copy `repo`, apply `spec`'s mutation, run `spec.tests` through the
    copy's own declared test command, report whether they reddened. The real
    tree is hashed before and after (in a `finally`, so every return path is
    covered); a difference raises -- the one failure this tool must never
    paper over."""
    before = tree_digests(repo)
    try:
        with copied_tree(repo) as work:
            target = work / spec.file
            if not target.is_file():
                return ProveOutcome(spec, "error",
                                    f"no such file in the copy: {spec.file}")
            original = target.read_text(encoding="utf-8")
            count = original.count(spec.old)
            if count == 0:
                return ProveOutcome(
                    spec, "error",
                    f"old string not found in {spec.file} (0 occurrences)")
            if count > 1:
                return ProveOutcome(
                    spec, "error",
                    f"old string is not unique in {spec.file} "
                    f"({count} occurrences) -- narrow it so the mutation is "
                    f"unambiguous")
            try:
                base_code, base_tail = run_declared(work, spec.tests, timeout=timeout)
            except DeclaredCommandError as e:
                return ProveOutcome(spec, "error", str(e))
            if base_code != 0:
                return ProveOutcome(
                    spec, "error",
                    f"baseline (unmutated) already red, exit {base_code}: {base_tail}")
            target.write_text(original.replace(spec.old, spec.new, 1), encoding="utf-8")
            code, tail = run_declared(work, spec.tests, timeout=timeout)
            if code in (-1, -2):
                # the named tests never went red; the run did not finish
                return ProveOutcome(spec, "error", f"mutated run did not complete: {tail}")
            if code == 0:
                return ProveOutcome(spec, "not-caught",
                                    "tests stayed green after the mutation")
            return ProveOutcome(spec, "caught", tail)
    finally:
        changed = tree_changes(before, tree_digests(repo))
        if changed:
            raise changed_tree_error("while proving a mutation", changed)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--file", default=None, metavar="PATH",
                    help="file to mutate, relative to --repo (direct form)")
    sp.add_argument("--old", default=None, metavar="STR",
                    help="exact text to replace (must occur exactly once)")
    sp.add_argument("--old-file", default=None, metavar="PATH", dest="old_file",
                    help="read --old from this file instead (multi-line escaping)")
    sp.add_argument("--new", default=None, metavar="STR", help="replacement text")
    sp.add_argument("--new-file", default=None, metavar="PATH", dest="new_file",
                    help="read --new from this file instead")
    sp.add_argument("--test", action="append", default=[], metavar="NODE_ID",
                    help="test node id that must redden (repeatable, direct form)")
    sp.add_argument("--spec", default=None, metavar="FILE",
                    help="batch form: a YAML list of {file, old, new, tests}, "
                         "instead of --file/--old/--new/--test")
    sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"per-run wall-clock ceiling in seconds (default {DEFAULT_TIMEOUT})")


def _specs_from_yaml(path: Path) -> "list[ProveSpec]":
    doc = yaml_io.yaml_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(doc, list):
        raise ValueError(f"{path}: --spec must be a YAML list of {{file, old, new, tests}}")
    out: "list[ProveSpec]" = []
    for i, entry in enumerate(doc):
        missing = [k for k in ("file", "old", "new", "tests") if k not in entry]
        if missing:
            raise ValueError(f"{path}: entry {i} missing {missing}")
        out.append(ProveSpec(file=entry["file"], old=entry["old"], new=entry["new"],
                             tests=tuple(entry["tests"])))
    return out


def _prove_args(ap: argparse.ArgumentParser) -> None:
    ap.epilog = ('Runs the repo\'s OWN declared domains.test.command inside a COPY with no .git -- a --test that needs git goes red at the BASELINE (exit 2 "baseline already red"), never a false "caught".')
    add_arguments(ap)


def _prove(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    as_json = bool(getattr(opts, "json", False))

    direct_given = bool(args.file or args.old or args.old_file or args.new
                        or args.new_file or args.test)
    if args.spec and direct_given:
        raise hs_family.UsageError(
            "test prove: --spec and the direct form (--file/--old/--new/--test) "
            "are mutually exclusive")
    if not args.spec and not direct_given:
        raise hs_family.UsageError(
            "test prove: nothing to prove -- pass --file/--old/--new/--test, "
            "or --spec FILE")
    if args.old and args.old_file:
        raise hs_family.UsageError("test prove: --old and --old-file are mutually exclusive")
    if args.new and args.new_file:
        raise hs_family.UsageError("test prove: --new and --new-file are mutually exclusive")

    try:
        if args.spec:
            specs = _specs_from_yaml(Path(args.spec))
        else:
            if not args.file or not args.test:
                raise hs_family.UsageError(
                    "test prove: the direct form needs --file and at least one --test")
            old = args.old if args.old is not None else (
                Path(args.old_file).read_text(encoding="utf-8") if args.old_file else None)
            new = args.new if args.new is not None else (
                Path(args.new_file).read_text(encoding="utf-8") if args.new_file else None)
            if old is None or new is None:
                raise hs_family.UsageError(
                    "test prove: need --old/--old-file AND --new/--new-file")
            specs = [ProveSpec(file=args.file, old=old, new=new, tests=tuple(args.test))]
    except (OSError, ValueError, yaml_io.YAMLError) as e:
        raise hs_family.UsageError(f"test prove: {e}") from e

    if not specs:
        raise hs_family.UsageError("test prove: --spec names an EMPTY list -- nothing to prove")

    outcomes = [prove_one(root, s, timeout=args.timeout) for s in specs]

    data = {"results": [{"file": o.spec.file, "tests": list(o.spec.tests),
                         "status": o.status, "detail": o.detail} for o in outcomes]}
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        for o in outcomes:
            tests = ", ".join(o.spec.tests)
            print(f"prove {o.spec.file}  [{tests}]: {o.status.upper()}")
            if o.detail:
                print(f"  {o.detail}")

    if any(o.status == "error" for o in outcomes):
        return 2
    if any(o.status == "not-caught" for o in outcomes):
        return 1
    return 0


PROVE = Command(add_args=_prove_args, run=_prove)
