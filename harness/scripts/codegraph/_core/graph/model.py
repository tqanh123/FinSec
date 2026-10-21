"""The resolved edge graph over a snapshot — the single door for edge walking.

Port of the frozen tool's `core/edges.py`. Four traps live here, each
one paid for on the real tree, and each one returns if rewritten elsewhere:

  1. A target that is a UNIT (not module/symbol) once made a gate pick "the
     dist of the FIRST module" in that unit — six edges into a tier-3 symbol
     validated as tier-0. Green over something the gate never saw.
  2. A target pointing into a MIXED file without naming a symbol cannot be
     attributed to the file's dist — that would fake-red twice.
  3. A SOURCE inside a mixed file cannot attribute its import edges to anyone.
  4. `src is None` / `dst is None` means UNRESOLVED — never "valid".

THE GROUP DIMENSION
    The `dist` field names are HISTORICAL (byte parity with the porting
    snapshot format): semantically this is the owning GROUP of a module or
    symbol — for workspace roots the package directory, for the porting domain
    the dist. The kernel carries the values; ladder ranking (tiers) is a
    porting concern supplied via the `ladder` protocol.

    For trees the port never touched (`dist` absent everywhere), the group
    comes from the descriptor's DECLARED `groups:` — an ordered list of
    {name, match} where match globs the module's repo-relative FILE PATH and
    the first matching entry wins. A module that HAS a dist keeps it: the
    declaration only fills the empty field, never overrides the port.

WHAT IT CANNOT SEE
    * Symbol edges are a LOWER BOUND (attribute calls, getattr, dict dispatch,
      callbacks do not resolve) — see the adapter contract.
    * The graph does not know which edge is DEAD code and passes no verdict;
      law lives in the caller.
    * Target existence is not checked: a group importing a nonexistent group
      is the map gate's business, not the walker's.
    * Declared groups are PATH GLOBS, not semantics: two directories glued
      into one group by a catch-all pattern read as one layer here while
      nothing but the pattern says so. A module matching NO pattern keeps
      group None — its edges stay undecided, which the caller must count,
      not bless.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..lang.ids import lookup_keys


class HasLadder(Protocol):
    dist_tier: dict[str, int]


def _path_glob(rel: str, pattern: str) -> bool:
    """One repo-relative path against one declared group pattern.

    `dir/**` matches the whole subtree (`dir` itself included); every other
    pattern is a FULL-PATH fnmatch. Deliberately NO basename fallback: a bare
    filename matching at any depth would make group membership depend on which
    directories happen to exist, not on what the declaration says.
    """
    if pattern.endswith("/**"):
        return rel == pattern[:-3] or rel.startswith(pattern[:-2])
    return fnmatch.fnmatch(rel, pattern)


#: the group rule, for a reader outside the graph (coverage floors group files by it)
path_glob = _path_glob


@dataclass(frozen=True)
class Edge:
    """One resolved edge. TWO INDEPENDENT AXES, deliberately not merged:

        axis 1  `src`/`dst` are GROUP ids -> tier verdict: legal / reverse / undecided
        axis 2  `src_root`/`dst_root` are ROOT ids -> does the edge cross trees

    Folding "bridge" into a fourth edge state would let a genuinely REVERSE
    edge hide inside "bridge, normal while porting". Two axes: that edge is
    red on axis 1 AND counted on axis 2. Direction is kept — a new tree
    borrowing from the old one is normal port debt; the reverse direction
    cannot be paid down by porting further.

    Blind spot: `src_root`/`dst_root` are None when the target matches no real
    module — such an edge is NEITHER cross-root NOR same-root.

    No rule kind reads axis 2 today (`_core.rules.kinds` walks edges on axis 1
    only); the fields stay on every `Edge` because `walk()` already resolves
    both roots to build axis 1, and a future cross-root check reads them
    directly (`e.src_root and e.dst_root and e.src_root != e.dst_root`) rather
    than re-deriving root identity from scratch.
    """

    kind: str                  # module | lazy | symbol
    src: str | None            # group of this end
    dst: str | None            # group of the other end
    label: str
    same_file: bool            # both ends inside ONE file split across groups
    src_root: str | None       # root id of this end
    dst_root: str | None       # root id of the other end


#: Signature of the verdict callback.
Judge = Callable[[Edge], None]


@dataclass
class Graph:
    """A snapshot resolved: group of every module/symbol, which files are mixed."""

    mod_dist: dict[str, str | None] = field(default_factory=dict)
    mod_root: dict[str, str | None] = field(default_factory=dict)
    mod_unit: dict[str, str] = field(default_factory=dict)
    sym_dist: dict[str, str | None] = field(default_factory=dict)
    unit_dists: dict[str, set[str | None]] = field(default_factory=dict)
    mixed_dist: set[str] = field(default_factory=set)
    idx: dict = field(default_factory=dict)
    ladder: object = None

    @classmethod
    def from_snapshot(cls, idx: dict, ladder: HasLadder | None = None,
                      groups: "list[dict] | tuple[dict, ...] | None" = None) -> "Graph":
        """Resolve the graph; `groups` fills group membership where `dist` is absent.

        `groups` entries are {name, match}: match globs the module's
        repo-relative path, FIRST match wins. A module with a real dist is
        never touched, and neither are its symbols — the port's own verdicts
        stay byte-identical. Only where the fill happened do symbols without
        a dist of their own inherit the module's declared group.
        """
        compiled = [(str(g["name"]), tuple(g["match"])) for g in (groups or ())]

        def _fill(m: dict) -> tuple[str | None, bool]:
            d = m.get("dist")
            if d:
                return d, False
            rel = m.get("path")
            if rel is None:
                return None, False
            for name, patterns in compiled:
                if any(_path_glob(rel, p) for p in patterns):
                    return name, True
            return None, False

        g = cls(idx=idx, ladder=ladder)
        for unit, mods in idx["units"].items():
            for m in mods:
                mdist, filled = _fill(m)
                g.mod_dist[m["module"]] = mdist
                g.mod_root[m["module"]] = m.get("root")
                g.mod_unit[m["module"]] = unit
                g.unit_dists.setdefault(unit, set()).add(mdist)
                dset: set[str | None] = set()
                for s in m["symbols"]:
                    for h in [s] + s.get("methods", []):
                        hdist = h.get("dist")
                        if filled and not hdist:
                            hdist = mdist
                        g.sym_dist[f"{m['module']}::{h['name']}"] = hdist
                        dset.add(hdist)
                dset.discard(None)
                if len(dset) > 1:
                    g.mixed_dist.add(m["module"])
        return g

    def dist_of_target(self, tgt: str, lang: "str | None" = None) -> str | None:
        """Which group an import target belongs to. None = UNRESOLVED.

        Longest-prefix match against real modules (trap 1). No module match:
        fall to the unit ONLY if that unit holds exactly one group — a
        multi-group unit returns None ("undecided", not "valid").
        """
        if tgt in getattr(self.ladder, "dist_tier", {}):
            return tgt
        if "::" in tgt:
            if tgt in self.sym_dist:
                return self.sym_dist[tgt]
            tgt = tgt.split("::", 1)[0]
        for cand in lookup_keys(tgt, lang):  # a top-level module has a one-part name
            if cand in self.mod_dist:
                return self.mod_dist[cand]
        u = self._unit_of(tgt, lang)
        dset = self.unit_dists.get(u) if u else None
        if dset and len(dset) == 1:
            return next(iter(dset))
        return None

    def _unit_of(self, mod: str, lang: "str | None" = None) -> str | None:
        """Unit of the longest matching module prefix. None = no module matched
        (the old `parts[1]` guess returned None for EVERY edge once unit keys
        gained their root prefix — a whole resolution branch silenced)."""
        for cand in lookup_keys(mod, lang):
            u = self.mod_unit.get(cand)
            if u:
                return u
        return None

    def root_of_target(self, tgt: str, lang: "str | None" = None) -> str | None:
        """Which root an edge target lives in. None = no real module matched."""
        for cand in lookup_keys(tgt, lang):
            if cand in self.mod_root:
                return self.mod_root[cand]
        return None

    def walk(self, judge: Judge,
             on_mixed_target: Callable[["Edge", str], None] | None = None,
             include_type_only: "bool | None" = None) -> None:
        """Walk EVERY edge, calling `judge(Edge)`; a target into a mixed file
        without a symbol goes to `on_mixed_target(Edge, tgt)`.

        Three kinds, counted SEPARATELY: module | lazy | symbol. One merged
        number hides the expensive story — a reverse edge living only at lazy
        level imports fine and dies at runtime, on exactly the CI branch that
        never runs. Mixed-file targets still reach `on_mixed_target` with
        `dst=None` (unattributable) but keep both roots — trap 2; an early
        `continue` here once erased every such edge from BOTH axes.

        A fourth kind, `type_only` (`imports_type_only`), when `include_type_only`
        says so; None means by the record's language: a pack's record (`lang`)
        counts it -- an `import type` is coupling -- a Python one does not.
        """
        for mods in self.idx["units"].values():
            for m in mods:
                # src through the RESOLVED map, not the raw payload: the fill
                # from declared groups lives in mod_dist, and reading m["dist"]
                # here would undeclare every group on a dist-less tree.
                src = self.mod_dist.get(m["module"], m.get("dist"))
                keys = [("imports_module_level", "module"), ("imports_function_local", "lazy")]
                if include_type_only or (include_type_only is None and m.get("lang")):
                    keys.append(("imports_type_only", "type_only"))
                for key, kind in keys:
                    for tgt in m.get(key, []):
                        label = f"{m['module']}  ->  {tgt}"
                        e = Edge(kind=kind, src=src, dst=None, label=label,
                                 same_file=m["module"] in self.mixed_dist,
                                 src_root=m.get("root"),
                                 dst_root=self.root_of_target(tgt, m.get("lang")))
                        if "::" not in tgt and tgt in self.mixed_dist:
                            if on_mixed_target is not None:
                                on_mixed_target(e, tgt)
                            continue
                        judge(Edge(kind=kind, src=src, dst=self.dist_of_target(tgt, m.get("lang")),
                                   label=label,
                                   same_file=m["module"] in self.mixed_dist,
                                   src_root=m.get("root"),
                                   dst_root=self.root_of_target(tgt, m.get("lang"))))
        for mods in self.idx["units"].values():
            for m in mods:
                for s in m["symbols"]:
                    for h in [s] + s.get("methods", []):
                        for r in h.get("refs", []):
                            if r not in self.sym_dist:
                                continue        # points outside the tree — not ours
                            own = f"{m['module']}::{h['name']}"
                            judge(Edge(kind="symbol",
                                       src=self.sym_dist.get(own, h.get("dist")),
                                       dst=self.sym_dist[r],
                                       label=f"{own}  ->  {r}",
                                       same_file=r.rsplit("::", 1)[0] == m["module"],
                                       src_root=m.get("root"),
                                       dst_root=self.root_of_target(r, m.get("lang"))))
