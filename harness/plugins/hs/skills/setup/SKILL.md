---
name: hs:setup
injectable: false
description: >-
  Configure this project's harness — guard/stage policy, output language, code style, hook
  components — through the validated config CLIs. Use when onboarding a fresh install or
  changing a config decision.
allowed-tools: [Bash, Read, Grep, Glob]
argument-hint: "[full | short | show | none] [--global]"
metadata:
  compliance-tier: workflow
---

# hs:setup — project posture configuration

Walks the user through the harness's tunable posture, writing choices via the validated config CLIs (never hand-edit YAML in-session). The authoritative index of every knob, its file, default, and env override is `docs/harness/config-reference.md` — read it before presenting options so the defaults you quote are correct.

For onboarding (fresh install) or later changes; re-invoking re-shows the menu.

**Where you stand**: run `hs-run setup next`. It reads what disk can answer about this
project's posture, names the state, and carries the `rules` and `references` THAT state
needs. Read what the envelope names, and only that. But satisfying the envelope is **not
enough** to have configured anything: it routes DOCUMENTS, while the config file on disk
is what a setup step changes — write it through the CLI that owns it.

## Scope — localized config is BIN-GLOBAL under a shared install (read FIRST)

**Self-host (bin == project — a plain dev checkout, `HARNESS_BIN_ROOT` unset):** writes land in your one repo, so "project posture" is exact — rest of this caveat is moot.

**Global / courier install (`HARNESS_BIN_ROOT` points at a shared engine):** the localized-config knobs — guard/stage policy, output language — are **BIN-GLOBAL by design**, NOT per-project: a knob you write here reaches EVERY project that shares this binary.
Before writing one, SAY it is bin-global and get explicit intent. Full reach, the per-project `HARNESS_XXX` env escape hatch, and the hardened-read-only-bin case: **see `references/global-install-scope.md`**.

## STOP — `--global` needs re-confirmation before touching shared-bin engine state

`--global` governs the shared ENGINE-BINARY state (install / repoint / recipient bootstrap) — separate from the config-file scope above; setup itself never installs/repoints the engine.
If (and only if) the user passes `--global`, STOP and re-confirm with an `AskUserQuestion` before touching any shared-bin engine state — treat a missing re-confirmation as a hard stop. Full detail: **see `references/global-install-scope.md`**.

## Where to start

Run `hs-run setup next`. It names the first condition that is not yet satisfied — a missing dependency, an untrusted repo — and routes what that condition needs. When it answers `configured`, the mechanical part is done and everything below is the user's to choose.

## Layer-0.5 — personal-first is the default

The harness is **personal-first**: local never blocks the HUMAN at commit/push/ship — it generates receipts (plan-graph / verification / review-decision) and traces; quality is enforced at REMOTE CI. The AGENT cage stays (guard `enforcement=block` + the safety floors). There is no solo-vs-team posture question and no reviewer roster to configure.

## Layer-0.7 — skill onboarding (fresh default-off install)

A fresh install ships **default-off** (the `default_off` catalog in `skill-defaults.yaml` is stashed — readable, not callable). Surface the split, offer to re-enable whole clusters (`AskUserQuestion` over `skill-defaults.yaml` `clusters:` → `hs_cli.py skills --enable <csv>`), never auto-enable.
Protocol: `references/skill-onboarding.md`.

## Preamble — pick the depth (present in the project's output language)

Present these four options first and wait for the choice:

1. **Full** — walk every group (guard/stage, output language, DoD/test-policy) with current value + default, running the per-step coherence lint after each group and the consolidated coherence pass at the end (see "Coherence lint").
2. **Short** — only the three that matter most on day one: review-policy profile (default/thorough/ship-grade), guard preset, output language.
3. **Show meanings + defaults** — read `config-reference.md` back to the user, change nothing.
4. **None** — exit without changes.

## The groups and their CLIs

Quote the CURRENT resolved value before asking for a new one (run the read form, no `--set`).

**Path form (courier):** the `harness/scripts/*.py` tools and `harness/data/*.yaml` files below are named by their engine-relative path. Under a courier/global install the engine is NOT at `./harness`, so run/read them through the env:
`python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/<tool>.py …` (a bare `harness/…` resolves from the repo CWD and misses; the `${…:-.}` fallback keeps a self-host clone running from `.`).

| Group | Knobs | Tool | Takes effect |
|---|---|---|---|
| Output | language (en\|vi), thinking_language, code_style (off\|0–5) | `"${HARNESS_BIN_ROOT:-.}"/harness/scripts/output_config.py --set k=v` | live — read per invocation |
| Cook execution | parallel (bool), parallel_max (int>0) | `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/setup/scripts/cook_config.py --set k=v` | live — read per cook run (chain: `--parallel` flag > `HARNESS_COOK_PARALLEL` > this file) |
| Critique | mode (advisory\|gate) | `"${HARNESS_BIN_ROOT:-.}"/harness/scripts/critique_config.py --set mode=v` | live — read per critique run; **mode=gate writes a verdict but does NOT enforce** until a stage lists `critique-consensus` in its `requires:` |
| Guard policy | preset (strict\|balanced\|lenient), per-guard off\|warn\|block | `"${HARNESS_BIN_ROOT:-.}"/harness/plugins/hs/skills/setup/scripts/guard_config.py set-preset <p>` / `set <guard> <mode>` | **restart** (env-bound) |
| Stage policy | per-stage artifact requirements / hard | edit `"${HARNESS_BIN_ROOT:-.}"/harness/data/stage-policy.yaml` (hand edit; git-visible) | **restart** (env-bound) |
| Hook components | runtime on/off for hook-backed features (decision-capture, docs-ssot, … — `list` prints them) | `"${HARNESS_BIN_ROOT:-.}"/harness/scripts/component_config.py --set <name>=enabled\|disabled` (read: `show` / `list`) | **live** (hook self-skips on the flag) |
| Code map | `codemap.yaml` + snapshots behind hs-run's code facts | **`/hs:codemap`** (it asks before writing; `hs-code --repo . init status` reads the state) | live |
| Gemini partner lane | master, mode, write, stop_review_gate, `route_all_injection`; purposes→tier; `loop` {max_rounds, default_mode} | hand-edit `"${HARNESS_BIN_ROOT:-.}"/harness/data/gemini-partner.yaml` (dev override `$HARNESS_GEMINI_PARTNER`) | **restart** (env-bound) |

**Gemini partner lane (opt-in, ships OFF).** Inert while `master: off` — leave it unless the user wants a second-engine pass. Blast-radius axes (`write: sandbox_write`, `route_all_injection: on`) and the one-time `injectable` allowlist bootstrap: **see `references/gemini-lane-onboarding.md`**.

`code_style` shapes generated CODE only — comment density, verbosity, examples — never evidence or a gate decision; profiles in `"${HARNESS_BIN_ROOT:-.}"/harness/data/output-styles/`. Treat a non-off value as a deliberate save.

**Advisory nudges / gates — discover live, do NOT trust a static list.** Toggleable nudge/gate hooks span FOUR control planes (the per-project layer · the shipped `harness-hooks.yaml` `enabled` · guard-policy mode · class-default); a repo's `settings.json` can be stale so a hook shipped on disk may not be wired.
READ the live state before surfacing nudges in Full mode — `hs-cli hooks` prints each hook's resolved posture and the layer that decided it; flip one for this project with `hs-cli hooks --off/--on <csv>` (it writes the per-project merge layer, not the shipped file). Method, control model, drift checks, and the goal-cycle nudge convention: **see `references/hook-discovery.md`**.

`component_config.py` governs **hook components** only — features backed by a hook self-skip on an `enabled:false` flag (live, no restart). It does NOT add or remove SKILLS. Every skill ships in the one `hs` plugin; a skill is on by being present on disk and off by install-omission, and is toggled after install with `hs-cli skills --enable/--disable <skill>` (restart to re-index
— see the restart reminder). So "turn on viz/uiux/devops" is a **skill** action (`hs-cli skills` / install selection), NOT a component toggle — do not route it through `component_config`.

Read forms (no write): `output_config.py --file <path>`, `guard_config.py show`.

## Restart reminder — say this out loud

Guard and stage policy are **env-bound**, so they only take effect on a **new session**; output language and code style are live. Full details, the `/clear` vs full-restart distinction, and the plugin-re-enable case: **see `references/restart-reminder.md`**. After changing guard or stage, say out loud:

> Guard/stage changed — restart the session (or open a new terminal) for it to take effect.

## Trust this repo for shell-detector auto-fire

A standards rule may carry a **shell detector** — an arbitrary command the review-time runner executes. `/hs:setup` is the deliberate "this repo is mine" moment, so it is the one place that grants trust.

Run the trust step in the open (never silently):

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/hs_cli.py trust "$(pwd)"
```

Mention how to inspect or undo it: `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/hs_cli.py trust --list`.

## Boundaries

- Always write through the CLIs (`*_config.py --set`, `guard_config.py`) so validation runs and the change is a clean git diff. Do not hand-edit the YAML from inside the session.
- An unknown key or out-of-range value exits non-zero and writes nothing — report the failure, do not retry blindly.
- This skill changes CONFIG, never code or evidence. Gate decisions still follow the written files.

## Review and test gate config (4 tiers — surface during Full or when asked)

The MAIN tier (review-policy, code-review, test-policy/DoD) affects the ship gate — surface it on day one. Full tier tables, the posture-aware DoD walk, and the tier-1/tier-2 override-surface decision: **see `references/gate-config.md`**.
Write scope is a deny-list: a `workflow-subagent` edits project source out of the box (no lane setup), refused only the hard floor.

## Coherence lint — warn on a config that fights itself (per-step + final)

After each group AND once at the end (Full mode), check the written config for internal incoherence or a mismatch to who the user is — a posture too strict or too loose for how this repo is actually worked. Warn and ASK, never silently correct; user decisions hold. Full check list + framing: **see `references/coherence-lint.md`**.

## Related skills

- `hs:codemap`: the repo's code map (set up, decline, refresh) — setup only points there.
- `hs:techstack`: read-only stack detection — run first on a non-Python repo so posture matches the real test command.
- `hs:rule-author`: for authoring new compliance standards or shell-detector rules — invoked from setup's TIER 3 SHOW step when the user wants to change `standards.yaml`.

## Rendering

Render reports by resolving `language` / `code_style` live via `python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/output_config.py --resolved` — never hand-read the tracked `output.yaml`, which is the fail-closed gate path and ignores a dev override. Evidence (file:line, IDs, SHAs, numbers, quotes) is never translated or restyled.
