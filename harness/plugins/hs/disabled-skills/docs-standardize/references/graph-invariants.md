# Graph invariants

Every invariant `_docslib/docslib/graph.py :: validate(model, findings)` checks. All are
**objective** (counting / graph-walking, no content judgment). `error` blocks the gate
(exit 2); `warn` = a gap. How to fix: adjust the **structure** (frontmatter /
`_index/*.yaml` / a missing file). Do NOT fix content on the reader's behalf; what needs a
human decision → `FIX.md`.

## 1) Frontmatter + id + parent/provenance

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `missing-frontmatter` | error | no `---` block | add frontmatter (see frontmatter-spec.md) |
| `bad-frontmatter` | error | broken YAML / not a mapping | fix the YAML |
| `missing-frontmatter-field` | error | a required field missing | add the field |
| `bad-id-grammar` / `bad-type` / `bad-status` / `bad-version` / `bad-tier` / `bad-owner` | error | grammar/enum violation | fix the value to match the contract |
| `duplicate-id` | error | `id` used by ≥2 files | make one id unique |
| `dangling-parent` | error | `parent` points at a nonexistent id | fix parent or create the parent doc |
| `dangling-provenance` | warn | a path in `provenance` does not exist | fix the path or drop the entry |

## 2) Parts (`_index/modules.yaml :: parts`)

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `part-bad-home` | error | `home` is not a real module id | point `home` at a valid module |
| `part-missing-file` | error | `at` does not point at a real file (relative to docs_root) | fix `at` or create the file |
| `part-bad-layer` | error | `layer` ∉ `{L2,L3,L4}` (when present) | fix `layer` |

## 3) Links (`_index/modules.yaml :: links`)

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `link-bad-from` | error | `from` is not a real module id | fix `from` |
| `link-dangling` | error | `uses` points at a nonexistent part | point `uses` at a real part |

## 4) Module: axis (intrinsic) + band (display)

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `module-bad-axis` | error | `axis` ∉ `{ingestion,extraction,decision,orchestration,posting}` | fix the axis (README frontmatter) |
| `module-missing-band` | error | `band` missing (taxonomy in `_index/bands.yaml :: modules[]`; legacy fallback `showcase.yaml`) | add the module's `band` in `_index/bands.yaml` |
| `module-bad-band` | error | `band` ∉ the band set of `_index/bands.yaml :: bands` (legacy: `showcase.yaml :: bands`) | fix the band |

## 5) Config parts (PTSP — `_index/modules.yaml :: config_parts`)

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `configpart-bad-home` | error | `home` is not a real module | fix home |
| `configpart-bad-owner` | error | `owner` ≠ `PTSP` | set `owner: PTSP` |
| `configpart-missing-bilingual` | error | `vi` or `en` missing | fill in both vi+en |

## 6) Safety (`_index/safety.yaml`)

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `safety-missing-id` | error | a safety entry lacks `id` | add the id |
| `safety-bad-anchor` | error | `anchors` contains a token that is not a real part | fix the anchor to a real part |

## 7) Foundations (`_index/foundations.yaml`)

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `foundation-missing-id` | error | a foundation lacks `id` | add the id |
| `foundation-bad-anchor` | error | `anchor` is neither a real part nor `infra` | fix the anchor (real part or `infra`) |

## 8) The capability-driven required-set (`capabilities.py :: check_module`)

Hybrid mandate: `README.md`+`design.md` ALWAYS; the rest follows the `capabilities` flags
(see the docs-scaffold required-set).

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `mandatory-doc-missing` | error | `README.md` or `design.md` missing | `hs:docs-scaffold` stub |
| `required-doc-missing` | warn | a flag-declared doc missing (api/workers/config/feature/agent) | scaffold the stub, then write the content |
| `undeclared-doc` | warn | `api.md`/`workers.md`/`config.md` exists but the matching flag is undeclared | declare the flag in the README, or remove the file |

## 9) README guard — DRY vs boundary (`_readme_guard`)

Canonical: `docs/architecture/responsibility-boundary.md`. Threshold:
`GUARD_BOUNDARY_MAX_LINES = 15`; a "substantive" line = `len > 20` and not starting `#`.

| Code | Sev | Meaning | Fix |
|---|---|---|---|
| `mandatory-doc-missing` | error | module lacks `README.md` | scaffold the README |
| `readme-copies-boundary` | error | README duplicates > 15 lines verbatim from the boundary doc | keep a summary + link, do not copy |
| `readme-missing-canonical-link` | warn | README never mentions `responsibility-boundary.md` | add the canonical link |

## Notes

- `error` → the gate exits 2. Fix structure until 0 errors.
- `warn` = a gap, not blocking. A missing-doc gap → scaffold. A needs-a-human gap → `FIX.md`.
- All invariants are **graph-walk/count** invariants; none judges "bad content".
