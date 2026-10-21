---
name: hs:docs-scaffold
injectable: false
description: Generate a doc skeleton from a template (frontmatter + headings + > TBD) for the capability-driven required set. No-clobber. Never invents content.
argument-hint: "[--module mod-07 --auto] | [--type <t> --out <path> --set k=v]"
allowed-tools: [Bash, Read, Glob]
metadata:
  compliance-tier: workflow
---

# hs:docs-scaffold — generate doc skeletons

Stub a missing doc from `templates/<type>.md` → each `{{token}}` becomes a value or `TBD`.
**No-clobber**: a file that already has content (`size > 0`) is never overwritten (unless `--force`).

**HARD boundary** (repeated in all three docs-* skills): **does not invent design content**, **does not debate quality**. It only generates the frame (valid frontmatter + headings + `> TBD`). The author fills the content; a content gap that needs a decision → `FIX.md`, never auto-filled.

## When to use

- A new module, or a module gaining a capability (`exposes_api`, `owns_agents`, …) → it needs new stub docs.
- Before `hs:docs-standardize`: clear `mandatory-doc-missing` / `required-doc-missing` with skeletons, then let the author write the content.
- NOT for rewriting a doc that already has content (no-clobber protects it; do not reach for `--force`).

## Required set (capability-driven, hybrid)

Source: the module `README.md` frontmatter `capabilities` (see `_docslib/docslib/capabilities.py`).

| Condition | Required doc (relative to the module dir) |
|---|---|
| ALWAYS | `README.md`, `design.md` |
| `exposes_api: true` | `api.md` |
| `has_workers: true` | `workers.md` |
| `tenant_config: true` | `config.md` |
| `has_features: [f, …]` | `features/<f>/spec.md` for each `f` |
| `owns_agents: [a, …]` | `agents/<a>/{agent,model-card,eval}.md` + `agents/<a>/prompt/SYSTEM.md` for each `a` |

Missing README/design = `mandatory-doc-missing` (error). Any other missing doc = `required-doc-missing` (warn).

## Step by step

1. **Find the gaps** — `capability_required.py` reads each README → lists the required docs still missing per module.
2. **Generate** — `scaffold_doc.py --module <id> --auto` stubs every missing required doc (no-clobber).
3. **Verify** — re-run `capability_required.py`; expect `✓ đủ required-set` (the script's own output string).
4. **Hand over** — tell the author: each `WROTE` file is a `> TBD` skeleton that needs content. Do not fill it yourself; move on to `hs:docs-standardize` to validate the structure once written.

## Scripts (real I/O)

**`scripts/capability_required.py`** — gap analyzer, exit 0. Reads `load_model(docs)` → for each module compares `required_docs(capabilities)` with the files on disk → prints/JSON `{dir, capabilities, required, missing}`. Generates NO files.
```
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/capability_required.py [--docs docs] [--module mod-07] [--json]
```

**`scripts/scaffold_doc.py`** — generates skeletons. Tokens `{{id}} {{type}} {{status}} {{owner}} {{version}} {{parent}} {{title}} {{date}}` → value or `TBD`. Defaults: `status=draft`, `version=0.1.0`, `owner=PTNT` (config → `PTSP`), `date=today`. Result per file: `WROTE` | `KEEP` (no-clobber) | `SKIP` (no template / type not mapped).

- Single file: `--type <t> --out <path> --set k=v [--set …] [--force]`
- Module: `--module mod-07 --auto` — generates EVERY missing required doc; derives `id` (dot-path under the module: README→`mid`, `features/<f>/…`→`mid.feature.<f>.<…>`, `agents/<a>/…`→`mid.agent.<a>.<…>`, `prompt/SYSTEM`→`…prompt`), `parent=mid`, and `owner` by type.

```
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/scaffold_doc.py --module mod-07 --auto
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/scaffold_doc.py --type adr --out docs/decisions/adr/adr-0007-x.md --set id=adr-0007 --set title="Choose X"
```

**`templates/<type>.md`** — 22 templates (one per type; the `index` type has none). Each template = tokenised frontmatter + headings + `> TBD`. Editing a template changes the generated frame (outside this skill's remit).

## Example commands

```
# 1) gaps across the repo
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/capability_required.py
# 2) one module only, JSON
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/capability_required.py --module mod-09 --json
# 3) stub every missing required doc of mod-09 (no-clobber)
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/scaffold_doc.py --module mod-09 --auto
# 4) force-overwrite one file (rarely needed — breaks no-clobber)
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-scaffold/scripts/scaffold_doc.py --type api --out docs/modules/.../mod-09/api.md --set id=mod-09.api --force
```

## Boundaries (does not do)

- Does NOT write design/API/prompt content — only `> TBD`.
- Does NOT overwrite a file that has content (no-clobber); `--force` only on the user's explicit request.
- Does NOT edit `capabilities` (the module owner's decision) — read only.
- Does NOT validate the graph (that is `hs:docs-standardize`'s job).
