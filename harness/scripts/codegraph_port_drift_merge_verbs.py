"""hs-code's `port` domain — `drift-merge`: carry a catch-up batch's src drift
into the target files that already hold it. A FLAT module under
`harness/scripts/` (same `hs-code-registry.d` reachability reason as every other
`codegraph_port_*_verbs.py`). The merge engine is `port_drift_merge`.

`hs-code port drift-merge --batch B [--file REL]... [--by-symbol] [--fuzz N]
[--on-conflict red|reject] [--merge REL=PATH]... [--twins REL=PATH,PATH]... [--tests]
[--apply]`

WHICH FILES: every src `.py` file the drift CHANGED (`M`, or `R` with content)
that still has an item `drift-hunks` holds undisposed — a file whose items all
carry a disposition is done and skipped, so a rerun after a person disposed of a
`.rej` never re-opens it. `--file` narrows that set. Added, deleted and
rename-only files are listed (`new`/`deleted`/`rename_only`) for their own route
(`add-module`, `judge-removed`, a disposition), never merged.

WHERE: the ledger (`port_refs.Index`) — the target modules the rows under either
anchor ported the file's module into, each resolved to its file through the
target's layout (`Resolver.file_of`). One home: three-way merge. Several: hunk by
hunk into the twin holding each hunk's context (`--fuzz`, default 3), or with
`--by-symbol` each changed top-level block into the module its own row names.
`--merge REL=PATH` / `--twins REL=PATH,PATH` name the target file(s) by hand and
replace the ledger's answer for that file; a path outside the target is exit 3.

HOW: both upstream versions (`git show <pin>:<rel>`) are rewritten through the
landing route first (`rewrite_landed_text`, the route `add-module` lands with),
or with `--tests` through the ledger-driven reference rewrite (`port_refs`). A
conflict is red and nothing is written (`--on-conflict red`, default); under
`reject` the target's side of each conflict is kept, the rest lands, and the
upstream side is appended to `<twin>.rej` (`drift-rej` lists it until a person
disposes of it). A rewrite leftover the drift ADDED (the new revision carries it
more often than the old), a merged text that would gain an import still in
upstream spelling, a missing twin or an unreadable revision is red for that file;
a leftover both revisions carry sits on a line the merge leaves as the twin was
landed, and is no problem of this merge. Under `--apply` a two-homes fork the
drift added is raised as an open item (step `drift-merge`, the landing route's
subject, so an approval there or here resolves both). Files are independent: one
red file does not stop another.

RECORDS: `--apply` appends `applied` (dest = the one target file) for every
undisposed item of a file merged clean into ONE file. A file whose hunks landed
across several twins has no one dest — named under `dispose_by_hand`.

Exit: 1 while any file is red or owes a `.rej`, else 0; 2 broken; 3 refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hs_family  # noqa: E402
import port_drift  # noqa: E402
import port_drift_merge as dm  # noqa: E402
import port_open  # noqa: E402
import port_refs  # noqa: E402
import port_spec  # noqa: E402
from codegraph._core.git import GitError, run_git  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph.refactor._layout import load_descriptor  # noqa: E402
from codegraph.refactor.errors import RefactorError  # noqa: E402
from codegraph_port_drift_verbs import _as_json, _batch_args, _ledger, _load  # noqa: E402
from codegraph_port_tree_verbs import (Fork, rewrite_landed_text,  # noqa: E402
                                       upstream_spelled_imports)
from hs_family.cli import Command  # noqa: E402


def _merge_args(ap: argparse.ArgumentParser) -> None:
    _batch_args(ap)
    ap.add_argument("--file", action="append", default=[], dest="files", metavar="REL")
    ap.add_argument("--by-symbol", action="store_true")
    ap.add_argument("--fuzz", type=int, default=3, metavar="N")
    ap.add_argument("--on-conflict", choices=("red", "reject"), default="red")
    ap.add_argument("--merge", action="append", default=[], metavar="REL=PATH")
    ap.add_argument("--twins", action="append", default=[], metavar="REL=PATH,PATH")
    ap.add_argument("--tests", action="store_true")


def _show(d: port_drift.Drift, sha: str, rel: str) -> "str | None":
    try:
        return run_git(d.source_repo, "show", "%s:%s" % (sha, rel)).decode("utf-8")
    except (GitError, UnicodeDecodeError):
        return None


def _module(rel: str, modules: dict) -> "str | None":
    mod = modules.get(rel, (None, None))[1]
    if mod and Path(rel).name == "__init__.py":
        mod += ".__init__"
    return mod


def _explicit(specs: "list[str]", flag: str, port_root: Path) -> "dict[str, list[str]]":
    out: "dict[str, list[str]]" = {}
    root = port_root.resolve()
    for spec in specs:
        rel, sep, paths = spec.partition("=")
        if not sep or not rel or not paths:
            raise hs_family.UsageError("%s wants REL=PATH[,PATH...], got %r" % (flag, spec))
        targets = [p for p in paths.split(",") if p]
        for p in targets:
            if not (root / p).resolve().is_relative_to(root):
                raise hs_family.RefusedError(
                    "%s %s: %s resolves outside the target %s — refusing to write there"
                    % (flag, rel, p, root))
        out[rel] = targets
    return out


class _Rewriter:
    """Upstream text -> target spelling, both revisions through the same route."""

    def __init__(self, d: port_drift.Drift, args: argparse.Namespace, apply: bool):
        self.d, self.apply, self.tests = d, apply, args.tests
        if not d.package:
            raise hs_family.UsageError("source %r declares no `package`" % d.source_id)
        self.package: str = d.package
        try:
            self.merged = port_spec.merged_spec_and_unit_dist(d.batch.specs_dir, self.package)[0]
        except port_spec.SpecError as e:
            raise hs_family.UsageError(str(e)) from e
        self.resolver = None
        if self.tests:
            try:
                descriptor = load_descriptor(d.port_root, "port drift-merge")
            except RefactorError as e:
                raise hs_family.UsageError(str(e)) from e
            ix = port_refs.load_index(_ledger(d.port_root), self.package, keys=[d.from_key, d.to_key])
            self.resolver = port_refs.Resolver(ix, d.port_root, self.package, descriptor)

    def pair(self, old: str, old_rel: str, old_here: "str | None", new: str, rel: str,
             new_here: "str | None", problems: "list[str]") -> "tuple[str, str]":
        """Both revisions respelled. A problem the old revision carries as often
        as the new one sits on a line the merge leaves as the twin landed it, so
        only what the drift added is this merge's — and only a fork the drift
        added is raised (`--apply`), as `drift-merge`'s own."""
        was: "list[tuple[object, str]]" = []
        now: "list[tuple[object, str]]" = []
        forks_was: "list[Fork]" = []
        forks_now: "list[Fork]" = []
        old_r = self._one(old, old_rel, old_here, problems, was, forks_was)
        new_r = self._one(new, rel, new_here, problems, now, forks_now)
        left = Counter(k for k, _m in was)
        for k, msg in now:
            if left[k]:
                left[k] -= 1
            else:
                problems.append(msg)
        if self.apply:
            known = Counter((f.module, f.names) for f in forks_was)
            for f in forks_now:
                if known[(f.module, f.names)]:
                    known[(f.module, f.names)] -= 1
                    continue
                port_open.raise_item(self.d.port_root, self.d.batch.name, kind="two-homes",
                                     subject=f.subject, candidates=f.candidates,
                                     evidence=f.evidence, step="drift-merge")
        return old_r, new_r

    def _one(self, text: str, rel: str, here: "str | None", problems: "list[str]",
             found: "list[tuple[object, str]]", forks: "list[Fork]") -> str:
        """`found` gets `(key, message)`: the key names the problem with no file
        or line, so the two revisions compare."""
        if not rel.endswith(".py"):
            return text
        if self.resolver is not None:
            rep = port_refs.Report()
            out = port_refs.rewrite_python(text, self.resolver, rep, rel, self.package, None)
            found += [((u["kind"], u["text"], u["why"]),
                       "%s:%s unresolved %s `%s` (%s)" % (rel, u["line"], u["kind"], u["text"],
                                                         u["why"]))
                      for u in rep.unresolved]
            return out
        if here is None:
            problems.append("%s: no snapshot names its module" % rel)
            return text
        mine: "list[str]" = []
        out = rewrite_landed_text(text, here, rel, self.merged, self.d.port_root, self.package,
                                  mine, batch_name=self.d.batch.name, apply=False, forks=forks)
        head = rel + ": "
        found += [(m[len(head):] if m.startswith(head) else m, m) for m in mine]
        return out


def _lands_upstream_imports(m: dm.Merged, port_root: Path, package: str) -> None:
    """A merged text may not gain an import the rewrite left in upstream
    spelling — whatever route carried it there (a moved line, a conflict kept
    on the twin's side while the rest lands)."""
    if not (m.port and m.changed):
        return
    gained = upstream_spelled_imports(m.text, package) - upstream_spelled_imports(
        (port_root / m.port).read_text(encoding="utf-8"), package)
    if gained:
        m.problems.append("%s: the merge lands upstream import(s) the rewrite could not place: %s"
                          % (m.port, "; ".join(sorted(gained))))


def _merge_one(d: port_drift.Drift, rw: _Rewriter, row: dict, homes: "list[str]",
               args: argparse.Namespace, from_mods: dict, to_mods: dict) -> "list[dm.Merged]":
    rel, old_rel = row["path"], row["old"] or row["path"]
    old_t, new_t = _show(d, d.from_sha, old_rel), _show(d, d.to_sha, rel)
    if old_t is None or new_t is None:
        missing = (old_rel, d.from_key) if old_t is None else (rel, d.to_key)
        return [dm.Merged(file=rel, port="", text="", changed=False,
                          problems=["%s: not readable at %s" % missing])]
    gone = [h for h in homes if not (d.port_root / h).is_file()]
    if gone:
        return [dm.Merged(file=rel, port="", text="", changed=False,
                          problems=["%s: target file(s) missing: %s" % (rel, ", ".join(gone))])]
    problems: "list[str]" = []
    old_r, new_r = rw.pair(old_t, old_rel, _module(old_rel, from_mods), new_t, rel,
                           _module(rel, to_mods), problems)
    if len(homes) == 1:
        out = [dm.three_way(rel, d.port_root / homes[0], homes[0], old_r, new_r)]
    elif args.by_symbol:
        out = _by_symbol(d, rel, homes, old_t, new_t, rw, old_rel, from_mods, to_mods, problems)
    else:
        out = dm.merge_twins(rel, {h: d.port_root / h for h in homes}, old_r, new_r, args.fuzz)
    if out:
        out[0].problems[:0] = problems
    for m in out:
        _lands_upstream_imports(m, d.port_root, rw.package)
    return out


def _by_symbol(d: port_drift.Drift, rel: str, homes: "list[str]", old_t: str, new_t: str,
               rw: _Rewriter, old_rel: str, from_mods: dict, to_mods: dict,
               problems: "list[str]") -> "list[dm.Merged]":
    """Each changed top-level block into the file of the module its row names."""
    ix = port_refs.load_index(_ledger(d.port_root), d.package or "", keys=[d.from_key, d.to_key])
    old_mod = from_mods.get(old_rel, (None, ""))[1]
    try:
        changed = dm.changed_names(old_t, new_t)
    except SyntaxError as e:
        return [dm.Merged(file=rel, port="", text="", changed=False,
                          problems=["%s: does not parse (%s)" % (rel, e)])]
    rs = port_refs.Resolver(ix, d.port_root, d.package or "", load_descriptor(d.port_root, "port drift-merge"))
    groups: "dict[str, set[str]]" = {}
    for name in changed:
        targets = sorted(ix.sym.get((old_mod, name)) or ())
        files = sorted({str(rs.file_of(m)) for m, _n in targets if rs.file_of(m)})
        if len(files) != 1:
            problems.append("%s: %r has %s — dispose it by hand" % (
                rel, name, "no ledger home" if not files else "%d homes %s" % (len(files), files)))
            continue
        groups.setdefault(files[0], set()).add(name)
    out: "list[dm.Merged]" = []
    for port_rel, names in sorted(groups.items()):
        old_r, new_r = rw.pair(dm.virtual_text(old_t, names), old_rel, _module(old_rel, from_mods),
                               dm.virtual_text(new_t, names), rel, _module(rel, to_mods), problems)
        m = dm.three_way(rel, d.port_root / port_rel, port_rel, old_r, new_r)
        m.symbols = sorted(names)
        out.append(m)
    return out or [dm.Merged(file=rel, port="", text="", changed=False)]


def _homes_of(rs: port_refs.Resolver, ix: port_refs.Index, row: dict,
              from_mods: dict) -> "tuple[list[str], list[str]]":
    old_mod = from_mods.get(row["old"] or row["path"], (None, ""))[1]
    mods = ix.module_places(old_mod)
    files: "list[str]" = []
    problems: "list[str]" = []
    for m in mods:
        f = rs.file_of(m)
        if f is None:
            problems.append("%s: ledger home %s has no one file in the target" % (row["path"], m))
        else:
            files.append(str(f))
    return files, problems


def _drift_merge(args: argparse.Namespace) -> int:
    d = _load(args)
    if not d.package:
        raise hs_family.UsageError(
            "source %r declares no `package` in `.port/port.yaml` — drift-merge rewrites "
            "upstream imports and needs it" % d.source_id)
    apply = bool(getattr(args.opts, "apply", False))
    explicit = {**_explicit(args.merge, "--merge", d.port_root),
                **_explicit(args.twins, "--twins", d.port_root)}
    rows = port_drift.file_rows(d)
    ids = port_drift.items(d)
    sidecar = port_drift.dispositions_path(d.port_root, d.batch.name)
    have = {str(ln.get("hunk")) for ln in port_drift.read_dispositions(sidecar)[0]}
    by_path: "dict[str, list[str]]" = {}
    for i in ids:
        by_path.setdefault(port_drift.item_path(i), []).append(i)
    src_py = [r for r in rows if r["area"] == "src" and r["path"].endswith(".py")]
    known = {r["path"] for r in rows}
    unknown = sorted(set(args.files) - known)
    if unknown:
        raise hs_family.UsageError("not a file of batch %r's drift: %s" % (d.batch.name,
                                                                           ", ".join(unknown)))
    data: dict = {"batch": d.batch.name, "apply": apply, "on_conflict": args.on_conflict,
                  "new": sorted(r["path"] for r in src_py if r["status"] == "A"),
                  "deleted": sorted(r["path"] for r in src_py if r["status"] == "D"),
                  "rename_only": [], "disposed": [], "files": [], "recorded": 0,
                  "rejects_owed": [], "dispose_by_hand": []}
    from_mods = port_drift.path_modules(d.from_idx)
    to_mods = port_drift.path_modules(d.to_idx)
    ix = port_refs.load_index(_ledger(d.port_root), d.package, keys=[d.from_key, d.to_key])
    try:
        rs = port_refs.Resolver(ix, d.port_root, d.package, load_descriptor(d.port_root, "port drift-merge"))
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e
    rw = _Rewriter(d, args, apply)
    reject = args.on_conflict == "reject"
    todo: "list[tuple[dict, list[str], list[str]]]" = []
    for r in rows:
        rel = r["path"]
        if rel in explicit:
            todo.append((r, explicit[rel], []))
            continue
        if r not in src_py or r["status"] not in "MR" or (args.files and rel not in args.files):
            continue
        items = by_path.get(rel, [])
        if items == ["%s:%d:file" % (rel, port_drift.HUNKLESS_START)]:
            data["rename_only"].append(rel)
            continue
        if items and all(i in have for i in items):
            data["disposed"].append(rel)
            continue
        files, problems = _homes_of(rs, ix, r, from_mods)
        todo.append((r, files, problems))

    red = False
    lines: "list[dict]" = []
    for r, homes, problems in todo:
        if problems or not homes:
            merged = [dm.Merged(file=r["path"], port="", text="", changed=False,
                                problems=problems or ["%s: the ledger names no home" % r["path"]])]
        else:
            merged = _merge_one(d, rw, r, homes, args, from_mods, to_mods)
        hard = any(m.problems or (m.conflicts and not reject) for m in merged)
        owed = [m.port for m in merged if m.rej]
        red = red or hard or bool(owed)
        data["files"] += [m.report() for m in merged]
        if hard:
            continue
        data["rejects_owed"] += owed
        ports = {m.port for m in merged if m.hunks_placed or m.changed or m.symbols} or \
            {m.port for m in merged}
        clean = not owed and not any(m.rejects for m in merged)
        undisposed = [i for i in by_path.get(r["path"], []) if i not in have]
        if clean and len(ports) == 1 and undisposed:
            (dest,) = ports
            lines += [port_drift.disposition(i, "applied", dest=dest) for i in undisposed]
        elif clean and undisposed:
            data["dispose_by_hand"].append({"file": r["path"], "ports": sorted(ports)})
        if apply:
            for m in merged:
                if m.port and m.changed:
                    (d.port_root / m.port).write_text(m.text, encoding="utf-8")
                if m.rej:
                    rp = d.port_root / (m.port + ".rej")
                    prev = rp.read_text(encoding="utf-8") if rp.is_file() else ""
                    rp.write_text(prev + "### drift-merge %s @ %s\n%s" % (m.file, d.to_key, m.rej),
                                  encoding="utf-8")
    if apply:
        port_drift.append_dispositions(sidecar, lines)
        data["recorded"] = len(lines)
    data["ok"] = not red
    if _as_json(args):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        _render(data)
    return int((Verdict.red(data) if red else Verdict.ok(data)).exit)


def _render(data: dict) -> None:
    for f in data["files"]:
        if f["hunks_placed"] or f["rejects"] or f["hunks_already"]:
            state = "%d hunk(s) placed" % len(f["hunks_placed"])
        else:
            state = "conflicts=%d" % f["conflicts"]
        print("drift-merge %s -> %s: %s, %s" % (f["file"], f["port"] or "-", state,
                                                "changes" if f["changed"] else "no change"))
        for g in f["regions"]:
            print("    conflict lines %d-%d (port %d, upstream %d)"
                  % (g["start"], g["end"], g["port_lines"], g["upstream_lines"]))
        for p in f["problems"]:
            print("    RED %s" % p)
        if f["rejects"]:
            print("    rejected %d hunk(s) -> .rej" % f["rejects"])
    for key in ("new", "deleted", "rename_only", "disposed"):
        if data[key]:
            print("  %s: %s" % (key, ", ".join(data[key])))
    for h in data["dispose_by_hand"]:
        print("  landed across %s — dispose %s by hand" % (", ".join(h["ports"]), h["file"]))
    print("floor:", "RED" if not data["ok"] else
          ("written (%d disposition(s))" % data["recorded"] if data["apply"]
           else "OK (dry-run; --apply writes)"))


DRIFT_MERGE = Command(add_args=_merge_args, run=_drift_merge)
