"""The initializers `hs-code init` knows, one pair of functions per registry row.

A domain declares its own row in its own fragment (`initializers:` beside
`domains:`); this module only holds the functions those rows name:

    status(root) -> {"ready": bool, "detail": str}
    run(ctx, opts) -> {"exit_code": int, ...}      # only for rows with `run:`

A row with `route:` instead of `run:` belongs to an owner with its own
interview (port → /hs:port); `init` reports it and never runs it.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import attrs  # noqa: E402

import port_workspace  # noqa: E402
from codegraph.graph.rules import INIT as GRAPH_INIT  # noqa: E402
from codegraph.graph.verbs import SNAPSHOT, SnapshotBaseUnresolved, _snapshot_base  # noqa: E402
from hs_family.cli import Outcome  # noqa: E402
from hs_run_facts.engine import _finished_snapshot_dirs, project_name  # noqa: E402


def graph_status(root: Path) -> dict:
    project = project_name(root)
    if not (root / "codemap.yaml").is_file():
        return {"ready": False, "detail": "no codemap.yaml"}
    if project is None:
        return {"ready": False, "detail": "codemap.yaml declares no project"}
    try:
        base = _snapshot_base(project, None, root)
    except SnapshotBaseUnresolved as e:
        return {"ready": False, "detail": str(e)}
    snaps = _finished_snapshot_dirs(base) if base.is_dir() else []
    if not snaps:
        return {"ready": False, "detail": "codemap.yaml present, no snapshot yet"}
    return {"ready": True, "detail": "codemap.yaml + %d snapshot(s)" % len(snaps)}


def _invoke(command, ctx, opts, argv=()) -> tuple:
    """Run another verb's own `Command` in-process with its own parser defaults;
    (exit, data-or-text) -- its `Outcome`'s data, or the one JSON document it prints."""
    ap = argparse.ArgumentParser(add_help=False)
    command.add_args(ap)
    ns = ap.parse_args(list(argv))
    ns.ctx, ns.opts = ctx, attrs.evolve(opts, json=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = command.run(ns)
    if isinstance(result, Outcome):
        return result.exit_code, dict(result.data)
    rc = int(result)
    out = buf.getvalue().strip()
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def graph_run(ctx, opts) -> dict:
    """codemap.yaml, the tools its engine runs, then the first snapshot — one step, so
    the next facts read finds a map it can use instead of a stale-snapshot stop.

    A codemap this run wrote is removed again when a later step fails (it would
    otherwise read as adopted for good); one that was there before is never touched."""
    from codegraph._core.lang.typescript.node import install
    root = ctx.root
    path = root / "codemap.yaml"
    row: dict = {}
    wrote = None
    if not path.is_file():
        rc, data = _invoke(GRAPH_INIT, ctx, opts)
        row["descriptor"] = data
        if rc != 0:
            row["exit_code"] = rc
            return row
        wrote = path.read_bytes() if path.is_file() else None
    if not opts.apply:
        row.update(exit_code=0, would=["graph init --apply", "lang-tools (if its engine needs one)",
                                       "graph snapshot --apply"])
        return row
    if not install.status(root).get("ready"):
        tools = install.run(ctx, opts)
        row["lang_tools"] = tools
        if int(tools.get("exit_code", 0)) != 0:
            row["exit_code"] = int(tools["exit_code"])
            return _unwind(row, path, wrote)
    rc, data = _invoke(SNAPSHOT, ctx, opts)
    row.update(exit_code=rc, snapshot=data)
    return _unwind(row, path, wrote) if rc != 0 else row


def _unwind(row: dict, path: Path, wrote) -> dict:
    """Remove `path` when it still holds exactly the bytes this run wrote."""
    if wrote is not None and path.is_file() and path.read_bytes() == wrote:
        path.unlink()
        row["removed"] = str(path)
    return row


def port_status(root: Path) -> dict:
    path = port_workspace.port_yaml_path(root)
    if path.is_file():
        return {"ready": True, "detail": str(path)}
    return {"ready": False, "detail": "no .port/port.yaml (only a repo being ported needs one)"}
