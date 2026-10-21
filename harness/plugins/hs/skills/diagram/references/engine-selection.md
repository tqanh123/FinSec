# Layout engine selection — measured, not assumed

`scripts/diagram_engine.py` probes before anything downstream builds on the result
(`agent-operational-discipline` rule: probe before you build on a guess). Every compiled
diagram's stderr prints the `tried` audit trail, so "why did I get the plain fallback" is
always answerable from the run itself.

## The routing table

| `diagram_type` | Preference chain | Why |
|---|---|---|
| `architecture` | ELK, then graphviz | nested boundaries + ports read better under ELK's container-aware routing |
| `dataflow` | ELK, then graphviz | nested stages, same reasoning as architecture |
| `workflow` | graphviz only | a single flow axis, no nesting ELK earns its cost on |
| `lifecycle` | graphviz only | same — a single state-transition axis |
| `sequence` | none | fixed linear lifeline layout; no ranking problem to solve |

## What is actually installed today (measured)

- `dot` (graphviz): present on the dev machine (2.43.0) and on the homelab test host
  (14.1.2) — `diagram_engine.probe_graphviz()` finds it via `shutil.which` + `dot -V`, and the
  compiler drives it through the OPTIONAL `graphviz` Python package when that is installed
  (`harness/requirements.txt`), falling back to a direct subprocess call otherwise.
- ELK (Eclipse Layout Kernel): NOT vendored. It needs a JVM or the `elkjs` Node bundle; this
  install ships neither. `diagram_engine.probe_elk()` checks for a real `node` binary with
  `elkjs` resolvable and answers `unavailable` honestly rather than assuming a JVM exists.
  `diagram_layout.compile_layout` therefore never actually reaches the `elk` branch on a stock
  install today — `architecture`/`dataflow` fall through to `graphviz`, which is still a real
  layered layout with orthogonal routing, just without ELK's container-aware port routing.
- A future install that vendors `elkjs` (after the EPL-2.0 licence check that vendoring step
  still needs) makes `probe_elk()` answer `available` with no code change here — the probe and
  the routing table are already real, only the vendored bundle is missing.

## Degrade order, every diagram type

1. Try the preference chain in order; the first `available` probe wins.
2. If nothing in the chain answers `available`, or the winning engine's actual invocation fails
   at runtime (e.g. `dot` crashes on unusual input), fall back to a pure-Python longest-path
   grid layout (`diagram_layout.layout_fallback_grid`) — plainer, but every node still has a
   position, no two nodes overlap, and every edge still connects.
3. `sequence` never enters this chain at all; its layout is always the same deterministic
   linear placement.

Nothing here silently draws a blank diagram or crashes the whole compile on a missing engine.
