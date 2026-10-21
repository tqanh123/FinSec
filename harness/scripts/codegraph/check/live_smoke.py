"""hs-code check live-smoke — start the REAL app lifespan and probe routes
(PORT ob-tools@60075cb:packages/portkit/src/portkit/gates/live_smoke.py).

`hs-code check smoke` proves every module IMPORTS. It cannot prove the app STARTS: a
lifespan wires the runtime (auth guard, identity, readiness, ...), and a change that
loses a name inside `lifespan()` — or a route whose annotation no longer resolves —
imports fine and dies at boot or at the first schema build. This door runs in the
target's own interpreter, on the target's own `import_roots` (`codegraph.check.tree`,
never a hard-coded layout — same discipline as `route-parity`), then

    import <module>; app = <attr>
    with TestClient(app):          # runs startup AND shutdown of the lifespan
        GET each --probe           # default /openapi.json: builds the schema of every
                                   # route, so one broken route fails here

    hs-code check live-smoke --app pkg.main:app [--python PY] [--env K=V]... \\
        [--probe /path]... [--timeout S]

The child process prints one marker line with a JSON result; this side parses it.

EXIT
    0  lifespan started and every probe answered under 400
    1  the lifespan raised, an import failed, or a probe answered >= 400
    2  cannot run honestly: no interpreter, no starlette in the target interpreter, the
       child died without a result, or a timeout

WHAT IT CANNOT SEE
    * ENV-dependent branches: a fail-closed lifespan refuses to boot without its auth
      env; pass `--env` (a dev-mode flag), and whatever that flag skips is skipped here
      too. This door does not know which env a real deployment sets.
    * External services: a DB / LLM / queue that is unreachable makes a best-effort
      lifespan branch DEGRADE quietly (that is what best-effort means) — green here says
      the boot path ran, not that the dependencies work. Degraded log lines land in
      `stderr_tail`, never judged.
    * Background tasks the lifespan spawns and cancels on exit (retry loops): a fault
      that happens only AFTER startup, inside them, is invisible.
    * Routes not probed: only `--probe` paths are requested; `/openapi.json` builds
      every route's schema but runs no handler.
    * A lifespan that blocks forever is a TIMEOUT (exit 2), not a red: the cause cannot
      be told from a slow machine.
    * Starlette's `TestClient` is what runs the probe: an ASGI app that is not
      Starlette-based is out of scope (exit 2 with the import error).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from codegraph.check._child import interpreter, run_marked_child
from codegraph.check.tree import CheckError, Tree, workspace
from hs_family.cli import Command

MARK = "PORTKIT_LIVE_SMOKE "

# Kept a STRING (never imported and called) so it runs under the TARGET's own
# interpreter and its own fastapi/starlette — see `route_parity.py`'s own note on why.
_CHILD = (r'''
import importlib, json, sys, traceback

def main():
    spec, probes = sys.argv[1], json.loads(sys.argv[2])
    res = {"stage": "import", "lifespan": None, "probes": [], "error": None, "no_starlette": False}
    def done():
        print("@MARK@" + json.dumps(res)); sys.stdout.flush()
    try:
        mod, _, attr = spec.partition(":")
        app = getattr(importlib.import_module(mod), attr or "app")
        res["stage"] = "import-starlette"
        try:
            from starlette.testclient import TestClient
        except ImportError as e:
            res["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
            res["no_starlette"] = True
            return done()
        res["stage"] = "lifespan"
        with TestClient(app) as c:
            res["lifespan"] = "started"
            res["stage"] = "probe"
            for p in probes:
                r = c.get(p)
                res["probes"].append({"path": p, "status": r.status_code})
        res["stage"] = "shutdown-ok"
    except BaseException as e:
        res["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        res["trace_tail"] = traceback.format_exc().splitlines()[-6:]
    done()

main()
''').replace("@MARK@", MARK)


def judge(child: Optional[Dict[str, Any]], probes: List[str]) -> "tuple[int, list[str]]":
    """`(exit, reasons)` from the child's own result — pure, so each cause is testable
    with no subprocess. `no_starlette` is its own CANNOT-RUN class (exit 2): the target
    interpreter itself is missing the one library this door depends on, which is not a
    finding about the app the way a raised lifespan or a lost route is — generalized
    past the upstream tool this was ported from, which folded a missing `starlette` into
    the same generic "the child errored" bucket as a broken app (exit 1); measured here
    (this tool's own interpreter has no `fastapi` and, on some hosts, no `starlette`
    either) that the two need to read differently, or a broken TARGET ENVIRONMENT reads
    exactly like a broken APP."""
    if child is None:
        return 2, ["child produced no result line"]
    if child.get("no_starlette"):
        return 2, ["%s: %s" % (child["stage"], child["error"])]
    why: List[str] = []
    if child.get("error"):
        why.append("%s: %s" % (child["stage"], child["error"]))
    elif child.get("lifespan") != "started":
        why.append("lifespan never started")
    for p in child.get("probes", []):
        if p["status"] >= 400:
            why.append("probe %s -> %d" % (p["path"], p["status"]))
    if not child.get("error") and len(child.get("probes", [])) < len(probes):
        why.append("not every probe ran")
    return (1 if why else 0), why


def live_smoke(repo: Path, app: str, python: str, probes: List[str],
               env_extra: Dict[str, str], timeout: float) -> Dict[str, Any]:
    try:
        descriptor, _ = workspace(repo)
        paths = Tree(repo=repo, descriptor=descriptor, idx={}, packages=()).import_paths()
    except CheckError as e:
        return {"exit": 2, "why": [str(e)], "child": None, "stderr_tail": []}
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(env_extra)
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in paths)
    try:
        payload, proc = run_marked_child(
            [python, "-c", _CHILD, app, json.dumps(probes)], cwd=repo, env=env,
            timeout=timeout, mark=MARK)
    except CheckError as e:
        return {"exit": 2, "why": [str(e)], "child": None, "stderr_tail": []}
    child = json.loads(payload) if payload is not None else None
    code, why = judge(child, probes)
    return {"exit": code, "why": why, "child": child, "stderr_tail": proc.stderr.splitlines()[-8:]}


def _live_smoke_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--app", required=True, help="module:attr of the ASGI app")
    ap.add_argument("--python", help="target interpreter (default: this tool's own)")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="env for the child (e.g. the dev-mode flag a fail-closed "
                         "lifespan needs); repeatable")
    ap.add_argument("--probe", action="append", default=[], metavar="PATH",
                    help="GET this path inside the live lifespan (default: "
                         "/openapi.json); repeatable")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds for the whole child (default: %(default)s)")


def _live_smoke(args: argparse.Namespace) -> int:
    ctx, opts = args.ctx, args.opts
    env_extra = {}
    for kv in args.env or []:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            print("hs-code check live-smoke: --env expects K=V, got %r" % kv)
            return 2
        env_extra[k] = v
    probes = args.probe or ["/openapi.json"]
    res = live_smoke(Path(ctx.root).resolve(), args.app, interpreter(args.python), probes,
                     env_extra, args.timeout)
    res.update({"schema": "hs-code.check.live-smoke/1", "app": args.app, "probes": probes})
    if getattr(opts, "json", False):
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
        return res["exit"]
    print("live-smoke: %s probes=%s" % (args.app, probes))
    for w in res["why"]:
        print("  %s %s" % ("RED" if res["exit"] == 1 else "CANNOT RUN", w))
    if res["exit"] == 1 and res["child"]:
        for ln in res["child"].get("trace_tail", []):
            print("    %s" % ln)
    print("live-smoke:", {0: "OK", 1: "RED", 2: "CANNOT RUN"}[res["exit"]])
    return res["exit"]


LIVE_SMOKE = Command(add_args=_live_smoke_args, run=_live_smoke)
