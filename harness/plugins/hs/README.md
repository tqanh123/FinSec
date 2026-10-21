<!-- generated: plugin-readme -->

# hs

SDLC harness — one hs: plugin holding the full skill catalog (spine SDLC loop hs:plan/cook/test/ship/fix/debug/code-review/review-pr/git/scout/understand/setup/triage plus opt-in groups flow/think/research/create/mem/meta and the domain ports ai/devops/stack/uiux/integrations/extra/viz). Groups are install-time logical labels; skills are selected per-skill at install.

**Default:** always-on (the SDLC spine — installed and enabled on every harness).
**Version:** 7.3.0

## Packaging notes

This plugin is **repo-embedded by design**, not marketplace-cache portable: its skills/agents assume the full harness tree at `harness/` (scripts, data, rules) under the project root, and its fail-closed gate hooks (`hs-cli gates` counts them) are wired per-project in `.claude/settings.json` rather than in a plugin-level `hooks/hooks.json` — a deliberate two-zone choice that keeps gate policy out of a user-scope manifest an agent could otherwise widen via a project-local write (the F3 hole). It still installs cleanly in global mode (one shared `$HARNESS_BIN_ROOT` binary, many projects); it just is not copy-into-`~/.claude/plugins/cache/` portable today. See the INV-2 plugin-self-containment investigation for the full trade-off.

`workflows/*.js` (`ping`, `base-fanout-consolidate`, `base-pipeline-verify`) uses Claude Code's plugin-bundled workflow loading (`<plugin>/workflows/*.js` namespaced `hs:`). This loading path is not yet listed in CC's official file-locations table (https://code.claude.com/docs/en/workflows) — treat it as an undocumented-but-observed convention. The `scriptPath` fallback documented in `harness/rules/orchestration-protocol.md` stays mandatory as the hedge if the convention ever changes.

## Skills (68)

| Invoke | Purpose |
|---|---|
| `/hs:advise` | Pressure-test an idea, plan, or design one question at a time, then give an honest verdict — what to do, what to avoid, better alternatives… |
| `/hs:bakeoff` | Empirical bake-off — run 2-4 candidate probes on one mechanical metric, pick the winner by numbers or hand to a human when inside the noise… |
| `/hs:bootstrap` | Bootstrap a new project from scratch -- clarify requirements, init git, create doc structure, delegate to hs:plan + hs:cook. |
| `/hs:brainstorm` | Brainstorm solutions with honest trade-off analysis — ideation, architecture decisions, technical debate, feasibility exploration. |
| `/hs:cleanup` | Remove files an over-install left behind, safely: auto-clear pristine version-dropped files, and decide modified ones interactively (keep o… |
| `/hs:code-review` | Review code with technical rigor — bugs, regressions, security. |
| `/hs:codemap` | Set up, refresh and check this repo's code map (codemap.yaml + snapshots) that backs the code facts plan, cook, fix and review read. |
| `/hs:coding-agent-orchestration` | Coordinate several coding agents across one workflow — who plans, implements, reviews, tests; how work splits and hands off. |
| `/hs:compound` | Compound the harness's own self-knowledge — telemetry, formalization candidates, backlog, a completeness critic — into ONE ranked self-impr… |
| `/hs:context-engineering` | Manage the context budget — check limits, optimize tokens, coordinate subagents, debug context failures. |
| `/hs:contract-test` | Validate a live contract by running the real probe — call the API/CLI, check status, exit code and payload — then anchor the result as manu… |
| `/hs:cook` | Execute an approved plan phase by phase — TDD red→green, generate verification/review-decision artifacts, trace every step. |
| `/hs:critique` | Multi-lens adversarial critique — fan independent lenses at an artifact, consolidate into one ranked verdict. |
| `/hs:debug` | Systematic debugging with root-cause analysis before fixing. |
| `/hs:deploy` | Deploy a project to any platform, target auto-detected from config files and docs/deployment.md. |
| `/hs:diagram` | Compile typed JSON IR (architecture, dataflow, lifecycle, sequence, workflow) into a validated interactive system map -- SVG, fragment, or … |
| `/hs:discover` | Shape an ambiguous problem into a discovery brief for hs:plan — research + brainstorm chain -> direction summary, trade-offs, open question… |
| `/hs:dispatch` | Decide who owns a piece of work before any of it starts — answer it directly, route it to one skill, or chain several — and set the quality… |
| `/hs:docs` | Analyze codebase and manage project documentation — init, update, summarize. |
| `/hs:docs-build` | Render docs (md + frontmatter) + _index + _present into public/ (showcase HTML, theme and effects kept) and dist/ (flat-md, pdf, excel). |
| `/hs:docs-scaffold` | Generate a doc skeleton from a template (frontmatter + headings + > TBD) for the capability-driven required set. |
| `/hs:docs-standardize` | Validate structural docs (frontmatter + graph invariants) → artifact JSON → gate. |
| `/hs:drawio` | Generate polished, editable .drawio diagrams — branded shape libraries (AWS/Azure/GCP/Cisco/K8s/UML/ER), swimlanes, strict geometry, PNG/SV… |
| `/hs:eli5` | Explain something so the reader actually gets it — the last answer restated plainly, or a topic, error, or snippet pitched at a named reade… |
| `/hs:eval-bootstrap` | Bootstrap a full evals/ framework for THIS repo — deterministic scorer, ground truth, optional LLM judge, CI, API-key-free. |
| `/hs:find-skills` | Locate and route to the correct hs:* skill — analyze intent, query the hs plugin registry, return the exact invoke command. |
| `/hs:finetune` | Route work that must improve a MEASURED result to the skill that owns that outcome, and hand over the bounded-iteration contract. |
| `/hs:fix` | Fix bugs, test failures, and CI/CD failures with an evidence-based workflow. |
| `/hs:gemini` | Delegate an advisory or coding job to the gemini partner lane — research, review, adversarial review, or a worktree-staged code task. |
| `/hs:git` | Git operations with conventional commits. |
| `/hs:goal` | Prepare a built-in /goal run at authoring time — interview the objective into a NEW self-contained goal.md, arm the autonomy bell, scaffold… |
| `/hs:goal-warmup` | Outcome-locked preflight before a long autonomous run: interview to an approved Outcome Contract, plan, review, whole-plan preflight matrix… |
| `/hs:handoff` | Create a concise, redacted conversation handoff for a fresh agent session. |
| `/hs:harness-creator` | Create new harness primitives (hook, rule, schema, data, script, agent) — not skills. |
| `/hs:insights` | Read-only usage insights from harness telemetry — hot vs never-used skills, workflow chains, gate-block patterns — with proposed optimizati… |
| `/hs:loop` | In-session self-optimization loop — N iterations against a measurable metric, learns from git history, auto-keep/discard changes. |
| `/hs:mermaidjs` | Text-based Mermaid.js v11 diagrams INLINE in markdown -- flowchart, sequence, class, ER, state, gantt, architecture -- renders natively on … |
| `/hs:partner` | Delegate an advisory or coding job to the ccs partner lane — a second full-Claude session under a named provider, provenance-stamped with i… |
| `/hs:plan` | Create a verified implementation plan — research, constraint-scan, phase design, red-team, and validate before cook. |
| `/hs:port` | Extract, compare, port, or adapt a feature from another repo into this project. |
| `/hs:predict` | 5 expert personas independently debate a proposed change before implementation, catching architectural, security, performance, and UX risks… |
| `/hs:preview` | Explain a change or architecture with a diagram when visuals are clearer than prose — flow/architecture, before-after, sequence; output to … |
| `/hs:problem-solving` | Structured unblocking — identify the block type, choose the right technique, reframe before resuming implementation. |
| `/hs:prompt` | Write one optimized, ready-to-paste prompt from a rough idea, or fix, adapt, or split an existing one. |
| `/hs:release` | Cut a harness release: cut version, push tag, pack dist/ for verification (release.yml publishes it). |
| `/hs:remember` | Capture what this session actually learned — decisions, non-obvious facts, correcting feedback — into the DEC ledger, auto-memory, or BACKL… |
| `/hs:research` | Verified technical research — pose a question, gather multiple sources, verify evidence, synthesize a report. |
| `/hs:review-pr` | Review a GitHub PR or GitLab MR (forge auto-detected) — diff, CI, correctness, security, breaking changes. |
| `/hs:rops` | Run a heavy build, test, benchmark, or container on a remote machine over ssh + rsync, from ANY repo — destination, excludes and aliases de… |
| `/hs:rule-author` | Author a per-repo review-rule override under docs/standards/ — detects conflicts with the shipped standards and records a reason for each. |
| `/hs:scenario` | Decompose a feature/code path across 15 dimensions to generate edge cases, risks, and test targets before implementation. |
| `/hs:scout` | Fast codebase exploration using parallel agents — find files, locate code, gather context before implementing or debugging. |
| `/hs:security-scan` | Scan codebase for security issues — hardcoded secrets, dependency CVEs, injection/authz gaps, STRIDE+OWASP. |
| `/hs:setup` | Configure this project's harness — guard/stage policy, output language, code style, hook components — through the validated config CLIs. |
| `/hs:shape` | Bridge an approved PO story spec into dev-task decomposition, roadmap and effort rollup, market-experiment specs, and the POC-gate loop. |
| `/hs:ship` | Gated ship pipeline: review PASS → verification PASS → human approval → push/pr. |
| `/hs:skill-creator` | Create or update hs:* skills for the harness — SKILL.md, frontmatter, thin-core, references, validate via catalog.py. |
| `/hs:spec` | Interview-driven product spec hierarchy (Vision/BRD/PRD/Epic/Story) with traceability, validation, and visualization. |
| `/hs:team` | Orchestrate parallel Agent Teams — research, cook, review, debug with multiple independent teammates. |
| `/hs:tech-graph` | Generate publish-grade static technical diagrams (SVG+PNG, 8 styles) — architecture, data flow, sequence, agent/memory, concept map — for e… |
| `/hs:techstack` | Detect the repo's tech stack — languages, test command, package manager, CI — so the harness adapts instead of assuming pytest. |
| `/hs:test` | Run and validate tests for the current change — unit/integration profiles, concise QA report, 100% pass gate. |
| `/hs:threejs` | Build 3D web experiences with Three.js. |
| `/hs:triage` | Orchestrate the defect lifecycle — reproduce, classify, and gate bugs via hs:scout→hs:debug→hs:fix→hs:test. |
| `/hs:understand` | Orchestrate codebase comprehension before touching code — chain hs:scout and hs:context-engineering to build a codebase map. |
| `/hs:web-testing` | Web testing with Playwright, Vitest, k6. |
| `/hs:workflow-orchestrate` | Design a spawn strategy for a delegated task — pick subagents vs Workflow vs Agent Teams, size and group the fan-out, batch-consolidate, ea… |
| `/hs:worktree` | Create, inspect, and clean up isolated git worktrees. |

Each skill's full contract lives in its `SKILL.md`; load-on-demand detail lives under the skill's `references/`. This index is generated — regenerate with `harness/scripts/generate_plugin_readme.py`.
