"""hs-code test run -- the harness runs a project's own test runner (vitest, jest: a
runner row whose `executor: harness`) on a scope, and reads the verdict itself.

    hs-code test run --targets PATH...      # these test files
    hs-code test run --changed PATH...      # the tests these changed files reach
    hs-code test run --base REF             # the tests what changed since REF reaches
    hs-code test run --all                  # every test the snapshot knows
        [--coverage]                        # + line coverage: on --all, the groups'
                                            #   floors; on a narrower scope, the files touched
        [--plan DIR --phase ID]             # + a JUnit under the plan's artifacts and
                                            #   one line per check in its check ledger
    hs-code test run --restore-interrupted ID | --discard-interrupted ID

How a run goes
    1. The scope: `--changed`/`--base`/`--all` read the code map's snapshot (fresh, or
       the run is BLOCKED) through `change.selection.select_tests`, BEFORE any runner
       is asked -- a runner's own "related" mode would answer a different question.
    2. Each test file belongs to the nearest directory, walking up from it, where a
       runner is detected (`detect.files`, `package_json_key`, `deps`): its unit. The
       unit's runner is asked what it would run (`select.list_argv`); a requested file
       it does not list is UNOWNED, a file it lists unasked (vitest matches by
       substring: `a.test.ts` also selects `a.test.tsx`) is excluded by absolute path.
    3. A runner argv carrying one of its `write_flags` (a snapshot update) is refused
       before anything runs; so is an untrusted repo (`hs-cli trust`).
    4. The runner runs from its unit, found under `node_modules/.bin` (never a package
       manager), with `project_tool.child_env` (CI=1: a missing snapshot fails instead
       of being written), inside `dirty_tree_guard.guarded`: a run that changes an
       uncommitted file has it put back and is refused.
    5. The verdict is the runner's JSON result file, never its exit code (a red test
       and a config that never loaded both exit 1): no file is BROKEN; a run of no
       test over a non-empty scope is BLOCKED, as is any unowned file; a requested
       file missing on disk is a FAIL by name; a run that wrote a snapshot FAILs.
       An empty scope is SKIP only when the answer is trustworthy (a fresh snapshot,
       a pack whose tests the harness runs enabled in codemap.yaml); else BLOCKED.

What it writes
    One check per runner, `<type>/<runner id>` (`unit/vitest`). With `--plan`: its
    JUnit under `<plan>/artifacts/harness-runs/` and a `kind: run` line in
    `harness-checks-<phase>.jsonl` (`harness_checks`), the inputs digest taken BEFORE
    the run; a refused run writes a BLOCKED line too, so cook reads "fix the setup",
    not "run again". It never writes `verification.json`: `cook verify` folds the
    ledger. Without `--plan`: the JUnit files under `<data home>/test-runs/<ts>/`,
    reported only.

Coverage (`--coverage`)
    Each unit's run adds its runner's `coverage.flags`, writing LCOV into the run's own
    temp dir (never the tree). `SF:` is relative to where the run started -- vitest's root,
    jest's rootDir, both the unit -- and `coverage_floors` joins it there. Only on `--all`
    is a number a floor can be held to: a narrower run reports the files it touched and
    judges nothing (vitest 4+ reports only what a test loaded). On `--all`:
      `coverage/total`    one cobertura `line-rate` over every reported file: what a change
                          class's line floor reads
      `coverage/<group>`  one JUnit case per code-map group with a declared floor
                          (`coverage.floors`, the higher of the plan's base and HEAD --
                          `policy_floor.coverage`); below it FAILs, a group a file on disk is
                          absent from is BLOCKED (configure `coverage.include` /
                          `collectCoverageFrom`, or narrow the group)
    A group with no floor is reported, never judged. A report that does not read is
    BROKEN -- never 0%.

Exit: 0 PASS/SKIP, 1 FAIL, 2 BROKEN, 3 BLOCKED (a refusal is 3 too).
Tests whose runner the agent runs (pytest) are not run here: they come back under
`elsewhere`, with the command that runs them.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import dirty_tree_guard
import harness_checks
import harness_paths
import hs_family
import policy_floor
import process_group
import secret_scan_before_ship as secret_scan
import test_result_writers
import test_runners
from hs_family.cli import Command

TIMEOUT_S = 1800.0
LIST_TIMEOUT_S = 300.0
_WORST = ("PASS", "SKIP", "FAIL", "BLOCKED", "BROKEN")
_RC = {"PASS": 0, "SKIP": 0, "FAIL": 1, "BROKEN": 2, "BLOCKED": 3}
_TAIL = 20


def add_arguments(sp: argparse.ArgumentParser) -> None:
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--targets", nargs="+", metavar="PATH", help="these test files")
    g.add_argument("--changed", nargs="+", metavar="PATH",
                   help="the tests these changed files reach")
    g.add_argument("--base", metavar="REF", help="the tests what changed since REF reaches")
    g.add_argument("--all", action="store_true", help="every test the snapshot knows")
    dirty_tree_guard.add_interrupted_flags(g)
    sp.add_argument("--plan", metavar="DIR", help="write the JUnit + check ledger line here")
    sp.add_argument("--phase", metavar="ID", help="the plan phase (required with --plan)")
    sp.add_argument("--timeout-s", type=float, default=TIMEOUT_S, metavar="S",
                    help="per runner invocation (default %(default)g)")
    sp.add_argument("--coverage", action="store_true",
                    help="also measure line coverage (judged against the groups' floors on --all)")


# ---------------------------------------------------------------- one runner's check

@dataclass
class Check:
    language: str
    spec: test_runners.RunnerSpec
    status: str = "PASS"
    reasons: "list[str]" = field(default_factory=list)
    units: "dict[str, list[str]]" = field(default_factory=dict)
    unowned: "list[str]" = field(default_factory=list)
    missing: "list[str]" = field(default_factory=list)
    results: "list[dict]" = field(default_factory=list)
    file: str = ""
    #: one per-file map per unit run (`coverage_floors.read_lcov`), and why one did not read
    coverage: "list[dict]" = field(default_factory=list)
    cov_problems: "list[str]" = field(default_factory=list)
    #: the inputs the run itself changed (`harness_checks.held`)
    changed: "list[str]" = field(default_factory=list)

    format = "junit"

    @property
    def extra_row(self) -> "dict[str, Any]":
        return {}

    @property
    def name(self) -> str:
        return "%s/%s" % (self.spec.type, self.spec.id)

    @property
    def type(self) -> str:
        return self.spec.type

    def worse(self, status: str, reason: str) -> None:
        if _WORST.index(status) > _WORST.index(self.status):
            self.status = status
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        out = {"check": self.name, "status": self.status, "units": sorted(self.units)}
        if self.reasons:
            out["reason"] = "; ".join(self.reasons)
        if self.file:
            out["file"] = self.file
        if self.changed:
            out["changed_by_run"] = self.changed
        return out


def _rel(root: Path, p: str) -> str:
    full = Path(p) if os.path.isabs(p) else root / p
    try:
        return Path(os.path.normpath(full)).relative_to(root).as_posix()
    except ValueError:
        raise hs_family.UsageError("test run: %s is outside the repo %s" % (p, root)) from None


def _classify(root: Path, scope: "list[str]") -> "tuple[dict[str, Check], list[str]]":
    """Each scope file onto the check of the runner that owns it; files whose suffix
    only an agent-run runner claims come back apart."""
    checks: "dict[str, Check]" = {}
    elsewhere: "list[str]" = []
    for rel in scope:
        claiming = test_runners.harness_specs(root, Path(rel).suffix)
        if not claiming:
            elsewhere.append(rel)
            continue
        found = test_runners.owner(root, rel, claiming)
        lang, spec = (found[1], found[2]) if found else claiming[0]
        c = checks.setdefault(spec.id, Check(lang, spec))
        if not (root / rel).is_file():
            c.missing.append(rel)
        elif found is None:
            c.unowned.append(rel)
        else:
            c.units.setdefault(found[0], []).append(rel)
    return checks, elsewhere


# ---------------------------------------------------------------- running

def _expand(items: "tuple[str, ...]", targets: "list[str]", **subs: str) -> "list[str]":
    out: "list[str]" = []
    for a in items:
        if a == test_runners.PLACEHOLDER:
            out.extend(targets)
            continue
        for k, v in subs.items():
            a = a.replace("{%s}" % k, v)
        out.append(a)
    return out


def _tail(proc: Any) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip().splitlines()
    return " | ".join(text[-_TAIL:])


def _listed(argv: "list[str]", list_file: Path, proc: Any) -> "set[str]":
    """The files a runner's list step names (a JSON list of paths or of `{file}`
    objects, in `list_file` when the argv wrote one, else on stdout)."""
    src = list_file.read_text(encoding="utf-8") if list_file.is_file() else proc.stdout
    doc = json.loads(src or "")
    if not isinstance(doc, list):
        raise ValueError("not a JSON list")
    return {os.path.realpath(x["file"] if isinstance(x, dict) else x) for x in doc}


def _run_unit(root: Path, c: Check, unit: str, rels: "list[str]", tmp: Path,
              timeout_s: float, coverage: bool = False) -> None:
    from codegraph._core.lang import project_tool
    spec = c.spec
    exe = str(project_tool.find_bin(root, unit, spec.bin))
    env = project_tool.child_env(root, unit)
    cwd = root / unit
    want = {os.path.realpath(root / r): r for r in rels}
    targets = sorted(want)
    owned, extras = set(want), set()
    list_argv = tuple(spec.select.get("list_argv") or ())
    if list_argv:
        list_file = tmp / ("%s-%d.list.json" % (spec.id, len(c.results)))
        argv = [exe] + _expand(list_argv, targets, list=str(list_file))
        try:
            proc = process_group.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                     timeout=LIST_TIMEOUT_S)
            listed = _listed(argv, list_file, proc)
        except subprocess.TimeoutExpired:
            c.worse("BROKEN", "%s: its list step ran past %gs" % (unit, LIST_TIMEOUT_S))
            return
        except (ValueError, KeyError, TypeError) as e:
            c.worse("BROKEN", "%s: its list step gave no file list (%s; rc %d): %s"
                    % (unit, e, proc.returncode, _tail(proc)))
            return
        owned = set(want) & listed
        extras = listed - set(want)
        unowned = sorted(want[p] for p in set(want) - listed)
        if unowned:
            c.unowned.extend(unowned)
        if extras and not spec.select.get("exclude_flag"):
            c.worse("BROKEN", "%s: %s also selects %s and has no `select.exclude_flag` to leave "
                    "them out" % (unit, spec.id, ", ".join(sorted(extras))))
            return
    if not owned:
        return
    result = tmp / ("%s-%d.result.json" % (spec.id, len(c.results)))
    argv = [exe] + _expand(spec.argv, sorted(owned), result=str(result))
    for x in sorted(extras):
        argv += [spec.select["exclude_flag"], x]
    cov_dir = None
    if coverage:
        if spec.coverage.get("flags"):
            cov_dir = tmp / ("%s-%d-cov" % (spec.id, len(c.results)))
            argv += _expand(tuple(spec.coverage["flags"]), [], coverage=str(cov_dir))
        else:
            c.cov_problems.append("%s declares no `coverage.flags`" % spec.id)
    try:
        proc = process_group.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                 timeout=timeout_s)
    except subprocess.TimeoutExpired:
        c.worse("BROKEN", "%s: the run went past %gs (--timeout-s)" % (unit, timeout_s))
        return
    if not result.is_file():
        c.worse("BROKEN", "%s: no result file -- the runner did not get to run tests (rc %d): %s"
                % (unit, proc.returncode, _tail(proc)))
        return
    try:
        doc = json.loads(result.read_text(encoding="utf-8"))
        verdict, detail = test_result_writers.jest_json_verdict(doc)
    except ValueError as e:
        c.worse("BROKEN", "%s: the result file is unreadable (%s)" % (unit, e))
        return
    if verdict == "EMPTY":
        c.worse("BLOCKED", "%s: ran no test over %d requested file(s)" % (unit, len(owned)))
        return
    c.results.append(doc)
    if verdict == "FAIL":
        c.worse("FAIL", "%s: %s" % (unit, detail))
    if cov_dir is not None:
        _read_coverage(root, c, unit, cov_dir)


def _read_coverage(root: Path, c: Check, unit: str, cov_dir: Path) -> None:
    """The run's LCOV (per line, mergeable); a run whose reporters wrote only istanbul's
    json-summary (absolute keys, per-file totals) is read from that."""
    import coverage_floors
    lcov, summary = cov_dir / "lcov.info", cov_dir / "coverage-summary.json"
    try:
        if lcov.is_file():
            # SF: relative to where the run started -- vitest's root, jest's rootDir: the unit
            c.coverage.append(coverage_floors.read_lcov(lcov.read_text(encoding="utf-8"), root,
                                                        base=root / unit))
        elif summary.is_file():
            c.coverage.append(coverage_floors.read_istanbul_summary(
                json.loads(summary.read_text(encoding="utf-8")), root))
        else:
            c.cov_problems.append("%s: the run wrote no lcov.info or coverage-summary.json" % unit)
    except (OSError, ValueError) as e:
        c.cov_problems.append("%s: its coverage report does not read: %s" % (unit, e))


@dataclass
class CovCheck:
    """`coverage/total` (cobertura) or `coverage/<group>` (JUnit): judged from the LCOV of
    the unit runs, a ledger line of its own."""
    name: str
    format: str
    units: "list[str]"
    status: str = "PASS"
    reasons: "list[str]" = field(default_factory=list)
    file: str = ""
    extra_row: "dict[str, Any]" = field(default_factory=dict)
    type: str = "coverage"

    def as_dict(self) -> dict:
        out = {"check": self.name, "status": self.status, "units": sorted(self.units)}
        if self.reasons:
            out["reason"] = "; ".join(self.reasons)
        if self.file:
            out["file"] = self.file
        return out


def _source_files(root: Path, idx: "dict | None", langs: "set[str]",
                  never: "set[str]") -> "set[str]":
    """The files a report should cover, as the code map sees them: a pack's own files, not
    a test or a story, not one no runner reports."""
    if idx is None:
        return set()
    import fnmatch
    import codegraph_change_reader
    from codegraph._core.lang import registry
    from codegraph._core.testfiles import build_test_layout
    layout = build_test_layout(root).with_roles(idx)
    packs = [p for p in registry.packs() if p.id in langs]
    return {m["path"] for m in codegraph_change_reader._modules(idx)
            if any(p.owns(m["path"]) for p in packs)
            and not layout.is_test_file(m["path"]) and not layout.is_story_file(m["path"])
            and not any(fnmatch.fnmatch(m["path"], g) for g in never)}


def _coverage(args: argparse.Namespace, root: Path, plan: "Path | None", checks: "dict[str, Check]",
              idx: "dict | None", out_dir: Path, stamp: str) -> "tuple[list[CovCheck], dict]":
    """The coverage checks of an `--all --coverage` run, and what the output says; a
    narrower run gets no check, only the files it touched."""
    import coverage_floors
    import policy_floor
    decl = getattr(_descriptor(root), "coverage", None) or {}
    mine = _coverage_runs(root, checks)
    merged = coverage_floors.merge(*[m for c in mine for m in c.coverage])
    problems = [x for c in mine for x in c.cov_problems]
    problems += ["%s did not complete (%s)" % (c.name, c.status) for c in mine
                 if c.status in ("BROKEN", "BLOCKED")]
    if not args.all:
        return [], {"scope": "touched -- floors are judged only on --all --coverage",
                    "files": {k: round(100.0 * v.hit / v.found, 1) for k, v in sorted(merged.items())
                              if v.found},
                    **({"problems": problems} if problems else {})}
    units = sorted({u for c in mine for u in c.units})
    provider = decl.get("provider", "default")
    total = CovCheck("coverage/total", "cobertura", units, extra_row={"provider": provider})
    report: dict = {"provider": provider}
    if not mine:
        total.status, total.reasons = "BLOCKED", ["no harness-run test ran over this scope"]
        return [total], report
    if problems or not merged:
        total.status, total.reasons = "BROKEN", problems or ["the runs reported no file"]
        return [total], report
    found, hit = coverage_floors.total(merged)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%s-coverage-total-%s.xml" % (args.phase or "run", stamp))
    path.write_text(test_result_writers.coverage_to_cobertura(found, hit), encoding="utf-8")
    total.file = path.relative_to(plan / "artifacts").as_posix() if plan else str(path)
    report["total"] = {"lines": found, "hit": hit, "pct": round(100.0 * hit / found, 1) if found else None}
    if plan is not None:
        prev = harness_checks.newest_run(plan, "coverage/total")
        if prev and prev.get("provider", provider) != provider:
            report["note"] = ("provider %s -> %s: these numbers start a new baseline, not a "
                              "regression" % (prev.get("provider"), provider))
    out = [total]
    try:
        floors, groups = policy_floor.coverage(root, plan, phase=args.phase)
    except policy_floor.PolicyBlocked as e:
        blocked = CovCheck("coverage/floors", "junit", units, "BLOCKED", ["refused: %s" % e])
        return out + [blocked], report
    gs = coverage_floors.codemap_groups(groups, {n: f.line for n, f in floors.items()})
    langs = {c.language for c in mine}
    never = {g for c in mine for g in c.spec.coverage.get("never_reported") or ()}
    disk = _source_files(root, idx, langs, never)
    results = coverage_floors.evaluate(merged, gs, lambda g: {r for r in disk if g.contains(r)})
    report["groups"] = [{"group": r.name, "status": r.status, "lines": r.found, "hit": r.hit,
                         "pct": round(r.pct, 1) if r.pct is not None else None,
                         **({"floor": r.floor, "why": floors[r.name].why} if r.name in floors else {}),
                         **({"reason": r.reason} if r.reason else {})} for r in results]
    for r in results:
        if r.floor is None:
            continue
        cc = CovCheck("coverage/%s" % r.name, "junit", units,
                      {"PASS": "PASS", "FAIL": "FAIL"}.get(r.status, "BLOCKED"),
                      extra_row={"provider": provider})
        if r.status == "UNMEASURABLE":
            cc.reasons = [r.reason + " Set the runner's coverage.include (vitest) or "
                          "collectCoverageFrom (jest) to the group's files, or narrow its match."]
        else:
            failure = ("%.1f%% is below the floor of %s (%d/%d lines missed)"
                       % (r.pct or 0.0, r.floor, r.found - r.hit, r.found)) if r.status == "FAIL" else ""
            if failure:
                cc.reasons = [failure]
            gp = out_dir / ("%s-coverage-%s-%s.xml" % (args.phase or "run", r.name, stamp))
            gp.write_text(test_result_writers.coverage_group_to_junit(
                cc.name, r.name, r.found, r.hit, r.floor, failure), encoding="utf-8")
            cc.file = gp.relative_to(plan / "artifacts").as_posix() if plan else str(gp)
        out.append(cc)
    return out, report


def _settle(c: Check) -> None:
    if c.unowned:
        c.worse("BLOCKED", "no %s unit lists %s -- the runner's config does not include it"
                % (c.spec.id, ", ".join(sorted(c.unowned))))
    if c.missing:
        c.worse("FAIL", "requested test file(s) not on disk: %s" % ", ".join(sorted(c.missing)))


def _junit(root: Path, c: Check, out_dir: Path, stem: str) -> "Path | None":
    tests = [r for d in c.results for r in d.get("testResults") or []]
    tests += [{"name": str(root / m), "assertionResults": [
        {"fullName": "test file not found", "status": "failed",
         "failureMessages": ["%s was requested and is not on disk" % m]}]} for m in c.missing]
    total = sum(int(d.get("numTotalTests") or 0) for d in c.results) + len(c.missing)
    if not total:
        return None
    doc = {"numTotalTests": total, "testResults": tests}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%s.xml" % stem)
    path.write_text(test_result_writers.jest_json_to_junit(doc, c.name, root=str(root)),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------- scope + provenance

def _descriptor(root: Path) -> Any:
    from codegraph._core.descriptor import DescriptorError, load
    try:
        return load(root / "codemap.yaml", root).descriptor
    except DescriptorError as e:
        raise hs_family.RefusedError("test run: codemap.yaml: %s" % e) from None


def _scope(args: argparse.Namespace, root: Path) -> "tuple[list[str], dict, Optional[str], Optional[dict]]":
    """(scope, extra output, why an empty scope cannot be trusted, the snapshot read)."""
    if args.targets:
        return [_rel(root, t) for t in args.targets], {}, None, None
    from codegraph._core.git import GitError, changed_since
    from codegraph.change import selection
    from codegraph_graph_freshness import SnapshotResolveError, resolve_idx
    from hs_family import gate_registry
    descriptor = _descriptor(root)
    try:
        changed = ([_rel(root, p) for p in args.changed] if args.changed
                   else changed_since(root, args.base) if args.base else [])
    except GitError as e:
        raise hs_family.RefusedError("test run: %s" % e) from None
    try:
        _b, _d, idx, _s = resolve_idx(root, descriptor, args.opts, None, None, "test run", False)
    except SnapshotResolveError as e:
        return [], {"changed": changed}, "no fresh snapshot to select tests from (%s)" % e, None
    try:
        whole = selection.whole_tree_files(root, descriptor)
    except gate_registry.GateRegistryError as e:
        raise hs_family.RefusedError("test run: %s" % e) from None
    sel = selection.select_tests(idx, changed if not args.all else [], root=root,
                                 whole_tree_files=whole)
    # nothing changed puts every test in the blind spot: that is `--all`'s scope
    scope = sel.blind_spot if args.all else sel.selected
    extra: dict = {"changed": changed} if not args.all else {}
    if sel.stories:
        extra["stories_not_run"] = sel.stories
    langs = set(getattr(descriptor, "languages", None) or {})
    if not langs & {lang for lang, _s in test_runners.harness_specs(root)}:
        return scope, extra, ("no language whose tests the harness runs is enabled in "
                              "codemap.yaml `languages:`"), idx
    return scope, extra, None, idx


def _runs_elsewhere(root: Path) -> "Optional[list[str]]":
    """The repo's own full-run commands when no runner the harness runs is set up in any
    directory of it (a Python-only repo: pytest is the agent's), map or no map, pack on or
    off -- None when one is (a TS workspace below a Python root included), or when no
    stack is detected."""
    import detect_techstack
    stacks = detect_techstack.detect(root).get("stacks") or []
    if not stacks or _harness_runner_set_up(root):
        return None
    return [c for c in (detect_techstack.full_test_command(s, root) for s in stacks) if c]


def _harness_runner_set_up(root: Path) -> bool:
    """Whether a runner the harness runs is `detected` in a directory of the repo that
    holds a package.json or one of its `detect.files`."""
    specs = [s for _l, s in test_runners.harness_specs(root)]
    names = {"package.json"} | {posixpath.basename(f) for s in specs for f in s.detect.get("files") or ()}
    git = ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root), "ls-files", "-z", "--cached", "--others",
           "--exclude-standard"]
    r = subprocess.run(git, capture_output=True, env=secret_scan.git_safe_env())
    files = [p.decode("utf-8", "surrogateescape") for p in r.stdout.split(b"\0") if p] if r.returncode == 0 else []
    dirs = {posixpath.dirname(f) or "." for f in files if posixpath.basename(f) in names}
    return any(test_runners.detected(root, d, s) for d in sorted(dirs) for s in specs)


def _manifest(root: Path, c: Check) -> "dict[str, str]":
    return inputs_manifest(root, c.language, list(c.units or {".": []}), c.spec.config_inputs,
                           c.spec.outputs, whole_units=True)


def _coverage_runs(root: Path, checks: "dict[str, Check]") -> "list[Check]":
    """The runs a coverage line is read off: `coverage.runner`'s, or every one when unset."""
    decl = getattr(_descriptor(root), "coverage", None) or {}
    return [c for c in checks.values() if c.units and decl.get("runner") in (None, c.spec.id)]


def _coverage_manifest(root: Path, checks: "dict[str, Check]") -> "dict[str, str]":
    """What a coverage line depends on: the units of the runs it is read off -- the `units`
    the line records, which the gate digests again -- under the inputs of every runner of
    its language the harness runs (`test_runners.harness_inputs`, as the gate reads them)."""
    units = sorted({u for c in _coverage_runs(root, checks) for u in c.units}) or ["."]
    lang = min((c.language for c in checks.values()), default="")
    config_inputs, outputs = test_runners.harness_inputs(root, lang)
    return inputs_manifest(root, lang, units, config_inputs, outputs, whole_units=True)


def inputs_manifest(root: Path, language: str, units: "list[str]", config_inputs: "tuple[str, ...]",
                  outputs: "tuple[str, ...]" = (), whole_units: bool = False,
                  reads: "tuple[str, ...]" = ()) -> "dict[str, str]":
    """What a harness-run tool's result depends on, as (path -> blob id): the files of its
    language pack, the pack's config (tsconfig chain, workspace manifests), the tool's
    `config_inputs` (globs, unit-relative) -- less its `outputs`. `whole_units`: a test
    reads more than its language (a snapshot, a fixture, a stylesheet), so a test run
    depends on every file under its units -- less the harness's own: the plans (the
    ledger the run itself appends) and the data root. `reads`: suffixes the tool reads
    beyond its pack's (a check's `reads:`), every such file under the units. The plans and
    the data root are never an input, on any branch; `outputs` leaves out only what the
    repo does not track -- a tracked file is never something a run writes."""
    from codegraph._core.lang import registry
    git = ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root)]
    top = Path(subprocess.run(git + ["rev-parse", "--show-toplevel"], check=True, capture_output=True,
                              text=True, env=secret_scan.git_safe_env()).stdout.strip())
    listed = subprocess.run(git + ["ls-files", "-z", "-t", "--cached", "--others", "--exclude-standard"],
                            check=True, capture_output=True, env=secret_scan.git_safe_env()).stdout
    tagged = [(p[:1], p[2:].decode("utf-8", "surrogateescape")) for p in listed.split(b"\0") if p]
    files = sorted({f for _, f in tagged})
    tracked = {f for tag, f in tagged if tag != b"?"}
    pack = next((p for p in registry.packs() if p.id == language), None)
    rels = {f for f in files if pack is not None and pack.owns(f)}
    under = [posixpath.normpath(u) for u in units]
    if reads:
        rels |= {f for f in files if posixpath.splitext(f)[1].lower() in reads
                 and any(u == "." or f.startswith(u + "/") for u in under)}
    try:
        rels |= set(registry.config_inputs(root, _descriptor(root)))
    except hs_family.RefusedError:
        pass
    for unit in units:
        for g in config_inputs:
            rels |= {p.relative_to(root).as_posix() for p in (root / unit).glob(g) if p.is_file()}
    outs = [posixpath.normpath(posixpath.join(u, o)) for u in units for o in outputs]
    if whole_units:
        rels |= {f for f in files if any(u == "." or f.startswith(u + "/") for u in under)}
    data = os.path.relpath(harness_paths.data_root_for(root), root)
    own = ["plans"] + ([Path(data).as_posix()] if not data.startswith("..") else [])

    def _in(r: str, dirs: "list[str]") -> bool:
        return any(r == o or r.startswith(o + "/") for o in dirs)
    rels = {r for r in rels if not _in(r, own) and not (r not in tracked and _in(r, outs))}
    return harness_checks.input_blobs(
        top, sorted(Path(os.path.relpath(root / r, top)).as_posix() for r in rels))


def _plan(args: argparse.Namespace, root: Path) -> "Path | None":
    if not args.plan:
        if args.phase:
            raise hs_family.UsageError("test run: --phase names a phase of --plan; give --plan")
        return None
    if not args.phase:
        raise hs_family.UsageError("test run: --plan needs --phase (the ledger is per phase)")
    plan = Path(args.plan) if os.path.isabs(args.plan) else root / args.plan
    if not (plan / "plan.md").is_file():
        raise hs_family.UsageError("test run: %s holds no plan.md" % plan)
    return plan


# ---------------------------------------------------------------- the verb

def _interrupted(args: argparse.Namespace, root: Path) -> int:
    print(dirty_tree_guard.answer_interrupted(root, args.restore_interrupted, args.discard_interrupted))
    return 0


def _refuse_written(spec: test_runners.RunnerSpec) -> None:
    hit = [a for a in spec.argv for f in spec.write_flags if a == f or a.startswith(f + "=")]
    if hit:
        raise hs_family.RefusedError(
            "test run: the %s argv carries %s, which writes into the tree (a snapshot update) "
            "-- remove it from languages.*.runners.%s in codemap.yaml; nothing run"
            % (spec.id, ", ".join(hit), spec.id))


def _ledger(plan: "Path | None", phase: str, c: "Check | CovCheck", digest: str, scope: str,
            base: Optional[str] = None) -> None:
    if plan is None:
        return
    # `units`: the ones the digest read, so the DoD gate digests the same set again;
    # `scope`: a `--targets` run proves nothing of the tests it left out
    row = {"kind": "run", "check": c.name, "type": c.type, "status": c.status,
           "inputs_digest": digest, "units": list(c.units or {".": []}), "scope": scope}
    if isinstance(c, Check):  # the spec it ran under: one loosened and put back is another
        row["spec"] = test_runners.fingerprint(c.spec)
    if base:  # the plan base it was held to -- a later base past it is a lost anchor
        row["base"] = base
    if c.file:
        row.update(format=c.format, file=c.file)
    if c.reasons:
        row["reason"] = "; ".join(c.reasons)
    row.update(c.extra_row)
    harness_checks.append(plan, phase, row)


def _run(args: argparse.Namespace) -> int:
    root = args.ctx.root.resolve()
    if args.restore_interrupted or args.discard_interrupted:
        return _interrupted(args, root)
    plan = _plan(args, root)
    if args.coverage and not args.all and plan is not None:
        raise hs_family.UsageError("test run: --coverage with --plan is the full run a floor is "
                                   "judged on -- add --all; a narrower --coverage only reports")
    base = None
    if plan is not None:
        # a run is held to the plan's base: none to give (no phase opened, its marks gone,
        # a torn ledger) and the run proves nothing, so it does not start
        try:
            base = policy_floor.resolve_base(root, plan, args.phase).sha
        except (policy_floor.PolicyBlocked, harness_checks.LedgerError) as e:
            why = (harness_checks.torn_reason(e) if isinstance(e, harness_checks.LedgerError)
                   else "policy: %s" % e)
            _print(args, {"status": "BLOCKED", "scope": [], "checks": [], "reason": why})
            return _RC["BLOCKED"]
    away = None if args.targets else _runs_elsewhere(root)
    if away is not None:
        _print(args, {"status": "SKIP", "scope": [], "checks": [],
                      "reason": "no runner the harness runs claims this repo's tests: its own "
                                "full run is under `elsewhere`",
                      "elsewhere": {"tests": [], "commands": away}})
        return _RC["SKIP"]
    scope, extra, untrusted_empty, idx = _scope(args, root)
    ran = ("all" if args.all else "targets" if args.targets else "changed" if args.changed else "base")
    checks, elsewhere = _classify(root, scope)
    try:
        for c in checks.values():
            _refuse_written(c.spec)
        if checks:
            from codegraph._core.adapter.command_adapter import AdapterRefused, require_commands_trusted
            try:
                require_commands_trusted(root, "hs-code test run",
                                         [c.spec.bin for c in checks.values()])
            except AdapterRefused as e:
                raise hs_family.RefusedError(str(e)) from None
        before: "dict[str, dict[str, str]]" = {}
        cov_before: "dict[str, str]" = {}
        tmp = Path(tempfile.mkdtemp(prefix="hs-test-run-"))
        try:
            outputs = sorted({Path(os.path.relpath(root / u / o, dirty_tree_guard._toplevel(root)))
                              .as_posix() for c in checks.values() for u in c.units
                              for o in c.spec.outputs})
            with dirty_tree_guard.guarded(root, outputs) if any(c.units for c in checks.values()) \
                    else _nothing():
                # read once the guard is held: what moved while it waited is no run's
                if plan:
                    before = {k: _manifest(root, c) for k, c in checks.items()}
                    cov_before = _coverage_manifest(root, checks) if args.coverage else {}
                for c in checks.values():
                    for unit, rels in sorted(c.units.items()):
                        try:
                            _run_unit(root, c, unit, rels, tmp, args.timeout_s, args.coverage)
                        except OSError as e:  # a shim whose interpreter is gone: the install
                            c.worse("BLOCKED", "%s: %s could not be run (%s) -- reinstall the "
                                    "project's dependencies" % (unit, c.spec.bin, e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except hs_family.RefusedError as e:
        for k, c in checks.items():
            c.status, c.reasons = "BLOCKED", ["refused: %s" % e]
            _ledger(plan, args.phase, c, harness_checks.blobs_digest(_manifest(root, c)) if plan else "",
                    ran, base)
        raise
    # read again after the run (the guard has already put back what was there before): a
    # run that moved an input it reads proves only the tree it started on
    digests: "dict[str, str]" = {}
    cov_digest = ""
    if plan:
        for k, c in checks.items():
            digests[k], c.changed = harness_checks.held(before[k], _manifest(root, c))
            if c.changed:
                c.worse("BLOCKED", harness_checks.moved_reason(
                    before[k], c.changed, "gitignore what the run writes, or name it in the runner's "
                    "`outputs`"))
        if args.coverage:
            cov_digest, _ = harness_checks.held(cov_before, _coverage_manifest(root, checks))
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out_dir = (plan / "artifacts" / "harness-runs" if plan
               else harness_paths.data_root_for(root) / "test-runs" / stamp)
    for k, c in checks.items():
        _settle(c)
        path = _junit(root, c, out_dir, "%s-%s-%s" % (args.phase or "run", c.spec.id, stamp)) \
            if c.status in ("PASS", "FAIL") else None
        if path is not None:
            c.file = (path.relative_to(plan / "artifacts").as_posix() if plan else str(path))
        _ledger(plan, args.phase, c, digests.get(k, ""), ran, base)
    cov_checks: "list[CovCheck]" = []
    if args.coverage:
        cov_checks, extra["coverage"] = _coverage(args, root, plan, checks, idx, out_dir, stamp)
        for cc in cov_checks:
            _ledger(plan, args.phase, cc, cov_digest, ran, base)
    every: "list[Check | CovCheck]" = [*checks.values(), *cov_checks]
    status = max((c.status for c in every), key=_WORST.index, default="SKIP")
    out: dict = {"status": status, "scope": scope, "checks": [c.as_dict() for c in every],
                 **extra}
    if not checks and untrusted_empty:
        status = out["status"] = "BLOCKED"
        out["reason"] = untrusted_empty
    unowned = sorted(x for c in checks.values() for x in c.unowned)
    missing = sorted(x for c in checks.values() for x in c.missing)
    if unowned:
        out["unowned"] = unowned
    if missing:
        out["missing"] = missing
    if elsewhere:
        cmds, _ = test_runners.commands(root, elsewhere)
        out["elsewhere"] = {"tests": elsewhere, "commands": cmds}
    _print(args, out)
    return _RC[status]


class _nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> None:
        return None


def _print(args: argparse.Namespace, out: dict) -> None:
    if args.opts.json:
        print(json.dumps(out, sort_keys=True, ensure_ascii=False))
        return
    print("test run: %s -- %d test file(s) in scope" % (out["status"], len(out["scope"])))
    for c in out["checks"]:
        print("  %s %s%s%s" % (c["check"], c["status"], " -- %s" % c["reason"] if c.get("reason") else "",
                               " (%s)" % c["file"] if c.get("file") else ""))
    if out.get("reason"):
        print("  %s" % out["reason"])
    for t in out.get("stories_not_run") or ():
        print("  story (not run): %s" % t)
    if out.get("elsewhere"):
        print("  not run here (the agent runs them): %s" % "; ".join(out["elsewhere"]["commands"]),
              file=sys.stderr)


RUN = Command(add_args=add_arguments, run=_run)
