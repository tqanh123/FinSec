"""hs-code's `graph` domain — the 9 query leaves ported from the reference
tool's `query.py`: where, file, unit, importers, changed, big, unassigned,
stats, trace — plus `tests`, the merged door replacing the
ported tool's own `query tests` (file level) and the reference `testkit`'s
`test map` (function level); its measurement logic lives in
`codegraph._core.testmap`, this module only resolves+prints, same split as
every other verb here.

Each verb is a `Command` constant (`add_args`/`run`) named by its registry row's
`entry:`; the family core parses the declared argv and hands `run` a namespace
carrying `ctx.root` (the target repo) and `opts` (`hs_code.ShellOptions`, the
shell's own flags). `--snap REF` and `--out DIR` are each verb's OWN
flags (never the shell's) — same convention `--depth`/`--top`/`--key` follow.

FRESHNESS, exactly the ported `run_query`'s law (`codegraph._core.snapshot.
freshness.check_freshness`): the primary snapshot (`--snap`, default latest)
is refused when its commit differs from HEAD over the descriptor's declared
roots, unless `--allow-stale` (shell flag) turns the refusal into a warning.
`--fresh` (shell flag) measures the on-disk tree instead, once per content in this
worktree's own base (`fresh_filed`); `--fresh` with `--snap` is a usage error (both name what to
measure). A verb taking a SECOND snapshot ref (`changed`/`trace`'s `SNAP_B`
positional) resolves it directly, with no freshness check of its own —
mirrors the ported CLI: only the PRIMARY answers "is this today's tree".
The resolve step itself (load + freshness check, and the `--fresh` scratch
measurement) lives in `codegraph_graph_freshness.py` — shared with
`codegraph/graph/rules.py`'s `rules`/`drift` verbs so the law is not
carried twice; `_run_query` below still owns the surrounding "which snapshot
directory, then call+emit" shape, which differs per caller.

The verb answers with its `data` as the envelope's `data` and every note (the
verb's own, and a freshness warning) as the envelope's `warnings`; `--text`
prints `res.text`, warnings/notes to stderr. Exit
is the `QueryResult`'s own code (0 answered · 1 a search found nothing · 2
cannot run). A measured empty answer (`importers`, `changed`, `unassigned`) is
exit 0 with a note naming what was scanned; a search (`where`, `grep`) keeps
grep's exit 1 on no match, with the same note;
a usage error (bad flag, wrong arity) is also exit 2, message on stderr,
never a traceback.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

_SCRIPTS = Path(__file__).resolve().parents[2]   # codegraph/<pkg>/x.py -> scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import codegraph_snapshot_reader  # noqa: E402
from codegraph._core import testmap  # noqa: E402
from ambiguous_input import Ambiguous, NotFound  # noqa: E402
from codegraph._core.descriptor import DescriptorError, load  # noqa: E402
from codegraph._core.query.engine import QueryEngine, QueryResult  # noqa: E402
from codegraph._core.snapshot.store import (SnapshotMissing, load_dir,  # noqa: E402
                                            resolve_snapshot)
from codegraph_graph_freshness import SnapshotResolveError, resolve_idx  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command, Outcome, state_of  # noqa: E402
import hs_family  # noqa: E402


def _add_snap_out(ap: argparse.ArgumentParser) -> None:
    """`--snap`/`--out`: shared by every query verb below — the primary
    snapshot to read (default: latest) and where a `--fresh` measurement
    would be filed."""
    ap.add_argument("--snap", default=None, metavar="REF",
                    help="the snapshot to read (default: latest in the store)")
    ap.add_argument("--out", default=None, metavar="DIR",
                    help="where a --fresh measurement is filed")


# ── the shared engine: resolve (+ freshness), call, emit ────────────────────

def _emit(res: QueryResult, opts: Any, warnings: "list[str]") -> "int | Outcome":
    """JSON mode: the envelope carries `data` and every note as a warning.
    `--text`: the table on stdout, notes on stderr."""
    if getattr(opts, "json", False):
        return Outcome(exit_code=res.code, state=state_of(res.code), data=res.data,
                       warnings=(*warnings, *res.notes))
    print(res.text)
    for n in res.notes:
        print(f"⚠ {n}", file=sys.stderr)
    return res.code


def _other_engine(base: Path, ref: str) -> QueryEngine:
    """A SECOND snapshot under the same store base, no freshness check — the
    ported CLI only ever checks the PRIMARY snapshot against HEAD. `ref` is
    always a caller-supplied positional (`changed SNAP_B`, `trace SYM
    SNAP_B`) here, never `None` — `store.resolve_current`'s "match the
    CURRENT tree" law does not apply: the whole point of naming a SECOND
    snapshot is comparing two specific, EXPLICITLY chosen points, neither
    of which is "latest"."""
    snap_dir = resolve_snapshot(base, ref)
    return QueryEngine(load_dir(snap_dir), snap_dir)


def _pair(eng: QueryEngine, other: QueryEngine) -> "tuple[str, str]":
    """How a two-snapshot warning names its sides: the primary, then `SNAP_B`."""
    return (f"snapshot {eng.idx.get('snapshot')}", f"SNAP_B {other.idx.get('snapshot')}")


def _run_query(root: Path, opts: Any, snap: "str | None", out: "str | None",
               verb: str, call: "Callable[[QueryEngine, Path], QueryResult]") -> "int | Outcome":
    descriptor_path = root / "codemap.yaml"
    try:
        loaded = load(descriptor_path, root)
    except DescriptorError as e:
        return Verdict.broken(str(e)).exit
    scratch: "Path | None" = None
    warnings: "list[str]" = []
    try:
        try:
            base, snap_dir, idx, scratch = resolve_idx(root, loaded.descriptor, opts, snap, out, verb,
                                                       fresh=bool(getattr(opts, "fresh", False)),
                                                       warnings=warnings)
        except (Ambiguous, NotFound) as e:
            return e.refuse(opts)
        except SnapshotResolveError as e:
            return Verdict.broken(str(e)).exit
        assert base is not None  # need_store: every query verb may read the store
        eng = QueryEngine(idx, snap_dir)
        try:
            result = call(eng, base)
        except (Ambiguous, NotFound) as e:
            return e.refuse(opts)
        except (SnapshotMissing, ValueError) as e:
            return Verdict.broken(str(e)).exit
        return _emit(result, opts, warnings)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


# ── the 9 verbs + `tests` ────────────────────────────────────────────────────

def _where_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("pattern", nargs="?", default=None,
                    help="regex searched anywhere in a symbol's name (and a method's bare "
                         "name), case-insensitive unless --case-sensitive; anchor with ^...$ "
                         "for a whole name (or pass it as --symbol)")
    ap.add_argument("--symbol", default=None, metavar="PATTERN",
                    help="the same regex, spelled as `importers --symbol` spells a symbol")
    ap.add_argument("--case-sensitive", action="store_true",
                    help="match case exactly, so a class and a function differing only in "
                         "case are told apart (default: case-insensitive)")


def _where(args: argparse.Namespace) -> "int | Outcome":
    """`where PATTERN` / `where --symbol PATTERN` — where a symbol matching `PATTERN`
    (regex, searched case-insensitively unless `--case-sensitive`) lives now. One
    spelling or the other; two different patterns, or none, is a usage error."""
    given = [p for p in (args.pattern, args.symbol) if p is not None]
    if not given or (len(given) == 2 and given[0] != given[1]):
        raise hs_family.UsageError(
            "where: give one regex, positionally or as --symbol PATTERN"
            + (f" (got {given[0]!r} and --symbol {given[1]!r})" if len(given) == 2 else ""))
    pattern = given[0]
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "where",
                      lambda eng, base: eng.where(pattern, case_sensitive=args.case_sensitive))


WHERE = Command(add_args=_where_args, run=_where)


def _grep_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("pattern", help="regex to find in every tracked file")
    ap.add_argument("--kind", default=None, metavar="K[,K...]",
                    help="keep only these kinds: code, docstring, comment, string, data, doc, text")
    ap.add_argument("--top", type=int, default=50, help="rows to list (counts cover every match)")
    ap.add_argument("-i", "--ignore-case", action="store_true")
    ap.add_argument("--include-copies", action="store_true",
                    help="also read the files codemap.yaml `copies:` declares (generated "
                         "views, backups); skipped and counted by default")


def _grep(args: argparse.Namespace) -> "int | Outcome":
    """`grep PATTERN` — every match in the tracked tree, each typed by where it sits:
    code / docstring / comment / string in `.py`, `data` (with its key path) in
    YAML/TOML/JSON, `doc` (heading path, fenced or not) in Markdown, else `text`
    (an SVG's labels included). The files codemap.yaml's `copies:` declares are
    skipped and counted unless `--include-copies`."""
    kinds = [k.strip() for k in args.kind.split(",") if k.strip()] if args.kind else None
    root = args.ctx.root

    def _call(eng: QueryEngine, base: Path) -> QueryResult:
        copies = load(root / "codemap.yaml", root).descriptor.copies
        return eng.grep(Path(root), args.pattern, kinds=kinds, top=args.top,
                        ignore_case=args.ignore_case, copies=copies,
                        include_copies=args.include_copies)
    return _run_query(root, args.opts, args.snap, args.out, "grep", _call)


GREP = Command(add_args=_grep_args, run=_grep)


def _file_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("file", help="the file's path, or a tail of it on whole `/` segments")


def _file(args: argparse.Namespace) -> "int | Outcome":
    """`file PATH` — what one file holds: symbols and imports. PATH is the file's path or a
    tail of it on whole `/` segments; a tail naming several files exits 2 with every candidate."""
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "file",
                      lambda eng, base: eng.file(args.file))


FILE = Command(add_args=_file_args, run=_file)


def _unit_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("unit")


def _unit(args: argparse.Namespace) -> "int | Outcome":
    """`unit UNIT` — unit digest: modules, entries, in/out edges, cycles."""
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "unit",
                      lambda eng, base: eng.unit(args.unit))


UNIT = Command(add_args=_unit_args, run=_unit)


def _importers_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("--symbol", default=None,
                    help="answer only the direct importers whose own text uses SYM (depth 1 only)")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--exact", action="store_true",
                    help="a package answers for its own module only, not its submodules' importers")
    ap.add_argument("module", help="a dotted module or package name, or a unique tail of one "
                    "on whole dotted segments")


def _importers(args: argparse.Namespace) -> "int | Outcome":
    """`importers [--depth N] [--exact] MODULE` — fan-in table (reverse-edge BFS). A
    package counts its submodules' importers too, unless `--exact`; a name that is no
    module resolves as a unique dotted tail, or refuses with every candidate.
    `importers MODULE --symbol SYM` — the direct importers whose own text uses SYM,
    with the lines (read from the working tree); depth 1 only."""
    root, opts = args.ctx.root, args.opts
    if args.symbol is not None and args.depth != 1:
        raise hs_family.UsageError(
            f"importers: --symbol answers depth 1 only (got --depth {args.depth}): "
            "a caller of a caller is not a call site of the symbol; walk "
            "further with --depth and no --symbol")
    if args.symbol is None:
        return _run_query(root, opts, args.snap, args.out, "importers",
                          lambda eng, base: eng.importers(args.module, depth=args.depth,
                                                          exact=args.exact))

    def _call(eng: QueryEngine, base: Path) -> QueryResult:
        desc = load(root / "codemap.yaml", root).descriptor
        return eng.importers_using(args.module, args.symbol, root, desc.import_roots, desc)
    return _run_query(root, opts, args.snap, args.out, "importers", _call)


IMPORTERS = Command(add_args=_importers_args, run=_importers)


def _changed_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("other", metavar="SNAP_B")


def _changed(args: argparse.Namespace) -> "int | Outcome":
    """`changed [--top N] SNAP_B` — symbols changed between the primary
    snapshot (`--snap`, default latest) and `SNAP_B`."""
    def _call(eng: QueryEngine, base: Path) -> QueryResult:
        other_eng = _other_engine(base, args.other)
        result = eng.changed(other_eng, top=args.top)
        warn = codegraph_snapshot_reader.stamp_mismatch(eng.idx, other_eng.idx,
                                                        labels=_pair(eng, other_eng))
        if warn:
            result.notes.append(warn)
        return result
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "changed", _call)


CHANGED = Command(add_args=_changed_args, run=_changed)


def _big_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("--top", type=int, default=25)


def _big(args: argparse.Namespace) -> "int | Outcome":
    """`big [--top N]` — largest files and longest symbols."""
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "big",
                      lambda eng, base: eng.big(top=args.top))


BIG = Command(add_args=_big_args, run=_big)


def _unassigned_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("--key", default="dist", metavar="NAME",
                    help="the kernel does not know what a `dist` is; --key names it (default: dist)")


def _unassigned(args: argparse.Namespace) -> "int | Outcome":
    """`unassigned [--key NAME]` — symbols the snapshot left without `--key`
    (default `dist`; the kernel does not know what a `dist` is, --key names
    it)."""
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "unassigned",
                      lambda eng, base: eng.unassigned(payload_key=args.key))


UNASSIGNED = Command(add_args=_unassigned_args, run=_unassigned)


def _stats_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)


def _stats(args: argparse.Namespace) -> "int | Outcome":
    """`stats` — snapshot measurements (commit, totals, roots)."""
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "stats",
                      lambda eng, base: eng.stats())


STATS = Command(add_args=_stats_args, run=_stats)


def _trace_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("symbol")
    ap.add_argument("other", metavar="SNAP_B")


def _trace(args: argparse.Namespace) -> "int | Outcome":
    """`trace SYMBOL SNAP_B` — follow one symbol across the primary snapshot
    and `SNAP_B` (4 levels: file, symbol, group, tier)."""
    def _call(eng: QueryEngine, base: Path) -> QueryResult:
        other_eng = _other_engine(base, args.other)
        result = eng.trace(args.symbol, other_eng)
        warn = codegraph_snapshot_reader.stamp_mismatch(eng.idx, other_eng.idx,
                                                        labels=_pair(eng, other_eng))
        if warn:
            result.notes.append(warn)
        return result
    return _run_query(args.ctx.root, args.opts, args.snap, args.out, "trace", _call)


TRACE = Command(add_args=_trace_args, run=_trace)


def _tests_args(ap: argparse.ArgumentParser) -> None:
    _add_snap_out(ap)
    ap.add_argument("--level", choices=("unit", "function"), default="unit")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("unit", nargs="?", default=None)


def _listed(label: str, d: dict, key: str, total: int, top: int) -> "list[str]":
    """Cut `d[key]` to `top` IN PLACE (the count beside it stays the total) and
    render it — a summary that only counts sends the reader to the API to
    learn WHICH ones."""
    d[key] = d[key][:top]
    L = [f"{label}:"] if d[key] else []
    L += [f"  {x}" for x in d[key]]
    if total > len(d[key]):
        L.append(f"  … {total - len(d[key])} more (--top to see them)")
    return L


def _unavailable(rows: "list[dict[str, str]]", data: dict, L: "list[str]") -> None:
    if rows:
        data["function_level_unavailable"] = rows
        L.append(f"  functions not read ({len(rows)} file(s)): "
                 + ", ".join(r["path"] for r in rows))


def _tests(args: argparse.Namespace) -> "int | Outcome":
    """`tests [--level unit|function] [--top N] [UNIT]` — which test file
    (unit level) or test function (function level) touches `UNIT`; with no
    `UNIT`, a summary at the chosen level. Replaces `query tests` (file
    level, used to read a `_tests.yaml` sidecar no generator in this tree
    ever wrote) and testkit's `test map` (function level, its own unit
    notion) — both now computed from the same snapshot every other query
    verb reads. See `codegraph._core.testmap` for the touch rules and
    blind spots (dynamic imports, conftest fixtures, parametrize, patch
    strings read from the WORKING TREE).

    Exit: 0 a result was found · 1 `UNIT` given but touches nothing · 2
    cannot run (unknown unit, a bad `--level`, or a usage error)."""
    root = args.ctx.root
    level, top, unit_name = args.level, args.top, args.unit

    def _call(eng: QueryEngine, base: Path) -> QueryResult:
        tmap = testmap.build_test_map(eng.idx, root)
        all_units = set(eng.idx.get("units", {}))

        if unit_name is None:
            if level == "function":
                d = testmap.function_stats(tmap, all_units)
                L = [f"{d['total_test_files']} test file(s), {d['total_test_funcs']} test function(s)",
                     f"  1 unit         : {d['one_unit_funcs']}",
                     f"  >1 unit        : {d['shared_funcs']}",
                     f"  0 unit (blind) : {d['blind_funcs']}",
                     f"  patch-only     : {d['patch_only_funcs']}",
                     f"  units with no direct test ({len(d['units_without_direct_test'])}): "
                     f"{', '.join(d['units_without_direct_test']) or '—'}"]
                L += _listed("blind", d, "blind_func_ids", d["blind_funcs"], top)
                _unavailable(tmap.function_level_unavailable(), d, L)
            else:
                d = testmap.unit_stats(tmap, all_units)
                L = [f"{d['total_test_files']} test file(s)",
                     f"  owned  : {d['owned_files']}",
                     f"  shared : {d['shared_files']}",
                     f"  orphan : {d['orphan_files']}",
                     f"  units with no test file ({len(d['units_without_test_file'])}): "
                     f"{', '.join(d['units_without_test_file']) or '—'}"]
                L += _listed("orphan", d, "orphan_paths", d["orphan_files"], top)
            return QueryResult(0, "\n".join(L), d)

        try:
            key = eng.resolve_unit(unit_name)
        except Ambiguous as e:
            return QueryResult.refused(e)
        except LookupError as e:
            return QueryResult(1, str(e))

        if level == "function":
            fn_ids = testmap.functions_touching(tmap, key)
            files = sorted({fid.split("::", 1)[0] for fid in fn_ids})
            L = [f"unit {key} (function level): {len(fn_ids)} function(s), {len(files)} file(s)"]
            for fid in fn_ids[:top]:
                L.append(f"  {fid}")
            if len(fn_ids) > top:
                L.append(f"  … {len(fn_ids) - top} more (--top to see them)")
            data = {"unit": key, "level": "function", "functions": fn_ids, "files": files}
            _unavailable(tmap.function_level_unavailable(key), data, L)
            return QueryResult(0 if fn_ids else 1, "\n".join(L), data)

        owned, shared = testmap.owned_and_shared(tmap, key)
        L = [f"unit {key} (unit level): {len(owned)} owned, {len(shared)} shared"]
        for p in owned[:top]:
            L.append(f"  owned   {p}")
        for p in shared[:top]:
            L.append(f"  shared  {p}")
        if len(owned) > top or len(shared) > top:
            L.append("  … (--top to see more)")
        data = {"unit": key, "level": "unit", "owned": owned, "shared": shared}
        return QueryResult(0 if (owned or shared) else 1, "\n".join(L), data)

    return _run_query(root, args.opts, args.snap, args.out, "tests", _call)


TESTS = Command(add_args=_tests_args, run=_tests)
