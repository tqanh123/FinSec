# Diagram-skill routing table

`hs:diagram` is the main door for a validated, interactive SYSTEM MAP (architecture, dataflow,
lifecycle, sequence, workflow) delivered as SVG / an embeddable fragment / a self-contained
interactive HTML reader. Each neighbouring skill keeps a role this one does not cover; none of
them are subsumed by this one.

| Need | Route to | Why not hs:diagram |
|---|---|---|
| Inline markdown diagram that renders natively on GitHub/GitLab, no separate file | `hs:mermaidjs` | hs:diagram's output is a standalone SVG/HTML artifact, not a fenced code block a markdown viewer renders itself |
| Branded vendor stencils (AWS/Azure/GCP/Cisco/K8s/UML/ER), strict geometry, draw.io-native editing | `hs:drawio` | hs:diagram has no shape catalog and produces read-only artifacts, not an editable `.drawio` file |
| Publication-grade static chart with one of 8 fixed hand-picked visual styles | `hs:tech-graph` | hs:diagram's presets (`classic`, `blueprint`) are deliberately few and role-driven, not a style catalog to pick from |
| A quick ASCII/Mermaid sketch inside a plan or PR report, read once and diffed in review | `hs:preview` | hs:diagram's artifacts are durable, rendered files meant to be opened in a browser, not terminal/diff-friendly prose |
| A validated interactive system map with grounded search/click-to-isolate topology | **hs:diagram** | — |

## Composition, not competition

- A durable architecture doc under `docs/` that needs a diagram -> author the IR with
  hs:diagram, then let `hs:docs` place the compiled artifact.
- `hs:advise`'s NEEDS_PREVIEW step routes to hs:diagram when it is enabled, and to
  `hs:mermaidjs` otherwise — the preview render is not this skill's only caller.
- hs:diagram does not replace hs:tech-graph's SVG generator or hs:drawio's shape catalog; a
  system map that also needs a specific vendor icon set still goes to hs:drawio for that part.

## Not an hs-run domain

Measured reason: `references/hs-run-fit.md`.
