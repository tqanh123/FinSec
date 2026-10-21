"""`map` — the snapshot must agree with the tree, BOTH ways, in NAME and in
CONTENT, and (on the aimed snapshot) every symbol must carry a dist.

    snapshot records a file the tree does not      -> red (stale entry)
    tree has a file the snapshot does not          -> red (unrecorded file)
    both exist, file hash differs                  -> red (content drift)
    aimed snapshot: symbol with no dist             -> red (dist null)
    a dist name outside the declared list          -> red (unknown dist)

Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
portkit/gates/map_gate.py` — the name-agreement / content-drift halves carry
over unchanged; the dist halves are ADAPTED to this tree's own porting
design (see "WHERE DIST COMES FROM" below).

WHY BOTH DIRECTIONS
    A one-way check ("every snapshot entry exists on disk") is green forever
    the moment somebody stops recording new files — and the snapshot is what
    every other gate reads, so a silently short snapshot makes them all
    quietly wrong. The reverse direction is the one that catches it.

WHERE DIST COMES FROM (the one real adaptation)
    Upstream reads a MODULE's dist off a `payload` key it writes straight
    into the snapshot's own `_index.json`/unit YAML. This tree's own
    snapshots are immutable once taken (a batch's frozen snapshot is a
    promise every line written under it agrees on one commit — see
    `port_batch_reader.py`'s own module docstring), so nothing writes a
    dist back INTO one. The porting domain's own `assign` verb instead
    writes its verdicts to an out-of-band sidecar file, one entry per
    SYMBOL/METHOD (this tree's own snapshot shape already carries a `dist`
    key at that granularity — see `codegraph._core.graph.model.Graph.
    sym_dist` — finer than upstream's module-only kernel format, which this
    rule follows since the data supports it). This kind reads that sidecar
    through the `assigned` rule PARAM — a caller-declared path, resolved
    the same way `file_size`'s own `verdicts:` param is (relative to the
    repo unless already absolute) — the sidecar wins over a symbol's own
    (always-absent, in this tree) `dist` field when both are present. This
    module carries NO KNOWLEDGE of where that path lives on disk or which
    domain writes it: a project's own `codemap.yaml` declares the rule and
    supplies `assigned:` as a plain param, the exact same way it supplies
    `roots:`/`dists:`/`applies_to:` — measured by this kind's own tests,
    which assert the source text names no on-disk layout string at all.

THE DIST-LAW MODULE SET CAN BE A SECOND SNAPSHOT
    A snapshot is only ever compared against the tree it was taken of — so
    when this kind checks NAME/CONTENT agreement, `ctx.idx`/`ctx.repo` must
    be one matched pair (one snapshot, the tree it was a snapshot OF). But
    the set of modules the dist law demands completeness FROM can be a
    DIFFERENT snapshot's own module list (a porting caller's frozen SOURCE
    milestone, say, while `ctx.idx` is the current TARGET tree's own
    snapshot) — `applies_to` names that snapshot, and `ctx.snapshots`
    (`RuleContext`'s own second door, keyed by snapshot NAME) is where a
    caller supplies it when it differs from `ctx.idx`. Resolution, in
    order: `applies_to == ctx.idx`'s own `"snapshot"` name → use `ctx.idx`
    itself, AIMED (the plain in-place case, upstream's own shape,
    source==target); else `applies_to` found in `ctx.snapshots` → use
    that, AIMED (the cross-snapshot/porting case). A name found in
    NEITHER is `RulesError` (broken) ONLY when the caller supplied
    `ctx.snapshots` at all (it opted into this door and got the name
    wrong) — never a silent fallback onto `ctx.idx`'s own module list,
    which would silently check the wrong tree's completeness. A caller
    that supplies NO `ctx.snapshots` at all and names an `applies_to`
    `ctx.idx` does not match is checking a REFERENCE snapshot (see "THE
    AIMED SNAPSHOT" below) — `ctx.idx`'s own modules are still the ones
    scanned, just NOT aimed. `dists:` declared with no `applies_to` checks
    unknown-dist over `ctx.idx`'s own modules with no completeness demand
    at all (nothing to be complete AGAINST).

THE AIMED SNAPSHOT, AND WHY `dist: null` REDS ONLY THERE
    A REFERENCE snapshot (an older milestone kept for comparison, or any
    `ctx.idx` that plainly isn't the one `applies_to` names and the caller
    isn't cross-referencing through `ctx.snapshots`) is never assigned, so
    null dists there are not debt — demanding them red is demanding the
    impossible, and a gate that reds forever is a gate people learn to
    ignore. On a reference snapshot the null count is PRINTED as a note,
    never counted as findings.

THE DECLARED DIST LIST
    Every dist name appearing in the snapshot (or its sidecar) must exist in
    `dists:` — otherwise a typo mints a dist nobody declared. `UNRULED` and
    `HOST` are verdicts, not dists, and pass.

THE MILESTONE NOTE IS NEVER A RED
    `frozen_commit` pins the batch's milestone. When the tree has moved past
    it, content drift IS THE WORK OF PORTING, not a defect — a red there
    would be reds for doing the job. It is said aloud as a note so a silent
    pass cannot pretend the tree still sits on the milestone.

WHAT IT CANNOT SEE
    * Files excluded by the descriptor's `scan` patterns are not "missing" —
      they were never meant to be recorded. A wrong exclude list hides real
      files and this rule will not notice.
    * Content drift is FILE-granularity (the recorded `file_sha`), not
      symbol-granularity: a file where one function changed and another was
      ported already still reads as one drifted file.
    * A snapshot (or sidecar) where NO symbol carries a dist at all is a
      format mismatch / never-assigned batch, not mass debt: reported as a
      note, never as a thousand null findings.
    * Generated files that appear between snapshot and check are reported as
      unrecorded until the snapshot is retaken — that is the intended
      answer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)

_VERDICTS = {"UNRULED", "HOST", "MIXED"}


class MapCheck:
    CAUSES = ("stale-entry", "unrecorded-file", "content-drift", "dist-null",
              "unknown-dist")

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        self.roots: "list[str]" = list(params.get("roots") or ["."])
        self.include: "tuple[str, ...]" = tuple(params.get("include") or ("*.py",))
        self.exclude: "tuple[str, ...]" = tuple(params.get("exclude") or ())
        self.dists: "set[str]" = set(params.get("dists") or ())
        self.applies_to: "str | None" = params.get("applies_to")
        self.frozen_commit: "str | None" = params.get("frozen_commit")
        raw_assigned = params.get("assigned")
        self.assigned: "Path | None" = Path(raw_assigned) if raw_assigned else None
        # the dist laws run only when DECLARED (`dists:` and/or `applies_to`):
        # a pure name-agreement pass over a snapshot nobody has assigned yet
        # is not dist debt.
        self.dist_laws = bool(self.dists or self.applies_to)
        if self.dist_laws and self.assigned is None:
            raise RulesError(
                f"rule {rule_id!r}: kind 'map' needs param `assigned` (the "
                f"assignment sidecar's path) when `dists`/`applies_to` is "
                f"declared — the dist laws read a symbol's dist from that "
                f"file, never a guess")

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.idx is None:
            raise RulesError(f"rule {self.rule_id!r}: kind 'map' needs a snapshot")
        modules: "list[dict]" = []
        for mods in (ctx.idx.get("units") or {}).values():
            modules.extend(mods)
        recorded = {m["path"] for m in modules if m.get("path")}
        on_disk: "set[str]" = set()
        for root in self.roots:
            base = ctx.repo / root
            if not base.exists():
                raise RulesError(
                    f"rule {self.rule_id!r}: root {root!r} does not exist — "
                    f"a missing root would read as 'nothing to check'")
            for pat in self.include:
                for p in base.rglob(pat):
                    if any(part in self.exclude for part in p.parts):
                        continue
                    on_disk.add(p.relative_to(ctx.repo).as_posix())

        v: "list[Violation]" = []
        notes: "list[str]" = []
        for path in sorted(recorded - on_disk):
            v.append(Violation(self.rule_id, "stale-entry",
                               f"snapshot records {path}, tree has no such file",
                               {"path": path}))
        for path in sorted(on_disk - recorded):
            v.append(Violation(self.rule_id, "unrecorded-file",
                               f"{path} exists but the snapshot does not record it",
                               {"path": path}))

        # ── content drift: the recorded file hash vs the file on disk ────
        drifted = 0
        for m in modules:
            rel, want = m.get("path"), m.get("file_sha")
            if not rel or not want or rel not in on_disk:
                continue
            try:
                text = (ctx.repo / rel).read_text(encoding="utf-8",
                                                   errors="replace")
            except OSError:
                continue
            if hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] != want:
                drifted += 1
                v.append(Violation(
                    self.rule_id, "content-drift",
                    f"{rel} on disk no longer hashes to the recorded "
                    f"`file_sha` — the body changed after the snapshot",
                    {"path": rel}))

        # ── dist laws: null on the AIMED snapshot, names outside the list ─
        # (opt-in: see __init__; sidecar wins over a symbol's own field)
        assigned_map: "dict[str, str]" = {}
        if self.dist_laws and self.assigned is not None:
            p = self.assigned if self.assigned.is_absolute() else ctx.repo / self.assigned
            if p.is_file():
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RulesError(
                        f"rule {self.rule_id!r}: {p} does not parse as JSON "
                        f"({exc})") from exc
                if not isinstance(loaded, dict):
                    raise RulesError(
                        f"rule {self.rule_id!r}: {p} does not parse to a "
                        f"mapping (got {type(loaded).__name__})")
                assigned_map = loaded

        dist_idx: "dict | None" = None
        aimed = False
        if self.dist_laws:
            if self.applies_to is None:
                dist_idx = ctx.idx
            elif ctx.idx.get("snapshot") == self.applies_to:
                dist_idx = ctx.idx
                aimed = True
            elif self.applies_to in (ctx.snapshots or {}):
                dist_idx = ctx.snapshots[self.applies_to]
                aimed = True
            elif ctx.snapshots:
                # the caller opted into the second door (it supplied AT
                # LEAST one entry) and the name it gave isn't in there —
                # genuinely broken wiring, never a silent fallback.
                raise RulesError(
                    f"rule {self.rule_id!r}: kind 'map' cannot find snapshot "
                    f"{self.applies_to!r} for its dist-law module set — "
                    f"checked the given snapshot's own name and "
                    f"`ctx.snapshots`, found neither")
            else:
                # no `ctx.snapshots` supplied at all: `ctx.idx` is a
                # REFERENCE snapshot — an older milestone kept for
                # comparison, never the one `applies_to` names — checked
                # for its own name/hash agreement same as any other, but
                # its nulls are not debt (upstream's own "aimed" shape,
                # `map_gate.py`'s own module docstring).
                dist_idx = ctx.idx

        dist_modules: "list[dict]" = []
        for mods in (dist_idx.get("units") or {}).values() if dist_idx is not None else ():
            dist_modules.extend(mods)

        carries_dist = False
        nulls = 0
        for m in dist_modules:
            for s in m.get("symbols") or []:
                for h in [s] + (s.get("methods") or []):
                    key = f"{m['module']}::{h['name']}"
                    d = assigned_map.get(key, h.get("dist"))
                    if d is not None:
                        carries_dist = True
                    if d is None:
                        nulls += 1
                        if aimed:
                            v.append(Violation(
                                self.rule_id, "dist-null",
                                f"{key} carries no dist — this is the "
                                f"snapshot `applies_to` aims at, so a "
                                f"finished assignment pass leaves no null "
                                f"behind",
                                {"path": m.get("path"), "symbol": key}))
                        continue
                    if self.dists and d not in self.dists and d not in _VERDICTS:
                        v.append(Violation(
                            self.rule_id, "unknown-dist",
                            f"{key} carries dist `{d}` — outside the "
                            f"declared `dists:` list (a typo mints a dist "
                            f"nobody declared)",
                            {"path": m.get("path"), "symbol": key, "dist": d}))
        if nulls and not aimed:
            why = ("a REFERENCE snapshot is never assigned — nulls here are "
                   "not debt" if self.applies_to
                   else "no `applies_to` declared — this rule cannot know "
                        "which snapshot the assignment must be complete "
                        "against")
            notes.append(f"{nulls} symbol(s) carry no dist; {why}")
        if self.dist_laws and dist_modules and not carries_dist:
            notes.append("NO symbol carries a dist at all — the assign pass "
                         "has never run against this snapshot (or its "
                         "sidecar is empty)")

        # ── the milestone note: said aloud, never a red ───────────────────
        if self.frozen_commit and ctx.idx.get("commit") != self.frozen_commit:
            notes.append(
                f"the tree sits at `{ctx.idx.get('commit', '?')[:8]}` while "
                f"the batch pins `{self.frozen_commit[:8]}` — content drift "
                f"here IS the work of porting, not a defect")

        if not on_disk:
            notes.append(f"0 files matched under {self.roots} — an empty scan "
                         f"is not a clean tree; check the roots and patterns")
        if drifted and self.frozen_commit and ctx.idx.get("commit") != \
                self.frozen_commit:
            notes.append(f"{drifted} drifted file(s) counted ABOVE are against "
                         "a moved tree — read them as the porting backlog")
        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes),
                          metrics={"files": len(on_disk), "drifted": drifted,
                                   "nulls": nulls})


register_kind("map", languages=("python",))(require_params("roots")(MapCheck))
