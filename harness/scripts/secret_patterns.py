"""secret_patterns — the ONE table of high-confidence secret shapes.

Precision-first: each entry requires a distinctive prefix or a
key=quoted-value assignment, so prose does not false-match. This module is
the single source the ship scanner, the secrets store and the handoff
redactor read; a second copy of the table is exactly the drift this module
exists to prevent. `find` names the matched spans; `redact` replaces each
with `[redacted:<name>]` — value gone, reason kept.
"""
from __future__ import annotations

import re

PATTERNS = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws-secret-key", re.compile(
        r"(?i)aws_?secret_?access_?key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}\b")),
    ("pem-private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("pgp-private-key", re.compile(r"-----BEGIN[ ]PGP[ ]PRIVATE[ ]KEY[ ]BLOCK-----")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github-fine-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("gitlab-pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{20}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    ("twilio-key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("do-token", re.compile(r"\bdop_v1_[a-f0-9]{64}\b")),
    ("azure-account-key", re.compile(r"AccountKey=[A-Za-z0-9/+]{40,}={0,2}")),
    ("db-uri-cred", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@]+:[^\s:/@]+"
        r"@(?P<host>[^\s:/?#]+)")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # A 16-char floor on the value, not `\S+`: the label alone ("Authorization: Bearer",
    # "Bearer <token>") is documentation, and matching it fires on every API reference in
    # the tree without ever pointing at a credential.
    ("bearer", re.compile(r"(?i)\bBearer\s+(?P<cred>[A-Za-z0-9._~+/=-]{16,})")),
    # http/https only — the db schemes are owned by db-uri-cred above, and a generic
    # `scheme://` here would double-report every DSN under two names.
    ("basic-auth-url", re.compile(
        r"(?i)\bhttps?://[^\s:/@]+:(?P<cred>[^\s:/@]{4,})@(?P<host>[^\s:/?#]+)")),
    ("generic-secret", re.compile(
        r"""(?i)(?:api[_-]?key|apikey|api[_-]?secret|secret|token|credential|password|passwd|pwd)\s*[:=]\s*['"][A-Za-z0-9/+=_-]{16,}['"]""")),
    ("generic-secret-unquoted", re.compile(
        r"""(?i)(?:api[_-]?key|apikey|api[_-]?secret|secret|token|credential|password|passwd|pwd)\s*[:=]\s*['"]?(?=[A-Za-z0-9/+=_-]*[0-9])[A-Za-z0-9/+=_-]{16,}""")),
]


def find(text: str) -> list:
    """Every named match in `text`, in table order."""
    out = []
    for name, rx in PATTERNS:
        m = rx.search(text)
        if m:
            out.append((name, m))
    return out


def redact(text: str) -> str:
    """`text` with each matched span replaced by `[redacted:<name>]`."""
    for name, rx in PATTERNS:
        text = rx.sub("[redacted:%s]" % name, text)
    return text
