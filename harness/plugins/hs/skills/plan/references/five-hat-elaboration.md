# Five-hat elaboration — constructive design generation (on-demand)

Load during the ELABORATE step of `hs:plan`, after intake and BEFORE the red-team
gate. Five internal "hats" each look at the same problem through a different lens and
each PRODUCES design material; the planner then synthesises that material into the
plan. This is the *constructive* counterpart to the adversarial passes — five-hat
BUILDS a design; red-team and premortem ATTACK one that already exists.

## When to run

- After intake/discovery has fixed the problem and constraints.
- Before there is anything to attack — five-hat produces the artifact the red-team
  gate then stresses.
- Default is **five sequential lenses in one planner thread**. Fan-out to `@researcher`
  is optional, only when the problem is large enough that parallel lenses pay off — do
  NOT spawn a persona sub-agent per hat (YAGNI; a lens is a viewpoint, not an agent).

## The five hats

| Hat | Lens | Leading question | Produces → lands in plan |
|---|---|---|---|
| Product Manager | jobs-to-be-done | Who hires this and for what outcome? | JTBD statements, personas, pain points → §Discovery |
| Strategist | value & guardrails | What is the one metric that must move, and what must not regress? | north-star + success + guardrail metrics → §Strategy |
| UX Designer | user flow | What does the user actually do, step by step, and where do they get stuck? | draft flows, state matrix, onboarding → §Strategy/§UX |
| System Architect | structure & contracts | What ships as one unit, and which contracts change? | C4 context/container sketch, contract deltas, data model → §Architecture |
| Delivery Manager | decomposition | How does this split into ordered, verifiable work? | task DAG, risks, DoD, spec classification → phase-DAG |

The planner orchestrates: pose each hat a focused question, take its structured output,
and fold it into the matching plan section. The hats are invisible to the user — the
user talks to the planner, not to a hat.

## Spec classification (Delivery hat)

Every acceptance criterion is classified before ELABORATE closes, into exactly one of
three forms. Ask in order:

1. Can an automated test assert a concrete input→output pair? → `test`, the default.
2. Is it a property that must hold at ALL times rather than for one pair (a schema shape,
   a count that never decreases, "never both X and Y")? → `invariant`. An invariant is
   never satisfied by sampling a few instances — it holds structurally or it is not an
   invariant. Universal ("every record has an id") and behavioural ("balance never goes
   negative after a withdrawal") are different shapes; split them rather than conflate.
3. Does it need a human judgment no automated check can render (visual polish, UX feel, a
   live external system with no double)? → `manual`. A `manual` AC MUST bind the literal
   keyword `manual_test_anchor.py` — without that anchor it is invisible to the DoD gate
   and silently fails later.
4. None of the three fits → the AC is not concrete enough yet; go back to 1.

The branch is named `invariant`, deliberately NOT `rule`: "rule" collides with the
rule-file vocabulary (`harness/rules/*.md`). Do not "restore" the older name when porting
prose from elsewhere. An AC that restates the implementation ("the function returns what
the function computes") is untestable in every branch — push back and ask what observable
behaviour the user actually cares about. A universal-and-behavioural AC splits into two
items — one invariant for structural enforcement, one test for the behaviour. Route each classified
item into the phase's acceptance block; that is what the Delivery hat hands to
[`phase-decomposition.md`](phase-decomposition.md).

## Five-hat ≠ red-team ≠ `hs:predict`

- **Five-hat builds.** It generates design material before any critique target exists.
- **Red-team ([`red-team-gate.md`](red-team-gate.md)) attacks.** It runs AFTER five-hat,
  over the design five-hat produced, hunting where it breaks or games its own metrics.
- **`hs:predict`** is a 5-expert adversarial debate over a proposed change — again a
  critique of an existing design, not construction.

Running a critique pass as if it were construction (or vice-versa) wastes both: you
cannot attack a design that does not exist yet, and you should not re-derive the design
inside the red-team.

## Anti-patterns

- **Empty hats** — a hat that "considered X" without producing concrete material is
  noise; each hat must land an artifact in a named plan section.
- **Spawning five persona agents for a small task** — the default is sequential lenses
  in one thread; fan-out is an optional optimisation for large problems only. Creating a
  new skill/agent per hat is out of scope (it drags in the full skill-wiring chain).
- **Re-coining the test/invariant/manual decision tree somewhere else** — this file is
  its only home now; two copies drift.
- **Rewriting the red-team angle** — five-hat is constructive; keep the critique in the
  red-team gate.

## Handoff

Five-hat material → [`phase-decomposition.md`](phase-decomposition.md) (turn the Delivery
hat's DAG into phases) → [`red-team-gate.md`](red-team-gate.md) (attack the result).

Grounded in an external planner methodology's Five Internal Hats table and its
spec-classification decision tree. That source assigns each hat to its own sub-agent
tier; the harness has no such agents, so a hat here is a sequential viewpoint or an
optional `@researcher` fan-out.
