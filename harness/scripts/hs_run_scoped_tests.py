#!/usr/bin/env python3
"""hs_run_scoped_tests.py — the ONE door onto `code.tests_reached`, turned
into a runnable stack command, for every hs-run domain that wants to try a
narrower test set before falling back to a wider one.

Three callers share this door and none of them re-implements the resolve
loop or hardcodes a runner literal:

  - `hs_run_cook.py` (the `defer_suite` debt-repay step): a REFUSAL state
    (`suite_debt_unpaid`). Cook calls `resolve_reached_tests` directly and
    folds EVERY non-fresh reason — not adopted, no subjects, stale, missing,
    a provider crash — into one branch: skip the targeted step, go straight
    to the full-suite repay, with a warning naming why. It does this itself
    rather than declaring `facts:` on that state in the registry, because
    `hs_run._apply_facts` rewriting a REFUSAL into `facts_stale` would turn
    "debt unpaid, closing refused" into "run a refresh command" — a
    different and narrower problem than the one actually blocking close.

  - `hs_run_test.py` and `hs_run_fix.py`'s own `--scope changed` handling:
    both call `scoped_or_full_command`, which wraps `resolve_reached_tests` +
    `stack_commands` into the one decision either domain needs — a scoped
    command, or the detected stack's own full command plus a warning, or the
    engine's own stale/missing rewrite passed back untouched. `test.yaml`'s
    `ready` state declares NO `facts:` row at all: an unconditional
    declaration would force every plain `hs-run test next` — no `--scope`
    at all — to start requiring a fresh codegraph snapshot, a coupling
    nothing asked for, and `when_flag:` cannot express "facts, but only
    under this flag" (`StateRouting.from_entry`'s nested block keeps a
    `{rules, refs, docs}` whitelist only — a `facts:` key written there
    parses clean and is silently dropped, exactly what
    `test_no_when_flag_block_carries_a_key_outside_rules_refs_docs`
    guards). So `hs_run_test.py` resolves the fact INLINE, the same way
    this module already does for cook, and hand-reconstructs the engine's
    own `facts_stale` rewrite as a LITERAL `state="facts_stale"` result
    (never a blind `dict.update` of the engine's outcome) so the AST-based
    state scanner (`verb_result_shapes.domain_emitted_states`) sees this
    module actually emitting it. `fix.yaml`'s `needs_review` row is the
    opposite case: it already declares `code.importers`/`code.
    tests_reached` UNCONDITIONALLY (its own pre-existing blast-radius
    facts, unrelated to `--scope`) — so `hs_run._apply_facts` already
    rewrites a stale/missing snapshot into `facts_stale` regardless of
    `--scope`, and `hs_run_fix.py`'s own scope handling leaves that branch
    untouched rather than reconstructing a second copy of the same rewrite.

  Why this module and not `hs_run_next_command.py` (the other shared seam
  every domain already imports): that module owns FORMATTING ONLY — it
  stamps the `next_command` field present/absent and does no detection of
  its own (see its own docstring). Resolving a fact and routing a target to
  its owning stack's command is a different job, one `hs_run_cook.py`
  already did before either `test` or `fix` needed it — so the new code
  joins that existing door instead of teaching the formatting-only one a
  second responsibility.
"""
from __future__ import annotations

from pathlib import Path

import attrs as _attrs  # REQUIRED — see harness/requirements.txt

#: Every message a caller may need to explain why the narrower step did not
#: run — public (no leading underscore) because all three callers format one
#: of these into their own envelope; a private, per-domain copy is exactly
#: the duplication this module exists to remove.
SKIPPED_WARNING = (
    "no importer test set could be named for the changed files (%s) -- "
    "skipping the targeted step, going straight to the full-suite debt repay")

NO_RUNNER_WARNING = (
    "%d importer test(s) reached the changed files, but no stack test "
    "runner could be resolved for %s (%s) -- skipping the targeted step, "
    "going straight to the full-suite debt repay rather than guessing a "
    "runner")

UNMATCHED_TARGET_WARNING = (
    "no command was named for these importer-reached targets -- no detected "
    "stack claims their test file extension, or the stack that does declares "
    "no test command (never guessed into another stack's runner): %s")


def stack_commands(root: Path, tests: list) -> "tuple[list, list]":
    """`tests` (the import-graph reached set) turned into REAL, runnable
    commands through `detect_techstack.detect` + `detect_techstack.
    scoped_test_commands` -- never a hardcoded runner literal, and never a
    single PRIMARY stack picked by position (`stacks[0]`/`stacks[-1]`): a
    polyglot repo (a Python marker AND a Node marker both present) means
    neither end of that list is safe, and each target is routed to the
    stack that actually owns its extension. Returns `(commands, unmatched)`
    -- one command per stack that owns at least one target, and the list
    of targets no detected stack's own `test_exts` claimed."""
    import detect_techstack
    stack = detect_techstack.detect(root)
    stacks = stack.get("stacks") or []
    return detect_techstack.scoped_test_commands(stacks, tests)



@_attrs.define(frozen=True, kw_only=True)
class ReachedTests:
    """`resolve_reached_tests`'s answer, declared once: `tests=None` means the
    fact could not answer (`reason` says why); `stale=True` adds the engine's own
    outcome as `rewrite`."""

    tests = _attrs.field()
    reason = _attrs.field(default=None)
    stale = _attrs.field(default=False)
    rewrite = _attrs.field(default=None)

    def as_dict(self) -> dict:
        return _attrs.asdict(self, recurse=False)


@_attrs.define(frozen=True, kw_only=True)
class ScopedCommand:
    """`scoped_or_full_command`'s answer, declared once: either `stale` with the
    engine's `rewrite`, or the `command` to run with `scoped` and `warnings`."""

    command = _attrs.field(default=None)
    scoped = _attrs.field(default=False)
    warnings: "list[str]" = _attrs.field(factory=list)
    stale = _attrs.field(default=False)
    rewrite = _attrs.field(default=None)

    def as_dict(self) -> dict:
        return _attrs.asdict(self, recurse=False)


def resolve_reached_tests(root: Path, *, domain: str, state: str) -> dict:
    """The import-graph test set the CURRENT tree's changed files reach
    (`code.tests_reached`, the facts engine's own provider), resolved through
    `hs_run_facts.engine.resolve` -- the SAME door `hs_run._apply_facts`
    calls for every other fact-declaring state -- called HERE directly
    rather than through a registry `facts:` declaration: every caller of
    this function needs the raw VALUE before it can build a command or a
    sentence, and `_apply_facts` only runs AFTER a verb has already
    returned (`hs_run.py:main`'s own ordering) -- by then whatever a verb
    built from the fact is already frozen into the envelope. Calling the
    engine inline is what makes the value available to build FROM at all.

    `subjects=None`: the engine's own merge-base-diff + dirty-tree default
    -- "every file that differs", not any one caller's declared set.

    Returns `ReachedTests.as_dict()` -- every key present, unset ones at
    their default -- in one of three cases, kept DISTINCT rather than folded here, so a
    caller can choose its own staleness handling instead of this module
    choosing for it (cook and test/fix disagree about what staleness should
    DO — see the module docstring):

      {"tests": [...]}
        A fresh resolve, possibly an empty list (a real, reportable "no
        importer test reaches the changed files").

      {"tests": None, "reason": <str>}
        The fact could not answer at all, for a reason with no engine-side
        rewrite to hand back: not adopted (no `codemap.yaml`), no subjects
        named, or a provider crash.

      {"tests": None, "reason": <str>, "stale": True, "rewrite": <dict>}
        The snapshot backing the fact is missing or stale. `rewrite` is
        `hs_run_facts.engine.resolve`'s own outcome dict, VERBATIM — the
        same `state`/`domain_state`/`exit_code`/`next_command`/
        `next_action`/`facts` shape `hs_run._apply_facts` would have merged
        onto the envelope had the calling state declared `facts:` in the
        registry. A caller that wants to surface this as its own outcome
        reads `rewrite`; a caller that wants to fold it (cook) reads only
        `reason` and ignores `rewrite` entirely.
    """
    import hs_run
    import hs_run_facts.engine as facts_engine  # noqa: PLC0415 -- lazy, mirrors hs_run._apply_facts
    policy = hs_run.fact_providers_policy()
    outcome = facts_engine.resolve(
        root=Path(root), declared=["code.tests_reached"], subjects=None,
        providers=policy["providers"], sources=policy["sources"],
        domain=domain, state=state)
    if outcome.get("state") == "facts_stale":
        fact = (outcome.get("facts") or {}).get("code.tests_reached")
        reason = fact.get("reason") if isinstance(fact, dict) else None
        return ReachedTests(tests=None, stale=True,
                            reason=reason or "code.tests_reached snapshot is stale",
                            rewrite=dict(outcome)).as_dict()
    fact = (outcome.get("facts") or {}).get("code.tests_reached")
    if not isinstance(fact, dict) or fact.get("unavailable") or fact.get("stale"):
        reason = fact.get("reason") if isinstance(fact, dict) else None
        return ReachedTests(
            tests=None,
            reason=reason or "code.tests_reached could not resolve for %s" % root).as_dict()
    return ReachedTests(tests=list(fact.get("value") or [])).as_dict()


def scoped_or_full_command(root: Path, *, domain: str, state: str) -> dict:
    """The whole `--scope changed` decision in one call: resolve `code.
    tests_reached` (`resolve_reached_tests`) and turn a fresh, non-empty
    answer into the owning stack's own scoped command(s) (`stack_commands`)
    -- falling back to the detected stack's own FULL `test_cmd`
    (`detect_techstack.detect`) when the targeted set is empty or the fact
    could not resolve for a reason that is not staleness. Never a hardcoded
    runner literal on either path.

    Returns `ScopedCommand.as_dict()` -- every key present, unset ones at
    their default -- in one of two cases:

      {"stale": True, "rewrite": <dict>}
        The snapshot is missing/stale — `rewrite` is `resolve_reached_
        tests`'s own engine outcome, untouched, for a caller that wants to
        surface it as its own `facts_stale` state.

      {"command": <str-or-None>, "scoped": <bool>, "warnings": [<str>, ...]}
        `scoped=True`: `command` is every matched stack's own command,
        joined with " && " — the narrower, importer-reached set.
        `scoped=False`: the targeted set was empty, or the fact could not
        resolve at all (not adopted, no subjects, a provider crash) —
        `command` is the PRIMARY detected stack's own `test_cmd` (or `None`
        when no stack is detected at all), and `warnings` names why the
        narrower set was not used.
    """
    step = resolve_reached_tests(root, domain=domain, state=state)
    if step.get("stale"):
        return ScopedCommand(stale=True, rewrite=step["rewrite"]).as_dict()
    tests = step.get("tests")
    warnings: "list[str]" = []
    command = None
    scoped = False
    if tests:
        commands, unmatched = stack_commands(root, tests)
        if commands:
            command = " && ".join(commands)
            scoped = True
            if unmatched:
                warnings.append(UNMATCHED_TARGET_WARNING % ", ".join(unmatched))
        else:
            warnings.append(
                NO_RUNNER_WARNING % (len(tests), root, "no stack test_cmd resolved"))
    else:
        warnings.append(SKIPPED_WARNING % (step.get("reason") or "0 importer tests reached"))
    if not scoped:
        import detect_techstack
        stacks = detect_techstack.detect(root).get("stacks") or []
        command = stacks[0].get("test_cmd") if stacks else None
    return ScopedCommand(command=command, scoped=scoped, warnings=warnings).as_dict()
