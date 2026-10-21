# After a split: the parent, its children, and the edges between them

When: a plan's approval recorded `--size-decision split`, or a phase waits on a sibling plan's phase.

`--size-decision split` records the choice on the parent; the children carry the link.

- **Each child names its parent** with plan.md frontmatter `parent_plan: <parent dir name>`.
  The parent finds its children by reading its sibling plans, so a child written later
  needs no edit to the parent.
- **The parent holds no phase.** Move every phase file into the child that cooks it.
  `plan next` on the parent then answers `split` (exit 0) and names each child; until a
  child names it and no phase file is left, it answers `split_pending` with what is
  missing. Whatever its status, it never recommends cook: every `cook` verb refuses it
  (`cook status` reports it at exit 0), open/close/finalize never move its status, and
  neither cook nor the gate ever takes it as the active plan. Moving the phases out
  drifts the parent's approval; that does not send it back to approval.
- **A phase that needs another child's phase** says so in its own plan's `plan-graph.yaml`,
  as an edge whose `from` names the other plan: `{from: <plan dir name>/<phase>, to: <phase>}`.
  Only the plan that waits writes it; an edge whose `to` names another plan is refused.
  `plan graph`, `plan next` and cook's preflight reject an edge naming a plan or phase that
  is not there, the plan itself, or a cycle through several plans.
- **Cook holds a waiting phase** until the other plan's phase passes: an unforced
  `cook next` hands out other work first, then answers `phases_blocked` naming
  `<plan>/<phase>` and the command that moves it; `cook next --phase <id>` answers
  `phase_waiting`. A waiting phase is still owed, so the plan cannot read done.
- **A ruling that the edge is met before the other phase passed** (its in-repo half
  merged, the rest waits on something outside) is written on that edge:
  `{from: <plan>/<phase>, to: <phase>, waived: {decision: <ruling id>, reason: <why>}}`,
  both non-empty, on a cross-plan edge only. The waiting phase is handed out, and every
  `cook next` / `cook close` names the waiver while the other phase has not passed; the
  other plan still owes that phase. A waiver widens what runs, so it drifts the approval:
  re-approve the plan after writing it.
- **Material every child follows** (a mockup, probes) stays in the parent. A child names it
  in `reference_dirs:` as `../<parent dir name>/<dir>`, which hashes it into the child's
  approval: an edit there re-opens the child. Outside its own dir a plan may name only a
  sibling plan's dir this way; a link from its own dirs into a sibling is still an escape.
