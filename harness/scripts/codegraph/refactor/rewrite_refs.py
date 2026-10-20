"""hs-code refactor rewrite-refs -- repoint every reference to a port's OLD package to its home.

The old package is gone from the target, yet scripts, tests, configs and docs still name it:
as imports, as monkeypatch/importlib strings, as `src/<old>/...` paths, as `python -m` lines.
This verb reads the port ledger (the measured old-symbol -> new-home table), resolves each
reference and rewrites it; dry-run by default, `--apply` writes.

Adapted from `ob-tools@60075cb:packages/portkit/src/portkit/rewrite/refs.py` (`run`,
`add_arguments`, `_iter_files`); the engine -- `Index`/`Resolver`/`rewrite_python`/
`rewrite_text`/`load_born` -- is `port_refs`, the same one `port tests-land` rewrites a
landed test with. What changed:
    * the old package is the source's `package` and the new namespace the target's
      `namespace_root` (`_port.port_context`), never the `gsm_agent`/`ob` literals;
    * the ledger is the port root's own, read through `Ledger.load_checked` -- a broken
      line is exit 2 naming its line, where upstream skipped it and built a smaller table;
    * the default scan is every file git counts as source (tracked + untracked, not
      ignored; every file outside a git work tree) instead of upstream's hand list of its own repo's directories; `.port/**`
      joins upstream's default excludes -- the ledger and specs NAME the old package on
      purpose and are the port's record, never a reference to repoint.

Resolution order, most specific first (never guessed): the `<old module>::<Name>` ledger row
(ported, `to:`), then the module's single dominant new home, else UNRESOLVED (listed with the
candidates, the line left as it is). `--born FILE` hands in modules the port bore without a
row; `--historical TEXT` renames the package in PROSE only (comments, docstrings, documents).

WHAT IT CANNOT SEE: `port_refs`'s list, unchanged -- a bare `import <old>.x` with no alias,
`getattr` dispatch, names built by concatenation, a symbol the ledger never saw.

Exit: 0 nothing left (or all written under --apply) · 1 rewrites pending in a dry-run, or a
reference resolved nowhere (left in the tree, listed) · 2 unusable input (no port, a broken
ledger line, a bad `--born` pair).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Iterator, List, Sequence, Tuple

import hs_family

from codegraph._core.git import source_files
from codegraph._core.verdict import Verdict
from hs_family.cli import Command

from ._port import port_context

_TEXT_SUFFIXES = {".md", ".yml", ".yaml", ".json", ".toml", ".txt", ".cfg", ".ini", ".sh", ".rst",
                  ".example"}
_DEFAULT_EXCLUDE = (".port/**", "**/__pycache__/**", "**/.orig", "*.orig", "*.rej", "plans/**",
                    "audit-runs/**", "Dockerfile", ".gitlab-ci.yml", ".test_durations")


def _excluded(rel: Path, exclude: Sequence[str]) -> bool:
    return any(rel.match(g) or (g.endswith("/**") and str(rel).startswith(g.rstrip("*/") + "/"))
               for g in exclude)


def iter_files(repo: Path, roots: "Sequence[str] | None", exclude: Sequence[str]) -> Iterator[Path]:
    """Python and text files under `roots` (repo-relative), or under everything git counts as
    source when no root is named (every file, outside a git work tree)."""
    if roots:
        cands: List[Path] = []
        for r in roots:
            p = repo / r
            cands += [p] if p.is_file() else sorted(p.rglob("*")) if p.is_dir() else []
    else:
        tracked = source_files(repo)
        cands = ([repo / rel for rel in tracked] if tracked is not None
                 else sorted(repo.rglob("*")))            # not a git tree: every file under it
    seen = set()
    for f in cands:
        if not f.is_file() or f in seen or f.is_symlink():
            continue
        rel = f.relative_to(repo)
        if _excluded(rel, exclude) or {"site-packages", ".venv", "__pycache__"} & set(rel.parts):
            continue
        if f.suffix == ".py" or f.suffix in _TEXT_SUFFIXES or f.name == ".env.example":
            seen.add(f)
            yield f


def _args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--source", default=None, dest="source_id", metavar="ID",
                    help="the `.port/port.yaml` source whose package is repointed")
    ap.add_argument("--key", action="append", default=None,
                    help="only ledger rows of this freeze key (repeatable; default: all)")
    ap.add_argument("--root", action="append", default=None,
                    help="repo-relative file/dir to scan (repeatable; default: every source file)")
    ap.add_argument("--exclude", action="append", default=None,
                    help="glob to skip (the defaults apply only when no --root is given)")
    ap.add_argument("--born", action="append", default=None, metavar="FILE",
                    help="modules the port bore (no ledger row): JSON {old: new} or `old new` lines")
    ap.add_argument("--historical", default=None, metavar="TEXT",
                    help="in PROSE replace the old package name of a ref that resolves nowhere")
    ap.add_argument("--report", metavar="FILE", default=None, help="write the full JSON report here")


def run(args: argparse.Namespace, repo: Path, apply: bool) -> Verdict:
    import port_refs
    from port_ledger_writer import BrokenLedgerError, Ledger

    pc = port_context(repo, "rewrite-refs", args.source_id)
    try:
        ledger = Ledger.load_checked(pc.ledger)
    except BrokenLedgerError as e:
        raise hs_family.UsageError("rewrite-refs: the ledger is broken: %s" % e) from e
    pkg = pc.package
    ix = port_refs.load_index(ledger, pkg, args.key)
    for b in args.born or []:
        bad = port_refs.load_born(ix, Path(b), pkg)
        if bad:
            raise hs_family.UsageError("rewrite-refs: " + "; ".join(bad)[:300])
    rs = port_refs.Resolver(ix, pc.port_root, pkg, pc.descriptor)
    exclude: Tuple[str, ...] = tuple(args.exclude or ()) if args.root else _DEFAULT_EXCLUDE
    rep = port_refs.Report()
    changed: List[str] = []
    for f in iter_files(pc.port_root, args.root, exclude):
        try:
            src = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pkg not in src:
            continue
        rel = str(f.relative_to(pc.port_root))
        new = (port_refs.rewrite_python(src, rs, rep, rel, pkg, args.historical) if f.suffix == ".py"
               else port_refs.rewrite_text(src, rs, rep, rel, pkg, 1, args.historical,
                                           port_refs.text_prose(f.suffix, src)))
        if new != src:
            changed.append(rel)
            if apply:
                f.write_text(new, encoding="utf-8")
    data = {"schema": "hs_code.refactor.rewrite-refs/1", "dry_run": not apply,
            "package": pkg, "files_changed": len(changed), "changed": changed,
            "counts": dict(rep.counts), "unresolved": len(rep.unresolved),
            "dropped": len(rep.dropped),
            "by_kind": dict(Counter(u["kind"] for u in rep.unresolved)),
            "unresolved_imports": [u for u in rep.unresolved if u["kind"] in ("import", "star")],
            "unknown_modules": dict(rs.unknown.most_common()),
            "sample_unresolved": rep.unresolved[:15]}
    if args.report:
        Path(args.report).write_text(json.dumps({**data, "unresolved_all": rep.unresolved,
                                                 "born_todo": {m: "" for m in sorted(rs.unknown)},
                                                 "dropped_all": rep.dropped}, indent=1),
                                     encoding="utf-8")
    # an unresolved ref is left in the tree as it was: exit 1 (listed) even after --apply
    owed = any(u["kind"] != "bare" for u in rep.unresolved)
    return Verdict.of(not (owed or (changed and not apply)), data)


def _rewrite_refs(args: argparse.Namespace) -> int:
    v = run(args, args.ctx.root, bool(getattr(args.opts, "apply", False)))
    if not getattr(args.opts, "json", False):
        d = v.data
        print("rewrite-refs %s: %d file(s) %s, %d unresolved, %d dropped" % (
            d["package"], d["files_changed"], "written" if not d["dry_run"] else "to rewrite",
            d["unresolved"], d["dropped"]))
        for u in d["sample_unresolved"]:
            print("  %s:%s %s — %s" % (u["file"], u["line"], u["kind"], u["why"]))
    else:
        print(json.dumps(v.data, sort_keys=True, ensure_ascii=False))
    return int(v.exit)


REWRITE_REFS = Command(add_args=_args, run=_rewrite_refs)
