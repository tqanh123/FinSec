# Redaction

`scripts/handoff_redact.py` runs over the full artifact body and over every
appended `--include-diff` / `--include-status` block, before either is
written to disk. It never scans and warns; it substitutes.

## Source of the pattern table

The table is `harness/scripts/secret_patterns.py`, the one table the ship scanner
(`secret_scan_before_ship.py`) and the secrets store read too; the redactor imports
it and adds only the placeholder and local-host carve-outs.
`"${HARNESS_BIN_ROOT:-.}"/harness/tests/test_handoff_redact.py::test_redact_patterns_match_ship_gate_source`
still compares the redactor's families with the ship gate's, family for family.

## Categories

| Family | Marker |
|---|---|
| `aws-access-key` | `[redacted:aws-access-key]` |
| `aws-secret-key` | `[redacted:aws-secret-key]` |
| `pem-private-key` | `[redacted:pem-private-key]` |
| `pgp-private-key` | `[redacted:pgp-private-key]` |
| `anthropic-key` | `[redacted:anthropic-key]` |
| `openai-key` | `[redacted:openai-key]` |
| `github-token` / `github-fine-pat` | `[redacted:github-token]` / `[redacted:github-fine-pat]` |
| `gitlab-pat` | `[redacted:gitlab-pat]` |
| `stripe-key` | `[redacted:stripe-key]` |
| `slack-token` | `[redacted:slack-token]` |
| `google-api-key` | `[redacted:google-api-key]` |
| `npm-token` | `[redacted:npm-token]` |
| `sendgrid-key` | `[redacted:sendgrid-key]` |
| `twilio-key` | `[redacted:twilio-key]` |
| `hf-token` | `[redacted:hf-token]` |
| `do-token` | `[redacted:do-token]` |
| `azure-account-key` | `[redacted:azure-account-key]` |
| `db-uri-cred` | `[redacted:db-uri-cred]` |
| `jwt` | `[redacted:jwt]` |
| `bearer` | `[redacted:bearer]` |
| `basic-auth-url` | `[redacted:basic-auth-url]` |
| `generic-secret` / `generic-secret-unquoted` | `[redacted:generic-secret]` / `[redacted:generic-secret-unquoted]` |

Add a family here and to the table only when it also lands in the ship-gate
source — this file documents the shared table, it does not extend it alone.

## Rules

1. Redact before write — never after, never as a follow-up pass. A capture
   step that appends `--include-diff`/`--include-status` output runs each
   block through `handoff_redact.redact_text` before it is folded into the
   artifact.
2. A marker is `[redacted:<family>]` and nothing else — no length, no hash,
   no fragment of the original value survives.
3. Overlapping hits resolve to the more specific family (`jwt` before
   `bearer`/`generic-secret`, `pem-private-key` before `generic-secret`) —
   table order encodes this; do not reorder it casually.
4. A hit inside the task-focus string itself is not silently redacted — refuse
   the invocation and ask the user to rephrase (mirrors the ship-gate's own
   "do not sanitize the trigger" posture).
5. A documentation placeholder (`your-api-key-here`, `<TOKEN>`, `...`) and a
   local-dev database URI (`postgres://postgres:postgres@localhost/dev`) are
   carved out of the label-keyed families — the same carve-outs
   `secret_scan_before_ship.py` uses, copied for the same reason: a real
   secret elsewhere still redacts, a lone placeholder does not burn a marker.

## Verification fixture (Scenario 4 in `capture-scenarios.md`)

Input carrying one `AWS_SECRET_ACCESS_KEY=…` line and one
`Authorization: Bearer eyJ…` line (both fake) must come out with
`[redacted:aws-secret-key]` and `[redacted:jwt]` (the JWT shape is more
specific than the bare bearer label and wins), no substring of either raw
value anywhere in the output, and a `Work performed` line reporting
`2 redactions applied.`
