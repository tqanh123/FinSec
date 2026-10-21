---
name: hs:diagram
injectable: true
description: Compile typed JSON IR (architecture, dataflow, lifecycle, sequence, workflow) into a validated interactive system map -- SVG, fragment, or self-contained HTML. Use for a topology or state-machine diagram whose data must be checked before it renders.
argument-hint: "<ir.json> [--format svg|fragment|html] [--preset classic|blueprint] [--theme light|dark] [--out <path>]"
allowed-tools: [Bash, Read, Write, Edit]
metadata:
  compliance-tier: workflow
---

# hs:diagram — typed IR to a validated system map

Author topology as typed JSON IR -- never coordinates. The compiler validates it, picks a
layout engine, ranks and orders the graph, routes orthogonal edges, and renders role-aware
nodes with a legend. This is the main door for a SYSTEM-MAP diagram (architecture, dataflow,
lifecycle, sequence, workflow); `references/routing-table.md` covers where a neighbouring
diagram skill still wins.

## The five archetypes

| Intent | `diagram_type` | Primary entities | Reference |
|---|---|---|---|
| Topology, microservices, boundaries | `architecture` | `components`, `boundaries`, `connections` | `references/ir-reference.md` |
| Multi-step execution, lanes, decisions | `workflow` | `lanes`, `steps`, `transitions` | `references/ir-reference.md` |
| Ordered calls, request/response timing | `sequence` | `participants`, `messages` | `references/ir-reference.md` |
| ETL pipelines, stream processing, lineage | `dataflow` | `stages`, `nodes`, `flows` | `references/ir-reference.md` |
| Finite state machines, status transitions | `lifecycle` | `states`, `transitions` | `references/ir-reference.md` |

Schema files for all five plus the shared envelope (`common`) live under `schemas/*.schema.json`
-- the machine-readable contract; `references/ir-reference.md` is the field/enum cheat sheet.

## Workflow

1. **Author the IR**: `{"schema_version": 1, "diagram_type": "<type>", "meta": {"title": "..."}}`
   plus the archetype's entities. IDs match `^[a-zA-Z0-9_-]{1,64}$`; placement is DERIVED from
   the IR (`layer` / `lane` / `stage.order` / graph structure), never authored as x/y.
2. **Validate**: `python3 scripts/diagram_ir.py <ir.json> --json` -- every error carries a
   JSON-pointer `path` and a fix `hint`; the compiler CLI (step 3) runs this automatically and
   refuses to render on any error (exit 2).
3. **Compile**:
   ```bash
   python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/diagram/scripts/diagram_compile.py \
     --input diagram.json --format html --preset classic --theme dark --out ./build/diagram.html
   ```
   `--format svg` for a clean vector file, `--format fragment` for a `<div>` scoped to `--id`
   (safe to paste into a page this skill has never seen), `--format html` for the self-contained
   interactive reader (search, click-to-isolate neighbours, theme toggle -- no network, no CDN).
4. **Engine**: architecture/dataflow prefer ELK, workflow/lifecycle use graphviz, sequence needs
   neither (`references/engine-selection.md` -- measured probe results, not an assumption).
   Every compiled diagram's stderr names the engine actually used and, on a fallback, why.
5. **Deliver**: read the CLI's stderr `engine:` line before treating an unusually plain diagram
   as a bug -- a `fallback-grid` line means neither engine was available on this machine.

## Presets and themes

Two presets (`classic`, `blueprint`) times light/dark = four renderable combinations; a preset
changes background/surface/font only -- the role-to-color mapping (`scripts/diagram_palette.py`)
never changes, so the legend is learned once. Both themes' CSS ship in every compiled output
(scoped by the diagram's element id), so the HTML reader's theme button needs no re-render.

## Boundaries

- Whiteboard/hand-drawn sketching, branded vendor stencils (AWS/Azure/GCP/Cisco/K8s/UML/ER),
  strict draw.io-native editing -> `hs:drawio`.
- Inline markdown diagrams that must render on GitHub/GitLab natively, no separate file ->
  `hs:mermaidjs`.
- Publication-grade static SVG/PNG with hand-picked styling (8 fixed visual styles) -> `hs:tech-graph`.
- A quick ASCII/Mermaid sketch inside a plan or PR report, not a durable artifact -> `hs:preview`.
- Full routing table with the "why": `references/routing-table.md`.
- Not an `hs-run` domain -- `references/hs-run-fit.md` records the measured reason.
- Does not draw an unread system: scout the real topology first (`hs:scout` / `hs:understand`)
  before authoring the IR.
