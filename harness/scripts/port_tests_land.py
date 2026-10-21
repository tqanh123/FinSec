"""port_tests_land.py — `hs-code port tests-land`'s pure computation, as one
FLAT module under `harness/scripts/` (sibling of `port_tests_plan.py`; the
CLI verb, `codegraph_port_tests_land_verb.py`, is this module's only
caller).

Port of ob-tools' `portkit.tests_move.tests_land` (pinned commit `60075cb`,
`packages/portkit/src/portkit/tests_move/tests_land.py`, 453 lines) — lands
an upstream test file the SOURCE repo's own tree carries but this port's
target tree does not yet, deciding WHICH dist's `tests/` owns it through a
four-rung ladder (`--owner` override -> the ledger's own `test_owner` rows
-> an approved classify table -> the dist of the code the test imports),
then `cp` + `port_refs.rewrite_python` in place.

DOORS REUSED, NEVER A SECOND COPY (per-function, no-orphan):
  * `port_spec.merged_spec_and_unit_dist` — the SAME merged module-map
    reader `port_tests_plan`/`codegraph_port_tests_verbs.TESTS` already
    read; that module's own docstring names this exact future caller.
  * `port_ledger_writer.Ledger.load_checked` / `append_event` — the ONE
    ledger read/write door; a broken ledger line refuses (`BrokenLedgerError`)
    BEFORE anything here runs, never a second, more forgiving parse.
  * `port_ledger_spec.TEST_OWNER_KEY` — the test-owner column's name; no
    `test_owner_of()` reader crossed with the ledger spec (deliberately —
    see that module's own docstring), so this port reads
    `ev.extra.get(TEST_OWNER_KEY)` directly through the same constant.
  * `port_refs.Resolver` / `Report` / `load_index` / `rewrite_python` — the
    reference-rewrite engine, ported alongside this module (`port_refs.py`).
  * `codegraph._core.layout.dotted_of` — `_dotted` below calls it with a
    single `""` root (the path is already tests-root-relative), never a
    second dotted-name resolver.
  * `codegraph._core.git.git_text` — `_sha8` below, never `subprocess` direct.
  * `hook_runtime.resolve_actor` — `_actor` below, matching every other
    `codegraph_port_*_verbs.py` sibling's own attribution call.
  * `codegraph._core.descriptor.load` / `codegraph.check.tree.snapshot_idx`
    / `workspace` / `codegraph._core.testmap.build_test_map` — the SAME
    snapshot door `hs-code test backlog`'s own `_measure_tmap` and
    `codegraph.test.classify.measure_repo` already call, reused here
    (`_measure_tmap` below) for `--check-home`'s touch map — never a second
    touch-map engine (`codegraph._core.testmap.py`'s own docstring records
    that it already REPLACED the upstream `testkit/analysis/testmap.py`
    door on purpose; porting that upstream module back here would undo
    that decision).
  * `codegraph.test.check_home.check_home_rows` — the two-way-red gate's
    pure core, split out of `check_home()` alongside this module so BOTH
    `hs-code test check-home` and this verb's `--check-home` drive the
    SAME algorithm over their own row/touch shapes, never two gates.

TWO SCHEMAS NAMED `classify`, ON PURPOSE, NEVER CONFUSED: `--classify-dir`
tables are the SOURCE repo's OWN `testkit.classify/1` artifact (produced by
ITS OWN tooling — the same class of input `--owner`/`--specs-dir` already
are; nothing in THIS harness needs to produce that directory for the ladder
rung or `--sync-owners` to be reachable through this verb's own flag).
`harness/scripts/codegraph/test/classify.py`'s own `hs_code.test.classify/1`
is THIS repo's unrelated, generalised port of the same upstream family —
`read_classify_table` below refuses a table carrying that schema BY NAME,
exit 2, never a silent skip (upstream's own `owner_from_classify` silently
skips an unparseable/wrong-schema file, `tests_land.py`'s own `except
(OSError, yaml.YAMLError): continue` — that is the ONE upstream shape this
port deliberately does not repeat, matching the ledger door's own "never a
second, more forgiving parse" rule above).

NO HARD-CODED PACKAGE: `_OLD_ROOT = "gsm_agent"` (upstream's own module
constant) is replaced by a `package` parameter, read by the caller off
`.port/port.yaml sources[].package` — same rule every sibling port module
in this domain states.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: `resolve_actor` lives under `harness/hooks/`, not `harness/scripts/` — the
#: same two-directory sys.path splice every sibling `codegraph_port_*_verbs.py`
#: needs before it can import it (matches `codegraph_port_ledger_verbs.py`'s
#: own preamble exactly, so a real CLI run — not just an in-repo pytest whose
#: conftest already adds both dirs — resolves it too).
_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import yaml_io  # noqa: E402
from codegraph._core.git import git_text  # noqa: E402
from codegraph._core import layout as _layout  # noqa: E402
from codegraph._core.layout import dotted_of  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hook_runtime import resolve_actor  # noqa: E402
from port_ledger_reader import Event  # noqa: E402
from port_ledger_spec import TEST_OWNER_KEY  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger, append_events, now_ts  # noqa: E402
from port_refs import Report, Resolver, load_index, module_files, rewrite_python  # noqa: E402
from port_spec import SpecError, merged_spec_and_unit_dist  # noqa: E402

#: The source repo's own classify-table schema (the upstream `map/test-homes/
#: <dist>.yaml` layout) — NOT this repo's own `hs_code.test.classify/1`
#: (`codegraph/test/classify.py`'s `SCHEMA`); see the module docstring.
CLASSIFY_SCHEMA = "testkit.classify/1"
_OWN_CLASSIFY_SCHEMA = "hs_code.test.classify/1"


class ClassifyTableError(ValueError):
    """A `--classify-dir` table is not a well-formed `testkit.classify/1`
    table — refused by name, never silently skipped."""


def _actor() -> str:
    return resolve_actor()


def _sha8(tree: Path) -> str:
    return git_text(tree, "rev-parse", "--short=8", "HEAD") or "unknown"


def _dotted(rel: str) -> "str | None":
    """`tests/deep/test_x.py` (already tests-root-relative) -> the dotted
    module name, via the ONE dotted-name door every other verb reads."""
    return dotted_of(rel, [""])


def _old_imports(text: str, package: str) -> "set[str]":
    """Dotted old-root import targets of a test file (AST)."""
    out: "set[str]" = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names if a.name.split(".")[0] == package)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module \
                and n.module.split(".")[0] == package:
            out.add(n.module)
    return out


def _test_names(text: str) -> "list[str]":
    """Top-level test functions and Test classes — the file's test surface."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
            out.append(n.name)
        elif isinstance(n, ast.ClassDef) and n.name.startswith("Test"):
            out.append(n.name)
    return sorted(out)


def _dist_of_module(module: str, repo: Path, descriptor: Any) -> "str | None":
    """Owning dist of a NEW-tree module, by file location under the target's
    declared layout."""
    packages = repo / descriptor.packages_dir
    py, init = module_files(repo, descriptor, module)
    hits = {p.relative_to(packages).parts[0] for p in py + init}
    return sorted(hits)[0] if len(hits) == 1 else None


def _dist_of_home(home: str, descriptor: Any) -> "str | None":
    """The dist a classify row's `home` names: the segment right after the
    declared `layout.packages_dir`, or `None` for a home outside it (e2e,
    vault — not dist-owned)."""
    parts = Path(home).parts
    base = Path(descriptor.packages_dir).parts
    if len(parts) > len(base) and parts[:len(base)] == base:
        return parts[len(base)]
    return None


# ── the source repo's OWN classify tables (testkit.classify/1) ─────────────

@dataclass(frozen=True)
class ClassifyRow:
    id: str
    path: str
    home: str
    status: str


def read_classify_table(doc: Any, source: str) -> "list[ClassifyRow]":
    """One `testkit.classify/1` doc -> its rows, or `ClassifyTableError`
    naming BOTH schemas (this repo's own `codegraph/test/classify.py` ships
    a DIFFERENT, unrelated schema — see the module docstring)."""
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if schema != CLASSIFY_SCHEMA:
        raise ClassifyTableError(
            "%s: schema %r != %r (this repo's own test classify tables are "
            "%r — a --classify-dir table must be the SOURCE repo's own "
            "testkit.classify/1, never this repo's)"
            % (source, schema, CLASSIFY_SCHEMA, _OWN_CLASSIFY_SCHEMA))
    rows = doc.get("tests")
    if not isinstance(rows, list):
        raise ClassifyTableError("%s: `tests` must be a list" % source)
    out: "list[ClassifyRow]" = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ClassifyTableError("%s: tests[%d] is not a mapping" % (source, i))
        out.append(ClassifyRow(id=str(row.get("id") or ""), path=str(row.get("path") or ""),
                               home=str(row.get("home") or ""), status=str(row.get("status") or "")))
    return out


def _load_classify_tables(classify_dir: Path) -> "list[tuple[Path, list[ClassifyRow]]]":
    """Every `*.yaml` under `classify_dir`, parsed through `read_classify_table`
    — the ONE reader `owner_from_classify`, `_sync_owners` and `_check_home`
    all share, so a wrong-schema table is refused (exit 2) identically no
    matter which of the three reads the directory first."""
    out: "list[tuple[Path, list[ClassifyRow]]]" = []
    for t in sorted(classify_dir.glob("*.yaml")):
        try:
            doc = yaml_io.yaml_load(t.read_text(encoding="utf-8")) or {}
        except (OSError, yaml_io.YAMLError) as e:
            raise ClassifyTableError("%s: %s" % (t, e)) from e
        out.append((t, read_classify_table(doc, str(t))))
    return out


# ── owner resolution ────────────────────────────────────────────────────────
def owner_from_classify(classify_dir: Path, rel_under_tests: str,
                        descriptor: Any) -> "str | None":
    """An approved classify row for this file -> the home's dist. Raises
    `ClassifyTableError` on a malformed/wrong-schema table — never a silent
    skip (see the module docstring)."""
    for _t, rows in _load_classify_tables(classify_dir):
        for row in rows:
            if row.status != "approved" or row.path != rel_under_tests:
                continue
            dist = _dist_of_home(row.home, descriptor)
            if dist:
                return dist
    return None


def owner_from_ledger(state: "dict[str, Event]", module: str, names: "list[str]") -> "tuple[str | None, str]":
    """The `test_owner` of any existing row for this file's functions (body
    match across anchors). Conflicting owners -> RED."""
    owners: "set[str]" = set()
    for k, ev in state.items():
        body = k.split("::", 1)[1] if "::" in k else k
        mod, _, name = body.rpartition("::")
        if mod == module and name in names:
            owner = ev.extra.get(TEST_OWNER_KEY)
            if owner:
                owners.add(owner)
    if len(owners) == 1:
        return next(iter(owners)), "ledger test_owner"
    if len(owners) > 1:
        return None, f"ledger rows disagree: {sorted(owners)} — pass --owner"
    return None, ""


def owner_from_imports(text: str, module_map: "dict[str, str]", repo: Path,
                       descriptor: Any, package: str) -> "tuple[str | None, str]":
    """The dist of the code the test imports: old imports through the merged
    module map (longest prefix) into the target tree. Unique dist wins."""
    dists: "set[str]" = set()
    unmapped: "list[str]" = []
    for old in _old_imports(text, package):
        new = None
        parts = old.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in module_map:
                new = module_map[cand]
                break
        if new is None:
            unmapped.append(old)
            continue
        d = _dist_of_module(new, repo, descriptor)
        if d:
            dists.add(d)
    if unmapped and not dists:
        return None, f"no mapped import (unmapped: {', '.join(sorted(unmapped))})"
    if len(dists) == 1:
        return next(iter(dists)), "unique dist of the mapped imports"
    return None, (f"imports span {sorted(dists) or 'no'} dist(s)"
                  + (f"; unmapped: {', '.join(sorted(unmapped))}" if unmapped else "")
                  + " — a per-file judgement, pass --owner")


# ── the two modes ────────────────────────────────────────────────────────────
def _plan_land(args: Any, repo: Path, descriptor: Any, module_map: "dict[str, str]",
               package: str, state: "dict[str, Event]",
               resolver: Resolver) -> "tuple[list[dict], list[dict]]":
    """`(plans, handoff)` — `handoff` is the STRUCTURED subset of `plans`'
    own `red` reasons a person can settle by turning into a plan phase
    a person can settle by turning into a plan phase: the "no unique
    owner" fork ONLY. A destination that
    already exists, or an unresolved rewrite, is a DIFFERENT class of
    problem (a hand edit / a later drift-merge concern) — never routed
    through `handoff`. Always present, possibly empty, even when nothing
    is red."""
    plans = []
    handoff: "list[dict[str, Any]]" = []
    overrides: "dict[str, str]" = {}
    for spec in args.owner or []:
        if "=" not in spec:
            raise ValueError(f"--owner wants FILE=DIST, got {spec!r}")
        f, d = spec.split("=", 1)
        overrides[f] = d
    for rel in args.land:
        src = args.source / rel
        if not src.is_file():
            plans.append({"file": rel, "red": f"{src} does not exist under --source"})
            continue
        tr = Path(rel)
        if tr.parts and tr.parts[0] != args.tests_root:
            plans.append({"file": rel, "red": f"not under the tests root "
                                              f"{args.tests_root}/ (pass --tests-root)"})
            continue
        text = src.read_text(encoding="utf-8")
        rel_under = str(tr.relative_to(args.tests_root))
        module = _dotted(str(tr)) or str(tr)
        names = _test_names(text)
        owner, why = (None, "")
        if rel in overrides:
            owner, why = overrides[rel], "--owner override"
        else:
            owner, why = owner_from_ledger(state, module, names)
        if owner is None and args.classify_dir:
            o = owner_from_classify(Path(args.classify_dir), rel_under, descriptor)
            if o:
                owner, why = o, "classify table"
        if owner is None:
            owner, why = owner_from_imports(text, module_map, repo, descriptor, package)
        dest = repo / _layout.test_home_of(descriptor, owner) / rel_under if owner else None
        row = {"file": rel, "module": module, "tests": names, "owner": owner,
               "owner_why": why, "dest": dest.relative_to(repo).as_posix() if dest else None}
        if owner is None:
            row["red"] = f"no unique owner ({why})"
            plans.append(row)
            handoff.append({"kind": "test-move", "path": rel, "why": row["red"]})
            continue
        if dest is not None and dest.exists():
            row["red"] = f"{dest} already exists — tests-land lands NEW files only " \
                         f"(modified twins are a later drift-merge concern)"
            plans.append(row)
            continue
        rep = Report()
        new = rewrite_python(text, resolver, rep, rel, package, None) if package in text else text
        owed = [u for u in rep.unresolved if u["kind"] in ("import", "star", "syntax")]
        row["rewrite_unresolved"] = len(rep.unresolved)
        row["text"] = new
        if owed:
            row["red"] = "; ".join(f"{u['file']}:{u['line']} {u['text'][:60]} ({u['why']})"
                                   for u in owed[:3])
        plans.append(row)
    return plans, handoff


def _owner_rows(sha8: str, src: "dict[str, str]",
                entries: "list[tuple[str, str, str, str]]") -> "list[Event]":
    """(module, func, owner, note) -> ownership events, deduped by the caller.
    `src` (`{id, sha}`, `sha` full 40-hex) is THIS harness's own write-door
    addition (`port_ledger_writer.validate_src`) — upstream's ledger carries
    no such field; every append here must still supply one."""
    return [Event(ts=now_ts(), node=f"{sha8}::{mod}::{func}", status="unported",
                  by="human", actor=_actor(), note=note,
                  extra={TEST_OWNER_KEY: owner, "src": src}) for mod, func, owner, note in entries]


def _write_rows(led: Ledger, evs: "list[Event]") -> "tuple[int, dict | None]":
    """All rows through the batch door, all-or-nothing: `(rows written, the
    refusal)` — the refusal names the node and its findings, never dropped."""
    ev, findings = append_events(led, evs, apply=True)
    if ev is None:
        return len(evs), None
    return 0, {"node": ev.node, "findings": [list(f) for f in findings]}


def _source_provenance(args: Any, package: str) -> "dict[str, str]":
    """`{id, sha}` for the write door's own `src` law — `id` is whichever
    source this land call is attributed to (`--source-id`, falling back to
    `package`); `sha` is the SOURCE worktree's current full commit (never
    the short `sha8` the ledger node anchors on — the door requires 40-hex)."""
    source_id = getattr(args, "source_id", None) or package
    full_sha = git_text(args.source, "rev-parse", "HEAD")
    return {"id": source_id, "sha": full_sha}


def _sync_owners(args: Any, led: Ledger, sha8: str) -> Verdict:
    """Populate the test_owner column from the approved classify tables."""
    cdir = Path(args.classify_dir)
    state = led.replay()
    planned: "list[dict]" = []
    entries: "list[tuple[str, str, str, str]]" = []
    try:
        tables = _load_classify_tables(cdir)
    except ClassifyTableError as e:
        return Verdict.broken(f"sync-owners: {e}")
    for t, rows in tables:
        for row in rows:
            if row.status != "approved":
                continue
            dist = _dist_of_home(row.home, args.descriptor)
            if not dist:
                continue                    # e2e/vault homes are not dist-owned
            module = _dotted(str(Path(args.tests_root) / row.path)) or ""
            if not module:
                continue
            func = row.id.rpartition("::")[2] if "::" in row.id else row.id
            key = f"{sha8}::{module}::{func}"
            prev = state.get(key)
            prev_owner = prev.extra.get(TEST_OWNER_KEY) if prev is not None else ""
            planned.append({"node": key, "test_owner": dist, "was": prev_owner or ""})
            if prev_owner != dist:
                entries.append((module, func, dist, f"test-owner from classify table {t.name}"))
    n_written, refused = 0, None
    if args.apply:
        src = _source_provenance(args, args.package)
        n_written, refused = _write_rows(led, _owner_rows(sha8, src, entries))
    payload = {"schema": "hs_code.port.tests-land/1", "mode": "sync-owners",
               "dry_run": not args.apply, "rows_seen": len(planned),
               "rows_planned": len(entries), "rows_written": n_written,
               "rows": planned, **({"refused": refused} if refused else {})}
    return Verdict(0 if (n_written == len(entries) or not args.apply) else 1, payload)


# ── --check-home: the split core, driven from the snapshot door (never a subprocess) ──
def _measure_tmap(root: Path) -> "tuple[Any, dict[str, str], str | None]":
    """The test map + unit->dist table, through the ONE snapshot door every
    other verb reads (`codegraph.check.tree.snapshot_idx`/`workspace` +
    `codegraph._core.testmap.build_test_map`) — the SAME calls `hs-code test
    backlog`'s own `_measure_tmap` and `codegraph.test.classify.measure_repo`
    make. `(None, {}, reason)` when the snapshot door cannot measure the
    target (not a git repo, no snapshot, a descriptor it refuses) — never a guessed touch map, and
    never a second touch-map engine."""
    try:
        from codegraph._core.descriptor import DescriptorError, load
        from codegraph._core.testmap import build_test_map
        from codegraph.check.tree import CheckError, snapshot_idx, workspace
        descriptor = load(root / "codemap.yaml", root).descriptor
        idx = snapshot_idx(root, descriptor, None, "port tests-land")
        _descriptor2, packages = workspace(root)
    except (DescriptorError, CheckError, OSError) as e:   # never a crash, see docstring
        return None, {}, str(e)
    tmap = build_test_map(idx, root)
    dist_of_unit = {p.unit: p.name for p in packages}
    return tmap, dist_of_unit, None


def _check_home(port_root: Path, dists: "list[str]", classify_dir: "Path | None") -> dict:
    """The test-side gate over the landed dists, IN-PROCESS (never a
    subprocess): `check_home_rows` (the shared core, `codegraph.test.
    check_home.py`) driven per dist by this repo's own `testkit.classify/1`
    tables + the snapshot door's own touch map. A dist with no table is
    `skipped`, never a crash; a target the snapshot door cannot measure
    reports `measured: false` with the reason, same shape, every dist."""
    from codegraph.test.check_home import check_home_rows

    results: "dict[str, Any]" = {}
    tmap, dist_of_unit, unmeasured_reason = _measure_tmap(port_root)
    for dist in sorted(set(dists)):
        table_path = classify_dir / f"{dist}.yaml" if classify_dir else None
        if table_path is None or not table_path.is_file():
            results[dist] = {"skipped": "no classify table"
                             + (f" at {table_path}" if table_path else " (--classify-dir)")}
            continue
        try:
            doc = yaml_io.yaml_load(table_path.read_text(encoding="utf-8")) or {}
            rows = read_classify_table(doc, str(table_path))
        except (OSError, yaml_io.YAMLError, ClassifyTableError) as e:
            raise ClassifyTableError(str(e)) from e
        if tmap is None:
            results[dist] = {"measured": False, "reason": unmeasured_reason}
            continue
        units = {u for u, d in dist_of_unit.items() if d == dist} or None
        results[dist] = check_home_rows(rows, tmap.all_funcs(), port_root, units)
    return results


# ── entry point ──────────────────────────────────────────────────────────────
def run(args: Any) -> Verdict:
    package = args.package
    repo = Path(args.repo)
    descriptor = args.descriptor
    if args.sync_owners:
        if not args.classify_dir or not Path(args.classify_dir).is_dir():
            return Verdict.broken("sync-owners: --classify-dir DIR of classify tables is required")
        ledger_path = Path(args.ledger) if args.ledger else None
        if ledger_path is None or not ledger_path.is_file():
            return Verdict.broken("sync-owners: --ledger FILE is required")
        sha8 = args.sha or _sha8(repo)
        try:
            led = Ledger.load_checked(ledger_path)
        except BrokenLedgerError as e:
            return Verdict.broken(f"sync-owners: {e}")
        return _sync_owners(args, led, sha8)

    if not args.source.is_dir() or not args.specs_dir.is_dir():
        return Verdict.broken("tests-land: --source (upstream worktree) and "
                              "--specs-dir are required")
    ledger_path = Path(args.ledger) if args.ledger else None
    if ledger_path is None or not ledger_path.is_file():
        return Verdict.broken("tests-land: --ledger FILE is required (the rewrite "
                              "resolves old refs through it)")
    try:
        led = Ledger.load_checked(ledger_path)
    except BrokenLedgerError as e:
        return Verdict.broken(f"tests-land: {e}")
    try:
        merged, _primary, _spans, _multi, _catch = merged_spec_and_unit_dist(args.specs_dir, package)
    except SpecError as e:
        return Verdict.broken(f"tests-land: {e}")
    state = led.replay()
    ix = load_index(led, package)
    resolver = Resolver(ix, repo, package, descriptor)
    sha8 = args.sha or _sha8(args.source)

    try:
        plans, handoff = _plan_land(args, repo, descriptor, merged.module_map, package, state,
                                    resolver)
    except ValueError as e:
        return Verdict.broken(f"tests-land: {e}")
    red = [p for p in plans if p.get("red")]

    check_home: dict = {}
    check_skipped = False
    if args.check_home:
        ok_dists = [p["owner"] for p in plans if p.get("owner") and not p.get("red")]
        try:
            check_home = _check_home(repo, ok_dists,
                                     Path(args.classify_dir) if args.classify_dir else None)
        except ClassifyTableError as e:
            return Verdict.broken(f"tests-land: --check-home: {e}")
        check_skipped = any(("skipped" in v or v.get("measured") is False) for v in check_home.values())

    n_rows, refused = 0, None
    if args.apply and not red:
        for p in plans:
            dest = repo / p["dest"] if p.get("dest") else None
            if dest is None or p.get("red"):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(p.pop("text"), encoding="utf-8")
        src = _source_provenance(args, package)
        n_rows, refused = _write_rows(led, _owner_rows(sha8, src, [
            (p["module"], n, p["owner"],
             f"landed to {p['dest']} by hs-code port tests-land; "
             "the landed STATE is checked by test check-home, not by this row")
            for p in plans if not p.get("red") for n in p["tests"]
        ]))

    payload = {"schema": "hs_code.port.tests-land/1", "mode": "land",
               "dry_run": not args.apply, "files": [
                   {k: v for k, v in p.items() if k != "text"} for p in plans],
               "rows_written": n_rows, "check_home": check_home,
               "red": [p.get("red") for p in red], "handoff": handoff,
               **({"refused": refused} if refused else {})}
    return Verdict(1 if (red or check_skipped or refused) else 0, payload)
