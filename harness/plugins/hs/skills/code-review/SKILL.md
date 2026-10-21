---
name: hs:code-review
injectable: false
description: Review code with technical rigor — bugs, regressions, security. Supports pending changes, PR number, commit hash, codebase scan. Verdict in review-decision.json; failing review blocks pr/ship/deploy. Use when code needs a rigorous pre-merge review.
argument-hint: "[--effort <low|medium|high|xhigh|max> | --low|--medium|--high|--xhigh|--max] [--profile <name>] [--rounds <n>] [--auto] [--in-place] [--fix | --fix-auto] [--reply] [--spec <plan>] [--pending | <#PR|commit|codebase>] [--html] [--wiki] [--ultra [N] | --debate]"
allowed-tools: [Bash, Read, Write, Grep, Glob, Task]
metadata:
  compliance-tier: gate
---

# hs:code-review — evidence-based code review

Review focused on production risk: logic defects, regressions, trust-boundary violations, security, performance. No rubber-stamping; no praise-padding. Evidence before claims.

**Posture**: assume code was written by an AI agent until proven otherwise. Clean structure, confident comments, and happy-path tests are not evidence of correctness. Rulebook-first, not a friction-avoiding collaborator.

**What each step needs is routed by the CLI, not listed here.** Every envelope carries the
`rules` and `references` THIS state needs, plus an `advisory` when the step has one. Read what
the envelope names, and only that. But the envelope routes DOCUMENTS, never the object of the
work: reading everything it names is not enough to have judged the diff — the changed files
on disk are what the review is about, so open them.

## Input modes

Auto-detect from argument. No argument → `AskUserQuestion`.

| Input | Mode | Diff source |
|---|---|---|
| `--pending` | Pending | staged + unstaged (`git diff HEAD`) |
| `#123` or PR URL | PR | `gh pr diff` |
| `abc1234` (7+ hex) | Commit | `git show` |
| `codebase` | Full scan | `repomix` CLI compact then analyze |
| *(none)* | Recent | changes in current context |

Argument resolution details: `references/input-mode-resolution.md`.

## Effort (recall-mode)

Effort scales Stage-2 finding production (fan-out lenses → adversarial verify → sweep). Pass it as a flag:
`--effort <low|medium|high|xhigh|max>`, or the shorthand `--low|--medium|--high|--xhigh|--max` (equivalent).
A bare leading token (`high`) is still accepted for back-compat but the flag forms are canonical.
Default **`low`** = single-pass review. Resolution is arg > env > config > default, and the CLI
does it — `low` returns state `ready`, anything higher returns `ready_recall` and routes the
fan-out documents to it. The recall engine changes only HOW findings are produced; the verdict,
gate, rules-layer, and dismissals path are unchanged.

## Orthogonal flags

| Flag | Effect |
|---|---|
| `--auto` | Agent self-adjudicates the **REVIEW / effort decision only** (auto-bumps to the suggested effort on a scope mismatch + logs it). Does NOT govern fixes. |
| `--in-place` / `--inline` | Review directly in the main agent. **Default is OFF for a direct invocation** — independence is what a direct ask is FOR: subagents (Task / Workflow) at effort ≥ medium, `@code-reviewer` preferred even at `low`. When cook/fix reach this skill as their END step, the caller's measured gate decides instead — see Stage 2. |
| `--fix` | Apply only the fixes that need no user confirmation (clear correctness bugs + safe cleanups); skip + report anything risky, out-of-diff, or a judgment call |
| `--fix-auto` | Auto-fix and self-decide ALL findings → drives `hs:fix --auto`. Mutually exclusive with `--fix`; both OFF by default |
| `--reply` | Post review to PR via `gh pr review` after review (or after fix loop). One-word alias: `--comment` |
| `--spec <plan>` | Enable Stage 1 spec-compliance before Stage 2 quality review |
| `--profile <name>` / `--rounds <n>` | Multi-round committing review — profiles, caps, axes. Protocol: `references/multi-round.md` |

Flags are composable: `hs:code-review --high #123 --fix --reply` (or `--effort high`) runs a high-recall review, the fix loop, then posts the final review.

## Workflow

```
1. Parse argument → determine mode + flags. For the `--pending` / Recent modes, get effort +
   scope + rules + risk in ONE call instead of four:
   `hs-run review next [--effort <level> | --<level>] [--range <commit>|<A..B>]`.
   exit 0 `nothing_changed` → nothing in what it read (the worktree, or the range); read
   `warnings` for commits it did not read. exit 2 → git cannot answer; read `warnings`, do NOT
   proceed. exit 10 → `artifacts[0]` gives `effort` `changed_files` `scope` `rules_source`
   `rule_count` `risk` `range`; steps 1.5/2.5/2.7 READ those instead of recomputing. Committed
   work (a phase, a commit) → `--range`, diff with `git diff <range>`. PR / codebase modes
   read history the verb does not: `review_recall.py --effort <level>`, then 1.5/2.5/2.7.
1.5 Scope read: print `scope · N files · signals → suggested effort`
   (`artifacts[0].scope`, or review_recall.assess_scope outside preflight modes). On mismatch
   vs resolved effort — no --auto →
   AskUserQuestion [bump (rec)/keep/other]; headless → record + proceed at resolved;
   --auto → self-bump + log. Agent never auto-bumps without --auto.
2. Edge-case scout (required when ≥3 files changed OR the diff carries a contract-delta — see references/contract-delta.md): hs:scout before review
2.5 Review-rules layer (references/review-rules-layer.md):
   - rules ← rule_view.load_rules_dual(root, changed_files) (operational rules from the standards
     tree — the single source; `hs-run review next` already reports its `rule_count` +
     `rules_source`, so a mismatch there means you are reviewing against a different rule set
     than the preflight measured — investigate, do not average the two)
   - judge applied rules → violations[]; write rule-scan.json via rule_view.build_rule_scan (records `changed_files`, the coverage-gate's diff source)
   - any violation severity==critical → verdict BLOCKED (review-decision BLOCKED too)
   - only info → PASS_WITH_RISK / Suggestion
2.7 Risk ceremony (references/risk-ceremony.md): `artifacts[0].risk` from step 1, or
   risk_rubric.derive_risk(root, changed_files) outside the preflight modes — same rubric,
   same tiers, the verb only reports it earlier.
   → high_risk (auth/migration/secret/api) ⇒ run hs:security-scan to PASS + reviewer≠author before verdict
   (skill-enforced, NOT a hard gate — security-scan is not in stage requires:; see risk-ceremony.md boundary)
   The verb REPORTS the tier; it never discharges the ceremony. `ceremony.require_security_scan:
   true` in the envelope is the obligation being stated, not satisfied.
3. [If --spec] Stage 1 — Spec compliance (references/spec-compliance.md)
   → FAIL? fix → re-review Stage 1 → then continue
4. Stage 2 — Quality review (effort-scaled, **subagent-by-default unless --in-place/--inline** — for a DIRECT invocation. Reached as `hs:cook`/`hs:fix`'s end step instead, the caller's measured gate already decided inline-vs-spawn; honor it rather than re-deriving):
   - **Route first through `hs:workflow-orchestrate`** (for effort ≥ medium, when the review fans out,
     before any spawn): state `reason` (why this recall fan-out), `strategy` (mode + `hs:base-pipeline-verify`
     + lens→count by effort), `scope` (diff surface + lens count from `code-review.yaml`). Findings feed a
     verdict, so this is a **find→verify** shape. Consume `route_depth` — `light` → proceed via the
     recall-mode ladder; `agent` → escalate the `@workflow-orchestrator` agent before spawning.
   - no `--in-place` (direct invocation; a caller-measured gate replaces this branch when cook/fix sent you):
     - effort==low → spawn the `@code-reviewer` agent (do not review inline in the main agent).
     - effort≥medium → fan-out lenses → verify → sweep;
       high+ via the ultracode Workflow tool — shared hs:base-pipeline-verify base,
       inline + Task fallback). Every path feeds the SAME verdict-truth-table +
       dismissals + review-decision.json.
   - `--in-place` → the main agent may perform the review directly; this is opt-in only
     and must be logged in the report (stamp: `in-place:true`).
   Checklist: correctness · security · breaking-changes ·
   performance · testing · anti-slop · architecture (references/review-dimensions.md)
   Architecture: when the diff is structural, `hs-run review next` answers
   `needs_architecture_review` and names which glob matched — do not re-derive the set,
   it is per-project. That state routes what the pass needs and says what to record.
   On the naming/anti-slop pass, using a term's registered forbidden wording is a finding.
5. Verdict: Approve / Request changes / Comment. Per-finding confirmed / dismissed / needs-human, code_evidence mandatory on confirmed, any condition FALSE ⇒ dismissed (references/verdict-truth-table.md). The severity taxonomy below pins to the harness's existing scales, never a 4th vocabulary.
   Before finalizing, lookup the dismissals store by fingerprint and SHOW any prior dismissal — never auto-hide ("${HARNESS_BIN_ROOT:-.}"/harness/scripts/dismissals_store.py).
6. Write artifact via write_review_decision.py (stamps run_seq + atomic write — NOT a hand-written YAML); see references/verdict-and-artifact.md
7. [If --fix] apply no-confirm fixes only (skip+report risky); [If --fix-auto]
   hs:fix --auto (self-decide all) → hs:git cp → re-review. --auto never implies fix.
8. [If --reply] Post review to PR (references/pr-review-workflow.md)
```

## Severity taxonomy

| Level | Meaning | Action |
|---|---|---|
| **Critical** | Bug, security, data loss, breaking change | Must fix before merge |
| **Important** | Wrong logic, missing validation, structural slop | Should fix |
| **Suggestion** | Style, minor, micro slop | Nice-to-have |

Full taxonomy + anti-slop patterns: `references/severity-taxonomy.md`.

**Complexity ladder (anti-slop sub-dimension).** On the anti-slop pass, judge whether new code
could drop to a lower rung of the minimal implementation ladder — **Delete** /
**Standard library** / **Existing** dependency-utility / **Tiny** change / **Shrink**. This **adds** to the
anti-slop taxonomy above, it does not replace it: report a complexity note separately and never
let it override a correctness, security, or scope finding.

## Output language

Resolve `language` / `code_style` live via `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/output_config.py --resolved` — never hand-read the tracked file.

## Boundaries

- Do NOT self-merge, do NOT self-deploy; review is a gate — the merge decision belongs to the human.
- Verdict `BLOCKED` → write artifact with `verdict: BLOCKED` → pr/ship/deploy do not clear (verdict != PASS): the remote CI receipts-gate (`artifact_check_cli.py` on each plan the PR touches) blocks; locally `gate_stage.py` only advises. The artifact records the blocking reason for audit/replay.
- Verdict `PASS` → artifact written with verdict PASS → a hard stage may proceed.
- Verdict `PASS_WITH_RISK` → artifact written, but a HARD stage (pr/ship/deploy) still BLOCKS: only an exact PASS clears it. `PASS_WITH_RISK` is a conscious soft-accept (record the risk, keep moving on soft stages), never a ship license.
- Re-review cycle maximum 3 times — if no convergence, escalate to user.
- Do not modify files outside `plans/reports/` (reports) and the active plan's artifact path.
- On completion: report verdict + absolute artifact path + unresolved questions.
- `--ultra [N]`/`--debate`: call `hs:workflow-orchestrate --ultra [N]|--debate --run-id <slug>` to size + drive N independent review passes over the SAME diff/target (no shared context); `hs:escalation-consultant` synthesizes the verdict, and only that synthesized verdict is written to `review-decision.json`. Approval gate is `hs:workflow-orchestrate`'s own.

## HARD-GATE (real wiring)

```
harness/hooks/gate_stage.py          — at stage pr|ship|deploy, flags a missing
                                       plans/<plan>/artifacts/review-decision.json
                                       or verdict != PASS: advisory locally,
                                       blocking in the remote receipts-gate
harness/schemas/artifact-review-decision.json
                                     — verdict schema (PASS|PASS_WITH_RISK|BLOCKED)
                                       + reviewer + role + rationale + plan_hash
harness/plugins/hs/agents/code-reviewer.md
                                     — agent that performs the review (Stage 2)
plans/reports/                       — long-term review report storage
```

Stage policy is read from `"${HARNESS_BIN_ROOT:-.}"/harness/data/stage-policy.yaml` — when a stage's `requires:` lists `review-decision`, the gate activates for that stage. The gate is a presence gate (verifies artifact EXISTS and verdict satisfies policy), not a role gate.

## Verification gate

Before any claim of "review complete" or "PASS", run the command that proves it fresh, read the full output and exit code, and report the true status. No verification evidence means no PASS claim.

## Observe checkpoint (end-of-work)

When the review is done, emit at most ONE closed-vocab signal — valid terms here:
`thin-evidence`, `gate-repeat-block`, `trigger-near-miss`. Most runs emit nothing.

## Workflow position

Typically runs after: `hs:cook` (review after implement) · `hs:fix` (review after bug fix) Typically runs before: `hs:git` ship
Related: `hs:scout` (scout before review) · `hs:test` (test before review)

## References (load on demand)

| Drawer | Content | When to load |
|---|---|---|
| `references/checklists/base.md` | Universal two-pass checklist (critical/informational): injection, race conditions, security boundaries, auth, test gaps, performance | Always load when running checklist-based review |
| `references/checklists/web-app.md` | Overlay for frontend frameworks: XSS, CSRF, N+1 queries, a11y, responsive layout | When project has React/Vue/Svelte/Next or JSX/TSX in diff |
| `references/checklists/api.md` | Overlay for REST/GraphQL/gRPC: rate limiting, input validation, data exposure, API design, observability | When project exposes API routes or OpenAPI spec exists |
| `references/checklist-workflow.md` | Step-by-step workflow to apply checklists: auto-detect type, load overlays, two-pass diff review, suppression check, output format | When running structured pre-landing or security-audit review |
| `references/multi-round.md` | Multi-round committing review: profiles, the round loop, the four axes (compounding/per_aspect/blind_main_sub/refute), caps, `--profile`/`--rounds`, `rounds_run` stamp | When `--profile`/`--rounds` is used |
| `references/feedback-reception.md` | How to RECEIVE review feedback: triage findings, push back with evidence, resist reflexive agreement | When acting on a review as the author, or coaching feedback reception |
| `references/issue-routing.md` | Where a deferred finding goes: BACKLOG entry + report link (headless) or interactive routing | When routing a finding that will not be fixed this round |
| `references/grid-consume.md` | Fold a coverage-grid `reject` into one review finding (advisory; grid never auto-BLOCKs, reviewer scores severity) | When a `coverage-grid` artifact exists for the target |

## Optional shadow comparison (off by default)

Profile `finding-triage`; procedure in `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/_shared/hs-run/shadow-compare.md`. The decision already made stands.
