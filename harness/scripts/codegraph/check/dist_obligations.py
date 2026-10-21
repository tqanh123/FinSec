"""hs-code check dist-obligations -- what a dist owes the repo's own registries.

A new dist is not done when its own directory is: the repo keeps lists that
must name it too (a root `pyproject.toml` source and `mypy_path`, a roles
block, a ledger a script reads, a durations file). `codemap.yaml`
`obligations:` declares one row per such list (`descriptor/obligations.py`
holds the shape); this verb reads each row's file and says met or missing.

    hs-code check dist-obligations <dist> [--module DOTTED]
    hs-code check dist-obligations --all

The dist is the directory of that name under a `kind: workspace` root;
`{module}` is the import path its name spells (`scaffold new-dist`'s own
rule), or `--module`. A row whose placeholder has no value (a dist keeping
its module elsewhere, no `--module`) is `unknown`, never `met`.

Exit: 0 every row met · 1 a row missing or unknown · 2 no codemap, no
`obligations:` declared, no such dist, or neither a dist nor `--all`.

WHAT IT CANNOT SEE
    * Whether a match MEANS the dist is registered: a `contains` that also
      matches a longer sibling's name (`acme-a` inside `acme-ab`) reads as
      met. Spell the row with the delimiters the file uses around a name.
    * A registry no row declares: an empty answer is "every DECLARED row
      met", never "nothing else is owed".
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import hs_family
from codegraph._core import regex_bound
from codegraph._core.descriptor import DescriptorError, load
from codegraph._core.descriptor.obligations import ObligationError, render
from codegraph.scaffold.inferred import module_of, workspace_dists
from hs_family.cli import Command, Outcome


def _values(dist: str, dist_dir: str, module: "str | None") -> "dict[str, str]":
    out = {"dist": dist, "dist_dir": dist_dir}
    if module:
        out.update(module=module, module_path=module.replace(".", "/"))
    return out


def check_dist(repo: Path, rows: "tuple[dict, ...]", dist: str, dist_dir: str,
               module: "str | None", cache: "dict[str, str | None] | None" = None) -> "list[dict]":
    """One `{id, path, how, needle, status, note[, why]}` per row; `status` is
    `met`, `missing` or `unknown` (a placeholder with no value for this dist)."""
    cache = {} if cache is None else cache
    values = _values(dist, dist_dir, module)
    budget = regex_bound.ScanBudget()
    out: "list[dict]" = []
    for row in rows:
        res: "dict[str, Any]" = {"id": row["id"], "how": row["how"], "note": row["note"]}
        try:
            path = render(row["path"], values)
            needle = render(row["pattern"], values, escape=row["how"] == "regex")
        except ObligationError as e:
            out.append({**res, "path": row["path"], "needle": row["pattern"],
                        "status": "unknown", "why": "%s -- pass --module DOTTED" % e})
            continue
        res.update(path=path, needle=needle)
        if path not in cache:
            try:
                cache[path] = (repo / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                cache[path] = None
        text = cache[path]
        if text is None:
            out.append({**res, "status": "missing", "why": "no such file: %s" % path})
            continue
        if row["how"] == "regex":
            try:
                pat = regex_bound.compile_bounded(needle, regex_bound.regex.MULTILINE)
                hit = regex_bound.bounded_search(pat, text, budget) is not None
            except (TimeoutError, regex_bound.PatternBudgetExceeded) as e:
                out.append({**res, "status": "unknown", "why": "regex timed out: %s" % e})
                continue
        else:
            hit = needle in text
        out.append({**res, "status": "met" if hit else "missing"})
    return out


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("dist", nargs="?", default=None,
                    help="a dist directory name under a kind: workspace root")
    ap.add_argument("--all", action="store_true", help="every dist under a kind: workspace root")
    ap.add_argument("--module", default=None, metavar="DOTTED",
                    help="the dist's import path ({module}), when its name does not spell it")


def _run(args: argparse.Namespace) -> int:
    repo, opts = Path(args.ctx.root).resolve(), args.opts
    if not args.dist and not args.all:
        raise hs_family.UsageError("check dist-obligations: name a dist, or pass --all")
    if args.dist and args.all:
        raise hs_family.UsageError("check dist-obligations: a dist or --all, not both")
    try:
        descriptor = load(repo / "codemap.yaml", repo).descriptor
    except (DescriptorError, OSError) as e:
        raise hs_family.UsageError(str(e)) from e
    rows = descriptor.obligations
    dists = {d.name: d for d, _ in workspace_dists(repo, descriptor)}
    if not rows:
        return _undeclared(repo, dists)
    cache: "dict[str, str | None]" = {}

    def one(name: str, module: "str | None") -> dict:
        d = dists[name]
        mod = module or module_of(d)
        dist_dir = d.relative_to(repo).as_posix()
        return {"dist": name, "dist_dir": dist_dir, "module": mod,
                "rows": check_dist(repo, rows, name, dist_dir, mod, cache)}

    if args.dist:
        if args.dist not in dists:
            raise hs_family.UsageError(
                "check dist-obligations: no dist %r under a kind: workspace root (%d found)"
                % (args.dist, len(dists)))
        data = one(args.dist, args.module)
        owed = [r for r in data["rows"] if r["status"] != "met"]
    else:
        reports = [one(n, None) for n in sorted(dists)]
        data = {"rows_declared": len(rows),
                "dists": [{"dist": r["dist"], "dist_dir": r["dist_dir"],
                           "missing": [x["id"] for x in r["rows"] if x["status"] != "met"]}
                          for r in reports]}
        owed = [x for r in reports for x in r["rows"] if x["status"] != "met"]
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        _print(data)
    return 1 if owed else 0


#: A repo file naming at least this share of the dists (and two of them) is a
#: registry a new dist will likely have to join.
_CANDIDATE_SHARE = 0.5
_CANDIDATE_MAX_BYTES = 2_000_000
_TOKEN = re.compile(r"[\w.-]+")


def footprint(repo: Path, dists: "dict[str, Path]") -> "list[dict]":
    """Source files (tracked, or untracked and not ignored) outside every dist that
    name most of the dists as a whole token, most first."""
    from codegraph._core.git import source_files
    names = set(dists)
    if len(names) < 2:
        return []
    inside = {d.relative_to(repo).as_posix() + "/" for d in dists.values()}
    need = max(2, int(len(names) * _CANDIDATE_SHARE + 0.999))
    out = []
    for rel in source_files(repo) or []:
        if any(rel.startswith(p) for p in inside):
            continue
        f = repo / rel
        try:
            if f.is_symlink() or not f.is_file() or f.stat().st_size > _CANDIDATE_MAX_BYTES:
                continue
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hit = {t.rstrip(".") for t in _TOKEN.findall(text)} & names
        if len(hit) >= need:
            out.append({"path": rel, "dists_named": len(hit), "of": len(names)})
    return sorted(out, key=lambda c: (-c["dists_named"], c["path"]))


def _undeclared(repo: Path, dists: "dict[str, Path]") -> Outcome:
    cands = footprint(repo, dists)
    rows = ""
    seen: "set[str]" = set()
    for c in cands[:10]:
        p = Path(c["path"])
        rid = p.stem.strip(".").replace(".", "-") or "row"
        if rid in seen:
            rid = "%s-%s" % (rid, p.suffix.lstrip(".") or len(seen))
        seen.add(rid)
        rows += '- id: %s\n  path: %s\n  contains: "{dist}"\n' % (rid, c["path"])
    template = "obligations:\n" + (rows or '- id: <id>\n  path: <repo file>\n  contains: "{dist}"\n')
    data = {"declared": False, "dists": len(dists), "candidates": cands, "template": template}
    warn = ("obligations not declared: codemap.yaml has no `obligations:` block, so this "
            "says nothing about what a dist owes -- not that it owes nothing. %d tracked "
            "file(s) outside the dists name at least half of them; `template` lists them "
            "as rows to review and paste" % len(cands))

    def _text() -> None:
        print("check dist-obligations: " + warn)
        for c in cands:
            print("  %3d/%d  %s" % (c["dists_named"], c["of"], c["path"]))
        print(template, end="")

    return Outcome(exit_code=3, state="refused", data=data, warnings=(warn,), text=_text)


def _print(data: dict) -> None:
    if "rows" in data:
        print("check dist-obligations %s (%s):" % (data["dist"], data["dist_dir"]))
        for r in data["rows"]:
            line = "  %-7s %-20s %s  %s %r" % (r["status"].upper(), r["id"], r["path"],
                                              r["how"], r["needle"])
            if r.get("why"):
                line += "  -- " + r["why"]
            if r.get("note"):
                line += "  [%s]" % r["note"]
            print(line)
        return
    owing = [d for d in data["dists"] if d["missing"]]
    print("check dist-obligations --all: %d dist(s), %d owe something (%d row(s) declared)"
          % (len(data["dists"]), len(owing), data["rows_declared"]))
    for d in owing:
        print("  MISSING %s: %s" % (d["dist"], ", ".join(d["missing"])))


DIST_OBLIGATIONS = Command(add_args=_args, run=_run)
