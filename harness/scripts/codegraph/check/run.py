"""hs-code check run -- the harness runs a repo's declared static checks (tsc -b, eslint:
`languages.<id>.checks` in codemap.yaml over the pack's rows, `static_checks`) and reads
the verdict itself, against a baseline, under the policy floor.

    hs-code check run [ID...]                      # every declared check, or these
    hs-code check run ID --freeze                  # write the first baseline
    hs-code check run ID --shrink                  # drop what the tool no longer reports
    hs-code check run ID --rebuild-baseline --approved-by WHO --dec DEC-id
                                                   # rewrite after the tool's major moved
    hs-code check run ID --repin-base SHA --dec DEC-id --plan DIR --phase ID
    hs-code check run --restore-interrupted ID | --discard-interrupted ID
        [--plan DIR --phase ID]                    # the plan's base, its ledger, its files

How a run goes
    1. The policy: `policy_floor.effective` -- the stricter of the base's and HEAD's
       mode per check; a base that cannot be read is BLOCKED (exit 3), never "HEAD's".
    2. Refused before anything runs (exit 3): an untrusted repo (`hs-cli trust`), an
       argv carrying one of the check's `write_flags`, and for tsc any file `tsc -b`
       would write (`emit_targets`) that is tracked with uncommitted changes, untracked
       and not ignored, or not there yet and not ignored (with the `.gitignore` line).
    3. The tool runs from the repo root through node_modules/.bin, inside
       `dirty_tree_guard`: an uncommitted file it changed anyway is put back, refused.
    4. The verdict is the parsed output (`parsers`), never the exit code. A row in the
       check's `broken_codes` (a config error) or a parse error is BROKEN (exit 2).
    5. The baseline: eslint's own `eslint-suppressions.json` when the repo has it,
       counted per (file, rule) by the harness; else the check's `baseline:` file
       (`codegraph.check.baseline`), refused when written under another major. When the
       base declares the same check, the reference is the newest `baseline_approval|
       baseline_record` line of the plan's ledgers, else the base's file: what the HEAD
       file holds past it counts as new (`baseline_grew`).
    6. mode `new`: a finding past the baseline, or a baseline entry no longer found
       (stale: --shrink), fails; `all`: any finding fails; `advisory`: PASS, counted.
       A baselined finding is a SARIF/JUnit `note`, an advisory one a `warning`.

What it writes: a SARIF (lint) or JUnit (typecheck) per check, and with `--plan` one
`run` line per check in `harness-checks-<phase>.jsonl` (the inputs digest taken after
the run); --freeze/--shrink add a `baseline_record`, --rebuild-baseline a
`baseline_approval`, --repin-base a `base_repin`. A rebuild or a re-pin needs a DEC the
register holds at HEAD and did not hold at the base.

Exit: 0 PASS, 1 FAIL, 2 BROKEN, 3 BLOCKED (a refusal is 3 too).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import dirty_tree_guard
import harness_checks
import harness_paths
import hs_family
import policy_floor
import process_group
import static_checks
import test_result_writers
from codegraph.check import baseline as bl
from codegraph.check import emit_targets, parsers
from hs_family.cli import Command

TIMEOUT_S = 1800.0
VERSION_TIMEOUT_S = 60.0
_WORST = ("PASS", "SKIP", "FAIL", "BLOCKED", "BROKEN")
_RC = {"PASS": 0, "SKIP": 0, "FAIL": 1, "BROKEN": 2, "BLOCKED": 3}
_FORMAT = {"lint": ("sarif", ".sarif"), "typecheck": ("junit", ".xml")}


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("ids", nargs="*", metavar="ID", help="the checks to run (default: every declared one)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--freeze", action="store_true", help="write the first baseline of ID")
    g.add_argument("--shrink", action="store_true", help="drop from ID's baseline what the tool no longer reports")
    g.add_argument("--rebuild-baseline", action="store_true",
                   help="rewrite ID's baseline after the tool's major version moved (--approved-by, --dec)")
    g.add_argument("--repin-base", metavar="SHA", help="move the plan's policy base to SHA (--dec)")
    dirty_tree_guard.add_interrupted_flags(g)
    sp.add_argument("--approved-by", metavar="WHO", help="who approved the rebuild (attribution)")
    sp.add_argument("--dec", metavar="DEC-ID", help="the decision allowing a rebuild or a re-pin")
    sp.add_argument("--plan", metavar="DIR", help="read the base from this plan; write its ledger + files")
    sp.add_argument("--phase", metavar="ID", help="the plan phase (required with --plan)")
    sp.add_argument("--timeout-s", type=float, default=TIMEOUT_S, metavar="S",
                    help="per check (default %(default)g)")


@dataclass
class Result:
    language: str
    spec: static_checks.CheckSpec
    mode: str
    why: str
    status: str = "PASS"
    reasons: "list[str]" = field(default_factory=list)
    counts: "dict[str, int]" = field(default_factory=dict)
    file: str = ""
    #: the inputs the run itself changed (`harness_checks.held`)
    changed: "list[str]" = field(default_factory=list)

    def worse(self, status: str, reason: str) -> None:
        if _WORST.index(status) > _WORST.index(self.status):
            self.status = status
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        out = {"check": self.spec.name, "status": self.status, "mode": self.mode, "why": self.why, **self.counts}
        if self.reasons:
            out["reason"] = "; ".join(self.reasons)
        if self.file:
            out["file"] = self.file
        if self.changed:
            out["changed_by_run"] = self.changed
        return out


# ---------------------------------------------------------------- plumbing

def _plan(args: argparse.Namespace, root: Path) -> "Path | None":
    if not args.plan:
        if args.phase:
            raise hs_family.UsageError("check run: --phase names a phase of --plan; give --plan")
        return None
    if not args.phase:
        raise hs_family.UsageError("check run: --plan needs --phase (the ledger is per phase)")
    plan = Path(args.plan) if os.path.isabs(args.plan) else root / args.plan
    if not (plan / "plan.md").is_file():
        raise hs_family.UsageError("check run: %s holds no plan.md" % plan)
    return plan


def _git_lines(root: Path, *argv: str) -> "set[str]":
    r = policy_floor._git(root, *argv)
    if r.returncode != 0:
        raise hs_family.RefusedError("check run: git %s failed: %s" % (" ".join(argv), r.stderr.strip()[:200]))
    return {p for p in r.stdout.split("\0") if p}


def _major(text: str) -> int:
    m = re.search(r"(\d+)\.\d+", text)
    if m is None:
        raise hs_family.RefusedError("check run: no version in %r" % text.strip()[:80])
    return int(m.group(1))


def _units(root: Path) -> "list[str]":
    from codegraph._core.descriptor import DescriptorError, load
    try:
        d = load(root / "codemap.yaml", root).descriptor
    except DescriptorError as e:
        raise hs_family.RefusedError("check run: codemap.yaml: %s" % e) from None
    return sorted({os.path.relpath(r.dir(root), root).replace(os.sep, "/") for r in d.roots})


def _argv(spec: static_checks.CheckSpec, units: "list[str]") -> "list[str]":
    out: "list[str]" = []
    for a in spec.argv:
        out.extend(units) if a == static_checks.UNITS else out.append(a)
    return out


def _build_roots(root: Path, argv: "list[str]") -> "list[str]":
    """The projects after `-b`: not a flag, not a flag's boolean value (`--pretty false`),
    and on disk (one that is not is tsc's own error, read back as BROKEN)."""
    rest = argv[argv.index("-b") + 1:] if "-b" in argv else []
    return [a for a in rest if not a.startswith("-") and a not in ("true", "false") and (root / a).exists()]


def _refuse_written(spec: static_checks.CheckSpec) -> None:
    hit = [a for a in spec.argv for f in spec.write_flags if a == f or a.startswith(f + "=")]
    if hit:
        raise hs_family.RefusedError(
            "the %s argv carries %s, which writes into the tree -- remove it from "
            "languages.*.checks.%s.argv in codemap.yaml; nothing run" % (spec.id, ", ".join(hit), spec.id))


def _refuse_targets(root: Path, guess: emit_targets.Guess) -> None:
    """Every file `tsc -b` would write is tracked and clean, or ignored -- else refused."""
    if guess.outside:
        raise hs_family.RefusedError("tsc -b would write outside the repo: %s; nothing run"
                                     % ", ".join(sorted(guess.outside)[:5]))
    tracked = _git_lines(root, "ls-files", "-z")
    dirty = _git_lines(root, "diff", "--name-only", "--relative", "-z", "HEAD")
    untracked = _git_lines(root, "ls-files", "-z", "--others", "--exclude-standard")
    fresh = [t for t in sorted(guess.targets) if t not in tracked and t not in untracked and not (root / t).exists()]
    ignored: "set[str]" = set()
    if fresh:
        r = policy_floor._git(root, "check-ignore", "--no-index", "-z", "--stdin", input_text="\0".join(fresh))
        ignored = {p for p in r.stdout.split("\0") if p}
    bad = ["%s is tracked and has uncommitted changes" % t for t in sorted(guess.targets & dirty)]
    bad += ["%s is untracked and not ignored: tsc -b would write over it" % t for t in sorted(guess.targets & untracked)]
    created = [t for t in fresh if t not in ignored]
    if created:
        dirs = sorted({next((d + "/" for d in sorted(guess.out_dirs, key=len, reverse=True)
                             if t.startswith(d + "/")), t) for t in created})
        bad.append("tsc -b would create %d untracked file(s) git does not ignore (%s) -- add to .gitignore: %s"
                   % (len(created), ", ".join(created[:3]), " ".join(dirs)))
    if bad:
        raise hs_family.RefusedError("nothing run: " + "; ".join(bad))


def _ledger_reference(plan: "Path | None", check: str) -> "Optional[Counter]":
    """The newest baseline_approval|baseline_record entries of `check` across the plan."""
    if plan is None:
        return None
    best: "tuple[str, list] | None" = None
    for f in sorted((plan / "artifacts").glob("harness-checks-*.jsonl")):
        phase = f.name[len("harness-checks-"):-len(".jsonl")]
        for row in harness_checks.rows(plan, phase):
            if row["kind"] in ("baseline_approval", "baseline_record") and row.get("check") == check:
                if best is None or str(row.get("ts", "")) >= best[0]:
                    best = (str(row.get("ts", "")), row.get("entries") or [])
    return None if best is None else _from_entries(best[1])


def _entries(counts: Counter) -> "list[dict]":
    return [{**k._asdict(), "count": n} for k, n in sorted(counts.items()) if n > 0]


def _from_entries(rows: "list[dict]") -> Counter:
    return Counter({bl.Key(r["tool"], r["file"], r["code"], r["message"], r.get("scope") or ""): int(r["count"])
                    for r in rows})


def _base_counts(root: Path, base: policy_floor.Base, lang: str, spec: static_checks.CheckSpec,
                 suppressions: bool) -> "tuple[Optional[Counter], bool]":
    """(the base's baseline of the check of the same id, at the base's own path; whether
    the base held that file). None when the base does not declare the check, or names a
    `baseline:` it never wrote (it refused every run until the first --freeze: there is
    nothing it held). A base that declares the check with neither held every finding to
    no baseline: an empty one -- what HEAD adds is growth."""
    held = policy_floor.checks_at(root, base).get((lang, spec.id))
    if held is None:
        return None, False
    b, at = held
    if suppressions:
        text = policy_floor.read_at(root, at, spec.suppressions_file)
        if text is not None:
            return _suppressed(spec, text), True
        return (None, False) if b.baseline else (Counter(), False)
    if not b.baseline:
        return Counter(), False
    text = policy_floor.read_at(root, at, b.baseline)
    if text is None:
        return None, False
    try:
        doc = json.loads(text)
        return _from_entries(doc["findings"]), True
    except (ValueError, KeyError, TypeError) as e:
        raise hs_family.RefusedError("the base's %s is unreadable: %s" % (b.baseline, e)) from None


def _suppressed(spec: static_checks.CheckSpec, text: str) -> Counter:
    try:
        pairs = parsers.eslint_suppressions(text)
    except parsers.ParseError as e:
        raise hs_family.RefusedError(str(e)) from None
    return Counter({bl.Key(spec.id, f, rule, "", ""): n for (f, rule), n in pairs.items()})


def _dec_rule(root: Path, base: policy_floor.Base, dec: str) -> None:
    """`dec` is in the register now and at none of the base's points: a decision this work
    made, not one a verify naming an older commit makes look new."""
    if dec not in policy_floor.dec_ids(root):
        raise hs_family.RefusedError("%s is not in the decision register (docs/decisions.yaml) -- record "
                                     "the decision first" % dec)
    for sha in base.policy_shas():
        if dec in policy_floor.dec_ids(root, sha):
            raise hs_family.RefusedError("%s was already in the register at the base %s: a rebuild or a "
                                         "re-pin needs a decision this work made" % (dec, sha[:12]))


# ---------------------------------------------------------------- one check

def _execute(root: Path, res: Result, args: argparse.Namespace, units: "list[str]",
             on_hold: "Optional[Callable[[], None]]" = None) -> "tuple[list[dict], int]":
    """(finding rows, the tool's major) -- or a refusal. `on_hold` runs once the dirty-tree
    guard is held, right before the tool: what moved while it waited is no run's."""
    from codegraph._core.lang import project_tool
    spec = res.spec
    _refuse_written(spec)
    exe = str(project_tool.find_bin(root, ".", spec.bin))
    env = project_tool.child_env(root, ".")
    try:
        ver = process_group.run([exe, "--version"], cwd=str(root), env=env, capture_output=True, text=True,
                                timeout=VERSION_TIMEOUT_S)
    except OSError as e:  # a shim whose interpreter is gone: the project's install, not the check
        res.worse("BLOCKED", "%s could not be run (%s) -- reinstall the project's dependencies" % (exe, e))
        return [], 0
    except subprocess.TimeoutExpired:
        res.worse("BROKEN", "%s --version ran past %gs" % (exe, VERSION_TIMEOUT_S))
        return [], 0
    major = _major(ver.stdout + ver.stderr)
    argv = _argv(spec, units)
    if spec.emit == "tsc":
        roots = _build_roots(root, argv)
        try:
            guess = emit_targets.tsc_targets(root, exe, roots or ["."], env)
        except emit_targets.EmitGuessError as e:
            if _broken(res, e.output, root):
                return [], major
            raise hs_family.RefusedError("cannot tell what tsc -b would write: %s; nothing run" % e) from None
        _refuse_targets(root, guess)
    with dirty_tree_guard.guarded(root):
        if on_hold is not None:
            on_hold()
        try:
            proc = process_group.run([exe, *argv], cwd=str(root), env=env, capture_output=True, text=True,
                                     timeout=args.timeout_s)
        except subprocess.TimeoutExpired:
            res.worse("BROKEN", "%s ran past %gs (--timeout-s)" % (spec.id, args.timeout_s))
            return [], major
    try:
        rows = parsers.PARSERS[spec.parser](proc.stdout, cwd=str(root), root=str(root))
    except parsers.ParseError as e:
        res.worse("BROKEN", "%s (rc %d; stderr: %s)" % (e, proc.returncode, proc.stderr.strip()[:200]))
        return [], major
    broken, found = parsers.split_broken(rows, spec.broken_codes)
    if broken:
        _never_judged(res, broken)
        return [], major
    return found, major


def _never_judged(res: Result, broken: "list[dict]") -> None:
    res.worse("BROKEN", "%s never judged the code: %s" % (
        res.spec.id, "; ".join("%s %s" % (r["code"] or "parse", r["message"][:120]) for r in broken[:3])))


def _broken(res: Result, output: str, root: Path) -> bool:
    """A config error the tool printed before the run (its broken codes) marks `res`
    BROKEN; output it does not name that way leaves `res` alone."""
    try:
        rows = parsers.PARSERS[res.spec.parser](output, cwd=str(root), root=str(root))
    except parsers.ParseError:
        return False
    broken, _found = parsers.split_broken(rows, res.spec.broken_codes)
    if broken:
        _never_judged(res, broken)
    return bool(broken)


def _judge(root: Path, plan: "Path | None", base: policy_floor.Base, res: Result, rows: "list[dict]",
           major: int, args: argparse.Namespace) -> "list[dict]":
    """Fills `res`; returns the rows with their SARIF/JUnit level."""
    spec = res.spec
    sup_path = root / spec.suppressions_file if spec.suppressions_file else None
    suppressions = bool(sup_path and sup_path.is_file())
    roots = [str(root), os.path.realpath(root)]
    if suppressions:
        found = Counter(bl.Key(spec.id, r["file"], r["code"], "", "") for r in rows)
        keyed = [(bl.Key(spec.id, r["file"], r["code"], "", ""), r) for r in rows]
    else:
        found = bl.count(rows, spec.id, roots)
        keyed = list(zip(bl.count_keys(rows, spec.id, roots), rows))
    path = root / spec.baseline if spec.baseline else None
    rebuild_cmd = ("hs-code check run %s --rebuild-baseline --approved-by <who> --dec <DEC-id>" % spec.id)
    reference = _ledger_reference(plan, spec.name)
    at_base, base_file = _base_counts(root, base, res.language, spec, suppressions)
    if reference is None:
        reference = at_base
    if args.freeze or args.shrink or args.rebuild_baseline:
        return _write_baseline(root, plan, base, res, found, major, path, suppressions, at_base, base_file,
                               args)
    head: "Optional[Counter]" = None
    if suppressions and sup_path is not None:
        head = _suppressed(spec, sup_path.read_text(encoding="utf-8"))
    elif path is not None and path.is_file():
        try:
            head = bl.load_for(path, spec.id, major, rebuild=rebuild_cmd)
        except bl.BaselineMajorMismatch as e:
            raise hs_family.RefusedError(str(e)) from None
        except bl.BaselineError as e:
            res.worse("BROKEN", str(e))
            return []
    elif path is not None and reference is None and res.mode == "new":
        raise hs_family.RefusedError("%s has no baseline yet (%s) -- write the first: hs-code check run %s --freeze"
                                     % (spec.id, spec.baseline, spec.id))
    grew = 0
    if head is not None and reference is not None:
        grew = sum((head - reference).values())
        effective = head & reference
    else:
        effective = head if head is not None else (reference or Counter())
    diff = bl.compare(found, effective)
    n_new = sum(r["found"] - r["baseline"] for r in diff["new"])
    n_stale = sum(r["baseline"] - r["found"] for r in diff["stale"])
    budget = Counter(effective)
    out = []
    for key, r in keyed:
        if res.mode == "advisory":
            level = "warning"
        elif res.mode == "all":
            level = "error"
        elif budget[key] > 0:
            budget[key] -= 1
            level = "note"
        else:
            level = "error"
        out.append(dict(r, level=level))
    res.counts = {"findings": len(rows), "new": n_new if res.mode != "all" else len(rows),
                  "baselined": sum(1 for r in out if r["level"] == "note"), "stale": n_stale,
                  "warnings": sum(1 for r in out if r["level"] == "warning"), "baseline_grew": grew}
    if res.mode == "advisory":
        return out
    if res.mode == "all" and rows:
        res.worse("FAIL", "%d finding(s); mode all fails on any" % len(rows))
    if res.mode == "new" and n_new:
        res.worse("FAIL", "%d finding(s) past the baseline%s" % (
            n_new, " (the baseline file grew %d past the base's)" % grew if grew else ""))
    if res.mode == "new" and n_stale:
        fix = ("eslint --prune-suppressions" if suppressions else "hs-code check run %s --shrink" % spec.id)
        res.worse("FAIL", "%d baseline entr(ies) no longer found -- shrink it: %s" % (n_stale, fix))
    return out


def _write_baseline(root: Path, plan: "Path | None", base: policy_floor.Base, res: Result, found: Counter,
                    major: int, path: "Optional[Path]", suppressions: bool, at_base: "Optional[Counter]",
                    base_file: bool, args: argparse.Namespace) -> "list[dict]":
    spec = res.spec
    if suppressions:
        raise hs_family.RefusedError("%s keeps its own baseline (%s): eslint writes it, not the harness"
                                     % (spec.id, spec.suppressions_file))
    if path is None:
        raise hs_family.RefusedError("languages.*.checks.%s declares no `baseline:` file" % spec.id)
    row: dict = {"check": spec.name}
    if args.freeze:
        if path.exists():
            raise hs_family.RefusedError("%s exists -- --freeze only writes the first baseline" % spec.baseline)
        if base_file:
            raise hs_family.RefusedError(
                "the base %s holds a baseline for %s: one deleted since is restored, not frozen again (git "
                "checkout %s -- <its path>)" % ((base.sha or "")[:12], spec.id, (base.sha or "")[:12]))
        if at_base is not None:
            # the base already held these findings to no baseline: taking them on is a decision
            if not (args.approved_by and args.dec):
                raise hs_family.RefusedError(
                    "the base %s declares %s with no baseline: a first one here accepts every finding it "
                    "holds -- add --approved-by <who> --dec <DEC-id> (a decision this work records)"
                    % ((base.sha or "")[:12], spec.id))
            _dec_rule(root, base, args.dec)
            bl.freeze(path, spec.id, major, found)
            row.update(kind="baseline_approval", entries=_entries(found), approved_by=args.approved_by,
                       dec=args.dec, added=[k._asdict() for k in sorted(found)], removed=[])
        else:
            bl.freeze(path, spec.id, major, found)
            row.update(kind="baseline_record", entries=_entries(found))
    elif args.shrink:
        if not path.is_file():
            raise hs_family.RefusedError("%s does not exist: nothing to shrink" % spec.baseline)
        try:
            kept = bl.shrink_file(path, spec.id, major, found)
        except bl.BaselineError as e:
            raise hs_family.RefusedError(str(e)) from None
        row.update(kind="baseline_record", entries=_entries(kept))
    else:
        if not (args.approved_by and args.dec):
            raise hs_family.UsageError("check run: --rebuild-baseline needs --approved-by and --dec")
        try:
            was, _old = bl.load(path)
        except bl.BaselineError as e:
            raise hs_family.RefusedError(str(e)) from None
        if was == major:
            raise hs_family.RefusedError("%s was written under %s major %d, the one installed: a rebuild is "
                                         "for a major change only" % (spec.baseline, spec.id, major))
        _dec_rule(root, base, args.dec)
        moved = bl.rebuild(path, spec.id, major, found, approved_by=args.approved_by)
        row.update(kind="baseline_approval", entries=_entries(found), approved_by=args.approved_by, dec=args.dec,
                   added=[k._asdict() for k in moved["added"]], removed=[k._asdict() for k in moved["removed"]])
    if plan is not None:
        harness_checks.append(plan, args.phase, row)
    res.counts = {"findings": sum(found.values()), "written": sum(Counter(_from_entries(row["entries"])).values())}
    res.reasons.append("%s %s" % (row["kind"], spec.baseline))
    return []


# ---------------------------------------------------------------- the verb

def _write_result(root: Path, plan: "Path | None", res: Result, rows: "list[dict]", phase: str) -> None:
    fmt, ext = _FORMAT[res.spec.type]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out_dir = (plan / "artifacts" / "harness-runs" if plan
               else harness_paths.data_root_for(root) / "check-runs" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("%s-%s-%s%s" % (phase or "run", res.spec.id, stamp, ext))
    if fmt == "sarif":
        path.write_text(test_result_writers.findings_to_sarif(res.spec.id, rows), encoding="utf-8")
    else:
        summary = ", ".join("%d %s" % (v, k) for k, v in res.counts.items() if v) or "no finding"
        path.write_text(test_result_writers.findings_to_junit(res.spec.name, rows, summary), encoding="utf-8")
    res.file = path.relative_to(plan / "artifacts").as_posix() if plan else str(path)


def _ledger(plan: "Path | None", phase: str, res: Result, digest: str,
            base: Optional[str] = None) -> None:
    if plan is None:
        return
    # `mode`: what the PASS was judged under -- one under a grace proves nothing once it ends;
    # `base`: the plan base it was held to -- a later base past it is a lost anchor
    row = {"kind": "run", "check": res.spec.name, "type": res.spec.type, "status": res.status,
           "inputs_digest": digest, "units": ["."], "mode": res.mode,
           "spec": res.spec.fingerprint()}
    if base:
        row["base"] = base
    if res.file:
        row.update(format=_FORMAT[res.spec.type][0], file=res.file)
    if res.reasons:
        row["reason"] = "; ".join(res.reasons)
    harness_checks.append(plan, phase, row)


def _repin(args: argparse.Namespace, root: Path, plan: "Path | None") -> int:
    if not args.dec or plan is None:
        raise hs_family.UsageError("check run: --repin-base needs --dec and --plan/--phase")
    sha = policy_floor._ok(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % args.repin_base)
    if not sha:
        raise hs_family.RefusedError("check run: %s is not a commit this repo holds" % args.repin_base)
    _dec_rule(root, policy_floor.Base(sha, "repin"), args.dec)
    harness_checks.append(plan, args.phase, {"kind": "base_repin", "sha": sha, "dec": args.dec})
    _print(args, {"status": "PASS", "base": {"sha": sha, "source": "repin"}, "checks": []})
    return 0


def _run(args: argparse.Namespace) -> int:
    root = args.ctx.root.resolve()
    if args.restore_interrupted or args.discard_interrupted:
        print(dirty_tree_guard.answer_interrupted(root, args.restore_interrupted, args.discard_interrupted))
        return 0
    plan = _plan(args, root)
    writes = args.freeze or args.shrink or args.rebuild_baseline
    if args.repin_base:
        return _repin(args, root, plan)
    if writes and len(args.ids) != 1:
        raise hs_family.UsageError("check run: --freeze/--shrink/--rebuild-baseline name exactly one check ID")
    try:
        declared = static_checks.declared(root)
    except static_checks.CheckSpecError as e:
        raise hs_family.RefusedError("check run: %s" % e) from None
    try:
        pol = policy_floor.effective(root, plan, phase=args.phase)
    except harness_checks.LedgerError as e:
        # no line is appended past a torn one: the run stops and names it
        _print(args, {"status": "BLOCKED", "reason": harness_checks.torn_reason(e), "checks": []})
        return _RC["BLOCKED"]
    except policy_floor.PolicyBlocked as e:
        for lang, spec in declared:
            if not args.ids or spec.id in args.ids:
                r = Result(lang, spec, spec.mode, "")
                r.worse("BLOCKED", "policy: %s" % e)
                _ledger(plan, args.phase, r, "")
        _print(args, {"status": "BLOCKED", "reason": "policy: %s" % e, "checks": []})
        return _RC["BLOCKED"]
    picked = {k: v for k, v in pol.checks.items() if not args.ids or k[1] in args.ids}
    unknown = sorted(set(args.ids) - {k[1] for k in pol.checks})
    if unknown:
        raise hs_family.UsageError("check run: no declared check %s (declared: %s)"
                                   % (", ".join(unknown), ", ".join(sorted(k[1] for k in pol.checks)) or "none"))
    results: "list[Result]" = []
    if picked:
        from codegraph._core.adapter.command_adapter import AdapterRefused, require_commands_trusted
        try:
            require_commands_trusted(root, "hs-code check run", [e.spec.bin for e in picked.values()])
        except AdapterRefused as e:
            for (lang, _cid), eff in sorted(picked.items()):
                r = Result(lang, eff.spec, eff.mode, eff.why)
                r.worse("BLOCKED", "refused: %s" % e)
                _ledger(plan, args.phase, r, "", pol.base.sha)
                results.append(r)
            _print(args, _summary(pol, results))
            return _RC["BLOCKED"]
    units = _units(root) if picked else []
    import declared_checks
    for (lang, _cid), eff in sorted(picked.items()):
        res = Result(lang, eff.spec, eff.mode, eff.why)
        results.append(res)

        def manifest() -> "dict[str, str]":
            return declared_checks.manifest(root, lang, ["."], eff.spec.config_inputs,
                                            reads=eff.spec.reads)
        before: "dict[str, str]" = {}
        try:
            rows, major = _execute(root, res, args, units,
                                   (lambda: before.update(manifest())) if plan and not writes else None)
            if res.status == "PASS":
                rows = _judge(root, plan, pol.base, res, rows, major, args)
                if not writes:
                    _write_result(root, plan, res, rows, args.phase)
        except hs_family.RefusedError as e:
            res.worse("BLOCKED", "refused: %s" % e)
        except policy_floor.PolicyBlocked as e:
            res.worse("BLOCKED", "policy: %s" % e)
        if not writes:
            # read as the gate reads it, before and after: a run that moved an input it
            # reads (a declaration emitted beside the source) proves only the tree it started on
            digest = ""
            if plan and not before:  # the tool never ran: nothing moved under it
                digest = harness_checks.blobs_digest(manifest())
            elif plan:
                digest, res.changed = harness_checks.held(before, manifest())
                if res.changed:
                    res.worse("BLOCKED", harness_checks.moved_reason(
                        before, res.changed, "gitignore what the tool emits"))
            _ledger(plan, args.phase, res, digest, pol.base.sha)
    out = _summary(pol, results)
    _print(args, out)
    return _RC[out["status"]]


def _summary(pol: policy_floor.Policy, results: "list[Result]") -> dict:
    status = max((r.status for r in results), key=_WORST.index, default="SKIP")
    out: dict = {"status": status, "base": {"sha": pol.base.sha, "source": pol.base.source},
                 "checks": [r.as_dict() for r in results]}
    if pol.base.note:
        out["base"]["note"] = pol.base.note
    if not results:
        out["reason"] = "no check is declared (languages.<id>.checks in codemap.yaml)"
    return out


def _print(args: argparse.Namespace, out: dict) -> None:
    if args.opts.json:
        print(json.dumps(out, sort_keys=True, ensure_ascii=False))
        return
    print("check run: %s" % out["status"])
    for c in out["checks"]:
        print("  %s %s [%s]%s%s" % (c["check"], c["status"], c["mode"],
                                    " -- %s" % c["reason"] if c.get("reason") else "",
                                    " (%s)" % c["file"] if c.get("file") else ""))
    if out.get("reason"):
        print("  %s" % out["reason"])


RUN = Command(add_args=add_arguments, run=_run)
