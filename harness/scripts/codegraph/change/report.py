"""`hs-code change report` — one measurement, two renderings: `report.json`
and a static HTML dashboard, both generated from the SAME data dict.

Ported from ob-tools' `devkit.docs.report` (pinned commit `60075cb`); the
engine is unchanged, `REPORT` is the hs-code adapter.

WHY ONE DICT
    A dashboard and a JSON export written by two code paths drift apart, and
    the one nobody looks at is wrong first. Here `collect()` builds the data
    ONCE; `render_json()` and `render_html()` only READ that dict — neither
    may recompute a number, so the two outputs cannot disagree. The tests
    enforce it mechanically: every figure in the JSON must appear verbatim in
    the HTML.

USAGE
    hs-code change report [--snapshot DIR] [--out DIR] [--payload-key KEY] [--top N]

    The snapshot is `--snapshot`, else the latest in this project's own
    store, refused when stale (the shell's `--allow-stale` overrides,
    `--fresh` measures the on-disk tree instead). Without `--out` the JSON rendering
    prints; with it, `DIR/report.json` + `DIR/report.html` are written under
    `--apply` — a dry run names the two files it would write.

WHAT THE DASHBOARD SHOWS (and nothing more)
    header     snapshot id, commit, mode, recorded totals
    units      per unit: modules, symbol entries, measured loc
    largest    top modules by loc — where reading starts
    payload    split by a payload key (default `dist`) when the snapshot
               carries one; "not measured" is PRINTED, never silently omitted
    unparseable  entries whose sha says the AST could not read them

WHAT IT CANNOT SEE
    * Everything the snapshot cannot see: dynamic symbols, files outside the
      declared roots, excluded paths. A green dashboard is "clean among the
      recorded", never "the tree is clean".
    * `loc` is per MODULE as the snapshot recorded it; it is not effort and
      not complexity — the ranking says where reading starts, not what is
      wrong.
    * No edges: imports/callers live in the query engine; this report is a
      size-and-shape view only.
    * The HTML is a static page with no script; it validates nothing at
      render time beyond what `collect()` checked. Numbers that disagree with
      the tree were wrong going in.

EXIT
    0 rendered · 2 cannot run honestly (missing/unreadable/stale snapshot)
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path
from typing import Any

from codegraph._core.query.engine import walk_symbols
from codegraph._core.verdict import Verdict
from hs_family.cli import Command


def _esc(t: object) -> str:
    return html.escape(str(t), quote=True)


def collect(idx: dict, *, payload_key: str = "dist", top: int = 15) -> dict:
    """Build the ONE data dict both renderings read. Pure: no I/O beyond idx."""
    units: dict[str, dict] = {}
    unparseable = 0
    for unit, m, s, _parent in walk_symbols(idx):
        row = units.setdefault(unit, {"unit": unit, "modules": set(),
                                      "entries": 0, "loc": 0})
        row["modules"].add(m["module"])
        row["entries"] += 1
        row["loc"] += int(m.get("loc") or 0)
        unparseable += (s.get("sha") == "unparseable")
    for row in units.values():
        row["modules"] = len(row["modules"])

    mods = sorted(
        ({"module": m["module"], "path": m.get("path"), "loc": m.get("loc"),
          "entries": len(m.get("symbols", []))}
         for ms in idx.get("units", {}).values() for m in ms),
        key=lambda r: (-(r["loc"] or 0), r["module"]))

    payload: dict[str, int] = {}
    carries = False
    for ms in idx.get("units", {}).values():
        for m in ms:
            v = (m.get("payload") or {}).get(payload_key)
            if v is not None:
                carries = True
                payload[str(v)] = payload.get(str(v), 0) + 1

    return {
        "snapshot": idx.get("snapshot"), "commit": idx.get("commit"),
        "mode": idx.get("mode"),
        "totals": idx.get("totals", {}),
        "counted": {
            "units": len(units),
            "entries": sum(r["entries"] for r in units.values()),
            "unparseable": unparseable,
        },
        "units": [units[u] for u in sorted(units)],
        "largest": mods[:top],
        "payload": {"key": payload_key, "measured": carries,
                    "split": dict(sorted(payload.items()))},
    }


def render_json(data: dict) -> str:
    """The machine rendering: the data dict, byte-stable (sorted keys)."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)


def _table(head: "list[str]", rows: "list[list[object]]") -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in head)
    trs = "".join(
        "<tr>" + "".join(f'<td class="n" style="text-align:right">{_esc(c)}</td>'
                         if isinstance(c, (int, float)) else f"<td>{_esc(c)}</td>"
                         for c in r) + "</tr>"
        for r in rows)
    return f'<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'


def render_html(data: dict) -> str:
    """The human rendering. Reads ONLY `data` — recomputing a number here is
    the bug this module exists to prevent."""
    t, c = data["totals"], data["counted"]
    kpis = [("units", t.get("units", c["units"])),
            ("modules", t.get("modules", "?")),
            ("entries", t.get("entries", c["entries"])),
            ("unparseable", c["unparseable"]),
            ("snapshot", data["snapshot"] or "—")]
    tiles = "".join(f'<div class="kpi"><div class="n">{_esc(v)}</div>'
                    f'<div class="l">{_esc(k)}</div></div>' for k, v in kpis)
    units_rows: "list[list[object]]" = [[r["unit"], r["modules"], r["entries"], r["loc"]]
                                        for r in data["units"]]
    large_rows: "list[list[object]]" = [[r["module"], r["path"] or "—", r["loc"], r["entries"]]
                                        for r in data["largest"]]
    if data["payload"]["measured"]:
        payload_html = _table(
            [data["payload"]["key"], "entries"],
            [[k, v] for k, v in data["payload"]["split"].items()])
    else:
        payload_html = (
            f'<p class="note">payload <code>{_esc(data["payload"]["key"])}</code> '
            f'not measured in this snapshot — an empty split here is NOT '
            f'"nobody belongs".</p>')
    css = ("body{font:15px/1.55 system-ui,sans-serif;margin:0;color:#1c2733;"
           "background:#fff}.wrap{max-width:1100px;margin:0 auto;padding:40px 24px}"
           "h1{font-size:30px;margin:0 0 4px}.sub{color:#5b6b7a;margin:0 0 24px}"
           "h2{font-size:18px;margin:32px 0 8px;border-bottom:2px solid #2b6cb0;"
           "padding-bottom:6px}.kpis{display:grid;gap:12px;margin:20px 0;"
           "grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}"
           ".kpi{border:1px solid #d9e2ec;border-top:3px solid #2b6cb0;"
           "border-radius:4px;padding:12px 14px}.kpi .n{font-size:26px;"
           "font-weight:650}.kpi .l{font-size:11px;color:#5b6b7a;"
           "text-transform:uppercase;letter-spacing:.08em}"
           "table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}"
           "th,td{text-align:left;padding:6px 9px;border-bottom:1px solid #d9e2ec}"
           "th{background:#f4f7f9;font-size:11.5px;text-transform:uppercase;"
           "color:#5b6b7a}code{background:#f1f4f7;padding:1px 5px;border-radius:3px}"
           ".note{font-size:12.5px;color:#5b6b7a}")
    return (f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>change report — {_esc(data["snapshot"] or "")}</title>
<style>{css}</style></head><body><div class="wrap">
<h1>change report</h1>
<p class="sub">snapshot <code>{_esc(data["snapshot"])}</code>
· commit <code>{_esc(data["commit"])}</code>
· mode <code>{_esc(data["mode"])}</code> — every number below is measured,
none is recomputed for this page.</p>
<div class="kpis">{tiles}</div>
<h2>Units</h2>
{_table(["unit", "modules", "entries", "loc"], units_rows)}
<h2>Largest modules</h2>
{_table(["module", "path", "loc", "entries"], large_rows)}
<h2>Payload split</h2>
{payload_html}
</div></body></html>
""")


def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--snapshot", metavar="DIR", default=None,
                    help="snapshot directory (default: the latest in this project's store)")
    sp.add_argument("--out", metavar="DIR", default=None,
                    help="write DIR/report.json + DIR/report.html under --apply "
                         "(default: print JSON)")
    sp.add_argument("--payload-key", metavar="KEY", default="dist",
                    help="payload key for the split table (default: dist)")
    sp.add_argument("--top", type=int, default=15,
                    help="largest-modules depth (default 15)")


def run(args: argparse.Namespace, repo: Path, opts: Any) -> Verdict:
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph_graph_freshness import SnapshotResolveError, resolve_idx

    try:
        descriptor = load(repo / "codemap.yaml", repo).descriptor
        _, _snap_dir, idx, scratch = resolve_idx(repo, descriptor, opts, None, None, "report",
                                                 fresh=bool(getattr(opts, "fresh", False)),
                                                 snap_dir=args.snapshot)
    except SnapshotResolveError as e:
        return Verdict.broken(str(e))
    except DescriptorError as e:
        return Verdict.broken(f"report: {e}")
    if scratch is not None:
        shutil.rmtree(scratch, ignore_errors=True)
    data = collect(idx, payload_key=args.payload_key, top=args.top)
    if not args.out:
        print(render_json(data))
        return Verdict(0, data)

    as_json = bool(getattr(opts, "json", False))
    apply = bool(getattr(opts, "apply", False))
    out = Path(args.out)
    files = [out / "report.json", out / "report.html"]
    if apply:
        try:
            out.mkdir(parents=True, exist_ok=True)
            files[0].write_text(render_json(data) + "\n", encoding="utf-8")
            files[1].write_text(render_html(data), encoding="utf-8")
        except OSError as e:
            return Verdict.broken(f"report: cannot write {out}: {e}")
    payload = {"report": data, "files": [str(f) for f in files], "dry_run": not apply}
    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    elif apply:
        print(f"wrote {files[0]} and {files[1]} (one data dict, two renderings)")
    else:
        print(f"[dry-run: nothing written — --apply writes {files[0]} and {files[1]}]")
    return Verdict(0, payload)


def _report(args: argparse.Namespace) -> int:
    """`report [--snapshot DIR] [--out DIR] [--payload-key KEY] [--top N]` —
    the hs-code verb. `--out` writes only under `--apply`."""
    return run(args, args.ctx.root, args.opts).exit


REPORT = Command(add_args=add_arguments, run=_report)
