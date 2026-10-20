"""hs-code refactor repin-hash -- recompute sha256 byte-fence pins from the real
bytes.

Port of ob-tools' `portkit.repin_hash` (pinned commit `60075cb`). A byte fence
is a test-side contract: a table pinning the sha256 of module files, so an edit
to a pinned module fails the suite until the pin is deliberately re-set. This
verb is the mechanical half of that re-set:

    scan   every module-level `<NAME>_SHA256 = {path: <64hex>}` dict literal
           in the tree's python files (AST, so prose that looks like a table
           never counts)
    check  sha256 of each pinned file's real bytes vs the pin
    dry-run (default) list old -> new per entry; --apply rewrites ONLY the hex
           string of changed entries (trailing comments on the line survive)

Upstream scanned `packages/` and `tests/` by default; here the default is the
whole repo (a flat tree has no `packages/`), `--scan-root DIR` (repeatable)
narrows it.

Exit: 0 every pin matches the bytes (or was re-pinned by --apply, nothing
unresolvable left) · 1 pins differ in dry-run, or an entry could not be
resolved (pinned file missing, table not a dict, value not 64-hex) -- every
skipped/unresolved entry is in the payload AND makes exit != 0 · 2 cannot run
honestly (no pin table under the scanned roots: nothing to check is not green).

WHAT IT CANNOT SEE
    * Only DICT-LITERAL tables with STRING keys and 64-hex STRING values are
      pins. A table built by `dict(...)`, a loop, or a merge of other tables
      is reported `unparseable-table`, never guessed.
    * A pin whose key resolves to no file (nearest `pyproject.toml` dir, the
      pin file's own dir, then the repo root) is `missing` -- RED, never
      skipped: a fence pointing at nothing stopped guarding.
    * Hashes pinned by CONTENT other than a whole file are invisible.
    * Whether the re-pin is LEGITIMATE is the owner's review, not this verb's.
    * Two entries pinning the same file with different hex both appear.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import hs_family
from codegraph._core import walk

from .errors import RefactorError
from hs_family.cli import Command

#: a pin table's name: UPPER_SNAKE ending in _SHA256.
_TABLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*_SHA256$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Pin:
    file: Path          # the file holding the table
    table: str          # table name
    key: str            # pinned path, as written
    value: str          # pinned hex, as written
    lineno: int         # line of the value inside the file


def _iter_py(repo: Path, roots: List[str]) -> Iterator[Path]:
    seen: set = set()
    repo = repo.resolve()
    for r in roots:
        p = (repo / r).resolve()
        if p != repo and repo not in p.parents:
            raise RefactorError(f"repin-hash: --scan-root {r} is outside the repo {repo}")
        cands = [p] if p.is_file() else sorted(p.rglob("*.py")) if p.is_dir() else []
        for f in cands:
            if not f.is_file() or f in seen or f.is_symlink():
                continue
            if walk.skipped(f.relative_to(repo).parts):
                continue
            seen.add(f)
            yield f


def find_tables(repo: Path, roots: List[str]) -> Tuple[List[Pin], List[Dict[str, str]]]:
    """Every pin entry + every unparseable table."""
    pins: List[Pin] = []
    bad: List[Dict[str, str]] = []
    for f in _iter_py(repo, roots):
        rel = f.relative_to(repo).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            bad.append({"file": rel, "table": "", "kind": "unparseable-file", "why": str(e)})
            continue
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            table = next((t.id for t in targets
                          if isinstance(t, ast.Name) and _TABLE_NAME.fullmatch(t.id)), None)
            if table is None:
                continue
            value = node.value
            if not isinstance(value, ast.Dict):
                bad.append({"file": rel, "table": table, "kind": "unparseable-table",
                            "why": f"value is {type(value).__name__}, not a dict literal"})
                continue
            for k, v in zip(value.keys, value.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    bad.append({"file": rel, "table": table, "kind": "bad-key",
                                "why": "non-string key"})
                    continue
                if not (isinstance(v, ast.Constant) and isinstance(v.value, str)
                        and _HEX64.fullmatch(v.value)):
                    bad.append({"file": rel, "table": table, "kind": "bad-value",
                                "why": f"key {k.value!r}: value is not a 64-hex string"})
                    continue
                pins.append(Pin(file=f, table=table, key=k.value, value=v.value, lineno=v.lineno))
    return pins, bad


def resolve_target(pin_file: Path, key: str, repo: Path) -> Optional[Path]:
    """The pinned file, against the bases a fence actually uses, in order: the
    nearest directory above the pin file carrying a `pyproject.toml` (a fence in
    `<pkg>/tests/` spelling a sibling `../other/src/...`), the pin file's own
    dir, then the repo root. None = the pin points at nothing."""
    bases = [pin_file.parent, repo]
    for up in pin_file.resolve().parents:
        if (up / "pyproject.toml").is_file():
            bases.insert(0, up)
            break
    for base in bases:
        cand = (base / key).resolve()
        if cand.is_file():
            return cand
    return None


_UNRESOLVED = ("missing", "bad-key", "bad-value", "unparseable-table", "unparseable-file",
               "apply-failed")


def repin(repo: Path, roots: List[str], apply: bool) -> Dict[str, Any]:
    pins, bad = find_tables(repo, roots)
    if not pins and not bad:
        raise RefactorError(f"repin-hash: no `*_SHA256` pin table under {repo} ({', '.join(roots)})"
                            " -- nothing to check is not green, it is nothing")
    rows: List[Dict[str, Any]] = []
    by_file: Dict[Path, List[Dict[str, Any]]] = {}
    for p in pins:
        target = resolve_target(p.file, p.key, repo)
        row: Dict[str, Any] = {"file": p.file.relative_to(repo).as_posix(), "table": p.table,
                               "key": p.key, "lineno": p.lineno, "old": p.value}
        if target is None:
            row.update(new=None, kind="missing")
        else:
            new = hashlib.sha256(target.read_bytes()).hexdigest()
            row.update(new=new, kind="match" if new == p.value else "changed")
        rows.append(row)
        if row["kind"] == "changed":
            by_file.setdefault(p.file, []).append(row)
    changed = sum(len(v) for v in by_file.values())
    rows.extend({"file": b["file"], "table": b["table"], "key": "", "lineno": 0, "old": None,
                 "new": None, "kind": b["kind"], "why": b["why"]} for b in bad)

    applied = 0
    if apply:
        for f, entries in by_file.items():
            lines = f.read_text(encoding="utf-8").split("\n")
            for e in entries:
                # replace ONLY the hex string on the entry's line: the trailing
                # comment (re-pin provenance) survives
                old = f'"{e["old"]}"'
                ln = lines[e["lineno"] - 1]
                if ln.count(old) != 1:
                    e["kind"] = "apply-failed"
                    e["why"] = (f'line {e["lineno"]}: {e["old"][:12]}... hex string not found '
                                f'exactly once -- the line moved since the scan; re-run')
                    continue
                lines[e["lineno"] - 1] = ln.replace(old, f'"{e["new"]}"', 1)
                applied += 1
                e["kind"] = "repinned"
            f.write_text("\n".join(lines), encoding="utf-8")

    unresolved = [r for r in rows if r["kind"] in _UNRESOLVED]
    owing = bool(unresolved) or any(r["kind"] == "changed" for r in rows)
    return {"dry_run": not apply, "roots": roots,
            "tables": len({(r["file"], r["table"]) for r in rows}),
            "checked": len(pins), "changed": changed, "applied": applied,
            "unresolved": len(unresolved), "rows": rows, "ok": not owing}


def _print_report(res: Dict[str, Any]) -> None:
    for r in res["rows"]:
        if r["kind"] == "match":
            continue
        tail = f" -> {r['new']}" if r.get("new") else ""
        why = f"  ({r['why']})" if r.get("why") else ""
        print(f"  {r['kind']:18} {r['file']}:{r['lineno']} {r['table']}[{r['key']}]{tail}{why}")
    print(f"repin-hash: {res['checked']} pin(s) in {res['tables']} table(s), {res['changed']} "
          f"changed, {res['applied']} re-pinned, {res['unresolved']} unresolved"
          + ("  (dry-run; --apply writes)" if res["changed"] and res["dry_run"] else ""))


def _repin_hash_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--scan-root", action="append", default=None, metavar="DIR",
                    help="scan this repo-relative dir instead of the whole repo (repeatable)")


def _repin_hash(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        res = repin(Path(root).resolve(), args.scan_root or ["."],
                    bool(getattr(opts, "apply", False)))
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    if getattr(opts, "json", False):
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(res)
    return 0 if res["ok"] else 1


REPIN_HASH = Command(add_args=_repin_hash_args, run=_repin_hash)
