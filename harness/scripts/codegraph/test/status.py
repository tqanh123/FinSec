"""hs-code test status -- one-line glance at the stored suite run for the
tree standing here, READ-ONLY: it never runs anything and never fetches
anything.

Port of ob-tools' `testkit.lane.lane.status_report`/`run_status`/`classify`
(pinned commit `60075cb`), collapsed onto this harness's own lane shape: there
the lane was a five-step chain with its own state file and a size/mtime tree
fingerprint; HERE the run step already is `hs-run suite next`, and the
readers (`redlist`, `durations`, `isolation-bisect`, ...) are this domain's
own separate verbs -- so this verb is the lane's "at a glance" over the
`suite` domain's own stored data, sharing `suite_status.classify`'s content-
key fingerprint rather than growing a second one.

    hs-code test status            # machine: the envelope's `data`
    hs-code test status --text     # human line

Pointer by state (never the full chain -- `hs-run suite next` already prints
that):
    red            -> `hs-code test redlist <junit>`
    stale/not_run  -> `hs-run suite next`
    green          -> nothing to do

A `junit.meta.pending.json` stamp present is reported ALONGSIDE whatever
state above -- a run was stamped and never promoted, meaning it is either
still in flight or died mid-chain before finishing.

Exit: 0 for every state except `red` (1) -- `not_run`/`stale` are NOT
failures here, only the absence of a fresh answer, and are printed plainly
at exit 0.

WHAT IT CANNOT SEE
    * A missing or unusable `ROPS_SUITE_LANE` recipe: this verb only reports
      the DATA already on disk, it never resolves or validates the suite
      recipe -- `hs-run suite next` (the pointer this verb hands back for
      `stale`/`not_run`) owns that.
    * Whether a `pending` stamp's chain is still running RIGHT NOW versus
      already dead -- only that one exists and was never promoted.
    * Freshness one edit later: like `suite_status.classify` itself, this is
      a snapshot of the tree AT CALL TIME, not a live watch.
"""
from __future__ import annotations

import argparse
import json

import suite_status
from hs_family.cli import Command


def add_arguments(sp: argparse.ArgumentParser) -> None:
    pass  # no positional/flag args -- reads whatever is already on disk for `root`


def _pointer(status: "suite_status.SuiteStatus") -> "str | None":
    if status.state == "red":
        return "hs-code test redlist %s" % status.junit_path
    if status.state in ("stale", "not_run"):
        return "hs-run suite next"
    return None


def render(status: "suite_status.SuiteStatus") -> str:
    when = (" (measured %s by %s)" % (status.ts, status.actor) if status.ts else "")
    if status.artifact is not None:
        a = status.artifact
        counts = (" -- %d total, %d passed, %d failed, %d error(s), %d skipped"
                 % (a.total, a.passed, a.failed, a.errors, a.skipped))
    else:
        counts = ""
    reason = " -- %s" % status.reason if status.reason else ""
    pointer = _pointer(status)
    tail = " -- next: %s" % pointer if pointer else ""
    lines = ["suite status: %s%s%s%s%s" % (status.state, when, counts, reason, tail)]
    if status.pending:
        lines.append(
            "a run was stamped%s and not promoted -- it is either still in "
            "flight or died mid-chain."
            % (" at %s" % status.pending_ts if status.pending_ts else ""))
    return "\n".join(lines)


def _status(args: argparse.Namespace) -> int:
    root = args.ctx.root.resolve()
    as_json = args.opts.json

    status = suite_status.classify(root)

    if as_json:
        print(json.dumps(status.as_dict(), sort_keys=True, ensure_ascii=False))
    else:
        print(render(status))
    return 1 if status.state == "red" else 0


STATUS = Command(add_args=add_arguments, run=_status)
