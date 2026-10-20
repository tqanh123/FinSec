"""hs-code test classify -- propose a kind (unit/integration/e2e) + a home for
every test function, from the STATIC test map (the tool PROPOSES, a human
approves).

Port of ob-tools' `testkit.analysis.classify` (pinned commit `60075cb`),
generalised to the repo's OWN declared layout instead of a `<tests_dir>
<src_root> <pkg>` triple typed on the command line: the whole tree is
measured through the same snapshot door every other `hs-code` verb reads
(`codegraph.check.tree.snapshot_idx` + `codegraph._core.testmap.
build_test_map`), and a unit's "dist" (for the `packages/<dist>/tests/`
home) is the workspace package name `codegraph.check.tree.workspace` already
resolves -- never a second `pyproject.toml` read. A flat tree (no
`layout.packages_dir`) homes every unit test at the one repo-root `tests/`
regardless of which unit it touches (`codegraph._core.layout.test_home_of`
is the single door deciding that; `refactor.namerepair`'s own test-root
scan calls the same door).

House ladder -- "home" is a ladder, not a directory:
    <= 1 unit, no external I/O           -> unit         -> that unit's dist's tests dir
    >= 2 units (a boundary)              -> integration  -> central vault
    live / external I/O / end-to-end     -> e2e          -> central e2e dir

WHAT IT CANNOT SEE
    * Inference runs on the STATIC graph only: runtime markers
      (`pytest.mark.skipif`), subprocess-spawned dependencies and teardown
      effects are invisible -- a proposal here is a SUGGESTION, never a verdict.
    * External-I/O detection is import-name based (psycopg/httpx/requests/
      aiohttp/sse_*); a client hidden behind one of the measured units is seen
      as "unit", not e2e. I/O via stdlib socket/http.server is NOT detected.
    * `live` markers are honored only as DECORATORS inside the function slice
      (decorator lines included). A module-level `pytestmark = pytest.mark.live`
      sits outside every slice and is INVISIBLE -- such files classify per
      function-touch only; markers constructed dynamically (`mark.get(name)`)
      are equally invisible.
    * A blind row (0 touch) is NOT "touches nothing" -- it is "this tool cannot
      infer". Orphan-looking tests need a human eye before trusting.
    * Monkeypatch-string touches inform the home proposal but carry MEDIUM
      confidence -- they are real dependencies the import walk cannot see, yet
      a string is not a call.

USAGE
    hs-code test classify [--out FILE] [--apply]
    hs-code test classify --review FILE
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hs_family
import yaml_io

from codegraph._core.layout import test_home_of
from codegraph.test.inputs import mapping
from hs_family.cli import Command

#: import names whose presence marks external I/O (e2e signal). Configurable
#: data, not law: a project extends this list in its descriptor, not in code.
EXTERNAL_IO_IMPORTS = frozenset({
    "psycopg", "psycopg2", "httpx", "requests", "aiohttp", "urllib3",
    "sse_starlette", "websockets", "socketio",
})
LIVE_MARKERS = frozenset({"live", "e2e"})

KIND_UNIT, KIND_INTEGRATION, KIND_E2E = "unit", "integration", "e2e"
VALID_KINDS = frozenset({KIND_UNIT, KIND_INTEGRATION, KIND_E2E})

SCHEMA = "hs_code.test.classify/1"


@dataclass(frozen=True)
class Signal:
    source: str        # refs | monkeypatch_string | marker_decorator | import_external_io | goldens
    detail: str
    confidence: str    # high | medium


@dataclass
class Proposal:
    id: str
    path: str
    func: str
    kind: str                       # proposed kind ("" when blind)
    home: str                       # proposed home path ("" when blind)
    status: str = "proposed"        # proposed | blind | approved (human)
    proposed_kind: str = ""         # kept when a human overrides
    signals: "list[Signal]" = field(default_factory=list)
    reasons: "list[str]" = field(default_factory=list)


def _home_for(kind: str, unit: str, dist_of_unit: "dict[str, str]", descriptor: Any,
             vault: str, e2e_home: str) -> str:
    if kind == KIND_UNIT:
        # A unit outside any declared workspace package (a flat root's own
        # unit, or a project with no `packages_dir` at all) has no dist to
        # home under a dist's own tests dir -- it shares the repo-root
        # `tests/` instead, the same fallback `test_home_of(None)` already
        # gives `refactor.namerepair`'s own flat-root scan.
        dist = dist_of_unit.get(unit) if descriptor.packages_dir else None
        return test_home_of(descriptor, dist)
    return vault if kind == KIND_INTEGRATION else e2e_home


def _marker_names(fn_text_markers: "set[str]") -> "set[str]":
    return {m for m in fn_text_markers if m in LIVE_MARKERS}


def _goldens_in(text: str) -> "list[str]":
    out = []
    for tok in ("golden", ".golden"):
        if tok in text:
            out.append(tok)
    return sorted(set(out))


def classify_map(tmap: Any, root: Path, *, dist_of_unit: "dict[str, str]", descriptor: Any,
                 vault: str = "tests/integration/",
                 e2e_home: str = "tests/e2e/") -> "list[Proposal]":
    """Infer kind + home for every test function the snapshot's test map
    carries. `dist_of_unit` maps a workspace unit key (e.g. `packages/acme-
    core`) to the dist name `test_home_of` homes it under; a unit missing
    from the map (a flat-root unit, or a unit outside any declared workspace)
    falls back to the shared repo-root `tests/` -- never an invented
    `packages/<unit-name>/tests/`, since a unit key is not a dist name.

    Marker/IO/golden signals are read from the FUNCTION's own slice (decorator
    lines above included); an external-IO import counts for a function only when
    the imported name is used inside that slice."""
    out: "list[Proposal]" = []
    texts = {tf.path: _read(root / tf.path).splitlines() for tf in tmap.files}
    for fn in tmap.all_funcs():
        lines = texts.get(fn.file, [])
        lo = max(0, fn.lines[0] - 3)          # decorator lines above the def
        hi = min(len(lines), fn.lines[1])
        slice_ = "\n".join(lines[lo:hi])
        p = _classify_one(fn, slice_, dist_of_unit, descriptor, vault, e2e_home)
        out.append(p)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _classify_one(fn: Any, slice_: str, dist_of_unit: "dict[str, str]", descriptor: Any,
                  vault: str, e2e_home: str) -> Proposal:
    signals: "list[Signal]" = []
    reasons: "list[str]" = []
    touched = fn.touch

    for u in fn.refs_touch:
        signals.append(Signal("refs", u, "high"))
    for u in fn.patch_touch:
        signals.append(Signal("monkeypatch_string", u, "medium"))

    markers = _marker_names(_markers_in_text(slice_))
    live = bool(markers & LIVE_MARKERS)
    if live:
        signals.append(Signal("marker_decorator", "live", "high"))

    ext = sorted(EXTERNAL_IO_IMPORTS.intersection(_names_in_slice(slice_)))
    if ext:
        signals.append(Signal("import_external_io", ",".join(ext), "high"))
    goldens = _goldens_in(slice_)
    if goldens:
        signals.append(Signal("goldens", ",".join(goldens), "medium"))

    if live:
        kind = KIND_E2E
        reasons.append("live/e2e marker on the file")
    elif ext:
        kind = KIND_E2E
        reasons.append(f"external I/O import(s): {', '.join(ext)}")
    elif len(touched) >= 2:
        kind = KIND_INTEGRATION
        reasons.append(f"touches {len(touched)} units: {', '.join(sorted(touched))}")
    elif len(touched) == 1:
        kind = KIND_UNIT
        reasons.append(f"touches exactly one unit: {next(iter(touched))}")
    else:
        return Proposal(id=fn.id, path=fn.file, func=fn.name, kind="", home="",
                        status="blind", signals=signals,
                        reasons=["no inferable touch — BLIND, not 'touches nothing' "
                                 "(dynamic imports / conftest / helper indirection "
                                 "are invisible here)"])

    unit = sorted(touched)[0] if touched else ""
    home = _home_for(kind, unit, dist_of_unit, descriptor, vault, e2e_home)
    return Proposal(id=fn.id, path=fn.file, func=fn.name, kind=kind, home=home,
                    status="proposed", signals=signals, reasons=reasons)


def _markers_in_text(text: str) -> "set[str]":
    return set(re.findall(r"pytest\.mark\.([A-Za-z_][A-Za-z0-9_]*)", text))


def _names_in_slice(slice_: str) -> "set[str]":
    """Identifiers USED in the function slice (word tokens). An import at file
    top matters to this function only if the name appears here too."""
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", slice_))


# ── YAML round-trip ──────────────────────────────────────────────────────────

def to_yaml(proposals: "list[Proposal]") -> dict:
    return {
        "schema": SCHEMA,
        "note": "PROPOSED by hs-code test classify from the static graph. A human "
                "approves or overrides every row. blind != touches nothing.",
        "tests": [{
            "id": p.id, "path": p.path, "func": p.func,
            "kind": p.kind, "home": p.home, "status": p.status,
            **({"proposed_kind": p.proposed_kind} if p.proposed_kind else {}),
            "signals": [{"source": s.source, "detail": s.detail,
                         "confidence": s.confidence} for s in p.signals],
            "reasons": p.reasons,
        } for p in proposals],
    }


def _rows_of(doc: dict) -> "list[dict]":
    """`doc['tests']`, shape-checked: a non-list `tests`, or a row that is not
    itself a mapping, is a malformed TABLE -- refused here as a `ValueError`
    naming the offender by index, never a bare `KeyError`/`TypeError`
    traceback surfacing two callers removed from the file that was actually
    wrong (a hand-edited YAML table, not this module)."""
    tests = doc.get("tests", [])
    if not isinstance(tests, list):
        raise ValueError(f"`tests` must be a list, got {type(tests).__name__}")
    return [mapping(row, f"tests[{i}]") for i, row in enumerate(tests)]


def from_yaml(doc: dict) -> "list[Proposal]":
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"unknown schema {doc.get('schema')!r}; expected {SCHEMA}")
    out: "list[Proposal]" = []
    for i, row in enumerate(_rows_of(doc)):
        missing = [k for k in ("id", "path", "func") if k not in row]
        if missing:
            raise ValueError(f"tests[{i}]: missing required key(s) {', '.join(missing)}")
        out.append(Proposal(
            id=row["id"], path=row["path"], func=row["func"],
            kind=row.get("kind", ""), home=row.get("home", ""),
            status=row.get("status", "proposed"),
            proposed_kind=row.get("proposed_kind", ""),
            signals=[Signal(s["source"], s["detail"], s["confidence"])
                     for s in row.get("signals", [])],
            reasons=list(row.get("reasons", []))))
    return out


def review(doc: dict) -> "tuple[list[str], list[str]]":
    """Validate a hand-edited table. Returns (errors, overridden_ids).

    A row is approvable when a human flipped `status` to `approved` (kind/home
    may differ from the proposal — the proposal is kept in proposed_kind).
    Rows still `proposed`/`blind` are ERRORS: the human decides, and
    a missing decision must not read as acceptance.
    """
    errors: "list[str]" = []
    overridden: "list[str]" = []
    for row in _rows_of(doc):
        rid, status, kind = row.get("id"), row.get("status"), row.get("kind")
        if status != "approved":
            errors.append(f"{rid}: status {status!r} — approve or override, "
                          f"silence is not a decision")
            continue
        if kind not in VALID_KINDS:
            errors.append(f"{rid}: kind {kind!r} not in {sorted(VALID_KINDS)}")
        if kind in (KIND_UNIT, KIND_INTEGRATION) and not row.get("home"):
            errors.append(f"{rid}: kind {kind} needs a home path")
        if row.get("proposed_kind") and row["proposed_kind"] != kind:
            overridden.append(str(rid))
    return errors, overridden


# ── measurement ──────────────────────────────────────────────────────────────

def measure_repo(root: Path, prog: str) -> "tuple[Any, dict[str, str], Any]":
    """(TestMap, dist-of-unit map, descriptor) for the whole repo, through the
    one snapshot door every other `hs-code` verb reads. `dist_of_unit` maps a
    workspace unit key to the dist name `codegraph._core.layout.test_home_of`
    homes it under -- read from `codegraph.check.tree.workspace`'s own
    `Package.name`, never a second `pyproject.toml` parse. Raises
    `hs_family.UsageError` when the tree cannot be measured this way (no
    `codemap.yaml`, not a git repo, an unparsable descriptor, ...) -- never a
    guessed empty map."""
    from codegraph._core.descriptor import DescriptorError
    from codegraph._core.testmap import build_test_map
    from codegraph.check.tree import CheckError, snapshot_idx, workspace
    try:
        descriptor, packages = workspace(root)
        idx = snapshot_idx(root, descriptor, None, prog)
    except (DescriptorError, CheckError, OSError) as e:
        raise hs_family.UsageError(f"{prog}: {e}") from e
    tmap = build_test_map(idx, root)
    dist_of_unit = {p.unit: p.name for p in packages}
    return tmap, dist_of_unit, descriptor


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--review", metavar="FILE",
                    help="validate a hand-edited table instead of proposing")
    sp.add_argument("--vault", default="tests/integration/",
                    help="home for an integration-kind proposal (tests/integration/)")
    sp.add_argument("--e2e-home", default="tests/e2e/",
                    help="home for an e2e-kind proposal (tests/e2e/)")
    sp.add_argument("--out", type=Path, default=None,
                    help="write the YAML table here (--apply writes it; a dry run "
                         "still prints the same table)")


def _run_review(args: argparse.Namespace, as_json: bool) -> int:
    try:
        doc = mapping(yaml_io.yaml_load(Path(args.review).read_text(encoding="utf-8")) or {},
                     args.review)
    except (OSError, yaml_io.YAMLError, ValueError) as e:
        raise hs_family.UsageError(f"classify: {e}") from e
    errors, overridden = review(doc)
    if as_json:
        print(json.dumps({"row_count": len(doc.get("tests", [])),
                          "overridden": overridden, "errors": errors},
                         sort_keys=True, ensure_ascii=False))
    else:
        for err in errors:
            print(f"REVIEW RED: {err}", file=sys.stderr)
        print(f"review: {len(doc.get('tests', []))} row(s), "
              f"{len(overridden)} human override(s)")
    return 1 if errors else 0


def _classify(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    if args.review:
        return _run_review(args, as_json)

    tmap, dist_of_unit, descriptor = measure_repo(root, "classify")
    proposals = classify_map(tmap, root, dist_of_unit=dist_of_unit, descriptor=descriptor,
                             vault=args.vault, e2e_home=args.e2e_home)
    doc = to_yaml(proposals)
    text = yaml_io.safe_dump(doc, sort_keys=False, allow_unicode=True)
    blind = sum(1 for p in proposals if p.status == "blind")

    written = False
    if args.out and apply:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        written = True

    if as_json:
        payload = {"schema": doc["schema"], "tests": doc["tests"], "total": len(proposals),
                  "blind": blind}
        if args.out:
            payload["out"] = {"path": str(args.out), "written": written}
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        if not args.out or not apply:
            print(text)
        dest = f" -> {args.out}" if written else (
            f" -> {args.out} [dry-run: --apply writes]" if args.out else " -> stdout")
        print(f"classify: {len(proposals)} func(s) proposed, {blind} blind "
              f"(needs human){dest}", file=sys.stderr)
    return 0


CLASSIFY = Command(add_args=add_arguments, run=_classify)
