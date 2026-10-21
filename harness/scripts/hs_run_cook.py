#!/usr/bin/env python3
"""hs_run_cook.py — the `cook` domain's verb entry points for hs-run.

This module replaces the earlier
honest-but-narrow floor (`next` only, "unresolved" for anything real) with the
full per-phase state machine cook actually needs. The asymmetry vs `plan`
(re-verified against the cook lifecycle) drives every shape
decision here — do NOT copy `plan`'s eventual verb shape wholesale:

  - E17: cook's grid promise is PER PHASE (`emit --phase` mandatory), never
    per-plan. `next`'s per-phase loop never gates an individual phase's
    "done" status on a phase micro-grid — the recon's own nuance beyond E17
    is that the phase-count promise is
    enforced ONLY by a separate, later gate (`--require-phase-grids`, cook
    Step 6e), never folded into per-phase completion. See `CLOSE` /
    `_finalize_projection` for where that gate actually lives — and see
    "E27's two artifact paths" below for why BOTH this module's `preflight`
    and its `close`/finalize projection must each run their own independent
    grid check, at two different times, against two different artifact
    shapes.
  - E15: cook's grid fill defaults to NO model (deterministic STUB) — this
    module never makes a grid-fill step a mandatory model turn anywhere.
  - E16: cook has no axis-selection step of its own; the axis-selection
    receipt is a MACRO-grid-only requirement (`grid_emit_guard.py`'s own
    `_is_macro_grid_path`, keyed on file path, not a field) — this module
    never invents one for the per-phase grid.

Wrap, never rewrite (survival invariant #4): every verb below calls into an
existing script's own Python API (`plan_graph`, `grid_emit_guard`,
`grid_flag_carry`, `derive_plan_completion`, `artifact_check`, `plan_status`,
`open_plan`, `close_plan`, `write_verification`, `frontmatter_parser`,
`defer_suite_carry`, `graph_test_scope`) — no judgment is re-implemented
here, only ordering + parameter-passing.

E27 — the trap this module exists to not fall into: cook reads TWO DIFFERENT
artifact paths at two different times.

  1. `preflight` (0.5a-0.5c): the ONE-TIME, plan-level check before phase 1 —
     `plan_graph.py --require`-equivalent (sidecar + `post:` obligations) plus
     `grid_emit_guard.py --require`-equivalent (the MACRO `coverage-grid.json`
     artifact, checked ONLY when grid mode is carried).
  2. `close` (6e-then-6g): the LAST-call check before finalizing — a
     COMPLETELY SEPARATE `grid_emit_guard.py --require-phase-grids`-equivalent
     that counts, for EVERY phase `plan-graph.yaml` declares, whether its own
     PER-PHASE `coverage-grid-<phase>.<fmt>` micro-grid exists. A macro grid
     passing (1) proves NOTHING about (2) — they read different filenames,
     evaluated by different functions (`grid_emit_guard.require_emit` vs
     `grid_emit_guard.require_phase_grids_cli`), and a CLI wiring only the
     first would silently miss the per-phase promise until the plan is
     closed.

C4/F1 fix: `close` being correct in isolation was never
enough, because it was UNREACHABLE on the path that actually runs.
`write_verification`'s own `drive_lifecycle` (called by `VERIFY`, default
ON) auto-flips `plan.md` to `completed` the instant
`derive_plan_completion.completion_state()["complete"]` — which checks
NOTHING about review-decision or the phase-grid gate — so `cook close` was
often never invoked at all by the time the plan already *looked* done. Two
changes close this, together:

  1. `CLOSE` itself now enforces `completion_state(...)["complete"]` AND
     a review-decision verdict of PASS BEFORE even reaching the phase-grid
     gate — so calling `close` directly (F1's exact repro: 0/2 phases, no
     review) refuses, regardless of grid mode.
  2. `NEXT`/`STATUS` no longer trust a `completed`/`in_progress`
     frontmatter status as authoritative (C0/F10) — they derive the
     state from artifacts every time and route through `_finalize_projection`
     regardless of what the frontmatter already claims. Because
     `_finalize_projection` runs its review-decision + phase-grid gates
     BEFORE ever reading `status`, THIS is the path that actually executes
     even when auto-finalize already flipped the frontmatter early — it
     reports `needs_review`/`needs_phase_grid_gate` honestly instead of
     `completed`.

Exit-code normalization (E24 — never pass a wrapped script's raw exit code
through): the four wrapped scripts this module calls for a MUTATING or
HARD-GATING action (`plan_graph`/`grid_emit_guard` internals via `preflight`;
`open_plan`; `close_plan` + `grid_emit_guard.require_phase_grids_cli` via
`close`; `write_verification` via `verify`) use THREE different raw exit-code
conventions among themselves (2 for a plan_graph/grid_emit_guard hard-stop, 1
for an open_plan/close_plan refusal, a SystemExit(1|2) from write_verification's
own `_die()`). Every one of those is mapped HERE to hs-run's own contract
(`EXIT_DONE`/`EXIT_NEEDS_MODEL`/`EXIT_NEEDS_STEP`/`EXIT_BROKEN` — mirroring
`hs_run.py`'s tiers, IMPORTED from it — the per-module copies this comment
once described were retired after ten of them drifted). A wrapped-script
failure NEVER surfaces as hs-run exit 0.

D10/L3 (the six-rung exit ladder, applied per-STATE not per-verb):
a prior version of this module gave EVERY `next`/`status` projection
EXIT_DONE(0) on the theory that "honestly reporting a block IS a successful
CLI run". A red-team + revalidation pass proved that
conflates two different machine-readable facts — "the CLI ran fine" (stdout
JSON, always valid) and "is there more work" (the actual exit code a
`while` loop keys on) — so a driver never learns the difference between
"done" and "0/2 phases passed, go spawn a developer". The table below is now
the single source of truth for every state this module can return:

  EXIT_DONE (0)         — no_plan (nothing exists yet), not_approved (nothing
                          for COOK to act on — a `plan` step must run first),
                          completed, closed/already_closed.
  EXIT_NEEDS_MODEL (10) — phase_needs_work, phase_failed, needs_verification,
                          declared_check_broken (a declared check's run could
                          not measure; its reason, not a rerun),
                          needs_review, needs_reopen_verify (a reopened
                          plan's named phase owes its verdict again),
                          waiting_on_model (`prompt`), `record developer`'s
                          `recorded` (a model must read the report and choose
                          the verdict).
  EXIT_NEEDS_STEP (11)  — needs_open, ok (`preflight`), opened/already_open,
                          verified, phase_needs_reverify, needs_phase_grid_gate,
                          needs_declared_check (`hs-code check run` / `test run`),
                          reopened (`open --reason`),
                          needs_close, `record <non-developer>`'s `recorded`
                          (its own follow-up is just `cook next` again) — a
                          deterministic CLI call remains; no judgment needed.
  EXIT_BROKEN (2)        — no_plan (`preflight`/`open`/`verify`/`close` — the
                          verb's ENTIRE job IS the gate/mutation, so an
                          unresolved plan means that job could not run at
                          all, unlike `next`/`status`'s idle "nothing to do
                          yet"), preflight_blocked, not_startable,
                          no_plan_graph, no_phase_resolved, unknown_phase,
                          phase_needs_post_artifact, state_conflict,
                          reopen_not_completed, and every `error`/`*_failed`
                          state.
  EXIT_REFUSED (3)       — the mutating verbs declining on purpose:
                          approval_refused, reopen_needs_reason, and close's
                          phases_incomplete, reopen_verify_owed,
                          review_missing, blocked_grid_phase_gate,
                          suite_debt_unpaid.

Hard Cases — NOT disk-provable, named explicitly rather than assumed:
whether a step was actually delegated through a real `Task(subagent_type=...)`
spawn, vs. the model doing the work inline and reporting success, is a
TRANSCRIPT fact — `cook/SKILL.md:90`'s own admission ("Task-tool calls = 0 ⇒
INCOMPLETE") is unverifiable from disk. This module never claims to have
proven a delegation happened; `RECORD` and `_finalize_projection` both
attach an explicit warning naming this limitation rather than silently
pretending the two-beat `prompt`/`record` pair is itself proof.
"""
from __future__ import annotations

import os
import re
import shlex
import sys

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import artifact_check  # noqa: E402
import derive_plan_completion  # noqa: E402
import repo_rules  # noqa: E402 — a repo's own rules, routed by what the phase creates
import frontmatter_parser  # noqa: E402
import defer_suite_carry  # noqa: E402
import graph_test_scope  # noqa: E402
import grid_emit_guard  # noqa: E402
import grid_flag_carry  # noqa: E402
import hs_run_next_command  # noqa: E402
import hs_run_scoped_tests  # noqa: E402
import plan_evidence_paths  # noqa: E402
import plan_family  # noqa: E402
import review_seen  # noqa: E402
import plan_graph  # noqa: E402
import plan_layout_check  # noqa: E402
import plan_status  # noqa: E402
import secret_scan_before_ship  # noqa: E402
import write_verification  # noqa: E402

# The one git-hardening posture, imported rather than redefined (`hs_run_
# security.py`'s own pattern, and `test_hs_run_git_hardening.py`'s own gate):
# `-c core.fsmonitor=false` in argv, `GIT_CONFIG_NOSYSTEM=1` in env, so a
# repo's own (or a machine-wide) git config cannot name a program git then
# executes. Every git subprocess call this module makes runs against a
# worktree the harness does not own, so every one of them carries both.
_GIT_SAFE = secret_scan_before_ship.GIT_SAFE_CONFIG
_git_env = secret_scan_before_ship.git_safe_env
from close_plan import close_plan as _close_plan  # noqa: E402
from open_plan import open_plan as _open_plan  # noqa: E402
import phase_commits_log  # noqa: E402

# hs_run.py's own four-tier contract, IMPORTED rather than
# re-declared: the per-module copies drifted, so hs_run owns the ladder and
# every domain reads it from there.
# The six-rung ladder (D10), IMPORTED rather than re-declared.
# Ten modules used to hand-copy these four constants and had already drifted:
# rung 11 carried two different names across the tree and two modules declared
# the same constant twice. `hs_run` owns the ladder as a closed `Exit` IntEnum;
# both historical spellings of rung 11 are bound to the same member there, so
# this import changes no name any caller in this file uses.
from hs_family.cli import Command  # noqa: E402
import hs_run  # noqa: E402
from hs_run import (EXIT_BROKEN, EXIT_DONE, EXIT_NEEDS_MODEL,  # noqa: E402
                     EXIT_NEEDS_STEP, EXIT_REFUSED)

_ROLE_PROMPTS = {
    "developer": (
        "Spawn Task(subagent_type='hs:developer') for phase {phase}: implement "
        "red->green per the phase file (test first, intentional fail, then make "
        "it pass). Tell it it is the delegate: it implements in place and spawns "
        "no developer of its own; if it asks cook for its instruction it runs "
        "`HARNESS_AGENT=developer hs-run cook prompt developer{plan_arg} --phase "
        "{phase}`. When it returns, run: hs-run cook record developer{plan_arg} "
        "--phase {phase} --out <file with its report>, then hs-run cook "
        "verify{plan_arg} --phase {phase} --verdict <PASS|BLOCKED> --check "
        "<name>:<status> ..."
    ),
    "tester": (
        "Spawn Task(subagent_type='hs:tester') to run the full suite and report "
        "verdict + checks[]. Persist the result via hs-run cook verify (this "
        "module cannot prove from disk that the run went through the Task tool "
        "rather than inline — see Hard Case #1 in this module's header)."
    ),
    "code-reviewer": (
        "Spawn Task(subagent_type='hs:code-reviewer') to review the diff. It MUST "
        "record the verdict through harness/scripts/write_review_decision.py with "
        "--rounds-run <N> (the round it just ran), not by writing the file itself: "
        "the flag is what keeps a per-round review-decision-r<N>.json, so an earlier "
        "BLOCKED survives the round that clears it — the writer REFUSES without it "
        "(exit 2, nothing written). Verdict is PASS/PASS_WITH_RISK/BLOCKED; PASS and "
        "PASS_WITH_RISK both let the plan close, and PASS_WITH_RISK's rationale is "
        "carried into the closed record."
    ),
    "docs-manager": (
        "Spawn Task(subagent_type='hs:docs-manager') to sync project docs "
        "against the finished plan."
    ),
    "git-manager": (
        "Spawn Task(subagent_type='hs:git-manager') to stage + commit the "
        "plan's changes with a conventional commit."
    ),
}

# NARROWED, not retired. `record` no longer needs this: a `run_id` chain (the CLI
# stamps one per invocation -> hook_dispatch resolves the session's latest ->
# the PreToolUse(Agent|Task) hook records it on the spawn) lets `_attest_spawn`
# check whether a spawn of the asked-for role landed against the matching
# `prompt` call, so `record` reports `spawn_attested` and drops this warning on
# a run it could actually verify. Keeping a caveat that fires unconditionally is
# how a reader learns to skip it — and then it is not read on the run that
# matters.
#
# The states BELOW still carry it, and correctly: Step 4 (test) has no
# `prompt`/`record` pair to attest against, so for that one the sentence is
# still simply true.
_HARD_CASE_WARNING = (
    "cannot verify from disk that this step actually went through a real "
    "Task(subagent_type=...) spawn rather than the model doing the work "
    "inline and reporting success — that is a session-transcript fact, not "
    "a disk fact (Hard Case #1; "
    "cook/SKILL.md:90's own admission: \"Task-tool calls = 0 ⇒ "
    "INCOMPLETE\" is a transcript check). Treat a `record` as an "
    "attestation, never as proof."
)

# The verdicts `close` will finish on. PASS_WITH_RISK is a first-class outcome of the
# review writer (write_review_decision._VERDICTS) and of phase verification
# (`_phase_substate` below), but close demanded the literal string "PASS" — so a review
# that named its residual risk honestly wedged the plan forever, while a review that
# said nothing sailed through. That is backwards pressure on the reviewer. BLOCKED stays
# out: this widens the door, it does not remove it. The writer and this tuple have no
# shared import, so test_hs_run_cook.py pins them to each other.
#
# BOUNDARY — do not copy this tuple into artifact_check.py. That module's `_pass_only`
# set demands exactly PASS for review-decision at a HARD SHIP STAGE (push/pr/merge), on a
# recorded ruling that a soft self-accept is not a ship license. The two gates answer
# different questions and must keep different answers: finishing a plan records that the
# work is done and what risk it carries (`_closed_warnings`), while shipping asks whether
# the risk is acceptable to release. A plan can close carrying risk and still be unable
# to ship — that is the intended shape, not a drift to reconcile.
_CLOSEABLE_VERDICTS = ("PASS", "PASS_WITH_RISK")

# What a TERMINAL state has to keep saying. Step 4 is a delegation nothing on disk can
# confirm, and the states that warn about it are the ones still in flight — so the
# permanent record, the one read months later, was the only one that looked fully checked.
_CLOSED_ATTESTATION_WARNING = (
    "closed on attestation, not on proof: Step 4 (test) delegation was never "
    "verifiable from disk — " + _HARD_CASE_WARNING
)


def _closed_warnings(rec) -> list:
    """Warnings the closed/completed states carry. Never empty by construction: an
    empty list on a finished plan reads as "every obligation was checked", and two of
    them never were. A risk-carrying verdict adds its rationale, so the residual risk
    travels with the record instead of only living in the review artifact."""
    out = [_CLOSED_ATTESTATION_WARNING]
    if isinstance(rec, dict) and rec.get("verdict") == "PASS_WITH_RISK":
        out.append("review-decision verdict was PASS_WITH_RISK — this plan closed "
                   "carrying reviewer-noted risk: %s"
                   % (rec.get("rationale") or "(no rationale recorded)"))
    return out


# ------------------------------------------------------------- plan resolve

def _plan_dirs(root: Path) -> list:
    """Every `plans/<id>-<slug>/` directory carrying a `plan.md` (unchanged
    since the first cut — the one disk signal guaranteed present the moment
    `scaffold.py plan` has run)."""
    plans_dir = Path(root) / "plans"
    if not plans_dir.is_dir():
        return []
    return sorted(p for p in plans_dir.iterdir()
                  if p.is_dir() and (p / "plan.md").is_file())


def _frontmatter_status(plan_dir: Path):
    """The plan's normalized frontmatter status, or None if unreadable/absent.
    Reuses frontmatter_parser + plan_status.normalize_status rather than a
    second regex — one parser, one fold table."""
    parsed = frontmatter_parser.parse_file(Path(plan_dir) / "plan.md")
    if not parsed.get("ok"):
        return None
    raw = (parsed.get("frontmatter") or {}).get("status")
    return plan_status.normalize_status(raw) if raw else None


def _resolve_plan_dir_verbose(root, explicit=None):
    """(plan_dir_or_None, miss_or_None).

    Same resolution `_resolve_plan_dir` used to do inline, but a miss now
    also names WHY (F6): `_resolve_plan_dir`'s old single `None`
    collapsed three different situations — an explicit `--plan` typo, zero
    startable candidates, and an AMBIGUOUS choice between multiple approved
    plans — into one generic message, which actively misdirected (F6's
    reproduced examples: an in_progress plan on disk reported as "none is
    in_progress"; two approved plans reported as "none is
    approved-and-unopened"). `miss["kind"]` is one of:
      - "explicit_not_found": the `--plan` path given does not exist.
      - "ambiguous_in_progress": >1 plan is in_progress at once and nothing
        named one — checked BEFORE the approved fallback, so a lone approved
        plan never wins over work already in flight.
      - "ambiguous_approved": >1 plan is approved-and-unopened at once.
      - "no_candidates": nothing in_progress, uniquely approved, or (as a
        last resort) freshly completed.

    Resolution ORDER: explicit wins outright; else
    an in_progress plan (`artifact_check.resolve_active_plan`) wins; else
    several in_progress plans are a miss; else
    exactly one approved-and-unopened plan; else the newest just-closed plan
    (mirrors the ship-gate's own allow_completed carve-out). Only when NONE
    of those succeed do we decide which miss reason to report."""
    root = Path(root)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = root / p
        if p.is_dir():
            return p, None
        # A file INSIDE the plan dir names that plan — `--plan <dir>/plan.md` is the
        # spelling the approved-plan envelope itself hands the operator, and the plan
        # domain already resolves it (`hs_run_plan._resolve_plan_arg`). Testing
        # `is_dir()` alone sent the file spelling to `explicit_not_found`, where the
        # fallback scan then answered about a DIFFERENT plan — `not_approved` on a plan
        # that IS approved. Resolving to the parent keeps one answer per plan.
        if p.is_file():
            # Only a file whose parent actually IS a plan dir. A phase file under
            # `phases/` has a parent that is not one, and returning it resolved to a
            # directory with no plan.md — reported downstream as `not_startable`, a
            # sentence about the plan's readiness for a path that names no plan at all.
            # `plan_approval.py` answers this same input by naming the miss; matching it
            # keeps one CLI with one answer.
            if (p.parent / "plan.md").is_file():
                return p.parent, None
            return None, {"kind": "explicit_not_found", "path": str(p)}
        return None, {"kind": "explicit_not_found", "path": str(p)}

    env_raw = os.environ.get("HARNESS_ACTIVE_PLAN")
    active = artifact_check.resolve_active_plan(str(root))
    if active is not None:
        return active, None
    if env_raw:
        # Named, but split: hand it to the caller, whose door refuses it as split —
        # "does not resolve to a plan" would name the wrong reason.
        named = _env_named_split(root, env_raw)
        if named is not None:
            return named, None
        # R15: HARNESS_ACTIVE_PLAN was explicitly SET but
        # resolve_active_plan's own fail-closed contract rejected it (it
        # names something outside <root>/plans/, or nothing at all —
        # artifact_check.py:186-189's own docstring calls this deliberate).
        # That refusal must not be silently overridden by falling through to
        # the disk-scan fallback below and opening a DIFFERENT plan the
        # operator never named — the operator's explicit intent failed, and
        # guessing a substitute is worse than refusing.
        return None, {"kind": "active_plan_rejected", "value": env_raw}

    # `resolve_active_plan` refuses to pick between several in_progress plans. That
    # refusal must surface as what it is: falling through reported "no ... in_progress
    # plan found" with three on disk, and a lone approved plan won over all of them.
    in_progress = [d for d in _plan_dirs(root) if _frontmatter_status(d) == "in_progress"
                   and plan_family.split_view(d) is None]
    if len(in_progress) > 1:
        return None, {"kind": "ambiguous_in_progress",
                      "candidates": [str(d) for d in in_progress]}

    # A split plan stays `approved` for good and is never cooked: never a candidate.
    approved = [d for d in _plan_dirs(root) if _frontmatter_status(d) == "approved"
                and plan_family.split_view(d) is None]
    if len(approved) == 1:
        return approved[0], None

    completed = artifact_check.resolve_active_plan(str(root), allow_completed=True)
    if completed is not None:
        return completed, None

    if len(approved) > 1:
        return None, {"kind": "ambiguous_approved",
                       "candidates": [str(d) for d in approved]}
    return None, {"kind": "no_candidates"}


def _env_named_split(root: Path, raw: str) -> "Path | None":
    """The split plan HARNESS_ACTIVE_PLAN names under <root>/plans/, or None."""
    plans = (Path(root) / "plans").resolve()
    for cand in ((Path(raw),) if Path(raw).is_absolute() else (plans / raw, Path(root) / raw)):
        try:
            cand.resolve().relative_to(plans)
        except ValueError:
            continue
        if (cand / "plan.md").is_file() and plan_family.split_view(cand) is not None:
            return cand
    return None


def _resolve_plan_dir(root: Path, explicit=None):
    """Back-compat thin wrapper over `_resolve_plan_dir_verbose` — most
    callers only need the path, not the miss reason."""
    plan_dir, _miss = _resolve_plan_dir_verbose(root, explicit=explicit)
    return plan_dir


def _ambiguous_in_progress_message(miss) -> str:
    return ("%d plans are in_progress at once (%s) — ambiguous which one this "
            "call means; pass --plan <plan dir> (or set HARNESS_ACTIVE_PLAN)"
            % (len(miss["candidates"]), ", ".join(miss["candidates"])))


# The one help line every cook verb's `--plan` carries, so the rule for when the
# flag is mandatory is stated where a caller looks for it, not only in an error.
_PLAN_HELP = ("plan dir, or its plan.md. Required when more than one plan is "
              "in_progress (or more than one is approved-and-unopened); "
              "otherwise the single active plan is used")


def _no_plan_message(miss) -> str:
    """The accurate, reason-specific message for a MUTATING/gate verb
    (`preflight`/`open`/`verify`/`close`) whose entire job could not even
    start because no plan resolved (F6/F8). These verbs all
    report state `no_plan` at EXIT_BROKEN uniformly (D10/L3's "no_plan phải
    trả MỘT con số duy nhất trong toàn cook") — only the message differs by
    the real reason."""
    if not miss:
        return "no approved-and-unopened or in_progress plan found"
    kind = miss["kind"]
    if kind == "ambiguous_in_progress":
        return _ambiguous_in_progress_message(miss)
    if kind == "explicit_not_found":
        return "--plan %s does not exist" % miss["path"]
    if kind == "active_plan_rejected":
        return ("HARNESS_ACTIVE_PLAN=%r does not resolve to a plan under "
                 "this project's plans/ dir — refusing to silently cook a "
                 "DIFFERENT plan instead; fix or unset HARNESS_ACTIVE_PLAN, "
                 "or pass --plan explicitly" % miss["value"])
    if kind == "ambiguous_approved":
        return ("%d plan(s) are approved-and-unopened at once (%s) — "
                 "ambiguous which to cook; pass --plan explicitly"
                 % (len(miss["candidates"]), ", ".join(miss["candidates"])))
    return "no approved-and-unopened or in_progress plan found"


def _not_approved_message(miss, dirs) -> str:
    """The `next`/`status` (pure projection) equivalent of `_no_plan_message`
    — same reason-distinguishing improvement, but `next`'s own "nothing for
    cook to act on yet" state stays EXIT_DONE (a projection honestly
    reporting an idle state is a complete, successful answer), unlike a
    mutating verb whose job failed to
    even run."""
    if not miss:
        return ("%d plan dir(s) found but none is approved-and-unopened or "
                 "in_progress — approve a plan first" % len(dirs))
    kind = miss["kind"]
    if kind == "ambiguous_in_progress":
        return _ambiguous_in_progress_message(miss)
    if kind == "explicit_not_found":
        return "no plan at %s" % miss["path"]
    if kind == "active_plan_rejected":
        return ("HARNESS_ACTIVE_PLAN=%r does not resolve to a plan under "
                 "this project's plans/ dir — refusing to silently cook a "
                 "DIFFERENT plan instead; fix or unset HARNESS_ACTIVE_PLAN, "
                 "or pass --plan explicitly" % miss["value"])
    if kind == "ambiguous_approved":
        return ("%d plan(s) are approved-and-unopened at once (%s) — "
                 "ambiguous; pass --plan explicitly"
                 % (len(miss["candidates"]), ", ".join(miss["candidates"])))
    return ("%d plan dir(s) found but none is approved-and-unopened or "
             "in_progress — approve a plan first" % len(dirs))


def _approval_gate_reason(plan_dir: Path, root) -> "str | None":
    """None when this plan may proceed past the approval gate (`preflight`/
    `open`); else the reason it must not.

    R2: C1 moved into cook. Before this fix,
    `OPEN`/`PREFLIGHT` decided a plan was safe to act on PURELY from
    `_frontmatter_status(d) == "approved"` (the old `hs_run_cook.py:288`
    bug) and never read the plan-approval ARTIFACT at all — a REJECTED
    verdict, a drifted approval, or a foreign plan's approval copied in all
    still let `cook open` write `status: in_progress` to disk. The plan
    domain already delegates this judgment correctly to the real owner,
    `artifact_check._check_plan_approval` (its declared owner)
    — this mirrors that exactly, reading the artifact through the SAME
    shared, containment-safe reader (`artifact_check._load_artifact`, used
    elsewhere in this module for `review-decision`) rather than a second
    hand-rolled parser, which would only reopen the F5/C5-shaped risk of a
    THIRD frontmatter/artifact reader disagreeing with the other two.

    Refuses in exactly ONE situation: an approval artifact IS present but
    does not currently validate — bad verdict, wrong plan binding, drifted
    plan_hash, or unreadable/forged (any `_load_artifact` problem other than
    "missing"). This mirrors the plan domain's OWN model precisely: `_project_
    inner`'s "approved" branch (hs_run_plan.py:493-499) is keyed ENTIRELY on
    the approval ARTIFACT's presence+validity, never on `status:` at all —
    `status:` is not even consulted for that decision there. A plan with NO
    approval artifact at all is untouched here regardless of what `status:`
    claims, for the same reason: `open_plan.py`'s own `_STARTABLE` set
    intentionally includes `pending`/`draft` as a solo/back-compat path with
    no approval workflow expected (see its module docstring, and
    `artifact_check.py`'s own: "Self-approval is deliberate anti-drift
    discipline, not an anti-fraud check") — inventing a NEW "status:
    approved requires a backing artifact" rule here would go beyond what
    R2's own three reproduced attacks needed (all three had an artifact
    PRESENT, just invalid) and would conflict with the domain-agnostic
    conformance suite's own cook fixture, which legitimately builds a
    `status: approved` plan with no approval artifact at all
    (`test_hs_run_contract_conformance.py::_cook_write_plan` — the SSOT for
    this contract, goal D12)."""
    rec, problem = artifact_check._load_artifact(Path(plan_dir), "plan-approval")
    if rec is None:
        if problem != "missing":
            return "plan-approval artifact is present but %s" % problem
        return None
    return artifact_check._check_plan_approval(Path(plan_dir), rec, root)


def _phase_order(graph: dict) -> list:
    """A deterministic phase visiting order: plan_graph's own topological
    batches (each batch sorted by the numbers in its ids, 2 before 10), flattened.
    For a plan with no edges at all every node lands in one batch — reuses
    `plan_graph.find_parallel_batches` rather than re-deriving order here
    (survival invariant #4)."""
    order = []
    for batch in plan_graph.find_parallel_batches(graph):
        order.extend(batch)
    return order


# ------------------------------------------------------------ 0.5a-0.5c: preflight

# ONE table, kind -> class. Every reason `_run_preflight` can append goes
# through `_preflight_reason` below, which requires its kind to be a row
# here — a kind with no row raises at the point the reason would have been
# created, not later at whatever `_preflight_exit_code` does with an
# unclassified value. `test_every_preflight_reason_kind_has_a_row_in_the_
# class_table` AST-walks `_run_preflight` for every kind literal it passes
# and fails if this table and the source ever disagree either direction.
#
#   "broken"  — the CLI genuinely cannot answer: a malformed/unreadable
#               artifact, a factual claim in the plan that is false. No
#               amount of "yes I meant it" clears this; something must be
#               FIXED on disk.
#   "refused" — the engine COULD proceed and declines on purpose, per a
#               policy this repo enforces deliberately (an unvalidated
#               approval, an opted-into gate not yet satisfied).
_REASON_CLASS = {
    "approval": "refused",
    "split": "refused",
    "graph_malformed": "broken",
    "graph_missing_post": "broken",
    "graph_bad_prefix": "broken",
    "graph_phase_file": "broken",
    "graph_cross_plan": "broken",
    "evidence_dangling": "broken",
    "grid_macro_missing": "refused",
}


def _preflight_reason(kind: str, text: str, *, reasons: list, kinds: list) -> None:
    """Append one preflight reason AND its machine-classifiable kind, in the
    ONE place every reason is created — `reasons` (user-facing text,
    `next_action`/`warnings` read it verbatim) and `kinds` (what
    `_preflight_exit_code` classifies) can never drift apart because both
    come from this single call, never two independent lists built by hand."""
    if kind not in _REASON_CLASS:
        raise KeyError(
            "preflight reason kind %r has no row in _REASON_CLASS — classify "
            "it broken or refused before shipping" % kind)
    reasons.append(text)
    kinds.append(kind)


def _preflight_exit_code(reason_kinds):
    """The heaviest reason decides: any BROKEN-kind reason present forces
    EXIT_BROKEN even alongside refusal-kind reasons — a caller cannot safely
    treat a plan that is ALSO structurally broken as merely refused. Only
    when every reason present is refusal-kind does the block report
    EXIT_REFUSED. Callers only reach here when `reason_kinds` is non-empty
    (i.e. `blocked` is already True)."""
    classes = {_REASON_CLASS[k] for k in reason_kinds}
    return EXIT_BROKEN if "broken" in classes else EXIT_REFUSED


def _split_reason(plan_dir: Path) -> "str | None":
    """Why this plan is never cooked, or None: the human chose to split it, and its
    work is cooked in the child plans."""
    split = plan_family.split_view(plan_dir)
    if split is None:
        return None
    owed = plan_family.split_sentence(split, plan_dir)
    return ("plan-size (0.5x): this plan was split (the human's size decision) and is "
            "never cooked itself — cook each child plan instead%s%s" % (
                ": " + ", ".join(str(k) for k in split["children"]) if split["children"] else "",
                "; the split is not finished: " + owed if owed else ""))


def _split_refusal(plan_dir: Path) -> "dict | None":
    """The answer every door that would start or continue cooking gives a split plan,
    whatever its status — the preflight alone runs only on an approved one."""
    why = _split_reason(plan_dir)
    if why is None:
        return None
    return hs_run.StepResult(
        state="preflight_blocked", next_action=why,
        artifacts=[str(plan_dir)], warnings=[why],
        exit_code=_preflight_exit_code(["split"]),
    ).as_dict()


def _run_preflight(plan_dir: Path, root=None) -> dict:
    """The ONE-TIME, plan-level preflight (cook Steps 0.5a-0.5c): the
    plan-approval gate (see `_approval_gate_reason`), the
    plan-graph sidecar must exist with every node's `post:` obligation
    declared (`plan_graph.py --require`-equivalent, unconditional per node
    regardless of --require in the wrapped script itself — see
    `plan_graph.py`'s own `_main`), that `post:` obligation must be
    resolvable at all (F4 — see the bad-prefix check below), and — ONLY when
    grid mode is carried (`grid_flag_carry.resolve_grid_carry`) — the MACRO
    `coverage-grid.json` artifact must satisfy `grid_emit_guard.require_emit`
    (0.5c). Also folds `defer_suite_carry.resolve_defer_suite_carry` — purely
    advisory, never appended to `reasons[]` (deferring the suite is never
    itself a block).

    Returns {"blocked": bool, "reasons": [str, ...], "reason_kinds": [str, ...],
    "grid_carry": {...}, "defer_suite_carry": {...}} — never raises; a
    malformed plan-graph is a reason, not a crash. `reason_kinds` is what
    `_preflight_exit_code` (module-level, above) classifies broken/refused
    against `_REASON_CLASS`; `reasons` stays the complete, human-readable
    text every caller already read."""
    reasons: list[str] = []
    kinds: list[str] = []
    approval_reason = _approval_gate_reason(plan_dir, root)
    if approval_reason:
        _preflight_reason("approval", "plan-approval (0.5x): %s" % approval_reason,
                          reasons=reasons, kinds=kinds)
    # The human chose to split this plan: its work is cooked in the child plans,
    # never here, whatever phases it still holds.
    split = _split_reason(plan_dir)
    if split:
        _preflight_reason("split", split, reasons=reasons, kinds=kinds)
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        _preflight_reason("graph_malformed", "plan-graph (0.5a): %s" % graph["error"],
                          reasons=reasons, kinds=kinds)
    else:
        missing = plan_graph.find_missing_post(graph)
        if missing:
            _preflight_reason(
                "graph_missing_post",
                "plan-graph (0.5a): node(s) missing a declared 'post:' artifact "
                "obligation: %s" % ", ".join(missing),
                reasons=reasons, kinds=kinds)
        # F4: a post: entry authored WITH the "artifacts/" prefix
        # double-resolves under <plan>/artifacts/artifacts/... and can never
        # satisfy derive_plan_completion — cook then falls into "go implement
        # it again" forever for a phase that already verified PASS. Catch the
        # authoring mistake here, before phase 1 ever starts, rather than
        # mid-cook (`_phase_substate` below is the defense-in-depth fallback
        # for a plan-graph edited to introduce this AFTER preflight already
        # ran).
        bad_post = plan_graph.find_post_problems(graph)
        if bad_post:
            _preflight_reason(
                "graph_bad_prefix",
                "plan-graph (0.5a): post: entries that never resolve: %s"
                % "; ".join(bad_post),
                reasons=reasons, kinds=kinds)
        # A declared `file:` is what `prompt developer` hands the implementer;
        # one pointing nowhere would send it to read a phase that is not there.
        phase_file_problems = plan_graph.find_phase_file_problems(graph, plan_dir)
        if phase_file_problems:
            _preflight_reason(
                "graph_phase_file",
                "plan-graph (0.5a): %s" % "; ".join(p["msg"] for p in phase_file_problems),
                reasons=reasons, kinds=kinds)
        # An edge on a sibling plan's phase that cannot hold would leave its phase
        # waiting forever, or never waiting at all.
        cross = plan_graph.find_cross_plan_problems(graph, plan_dir)
        if cross:
            _preflight_reason("graph_cross_plan", "plan-graph (0.5a): %s" % "; ".join(cross),
                              reasons=reasons, kinds=kinds)

    # 0.5d — an evidence row that points at nothing. BLOCKING, unlike its
    # advisory neighbour `_layout_warnings`, because the two make different
    # claims: a misplaced phase file is drift a human should look at, while an
    # `[OBSERVED]` row citing a file that does not exist is a statement in the
    # plan that is FALSE, and every later phase builds on it. Measured here: a
    # row cited an artifact that had never existed under any name, and every
    # gate stayed green because none read what a row POINTS AT.
    #
    # Blocking is affordable only because the scope is narrow — evidence-marked
    # lines and nothing else. Over every markdown under plans/ that is one
    # citation; an unscoped check would judge forty and be wrong on six.
    for finding in plan_evidence_paths.dangling_evidence_paths(plan_dir, repo=root):
        _preflight_reason("evidence_dangling", "plan-evidence (0.5d): %s" % finding,
                          reasons=reasons, kinds=kinds)

    carry = grid_flag_carry.resolve_grid_carry(plan_dir)
    if carry["carried"]:
        msg, code = grid_emit_guard.require_emit(plan_dir)
        if code != 0:
            _preflight_reason("grid_macro_missing", "grid macro presence (0.5c): %s" % msg,
                              reasons=reasons, kinds=kinds)

    # Advisory only, like grid_carry above: NEVER appended to reasons[] — the
    # phase's own decision is that deferring the suite must never itself be
    # gated (that would defeat the point of the flag).
    defer_carry = defer_suite_carry.resolve_defer_suite_carry(plan_dir)

    return PreflightResult(reasons=reasons, reason_kinds=kinds, grid_carry=carry,
                           defer_suite_carry=defer_carry,
                           warnings=_layout_warnings(plan_dir) + [
                               "plan-evidence (0.5d): %s" % w for w in
                               plan_evidence_paths.sibling_evidence_paths(plan_dir, repo=root)]
                           ).as_dict()


@_attrs.define(frozen=True, kw_only=True)
class PreflightResult:
    """What cook's preflight found, declared rather than assembled.

    `blocked` is derived from `reasons` on purpose: the two disagreeing -- a
    non-empty reason list with `blocked: False` -- is the exact "reports done
    when it meant broken" shape, and deriving it makes that disagreement
    unrepresentable instead of merely unlikely."""

    reasons: list = _attrs.field(factory=list)
    reason_kinds: list = _attrs.field(factory=list)
    grid_carry: dict = _attrs.field(factory=dict)
    defer_suite_carry: dict = _attrs.field(factory=dict)
    warnings: list = _attrs.field(factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.reasons)

    def as_dict(self) -> dict:
        return {"blocked": self.blocked, "reasons": list(self.reasons),
                "reason_kinds": list(self.reason_kinds),
                "grid_carry": self.grid_carry,
                "defer_suite_carry": self.defer_suite_carry,
                "warnings": list(self.warnings)}


def _layout_warnings(plan_dir: Path) -> list:
    """Plan-layout drift, ADVISORY — `warnings`, never `reasons`.

    Kept out of `reasons` on purpose: `plan_layout_check` is exit-0 by
    construction, and a check that starts blocking work is a check someone
    switches off. The blocking guarantee stays where it already is, in
    `plan_approval`'s hash. What this adds is that the drift is now SAID by the
    verb rather than by a line of skill prose asking a model to remember to run
    a script — a model that forgets produces the same silence as a clean plan.

    Read-only and defensive: an unreadable plan dir yields no warning rather
    than taking preflight down, since the advisory is not what preflight is
    for."""
    try:
        return list(plan_layout_check.layout_warnings(plan_dir))
    except Exception:
        return []




def _cook_door(fn, *, report_only: bool = False):
    """Every cook verb that acts on a plan answers a split one first, with the same
    refusal — one door, so a verb added later cannot forget it. The plan-less pick
    never lands on a split plan (`_resolve_plan_dir_verbose`); a named one is
    refused here. `report_only` (status) says the same at exit 0: status always
    reports, never refuses."""
    def run(ns):
        plan_dir, _miss = _resolve_plan_dir_verbose(Path(ns.ctx.root),
                                                    explicit=getattr(ns, "plan", None))
        refused = _split_refusal(plan_dir) if plan_dir is not None else None
        if refused and report_only:
            refused["exit_code"] = EXIT_DONE
        return hs_run_next_command.finalize(refused) if refused else fn(ns)
    run.__name__ = getattr(fn, "__name__", "run")
    run.__doc__ = fn.__doc__
    return run


def _preflight(ns) -> dict:
    return hs_run_next_command.finalize(_cmd_preflight_impl(ns.ctx.root, plan=ns.plan))


PREFLIGHT = hs_run.verb(_cook_door(_preflight), options=("plan",), helps={"plan": _PLAN_HELP})


def _cmd_preflight_impl(root, *, plan=None) -> dict:
    """`hs-run cook preflight [--plan <dir>]` — the direct wrap of cook Steps
    0.5a-0.5c. Unlike `next`, this verb's whole job IS the gate action, so a
    genuine block normalizes to EXIT_BROKEN (E24) — never a silent exit 0.
    A cleared preflight still leaves the deterministic `cook open` step —
    EXIT_NEEDS_STEP, not EXIT_DONE (D10/L3)."""
    plan_dir, miss = _resolve_plan_dir_verbose(Path(root), explicit=plan)
    if plan_dir is None:
        return hs_run.StepResult(
            state="plan_unresolved",
            next_action=_no_plan_message(miss),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    pf = _run_preflight(plan_dir, root=root)
    if pf["blocked"]:
        return hs_run.StepResult(
            state="blocked",
            next_action="fix before opening: %s" % "; ".join(pf["reasons"]),
            artifacts=[str(plan_dir)],
            warnings=pf["reasons"] + pf["warnings"],
            exit_code=_preflight_exit_code(pf["reason_kinds"]),
        ).as_dict()
    return hs_run.StepResult(
        state="ok",
        next_action="preflight clear — open the plan: hs-run cook open %s" % plan_dir,
        # `--plan <dir>`, NOT the bare positional the sentence shows. `OPEN`
        # reads its target from `--plan` only, so the bare form names a
        # directory the verb never looks at.
        # That used to fail SILENTLY: the positional was ignored and the verb
        # re-resolved the active plan, reporting `no_plan` for a directory that
        # exists. It now REJECTS it — measured: `hs-run cook open /tmp/x` ->
        # "unrecognized arguments: /tmp/x", exit 2. The declared-flag parser is
        # what closed it; the old containment check could not, since it only
        # asks whether a command-shaped substring is present, never whether the
        # command would do what the sentence says.
        # `next_action` is left exactly as it was — fixing the human sentence is
        # a separate change with a separate reader.
        next_command="hs-run cook open --plan %s" % plan_dir,
        artifacts=[str(plan_dir)], warnings=pf["warnings"],
        extra={"grid_carried": pf["grid_carry"]["carried"],
               "defer_suite_carried": pf["defer_suite_carry"]["carried"]},
        exit_code=EXIT_NEEDS_STEP,
    ).as_dict()


# --------------------------------------------------------------- 0.6: open



def _open(ns) -> dict:
    return hs_run_next_command.finalize(_cmd_open_impl(
        ns.ctx.root, plan=ns.plan, reason=ns.reason, phases=ns.phase))


_OPEN_HELPS = {
    "plan": _PLAN_HELP,
    "reason": "reopen a completed plan, saying why (kept in its phase-commits log): "
              "it returns to in_progress, its review-decision moves aside so a fresh "
              "recorded review is owed, and close waits for every --phase verified "
              "again",
    "phase": "with --reason: phase(s) whose evidence went stale, comma-separated — "
             "each must be verified again before close"}
OPEN = hs_run.verb(_cook_door(_open), options=("plan", "reason", "phase"), helps=_OPEN_HELPS)


def _cmd_open_impl(root, *, plan=None, reason=None, phases=None) -> dict:
    """`hs-run cook open [--plan <dir>] [--reason <why> [--phase <id>,...]]` —
    wraps `open_plan.open_plan` (cook Step 0.6). On a completed plan it reopens
    instead (`_reopen`), and only with `--reason`: the flags on any other plan
    are a usage error, a completed plan without them a refusal. The raw script's own refusal is exit 1 (E24); here it is
    normalized to EXIT_BROKEN(2), never passed through and never reported as
    a silent success. A successful open still leaves the deterministic
    `cook next` step — EXIT_NEEDS_STEP (D10/L3).

    R2: before touching disk, this now runs the SAME
    plan-approval judgment `_run_preflight` runs (`_approval_gate_reason`,
    the real owner `artifact_check._check_plan_approval`) — a REJECTED
    verdict, a drifted approval, or a foreign plan's approval copied in all
    refuse here now, `approval_invalid` (mirrors the plan domain's own state
    name for the identical judgment, hs_run_plan.py:508)."""
    plan_dir, miss = _resolve_plan_dir_verbose(Path(root), explicit=plan)
    if plan_dir is None:
        return hs_run.StepResult(
            state="plan_unresolved",
            next_action=_no_plan_message(miss),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    status = _frontmatter_status(plan_dir)
    named = [p for p in re.split(r"[,\s]+", phases or "") if p]
    if (reason is not None or named) and status != "completed":
        return hs_run.StepResult(
            state="reopen_not_completed",
            next_action="--reason/--phase reopen a completed plan; %s is %s — "
                        "hs-run cook next --plan %s" % (plan_dir, status, plan_dir),
            artifacts=[str(plan_dir)], warnings=[], exit_code=EXIT_BROKEN).as_dict()
    if status == "completed" and not (reason or "").strip():
        return hs_run.StepResult(
            state="reopen_needs_reason",
            next_action="%s is completed; reopening it takes the reason its evidence "
                        "went stale: hs-run cook open --plan %s --reason <why> "
                        "[--phase <id>,<id>]" % (plan_dir, plan_dir),
            artifacts=[str(plan_dir)], warnings=[], exit_code=EXIT_REFUSED).as_dict()
    gate_reason = _approval_gate_reason(plan_dir, root)
    if gate_reason is not None:
        return hs_run.StepResult(
            # `approval_refused`, not `approval_invalid`. The projection verb in
            # the plan domain reports `approval_invalid` at rung 10 for the same
            # artifact; this is `open` refusing to mutate, which is a stop rather
            # than a step someone can take a model turn on.
            state="approval_refused",
            next_action="cook open refuses: %s" % gate_reason,
            artifacts=[str(plan_dir)],
            warnings=[gate_reason],
            exit_code=EXIT_REFUSED,
        ).as_dict()
    if status == "completed":
        return _reopen(plan_dir, root, reason.strip(), named)
    res = _open_plan(plan_dir, root=root)
    if not res.ok:
        return hs_run.StepResult(
            state="open_failed",
            next_action=res.message,
            artifacts=[str(plan_dir)],
            warnings=[res.message],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    next_cmd = "hs-run cook next --plan %s" % plan_dir
    snapshot_cmd, snapshot_note = _snapshot_heads_up(Path(root))
    if snapshot_cmd:
        return hs_run.StepResult(
            state="opened" if res.changed else "already_open",
            next_action="take the code-graph snapshot the first phase reads: %s — "
                        "then: %s" % (snapshot_cmd, next_cmd),
            next_command=snapshot_cmd,
            artifacts=[str(plan_dir)],
            warnings=[snapshot_note],
            exit_code=EXIT_NEEDS_STEP,
        ).as_dict()
    return hs_run.StepResult(
        state="opened" if res.changed else "already_open",
        next_action=next_cmd,
        next_command=next_cmd,
        artifacts=[str(plan_dir)],
        warnings=[snapshot_note] if snapshot_note else [],
        exit_code=EXIT_NEEDS_STEP,
        # Lands on the run ledger under this session: the prompt hook resolves
        # the active plan from it (hs_run.latest_opened_plan).
        extra={"plan": str(plan_dir)},
    ).as_dict()


@_attrs.define(frozen=True, kw_only=True)
class ReopenRecord:
    """The `kind: reopen` line: a completed plan taken back to in_progress, why, the
    phases owed a verify, and where each review-decision it retired now lives."""

    reason: str
    phases: list = _attrs.field(factory=list)
    from_status: str = "completed"
    retired: list = _attrs.field(factory=list)
    actor: str = ""
    ts: str = ""

    def as_dict(self) -> dict:
        return {"kind": "reopen", "reason": self.reason, "phases": list(self.phases),
                "from_status": self.from_status, "retired": list(self.retired),
                "actor": self.actor, "ts": self.ts}


def _reopen(plan_dir: Path, root, reason: str, phases: list) -> dict:
    """completed -> in_progress, after the approval gate `open` runs.

    Every review-decision on disk (each format) moves aside to
    `review-decision-retired-<ts>.<ext>`: it reviewed the plan as closed, and every
    reader of the canonical name — close, the hard-stage gate, fix — now finds none.
    The live `record code-reviewer` lines are superseded with it, and the ledger line
    lands before the flip: a flip with no line would close again on the old review.
    A flip that then fails leaves the line, and `next` already owes what it names;
    the same command again finishes the reopen."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    for phase in phases:
        why = _phase_refusal(plan_dir, phase, graph)
        if why:
            return _unknown_phase(plan_dir, why)
    import hook_runtime
    actor, ts = hook_runtime.resolve_actor(), write_verification._now_iso()
    retired = []
    try:
        for ext in artifact_check._ARTIFACT_EXTS:
            live = plan_dir / "artifacts" / ("review-decision%s" % ext)
            if live.is_file():
                aside = live.with_name("review-decision-retired-%s%s"
                                       % (re.sub(r"[^0-9]", "", ts), ext))
                os.replace(live, aside)
                retired.append(aside.name)
        _append_phase_commits_record(plan_dir, ReopenRecord(
            reason=reason, phases=list(dict.fromkeys(phases)), retired=retired,
            actor=actor, ts=ts).as_dict())
        for phase, recs in _live_records(plan_dir, "code-reviewer").items():
            if recs:
                _append_record_log_entry(plan_dir, SupersedeLogEntry(
                    role="code-reviewer", phase=phase, actor=actor, ts=ts).as_dict())
        res = plan_status.flip_status(plan_dir, allowed_from={"completed"},
                                      to="in_progress", error_on_other=True, root=root)
    except OSError as e:
        res = plan_status.Result(False, False, "%s: %s" % (type(e).__name__, e))
    if not res.ok:
        why = ("reopen did not finish (%s)%s — fix that and run the same command again"
               % (res.message, "; review-decision moved aside: %s" % ", ".join(retired)
                  if retired else ""))
        return hs_run.StepResult(state="open_failed", next_action=why,
                                 artifacts=[str(plan_dir)], warnings=[why],
                                 exit_code=EXIT_BROKEN).as_dict()
    nxt = "hs-run cook next --plan %s" % plan_dir
    return hs_run.StepResult(
        state="reopened",
        next_action=nxt,
        next_command=nxt,
        artifacts=[str(plan_dir), str(_phase_commits_path(plan_dir))],
        warnings=[],
        exit_code=EXIT_NEEDS_STEP,
        extra={"plan": str(plan_dir), "phases": list(dict.fromkeys(phases))},
    ).as_dict()


def _reverify_owed(plan_dir: Path) -> "tuple[str, list] | None":
    """(sentence, phases) while a reopen names a phase no later verify answered, else
    None — one predicate for `next`, `close` and the hard-stage gate."""
    line, owed = phase_commits_log.reverify_owed(plan_dir)
    if not owed or line is None:
        return None
    return ("plan reopened (%s): phase(s) %s must be verified again before its review "
            "and close — hs-run cook verify --plan %s --phase <id> %s (`--sha keep` "
            "names the commits a phase already recorded)"
            % (line.get("reason"), ", ".join(owed), plan_dir, _VERIFY_PLACEHOLDERS), owed)


def _snapshot_heads_up(root: Path):
    """(command, note) when the code-graph snapshot the per-phase step's code facts
    read is missing or stale for THIS worktree; (None, None) when it is fresh, or the
    repo never adopted a code map (nothing to miss).

    Reports, never takes it: writing another tool's store is not what opening a plan
    does. The facts it checks and the command it names come from the same registry
    rows the facts layer itself reads at `needs_implementation`, so the two cannot
    name different commands. Never raises — a heads-up is not a gate."""
    try:
        import hs_run_facts.engine as facts_engine
        _blk, row = hs_run._state_row(hs_run.load_step_routing(), "cook",
                                      "needs_implementation")
        declared = list((row or {}).get("facts") or [])
        if not declared or facts_engine.project_name(root) is None:
            return None, None
        base = facts_engine.store_base(root)
        if base is None:
            return None, None
        status = facts_engine.select_snapshot(root, base).status
        if status not in ("missing", "stale"):
            return None, None
        policy = hs_run.fact_providers_policy()
        sources = {(policy["providers"].get(f) or {}).get("source") for f in declared}
        raw = next((policy["sources"].get(s, {}).get("refresh_command")
                    for s in sorted(filter(None, sources))
                    if policy["sources"].get(s, {}).get("refresh_command")), None)
        what = ("no code-graph snapshot exists for this worktree yet (%s)" % base
                if status == "missing" else
                "the code-graph snapshot under %s is older than HEAD" % base)
        if not raw:
            return None, ("%s — the first phase's code facts (%s) will stop at "
                          "facts_stale, and no refresh command is declared for them"
                          % (what, ", ".join(declared)))
        cmd = facts_engine._build_refresh_command(str(raw), root)
        return cmd, ("%s — the first phase's code facts (%s) would stop at "
                     "facts_stale; take it now: %s" % (what, ", ".join(declared), cmd))
    except Exception:  # noqa: BLE001 — a heads-up never breaks the open it rides on
        return None, None


# ------------------------------------------------------------- 4: verify

# Read from the writer that enforces them, never retyped: a hand-copied list here
# would go on printing a verdict the writer rejects the day the two drift.
_VERDICT_CHOICES = tuple(sorted(write_verification._VERDICTS))
_CHECK_STATUSES = tuple(write_verification._LEGAL_STATUSES)
_VERIFY_PLACEHOLDERS = "--verdict <%s> --check <name>:<%s> ..." % (
    "|".join(_VERDICT_CHOICES), "|".join(_CHECK_STATUSES))


# The `--sha` token that names the phase's last recorded commits.
_SHA_KEEP = "keep"


def _verify_args(p) -> None:
    """`cook verify`'s declared flag set — the one its `--help` usage is printed
    from, so the two cannot drift apart. It mirrors `write_verification`'s own
    flags, which `_verify_argv` forwards."""
    p.add_argument("--plan", help=_PLAN_HELP)
    p.add_argument("--phase", help="the plan-graph.yaml node this verdict is for")
    p.add_argument("--verdict", help="one of: %s" % ", ".join(_VERDICT_CHOICES))
    # `--stage` is what a deferred-suite debt-payment run stamps itself with, and
    # this verb is the ONLY path that suppresses the auto-finalize status flip.
    # Undeclared, the reminder printed a command this parser rejected, so the
    # only way to pay the debt was the raw writer — around that suppression.
    p.add_argument("--stage", help="which run this was; %s marks the deferred "
                                   "full-suite run that pays a defer_suite debt"
                                   % DEBT_STAGE)
    p.add_argument("--from", dest="from_file",
                   help="a verification record file (or - for stdin) carrying the "
                        "verdict and checks; --verdict/--check/--stage are refused beside "
                        "it. --phase may accompany it and must match the file's `phase` "
                        "(it fills one the file leaves out)")
    p.add_argument("--check", action="append", default=[],
                   help="name:status[:result file], repeatable; name is a test-policy "
                        "test type, or <type>/<name> to keep the tool's own name (lint/ruff, "
                        "typecheck/mypy); status one of: %s. The result file (junit, sarif, "
                        "...: what the type's format reads) is copied under the plan's "
                        "artifacts/results/ -- the DoD gate reads it, not the status; a run "
                        "on another machine is fetched back first (`hs-rops fetch`)"
                        % ", ".join(_CHECK_STATUSES))
    p.add_argument("--sha", action="append", default=[],
                   help="commit,commit — the commits this phase made; replaces the range "
                        "from the phase's open mark (a phase cooked in another worktree, "
                        "or committed before it was marked). Repeatable; commas or "
                        "whitespace separate them; `keep` names the commits the phase's "
                        "last verify recorded. A merge, or a commit off HEAD's "
                        "first-parent line, is refused — "
                        "`git rev-list --first-parent --no-merges <start>..HEAD` — unless "
                        "the phase's last verify already recorded it (a lane merged "
                        "since); one another branch's first-parent line also holds is "
                        "recorded with a warning. The review gate still sizes the run up "
                        "to HEAD, whatever is named")


def _verify_argv(ns, plan_dir) -> list:
    """`write_verification.main`'s argv, rebuilt from the parsed flags: the plan
    dir positionally (it has no `--plan` selector), then every flag it knows."""
    argv = [str(plan_dir)]
    for flag, value in (("--from", ns.from_file), ("--phase", ns.phase),
                        ("--stage", ns.stage), ("--verdict", ns.verdict)):
        if value is not None:
            argv += [flag, value]
    for check in ns.check:
        argv += ["--check", check]
    return argv


def _verified_phase(ns, plan_dir) -> "str | None":
    """The phase this verify call recorded: its `--phase` (checked against the file by
    write_verification when `--from` is also given), else the `--from` file's own
    `phase`. The canonical record is the last resort only — a sibling tree verifying
    another phase of the same plan can overwrite it between the write and this read."""
    named = _named_phase(ns)
    if named:
        return named
    rec, _p = artifact_check._load_artifact(plan_dir, "verification")
    return rec.get("phase") if isinstance(rec, dict) else None


def _named_phase(ns) -> "str | None":
    """The phase the caller named before anything is written: `--phase`, else the
    `--from` file's own `phase`. None for stdin or a file naming none."""
    if ns.phase:
        return ns.phase
    if ns.from_file and ns.from_file != "-":
        try:
            rec, _err = artifact_check._parse_artifact_text(
                Path(ns.from_file).read_text(encoding="utf-8"), Path(ns.from_file))
        except (OSError, UnicodeDecodeError):
            rec = None
        if isinstance(rec, dict) and rec.get("phase"):
            return str(rec["phase"])
    return None


def _next_for(plan_dir, phase) -> str:
    return "hs-run cook next --plan %s%s" % (plan_dir, " --phase %s" % phase if phase else "")


def _verify(ns) -> dict:
    return hs_run_next_command.finalize(_cmd_verify_impl(ns, ns.ctx.root))


VERIFY = Command(add_args=_verify_args, run=_cook_door(_verify))


def _cmd_verify_impl(ns, root) -> dict:
    """`hs-run cook verify [--plan <dir>] --phase P --verdict V [--check n:s
    ...] | --from FILE` — wraps `write_verification.main` (cook Step 4).

    `write_verification.py` has no `--plan`/`--root` selector flag; it takes
    the plan dir POSITIONALLY. This verb resolves the active/explicit plan
    dir itself and prepends it, and normalizes write_verification's own
    SystemExit(1|2) (its `_die()` path) to EXIT_BROKEN — the raw code is
    never passed through (E24). A successful write still leaves the
    deterministic `cook next` step — EXIT_NEEDS_STEP (D10/L3).

    R1 fix (HIGHEST SEVERITY, and the original defect this whole
    module exists to close): `write_verification.main`'s own call to
    `vsnap.drive_lifecycle` (`write_verification.py:250-251`), gated on
    `verification_snapshot.auto_finalize_enabled()` (default ON), used to
    flip `plan.md` all the way to `completed` the instant every phase held a
    PASS snapshot — behind the review-decision + phase-grid gate
    `CLOSE` enforces. `close` never even ran; the plan was already
    "done", `verify`'s own response never said so ("state": "verified",
    nothing about closing), and a subsequent `review-decision.json` verdict
    BLOCKED then locked the plan in `completed` with NO verb able to reopen
    it (`open_plan` only accepts pending/approved/draft; `close_plan` only
    accepts in_progress; `cook open --reason` reopens one now, and owes the
    review again). Hand-reproduced in both the self-host shape and
    the exact global-install shape (HARNESS_BIN_ROOT set,
    CLAUDE_PROJECT_DIR pointing at a project distinct from the bin root —
    the self-host shape alone silently no-ops the flip, per the
    `test_default_env_*` tests' own docstrings, so it does not surface the
    bug at all without reproducing that exact shape).

    Fix: force HARNESS_AUTO_FINALIZE=0 for the duration of THIS in-process
    call only (the previous value is saved and restored in `finally` —
    never a global, process-wide disable). `CLOSE` remains the ONLY
    path in this module that can flip a plan to `completed`, and its own
    completion gate is unaffected by this env var (it does not read it) — so
    with this fix a plan can reach `completed` ONLY through a call that
    already enforces the review gate (a closeable review-decision or a
    measured small-diff waiver) + the phase-grid gate, which
    means the "locked in completed with no way back" failure mode this verb
    used to cause cannot arise via `hs-run cook verify` at all (proven by
    `test_verify_does_not_auto_finalize_plan_behind_review_gate` and the
    BLOCKED-review follow-up in the same test). The per-phase snapshot
    (verification-<phase>.json) still happens regardless — the kill-switch
    only suppresses the STATUS FLIP, matching `verification_snapshot.py`'s
    own comment at line 52.

    `-h`/`--help` never reaches write_verification's own
    `argparse.ArgumentParser(add_help=True)` — the family core answers it from
    `_verify_args` before this runs. Before that held, `--help` printed
    write_verification's usage to STDOUT (corrupting this process's JSON
    envelope) while `write_verification.main` still returned 0, so this verb
    reported `state: "verified"`: a lie, nothing was verified. Both stdout AND
    stderr are still captured around the wrapped call, as defense in depth
    against any other unexpected stdout write from write_verification."""
    plan_dir, miss = _resolve_plan_dir_verbose(Path(root), explicit=ns.plan)
    if plan_dir is None:
        return hs_run.StepResult(
            state="plan_unresolved",
            next_action=_no_plan_message(miss),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    import harness_checks
    gone = harness_checks.marks_removed(plan_dir)
    if gone:
        return hs_run.StepResult(
            state="verify_failed",
            next_action=harness_checks.marks_removed_reason(plan_dir, gone, _named_phase(ns) or "<phase>"),
            artifacts=[str(plan_dir)], warnings=[], exit_code=EXIT_BROKEN,
        ).as_dict()
    named = None
    sha_warnings: list = []
    if getattr(ns, "sha", None):
        phase = _named_phase(ns)
        tokens = [t for t in re.split(r"[,\s]+", " ".join(ns.sha)) if t]
        recorded = _phase_commit_shas(plan_dir, phase) if phase else []
        if _SHA_KEEP in tokens and not recorded:
            return hs_run.StepResult(
                state="verify_failed",
                next_action=("--sha %s names the commits phase %s's last verify recorded, "
                             "and it recorded none — name them: --sha <c>,<c>"
                             % (_SHA_KEEP, phase) if phase else
                             "--sha %s needs the phase: --phase <id>, or a --from file "
                             "naming its phase" % _SHA_KEEP),
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        tokens = [c for t in tokens for c in (recorded if t == _SHA_KEEP else [t])]
        named, unresolved = _resolve_named_commits(Path(root), " ".join(tokens))
        if unresolved:
            return hs_run.StepResult(
                state="verify_failed",
                next_action="--sha names no commit in %s: %s — nothing was recorded"
                            % (root, ", ".join(unresolved)),
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        named_off = _off_own_line(Path(root), named)
        # A commit the phase's own last verify recorded was its work on the line it
        # was made on; a lane merged since puts it off this HEAD's first-parent line.
        kept = [c for c, why in named_off if c in set(recorded) and why != _NOT_IN_HISTORY]
        off = [(c, why) for c, why in named_off if c not in set(kept)]
        if kept:
            sha_warnings.append(
                "phase %s: %d named commit(s) are off HEAD's first-parent line but its "
                "last verify already recorded them, so they stay its own: %s"
                % (phase, len(kept), _listed([c[:9] for c in kept], 5, "commit(s)")))
        if off:
            mark = _phase_open_mark(plan_dir, phase, Path(root)) if phase else None
            return hs_run.StepResult(
                state="verify_failed",
                next_action=(
                    "--sha names %d commit(s) that are not this branch's own work, so "
                    "nothing was recorded: %s. A phase's own commits sit on HEAD's "
                    "first-parent line and are no merge; `git rev-list --first-parent "
                    "--no-merges %s..HEAD` %s" % (
                        len(off), _listed(["%s (%s)" % (c[:9], why) for c, why in off], 5,
                                          "commit(s)"),
                        mark[:12] if mark else "<the phase's first commit>^",
                        _own_line_listing(Path(root), mark))
                    + _where_they_are_own(Path(root), plan_dir, phase,
                                          [(c, why) for c, why in named_off
                                           if why != "a merge"], kept)),
                artifacts=[str(plan_dir)],
                warnings=list(sha_warnings),
                exit_code=EXIT_REFUSED,
            ).as_dict()
    full_argv = _verify_argv(ns, plan_dir)
    # The phase's commits, taken before the write, scope the record's blast radius:
    # a merge-base diff is empty once the phase's lane merged.
    pre_phase = _named_phase(ns)
    commit_set = (_phase_commit_set(plan_dir, pre_phase, Path(root), named=named)
                  if pre_phase else None)
    if commit_set and commit_set[0]:
        full_argv += ["--commits", ",".join(commit_set[0])]

    import contextlib
    import io
    buf = io.StringIO()
    prev_auto_finalize = os.environ.get("HARNESS_AUTO_FINALIZE")
    os.environ["HARNESS_AUTO_FINALIZE"] = "0"
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = write_verification.main(full_argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        if prev_auto_finalize is None:
            os.environ.pop("HARNESS_AUTO_FINALIZE", None)
        else:
            os.environ["HARNESS_AUTO_FINALIZE"] = prev_auto_finalize
    err_text = buf.getvalue().strip()

    if code != 0:
        action = err_text or "write_verification failed (see warnings)"
        if ns.from_file is None and not (ns.phase and ns.verdict):
            # The verdict is the caller's judgment, so the form carries placeholders
            # and next_command stays null; the action still shows the whole form.
            action += (" — the full form: hs-run cook verify --plan %s --phase %s %s"
                       % (plan_dir, ns.phase or "<id>", _VERIFY_PLACEHOLDERS))
        return hs_run.StepResult(
            state="verify_failed",
            next_action=action,
            artifacts=[str(plan_dir)],
            warnings=[err_text] if err_text else [],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    # Record which real commits this phase's own verify covers, in the
    # append-only log — never in the canonical `verification.json`
    # this same call just wrote, which a LATER phase's own verify overwrites
    # wholesale. The phase actually verified is read back from what write_
    # verification itself just wrote (the canonical record's own `phase`
    # field), not re-derived from the flags — `--from FILE` never puts `--phase`
    # on the command line at all.
    verified_phase = _verified_phase(ns, plan_dir)
    commit_warnings = (_record_phase_commits(
        plan_dir, str(verified_phase), Path(root), named=named,
        computed=commit_set if verified_phase == pre_phase else None)
        if verified_phase else [])
    # What write_verification said on a success is still said: the advisory that
    # the phase's snapshot kept an earlier record is the caller's only cue to read
    # the latest one elsewhere.
    advisories = [ln for ln in err_text.splitlines() if ln.startswith("[advisory]")]
    latest_p = plan_dir / "artifacts" / ("phase-latest-%s.json" % verified_phase)
    latest = [str(latest_p)] if verified_phase and latest_p.is_file() else []
    return hs_run.StepResult(
        # `verification_recorded`, not `verified`. This verb WROTE the artifact and
        # still owes the caller one deterministic step (rung 11); the sibling `test`
        # domain's `verified` is a rung-0 reading of an artifact someone else wrote.
        # One word on two rungs made a driver's next move depend on which verb it
        # happened to ask rather than on what is on disk.
        state="verification_recorded",
        # Scoped to the phase just verified: an unforced `next` run in a worktree
        # hands out, and marks, whichever phase is free.
        next_action=("phase %s's verification is recorded; exit 11 is not a failure but "
                     "the one deterministic step left: %s"
                     % (verified_phase or "?", _next_for(plan_dir, verified_phase))),
        next_command=_next_for(plan_dir, verified_phase),
        artifacts=[str(plan_dir / "artifacts")] + latest,
        warnings=advisories + sha_warnings + commit_warnings,
        exit_code=EXIT_NEEDS_STEP,
    ).as_dict()


# ---------------------------------------------------------- 6e + 6g: close



def _close(ns) -> dict:
    # resolved BEFORE closing: a close flips the plan's status, and asked again the
    # resolver would name another plan
    plan_dir, _miss = _resolve_plan_dir_verbose(Path(ns.ctx.root), explicit=ns.plan)
    result = _cmd_close_impl(ns.ctx.root, plan=ns.plan)
    if plan_dir is not None:
        result = _with_waived_edges(plan_dir, result, ns.ctx.root)
    return hs_run_next_command.finalize(result)


CLOSE = hs_run.verb(_cook_door(_close), options=("plan",), helps={"plan": _PLAN_HELP})


def _cmd_close_impl(root, *, plan=None) -> dict:
    """`hs-run cook close [--plan <dir>]` — wraps the closing steps, in
    order: (1) the completion + review gate, (2)
    the 6e phase-count grid gate (`grid_emit_guard.require_phase_grids_cli`),
    THEN (3) `close_plan.close_plan` (6g).

    F1's exact repro (HIGHEST SEVERITY): before this fix, `close`
    ran ONLY the 6e grid gate — which returns 0 immediately when grid mode is
    off (cook's DEFAULT, E15) — so a plan with 0/2 phases verified and no
    review-decision closed anyway. Before `close` (and before every
    auto-finalize path — see this module's docstring for how `next` covers
    the auto-finalize path) enforce `review-decision` = PASS (or a measured
    small-diff waiver) AND
    `completion_state(...)["complete"]`, else EXIT_BROKEN naming that
    function's own reason. This is a SEPARATE call from `preflight`'s 0.5c
    macro grid check (E27), reading a different file set
    (`coverage-grid-<phase>.<fmt>` per declared phase, not the macro
    `coverage-grid.json`). Any failure refuses to even attempt the close
    (never partially closes then reports a mismatched success).

    R3: `-h`/`--help` used to reach this verb's own body
    unfiltered and actually close the plan (`hs-run cook close --help`
    flipped `status: in_progress -> completed`) — guarded here regardless of
    what hs_run.py's dispatcher does."""
    plan_dir, miss = _resolve_plan_dir_verbose(Path(root), explicit=plan)
    if plan_dir is None:
        return hs_run.StepResult(
            state="plan_unresolved",
            next_action=_no_plan_message(miss),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()

    comp = derive_plan_completion.completion_state(plan_dir, root=root)
    if not comp["complete"]:
        return hs_run.StepResult(
            # `phases_incomplete`, not `needs_implementation`. This is the MUTATING
            # close verb refusing to write, on purpose, at EXIT_REFUSED — the four
            # gates in this function all decline to close for a reason a person can
            # act on, never because the CLI itself is stuck. The projection reports
            # the same tree at rung 0 as work still owed. Same predicate, different
            # question — so a different word, matching how `review_missing` was
            # split from `needs_review` at the gate directly below.
            state="phases_incomplete",
            next_action=comp["reason"],
            artifacts=[str(plan_dir)],
            warnings=[comp["reason"]],
            exit_code=EXIT_REFUSED,
        ).as_dict()
    reverify = _reverify_owed(plan_dir)
    if reverify:
        return hs_run.StepResult(
            state="reopen_verify_owed", next_action=reverify[0], artifacts=[str(plan_dir)],
            warnings=[reverify[0]], exit_code=EXIT_REFUSED,
            extra={"phases": reverify[1]}).as_dict()
    # The deferred full suite is owed before the review: run after it, the suite's own
    # fixes went unreviewed, or every commit since (docs, another session's) asked for
    # the review again. Reuses `_suite_debt_unpaid` — the exact function `pending`
    # reads — so the two can never answer this question differently.
    if (defer_suite_carry.resolve_defer_suite_carry(plan_dir)["carried"]
            and _suite_debt_unpaid(plan_dir)):
        action, extra_warnings = _debt_repay_message(Path(root))
        return hs_run.StepResult(
            state="suite_debt_unpaid",
            next_action=action,
            artifacts=[str(plan_dir)],
            warnings=extra_warnings,
            exit_code=EXIT_REFUSED,
        ).as_dict()
    rec = None
    disposition = _review_disposition(plan_dir, root)
    if not disposition.cleared:
        owed, owed_extra = _review_owed(plan_dir, disposition)
        return hs_run.StepResult(
            # `review_missing`, not `needs_review`. Same predicate, different
            # question: this is a MUTATING verb refusing to write, where the
            # projection verb's `needs_review` reports the same tree at rung 10
            # as work still owed. One word riding both rungs told a driver two
            # different things about one artifact.
            state="review_missing",
            next_action="%s — before close can proceed" % owed,
            extra={"gate_scope": "plan", **owed_extra},
            artifacts=[str(plan_dir)],
            warnings=(["cook close requires a review-decision verdict in %s "
                       "before it will finalize the plan" % (_CLOSEABLE_VERDICTS,)]
                      + disposition.triggers
                      + ([disposition.why] if disposition.why else [])),
            exit_code=EXIT_REFUSED,
        ).as_dict()
    if disposition.record is not None:
        rec = disposition.record
    msg, code = grid_emit_guard.require_phase_grids_cli(plan_dir)
    if code != 0:
        return hs_run.StepResult(
            state="blocked_grid_phase_gate",
            next_action=msg,
            artifacts=[str(plan_dir)],
            warnings=[msg],
            exit_code=EXIT_REFUSED,
        ).as_dict()
    res = _close_plan(plan_dir, root=root)
    if not res.ok:
        return hs_run.StepResult(
            state="close_failed",
            next_action=res.message,
            artifacts=[str(plan_dir)],
            warnings=[res.message],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    # A close on a measured waiver leaves the waiver's own numbers as the
    # trace: a plan that finished without an independent review must never
    # read as though one happened.
    closed_warnings = _closed_warnings(rec)
    if disposition.waiver:
        closed_warnings = closed_warnings + [
            "closed without an independent review — " + disposition.waiver]
    return hs_run.StepResult(
        state="closed" if res.changed else "already_closed",
        next_action="cook chain complete for %s" % plan_dir,
        # Stated rather than inherited. 0 IS right here — the chain is over —
        # but a rung that arrives by default looks identical to one nobody
        # thought about, and four states reached callers that way.
        exit_code=EXIT_DONE,
        artifacts=[str(plan_dir)], warnings=closed_warnings,
    ).as_dict()


# --------------------------------------------------------- two-beat prompt/record

def _prompt(ns) -> dict:
    # Commas AND spaces: the comma is the documented spelling, but a
    # space-separated habit silently produced ONE unknown token and the
    # posture answered in_place as if nothing had been asked.
    flags = [t for t in re.split(r"[,\s]+", ns.flags or "") if t.strip()]
    return hs_run_next_command.finalize(
        _cmd_prompt_impl(ns.role, ns.phase, flags=flags, root=ns.ctx.root,
                         plan=ns.plan))


PROMPT = hs_run.verb(_cook_door(_prompt), options=("plan", "phase", "flags"), optional=("role",),
                    helps={"plan": _PLAN_HELP,
                           "flags": "comma-separated forcing flags relayed to the "
                                    "workflow-weight posture (parallel/subagents force a "
                                    "subagent, in_place holds the phase in place)"})


_COUNT_UNKNOWN = "could not count declared files: %s"


def _declared_files_or_why(plan_dir, miss, phase, root):
    """(files, None) when the phase's declared file set was READ, else
    (None, reason). plan-graph.yaml is the one machine source for per-phase file
    ownership — the phase file's `Related Code Files` prose (`**Create**: a; b`)
    is free text with globs, brace sets and directory stems, and is not read."""
    if plan_dir is None:
        return None, _no_plan_message(miss)
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return None, "plan-graph.yaml problem: %s" % graph["error"]
    declared = plan_graph.declared_files(graph, phase)
    if not declared:
        return None, ("phase %s declares no files_to_create/files_to_modify/"
                      "files_to_delete in %s/plan-graph.yaml — the count is read from "
                      "that sidecar only (a phase file's Create/Modify prose is not "
                      "parsed); declare the files there" % (phase, plan_dir))
    return sorted(plan_graph.expand_declared(root, declared)), None


def _phase_posture(plan_dir, miss, phase, flags, root):
    """(posture, count_problem) — the workflow-weight execution posture for ONE
    phase of the plan the verb resolved.

    The reader (workflow_weight.posture) owns the rule — in place by default,
    subagent past the declared-file threshold, either way when a flag or the
    plan's `posture:` forces it — so this only gathers its input: the phase's
    `files_to_*` union from plan-graph and the plan's declared posture.
    An unknown count is NOT zero: when the files cannot be read the reader still
    answers (a forcing flag still forces; otherwise its default stands) but the
    reason says the count is unknown and why, instead of a measured-looking
    "0 declared files <= 8"."""
    import workflow_weight
    try:
        files, problem = _declared_files_or_why(plan_dir, miss, phase, root)
    except Exception as exc:  # noqa: BLE001 — posture is an input, never a crash here
        files, problem = None, "reading plan-graph.yaml raised %s" % exc
    p = workflow_weight.posture(files or [], flags=tuple(flags),
                                declared=workflow_weight.declared_posture(plan_dir))
    if problem is None:
        return p, None
    problem = _COUNT_UNKNOWN % problem
    if not p.reason.startswith("forced"):
        p = p._replace(reason="%s — staying at the default (%s)" % (problem, p.mode))
    return p, problem


_IN_PLACE_PROMPT = (
    "Implement phase {phase} IN PLACE per the phase file — red->green: a test "
    "that fails for the stated reason first, then make it pass. The "
    "workflow-weight posture answered in place ({reason}), so the main thread "
    "does the implement itself; verify stays yours and the paired commit "
    "follows as usual. When done, run: hs-run cook record developer{plan_arg} "
    "--phase {phase} --out <file with your implementation notes>"
)


def _declared_phase_file(plan_dir, phase):
    """The phase document plan-graph.yaml's `file:` names for `phase`, or None —
    no sidecar, no declaration, or one the validator refuses. Never guessed from
    a file-name convention: node ids are free-form."""
    if plan_dir is None:
        return None
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return None
    return plan_graph.phase_file(graph, phase, plan_dir)


def _known_flags() -> list:
    try:
        import workflow_weight
        return workflow_weight.forcing_flags()
    except Exception:  # noqa: BLE001 — a warning must never break the beat
        return []


def _unknown_flags(flags) -> list:
    """Flags the workflow-weight policy does not know, spelled for a warning
   : a typo'd `--flags paralell` used to be silently ignored."""
    try:
        import workflow_weight
        known = {f.lstrip("-").replace("-", "_")
                 for f in workflow_weight.forcing_flags()}
    except Exception:  # noqa: BLE001 — a warning must never break the beat
        return []
    given = {f: f.lstrip("-").replace("-", "_") for f in flags or ()}
    return [f for f, norm in given.items() if norm not in known]


#: `HARNESS_AGENT` values naming the developer delegate itself — the identity the
#: harness already reads for attribution (`hook_runtime.resolve_actor`).
_DEVELOPER_AGENTS = frozenset({"developer", "hs:developer"})


def _is_developer_delegate() -> bool:
    return (os.environ.get("HARNESS_AGENT") or "").strip() in _DEVELOPER_AGENTS


def _phase_refusal(plan_dir, phase, graph=None) -> "str | None":
    """Why `phase` names no node of this plan, or None. None too with no `--phase`,
    no plan, or a graph that does not parse — the degrade `write_verification`
    takes. An EMPTY `--phase` is refused: read as absent, `next` would answer and
    claim the first owed phase for a caller that named one."""
    if plan_dir is None or phase is None:
        return None
    graph = graph if graph is not None else plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return None
    nodes = plan_graph._all_nodes(graph)
    if phase in nodes:
        return None
    return ("--phase %r is not a node in %s/plan-graph.yaml (known: %s)%s"
            % (phase, plan_dir, ", ".join(sorted(nodes, key=plan_graph.phase_sort_key)) or "(none)",
               plan_graph.phase_spelling_hint(graph, phase)))


def _unknown_phase(plan_dir, why) -> dict:
    return hs_run.StepResult(
        state="unknown_phase", next_action=why, artifacts=[str(plan_dir)],
        warnings=[], exit_code=EXIT_BROKEN).as_dict()


def _cmd_prompt_impl(role, phase, *, flags=(), root=None, plan=None) -> dict:
    """`hs-run cook prompt <role> [--plan <dir>] [--phase <id>] [--flags <csv>]` — beat 1 of the
    two-beat model pattern. Always returns EXIT_NEEDS_MODEL(10): its entire job
    is to hand the model the right instruction, never to execute it. For the
    developer role the instruction comes from the workflow-weight posture —
    in place by default, a subagent past the declared-file threshold; a forcing
    flag (`--flags parallel|subagents|in_place`) or the plan's `posture:` overrides
    it, and two that disagree are refused. Other roles still spawn:
    their beats (test, review, docs, git) are the independent-eyes steps."""
    if not role:
        return hs_run.StepResult(
            state="error",
            next_action="usage: hs-run cook prompt <role> [--phase <id>]",
            artifacts=[],
            warnings=["missing role"],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    if role not in _ROLE_PROMPTS:
        return hs_run.StepResult(
            state="error",
            next_action="unknown role %r (known: %s)"
                            % (role, ", ".join(sorted(_ROLE_PROMPTS))),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    if role == "developer" and not phase:
        return hs_run.StepResult(
            state="error",
            next_action="prompt developer needs --phase <id>",
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    plan_dir, miss = (_resolve_plan_dir_verbose(Path(root), explicit=plan)
                      if root else (None, None))
    why = _phase_refusal(plan_dir, phase)
    if why:
        return _unknown_phase(plan_dir, why)
    plan_arg = " --plan %s" % plan_dir if plan_dir is not None else ""
    if role == "developer" and _is_developer_delegate():
        # The delegate's consult, not the lead's prompt beat: no prompt-log line,
        # so `record` still pairs with the lead's own answer and the spawn it owes.
        text = ("You are the developer delegate for phase %s (HARNESS_AGENT=%s): "
                "implement it IN PLACE per the phase file — red->green, a test that "
                "fails for the stated reason first, then make it pass. Spawn no "
                "developer of your own. Return your report to the lead, who runs "
                "`hs-run cook record developer%s --phase %s` and the verify."
                % (phase, os.environ.get("HARNESS_AGENT"), plan_arg, phase))
        doc = _declared_phase_file(plan_dir, phase)
        if doc is not None:
            text += " Phase file: %s" % doc
        return hs_run.StepResult(
            state="waiting_on_model",
            next_action="implement the phase in place per prompt_text; the lead "
                        "records it when you return",
            artifacts=[str(doc)] if doc is not None else [],
            warnings=[], exit_code=EXIT_NEEDS_MODEL,
            extra={"prompt_text": text, "role": role, "posture_mode": "in_place",
                   "posture_reason": "the caller is the developer delegate"},
        ).as_dict()
    if role == "developer":
        import workflow_weight
        try:
            p, count_problem = _phase_posture(plan_dir, miss, phase, flags, root)
        except workflow_weight.PostureConflict as exc:
            return hs_run.StepResult(
                state="error",
                next_action="%s — pick one posture (drop a --flags entry or change the "
                            "plan's `posture:`)" % exc,
                artifacts=[], warnings=[], exit_code=EXIT_BROKEN,
            ).as_dict()
        if plan_dir is not None:
            _append_prompt_log(plan_dir, role, phase, p.mode)
            _mark_phase_open(plan_dir, phase, Path(root), claimed=True)
            if flags:
                _record_posture_flags(plan_dir, phase, flags)
        warnings = ["--flags %r is not a forcing flag the policy knows "
                    "(known: %s) — the posture answered as if it were not given"
                    % (f, ", ".join(_known_flags())) for f in _unknown_flags(flags)]
        if count_problem:
            warnings.append(count_problem)
        record_cmd = ("hs-run cook record developer%s --phase %s --out <file>"
                      % (plan_arg, phase))
        if p.mode == "subagent":
            action = "spawn the subagent per prompt_text, then run: %s" % record_cmd
            text = _ROLE_PROMPTS[role].format(phase=phase, plan_arg=plan_arg)
        else:
            action = ("implement the phase in place per prompt_text, then run: %s"
                      % record_cmd)
            text = _IN_PLACE_PROMPT.format(phase=phase, reason=p.reason,
                                           plan_arg=plan_arg)
        doc = _declared_phase_file(plan_dir, phase)
        if doc is not None:
            text += " Phase file: %s" % doc
        return hs_run.StepResult(
            state="waiting_on_model",
            next_action=action,
            artifacts=[str(doc)] if doc is not None else [],
            warnings=warnings,
            exit_code=EXIT_NEEDS_MODEL,
            extra={"prompt_text": text, "role": role, "posture_mode": p.mode,
                   "posture_reason": p.reason, **_plan_key(plan_dir)},
        ).as_dict()
    if plan_dir is not None:
        _append_prompt_log(plan_dir, role, phase)
    text = _ROLE_PROMPTS[role].format(phase=phase or "")
    record_cmd = "hs-run cook record %s%s --out <file>" % (role, plan_arg)
    if phase:
        record_cmd += " --phase %s" % phase
    return hs_run.StepResult(
        state="waiting_on_model",
        next_action="spawn the subagent per prompt_text, then run: %s" % record_cmd,
        artifacts=[],
        warnings=[],
        exit_code=EXIT_NEEDS_MODEL,
        extra={"prompt_text": text, "role": role, **_plan_key(plan_dir)},
    ).as_dict()


_ATTEST_MODE_ENV = "HARNESS_SPAWN_ATTEST_MODE"


# ------------------------------------------------------- record ledger (beat 2)

# `record` used to check ONLY that `--out` existed and answer `recorded` —
# a mutation the CLI never actually performed. `_review_disposition` (below)
# then cleared the whole review obligation on a `review-decision` artifact's
# mere PRESENCE, which anyone with a text editor can produce: a real run was
# observed where `prompt code-reviewer` ran twice, `record code-reviewer` ran
# zero times, and the plan still closed because the subagent wrote the
# artifact itself. `record` now earns its "mutates on success" row for real:
# every call appends one line to this append-only ledger, and the artifact
# branch of `_review_disposition` demands a `code-reviewer` line here before
# a `review-decision` verdict may clear the gate — the measured-waiver branch
# (no artifact at all, a small enough diff) is untouched, since that path
# never claimed a review happened in the first place.
_RECORD_LOG_FILE = "record-log.jsonl"


@_attrs.define(frozen=True, kw_only=True)
class RecordLogEntry:
    """One `hs-run cook record <role> --out <file>` call. `out_sha256` pins
    WHICH file was recorded, not merely that some file was — a ledger line
    naming a path is not proof of what that path held at the time."""

    role: str
    phase: "str | None" = None
    out: str = ""
    out_sha256: str = ""
    actor: str = ""
    ts: str = ""
    # A plan-level code-reviewer record pins the review-decision on disk when it
    # ran: a later review recorded against the SAME decision left its own verdict
    # unwritten (`_review_claim`).
    decision: "dict | None" = None
    pins_decision: bool = False
    # The patch-ids of every change a review could read when it was recorded
    # (`_review_seen`): the code it saw, by content (`_review_claim`).
    seen: "list | None" = None
    # A report outside the plan dir is copied into it (`artifacts/records/`): a
    # session's scratchpad does not outlive the session, the plan does.
    kept: "str | None" = None

    def as_dict(self) -> dict:
        out: dict = {"kind": "record", "role": self.role, "phase": self.phase,
                     "out": self.out, "out_sha256": self.out_sha256,
                     "actor": self.actor, "ts": self.ts}
        if self.kept:
            out["kept"] = self.kept
        if self.pins_decision:
            out["decision"] = self.decision
            out["seen"] = list(self.seen or [])
        return out


@_attrs.define(frozen=True, kw_only=True)
class SupersedeLogEntry:
    """`hs-run cook record <role> --phase P --supersede`: every earlier record of
    this role and phase stops counting. Appended, so the retired lines stay readable."""

    role: str
    phase: "str | None" = None
    actor: str = ""
    ts: str = ""

    def as_dict(self) -> dict:
        return {"kind": "supersede", "role": self.role, "phase": self.phase,
                "actor": self.actor, "ts": self.ts}


@_attrs.define(frozen=True, kw_only=True)
class PromptLogEntry:
    """One `hs-run cook prompt <role>` call, written into the plan it resolved —
    the half of the two-beat pair `record` looks for by plan, role and phase.

    `posture_mode` is what the prompt answered (developer only), so `record` knows
    whether a spawn was expected at all. `session` + `at` (epoch, the run ledger's
    own clock) let `record` find the run-ledger line THIS call produced: the first
    `cook prompt` run of that session at or after `at`. The verb cannot know its
    own run id — the dispatcher mints it after the verb returns."""

    role: str
    phase: "str | None" = None
    posture_mode: "str | None" = None
    session: str = ""
    at: float = 0.0
    actor: str = ""
    ts: str = ""

    def as_dict(self) -> dict:
        return {"kind": "prompt", "role": self.role, "phase": self.phase,
                "posture_mode": self.posture_mode, "session": self.session,
                "at": self.at, "actor": self.actor, "ts": self.ts}


def _append_prompt_log(plan_dir, role, phase, posture_mode=None) -> None:
    """Best-effort: a ledger that cannot be written never takes the prompt beat
    down; `record` then reports the pair as unmatched, which is the truth."""
    try:
        import time as _time
        import hook_runtime
        _append_record_log_entry(Path(plan_dir), PromptLogEntry(
            role=role, phase=phase, posture_mode=posture_mode,
            session=hs_run._resolve_session() or "", at=_time.time(),
            actor=hook_runtime.resolve_actor(), ts=write_verification._now_iso(),
        ).as_dict())
    except Exception:  # noqa: BLE001 — bookkeeping, never the beat
        pass


def _plan_key(plan_dir) -> dict:
    """`{"plan": <dir>}` for an envelope whose call resolved one: the run ledger keeps
    it, so a reader finds every plan a session prompted in."""
    return {"plan": str(plan_dir)} if plan_dir is not None else {}


def _next_prompt_at(plan_dir, prompt):
    """The clock of the next `prompt` line of the same role and session, in `plan_dir`
    or any plan the session prompted in since: where `prompt`'s spawn window ends -- a
    spawn after it answers that call, not this one. None when none is recorded."""
    at = float(prompt.get("at") or 0)
    session = prompt.get("session")
    plans = {str(plan_dir)} | {rec.plan for rec in hs_run.read_run_ledger()
                               if rec.session == session and rec.domain == "cook"
                               and rec.verb == "prompt" and rec.plan
                               and float(rec.ts or 0) >= at}
    later = [float(r.get("at") or 0) for d in sorted(plans) for r in _read_record_log(Path(d))
             if r.get("kind") == "prompt" and r.get("role") == prompt.get("role")
             and r.get("session") == session and float(r.get("at") or 0) > at]
    return min(later) if later else None


def _matching_prompt(plan_dir, role, phase):
    """The latest `prompt` line for this role and phase in this plan, or None."""
    found = None
    for rec in _read_record_log(Path(plan_dir)):
        if (rec.get("kind") == "prompt" and rec.get("role") == role
                and rec.get("phase") == phase):
            found = rec
    return found


def _record_log_path(plan_dir: Path) -> Path:
    return plan_dir / "artifacts" / _RECORD_LOG_FILE


def _append_record_log_entry(plan_dir: Path, record: dict) -> None:
    """Append-only, never a read-modify-write — same idiom as
    `_append_phase_commits_record`, a SEPARATE file: this ledger answers "did
    `record` ever run for this role", the phase-commits log answers "what
    landed since this phase opened" — two different questions."""
    import json as _json
    path = _record_log_path(plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(record, sort_keys=True))
        fh.write("\n")


def _read_record_log(plan_dir: Path) -> list:
    """Every well-formed line, oldest-first — a corrupt line degrades to
    "not there" (skipped), mirroring `_read_phase_commits_records`: a reader
    other verbs' happy path does not depend on must never take one down."""
    path = _record_log_path(plan_dir)
    if not path.is_file():
        return []
    import json as _json
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json.loads(line))
        except ValueError:
            continue
    return out


_ANY_PHASE = object()


def _has_recorded_role(plan_dir: Path, role: str, *, phase=_ANY_PHASE) -> bool:
    """Whether `hs-run cook record <role>` genuinely ran for this plan, ever
    — the disk fact `_review_disposition` demands before a `review-decision`
    artifact alone may clear the review obligation. A `record developer` line
    does not count for `role="code-reviewer"`: the ledger is keyed by role on
    purpose, so one delegation's attestation can never stand in for another's.
    `phase` narrows it to one phase's own record (the per-phase projection)."""
    live = _live_records(plan_dir, role)
    return any(live.values()) if phase is _ANY_PHASE else bool(live.get(phase))


def _live_records(plan_dir: Path, role: str) -> dict:
    """{phase: [record, ...]} for `role`, oldest-first, minus every record a later
    `kind: supersede` line for the same role and phase retired."""
    live: dict = {}
    for rec in _read_record_log(plan_dir):
        if rec.get("role") != role:
            continue
        if rec.get("kind") == "record":
            live.setdefault(rec.get("phase"), []).append(rec)
        elif rec.get("kind") == "supersede":
            live[rec.get("phase")] = []
    return live


@_attrs.define(frozen=True, kw_only=True)
class OwedCheck:
    """One declared check a phase owes before its verdict: the ledger's name for it,
    why its newest run does not prove it (`harness_checks.provenance_problem`'s kind
    and detail), and the command that runs it for this plan and phase."""

    check: str
    kind: str
    why: str
    command: str

    def as_dict(self) -> dict:
        return {"check": self.check, "kind": self.kind, "why": self.why, "command": self.command}


def _declared_check_step(plan_dir: Path, phase: str, root, numbers: str):
    """(envelope, warnings): the `hs-code check run` / `test run` a phase owes before its
    verdict, or None. Asks `harness_checks.unproven` on the changed paths the DoD gate
    judges -- the gate's own answer, so a phase is never verified only for ship to refuse
    it. A hard requirement routes; a soft one rides as a warning. A run that could not
    measure (BROKEN/BLOCKED) names its reason: the same command again would say the same."""
    import change_class_derivation as ccd
    import harness_checks
    floor, enforcement = artifact_check.class_coverage_floor(root)
    try:
        # a plan that defers its suite proves each phase on its scoped run; the DoD gate
        # still holds the full one
        scoped = defer_suite_carry.resolve_defer_suite_carry(plan_dir)["carried"]
        rows = harness_checks.unproven(Path(root), plan_dir, ccd.repo_changed_paths(str(root)),
                                       floor, enforcement, scoped_ok=scoped)
    except harness_checks.Unreadable as e:
        return hs_run.StepResult(
            state="declared_check_broken",
            # the declaration, the plan's base or its check ledger: the error names which
            next_action="the declared checks cannot be held to: %s — fix what it names; "
                        "the DoD gate refuses ship until it reads" % e,
            next_command=None, artifacts=[str(Path(root) / "codemap.yaml")], warnings=[],
            exit_code=EXIT_NEEDS_MODEL, extra={"phase": phase, "summary_numbers": numbers},
        ).as_dict(), []

    def cmd(row):
        return "%s --plan %s --phase %s" % (row["command"], plan_dir, phase)

    warnings = ["advisory check %s: %s — `%s`" % (r["check"], r["why"], cmd(r))
                for r in rows if r["enforcement"] != "hard"]
    owed = [OwedCheck(check=r["check"], kind=r["problem"], why=r["why"], command=cmd(r))
            for r in rows if r["enforcement"] == "hard"]
    if not owed:
        return None, warnings
    runnable = [o for o in owed if o.kind != "unmeasured"]
    broken = ["%s: %s" % (o.check, o.why) for o in owed if o.kind == "unmeasured"]
    if not runnable:
        return hs_run.StepResult(
            state="declared_check_broken",
            next_action="phase %s owes declared checks whose newest run could not measure — "
                        "%s. Fix what the reason names, then run the check again"
                        % (phase, "; ".join(broken)),
            next_command=None, artifacts=[str(plan_dir / "artifacts")],
            warnings=warnings, exit_code=EXIT_NEEDS_MODEL,
            extra={"phase": phase, "owed_checks": [o.as_dict() for o in owed],
                   "summary_numbers": numbers},
        ).as_dict(), []
    return hs_run.StepResult(
        state="needs_declared_check",
        next_action="phase %s owes %d declared check(s) the harness's own run does not prove "
                    "now — %s. Run each, then cook next" % (
                        phase, len(runnable),
                        "; ".join("%s: %s (`%s`)" % (o.check, o.why, o.command) for o in runnable)),
        # one `--all --coverage` run also proves the runner's own check
        next_command=next((o.command for o in runnable if "--coverage" in o.command),
                          runnable[0].command), artifacts=[str(plan_dir / "artifacts")],
        warnings=warnings + ["could not measure — %s" % b for b in broken],
        exit_code=EXIT_NEEDS_STEP,
        extra={"phase": phase, "owed_checks": [o.as_dict() for o in owed],
               "summary_numbers": numbers},
    ).as_dict(), []


def _stale_build_warning(plan_dir: Path, phase: str) -> "str | None":
    """Why the phase's live build record is known stale, or None: its report is
    gone, or it predates the plan's current approval. Either way it may describe a
    build that never finished — a pause, a re-plan, another session's scratchpad."""
    recs = _live_records(plan_dir, "developer").get(phase) or []
    if not recs:
        return None
    last = recs[-1]
    why = []
    if last.get("out") and not Path(last["out"]).is_file():
        why.append("its report %s no longer exists%s" % (
            last["out"], " (a copy is kept at %s)" % last["kept"]
            if last.get("kept") and Path(last["kept"]).is_file() else ""))
    approval, _problem = artifact_check._load_artifact(plan_dir, "plan-approval")
    approved_at = _parse_ts((approval or {}).get("ts") if isinstance(approval, dict) else None)
    recorded_at = _parse_ts(last.get("ts"))
    if approved_at and recorded_at and recorded_at < approved_at:
        why.append("it predates the plan's current approval (%s < %s)"
                   % (last.get("ts"), approval.get("ts")))
    if not why:
        return None
    return ("phase %s's build record may not describe a finished build: %s — if it "
            "does not, retire it and build the phase: hs-run cook record developer "
            "--plan %s --phase %s --supersede" % (phase, "; ".join(why), plan_dir, phase))


def _decision_pin(plan_dir: Path) -> "dict | None":
    """{sha256, verdict} of the review-decision on disk, whatever its format, or None
    when there is none to read."""
    import hashlib
    import json as _json
    rec, _problem = artifact_check._load_artifact(plan_dir, "review-decision")
    if not isinstance(rec, dict):
        return None
    blob = _json.dumps(rec, sort_keys=True, default=str).encode("utf-8")
    return {"sha256": hashlib.sha256(blob).hexdigest(), "verdict": rec.get("verdict")}


def _write_verdict_command(plan_dir: Path) -> str:
    import runnable_command
    return ("%s %s --verdict PASS|PASS_WITH_RISK|BLOCKED --rationale \"<why>\" "
            "--rounds-run <N>" % (runnable_command.python_script("write_review_decision.py"),
                                  plan_dir))


def _review_lines(plan_dir: Path) -> list:
    """Every live code-reviewer record, phase-level ones included, in ledger order —
    the order the reviews were recorded in."""
    out: list = []
    for rec in _read_record_log(plan_dir):
        if rec.get("role") != "code-reviewer":
            continue
        if rec.get("kind") == "record":
            out.append(rec)
        elif rec.get("kind") == "supersede":
            out = [r for r in out if r.get("phase") != rec.get("phase")]
    return out


def _review_claim(plan_dir: Path, root) -> "tuple[str, bool] | None":
    """(what the recorded reviews still owe, whether that is a fresh review) or None.

    The review recorded last owns the review-decision it pinned when it was recorded
    (the verdict is written first, then the review is recorded). It owes its verdict
    when none is on disk; when the decision changed after it, no recorded review owns
    that decision until the one it belongs to is recorded; and when an earlier review
    with another report pinned the same decision, the last one never wrote its own.
    Recording one report twice is not a second review. Lines written before records
    pinned a decision are not judged on ownership.

    It is stale when the run's verified work goes past the HEAD it was recorded at:
    commits it never saw. A verify that adds no commit (a re-verify, the deferred-suite
    run) leaves it standing."""
    lines = _review_lines(plan_dir)
    if not lines:
        return None
    last = lines[-1]
    now = _decision_pin(plan_dir)
    record = "hs-run cook record code-reviewer --plan %s --out %s"
    if "decision" in last:
        if now is None:
            return ("the review recorded last (%s, at %s) has no verdict on disk — write "
                    "it: %s, then record it again: %s"
                    % (last.get("out"), last.get("ts"), _write_verdict_command(plan_dir),
                       record % (plan_dir, _report_path(last))), False)
        if last.get("decision") != now:
            return ("review-decision (verdict %s) was written after the review recorded "
                    "last (%s, at %s), so no recorded review owns it — record the review "
                    "it belongs to: %s"
                    % (now.get("verdict"), last.get("out"), last.get("ts"),
                       record % (plan_dir, "<its report>")), False)
        first = next((r for r in lines[:-1] if r.get("decision") == now
                      and r.get("out_sha256") != last.get("out_sha256")), None)
        if first is not None:
            return ("the review recorded last (%s, at %s) has no verdict on disk: "
                    "review-decision is still the one recorded with %s at %s (verdict %s) "
                    "— write this review's verdict: %s, then record it again: %s. If it is "
                    "the same review recorded under another file, retire the plan's "
                    "review records (hs-run cook record code-reviewer --plan %s "
                    "--supersede) and record it once"
                    % (last.get("out"), last.get("ts"), first.get("out"), first.get("ts"),
                       now.get("verdict"), _write_verdict_command(plan_dir),
                       record % (plan_dir, _report_path(last)), plan_dir), False)
    if "seen" not in last:
        return None
    gone = _verified_gone(plan_dir)
    if gone:
        return gone, False
    seen = set(last.get("seen") or [])
    unseen = [sha for sha, pid in _verified_patches(plan_dir).items() if pid not in seen]
    if not unseen:
        return None
    return ("the review recorded last (%s, at %s) never saw commit %s%s, verified since "
            "— a change it never read. Review again: %s, write its verdict (%s), then %s"
            % (last.get("out"), last.get("ts"), unseen[-1][:12],
               " (and %d more)" % (len(unseen) - 1) if len(unseen) > 1 else "",
               _review_prompt_command(plan_dir), _write_verdict_command(plan_dir),
               record % (plan_dir, "<file>")), True)


def _verified_patches(plan_dir: Path) -> "dict[str, str]":
    """sha -> patch-id of every verified commit a review reads, oldest line first. A
    line an older harness wrote carries no patch-ids: they are read from the plan's
    repo now, and a commit git no longer knows there is not judged."""
    repo = plan_graph.root_for_plan_dir(plan_dir)
    out: dict = {}
    for r in phase_commits_log.read_records(plan_dir):
        if r.get("kind") != "verify":
            continue
        ids = r.get("patch_ids")
        out.update(ids if isinstance(ids, dict)
                   else review_seen.patch_ids(repo, r.get("sha") or []))
    return out


def _review_seen(plan_dir: Path, report: "str | None" = None) -> list:
    """The patch-ids a review recorded now could have read: every verified change,
    and every change from the run's base to HEAD in the plan's repo and in each tree
    the run opened or verified a phase in (a review reads the code there, verified
    yet or not). A report recorded before — retired or not — is the same review: it
    keeps what it saw the first time, so recording it again never widens that."""
    for r in _read_record_log(plan_dir):
        if (report and r.get("kind") == "record" and r.get("role") == "code-reviewer"
                and r.get("out_sha256") == report and isinstance(r.get("seen"), list)):
            return list(r["seen"])
    seen = set(_verified_patches(plan_dir).values())
    base, _tails, _measurable = _run_commit_ends(plan_dir)
    trees = [plan_graph.root_for_plan_dir(plan_dir)] + [
        Path(str(r["tree"])) for r in phase_commits_log.read_records(plan_dir)
        if r.get("kind") in ("open", "verify") and r.get("tree")
        and Path(str(r["tree"])).is_dir()]
    for tree in dict.fromkeys(trees):
        head = _git_head(tree)
        if base and head:
            seen.update(review_seen.patch_ids(
                tree, review_seen.range_shas(tree, base, head, limit=_SEEN_RANGE_LIMIT)
            ).values())
    return sorted(seen)


# The newest commits of base..HEAD a review is credited with reading unverified. Every
# verified change is credited from the ledger whatever its age; this only bounds the
# work done since the last verify, so a cut costs one more review, never a pass.
_SEEN_RANGE_LIMIT = 500


def _last_verify(plan_dir: Path) -> "dict | None":
    lines = [r for r in phase_commits_log.read_records(plan_dir) if r.get("kind") == "verify"]
    return lines[-1] if lines else None


def _verified_gone(plan_dir: Path) -> "str | None":
    """Why the run's verified work is not the code checked out, or None. The verified
    tail must be in HEAD's history of the tree the last verify read or of the plan's
    repo (work merged on from a worktree reads there). A ref elsewhere — a pushed
    branch, a backup, a tag — proves the commit exists, never that the code checked
    out is the code verified: after an amend or rebase, or on another branch, the
    code at HEAD was never verified and no review can be judged against it. A line
    with no tree (an older harness) is not judged."""
    last = _last_verify(plan_dir)
    if not last or not last.get("tree"):
        return None
    shas = [str(s) for s in (last.get("sha") or []) if s]
    tail = shas[0] if shas else (str(last.get("head") or "") or None)
    if not tail:
        return None
    trees = [Path(str(last["tree"])), plan_graph.root_for_plan_dir(plan_dir)]
    heads: "list[tuple[Path, str]]" = []
    for tree in dict.fromkeys(trees):
        head = _git_head(tree) if tree.is_dir() else None
        if head:
            heads.append((tree, head))
    if not heads or any(_git_contains(tree, head, tail) is True for tree, head in heads):
        return None
    return ("the run's verified work ends at commit %s, which HEAD in %s no longer holds "
            "— history was rewritten, or another branch is checked out. Verify the code at "
            "HEAD: hs-run cook verify --plan %s --phase %s --verdict <PASS|FAIL> "
            "--check <kind>:<status>, then close again"
            % (tail[:12], " or ".join("%s (%s)" % (tree, head[:12]) for tree, head in heads),
               plan_dir, last.get("phase") or "<phase>"))


def _git_contains(root: Path, head: str, sha: str) -> "bool | None":
    """Whether `sha` is in `head`'s history; None when git cannot say (either is
    unknown here, or there is no repo) — only a definite no reads as unseen work."""
    import subprocess
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), "merge-base",
                            "--is-ancestor", sha, head],
                           capture_output=True, text=True, env=_git_env())
    except OSError:
        return None
    return {0: True, 1: False}.get(r.returncode)


def _parse_ts(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _keep_report(plan_dir: Path, role: str, phase, data: bytes, digest: str,
                 suffix: str) -> Path:
    """The copy kept of a report that lives outside `plan_dir`, content-addressed so two
    reports of one phase never overwrite each other. Written whole or not at all: a
    half-written copy would pass `is_file()` on every later call."""
    target = plan_dir / "artifacts" / "records" / ("%s-%s-%s%s" % (
        phase or "plan", role, digest[:12], suffix or ".md"))
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp-%d" % os.getpid())
        try:
            tmp.write_bytes(data)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    return target


def _report_path(rec: dict) -> "str | None":
    """The report a ledger line points a re-run at: its own file while it is there,
    else the copy kept in the plan (a scratchpad report is gone after a restart)."""
    out, kept = rec.get("out"), rec.get("kept")
    if out and not Path(out).is_file() and kept and Path(kept).is_file():
        return kept
    return out


def _append_record_log(plan_dir, role, phase, out_path, root=None) -> "tuple[str | None, str | None]":
    """Best-effort append for beat 2 into the plan the verb resolved — through
    `_resolve_plan_dir_verbose`, the `--plan` -> `in_progress` ->
    approved-and-unopened -> freshly-closed chain `next`/`close` already use —
    NOT the narrower `artifact_check.resolve_active_plan`. That narrower read
    (in_progress only) went BLIND on exactly the scenario this module's
    own header exists to handle: `write_verification`'s auto-finalize can
    flip `plan.md` to `completed` before review ever clears, and `record
    code-reviewer` must still land its ledger line for a plan already
    sitting in that premature-completed state, or the gate it is supposed to
    clear can never be told it ran (measured: a real driver hit exactly this
    ordering and the ledger stayed empty).

    Returns (why the line did not land, or None; where a report outside the plan
    was kept, or None). A miss must not take the `record` beat down, but it is
    said: a silent skip left the review gate and the per-phase projection unable
    to see a call the caller believes it made."""
    try:
        import hashlib
        import hook_runtime
        data = Path(out_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        inside = Path(out_path).resolve().is_relative_to(Path(plan_dir).resolve())
        kept, kept_problem = None, None
        if not inside:
            try:
                kept = _keep_report(Path(plan_dir), role, phase, data, digest,
                                    Path(out_path).suffix)
            except OSError as exc:  # the copy is a convenience; the ledger line is not
                kept_problem = exc
        pins = role == "code-reviewer"
        _append_record_log_entry(Path(plan_dir), RecordLogEntry(
            role=role, phase=phase, out=str(out_path), out_sha256=digest,
            kept=str(kept) if kept else None,
            actor=hook_runtime.resolve_actor(),
            ts=write_verification._now_iso(),
            decision=_decision_pin(Path(plan_dir)) if pins else None,
            pins_decision=pins,
            seen=_review_seen(Path(plan_dir), report=digest) if pins else None,
        ).as_dict())
    except Exception as exc:  # noqa: BLE001 — the ledger write must never break the beat
        return "record ledger line not written: %s" % exc, None
    if kept_problem is not None:
        return None, ("%s lives outside the plan and was not kept: %s — the ledger line "
                      "names only the original" % (out_path, kept_problem))
    note = (None if kept is None else
            "%s lives outside the plan; kept a copy at %s (the ledger line names both)"
            % (out_path, kept))
    return None, note


def _prompt_run(session, at=None):
    """(run id, epoch clock) of a `cook prompt` call of `session`: the first one at
    or after `at` (the clock of the `PromptLogEntry` it wrote), else the latest."""
    found = (None, None)
    for rec in hs_run.read_run_ledger():
        if (rec.session == session and rec.domain == "cook" and rec.verb == "prompt"
                and rec.run_id):
            ts = float(rec.ts or 0)
            if at is None:
                found = (rec.run_id, ts)
            elif ts >= at:
                return rec.run_id, ts
    return found


def _attest_spawn(role, prompt=None, plan_dir=None):
    """(attested, note) — did a spawn of `role` land against the `hs-run cook
    prompt` call this record pairs with?

    This is what `run_id` bought. `_HARD_CASE_WARNING` used to be unconditionally
    true; now the chain (CLI stamps an id -> dispatcher resolves it -> the
    PreToolUse hook records it on the spawn) leaves a disk fact where there was
    only a transcript fact.

    `prompt` is the plan's own `PromptLogEntry` for this role and phase; its run
    is looked up by session and clock. Without one (no plan resolved) the session's
    latest prompt call is used. The session is resolved exactly as the run-ledger
    WRITER resolves it (`hs_run._resolve_session`: the env id, else the newest
    session file) — reading only the env var matched nothing on every Bash-tool
    call, where that var is unset.

    A spawn matches on session AND subagent_type AND either the call's run_id (the
    stamp the dispatcher put on it) or its clock: at or after this prompt, before
    the plan's next prompt of the same role and session — a spawn after that one
    answers it, so an earlier phase built in place never borrows a later phase's
    spawn. Dropping subagent_type would leave a check that proves only that
    SOMETHING was spawned — the laundering this exists to catch, surviving it.

    Returns `(False, note)` for every hole in the chain, and the note says NO
    EVIDENCE, never "no spawn happened". The difference is the whole design: the
    hook may be disabled, the state dir may differ, the session may have been
    resumed, agents may run in parallel. Claiming absence from those is the
    overreach that got the `Agent|Task` spawn-window guard disabled outright for
    over-blocking legitimate work."""
    try:
        import spawn_provenance

        session = hs_run._resolve_session() or ""
        until = None
        if prompt is not None:
            session = str(prompt.get("session") or session)
            at = float(prompt.get("at") or 0)
            run_id, since = _prompt_run(session, at)
            if not at:
                since = None  # an entry with no clock pairs by run id alone
            until = _next_prompt_at(plan_dir, prompt) if plan_dir else None
        else:
            # `hs_run` owns the ledger format and its location — the writer's own
            # default, so reader and writer cannot look in two places.
            run_id, since = _prompt_run(session)
        if not run_id:
            # Names Task explicitly: this is still the "did a real
            # Task(subagent_type=...) spawn happen" question, just answered at
            # an earlier point in the chain. Dropping the word would strip the
            # reader of the one term that says WHAT was not attested — the
            # caveat becomes a shrug.
            return False, ("no record of an `hs-run cook prompt %s` call for this "
                            "session, so a real Task(subagent_type=...) spawn "
                            "cannot be attested against it — treat this `record` "
                            "as an attestation, not proof (no evidence either "
                            "way)" % role)

        # Read the agent name back out of the prompt this module ALREADY hands
        # the model, rather than keeping a second table beside `_ROLE_PROMPTS`.
        # A parallel table is one edit away from disagreeing with the prompt it
        # is supposed to mirror, and the disagreement would show up as a
        # false "not attested" on a run that did everything right.
        m = re.search(r"subagent_type='([^']+)'", _ROLE_PROMPTS.get(role, ""))
        want = m.group(1) if m else "hs:%s" % role
        # `spawn_provenance` owns its own format — this used to reach past it
        # into `_store_path()` and walk the lines here, a fourth hand-written
        # parser agreeing with the writer only by coincidence.
        # TRI-STATE, and the third state is why this is not a bare `if`.
        # `None` means the store could not be READ (a directory at the path, a
        # permission fault) — "I could not look" is not "it did not happen", and
        # collapsing it into False turns a strict check into a wrong one, even in
        # block mode.
        seen = spawn_provenance.has_spawn(session=session, run_id=run_id,
                                          subagent_type=want, since=since, until=until)
        if seen is None:
            return None, ("the spawn ledger path exists but is not a readable "
                           "file — no evidence either way")
        if seen:
            return True, None
        return False, ("no record of a `%s` Task spawn against this prompt call "
                        "(no evidence either way — the hook may be off, the "
                        "state dir may differ, or the session may have been "
                        "resumed)" % want)
    except Exception:
        # Fail-OPEN even in block mode: "I could not look" is not "it did not
        # happen", and a gate that conflates them is not strict, it is wrong.
        # The THIRD return value says so — `None` means "unreadable", distinct
        # from `False` ("read it, found nothing"), and only `False` may block.
        # Collapsing the two is how a broken store becomes an outage.
        return None, ("could not read the spawn ledger — no evidence either way")


def _record(ns) -> dict:
    if ns.supersede:
        return hs_run_next_command.finalize(
            _cmd_supersede_impl(ns.ctx.root, ns.role, phase=ns.phase, plan=ns.plan))
    result = _cmd_record_impl(ns.ctx.root, ns.role, out=ns.out, phase=ns.phase,
                              plan=ns.plan)
    if result.get("state") == "recorded":
        role, plan_dir = result.get("role"), result.get("plan")
        warnings = [w for w in (result.get("warnings") or [])
                    if w is not _HARD_CASE_WARNING]
        prompt = _matching_prompt(plan_dir, role, ns.phase) if plan_dir else None
        # With no paired prompt, the posture that prompt would have answered: a plan
        # built in the main session owes no spawn whether or not anyone asked first.
        unpaired = (_phase_posture(Path(plan_dir), None, ns.phase, (), ns.ctx.root)[0]
                    if role == "developer" and plan_dir and prompt is None else None)
        if role == "developer" and (
                (prompt is not None and prompt.get("posture_mode") == "in_place")
                or (unpaired is not None and unpaired.mode == "in_place")):
            # The phase is built in place: no spawn was asked for, so there is
            # nothing to attest and nothing to refuse.
            result["spawn_expected"] = False
            result["warnings"] = warnings + ([
                "no `hs-run cook prompt developer --phase %s` is recorded; the phase's "
                "posture answers in place (%s), so no spawn is owed"
                % (ns.phase, unpaired.reason)]
                if unpaired is not None else [])
            return hs_run_next_command.finalize(result)
        result["spawn_expected"] = True
        if plan_dir and prompt is None:
            attested, note = False, (
                "no `hs-run cook prompt %s%s` is recorded for plan %s, so this "
                "record has no prompt call to pair with and a real "
                "Task(subagent_type=...) spawn cannot be attested against it (no "
                "evidence either way)"
                % (role, " --phase %s" % ns.phase if ns.phase else "", plan_dir))
        else:
            attested, note = _attest_spawn(role, prompt, plan_dir)
            if (attested is False and plan_dir and prompt is not None
                    and prompt.get("posture_mode") == "subagent"):
                note += ("; the paired prompt asked for a subagent — if this plan's phases "
                         "are built in the main session, declare `posture: in_place` in "
                         "%s's frontmatter and prompt again with no subagent --flags "
                         "(hs-run cook prompt developer --plan %s --phase %s): that prompt "
                         "asks for no spawn, and a record after it owes none"
                         % (Path(plan_dir) / "plan.md", plan_dir, ns.phase))
        result["spawn_attested"] = attested
        if attested is True:
            # The blanket warning is RETIRED on an attested run rather than kept
            # "just in case": a caveat that fires unconditionally trains the
            # reader to skip it, and then it is not there when it matters.
            result["warnings"] = warnings
        else:
            result["warnings"] = warnings + [note]
            # `attested is None` = the ledger could not be READ. Only a clean
            # read that found nothing (`False`) may block; an unreadable store
            # says nothing about whether a spawn happened.
            if (attested is False
                    and (os.environ.get(_ATTEST_MODE_ENV) or "").strip().lower() == "block"):
                # A deliberate refusal, not BROKEN: the ingest itself worked
                # (the file was read, `recorded` was reachable) and this mode
                # chooses, on purpose, not to advance past it without disk
                # evidence of a real spawn.
                result["exit_code"] = EXIT_REFUSED
                result["next_action"] = note
    return hs_run_next_command.finalize(result)


RECORD = hs_run.verb(_cook_door(_record), options=("plan", "out", "phase"), optional=("role",),
                    flags=("supersede",),
                    helps={"plan": _PLAN_HELP,
                           "supersede": "retire this role's earlier records for --phase "
                                        "(a build that never finished); takes no --out"})


def _cmd_supersede_impl(root, role, *, phase=None, plan=None) -> dict:
    """Append a `kind: supersede` line: the phase's earlier `role` records stop
    counting, so `cook next` asks for the build again."""
    if role not in _ROLE_PROMPTS:
        return hs_run.StepResult(
            state="error",
            next_action="usage: hs-run cook record <role> --phase <id> --supersede "
                        "(known roles: %s)" % ", ".join(sorted(_ROLE_PROMPTS)),
            artifacts=[], warnings=[], exit_code=EXIT_BROKEN).as_dict()
    plan_dir, miss = _resolve_plan_dir_verbose(Path(root), explicit=plan)
    if plan_dir is None:
        return hs_run.StepResult(state="plan_unresolved", next_action=_no_plan_message(miss),
                                 artifacts=[], warnings=[], exit_code=EXIT_BROKEN).as_dict()
    why = _phase_refusal(plan_dir, phase)
    if why:
        return _unknown_phase(plan_dir, why)
    retired = len(_live_records(plan_dir, role).get(phase) or [])
    import hook_runtime
    _append_record_log_entry(plan_dir, SupersedeLogEntry(
        role=role, phase=phase, actor=hook_runtime.resolve_actor(),
        ts=write_verification._now_iso()).as_dict())
    nxt = "hs-run cook next --plan %s" % plan_dir
    return hs_run.StepResult(
        state="superseded",
        next_action=nxt,
        next_command=nxt,
        artifacts=[str(_record_log_path(plan_dir))],
        warnings=[] if retired else ["no live %s record for phase %s — nothing to retire"
                                     % (role, phase)],
        exit_code=EXIT_NEEDS_STEP,
        extra={"role": role, "phase": phase, "retired": retired},
    ).as_dict()


def _cmd_record_impl(root, role, *, out=None, phase=None, plan=None) -> dict:
    """`hs-run cook record <role> [--plan <dir>] --out <file> [--phase <id>]` — beat 2. Reads
    the subagent's report file and advances; it never claims the file's
    content actually came from a real Task-tool spawn (Hard Case #1) — that
    is a transcript fact this CLI has no window into (the
    reasoning about why a CLI cannot see who spawned what).

    A genuine mutation, not just a read: once `--out` is confirmed to exist
    this appends one line to the append-only record ledger
    (`_append_record_log`) — `role`, `phase`, the `--out` path, and its
    sha256. `_review_disposition` reads this ledger for the `code-reviewer`
    role before a `review-decision` artifact alone may clear the review
    obligation, so writing nothing here is what let a hand-written artifact
    close a plan no `record` call ever touched. Best-effort: an unresolvable
    plan degrades this to a no-op rather than failing the beat, with a
    warning naming why the line did not land.

    Exit tier (D10/L3) depends on the role: `developer`'s own follow-up
    (`cook verify --verdict <PASS|BLOCKED> ...`) needs a MODEL to read the
    report and choose the verdict — EXIT_NEEDS_MODEL. Every other role's
    follow-up is just `hs-run cook next` (pure re-projection, no judgment
    left) — EXIT_NEEDS_STEP."""
    if not role:
        return hs_run.StepResult(
            state="error",
            next_action="usage: hs-run cook record <role> --out <file> [--phase <id>]",
            artifacts=[],
            warnings=["missing role"],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    if role not in _ROLE_PROMPTS:
        return hs_run.StepResult(
            state="error",
            next_action="unknown role %r (known: %s)"
                            % (role, ", ".join(sorted(_ROLE_PROMPTS))),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    out_path = out
    if not out_path:
        return hs_run.StepResult(
            state="error",
            next_action="record needs --out <file>",
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    if not Path(out_path).is_file():
        return hs_run.StepResult(
            state="error",
            next_action="--out file does not exist: %s" % out_path,
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    plan_dir, miss = (_resolve_plan_dir_verbose(Path(root), explicit=plan)
                      if root else (None, None))
    why = _phase_refusal(plan_dir, phase)
    if why:
        return _unknown_phase(plan_dir, why)
    ledger_problem: "str | None"
    kept_note: "str | None" = None
    if plan_dir is None:
        ledger_problem = ("record ledger line not written: %s"
                          % _no_plan_message(miss))
    else:
        ledger_problem, kept_note = _append_record_log(plan_dir, role, phase, out_path, root)
    plan_arg = " --plan %s" % plan_dir if plan_dir is not None else ""
    if role == "developer":
        follow_up = "hs-run cook verify%s --phase %s %s" % (
            plan_arg, phase or "<id>", _VERIFY_PLACEHOLDERS)
        tier = EXIT_NEEDS_MODEL
        # Null, not the sentence above: it carries `<PASS|BLOCKED>` placeholders
        # a model must resolve by READING the report — that judgment is exactly
        # why this branch is tier 10. Handing back a string with angle brackets
        # in it would be a command that fails at the shell, dressed as one that
        # runs.
        follow_up_cmd = None
    else:
        follow_up = "hs-run cook next%s" % plan_arg
        tier = EXIT_NEEDS_STEP
        follow_up_cmd = follow_up
    claim = (_review_claim(plan_dir, root)
             if role == "code-reviewer" and plan_dir is not None and not ledger_problem
             else None)
    unwritten = claim[0] if claim and not claim[1] else None
    if unwritten:
        # The recorded review's verdict must be READ from its report to be written:
        # a judgment, so the step is the model's.
        follow_up, follow_up_cmd, tier = unwritten, None, EXIT_NEEDS_MODEL
    extra = {"role": role}
    if plan_dir is not None:
        extra["plan"] = str(plan_dir)
    return hs_run.StepResult(
        state="recorded",
        next_action=follow_up,
        next_command=follow_up_cmd,
        artifacts=[out_path],
        warnings=[_HARD_CASE_WARNING] + [w for w in (ledger_problem, kept_note) if w],
        exit_code=tier,
        extra=extra,
    ).as_dict()


# ---------------------------------------------------------------- finalize

def _run_commit_ends(plan_dir: Path, root: "Path | None" = None):
    """(base, tails, measurable) for the WHOLE run, from the append-only
    phase-commits log — ONE helper so the gate and any future reader draw
    the same bracket from the same lines.

    base: the FIRST `open` record's mark taken in `root`'s tree (any tree when
    `root` is None or holds none), first-wins (`_phase_open_mark`'s own rule —
    the boundary must never move once set).

    the tail: the LAST `verify` record's own sha list, last-wins across ALL
    phases — the same rule `_phase_commit_shas` applies per phase, applied
    to the run. An empty list on that last record is NOT skipped over to an
    earlier phase's sha: the self-heal shape (`_record_phase_commits`: a
    phase with no open mark records `sha: []`) means the run's TAIL was
    never sized, and measuring a shorter range would silently waive work
    the gate never saw (reproduced once: 300 lines
    outside the bracket drew `required:false`). `sha: []` on the last record therefore
    means measurable=False.

    An empty verify record that is NOT the last one stays harmless — a
    later record supersedes it, exactly as per phase.

    tails: that last record's first sha, then the HEAD the record was written at when
    it differs (a `--sha` record keeps it). `--sha` is the caller's own list, so it
    alone never decides how much work the bracket holds."""
    records = _read_phase_commits_records(plan_dir)
    marked = [r for r in _opens(plan_dir) if r.get("mark")]
    first = (_in_tree(marked, root) if root is not None else None) or (marked[0] if marked else None)
    base = str(first["mark"]) if first else None
    shas, head = None, None
    for r in records:
        if r.get("kind") == "verify":
            shas = [str(s) for s in (r.get("sha") or []) if s]
            head = str(r.get("head") or "") or None
    measurable = base is not None and shas is not None and bool(shas)
    # newest-first, as `rev-list` and `--sha` both write it: the tail is shas[0]
    tails = list(dict.fromkeys(t for t in ((shas[0] if shas else None), head) if t))
    return base, (tails if shas else []), measurable


@_attrs.define(frozen=True, kw_only=True)
class ReviewGate:
    """`_review_gate`'s answer — a declared object, not a hand-built dict
    (a field misspelled is a TypeError where it was typed, not a None at a
    reader three modules away)."""
    required: bool
    triggers: list = _attrs.field(factory=list)
    why: "str | None" = None

    def as_dict(self) -> dict:
        return {"required": self.required, "triggers": list(self.triggers),
                "why": self.why}


@_attrs.define(frozen=True, kw_only=True)
class ReviewDisposition:
    """`_review_disposition`'s answer: cleared by the artifact (`record`),
    by a measured waiver (`waiver` trace), or not cleared (`triggers` +
    `why`)."""
    cleared: bool
    record: "dict | None" = None
    waiver: "str | None" = None
    triggers: list = _attrs.field(factory=list)
    why: "str | None" = None
    # The verdict a review-decision on disk carries, cleared or not: what is
    # still owed differs (only `record`, or a fresh review) and the sentence says which.
    on_disk_verdict: "str | None" = None
    # Why a review-decision file that IS there could not be read; None when absent.
    on_disk_problem: "str | None" = None
    # The plan was reopened: no measured waiver stands in for its fresh review.
    reopened: bool = False
    # What the recorded reviews still owe when the decision on disk is not theirs:
    # the latest round's verdict unwritten, or a verify after the review. The
    # sentence names the exact move, so it replaces the generic one.
    owed: "str | None" = None
    # `owed` asks for a new review (not only a verdict or a record): the prompt
    # command rides along, as for any review still owed.
    fresh_review: bool = False

    def as_dict(self) -> dict:
        return {"cleared": self.cleared, "record": self.record,
                "waiver": self.waiver, "triggers": list(self.triggers),
                "why": self.why, "on_disk_verdict": self.on_disk_verdict,
                "on_disk_problem": self.on_disk_problem, "reopened": self.reopened,
                "owed": self.owed}


def _review_gate(plan_dir: Path, root) -> "ReviewGate":
    """The workflow-weight answer for THIS cook run's real diff: is the
    independent review mandatory?

    The diff is bracketed by `_run_commit_ends` over the append-only
    phase-commits log the run itself wrote, so the size being judged is the
    work that actually landed, never a self-report.

    A run that cannot be sized keeps the review MANDATORY — a waiver is
    earned by a measurement, never by the measurement failing, and never by
    the bracket quietly ending short of the run's tail. Same for a reader
    error: the projection must not die at the gate it is supposed to
    inform."""
    import workflow_weight
    base, tails, measurable = _run_commit_ends(plan_dir, root)
    head = tails[0] if tails else None
    if not measurable:
        if base is None:
            why = ("review stays mandatory: no measurable commit range — no "
                   "open mark in the phase-commits log brackets this run's work")
        elif head is None:
            why = ("review stays mandatory: no measurable commit range — the "
                   "last phase verify carries no sha, cannot size the run's tail")
        else:
            why = ("review stays mandatory: no measurable commit range to "
                   "size this run's work")
        return ReviewGate(required=True, triggers=[], why=why)
    verdicts = []
    for tail in tails:
        try:
            verdict = workflow_weight.review(root, base, tail)
        except workflow_weight.WorkflowWeightError as e:
            return ReviewGate(required=True, triggers=[],
                              why="review stays mandatory: sizing the real diff failed: %s" % e)
        if verdict.required:
            return ReviewGate(required=True, triggers=list(verdict.triggers), why=None)
        verdicts.append((tail, verdict))
    head, verdict = max(verdicts, key=lambda tv: tv[1].metrics.code_effective_lines)
    m = verdict.metrics
    return ReviewGate(required=False, triggers=[],
                      why=("real diff %s..%s measured code=%d instruction_md=%d docs_md=%d "
                           "lines, risk tier=%s — under every review threshold"
                           % (base[:8], head[:8], m.code_effective_lines,
                              m.instruction_markdown_lines, m.docs_markdown_lines,
                              verdict.risk_tier)))


def _review_prompt_command(plan_dir: Path) -> str:
    return "hs-run cook prompt code-reviewer --plan %s" % plan_dir


def _review_owed(plan_dir: Path, disposition: "ReviewDisposition") -> "tuple[str, dict]":
    """(sentence, extra) naming what the uncleared plan-level review still owes —
    only `record` when a closeable decision sits on disk unrecorded, else a review.
    `review_prompt_command` rides only when the prompt beat is owed: the rung is
    10, so `next_command` stays null, and this is the field a driver reads instead
    of picking the command out of the sentence."""
    record = ("hs-run cook record code-reviewer --plan %s --out <file>" % plan_dir)
    verdict = disposition.on_disk_verdict
    if disposition.owed:
        return disposition.owed, ({"review_prompt_command": _review_prompt_command(plan_dir)}
                                  if disposition.fresh_review else {})
    if verdict in _CLOSEABLE_VERDICTS:
        return ("plan-level review-decision is on disk (verdict %r) but was never "
                "recorded: %s, with --out the reviewer's report" % (verdict, record)), {}
    beats = "%s, then %s" % (_review_prompt_command(plan_dir), record)
    if disposition.reopened and verdict is None:
        head = ("the plan was reopened, so it owes a fresh review (the one it closed "
                "on was moved aside, and no measured waiver stands in): ")
    elif disposition.on_disk_problem:
        head = ("plan-level review-decision is on disk but cannot be read (%s) — "
                "repair it, or review again: " % disposition.on_disk_problem)
    elif verdict is not None:
        head = ("plan-level review-decision says %r, which cannot close the plan — "
                "fix its findings, then review again: " % verdict)
    else:
        head = "plan-level review-decision missing: "
    return head + beats, {"review_prompt_command": _review_prompt_command(plan_dir)}


def _review_disposition(plan_dir: Path, root) -> "ReviewDisposition":
    """ONE predicate, BOTH doors (`next`'s projection and `close`'s refusal):
    is this run's independent-review obligation satisfied?

    Cleared EITHER by a closeable review-decision artifact BACKED BY a real
    `hs-run cook record code-reviewer` ledger line (the reviewed path) OR by
    a measured waiver from `_review_gate` (the small-and-low-risk path,
    untouched by the ledger requirement — it never claimed a review
    happened). One function on purpose — the defect this exists to
    prevent was exactly two call sites each re-deriving "is review
    needed": `next` waived on the measured diff and pointed at `close`,
    `close` re-checked only the artifact and refused. Two commands, one
    tree, two verdicts.

    The artifact alone used to be enough: anyone (including the reviewing
    subagent itself) can write `review-decision.json` with a text editor, so
    presence proved nothing about whether `record` — the CLI's own beat 2 —
    ever ran. A `code-reviewer` line in `_read_record_log` is the disk fact
    that it did; its absence degrades a would-be-closeable artifact to
    `cleared=False`, naming the exact command that would fix it, rather than
    silently trusting the artifact the way this used to."""
    rec, problem = artifact_check._load_artifact(plan_dir, "review-decision")
    verdict = rec.get("verdict") if isinstance(rec, dict) else None
    # A decision that is there but unreadable, verdict-less, or that cannot close
    # is an answer, not an absence: no measured waiver stands in for it.
    if problem not in (None, "missing") or (rec is not None and verdict is None):
        problem = problem if problem not in (None, "missing") else "it carries no verdict"
        return ReviewDisposition(cleared=False, on_disk_problem=problem,
                                 why="review-decision cannot be read: %s" % problem)
    # A recorded review that owes its verdict is an answer, even with no decision on
    # disk: no measured waiver stands in for a review that happened.
    claim = _review_claim(plan_dir, root)
    if claim and not claim[1]:
        return ReviewDisposition(cleared=False, on_disk_verdict=verdict, owed=claim[0],
                                 why=claim[0])
    if verdict is not None and verdict not in _CLOSEABLE_VERDICTS:
        return ReviewDisposition(cleared=False, on_disk_verdict=verdict,
                                 why="review-decision verdict %r cannot close the plan"
                                     % verdict)
    if verdict in _CLOSEABLE_VERDICTS:
        if _has_recorded_role(plan_dir, "code-reviewer"):
            if claim:
                return ReviewDisposition(cleared=False, on_disk_verdict=verdict,
                                         owed=claim[0], why=claim[0], fresh_review=True)
            return ReviewDisposition(cleared=True, record=rec, on_disk_verdict=verdict)
        return ReviewDisposition(
            cleared=False, on_disk_verdict=verdict,
            why=("review-decision exists (verdict %r) but `hs-run cook record "
                 "code-reviewer --out <file>` was never run for this plan — an "
                 "artifact on disk is not proof the CLI's own record beat ran"
                 % rec.get("verdict")),
        )
    if phase_commits_log.reopens(plan_dir):
        return ReviewDisposition(
            cleared=False, reopened=True,
            why=("the plan was reopened: its review-decision was moved aside and a "
                 "fresh, recorded review is owed — a measured waiver does not apply"))
    gate = _review_gate(plan_dir, root)
    if not gate.required:
        return ReviewDisposition(cleared=True, waiver=gate.why)
    return ReviewDisposition(cleared=False, triggers=gate.triggers, why=gate.why)


_PLAN_GATE_KEYS = ("gate_scope", "summary_numbers", "review_prompt_command",
                   "missing_grid_phases")


def _finalize_projection(plan_dir: Path, root, comp: dict, forced_phase=None) -> dict:
    """`_finalize_state`'s answer, stamped ONCE for every door that asks (`next`,
    `status`): the gate is the plan's (`gate_scope`), the count it rests on
    (`summary_numbers`), and — when a passed phase was named — that phase's own
    standing first, so a plan-level gate never reads as that phase's gap."""
    paused = sorted((str(n) for n in comp.get("paused_phases") or ()), key=plan_graph.phase_sort_key)
    held = "paused, not counted: %s" % ", ".join(paused) if paused else ""
    numbers = "%d/%d phase(s) passed" % (len(comp["passed_phases"]), comp["n_total"])
    head = "every counted phase passed (%s)" % held if held else "every phase passed"
    result = _finalize_state(plan_dir, root, head)
    result.setdefault("gate_scope", "plan")
    result.setdefault("summary_numbers", "%s; %s" % (numbers, held) if held else numbers)
    if forced_phase is None:
        return result
    if forced_phase in (result.get("phases") or ()):
        result.update(phase=forced_phase, next_action=(
            "phase %s was reopened and owes its verify again — %s"
            % (forced_phase, result["next_action"])))
        return result
    passed, records = _passed_sentence(plan_dir, forced_phase)
    result.update(records)
    if records.get("snapshot_note"):
        result["artifacts"] = list(result.get("artifacts") or []) + [records["latest_record"]]
    if result["state"] == "completed":
        action = "%s and the plan is closed — nothing left" % passed
    elif forced_phase in (result.get("missing_grid_phases") or ()):
        action = ("phase %s passed verification but its own executor micro-grid is "
                  "missing — the plan's close gate wants one per phase: %s"
                  % (forced_phase, result["next_action"]))
    else:
        action = "%s; the gate left belongs to the plan: %s" % (passed, result["next_action"])
    result.update(phase=forced_phase, phase_state="phase_passed", next_action=action)
    return result


def _finalize_state(plan_dir: Path, root, head: str) -> dict:
    """Cook Steps 4-6's projection once every phase has passed: review (6c) ->
    the 6e phase-count grid gate (E27's SECOND path, distinct from
    `preflight`'s macro check) -> close (6g). Steps 4 (test) and 6f
    (docs/git) fold into these same disk facts ("no dedicated
    artifact... folded into verification.json" / "nothing distinguishes
    spawned via Task from written inline") — this function never invents a
    disk check for either, it only warns that they are unverifiable.

    C4: this is the path that ACTUALLY EXECUTES even when
    `write_verification`'s auto-finalize already flipped `plan.md` to
    `completed` before review/grid ever ran — `NEXT` calls this
    regardless of what the frontmatter already claims (see this module's
    docstring + `NEXT`), so review-decision and the phase-grid gate are
    enforced on the SAME call that a driver actually makes, not only on a
    `close` call that may never happen.

    The review stop is EARNED BY MEASUREMENT, not unconditional: with no
    review-decision on disk, `_review_gate` sizes this run's real diff and
    the state stays `needs_review` only when a threshold or the risk tier
    says so — a run that measured small and low-risk falls THROUGH to the
    grid gate and close, carrying the waiver and its numbers in warnings
    so the skipped gate stays visible instead of silent."""
    reverify = _reverify_owed(plan_dir)
    if reverify:
        return hs_run.StepResult(
            state="needs_reopen_verify", next_action="%s — %s" % (head, reverify[0]),
            artifacts=[str(plan_dir)], warnings=[], exit_code=EXIT_NEEDS_MODEL,
            extra={"phases": reverify[1]}).as_dict()
    if (defer_suite_carry.resolve_defer_suite_carry(plan_dir)["carried"]
            and _suite_debt_unpaid(plan_dir)):
        action, debt_warnings = _debt_repay_message(Path(root))
        return hs_run.StepResult(
            state="needs_suite_run", next_action="%s — %s" % (head, action),
            artifacts=[str(plan_dir)], warnings=debt_warnings,
            exit_code=EXIT_NEEDS_MODEL).as_dict()
    rec = None
    review_waiver = None
    disposition = _review_disposition(plan_dir, root)
    if not disposition.cleared:
        owed, owed_extra = _review_owed(plan_dir, disposition)
        return hs_run.StepResult(
            state="needs_review",
            # One review-decision for the whole plan, never one per phase: a
            # driver that asked about a phase read this as that phase's gap.
            next_action="%s — %s" % (head, owed),
            extra=owed_extra,
            artifacts=[str(plan_dir)],
            warnings=(["Step 4 (test) delegation cannot be verified from disk either "
                       "way — " + _HARD_CASE_WARNING]
                      + disposition.triggers
                      + ([disposition.why] if disposition.why else [])),
            exit_code=EXIT_NEEDS_MODEL,
        ).as_dict()
    if disposition.waiver:
        review_waiver = ("independent review waived after measuring the real diff — "
                         + disposition.waiver)
    else:
        rec = disposition.record

    msg, code, missing = grid_emit_guard.phase_grid_gate(plan_dir)
    if code != 0:
        return hs_run.StepResult(
            state="needs_phase_grid_gate",
            extra={"missing_grid_phases": missing},
            next_action=msg,
            # The ONE state in this domain where the REMEDY is not expressible
            # as a verbatim command: `grid_engine.py build` refuses hand-typed
            # axis values on purpose (`build_executor_axes: at least one step
            # must be supplied (axis values must come from plan, not
            # hand-typed)`), so the missing micro-grid needs a model to read the
            # phase file first — there is no fixed string this CLI could hand a
            # driver that would produce it. `next_action` (msg) keeps naming
            # every missing phase and the `grid_engine.py emit --phase <id>`
            # recipe for the human.
            #
            # `next_command` names the gate RE-CHECK, but only for the phases
            # this call found missing — NOT a bare `cook next`. Measured: a bare
            # `cook next` here re-derives the identical state, so a driver
            # looping on 11 spins forever (reproduced: 6/6 iterations, byte-
            # identical envelope). Pointing at the guard's own read-only check
            # instead makes the repeat SELF-LIMITING — it exits 2 while the
            # grids are still missing and 0 once they land, so the loop breaks
            # on the gate's own answer rather than on this projection's.
            #
            # The tier stays 11: re-tiering to 10 was measured and rejected — it
            # contradicts this state's already-tested contract in three test
            # files (test_hs_run_cook.py, test_hs_run.py, and the conformance
            # suite's own exit-11 fixture).
            next_command=("python3 harness/scripts/grid_emit_guard.py --plan "
                          "%s --require-phase-grids" % plan_dir),
            artifacts=[str(plan_dir)],
            warnings=[msg] + ([review_waiver] if review_waiver else []),
            exit_code=EXIT_NEEDS_STEP,
        ).as_dict()

    status = _frontmatter_status(plan_dir)
    if status != "completed":
        return hs_run.StepResult(
            state="needs_close",
            next_action="hs-run cook close --plan %s" % plan_dir,
            next_command="hs-run cook close --plan %s" % plan_dir,
            artifacts=[str(plan_dir)],
            warnings=[review_waiver] if review_waiver else [],
            exit_code=EXIT_NEEDS_STEP,
        ).as_dict()
    return hs_run.StepResult(
        state="completed",
        exit_code=EXIT_DONE,
        next_action="nothing left — plan closed",
        artifacts=[str(plan_dir)],
        warnings=_closed_warnings(rec) + ([review_waiver] if review_waiver else []),
    ).as_dict()


def _phase_snapshot_record(plan_dir: Path, phase: str):
    """The phase's own phase-latest marker as a record, or None.

    NOT verification-<phase>.json: that IS the file whose absence puts a phase
    in this branch, so reading it would answer None exactly when the answer
    matters. `write_verification` maintains phase-latest-<phase>.json on every
    run regardless of verdict, precisely so a per-phase verdict survives a
    LATER phase overwriting the single canonical verification.json —
    `derive_plan_completion._phase_latest_regression` reads it for the same
    reason. Returns None on anything unreadable or on a marker whose `phase`
    disagrees with its filename: that disagreement is corruption, and trusting
    the name over the content would let a copied marker answer for a phase it
    never covered.
    """
    p = plan_dir / "artifacts" / ("phase-latest-%s.json" % phase)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # The SHARED parser, not a second json.loads: one reader means one answer
    # about what a malformed artifact is, and it already fails soft.
    rec, err = artifact_check._parse_artifact_text(text, p)
    if err or not isinstance(rec, dict) or rec.get("phase") != phase:
        return None
    return rec


_EXIT5_EXPLANATION = (
    "exit code 5 on this scoped run means the declared test set "
    "collected no tests, not that the suite is broken")

# Measured: pytest aborts at USAGE with exit 4 when any named path is absent, and
# runs nothing at all — including the files that do exist. Different code, and a
# different instruction, from exit 5 (files loaded, no tests in them), so a run
# that names a not-yet-written path ships this sentence instead of guessing.
_ABSENT_EXIT_EXPLANATION = (
    "exit code 4 here means a declared path is not on disk yet — pytest stops at "
    "usage and runs nothing, including the tests that DO exist, so write the "
    "missing file first; exit code 5 instead means the set loaded but held no "
    "tests, and neither means the suite is broken")

_NO_TEST_PATHS_WARNING = (
    "defer_suite carried but phase %s declares no file that looks like a "
    "test path in plan-graph.yaml — nothing to scope")

_NOTHING_DECLARED_WARNING = (
    "defer_suite carried but phase %s declares no files at all in "
    "plan-graph.yaml — this node's test scope cannot be derived")

_MALFORMED_DECLARATION_WARNING = (
    "defer_suite carried but phase %s's files_to_create/files_to_modify is "
    "wrong-typed in plan-graph.yaml — the value is there but is not a list, so "
    "no scope could be read from it; write it as a YAML list (one `- path` per "
    "line). Advisory only; this does not block")

_ABSENT_IN_SCOPE_WARNING = (
    "defer_suite scoped phase %s to a set that includes declared test path(s) "
    "not yet on disk (%s) — they are named in the command on purpose, so the "
    "run reports them missing instead of passing without them; write them, or "
    "fix the declared paths in plan-graph.yaml")

_UNCLAIMED_WARNING = (
    "phase %s declares test(s) no runner claims by suffix (%s) — they are left out of "
    "the command: run them by hand, or declare a runner whose test_exts covers them")

_DECLARED_ABSENT_WARNING = (
    "defer_suite carried but phase %s declares at least one test path that "
    "does not exist on disk — create the missing test file(s) or fix the "
    "declared paths in plan-graph.yaml. Advisory only; this does not block")


def _phase_scoped_next_action(plan_dir: Path, phase: str, default_next_action: str,
                              root: "Path | None" = None):
    """(next_action, extra_warnings) for `needs_implementation` once
    `defer_suite` (phase 2's own carry contract) is folded in — nodes phase
    1's `graph_test_scope.resolve_test_scope` into the ONE state this plan's
    per-phase loop reaches once per phase, so it stops being orphaned
    (`no-orphan-code`).

    `default_next_action` passes through unchanged, with `extra_warnings ==
    []`, whenever: the flag is not carried (no state may leak scoped
    behavior onto an unflagged plan); the resolved status is
    `declared_absent` (phase 4 owns that advisory, not this function); or the
    resolver itself could not derive a scope (`no_graph`/`unknown_node` —
    genuinely unexpected this far into `_cmd_next_impl`, since the SAME
    plan-graph already parsed cleanly a few lines up, but degrading rather
    than trusting an undecidable scope is the honest answer either way).

    Never executes a test runner itself (the engine COMPUTES the
    command and SAYS it; the agent runs it) and never raises (mirrors both
    wrapped resolvers' own no-raise contract)."""
    carry = defer_suite_carry.resolve_defer_suite_carry(plan_dir)
    if not carry["carried"]:
        return default_next_action, []
    scope = graph_test_scope.resolve_test_scope(plan_dir, phase)
    status = scope["status"]
    if status in ("resolved", "declared_absent"):
        # Declared-but-absent paths go in the command TOO. Test-first means the
        # phase's own new test does not exist yet when the scope is read, and
        # dropping it here ran only the OLD tests and reported green — measured,
        # with no warning at all. Naming it makes the runner say "file not found",
        # which IS the first red a test-first loop is supposed to see.
        #
        # `declared_absent` (NOTHING resolved yet) is the same situation with one
        # file less luck — the PURE test-first shape, where the only declared test
        # is the one this phase is about to write. Falling back to the unscoped
        # default there made the flag stop applying at the phase's reddest moment,
        # and made the two shapes behave differently for no reason a reader could
        # act on.
        named = sorted(set(scope["files"]) | set(scope["declared"]))
        import test_runners
        at = root if root is not None else plan_dir.parent.parent
        try:
            shown: "Path | str" = plan_dir.resolve().relative_to(Path(at).resolve()).as_posix()
        except ValueError:
            shown = plan_dir
        commands, unclaimed = test_runners.commands(at, named, plan=shown, phase=phase)
        cmd = "; ".join(commands) or "(no runner claims a declared test — see warnings)"
        harness = any(c.startswith(test_runners.render(test_runners.HARNESS_VERB)) for c in commands)
        warnings = []
        if unclaimed:
            # a path is not a command: joined into the command line it ran as one
            warnings.append(_UNCLAIMED_WARNING % (phase, ", ".join(unclaimed)))
        if scope["declared"]:
            warnings.append(_ABSENT_IN_SCOPE_WARNING
                            % (phase, ", ".join(sorted(scope["declared"]))))
        return (
            "phase %s has no verification recorded — build it red->green, "
            "then run the test set defer_suite scoped to this phase (not "
            "the full suite): %s. %s. Then: hs-run cook prompt developer "
            "--phase %s, hs-run cook record developer --phase %s --out "
            "<file>, hs-run cook verify --phase %s --verdict ... --check ..."
            % (phase, cmd,
               test_runners.HARNESS_EXIT_NOTE if harness and not any(
                   not c.startswith(test_runners.render(test_runners.HARNESS_VERB)) for c in commands)
               else _ABSENT_EXIT_EXPLANATION if scope["declared"]
               else _EXIT5_EXPLANATION,
               phase, phase, phase)
        ), warnings
    if status == "no_test_paths":
        return default_next_action, [_NO_TEST_PATHS_WARNING % phase]
    if status == "nothing_declared":
        return default_next_action, [_NOTHING_DECLARED_WARNING % phase]
    if status == "malformed_declaration":
        return default_next_action, [_MALFORMED_DECLARATION_WARNING % phase]
    return default_next_action, [
        "defer_suite carried but the test-scope resolver could not derive "
        "phase %s's scope (%s: %s) — falling back to the unscoped hint"
        % (phase, status, scope.get("error") or "no node data")
    ]


def _phase_substate(plan_dir: Path, phase: str, graph: dict):
    """(substate, verdict, post_reason) for a phase:

      - "phase_passed": its `post:` obligation holds — the one predicate
        derive_plan_completion counts a phase done by, asked first, so "passed"
        has a single door whatever record the phase's other files carry.

      - "phase_failed": the canonical verification.json names this phase
        with a non-PASS verdict (red->green ran, verify said no).
      - "phase_needs_reverify": the canonical verification.json names this
        phase with a PASS verdict, but its verification-<phase>.json
        snapshot is missing — completion_state only trusts the snapshot
        (a deleted artifact regresses the projection), so the fix
        is to re-run verify, not to redo the implementation.
      - "phase_needs_post_artifact" (F4): the canonical
        verification.json PASSES for this phase and its snapshot exists, but
        the plan-graph's OWN declared `post:` obligation for this node is
        STILL not satisfied (e.g. an "artifacts/"-prefixed post: entry that
        can never resolve — see `_run_preflight`'s own rejection of that
        authoring mistake — or a second required artifact besides the
        snapshot). Reuses `derive_plan_completion._node_post_satisfied` so
        this branch's reason string is the SAME one completion_state would
        give, never re-derived. Without this branch the code fell through to
        "needs_implementation" — telling the model to redo work that
        already passed, forever (`hs_run_cook.py:583-604`'s own bug).
      - "needs_verification": no verification recorded yet, but `record
        developer --phase <phase>` has run for it — the build was reported
        done and the verdict is what is owed. Without this branch the next
        projection asked the caller to build the phase again.
      - "needs_implementation": no verification recorded for this phase at
        all yet (the common case for a phase not yet started)."""
    snap = plan_dir / "artifacts" / ("verification-%s.json" % phase)
    post = plan_graph.node_artifacts(graph, phase)["post"]
    why = derive_plan_completion._node_post_satisfied(plan_dir / "artifacts", post, phase)
    # The phase's own latest record first: the canonical verification.json is shared by
    # every phase of the plan, and parallel phases overwrite it. It answers only for a
    # plan written before the per-phase marker existed.
    rec = _phase_snapshot_record(plan_dir, phase)
    if rec is None:
        rec, _p = artifact_check._load_artifact(plan_dir, "verification")
    own = isinstance(rec, dict) and rec.get("phase") == phase
    if why is None:
        return "phase_passed", rec.get("verdict") if own else None, None
    if own:
        verdict = rec.get("verdict")
        if verdict not in ("PASS", "PASS_WITH_RISK"):
            return "phase_failed", verdict, None
        if not snap.is_file():
            return "phase_needs_reverify", verdict, None
        return "phase_needs_post_artifact", verdict, why
    if _has_recorded_role(plan_dir, "developer", phase=phase):
        return "needs_verification", None, None
    return "needs_implementation", None, None


def _snapshot_shortfall(plan_dir: Path, phase: str) -> "str | None":
    """Why this phase's own verification-<phase>.json does not count it done,
    or None (counts, absent, or unreadable — the latter two are other states)."""
    snap = plan_dir / "artifacts" / ("verification-%s.json" % phase)
    try:
        rec, err = artifact_check._parse_artifact_text(snap.read_text(encoding="utf-8"), snap)
    except (OSError, UnicodeDecodeError):
        return None
    if err or not isinstance(rec, dict):
        return None
    why = derive_plan_completion.verification_shortfall(rec)
    return "verification-%s.json%s" % (phase, why) if why else None


def _post_artifact_next_action(plan_dir: Path, phase: str, verdict, post_why) -> str:
    """The way out of `phase_needs_post_artifact`. When the phase's own snapshot
    is what falls short, the exit is a passing re-verify — it replaces a snapshot
    that does not count — not an edit to plan-graph.yaml."""
    if _snapshot_shortfall(plan_dir, phase):
        return ("phase %s's snapshot does not count it done: %s — re-run hs-run "
                "cook verify --plan %s --phase %s with a passing verdict and every "
                "check PASS or SKIP: it replaces a snapshot that does not count, "
                "and the replaced record is appended to phase-superseded-%s.jsonl"
                % (phase, post_why, plan_dir, phase, phase))
    return ("phase %s verification verdict is %s (already PASS-equivalent) but its "
            "plan-graph.yaml 'post:' obligation is not satisfied: %s — fix the "
            "post: entry for this node in plan-graph.yaml (a bare filename under "
            "artifacts/, never prefixed with 'artifacts/' itself) or produce the "
            "artifact it names" % (phase, verdict, post_why))


# ------------------------------------------------- per-phase commit log

_PHASE_COMMITS_FILE = phase_commits_log.FILE_NAME


@_attrs.define(frozen=True, kw_only=True)
class PhaseOpenRecord:
    """The `kind: open` line in the append-only phase-commits log — the
    commit-range BOUNDARY a phase's own `verify` later measures from: the
    HEAD mark recorded the moment a phase first becomes `needs_
    implementation`. `mark` is `None` when the worktree's git could not be
    read at all (no `.git`, detached HEAD): the attempt is still recorded,
    so a later call for the same phase never retries the failed
    subprocess. `claimed`: written by a call that named the phase (`next
    --phase`, `prompt developer --phase`) — the tree cooks it; a mark an
    unforced projection took is not a claim."""

    phase: str
    mark: "str | None"
    actor: str
    ts: str
    tree: "str | None" = None
    claimed: bool = False

    def as_dict(self) -> dict:
        out: dict = {"kind": "open", "phase": self.phase, "mark": self.mark,
                     "tree": self.tree, "actor": self.actor, "ts": self.ts}
        if self.claimed:
            out["claimed"] = True
        return out


@_attrs.define(frozen=True, kw_only=True)
class PhaseCommitRecord:
    """The `kind: verify` line: the real commit SHAs THIS
    verify call attributes to `phase`, from its own recorded open mark to
    HEAD (`git rev-list --first-parent --no-merges`). A SEPARATE file from
    the canonical `verification.json` — the record `_phase_substate` reads
    and every LATER phase's own verify call overwrites wholesale — so an
    earlier phase's own line here is never at that write's mercy."""

    phase: str
    sha: list = _attrs.field(factory=list)
    actor: str = _attrs.field(default="")
    ts: str = _attrs.field(default="")
    # The `ts` of the phase's previous verify line, which this one replaces (readers
    # take the last line per phase): a wrong earlier line reads as replaced, in place.
    supersedes: "str | None" = _attrs.field(default=None)
    # HEAD of the tree a `--sha` verify read: the caller's list never makes the run
    # bracket's tail shorter than the work actually there. A computed range already
    # ends at HEAD's own line, so it carries none.
    head: "str | None" = _attrs.field(default=None)
    # The tree this verify read, and the patch-id of each listed commit a review reads
    # (`review_seen`): what a recorded review is judged against, by content.
    tree: "str | None" = _attrs.field(default=None)
    patch_ids: "dict | None" = _attrs.field(default=None)

    def as_dict(self) -> dict:
        out: dict = {"kind": "verify", "phase": self.phase, "sha": list(self.sha),
                     "actor": self.actor, "ts": self.ts}
        if self.supersedes:
            out["supersedes"] = self.supersedes
        if self.head:
            out["head"] = self.head
        if self.tree:
            out["tree"] = self.tree
        if self.patch_ids is not None:
            out["patch_ids"] = dict(self.patch_ids)
        return out


@_attrs.define(frozen=True, kw_only=True)
class PostureFlagsRecord:
    """The `kind: posture_flags` line: the forcing flags the CALLER gave
    `cook prompt developer` for one phase. The nudge has no channel to the
    CLI, so this append-only line is the only way a later main-thread write
    can be judged against the posture the caller actually asked for.
    Written only when flags were given — an absent line means no forcing."""
    phase: str
    flags: list = _attrs.field(factory=list)
    actor: str = _attrs.field(default="")
    ts: str = _attrs.field(default="")

    def as_dict(self) -> dict:
        return {"kind": "posture_flags", "phase": self.phase,
                "flags": list(self.flags), "actor": self.actor, "ts": self.ts}


@_attrs.define(frozen=True, kw_only=True)
class PhaseStatusRow:
    """One phase in `hs-run cook status`: whether its verification passed, the
    commit SHAs its last `verify` recorded (`[]` when never verified), the file
    holding its latest verification, and — when the snapshot kept an earlier
    record — which file each gate reads."""

    phase: str
    passed: bool
    commits: list = _attrs.field(factory=list)
    latest_record: "str | None" = None
    snapshot_note: "str | None" = None

    def as_dict(self) -> dict:
        out = {"phase": self.phase, "passed": self.passed, "commits": list(self.commits)}
        if self.latest_record:
            out["latest_record"] = self.latest_record
        if self.snapshot_note:
            out["snapshot_note"] = self.snapshot_note
        return out


def _phase_record_files(plan_dir: Path, phase: str) -> dict:
    """{latest_record, snapshot_note} for one phase, each present only when it says
    something. The snapshot is first-wins, replaced only as
    `verification_snapshot._supersede` allows, so a reader of
    verification-<phase>.json alone may not see the latest verify."""
    art = plan_dir / "artifacts"
    latest = art / ("phase-latest-%s.json" % phase)
    rec = _phase_snapshot_record(plan_dir, phase)
    if rec is None:
        return {}
    out = {"latest_record": str(latest)}
    kept = art / ("verification-%s.json" % phase)
    try:
        snap, _err = artifact_check._parse_artifact_text(kept.read_text(encoding="utf-8"), kept)
    except (OSError, UnicodeDecodeError):
        snap = None
    if isinstance(snap, dict) and snap != rec:
        out["snapshot_note"] = (
            "verification-%s.json holds an earlier record (ts %s, verdict %s) — the "
            "snapshot completion and the hard-stage snapshot sweep read; first-wins, so a "
            "re-verify replaces it only when it does not count the phase, or the new "
            "one shows the DoD gate strictly more (a passing result file for one more "
            "type, or no name outside test_types where it had one, dropping none). The "
            "latest verify (ts %s, verdict %s) is %s, which completion reads "
            "only to un-count a regression. The DoD test-policy check reads "
            "verification.json, the plan's last write"
            % (phase, snap.get("ts"), snap.get("verdict"), rec.get("ts"),
               rec.get("verdict"), latest.name))
    return out


def _phase_commits_path(plan_dir: Path) -> Path:
    return phase_commits_log.path(plan_dir)


def _append_phase_commits_record(plan_dir: Path, record: dict) -> None:
    """Append-only, never a read-modify-write (CLAUDE.md Store rules): one
    JSON line per call, on a file created on first use. This is a SEPARATE
    file from the canonical `verification.json` — the record `_phase_
    substate` reads and every later phase's own `verify` call overwrites
    wholesale — so an earlier phase's own commit record can never be lost
    to a later phase's write the way its canonical verdict already is."""
    import json as _json
    path = _phase_commits_path(plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(record, sort_keys=True))
        fh.write("\n")


def _read_phase_commits_records(plan_dir: Path) -> list:
    """Every well-formed line, oldest-first (`phase_commits_log.read_records`)."""
    return phase_commits_log.read_records(plan_dir)


def _opens(plan_dir: Path, phase: "str | None" = None) -> list:
    return [rec for rec in _read_phase_commits_records(plan_dir)
            if rec.get("kind") == "open" and (phase is None or rec.get("phase") == phase)]


def _in_tree(recs: list, tree: "Path | None") -> "dict | None":
    """The first record taken in `tree`; failing that, the first one carrying no tree
    (written before marks named theirs). `tree=None`: the first record of all."""
    if tree is None:
        return recs[0] if recs else None
    here = str(Path(tree).resolve())
    return (next((r for r in recs if r.get("tree") == here), None)
            or next((r for r in recs if not r.get("tree")), None))


def _claimed_elsewhere(plan_dir: Path, phase: str, tree: Path) -> list:
    """Trees other than `tree` that claimed `phase`, in first-claim order."""
    here = str(Path(tree).resolve())
    return list(dict.fromkeys(r["tree"] for r in _opens(plan_dir, phase)
                              if r.get("claimed") and r.get("tree") and r["tree"] != here))


def _phase_open_record(plan_dir: Path, phase: str, tree: "Path | None" = None) -> "dict | None":
    """The first `kind: open` record naming `phase` taken in `tree`, whole."""
    return _in_tree(_opens(plan_dir, phase), tree)


def _phase_open_mark(plan_dir: Path, phase: str, tree: "Path | None" = None) -> "str | None":
    """The FIRST `kind: open` record naming `phase` in `tree` — first-wins, since
    the mark is the phase's own commit-range BOUNDARY and must never move once
    set. One mark per tree: a `next` read in the main tree stamps the main
    tree's HEAD, and the lane building the phase stamps its own, so a verify
    measures from the mark of the tree it reads. `None` covers BOTH "no record
    at all" and "a record exists but captured no mark" (`_phase_open_record`
    is not None tells those two apart, for callers that must not retry a failed
    git read)."""
    rec = _phase_open_record(plan_dir, phase, tree)
    mark = (rec or {}).get("mark")
    return str(mark) if mark else None


def _phase_commit_shas(plan_dir: Path, phase: str) -> list:
    """The LAST `kind: verify` record naming `phase` — last-wins, the
    opposite of `_phase_open_mark`'s first-wins: each `verify` call's own
    range runs from the SAME fixed open mark to the CURRENT HEAD at that
    call, so a later record's `sha[]` is a superset of an earlier one for
    the same phase, never a different, disjoint answer. `[]` for a phase
    never verified — the one shipped reader of this append-only log
    (`hs-run cook status`), closing the write-with-no-consumer gap X1's own
    write existed to fix in the first place (see the research report's
    framing: this data is the precondition for measuring recall/parallel-
    safety against real commits, not a live decision `cook` itself makes)."""
    shas: "list | None" = None
    for rec in _read_phase_commits_records(plan_dir):
        if rec.get("kind") == "verify" and rec.get("phase") == phase:
            shas = list(rec.get("sha") or [])
    return shas if shas is not None else []


def _git_head(root: Path) -> "str | None":
    import subprocess
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, env=_git_env())
    except OSError:
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _declared_branch_home(plan_dir: Path, root: Path) -> "tuple[str, str] | None":
    """(branch, worktree) when the plan declares a branch another worktree of this repo
    has checked out and `root`'s HEAD does not hold that branch's tip -- judged by
    ancestry, as the facts' branch warning is, so a work branch cut from the declared
    one is its home too. None otherwise, including a branch no worktree holds (nowhere
    better to point at) and any git that cannot be read."""
    import subprocess
    from hs_run_facts import engine as facts_engine
    parsed = frontmatter_parser.parse_file(Path(plan_dir) / "plan.md")
    branch = facts_engine.declared_branch(parsed.get("frontmatter"))
    if not branch:
        return None

    def git(*argv: str) -> "subprocess.CompletedProcess | None":
        try:
            return subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *argv],
                                  capture_output=True, text=True, env=_git_env())
        except OSError:
            return None

    listed = git("worktree", "list", "--porcelain")
    if listed is None or listed.returncode != 0:
        return None
    home, path = None, None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/%s" % branch:
            home = path
    # a worktree whose directory is gone (prunable: a /tmp tree after a reboot) is no home
    if not home or not Path(home).is_dir() or Path(home).resolve() == Path(root).resolve():
        return None
    tip, head = git("rev-parse", "--verify", "--quiet", "--end-of-options",
                    branch + "^{commit}"), _git_head(root)
    if tip is None or tip.returncode != 0 or not head:
        return None
    held = git("merge-base", "--is-ancestor", tip.stdout.strip(), head)
    if held is not None and held.returncode == 0:
        return None
    return branch, str(Path(home).resolve())


def _mark_phase_open(plan_dir: Path, phase: str, root: Path, *,
                     claimed: bool = False) -> "dict | None":
    """The FIRST time `phase` is projected as `needs_implementation`, stamp
    the CURRENT worktree's own HEAD as that phase's commit-range boundary.
    A repeat call for the SAME phase is a no-op — the mark must never move
    once set, or a phase re-visited after its own commits already landed
    would silently narrow its own range on the next verify.

    Scoped to ONE worktree's own branch by construction: `root` is
    whichever tree `cook next` is actually invoked from, and the record
    names it, so two lanes (two worktrees, two branches) — or the main
    tree projecting the plan and the lane building it — each stamp and
    read their OWN HEAD. This is the one write a `next` makes; it never
    changes what any `next` reports.

    Never raises: a repo this cannot read (no git, detached HEAD, a
    corrupt `.git`) still gets an `open` record — with `mark: null` — so
    the ONE failed attempt is recorded rather than retried on every
    subsequent call; the commit-recording path below then degrades to an
    empty commit list for this phase instead of failing verify.

    `claimed` on a tree that already holds an unclaimed mark appends a claim
    carrying that SAME mark: the boundary stays first-wins. Returns the record
    it appended, or None when it wrote nothing."""
    import hook_runtime
    import harness_checks
    if harness_checks.marks_removed(plan_dir):
        return None
    here = str(Path(root).resolve())
    first = _phase_open_record(plan_dir, phase, root)
    if first is not None:
        if not claimed or any(r.get("claimed") and r.get("tree") == here
                              for r in _opens(plan_dir, phase)):
            return None
        head = first.get("mark")
    else:
        head = _git_head(root)
    record = PhaseOpenRecord(
        phase=phase, mark=head, tree=here, claimed=claimed,
        actor=hook_runtime.resolve_actor(), ts=write_verification._now_iso(),
    ).as_dict()
    _append_phase_commits_record(plan_dir, record)
    return record


def _phase_commits_since_mark(root: Path, mark: str) -> list:
    """`git rev-list --first-parent --no-merges <mark>..HEAD` on `root`'s
    CURRENT branch.

    `--first-parent`: without it, a lane branch that merges main mid-phase
    pulls every commit reachable ONLY through main's own side of that merge
    into the range too — measured on a real 3-times-merged branch, 12
    commits where 7 were the lane's own. `--first-parent` walks only the
    lane's own history, so a commit that entered solely via `git merge
    main` is never counted as this phase's own work.

    `--no-merges`: the merge commit ITSELF (main's history entering the
    lane) is not one of the phase's own commits either — a merge with no
    conflicts and no manual changes of its own is bookkeeping, not
    authored work.

    Empty list on any git failure or on 0 commits since the mark — both
    real, reportable answers (a phase that opened and closed with no new
    commits is not an error), never conflated with "could not compute"."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", *_GIT_SAFE, "-C", str(root), "rev-list", "--first-parent", "--no-merges",
             "%s..HEAD" % mark],
            capture_output=True, text=True, env=_git_env())
    except OSError:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _listed(items: list, n: int, noun: str = "") -> str:
    """The first `n` of `items`, comma-joined, then how many were left out: a list in a
    warning is read by an agent whose budget a few thousand characters of branch names
    spends for nothing."""
    more = len(items) - n
    tail = " … +%d more%s" % (more, " " + noun if noun else "") if more > 0 else ""
    return ", ".join(items[:n]) + tail


def _git_stdout(root: Path, *args: str) -> "str | None":
    """`git -C root <args>`'s stdout, or None when git fails or cannot start."""
    import subprocess
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *args],
                           capture_output=True, text=True, env=_git_env())
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def _held_by_other_branches(root: Path, mark: str, shas: list) -> "dict[str, list]":
    """{sha: [local branch, ...]} for each of `shas` that another local branch
    already holds. Such a commit is that branch's history, not this tree's work.
    Without this filter, an open mark older than the base this branch was cut from
    counts the base's own commits. A branch whose history holds HEAD is this tree's
    own line, not "another": the checked-out branch, the branch a detached worktree
    sits on, a branch the phase's work was already merged into. Remote refs are left
    out, so a phase's own pushed commits are never read as someone else's. Returns
    {} on any git failure (the range stays unfiltered)."""
    def git(*a):
        return _git_stdout(root, *a)

    if not shas:
        return {}
    out: "dict[str, list]" = {}
    for ref in (git("for-each-ref", "--format=%(refname)", "--no-contains=HEAD",
                    "refs/heads") or "").split():
        listed = git("rev-list", ref, "--not", mark)
        for sha in set((listed or "").split()) & set(shas):
            out.setdefault(sha, []).append(ref[len("refs/heads/"):])
    return out


def _record_posture_flags(plan_dir, phase, flags) -> None:
    """Append the caller's forcing flags for `phase` to the phase-commits log of
    the plan `prompt` resolved. Best-effort: a log that cannot be written must not
    take the prompt beat down — the nudge simply stays flags-blind, exactly as
    before this record existed."""
    try:
        import hook_runtime
        _append_phase_commits_record(Path(plan_dir), PostureFlagsRecord(
            phase=phase, flags=[str(f) for f in flags],
            actor=hook_runtime.resolve_actor(),
            ts=write_verification._now_iso(),
        ).as_dict())
    except Exception:  # noqa: BLE001 — bookkeeping, never the beat
        pass


_NOT_IN_HISTORY = "not in HEAD's history"


def _off_own_line(root: Path, shas: list) -> list:
    """[(sha, why)] for each of `shas` that is not this branch's own work: a merge
    commit, or a commit off HEAD's first-parent line (it came in through a merge).
    The same line the computed range walks (`_phase_commits_since_mark`). [] when
    git cannot answer — the names were already resolved, so nothing is guessed."""
    def git(*a):
        out = _git_stdout(root, *a)
        return None if out is None else out.split()

    line = git("rev-list", "--first-parent", "HEAD")
    merges = git("rev-list", "--no-walk", "--merges", *shas)
    if line is None or merges is None:
        return []
    line_set, merge_set = set(line), set(merges)
    return [(c, "a merge" if c in merge_set
             else "not on HEAD's first-parent line" if _is_ancestor(root, c)
             else _NOT_IN_HISTORY)
            for c in shas if c in merge_set or c not in line_set]


def _own_line_listing(root: Path, mark: "str | None") -> str:
    """What the refusal's `rev-list` answers, run rather than left to the reader: a
    bare "lists them" with an empty field beside it read as "none" on a line of 99."""
    if mark and not _is_ancestor(root, mark):
        # A rewrite since the phase opened: the range would count the base's commits
        # as the phase's own.
        return ("does not list them here: the phase's mark %s is not in HEAD's history "
                "(rewritten since the phase opened), so the range takes in other work too"
                % mark[:12])
    out = _git_stdout(root, "rev-list", "--first-parent", "--no-merges",
                      "%s..HEAD" % mark) if mark else None
    if out is None:
        return "lists them"
    own = out.split()
    if not own:
        return "lists none: the line has no commit of its own since the phase's mark"
    return "lists %d commit(s), newest first: %s" % (
        len(own), _listed([c[:9] for c in own], 5, "commit(s)"))


def _where_they_are_own(root: Path, plan_dir: Path, phase: "str | None",
                        off: list, kept: list) -> str:
    """The refusal's way out for named non-merge commits off this HEAD's first-parent
    line (`off`, those `kept` among them). One not in HEAD's history is not this
    tree's to keep: merge it first, and the next refusal reckons with it. One another
    phase's last verify recorded is that phase's. The rest may be this phase's own,
    built on another line and merged here. A phase keeps its last verify's list only,
    so the way out names them all, kept ones too: a verify from a worktree whose HEAD
    is the newest records them when that one line holds them all, and this tree then
    keeps them. Which line built the phase the engine cannot tell (a lane merged into
    the integration branch and the reverse have one shape), so for commits merged here
    it states the rule and the flags. Commits not in HEAD's history at all go to the ONE
    live worktree whose first-parent line holds every one of them, when there is one
    (`_worktree_holding`); otherwise the advice is to merge them here first."""
    if not phase or not off:
        return ""
    def counted(n: int, rest: str) -> str:
        return "%d %s %s" % (n, "is" if n == 1 else "are", rest)

    said = ""
    taken: dict = {}
    for rec in _read_phase_commits_records(plan_dir):
        if rec.get("kind") == "verify" and rec.get("phase") not in (None, phase):
            taken[str(rec["phase"])] = set(rec.get("sha") or [])
    owner = {c: p for p, recorded in sorted(taken.items()) for c in recorded}
    others = [c for c, why in off
              if why != _NOT_IN_HISTORY and c not in kept and c in owner]
    if others:
        said += ". Not this phase's, by another phase's last verify: %s" % _listed(
            ["%s is phase %s's" % (c[:9], owner[c]) for c in others], 5, "commit(s)")
    unmerged = [c for c, why in off if why == _NOT_IN_HISTORY]
    if unmerged:
        read = ". HEAD read: %s" % _tree_and_branch(root)
        holder = _worktree_holding(root, [c for c, _why in off])
        if holder is not None:
            # Only that worktree's line holds them: it is where the phase was built,
            # and merging into a tree it was never built in is not the way out.
            return said + (". %s; %s (branch %s) holds all of them on its own line: run "
                           "this verify from %s%s" % (
                               counted(len(unmerged), _NOT_IN_HISTORY), holder[0],
                               holder[1], holder[0], read))
        # A way out for the merged ones now would be replaced by the verify that takes
        # these after their merge: one verify, once all are here, keeps them all.
        return said + ". %s: merge %s here first, then run this verify again%s" % (
            counted(len(unmerged), _NOT_IN_HISTORY), "it" if len(unmerged) == 1 else "them",
            read)
    wanted = [c for c, _why in off if c not in others]
    if not set(wanted) - set(kept):
        return said
    out = _git_stdout(root, "merge-base", "--independent", *wanted)
    newest = out.split() if out is not None else []
    line = (_git_stdout(root, "rev-list", "--first-parent", newest[0])
            if len(newest) == 1 else "")
    if out is None or line is None:
        return said + (". git could not say whether one line holds all %d of these, so "
                       "this names no verify that records them" % len(wanted))
    if not set(wanted) <= set(line.split()):
        return said + (". A phase keeps the list of its last verify, and no one line holds "
                       "all %d of these on its first-parent history, so no single verify "
                       "records them" % len(wanted))
    return said + (". A phase built in another worktree is verified there, before or after "
                   "its merge, from a worktree whose HEAD is its newest commit (`git "
                   "worktree add --detach <dir> %s` makes one): run this verify there with "
                   "--phase %s --plan %s --sha %s; once it has recorded them, a verify here "
                   "keeps them as the phase's own"
                   % (newest[0][:12], shlex.quote(str(phase)),
                      shlex.quote(str(Path(plan_dir).resolve())),
                      ",".join(c[:12] for c in wanted)))


def _tree_and_branch(root: Path) -> str:
    """`<tree> (branch <b>)`, or `(detached at <sha>)`: which HEAD a verify read."""
    branch = (_git_stdout(root, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()
    if branch:
        return "%s (branch %s)" % (Path(root).resolve(), branch)
    head = (_git_stdout(root, "rev-parse", "--short=12", "HEAD") or "?").strip()
    return "%s (detached at %s)" % (Path(root).resolve(), head)


def _worktree_holding(root: Path, shas: list) -> "tuple[str, str] | None":
    """(path, branch) of the ONE other worktree whose HEAD's first-parent line holds
    every one of `shas` — the line a verify run there checks them against — else None.
    A worktree whose dir is gone (`prunable`: deleted without `worktree remove`) is no
    place to run anything and is skipped."""
    out = _git_stdout(root, "worktree", "list", "--porcelain")
    if not out or not shas:
        return None
    here = Path(root).resolve()
    found = []
    for block in out.strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path, head = fields.get("worktree"), fields.get("HEAD")
        if (not path or not head or Path(path).resolve() == here
                or any(line.split(" ", 1)[0] == "prunable" for line in block.splitlines())
                or not Path(path).is_dir()):
            continue
        line = _git_stdout(root, "rev-list", "--first-parent", head)
        if line is not None and set(shas) <= set(line.split()):
            found.append((str(Path(path).resolve()),
                          (fields.get("branch") or "").replace("refs/heads/", "") or "detached"))
    return found[0] if len(found) == 1 else None


def _is_ancestor(root: Path, sha: str) -> bool:
    """Whether `sha` is in HEAD's history at all — merged in, not merely recorded."""
    import subprocess
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), "merge-base",
                            "--is-ancestor", sha, "HEAD"],
                           capture_output=True, text=True, env=_git_env())
    except OSError:
        return False
    return r.returncode == 0


def _made_on_other_branches(root: Path, shas: list) -> "dict[str, list]":
    """{sha: [branch, ...]} for each of `shas` on the first-parent line of a branch that
    does not hold HEAD: that branch may have made the commit (its base history, say), or
    the phase's own work was fast-forwarded there — git cannot tell which. A commit
    such a branch holds only through a merge is this line's work merged there, and is
    not named. Local and remote branches count, except this branch's own upstream and
    any remote branch of the same name. {} when git cannot answer."""
    def git(*a):
        return _git_stdout(root, *a)

    if not shas:
        return {}
    me = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    up = (git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") or "").strip()
    want: set = set(shas)
    out: dict = {}
    for ref in (git("for-each-ref", "--format=%(refname:short)", "--no-contains=HEAD",
                    "refs/heads", "refs/remotes") or "").split():
        if ref == up or (me and me != "HEAD" and ref.split("/", 1)[-1] == me) \
                or ref.endswith("/HEAD"):
            continue
        for sha in set((git("rev-list", "--first-parent", ref) or "").split()) & want:
            out.setdefault(sha, []).append(ref)
    return out


# A reflog subject that says the branch moved because a commit was MADE on it. A
# fast-forward (`cherry-pick --ff` included), a reset, a rebase's finish or the
# branch's creation moves it to a commit made elsewhere.
_MADE_HERE = re.compile(
    r"(?:commit(?: \((?:amend|initial|cherry-pick)\))?|revert|am|cherry-pick(?=: (?!fast-forward$)))"
    r":")
# `git branch -c` hands the new branch the source's reflog; entries older than this
# one are the source's.
_COPIED = re.compile(r"Branch: copied ")


def _committed_on_this_branch(root: Path, shas: list) -> set:
    """The `shas` this branch's own reflog records as committed on it. A branch cut
    later from this line holds them without holding HEAD — topology reads that the
    same as this branch cut from a base — but only this branch's reflog says they
    were made here. Empty when HEAD is detached, the reflog is gone or off, or git
    cannot answer: the topology filter then stands alone."""
    def git(*a):
        return _git_stdout(root, *a)

    if not shas:
        return set()
    me = (git("symbolic-ref", "-q", "HEAD") or "").strip()
    if not me:
        return set()
    made: set = set()
    for line in (git("log", "--walk-reflogs", "--format=%H%x09%gs", me, "--")
                 or "").splitlines():                       # newest first
        sha, _, subject = line.partition("\t")
        if _COPIED.match(subject):
            break
        if _MADE_HERE.match(subject):
            made.add(sha)
    return made & set(shas)


def _resolve_named_commits(root: Path, raw: str) -> "tuple[list, list]":
    """(full shas newest-first by ancestry, the names that resolve to no commit) for `--sha`.
    Newest-first is the order `rev-list` gives the computed range, and the run
    bracket reads a record's first sha as its tail."""
    import subprocess
    names = [n for n in re.split(r"[,\s]+", raw) if n]
    full, bad = [], []
    for name in names:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), "rev-parse", "--verify",
                            "--quiet", "%s^{commit}" % name],
                           capture_output=True, text=True, env=_git_env())
        (full.append(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip()
         else bad.append(name))
    if bad or not full:
        return [], bad or names or [raw]

    def depth(sha):  # a descendant always counts more ancestors; commit dates can tie
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), "rev-list", "--count", sha],
                           capture_output=True, text=True, env=_git_env())
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
    return sorted(dict.fromkeys(full), key=depth, reverse=True), []


def _phase_commit_set(plan_dir: Path, phase: str, root: Path, *,
                      named: "list | None" = None) -> "tuple[list, list]":
    """(shas, warnings): the commits THIS verify covers for `phase`, writing nothing,
    so the record's blast radius can be scoped by them before the write. A phase with
    no open mark yet covers none: the mark `_record_phase_commits` sets is HEAD now."""
    warnings: list = []
    opened = _phase_open_record(plan_dir, phase, root)
    elsewhere = [r.get("tree") for r in _opens(plan_dir, phase) if r.get("tree")]
    mark = _phase_open_mark(plan_dir, phase, root) if opened is not None else None
    if named is not None:
        shas = list(named)
        made = _made_on_other_branches(root, shas)
        mine = _committed_on_this_branch(root, list(made))
        made = {c: b for c, b in made.items() if c not in mine}
        if made:
            warnings.append(
                "phase %s: %d named commit(s) also sit on the first-parent line of a branch "
                "that does not hold HEAD: %s — this phase's own work fast-forwarded "
                "there needs nothing; if they are that branch's base history, verify "
                "again without them and the later record replaces this one" % (
                    phase, len(made), _listed(
                    ["%s (%s)" % (c[:9], _listed(sorted(made[c]), 3))
                     for c in shas if c in made], 5, "commit(s)")))
        return shas, warnings
    shas = _phase_commits_since_mark(root, mark) if mark else []
    fix = ("name its commits: hs-run cook verify --plan %s --phase %s ... --sha <c>,<c>"
           % (plan_dir, phase))
    held = _held_by_other_branches(root, mark, shas) if mark else {}
    mine = _committed_on_this_branch(root, list(held))
    held = {c: b for c, b in held.items() if c not in mine}
    if held:
        shas = [c for c in shas if c not in held]
        warnings.append("phase %s: %d commit(s) since its open mark are already on "
                        "another branch, so they are that branch's history and were "
                        "not counted: %s (`git branch --contains <sha>` lists every "
                        "branch) — if they are this phase's, %s"
                        % (phase, len(held), _listed(
                            ["%s (%s)" % (c[:9], _listed(sorted(held[c]), 3))
                             for c in sorted(held)], 5, "commit(s)"), fix))
    here = str(Path(root).resolve())
    if opened is None and elsewhere:
        warnings.append("phase %s's open mark was taken in %s, not in %s which this "
                        "verify reads — it set one here at HEAD now, so its record names "
                        "no commit made before this call; %s"
                        % (phase, ", ".join(dict.fromkeys(elsewhere)), here, fix))
    elif opened is None:
        since = _since_prerequisites(plan_dir, phase, root)
        warnings.append("phase %s had no open mark — this verify set it at HEAD now, so "
                        "its record names no commit made before this call; %s%s"
                        % (phase, fix, "" if since is None else
                           "; the commits since phase %s's last verify, if they are this "
                           "phase's: hs-run cook verify --plan %s --phase %s ... --sha %s"
                           % (since[0], plan_dir, phase, ",".join(c[:12] for c in since[1]))))
    return shas, warnings


def _on_own_line(root: Path, sha: str) -> "str | None":
    """`sha` (full) when it is on HEAD's first-parent line, else the oldest commit on
    that line that has it — the merge that brought it in, whatever the merged branch
    did after `sha`. `<off-line sha>..HEAD --first-parent` also lists this branch's own
    commits from before that merge."""
    full = _git_stdout(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % sha)
    line = _git_stdout(root, "rev-list", "--first-parent", "HEAD")
    if not full or line is None:
        return None
    full, commits = full.strip(), line.split()
    if full in commits:
        return full
    holding = set((_git_stdout(root, "rev-list", "--ancestry-path", "%s..HEAD" % full)
                   or "").split())
    return next((c for c in reversed(commits) if c in holding), None)


def _since_prerequisites(plan_dir: Path, phase: str, root: Path) -> "tuple[str, list] | None":
    """(prerequisite, [sha newest-first]): the commits on this branch's first-parent
    line after the newest commit a prerequisite of `phase` last recorded — the work a
    phase built while its prerequisite still waited for its verify, when no `next`
    ever projected it. None when there is no such prerequisite or nothing since."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    preds = sorted(src for src, dsts in plan_graph.build_adj(graph).items() if phase in dsts)
    best = None
    for pred in preds:
        recs = [r for r in _read_phase_commits_records(plan_dir)
                if r.get("kind") == "verify" and r.get("phase") == pred]
        anchor = (recs[-1].get("sha") or [None])[0] if recs else None
        anchor = anchor or _phase_open_mark(plan_dir, pred, root)
        if not anchor or not _is_ancestor(root, anchor):
            continue
        anchor = _on_own_line(root, anchor)
        if anchor is None:
            continue
        since = _phase_commits_since_mark(root, anchor)
        if best is None or len(since) < len(best[1]):
            best = (pred, since)
    return best if best and best[1] else None


def _record_phase_commits(plan_dir: Path, phase: str, root: Path, *,
                          named: "list | None" = None,
                          computed: "tuple[list, list] | None" = None) -> list:
    """Append THIS verify call's own `{phase, sha[], actor, ts}` to the
    append-only phase-commits log — called once per successful
    `cook verify`, regardless of whether this is the plan's last phase or
    not, so an earlier phase's record is never at the mercy of a later
    phase's own write the way the canonical `verification.json` already is.

    A phase with no recorded open mark self-heals by marking NOW, at the
    current HEAD — this call's own record is then correctly `[]` (nothing
    happened between "just marked" and "right now"), and every SUBSEQUENT
    verify for the same phase computes a real range from that point.
    Degrades to an empty commit list on any git failure; never raises, and
    never blocks a verify that otherwise succeeded over this bookkeeping.

    `named` (`--sha`) replaces the computed range, and the record also keeps the HEAD
    it was written at so the run bracket is never shorter than the work there; a named
    commit another branch's first-parent line holds comes back as a warning. Without
    `named`, the ways the range is known wrong come back as warnings: the mark was set
    by this very call (work committed before it is not counted), it was taken in
    another tree than the one this verify reads, or commits since it sit on another
    branch. A commit this branch's own reflog records as made here stays this phase's
    either way: a branch cut later from this line holds it too. `computed` is
    `_phase_commit_set`'s answer taken before the write, reused so the record and the
    blast radius name one set."""
    shas, warnings = computed or _phase_commit_set(plan_dir, phase, root, named=named)
    if _phase_open_record(plan_dir, phase, root) is None:
        _mark_phase_open(plan_dir, phase, root)
    import hook_runtime
    before = [r for r in _read_phase_commits_records(plan_dir)
              if r.get("kind") == "verify" and r.get("phase") == phase]
    _append_phase_commits_record(plan_dir, PhaseCommitRecord(
        phase=phase, sha=shas, actor=hook_runtime.resolve_actor(),
        ts=write_verification._now_iso(),
        supersedes=str(before[-1].get("ts") or "") or None if before else None,
        head=_git_head(root) if named is not None else None,
        tree=str(root),
        patch_ids=review_seen.patch_ids(root, shas),
    ).as_dict())
    return warnings


# The `stage` a debt-payment verify must carry. Any other value — notably the
# per-phase default — is a scoped run and does not settle the debt.
DEBT_STAGE = "deferred-suite"

# Built from DEBT_STAGE rather than repeating it: the sentence tells the agent the
# exact command, and a literal drifting from the value the gate compares against
# would print instructions that do not settle the debt they describe.
_PENDING_DEBT_ITEM = (
    "the deferred full suite has not been verified yet for this plan — run "
    "it once and record it before cook close, stamped as the run it was: "
    "hs-run cook verify --phase <last-node> --stage %s --verdict "
    "PASS --check regression:PASS" % DEBT_STAGE)

def _debt_repay_message(root: Path) -> "tuple[str, list]":
    """(next_action, warnings) for an unpaid defer_suite debt: the
    import-graph test set the changed files reach comes FIRST, so a red
    run there reads as STOP -- do not even reach the full suite -- and
    only a green run there is followed by the full-suite repay command
    (`_PENDING_DEBT_ITEM`). `hs-run` never re-reads a test result (the
    same boundary `hs_run_fix.py` holds for its own red->green step): this
    hands back the two commands in the order they must run, never a state
    machine that tracks which one already happened -- a verify stamped
    with anything other than `DEBT_STAGE` (including one that looks like
    "the targeted step's own record") still leaves `_suite_debt_unpaid`
    true, exactly as before this function existed.

    The resolve loop, the stack->command routing, and the three warning
    messages all live in `hs_run_scoped_tests` -- the shared door `test`
    and `fix`'s own `--scope changed` handling reuses, so none of the three
    re-implements it. This state (`suite_debt_unpaid`, the mutating close
    verb's own hard REFUSAL) calls `resolve_reached_tests` directly rather
    than declaring `facts:` on the row in the registry: `hs_run._apply_facts`
    would overwrite `state`/`exit_code` wholesale the moment a snapshot were
    merely stale, turning "defer_suite debt unpaid, closing refused" into
    "facts_stale, run a refresh command" -- a different and much narrower
    problem than the one actually blocking close. 0 importers, staleness, or
    the fact unable to resolve at all — all three skip the targeted step
    outright and return `_PENDING_DEBT_ITEM` unchanged, with one warning
    naming why -- never silently, and never blocking the plan's close over
    a facts outage unrelated to the debt itself. The command(s) ITSELF
    always come from each target's own detected stack
    (`hs_run_scoped_tests.stack_commands`) -- never a hardcoded `pytest`
    invocation, which is wrong on every non-Python target, and never a
    single stack picked for ALL targets regardless of which one they belong
    to -- a polyglot changed-file set gets one command PER owning stack,
    joined, and any target no stack claims is named in its own warning
    rather than silently dropped or guessed into another stack's command."""
    step = hs_run_scoped_tests.resolve_reached_tests(
        root, domain="cook", state="suite_debt_unpaid")
    tests = step.get("tests")
    if not tests:
        return _PENDING_DEBT_ITEM, [
            hs_run_scoped_tests.SKIPPED_WARNING
            % (step.get("reason") or "0 importer tests reached")]
    commands, unmatched = hs_run_scoped_tests.stack_commands(root, tests)
    if not commands:
        return _PENDING_DEBT_ITEM, [
            hs_run_scoped_tests.NO_RUNNER_WARNING
            % (len(tests), root, "no stack test_cmd resolved")]
    joined_cmd = " && ".join(commands)
    action = (
        "defer_suite debt unpaid: first run the importer test set the "
        "changed files reach -- %s. A failure there means STOP, do not "
        "run the full suite yet. Once it is green: %s"
        % (joined_cmd, _PENDING_DEBT_ITEM))
    warnings = [action]
    if unmatched:
        warnings.append(
            hs_run_scoped_tests.UNMATCHED_TARGET_WARNING % ", ".join(unmatched))
    return action, warnings


def _suite_debt_unpaid(plan_dir: Path) -> bool:
    """The ONE signal authorized for "has the defer_suite debt been
    paid" — the plan's own canonical verification artifact carries a
    closeable verdict, exactly the same read `_finalize_projection`'s
    review-decision gate above already does for a different artifact kind.
    No test count, no junit — one more field on a record that already carries
    it. The stage says WHICH run it was, not how much it ran, so a scoped run
    deliberately stamped as the debt run still reads as paid: an agent who
    forgets is caught, one who misrepresents is not (named in
    `deferred-suite.md`).

    Shared by design: phase 5's own `cook close` gate reads this SAME
    function, so `pending` here and that gate can never disagree about
    whether the debt is paid — two readers of one fact instead of two
    independently-derived opinions of it.

    Read-only, no subprocess: `artifact_check._load_artifact` parses the
    file already on disk. Missing/unreadable reads as unpaid — no evidence
    means no consent this happened.

    The `stage` is load-bearing, and reading the verdict ALONE was measured
    wrong: every phase's scoped verify overwrites this same canonical file, so
    the last phase silently paid the debt with a handful of tests and close
    returned 0 with the full suite never run. `stage` already exists on the
    record and is descriptive free text, so requiring the debt-payment value
    distinguishes the two runs without counting tests, reading junit, or
    inventing an artifact — the run says which run it was."""
    rec, _p = artifact_check._load_artifact(Path(plan_dir), "verification")
    if not isinstance(rec, dict):
        return True
    return not (rec.get("verdict") in _CLOSEABLE_VERDICTS
                and str(rec.get("stage") or "").strip() == DEBT_STAGE)


def _pending_for_debt(plan_dir: Path, root: Path) -> list:
    """`pending[]`'s own content for the defer_suite debt — derived from disk on
    EVERY call (never a stored cursor, matching `Envelope.pending`'s own
    contract), so a call made right after the debt is paid regresses to `[]`
    exactly as honestly as the disk does. Absent the flag: always `[]` — a
    plan that never opted in never rides this channel (the
    default-is-invariant, restated for a field the reminder owns).

    Exactly one item, never a phase name (`verification-<phase>.json` is
    first-wins and every defer_suite snapshot is small by design, so "find
    the snapshot with a matching total" cannot answer this question either
    way — see `deferred-suite.md`). That one item is `_debt_repay_message`'s
    own `next_action`: the targeted importer test set first, the
    full-suite repay command last — the SAME text `CLOSE`'s own
    `suite_debt_unpaid` refusal hands back, so a driver reading `next`
    before ever calling `close` sees the identical two-step instruction."""
    if not defer_suite_carry.resolve_defer_suite_carry(plan_dir)["carried"]:
        return []
    if _suite_debt_unpaid(plan_dir):
        action, _warnings = _debt_repay_message(root)
        return [action]
    return []


def _phase_declared_subjects(plan_dir: Path, phase: str | None):
    """Every file `phase` declares in `plan-graph.yaml`
    (`files_to_create`+`files_to_modify`, sorted, UNFILTERED) — the
    `code.tests_reached` fact's subject set at `needs_implementation`, where
    the working-tree diff is usually still empty (nothing has been written
    yet, so the engine's own merge-base-diff default would answer for a
    DIFFERENT, empty change instead of this phase's actual scope).

    No test/non-test split here: an earlier version filtered the declared
    set through `change_class_derivation._TEST_PATH_RE`, a suffix-only
    regex with no directory requirement — it read a production script like
    `harness/scripts/hs_run_test.py` (ends in `_test.py`, lives outside any
    test directory) as a test path and dropped it from the subject set
    entirely. That filter was also unnecessary: `code.tests_reached`'s own
    provider already self-selects a declared test subject at depth 0 using
    `codegraph_change_reader.FilesLayout.is_test_file`, the directory-aware
    convention `hs-code change test select` itself relies on — a SECOND,
    narrower test-shape check on this side of the seam could only disagree
    with that one, never improve on it.

    Returns `None` (never `[]`) for an empty/absent/malformed declaration or
    an unknown node — `None` is `hs_run_facts.engine.resolve`'s own "no
    override, compute the default instead" signal. A bare `[]` here would
    instead claim "this phase touches zero files", a different and false
    statement about a node whose scope was simply never describable this
    way.

    Reuses `plan_graph._files` (the exact reader `graph_test_scope.
    resolve_test_scope` already calls for the SAME node) rather than
    re-parsing `plan-graph.yaml` a second way — a second reader answering
    "what does this node declare" would drift from the first the day either
    one's edge cases change."""
    if phase is None:
        return None
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return None
    subtasks = graph.get("subtasks") or {}
    if phase not in subtasks:
        return None
    declared = plan_graph._files(graph, phase, "files_to_create", "files_to_modify")
    if not declared:
        return None
    return sorted(declared)


def _attach_fact_subjects(plan_dir: Path, phase: str | None, result: dict) -> None:
    """`fact_subjects` attaches HERE, once, only for `needs_implementation` —
    mirrors `_attach_route_flags`'s own shape (a `result.setdefault(...)`
    called centrally, rather than every one of `_cmd_next_for_plan`'s early
    returns having to remember it). ABSENT, never `None`/`[]`, when
    `_phase_declared_subjects` has nothing to say — `hs_run._apply_facts`
    only pops the key when it is THERE (`result.pop('fact_subjects', None)`),
    so an absent key already means "let the engine compute its own default",
    with no `None` sentinel needed on this side of the seam."""
    if result.get("state") != "needs_implementation":
        return
    subjects = _phase_declared_subjects(plan_dir, phase)
    if subjects:
        result.setdefault("fact_subjects", subjects)
    # The plan's declared branch: the engine warns when the facts measure another.
    from hs_run_facts import engine as facts_engine
    parsed = frontmatter_parser.parse_file(Path(plan_dir) / "plan.md")
    branch = facts_engine.declared_branch(parsed.get("frontmatter"))
    if branch:
        result.setdefault("fact_branch", branch)


def _attach_route_flags(plan_dir: Path, result: dict) -> None:
    """cook's own answer to "which conditional `when_flag:` blocks apply" —
    mirrors `hs_run_plan._attach_route_flags` in shape (attach only when at
    least one flag is on; ABSENT, never empty, when none are — the dispatcher
    pops this key generically at `hs_run.py:2313`, no domain-specific change
    needed here). Reuses `defer_suite_carry.resolve_defer_suite_carry` rather
    than re-parsing `plan.md` frontmatter a second time — that resolver
    already has the exact fail-closed, never-raise contract this needs
    (unreadable frontmatter reads as not-carried, never an exception)."""
    if defer_suite_carry.resolve_defer_suite_carry(plan_dir)["carried"]:
        result.setdefault("route_flags", ["defer_suite"])


def _next(ns) -> dict:
    result = _cmd_next_impl(ns.ctx.root, plan=ns.plan, phase=ns.phase)
    # Named subjects win over the phase's declared files, at every state.
    hs_run.request_fact_subjects(result, ns.fact_subjects)
    return hs_run_next_command.finalize(result)


NEXT = hs_run.verb(_cook_door(_next), options=("plan", "phase", "fact-subjects"),
                   helps={"plan": _PLAN_HELP, "fact-subjects": hs_run.FACT_SUBJECTS_HELP})


def _cmd_next_impl(root, *, plan=None, phase=None) -> dict:
    """`hs-run cook next [--plan <dir>] [--phase <id>]` — the per-phase
    projection. Every call re-reads disk fresh; nothing here is
    an authoritative progress file. `--phase` forces the phase inspected
    (mainly useful for `hs-run cook verify`'s own follow-up); absent, the
    first not-yet-passed phase (in `_phase_order`'s deterministic order) is
    used — validated against the plan-graph's own node list first (F15): a
    `--phase` that is not a node is EXIT_BROKEN, never handed to the state
    machine as if it were a real unstarted node. The one exception is a token
    `plan_graph.resolve_phase_id` maps to exactly one node (`<n>`/`p<n>` -> `P<n>`,
    `<n>` -> `phase-<n>-<slug>`):
    that node is answered, with a warning naming the exact id.

    C0/F10: `status:` frontmatter is corroboration ONLY, never
    authoritative — this used to read `status == "completed"` and
    short-circuit before any artifact check ran (a progress file the
    projection contract forbids). Now: derive the answer from plan-graph + completion_state
    every time; a `completed`/`in_progress` frontmatter that DISAGREES with
    what the artifacts show is a first-class `state_conflict` at
    EXIT_BROKEN, naming both sides — never a silent "done"."""
    root = Path(root)
    explicit_plan, forced_phase = plan, phase

    dirs = _plan_dirs(root)
    # A NAMED target (`--plan`, or an explicitly-set HARNESS_ACTIVE_PLAN) that names
    # nothing is never "no plan exists yet, done": with no plans tree at all it goes
    # on to the resolver below, which names the miss — the same answer it gives
    # while a different real plan sits on disk.
    if not dirs and not (explicit_plan or os.environ.get("HARNESS_ACTIVE_PLAN")):
        return hs_run.StepResult(
            state="no_plan",
            exit_code=EXIT_DONE,
            next_action="no plan exists to cook yet — create + approve one "
                             "first (hs-run plan next), then cook it",
            artifacts=[],
            warnings=[],
            extra={"summary_numbers": "0 plans found"},
        ).as_dict()

    plan_dir, miss = _resolve_plan_dir_verbose(root, explicit=explicit_plan)
    if plan_dir is None:
        # R15 / conformance property 7 (D12): a caller that
        # EXPLICITLY named a target — a `--plan` that does not exist, or
        # an explicitly-set HARNESS_ACTIVE_PLAN that resolve_active_plan's
        # own fail-closed contract rejected — must never read as an idle
        # "nothing to do yet" (EXIT_DONE), even while a DIFFERENT real
        # plan sits on disk. That is a genuinely blocking situation (the
        # named target failed to resolve), not an ambiguous "no plan
        # exists ANYWHERE yet" one — mirrors the plan domain's own
        # identical fix for `--plan <bogus>` (hs_run_plan.py's own
        # C0-MOVED note on this exact branch). `ambiguous_approved` /
        # `no_candidates` are deliberately NOT included here: those stay
        # at EXIT_DONE to match `test_property6_ambiguous_ordering_
        # same_exit_code_across_domains`'s cross-domain parity
        # requirement with the plan domain's own `multiple_plans`
        # (exit 0).
        #
        # Decided BEFORE the result is built rather than written over it
        # afterwards: the value object is frozen, and a result that a later
        # line can still edit is not a result, it is a scratchpad.
        #
        # The two named-target-failed kinds are NOT the same failure shape:
        # `explicit_not_found` is a bad `--plan` path — usage/config, the CLI
        # cannot answer at all (EXIT_BROKEN). `active_plan_rejected` is the
        # operator SETTING `HARNESS_ACTIVE_PLAN` to something that fails
        # `resolve_active_plan`'s own fail-closed contract — the CLI COULD
        # answer about a different, real plan on disk and deliberately
        # declines to guess (EXIT_REFUSED). `ambiguous_approved`/
        # `no_candidates` are neither: those stay EXIT_DONE (see above).
        miss_kind = miss.get("kind") if miss else None
        if miss_kind == "explicit_not_found":
            not_approved_exit = EXIT_BROKEN
        elif miss_kind == "active_plan_rejected":
            not_approved_exit = EXIT_REFUSED
        else:
            not_approved_exit = EXIT_DONE
        return hs_run.StepResult(
            state="not_approved",
            exit_code=not_approved_exit,
            next_action=_not_approved_message(miss, dirs),
            artifacts=[str(d) for d in dirs], warnings=[],
            extra={"summary_numbers": "%d plan(s) found" % len(dirs)},
        ).as_dict()

    spelled = None
    if forced_phase is not None:
        graph = plan_graph.parse_phase_graph(plan_dir)
        node, _cands = ((None, []) if graph.get("error")
                        else plan_graph.resolve_phase_id(graph, forced_phase))
        if node is not None and node != forced_phase:
            spelled, forced_phase = forced_phase, node
    result = _cmd_next_for_plan(plan_dir, forced_phase, root)
    if spelled is not None:
        # Followed, not just suggested: the match is one node by casing or by the
        # number its id spells out as a phase, never a suffix guess — and `next` stamps that node's
        # open mark, so a guess here would be a claim. The writers still take the
        # exact id only, so the warning teaches it.
        result["warnings"] = list(result.get("warnings") or []) + [
            "--phase %r read as %r (the one plan-graph node it names) — pass "
            "--phase %s; prompt/record/verify take the exact id only"
            % (spelled, forced_phase, forced_phase)]
    if forced_phase is None and result.get("phase"):
        result = _name_ready_alongside(plan_dir, result, root)
    # `pending`/`route_flags` attach HERE, once, to every state
    # `_cmd_next_for_plan` can return — mirroring `hs_run_plan._project`'s own
    # `result.setdefault(...)` pattern rather than requiring each of that
    # function's dozen early returns to remember it separately. Derived from
    # disk fresh on this call, never a stored cursor (`Envelope.pending`'s own
    # contract) — flag off leaves both untouched (`route_flags` absent,
    # `pending` `[]`, matching the default-is-invariant).
    _attach_route_flags(plan_dir, result)
    _attach_fact_subjects(plan_dir, result.get("phase"), result)
    repo_rules.attach(plan_dir, result)
    result.setdefault("pending", _pending_for_debt(plan_dir, root))
    return _with_check_name_warnings(plan_dir, _with_waived_edges(plan_dir, result, root))


def _check_name_warnings(plan_dir: Path) -> list:
    """The record the DoD gate reads — the canonical verification, whichever
    phase wrote it last — when it names a check the gate cannot count. The
    writer's advisory reaches only whoever writes; a record from before the
    `<type>/<name>` form would otherwise stay silent until ship refuses it.
    Another phase's own snapshot is not read: the gate never sees it, and
    re-verifying that phase would make ITS record the canonical one.

    A nudge, so it fails open: any error reading the policy or the record
    leaves the answer it rides on untouched."""
    try:
        import test_policy
        rec, _problem = artifact_check._load_artifact(plan_dir, "verification")
        if not isinstance(rec, dict):
            return []
        policy = test_policy.load_test_policy(
            root=plan_graph.root_for_plan_dir(plan_dir), trace=False)
        known = set(policy.get("test_types") or {})
        unread = sorted(str(c.get("name")) for c in rec.get("checks") or []
                        if isinstance(c, dict) and c.get("name")
                        and test_policy.check_type(c["name"]) not in known)
        if not unread:
            return []
        path = artifact_check._artifact_path(plan_dir, "verification")
        return ["phase %s's verification — the record the DoD gate reads — names "
                "check(s) %s outside test-policy test_types, so the gate counts none "
                "of them. Copy %s, rename each as <type>/<name> (format/file kept), "
                "then: hs-run cook verify --plan %s --from <the copy>"
                % (rec.get("phase"), unread, path, plan_dir)]
    except Exception:  # noqa: BLE001 — advisory; never the reason next/status fails
        return []


def _declared_phase_status(plan_dir: Path) -> list:
    """The warning for the phase files that still declare a frontmatter `status:`, or
    none. Nothing writes it after scaffold and nothing reads it: a phase's state is
    derived from its verification records, so a declared one only goes stale."""
    files = sorted(set((plan_dir / "phases").glob("phase-*.md")) | set(plan_dir.glob("phase-*.md")),
                   key=lambda f: (plan_graph.phase_sort_key(f.name), str(f)))
    stale = []
    for f in files:
        parsed = frontmatter_parser.parse_file(f)
        if parsed.get("ok") and "status" in (parsed.get("frontmatter") or {}):
            stale.append(f.relative_to(plan_dir).as_posix())
    if not stale:
        return []
    return ["%d phase file(s) declare a frontmatter `status:` that no harness step "
            "writes or reads, so it goes stale: %s — a phase's state is derived from "
            "its verification-<phase>.json (the `phases` rows here); delete the line"
            % (len(stale), _listed(stale, 5, "file(s)"))]


def _with_check_name_warnings(plan_dir: Path, result: dict) -> dict:
    extra = _check_name_warnings(plan_dir)
    if extra:
        result["warnings"] = list(result.get("warnings") or []) + extra
    return result


def _with_cross_plan_waits(graph: dict, plan_dir: Path, root) -> dict:
    """`graph` holding each phase whose sibling-plan prerequisite has not passed — the
    other plan's passed set read by the same counter that answers for this one."""
    def passed_of(d):
        return derive_plan_completion.completion_state(d, root=root)["passed_phases"]
    return plan_graph.with_waits(graph, plan_graph.unmet_cross_plan(graph, plan_dir, passed_of))


def _with_waived_edges(plan_dir: Path, result: dict, root) -> dict:
    """`result` naming each waived cross-plan edge whose other phase is still owed."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return result
    said = plan_graph.waived_open(graph, plan_dir, lambda d: derive_plan_completion.completion_state(
        d, root=root)["passed_phases"])
    if said:
        result["warnings"] = list(result.get("warnings") or []) + said
    return result


def _cross_plan_next(plan_dir: Path, waits: list) -> str:
    """The command that moves each prerequisite `<plan>/<phase>` along."""
    return "; ".join("hs-run cook next --plan %s --phase %s"
                     % (Path(plan_dir).parent / w.rpartition("/")[0], w.rpartition("/")[2])
                     for w in waits)


def _phase_waiting(plan_dir: Path, phase: str, graph: dict, numbers: str) -> dict:
    """A phase named by `--phase` that waits on a sibling plan's phase: the wait is the
    answer, and the move is in the other plan."""
    waits = plan_graph.waits_on(graph, phase)
    return hs_run.StepResult(
        state="phase_waiting",
        next_action="%s waits on %s, not passed yet — cook that first: %s"
                    % (phase, ", ".join(waits), _cross_plan_next(plan_dir, waits)),
        artifacts=[str(plan_dir / "plan-graph.yaml")],
        warnings=[],
        exit_code=EXIT_NEEDS_MODEL,
        extra={"phase": phase, "waits_on": waits, "summary_numbers": numbers},
    ).as_dict()


def _ready_phases(graph: dict, passed) -> list:
    """Phases not passed whose every prerequisite passed and that no pause holds,
    in visiting order."""
    holds = plan_graph.phase_holds(graph)
    held = holds["paused"] | set(holds["blocked"])
    preds: dict = {}
    for src, dsts in plan_graph.build_adj(graph).items():
        for d in dsts:
            preds.setdefault(d, set()).add(src)
    done = set(passed)
    return [n for n in _phase_order(graph)
            if n not in done and n not in held and preds.get(n, set()) <= done]


def _name_ready_alongside(plan_dir: Path, result: dict, root) -> dict:
    """The phases ready in the same batch as the one this `next` answered for, each
    with the command that projects it: one answer per call must not read as one
    runnable phase."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return result
    graph = _with_cross_plan_waits(graph, plan_dir, root)
    passed = derive_plan_completion.completion_state(plan_dir, root=root)["passed_phases"]
    others = [n for n in _ready_phases(graph, passed) if n != result["phase"]]
    if not others:
        return result
    out = dict(result)
    out["ready_alongside"] = others
    out["warnings"] = list(result.get("warnings") or []) + [
        "%d more phase(s) ready alongside %s: %s" % (len(others), result["phase"], "; ".join(
            "%s%s — hs-run cook next --plan %s --phase %s"
            % (n, _claimed_note(plan_dir, n, root), plan_dir, n) for n in others))]
    return out


def _claimed_note(plan_dir: Path, phase: str, root) -> str:
    trees = _claimed_elsewhere(plan_dir, phase, Path(root))
    return " (claimed by %s)" % ", ".join(trees) if trees else ""


def _phase_paused(plan_dir: Path, phase: str, graph: dict, numbers: str) -> dict:
    """A phase a pause holds, named by `--phase`: the pause is the answer, before any
    build or verify state the phase may also have. Lifting it is a human's call;
    `reopen_when` is prose for that human, never run here."""
    holders = plan_graph.held_by(graph, phase)
    own = plan_graph.pause_fields(graph, phase) if holders == [phase] else {}
    notes = "; ".join(plan_graph.pause_note(graph, p) for p in holders)
    if holders != [phase]:
        notes = "%s waits on %s" % (phase, notes)
    return hs_run.StepResult(
        state="phase_paused",
        next_action="%s — lift the pause by clearing `paused:` in %s once its reason no "
                    "longer holds" % (notes, plan_dir / "plan-graph.yaml"),
        artifacts=[str(plan_dir / "plan-graph.yaml")],
        warnings=[],
        exit_code=EXIT_NEEDS_MODEL,
        extra={"phase": phase, "held_by": holders, "summary_numbers": numbers, **own},
    ).as_dict()


def _passed_sentence(plan_dir: Path, phase: str) -> "tuple[str, dict]":
    """("phase X already passed ...", {latest_record, snapshot_note}). The snapshot
    counts the phase; when a later verify holds a different record, the sentence says
    where it is, so a reader of the snapshot alone does not take it for the latest."""
    records = _phase_record_files(plan_dir, phase)
    said = "phase %s already passed (verification-%s.json counts it)" % (phase, phase)
    if records.get("snapshot_note"):
        said += "; its latest verify is %s, not that snapshot: %s" % (
            Path(records["latest_record"]).name, records["snapshot_note"])
    return said, (records if records.get("snapshot_note") else {})


def _phase_passed(plan_dir: Path, phase: str, graph: dict, comp: dict, numbers: str) -> dict:
    ready = [n for n in _ready_phases(graph, set(comp["passed_phases"]) | {phase})]
    passed, records = _passed_sentence(plan_dir, phase)
    return hs_run.StepResult(
        state="phase_passed",
        next_action=("%s — %s"
                     % (passed, "ready next: %s — hs-run cook next --plan %s "
                        "--phase <id>" % (", ".join(ready), plan_dir)
                        if ready else "no other phase is ready yet")),
        artifacts=[str(plan_dir / "artifacts" / ("verification-%s.json" % phase))]
        + ([records["latest_record"]] if records else []),
        warnings=[], exit_code=EXIT_DONE,
        extra={"phase": phase, "ready": ready, "summary_numbers": numbers, **records},
    ).as_dict()


def _cmd_next_for_plan(plan_dir: Path, forced_phase, root) -> dict:
    """`_cmd_next_impl`'s per-phase projection body, once a `plan_dir` is
    resolved — split out so `pending`/`route_flags` can attach centrally at
    the ONE call site above instead of at each of this function's early
    returns."""
    status = _frontmatter_status(plan_dir)
    if status == "approved":
        pf = _run_preflight(plan_dir, root=root)
        if pf["blocked"]:
            return hs_run.StepResult(
                state="preflight_blocked",
                next_action="fix before opening: %s" % "; ".join(pf["reasons"]),
                artifacts=[str(plan_dir)],
                warnings=pf["reasons"] + pf.get("warnings", []),
                exit_code=_preflight_exit_code(pf["reason_kinds"]),
            ).as_dict()
        # The preflight's advisories (a sibling-plan citation, a misplaced phase file)
        # ride on whichever door answers: `next` is the one most agents read.
        return hs_run.StepResult(
            state="needs_open",
            next_action="hs-run cook open --plan %s" % plan_dir,
            next_command="hs-run cook open --plan %s" % plan_dir,
            artifacts=[str(plan_dir)],
            warnings=pf.get("warnings", []),
            exit_code=EXIT_NEEDS_STEP,
        ).as_dict()

    if status not in ("in_progress", "completed"):
        return hs_run.StepResult(
            state="not_startable",
            next_action="plan status is %r — cook cannot act on it" % status,
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()

    # From here `status` is corroboration only. The real answer is
    # derived from the plan-graph + completion_state, never cached from
    # frontmatter — a disagreement is a first-class state_conflict, not a
    # silently-trusted "done".
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        if status == "completed":
            return hs_run.StepResult(
                state="state_conflict",
                next_action="frontmatter says completed but plan-graph.yaml is "
                    "unreadable (%s) — cannot corroborate; refusing to trust "
                    "either side silently" % graph["error"],
                artifacts=[str(plan_dir)],
                warnings=["frontmatter: status=completed",
                             "artifacts: %s" % graph["error"]],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        return hs_run.StepResult(
            state="no_plan_graph",
            next_action="plan-graph.yaml problem: %s" % graph["error"],
            artifacts=[str(plan_dir)],
            warnings=[graph["error"]],
            exit_code=EXIT_BROKEN,
        ).as_dict()

    graph = _with_cross_plan_waits(graph, plan_dir, root)
    # A named `--phase` is checked against the graph whatever the plan's progress:
    # on a finished plan an unknown one used to fall through to `needs_close`,
    # answering ordinary work to a target that is not there.
    why = _phase_refusal(plan_dir, forced_phase, graph)
    if why:
        return _unknown_phase(plan_dir, why)

    comp = derive_plan_completion.completion_state(plan_dir, root=root)
    # The plan's own disagreement answers first, whichever phase is named.
    if not comp["complete"] and status == "completed":
        return hs_run.StepResult(
            state="state_conflict",
            next_action="frontmatter says completed but artifacts show %s — the "
                "two disagree, refusing to report either as authoritative"
                % comp["reason"],
            artifacts=[str(plan_dir)],
            warnings=["frontmatter: status=completed",
                         "artifacts: %s" % comp["reason"]],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    # A phase named by --phase that a pause holds answers that, whatever the plan's
    # count says: a pause takes the phase out of what done means, so with every other
    # phase passed the plan reads complete and the plan's final review would answer.
    if forced_phase is not None and plan_graph.held_by(graph, forced_phase):
        return _phase_paused(plan_dir, forced_phase, graph, "%d/%d phase(s) passed" % (
            len(comp["passed_phases"]), comp["n_total"]))
    if (forced_phase is not None and forced_phase not in comp["passed_phases"]
            and plan_graph.waits_on(graph, forced_phase)):
        return _phase_waiting(plan_dir, forced_phase, graph, "%d/%d phase(s) passed" % (
            len(comp["passed_phases"]), comp["n_total"]))
    if not comp["complete"]:
        order = _phase_order(graph)
        owed = [n for n in order if n not in comp["passed_phases"]]
        # An unforced projection hands this tree its OWN claim first (a successor it
        # claimed while the prerequisite waits elsewhere), then a READY phase no other
        # tree claimed (a phase whose prerequisite another tree is still cooking is not
        # free); with none left it still names one, and takes no mark.
        here = str(Path(root).resolve())
        mine = [n for n in owed if any(r.get("claimed") and r.get("tree") == here
                                       for r in _opens(plan_dir, n))]
        free = [n for n in _ready_phases(graph, comp["passed_phases"])
                if not _claimed_elsewhere(plan_dir, n, root)]
        phase = forced_phase or next(iter(mine or free or owed), None)
        cooked_elsewhere = ([] if forced_phase is not None or phase is None
                            else _claimed_elsewhere(plan_dir, phase, root))
        # A phase this tree claimed too is its own: no second mark, no invitation to
        # claim it — only who else holds it.
        shared = cooked_elsewhere if phase in mine else []
        if shared:
            cooked_elsewhere = []
        blocked = plan_graph.phase_holds(graph)["blocked"]
        if phase is None and blocked:
            # Not done (a blocked phase stays in the denominator) and nothing
            # runnable: every remaining phase needs a paused one, or a sibling
            # plan's phase not passed yet. A pause is a human's to lift; a wait
            # moves when the other plan's phase passes.
            waits = sorted({w for p in blocked for w in plan_graph.waits_on(graph, p)})
            moves = []
            if any(plan_graph.held_by(graph, p) for p in blocked):
                moves.append("Lift the pause (clear `paused:` in plan-graph.yaml once its "
                             "reason no longer holds), or remove the edge from a phase that "
                             "does not need it")
            if waits:
                moves.append("Cook the other plan's phase first: %s"
                             % _cross_plan_next(plan_dir, waits))
            return hs_run.StepResult(
                state="phases_blocked",
                next_action="no runnable phase left — %s. %s" % (
                    "; ".join(blocked[p] for p in sorted(blocked, key=plan_graph.phase_sort_key)),
                    ". ".join(moves)),
                artifacts=[str(plan_dir / "plan-graph.yaml")],
                warnings=[],
                exit_code=EXIT_NEEDS_MODEL,
                extra={"blocked_phases": sorted(blocked, key=plan_graph.phase_sort_key),
                       "summary_numbers": "%d/%d phase(s) passed"
                       % (len(comp["passed_phases"]), comp["n_total"])},
            ).as_dict()
        if phase is None:
            return hs_run.StepResult(
                state="no_phase_resolved",
                next_action="plan-graph declares no phases",
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        numbers = "%d/%d phase(s) passed" % (len(comp["passed_phases"]), comp["n_total"])
        substate, verdict, post_why = _phase_substate(plan_dir, phase, graph)
        if substate == "phase_passed":
            # The count's own predicate, not the canonical record: parallel phases
            # share one verification.json and the later verify overwrites it, while
            # each phase's own snapshot is what counts it done.
            return _phase_passed(plan_dir, phase, graph, comp, numbers)
        if substate == "phase_failed":
            return hs_run.StepResult(
                state="phase_failed",
                next_action="phase %s verification verdict is %s — fix and "
                                 "re-run: hs-run cook verify --phase %s --verdict "
                                 "..." % (phase, verdict, phase),
                artifacts=[str(plan_dir / "artifacts" / "verification.json")],
                warnings=[],
                exit_code=EXIT_NEEDS_MODEL,
                extra={"phase": phase, "verdict": verdict, "summary_numbers": numbers},
            ).as_dict()
        if substate == "phase_needs_reverify":
            return hs_run.StepResult(
                state="phase_needs_reverify",
                next_action=(
                    "verification-%s.json snapshot is missing though the "
                    "canonical verification still shows verdict %s for phase "
                    "%s — re-run hs-run cook verify --phase %s --verdict %s "
                    "--check <name>:<status> ... to regenerate it"
                    % (phase, verdict, phase, phase, verdict)),
                # Both the phase AND the verdict are already KNOWN here (the
                # canonical verification still names them) — this state is a
                # missing SNAPSHOT of a decision already made, not a new
                # decision, which is why it is tier 11 and not 10. The `--check`
                # placeholders are dropped rather than passed through: measured,
                # `write_verification` makes `--check` optional (`action=
                # "append", default=[]`), so this shorter form runs verbatim,
                # whereas the sentence's `<name>:<status> ...` would not.
                next_command=("hs-run cook verify --plan %s --phase %s "
                              "--verdict %s" % (plan_dir, phase, verdict)),
                artifacts=[str(plan_dir / "artifacts")],
                warnings=[],
                extra={"phase": phase, "verdict": verdict,
                       "summary_numbers": numbers},
                exit_code=EXIT_NEEDS_STEP,
            ).as_dict()
        if substate == "phase_needs_post_artifact":
            return hs_run.StepResult(
                state="phase_needs_post_artifact",
                next_action=_post_artifact_next_action(plan_dir, phase, verdict, post_why),
                artifacts=[str(plan_dir / "artifacts")],
                warnings=[post_why],
                exit_code=EXIT_BROKEN,
                extra={"phase": phase, "verdict": verdict, "summary_numbers": numbers},
            ).as_dict()
        if substate == "needs_verification":
            routed, check_warnings = _declared_check_step(plan_dir, phase, root, numbers)
            if routed is not None:
                return routed
            return hs_run.StepResult(
                state="needs_verification",
                next_action=(
                    "phase %s's build is recorded and no verification is on "
                    "disk yet — read the report, choose the "
                    "verdict, then: hs-run cook verify --plan %s --phase %s %s"
                    % (phase, plan_dir, phase, _VERIFY_PLACEHOLDERS)),
                # Null: the verdict is the model's judgment, so the command still
                # carries placeholders and would fail verbatim at the shell.
                next_command=None,
                artifacts=[str(_record_log_path(plan_dir))],
                warnings=[w for w in (_stale_build_warning(plan_dir, phase),) if w] + check_warnings,
                exit_code=EXIT_NEEDS_MODEL,
                extra={"phase": phase, "summary_numbers": numbers},
            ).as_dict()
        # `needs_implementation` is what `_phase_substate` returns for a phase
        # with no verification recorded at all — a phase not yet started. It had
        # no branch here, so it fell into `phase_needs_work` below, whose own
        # registry row says "The phase's verification did not pass": one name
        # covering both "never built" and "built and rejected".
        #
        # The cost was not cosmetic. `cook.yaml` routes per-phase-tdd.md and
        # subagent-patterns.md — 185 lines carrying the per-phase delegation
        # mandate (delegate-by-default then, posture-driven now) — at
        # `needs_implementation`, and the loop never emitted it, so
        # those drawers were reachable only through `status`, the read-only verb.
        # That is why cook/SKILL.md still carries 14 lines restating the mandate:
        # prose has to repeat what routing was built to deliver.
        if substate == "needs_implementation":
            # The FIRST time this phase is projected as ready to
            # build, stamp its own commit-range boundary — a no-op on every
            # later call for the same phase (`_mark_phase_open`'s own
            # first-wins contract).
            opened = None
            elsewhere = None if forced_phase is not None else _declared_branch_home(plan_dir, root)
            if not cooked_elsewhere and elsewhere is None:
                opened = _mark_phase_open(plan_dir, phase, root, claimed=forced_phase is not None)
            default_next_action = (
                "phase %s has no verification recorded — build it "
                "red->green, then: hs-run cook prompt developer "
                "--phase %s, hs-run cook record developer --phase %s "
                "--out <file>, hs-run cook verify --plan %s --phase %s %s"
                % (phase, phase, phase, plan_dir, phase, _VERIFY_PLACEHOLDERS))
            next_action, extra_warnings = _phase_scoped_next_action(
                plan_dir, phase, default_next_action, root)
            if opened is not None:
                # The one write a `next` makes, said where a caller reading the plan
                # sees it: a session using `next` as a read stamped a plan another
                # session was cooking and was never told.
                extra_warnings = list(extra_warnings) + [
                    "this call %s phase %s in %s at %s (artifacts/phase-commits.jsonl); "
                    "to read the plan without writing: hs-run cook status --plan %s"
                    % ("claimed" if opened.get("claimed") else "opened", phase,
                       opened["tree"], (opened.get("mark") or "no commit")[:12], plan_dir)]
            if shared:
                extra_warnings = list(extra_warnings) + [
                    "phase %s is also claimed by %s" % (phase, ", ".join(shared))]
            import harness_checks
            gone = harness_checks.marks_removed(plan_dir) if opened is None else []
            if gone:
                extra_warnings = list(extra_warnings) + [
                    harness_checks.marks_removed_reason(plan_dir, gone, phase)]
            if elsewhere is not None and not cooked_elsewhere:
                extra_warnings = list(extra_warnings) + [
                    "the plan's branch %s is checked out in %s and this tree's HEAD does not "
                    "hold its tip — no mark taken here; cook it there: cd %s && hs-run cook "
                    "next --plan %s (to cook it in this tree too: hs-run cook next --plan %s "
                    "--phase %s)" % (elsewhere[0], elsewhere[1], shlex.quote(elsewhere[1]),
                                     plan_dir, plan_dir, phase)]
            if cooked_elsewhere:
                extra_warnings = list(extra_warnings) + [
                    "phase %s is claimed by %s and no unclaimed phase is owed — no mark "
                    "taken here; to cook it in this tree too: hs-run cook next --plan %s "
                    "--phase %s" % (phase, ", ".join(cooked_elsewhere), plan_dir, phase)]
            return hs_run.StepResult(
                state="needs_implementation",
                next_action=next_action,
                artifacts=[str(plan_dir)],
                warnings=extra_warnings,
                exit_code=EXIT_NEEDS_MODEL,
                extra={"phase": phase, "summary_numbers": numbers},
            ).as_dict()
        return hs_run.StepResult(
            state="phase_needs_work",
            next_action="hs-run cook prompt developer --phase %s, then "
                             "hs-run cook record developer --phase %s --out "
                             "<file>, then hs-run cook verify --phase %s "
                             "--verdict ... --check ..."
                             % (phase, phase, phase),
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_NEEDS_MODEL,
            extra={"phase": phase, "summary_numbers": numbers},
        ).as_dict()

    return _finalize_projection(plan_dir, root, comp, forced_phase)


# ------------------------------------------------------------------ status

def _status(ns) -> dict:
    return hs_run_next_command.finalize(_cmd_status_impl(ns.ctx.root, plan=ns.plan))


STATUS = hs_run.verb(_cook_door(_status, report_only=True), options=("plan",), helps={"plan": _PLAN_HELP})


def _cmd_status_impl(root, *, plan=None) -> dict:
    """`hs-run cook status [--plan <dir>]` — a human-oriented, whole-plan
    summary (every phase's pass/fail, not just the current one). Pure
    projection, same posture as `next`: always EXIT_DONE (`status` is the ONE verb that always reports 0, read-only, reports
    only), UNLESS this call itself cannot give a trustworthy single answer —
    a `state_conflict` (frontmatter disagrees with derived artifacts, C0) is
    the one pre-existing exception, at EXIT_BROKEN.

    R18 fix: with two approved-and-unopened plans on disk and no
    `--plan` given, this used to report `state: "no_plan"` while its OWN
    `next_action` said "2 plan(s) are approved-and-unopened at once" — a
    direct self-contradiction. Named honestly now (`ambiguous_approved`).
    The EXIT CODE is deliberately left at EXIT_DONE, unlike the R15 fix in
    `NEXT` — the domain-agnostic conformance suite (D12, the SSOT for
    this contract) asserts `status` stays exit 0 even for an explicit
    mistyped `--plan` (`test_property7_status_is_exit_zero_on_mistyped_
    plan`, parametrized across every domain, no carve-out offered for an
    ambiguous or rejected target either); the redteam report's own suggested
    fix for R18 ("trạng thái CHẶN không được trả 0") would have violated
    that already-built, concurrently-owned property test, so the exit-code
    change was dropped — see this run's report for the explicit note."""
    root = Path(root)
    plan_dir, miss = _resolve_plan_dir_verbose(root, explicit=plan)
    if plan_dir is None:
        if miss and miss.get("kind") == "ambiguous_approved":
            return hs_run.StepResult(
                state="ambiguous_approved",
                next_action=_not_approved_message(miss, _plan_dirs(root)),
                artifacts=[str(d) for d in miss["candidates"]], warnings=[],
                # Property 7: this is `status`, the one always-0 verb. It names
                # the ambiguity in the body and leaves the choice — and the
                # rung — to whoever acts on it.
                exit_code=EXIT_DONE,
            ).as_dict()
        return hs_run.StepResult(
            state="no_plan",
            exit_code=EXIT_DONE,
            next_action=_not_approved_message(miss, _plan_dirs(root)),
            artifacts=[],
            warnings=[],
        ).as_dict()
    result = _with_check_name_warnings(plan_dir, _status_for_plan(plan_dir, root))
    stale = _declared_phase_status(plan_dir)
    if stale:
        result["warnings"] = list(result.get("warnings") or []) + stale
    return result


def _status_for_plan(plan_dir: Path, root: Path) -> dict:
    """`status` once its plan resolved — split so the plan-wide warnings attach at
    one return, over the plan this call itself resolved."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    status = _frontmatter_status(plan_dir) or "unknown"
    if graph.get("error"):
        if status == "completed":
            return hs_run.StepResult(
                state="state_conflict",
                next_action="frontmatter says completed but plan-graph.yaml is "
                    "unreadable (%s)" % graph["error"],
                artifacts=[str(plan_dir)],
                warnings=["frontmatter: status=completed",
                             "artifacts: %s" % graph["error"]],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        return hs_run.StepResult(
            state="no_plan_graph",
            next_action="plan-graph.yaml problem: %s" % graph["error"],
            artifacts=[str(plan_dir)], warnings=[graph["error"]],
            # EXIT_BROKEN, matching the sibling emitter further up. Both reach
            # this line from the same fact — plan-graph.yaml cannot be parsed —
            # and an unreadable graph is exactly as unusable whichever branch
            # noticed it. The rung split here was justified in a comment rather
            # than by a difference in the tree, which is how one word ends up
            # telling two drivers two different things about one broken file.
            exit_code=EXIT_BROKEN,
        ).as_dict()
    comp = derive_plan_completion.completion_state(plan_dir, root=root)
    order = _phase_order(graph)
    phases = [PhaseStatusRow(phase=n, passed=n in comp["passed_phases"],
                             commits=_phase_commit_shas(plan_dir, n),
                             **_phase_record_files(plan_dir, n)).as_dict()
              for n in order]
    numbers = "%d/%d phase(s) passed" % (len(comp["passed_phases"]), comp["n_total"])
    if status == "completed" and not comp["complete"]:
        return hs_run.StepResult(
            state="state_conflict",
            next_action="frontmatter says completed but artifacts show %s"
                             % comp["reason"],
            artifacts=[str(plan_dir)],
            warnings=["frontmatter: status=completed",
                         "artifacts: %s" % comp["reason"]],
            exit_code=EXIT_BROKEN,
            extra={"phases": phases, "summary_numbers": numbers},
        ).as_dict()

    if comp["complete"]:
        # R9: every phase passing does NOT mean the plan is
        # REALLY done — review-decision + the phase-grid gate must also
        # clear, exactly what `next`'s own `_finalize_projection` already
        # enforces (C4/F1). Before this fix `status` trusted whatever
        # `status:` said the instant `comp["complete"]` agreed with it,
        # without ever checking those two gates itself — so on the IDENTICAL
        # disk state (e.g. after write_verification's own premature
        # auto-finalize flip, R1) `cook next` could honestly report
        # `needs_review` (rc 10) while `cook status` said `completed` (rc
        # 0): one plan, two contradicting answers. `status`'s own posture is
        # unchanged (always exit 0, read-only) — only the STATE
        # NAME it reports is now derived from the SAME finalize projection
        # `next` uses, never a frontmatter shortcut.
        proj = _finalize_projection(plan_dir, root, comp)
        return hs_run.StepResult(
            state=proj["state"],
            next_action=proj["next_action"],
            # NOT the projection's rung. `next` and `status` read the same disk
            # and agree on the STATE; they deliberately disagree on the exit,
            # because Property 7 makes `status` the one always-0 verb — it
            # reports what is owed in the body and never escalates its own exit
            # for someone else's unfinished work. Carrying the rung across here
            # was measured and reverted: it turned a read-only report into a
            # failed action for every caller that branches on the number.
            exit_code=EXIT_DONE,
            artifacts=[str(plan_dir)],
            warnings=proj.get("warnings", []),
            # The projection's plan-gate keys ride as `next` hands them over:
            # one tree, one set of facts, whichever door was asked.
            extra={"phases": phases, **{k: proj[k] for k in _PLAN_GATE_KEYS if k in proj}},
        ).as_dict()

    # The other half of the fix above. That branch stopped trusting `status:` when
    # the phases agreed it was complete; this one still returned the frontmatter
    # value verbatim whenever they did not — and `plan_status.CANONICAL_STATUSES`
    # is a DIFFERENT vocabulary from `State`. Two of its five values (`pending`,
    # `in_progress`) are not `State` members at all, so `cook status` shipped
    # states that the state map, the routing table and the never-done set could
    # not see, while every coverage gate called the domain fully covered. A
    # partly-lifted shortcut reads as deliberate, which is why the second half
    # outlived the first.
    #
    # `phases_remain`, not the `needs_implementation` this branch used to carry.
    # The frontmatter value is not lost — it rides `extra` as `plan_status` for a
    # caller that genuinely wants the plan's own label rather than the verb's
    # reading of the tree. It is a SEPARATE key on purpose: the two answer
    # different questions, and letting them share the `state` slot is what made
    # them look interchangeable.
    #
    # Why the word had to change: `next` now emits `needs_implementation` for the
    # phase it resolved, at rung 10, since that is the state the registry routes
    # the delegate mandate at. This branch answers a WIDER question — "does this
    # plan still have unfinished phases", about the whole plan, at rung 0 — and
    # one word riding both 0 and 10 tells two drivers two different things about
    # one tree. Same split the six renames earlier in this migration made: the
    # verb that acts on a phase keeps the specific word, the whole-plan
    # projection takes a word that says what it actually measured.
    return hs_run.StepResult(
        state="phases_remain",
        next_action="hs-run cook next --plan %s" % plan_dir,
        # Property 7 again: the next_action names work that is still owed, and
        # `status` says so in the body while still leaving on 0. The rung is the
        # verb's own health, not the plan's.
        exit_code=EXIT_DONE,
        artifacts=[str(plan_dir)], warnings=[],
        extra={"phases": phases, "summary_numbers": numbers,
               "plan_status": status},
    ).as_dict()


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
        % (Path(__file__).name, 'cook'))
    raise SystemExit(EXIT_BROKEN)
