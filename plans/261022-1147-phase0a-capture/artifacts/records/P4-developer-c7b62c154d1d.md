# P4 implementation notes (2026-10-28)

Posture: subagent (19 declared files). hs:developer built the network-free slice (report: scratchpad p4-dev-report.md):
`connectors/edgar_acceptance.py` + re-exports, `tests/test_edgar_time.py` synthetic part (RED on NotImplementedError, then
GREEN), `scripts/spike/record_edgar_headers.py` + `tests/test_record_edgar_headers.py` (29 tests, written after the script;
8 deliberate breaks each failed a test), `.gitattributes` R4.5 lines. Main reviewed the parser and the recorder's identity
handling (never printed, logged or stored).

Main, after OB6 (`.env` with EDGAR_IDENTITY; `--check-env` printed ok):
- Recording run 1 exited 3: the Form 4 is not under the agent CIK folder 1140361 (HTTP 404). EDGAR full-text search
  (efts.sec.gov/LATEST/search-index, one request) listed CIKs 0002100523 (owner) and 0000320193 (issuer). Run 2 with
  `--cik 320193` wrote 8 headers + manifest. Every .hdr.sgml carries the tag. Form 4 redactions: hdr 5, html 10.
- Golden tests added (34 tests in the file). They were written after the parser, so they are green on first run; their
  bite was shown by mutation: a fixed -05:00 offset fails 7 (every EDT case), a one-byte fixture change fails the sha
  test, an unredacted owner name fails the PII test.
- Delta docs: AGENTS.md (D8 dated 2026-10-28 from plan.md D8; EDGAR Verified Facts settled with the four values; package
  layout; test counts), system-architecture (as-built row, SEC EDGAR line, EDGAR_IDENTITY reader), code-standards
  (EDGAR timestamps, golden test, recorded-fixture convention), roadmap (0a EDGAR done, §6.4 item 2 done, §5.2 row),
  spike report §4.
- "capture window started" line: not written; the window has not opened (the laptop is not deployed yet). The first
  phase commit after the window opens adds it (P5, P6 or P7).

Deviations: `tests/test_record_edgar_headers.py` (outside the table); FORMER-CONFORMED-NAME also redacted (stricter than
D8; it only appears in the issuer block in this Form 4, so nothing was cut there); manifest carries extra fields;
exit code 3 for "rerun with --cik"; the 10-Q JSON date in the trap test is the filing date (the roadmap gives the time only).

Gate: .venv 535 passed; global 500 passed, 1 skipped (fastapi); smoke 0; both standards checks conform;
`git check-attr text` on the fixtures = unset; git grep for the identity value and its email = 0 hits.
