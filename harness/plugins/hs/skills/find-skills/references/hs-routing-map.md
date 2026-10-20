# hs:* Routing Map — by intent and SDLC stage

`hs:find-skills` uses this file for routing RULES + the high-traffic intent table below. This table is NOT the full catalog — the live catalog is `load_catalog()` (`"${HARNESS_BIN_ROOT:-.}"/harness/scripts/catalog.py`) plus `"${HARNESS_BIN_ROOT:-.}"/harness/scripts/disabled_skills.py --list`. ALWAYS merge those live sources before concluding a gap; a skill absent from this file may still exist.

**Selection rules**:

- Use the most specific skill first; router skills (`find-skills`, `triage`, `understand`) serve genuine orchestration only, not mere uncertainty.
- `--stage <plan|build|verify|ship|doc|meta>` filters by the "Stage" column below.
- Tier `workflow` = has gates/artifact constraints; `knowledge` = advisory, generates output, does not block the pipeline.

---

## Planning and discovery (Stage: PLAN)

| When to use | Skill | Tier |
|---|---|---|
| Create a verified implementation plan (research → phase → red-team) | `/hs:plan` | workflow |
| Multi-source technical research, evidence verification, option comparison | `/hs:research` | workflow |
| Brainstorm solutions, trade-offs, architecture debate before committing to a direction | `/hs:brainstorm` | workflow |
| Shape an ambiguous problem into a discovery brief for `hs:plan` | `/hs:discover` | workflow |
| Structured unblocking when a loop or hypothesis has failed 3+ times | `/hs:problem-solving` | workflow |
| 5 personas debate risk before a large or high-risk change | `/hs:predict` | knowledge |
| Decompose a feature across 15 dimensions into edge cases, risks, test targets | `/hs:scenario` | knowledge |

## Understanding the codebase before touching it (Stage: PLAN)

| When to use | Skill | Tier |
|---|---|---|
| Orchestrate comprehension: chain repomix→scout→context-engineering into a map | `/hs:understand` | workflow |
| Fast discovery with parallel agents — find files, locate code | `/hs:scout` | workflow |
| Manage context budget, multi-agent coordination, debug context failures | `/hs:context-engineering` | workflow |

## Execution / cook (Stage: BUILD)

| When to use | Skill | Tier |
|---|---|---|
| Execute an approved plan by phase — TDD red→green, produce artifacts | `/hs:cook` | workflow |
| Start a new project from scratch → delegate to `hs:plan` + `hs:cook` | `/hs:bootstrap` | workflow |
| In-session self-optimizing loop against a measurable metric (no ship) | `/hs:loop` | workflow |

## Bug fixing / debug (Stage: BUILD)

| When to use | Skill | Tier |
|---|---|---|
| Orchestrate the full error lifecycle: scout→debug→fix→test, gate bug | `/hs:triage` | workflow |
| Fix a specific bug/error or CI failure (cause is already known) | `/hs:fix` | workflow |
| Debug root cause before fixing (cause is not yet known) | `/hs:debug` | workflow |

## Review and gate (Stage: VERIFY)

| When to use | Skill | Tier |
|---|---|---|
| Review diff/commit/full-codebase — verdict blocks the pr/ship/deploy stage | `/hs:code-review` | workflow |
| Review a GitHub PR broadly, supports `--fix` / `--reply` | `/hs:review-pr` | workflow |
| Security scan — hardcoded secrets, CVE, injection/authz, STRIDE+OWASP | `/hs:security-scan` | workflow |

## Test (Stage: VERIFY)

| When to use | Skill | Tier |
|---|---|---|
| Run and validate tests for current changes, gate 100% pass | `/hs:test` | workflow |
| Pin an API/schema contract and catch a drift between producer and consumer | `/hs:contract-test` | knowledge |

## Ship (Stage: SHIP)

| When to use | Skill | Tier |
|---|---|---|
| Git commit/push/PR/merge, auto-split, secret scan before commit | `/hs:git` | workflow |
| Ship pipeline with gate: review→verification→human approval→push/pr | `/hs:ship` | workflow |

## Documentation (Stage: DOC)

| When to use | Skill | Tier |
|---|---|---|
| Analyze codebase and manage project docs — init/update/summarize | `/hs:docs` | workflow |

## Diagram and graph tooling

| When to use | Skill | Tier |
|---|---|---|
| Publish-grade static diagram image (SVG+PNG, 8 styles) to embed in docs/slides | `/hs:tech-graph` | knowledge |
| INLINE diagram in markdown (renders on GitHub/GitLab) via Mermaid.js v11 | `/hs:mermaidjs` | knowledge |
| Build or debug 3D web content (scenes, meshes, cameras, WebGL) | `/hs:threejs` | knowledge |
| EXPLAIN a change or architecture as a diagram (no diagram file delivered) | `/hs:preview` | workflow |

## Orchestrator / coordination (Stage: META)

| When to use | Skill | Tier |
|---|---|---|
| Find the right `hs:*` skill for a task (this skill itself) | `/hs:find-skills` | workflow |
| Coordinate parallel Agent Teams — research/cook/review/debug | `/hs:team` | workflow |

## Unattended

| When to use | Skill | Tier |
|---|---|---|
| Multi-iteration self-optimizing loop against a metric (in session) | `/hs:loop` | workflow |

**Autonomous-iteration routing:** improve a measurable metric over N iterations (single number from a shell command) → `hs:loop`; phased plan with gates before cooking → `hs:plan`; ambiguous problem needing discovery first → `hs:discover`; single verified research pass, no loop → `hs:research`. Every skill in this group inherits the autonomous
safety posture in `loop/references/safety-guardrails.md` (atomic commit per iteration, verify-or-rollback, command safety screen, ship requires explicit approval).

## Harness utilities and extension

| When to use | Skill | Tier |
|---|---|---|
| Create or update an `hs:*` skill (SKILL.md, frontmatter, references) | `/hs:skill-creator` | workflow |
| Create a harness primitive (hook/rule/schema/data/script/agent) — not a skill | `/hs:harness-creator` | workflow |
| Create, inspect, and clean isolated git worktrees | `/hs:worktree` | workflow |
| Decide who owns a piece of work before it starts — answer, route, or chain, and set the gate the risk earns | `/hs:dispatch` | workflow |
| Prepare a long unattended `/goal` run: interview the objective, arm the bell, scaffold | `/hs:goal` | workflow |
| Preflight before leaving a run unattended — Ready / Blocked / Decision-required | `/hs:goal-warmup` | workflow |
| Hand a fresh session the redacted state of this one | `/hs:handoff` | workflow |
| Explain something plainly, pitched at a named audience | `/hs:eli5` | knowledge |
| Write, fix, adapt or split a prompt for any AI tool | `/hs:prompt` | knowledge |
| Stand up an evals/ framework for this repo — scorer, ground truth, CI | `/hs:eval-bootstrap` | workflow |
| Build the change so the NEXT one is cheaper, not just this one | `/hs:compound` | workflow |
| Cut a versioned harness release — tag, dist, gh release | `/hs:release` | workflow |
| Sweep what a removed or disabled skill left behind | `/hs:cleanup` | workflow |

---

## Role assignment when multiple skills match

- **Clear cause → `hs:fix`; unknown cause → `hs:debug`; full error lifecycle → `hs:triage`.**
- **Small diff/commit → `hs:code-review`; GitHub PR/branch → `hs:review-pr`.**
- **Unfamiliar codebase → `hs:understand` (orchestrate) before `hs:plan`.**
- **Ambiguous problem → `hs:discover` before `hs:plan`; multi-source needed → `hs:research`.**
- **Diagram: inline-doc → `hs:mermaidjs`; publish-grade → `hs:tech-graph`; explain a change → `hs:preview`.**
- **Create a skill → `hs:skill-creator`; create a hook/script/rule/agent → `hs:harness-creator`.**

## Gap report

Before declaring a gap, verify against the LIVE catalog (`load_catalog()` + `disabled_skills.py --list`) — this file is a curated subset, not the census. If no skill matches after that check: clearly state "No skill for [intent]", suggest a workaround using the nearest skill or native tools. Do not fabricate skill names that fail the live-catalog check.
