"""Graph model over snapshots — mixed-file / edge-kind / bridge resolution.

Upstream's `deps.py` (measured third-party/workspace imports vs a
`pyproject.toml` declaration) is not ported: that question is answered by
`hs-code check pkgsync`, and nothing in the query engine calls it.
"""
from .model import Edge, Graph, HasLadder, Judge

__all__ = ["Edge", "Graph", "HasLadder", "Judge"]
