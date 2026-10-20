"""hs-code's `port` domain — the open-items write verbs: `propose` (the
MODEL's answer to a fork `hs-code port add-module`/`judge-nodes` raised) and
`approve` (a HUMAN's `approve`/`--reject`). A FLAT module under
`harness/scripts/` (sibling of every other `codegraph_port_*_verbs.py` —
same reason: the `hs-code-registry.d` reachability channel matches a
dot-free `module:` name).

Both verbs are dry-run by default (`--apply` writes) — the SAME contract
every other write verb in this family holds to: a plain run validates and
prints what WOULD happen; nothing lands on disk without `--apply`.

`propose`: the model's own choice + reasoning for a pending item. Exit 2 for
anything malformed (unknown batch, `--choice` outside the item's own
candidates, `--scope architecture` with a missing/unreadable `--decision`,
or `--decision` given for `--scope item`); exit 1 when the item does not
exist or is no longer pending (already `approved`/`rejected` — asking a
settled question is a finding, not a crash).

`approve`: resolve_actor() at machine shape (`"ci"`, or an `/agent:` suffix)
refuses OUTRIGHT (`hs_family.RefusedError`, exit 3) before anything is even
read — approval is a human act by definition (`port_open`'s own closing
rule). `--choice` defaults to the item's LATEST proposal (none proposed yet
and no `--choice` given is exit 2, naming the missing proposal); a
`scope: architecture` proposal is blocked (exit 1, nothing written) until
its `decision` id exists in the decision register
(`port_ledger_laws.BlockerContext.cleared`) — a SHAPE check
(`decision_register.is_readable_id`) already ran at `propose` time, this is
the EXISTENCE check, deferred to approval because the DEC may not exist yet
when the model merely proposes routing through it.

An approved item CARRYING a `node` (the `judge-nodes` `new-in-target` case —
`add-module`'s own `two-homes` items never carry one) also appends ONE
`ported -> [choice]` line to the PORT ledger, `by: human`, validated
(`append_event(..., apply=False)`) BEFORE either line is written — the two
writes are prepared as one all-or-nothing unit, then applied ledger first
(the harder of the two write doors to satisfy), open-items line second, so
a failure between them never leaves an item silently `approved` with no
ledger row backing it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hook_runtime import resolve_actor  # noqa: E402

import decision_register  # noqa: E402
import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_ledger_laws  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_open  # noqa: E402
import port_workspace  # noqa: E402
from ambiguous_input import NotFound  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_ledger_verbs import _not_written  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_reader import Event  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger, append_event, now_ts  # noqa: E402

_PENDING = ("open", "proposed")


def _red(message: str, **extra: Any) -> int:
    """`Verdict.red` carries no auto-print of its own (unlike `.broken`/
    `.unresolved`, which print their own reason) — every RED finding this
    module reports gets one line on stderr so a caller reading only stdout+
    stderr (never `--json`'s `data`) can still see WHY."""
    print(message, file=sys.stderr)
    return int(Verdict.red({"error": message, **extra}).exit)


def _find_port_root(root):
    try:
        return port_workspace.find_port_root(root), None
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return None, Verdict.broken(str(e)).exit


def _load_open_item(port_root: Path, batch_name: str, item_id: str, verb: str):
    """`(item, error_exit)` — every check `propose`/`approve` share before
    their own logic: the batch exists, `open.jsonl` reads clean, and
    `item_id` names a real item. `error_exit` is `None` on success."""
    try:
        port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return None, Verdict.unresolved(e).exit
    result = port_open.read_open(port_root, batch_name)
    if result.broken:
        return None, Verdict.broken(
            "port %s: %s" % (verb, "; ".join(
                "line %d: %s" % (n, m) for n, _r, m in result.broken))).exit
    item = result.items.get(item_id)
    if item is None:
        return None, Verdict.unresolved(
            NotFound("no open item %r under batch %r" % (item_id, batch_name),
                    "open-item", item_id)).exit
    return item, None


# ── propose ──────────────────────────────────────────────────────────────────

def _propose_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("item_id")
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--choice", required=True)
    ap.add_argument("--why", required=True)
    ap.add_argument("--scope", choices=("item", "architecture"), default="item")
    ap.add_argument("--decision", default=None)


def _propose(args: argparse.Namespace) -> int:
    """`hs-code port propose <id> --batch B --choice C --why W
    [--scope item|architecture] [--decision DEC-…] [--apply]` — the model's
    answer to a pending open item. See the module docstring for the exit
    table."""
    root, opts = args.ctx.root, args.opts
    item_id, batch_name, choice, why, scope, decision = (
        args.item_id, args.batch_name, args.choice, args.why, args.scope, args.decision)

    port_root, err = _find_port_root(root)
    if err is not None:
        return err

    item, err = _load_open_item(port_root, batch_name, item_id, "propose")
    if err is not None:
        return err
    if item.state not in _PENDING:
        return _red("port propose: item %r is not pending (state=%s)"
                   % (item_id, item.state))
    if choice not in item.candidates:
        return Verdict.broken(
            "port propose: --choice %r is not one of item %r's candidates %s"
            % (choice, item_id, list(item.candidates))).exit
    if scope == "architecture":
        if not decision or not decision.strip():
            return Verdict.broken(
                "port propose: --scope architecture needs --decision <DEC-id>").exit
        if not decision_register.is_readable_id(decision):
            return Verdict.broken(
                "port propose: --decision %r is not a readable DEC id" % decision).exit
    elif decision:
        return Verdict.broken(
            "port propose: --decision only applies with --scope architecture").exit

    apply = bool(getattr(opts, "apply", False))
    data: "dict[str, Any]" = {
        "schema": "port.open_propose/1", "id": item_id, "choice": choice,
        "scope": scope, "decision": decision, "dry_run": not apply,
    }
    if apply:
        try:
            port_open.propose_item(port_root, batch_name, item_id, choice=choice, why=why,
                                   scope=scope, decision=decision)
        except (port_open.ItemNotFoundError, port_open.ItemNotPendingError, ValueError) as e:
            return Verdict.broken(str(e)).exit

    if bool(getattr(opts, "json", False)):
        import json
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("item    : %s (batch %s)" % (item_id, batch_name))
        print("choice  : %s" % choice)
        print("scope   : %s%s" % (scope, " (decision %s)" % decision if decision else ""))
        print("written" if apply else "dry-run: valid, nothing written (--apply appends)")
    return int(Verdict.ok(data).exit)


PROPOSE = Command(add_args=_propose_args, run=_propose)


# ── approve ──────────────────────────────────────────────────────────────────

def _approve_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("item_id")
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--choice", default=None)
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--why", default=None)


def _approve(args: argparse.Namespace) -> int:
    """`hs-code port approve <id> --batch B [--choice C] [--reject --why W]
    [--apply]` — a human's `approve`/`reject` of a pending open item. See
    the module docstring for the full exit table."""
    root, opts = args.ctx.root, args.opts
    item_id, batch_name, choice, reject, why = (
        args.item_id, args.batch_name, args.choice, args.reject, args.why)

    port_root, err = _find_port_root(root)
    if err is not None:
        return err

    actor = resolve_actor()
    if port_open.is_machine_actor(actor):
        raise hs_family.RefusedError(
            "port approve: actor %r is machine-shaped — approval/rejection needs a "
            "human, not a CI run or an agent session" % actor)

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit

    item, err = _load_open_item(port_root, batch_name, item_id, "approve")
    if err is not None:
        return err
    if item.state not in _PENDING:
        return _red("port approve: item %r is not pending (state=%s)"
                   % (item_id, item.state))

    apply = bool(getattr(opts, "apply", False))
    want_json = bool(getattr(opts, "json", False))

    if reject:
        if not why or not why.strip():
            return Verdict.broken("port approve: --reject needs --why").exit
        data: "dict[str, Any]" = {
            "schema": "port.open_approve/1", "id": item_id, "event": "rejected",
            "why": why, "dry_run": not apply,
        }
        if apply:
            try:
                port_open.reject_item(port_root, batch_name, item_id, why=why)
            except (port_open.ItemNotFoundError, port_open.ItemNotPendingError) as e:
                return Verdict.broken(str(e)).exit
        _print_approve_result(data, want_json, apply)
        return int(Verdict.ok(data).exit)

    resolved_choice = choice
    if resolved_choice is None:
        if item.proposal is None:
            return Verdict.broken(
                "port approve: item %r has no proposal to use its choice from — "
                "pass --choice explicitly" % item_id).exit
        resolved_choice = item.proposal["choice"]
    if resolved_choice not in item.candidates:
        return Verdict.broken(
            "port approve: --choice %r is not one of item %r's candidates %s"
            % (resolved_choice, item_id, list(item.candidates))).exit

    proposal = item.proposal or {}
    scope, decision = proposal.get("scope"), proposal.get("decision")
    if scope == "architecture":
        ctx = port_ledger_laws.BlockerContext({}, port_root)
        if not ctx.cleared("decision:%s" % decision):
            return _red("port approve: blocked until %s exists" % decision,
                       decision=decision)

    ledger: "Ledger | None" = None
    ev: "Event | None" = None
    if item.node:
        source, source_problem = port_batch_reader.find_source(batch, None)
        if source_problem:
            return Verdict.unresolved(source_problem).exit
        try:
            ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
        except BrokenLedgerError as e:
            return Verdict.broken(_not_written(e.findings)).exit
        src = {"id": source.get("id"), "sha": str(source.get("pinned_sha"))}
        ev = Event(
            ts=now_ts(), node=item.node, status="ported", by="human", actor=actor,
            extra={"to": (resolved_choice,), "src": src,
                  "reason": "open item %s: %s" % (item_id, proposal.get("why") or "")},
            note="open item %s approved" % item_id)
        findings = append_event(ledger, ev, apply=False)
        if findings:
            return Verdict.broken(_not_written(findings)).exit

    data = {
        "schema": "port.open_approve/1", "id": item_id, "event": "approved",
        "choice": resolved_choice, "node": item.node, "dry_run": not apply,
    }
    if apply:
        if ev is not None and ledger is not None:
            findings = append_event(ledger, ev, apply=True)
            if findings:
                raise hs_family.RefusedError(_not_written(findings))
        try:
            port_open.approve_item(port_root, batch_name, item_id, choice=resolved_choice)
        except (port_open.ItemNotFoundError, port_open.ItemNotPendingError) as e:
            return Verdict.broken(str(e)).exit
    _print_approve_result(data, want_json, apply)
    return int(Verdict.ok(data).exit)


def _print_approve_result(data: dict, want_json: bool, apply: bool) -> None:
    if want_json:
        import json
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return
    print("item    : %s" % data["id"])
    print("event   : %s" % data["event"])
    if data.get("node"):
        print("node    : %s -> %s" % (data["node"], data.get("choice")))
    print("written" if apply else "dry-run: valid, nothing written (--apply appends)")


APPROVE = Command(add_args=_approve_args, run=_approve)
