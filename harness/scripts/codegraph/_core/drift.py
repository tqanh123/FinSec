"""codegraph._core.drift — is the snapshot still the tree? Three levels.

A number read from a STALE snapshot still looks perfectly normal — that is the
trap this module exists for. Levels:

    (a) banner   — a one-line warning meant for the TOP of every report that
                   reads a snapshot: "the tree moved, these numbers are stale".
    (b) strict   — the caller's `--strict` turns GRAPH drift into exit 1 for
                   CI. Drift IS red; there is no "probably still fine" middle
                   state, and no flag that force-greens it.
    (c) detail   — the full report: which files changed / vanished / were
                   born since the snapshot pass. `detail_lines` renders it;
                   the caller decides where those lines go (`--text` stdout, or a field of the envelope's `data`).

GRAPH DRIFT VS COSMETIC DRIFT
    The snapshot's `file_sha` hashes the RAW text — a comment-only edit
    changes it, while every symbol body-sha (comments stripped) stays put.
    Recomputing through the same adapter lets this module DECOMPOSE a byte
    change: files whose symbol set and body-shas are identical are
    `cosmetic` (listed, not red — the measured graph is not stale); anything
    else is `changed` (red). Crying wolf on every doc touch is how real
    drifts get ignored.

WHAT IT CANNOT SEE
    * Cosmetic-only edits (comments, docstrings the adapter strips from
      bodies) are listed but do not redden: the GRAPH cannot see them, and
      neither can this gate — by contract, not by accident.
    * A file the snapshot pass would not measure is never reported as new —
      drift judges the measured tree, not the whole repo. "Would measure" is
      the pass's OWN enumeration (`pipeline.measured_paths`: the roots via
      `discover`, honoring `scan` and a workspace's code dir, plus the test
      trees no root covers), never a second walk of its own; without the
      project descriptor the roots' paths alone are walked with the default
      `scan` and `src` code dir.
    * A file whose ROOT vanished reports as root_missing (counted, listed),
      not as one missing file per module — the unit of disappearance is real.
    * Whitespace-only body edits keep the body-sha (normalized): a reformat
      is cosmetic here even though bytes move.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .adapter import adapter_for
from .descriptor.model import ProjectDescriptor, Root
from .lang import registry as lang_registry
from .lang.base import BROKEN_PROBLEMS
from .lang.ids import PYTHON, lang_of
from .snapshot.pipeline import _adapter_set, _parse, measured_paths


def engine_mismatch(idx: dict, descriptor: "ProjectDescriptor | None") -> "dict[str, dict[str, str]]":
    """{language: {snapshot, declared}} where the snapshot's body hashes were taken by
    another engine than the one `descriptor` declares now (a hash's `<engine>:` prefix):
    every hash differs, by measurement rather than by edit."""
    out: "dict[str, dict[str, str]]" = {}
    if descriptor is None:
        return out
    for m in [m for mods in idx["units"].values() for m in mods]:
        lang = lang_of(m)
        declared = lang_registry.options_of(descriptor, lang).get("engine")
        if lang == PYTHON or lang in out or not declared:
            continue
        sha = next((s["sha"] for s in m.get("symbols", ()) if ":" in s.get("sha", "")), None)
        if sha is not None and sha.split(":", 1)[0] != declared:
            out[lang] = {"snapshot": sha.split(":", 1)[0], "declared": declared}
    return out


def _flat_symbol_shas(symbols) -> dict[str, str]:
    """name -> body sha, methods included under their own bare names.

    Accepts BOTH shapes this module compares: the adapter's `SymbolInfo`
    dataclasses (attribute access) and the snapshot index's plain dicts —
    one normalizer, no subscripting accidents at the call site.
    """
    def name(s):
        return s["name"] if isinstance(s, dict) else s.name

    def sha(s):
        return s["sha"] if isinstance(s, dict) else s.sha

    def methods(s):
        return s.get("methods", ()) if isinstance(s, dict) else s.methods

    out: dict[str, str] = {}
    for s in symbols:
        out[name(s)] = sha(s)
        for mm in methods(s):
            out[name(mm)] = sha(mm)
    return out


def drift_report(repo: Path, idx: dict,
                 descriptor: "ProjectDescriptor | None" = None) -> dict:
    """Tree vs snapshot: changed (graph drift, RED) / cosmetic (byte-only) /
    missing / new files + clean count.

    `changed` rows are (path, reason) where reason is "symbols" (the measured
    graph moved), "unparseable" (a file that no longer parses is drift, not
    a silent skip) or "partial" (a file of another language whose reader read
    around a syntax error: its symbols are not all there, so none reads as
    deleted) or "unreadable" (a record of a language `codemap.yaml` no longer
    enables: no reader of this tree reads it). A reader refusing (an engine
    missing what it needs) is `SnapshotRefused`, one breaking its contract
    `SnapshotError` -- never a crash. `cosmetic` rows are paths whose bytes moved but whose
    symbol set and body-shas did not. A snapshot whose body hashes another
    engine took (`engine_mismatch`) is compared to nothing: it says so and
    names both engines.
    """
    repo = repo.resolve()
    mismatch = engine_mismatch(idx, descriptor)
    if mismatch:
        return {"changed": [], "cosmetic": [], "missing": [], "new": [], "clean": 0, "root_missing": [],
                "engine_mismatch": mismatch, "snapshot": idx.get("snapshot"), "commit": idx.get("commit")}
    readers = None
    snap_paths: set[str] = set(idx.get("test_modules_unparsed") or ())
    changed: list[tuple[str, str]] = []
    cosmetic: list[str] = []
    missing: list[str] = []
    clean = 0
    records = [m for mods in idx["units"].values() for m in mods]
    records += list(idx.get("test_modules") or ())
    for m in records:
        snap_paths.add(m["path"])
        target = repo / m["path"]
        if not target.is_file():
            missing.append(m["path"])
            continue
        raw = hashlib.sha256(
            target.read_text(encoding="utf-8").encode()).hexdigest()[:16]
        if raw == m["file_sha"]:
            clean += 1
            continue
        # bytes moved — does the MEASURED GRAPH move with them?
        if lang_of(m) != PYTHON:
            # a path id has no container; a syntax error is a problem row, not a raise
            readers = readers or _adapter_set(repo, descriptor or ProjectDescriptor(schema_version=2, project=""))
            adapter = readers.adapter_for(target)
            if adapter is None:
                changed.append((m["path"], "unreadable"))
                continue
            parsed = _parse(adapter, target, target.read_text(encoding="utf-8"), (), m["module"])
            same = _flat_symbol_shas(parsed.symbols) == _flat_symbol_shas(m["symbols"])
            if same:
                cosmetic.append(m["path"])
            else:
                broken = any(pr.get("kind") in BROKEN_PROBLEMS for pr in parsed.problems)
                changed.append((m["path"], "partial" if broken else "symbols"))
            continue
        container = tuple(m["module"].split(".")[:-1])
        try:
            parsed = adapter_for(target).parse(
                target, target.read_text(encoding="utf-8"),
                container, m["module"])
        except SyntaxError:
            changed.append((m["path"], "unparseable"))
            continue
        if _flat_symbol_shas(parsed.symbols) != _flat_symbol_shas(m["symbols"]):
            changed.append((m["path"], "symbols"))
        else:
            cosmetic.append(m["path"])

    new_files: list[str] = []
    root_missing: list[str] = []
    roots = []
    for r in idx.get("roots", ()):
        if not (repo / r.get("path", "")).is_dir():
            root_missing.append(r.get("id", "?"))
            continue
        roots.append(Root(id=r.get("id", "?"), path=r.get("path", "."), kind=r.get("kind", "flat")))
    if roots:
        # The pass's OWN enumeration (roots through `discover`, test trees
        # through `unrooted_tests`), so a snapshot taken a second ago is never
        # drifted by a file the pass was never going to measure.
        desc = descriptor or ProjectDescriptor(schema_version=2, project=idx.get("package") or "")
        owned, tests = measured_paths(repo, desc, roots)
        new_files = sorted((owned | tests) - snap_paths)

    return {"changed": sorted(changed), "cosmetic": sorted(cosmetic),
            "missing": sorted(missing), "new": sorted(new_files),
            "clean": clean, "root_missing": sorted(root_missing),
            "snapshot": idx.get("snapshot"), "commit": idx.get("commit")}


def drifted(report: dict) -> bool:
    """GRAPH drift only — cosmetic bytes do not redden (see module docstring)."""
    return bool(report["changed"] or report["missing"] or report["new"]
                or report["root_missing"])


def banner(report: dict) -> str:
    """Level (a): the one-liner for the top of another report's output."""
    line = (f"⚠ SNAPSHOT DRIFT: {len(report['changed'])} changed, "
            f"{len(report['missing'])} missing, {len(report['new'])} new "
            f"(snapshot {report['snapshot']}) — numbers below may be stale; "
            f"rerun the snapshot pass")
    if report["cosmetic"]:
        line += f" · {len(report['cosmetic'])} cosmetic-only (not graph drift)"
    return line


def detail_lines(report: dict) -> list[str]:
    """Level (c): the full report as printable lines — never printed here,
    the caller decides where they go (`--text` stdout vs a field of the
    envelope's `data`)."""
    lines = [f"snapshot : {report['snapshot']}  (commit {report['commit']})",
             f"clean    : {report['clean']} file(s) unchanged"]
    if report["root_missing"]:
        lines.append(f"ROOTS GONE: {', '.join(report['root_missing'])}")
    for label, rows in (("CHANGED", report["changed"]), ("MISSING", report["missing"]),
                        ("NEW", report["new"])):
        lines.append(f"{label}: {len(rows)}")
        for row in rows[:25]:
            detail = f"  (was {row[1]})" if isinstance(row, tuple) else ""
            lines.append(f"  {row[0] if isinstance(row, tuple) else row}{detail}")
        if len(rows) > 25:
            lines.append(f"  … {len(rows) - 25} more")
    if report["cosmetic"]:
        lines.append(f"COSMETIC (bytes moved, graph did not): {len(report['cosmetic'])}")
        for p in report["cosmetic"][:25]:
            lines.append(f"  {p}")
    return lines
