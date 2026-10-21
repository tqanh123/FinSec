"""What a plan owes the checks its repo declares, and whether the harness's own record of
a run still proves one.

Two kinds of declaration, one shape:
    check    `languages.<lang>.checks` in codemap.yaml (`tsc`, `eslint`), run by
             `hs-code check run <id>`; enforcement from its mode -- `new`/`all` hard,
             `advisory` soft -- read through `policy_floor` (the stricter of the plan's
             base and HEAD)
    runner   a test runner the harness runs (`executor: harness`: vitest, jest), run by
             `hs-code test run --all`; hard. A runner an agent runs (`executor: agent`:
             pytest) is owed nothing here -- its result reaches the gate as today, by type.
    coverage in a repo whose tests the harness runs: `coverage/<group>` for a code-map
             group with a line floor (`coverage.floors`, the higher of base and HEAD) that
             the change touches, and `coverage/total` when the change class carries a
             numeric line floor -- both from `hs-code test run --all --coverage`

    owed     a declaration is owed when the plan changes a file its language pack owns
             under one of the codemap's roots (a runner: a file under a unit the runner is
             set up in) -- by NAME (`typecheck/tsc`, `unit/vitest`), never by type
    problem  why the newest `run` line of a requirement does not prove it now
             (`harness_checks.provenance_problem`), the inputs digested as the run did

The DoD gate and `cook next` both ask here, so the two cannot disagree about what is owed
or whether a run still counts -- in a child process (`harness_checks.unproven` runs this
file): what is owed reads the code map through the code-graph engine, which neither
hs-run nor the hooks import. Data, not a branch per language: packs, checks and runners
are rows. A repo that declares nothing owes nothing.

A change path is relative to the git top; the codemap's roots to the repo, which may sit
under a larger checkout -- `git rev-parse --show-prefix` joins the two.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import harness_checks
import policy_floor
import static_checks
import test_runners


@dataclass(frozen=True)
class Requirement:
    kind: str          # check | runner
    lang: str
    name: str          # `<type>/<id>`: the ledger's `check`
    enforcement: str   # hard | soft
    why: str
    command: str       # what runs it, plan and phase still to add
    config_inputs: "tuple[str, ...]" = ()
    outputs: "tuple[str, ...]" = ()
    full: bool = False  # proven only by a run over every test (`--all`)
    mode: str = ""      # a check's: a run judged under a looser one does not prove it
    reads: "tuple[str, ...]" = ()  # a check's: suffixes its tool reads beyond its pack's
    spec: str = ""      # the spec's fingerprint: a run under another one does not prove it


def _units(root: Path) -> "list[str]":
    """The codemap's root dirs, relative to `root`."""
    import os
    from codegraph._core.descriptor import load
    d = load(root / "codemap.yaml", root).descriptor
    return sorted({os.path.relpath(r.dir(root), root).replace(os.sep, "/") for r in d.roots})


def _under(rel: str, units: "list[str]") -> bool:
    return any(u in ("", ".") or rel == u or rel.startswith(u.rstrip("/") + "/") for u in units)


def _changed(root: Path, changed: Iterable[str]) -> "list[str]":
    """`changed` (relative to the git top) made relative to `root`; paths outside it dropped."""
    prefix = policy_floor._ok(root, "rev-parse", "--show-prefix") or ""
    return [c[len(prefix):] for c in changed if c.startswith(prefix)]


def since_base(root: Path, base: Optional[str]) -> "list[str]":
    """Every path the plan changed since its base, relative to the git top: committed,
    staged, or not added yet. What a caller derives (an upstream, a working-tree diff)
    misses a phase already committed on a branch no upstream tracks."""
    if not base:
        return []
    out: "set[str]" = set()
    for argv in (("diff", "--name-only", "-z", base), ("ls-files", "-z", "-o", "--exclude-standard", "--full-name")):
        r = policy_floor._git(root, *argv)
        if r.returncode != 0:
            raise policy_floor.PolicyBlocked("git %s failed: %s" % (" ".join(argv[:2]), (r.stderr or "").strip()[:200]))
        out.update(p for p in r.stdout.split("\0") if p)
    return sorted(out)


def _cooked(plan: Path) -> bool:
    """Whether a phase of `plan` was opened (its base anchor exists), a declared check was
    run under it, or a verification names the commits a phase made since its mark: any of
    them, and it had a base once."""
    art = Path(plan) / "artifacts"
    return ((art / "phase-commits.jsonl").exists() or any(art.glob("harness-checks-*.jsonl"))
            or bool(harness_checks.verified_commits(Path(plan))))


_COVERAGE_RUN = "hs-code test run --all --coverage"


def owed(root: Path, plan: Path, changed: Iterable[str], phase: Optional[str] = None,
         class_floor: "Optional[float]" = None, class_enforcement: str = "hard") -> "list[Requirement]":
    """What `changed` (paths relative to the git top) owes; `class_floor` is the change
    class's numeric line floor, if it has one. Raises `static_checks.CheckSpecError` /
    `policy_floor.PolicyBlocked` / `test_runners.RunnerError` when a declaration, or the
    policy it is held to, cannot be read -- a caller fails closed on each."""
    root = Path(root)
    head = static_checks.declared(root)
    # a runner is declared once its language is enabled (codemap `languages:`)
    langs = test_runners.enabled_languages(root)
    runners = [(lang, s) for lang, s in test_runners.harness_specs(root) if lang in langs]
    base: Optional[str] = None
    try:
        pol = policy_floor.effective(root, plan, phase=phase)
        checks, base = pol.checks, pol.base.sha
        runners = _with_base_runners(root, pol.base, runners)
        floors, groups = (policy_floor.coverage(root, plan, phase=phase, base=pol.base) if runners
                          else ({}, []))
    except policy_floor.PolicyBlocked:
        # a plan once cooked had a base: one it lost (a mark rewritten, emptied) is not
        # a plan that declares nothing, whatever HEAD says now
        if head or (runners and _head_floors(root)) or _cooked(plan):
            raise
        checks, floors, groups = {}, {}, []  # nothing declared at HEAD and no base to read one at
    if not checks and not runners:
        return []
    rels = _changed(root, set(changed) | set(since_base(root, base)))
    if not rels:
        return []
    from codegraph._core.lang import registry
    packs = {p.id: p for p in registry.packs()}
    units = _units(root)
    mine = [r for r in rels if _under(r, units)]
    out: "list[Requirement]" = []
    for (lang, _cid), eff in sorted(checks.items()):
        pack = packs.get(lang)
        if pack is not None and any(pack.owns(r) for r in mine):
            out.append(Requirement("check", lang, eff.spec.name,
                                   "soft" if eff.mode == "advisory" else "hard", eff.why,
                                   "hs-code check run %s" % eff.spec.id, eff.spec.config_inputs,
                                   mode=eff.mode, reads=eff.spec.reads, spec=eff.spec.fingerprint()))
    seen: "set[str]" = set()
    ran: "list[str]" = []
    at_head = test_runners.harness_specs(root)
    for rel in mine:
        claiming = [(lang, s) for lang, s in runners if lang in packs and packs[lang].owns(rel)]
        found = test_runners.owner(root, rel, claiming) if claiming else None
        if found is None:
            continue
        _unit, lang, spec = found
        ran.append(rel)
        name = "%s/%s" % (spec.type, spec.id)
        if name not in seen:
            seen.add(name)
            if (lang, spec) in at_head:
                why, cmd = "a test runner the harness runs", "hs-code test run --all"
            else:  # only the base's row runs it: no run under HEAD's row can write this line
                why = ("the base has the harness run %s; HEAD's row hands it to the agent or no "
                       "longer finds it" % spec.id)
                cmd = ("put languages.%s.runners.%s in codemap.yaml back to the base's row, then "
                       "hs-code test run --all" % (lang, spec.id))
            out.append(Requirement("runner", lang, name, "hard", why, cmd, spec.config_inputs,
                                   spec.outputs, True, spec=test_runners.fingerprint(spec)))
    if not runners or not (ran or floors):
        return out
    lang = min(lang for lang, _s in runners)
    cfg, outs = test_runners.harness_inputs(root, lang)
    if class_floor is not None and ran:
        out.append(Requirement("coverage", lang, "coverage/total", class_enforcement,
                               "the change class's %s%% line floor" % class_floor,
                               _COVERAGE_RUN, cfg, outs, True))
    import coverage_floors
    for g in coverage_floors.codemap_groups(groups, {n: f.line for n, f in floors.items()}):
        if g.floor is not None and any(g.contains(r) for r in rels):
            out.append(Requirement("coverage", lang, "coverage/%s" % g.name, "hard",
                                   "group %s's %s%% line floor (%s)" % (g.name, g.floor, floors[g.name].why),
                                   _COVERAGE_RUN, cfg, outs, True))
    return out


def _with_base_runners(root: Path, base: policy_floor.Base, head: "list[tuple[str, test_runners.RunnerSpec]]"
                       ) -> "list[tuple[str, test_runners.RunnerSpec]]":
    """HEAD's harness runners and the base's rows together, each row whole: a runner the
    base had the harness run stays owed when HEAD hands it to the agent (`executor:
    agent`) or rewrites what it detects or claims -- the stricter of the two, as for a
    check's mode. HEAD's row comes first, so where both detect it HEAD's is the one run.
    Every policy point of the base adds its rows."""
    out = list(head)
    for doc in policy_floor.codemaps_at(root, base):
        out += [(lang, s) for lang, s in test_runners.harness_specs_of(doc.get("languages")) if (lang, s) not in out]
    return out


def _head_floors(root: Path) -> bool:
    """Whether HEAD's code map declares a coverage floor."""
    import yaml_io
    cm = Path(root) / "codemap.yaml"
    try:
        doc = yaml_io.safe_load(cm.read_text(encoding="utf-8")) if cm.is_file() else {}
    except (OSError, ValueError):
        return True
    return bool(((doc or {}).get("coverage") or {}).get("floors"))


def digest(root: Path, lang: str, units: "list[str]", config_inputs: "tuple[str, ...]",
           outputs: "tuple[str, ...]" = (), whole_units: bool = False,
           reads: "tuple[str, ...]" = ()) -> str:
    """What a result depends on, read the way `hs-code check run` / `test run` read it
    (the line records the `units` it ran; a test run reads them whole)."""
    import harness_checks
    return harness_checks.blobs_digest(manifest(root, lang, units, config_inputs, outputs, whole_units,
                                                reads))


def manifest(root: Path, lang: str, units: "list[str]", config_inputs: "tuple[str, ...]",
             outputs: "tuple[str, ...]" = (), whole_units: bool = False,
             reads: "tuple[str, ...]" = ()) -> "dict[str, str]":
    """`digest`'s (path -> blob id) map, the two sides `harness_checks.held` compares."""
    from codegraph.test.run import inputs_manifest
    return inputs_manifest(Path(root), lang, list(units) or ["."], config_inputs, outputs,
                           whole_units, reads)


def unproven(root: Path, plan: Path, changed: Iterable[str], class_floor: "Optional[float]" = None,
             class_enforcement: str = "hard", scoped_ok: bool = False
             ) -> "list[tuple[Requirement, tuple[str, str]]]":
    """Each requirement `changed` owes that its newest run does not prove now, with why --
    the one answer the DoD gate and `cook next` both read. `scoped_ok` takes a narrower
    test run for the full one (cook, in a plan that defers its suite). Raises as `owed`."""
    out = []
    seen: "dict[tuple, str]" = {}  # one digest per (lang, units, inputs) this ask reads
    for req in owed(root, plan, changed, class_floor=class_floor, class_enforcement=class_enforcement):
        p = problem(root, plan, req, scoped_ok=scoped_ok, digests=seen)
        if p:
            out.append((req, p))
    return out


def problem(root: Path, plan: Path, req: Requirement, phase: Optional[str] = None,
            scoped_ok: bool = False, digests: "Optional[dict[tuple, str]]" = None
            ) -> "Optional[tuple[str, str]]":
    """Why `req` is not proven now, as (kind, detail) -- see
    `harness_checks.provenance_problem` -- or None. A full requirement reads only the
    runs over every test: a `--targets` PASS says nothing of the tests it left out."""
    full = req.full and not scoped_ok
    row = harness_checks.newest_run(plan, req.name, phase,
                                    (lambda r: r.get("scope") == "all") if full else None)
    if row is None and full:
        narrow = harness_checks.newest_run(plan, req.name, phase)
        if narrow is not None:
            ran = "--%s" % narrow["scope"] if narrow.get("scope") else "one that recorded no scope"
            return "missing", ("only a narrower run (%s) is on record; the gate holds the run "
                               "over every test" % ran)
    units = tuple((row or {}).get("units") or ["."])
    key = (req.lang, units, req.config_inputs, req.outputs, req.kind != "check", req.reads)

    def now() -> str:
        cache = digests if digests is not None else {}
        if key not in cache:
            cache[key] = digest(root, req.lang, list(units), req.config_inputs, req.outputs,
                                req.kind != "check", req.reads)
        return cache[key]
    found = harness_checks.provenance_problem(plan, row, now)
    if found is None and req.mode and row is not None and _looser(row.get("mode"), req.mode):
        return "stale", ("its run was judged under mode %s; the policy holds it to %s now"
                         % (row.get("mode") or "(none recorded)", req.mode))
    if found is None and req.spec and row is not None and row.get("spec") != req.spec:
        return "stale", ("its run ran under another spec of %s (argv, bin, baseline, ...) than "
                         "the code map declares now -- run it again" % req.name)
    return found


def _looser(ran: Optional[str], held: str) -> bool:
    """Whether a run judged under mode `ran` is laxer than `held` (an unknown one is)."""
    modes = static_checks.MODES
    return ran not in modes or modes.index(ran) > modes.index(held)


def main(argv: "Optional[list[str]]" = None) -> int:
    """`--root R --plan P`, the changed paths as a JSON list on stdin: one JSON line out --
    `{"unproven": [...]}`, or `{"error": ...}` with exit 3 when a declaration or its policy
    cannot be read."""
    ap = argparse.ArgumentParser(prog="declared_checks.py")
    ap.add_argument("--root", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--class-floor", type=float, default=None)
    ap.add_argument("--class-enforcement", default="hard")
    ap.add_argument("--scoped-ok", action="store_true")
    a = ap.parse_args(argv)
    try:
        rows = [{"check": req.name, "enforcement": req.enforcement, "command": req.command,
                 "problem": kind, "why": why}
                for req, (kind, why) in unproven(Path(a.root), Path(a.plan), json.load(sys.stdin),
                                                 a.class_floor, a.class_enforcement, a.scoped_ok)]
    except (static_checks.CheckSpecError, policy_floor.PolicyBlocked, test_runners.RunnerError,
            harness_checks.LedgerError) as e:
        print(json.dumps({"error": str(e)}))
        return 3
    print(json.dumps({"unproven": rows}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
