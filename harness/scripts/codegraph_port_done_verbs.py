"""hs-code's `port` domain — `done`: close a batch by a mark. A FLAT module
under `harness/scripts/` (same `hs-code-registry.d` reachability reason as
every other `codegraph_port_*_verbs.py`).

`hs-code port done --batch B [--reason TEXT] [--reopen] [--apply]`

Adapted from `ob-tools@60075cb:packages/portkit/src/portkit/batch/done.py`
(tick a CHECKLIST item, then regenerate the handbook). Here the mark goes to
the batch's own `.port/batches/<b>/done.jsonl` (`port_runs.append_done_mark`,
append-only, `actor` + `ts`), and nothing is regenerated — `guide-doc` and
`--arch` are retired. The mark is a claim the caller makes: this verb runs
no step and no gate. It records the lane steps NOT green when it was
written (`open_steps`), so the claim stays visible, and `hs-run port next`
skips a closed batch — a lane that grew a step after the batch closed does
not reopen it. `--reopen` appends a `reopened` mark; the batch is walked
again.

Refused (exit 3): closing a closed batch, reopening an open one, and closing
a batch with a pending open item — only a human's approve/reject settles
one, and closing over it drops that decision.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_open  # noqa: E402
import port_runs  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--reason", default=None)
    ap.add_argument("--reopen", action="store_true")


def _done(args: argparse.Namespace) -> int:
    try:
        port_root = port_workspace.find_port_root(args.ctx.root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit
    try:
        batch = port_batch_reader.load_batch(port_root, args.batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit
    try:
        closed = port_runs.closed_mark(port_root, batch.name) is not None
        pending = port_open.pending_items(port_root, batch.name)
    except (port_runs.DoneMarksError, port_open.OpenLedgerError) as e:
        return Verdict.broken(str(e)).exit
    open_steps = port_runs.open_step_ids(port_root, batch)
    if args.reopen and not closed:
        raise hs_family.RefusedError("port done: batch %r is not closed" % batch.name)
    if not args.reopen and closed:
        raise hs_family.RefusedError("port done: batch %r is already closed — `--reopen` "
                                     "to walk it again" % batch.name)
    if not args.reopen and pending:
        raise hs_family.RefusedError(
            "port done: batch %r has %d pending open item(s) (%s) — approve or reject "
            "them first" % (batch.name, len(pending), ", ".join(i.id for i in pending[:5])))
    apply = bool(getattr(args.opts, "apply", False))
    state = "reopened" if args.reopen else "done"
    data = {"batch": batch.name, "state": ("closed" if closed else "open"), "to": state,
            "open_steps": open_steps, "reason": args.reason, "apply": apply}
    if apply:
        port_runs.append_done_mark(port_root, batch.name, state, args.reason, open_steps)
        data["state"] = "open" if args.reopen else "closed"
    if getattr(args.opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("batch %s: %s%s" % (batch.name, data["state"],
                                  "" if apply else " (dry-run: --apply writes the %s mark)" % state))
        if open_steps:
            print("  not green: %s" % ", ".join(open_steps))
    return int(Verdict.ok(data).exit)


DONE = Command(add_args=_args, run=_done)
