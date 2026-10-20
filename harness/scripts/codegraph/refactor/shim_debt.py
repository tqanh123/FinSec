"""hs-code refactor shim-debt -- the ledger of re-exports `move --symbol` leaves.

Port of ob-tools' `devkit.codemod.shimdebt` (pinned commit `60075cb`). A move
keeps every importer working by leaving `from <dst> import <sym>` at the old
door. That bridge is DEBT with three duties:

  BORN   `hs-code refactor move --symbol --apply` appends one line per bridge
         (`record_bridge`) to the manifest and prints a DEBT notice;
  COUNT  this verb walks the tree and counts who still crosses the OLD door
         (`from <src> import <sym>`) -- a number and a file list;
  ROT    `--strict` reds a bridge that still stands while NOBODY crosses it
         (retire it); a manifest line whose bridge no longer exists in the
         tree (stale -- the list only shrinks) is red always.

The manifest is `<repo>/.hs-code/refactor-bridges.jsonl` unless `--manifest`
names another: repo-lifetime and committed with the tree, since a bridge
outlives any one session. Each line carries `ts` and `actor` (attribution,
`hook_runtime.resolve_actor`), the dotted `src_module`/`dst_module` the bridge
spells, and the repo-relative `src_path`/`dst_path` (upstream derived the file
from the dotted name, which only holds when the repo root is the sole
sys.path entry; a line without `src_path` still falls back to that).

Exit: 0 every bridge crossed (or rotten without --strict), no stale line, or no
manifest at all · 1 a stale line, or a rotten bridge under --strict · 2 a
manifest line that is not JSON, or not an object naming `symbol`, `src_module`
and `dst_module`.

WHAT IT CANNOT SEE
    * Importers are counted as `from <src> import <sym[, alias]>` lines by AST:
      dynamic access (`importlib`, `getattr` by string) is invisible.
    * `import <src>` + attribute use (`src.sym`) is NOT counted: it goes
      through the module, not the re-exported name -- deleting the bridge
      does not break it.
    * Relative imports (`from .src import sym`) are not resolved.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import hs_family

from codegraph._core import walk

from .errors import RefactorError
from hs_family.cli import Command

MANIFEST = Path(".hs-code") / "refactor-bridges.jsonl"
_REQUIRED = ("symbol", "src_module", "dst_module")


def _actor() -> str:
    hooks = str(Path(__file__).resolve().parents[3] / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import hook_runtime
    return hook_runtime.resolve_actor()


def record_bridge(repo: Path, symbol: str, src_rel: str, dst_rel: str, src_module: str,
                  dst_module: str, manifest: Optional[Path] = None) -> Path:
    """Append one bridge line -- the debt is in a LIST now, not a memory."""
    path = manifest or (repo / MANIFEST)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor": _actor(),
            "symbol": symbol, "src_module": src_module, "dst_module": dst_module,
            "src_path": src_rel, "dst_path": dst_rel}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
    return path


def _py_files(repo: Path):
    for py in sorted(repo.rglob("*.py")):
        if not walk.skipped(py.relative_to(repo).parts):
            yield py


def count_importers(repo: Path, src_module: str, symbol: str) -> List[Tuple[Path, int]]:
    """Files that still import `symbol` through the OLD door `src_module`."""
    hits: List[Tuple[Path, int]] = []
    for py in _py_files(repo):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.level == 0
                    and node.module == src_module
                    and any(a.name == symbol for a in node.names)):
                hits.append((py, node.lineno))
    return hits


def bridge_stands(src_file: Path, symbol: str, dst_module: str) -> bool:
    """The re-export line `from <dst> import <sym>` still exists at src."""
    if not src_file.is_file():
        return False
    try:
        tree = ast.parse(src_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return any(isinstance(node, ast.ImportFrom) and node.module == dst_module
               and any(a.name == symbol for a in node.names) for node in ast.walk(tree))


def read_manifest(path: Path) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError as e:
            raise RefactorError("shim-debt: %s:%d is not JSON (%s)" % (path, n, e)) from None
        missing = [k for k in _REQUIRED if not isinstance(line, dict) or not line.get(k)]
        if missing:
            raise RefactorError("shim-debt: %s:%d is not a bridge line (missing %s)"
                                % (path, n, ", ".join(missing)))
        lines.append(line)
    return lines


def shim_debt(repo: Path, manifest: Path, strict: bool) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    red = False
    for e in read_manifest(manifest):
        sym, src, dst = e["symbol"], e["src_module"], e["dst_module"]
        src_file = repo / (e.get("src_path") or Path(*src.split(".")).with_suffix(".py"))
        row: Dict[str, Any] = {"symbol": sym, "src": src, "dst": dst, "importers": 0, "where": []}
        if not bridge_stands(src_file, sym, dst):
            row["state"] = "stale"
            red = True
        else:
            hits = count_importers(repo, src, sym)
            row["importers"] = len(hits)
            row["where"] = ["%s:%d" % (p.relative_to(repo).as_posix(), ln) for p, ln in hits]
            row["state"] = "crossed" if hits else "rotten"
            red = red or (strict and not hits)
        rows.append(row)
    return {"manifest": str(manifest), "bridges": rows, "strict": strict, "ok": not red}


def _print_report(res: Dict[str, Any]) -> None:
    print(f"shim-debt: {len(res['bridges'])} bridge(s) on record in {res['manifest']}\n")
    for r in res["bridges"]:
        if r["state"] == "stale":
            print(f"  STALE  {r['symbol']}: bridge at {r['src']} is GONE -- prune the manifest line")
            continue
        print(f"  {r['importers']:3d} importer(s)  {r['symbol']}  via {r['src']}  "
              f"[{', '.join(r['where']) or '-'}]")
        if r["state"] == "rotten":
            print(f"        ROTTEN: the bridge stands but nobody crosses it -- delete "
                  f"`from {r['dst']} import {r['symbol']}` at {r['src']} and prune this line")
    if res["ok"]:
        print("\nGREEN: every bridge is crossed or gone; no stale line.")
    else:
        print("\nRED (--strict)." if res["strict"] else "\nRED: stale manifest line(s).")


def _shim_debt_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--manifest", type=Path, default=None,
                    help="the bridge ledger (default: <repo>/%s)" % MANIFEST.as_posix())
    ap.add_argument("--strict", action="store_true",
                    help="red on a standing bridge nobody crosses (retire it)")


def _shim_debt(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    repo = Path(root).resolve()
    manifest = args.manifest or repo / MANIFEST
    as_json = bool(getattr(opts, "json", False))
    if not manifest.is_file():
        res: Dict[str, Any] = {"manifest": str(manifest), "bridges": [], "strict": args.strict,
                               "ok": True}
        if as_json:
            print(json.dumps(res, sort_keys=True, ensure_ascii=False))
        else:
            print(f"shim-debt: no manifest at {manifest} -- no bridge on record.")
        return 0
    try:
        res = shim_debt(repo, manifest, args.strict)
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    if as_json:
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(res)
    return 0 if res["ok"] else 1


SHIM_DEBT = Command(add_args=_shim_debt_args, run=_shim_debt)
