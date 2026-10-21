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
    # The block whole, not the header alone: `redact` on a key leaves no body. With no
    # END in reach (a cut, a bound), the base64 lines after the header go with it.
    ("pem-private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"(?:[\s\S]{0,16384}?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"|(?:\r?\n[A-Za-z0-9+/=]{16,})*)")),
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
    # Not right after a base64url char: `\b` lets `-eyJ` start a match, and a run of them
    # rescanned the rest of the run from each, quadratic on untrusted output.
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
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
    # The digit lookahead is bounded: unbounded, it rescanned the whole run from every
    # label, and `redact` on untrusted output went quadratic.
    ("generic-secret-unquoted", re.compile(
        r"""(?i)(?:api[_-]?key|apikey|api[_-]?secret|secret|token|credential|password|passwd|pwd)\s*[:=]\s*['"]?(?=[A-Za-z0-9/+=_-]{0,255}[0-9])[A-Za-z0-9/+=_-]{16,}""")),
]


def find(text: str) -> list:
    """Every named match in `text`, in table order."""
    out = []
    for name, rx in PATTERNS:
        m = rx.search(text)
        if m:
            out.append((name, m))
    return out


def spans(text: str) -> list:
    """What `redact` replaces, as sorted (start, end, name) in `text`'s own
    coordinates — for a caller that cuts the text and must know where each
    secret sat. `redact` substitutes pattern by pattern, and one replacement can
    expose or swallow the next pattern's label, so this replays that same order
    with every char of the working text mapped back to the raw span it stands
    for. A match can end inside an earlier marker (a URL host taking `[redacted`),
    so spans that overlap are merged under the first name: no raw text outside
    them is text `redact` removed."""
    cur = text
    src: list = list(range(len(text)))  # raw index, or the (start, end, name) a marker char stands for
    for name, rx in PATTERNS:
        found = [m for m in rx.finditer(cur) if m.end() > m.start()]
        if not found:
            continue
        marker = "[redacted:%s]" % name
        out: list = []
        nsrc: list = []
        at = 0
        for m in found:
            out.append(cur[at:m.start()])
            nsrc.extend(src[at:m.start()])
            covered = src[m.start():m.end()]
            tag = (min(x if isinstance(x, int) else x[0] for x in covered),
                   max(x + 1 if isinstance(x, int) else x[1] for x in covered), name)
            out.append(marker)
            nsrc.extend([tag] * len(marker))
            at = m.end()
        out.append(cur[at:])
        nsrc.extend(src[at:])
        cur, src = "".join(out), nsrc
    result: list = []
    for x in src:
        if isinstance(x, int) or (result and result[-1] is x):
            continue
        if result and x[0] < result[-1][1]:
            if x[1] > result[-1][1]:
                result[-1] = (result[-1][0], x[1], result[-1][2])
        else:
            result.append(x)
    return result


def redact(text: str) -> str:
    """`text` with each matched span replaced by `[redacted:<name>]`."""
    for name, rx in PATTERNS:
        text = rx.sub("[redacted:%s]" % name, text)
    return text
