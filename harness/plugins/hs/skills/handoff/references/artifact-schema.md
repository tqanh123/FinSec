# Handoff slip — artifact schema

The captured artifact is one Markdown file with a fixed section order, checked by
`scripts/validate_slip.py` before any `--dispatch`. This is the schema that
validator enforces — read it here rather than re-deriving the rule from the
script.

## Frontmatter (optional, recommended)

```yaml
---
handoff-version: 1
generated: 2026-09-29T10:49:00Z
focus: "continue the callback fix"
workspace: /path/to/repo
branch: feature/callback-fix
head: 26f9ff8
---
```

`handoff-version` lets the validator refuse a slip written under a future,
incompatible schema rather than misreading it. `1` is the only version this
skill writes or accepts today.

## Required section order

```markdown
# HANDOFF: <short title>

## Mission and current status
## Scope and guardrails
## Current state
## Decisions and rationale
## Work performed
## Verification
## Open risks and blockers
## Exact next actions
## Source pointers
```

Extra H2 sections may sit between required ones — the validator tolerates
them. It fails closed on two shapes only: a required heading absent, or the
required headings appearing out of the order above (a doc that has all nine
but shuffled is still refused, because a successor agent reading top-to-bottom
would hit "Decisions" before "Current state" exists to ground it).

## Section contracts

- **Mission and current status** — the desired outcome, a `Done:` list, a
  `Remaining:` list, `Urgency:` when known. Quote the task-focus string
  verbatim on the first line when one was given.
- **Scope and guardrails** — `Workspace:`, `In scope:`, `Out of scope:`,
  `Constraints:`, `Safety boundaries:`.
- **Current state** — `Branch:`, `HEAD:`, `Working tree:`, `Changed files:`
  (bounded), `Untracked files:` (bounded), `Intentional local modifications:`
  (`yes|no|not captured`).
- **Decisions and rationale** — `Decision — Rationale — Alternative rejected —
  Reference` rows.
- **Work performed** — commands run with their observed outcome, changes
  described at file granularity, ending with `N redactions applied.`
- **Verification** — `Check — Command — Outcome — When`, plus a `Not run:`
  sub-list naming skipped checks and why.
- **Open risks and blockers** — each bullet carries `Type:` (blocker | risk |
  question), `Owner:` (`unknown` when unknown), `Impact:`.
- **Exact next actions** — a numbered list; item 1 is bold-prefixed
  `**First safe step**`. The validator refuses a slip with zero items or a
  first item missing that prefix — a successor agent with no declared safe
  first move is exactly the case this section exists to prevent.
- **Source pointers** — paths/URLs the successor needs; never a fabricated
  URL.

Any section with nothing trustworthy to say gets the literal text
`Not captured in this session` — never an empty body, never a placeholder,
never an invented inference. The validator does not special-case this string;
it is a convention for the writing agent, not a distinct machine check.

## Validation summary

A slip passes `validate_slip.py` only when:

- the document opens with an H1 starting `HANDOFF: `;
- every required H2 heading in [Required section order](#required-section-order)
  is present, spelled exactly, and in the declared relative order;
- `Exact next actions` has at least one numbered item and the first is
  bold-prefixed `**First safe step**`;
- no line matches a pattern in `references/redaction-patterns.md` — run the
  slip through `scripts/handoff_redact.py` before validating, not after;
- a stated `handoff-version` is one the validator knows (`1`).

Any failure refuses — see `scripts/validate_slip.py`'s own docstring for the
exit contract. Dispatch never proceeds past a refusal; capture-only mode still
writes the artifact (a human can fix it by hand) but reports the refusal
alongside the path.
