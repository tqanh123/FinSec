# Workflow review artifacts

The artifacts `hs:cook`/`hs:fix` gates actually read, and the one computed field they
carry. Scores are advisory; approval comes from the structured record plus the validator.
Write each through its writer script — never hand-write the YAML/JSON (the writer stamps
run sequence and writes atomically; a hand write skips both).

## `verification.json` — written by `write_verification.py`

Per-phase record (see `hs-run cook verify`, the command the envelope prints). One field is
computed for you and must never be hand-authored: `blastRadius`.

```json
{
  "commands": [
    {"command": "pytest harness/tests/ -q", "status": "pass", "exitCode": 0,
     "timestamp": "2026-05-19T00:00:00.000Z", "summary": "All hook tests passed."}
  ],
  "blastRadius": {
    "reverseDeps": {"harness/hooks/gate_stage.py": ["harness/scripts/hs_run_fix.py"]},
    "testsTouched": ["harness/tests/test_gate_stage.py"],
    "basis": "code.importers + code.tests_reached (hs_run_facts.engine), default subjects",
    "snapshot": "2026-05-19T00:00:00.000Z"
  }
}
```

`write_verification.py` fills `blastRadius` itself for a code change by resolving
`code.importers` + `code.tests_reached` through `hs_run_facts.engine` — never hand-author
it. When the facts cannot answer, the field carries `{"unavailable": true, "reason": "..."}`
or `{"stale": true, "reason": "..."}` instead of the shape above; it is never silently
absent from a code change and never a fabricated empty list. On a code change the
hard-stage gate blocks a `blastRadius` that is present but empty or half-filled, or a
marker with no reason; an absent key passes with a trace, since every verification
written before the field has none.

## `review-decision` — written by `write_review_decision.py`

The verdict artifact the hard-stage gate reads (`artifact_check`, presence + verdict).
Canonical file `plans/<active>/artifacts/review-decision.yaml` (`.json` legacy-accepted),
schema `"${HARNESS_BIN_ROOT:-.}"/harness/schemas/artifact-review-decision.json`:

```yaml
verdict: PASS | PASS_WITH_RISK | BLOCKED
reviewer: <resolve_actor() output>
role: reviewer
rationale: <WHY — a paragraph explaining the verdict>
# optional: plan_hash, effort, rounds_run, strategy, architecture_review{checked, doc_sha, drift[]}
```

Write it ONLY via the writer script (it refuses without `--rounds-run`, stamps the
first-wins `review-decision-r<N>.json` snapshot, and writes atomically). A hand-written
record with invented keys (`decision`, `score`, `criticalCount`) is NOT read by the gate —
the gate checks `verdict`.

## Approval rules (what the gate enforces)

- A score never approves by itself.
- Any evidenced critical issue blocks.
- `PASS_WITH_RISK` may continue only on soft stages with the risk surfaced; a HARD stage
  (`ship`, `push`, `pr`, `deploy`) needs exactly `PASS`.
- High-risk `--auto` requires explicit human approval before finalize/commit/ship.
- If artifact generation fails, retry once. If it still fails, escalate to the user
  instead of bypassing.

## Redaction policy

Artifacts must not contain raw secrets, API keys, bearer tokens, cookies, authorization
headers, private keys, or dotenv lines. Command output must be summarized. If a snippet
is needed, redact first:

```json
{"summary": "Request failed with Authorization header redacted.", "redacted": true}
```
