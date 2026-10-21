"""codegraph.change — the `hs-code change` domain's measurement kernel.

Sibling to `codegraph._core` (the snapshot/query/rules kernel every domain
shares): this package holds engines that answer a SECOND question over an
already-measured snapshot rather than measuring the tree itself: `impact`
(blast radius / fan-in), `api_diff` (public surface by name and signature),
`snapshot_diff` (two snapshots as one Markdown artifact), `hotspots` (history
churn × measured size) and `report` (one data dict, JSON + HTML). Each of the
last four also carries its own hs-code verb entry (`cmd_*`).
"""
from __future__ import annotations
