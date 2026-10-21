"""The reader contract's errors -- a leaf, importing nothing of `codegraph`.

Both the adapter package (`adapter.book`, which rebinds these names) and the
language seam (`lang.base.LanguageError`) raise them; living here, neither has to
import the other to share them.
"""
from __future__ import annotations


class AdapterError(Exception):
    """A book row, or a reader's answer, is outside the contract."""


class AdapterRefused(AdapterError):
    """A reader could run but may not: a project's command in an untrusted repo."""
