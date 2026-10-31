"""EDGAR information time from the filing header `<ACCEPTANCE-DATETIME>` (plan 261022-1147, phase 4, R4.3).

The header value is 14 digits `YYYYMMDDHHMMSS` with no zone. It is read as America/New_York local time, whichever of EST
or EDT is in effect (17 CFR 232.13, VERIFIED https://www.ecfr.gov/current/title-17/chapter-II/part-232/subpart-A/section-232.13),
never as a fixed -05:00 offset, and stored as tz-aware UTC. The submissions-JSON `acceptanceDateTime` is for discovery only
(AGENTS.md Verified Facts).

The parser raises instead of guessing. The checks run in a fixed order, so each bad value raises exactly one error class:
format, calendar, PEP 495 (ambiguous or nonexistent wall time), EDGAR hours and weekday, then the conversion to UTC.
Stdlib plus the tzdata package only; Windows has no system IANA database (AGENTS.md Decisions, 2026-10-27).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

NEW_YORK = "America/New_York"
# EDGAR accepts filings 06:00-22:00 ET, Monday to Friday (VERIFIED https://sec.gov/node/956846); federal holidays are a
# Phase 1 calendar check.
EDGAR_HOURS: tuple[time, time] = (time(6, 0), time(22, 0))
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

SessionBucket = Literal["pre_open", "regular", "after_close", "non_session_day"]

_RAW_FORM = re.compile(r"[0-9]{14}")  # ASCII digits only: `\d` would also accept other Unicode digits
_RAW_FORMAT = "%Y%m%d%H%M%S"
# The raw tag (the .hdr.sgml file and the HTML comment of -index-headers.html) and its escaped copy inside <PRE>.
_ACCEPTANCE_TAG = re.compile(r"(?:<|&lt;)ACCEPTANCE-DATETIME(?:>|&gt;)([^\r\n]*)")


class EdgarAcceptanceError(ValueError):
    """An EDGAR acceptance time that cannot be read without guessing. The message names the raw value and the rule."""


class EdgarAcceptanceMissing(EdgarAcceptanceError):
    """The header has no `<ACCEPTANCE-DATETIME>` tag."""


class EdgarAcceptanceConflict(EdgarAcceptanceError):
    """The header carries `<ACCEPTANCE-DATETIME>` more than once with different values."""


class EdgarAcceptanceFormatError(EdgarAcceptanceError):
    """The value is not 14 ASCII digits, or not a calendar date and time."""


class EdgarAcceptanceAmbiguous(EdgarAcceptanceError):
    """The New York wall time happens twice (the autumn DST fold)."""


class EdgarAcceptanceNonexistent(EdgarAcceptanceError):
    """The New York wall time never happens (the spring DST gap)."""


class EdgarAcceptanceOutsideHours(EdgarAcceptanceError):
    """The time falls outside the EDGAR filing hours or on a weekend."""


def _new_york() -> ZoneInfo:
    """America/New_York, created on first use (ZoneInfo caches the instance); a missing database names the fix."""
    try:
        return ZoneInfo(NEW_YORK)
    except ZoneInfoNotFoundError as err:
        raise ZoneInfoNotFoundError(
            f"time zone {NEW_YORK} is not available: install tzdata (a core dependency; Windows has no system IANA database)"
        ) from err


def extract_acceptance_raw(header_text: str) -> str:
    """The `<ACCEPTANCE-DATETIME>` value of an EDGAR header, from the raw tag and from its `&lt;...&gt;` escaped copy.

    The value is the rest of the line, stripped. Every occurrence must carry the same value: -index-headers.html has the
    header twice (an HTML comment and a <PRE> block). The value itself is not validated here; see
    `parse_acceptance_datetime`.
    """
    values = [match.group(1).strip() for match in _ACCEPTANCE_TAG.finditer(header_text)]
    if not values:
        raise EdgarAcceptanceMissing(
            f"no <ACCEPTANCE-DATETIME> tag in the header ({len(header_text)} characters): the rule is one tag per header"
        )
    distinct = sorted(set(values))
    if len(distinct) > 1:
        raise EdgarAcceptanceConflict(
            f"<ACCEPTANCE-DATETIME> has {len(distinct)} different values {distinct!r} in {len(values)} occurrences: "
            "the rule is that every copy agrees"
        )
    return distinct[0]


def parse_acceptance_datetime(
    raw: str, *, hours: tuple[time, time] = EDGAR_HOURS, weekdays_only: bool = True
) -> datetime:
    """The tz-aware UTC instant of a 14-digit `<ACCEPTANCE-DATETIME>` value read as America/New_York wall time.

    `hours` is the closed interval [start, end] of accepted New York times (default 06:00-22:00, both ends included);
    `weekdays_only` refuses Saturday and Sunday. Raises, in this order: `EdgarAcceptanceFormatError` (not 14 ASCII digits,
    or not a calendar date), `EdgarAcceptanceAmbiguous` / `EdgarAcceptanceNonexistent` (PEP 495 fold or gap),
    `EdgarAcceptanceOutsideHours`. The value is never stripped or repaired.
    """
    if not isinstance(raw, str) or not _RAW_FORM.fullmatch(raw):
        raise EdgarAcceptanceFormatError(f"{raw!r} is not an EDGAR acceptance time: the rule is exactly 14 ASCII digits")
    try:
        wall = datetime.strptime(raw, _RAW_FORMAT)  # naive New York wall time, given its zone below
    except ValueError as err:
        raise EdgarAcceptanceFormatError(
            f"{raw!r} is not a calendar date and time: the rule is a valid YYYYMMDDHHMMSS ({err})"
        ) from err

    local = wall.replace(tzinfo=_new_york())
    # PEP 495 strict check (VERIFIED https://peps.python.org/pep-0495/): zoneinfo never raises, it picks an offset.
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        back = local.astimezone(timezone.utc).astimezone(local.tzinfo).replace(tzinfo=None)
        if back != wall:
            raise EdgarAcceptanceNonexistent(
                f"{raw!r} never happens in {NEW_YORK} (DST gap): the rule is a wall time that exists"
            )
        raise EdgarAcceptanceAmbiguous(
            f"{raw!r} happens twice in {NEW_YORK} (DST fold): the rule is a wall time with one UTC instant"
        )

    start, end = hours
    if not start <= wall.time() <= end:
        raise EdgarAcceptanceOutsideHours(
            f"{raw!r} is outside the EDGAR hours: the rule is {start.isoformat()}-{end.isoformat()} {NEW_YORK}"
        )
    if weekdays_only and wall.weekday() >= 5:
        raise EdgarAcceptanceOutsideHours(f"{raw!r} falls on a {wall:%A}: the rule is Monday to Friday")
    return local.astimezone(timezone.utc)


def us_session_bucket(info_time_utc: datetime) -> SessionBucket:
    """Where a tz-aware instant falls in the New York trading day: pre_open, regular, after_close or non_session_day.

    Before 09:30 is `pre_open`, 09:30 to before 16:00 is `regular`, 16:00:00 and later is `after_close` (day 0 is the first
    session whose close comes after `information_time`, roadmap §2.1), and Saturday or Sunday in New York is
    `non_session_day`. Federal holidays and early closes are not checked. This is US session logic, not EDGAR logic:
    it moves to `calendar/` in Phase 1.
    """
    if info_time_utc.tzinfo is None or info_time_utc.utcoffset() is None:
        raise ValueError(f"{info_time_utc!r} is naive: the rule is a tz-aware instant")
    local = info_time_utc.astimezone(_new_york())
    if local.weekday() >= 5:
        return "non_session_day"
    clock = local.time()
    if clock < REGULAR_OPEN:
        return "pre_open"
    if clock < REGULAR_CLOSE:
        return "regular"
    return "after_close"
