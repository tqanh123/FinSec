"""hs-code's `port` domain — `docs`: what one batch did, read from its ledger
and its own rulings. A FLAT module under `harness/scripts/` (same
`hs-code-registry.d` reachability reason as every other
`codegraph_port_*_verbs.py`).

`hs-code port docs --batch B [--source ID] [--apply]`

Adapted from `ob-tools@60075cb:packages/ob-cli/src/obt/docs/wavedocs.py`
(`progress_data`, `mapping_data`, `decisions_data`). Three documents, every
count per NODE of the replayed state, never per line — the ledger supersedes
by appending (upstream measured 65728 rows against 25608 nodes on one batch).

WHICH NODES: the ledger here is one per port root, not one per batch, so a
batch's nodes are those keyed at its own anchors — the frozen snapshot's
commit (`<hash8>::`) and, when declared, its new snapshot's (`new@<hash8>::`).

DECISIONS: upstream read the batch's decision FILES because that is where a
ruling is written. Here a batch's rulings are its open items
(`.port/batches/<b>/open.jsonl` — who chose what between which candidates) and
the decisions its rows wait on (`blocked_by: decision:<id>`), titled from the
decision register when it holds the id. Nothing is summarised out of prose.

STAMP: each document names the sha256 of the ledger file it was read from, the
row count, and nothing that changes between runs — the same ledger gives the
same bytes, so a diff of two generations is a diff of the ledger. Upstream
stamped the pinned snapshot instead; the ledger is this family's input.

`--apply` writes `.port/batches/<b>/docs/{progress,mapping,decisions}.md`;
without it the three documents' data is printed and nothing is written. The
`--arch` architecture page and `guide-doc` are not ported.

WHAT IT CANNOT SEE (upstream's list, unchanged in substance): work that left
no ledger line; whether a recorded status is true; why a node ended where it
did beyond the row's own `reason`. A broken ledger or open-item line is exit
2 — a hole in the denominator is refused, not counted around.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import decision_register  # noqa: E402
import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_open  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_spec import new_anchor  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger  # noqa: E402

_DOCS = ("progress", "mapping", "decisions")


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")


def _module_of(node: str) -> str:
    parts = node.split("::")
    return parts[1] if len(parts) >= 3 else "?"


def _anchors(port_root: Path, batch: port_batch_reader.Batch, source_id: "str | None") -> "list[str]":
    if not batch.frozen_snapshot:
        raise hs_family.UsageError("batch %r has no `frozen_snapshot` — no anchor names its nodes"
                                   % batch.name)
    source, problem = port_batch_reader.find_source(batch, source_id)
    if problem:
        raise hs_family.UsageError(problem)
    old_commit, _idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    out = [old_commit[:8]]
    if batch.new_snapshot:
        out.append(new_anchor(port_batch_reader.snapshot_commit(port_root, batch.new_snapshot)))
    return out


def progress_data(ledger: Ledger, state: dict, nodes: "set[str]") -> dict:
    stamps = sorted(state[n].ts for n in nodes if state[n].ts)
    return {"nodes": len(nodes), "rows": sum(1 for ev in ledger.events if ev.node in nodes),
            "by_status": dict(sorted(Counter(state[n].status for n in nodes).items())),
            "by_hand": dict(sorted(Counter(state[n].by for n in nodes).items())),
            "first": stamps[0] if stamps else None, "last": stamps[-1] if stamps else None}


def mapping_data(state: dict, nodes: "set[str]") -> dict:
    rows = [{"node": n, "status": state[n].status, "by": state[n].by, "ts": state[n].ts,
             "to": list(state[n].extra.get("to") or ()),
             "reason": state[n].extra.get("reason") or "", "module": _module_of(n)}
            for n in sorted(nodes)]
    by_module: "dict[str, Counter]" = defaultdict(Counter)
    for r in rows:
        by_module[r["module"]][r["status"]] += 1
    return {"rows": rows, "by_module": {m: dict(sorted(c.items()))
                                        for m, c in sorted(by_module.items())}}


def decisions_data(port_root: Path, batch: str, state: dict, nodes: "set[str]") -> dict:
    read = port_open.read_open(port_root, batch)
    if read.broken:
        raise hs_family.UsageError("open items of batch %r are broken: %s" % (
            batch, port_open.broken_line_findings(read.broken)))
    items = [{"id": it.id, "kind": it.kind, "subject": it.subject, "state": it.state,
              "candidates": list(it.candidates), "step": it.step,
              "choice": (it.resolution or {}).get("choice"),
              "why": (it.resolution or {}).get("why"),
              "actor": (it.resolution or {}).get("actor"),
              "ts": (it.resolution or {}).get("ts")}
             for it in sorted(read.items.values(), key=lambda i: i.id)]
    waits: "Counter[str]" = Counter(
        b.partition(":")[2].strip() for n in nodes
        for b in state[n].extra.get("blocked_by") or () if b.startswith("decision:"))
    titles = {str(r.get("id")): r.get("title") for r in decision_register.parse_decisions(port_root)}
    return {"items": items,
            "waited_on": [{"id": d, "rows": c, "title": titles.get(d)}
                          for d, c in sorted(waits.items())]}


def _cell(v: object) -> str:
    return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")


def _table(head: "list[str]", rows: "list[list]") -> "list[str]":
    return (["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            + ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows])


def render(batch: str, data: dict) -> "dict[str, str]":
    stamp = "ledger sha256 `%s` · %d row(s) read" % (data["ledger_sha256"], data["ledger_rows"])
    p, m, d = data["progress"], data["mapping"], data["decisions"]
    progress = ["# progress — %s" % batch, "", stamp, "",
                "One entry per NODE, not per ledger line: %d node(s) from %d row(s), first %s, "
                "last %s." % (p["nodes"], p["rows"], p["first"], p["last"]), "", "## by status", ""]
    progress += _table(["status", "nodes"], [[k, v] for k, v in p["by_status"].items()])
    progress += ["", "## recorded by", ""] + _table(["by", "nodes"],
                                                     [[k, v] for k, v in p["by_hand"].items()])
    statuses = sorted({s for c in m["by_module"].values() for s in c})
    mapping = ["# mapping — %s" % batch, "", stamp, "", "## per module", ""]
    mapping += _table(["module", *statuses], [[mod, *[c.get(s, 0) for s in statuses]]
                                              for mod, c in m["by_module"].items()])
    mapping += ["", "## every node", ""] + _table(
        ["node", "status", "to", "by", "reason"],
        [[r["node"], r["status"], ", ".join(r["to"]), r["by"], r["reason"]] for r in m["rows"]])
    decisions = ["# decisions — %s" % batch, "", stamp, "", "## open items", ""]
    decisions += (_table(["id", "kind", "subject", "state", "choice", "candidates", "by", "at"],
                         [[i["id"], i["kind"], i["subject"], i["state"], i["choice"] or i["why"],
                           ", ".join(i["candidates"]), i["actor"], i["ts"]] for i in d["items"]])
                  if d["items"] else ["The batch raised no open item."])
    decisions += ["", "## decisions its rows wait on", ""]
    decisions += (_table(["id", "rows", "title"],
                         [[w["id"], w["rows"], w["title"] or "(not in the decision register)"]
                          for w in d["waited_on"]])
                  if d["waited_on"] else ["No row waits on a decision."])
    return {"progress": "\n".join(progress) + "\n", "mapping": "\n".join(mapping) + "\n",
            "decisions": "\n".join(decisions) + "\n"}


def _docs(args: argparse.Namespace) -> int:
    try:
        port_root = port_workspace.find_port_root(args.ctx.root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit
    try:
        batch = port_batch_reader.load_batch(port_root, args.batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit
    path = port_ledger_reader.ledger_path(port_root)
    try:
        ledger = Ledger.load_checked(path)
    except BrokenLedgerError as e:
        raise hs_family.UsageError("the ledger is broken: %s" % e) from e
    anchors = _anchors(port_root, batch, args.source_id)
    state = ledger.replay()
    nodes = {n for n in state if n.split("::", 1)[0] in anchors}
    data: dict = {"batch": batch.name, "anchors": anchors,
            "ledger_sha256": hashlib.sha256(path.read_bytes() if path.is_file() else b"").hexdigest(),
            "ledger_rows": len(ledger.events),
            "progress": progress_data(ledger, state, nodes), "mapping": mapping_data(state, nodes),
            "decisions": decisions_data(port_root, batch.name, state, nodes)}
    apply = bool(getattr(args.opts, "apply", False))
    out_dir = port_batch_reader.batches_dir(port_root) / batch.name / "docs"
    data["apply"], data["out"] = apply, [str(out_dir / ("%s.md" % n)) for n in _DOCS]
    if apply:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in render(batch.name, data).items():
            (out_dir / ("%s.md" % name)).write_text(text, encoding="utf-8")
    if getattr(args.opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        p = data["progress"]
        print("docs %s: %d node(s), %d row(s), ledger %s" % (
            batch.name, p["nodes"], p["rows"], data["ledger_sha256"][:12]))
        for f in data["out"]:
            print("  %s %s" % ("wrote" if apply else "would write", f))
    return int(Verdict.ok(data).exit)


DOCS = Command(add_args=_args, run=_docs)
