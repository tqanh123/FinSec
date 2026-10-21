"""port_drift.py — what a catch-up batch's source drifted by, read from facts
already on disk: the predecessor's pin and this batch's pin (the two commits),
their frozen snapshots (the two symbol tables) and the ledger (where each old
symbol lives in the target now). A FLAT module under `harness/scripts/`.

Ported from `ob-tools@60075cb:packages/portkit/src/portkit/drift/{scan,hunks}.py`
(`parse_name_status`, `area_of`, `zone`, `parse_hunks`, `validate_line`,
`check`, `check_dest`, `mark`). Every upstream input that was a flag or a
hand-kept file is DERIVED here instead, so a catch-up batch alone is enough:

    --from/--to refs        the two batches' `pinned_sha`
    --a/--b snapshot dirs   the two batches' `frozen_snapshot`
    src roots               the `roots` both snapshots record (upstream: the
                            batch descriptor's roots that exist in the tree)
    areas config            a non-src path is zoned by its first directory
                            (upstream: a hand-kept `areas:` list)
    monoliths config        a file is SPLIT when the ledger ported its symbols
                            into more than one target module (upstream: a
                            hand-kept list of substrings with prose notes)
    --diff FILE             `git diff -M <from> <to>` over the src roots,
                            recomputed from the pins on every read, so the hunk
                            list can never be a stale file
    --dispositions FILE     `.port/batches/<batch>/drift/hunks.jsonl`

One change on top of upstream: a file the diff names with NO `@@` hunk (a pure
rename, a binary or mode-only change) is an item too, id `<path>:0:file`.
Upstream counted such files and said a clean run proves nothing about them;
here each one needs its own disposition like any hunk.

GIT: `diff` and `rev-parse` only, read-only, on the source repo.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hs_family
import port_batch_reader
import port_config
import port_workspace
from codegraph._core.git import GitError, run_git

UNRULED = "UNRULED"
HUNKLESS_START = 0

STATUSES = ("applied", "superseded-by", "out-of-pen", "stuck-on-<slug>", "downgraded")
_NEEDS = {"applied": "dest", "superseded-by": "by", "out-of-pen": "reason",
          "downgraded": "reason"}
_STUCK = re.compile(r"^stuck-on-[a-z0-9][a-z0-9-]*$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_LINE_KEYS = frozenset({"hunk", "status", "dest", "by", "reason", "replaces", "actor", "ts"})
#: `-c core.quotepath=off`: a path with a non-ASCII byte is printed as itself, not
#: as a quoted octal escape no `startswith(root)` would ever match.
_GIT_PATHS = ("-c", "core.quotepath=off")


@dataclass(frozen=True)
class Drift:
    """The two ends of a catch-up batch, resolved once."""
    port_root: Path
    batch: Any
    from_batch: Any
    source_id: str
    source_repo: Path
    package: "str | None"
    from_sha: str
    to_sha: str
    from_idx: dict
    to_idx: dict
    src_prefixes: "tuple[str, ...]"

    @property
    def from_key(self) -> str:
        return self.from_sha[:8]

    @property
    def to_key(self) -> str:
        return self.to_sha[:8]


def load(port_root: Path, batch_name: str, source_id: "str | None" = None) -> Drift:
    """Both ends of `batch_name`'s drift. `hs_family.UsageError` (exit 2) when the
    batch follows no other or either end cannot be read; the pin/snapshot
    agreement is `port_batch_reader.pinned_frozen_snapshot`'s own refusal."""
    batch = port_batch_reader.load_batch(port_root, batch_name)
    if not batch.from_batch:
        raise hs_family.UsageError(
            "batch %r declares no `from_batch` — only a batch opened by `port catch-up` "
            "has a predecessor to measure drift against" % batch.name)
    prev = port_batch_reader.load_batch(port_root, batch.from_batch)
    source, problem = port_batch_reader.find_source(batch, source_id)
    if problem:
        raise hs_family.UsageError(str(problem))
    sid = str(source.get("id"))
    prev_source, prev_problem = port_batch_reader.find_source(prev, sid)
    if prev_problem:
        raise hs_family.UsageError("predecessor %r: %s" % (prev.name, prev_problem))
    for b in (prev, batch):
        if not b.frozen_snapshot:
            raise hs_family.UsageError(
                "batch %r declares no `frozen_snapshot` — drift is measured between "
                "two frozen snapshots" % b.name)
    from_sha, from_idx = port_batch_reader.pinned_frozen_snapshot(port_root, prev, prev_source)
    to_sha, to_idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    repo, package = source_entry(port_root, sid)
    return Drift(port_root=port_root, batch=batch, from_batch=prev, source_id=sid,
                 source_repo=repo, package=package, from_sha=from_sha, to_sha=to_sha,
                 from_idx=from_idx, to_idx=to_idx,
                 src_prefixes=src_prefixes(from_idx, to_idx))


def source_entry(port_root: Path, source_id: str) -> "tuple[Path, str | None]":
    """`.port/port.yaml sources[<id>]`: its `repo` resolved against the port root,
    and its `package` (None when the source declares none)."""
    cfg, problems = port_config.load_and_validate(port_workspace.port_yaml_path(port_root))
    if problems:
        raise hs_family.UsageError("`.port/port.yaml` is invalid: %s" % "; ".join(problems))
    entry, problem = port_config.find_source(cfg, source_id)
    if problem:
        raise hs_family.UsageError(str(problem))
    repo = entry.get("repo") if isinstance(entry, dict) else None
    if not isinstance(repo, str) or not repo:
        raise hs_family.UsageError("source %r has no `repo:` in `.port/port.yaml`" % source_id)
    package = entry.get("package")
    return (port_root / repo).resolve(), package if isinstance(package, str) else None


def src_prefixes(*indexes: dict) -> "tuple[str, ...]":
    """`<root path>/` for every root either snapshot records, longest first."""
    out = {str(r["path"]).rstrip("/") + "/" for ix in indexes
           for r in ix.get("roots") or () if isinstance(r, dict) and r.get("path")}
    return tuple(sorted(out, key=lambda p: (-len(p), p)))


def area_of(path: str, prefixes: "tuple[str, ...]") -> str:
    if any(path.startswith(p) for p in prefixes):
        return "src"
    return path.split("/", 1)[0] if "/" in path else "(top level)"


def parse_name_status(raw: bytes) -> "list[tuple[str, str, str | None]]":
    """`git diff --name-status -M -z` -> [(status letter, path, old path|None)]."""
    tok = raw.decode("utf-8", "surrogateescape").split("\0")
    out: "list[tuple[str, str, str | None]]" = []
    i = 0
    while i < len(tok) and tok[i]:
        st = tok[i]
        if st[0] in "RC":
            out.append((st[0], tok[i + 2], tok[i + 1]))
            i += 3
        else:
            out.append(("M" if st[0] == "T" else st[0], tok[i + 1], None))
            i += 2
    return out


def _git(d: Drift, *args: str) -> bytes:
    try:
        return run_git(d.source_repo, *_GIT_PATHS, *args)
    except GitError as e:
        raise hs_family.UsageError("source %s: %s" % (d.source_repo, e)) from e


def file_rows(d: Drift) -> "list[dict]":
    """Every file the source changed between the two pins, with its area."""
    raw = _git(d, "diff", "--name-status", "-M", "-z", d.from_sha, d.to_sha)
    return [{"status": st, "path": path, "old": old, "area": area_of(path, d.src_prefixes)}
            for st, path, old in parse_name_status(raw)]


def path_modules(*indexes: dict) -> "dict[str, tuple[str, str]]":
    """repo-relative path -> (unit key, dotted module), from whichever snapshot
    knows the path."""
    out: "dict[str, tuple[str, str]]" = {}
    for ix in indexes:
        for unit, mods in (ix.get("units") or {}).items():
            for m in mods:
                out.setdefault(m["path"], (unit, m["module"]))
    return out


def diff_text(d: Drift) -> str:
    """The unified diff of the src roots between the two pins."""
    if not d.src_prefixes:
        return ""
    return _git(d, "diff", "-M", "--no-color", "--no-ext-diff", d.from_sha, d.to_sha, "--",
                *d.src_prefixes).decode("utf-8", "replace")


def parse_hunks(diff: str) -> dict:
    """`{'hunks': [{'id','path','old_start','old','new'}], 'hunkless_files': [path]}`
    — id is `<path>:<old_start>:<sha8 of the hunk body>`, upstream's own
    spelling; `old`/`new` are the line numbers the hunk removes/adds, blank
    lines left out (they change no symbol)."""
    hunks: "list[dict]" = []
    seen: "dict[str, int]" = {}
    path: "str | None" = None
    cur: "dict | None" = None
    body: "list[str]" = []

    def flush() -> None:
        nonlocal cur, body
        if cur is not None:
            sha = hashlib.sha256("\n".join(body).encode()).hexdigest()[:8]
            cur["id"] = "%s:%d:%s" % (cur["path"], cur["old_start"], sha)
            o, n = cur["old_start"], cur.pop("new_start")
            cur["old"], cur["new"] = [], []
            for ln in body:
                if ln.startswith("\\"):
                    continue
                if ln[:1] == "-":
                    if ln[1:].strip():
                        cur["old"].append(o)
                    o += 1
                elif ln[:1] == "+":
                    if ln[1:].strip():
                        cur["new"].append(n)
                    n += 1
                else:
                    o, n = o + 1, n + 1
            hunks.append(cur)
        cur, body = None, []

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            path = m.group(2) if m else None
            if path:
                seen.setdefault(path, 0)
        elif line.startswith("+++ ") and cur is None:
            p = line[4:].strip()
            if p != "/dev/null":
                path = p[2:] if p.startswith("b/") else p
                seen.setdefault(path, 0)
        elif (m := _HUNK.match(line)) and path:
            flush()
            cur = {"path": path, "old_start": int(m.group(1)), "new_start": int(m.group(2))}
            seen[path] += 1
        elif cur is not None:
            body.append(line)
    flush()
    return {"hunks": hunks, "hunkless_files": sorted(p for p, n in seen.items() if not n)}


def items(d: Drift) -> "list[str]":
    """Every id that needs one disposition: each hunk, then each hunkless file."""
    parsed = parse_hunks(diff_text(d))
    return ([h["id"] for h in parsed["hunks"]]
            + ["%s:%d:file" % (p, HUNKLESS_START) for p in parsed["hunkless_files"]])


def item_path(item: str) -> str:
    """`path:start:tail` -> `path` (the tail two fields are fixed, so rsplit is safe)."""
    return item.rsplit(":", 2)[0]


def dispositions_path(port_root: Path, batch_name: str) -> Path:
    return port_batch_reader.batches_dir(port_root) / batch_name / "drift" / "hunks.jsonl"


def read_dispositions(path: Path) -> "tuple[list[dict], list[dict]]":
    """(lines, malformed) — an unreadable line is reported, never skipped."""
    lines: "list[dict]" = []
    bad: "list[dict]" = []
    if not path.is_file():
        return lines, bad
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            bad.append({"line": n, "why": ("unparseable: %s" % exc)[:100]})
            continue
        obj["_n"] = n
        lines.append(obj)
    return lines, bad


def validate_line(line: dict) -> "str | None":
    """Why one disposition line is malformed, or None."""
    unknown = sorted(set(line) - _LINE_KEYS - {"_n"})
    if unknown:
        return "unknown key(s) %s" % ", ".join(unknown)
    if not isinstance(line.get("hunk"), str) or not line["hunk"]:
        return "missing hunk id"
    rep = line.get("replaces")
    if rep is not None and (type(rep) is not int or rep < 1):
        return "`replaces` must be the line number of an earlier line, not %r" % (rep,)
    for key in ("actor", "ts"):
        if not str(line.get(key) or "").strip():
            return "missing `%s`" % key
    st = line.get("status")
    if st in _NEEDS:
        return None if str(line.get(_NEEDS[st]) or "").strip() else \
            "status %s needs `%s`" % (st, _NEEDS[st])
    if isinstance(st, str) and _STUCK.match(st):
        return None if str(line.get("reason") or "").strip() else "status %s needs `reason`" % st
    return "unknown status %r (closed set: %s)" % (st, ", ".join(STATUSES))


def active(lines: "list[dict]") -> "tuple[list[dict], list[dict]]":
    """(lines still in force, malformed `replaces`). A line naming an earlier
    line of the SAME item in `replaces` takes its place; the replaced line stays
    in the file as history. `replaces` naming no earlier line of that item is
    malformed, and so is a second line replacing the same one."""
    by_n = {ln["_n"]: ln for ln in lines}
    replaced: "set[int]" = set()
    bad: "list[dict]" = []
    for ln in lines:
        rep = ln.get("replaces")
        if rep is None or validate_line(ln):
            continue
        old = by_n.get(rep)
        if old is None or rep >= ln["_n"] or old.get("hunk") != ln.get("hunk"):
            bad.append({"line": ln["_n"], "why": "`replaces` %s names no earlier line of %s"
                        % (rep, ln.get("hunk"))})
        elif rep in replaced:
            bad.append({"line": ln["_n"], "why": "line %d is already replaced" % rep})
        else:
            replaced.add(rep)
    return [ln for ln in lines if ln["_n"] not in replaced], bad


def _spans(idx: dict) -> "dict[str, list[tuple[int, int, str]]]":
    """path -> `(first, last, name)` per symbol and method of a snapshot."""
    out: "dict[str, list[tuple[int, int, str]]]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            rows = out.setdefault(m["path"], [])
            for s in m.get("symbols") or ():
                if s.get("lines"):
                    rows.append((s["lines"][0], s["lines"][1], s["name"]))
                for mm in s.get("methods") or ():
                    if mm.get("lines"):
                        rows.append((mm["lines"][0], mm["lines"][1],
                                     "%s.%s" % (s["name"], mm["name"])))
    return out


def _innermost(spans: "list[tuple[int, int, str]]", line: int) -> "str | None":
    inside = [(b - a, name) for a, b, name in spans if a <= line <= b]
    return min(inside)[1] if inside else None


def judged_items(d: Drift, state: dict) -> "list[str]":
    """The hunks the ledger already judged at symbol level: every non-blank
    line the hunk adds lies in a symbol of the TO snapshot that has a row
    under the TO anchor, and every one it removes lies in a symbol that has
    one too or whose FROM row is `dropped`. `state` is the ledger's
    node -> last event. A line outside every symbol (an import, a module
    docstring), a hunk that only moves blank lines and a hunkless file are
    never judged this way."""
    renamed = {r["path"]: r["old"] for r in file_rows(d) if r.get("old")}
    return judged_hunks(parse_hunks(diff_text(d))["hunks"], path_modules(d.from_idx, d.to_idx),
                        _spans(d.from_idx), _spans(d.to_idx), renamed, state,
                        d.from_key, d.to_key)


def judged_hunks(hunks: "list[dict]", mods: dict, from_sp: dict, to_sp: dict,
                 renamed: "dict[str, str]", state: dict, from_key: str,
                 to_key: str) -> "list[str]":
    def judged(mod: str, name: "str | None", *, removed: bool) -> bool:
        if name is None:
            return False
        if "%s::%s::%s" % (to_key, mod, name) in state:
            return True
        ev = state.get("%s::%s::%s" % (from_key, mod, name))
        return removed and ev is not None and ev.status == "dropped"

    out: "list[str]" = []
    for h in hunks:
        path = h["path"]
        old_path = renamed.get(path, path)
        to_mod = mods.get(path, ("", ""))[1]
        from_mod = mods.get(old_path, ("", ""))[1]
        if not (h["old"] or h["new"]):
            continue
        if all(judged(to_mod, _innermost(to_sp.get(path, []), n), removed=False)
               for n in h["new"]) and \
                all(judged(from_mod, _innermost(from_sp.get(old_path, []), o), removed=True)
                    for o in h["old"]):
            out.append(h["id"])
    return out


def check(ids: "list[str]", lines: "list[dict]", bad: "list[dict]",
          judged: "list[str] | None" = None) -> dict:
    """Two-way: an item with no line in force (and not `judged` — the ledger
    already judged every symbol it changes), a line naming no item, a second
    line in force for one item and a malformed line are each red."""
    idset = set(ids)
    malformed = list(bad) + [{"line": ln["_n"], "why": why}
                             for ln in lines if (why := validate_line(ln))]
    lines, bad_rep = active(lines)
    malformed += bad_rep
    by_item: "dict[str, list[dict]]" = {}
    for line in lines:
        by_item.setdefault(str(line.get("hunk")), []).append(line)
    counts: Counter = Counter()
    for item, ls in by_item.items():
        if item in idset and len(ls) == 1 and not validate_line(ls[0]):
            st = ls[0]["status"]
            counts["stuck" if st.startswith("stuck-on-") else st] += 1
    by_rows = [i for i in (judged or ()) if i in idset and i not in by_item]
    undisposed = [i for i in ids if i not in by_item and i not in set(by_rows)]
    orphans = sorted(i for i in by_item if i not in idset)
    dupes = sorted(i for i, ls in by_item.items() if len(ls) > 1)
    return {"items": len(ids), "counts": dict(sorted(counts.items())), "judged": by_rows,
            "undisposed": undisposed, "orphans": orphans, "duplicates": dupes,
            "malformed": malformed,
            "ok": not (undisposed or orphans or dupes or malformed)}


def defined_names(text: str) -> "set[str]":
    """Every def/class/assigned name a module DEFINES (not one it mentions)."""
    tree = ast.parse(text)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    names |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    names |= {n.target.id for n in ast.walk(tree)
              if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    return names


def check_dest(port_root: Path, dest: str, symbol: "str | None" = None) -> "str | None":
    """Why an `applied` dest is not where the hunk landed, or None: it must be a
    file inside the target and, with `symbol`, a module that DEFINES it."""
    root = port_root.resolve()
    path = (root / dest).resolve()
    if not path.is_relative_to(root):
        return "dest %s resolves outside the target %s" % (dest, root)
    if not path.is_file():
        return "dest %s is not a file under %s" % (dest, root)
    if symbol:
        text = path.read_text(encoding="utf-8", errors="replace")
        found = symbol in text
        if path.suffix == ".py":
            try:
                found = symbol in defined_names(text)
            except SyntaxError:
                pass
        if not found:
            return "dest %s does not define the symbol %r" % (dest, symbol)
    return None


def placement(port_root: Path, descriptor: Any, dest: str, homes: "list[str]",
              bridges: "list[dict]") -> "str | None":
    """Why an `applied` dest is the wrong place for a src hunk, or None:
    outside every dist's import root (`non-dist-target`), the old door of a
    symbol `refactor move` moved away (`moved-slice`, from the bridge ledger), or
    not one of the files the ledger homes the hunk's upstream file in
    (`wrong-home`, when it homes it anywhere)."""
    from codegraph._core.layout import dist_import_root, dist_of_path

    rel = Path(dest).as_posix().lstrip("./")
    dist = dist_of_path(descriptor, rel)
    if dist is None or (descriptor.packages_dir
                        and not rel.startswith(dist_import_root(descriptor, dist) + "/")):
        return "non-dist-target: %s is not under a dist's import root" % rel
    moved = sorted({str(b.get("dst_path") or b.get("dst_module")) for b in bridges
                    if b.get("src_path") == rel})
    if moved:
        return "moved-slice: %s is the old door of a moved symbol — land it in %s" % (
            rel, ", ".join(moved))
    if homes and rel not in homes:
        return "wrong-home: the ledger homes this file in %s, not %s" % (", ".join(homes), rel)
    return None


def disposition(item: str, status: str, **fields: "str | int | None") -> dict:
    from hook_runtime import resolve_actor
    from port_ledger_writer import now_ts

    line = {"hunk": item, "status": status, **{k: v for k, v in fields.items() if v}}
    line.setdefault("actor", resolve_actor())
    line.setdefault("ts", now_ts())
    return line


def append_dispositions(path: Path, lines: "list[dict]") -> None:
    """The one write door onto the sidecar: every line validated first, then
    appended; nothing already there is rewritten."""
    for line in lines:
        why = validate_line(line)
        if why:
            raise hs_family.UsageError("disposition for %s: %s" % (line.get("hunk"), why))
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def zone(rows: "list[dict]", symbols: dict, modules: "dict[str, tuple[str, str]]",
         rules: dict, resolve_dist) -> dict:
    """Every src `.py` file and every changed symbol -> a dist through `rules`."""
    def dist_of(path: str, symbol: "str | None") -> str:
        unit = modules.get(path, ("", ""))[0]
        return resolve_dist(path, unit, symbol, rules)[0]

    per_dist: "dict[str, Counter]" = defaultdict(Counter)
    unruled: "list[str]" = []
    for r in rows:
        if r["area"] != "src" or not r["path"].endswith(".py"):
            continue
        r["dist"] = dist_of(r["path"], None)
        per_dist[r["dist"]]["files_" + r["status"]] += 1
        if r["dist"] == UNRULED and r["status"] in "AMR":
            unruled.append(r["path"])
    for kind in ("changed", "added", "removed"):
        for name, path in symbols[kind]:
            per_dist[dist_of(path, name)][kind] += 1
    return {"per_dist": {k: dict(sorted(v.items())) for k, v in sorted(per_dist.items())},
            "unruled_files": sorted(unruled)}


def split_files(rows: "list[dict]", modules: "dict[str, tuple[str, str]]",
                homes: "dict[str, Counter]") -> "list[dict]":
    """Changed src files whose symbols the ledger ported into several modules
    (`homes` = `port_refs.Index.places`: old module -> target module counts)."""
    out = []
    for r in rows:
        mod = modules.get(r["old"] or r["path"], ("", ""))[1]
        if r["area"] == "src" and len(homes.get(mod, ())) > 1:
            out.append({"path": r["path"], "status": r["status"], "homes": sorted(homes[mod])})
    return sorted(out, key=lambda h: h["path"])
