#!/usr/bin/env python3
"""hs_run_test.py — the `test` domain's verb entry point for hs-run.

ONE registered verb, `next` (`NEXT`). `preflight` and `report` are its
INTERNAL steps, not verbs of their own any more — `test.yaml` folded them out of
the registry (USR-HARNESS-HS-RUN-ONE-DOOR: neither took an argument `next` could
not invent, neither mutates, and neither answered a question `next` did not
already answer at some exit rung). `hs-run test preflight` / `hs-run test report`
are unknown-verb errors from the CLI today; the only way to reach either function
directly is a Python import, which is what `NEXT` itself does and what a test
targeting one function's own logic in isolation does.

`preflight` wraps two already-existing, stable functions — this module adds ZERO
new detection logic of its own (survival invariant #4: a verb wraps a script that
already owns the logic, it never re-implements it):

  - detect_techstack.detect(root) -> {root, stacks, ci, primary}
  - preflight_deps.missing_required() -> list[str]
  - preflight_deps.install_command(missing) -> str

Two questions, split at the run. `preflight` answers "can the suite run"; `report`
answers "what did the last run record". `NEXT` picks between them by reading
disk — a recorded verdict is the later state, so it is checked first; nothing
recorded yet falls through to `preflight`. Neither RUNS the suite: the test-RUN
step still shells out to the stack's own runner (pytest/jest/go test/...), and no
harness script owns "run the suite and interpret the result", so no `run` verb is
manufactured to fill that in.

`report` is not that missing verb wearing a different name. It reads what is
already written — which plan is open (hs_run_cook's resolver) and whether the
verification artifact would survive the push gate (artifact_check's validator) —
so invariant #4 holds for it exactly as for `preflight`. Whether a recorded FAIL
is the intentional red half of a TDD cycle or a real defect is NOT on disk, and
this module does not guess: `suite_failed` reports the FAIL and leaves that call
to the reader.

The six-rung exit ladder (D10) is IMPORTED from hs_run.py,
not redefined here. It was once copied into every domain module on a
no-load-time-dependency argument; ten copies had already drifted (rung 11
carried two different names) and the copies were retired. This module
therefore does depend on hs_run.py at import time — deliberately, and for the
verb declaration as well as the ladder.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)
import detect_techstack  # noqa: E402
import hs_run_next_command  # noqa: E402
import hs_run_scoped_tests  # noqa: E402
import preflight_deps  # noqa: E402

# The six-rung ladder (D10), IMPORTED rather than re-declared.
# Ten modules used to hand-copy these four constants and had already drifted:
# rung 11 carried two different names across the tree and two modules declared
# the same constant twice. `hs_run` owns the ladder as a closed `Exit` IntEnum;
# both historical spellings of rung 11 are bound to the same member there, so
# this import changes no name any caller in this file uses.
import hs_run  # noqa: E402 — the verb declaration lives here
from hs_run import EXIT_BROKEN, EXIT_DONE, EXIT_NEEDS_MODEL, EXIT_STEP_REMAINING  # noqa: E402


def preflight(root: Path) -> dict:
    """Three-way projection, honest at every branch:

    - no stack detected at all -> exit 10 (a model/human has to pick a test
      command; a guess here would be worse than asking).
    - a stack IS detected but its `test_cmd` is None (e.g. a Node repo with
      no `scripts.test` declared in package.json — detect_techstack refuses
      to invent one) -> same exit 10 reasoning, not "ready".
    - a REQUIRED harness dependency is missing (pytest/PyYAML/...) -> exit 2,
      a hard blocker, not a projection.
    - stack known + test_cmd known + deps satisfied -> exit 11. next_action
      MUST be a backtick-quoted, verbatim-runnable command
      (hs_run.py's own `_looks_runnable` forces anything else to EXIT_BROKEN
      — see that module's `_RUNNABLE_HINT_RE` docstring) — a plain-prose
      "run the X suite" (the draft's original wording) fails that contract,
      so this promotes the real detected `test_cmd` into the sentence.

    Preflight takes no argv: its one caller, `NEXT`, has already refused a
    mistyped flag against its own declaration before calling in here.
    """
    stack = detect_techstack.detect(root)
    stacks = stack.get("stacks") or []
    if not stacks:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="no_stack_detected", exit_code=EXIT_NEEDS_MODEL,
            next_action="no tech stack detected under %s — ask the user which "
                        "test command to use" % root,
            artifacts=[stack],
        ).as_dict())
    primary = stacks[0]
    test_cmd = primary.get("test_cmd")
    if not test_cmd:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="stack_no_test_cmd", exit_code=EXIT_NEEDS_MODEL,
            next_action="%s stack detected under %s but no test command is "
                        "declared — ask the user which test command to use"
                        % (primary.get("language"), root),
            artifacts=[stack],
        ).as_dict())
    missing = preflight_deps.missing_required()
    if missing:
        # The install command is the genuine next step here, but this tier is
        # EXIT_BROKEN, not 11 — a hard blocker, not a projection. `next_command`
        # stays null: the dispatcher's pairing rule only governs 10/11, and
        # claiming a runnable step on a BROKEN result would invite a driver to
        # keep going on a state that says stop.
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="deps_missing", exit_code=EXIT_BROKEN,
            next_action=preflight_deps.install_command(missing),
            artifacts=[MissingDeps(missing=list(missing)).as_dict()],
        ).as_dict())
    # Installed, importable, and outside its declared range. This is the tier a plain
    # import check cannot see: the package loads, so `missing_required()` is satisfied,
    # and the break arrives later at the first real call. Reporting it as `ready` sent
    # the runner at a suite that was going to fail for a reason the preflight had the
    # data to name. The function has shipped for as long as this verb has; it was
    # imported here and never called.
    stale = preflight_deps.version_violations()
    if stale:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="deps_stale", exit_code=EXIT_BROKEN,
            next_action="pin the out-of-range dependencies to their declared "
                        "ranges before running the suite",
            artifacts=[StaleDeps(stale=[StaleDep(package=pkg, detail=detail)
                                        for pkg, detail in stale]).as_dict()],
        ).as_dict())
    # The stack's OWN detected command, promoted out of the sentence into the
    # machine field — never a hardcoded `pytest`, which would be a lie on a Go
    # or Node target. `next_action` keeps its human phrasing.
    return hs_run_next_command.finalize(hs_run.StepResult(
        state="ready", exit_code=EXIT_STEP_REMAINING, next_command=test_cmd,
        next_action="run `%s` under %s" % (test_cmd, root),
        artifacts=[stack],
    ).as_dict())


@_attrs.define(frozen=True, kw_only=True)
class RecordedRun:
    """What the last run left on disk, as this verb reports it.

    `failed_checks` names each failed check as recorded (`unit`, `lint/ruff`) and `verdict` is the word
    the artifact carries — two fields a reader downstream is entitled to find, so an
    omission is a TypeError here rather than a None several stages away.
    """

    plan = _attrs.field()
    verdict = _attrs.field(default="")
    failed_checks: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


@_attrs.define(frozen=True, kw_only=True)
class MissingDeps:
    """The packages a suite run needs and does not have.

    A record rather than a dict literal at the emit site: the key a downstream
    reader greps for is declared once, so a rename is one edit instead of a
    grep across every domain that reports the same blocker.
    """

    missing: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return {"missing": list(self.missing)}


@_attrs.define(frozen=True, kw_only=True)
class StaleDep:
    """One installed-but-out-of-range package, with the range it violates."""

    package = _attrs.field()
    detail = _attrs.field(default="")

    def as_dict(self) -> dict:
        return {"package": str(self.package), "detail": str(self.detail)}


@_attrs.define(frozen=True, kw_only=True)
class StaleDeps:
    """Every out-of-range package in one artifact, so a reader sees the set."""

    stale: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return {"stale": [s.as_dict() for s in self.stale]}


@_attrs.define(frozen=True, kw_only=True)
class PlanRef:
    """Which plan a not-yet-recorded answer is about.

    `problems` is empty rather than absent when there is nothing wrong: a reader
    never has to tell "no problems" apart from "this emit site forgot the key".
    """

    plan = _attrs.field()
    problems: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return {"plan": str(self.plan), "problems": list(self.problems)}


def _unverified_reason(plan: Path, root: Path) -> str:
    """Why the recorded PASS does not survive re-derivation, or "" when it does.

    Wraps `artifact_check.evaluate_test_policy`, which re-reads the RAW result
    files a verification's checks reference. The push gate already calls it
    (`gate_stage.py:268`); asking the same function here means this verb and that
    gate cannot reach opposite conclusions about one tree.

    Degrades to "" — report the artifact's own PASS — when the evaluation itself
    cannot run (no change class derivable, policy unreadable). This verb PROJECTS;
    turning its own inability to check into an accusation would be inventing a
    finding, and the push gate still fails closed on the same tree.
    """
    try:
        import artifact_check
        import change_class_derivation as ccd

        changed = ccd.repo_changed_paths(str(root))
        derivation = ccd.derive_from_repo(str(root))
        verdict = artifact_check.evaluate_test_policy(
            plan, derivation.cls, changed, root=str(root),
            ambiguous=derivation.ambiguous)
    except Exception:  # noqa: BLE001 — an evaluation that cannot run makes no claim
        return ""
    # `soft` enforcement is advisory by the policy's own design, so a soft FAIL
    # stays a warning rather than moving the rung — the same routing the gate does.
    if getattr(verdict, "status", "") == "FAIL" \
            and getattr(verdict, "enforcement", "") != "soft":
        return str(getattr(verdict, "reason", "") or "raw results do not back the verdict")
    return ""


def report(root: Path) -> dict:
    """What the suite already recorded, read off disk.

    `preflight` answers "can the suite run". Nothing answered "has a result been
    written", which is the half of this skill the body spends most of its lines on:
    the QA report, the verification artifact, the verdict route, the grid fold. With
    no state for it, those drawers had no step to be routed from, so they sat in prose
    that every run reads whether or not a suite has finished.

    Same invariant as `preflight` — this wraps functions that already own the judgment
    and adds none of its own. Which plan is active: `hs_run_cook._plan_dirs` plus the
    frontmatter status reader, the same pair `cook` resolves with. Whether the artifact
    would survive the push gate: `artifact_check.validate_verification_wellformed`,
    which already knows the canonical test-type names. The verdict itself is a field.

    What it deliberately does NOT do is run anything or judge a result. A recorded FAIL
    is reported as recorded; deciding whether that failure is the intentional red half
    of a TDD cycle or a real defect is not on disk, and manufacturing an answer here
    would be exactly the invented judgment the module docstring refuses.
    """
    import hs_run_cook  # local: only this verb needs the plan resolver
    import artifact_check

    plans = [p for p in hs_run_cook._plan_dirs(root)
             if hs_run_cook._frontmatter_status(p) == "in_progress"] \
        or hs_run_cook._plan_dirs(root)
    if not plans:
        # EXIT_DONE, matching what `cook` and `plan` report for the identical
        # predicate (an empty plans/ dir). It read EXIT_NEEDS_MODEL here, which
        # made one condition on disk answer 0 from two domains and 10 from this
        # one — a driver's next move then depended on which verb it happened to
        # ask. The sentence still names what is owed; only the rung moves.
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="no_plan", exit_code=EXIT_DONE,
            next_action="no plan under %s/plans — a verification has nowhere to be "
                        "written, and the hard stage gate blocks without one even "
                        "on a fully green suite" % root,
        ).as_dict())
    plan = plans[0]
    art = None
    for name in ("verification.yaml", "verification.json"):
        cand = plan / "artifacts" / name
        if cand.is_file():
            art = cand
            break
    if art is None:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="no_verification", exit_code=EXIT_NEEDS_MODEL,
            next_action="run the suite, then write the verdict + checks[] to "
                        "%s/artifacts/verification.yaml" % plan.name,
            artifacts=[PlanRef(plan=plan.name).as_dict()],
        ).as_dict())

    ok, problems = artifact_check.validate_verification_wellformed(plan, root)
    if not ok:
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="verification_malformed", exit_code=EXIT_NEEDS_MODEL,
            next_action="fix the verification artifact — the push gate rejects it "
                        "as written",
            artifacts=[PlanRef(plan=plan.name,
                               problems=list(problems)).as_dict()],
        ).as_dict())

    import hook_runtime
    blob = hook_runtime.yaml_load(art.read_text(encoding="utf-8")) or {}
    verdict = str(blob.get("verdict") or "").upper()
    failed = [c for c in (blob.get("checks") or [])
              if str((c or {}).get("status") or "").upper() == "FAIL"]
    if verdict == "PASS" and not failed:
        # The verdict is what the artifact SAYS. Before reporting rung 0 — the
        # only rung that means "nothing left to do" — re-derive it from the raw
        # result files the checks name.
        #
        # Measured before this landed: a hand-typed `verdict: PASS` naming a test
        # type with no result file anywhere produced an envelope byte-identical to
        # a tree with a real JUnit file. `validate_verification_wellformed` cannot
        # catch it — a check with no `format` key is skipped by design
        # (artifact_check.py:663-665) — so the shape check passed a record that
        # had graded itself, and a driver looping to 0 would hand off unrun work.
        #
        # The re-derivation is not new logic: `evaluate_test_policy` reads the raw
        # files and is already what the push gate calls (gate_stage.py:268). This
        # asks the same function, so the two cannot disagree about one tree.
        stale = _unverified_reason(plan, root)
        if stale:
            return hs_run_next_command.finalize(hs_run.StepResult(
                state="verdict_unbacked", exit_code=EXIT_NEEDS_MODEL,
                # The reason is quoted, not paraphrased. `evaluate_test_policy`
                # fails for two different shapes — a check whose result file is
                # missing/failing, and a required test type with no check at all —
                # and a sentence hard-coding "run the suite" sends the wrong
                # instruction for the second. What is owed is in the reason.
                next_action=(
                    "%s records verdict PASS, but re-deriving it against the DoD "
                    "and the raw result files says otherwise: %s. Close that, then "
                    "record what the suite actually did." % (plan.name, stale)),
                artifacts=[RecordedRun(plan=plan.name, verdict=verdict).as_dict()],
                warnings=[stale],
            ).as_dict())
        return hs_run_next_command.finalize(hs_run.StepResult(
            state="verified", exit_code=EXIT_DONE,
            next_action="verdict PASS recorded in %s — hand off" % plan.name,
            artifacts=[RecordedRun(plan=plan.name, verdict=verdict).as_dict()],
        ).as_dict())
    return hs_run_next_command.finalize(hs_run.StepResult(
        state="suite_failed", exit_code=EXIT_NEEDS_MODEL,
        next_action="verdict %s recorded in %s — fix the code, or fix a test that "
                    "is genuinely wrong and say why" % (verdict or "?", plan.name),
        artifacts=[RecordedRun(
            plan=plan.name, verdict=verdict,
            failed_checks=[c.get("name") for c in failed]).as_dict()],
    ).as_dict())


def _next(ns) -> dict:
    """The one door. Which of the two verbs applies is a DISK question — has a result
    been recorded yet — so the skill body names this and nothing else.

    Answering it by hand meant the body carried a decision table, and a table in the
    body is a second index that falls behind the code. It also meant `report` was
    reachable only by an agent that already knew the verb existed: `preflight` ended
    at "run pytest" and said nothing about what to do with the answer.

    Order matters and is the projection, not a preference: a recorded verdict is the
    later state, so it is checked FIRST. Falling through to `preflight` when nothing
    is recorded is what makes a fresh tree start at the beginning.

    That fold has a real cost, paid here rather than left implicit: `no_plan` and
    `no_verification` are `report`'s own answer for "nothing recorded yet", and both
    are ALWAYS discarded in favour of whatever `preflight` says instead — measured,
    not assumed: on a ready stack with no `plans/` tree at all, and separately on a
    ready stack with an open plan and nothing recorded, this returned `ready` with an
    EMPTY `warnings` list both times, so an agent reading the envelope could not tell
    "you are simply ready" from "you are ready, but there is nowhere for the verdict
    to go" from "you are ready, and a plan is already waiting on a result". The two
    trees are real — hs:test can run standalone, ahead of `hs:cook open`ing a plan —
    and the sentence `report` would have said is the one piece of information this
    verb has and `preflight` does not, so it survives as a warning instead of being
    silently dropped on the fold.

    The sentence survived the fold; its DRAWERS did not, and that is the other half
    of the same cost. `next` is the only verb `test.yaml` registers, so a document
    routed at a discarded state reaches nobody — including the one telling a reader
    HOW to write the verdict this warning says is owed. Merging drawers onto the
    folded envelope is a no-op: a verb returns a bare result and the registry lookup
    happens one layer up (`hs_run.py:2277`), keyed on the state the verb finally
    reported. The fix had to land in the REGISTRY, and it has —
    `references/qa-report-format.md` now rides `ready`, so the warning and the
    instructions for acting on it arrive together.

    `--scope changed` rides HERE, the only verb `test.yaml` registers
    (next-only: no second verb is added for it) — see `_apply_scope_changed`
    for what it does and why it is resolved inline rather than through a
    registry `facts:` declaration on `ready`.
    """
    root = ns.ctx.root
    recorded = report(root)
    if recorded.get("state") not in ("no_plan", "no_verification"):
        return _rerun_hint(recorded)
    folded = preflight(root)
    folded.setdefault("warnings", [])
    folded["warnings"] = list(folded["warnings"]) + [str(recorded.get("next_action") or "")]
    if ns.scope == "changed":
        folded = _apply_scope_changed(folded, root)
    return _rerun_hint(folded)


NEXT = hs_run.verb(_next, options=("scope",))


def _apply_scope_changed(result: dict, root: Path) -> dict:
    """`--scope changed` on a `ready` envelope: narrow the full-suite
    `next_command` `preflight` already named down to the import-graph
    test set the changed files reach, turned into the owning stack's own
    command(s) through `hs_run_scoped_tests` — the SAME door `hs-run cook
    next`'s targeted debt-repay step already shares, so neither module writes
    a second resolve loop or hardcodes a runner literal.

    Only touches a `ready` envelope: every other state this verb can reach
    (`no_stack_detected`, `stack_no_test_cmd`, `deps_missing`, `deps_stale`,
    or a `report`-side state folded through) has no `next_command` to
    narrow, so `--scope` there changes nothing.

    Resolved INLINE (`hs_run_scoped_tests.resolve_reached_tests`), never
    through a registry `facts:` declaration on `ready`: that state declares
    none, on purpose — an UNCONDITIONAL declaration would force every plain
    `hs-run test next` (no `--scope` at all) to start requiring a fresh
    codegraph snapshot, a coupling nothing asked for, and `when_flag:`
    cannot express "facts, but only under this flag" (`StateRouting.
    from_entry`'s nested block keeps a `{rules, refs, docs}` whitelist
    only). Calling the engine inline is what keeps staleness scoped to the
    calls that actually asked for the fact.

    Three outcomes, matching the case table `--scope changed` is built for:
      - the fact resolves fresh with >=1 test reached: `next_command`/
        `next_action` become the scoped command(s).
      - the fact resolves fresh with 0 tests reached, or cannot resolve at
        all for a reason that is not staleness (not adopted, no subjects, a
        provider crash): the full-suite command `preflight` already
        named survives untouched, plus a warning naming why.
      - the snapshot is missing/stale: the WHOLE envelope becomes the
        engine's own `facts_stale` outcome — literally reconstructed here
        (rather than merged wholesale from the engine's dict) so the state
        scanners that police this table (`verb_result_shapes.
        domain_emitted_states`) see a real `"facts_stale"` literal in this
        module, the same as every other state it routes.
    """
    if result.get("state") != "ready":
        return result
    outcome = hs_run_scoped_tests.scoped_or_full_command(root, domain="test", state="ready")
    if outcome.get("stale"):
        # Every field the engine's stale outcome carries passes through; only
        # the envelope's own artifacts/warnings are kept from this verb.
        return {
            **outcome["rewrite"],
            "state": "facts_stale",
            "artifacts": list(result.get("artifacts") or []),
            "warnings": list(result.get("warnings") or []),
        }
    result = dict(result)
    warnings = list(result.get("warnings") or []) + list(outcome.get("warnings") or [])
    command = outcome.get("command")
    if outcome.get("scoped") and command:
        result["next_command"] = command
        result["next_action"] = "run `%s` under %s (scope: changed)" % (command, root)
    result["warnings"] = warnings
    return result


def _rerun_hint(out: dict) -> dict:
    """Point the reader back at `next`, never at the verb that happened to answer.

    Without this the envelope names the inner verb, and an agent that copies the line
    it was handed is back to picking verbs by hand — the exact thing this door exists
    to stop. Rungs 0 and 2 are left alone: nothing follows a finished or broken run,
    and a re-run hint there would read as "try again", which is not the next step.

    Rung 11 gets the hint in the SENTENCE only. `next_command` stays the bare runnable
    string a driver executes verbatim — that field is the machine half of the contract
    and appending prose to it would break the one guarantee it makes.
    """
    if out.get("exit_code") in (EXIT_NEEDS_MODEL, EXIT_STEP_REMAINING):
        out["next_action"] = "%s — then: hs-run test next" % out.get("next_action")
    return out


if __name__ == "__main__":  # pragma: no cover - refusal path, asserted by subprocess
    # Not a command. On this ladder `0` is a CLAIM — "nothing left to do" — so a
    # module that runs here, does nothing and exits 0 tells an automated caller
    # the work is finished. Refuse loudly on the broken rung instead.
    import sys as _sys
    _sys.stderr.write(
        "%s is a hs-run domain module, not a command.\n"
        "Verbs are dispatched through the CLI, which resolves the project root, "
        "loads the registry and owns the exit ladder — none of which happens when "
        "this file is run on its own.\n"
        "Run it through the dispatcher instead:  hs-run %s <verb>\n"
        % (Path(__file__).name, 'test'))
    raise SystemExit(EXIT_BROKEN)
