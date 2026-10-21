"""hs-code's `port` domain — the ledger-write verbs. A FLAT module under
`harness/scripts/`, like the rest of the port domain: its engine is flat too
(`port_*.py`, read by hs-run without pulling the `codegraph` package in).

`MAP` is ported (algorithm-faithful) from `ob-tools@60075cb:packages/
portkit/src/portkit/port_verbs.py:82` (`run_map`), registered upstream at
`port_verbs.py:312` — now covering the single-node path, the `--prefix`/
`--nodes-from` bulk path (`portkit/map_bulk.py`'s own `select_nodes`/
`read_node_list`/`run_bulk`, ported alongside it), and the `new@batch`
anchor shorthand (`ledger_io.py`'s own `_anchor_batch`, ported into
`port_batch_reader.py`) once a batch is in play.

`CTX` is ported (algorithm-faithful, this tree's own `.port/` layout)
from `ob-tools@60075cb:packages/portkit/src/portkit/batch/context.py`
(`run_ctx`). `INIT` is ported (algorithm-faithful) from `port_verbs.
py:271` (`run_init`), now declaring against a NAMED BATCH's frozen
snapshot rather than a bare `--snap DIR`.

Each verb is a `Command` constant (`add_args`/`run`) named by its registry row's
`entry:`; the family core parses the declared argv and hands `run` a namespace
carrying `ctx.root` (the target repo) and `opts` (`hs_code.ShellOptions`, the
shell's own flags). Dry-run by default
(`append_event(..., apply=False)` — writes nothing); `--apply` writes for
real.

EXIT CODES follow the family table, not upstream's (upstream answered every
refusal with rc 2): a well-formed request that a ledger law or a batch's own
rule blocks — an illegal status transition, a `src` that cannot be resolved
or validated, a node anchor that disagrees with a batch's pinned sha, a
`new@batch` anchor with no batch in play — is a deliberate refusal, exit 3,
raised as `hs_family.RefusedError`; state that is already broken — a ledger
holding a broken line or a partial tail, an invalid `.port/port.yaml` or
`batch.yaml`, an unknown `--batch` name — is exit 2, the same answer
`hs-run port next` gives for a broken ledger; a misspelled or missing flag
is exit 2. A write verb with nothing left to write is DONE, exit 0, and
says so on stdout: `init` whose snapshot-minus-ledger diff is empty, and a
bulk `map` whose `--prefix`/`--nodes-from` matched no node. Upstream
answered both with 1 ("nothing found", `port_verbs.py:298-299`); here 1 is
reserved for read/check verbs that report findings, so rerunning a write
verb that already did its work never reads as a failure.

GIT: read-only, through `codegraph._core.git.resolve_ref` only (the ONE
door every caller that turns a caller-named ref into a full commit SHA goes
through here) — no `commit`/`push`, ever; the AST gate in
`harness/tests/port/test_port_verb_modules_never_write_git.py` holds every
module in this glob to that. A `--batch` node never even reaches this door:
its `src.sha` is the batch's own PINNED sha, never re-resolved live.
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

from hook_runtime import resolve_actor  # noqa: E402

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_ledger_laws  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_ledger_spec  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.git import GitError, resolve_ref  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from hs_family.errors import UsageError as _UsageError  # noqa: E402
from port_ledger_spec import PortingLedgerError  # noqa: E402
from port_ledger_writer import (  # noqa: E402
    BrokenLedgerError, Ledger, append_event, append_events, now_ts)


def _map_args(ap: argparse.ArgumentParser) -> None:
    """Exactly one of NODE / `--prefix` / `--nodes-from` selects what gets
    written — enforced in `_map` (post-parse: argparse's own mutually
    exclusive groups cannot span a positional and two options together)."""
    ap.add_argument("node", nargs="?", default=None)
    ap.add_argument("--to", action="append", default=[], metavar="DEST")
    ap.add_argument("--blocked-by", action="append", default=[], metavar="REF")
    ap.add_argument("--only-status", action="append", default=[], metavar="STATUS")
    ap.add_argument("--status", default=None)
    ap.add_argument("--reason", default=None)
    ap.add_argument("--by", default="human")
    ap.add_argument("--note", default=None)
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--nodes-from", default=None)
    ap.add_argument("--batch", default=None, dest="batch_name")


def _check_map_args(args: argparse.Namespace) -> None:
    node, prefix, nodes_from = args.node, args.prefix, args.nodes_from
    if sum(bool(x) for x in (node, prefix, nodes_from)) != 1:
        raise _UsageError(
            "port map: give exactly one of NODE, --prefix or --nodes-from")
    if not args.status:
        raise _UsageError("port map: --status is required")
    if args.status not in port_ledger_spec.STATUSES:
        raise _UsageError(
            "port map: --status %r outside the closed set (%s)"
            % (args.status, ", ".join(sorted(port_ledger_spec.STATUSES))))
    for s in args.only_status:
        if s not in port_ledger_spec.STATUSES:
            raise _UsageError(
                "port map: --only-status %r outside the closed set (%s)"
                % (s, ", ".join(sorted(port_ledger_spec.STATUSES))))


def _ctx_args(ap: argparse.ArgumentParser) -> None:
    """`port ctx` takes no positional, only `--batch`."""
    ap.add_argument("--batch", default=None, dest="batch_name")


def _check_ledger_args(ap: argparse.ArgumentParser) -> None:
    """Both optional; `port check-ledger` falls back to the port root's sole
    batch/source the same way `port map`'s non-batch path falls back to its
    sole source (`port_config.find_source`), never guessing among several."""
    ap.add_argument("--batch", default=None, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")


def _init_args(ap: argparse.ArgumentParser) -> None:
    """`--batch` is required; `port init` never guesses which batch's frozen
    snapshot to declare from."""
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")


def _not_written(findings: list) -> str:
    return ("NOT WRITTEN — %d error(s) in the planned line:\n" % len(findings)
            + "\n".join("  · [%s] %s" % (code, msg) for code, msg in findings))


def write_all(ledger: Ledger, events: list, verb: str, *, apply: bool) -> None:
    """Every port verb's batch write: the whole plan through `append_events`
    (validated in order, all-or-nothing, one lock and one read); a refusal
    names the node and writes nothing. `apply=False` validates only."""
    refused, findings = append_events(ledger, events, apply=apply)
    if refused is not None:
        raise hs_family.RefusedError("port %s: refused at %r; nothing written\n%s"
                                     % (verb, refused.node, _not_written(findings)))


def _select_nodes(state: dict, prefix: str, only_status: "list[str]") -> "list[str]":
    """Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/
    src/portkit/map_bulk.py:29-33` (`select_nodes`). Nodes whose REPLAYED
    latest status is in `only_status` (all, when empty), sorted, under the
    string `prefix` — a plain string match, never a key-shape parse: a
    prefix like `82fe9be7::gsm_agent.causal.` selects every sub-module on
    purpose, and the printed count below is the guard against that."""
    keep = set(only_status or ())
    return sorted(k for k, e in state.items()
                  if k.startswith(prefix) and (not keep or e.status in keep))


def _read_node_list(path: str) -> "list[str]":
    """Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/
    src/portkit/map_bulk.py:36-43` (`read_node_list`). One node key per
    line; blank lines and `#`-comments skipped, de-duplicated in file
    order."""
    seen: "dict[str, None]" = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                seen.setdefault(line)
    return list(seen)


def _snapshot_node_keys(idx: dict, anchor: str) -> "set[str]":
    """Every OLD-tree node key one snapshot's `_index.json` names, at
    `anchor` — ported (algorithm-faithful, trimmed to the KEY SET
    `INIT`'s bulk declare needs) from `ob-tools@60075cb:packages/
    portkit/src/portkit/ledger/ledger_io.py:81-95` (`snapshot_nodes`).
    Upstream's per-node payload (`sha`/`path`/`dist`/`kind`/`lines`) has no
    caller on this path — `init` only ever diffs the KEY SET against the
    ledger's own `replay()` state."""
    out: "set[str]" = set()
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            for s in m.get("symbols") or []:
                for holder in [s] + list(s.get("methods") or []):
                    out.add("%s::%s::%s" % (anchor, m["module"], holder["name"]))
    return out


def _ctx(args: argparse.Namespace) -> int:
    """`hs-code port ctx [--batch <name>]` — print every path this domain's
    defaults resolve to, the contract between the walk-up and the caller.
    Ported (algorithm-faithful shape, this tree's own `.port/` layout) from
    `ob-tools@60075cb:packages/portkit/src/portkit/batch/context.py`
    (`run_ctx`). Read-only; never writes; always exit 0 (a repo with no
    `--batch` given, or no batches declared at all, is a normal state, not
    a finding to flag). The envelope's `data` is ONE document (`port_root`, `ledger`,
    `batch`, `batches`) built from the same `data` dict the text lines are
    derived from, so the two can't drift."""
    root, opts, batch_name = args.ctx.root, args.opts, args.batch_name

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    ledger_p = port_ledger_reader.ledger_path(port_root)
    ledger_exists = ledger_p.is_file()
    events_n = broken_n = 0
    if ledger_exists:
        ledger = Ledger.load(ledger_p)
        events_n = len(ledger.events)
        broken_n = len(ledger.broken)

    known = port_batch_reader.list_batches(port_root)
    data: dict = {
        "port_root": str(port_root),
        "ledger": {"path": str(ledger_p), "events": events_n, "broken": broken_n},
        "batch": None,
        "batches": known,
    }

    if batch_name is not None:
        try:
            batch = port_batch_reader.load_batch(port_root, batch_name)
        except (port_batch_reader.BatchNotFoundError,
                port_batch_reader.BatchConfigError) as e:
            return Verdict.unresolved(e).exit
        ids = sorted(str(s.get("id")) for s in batch.sources)
        specs = (sorted(p.stem for p in batch.specs_dir.glob("*.yaml"))
                 if batch.specs_dir.is_dir() else [])
        data["batch"] = {"name": batch.name, "dir": str(batch.dir), "sources": ids,
                          "frozen": batch.frozen_snapshot or None,
                          "new": batch.new_snapshot or None, "specs": specs}

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(Verdict.ok(data).exit)

    events_s = "(none yet)" if not ledger_exists else "%d event(s)" % events_n
    if broken_n:
        events_s += "  ⚠ %d broken line(s)" % broken_n
    print("port root : %s" % data["port_root"])
    print("ledger    : %s  (%s)" % (ledger_p, events_s))

    b = data["batch"]
    if b is None:
        print("batch     : (none — pass --batch NAME)")
        print("batches   : %s" % (", ".join(known) or "(none declared)"))
        return int(Verdict.ok(data).exit)

    print("batch     : %s" % b["name"])
    print("batch dir : %s" % b["dir"])
    print("sources   : %s" % (", ".join(b["sources"]) or "(none)"))
    print("frozen    : %s" % (b["frozen"] or "(none)"))
    print("new       : %s" % (b["new"] or "(none)"))
    print("specs     : %s" % (", ".join(b["specs"]) or "(none)"))
    return int(Verdict.ok(data).exit)


CTX = Command(add_args=_ctx_args, run=_ctx)


def _init(args: argparse.Namespace) -> int:
    """`hs-code port init --batch <name> [--source <id>]` — declare
    `unported` in BULK for every node of the batch's frozen snapshot that
    has no ledger line yet. Ported (algorithm-faithful) from `ob-tools@
    60075cb:packages/portkit/src/portkit/port_verbs.py:271` (`run_init`),
    now declaring against a NAMED BATCH's frozen snapshot instead of a bare
    `--snap DIR` — the only verb that writes in bulk straight from a
    snapshot, never from a caller-given node list.

    `src.id`/`src.sha` on every appended line come from the batch: `src.sha`
    is the batch's own PINNED sha (the frozen snapshot must agree with it —
    a batch whose frozen snapshot was taken at a different commit is
    refused outright, never silently trusted, through `port_batch_reader.
    pinned_frozen_commit`), `src.id` is the batch's sole declared source, or
    the one named by `--source` when it declares more than one.
    """
    root, opts = args.ctx.root, args.opts
    batch_name, source_id = args.batch_name, args.source_id

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError,
            port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit

    if not batch.frozen_snapshot:
        return Verdict.broken(
            "batch %r has no `frozen_snapshot` declared — nothing to "
            "declare `unported` against" % batch.name).exit

    source, source_problem = port_batch_reader.find_source(batch, source_id)
    if source_problem:
        return Verdict.unresolved(source_problem).exit

    # refuse a frozen snapshot that is not the batch's own pin BEFORE any
    # listing/dry-run output — a mismatch writes nothing, ever.
    pinned, idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)

    nodes = _snapshot_node_keys(idx, pinned[:8])

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(_not_written(e.findings)).exit
    state = ledger.replay()
    todo = sorted(set(nodes) - set(state))

    print("batch      : %s" % batch.name)
    print("snapshot   : %s" % batch.frozen_snapshot)
    print("old nodes  : %d" % len(nodes))
    print("with lines : %d" % len(set(nodes) & set(state)))
    print("to declare `unported`: %d" % len(todo))

    if not todo:
        print("\nnothing to declare.")
        return int(Verdict.ok().exit)

    apply = bool(getattr(opts, "apply", False))
    if not apply:
        for k in todo[:20]:
            print("  %s" % k)
        if len(todo) > 20:
            print("  … %d more" % (len(todo) - 20))
        print("\ndry-run: nothing written (--apply to append)")
        return int(Verdict.ok().exit)

    ts, actor = now_ts(), resolve_actor()
    src = {"id": source.get("id"), "sha": pinned}
    events = [
        port_ledger_reader.Event(
            ts=ts, node=k, status="unported", by="human", actor=actor,
            note="bulk declaration by port init", extra={"src": src})
        for k in todo
    ]
    write_all(ledger, events, "init", apply=True)
    print("\nappended %d line(s) to %s" % (len(events), ledger.path))
    return int(Verdict.ok().exit)


INIT = Command(add_args=_init_args, run=_init)


def _check_ledger(args: argparse.Namespace) -> int:
    """`hs-code port check-ledger [--batch <name>] [--source <id>]`
    — a thin door over `port_ledger_laws.collect`: read-only, never writes.
    `--batch` falls back to the port root's sole batch when it declares
    exactly one, the same single-entry convenience `port_batch_reader.
    find_source` already gives `--source`; more than one with none named is
    refused, naming every known name.

    The ledger is read ONLY through `Ledger.load_checked` — a broken line
    (L0) refuses outright (exit 2), before a single one of the other eleven
    laws even runs: L0 is not a softer finding among the twelve, it is a
    different KIND of failure (the plan's own "separate never-off tier"
    ruling — see `port_ledger_laws`'s own module docstring). Every OTHER
    finding is reported, never refused on: the exit code is 1 when
    `collect()` found anything, 0 when it found nothing — the same
    read/check convention `judge-nodes`/`judge-removed` already use,
    distinct from `map`/`init`'s write-verb table."""
    root, opts = args.ctx.root, args.opts
    batch_name, source_id = args.batch_name, args.source_id

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    if batch_name is None:
        known = port_batch_reader.list_batches(port_root)
        if len(known) != 1:
            return Verdict.broken(
                "port check-ledger: %d batch(es) declared (%s) — pass --batch NAME"
                % (len(known), ", ".join(known) or "none")).exit
        batch_name = known[0]

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError,
            port_batch_reader.BatchConfigError) as e:
        return Verdict.broken(str(e)).exit

    source, source_problem = port_batch_reader.find_source(batch, source_id)
    if source_problem:
        return Verdict.broken(source_problem).exit

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(
            "port check-ledger: %s" % "; ".join(msg for _code, msg in e.findings)).exit

    scan = port_ledger_laws.collect(ledger, port_root, batch, source)

    data = {
        "batch": batch.name,
        "old_key": scan.old_key,
        "old_key_rows": scan.old_key_rows,
        "new_key": scan.new_key,
        "new_ran": scan.new_ran,
        "events": scan.n_events,
        "uncheckable": scan.uncheckable,
        "pending_decisions": scan.pending_decisions,
        "drift_lost": scan.drift_lost,
        "findings": [{"law": f.law, "node": f.node, "msg": f.msg} for f in scan.findings],
    }
    verdict = Verdict.red(data) if scan.findings else Verdict.ok(data)

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return int(verdict.exit)

    print("batch      : %s" % batch.name)
    print("old key    : %s  (%d row(s))" % (scan.old_key, scan.old_key_rows))
    print("new key    : %s" % (scan.new_key if scan.new_ran else "(no new_snapshot — L7/L10/L11 did not run)"))
    print("events     : %d" % scan.n_events)
    if scan.uncheckable:
        print("uncheckable: %d blocker(s) (gate:/test:/external:)" % scan.uncheckable)
    if scan.pending_decisions:
        print("pending    : %s" % ", ".join(sorted(scan.pending_decisions)))
    if scan.drift_lost:
        print("drift lost : %d destination(s) — no snapshot left to compare against" % scan.drift_lost)
    if not scan.findings:
        print("\nclean — no law finding.")
        return int(verdict.exit)
    print("\n%d finding(s):" % len(scan.findings))
    for f in scan.findings:
        print("  [%s] %s: %s" % (f.law, f.node, f.msg))
    return int(verdict.exit)


CHECK_LEDGER = Command(add_args=_check_ledger_args, run=_check_ledger)


def _map(args: argparse.Namespace) -> int:
    """`hs-code port map (<node>|--prefix <p>|--nodes-from <file>) --status
    <status> [--to <dest>]... [--reason <text>] [--blocked-by <ref>]...
    [--by human|suggest:<sha>] [--note <text>] [--source <id>]
    [--only-status <status>]... [--batch <name>]` — one or many lines,
    every one through the ONE write door.

    Dry-run by default (`--apply` writes for real). The bulk path
    (`--prefix`/`--nodes-from`) validates the WHOLE plan before writing a
    single line — one blocked line refuses the batch, nothing written.

    `--batch NAME` resolves the `new@batch` anchor shorthand (checked as a
    prefix, in the node AND in every `--to`) against that batch's own
    latest new-tree snapshot, and makes its PINNED source sha win over a
    live git resolve: a node whose own 8-hex anchor disagrees with the
    batch's pin is refused outright, never silently trusted, and the source
    repo is never even touched. Without `--batch`, `src.sha` is resolved
    live — that source's repo, at the node key's own anchor
    (`codegraph._core.git.resolve_ref`) — exactly as before this domain had
    a batch concept at all.
    """
    _check_map_args(args)
    root, opts = args.ctx.root, args.opts
    (node, prefix, nodes_from, only_status, status, to, reason,
     blocked_by, by, note, source_id, batch_name) = (
        args.node, args.prefix, args.nodes_from, args.only_status, args.status, args.to,
        args.reason, args.blocked_by, args.by, args.note, args.source_id, args.batch_name)

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    config_path = port_workspace.port_yaml_path(port_root)
    if not config_path.is_file():
        return Verdict.broken(
            "no `.port/port.yaml` under %s — run /hs:port to plan a port"
            % port_root).exit
    cfg, problems = port_config.load_and_validate(config_path)
    if problems:
        return Verdict.broken(
            "`.port/port.yaml` is invalid:\n"
            + "\n".join("  · %s" % p for p in problems)).exit

    batch = None
    if batch_name is not None:
        try:
            batch = port_batch_reader.load_batch(port_root, batch_name)
        except (port_batch_reader.BatchNotFoundError,
                port_batch_reader.BatchConfigError) as e:
            return Verdict.unresolved(e).exit

    def resolve_anchor(key_str: str) -> str:
        if not key_str.startswith(port_batch_reader.BATCH_ANCHOR_PREFIX):
            return key_str
        if batch is None:
            raise hs_family.RefusedError(
                "port map: %r carries an unresolved `new@batch` anchor — "
                "pass --batch NAME to resolve it, or write the literal "
                "`new@<hash8>` form instead" % key_str)
        return port_batch_reader.resolve_batch_anchor(port_root, batch, key_str)

    if batch is not None:
        source, source_problem = port_batch_reader.find_source(batch, source_id)
    else:
        source, source_problem = port_config.find_source(cfg, source_id)
    if source_problem:
        return Verdict.unresolved(source_problem).exit

    def build_src(key, raw: str) -> dict:
        if batch is not None:
            pinned = port_batch_reader.check_anchor_pinned(
                key.anchor, batch, source, what="port map: node %r's anchor" % raw)
            return {"id": source.get("id"), "sha": pinned}
        source_repo = (port_root / str(source.get("repo", ""))).resolve()
        try:
            sha = resolve_ref(source_repo, key.anchor)
        except GitError as e:
            raise hs_family.RefusedError(
                "port map: could not resolve %r in source %r (%s)"
                % (key.anchor, source.get("id"), e))
        return {"id": source.get("id"), "sha": sha}

    def parse_and_check(raw: str):
        try:
            key = port_ledger_spec.parse_key(raw)
        except PortingLedgerError as e:
            raise hs_family.RefusedError("port map: %s" % e)
        if key.is_new:
            raise hs_family.RefusedError(
                "port map: node %r carries a new-tree anchor (%r) — this "
                "verb only resolves `src.sha` by live-checking the OLD-tree "
                "SOURCE repo; a new-tree node's line comes from `add-module`"
                "/`port-batch`/`judge-nodes`/`judge-removed` instead"
                % (raw, key.anchor))
        return key

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(_not_written(e.findings)).exit
    apply = bool(getattr(opts, "apply", False))
    to_resolved = [resolve_anchor(t) for t in to]

    if prefix is not None or nodes_from is not None:
        return _cmd_map_bulk(
            ledger=ledger, apply=apply, prefix=prefix, nodes_from=nodes_from,
            only_status=only_status, status=status, to=to_resolved,
            reason=reason, blocked_by=blocked_by, by=by, note=note,
            resolve_anchor=resolve_anchor, parse_and_check=parse_and_check,
            build_src=build_src)

    raw_node = resolve_anchor(node)
    key = parse_and_check(raw_node)
    src = build_src(key, raw_node)
    ev = port_ledger_reader.Event(
        ts=now_ts(), node=raw_node, status=status, by=by,
        actor=resolve_actor(), note=note,
        extra={"to": tuple(to_resolved), "reason": reason,
               "blocked_by": tuple(blocked_by), "src": src})

    prev = ledger.replay().get(raw_node)
    findings = append_event(ledger, ev, apply=apply)
    if findings:
        raise hs_family.RefusedError(_not_written(findings))

    if not apply:
        print("dry-run: valid, nothing written — %s %s%s (--apply to append)"
              % (raw_node, (prev.status + " -> ") if prev else "", status))
        return int(Verdict.ok().exit)

    print("written: %s" % raw_node)
    print("         %s%s" % ((prev.status + " -> ") if prev else "", status))
    return int(Verdict.ok().exit)


MAP = Command(add_args=_map_args, run=_map)


def _cmd_map_bulk(*, ledger: Ledger, apply, prefix, nodes_from, only_status, status,
                   to, reason, blocked_by, by, note, resolve_anchor,
                   parse_and_check, build_src) -> int:
    """The `--prefix`/`--nodes-from` path of `MAP`: one event PER
    selected/listed node, all sharing the same `--status`/`--to`/`--reason`/
    `--blocked-by` — ported (algorithm-faithful) from `ob-tools@60075cb:
    packages/portkit/src/portkit/map_bulk.py:46-77` (`run_bulk`),
    ALL-OR-NOTHING: the whole plan goes through `append_events` (`write_all`),
    one blocked line refuses the whole plan."""
    if prefix is not None:
        nodes = _select_nodes(ledger.replay(), prefix, only_status)
        label = repr(prefix)
    else:
        try:
            listed = _read_node_list(nodes_from)
        except OSError as e:
            return Verdict.broken(str(e)).exit
        nodes = [resolve_anchor(n) for n in listed]
        label = repr(nodes_from)

    if not nodes:
        print("no node matched %s — nothing to write" % label)
        return int(Verdict.ok().exit)

    print("%d node(s) under %s -> %s%s"
          % (len(nodes), label, status,
             "" if apply else "  (dry-run, --apply appends)"))

    stamp = now_ts()
    actor = resolve_actor()
    events = []
    for raw in nodes:
        key = parse_and_check(raw)
        src = build_src(key, raw)
        events.append(port_ledger_reader.Event(
            ts=stamp, node=raw, status=status, by=by, actor=actor, note=note,
            extra={"to": tuple(to), "reason": reason,
                   "blocked_by": tuple(blocked_by), "src": src}))

    write_all(ledger, events, "map", apply=apply)
    if not apply:
        print("dry-run: valid, nothing written (--apply to append)")
        return int(Verdict.ok().exit)
    print("written: %d line(s) -> %s" % (len(events), ledger.path))
    return int(Verdict.ok().exit)
