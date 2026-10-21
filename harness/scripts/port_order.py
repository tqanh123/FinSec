"""port_order.py — ranks a port ledger's TODO listing by the target repo's
OWN declared tier ladder, as one FLAT module under `harness/scripts/`
(matches the `port-port` coverage-floor glob, sibling of `port_ledger_
reader.py`/`port_ledger_spec.py`).

Ported (algorithm-faithful, GENERALIZED) from `ob-tools@60075cb:packages/
portkit/src/portkit/batch/port_order.py`'s own `Order` class. Upstream's
own module docstring already states the rule this module now actually
follows — `port_verbs.py`'s own module docstring: "Ordering (`todo`) reads
tier ranks from the snapshot's own `tiers` table; a node whose tier is
unknown sorts LAST, visibly." But `Order.__init__` did not read one: it
hard-coded a six-name OpsBrain ladder (`platform`/`contract`/`sdk`/
`profile`/`service`/`deployment` -> 1/0/2/3/4/5) and only ever consulted the
snapshot's own `tiers` table for NAME MEMBERSHIP, never for the RANK
number. A repo whose dists are tiered `base`/`edge` (this harness's own
`codemap.yaml` already carries exactly this shape, `rules: [{kind:
layer_direction, tiers: {base: 0, rest: 1}}]`) ported through the
hard-coded table and got two UNKNOWN tiers, tied, sorted by dist name only
— which is silent proof the six-name dict was never a real ladder, only one
target's institutional knowledge smuggled into port physics. Dropped
outright here, not adapted: no literal tier name appears in this module.

WHERE THE RANK NUMBER COMES FROM — `tier_ranks()` below reads the TARGET's
own `codemap.yaml` for a rule whose `kind` already loads exactly the
`{name: rank}` int-mapping shape this ladder needs (`codegraph/_core/rules/
kinds/layer_direction.py`, `same_tier_edge.py` — `git grep -n "tiers"
harness/scripts/codegraph harness/scripts/port_*.py` finds both; `packaging`'s
own `tiers` param is a bare SET of names, membership only, no rank, and is
not read here). No second schema: this is the ONE place in the whole tree a
named tier already carries an integer rank, so `port status`'s own ordering
reuses it instead of inventing a second `tiers:` shape under `.port/`. A
target descriptor declaring no such rule yields an EMPTY ladder — every
tier then reads as unknown, the listing falls back to dist-name order, and
`port status` says so in its own `warnings`. A `codemap.yaml` the descriptor
door refuses raises `LadderError` rather than reading as "no ladder".

WHAT THIS MODULE CANNOT SEE (inherited from upstream, unchanged):
  * The ladder itself is trusted: a rank mapped to the wrong tier name
    produces confident nonsense, same as upstream's own caveat.
  * A node missing from the snapshot, or whose module carries no `dist`,
    sorts LAST — not interleaved with a ranked one, which would make the
    ladder read as an architecture statement it is not backing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from codegraph._core.descriptor import DescriptorError, load


#: Rule kinds this harness already loads a `{name: rank}` INTEGER mapping
#: for (see the module docstring). `packaging`'s own `tiers` param is a bare
#: set of names (membership only, no rank) and is excluded on purpose.
_LADDER_RULE_KINDS = frozenset({"layer_direction", "same_tier_edge"})

#: A tier absent from the declared ladder sorts after every ranked one,
#: visibly (never tied with a genuinely rank-0 tier).
_UNRANKED = 999


class LadderError(Exception):
    """The target carries a `codemap.yaml` the descriptor door refuses."""


def tier_ranks(port_root: "str | Path") -> "dict[str, int]":
    """The target's own declared `{tier name: rank}` ladder: the first
    `rules[]` entry of its `codemap.yaml` (read through the descriptor door)
    whose `kind` carries that shape. `{}` when there is no `codemap.yaml` or
    no such rule -- a target with no ladder opinion is a normal state. A
    descriptor the door refuses raises `LadderError`: an unreadable file is
    not the same answer as "no ladder"."""
    root = Path(port_root)
    if not (root / "codemap.yaml").is_file():
        return {}
    try:
        rules = load(root / "codemap.yaml", root).descriptor.rules
    except DescriptorError as e:
        raise LadderError(str(e)) from e
    for rule in rules:
        tiers = rule.get("tiers") if rule.get("kind") in _LADDER_RULE_KINDS else None
        if isinstance(tiers, dict) and tiers:
            return {str(k): int(v) for k, v in tiers.items()}
    return {}


def snapshot_node_dists(idx: Mapping[str, Any], anchor: str) -> "dict[str, str | None]":
    """Every node key of one already-read `_index.json` -> its own module's
    `dist` — keyed exactly like a ledger node (`<anchor>::module::name`),
    same walk shape `codegraph_port_ledger_verbs._snapshot_node_keys`/
    `codegraph_port_judge_verbs._snapshot_node_shas` already use, trimmed to
    the ONE field this module's ladder reads (`dist`, never `sha`/`path`/
    `lines`) — each `port_*.py` caller keeps its own trim rather than
    importing another module's private helper."""
    out: "dict[str, str | None]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            module = m.get("module")
            for s in m.get("symbols") or ():
                for holder in [s] + list(s.get("methods") or ()):
                    key = "%s::%s::%s" % (anchor, module, holder.get("name"))
                    out[key] = holder.get("dist")
    return out


class Order:
    """Print order for one OLD-tree snapshot's own node set: LADDER first
    (when the target declares one), then dist, then the node key itself —
    ported (algorithm-faithful, generalized) from upstream's own `Order`.
    Not alphabetical: `port status`'s todo listing is an architecture
    statement, edges only look down the ladder, so a lower-tier dist should
    read before a higher one imports it. A node missing from the snapshot,
    without a `dist`, or whose `dist`'s tier the target never ranked, sorts
    LAST — an unanswered question, never interleaved with a ranked one."""

    def __init__(self, rank: "Mapping[str, int]", idx: Mapping[str, Any], anchor: str) -> None:
        self.rank: "dict[str, int]" = dict(rank)
        #: dist -> tier NAME, straight off the snapshot's own `tiers` table
        #: (`port_legacy_snapshot.py`'s own `index_json`) — never re-derived.
        self.unit_tier: "dict[str, str]" = dict(idx.get("tiers") or {})
        self.nodes: "dict[str, str | None]" = snapshot_node_dists(idx, anchor)

    def meta(self, key: str) -> "tuple[str | None, str | None]":
        dist = self.nodes.get(key)
        return dist, self.unit_tier.get(dist or "")

    def of(self, key: str) -> tuple:
        dist, tier = self.meta(key)
        r = self.rank.get(tier or "", _UNRANKED) if tier else _UNRANKED
        return (r, dist or "~", key)

    def sort(self, keys) -> "list[str]":
        return sorted(keys, key=self.of)
