# Hand-off anchors — editing another plan by line

When this plan tells another plan's reader to edit its files, write each edit so `hs-run plan next` can check it:

```
**EDIT** in `plans/<other-plan>/phases/phase-9-run-step.md`:
- `:80` holds «first words of line 80 … its last words» ⇒ ADD after it: "..."
- `:329-333` holds «line 329's text» … «line 333's text» ⇒ REPLACE with: "..."
```

- The target is the last backticked `plans/...` path on a non-bullet line since the last heading.
- An anchor is a `` - `:N` `` or `` - `:N-M` `` bullet; its quote is every «…» before the first `⇒`. `…` inside a quote elides; quotes may wrap onto indented lines.
- A bullet that does not open with `` `:N` `` (`- after `:250` add ...`) names a place, not content, and is not an anchor. A `` - `:N` `` bullet under a target line but with no «quote» (outside an `applied:` block), or one quoting «…» with no target line recognised above it, IS one and cannot be checked: it is warned on, not dropped. A `⇒` inside a «quote» is part of the quote.
- Once a block is pasted, add `applied: <date or note>` on its own line under the target line: that target's anchors stop being checked (their quotes are gone by design), while the target plan's approval still is. The line sits outside the approval hash, so marking a block in an approved plan does not re-open its approval.

On every `plan next` of the SENDING plan:

- an anchor whose quoted text is no longer on lines N..M is reported with where it is now (`now at :M`) or `not found` — pasting another block above it shifted the lines;
- every callable a block's quotes write as a call or definition (`` `name(` ``, `` `name`( ``) is looked up across the target plan's `plan.md` and `phases/*.md`; lines naming it that no anchor covers are listed per target plan — a changed signature reaches them too.
  A name on more than 15 lines is vocabulary, skipped and counted (`symbols_too_common`); `unanchored_mentions` counts the lines listed;
- every target plan whose own `plan next` answers `approval_drifted`, `approval_invalid` or `state_conflict` is named: pasting into an approved plan drifts its approval, and it needs re-review before cook.

The envelope always carries `handoff_anchors` — `checked` (`matched` / `moved` / `not_found` / `no_target_file`), `applied`, `unchecked`, `blocks`, `targets` — zeros included, so an empty warning list reads as "N checked, all held" or "nothing to check", never both.

All of it is advisory; it never moves the sending plan's step.
