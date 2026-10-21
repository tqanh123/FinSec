---
name: hs:docs-build
injectable: false
description: Render docs (md + frontmatter) + _index + _present into public/ (showcase HTML, theme and effects kept) and dist/ (flat-md, pdf, excel). Pure rendering — content lives in the md files; a legacy merged showcase.yaml still works.
argument-hint: "[--docs docs] [--out public] [--dist dist]"
allowed-tools: [Bash, Read, Glob]
metadata:
  compliance-tier: workflow
---

# hs:docs-build — render docs → public/ + dist/

Renders `docs/**/*.md` (frontmatter + body) + `_index/` (graph + `bands.yaml`) + `_present/` into:
- **`public/`** — the showcase HTML (theme + interactive dim/highlight/dialog effects kept).
- **`dist/`** — the release set: flat-md (merged), pdf, excel.

**Content principle**: content lives in the **physical md files**. After the split, the design
taxonomy (`band`/`cluster` + `bands`) sits in `_index/bands.yaml`; presentation
(`pages`/`categories`/`asset_slots`/`footer_pages`/`theme`/`order`/`sections`/`text_fix`) sits
in `_present/*`. A legacy merged `showcase.yaml` is back-compat (read through a split shim).
Build NEVER generates content — it renders what exists.

**HARD boundary** (repeated in all three docs-* skills): the skill **does not invent design
content** and **does not debate quality**. Build is pure rendering; the structure must already
PASS `hs:docs-standardize`.

> The build scripts live in `skills/docs-build/scripts/` (12 files: `build_showcase.py`,
> `build_flat_md.py`, `build_pdf.py`, `build_excel.py`, `build_all.py`, `ssg_engine.py`,
> `e2e_static.py`, `e2e_browser.mjs`, `render_check.py`, `render_check.mjs`, `render_md_page.py`,
> `_selfcheck.py`). This document describes the **input/output/behaviour contract**.

## Preconditions

- `hs:docs-standardize` PASS (0 errors) — build runs on docs that already meet the structure contract.
- `docs/_index/` must exist — `build_showcase.py`/`build_all.py` refuse (exit 2, reason + fix printed, nothing written) when it is missing, instead of writing near-empty data-JS. `docs/_present/` alone missing is fine (legacy `_index/showcase.yaml` shims it).
- A repo running its own hand-written showcase pipeline (no `docs/_index/`) skips this skill's showcase step and runs its own build script instead.
- `docs/_present/*` (presentation: pages/categories/asset_slots/footer_pages/theme) + `_index/bands.yaml` (taxonomy) exist; a legacy merged `showcase.yaml` still runs through the shim. The showcase data JS comes from `_docslib/docslib/graph.py :: generate_showcase_data(model, out_dir)`
  → `module-m4-data.js`, `ptnt-layers-data.js`, `ptnt-clusters-data.js`, `part-modref-data.js`.

## Contract (input → output)

| Script | Input | Output |
|---|---|---|
| `build_showcase.py` | `docs/`, `_present/*` + `_index/bands.yaml` (legacy merged `showcase.yaml` via the shim), `assets/` | `public/` (HTML + theme + JS data) |
| `build_flat_md.py` | `docs/`, `_present/*` (section order; legacy `showcase.yaml` via the shim) | `dist/<name>.md` (merged in section order) |
| `build_pdf.py` | flat-md / HTML | `dist/<name>.pdf` |
| `build_excel.py` | `_index/*.yaml` + frontmatter | `dist/<name>.xlsx` (module/part/config matrix) |
| `render_check.py` | `public/` (already built) | layout-defect text report (SKIPPED, not a fail, on a host with no headless browser) |

Pipeline detail (md→html, section order, dependencies, CI points): `references/render-pipeline.md`. E2E test spec: `references/e2e.md`. Default shell bundle: `default-theme/`.

The real render flow (`build_showcase.py` generates the data-JS → `render_md_page.py` turns md into an HTML partial → the assembler packs `public/`; the assembler is `docs/showcase/build.py`, a **consumer-repo-owned adapter called through a subprocess, not one of the 12 scripts shipped here**): `references/render-pipeline.md`.

## Step by step

1. **Gate first** — run `hs:docs-standardize` (`docs_gate.py --fresh`); build only on 0 errors. If a glossary exists (`docs/glossary.yaml`), seed the term table into the generated docs through `glossary_register.py --root . --list` (shared term/definition/forbidden/backing schema); no file → skip quietly.
2. **Generate the showcase data** — `generate_showcase_data(model, docs/showcase/assets/js)` → 4 `*.js` files.
3. **Render the showcase** — `build_showcase.py` renders md→html + theme/assets → `public/`.
4. **Render dist** — `build_flat_md.py` → `build_pdf.py` → `build_excel.py` → `dist/`.
5. **Render-check** — `render_check.py --public public` (multi-viewport layout, real headless Chrome). Its own preflight SKIPS loudly (exit 3, never a silent pass) on a host with no headless browser — `build_all.py` wires this in as a step that never counts a skip as a build failure. `references/e2e.md` covers where this sits next to the two other e2e tiers.
6. **Verify** — open `public/index.html`; check the interactive effects and that the section order matches the presentation config.

## What presentation and taxonomy own (after the split)

Presentation → `_present/*` (legacy merged: `showcase.yaml`):
- `pages[]` — `{key, en, vi, partial, title, accent, empty}` (page name + html partial + accent colour).
- `categories[]` — `{key, en, vi, pages[]}` (bilingual sidebar groups).
- `asset_slots` — `{js[], css[], vendor[]}` (part-file lists; `@generated` expands to the 4 data-JS files).
- `footer_pages[]` — `{key, vi, en}` (footer-bar links, custom order).
- `theme` — theme name (default `default-theme`).
- `text_fix[]` — small display-string fixes (the md source is not changed).

Design taxonomy → `_index/bands.yaml` (NOT in presentation):
- `bands[]` — `{id, cluster_vi, cluster_en}` (bilingual layer-view legend) + a per-module `band`.

Neither owns content. Changing `detail`/`order` changes how things render, never the md source.

## Boundaries (does not do)

- Does NOT generate or edit md content — render only.
- Does NOT build while `hs:docs-standardize` still has errors.
- Does NOT change `_index/*.yaml` (that is standardize's / migrate's job).

## Related skills

- `hs:deploy`: after a PASSing build, publish `public/` to GitHub/GitLab Pages (it detects "Static site (HTML/CSS/JS)" → Pages). docs-build only renders; it does not deploy.
