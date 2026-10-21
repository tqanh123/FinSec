"""QueryEngine — the generic verbs over one (or two) loaded snapshots.

Port of the frozen tool's `query.py` measurement semantics. Every verb
returns a `QueryResult` carrying BOTH the human text and a JSON-ready `data`
payload — the CLI layer prints `text` under `--text` or serializes `data` into the envelope,
one source, never two formats drifting apart.

Unit keys are ROOT-PREFIXED (`<root-id>/<unit>`). Verbs accept
either the full key or a bare unit name; a bare name matching several roots is
an ERROR listing the candidates, never a silent pick.

WHAT IT CANNOT SEE
    * Inherits every snapshot blind spot (see `codegraph._core.adapter`): `callers` is
      a lower bound, dynamic symbols are absent, comment-only changes are
      invisible to `sha`.
    * `changed` matches symbols BY NAME across two snapshots: a rename shows
      up as a removed+added pair, never "renamed" — no guessing.
    * `trace` links old and new trees by symbol NAME; a moved-and-renamed
      symbol degenerates to removed+added. Payload fields (dist/tier) are read
      as CARRIED data — absent payload reads as unattributed, not as "none".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import regex

import codegraph_change_reader
from .. import regex_bound
from ambiguous_input import Ambiguous, NotFound
from . import mentions, symbol_uses
from ..lang.ids import PYTHON, is_path_id, lang_of
from ..layout import deepest_root, flat_leaf_index, spellings_of
from ..pathtail import tail_matches
from ..units import module_unit_map, resolve_unit, select_units, unit_of_dotted

# `where` runs a command-line regex against every symbol name, so a catastrophic
# pattern would hang the verb; `regex_bound` bounds each match AND the whole
# scan where `re` (and a per-call-only bound) cannot — see that module's own
# docstring for the measured projection this closes.


@dataclass
class QueryResult:
    code: int                       # 0 answered · 1 a search found nothing · 2 cannot run
    text: str
    data: Any = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def refused(cls, amb: Ambiguous) -> "QueryResult":
        return cls(2, str(amb), amb.payload)


def _module_symbol_pairs(targets: list) -> list[dict]:
    """`<module>` or `<module>::<symbol>` targets (`Graph.dist_of_target`'s own
    shape, see `importers`'s `base_module`) as TWO fields, never a mixed
    string sitting in a module-list position — a caller closing over `module`
    (a file-import closure) must not silently drop a target that carries a
    symbol: that string never equals a real module path, so it dead-ends the
    closure instead of erroring."""
    out: list[dict] = []
    for t in targets:
        mod, sep, sym = t.partition("::")
        out.append({"module": mod, "symbol": sym if sep else None})
    return out


# `walk_symbols` MOVED to `codegraph_change_reader` (a flat module outside
# this package, so `hs_run_facts.code_callers` and its siblings can read the
# symbol table without importing the whole measurement engine — see that
# module's own docstring). Rebound to the SAME function object, not
# reimplemented beside it: the identical pattern `codegraph.change.
# selection.import_channel` already uses for its own move. `walk_symbols is
# codegraph_change_reader.walk_symbols` holds — one definition.
walk_symbols = codegraph_change_reader.walk_symbols


class QueryEngine:
    """Read-only queries over a snapshot index (+ optional second index)."""

    def __init__(self, index: dict, snapshot_dir: Path | None = None):
        self.idx = index
        self.dir = snapshot_dir

    # ── unit-key resolution (full "<root>/<unit>" or bare "<unit>") ─────────

    def resolve_unit(self, name: str) -> str:
        """The full unit key for `name`. Bare names resolve only when UNIQUE —
        two roots can both have a `graph` unit and picking one silently would
        measure the wrong half of the tree.

        Raised as `LookupError` because that is what this engine's callers
        already catch; the rule itself lives in `codegraph._core.units`.
        """
        try:
            return resolve_unit(self.idx, name)
        except Ambiguous:
            raise
        except ValueError as e:
            raise LookupError(str(e)) from None

    #: The unit label a `test_modules` record answers to: a test-tree file no
    #: root measures is a graph node for reverse walks, never a unit's code.
    TEST_UNIT = "test"

    def _records(self) -> "Iterable[tuple[str, dict]]":
        """(unit, module record) for every unit's code, then every
        `test_modules` record under `TEST_UNIT`."""
        for unit, mods in self.idx["units"].items():
            for m in mods:
                yield unit, m
        for m in self.idx.get("test_modules") or ():
            yield self.TEST_UNIT, m

    def _symbols(self) -> "Iterable[tuple[str, dict, dict, str | None]]":
        """`walk_symbols`'s shape over `_records`: test-tree symbols included."""
        for unit, m in self._records():
            for sym in m.get("symbols", ()) or ():
                yield unit, m, sym, None
                for mm in sym.get("methods", []) or []:
                    yield unit, m, mm, sym["name"]

    def _workspace_dist(self, unit: str, path: str) -> "str | None":
        """A `kind: workspace` root's unit IS a dist directory: its name, when
        no payload carried a dist. A file no unit measures (a test tree, a
        non-Python file) sits in the dist directory its path is under."""
        roots = [SimpleNamespace(**r) for r in self.idx.get("roots", [])]
        if unit in self.idx["units"]:
            root_id, _, name = unit.partition("/")
            kinds = {r.id: r.kind for r in roots}
            return name if kinds.get(root_id) == "workspace" and name and "/" not in name else None
        parts = tuple(p for p in path.split("/") if p not in ("", "."))
        hit = deepest_root(roots, parts)
        if hit is None or hit[0].kind != "workspace" or hit[1] + 1 >= len(parts):
            return None
        return parts[hit[1]]

    # ── verbs ───────────────────────────────────────────────────────────────

    def where(self, pattern: str, *, case_sensitive: bool = False) -> QueryResult:
        """Where does this symbol live NOW (file, lines, sha, assigned group).

        `pattern` is searched (unanchored) in each symbol's name and in a
        method's bare name, ignoring case unless `case_sensitive`.

        Bounded on TWO axes (`regex_bound`): a per-symbol timeout AND a total
        deadline for the whole scan — a pattern that stays just under the
        per-call bound on every single name still has to answer for the
        whole snapshot's worth of symbols, not just one."""
        try:
            pat = regex_bound.compile_bounded(pattern, 0 if case_sensitive else regex.I)
        except regex.error as e:
            raise ValueError("not a regex: %r (%s)" % (pattern, e)) from None
        rows: list[dict] = []
        lines: list[str] = []
        budget = regex_bound.ScanBudget()
        searched = 0
        for unit, m, s, parent in self._symbols():
            searched += 1
            try:
                # A method is stored `Class.meth`; its bare name is also its name.
                hit = regex_bound.bounded_search(pat, s["name"], budget) or (
                    parent is not None and "." in s["name"]
                    and regex_bound.bounded_search(pat, s["name"].rsplit(".", 1)[1], budget))
            except regex_bound.PatternBudgetExceeded as e:
                raise ValueError(str(e)) from None
            except TimeoutError:
                raise ValueError("pattern %r ran past %.0fs on %r — simplify it"
                                 % (pattern, regex_bound.PATTERN_TIMEOUT_SECONDS,
                                    s["name"])) from None
            if not hit:
                continue
            dist = s.get("dist") or self._workspace_dist(unit, m["path"]) or "—"
            rows.append({"name": s["name"], "kind": s.get("kind", "method"),
                         "lines": s["lines"], "sha": s["sha"], "file": m["path"],
                         "module": m["module"], "unit": unit, "dist": dist,
                         "parent": parent})
            lines.append(s["name"])
            lines.append(f"   kind  {s.get('kind', 'method')}   lines {s['lines'][0]}–{s['lines'][1]}   sha {s['sha']}")
            lines.append(f"   file  {m['path']}")
            lines.append(f"   unit  {unit}   dist {dist}")
            if parent:
                lines.append(f"   in class {parent}")
        lines.append(f"\n{len(rows)} result(s) for /{pattern}/")
        notes = [] if rows else [f"no symbol name matches /{pattern}/: {searched} symbol(s) "
                                 f"searched ({'case-sensitive' if case_sensitive else 'ignoring case'})"]
        return QueryResult(0 if rows else 1, "\n".join(lines), {"pattern": pattern, "rows": rows},
                           notes)

    def files_matching(self, path: str) -> list[tuple[str, dict]]:
        """(unit, module) for every file whose path IS `path` or ends in `/` + `path`:
        whole path segments only, so `store.py` never names `findings_store.py`."""
        return [(unit, m) for unit, m in self._records() if tail_matches(m["path"], path)]

    def file(self, path: str) -> QueryResult:
        """What does this file hold: symbols, imports (module-level vs lazy). `path`
        names one file by whole trailing segments (`files_matching`); several files
        refuse with exit 2 and every candidate, never the first one found."""
        hits = self.files_matching(path)
        if len(hits) > 1:
            return QueryResult.refused(Ambiguous("path", path, (m["path"] for _, m in hits),
                                                 "pass more of the path"))
        for unit, m in hits:
                nm = sum(1 for s in m["symbols"] for _ in [s] + s.get("methods", []))
                L = [m["path"],
                     f"  module {m['module']}   unit {unit}   {m['loc']} lines   {m['bytes']} B   sha {m['file_sha']}",
                     f"  imports module-level : {', '.join(m['imports_module_level']) or '—'}",
                     f"  imports in functions : {', '.join(m['imports_function_local']) or '—'}",
                     f"  external             : {', '.join(m['imports_external']) or '—'}",
                     f"  {nm} entries:"]
                for s in m["symbols"]:
                    L.append(f"    {s['kind']:6} {s['name']:44} {s['lines'][0]:>6}–{s['lines'][1]:<6} {s['sha']}")
                    for mm in s.get("methods", []):
                        L.append(f"    {'  →':6} {mm['name']:44} {mm['lines'][0]:>6}–{mm['lines'][1]:<6} {mm['sha']}")
                data = {"file": m["path"], "module": m["module"], "unit": unit,
                        "loc": m["loc"], "bytes": m["bytes"], "file_sha": m["file_sha"],
                        "imports_module_level": _module_symbol_pairs(m["imports_module_level"]),
                        "imports_function_local": _module_symbol_pairs(m["imports_function_local"]),
                        "imports_external": m["imports_external"],
                        "entries": nm,
                        "symbols": [{"kind": x.get("kind", "method"), "name": x["name"],
                                     "lines": x["lines"], "sha": x["sha"],
                                     "methods": [{"name": y["name"], "lines": y["lines"],
                                                  "sha": y["sha"]}
                                                 for y in x.get("methods", [])]}
                                    for x in m["symbols"]]}
                return QueryResult(0, "\n".join(L), data)
        return QueryResult(1, f"no file matches {path!r}")

    def unit(self, name: str) -> QueryResult:
        """Unit digest: modules, entries, in/out edges, cycle partners."""
        try:
            key = self.resolve_unit(name)
        except Ambiguous as e:
            return QueryResult.refused(e)
        except LookupError as e:
            return QueryResult(1, str(e))
        mods = select_units(self.idx, key)
        ent = sum(len(m["symbols"]) + sum(len(s.get("methods", [])) for s in m["symbols"])
                  for m in mods)
        # An edge names the unit its target is LISTED under (longest module
        # prefix), never a dotted segment: `vsf.di.<x>` split over dists gives
        # every module the segment `di` and no unit of that name. Imports
        # inside the unit are no edge of it; a target no measured module
        # matches is kept by name in `out_unresolved`, not dropped.
        mod_unit = module_unit_map(self.idx)
        unresolved: set[str] = set()

        def targets(imports: Iterable[str], lang: "str | None") -> set[str]:
            out: set[str] = set()
            for x in imports:
                u = unit_of_dotted(x, mod_unit, lang)
                if u is None:
                    unresolved.add(x.split("::", 1)[0])
                elif u != key:
                    out.add(u)
            return out

        def importers(field: str) -> set[str]:
            return {u for u, ms in self.idx["units"].items() if u != key
                    for m in ms if any(unit_of_dotted(x, mod_unit, m.get("lang")) == key for x in m[field])}

        out_m, out_f = set(), set()
        for m in mods:
            out_m |= targets(m["imports_module_level"], m.get("lang"))
            out_f |= targets(m["imports_function_local"], m.get("lang"))
        inn_m = importers("imports_module_level")
        inn_f = importers("imports_function_local")
        cyc = sorted((out_m | out_f) & (inn_m | inn_f))
        L = [f"unit {key}: {len(mods)} module(s), {ent} entries, {sum(m['loc'] for m in mods)} lines",
             f"  out (module) {len(out_m):3}: {', '.join(sorted(out_m)) or '—'}",
             f"  out (lazy)   {len(out_f):3}: {', '.join(sorted(out_f)) or '—'}",
             f"  in  (module) {len(inn_m):3}: {', '.join(sorted(inn_m)) or '—'}",
             f"  in  (lazy)   {len(inn_f):3}: {', '.join(sorted(inn_f)) or '—'}"]
        if unresolved:
            L.append(f"  unresolved   {len(unresolved):3}: {', '.join(sorted(unresolved))}")
        if cyc:
            L.append(f"  ⚠ CYCLES with: {', '.join(cyc)}")
        for m in sorted(mods, key=lambda m: -m["bytes"]):
            L.append(f"    {m['bytes']:>8} B  {m['loc']:>6} lines  {m['path']}")
        data = {"unit": key, "modules": len(mods), "entries": ent,
                "out_module": sorted(out_m), "out_lazy": sorted(out_f),
                "in_module": sorted(inn_m), "in_lazy": sorted(inn_f), "cycles": cyc,
                "out_unresolved": sorted(unresolved)}
        return QueryResult(0, "\n".join(L), data)

    def grep(self, repo: Path, pattern: str, *, kinds: "Iterable[str] | None" = None,
             top: int = 50, ignore_case: bool = False, copies: Any = None,
             include_copies: bool = False) -> QueryResult:
        """Every match of `pattern` in the tracked tree, one row per match, typed
        by where it sits (`mentions.scan`). Each row names its file's unit and
        role from this snapshot (`test` for a test-tree file; `—` for a file no
        root measures). Counts per kind cover every match; `rows` stop at `top`.

        `copies` (the descriptor's `CopiesSpec`) names generated views and backups:
        skipped unless `include_copies`, their count by kind in `skipped_copies` and
        a note -- a skip nobody is told of reads as "no match there". Read anyway,
        each row's `copy` names the kind (`None` for a live file)."""
        by_path = {m["path"]: (unit, m["module"]) for unit, m in self._records()}
        kind_of = copies.kind_of if copies else (lambda rel: None)
        files: "list[str]" = []
        skipped: "dict[str, int]" = {}
        for rel in mentions.tracked_files(repo):
            k = kind_of(rel) if copies and not include_copies else None
            if k is None:
                files.append(rel)
            else:
                skipped[k] = skipped.get(k, 0) + 1
        hits, notes = mentions.scan(repo, pattern, kinds=kinds, ignore_case=ignore_case,
                                    module_of=lambda p: by_path.get(p, (None, None))[1],
                                    files=files)
        if not hits:
            notes.append(f"no match for /{pattern}/: {len(files)} tracked file(s) searched")
        if skipped:
            notes.append("skipped %d file(s) codemap.yaml declares as copies (%s) -- "
                          "--include-copies reads them"
                          % (sum(skipped.values()),
                             ", ".join(f"{k} {n}" for k, n in sorted(skipped.items()))))
        by_kind: "dict[str, int]" = {}
        for h in hits:
            unit = by_path.get(h["file"], ("—", None))[0]
            h["unit"] = unit
            h["dist"] = self._workspace_dist(unit, h["file"])
            h["copy"] = kind_of(h["file"])
            by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
        rows = hits[:top]
        L = [f"{len(hits)} match(es) for /{pattern}/: "
             + (", ".join(f"{k} {by_kind[k]}" for k in mentions.KINDS if k in by_kind) or "none")]
        for h in rows:
            where = h["enclosing"] or h["context"] or ""
            if h["copy"]:
                where = f"[{h['copy']} copy] {where}"
            L.append(f"  {h['file']}:{h['line']}  {h['kind']:9} {where}")
            L.append(f"      {h['snippet']}")
        if len(hits) > top:
            L.append(f"  … {len(hits) - top} more (--top)")
        data = {"pattern": pattern, "total": len(hits), "by_kind": by_kind,
                "truncated": len(hits) > top, "rows": rows, "skipped_copies": skipped}
        return QueryResult(0 if hits else 1, "\n".join(L), data, notes)

    def deps(self, name: str) -> QueryResult:
        """Alias of `unit` (kept as its own verb — same answer, familiar door)."""
        return self.unit(name)

    def resolve_module(self, name: str) -> "tuple[str, str | None]":
        """`(module, note)` for a dotted name a reader typed. A module this snapshot
        holds, or a package some module sits under (`pkg` of `pkg.sub`, with or
        without an `__init__.py`), is itself. Otherwise `name` is read as a tail on
        whole dotted segments (`golden_replay` names `scripts.golden_replay.
        golden_replay`, never `x.test_golden_replay`), the rule `graph file` applies
        to `/` segments: one module resolves, with a note saying so; several are
        `Ambiguous` with every candidate; none is `NotFound` -- never an empty table
        that reads as "no importers"."""
        records = [m for _, m in self._records()]
        names = {m["module"] for m in records}
        # by the record's own `lang`, never the string: a Python `app/ts.py` is `app.ts`
        paths = {m["module"] for m in records if lang_of(m) != PYTHON}
        dotted = names - paths
        if name in names:
            return name, None
        # A path id is a file, never a package: no strict prefix of one is a module
        # (`lang.ids`), and its tail is read on whole `/` segments.
        typed_path = is_path_id(name, {Path(p).suffix.lower() for p in paths})
        if not typed_path and any(n.startswith(name + ".") for n in dotted):
            return name, None
        hits = sorted(n for n in dotted if n.endswith("." + name)) \
            + sorted(n for n in paths if n.endswith("/" + name))
        if len(hits) > 1:
            raise Ambiguous("module", name, hits, "pass more of the name")
        if not hits:
            raise NotFound(f"no module {name!r} in this snapshot, and no module whose "
                           f"dotted name ends in .{name} or whose path ends in /{name}",
                           "module", name)
        return hits[0], (f"{name!r} is not a module name in this snapshot; read it as "
                         f"{hits[0]!r}, the one module whose name ends in it")

    def importers(self, module: str, depth: int = 1, exact: bool = False) -> QueryResult:
        """Fan-in table: every module that imports `module`, exact dotted-name
        match (module-level and function-local both count). `module` is read
        through `resolve_module`, so a unique tail answers for its module.

        A PACKAGE (a name other modules sit under) also counts the importers of
        each submodule: `from pkg import sub` is an edge to `pkg.sub`, so the
        package module alone would list almost nobody. Each row's `reaches`
        names the member its descent ends at; imports between the package's
        own members are not listed (they are the package, not its users).
        `exact` restricts the answer to the package module itself. `depth` follows
        the reverse edge one hop at a time — depth 1 (default) is direct
        importers only, depth 2 adds importers-of-importers, and so on. Each
        row's `via` is the DESCENT PATH from that row's own module down to
        `module`: `depth` 1 rows carry `[module]` (their own edge IS the
        target); `depth` 2 rows carry `[intermediate, module]`, so a reader
        can check the intermediate is itself a `depth` 1 importer without a
        second query — this stays a fan-in table, not a lineage tracker;
        `query trace` is the tool for that.

        A test-tree file no root measures (`test_modules`) is an importer like
        any other, its `unit` reading `TEST_UNIT`.

        A row whose edge crosses a package boundary carries that edge's
        `import_flags` as `flags` (`lang.typescript.boundary`); no other row has the key.

        WHAT IT CANNOT SEE
            * Exact dotted-name match only: an alias (`import pkg.mod as m`)
              or a re-export surfaced under a different public name is
              invisible here — the same blind spot import edges carry
              everywhere else in this engine.
            * A module reachable through more than one path is attributed to
              the depth it is FIRST reached at (`seen`); a shorter path found
              later at the same depth still gets its own row, but a longer,
              redundant path to an already-seen module does not. The `via`
              path recorded for one module's OWN descent (used to build a
              deeper row's path) is likewise the first one found, not every
              path that exists.
        """
        if depth < 1:
            return QueryResult(2, f"importers: --depth must be >= 1, got {depth}")
        query = module
        module, resolved_note = self.resolve_module(module)
        notes = [resolved_note] if resolved_note else []

        def base_module(tgt: str) -> str:
            # `from pkg.mod import name` targets are stored `pkg.mod::name`
            # (Graph.dist_of_target's own convention) — a bare `import pkg.mod`
            # target carries no `::` at all.
            return tgt.split("::", 1)[0]

        meta: dict[str, tuple[str, str]] = {}     # module -> (unit, path)
        edges: dict[str, set[str]] = {}           # importer module -> imported dotted names
        flags_of: dict[str, list[dict]] = {}      # importer module -> its `import_flags`
        for unit, m in self._records():
            meta[m["module"]] = (unit, m["path"])
            if m.get("import_flags"):
                flags_of[m["module"]] = m["import_flags"]
            edges[m["module"]] = ({base_module(t) for t in m["imports_module_level"]}
                                  | {base_module(t) for t in m["imports_function_local"]})

        members = set() if exact else {n for n in meta if n.startswith(module + ".")}
        targets = {module} | members

        def descent_path(target: str) -> list[str]:
            # `target` is what THIS importer directly imports. If that IS the
            # queried module (or one of its package's members), the path ends
            # there; otherwise it is one more hop's own recorded descent, prepended.
            return [target] if target in targets else [target] + via_of[target]

        rows: list[dict] = []
        via_of: dict[str, list[str]] = {}   # importer module -> its own descent path
        frontier = set(targets)
        seen = set(targets)
        for level in range(1, depth + 1):
            next_frontier: set[str] = set()
            for importer_mod, imported in edges.items():
                if importer_mod in seen:
                    continue
                hit = imported & frontier
                if not hit:
                    continue
                unit, path = meta.get(importer_mod, ("—", "—"))
                for target in sorted(hit):
                    via = descent_path(target)
                    row = {"module": importer_mod, "unit": unit, "file": path,
                           "depth": level, "via": via, "reaches": via[-1]}
                    crossing = [f for f in flags_of.get(importer_mod, ()) if base_module(f["to"]) == target]
                    if crossing:
                        row["flags"] = crossing
                    rows.append(row)
                # One descent recorded per module (the first target, sorted) —
                # it only feeds the NEXT hop's path, never a second row here.
                via_of[importer_mod] = descent_path(sorted(hit)[0])
                next_frontier.add(importer_mod)
            seen |= next_frontier
            frontier = next_frontier
            if not frontier:
                break

        rows.sort(key=lambda r: (r["depth"], r["module"]))
        distinct = sorted(seen - targets)
        if members:
            notes.append(f"{module!r} is a package: the importers of its {len(members)} "
                         f"submodule(s) are counted too (each row's `reaches` names the one "
                         f"it imports); imports between its own submodules are not listed; "
                         f"--exact answers for the package module alone")
        L = [f"importers of {module!r} (depth {depth}): {len(rows)} row(s), "
             f"{len(distinct)} distinct module(s)"]
        for r in rows:
            L.append(f"  depth {r['depth']}  {r['module']:44} unit {r['unit']}  "
                     f"via {' -> '.join(r['via'])}")
        if not distinct:
            L.append("  (no importers found — exact dotted-name match only; an "
                     "alias or re-export is invisible here)")
            notes.append(f"no module imports {module!r}: {len(edges) - len(targets)} "
                         f"module(s) scanned")
        data = {"module": module, "query": query, "depth": depth, "exact": exact,
                "package_members": len(members), "rows": rows,
                "distinct_importers": distinct}
        return QueryResult(0, "\n".join(L), data, notes)

    def _importers_using_pack(self, module: str, symbol: str, repo: Path, descriptor: Any, own: dict,
                              whole: "list[dict]", resolved_note: "str | None") -> QueryResult:
        """`importers_using` for a TypeScript module: each importer read by the pack."""
        from codegraph._core.lang.typescript import symbol_uses as ts_uses
        if descriptor is None:
            from codegraph._core.descriptor import load
            descriptor = load(repo / "codemap.yaml", repo).descriptor
        rows: list[dict] = []
        dropped: list[str] = []
        notes: list[str] = [resolved_note] if resolved_note else []
        with ts_uses.importer_lines(repo, own["path"], symbol, descriptor) as lines_of:
            for r in whole:
                try:
                    text: "str | None" = (repo / r["file"]).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    text = None
                if text is None:
                    notes.append(f"{r['file']}: could not be read -- kept, check it by hand")
                    rows.append({**r, "lines": None, "unresolved_dynamic": False})
                    continue
                lines = lines_of(r["file"], text)
                if lines:
                    rows.append({**r, "lines": lines, "unresolved_dynamic": False})
                else:
                    dropped.append(r["file"])
        return self._using_result(module, symbol, rows, dropped, notes, whole, [])

    def importers_using(self, module: str, symbol: str, repo: Path,
                        import_roots: "Iterable[str]" = (), descriptor: Any = None) -> QueryResult:
        """The DIRECT importers of `module` whose own text uses `symbol`
        (`symbol_uses.use_lines`), each with the lines. Depth 1 only: a caller of a
        caller is not a call site of `symbol`. Each importer is read from `repo` (the
        working tree). `module` is recognised by its own name, each name the
        descriptor's `import_roots` give its file, and its bare leaf when its flat
        root resolves that leaf to it alone (`layout.flat_leaf_index`, the snapshot's
        own rule).

        An importer that cannot be read or parsed is KEPT with `lines: null` and a
        note: dropping it would claim it never uses `symbol`, which nobody measured.
        So is one with no traced use that still writes `.symbol` on a receiver rooted in
        no import of the file (a parameter, a returned value): `unresolved_dynamic`
        true, its lines from `symbol_uses.attribute_lines`, and a note naming it.
        A module or symbol that is not in the snapshot is NotFound (exit 1), so a typo
        never reads as "no callers"; a unique tail resolves (`resolve_module`).
        Importers of the module ITSELF only: a package's submodules each define
        their own symbols. A module of another language is read by its pack
        (`typescript.symbol_uses`, with the engine `descriptor` declares)."""
        module, resolved_note = self.resolve_module(module)
        mods = {m["module"]: m for mods_ in self.idx["units"].values() for m in mods_}
        own = mods.get(module)
        if own is None:
            raise NotFound(f"importers: no module {module!r} in this snapshot", "module", module)
        lang = lang_of(own)
        if lang not in (PYTHON, "typescript"):
            from hs_family.errors import RefusedError
            raise RefusedError(f"importers --symbol: {module} is {lang}; symbol uses read python and "
                               f"typescript only in this build (language_unsupported) -- `graph "
                               f"importers {module}` lists its importers")
        bound = {t.partition("::")[2] or t.rsplit(".", 1)[-1]
                 for t in own["imports_module_level"] + own["imports_function_local"]}
        if symbol not in {s["name"] for s in own["symbols"]} | bound:
            raise NotFound(f"importers: {module!r} defines or imports no top-level {symbol!r}",
                           "symbol", symbol)
        whole = self.importers(module, depth=1, exact=True).data["rows"]
        if lang != PYTHON:
            return self._importers_using_pack(module, symbol, repo, descriptor, own, whole, resolved_note)
        rows: list[dict] = []
        dropped: list[str] = []
        dynamic: list[str] = []
        notes: list[str] = [resolved_note] if resolved_note else []
        spelled = set(spellings_of(own["path"], tuple(import_roots))) | {module}
        flat_dirs = {r["id"]: repo / r["path"] for r in self.idx.get("roots", [])
                     if r.get("kind") == "flat"}
        leaf = module.rsplit(".", 1)[-1]
        if flat_leaf_index(flat_dirs, ((m["root"], repo / m["path"], m["module"])
                                       for m in mods.values())).get(leaf) == [module]:
            spelled.add(leaf)
        for r in whole:
            try:
                text: "str | None" = (repo / r["file"]).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = None
            lines = None if text is None else symbol_uses.use_lines(
                text, r["module"], r["file"].endswith("__init__.py"), spelled, symbol)
            if lines is None:
                notes.append(f"{r['file']}: could not be read or parsed -- kept, check it by hand")
            elif not lines:
                untraced = symbol_uses.attribute_lines(text or "", symbol)
                if not untraced:
                    dropped.append(r["file"])
                    continue
                dynamic.append(r["file"])
                rows.append({**r, "lines": untraced, "unresolved_dynamic": True})
                continue
            rows.append({**r, "lines": lines, "unresolved_dynamic": False})
        return self._using_result(module, symbol, rows, dropped, notes, whole, dynamic)

    @staticmethod
    def _using_result(module: str, symbol: str, rows: "list[dict]", dropped: "list[str]",
                      notes: "list[str]", whole: "list[dict]", dynamic: "list[str]") -> QueryResult:
        if dynamic:
            notes.append(f"{len(dynamic)} importer(s) write `.{symbol}` on a receiver this "
                         f"tool cannot trace to {module!r} -- kept as unresolved_dynamic, "
                         f"check by hand: {', '.join(dynamic)}")
        L = [f"importers of {module!r} using {symbol!r} (depth 1): {len(rows)} file(s); "
             f"{len(dropped)} importer(s) never use it"]
        for r in rows:
            at = ",".join(map(str, r["lines"])) if r["lines"] else "?"
            flag = "  [unresolved receiver]" if r.get("unresolved_dynamic") else ""
            L.append(f"  {r['file']}:{at}  ({r['module']}){flag}")
        data = {"module": module, "symbol": symbol, "depth": 1, "rows": rows,
                "distinct_importers": sorted(r["module"] for r in rows),
                "dropped": dropped, "importers": len(whole)}
        if not whole:
            notes.append(f"{module!r} has no importers, so no file reaches {symbol!r} "
                         f"through it")
        elif not rows:
            notes.append(f"{len(whole)} importer(s) of {module!r} checked, none uses "
                         f"{symbol!r}")
        return QueryResult(0, "\n".join(L), data, notes)

    def changed(self, other: "QueryEngine", top: int = 25) -> QueryResult:
        """Which symbols CHANGED between two snapshots — THE porting question.
        Matching is by `sha` of the normalized body: moves, reformatting and
        comment edits do NOT register as change."""
        def flat(ix: dict) -> dict:
            return {(s["name"], m["path"]): s["sha"]
                    for _, m, s, _ in walk_symbols(ix)}

        A, B = flat(self.idx), flat(other.idx)
        ka, kb = set(A), set(B)
        changed = sorted(k for k in ka & kb if A[k] != B[k])
        added = sorted(kb - ka)
        removed = sorted(ka - kb)
        L = [f"{self.idx.get('snapshot')}  →  {other.idx.get('snapshot')}",
             f"  changed: {len(changed)}",
             f"  added  : {len(added)}",
             f"  removed: {len(removed)}",
             "  (a RENAME shows as removed+added — the tool does not guess)"]
        for label, rows in (("CHANGED", changed), ("ADDED", added), ("REMOVED", removed)):
            if not rows:
                continue
            L.append(f"\n{label}:")
            for name, path in rows[:top]:
                L.append(f"  {name:52} {path}")
            if len(rows) > top:
                L.append(f"  … {len(rows) - top} more (--top to see them)")
        data = {"a": self.idx.get("snapshot"), "b": other.idx.get("snapshot"),
                "changed": changed, "added": added, "removed": removed}
        notes = [] if (changed or added or removed) else [
            f"nothing changed, added or removed: {len(ka)} symbol(s) compared by body sha"]
        return QueryResult(0, "\n".join(L), data, notes)

    def unassigned(self, payload_key: str = "dist",
                   payload_keys: tuple[str, ...] = ("dist", "tier")) -> QueryResult:
        """Symbols the snapshot left WITHOUT a value for `payload_key`.

        The kernel does not know what a "dist" is: the key is a parameter, and
        the domain that owns the key passes it (specs/02 §4 keeps the verb in
        the kernel because "which symbols lack an attribute" is generic, while
        "which dist" is not).

        WHAT IT CANNOT SEE
            * A missing key and an explicitly-null key are the SAME answer here
              (both read as unassigned). A domain that means them differently
              must carry a sentinel value, not absence — `null` is "script did
              not run", `UNRULED` is "looked at, not decided".
            * A snapshot whose format never wrote the key at all reports EVERY
              symbol as unassigned. That is why the blind line below prints the
              total symbol count and how many lack the key: 100% unassigned is
              a format mismatch, not 6 000 findings.
            * It reads the symbol record only. A key present on the module but
              absent from the symbol (or the reverse) is not reconciled here.
        """
        per_unit: dict[str, int] = {}
        total = 0
        for unit, _m, s, _parent in walk_symbols(self.idx):
            total += 1
            if s.get(payload_key) is None:
                per_unit[unit] = per_unit.get(unit, 0) + 1
        found = sum(per_unit.values())
        L = [f"{found} item(s) with no `{payload_key}`, across {len(per_unit)} unit(s):"]
        for u, n in sorted(per_unit.items(), key=lambda kv: (-kv[1], kv[0])):
            L.append(f"  {n:6}  {u}")
        # Blind line: without the denominator, a total miss reads like a clean
        # sweep — the empty-cell trap the whole repo is built around.
        if total and found == total:
            L.append(f"  ⚠ ALL {total} symbols lack `{payload_key}` — the snapshot "
                     f"format most likely never wrote this key; this is not "
                     f"{found} findings.")
        else:
            L.append(f"  (of {total} symbols; {total - found} carry a `{payload_key}`)")
        data = {"payload_key": payload_key, "count": found, "total": total,
                "per_unit": dict(sorted(per_unit.items(), key=lambda kv: (-kv[1], kv[0])))}
        if not total:
            notes = [f"the snapshot holds 0 symbols -- nothing was measured, so no "
                     f"`{payload_key}` answer exists"]
        elif not found:
            notes = [f"all {total} symbol(s) carry a `{payload_key}`"]
        else:
            notes = []
        return QueryResult(0 if total else 1, "\n".join(L), data, notes)

    def big(self, top: int = 25) -> QueryResult:
        """Largest files and longest symbols."""
        files = [(m["bytes"], m["loc"], m["path"]) for ms in self.idx["units"].values() for m in ms]
        syms = [(s["lines"][1] - s["lines"][0], s["name"], m["path"])
                for _, m, s, _ in walk_symbols(self.idx)]
        L = [f"{top} largest files:"]
        for b, loc, p in sorted(files, reverse=True)[:top]:
            L.append(f"  {b / 1024:8.1f} KB  {loc:>6} lines  ~{b // 4:>7} tok  {p}")
        L.append(f"\n{top} longest symbols:")
        for n, name, p in sorted(syms, reverse=True)[:top]:
            L.append(f"  {n:6} lines  {name:44} {p}")
        data = {"files": sorted(files, reverse=True)[:top],
                "symbols": sorted(syms, reverse=True)[:top]}
        return QueryResult(0, "\n".join(L), data)

    def stats(self) -> QueryResult:
        """Snapshot measurements (commit, totals, roots)."""
        L = [f"snapshot : {self.idx.get('snapshot')}",
             f"commit   : {self.idx.get('commit')}"]
        for k, v in self.idx.get("totals", {}).items():
            L.append(f"{k:22}: {v}")
        L.append(f"roots    : {', '.join(r['id'] + '(' + r['kind'] + ')' for r in self.idx.get('roots', []))}")
        return QueryResult(0, "\n".join(L),
                           {"snapshot": self.idx.get("snapshot"),
                            "commit": self.idx.get("commit"),
                            "totals": self.idx.get("totals", {}),
                            "roots": self.idx.get("roots", [])})

    def trace(self, symbol: str, other: "QueryEngine",
              payload_keys: tuple[str, ...] = ("dist", "tier")) -> QueryResult:
        """Link one symbol across TWO snapshots (old tree -> new tree) at four
        levels: file, symbol, group, tier. The porting-work question — and the
        first verb the kernel absorbed because it is pure graph.

        Blind spots: rename degenerates to removed+added (no guess); payload
        fields absent from a snapshot read as unattributed; callers are a
        lower bound (inherited)."""
        def find(ix: dict) -> list[dict]:
            rows = []
            for unit, m, s, parent in walk_symbols(ix):
                if s["name"] == symbol or s["name"].endswith("." + symbol):
                    rows.append({"name": s["name"], "file": m["path"],
                                 "module": m["module"], "unit": unit,
                                 "sha": s["sha"],
                                 **{k: (s.get(k) if k in s else None)
                                    for k in payload_keys}})
            return rows

        a_rows, b_rows = find(self.idx), find(other.idx)
        if not a_rows and not b_rows:
            return QueryResult(1, f"symbol {symbol!r} not found in either snapshot")
        L = [f"trace {symbol!r}: {len(a_rows)} hit(s) in {self.idx.get('snapshot')}, "
             f"{len(b_rows)} in {other.idx.get('snapshot')}",
             "  (a rename across trees shows as gone+new — not linked)"]
        data: dict[str, Any] = {"symbol": symbol, "old": a_rows, "new": b_rows}
        for label, rows in ((self.idx.get("snapshot", "A"), a_rows),
                            (other.idx.get("snapshot", "B"), b_rows)):
            for r in rows:
                payload = " ".join(f"{k}={r.get(k)}" for k in payload_keys if r.get(k) is not None)
                L.append(f"  [{label}] {r['module']}::{r['name']}  {r['file']}  unit {r['unit']}"
                         + (f"  {payload}" if payload else "  (no payload)"))
        if len(a_rows) == 1 and len(b_rows) == 1 and a_rows[0]["sha"] == b_rows[0]["sha"]:
            L.append("  sha IDENTICAL across trees — body unchanged since the fork point")
        elif a_rows and b_rows:
            L.append("  sha DIFFERS (or several candidates) — compare bodies before porting decisions")
        return QueryResult(0, "\n".join(L), data)
