"""codegraph — the codebase-map measurement library `hs-code graph` drives.

Ported from an external reference implementation's snapshot pipeline (a
deterministic Python code-tree measurer): scan a tree into a symbol-level
graph, write it to disk. The measurement engine itself lives under `_core/`
(mirroring the source layout, kept as a private subpackage so a caller
outside this package always goes through `codegraph._core...`, never reaches
into it by a shorter path this module does not itself declare); the public
surface is the hs-code verb modules (`codegraph.graph.verbs` and its siblings
in `codegraph/graph/`) that wire it to real `<domain> <verb>` rows in
`harness/data/hs-code-registry.d/`.

Each module here arrives with the verb that calls it: `graph snapshot`
brought adapters, descriptors, presets and the snapshot pipeline; the query
verbs (`codegraph.graph.query`) brought the query engine, `units`,
`git` and `snapshot.freshness`. The graph/edge model lands with the rule
verbs that build a `Graph`.
"""
