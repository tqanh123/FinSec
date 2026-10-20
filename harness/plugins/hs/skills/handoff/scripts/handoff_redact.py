#!/usr/bin/env python3
"""handoff_redact.py — replace likely secrets with a stable marker before an
`hs:handoff` artifact is written.

The pattern table is `harness/scripts/secret_patterns.py`, the one table the
ship scanner and the secrets store also read; this module adds only the
carve-outs below and the substitution.
`harness/tests/test_handoff_redact.py::test_redact_patterns_match_ship_gate_source`
still pins the table against the ship gate, family for family.

Unlike the two existing tiers (which only SCAN and warn), this one REDACTS:
`redact_text` returns the text with every hit replaced by
`[redacted:<family>]` — never the raw value, its length, or a hash of it —
plus a per-family hit count for the artifact's "N redactions applied" line.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "scripts"))
import secret_patterns  # noqa: E402

_PATTERNS = secret_patterns.PATTERNS

# Same carve-outs as the ship-gate source: a label-keyed match on a documentation
# placeholder, or a db-uri-cred pointed at a local-dev host, is not a leak worth
# burning a marker on.
_LABEL_FAMILIES = ("generic-secret", "bearer", "basic-auth-url")
_PLACEHOLDER_RE = re.compile(
    r"(?i)\b(?:your|example|sample|placeholder|changeme|dummy|redacted|todo)"
    r"|<[A-Za-z_]|\.\.\.|here['\"]?\s*$")
_DB_LOCAL_HOST_RE = re.compile(
    r"(?i)^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?"
    r"|[a-z0-9_-]+"
    r"|[a-z0-9_-]+\.(?:local|localhost))$")


def _placeholder_probe(m):
    return m.groupdict().get("cred") or m.group(0)


def _is_placeholder_hit(name, m):
    if name.startswith(_LABEL_FAMILIES) and _PLACEHOLDER_RE.search(_placeholder_probe(m)):
        return True
    host = m.groupdict().get("host")
    if host and _DB_LOCAL_HOST_RE.match(host.strip()):
        return True
    return False


def scan_text(text: str) -> list:
    """Family names that fire on `text`, in table order, deduped."""
    text = text or ""
    hits = []
    for name, rx in _PATTERNS:
        for m in rx.finditer(text):
            if _is_placeholder_hit(name, m):
                continue
            if name not in hits:
                hits.append(name)
            break
    return hits


def redact_text(text: str):
    """`(redacted_text, counts)` — every real hit replaced by
    `[redacted:<family>]`; `counts` maps family -> number of hits replaced.
    A placeholder/local-host hit is left untouched and not counted, exactly
    as `scan_text` would not report it."""
    text = text or ""
    counts: dict = {}

    def _sub(name, rx, s):
        def repl(m):
            if _is_placeholder_hit(name, m):
                return m.group(0)
            counts[name] = counts.get(name, 0) + 1
            return "[redacted:%s]" % name
        return rx.sub(repl, s)

    for name, rx in _PATTERNS:
        text = _sub(name, rx, text)
    return text, counts
