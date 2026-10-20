# Update workflow — update docs after a change

Use when `docs/` exists but the codebase has changed significantly (new feature, refactor, architecture change).

## Phase 1 — Scout codebase

1. Use `hs:scout` to explore the codebase; compute LOC per directory.
2. Skip: `.git`, `__pycache__`, `harness/state/`, `node_modules`, `.cache/`.
3. Merge scout reports into a context summary.

## Phase 1.5 — Read existing docs

**Read before modifying** — required.

1. Count files: `ls docs/*.md 2>/dev/null | wc -l`
2. Check LOC: `wc -l docs/*.md 2>/dev/null | sort -rn`
3. Parallel read strategy (pin `model:"haiku"` on every `Explore` spawn — it inherits Opus otherwise, wasteful for reading files):
   - 1-3 files: docs-manager reads directly
   - 4-6 files: spawn 2-3 `Explore` agents with `model:"haiku"`
   - 7+ files: spawn 4-5 `Explore` agents with `model:"haiku"` (max 5)
4. Distribute files by LOC (larger files → dedicated agent).
5. Merge results into context for docs-manager.

## Phase 2 — Update docs (docs-manager agent)

Spawn `@docs-manager` agent (Task tool) with merged context + doc readings. Agent updates:

| File | Notes |
|---|---|
| `docs/codebase-summary.md` | always update |
| `docs/system-architecture.md` | when architecture/data flow changes |
| `docs/code-standards.md` | when patterns/conventions change |
| `docs/project-roadmap.md` | when roadmap changes |
| `docs/project-overview-pdr.md` | when scope/requirements change |

**Important for docs-manager:** every reference (function, path, env var, API endpoint) must exist in the current code — remove stale sections instead of leaving "TODO: update".
File and directory paths are checked by the reference gate, not by reading around: `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/docs_refs.py --check --root .` (exit 0 clean · 1 one `doc:line: ...` per finding · 2 not a git work tree). Functions, env vars and endpoints: confirm with grep.

**Standards docs follow a template.** When updating `docs/system-architecture.md` or `docs/code-standards.md`, keep them conformant to the template SSOT: after editing, run `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/scaffold_standards.py --type <t> --check docs/<t>.md` (exit 0 conform / 1 drift).
Drift (exit 1) lists missing/extra `## ` sections — add the missing ones (with `> TBD` if undecided) or fold the
extra back in. Compare against the canonical headers with `--type <t> --print`. Prose standards pair only — do NOT route through hs:docs-scaffold / hs:docs-standardize (the module-docs template system).

## Phase 3 — Check size

After docs-manager finishes:
```bash
wc -l docs/*.md 2>/dev/null | sort -rn
```
File exceeds 800 LOC → report + ask user: split now or keep as-is?

## Phase 4 — Verify after update

Run the reference gate: `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/docs_refs.py --check --root .`. Exit 1 prints one line per finding.
Fix each: point the doc at the file that exists now, or — when the doc records that the file was removed — say so on the same line as `đã gỡ ở commit <sha>` or `removed in commit <sha>` (the gate asks git that the commit really removed it). `docs/decisions.md` and `CHANGELOG.md` are history and exempt;
`--exempt GLOB` replaces that list. Then check that dates and claims match the change, and that no deleted function is still named.

What the gate answers, and what it does not.
It reads only `docs/**/*.md` and resolves four things there: every path (a token with `/`, and a bare file name that git shows was deleted), every row of a declared source ledger, every `module.name` against a HEAD snapshot when one exists, and every cited decision id against `docs/decisions.yaml` when the project keeps one.
It does not read root `README.md`/`CLAUDE.md`, skill files, or built pages, so check a path or decision id named there by hand.
It does not check counts, flags, or whether a sentence still describes the behaviour: a decision id that exists but is the wrong decision passes.
Exit 0 means every name resolves.
It does not mean the prose is true.

## Constraints

- Only modify/create files in `docs/` — do not create markdown elsewhere.
- Do not start implementing code.
- Additional arguments are passed to docs-manager via `$ARGUMENTS`.
