#!/usr/bin/env python3
"""diagram_text.py — deterministic text metrics for hs:diagram.

No font is loaded at compile time (the compiler runs headless, often on a
machine with no font stack at all), so width is ESTIMATED from character
class rather than measured with a real glyph table. `unicodedata` is stdlib,
already installed everywhere Python runs, and gives a principled signal
(`east_asian_width`) instead of a hand-picked list of "wide" characters —
the same input always yields the same width, which is what keeps two runs of
the layout byte-identical.
"""
from __future__ import annotations

import unicodedata

_NARROW = set("iljI.,:;!|'`()[]{} \t")
_WIDE = set("mwMW@%")


def char_width_units(ch: str) -> float:
    """Advance width of one character, in units of the font size."""
    if ch in _NARROW:
        return 0.30
    if ch in _WIDE:
        return 0.86
    width_class = unicodedata.east_asian_width(ch)
    if width_class in ("W", "F"):  # CJK and other full-width scripts
        return 1.0
    if unicodedata.combining(ch):
        return 0.0
    if ch.isdigit():
        return 0.56
    if ch.isupper():
        return 0.66
    return 0.54


def text_width(text: str, font_size: float = 12.0, weight: int = 400) -> float:
    """Estimated pixel width of `text` set at `font_size`."""
    units = sum(char_width_units(ch) for ch in text or "")
    scale = 1.06 if weight >= 600 else 1.0
    return units * font_size * scale


def truncate_to_width(text: str, max_width: float, font_size: float = 12.0,
                       weight: int = 400) -> str:
    """Shorten `text` with an ellipsis so it fits `max_width` at `font_size`."""
    text = text or ""
    if text_width(text, font_size, weight) <= max_width:
        return text
    ellipsis = "…"
    out = text
    while len(out) > 1 and text_width(out + ellipsis, font_size, weight) > max_width:
        out = out[:-1]
    return out.rstrip() + ellipsis


def fmt(value: float) -> str:
    """Round to one decimal so serialized coordinates stay compact and stable."""
    rounded = round(value * 10) / 10
    if rounded == 0:
        rounded = 0.0  # normalise -0.0
    if rounded == int(rounded):
        return str(int(rounded))
    return str(rounded)
