"""EDGAR information time: `<ACCEPTANCE-DATETIME>` read as America/New_York, stored as tz-aware UTC (plan 261022-1147, R4.4).

The first part needs no fixture: synthetic header strings and made-up times. The golden part reads the recorded SEC
headers in tests/fixtures/edgar_headers in binary mode. No network, no clock.
Every expected value is a literal; the comment beside it shows the arithmetic (EDT = UTC-4, EST = UTC-5).
Weekdays (OBSERVED 2026-10-28 with `datetime.strftime("%A")`): 2026-01-29 Thursday, 2026-03-08 Sunday,
2026-07-30 Thursday, 2026-10-21 Wednesday, 2026-10-24 Saturday, 2026-11-01 Sunday.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from finsec_alpharadar.connectors import edgar_acceptance
from finsec_alpharadar.connectors.edgar_acceptance import (
    EdgarAcceptanceAmbiguous,
    EdgarAcceptanceConflict,
    EdgarAcceptanceError,
    EdgarAcceptanceFormatError,
    EdgarAcceptanceMissing,
    EdgarAcceptanceNonexistent,
    EdgarAcceptanceOutsideHours,
    extract_acceptance_raw,
    parse_acceptance_datetime,
    us_session_bucket,
)


def _raises_exactly(error: type[EdgarAcceptanceError], raw: str) -> EdgarAcceptanceError:
    """The fixed error order means each bad string raises exactly one class, not merely a subclass of the base."""
    with pytest.raises(EdgarAcceptanceError) as excinfo:
        parse_acceptance_datetime(raw)
    assert type(excinfo.value) is error, f"{raw!r}: expected {error.__name__}, got {type(excinfo.value).__name__}"
    assert repr(raw) in str(excinfo.value), "the message names the raw string"
    return excinfo.value


# --- synthetic negatives: format -> calendar -> PEP 495 -> hours/weekday -------------------------------------


def test_thirteen_digits_is_a_format_error() -> None:
    _raises_exactly(EdgarAcceptanceFormatError, "2026073016302")  # 13 digits, the rule is exactly 14


def test_non_digit_is_a_format_error() -> None:
    _raises_exactly(EdgarAcceptanceFormatError, "2026073016302X")  # 14 characters, one not a digit
    _raises_exactly(EdgarAcceptanceFormatError, "2026-07-30 16:30")  # an ISO-looking value is not the header form
    _raises_exactly(EdgarAcceptanceFormatError, "2026073016302٢")  # ARABIC-INDIC DIGIT TWO: only ASCII digits count
    _raises_exactly(EdgarAcceptanceFormatError, "20260730163028\n")  # the parser never strips; extraction does


def test_month_13_and_feb_30_are_format_errors() -> None:
    _raises_exactly(EdgarAcceptanceFormatError, "20261301120000")  # month 13
    _raises_exactly(EdgarAcceptanceFormatError, "20260230120000")  # 2026-02-30: February 2026 has 28 days


def test_empty_is_a_format_error() -> None:
    _raises_exactly(EdgarAcceptanceFormatError, "")


def test_2300_is_outside_hours() -> None:
    # 2026-10-21 is a Wednesday; 23:00:00 ET is after the 22:00 close of EDGAR.
    _raises_exactly(EdgarAcceptanceOutsideHours, "20261021230000")


def test_saturday_noon_is_outside_hours() -> None:
    # 2026-10-24 is a Saturday; 12:00 is inside 06:00-22:00, so only the weekday rule fires.
    _raises_exactly(EdgarAcceptanceOutsideHours, "20261024120000")


def test_2026_11_01_0130_is_ambiguous() -> None:
    # DST ends Sunday 2026-11-01 at 02:00 EDT -> 01:00 EST, so 01:30 happens twice (-04:00, then -05:00).
    # It is also a Sunday before 06:00; the fixed order makes it Ambiguous, not OutsideHours.
    _raises_exactly(EdgarAcceptanceAmbiguous, "20261101013000")


def test_2026_03_08_0230_is_nonexistent() -> None:
    # DST starts Sunday 2026-03-08 at 02:00 EST -> 03:00 EDT, so 02:30 never happens on the wall clock.
    _raises_exactly(EdgarAcceptanceNonexistent, "20260308023000")


def test_missing_tag_raises_missing() -> None:
    header = "<SEC-HEADER>0000000000-26-000001.hdr.sgml : 20261021\n<ACCESSION-NUMBER>0000000000-26-000001\n<TYPE>8-K\n"
    with pytest.raises(EdgarAcceptanceMissing):
        extract_acceptance_raw(header)
    with pytest.raises(EdgarAcceptanceMissing):
        extract_acceptance_raw("")


def test_two_different_tags_raise_conflict() -> None:
    header = "<ACCEPTANCE-DATETIME>20261021163028\n<TYPE>8-K\n<ACCEPTANCE-DATETIME>20261021163029\n"
    with pytest.raises(EdgarAcceptanceConflict) as excinfo:
        extract_acceptance_raw(header)
    assert "20261021163028" in str(excinfo.value) and "20261021163029" in str(excinfo.value)


def test_raw_and_escaped_copies_must_agree() -> None:
    # -index-headers.html carries the header twice: raw tags in an HTML comment, escaped tags in <PRE>.
    agreeing = (
        "<!--\n<ACCEPTANCE-DATETIME>20261021163028\r\n-->\n"
        "<PRE>&lt;ACCEPTANCE-DATETIME&gt;20261021163028\nACCESSION NUMBER:\t\t0000000000-26-000001\n"
    )
    assert extract_acceptance_raw(agreeing) == "20261021163028"  # the line ending is stripped, the value is not changed
    disagreeing = agreeing.replace("&gt;20261021163028", "&gt;20261021173028")
    with pytest.raises(EdgarAcceptanceConflict):
        extract_acceptance_raw(disagreeing)


# --- synthetic positives (the golden fixture tests come with the recorded headers) -------------------------


def test_synthetic_edt_and_est_values_convert_to_utc() -> None:
    edt = parse_acceptance_datetime("20260730163028")
    assert edt == datetime(2026, 7, 30, 20, 30, 28, tzinfo=timezone.utc)  # 16:30:28 EDT + 4 h
    assert edt.utcoffset() == timedelta(0)
    est = parse_acceptance_datetime("20260129163033")
    assert est == datetime(2026, 1, 29, 21, 30, 33, tzinfo=timezone.utc)  # 16:30:33 EST + 5 h, never a fixed -04:00
    assert est.utcoffset() == timedelta(0)


def test_edgar_hours_are_inclusive_at_both_ends() -> None:
    assert parse_acceptance_datetime("20261021060000") == datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)  # 06:00 EDT + 4 h
    assert parse_acceptance_datetime("20261021220000") == datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)  # 22:00 EDT + 4 h
    _raises_exactly(EdgarAcceptanceOutsideHours, "20261021055959")  # one second before 06:00
    _raises_exactly(EdgarAcceptanceOutsideHours, "20261021220001")  # one second after 22:00


def test_hours_and_weekday_rules_are_parameters() -> None:
    late = parse_acceptance_datetime("20261021230000", hours=(time(0, 0), time(23, 59, 59)))
    assert late == datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)  # 23:00 EDT + 4 h
    saturday = parse_acceptance_datetime("20261024120000", weekdays_only=False)
    assert saturday == datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)  # 12:00 EDT + 4 h


def test_missing_zone_data_says_install_tzdata(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_zone_data(key: str) -> None:
        raise ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(edgar_acceptance, "ZoneInfo", no_zone_data)
    with pytest.raises(ZoneInfoNotFoundError) as excinfo:
        parse_acceptance_datetime("20260730163028")
    assert "install tzdata" in str(excinfo.value)


# --- US session bucket (holidays come with the Phase 1 calendar) ----------------------------------------------


def test_session_bucket_0601_is_pre_open() -> None:
    assert us_session_bucket(datetime(2026, 10, 21, 10, 1, 2, tzinfo=timezone.utc)) == "pre_open"  # 10:01:02Z - 4 h = 06:01:02 EDT


def test_session_bucket_noon_is_regular() -> None:
    assert us_session_bucket(datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)) == "regular"  # 16:00Z - 4 h = 12:00 EDT
    assert us_session_bucket(datetime(2026, 1, 29, 14, 30, tzinfo=timezone.utc)) == "regular"  # 14:30Z - 5 h = 09:30 EST, the open


def test_session_bucket_1600_exactly_is_after_close() -> None:
    # Day 0 is the first session whose close comes AFTER information_time, so 16:00:00 already belongs to the next day.
    assert us_session_bucket(datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)) == "after_close"  # 20:00Z - 4 h = 16:00:00 EDT
    assert us_session_bucket(datetime(2026, 10, 21, 19, 59, 59, tzinfo=timezone.utc)) == "regular"  # 15:59:59 EDT


def test_session_bucket_1630_is_after_close() -> None:
    assert us_session_bucket(datetime(2026, 7, 30, 20, 30, 28, tzinfo=timezone.utc)) == "after_close"  # 20:30:28Z - 4 h = 16:30:28 EDT
    assert us_session_bucket(datetime(2026, 1, 29, 21, 30, 33, tzinfo=timezone.utc)) == "after_close"  # 21:30:33Z - 5 h = 16:30:33 EST


def test_session_bucket_weekend_is_non_session_day() -> None:
    assert us_session_bucket(datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)) == "non_session_day"  # 12:00 EDT, a Saturday
    # Friday 2026-10-23 22:00 EDT is already Saturday 02:00 in UTC: the day is taken in New York, so it is after_close.
    assert us_session_bucket(datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)) == "after_close"


def test_session_bucket_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        us_session_bucket(datetime(2026, 10, 21, 16, 0))


def test_public_names_are_re_exported_from_connectors() -> None:
    from finsec_alpharadar import connectors

    for name in ("parse_acceptance_datetime", "extract_acceptance_raw", "us_session_bucket", "EdgarAcceptanceError"):
        assert name in connectors.__all__ and getattr(connectors, name) is getattr(edgar_acceptance, name)


# --- golden: recorded SEC headers (tests/fixtures/edgar_headers, recorded 2026-10-28 by record_edgar_headers.py) ------

FIXTURES = Path(__file__).parent / "fixtures" / "edgar_headers"
FORM4 = "0001140361-26-040127"
# Expected values are written by hand from the recorded raw strings and the US DST rule (EDT = UTC-4 from 2026-03-08 to
# 2026-11-01, EST = UTC-5 outside it); never computed by the parser under test, and never taken from the JSON.
GOLDEN = [
    # (accession, raw <ACCEPTANCE-DATETIME>, information time UTC)
    ("0000320193-26-000018", "20260730163028", datetime(2026, 7, 30, 20, 30, 28, tzinfo=timezone.utc)),  # 16:30:28 EDT + 4 h
    (FORM4, "20261019183255", datetime(2026, 10, 19, 22, 32, 55, tzinfo=timezone.utc)),  # 18:32:55 EDT + 4 h
    ("0000320193-26-000020", "20260731060102", datetime(2026, 7, 31, 10, 1, 2, tzinfo=timezone.utc)),  # 06:01:02 EDT + 4 h
    ("0000320193-26-000005", "20260129163033", datetime(2026, 1, 29, 21, 30, 33, tzinfo=timezone.utc)),  # 16:30:33 EST + 5 h
]
# The submissions-JSON acceptanceDateTime of the same filings (docs/project-roadmap.md §2.1, OBSERVED 2026-10-20). The
# roadmap gives only the time for the 10-Q; its date is the filing's (2026-07-31). Read as UTC, each is 4 h (EDT) or
# 5 h (EST) late: the JSON is discovery only.
JSON_VALUES = {
    "0000320193-26-000018": "2026-07-31T00:30:28Z",
    FORM4: "2026-10-20T02:32:55Z",
    "0000320193-26-000020": "2026-07-31T14:01:02Z",
    "0000320193-26-000005": "2026-01-30T02:30:33Z",
}


def _manifest() -> dict[str, Any]:
    return json.loads((FIXTURES / "manifest.json").read_bytes())


def _entries() -> dict[str, dict[str, Any]]:
    return {entry["file"]: entry for entry in _manifest()["files"]}


def _text(name: str) -> str:
    with (FIXTURES / name).open("rb") as handle:
        return handle.read().decode("utf-8")


def test_fixture_bytes_match_the_manifest() -> None:
    entries = _entries()
    assert len(entries) == 8, "four accessions, a .hdr.sgml and an -index-headers.html each"
    for name, entry in entries.items():
        with (FIXTURES / name).open("rb") as handle:
            data = handle.read()
        assert hashlib.sha256(data).hexdigest() == entry["sha256_committed"], f"{name} changed since it was recorded"
        assert len(data) == entry["bytes"] and entry["user_agent_declared"] is True


@pytest.mark.parametrize(("accession", "raw", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_golden_information_time(accession: str, raw: str, expected: datetime) -> None:
    html = _text(f"{accession}-index-headers.html")
    assert extract_acceptance_raw(html) == raw, "the raw tag and its escaped <PRE> copy agree and equal the literal"
    assert _entries()[f"{accession}-index-headers.html"]["acceptance_raw"] == raw
    info_time = parse_acceptance_datetime(raw)
    assert info_time == expected
    assert info_time.utcoffset() == timedelta(0) and info_time.tzinfo is not None


def test_hdr_sgml_agrees_or_is_recorded_as_tagless() -> None:
    entries = _entries()
    for accession, raw, _ in GOLDEN:
        name = f"{accession}.hdr.sgml"
        if entries[name]["hdr_has_acceptance"]:
            assert extract_acceptance_raw(_text(name)) == raw
        else:
            with pytest.raises(EdgarAcceptanceMissing):
                extract_acceptance_raw(_text(name))
    # OBSERVED 2026-10-28: every recorded .hdr.sgml carries the tag.
    assert all(entries[f"{g[0]}.hdr.sgml"]["hdr_has_acceptance"] for g in GOLDEN)


def test_one_edt_and_one_est_filing_are_covered() -> None:
    # A fixed -05:00 offset (the PDS spec says "EST") would pass the EST filing and fail every EDT one.
    offsets = {parse_acceptance_datetime(raw).astimezone(ZoneInfo("America/New_York")).utcoffset() for _, raw, _ in GOLDEN}
    assert offsets == {timedelta(hours=-4), timedelta(hours=-5)}


@pytest.mark.parametrize(("accession", "raw", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN])
def test_submissions_json_value_read_as_utc_is_not_the_information_time(accession: str, raw: str, expected: datetime) -> None:
    as_utc = datetime.fromisoformat(JSON_VALUES[accession].replace("Z", "+00:00"))
    assert as_utc != parse_acceptance_datetime(raw) == expected
    edt = expected.astimezone(ZoneInfo("America/New_York")).utcoffset() == timedelta(hours=-4)
    assert as_utc - expected == timedelta(hours=4 if edt else 5), "the JSON 'Z' carries the New York offset a second time"


def test_form4_fixture_is_redacted() -> None:
    owner_fields = ("CONFORMED-NAME", "STREET1", "STREET2", "CITY", "STATE", "ZIP", "PHONE")
    spaced_fields = ("COMPANY CONFORMED NAME", "STREET 1", "STREET 2", "CITY", "STATE", "ZIP", "BUSINESS PHONE")
    for name in (f"{FORM4}.hdr.sgml", f"{FORM4}-index-headers.html"):
        text = _text(name)
        blocks = re.findall(r"<REPORTING-OWNER>(.*?)</REPORTING-OWNER>", text, re.DOTALL)
        assert blocks, f"{name}: no reporting-owner block"
        for block in blocks:
            for field in owner_fields:
                for value in re.findall(rf"^<{field}>(.*)$", block, re.MULTILINE):
                    assert value.strip() in ("", "REDACTED"), f"{name}: <{field}> is not redacted"
            assert "<CIK>0002100523" in block, "the owner CIK stays (a public identifier)"
        if name.endswith(".html"):
            spaced = re.search(r"REPORTING-OWNER:(.*?)\nISSUER:", text, re.DOTALL)
            assert spaced is not None
            for field in spaced_fields:
                for value in re.findall(rf"^\s*{field}:\s*(.*)$", spaced.group(1), re.MULTILINE):
                    assert value.strip() in ("", "REDACTED"), f"{name}: '{field}:' is not redacted"
            assert "CENTRAL INDEX KEY:\t\t\t0002100523" in spaced.group(1)
        assert extract_acceptance_raw(text) == "20261019183255", "the acceptance time is untouched"
        entry = _entries()[name]
        assert entry["redactions"] and entry["sha256_original"] != entry["sha256_committed"]
