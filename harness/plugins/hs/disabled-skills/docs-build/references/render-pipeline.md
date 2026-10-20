# Render pipeline

This describes the **render contract**. The build scripts + `assets/` are the
implementation; this document describes the actual input/output contract.

## Flow overview

```
docs/**/*.md (frontmatter+body)          ┐
docs/_index/bands.yaml (taxonomy band)   ├─► load_model ─► generate_showcase_data ─► <out-dir>/assets/js/*.js
docs/_present/* (order/sections/detail)  │                                          │
docs/_index/{modules,foundations,        │                                          ▼
            safety}.yaml                 ┘   build_showcase.py + assets/  ─► public/ (HTML+theme+JS)
                                              build_flat_md.py             ─► dist/<name>.md
                                              build_pdf.py                 ─► dist/<name>.pdf
                                              build_excel.py               ─► dist/<name>.xlsx
```

Content lives in the **physical md files**. Design taxonomy
(`band`/`cluster`) lives in `_index/bands.yaml`; presentation
(`order`/`sections`/`detail`/`text_fix`) lives in `_present/*`. The legacy combined
`showcase.yaml` is back-compat (a split-read shim until migration).

## md → html

1. Split frontmatter + body per `.md` (via `_docslib/docslib/frontmatter.py :: parse`).
2. Markdown body → HTML through the `markdown` library (recommended extensions:
   `tables`, `fenced_code`, `toc`, `attr_list`).
3. Attach theme/CSS/JS from `assets/`; keep the baseline top-down 3-layer showcase
   interactions (dim/highlight/dialog).
4. Showcase data (the graph) is NOT rendered from prose — it is generated from `_index/`
   via `graph.generate_showcase_data(model, docs/showcase/assets/js)` (the assembler
   bundles these 4 files together with JS_PARTS → `public/assets/showcase.js`):
   - `module-m4-data.js` → `MOD_M4` (per-module band/parts/configParts/consumes/feeds).
   - `ptnt-layers-data.js` → `PTNT_LAYERS` (bands/l3/l4/foundations/safety).
   - `ptnt-clusters-data.js` → `PTNT_CLUSTERS` (MOD-free clusters + links + safety).
   - `part-modref-data.js` → `PART_MODREF` (owner/consumers/universal per part).
   The `*.js` files carry a GENERATED banner — do not hand-edit them.

## Internal assembler (`docs/showcase/build.py`, consumer repo, invoked via subprocess)

`build_showcase.py` (the script shipped here) performs two steps, then shells out to the
real assembler:

1. Load the model (`_present/*` + `_index/bands.yaml`, legacy `showcase.yaml` through the
   shim) → generate the 4 data-JS files (`module-m4-data.js`, `ptnt-layers-data.js`,
   `ptnt-clusters-data.js`, `part-modref-data.js`).
2. Every page with a `source:` `.md` → `render_md_page.py` converts md → an HTML partial.
3. Invoke `docs/showcase/build.py` (the adapter, NOT one of the 12 docs-build scripts) —
   it calls `_assemble` to concatenate CSS/JS parts → builds `Ctx` → `ssg_engine.build(ctx, out)`.
4. `ssg_engine` receives `Ctx.css`, `Ctx.js` (the assembled strings) + `Ctx.assets_dir`
   → generates the whole `public/`.

## Section order (from `_present/*`)

`sections[]` decides render order + depth (SSOT `docs/_present/*`; legacy: `showcase.yaml`):
```yaml
sections:
- {id: overview,      order: 1}
- {id: architecture,  order: 2}
- {id: quality,       order: 3}
- {id: governance,    order: 4}
- {id: techstack,     order: 5}
- {id: modules,       order: 6}
- {id: operations,    order: 7}
- {id: guides,        order: 8}
- {id: glossary,      order: 9}
- {id: decisions,     order: 99}
```
- `order` → the section's position in the showcase + flat-md (`_present/*`).
- `detail: full` renders fully; `summary` renders condensed (summary/heading-level)
  (`_present/*`).
- `_present :: modules[].order` → render order; `_index/bands.yaml :: modules[].band` →
  the band when rendering layer/cluster views.
- `_index/bands.yaml :: bands[]` (vi/en) → the bilingual legend.

## Output

- **public/** — showcase HTML (index + section pages) + `assets/` (theme:
  `showcase.css`, `showcase.js` with the data-JS bundled in, `lib/`). Baseline
  interactions preserved.
- **dist/** — static releases:
  - flat-md: md merged in section order (one linearly-readable file).
  - pdf: from flat-md or HTML (xhtml2pdf).
  - excel: the module/part/config matrix + reuse, from `_index/*.yaml` + frontmatter
    (openpyxl).

## Dependencies

| Library | Used for |
|---|---|
| `pyyaml` | reading `_index/*.yaml` + frontmatter (already used in `_docslib`) |
| `markdown` | md body → HTML |
| `xhtml2pdf` | HTML/flat-md → PDF |
| `openpyxl` | generating `.xlsx` |

## CI points

1. **Gate before build** — `hs:docs-standardize` (`docs_gate.py --fresh`) must PASS
   (0 errors). The build does NOT run while structural errors remain.
2. **Build** — run the 4 build scripts; a non-zero exit fails CI.
3. **Smoke** — check `public/index.html` + `public/assets/showcase.js` contain
   `var MOD_M4/PTNT_LAYERS/PTNT_CLUSTERS/PART_MODREF`; `dist/` has md/pdf/xlsx.
4. Build artifacts (if any) are kept apart from validation artifacts
   (`harness/state/docs-check.json`).

## Boundaries

- The build only renders — it does NOT generate/edit content, does NOT modify
  `_index/*.yaml`.
- Showcase-data `*.js` is generated by `graph.generate_showcase_data`; do not hand-edit
  (GENERATED banner).
- Presentation-Needs-A-Human decisions (theme/order/detail) live in `_present/*`
  (separate from the design taxonomy `bands.yaml`).

## Playbook orchestration (`docs/playbook.yaml`)

One orchestrating config — content ⟂ ui ⟂ output (in the spirit of an Antora playbook).
Changing the theme or adding a content source is one config line, not a code change.
Loader: `_docslib/docslib/playbook.py :: load_playbook`.
```yaml
content:                 # content sources (dir/glob rel docs-root)
- modules/
- architecture/
- guides/
ui: {bundle: _present, theme: showcase}   # presentation bundle + theme
output: build/           # build out-dir (defaults to build/ when absent)
```
Fail-closed: a missing file / missing `content` / `content` not a list → an actionable
`PlaybookError`.
- **The build reads the playbook** for its out-dir (replacing the hardcoded
  `docs/showcase/assets/js`) — repointing `build_showcase.py` (Step A out-dir =
  `playbook.output`, Step B the assembler reading the same out-dir) happens in the
  showcase-repo migration, because it touches the `docs/showcase/build.py` assembler
  living in the showcase repo (a half-done change detached from the assembler = breakage).
  The harness side ships the loader + convention; no playbook yet → keep legacy.

## Derived output — commit sources, gitignore output

The source tree carries NO build output. The SSOT of "what counts as derived output" =
`_docslib/docslib/derived.py` (`DERIVED_OUTPUT_GLOBS` + `is_derived_output`). The build
writes to its out-dir (CI `public/`), CI `pages:` is the only builder — do NOT commit it
back into `docs/`.

Safe derived-set `.gitignore` convention (consumer repo):
```gitignore
# Docs build output — generated in CI (GitLab pages: → root public/), do not commit
docs/public/
docs/showcase/assets/js/*-data.js
docs/_diagram/png/
```
- `*-data.js` = the 4 files generated by `generate_showcase_data` (do NOT touch
  hand-authored JS such as `09-search.js`).
- `_diagram/png/` is derived from `_diagram/puml/` (keep `puml/` as the source).
- `showcase/partials/*.html` is DELIBERATELY not in this set (mixed hand-authored prose +
  md-sourced render) — a user decision, see BACKLOG.

⚠️ The `git rm --cached docs/public/**` step (removing already-committed output) is
**irreversible** + happens in the external showcase repo → run it only AFTER the GitLab
`pages:` pipeline is green, proving regeneration works (a committed `docs/public/` is
STALE, not a valid fallback). Do NOT run it yourself — ask the operator.
