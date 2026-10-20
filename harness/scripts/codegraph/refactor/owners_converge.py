"""codegraph.refactor.owners_converge -- run `codegraph.check.owners.assign`
to a FIXED POINT, so `hs-code refactor move --flat --converge` can plan
a move list straight from `codemap.yaml` `owners.channels`/`owners.layout`
instead of one static round: a module a single round leaves unclaimed can
still be reachable through an importer once that importer's OWN planned move
is simulated, and only a later round sees it.

Port of ob-tools' `devkit.analysis.owners.assign_owners_converged` +
`_planned_moves`/`_layout_notes` (pinned commit `60075cb`, `packages/devkit/
src/devkit/analysis/owners.py`) and `devkit.codemod.moveflat.moves_from_map`/
`destination` (same pin, `packages/devkit/src/devkit/codemod/moveflat.py`).
Adapted for this repo:

    * upstream threads a `path_remap: dict[str, str]` KEYWORD through
      `assign_owners`'s own internals (`_classify`, called at every claim
      site and at the "already home" check) so a later round's evidence
      reads a citing path through the map. This harness's `codegraph.check.
      owners.assign` carries no such parameter, and this module's own contract
      says so explicitly: CALL it, never copy its rules. `path_remap`
      here instead returns a snapshot INDEX whose every record's own `path`
      field, and every `invoked_by`/`path_drivers` STRING equal to a
      REMAPPED key, is rewritten before the index reaches `assign()`
      unmodified -- a module's own home, an importer's home (via `assign`'s
      existing import-fallback branch), and a citer's channel all read
      tomorrow's planned tree without one line of `assign`'s claim or
      depended-on logic duplicated here. The one thing this trades away:
      upstream's `_classify` never touches the PHYSICAL read `_ref_lines`
      does for a citing file's live/prose lines (nothing has really moved),
      while this rewrite touches every occurrence of a remapped string
      whether it is a path a claim reads or one the caller passes on for
      something else. See WHAT IT CANNOT SEE.
    * `--map FILE`/`--channel`/`load_layout`/`read_map`/the snapshot
      freshness check (upstream `moveflat.run`) is NOT ported -- that is
      upstream's STATIC table-FILE consumption path (a separate `obt dev
      owners --out` run, read later by a `--map` file argument, needing its
      own staleness check against HEAD). This door runs the convergence
      loop LIVE against the tree `move --flat --converge` is already
      invoked against, so there is no stale file to check freshness of.

WHAT IT CANNOT SEE
    * a module that keeps changing home past `max_rounds` without ever
      repeating one it already held -- reported the same as a genuine
      oscillation (the two are indistinguishable from outside the cap:
      both are "still changing when the budget ran out"), never silently
      accepted as converged;
    * `path_remap`'s own reach: it rewrites EVERY occurrence of a remapped
      string in `invoked_by`/`path_drivers`, not only the ones a claim
      site reads as a citing path -- a string that happens to equal an old
      path for some OTHER reason reads through the map too. Not hit by any
      of this port's three cases (each string there is a genuine citer),
      but not proven absent either;
    * every blind spot `codegraph.check.owners.assign` itself already
      carries (WHAT IT CANNOT SEE in that module) -- this loop only ever
      re-runs that measurement, never replaces it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import attrs

from codegraph.check.owners import Assignment, OwnerReport, OwnerSpec, assign

#: Cap on `assign_owners_converged`'s own round count -- a guard against a
#: table that keeps changing without ever repeating a home it already held
#: (an oscillation the history-free design below cannot otherwise tell apart
#: from a genuinely long, one-directional chain of re-homes).
DEFAULT_MAX_ROUNDS = 10


class OwnersConvergeError(Exception):
    """Cannot plan honestly: a channel names no layout destination, or the
    caller asked for fewer than one round."""


def destination(home: str, src: str, layout: Dict[str, str]) -> Optional[str]:
    """Where the layout places `src` for `home` (`channel` or
    `channel:owner`), or None when the layout places no directory for the
    channel."""
    channel, _, owner = home.partition(":")
    template = layout.get(channel)
    if template is None:
        return None
    dst_dir = template.format(owner=owner) if "{owner}" in template else template
    return (Path(dst_dir) / Path(src).name).as_posix()


def moves_from_map(rep: OwnerReport, layout: Dict[str, str],
                   channels: Optional[set] = None) -> Dict[str, str]:
    """Turn a converged ownership report into a `{src: dst}` move list, using
    the declared layout. Only `rep.assigned` modules move -- `shared`,
    `contested`, `unclaimed` and `stay` are shapes the measurement
    deliberately refused to move. A channel the layout does not place is
    refused BY NAME."""
    moves: Dict[str, str] = {}
    unplaced: set = set()
    for mod in rep.assigned:
        a = rep.homes[mod]
        home = a.home
        if not home:
            raise OwnersConvergeError(
                "%s is listed as assigned but carries no home: the table is "
                "inconsistent with itself, so nothing is moved" % mod)
        src = a.path
        if not src:
            raise OwnersConvergeError(
                "%s carries no path: a move needs a FILE" % mod)
        channel = home.partition(":")[0]
        if channels is not None and channel not in channels:
            continue
        dst = destination(home, src, layout)
        if dst is None:
            unplaced.add(channel)
            continue
        if dst != src:
            moves[src] = dst
    if unplaced:
        raise OwnersConvergeError(
            "the layout places no destination for channel(s): %s (declared: %s)"
            % (", ".join(sorted(unplaced)), ", ".join(sorted(layout)) or "nothing"))
    return moves


def path_remap(idx: Dict[str, Any], remap: Dict[str, str]) -> Dict[str, Any]:
    """A snapshot index where every record's own `path`, and every string in
    its `invoked_by`/`path_drivers` lists, is rewritten through `remap` when
    it names an old path a prior round planned to move -- lets `assign()`
    classify a module's home, an importer's home, and a citer's channel
    against tomorrow's tree without a single line of `assign`'s own claim or
    depended-on logic being touched or duplicated here. Every OTHER field
    (imports, symbols) stays exactly as measured: nothing has actually moved
    yet, so the physical shape of the tree `assign()` reads elsewhere is
    unaffected."""
    if not remap:
        return idx
    out = dict(idx)
    new_units: Dict[str, List[dict]] = {}
    for unit, recs in idx.get("units", {}).items():
        new_recs = []
        for rec in recs:
            rec = dict(rec)
            p = rec.get("path")
            if p in remap:
                rec["path"] = remap[p]
            for field in ("invoked_by", "path_drivers"):
                vals = rec.get(field)
                if vals:
                    rec[field] = [remap.get(v, v) for v in vals]
            new_recs.append(rec)
        new_units[unit] = new_recs
    out["units"] = new_units
    return out


def layout_notes(spec: OwnerSpec, layout: Dict[str, str]) -> Dict[str, str]:
    """channel -> note, for a layout directory with no `{owner}` whose own
    pattern never reads a module placed there as home: its modules cannot
    name their owner in the destination, so only the destination itself can
    say they already arrived."""
    no_home = set(spec.no_home_channels)
    notes: Dict[str, str] = {}
    for cid, rx in spec.channels:
        template = layout.get(cid)
        if template is None or "{owner}" in template or cid in no_home:
            continue
        if not rx.search("%s/_.py" % template.rstrip("/")):
            notes[cid] = "layout is a shared dir (%s); home cannot name its owner" % template
    return notes


@attrs.define(frozen=True, kw_only=True)
class ConvergenceResult:
    report: OwnerReport
    #: The round the module->home table stopped changing at -- one more
    #: than the last round that changed it, since an unchanged round is
    #: what proves the fixed point.
    rounds: int
    converged: bool
    #: module -> the round its FINAL home first appeared.
    round_of: Dict[str, int] = attrs.field(factory=dict)
    #: module -> the homes an earlier round gave it, oldest first.
    rehomed_from: Dict[str, Tuple[Optional[str], ...]] = attrs.field(factory=dict)
    #: Modules still changing home when the round cap was reached.
    oscillating: Tuple[str, ...] = ()
    layout_notes: Dict[str, str] = attrs.field(factory=dict)


def assign_owners_converged(idx: Dict[str, Any], spec: OwnerSpec, repo: Path, *,
                            layout: Optional[Dict[str, str]],
                            entrypoints: Any = None, prefix: Optional[str] = None,
                            cli_only: bool = True,
                            max_rounds: int = DEFAULT_MAX_ROUNDS) -> ConvergenceResult:
    """`assign`, iterated until the module->home table stops changing.

    A proposed move changes what channel classifies the CITING file it came
    from -- the very thing round 1 itself measured claims against -- so a
    later round can both resolve a module round 1 left unclaimed and re-home
    one round 1 already placed. Every move is SIMULATED (`path_remap`, never
    a real `git mv`), always from the module's REAL path, so a re-homed
    module moves once, straight to its last home.

    `layout=None` (no `owners.layout` declared) makes this exactly round 1 --
    there is no destination to simulate a move INTO, so nothing can cascade.
    """
    if layout is None:
        rep0 = assign(idx, spec, repo, entrypoints, prefix, cli_only)
        return ConvergenceResult(report=rep0, rounds=1, converged=True,
                                 round_of={m: 1 for m in rep0.assigned})
    if max_rounds < 1:
        raise OwnersConvergeError("max_rounds must be >= 1, got %r" % (max_rounds,))

    real_path_of = {rec["module"]: rec.get("path")
                    for recs in idx.get("units", {}).values() for rec in recs}
    table: Dict[str, Assignment] = {}
    round_of: Dict[str, int] = {}
    seen_homes: Dict[str, List[Optional[str]]] = {}
    rep: Optional[OwnerReport] = None
    round_no = 0
    touched: List[str] = []
    converged = False
    for round_no in range(1, max_rounds + 1):
        remap: Dict[str, str] = {}
        for m, a in table.items():
            if a.home and a.path:
                dst = destination(a.home, a.path, layout)
                if dst and dst != a.path:
                    remap[a.path] = dst
        rep = assign(path_remap(idx, remap), spec, repo, entrypoints, prefix, cli_only)
        touched = []
        for m in rep.assigned:
            a = rep.homes[m]
            if m in table and table[m].home == a.home:
                continue
            real_path = real_path_of.get(m)
            if real_path:
                a = attrs.evolve(a, path=real_path)
            table[m] = a
            round_of[m] = round_no
            seen_homes.setdefault(m, []).append(a.home)
            touched.append(m)
        if not touched:
            converged = True
            break

    assert rep is not None
    final_homes = dict(rep.homes)
    final_homes.update(table)
    rep.homes = final_homes
    rehomed_from = {m: tuple(h[:-1]) for m, h in seen_homes.items() if len(h) > 1}
    # A move whose destination is the path it already has is no move at
    # all: the channel's own pattern could not see it was home (a shared
    # layout dir), the layout can.
    for m, a in table.items():
        if a.home and a.path and destination(a.home, a.path, layout) == a.path:
            rep.homes[m] = attrs.evolve(a, kind="stay", stay_reason="already home (layout)")
            round_of.pop(m, None)
            rehomed_from.pop(m, None)

    return ConvergenceResult(
        report=rep, rounds=round_no, converged=converged, round_of=round_of,
        rehomed_from=rehomed_from,
        oscillating=() if converged else tuple(sorted(touched)),
        layout_notes=layout_notes(spec, layout))
