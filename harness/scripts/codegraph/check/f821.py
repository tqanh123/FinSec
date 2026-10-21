"""hs-code check f821 — undefined names (ruff F821) per scope, against a two-way baseline.

A dropped import, def or decorator parses and imports fine and dies at runtime, as
`NameError`. ruff sees it statically; this door compares ruff's findings with a
committed baseline in BOTH directions:

    new     a (scope, file, name) the baseline never saw, or one more occurrence: red
    stale   a baseline entry ruff no longer reports: the list shrank and the file must
            shrink with it (--write-baseline), red until it does

Scopes: every package of a `workspace` root (named by the package), and every other
declared root (named `@<root id>`) — read from the layout, no snapshot taken. A file
belongs to its most specific scope: a root inside another is not scanned twice.

    hs-code check f821 --baseline FILE                    # check
    hs-code check f821 --baseline FILE --freeze           # first write; refuses over a file
    hs-code check f821 --baseline FILE --write-baseline   # shrink only
    hs-code check f821 --baseline FILE --add-scope NAME   # a scope new to the gate, once
    hs-code [--apply] check f821 --resolve [--source ID] [--batch B] [--pins FILE]
                                                          # propose / write the fixes
                                                          # (`codegraph.check.f821_resolve`)

WHAT IT CANNOT SEE
    * Only what ruff's F821 resolves: a name built by `getattr`/`globals()` is invisible,
      and so is one a star import provides (ruff says F405, not F821).
    * `# noqa: F821` is honoured by ruff: a suppression is invisible.
    * A name imported from a module that lost it is an ImportError, not F821 — smoke's job.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from codegraph.check import baseline
from codegraph.check.tree import CheckError, workspace
from hs_family.cli import Command

_HARNESS_REPO = Path(__file__).resolve().parents[4]

_HEAD = ("# f821 baseline — a WORK LIST, only ever shrinking. Keys are `path-in-scope::name`,\n"
         "# values the occurrence count. --write-baseline refuses to add; --freeze writes the first.\n")


def find_ruff(repo: Path, explicit: Optional[str]) -> str:
    for cand in (explicit, str(repo / ".venv" / "bin" / "ruff") if (repo / ".venv" / "bin" / "ruff").is_file()
                 else None, shutil.which("ruff")):
        if cand:
            return cand
    raise CheckError("ruff not found: pass --ruff, or install it in the target's .venv or on PATH")


def scopes(repo: Path) -> Dict[str, Path]:
    descriptor, packages = workspace(repo)
    out = {p.name: p.dir.resolve() for p in packages}
    for r in descriptor.roots:
        if r.kind != "workspace" and r.dir(repo).is_dir():
            out["@" + r.id] = r.dir(repo).resolve()
    return out


def _inner(d: Path, all_dirs) -> List[Path]:
    return [x for x in all_dirs if x != d and d in x.parents]


def ruff_rows(ruff: str, repo: Path, d: Path, skip=()) -> List[dict]:
    """ruff's F821 rows under `d`, minus files under `skip` (scopes nested in `d`)."""
    try:
        r = subprocess.run([ruff, "check", "--select", "F821", "--output-format", "json", "--no-cache",
                            "--isolated", str(d)], cwd=str(repo), capture_output=True, text=True)
    except OSError as e:
        raise CheckError("cannot execute ruff `%s`: %s" % (ruff, e))
    if r.returncode not in (0, 1):
        raise CheckError("ruff failed on %s: %s" % (d, (r.stderr or r.stdout).strip()[:200]))
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError as e:
        raise CheckError("ruff output for %s is not JSON: %s" % (d, e))
    return [r for r in rows if not any(x in Path(r["filename"]).resolve().parents for x in skip)]


def _name(row: dict) -> str:
    name = (row.get("message") or "").split("`")[1::2]
    return name[0] if name else "?"


def scan_scope(ruff: str, repo: Path, d: Path, skip=()) -> Counter:
    """F821 findings under `d` as `path-in-scope::name` counts."""
    out: Counter = Counter()
    for row in ruff_rows(ruff, repo, d, skip):
        rel = Path(row["filename"]).resolve().relative_to(d).as_posix()
        out["%s::%s" % (rel, _name(row))] += 1
    return out


def findings(ruff: str, repo: Path) -> List[Tuple[Path, str, int, int]]:
    """Every F821 finding over the gate's scopes: (file, name, row, 0-based column)."""
    sc = scopes(repo)
    out = []
    for d in sc.values():
        for r in ruff_rows(ruff, repo, d, _inner(d, sc.values())):
            out.append((Path(r["filename"]).resolve(), _name(r), r["location"]["row"],
                        r["location"]["column"] - 1))
    return sorted(set(out))


def load_baseline(p: Path) -> Dict[str, Counter]:
    import yaml_io
    doc = yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
    return {str(s): Counter({str(k): int(v) for k, v in (m or {}).items()})
            for s, m in (doc.get("dists") or {}).items()}


def render(found: Dict[str, Counter]) -> str:
    import yaml_io
    body = {"dists": {s: dict(sorted(c.items())) for s, c in sorted(found.items()) if c}}
    return _HEAD + yaml_io.safe_dump(body, sort_keys=False)


def _keys(scoped: Dict[str, Counter]) -> Counter:
    """f821's `{scope: {path::name: n}}` as engine keys: the name is the code, no message."""
    out: Counter = Counter()
    for s, c in scoped.items():
        for key, n in c.items():
            path, _, name = key.rpartition("::")
            out[baseline.Key("f821", path, name, "", s)] += n
    return out


def compare(found: Dict[str, Counter], base: Dict[str, Counter]) -> Dict[str, List[dict]]:
    diff = baseline.compare(_keys(found), _keys(base))
    return {side: [{"scope": r["key"].scope, "file": r["key"].file, "name": r["key"].code,
                    "found": r["found"], "baseline": r["baseline"]}
                   for r in sorted(rows, key=lambda r: (r["key"].scope, "%s::%s" % (r["key"].file, r["key"].code)))]
            for side, rows in diff.items()}


def shrink(found: Dict[str, Counter], base: Dict[str, Counter]) -> Dict[str, Counter]:
    out: Dict[str, Counter] = {}
    for k, n in baseline.shrink(_keys(found), _keys(base)).items():
        out.setdefault(k.scope, Counter())["%s::%s" % (k.file, k.code)] = n
    return out


def _f821_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--baseline", metavar="FILE", help="the two-way baseline (every mode but --resolve)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--freeze", action="store_true", help="write the FIRST baseline")
    mode.add_argument("--write-baseline", action="store_true", help="shrink the baseline to what remains")
    mode.add_argument("--add-scope", metavar="NAME", help="enter a scope new to the baseline, once")
    mode.add_argument("--resolve", action="store_true",
                      help="propose the statement each undefined name needs (dry-run; --apply writes)")
    ap.add_argument("--ruff", help="ruff binary (default: the target's .venv, then PATH)")
    ap.add_argument("--source", metavar="ID", default=None,
                    help="with --resolve in a port: the `.port/port.yaml` source it was cut from")
    ap.add_argument("--batch", metavar="B", default=None,
                    help="with --resolve in a port: the batch whose specs map old -> new modules")
    ap.add_argument("--copy-max", type=int, default=40, metavar="N",
                    help="with --resolve: copy an upstream def verbatim only up to N lines")
    ap.add_argument("--pins", metavar="FILE", default=None,
                    help="with --resolve: per-case rulings (JSON) — stmt | tc | copy_from | blocked")
    ap.add_argument("--proposals", metavar="FILE", default=None,
                    help="with --resolve: write the full machine-readable proposals here")


def _port_ctx(repo: Path, args: argparse.Namespace):
    """`rebind.Ctx` of the port `repo` is, or None outside a port."""
    import hs_family
    if not (repo / ".port" / "port.yaml").is_file():
        if args.source or args.batch:
            raise CheckError("--source/--batch name a port source, and %s has no .port/port.yaml" % repo)
        return None
    from codegraph.refactor._port import port_context
    from codegraph.refactor.rebind import Ctx as PortCtx
    try:
        return PortCtx(port_context(repo, "check f821 --resolve", args.source, args.batch))
    except hs_family.UsageError as e:
        raise CheckError(str(e)) from e


def _resolve(repo: Path, ruff: str, args: argparse.Namespace, opts) -> int:
    from codegraph.check import f821_resolve as fr
    from codegraph.check.tree import workspace
    descriptor, packages = workspace(repo)
    known = fr.known_names(repo, _HARNESS_REPO)
    pins = {}
    if args.pins:
        try:
            pins = json.loads(Path(args.pins).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CheckError("--pins %s: %s" % (args.pins, e)) from e
    out = fr.run_resolve(               # a fresh context each round: the last one's writes moved names
        lambda: findings(ruff, repo),
        lambda: fr.Ctx(repo, descriptor, packages, port=_port_ctx(repo, args),
                       copy_max=args.copy_max, pins=pins, known=known),
        bool(getattr(opts, "apply", False)))
    if args.proposals:
        Path(args.proposals).parent.mkdir(parents=True, exist_ok=True)
        Path(args.proposals).write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    how = Counter(r["how"] for r in out["resolved"])
    done = not out["orphan"] and (not out["resolved"] or not out["dry_run"])
    if getattr(opts, "json", False):
        print(json.dumps({**out, "ruff": ruff, "ok": done}, sort_keys=True, ensure_ascii=False))
        return 0 if done else 1
    print("f821 --resolve: %d finding(s) · %d resolved %s · %d orphan · %s"
          % (out["findings"], len(out["resolved"]), dict(sorted(how.items())), len(out["orphan"]),
             "written to %d file(s)" % out["applied_files"] if not out["dry_run"] else "dry-run"))
    for r in out["resolved"] if out["dry_run"] else ():
        print("  %s `%s` -> %s (%s)" % (r["file"], r["name"], r.get("stmt") or "copy", r["how"]))
    for o in out["orphan"]:
        print("  ORPHAN %s `%s` x%d: %s %s" % (o["file"], o["name"], o["uses"], o["unresolved"],
                                               o["candidates"][:3]))
    return 0 if done else 1


def _f821(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    repo = Path(root).resolve()
    try:
        ruff = find_ruff(repo, args.ruff)
        if args.resolve:
            return _resolve(repo, ruff, args, opts)
        if not args.baseline:
            raise CheckError("--baseline FILE is required (or --resolve)")
        bl = Path(args.baseline)
        sc = scopes(repo)
        found = {s: scan_scope(ruff, repo, d, _inner(d, sc.values())) for s, d in sc.items()}
        if not found:
            raise CheckError("no scope to scan: codemap.yaml declares no root on disk")
        if args.freeze:
            if bl.exists():
                raise CheckError("%s exists — --freeze only writes the FIRST baseline" % bl)
            bl.parent.mkdir(parents=True, exist_ok=True)
            bl.write_text(render(found), encoding="utf-8")
            print("f821: baseline frozen -> %s (%d finding(s))" % (bl, sum(sum(c.values()) for c in found.values())))
            return 0
        if not bl.is_file():
            raise CheckError("baseline %s not found (first time: --freeze)" % bl)
        base = load_baseline(bl)
        if args.add_scope:
            if args.add_scope not in found:
                raise CheckError("scope %s is not scanned (scopes: %s)" % (args.add_scope, ", ".join(sorted(found))))
            if args.add_scope in base:
                raise CheckError("scope %s is already in %s — it can only shrink" % (args.add_scope, bl))
            bl.write_text(render({**base, args.add_scope: found[args.add_scope]}), encoding="utf-8")
            print("f821: scope %s added to %s" % (args.add_scope, bl))
            return 0
        if args.write_baseline:
            bl.write_text(render(shrink(found, base)), encoding="utf-8")
            print("f821: baseline shrunk -> %s" % bl)
            return 0
    except CheckError as e:
        print("hs-code check f821: %s" % e)
        return 2
    diff = compare(found, base)
    ok = not diff["new"] and not diff["stale"]
    if getattr(opts, "json", False):
        print(json.dumps({"schema": "hs-code.check.f821/1", "scopes": len(found), "ruff": ruff,
                          "findings": sum(sum(c.values()) for c in found.values()), **diff, "ok": ok},
                         sort_keys=True))
        return 0 if ok else 1
    print("f821: %d scope(s), %d finding(s) [%s]" % (len(found), sum(sum(c.values()) for c in found.values()), ruff))
    for x in diff["new"]:
        print("  RED new   %s: %s `%s` (%d -> %d)" % (x["scope"], x["file"], x["name"], x["baseline"], x["found"]))
    for x in diff["stale"]:
        print("  RED stale %s: %s `%s` baseline %d, found %d — shrink it (--write-baseline)"
              % (x["scope"], x["file"], x["name"], x["baseline"], x["found"]))
    print("f821:", "OK" if ok else "RED")
    return 0 if ok else 1


F821_VERB = Command(add_args=_f821_args, run=_f821)
