# The frontmatter contract

The source of truth (SSOT) = the YAML frontmatter of each `.md` + the thin `_index/*.yaml`.
Validated by `_docslib/docslib/frontmatter.py` (structural shape only, it does NOT judge
content). The SSOT of the schema itself: `_docslib/docslib/frontmatter.py`
(DOC_TYPES/ID_RE/STATUSES/TIERS).

## Fields

| Field | Required | Type / Grammar | Notes |
|---|---|---|---|
| `id` | ✓ | dot-path, each segment kebab `[a-z0-9]` joined by `-`/`.`; globally-unique | see grammar below |
| `type` | ✓ | ∈ `DOC_TYPES` (23) | decides the template frame |
| `status` | ✓ | `draft \| review \| stable \| superseded` | |
| `owner` | ✓ | string (`PTNT \| PTSP \| <team>`) | a config_part owner must be `PTSP` |
| `version` | ✓ | semver `x.y.z[-pre][+build]` | |
| `tier` | optional | ∈ `TIERS` when present | enum-checked when present |
| `parent` | optional | the parent doc's `id` | resolved at graph level (dangling-parent = error) |
| `provenance` | optional | `[path, …]` raw sources | resolved at graph level (dangling = warn) |
| `capabilities` | optional | mapping; `module-readme` ONLY | drives the required-set (see capabilities.py) |

## The `id` grammar

```
ID_RE = ^[a-z0-9]+(?:[-.][a-z0-9]+)*$
```

- Segments use `a-z0-9` only; joined by `-` (within a segment) or `.` (dot-path hierarchy).
- No uppercase, no `_`, no spaces, must not start/end with `-`/`.`.
- Globally unique across `docs/` (a duplicate → `duplicate-id`, error).
- Dot-path convention (the scaffold derives it): module README = `mod-07`; children =
  `mod-07.design`, `mod-07.api`, `mod-07.feature.<f>.spec`, `mod-07.agent.<a>.model-card`,
  `mod-07.agent.<a>.prompt`.

## Enums

```
DOC_TYPES = sad, overview, quality, governance, module-readme, module-design,
            part, api, worker, config, techstack, operations, guide, feature-spec,
            agent-spec, model-card, eval, prompt, mcp-tool, adr, glossary,
            changelog, index            (23)
STATUSES  = draft, review, stable, superseded
TIERS     = L0, L1, L1x, L2, L3, platform, module      (optional)
SEMVER    = ^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$
```

## Example blocks

Module README (with capabilities):
```yaml
---
id: mod-07
type: module-readme
status: draft
owner: PTNT
version: 0.1.0
tier: L2
capabilities:
  exposes_api: true
  has_workers: true
  tenant_config: false
  has_features: [reconcile, export]
  owns_agents: [classifier]
---
```

Child doc (design), with parent + provenance:
```yaml
---
id: mod-07.design
type: module-design
status: draft
owner: PTNT
version: 0.1.0
parent: mod-07
provenance: [_migration/module-attrs.json]
---
```

## Related findings (frontmatter.validate)

| Code | Severity | Cause |
|---|---|---|
| `missing-frontmatter` | error | no `---` block |
| `bad-frontmatter` | error | broken YAML / not a mapping |
| `missing-frontmatter-field` | error | one of `id,type,status,owner,version` missing |
| `bad-id-grammar` | error | `id` violates `ID_RE` |
| `bad-type` | error | `type` ∉ `DOC_TYPES` |
| `bad-status` | error | `status` ∉ `STATUSES` |
| `bad-version` | error | `version` not semver |
| `bad-tier` | error | `tier` present but ∉ `TIERS` |
| `bad-owner` | error | `owner` not a string |

How to fix: correct the frontmatter to match the contract. Do NOT touch the prose. A field
that needs a human decision (e.g. who the real `owner` is) → `FIX.md`.
