"""codegraph.change.impact — blast-radius analysis over one or two snapshots.

Port of ob-tools' `devkit.analysis.impact` (pinned commit `60075cb`,
`packages/devkit/src/devkit/analysis/impact.py`). Answers "if I change this,
what explodes?": fan-in (callers) of one symbol, or of a whole changed-symbol
set, over the `callers` index the snapshot already carries (no re-parsing,
no re-inverting of `refs` here).

TWO TREES, ONE ANSWER
    A symbol's blast radius is the UNION of its callers in the OLD tree and
    the NEW tree, reported SEPARATELY (never merged): "who calls this in the
    OLD tree" and "in the NEW tree" are different facts, and merging them
    would hide which half is which.

Adapted for this repo:
    * `walk_symbols`/`Verdict`/`SnapshotMissing`/`load_dir` come from the
      already-ported `codegraph._core` kernel — no re-port of those.
      `qualified_index`/`callers_index`/`CallersIndexMissing` are REBOUND
      (not defined here) to `codegraph_change_reader`'s own objects — see
      the rebind comment just above where each used to live in this file.
    * The CLI's own `--changed FILE` (a `graph changed` envelope `data` or a
      bare symbol list) stays a CLI-layer concern (`codegraph.change.verbs.
      py`'s `_load_targets`) — this module only ever sees an already-resolved
      `list[str]` of qualified target names, never a file path.
    * `targets_from_files` is NEW here (obt's `--changed` never took bare
      file paths): `hs-code change impact --base <ref>` with no explicit
      `--changed`/symbol defaults its target set from `git diff --name-only`,
      which names FILES, not symbols — this is the bridge from one to the
      other, matched against the module `path` a snapshot already carries.

WHAT IT CANNOT SEE
    * fan-in is a LOWER BOUND: attribute calls, getattr, dict dispatch and
      callbacks do not resolve into `refs`. Zero callers means "the AST
      found none", never "nobody calls this".
    * Refs pointing outside the measured roots are counted as `outside_tree`
      and printed — a zero fan-in WITH outside-tree hits is "callers exist
      where we do not look".
    * Symbols link across trees by NAME (qualified `module::name`); a rename
      degenerates to removed+added, never guessed.
    * Tier warnings need `labels` (group -> tier int): without it the report
      is group-aware but rank-blind. Lower tier number = more foundational; a
      caller at a LOWER tier reaching a HIGHER tier is the no-upward-import
      direction and is warned.
    * `targets_from_files` matches by the module's CURRENT path in the index
      it is run against; a file renamed between the two trees resolves only
      on the side whose path still matches — same "no guessing" law as
      everything else here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import codegraph_change_reader
from ambiguous_input import Ambiguous
from codegraph._core.query.engine import walk_symbols


@dataclass
class ImpactResult:
    """The one blast-radius result shape: an exit code, printable text, and a
    JSON-ready payload — mirrors `codegraph._core.query.engine.QueryResult`'s
    own (code, text, data) shape without importing it: this module answers a
    TWO-snapshot question, a different kernel concern than `QueryEngine`'s
    single-index queries, so it carries its own small result type rather than
    borrowing one documented as that engine's own."""
    code: int
    text: str
    data: Any = None


# `qualified_index`/`callers_index`/`CallersIndexMissing` MOVED to
# `codegraph_change_reader` (a flat module outside this package — see that
# module's own docstring: `hs_run_facts.code_callers` needs the same symbol
# table without importing the whole measurement engine). Rebound to the SAME
# objects, not reimplemented beside them — the identical pattern `codegraph.
# change.selection.import_channel` already uses for its own move.
# `qualified_index is codegraph_change_reader.qualified_index` holds — one
# definition. Its value shape widened to a SUPERSET (`module`/`path`/`dist`/
# `parent`/`lines`) that still carries the `dist`/`parent` fields
# `tier_warnings`/this module's own callers read below, alongside the
# `lines` field `hs_run_facts.code_callers` needs — a narrower rebind would
# have silently dropped `dist` and broken `tier_warnings`'s cross-group /
# no-upward-import detection without a single test failing loudly for the
# right reason.
qualified_index = codegraph_change_reader.qualified_index
CallersIndexMissing = codegraph_change_reader.CallersIndexMissing
callers_index = codegraph_change_reader.callers_index


def _strip_comment(line: str) -> str:
    """Code part of a line (a `#` inside a string literal is over-cut — the
    scan is a lower-bound hint, not a parser, and says so in its label)."""
    return line.split("#", 1)[0]


def text_refs(idx: dict, target: str, root: Path) -> "tuple[list[str], int]":
    """Textual mentions of `target`'s bare name in the snapshot's own files.

    The AST cannot see a module reached through an injected handle, getattr
    or a string table, so a zero fan-in for a constant is the common false
    negative. This is the fallback: every whole-word occurrence of the bare
    name in the files the snapshot measured, minus the definition's own
    lines and comments.

    -> (["path:line", ...], files_missing). Missing files are counted, never
    skipped silently: a scan over half the tree must say so.
    """
    module, _, bare = target.rpartition("::")
    pat = re.compile(r"(?<![\w])" + re.escape(bare.rsplit(".", 1)[-1]) + r"(?![\w])")
    hits: "list[str]" = []
    missing = 0
    seen: "set[str]" = set()
    for _unit, m, s, _parent in walk_symbols(idx):
        if m["path"] in seen:
            continue
        seen.add(m["path"])
        f = root / m["path"]
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            missing += 1
            continue
        own: "set[int]" = set()
        if m["module"] == module:
            for _u, m2, s2, _p in walk_symbols(idx):
                if m2 is m and f"{m2['module']}::{s2['name']}" == target:
                    a, b = s2.get("lines", (0, 0))
                    own = set(range(a, b + 1))
        for n, line in enumerate(lines, 1):
            if n in own:
                continue
            if pat.search(_strip_comment(line)):
                hits.append(f"{m['path']}:{n}")
    return hits, missing


def blast_radius(roots: "list[str]", callers: "dict[str, list[str]]",
                 depth: int = 1) -> "dict[str, int]":
    """BFS over fan-in: qualified symbol -> distance from the changed set.

    depth=1 is direct callers; every extra level pulls in callers-of-callers.
    A symbol reachable at two distances keeps the SHORTEST — that is the
    distance a change actually travels to reach it.
    """
    radius: "dict[str, int]" = {r: 0 for r in roots}
    frontier = list(roots)
    for d in range(1, depth + 1):
        nxt: "list[str]" = []
        for sym in frontier:
            for c in callers.get(sym, ()):
                if c not in radius:
                    radius[c] = d
                    nxt.append(c)
        frontier = nxt
        if not frontier:
            break
    return radius


class SymbolNotFound(LookupError):
    """No symbol named this exists in the snapshot. The ORDINARY shape a
    diff-generated target takes in one of the two trees — a symbol the diff
    ADDS is, by definition, not in the old tree; one it REMOVES is not in
    the new one. `run_impact(strict=False)` catches this specifically and
    reports it as an absence, never a run-ending error."""


class SymbolAmbiguous(Ambiguous):
    """A bare name matches more than one definition — never silently
    picked, in EITHER resolution mode: this is a real "which one do you
    mean" problem, not "the tree does not have it". It ends the run with
    exit 2 and every definition, the refusal every hs-code lookup gives."""

    def __init__(self, name: str, hits: "list[str]") -> None:
        super().__init__("symbol", name, hits, "use the qualified form")


def _resolve_symbol(symbols: "dict[str, dict]", name: str) -> str:
    """Bare name resolves only when unique — two trees can both define `run`."""
    if name in symbols:
        return name
    hits = sorted(q for q in symbols if q.endswith("::" + name))
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SymbolAmbiguous(name, hits)
    raise SymbolNotFound(f"symbol {name!r} not found in this snapshot")


def tier_warnings(radius: "dict[str, int]", callers: "dict[str, list[str]]",
                  symbols: "dict[str, dict]", labels: "dict[str, int]") -> "list[str]":
    """Cross-group and no-upward edges inside the radius. Carried data only:
    groups come from the snapshot payload, ranks from `labels` — this engine
    never ranks, only reads both."""
    out: "list[str]" = []
    for target in sorted(radius):
        t_dist = symbols.get(target, {}).get("dist")
        if not t_dist:
            continue
        for caller in callers.get(target, ()):
            info = symbols.get(caller)
            if info is None:
                continue
            c_dist = info.get("dist")
            if not c_dist:
                continue
            if c_dist != t_dist:
                out.append(f"cross-group: {caller} ({c_dist}) -> {target} ({t_dist})")
                ct, tt = labels.get(c_dist), labels.get(t_dist)
                if ct is not None and tt is not None and ct < tt:
                    out.append(f"  no-upward: caller tier {ct} reaches tier {tt}")
    return out


def targets_from_files(idx: dict, files: "Iterable[str]") -> "list[str]":
    """Every symbol whose module `path` is one of `files` -> sorted qualified
    names. The bridge `--base`'s DEFAULT changed set needs:
    `git diff --name-only` names FILES, but `blast_radius` only knows
    SYMBOLS. A path with no match (deleted, or renamed away in this index)
    contributes nothing — same "removed symbols have no radius left" law
    obt's own `--changed` JSON loader already applies to a `removed` entry.
    """
    wanted = set(files)
    out = {f"{m['module']}::{s['name']}"
          for _unit, m, s, _parent in walk_symbols(idx) if m["path"] in wanted}
    return sorted(out)


def run_impact(idxs: "list[dict]", *, symbol: "str | None" = None,
               changed_targets: "list[str] | None" = None, depth: int = 1,
               labels: "dict[str, int] | None" = None,
               scan_root: "Path | None" = None,
               scan_roots: "list[Path | None] | None" = None,
               no_text_scan: bool = False, strict: bool = True,
               tree_names: "list[str] | None" = None) -> ImpactResult:
    """Blast radius of `symbol` (or every name in `changed_targets`) across
    every snapshot in `idxs` — one caller-index inversion, one BFS, one
    (optional) zero-caller text-scan fallback per snapshot, reported
    separately per snapshot (see the module's own TWO TREES ONE ANSWER note).

    Exactly one of `symbol`/`changed_targets` must be given (a caller
    resolving neither is a usage error, raised here rather than swallowed
    into a confusing empty report). `changed_targets == []` is a REAL,
    distinct answer ("nothing changed" — not "no symbol picked"): the caller
    passes the empty list on purpose (e.g. `git diff` found no touched
    file), and this returns a clean 0 without touching a single snapshot.

    `strict` (default True, obt's own all-or-nothing law): a target not
    found in a snapshot blanks that WHOLE snapshot's report (no radius
    computed, that tree's exit turns 1) — correct for a single, DELIBERATELY
    named target (a caller-typed `SYMBOL`, or an explicit `--changed FILE`):
    a name that resolves nowhere is very likely a typo, worth a loud error.

    `strict=False` resolves each target INDEPENDENTLY per snapshot: a target
    missing from one tree is recorded under that tree's own `absent` list
    and simply contributes no root there — it does NOT blank the tree or
    turn the exit red by itself. This is the shape a target list DERIVED
    from a diff needs: the most common real change adds a symbol (absent
    from the old tree, by definition) or removes one (absent from the new
    tree) — an all-or-nothing resolution would blank the tree for exactly
    the everyday case, hiding every OTHER target's radius along with it. An
    ambiguous bare name (`SymbolAmbiguous`, more than one candidate) is
    still a real error in EITHER mode — it means "cannot tell which one you
    meant", never "the tree does not have it" — and ends the run: it is
    raised, and the verb refuses with exit 2 and every definition.

    `tree_names` labels each tree in the report (default: its snapshot name).
    Two snapshots of one commit carry one name — an uncommitted change
    measured against its own HEAD — and keyed by that name the second tree
    overwrote the first.

    `scan_roots` gives each tree the directory its own files are read from for
    the text scan (`scan_root` is the one-root shorthand). One root for two
    trees read the old snapshot's line numbers out of the new tree's files.
    """
    labels = labels or {}
    if symbol:
        targets = [symbol]
    elif changed_targets is not None:
        targets = changed_targets
        if not targets:
            return ImpactResult(
                0, "impact: no changed/added symbols — nothing to explode",
                {"targets": [], "trees": {}, "note": "no changed/added symbols"})
    else:
        raise ValueError("impact: give a symbol or a changed-target list")

    lines: "list[str]" = []
    payload: "dict[str, Any]" = {"targets": targets, "depth": depth, "trees": {}}
    exit_code = 0
    names = tree_names or [idx.get("snapshot", "?") for idx in idxs]
    roots_for = scan_roots or [scan_root] * len(idxs)
    for idx, tag, scan_root in zip(idxs, names, roots_for):
        symbols = qualified_index(idx)
        callers, outside, ambiguous = callers_index(idx)
        absent: "list[str]" = []
        if strict:
            try:
                roots = [_resolve_symbol(symbols, t) for t in targets]
            except SymbolAmbiguous:
                raise
            except LookupError as e:
                lines.append(f"impact: [{tag}] {e}")
                exit_code = 1
                continue
        else:
            roots = []
            for t in targets:
                try:
                    roots.append(_resolve_symbol(symbols, t))
                except SymbolNotFound:
                    absent.append(t)          # added/removed — an ordinary diff shape
            if absent:
                lines.append(
                    f"[{tag}] absent (not in this tree — likely added/removed by "
                    f"the diff): {', '.join(sorted(absent))}")
        radius = blast_radius(roots, callers, depth)
        text: "dict[str, list[str]]" = {}
        text_missing = 0
        if not no_text_scan and scan_root is not None:
            for r in roots:
                if not callers.get(r):
                    hits, miss = text_refs(idx, r, scan_root)
                    text_missing = max(text_missing, miss)
                    if hits:
                        text[r] = hits
        lines.append(
            f"[{tag}] blast radius of {len(roots)} target(s) at depth {depth}: "
            f"{len(radius)} symbol(s), outside-tree refs {outside} (fan-in floor)"
            + (f", ambiguous refs {ambiguous} (a bare name two definitions share — "
               f"given to neither)" if ambiguous else ""))
        for q in sorted(radius, key=lambda q: (radius[q], q)):
            info = symbols.get(q)
            where = info["path"] if info else "(outside this tree)"
            lines.append(f"  d{radius[q]}  {q:<64} {where}")
        warnings = tier_warnings(radius, callers, symbols, labels)
        for r, hits in text.items():
            lines.append(
                f"  text-refs of {r}: {len(hits)} textual mention(s) the AST did "
                f"not resolve (injected handle / getattr / string table) — "
                f"lower-bound hints")
            for h in hits[:20]:
                lines.append(f"    {h}")
            if len(hits) > 20:
                lines.append(f"    … {len(hits) - 20} more")
        if text_missing:
            lines.append(
                f"  text-scan: {text_missing} snapshot file(s) not found under "
                f"{scan_root} — pass a real --scan-root REPO_ROOT")
        payload["trees"][tag] = {
            "radius": radius, "outside_tree_refs": outside, "ambiguous_refs": ambiguous,
            "warnings": warnings, "text_refs": text,
            "text_scan_missing_files": text_missing,
            "absent": sorted(absent)}
        if warnings:
            lines.append(f"  warnings ({len(warnings)}):")
            for w in warnings[:20]:
                lines.append(f"    {w}")
            if len(warnings) > 20:
                lines.append(f"    … {len(warnings) - 20} more")
    return ImpactResult(exit_code, "\n".join(lines), payload)
