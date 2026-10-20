#!/usr/bin/env python3
"""pack_messages.py — the message-key table reader for port/pack/check code.

An upstream message (a gate finding, a refusal reason, a report line) that
originates in Vietnamese must never be hardcoded directly in ported code —
"normalize to one place" means every such string lives in a TABLE, keyed,
with one entry per language, and code looks it up by KEY. The table itself
is data (a plain dict, or a YAML file read through `yaml_io`, per this
repo's own "human-edited config is YAML" rule) — this module is the one
reader every port/pack/check caller shares, so a message never gets looked
up two slightly different ways.

The resolved language comes from `output_config.language()` by default — the
SAME resolver every other generated-prose surface in this tree already reads
(`harness/data/output.yaml`) — so changing that one config changes every
port/pack/check message along with everything else the harness prints,
never a second, independent language switch.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import output_config


class MessageTableError(Exception):
    """A message table (or one call into it) is malformed — a missing key,
    a non-mapping entry, or an entry with no usable language at all. Never
    silently falls back to a placeholder: a caller that mistypes a key must
    see it fail, not print `None` or an empty string to a user."""


def resolve(table: Dict[str, Any], key: str, *, language: Optional[str] = None) -> str:
    """Look `key` up in `table` (`{key: {lang: text, ...}, ...}`) and return
    the entry for `language` (default: `output_config.language()`, this
    install's resolved generated-prose language).

    English is the one universal fallback when the resolved language has no
    entry of its own — every message table is expected to carry at least an
    `en` entry, exactly the way `output_config.py` treats English as its own
    base case. A table entry with NEITHER the resolved language NOR `en` is
    a `MessageTableError`, not a silent empty string.
    """
    lang = language if language is not None else output_config.language()
    entry = table.get(key)
    if entry is None:
        raise MessageTableError("no message table entry for key %r" % key)
    if not isinstance(entry, dict):
        raise MessageTableError(
            "message table entry for key %r is not a mapping (got %s)"
            % (key, type(entry).__name__))
    if lang in entry:
        return entry[lang]
    if "en" in entry:
        return entry["en"]
    raise MessageTableError(
        "message table entry for key %r has neither %r nor an `en` fallback"
        % (key, lang))
