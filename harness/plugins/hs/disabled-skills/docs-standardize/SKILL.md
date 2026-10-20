---
name: hs:docs-standardize
injectable: false
description: Validate structural docs (frontmatter + graph invariants) → artifact JSON → gate. Finds gaps against the templates. Does NOT edit content and does NOT judge quality.
argument-hint: "[--fresh] | [--docs docs] [--artifact <path>] [--quiet]"
allowed-tools: [Bash, Read, Glob]
metadata:
  compliance-tier: gate
---

# hs:docs-standardize — validate structure + find gaps

Checks the **structure** of the docs (not their content): valid frontmatter, unique ids,
parent/provenance resolve, graph invariants (parts/links/config/safety/foundation), and the
capability-driven required set. **Edits no file.**

**HARD boundary** (repeated in all three docs-* skills): the skill **does not invent design
content** and **does not debate quality or content**; it only normalises structure and finds
gaps against the templates. Anything that needs a person to decide → write it to `FIX.md`,
**never fix it yourself**.

## Three layers (the same shape as the harness's own analyzer → artifact → gate)

```
analyzer (check_docs.py, exit 0)  →  artifact JSON (source of truth)  →  gate (docs_gate.py, exit 2 on error)
```

- The **analyzer** only counts and walks the graph → `Findings` (error|warn|info) → writes
  `docs-check.json` into the project's state home (`harness_paths.state_dir_for(<repo>)`:
  `HARNESS_STATE_DIR` > `HARNESS_DATA_ROOT/state` > `<repo>/.harness/state`).
  It ALWAYS exits 0 (even with errors) — the analyzer does not decide.
- The **artifact** (JSON, produced by `_docslib/docslib/findings.py :: artifact()`) is the **only
  source of truth**. Every judgment (gate, report, LLM advisory) must anchor to a number or
  finding in the artifact.
- The **gate** reads the artifact and exits 2 on any `severity=error`; warn/info do not block.

## When to use

- After writing or editing frontmatter, or adding a doc → check the structure.
- After `hs:docs-scaffold` → confirm the skeletons meet the contract.
- Before `hs:docs-build` / ship → the CI gate (`--fresh` to be sure the artifact is new).

## Step by step

1. **Analyze** — `check_docs.py` → prints a summary + writes the artifact. Read the `error` findings first.
2. **Triage**:
   - `error` (structural, objective) → the STRUCTURE can be fixed (bad id grammar, unknown type,
     dangling parent…). Fix frontmatter/_index, NOT the prose.
   - `warn` (gap) → required-doc-missing / undeclared-doc / dangling-provenance / missing-canonical-link.
     A missing doc → `hs:docs-scaffold`. A gap that needs a person to decide → `FIX.md`.
3. **Gate** — `docs_gate.py --fresh` → PASS on 0 errors.
4. **Advisory (optional)** — an LLM check of prose only on request; by DEFAULT it flags nothing,
   always anchors to the artifact, and NEVER calls a design "bad". See `references/validation-split.md`.

## Scripts (real I/O)

**`scripts/check_docs.py`** — analyzer, exit 0. `load_model(docs)` → `graph.validate(model, f)`
→ `f.write_artifact(...)`. Flags: `--docs docs --artifact <path> --quiet` (default artifact: the
state home above). The artifact adds `extra`: `{modules, docs, parts, links}` (counts).

**`scripts/docs_gate.py`** — gate, exit 2 on any error. Reads the artifact; does NOT re-run the
validation logic. Flags: `--artifact <path> --docs docs --fresh`. `--fresh` runs `check_docs.py`
first (a new artifact). No artifact and no `--fresh` → error exit (reminds you to run the analyzer).

**`scripts/migrate_module_map.py`** — ONE-SHOT: `docs/modules/module-map.yaml` →
`docs/_index/{modules,foundations,safety,showcase}.yaml` + a sidecar `_migration/module-attrs.json`
(intrinsic per-module data used to fill frontmatter). Idempotent (overwrites `_index/*`); never
touches the original map. Not a routine step — run once, when the map is split.

**`scripts/migrate_facts_to_frontmatter.py`** — ONE-SHOT, legacy: moves graph facts (layer/note/
universal_spine, reuse edges) from `_index/modules.yaml` into frontmatter (part docs / module
READMEs) under the frontmatter-as-SSOT model. Idempotent (`--check` verifies parity before the
hand facts are deleted). Not a routine step.

**`scripts/retrofit_frontmatter.py`** — ONE-SHOT, legacy: injects missing frontmatter into old docs
(top-docs hand map + module README `_migration/module-attrs.json` + parts from `_index/modules.yaml`).
Idempotent (a doc with frontmatter is skipped); creates NO content. Not a routine step.

## Example commands

```
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-standardize/scripts/check_docs.py                    # analyze + summary + artifact
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-standardize/scripts/check_docs.py --quiet            # write the artifact only (CI)
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-standardize/scripts/docs_gate.py --fresh             # re-analyze, then gate (exit 2 on error)
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-standardize/scripts/docs_gate.py                     # gate on the existing artifact
python3 "${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/docs-standardize/scripts/migrate_module_map.py            # one-shot map split
```

## References

- `references/frontmatter-spec.md` — the frontmatter contract: required/optional fields, id
  grammar, enums (type/status/tier/version), example blocks.
- `references/graph-invariants.md` — every invariant `graph.validate` checks + severity + how to fix.
- `references/validation-split.md` — the line between the script check (exit 0/2, objective) and
  the LLM check (advisory prose, flags nothing by default).

## Boundaries (does not do)

- Does NOT edit prose; only fixes structure (frontmatter/_index) when an error must be cleared.
- Does NOT judge design quality (not even as an LLM advisory) — structural/objective only.
- Does NOT generate docs (`hs:docs-scaffold`'s job); does NOT build (`hs:docs-build`'s job).
- A content decision that needs a person → `FIX.md`, never fixed here.

## Exclusions: `.docsignore`

When the docs tree holds files owned by another project (e.g. `docs/product/` from
`product-spec`), create `docs/.docsignore` — one pattern per line, **gitignore syntax**
(`_docslib/docslib/discover.py` compiles it with `pathspec` "gitignore"):

- `some-dir/` → excludes the whole tree (a trailing `/` anchors a directory)
- `file.md` → excludes that file name (`*` stops at `/`, `**` crosses it)
- `!keep.md` → re-includes a path an earlier pattern excluded
- `# comment` and blank lines → ignored

A malformed pattern fails the run with the offending line named (`DocsIgnoreError`).
`check_docs.py` and `docs_gate.py` read and apply `.docsignore` automatically; no flag needed.
