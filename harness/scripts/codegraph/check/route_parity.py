"""hs-code check route-parity — the target app must serve the routes an upstream app
serves (PORT ob-tools@60075cb:packages/portkit/src/portkit/route_parity.py).

A route is a promise to a client. When a change drops one, nothing else goes red: the
file parses, every import resolves, a lint pass sees no undefined name (the dropped
handler is simply not there any more) — the only reader of a route is a caller nobody
here runs. This door compares the two OpenAPI path+method sets, two-way:

    missing   the upstream serves it, the target does not     (a route LOST)
    extra     the target serves it, the upstream does not     (upstream DELETED it and
                                                               the target still keeps it)

against a committed baseline of RULED differences (each carries a reason):

    hs-code check route-parity --app pkg.main:app --upstream DIR --upstream-app pkg.main:app \\
        --baseline FILE                    # check
    ... --baseline FILE --freeze           # first write; refuses over an existing file
    ... --baseline FILE --write-baseline   # shrink only
    hs-code check route-parity --target-routes A.json --upstream-routes B.json --baseline FILE
                                           # offline: two saved [[method, path], ...] files

Both apps run in the TARGET's own interpreter (`--python`, else this tool's own — see
`codegraph.check._child.interpreter`), each on its own tree's `import_roots` (read
through `codegraph.check.tree`, never a hard-coded `packages/*/src` layout): the child
only ever calls `app.openapi()["paths"]` — nothing about how the app is built, wired, or
laid out on disk reaches this door beyond that one dict.

EXIT
    0  both directions match the baseline
    1  a NEW difference, or a STALE baseline entry (the list only ever shrinks)
    2  cannot run honestly: no interpreter, an app failed to import, a timeout, or a
       baseline entry with no reason

WHAT IT CANNOT SEE
    * Routes outside the OpenAPI schema: `include_in_schema=False`, WebSocket routes and
      mounted sub-apps do not appear in `app.openapi()`.
    * Flag/env-gated routers: a router registered only under an env flag is judged in
      whatever env the child inherits (`--env K=V` to flip it); a route that exists only
      under another env is invisible.
    * Shape: the same method+path with a lost/renamed field, a changed status code, a
      different auth dependency or a different handler is a MATCH here. Route ORDER
      (matching precedence of overlapping paths) is not compared either.
    * Path-parameter renames count as a different route (`{a}` vs `{b}`): a false red is
      cheaper than a false green.
    * The upstream app must IMPORT in the target interpreter (its own dependencies
      installed): one that cannot is exit 2, never read as an empty route set.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from codegraph.check._child import interpreter, run_marked_child
from codegraph.check.tree import CheckError, Tree, workspace
from hs_family.cli import Command

MARK = "PORTKIT_ROUTE_PARITY "
METHODS = ("get", "post", "put", "patch", "delete")
UNRULED = "UNRULED"

# Kept a STRING (never imported as a module and called) so it runs under the TARGET's
# own interpreter and its own fastapi/starlette — never this tool's (which may not even
# have either installed; see `harness/scripts/hs_code.py`'s own test-environment note).
_CHILD = (r'''
import importlib, json, sys
spec = sys.argv[1]
res = {"routes": None, "error": None}
try:
    mod, _, attr = spec.partition(":")
    app = getattr(importlib.import_module(mod), attr or "app")
    res["routes"] = sorted(
        [m, p] for p, d in app.openapi()["paths"].items() for m in d
        if m in @METHODS@)
except BaseException as e:
    res["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
print("@MARK@" + json.dumps(res)); sys.stdout.flush()
''').replace("@MARK@", MARK).replace("@METHODS@", repr(METHODS))


def collect(python: str, repo: Path, app: str, env_extra: Dict[str, str],
           timeout: float) -> Tuple[Optional[List[Tuple[str, str]]], Optional[str]]:
    """`(routes, error)` of ONE app: `repo`'s own `import_roots` (`codegraph.check.tree`,
    the descriptor only — no snapshot is taken; this door never reads `repo`'s AST) on
    the child's `PYTHONPATH`, `app` imported and its `.openapi()["paths"]` read."""
    try:
        descriptor, _ = workspace(repo)
        paths = Tree(repo=repo, descriptor=descriptor, idx={}, packages=()).import_paths()
    except CheckError as e:
        return None, str(e)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(env_extra)
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in paths)
    payload, proc = run_marked_child([python, "-c", _CHILD, app], cwd=repo, env=env,
                                     timeout=timeout, mark=MARK)
    if payload is None:
        tail = " | ".join(proc.stderr.splitlines()[-3:])
        return None, "%s: child produced no result line (%s)" % (app, tail)
    res = json.loads(payload)
    if res.get("error"):
        return None, "%s: %s" % (app, res["error"])
    return [(m, p) for m, p in res["routes"]], None


def load_routes(p: Path) -> List[Tuple[str, str]]:
    return sorted({(str(m).lower(), str(x)) for m, x in json.loads(p.read_text(encoding="utf-8"))})


def load_baseline(p: Path) -> Tuple[Dict[Tuple[str, str], str], Dict[Tuple[str, str], str]]:
    """`({missing key: reason}, {extra key: reason})`; a missing reason raises
    `CheckError` (the caller turns it into exit 2) — an unexplained difference is not a
    ruled one."""
    import yaml_io
    doc = yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
    out: List[Dict[Tuple[str, str], str]] = []
    for side in ("missing", "extra"):
        d: Dict[Tuple[str, str], str] = {}
        for e in doc.get(side) or []:
            reason = str(e.get("reason") or "").strip()
            if not reason:
                raise CheckError("%s %s %s: no reason" % (side, e.get("method"), e.get("path")))
            d[(str(e["method"]).lower(), str(e["path"]))] = reason
        out.append(d)
    return out[0], out[1]


def diff(up: List[Tuple[str, str]], target: List[Tuple[str, str]],
        bl_missing: Dict, bl_extra: Dict) -> Dict:
    """Pure judgement: every cause is testable with no subprocess."""
    us, ts = set(up), set(target)
    missing, extra = us - ts, ts - us
    return {
        "new_missing": sorted(missing - set(bl_missing)),
        "new_extra": sorted(extra - set(bl_extra)),
        "stale_missing": sorted(set(bl_missing) - missing),
        "stale_extra": sorted(set(bl_extra) - extra),
        "missing": sorted(missing), "extra": sorted(extra),
        "upstream": len(us), "target": len(ts),
    }


def render_baseline(missing: Dict, extra: Dict, header: str) -> str:
    def blk(name: str, d: Dict) -> str:
        if not d:
            return "%s: []\n" % name
        body = "".join("  - {method: %s, path: %s, reason: %s}\n"
                       % (m, json.dumps(p, ensure_ascii=False), json.dumps(r, ensure_ascii=False))
                       for (m, p), r in sorted(d.items()))
        return "%s:\n%s" % (name, body)
    return (header + "\n" + "# missing = the upstream serves it, the target does not (a route lost)\n"
           + blk("missing", missing) + "# extra = the target serves it, the upstream does not\n"
           + blk("extra", extra))


def _header(existing: Optional[Path]) -> str:
    if existing is not None and existing.is_file():
        head = []
        for ln in existing.read_text(encoding="utf-8").splitlines():
            if ln.startswith("#") and not ln.startswith(("# missing", "# extra")):
                head.append(ln)
            elif head or ln.strip():
                break
        if head:
            return "\n".join(head)
    return ("# route-parity baseline: RULED differences between the upstream and the target\n"
           "# OpenAPI path+method sets. Two-way: a new difference is red, and so is an entry\n"
           "# that no longer differs.")


def _collect_pair(args: argparse.Namespace) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """`(upstream_routes, target_routes)` — offline files when given, else two live
    child imports. Raises `CheckError` for any way this cannot run honestly, so the
    caller's success path never has to prove a route list is not `None` by hand."""
    if args.target_routes and args.upstream_routes:
        try:
            return load_routes(Path(args.upstream_routes)), load_routes(Path(args.target_routes))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            raise CheckError(str(e))
    if not args.upstream:
        raise CheckError("--upstream DIR, or --target-routes/--upstream-routes, is required")
    if not args.app:
        raise CheckError("--app module:attr (the target's ASGI app) is required")
    upstream_app = args.upstream_app or args.app
    env_extra = dict(kv.partition("=")[::2] for kv in args.env or [])
    python = interpreter(args.python)
    target, e1 = collect(python, Path(args.ctx.root).resolve(), args.app, env_extra, args.timeout)
    up, e2 = collect(python, Path(args.upstream).resolve(), upstream_app, env_extra, args.timeout)
    err = e1 or e2
    if err or up is None or target is None:
        raise CheckError(err or "no routes collected")
    return up, target


def _route_parity_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--app", help="target app module:attr (e.g. pkg.main:app)")
    ap.add_argument("--upstream", default=None, help="the upstream checkout to compare against")
    ap.add_argument("--upstream-app", default=None,
                    help="upstream app module:attr (default: same as --app)")
    ap.add_argument("--baseline", required=True, help="ruled-differences yaml (two-way)")
    ap.add_argument("--python", help="interpreter for both imports (default: this tool's own)")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="env for both children (flip a flag-gated router); repeatable")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds per import")
    ap.add_argument("--target-routes", default=None, metavar="JSON",
                    help="offline: a saved [[method, path], ...] for the target side")
    ap.add_argument("--upstream-routes", default=None, metavar="JSON",
                    help="offline: a saved [[method, path], ...] for the upstream side")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--freeze", action="store_true",
                      help="write the first baseline (refuses over an existing file)")
    mode.add_argument("--write-baseline", action="store_true",
                      help="shrink the baseline to what still differs (never grows)")


def _route_parity(args: argparse.Namespace) -> int:
    opts = args.opts
    baseline = Path(args.baseline)
    try:
        up, target = _collect_pair(args)
    except CheckError as e:
        print("route-parity: CANNOT RUN — %s" % e)
        return 2

    try:
        if args.freeze or args.write_baseline:
            old_m: Dict[Tuple[str, str], str] = {}
            old_e: Dict[Tuple[str, str], str] = {}
            if args.write_baseline:
                if not baseline.is_file():
                    raise CheckError("baseline %s not found (first time: --freeze)" % baseline)
                old_m, old_e = load_baseline(baseline)
            elif baseline.exists():
                raise CheckError("%s exists — --freeze only writes the first baseline" % baseline)
            d = diff(up, target, {}, {})
            stamp = "%s (frozen %s)" % (UNRULED, date.today().strftime("%y%m%d"))
            if args.freeze:
                m = {k: stamp for k in d["missing"]}
                x = {k: stamp for k in d["extra"]}
            else:
                m = {k: r for k, r in old_m.items() if k in set(d["missing"])}
                x = {k: r for k, r in old_e.items() if k in set(d["extra"])}
                grew = (set(d["missing"]) - set(m)) | (set(d["extra"]) - set(x))
                if grew:
                    print("route-parity: --write-baseline only shrinks; %d new difference(s) "
                         "must be fixed or ruled by hand" % len(grew))
                    return 1
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_text(render_baseline(m, x, _header(baseline if args.write_baseline else None)),
                                encoding="utf-8")
            print("route-parity: wrote %s (%d missing, %d extra)" % (baseline, len(m), len(x)))
            return 0

        if not baseline.is_file():
            print("route-parity: no baseline at %s — first run `hs-code check route-parity "
                 "... --baseline %s --freeze`" % (baseline, baseline))
            return 2
        bl_m, bl_e = load_baseline(baseline)
    except CheckError as e:
        print("route-parity: CANNOT RUN — %s" % e)
        return 2

    d = diff(up, target, bl_m, bl_e)
    red = bool(d["new_missing"] or d["new_extra"] or d["stale_missing"] or d["stale_extra"])
    d.update({"schema": "hs-code.check.route-parity/1",
             "unruled": sum(1 for r in [*bl_m.values(), *bl_e.values()] if r.startswith(UNRULED))})
    if getattr(opts, "json", False):
        print(json.dumps(d, sort_keys=True, ensure_ascii=False))
        return 1 if red else 0
    print("route-parity: upstream %d route(s) · target %d · differ: %d missing, %d extra "
         "(baseline rules %d, %d unruled)"
         % (d["upstream"], d["target"], len(d["missing"]), len(d["extra"]),
            len(bl_m) + len(bl_e), d["unruled"]))
    for key, label in (("new_missing", "NEW MISSING (upstream serves, target does not)"),
                       ("new_extra", "NEW EXTRA (target serves, upstream does not)"),
                       ("stale_missing", "STALE baseline `missing` (now served)"),
                       ("stale_extra", "STALE baseline `extra` (now gone)")):
        for m, p in d[key]:
            print("  RED %s: %s %s" % (label, m.upper(), p))
    print("route-parity:", "RED" if red else "OK")
    return 1 if red else 0


ROUTE_PARITY = Command(add_args=_route_parity_args, run=_route_parity)
