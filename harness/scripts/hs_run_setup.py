#!/usr/bin/env python3
"""hs_run_setup.py — the `setup` domain's verb entry point for hs-run.

`hs:setup` walks the configuration groups, and most of that walk is a
conversation. None of those answers leave a mark that separates "asked and
answered" from "never asked" — two trees holding the same value, one hand-tuned
and one shipped that way, are indistinguishable on disk. Inventing a state for
them would mean inventing the evidence too.

Two steps are different, and those are the ones this verb takes:

  - preflight_deps.missing_required() -> a package is installed or it is not
  - trust_store.is_trusted(root)      -> this repo is in the trust store, or not

Both go through the libraries that already own them. There was a third, reading
a conversational-register file for pre-split keys; that file and the migration it
guarded are gone with the conversational register.

`deps_missing` rides rung 2, not 11, and the precedent is exact rather than
analogous: `hs_run_test.py:122-132` emits this state from the IDENTICAL
predicate at EXIT_BROKEN, with the reason in the code — "a hard blocker, not a
projection". The premise that would have argued for 11 ("we want to print the
pip command, and only 11 may carry one") is false: `_next_command_violation`
constrains rungs 11 and 10 and leaves rung 2 untouched, so a rung-2 result can
name the install command in `next_action` without promising a driver may run it.

Ordering is load-bearing. Deps come first because every later check imports
something; trust comes before any standards rule can fire a shell detector.
"""
from __future__ import annotations

import sys
from pathlib import Path

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)

import hs_run_next_command
import preflight_deps
import trust_store

import hs_run  # noqa: E402
from hs_run import EXIT_BROKEN, EXIT_NEEDS_MODEL  # noqa: E402

@_attrs.define(frozen=True, kw_only=True)
class MissingDeps:
    """The packages preflight could not import, as this verb reports them.

    A record rather than a bare `{"missing": [...]}`: the key name is then
    declared in one place instead of at every construction site, and a caller
    reading `artifacts[0]` finds a shape it can rely on. Mirrors `RecordedRun`
    in the sibling `test` domain, which had the same reason.
    """

    missing: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return {"missing": list(self.missing)}


@_attrs.define(frozen=True, kw_only=True)
class TrustState:
    """Whether this repo may run a standards rule's shell detector."""

    repo = _attrs.field()
    trusted: bool = _attrs.field(default=False)

    def as_dict(self) -> dict:
        return {"repo": str(self.repo), "trusted": bool(self.trusted)}


@_attrs.frozen
class _Posture:
    """What the three libraries report about this tree, gathered once.

    A value object rather than three loose locals: each state below needs a
    different subset, and the failure mode of threading them separately is a
    later edit reading one that was never refreshed.
    """

    missing_deps: tuple = _attrs.field(converter=tuple)
    trusted: bool = _attrs.field()
    error: str = _attrs.field(default="")


def _survey(root: Path) -> _Posture:
    """Read the two predicates, or carry the failure that stopped one.

    Fail-CLOSED on an unreadable policy: reporting "configured" for a tree whose
    config could not be read would send the caller into the walk with defaults
    it never chose.
    """
    try:
        missing = preflight_deps.missing_required()
    except Exception as exc:  # noqa: BLE001
        return _Posture(missing_deps=(), trusted=False,
                        error="preflight: %s" % exc)
    try:
        trusted = bool(trust_store.is_trusted(str(root)))
    except Exception as exc:  # noqa: BLE001
        return _Posture(missing_deps=tuple(missing), trusted=False,
                        error="trust store: %s" % exc)
    return _Posture(missing_deps=tuple(missing), trusted=trusted)


def _next(ns) -> dict:
    """Report the first onboarding condition that is not yet satisfied.

    `next_command` is null on every path, explicitly rather than absent. There is
    no rung-11 tier here: the one command that would qualify installs packages,
    and that is the tier the sibling `test` domain already placed at rung 2 for
    the same predicate. Guard and stage changes are env-bound and need a new
    session, so a driver looping on 11 would run them and see nothing change.
    """
    return hs_run_next_command.finalize(_cmd_next_impl(ns.ctx.root))


# No flags of its own: the empty declaration still refuses a mistyped one.
NEXT = hs_run.verb(_next)


def _cmd_next_impl(root: Path) -> dict:
    posture = _survey(root)

    if posture.error:
        return hs_run.StepResult(
            state="policy_unreadable", exit_code=EXIT_BROKEN,
            next_action="STOP — a configuration source under %s could not be "
                        "read (%s); repair it before configuring anything else"
                        % (root, posture.error),
            warnings=[posture.error],
        ).as_dict()

    if posture.missing_deps:
        return hs_run.StepResult(
            state="deps_missing", exit_code=EXIT_BROKEN,
            next_action=preflight_deps.install_command(
                list(posture.missing_deps)),
            artifacts=[MissingDeps(missing=list(posture.missing_deps)).as_dict()],
            warnings=["%d required package(s) missing — later hooks and tests "
                      "fail with opaque ImportErrors until they are installed"
                      % len(posture.missing_deps)],
        ).as_dict()

    if not posture.trusted:
        return hs_run.StepResult(
            state="trust_needed", exit_code=EXIT_NEEDS_MODEL,
            next_action="%s is not in the trust store, so a standards rule's "
                        "shell detector stays grep-only here — ask the user "
                        "before granting it, never grant it silently" % root,
            extra=TrustState(repo=root, trusted=False).as_dict(),
        ).as_dict()

    return hs_run.StepResult(
        state="configured", exit_code=EXIT_NEEDS_MODEL,
        next_action="deps present, repo trusted — every remaining choice is "
                    "the user's, so ask rather than assume a default",
        extra=TrustState(repo=root, trusted=True).as_dict(),
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
        % (Path(__file__).name, 'setup'))
    raise SystemExit(EXIT_BROKEN)
