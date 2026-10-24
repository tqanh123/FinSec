"""`PayloadFieldError` and the field readers that raise it, shared by the tick payload parsers in this package."""
from __future__ import annotations

from typing import Any, Mapping, Optional


class PayloadFieldError(ValueError):
    """A raw source payload lacks a required field or carries a malformed one.

    The message names the source ("alpaca", "ssi"), the symbol (None when the symbol itself is missing or
    malformed), the field and, when there is one, the bad value. A `ValueError`, not a `KeyError`: `str()` of a
    `KeyError` wraps its message in quotes. Raise it with `from err` when it replaces a `KeyError`, `ValueError`
    or `TypeError`.
    """

    def __init__(self, source: str, symbol: Optional[str], field: str, problem: str) -> None:
        super().__init__(source, symbol, field, problem)  # every argument in args, so the error pickles
        self.source = source
        self.symbol = symbol
        self.field = field
        self.problem = problem

    def __str__(self) -> str:
        return f"{self.source} payload, symbol {self.symbol!r}: field {self.field!r} {self.problem}"


def _required_value(source: str, symbol: Optional[str], field: str, msg: Mapping[str, Any], key: str) -> Any:
    try:
        return msg[key]
    except (KeyError, TypeError) as err:  # TypeError: the message is not a mapping
        raise PayloadFieldError(source, symbol, field, "is missing") from err


def _required_text(
    source: str, symbol: Optional[str], field: str, msg: Mapping[str, Any], key: Optional[str] = None
) -> str:
    """`msg[key]` (`key` defaults to `field`) as a non-empty string without surrounding whitespace.

    None or any non-string raises (never "None"), and so does " NVDA ", which would be a distinct symbol key.
    """
    value = _required_value(source, symbol, field, msg, key or field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        problem = f"must be a non-empty string without surrounding whitespace, got {value!r}"
        raise PayloadFieldError(source, symbol, field, problem)
    return value


def _required_number(
    source: str, symbol: Optional[str], field: str, msg: Mapping[str, Any], key: Optional[str] = None
) -> float:
    """`float(msg[key])` (`key` defaults to `field`); a missing key or a value `float()` rejects raises.

    OverflowError covers an integer too large for a float: JSON keeps a long integer literal as an exact int.
    """
    value = _required_value(source, symbol, field, msg, key or field)
    if isinstance(value, bool):  # float(True) == 1.0: a bool is not a number
        raise PayloadFieldError(source, symbol, field, f"must be a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise PayloadFieldError(source, symbol, field, f"must be a number, got {value!r}") from err
