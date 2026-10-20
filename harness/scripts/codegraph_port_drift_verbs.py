"""hs-code's `port` domain — the drift verbs of a catch-up batch. A FLAT module
under `harness/scripts/` (same `hs-code-registry.d` reachability reason as every
other `codegraph_port_*_verbs.py`).

`drift-scan --batch B` — what the source added, changed, deleted and renamed
between the predecessor's pin and B's, per file, per symbol (matched by body
between the two frozen snapshots) and per dist (B's own assignment rules).
Read-only; exit 0 whenever it could measure. A deletion or rename is listed
under `red` and becomes a disposition item of `drift-hunks`, not a stop:
upstream exited 1 there, which on a lane would hold the lane red with nothing
the scan itself could ever change.

`drift-hunks --batch B` — every hunk of the src drift (and every hunkless file)
carries exactly one disposition in `.port/batches/B/drift/hunks.jsonl`. Check
(default): red while an item has no line, a line names no item, an item has two
lines, or a line is malformed. `--mark ID --status S` / `--mark-file PATH
--status S` append validated lines (dry-run unless `--apply`); `applied` needs
`--dest`, a file inside the target (and, with `--symbol`, one that defines it).
`--list` prints the item ids.

The engine is `port_drift` (see its docstring for what each derives instead of
upstream's flags).
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
import port_drift  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_refs  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core import walk  # noqa: E402
from codegraph._core.query.engine import QueryEngine  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_batch_verbs import load_assignment_rules, resolve_dist  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger  # noqa: E402


def _batch_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")


def _load(args: argparse.Namespace) -> port_drift.Drift:
    try:
        port_root = port_workspace.find_port_root(args.ctx.root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        raise hs_family.UsageError(str(e)) from e
    try:
        return port_drift.load(port_root, args.batch_name, args.source_id)
    except port_batch_reader.BatchNotFoundError as e:
        raise hs_family.UsageError(str(e)) from e


def _ledger(port_root: Path) -> Ledger:
    try:
        return Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        raise hs_family.UsageError("the ledger is broken: %s" % e) from e


def _homes(d: port_drift.Drift) -> dict:
    """old module -> target module counts, from the rows under either anchor —
    method rows included (`Index.places`)."""
    return port_refs.load_index(_ledger(d.port_root), d.package or "",
                                keys=[d.from_key, d.to_key]).places


def _as_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args.opts, "json", False))


# ── drift-scan ───────────────────────────────────────────────────────────────

def _scan(args: argparse.Namespace) -> int:
    d = _load(args)
    rows = port_drift.file_rows(d)
    sym = QueryEngine(d.from_idx).changed(QueryEngine(d.to_idx), top=0).data
    symbols = {k: [list(x) for x in sym[k]] for k in ("changed", "added", "removed")}
    modules = port_drift.path_modules(d.from_idx, d.to_idx)
    rules_path = d.batch.dir / "map" / "assignment.yaml"
    notes: "list[str]" = []
    if rules_path.is_file():
        rules = load_assignment_rules(rules_path)
    else:
        rules = {}
        notes.append("no %s: every src file is UNRULED" % rules_path.relative_to(d.port_root))
    zones = port_drift.zone(rows, symbols, modules, rules, resolve_dist)
    homes = _homes(d)
    by_status: "dict[str, int]" = {}
    by_area: "dict[str, dict[str, int]]" = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        area = by_area.setdefault(r["area"], {})
        area[r["status"]] = area.get(r["status"], 0) + 1
    data = {"batch": d.batch.name, "from_batch": d.from_batch.name,
            "from": d.from_key, "to": d.to_key, "source": d.source_id,
            "src_roots": list(d.src_prefixes),
            "files": {"by_status": dict(sorted(by_status.items())),
                      "by_area": {k: dict(sorted(v.items())) for k, v in sorted(by_area.items())}},
            "rows": rows, "symbols": symbols, "zones": zones,
            "split": port_drift.split_files(rows, modules, homes),
            "red": [r for r in rows if r["status"] in "DR"], "notes": notes}
    if _as_json(args):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print(_render_scan(data))
    return int(Verdict.ok(data).exit)


def _render_scan(data: dict, top: int = 15) -> str:
    bs, s = data["files"]["by_status"], data["symbols"]
    out = ["drift-scan  %s: %s -> %s  (from batch %s)" % (data["batch"], data["from"], data["to"],
                                                         data["from_batch"]),
           "  files    A %d  M %d  D %d  R %d" % tuple(bs.get(k, 0) for k in "AMDR"),
           "  areas    " + " | ".join("%s %d" % (a, sum(c.values()))
                                      for a, c in data["files"]["by_area"].items()),
           "  symbols  +%d ~%d -%d" % (len(s["added"]), len(s["changed"]), len(s["removed"])),
           "  by dist  (symbols + ~ - / files)"]
    for dist, c in data["zones"]["per_dist"].items():
        out.append("    %-26s +%-4d ~%-4d -%-3d files A%d M%d D%d R%d" % (
            dist, c.get("added", 0), c.get("changed", 0), c.get("removed", 0),
            c.get("files_A", 0), c.get("files_M", 0), c.get("files_D", 0), c.get("files_R", 0)))
    un = data["zones"]["unruled_files"]
    out.append("  UNRULED src files: %d" % len(un))
    out += ["    %s" % p for p in un[:top]]
    out.append("  split files (the ledger ported them into several modules): %d" % len(data["split"]))
    out += ["    %s %s -> %s" % (h["status"], h["path"], ", ".join(h["homes"]))
            for h in data["split"][:top]]
    if data["red"]:
        out.append("  deleted/renamed: %d — each needs a disposition in `drift-hunks`"
                   % len(data["red"]))
        out += ["    %s %s%s" % (r["status"], (r["old"] + " -> ") if r["old"] else "", r["path"])
                for r in data["red"][:top]]
    out += ["  note: %s" % n for n in data["notes"]]
    return "\n".join(out)


# ── drift-hunks ──────────────────────────────────────────────────────────────

def _hunks_args(ap: argparse.ArgumentParser) -> None:
    _batch_args(ap)
    ap.add_argument("--list", action="store_true", dest="list_items")
    ap.add_argument("--mark", default=None, metavar="ID")
    ap.add_argument("--mark-file", default=None, metavar="PATH")
    ap.add_argument("--status", default=None)
    ap.add_argument("--dest", default=None)
    ap.add_argument("--by", default=None)
    ap.add_argument("--reason", default=None)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--replace", action="store_true")


def _hunks(args: argparse.Namespace) -> int:
    d = _load(args)
    ids = port_drift.items(d)
    sidecar = port_drift.dispositions_path(d.port_root, d.batch.name)
    if args.list_items:
        data = {"batch": d.batch.name, "items": ids}
        print(json.dumps(data, ensure_ascii=False) if _as_json(args) else "\n".join(ids))
        return int(Verdict.ok(data).exit)
    if args.mark or args.mark_file:
        return _mark(args, d, ids, sidecar)
    lines, bad = port_drift.read_dispositions(sidecar)
    judged = port_drift.judged_items(d, _ledger(d.port_root).replay())
    res = {"batch": d.batch.name, **port_drift.check(ids, lines, bad, judged)}
    if _as_json(args):
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
    else:
        print("drift-hunks %s: %d item(s), %s, %d judged by ledger rows"
              % (d.batch.name, res["items"], res["counts"], len(res["judged"])))
        for key, tag in (("undisposed", "no disposition"),
                         ("orphans", "disposition names no item"),
                         ("duplicates", "more than one disposition")):
            for item in res[key]:
                print("  RED  %s: %s" % (tag, item))
        for m in res["malformed"]:
            print("  RED  line %s: %s" % (m["line"], m["why"]))
        print("floor:", "OK" if res["ok"] else "RED")
    return int((Verdict.ok(res) if res["ok"] else Verdict.red(res)).exit)


def _mark(args: argparse.Namespace, d: port_drift.Drift, ids: "list[str]", sidecar: Path) -> int:
    if not args.status:
        raise hs_family.UsageError("--mark/--mark-file needs --status (%s)"
                                   % ", ".join(port_drift.STATUSES))
    if args.mark:
        if args.mark not in ids:
            raise hs_family.UsageError("%s is not an item of batch %r's drift (`--list` names them)"
                                       % (args.mark, d.batch.name))
        todo = [args.mark]
    else:
        todo = [i for i in ids if port_drift.item_path(i) == args.mark_file]
        if not todo:
            raise hs_family.UsageError("no item of %s in batch %r's drift"
                                       % (args.mark_file, d.batch.name))
    in_force = {str(ln.get("hunk")): ln["_n"]
                for ln in port_drift.active(port_drift.read_dispositions(sidecar)[0])[0]}
    fields: "dict[str, str | int | None]" = {"dest": args.dest, "by": args.by,
                                             "reason": args.reason}
    if args.replace:
        if not args.mark or args.mark not in in_force:
            raise hs_family.UsageError("--replace needs --mark naming an item that already has a "
                                       "disposition in force")
        fields["replaces"] = in_force[args.mark]
        skipped: "list[str]" = []
    else:
        skipped = [i for i in todo if i in in_force]
        if args.mark and skipped:
            raise hs_family.RefusedError(
                "%s already has a disposition — the sidecar is append-only; a second line "
                "would make it ambiguous (`--replace` supersedes it with a new line)" % args.mark)
        todo = [i for i in todo if i not in in_force]
    lines = [port_drift.disposition(i, args.status, **fields) for i in todo]
    for line in lines or [port_drift.disposition("probe", args.status, **fields)]:
        why = port_drift.validate_line(line)
        if why:
            raise hs_family.UsageError(why)
    if args.status == "applied":
        why = port_drift.check_dest(d.port_root, args.dest or "", args.symbol)
        if not why:
            ctx = _Placement(d)
            for item in todo:
                why = ctx.why(item, args.dest or "")
                if why:
                    break
        if why:
            raise hs_family.RefusedError(why)
    apply = bool(getattr(args.opts, "apply", False))
    if apply:
        port_drift.append_dispositions(sidecar, lines)
    data = {"batch": d.batch.name, "status": args.status, "marked": todo,
            "skipped": skipped, "apply": apply}
    if _as_json(args):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("%s %d item(s) -> %s, %d already disposed%s" % (
            "recorded" if apply else "dry-run: would record", len(todo), args.status,
            len(skipped), "" if apply else " (--apply writes)"))
    return int(Verdict.ok(data).exit)


# ── where an applied hunk may land (drift-hunks --mark, drift-place) ──────────

class _Placement:
    """`port_drift.placement` with its three inputs read once: the target's
    layout, the bridge ledger `refactor move` keeps, and the ledger homes of
    each drifted file (by its path at the FROM pin, so a rename finds them)."""

    def __init__(self, d: port_drift.Drift):
        from codegraph.refactor import shim_debt
        from codegraph.refactor._layout import load_descriptor
        from codegraph.refactor.errors import RefactorError

        try:
            self.descriptor = load_descriptor(d.port_root, "port drift-place / drift-hunks --mark")
        except RefactorError as e:
            raise hs_family.UsageError(str(e)) from e
        manifest = d.port_root / shim_debt.MANIFEST
        try:
            self.bridges = shim_debt.read_manifest(manifest) if manifest.is_file() else []
        except RefactorError as e:
            raise hs_family.UsageError(str(e)) from e
        self.port_root = d.port_root
        ix = port_refs.load_index(_ledger(d.port_root), d.package or "",
                                  keys=[d.from_key, d.to_key])
        rs = port_refs.Resolver(ix, d.port_root, d.package or "", self.descriptor)
        mods = port_drift.path_modules(d.from_idx, d.to_idx)
        self.homes: "dict[str, list[str]]" = {}
        for r in port_drift.file_rows(d):
            old_mod = mods.get(r["old"] or r["path"], ("", ""))[1]
            files = (rs.file_of(m) for m in ix.module_places(old_mod))
            self.homes[r["path"]] = sorted(str(f) for f in files if f is not None)

    def why(self, item: str, dest: str) -> "str | None":
        return port_drift.placement(self.port_root, self.descriptor, dest,
                                    self.homes.get(port_drift.item_path(item), []), self.bridges)


def _place_args(ap: argparse.ArgumentParser) -> None:
    _batch_args(ap)
    ap.add_argument("--patch", default=None, metavar="FILE")


def _patch_targets(text: str) -> "list[str]":
    out = []
    for line in text.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            if p != "/dev/null":
                out.append(p[2:] if p.startswith("b/") else p)
    return out


def _place(args: argparse.Namespace) -> int:
    d = _load(args)
    ctx = _Placement(d)
    sidecar = port_drift.dispositions_path(d.port_root, d.batch.name)
    findings: "list[dict]" = []
    checked = 0
    for ln in port_drift.active(port_drift.read_dispositions(sidecar)[0])[0]:
        if ln.get("status") != "applied" or not ln.get("dest"):
            continue
        checked += 1
        dest = str(ln["dest"])
        why = port_drift.check_dest(d.port_root, dest) or ctx.why(str(ln["hunk"]), dest)
        if why:
            findings.append({"source": "disposition", "line": ln["_n"], "hunk": ln["hunk"],
                             "path": dest, "why": why})
    if args.patch:
        patch = Path(args.patch)
        patch = patch if patch.is_absolute() else d.port_root / patch
        if not patch.is_file():
            raise hs_family.UsageError("--patch %s is not a file" % args.patch)
        for target in _patch_targets(patch.read_text(encoding="utf-8", errors="replace")):
            checked += 1
            why = (port_drift.check_dest(d.port_root, target)
                   or port_drift.placement(d.port_root, ctx.descriptor, target, [], ctx.bridges))
            if why:
                findings.append({"source": "patch", "line": None, "hunk": None, "path": target,
                                 "why": why})
    data = {"batch": d.batch.name, "checked": checked, "findings": findings, "ok": not findings}
    if _as_json(args):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("drift-place %s: %d landing(s) checked, %d refused" % (d.batch.name, checked,
                                                                    len(findings)))
        for f in findings:
            print("  RED  %s: %s%s" % (f["path"], f["why"],
                                       "  [%s]" % f["hunk"] if f["hunk"] else ""))
        print("floor:", "OK" if data["ok"] else "RED")
    return int((Verdict.ok(data) if data["ok"] else Verdict.red(data)).exit)


# ── .rej leftovers (drift-rej) ───────────────────────────────────────────────



def _scan_suffix(root: Path, suffix: str) -> "list[Path]":
    import os
    out: "list[Path]" = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(x for x in dn if x != ".port" and not walk.skipped((x,)))
        out += [Path(dp) / f for f in sorted(fn) if f.endswith(suffix)]
    return out


def _describe_rej(p: Path, root: Path) -> dict:
    import hashlib
    data = p.read_bytes()
    text = data.decode("utf-8", errors="replace")
    return {"path": p.relative_to(root).as_posix(), "sha": hashlib.sha256(data).hexdigest()[:16],
            "hunks": sum(1 for ln in text.splitlines() if ln.startswith(("@@", "<<<<<<< "))),
            "head": " | ".join(text.splitlines()[:3])[:160]}


def _rej_args(ap: argparse.ArgumentParser) -> None:
    _batch_args(ap)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--orig", action="store_true")


def rej_record_path(port_root: Path, batch_name: str) -> Path:
    return port_batch_reader.batches_dir(port_root) / batch_name / "drift" / "rej.jsonl"


def _rej(args: argparse.Namespace) -> int:
    from hook_runtime import resolve_actor
    from port_ledger_writer import now_ts

    d = _load(args)
    root = d.port_root
    rejs = [_describe_rej(p, root) for p in _scan_suffix(root, ".rej")]
    origs = [p.relative_to(root).as_posix() for p in _scan_suffix(root, ".orig")] if args.orig else []
    apply = bool(getattr(args.opts, "apply", False))
    cleaned = 0
    if args.clean and rejs and apply:
        record = rej_record_path(root, d.batch.name)
        record.parent.mkdir(parents=True, exist_ok=True)
        actor, ts = resolve_actor(), now_ts()
        with record.open("a", encoding="utf-8") as fh:
            for r in rejs:
                fh.write(json.dumps({**r, "actor": actor, "ts": ts}, ensure_ascii=False,
                                    sort_keys=True) + "\n")
        for r in rejs:
            (root / r["path"]).unlink()
        cleaned, rejs = len(rejs), []
    data = {"batch": d.batch.name, "rej": rejs, "orig": origs, "cleaned": cleaned,
            "apply": apply, "ok": not rejs}
    if _as_json(args):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("drift-rej %s: %d .rej left, %d cleaned%s" % (
            d.batch.name, len(rejs), cleaned, ", %d .orig (info)" % len(origs) if args.orig else ""))
        for r in rejs:
            print("  RED  %s (%d refused hunk(s))" % (r["path"], r["hunks"]))
        for o in origs:
            print("  info %s" % o)
        if args.clean and not apply and rejs:
            print("dry-run: --apply records then deletes them")
        print("floor:", "OK" if data["ok"] else "RED")
    return int((Verdict.ok(data) if data["ok"] else Verdict.red(data)).exit)


DRIFT_SCAN = Command(add_args=_batch_args, run=_scan)
DRIFT_PLACE = Command(add_args=_place_args, run=_place)
DRIFT_REJ = Command(add_args=_rej_args, run=_rej)
DRIFT_HUNKS = Command(add_args=_hunks_args, run=_hunks)
