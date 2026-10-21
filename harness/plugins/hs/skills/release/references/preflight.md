# Preflight gate (before the cut)

`release_orchestrator.preflight()` is a hard gate — any failure aborts before step 1, so a broken tree never gets a tag. Checks, in order:

1. **decisions reconciled** — refuse the cut while the Decision Register has drifted (new DECs / flips) since the last reconcile marker. Cheapest, first.
2. **docs structure** — `docs-standardize/scripts/docs_gate.py --fresh` must pass (exit 2 on a `severity=error`: missing frontmatter / broken graph invariant). Release VERIFIES doc structure — it never regenerates content. Stale/broken docs → abort; refresh + commit them first, then cut.
3. **clean tree** — `git status --porcelain` must be empty, checked BEFORE the CI: the CI must run on the commit the cut hashes and tags.
4. **CI** — `release/release.yaml` `ci.where` (or `--ci remote|local`):
   - `remote` (default): every job the repo's `ci_local.sh` dispatches runs on the host `.rops.env` names,
     one `hs-rops <ci.alias> --lane <ci.lane> -- <job>` cell each; then `ci_local.sh --local-only` runs
     here (the diff-vs-`main` steps: a lane holds no `main`, and there they would skip and read as green).
     `ROPS_HOST` or `ROPS_CMD_<ALIAS>` missing, or the host not answering ssh, STOPS the cut naming what is
     missing and `--ci local`. A red cell stops it naming the cell.
   - `local`: the whole of the repo's `ci_local.sh` runs here.
   The dry-run checklist prints where the CI will run, or why the cut would stop.
5. **skill structure** — `check_skill_structure.py --strict` over the skills root.
6. **skill cross-refs** — `validate_skill_crossrefs.py` (no broken references).
7. **gh auth** — `gh auth status` must succeed; the idempotency probe (`release_published()`, `gh release view`) needs it. Step 3 itself does not call `gh` — it only packs `dist/` locally, and `.github/workflows/release.yml` is the one that publishes.

The cheap gates (reconcile, docs, clean tree) run before the CI so a predictable failure surfaces without paying for the full suite first.

**Scope note:** the docs gate only covers structural docs under `docs/` (the frontmatter + graph tree; `.docsignore` exclusions apply). It does NOT touch root `*.md` (README/CLAUDE/BACKLOG) and does NOT judge content freshness — stale prose is an authoring step you run + review before the cut, not something release generates.

If preflight aborts, fix the cause and re-run the dry-run checklist — the engine has written nothing yet.
