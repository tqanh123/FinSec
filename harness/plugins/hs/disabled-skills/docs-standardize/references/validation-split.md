# The Script-check vs LLM-check boundary

Two kinds of checking. **Only Script-check blocks.** LLM-check is advisory, silent by
default.

## Script-check (objective — counting / graph-walking)

- Executed by `check_docs.py` → `graph.validate` → a JSON artifact. The `docs_gate.py`
  gate reads the numbers → exit 0/2.
- Judges the **structural/objective**: frontmatter shape, id grammar/uniqueness,
  parent/provenance resolution, the parts/links/config/safety/foundation graph, the
  capability-driven required-set, the README-DRY guard.
- Properties: **deterministic** (same input → same output), no model needed, no content
  inference.
- `error` → BLOCKED (exit 2). `warn`/`info` → not blocking.
- Full catalog: `references/graph-invariants.md`.

## LLM-check (advisory — judging prose)

- NOT part of the analyzer/gate. It is an optional pass by a human/agent reading prose,
  **only when explicitly requested**.
- Narrow scope, e.g.: vagueness (`> TBD` left over where the content should already be
  written), contradiction (two docs saying opposite things about the same part),
  duplication (verbatim-copied passages between docs — a soft complement to
  `readme-copies-boundary`).
- **Hard rules**:
  1. **Do not flag by default.** Silence is the correct state; speak only with concrete
     evidence.
  2. **Always anchor to numbers in the artifact.** Every remark references a real
     finding/count (`harness/state/docs-check.json`), no generalities.
  3. **Never judge "bad design" / "poor content".** The skill does not debate quality.
     At most: point at the *location* that needs a human look (e.g. "docs A and B
     describe part X differently").
  4. **Do not self-fix.** An observation that needs a human decision → record in
     `FIX.md`, let the human decide.
- Advisory does **not** change the gate verdict. A repo that PASSes the gate still
  PASSes even if LLM-check has notes.

## Division of labor

| | Script-check | LLM-check |
|---|---|---|
| Run by | `check_docs.py` / `docs_gate.py` | human/agent, on demand |
| Object | structure (frontmatter/graph/required-set) | prose |
| Deterministic? | yes | no |
| Blocks CI? | yes (`error`→exit 2) | NO |
| Default | always runs | do not flag |
| Judges quality? | NO | NO |
| Output | JSON artifact | artifact-anchored notes / `FIX.md` |

## Procedure when you want LLM-check

1. Ensure a fresh artifact (`check_docs.py --quiet`).
2. Read the prose; raise only observations **with evidence** + anchored to the relevant
   findings/numbers.
3. No "good/bad" verdicts; only "location X needs a human look for objective reason Y".
4. Observations → `FIX.md` (each item: location + reason + left-for-human). No
   self-fixing, no blocking.
