# Skill onboarding — the default-off catalog (Layer-0.7)

A fresh install ships **default-off**: the 16-skill spine floor plus the interview keep-list (QA-extras, assistant-brain, remember/rule-author/docs, and the live spine deps) stay ON and the rest are STASHED under `harness/plugins/hs/disabled-skills/`, reachable on demand. Run this once on a fresh install, in the project's output language.

## What to surface

1. **State the split.** Read `"${HARNESS_BIN_ROOT:-.}"/harness/data/skill-defaults.yaml`: the `default_off` list size is the stashed count; `all_skills − default_off` is the ON count. Say both numbers.
2. **A stashed skill cannot be run.** It is readable, not callable: open its stash `SKILL.md` and do the work inline, or turn it on. Say this, so an off skill reads as a choice rather than as breakage.
3. **Offer clusters, do not push them.** Turn whole clusters back on with `AskUserQuestion` (multiSelect, one option per `clusters:` group in `skill-defaults.yaml` — e.g. `viz`, `uiux`, `ai`, `stack`, `devops`, `integrations`, `extra`). Recommended = keep the recommended set (pick nothing).

## Apply the picks

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/hs_cli.py skills --enable <csv-of-cluster-skills>
```

Each cluster expands to its member skills (join the picked clusters' lists). Nothing picked
→ leave the recommended set as-is. **Never auto-enable clusters on the default install path** — the recommended set is the default, and the stash keeps the rest one command away.
