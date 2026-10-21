# Coherence lint — warn on a config that fights itself (per-step + final)

Setup's job: not only write values, but catch a config internally incoherent or mismatched to the user. Run this lint TWICE: a quick per-step check right after each group is written, and a holistic pass at the very end (Full mode) consolidating every mismatch. Warn and ASK — never silently "correct" a deliberate choice. Frame each as: *"you chose X together with Y
— that pairing usually means Z; intended?"*. A confirmed mismatch is left as-is (user decisions hold); the lint only surfaces, it does not overrule.

Apply these checks (extend as new knobs land):

**Preset ↔ strictness:**
- guard preset `solo`/`lenient` while the review default is `thorough`/`ship-grade` and every feature carries a full DoD (integration + coverage) → **too strict for a one-person repo**: daily friction with no second pair of eyes to justify it. Suggest a light-default review with deep on demand.
- guard preset `strict`/`balanced` while `protected-branches` is empty and the DoD is unit-only → **too loose for a shared repo**: nothing independent gates a merge onto the main branch. Suggest a coverage floor and listing the branch in `protected-branches`.
- a preset changed in `guard-policy.yaml` while `stage-policy.yaml` keeps the values the old preset assumed → **split-brain**: the human-friction layer and the gate layer disagree.

**Output register ↔ reader:**
- `language` set to a language the repo's own prose is not written in → reports and code
  comments will disagree with the tree around them; confirm that split is intended.

The final pass prints ONE consolidated list (file · chosen value · why it looks off · the fitting alternative) and asks which, if any, to revisit. If nothing is off, say so in one line and move on.
