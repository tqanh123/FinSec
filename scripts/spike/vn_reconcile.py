"""Reconcile 30 VN disclosures (portal posting time vs vnstock events) and print the VN go/no-go table
(plan 261022-1147, phase 6, R6.3).

Run from the repo root with the dev venv (dev user; this script never imports vnstock):
    .venv/Scripts/python scripts/spike/vn_reconcile.py sample --from 2026-10-26 --to 2026-10-30 [--backup-mirror <dir>]
    .venv/Scripts/python scripts/spike/vn_reconcile.py compare --sample <run>/sample.json --vnstock-dir <counts out>/raw \
        [--symbol-overrides <data root>/.../overrides.json]
    .venv/Scripts/python scripts/spike/vn_reconcile.py go-no-go --counts <counts-summary.json> \
        --reconcile <reconcile-summary.json> --history <portal/<date>/<run>-history/history-summary.json>
`sample` reaches the network (HNX RSS feed 3, the HNX POST listing, the HOSE stock listing); `compare` and `go-no-go`
only read files. Never collected by pytest; `tests/test_vn_reconcile.py` checks the pure functions with fakes.

`sample` reads the capture window from `<backup-mirror>/daily/<d>/` ONLY through
`finsec_alpharadar.capture.backup.iter_backup_members` (sha256 against the manifest first, whitelisted members, read in
memory, nothing written to disk). The window counts as open with at least 2 backup days; otherwise every stratum comes
from portal history and the detection latency is skipped with reason "window_not_open" (phase step 1). Portal history:
HNX RSS feed 3 (last 100 items) first, the HNX POST listing only while an HNX stratum is short, then the HOSE
`securitiesType/1` (stocks) listing walking back from `--to` one trading day at a time until every HOSE stratum holds
`--pool-target` items. Politeness: >= 5 s between two requests to one host, at most `--max-requests` (<= 60) requests
and `--max-days` (<= 30) days; every body is saved under `$FINSEC_DATA_ROOT/spike/vn-reconcile/<UTC-date>/<run>/`
BEFORE it is parsed.

Symbols: the title prefix before ':' (3 alphanumerics) on HOSE; HNX RSS titles carry none (0 of 100, OBSERVED
2026-10-28), so an HNX item keeps `symbol: null` and is left out of `compare` unless `--symbol-overrides` (a JSON file
inside the data root, `{"HNX:<id>": "SYM", "HOSE:<id>": "SYM"}`, 3-4 uppercase alphanumerics, filled by the owner)
supplies it. No network call is made for symbols.

Data hygiene (PDPL, copyright, D6): titles and person names exist only inside the data root (`sample.json`, raw
bodies). Everything printed, and every summary (`reconcile-summary.json`, the go/no-go markdown), holds counts, rates,
ids, symbols and types only. vnstock rows are read from the data root and never copied.

Times: HOSE `posted_at_utc = postedDate - 25200` is DERIVED (research vn-disclosure-portals.md §1; Phase 1 makes it
official with a golden test); HNX RSS `pubDate` carries `+0700` (OBSERVED); the HNX POST listing shows
`dd/MM/yyyy HH:MM` with ICT implied (ASSUMED). Buckets: 08:00-14:45, 14:45-17:30, 17:30-24:00, 00:00-08:00 ICT on
trading days, else "non_trading_day" (weekend or a holiday in `configs/capture.toml`).
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import math
import os
import re
import statistics
import sys
import unicodedata
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from _spikelib import (
    FetchResult,
    SpikeEnvError,
    append_jsonl,
    fetch,
    sha256_hex,
    spike_dir,
    utc_now,
    write_json,
)

from finsec_alpharadar.capture.backup import (
    BackupError,
    backup_names,
    iter_backup_members,
)
from finsec_alpharadar.capture.config import CaptureConfigError, load_config_file
from finsec_alpharadar.capture.record import CaptureRecord, RecordType
from finsec_alpharadar.capture.sources import rss_item_fragments
from finsec_alpharadar.data_root import DataRootError, resolve_data_root
from finsec_alpharadar.quant.power_rule import DEFAULT_RHO, assess

__all__ = ["BackupError", "FetchResult"]  # re-exported for the tests

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "capture.toml"

# Shapes shared with scripts/spike/vn_counts.py (slice C). `go-no-go` and `compare` read exactly these.
COUNTS_SUMMARY_SHAPE = """counts-summary.json (written by vn_counts.py events; aggregates only, no data rows):
{
  "kind": "vn_counts_summary",
  "schema_version": 1,                                   (go-no-go refuses any other kind or schema_version)
  "universe": {"size": <int, symbols in the universe file>, ...},
  "symbols": {"requested": int, "ok": int, "empty": int, "failed": int, "skipped": int, ...},
                                                          (ok = symbols fetched, empty ones included; D2 scales by ok)
  "stop": null | {"reason": str, ...},                    (non-null = the run stopped early: N is DERIVED)
  "oldest_date": "<YYYY-MM-DD>" | null,                 (oldest ICT public_date over every group)
  "by_group_year": [
    {"group": "BCTC" | "NQ_HDQT" | "DHDCD" | "DIVIDEND" | "INSIDER" | "OTHER", "year": <int, ICT year>,
     "n_docs": int, "n_events": int, "n_issuers": int, "n_distinct_dates": int, "max_events_per_date": int,
     "share_with_intraday_time": float | null, "n_events_with_intraday_time": int,
     "n_distinct_dates_with_intraday_time": int},
    ...
  ],
  ...                                                     (by_group, pools, codes_seen, status: not read here)
}
n_events counts distinct (symbol, event_code, ICT public_date) keys; n_distinct_dates is per (group, year), so it adds
up over years. A group with no readable row has no by_group_year row. go-no-go reads the groups BCTC and NQ_HDQT,
years >= LABEL_DEPTH_START_YEAR only. An older summary without symbols.ok is read with universe.size (N DERIVED)."""

VNSTOCK_RAW_LAYOUT = """vnstock events rows (written by vn_counts.py events under <out>/raw/, data root only):
one file per symbol, `<SYMBOL>.json`, holding a JSON list of row objects (one per `events(source="vci")` row; a
{"rows": [...]} object is accepted too; any other JSON is refused). Keys are the frame's columns, at least "id",
"symbol", "event_code", "category", "event_title_vi" and "public_date". Dates are ISO-8601 text, rewritten by
vn_counts.py to the ICT offset (`2026-09-16T00:00:00+07:00`); a `Z` suffix and a plain "YYYY-MM-DD" are read too. A
row without "symbol" takes the file stem."""

TYPE_BCTC = "BCTC"
TYPE_NQ_HDQT = "NQ_HDQT"
TYPE_INSIDER = "INSIDER"
EVENT_TYPES = (TYPE_BCTC, TYPE_NQ_HDQT, TYPE_INSIDER)
TYPE_PRIORITY = (TYPE_INSIDER, TYPE_NQ_HDQT, TYPE_BCTC)  # a title matching several types takes the first here
EXCHANGES = ("HOSE", "HNX")
NON_TRADING = "non_trading_day"
BUCKETS = ("08:00-14:45", "14:45-17:30", "17:30-24:00", "00:00-08:00", NON_TRADING)

# Keyword rules (phase file R6.3), matched on NFC + casefold text.
TITLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    TYPE_BCTC: ("báo cáo tài chính", "bctc"),
    TYPE_NQ_HDQT: ("nghị quyết hđqt", "nghị quyết hội đồng quản trị"),
    TYPE_INSIDER: ("đăng ký giao dịch", "người nội bộ"),
}
INSIDER_CODE_PREFIX = "DD"  # vnstock insider dealing codes DD* (research vnstock-gen5.md; vn_counts.py group map)
INSIDER_CATEGORY = "MAJOR_SHAREHOLDER_TRADING"

# VN has used UTC+7 without DST since 1975; a fixed offset keeps the pure functions free of tzdata.
ICT = timezone(timedelta(hours=7), "ICT")
HOSE_PSEUDO_UTC_OFFSET_S = 25200  # DERIVED, research vn-disclosure-portals.md §1
HOSE_EPOCH_MIN_S = 946684800  # 2000-01-01T00:00:00Z: a postedDate outside [2000, 2100) is dropped
HOSE_EPOCH_MAX_S = 4102444800  # 2100-01-01T00:00:00Z
LABEL_DERIVED, LABEL_OBSERVED, LABEL_ASSUMED, LABEL_MISSING = "DERIVED", "OBSERVED", "ASSUMED", "MISSING"
HOSE_TIME_BASIS = "DERIVED: postedDate - 25200 s (pseudo-UTC epoch, research vn-disclosure-portals.md §1)"
HNX_RSS_TIME_BASIS = "OBSERVED: RSS pubDate with +0700"
HNX_LISTING_TIME_BASIS = "ASSUMED: HNX listing dd/MM/yyyy HH:MM read as ICT"
ECM_START = date(2024, 8, 15)  # HOSE single disclosure point (research vn-disclosure-portals.md §1, VERIFIED press)
# The history check counts `securitiesType/0` (every security); stocks (`/1`) were about half of it on 2026-10-21/22
# (research vn-disclosure-portals.md §1, OBSERVED once), so the stock share is DERIVED.
STOCK_SHARE_OF_LISTING = 0.5

MATCH_WINDOW_DAYS = 3
MIN_HOST_GAP_S = 5.0
MAX_REQUESTS_CAP = 60
MAX_DAYS_CAP = 30
MIN_TIMEOUT_S = 30.0
PER_TYPE = 10
PER_EXCHANGE = 5
BACKUP_SOURCE = "capture_backup"
MIN_WINDOW_DAYS = 2

HOSE_HOST = "api.hsx.vn"
HNX_HOST = "hnx.vn"
HOSE_STOCK_LISTING = "https://api.hsx.vn/n/api/v1/1/news/securitiesType/1"  # 1 = stocks (research §1)
HOSE_HEADERS = {"Origin": "https://www.hsx.vn", "Referer": "https://www.hsx.vn/", "Accept": "application/json"}
HOSE_PAGE_SIZE = 20  # research §1: keep pageSize <= 20
HNX_RSS3 = "https://hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss"
HNX_POST_URL = "https://hnx.vn/ModuleArticles/ArticlesCPEtfs/NextPageTinCPNY_CBTCPH"
HNX_POST_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",  # [ASSUMED required], as portal_probe.py
    "Referer": "https://hnx.vn/vi-vn/thong-tin-cong-bo-ny-hnx.html",
}
# Field names OBSERVED (research §2); empty values = no filter [ASSUMED]. The listing never returned rows to the probe.
HNX_POST_FORM = {"pAction": "", "pNhomTin": "", "pTieuDeTin": "", "pMaChungKhoan": "", "pFromDate": "", "pToDate": "",
                 "pOrderBy": "", "pNumRecord": "50"}

# D2 BCTC fallback when vnstock events carry no BCTC (research vn-calendar-sessions.md §4 and §5.1), all DERIVED.
BCTC_EVENTS_PER_ISSUER_YEAR = 4
BCTC_DATES_PER_YEAR = 72  # ~18 publication dates per quarter
UNIVERSE_TARGET = 200  # roadmap §2.5 research universe; scaling to it is DERIVED
# Price bars for labels reach back only to 2018-10-25 (vnstock ohlcv cap, AGENTS.md Verified Facts, OBSERVED
# 2026-10-26), so D2 counts events from this year on: 2018 is partial and dropped (main's decision, 2026-10-28).
LABEL_DEPTH_START_YEAR = 2019
# The HOSE listing counts documents (VI/EN pairs, separate and consolidated statements, explanation letters); research
# vn-calendar-sessions.md §4 puts documents at x2-4 per event. R1 divides its N by this DERIVED factor from that range.
DOCUMENT_TO_EVENT_FACTOR = 2.5
COUNTS_KIND = "vn_counts_summary"  # what vn_counts.py writes
COUNTS_SCHEMA_VERSION = 1

_WS = re.compile(r"\s+")
_SYMBOL_PREFIX = re.compile(r"^\s*([A-Za-z0-9]{3})\s*:")
_LISTING_SYMBOL = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{3}$")  # needs a letter: a 3-digit row number is not a ticker
_OVERRIDE_SYMBOL = re.compile(r"^[A-Z0-9]{3,4}$")
_CDATA = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.DOTALL)
_TAG = re.compile(r"<[^<>]*>")
_HNX_POST_ID = re.compile(r"funcViewDetailArticlesByID\((\d+)")
_HNX_POST_TIME = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})")
_HNX_POST_TITLE = re.compile(r"funcViewDetailArticlesByID\([^)]*\)[^>]*>(.*?)</a\s*>", re.DOTALL | re.IGNORECASE)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td\s*>", re.DOTALL | re.IGNORECASE)
_LONG_FRACTION = re.compile(r"(\.\d{6})\d+")

Fetcher = Callable[[str, str, Mapping[str, str], "bytes | None"], FetchResult]  # (method, url, headers, data)
Sleeper = Callable[[float], None]
Monotonic = Callable[[], float]


# --- text rules -------------------------------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """NFC, casefold, NFC again, whitespace runs collapsed to one space."""
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    return _WS.sub(" ", folded).strip()


_NORMALIZED_KEYWORDS = {kind: tuple(normalize_text(k) for k in words) for kind, words in TITLE_KEYWORDS.items()}


def classify_title(title: str) -> str | None:
    """The event type of a title by the keyword rules, INSIDER before NQ_HDQT before BCTC; None when none matches."""
    text = normalize_text(title or "")
    for kind in TYPE_PRIORITY:
        if any(keyword in text for keyword in _NORMALIZED_KEYWORDS[kind]):
            return kind
    return None


def symbol_from_title(title: str) -> str | None:
    """The title prefix before ':' when it is exactly 3 letters or digits, upper-cased (main's resolution 3)."""
    match = _SYMBOL_PREFIX.match(title or "")
    return match.group(1).upper() if match else None


def id_sort_key(item_id: str) -> tuple[int, int, str]:
    """Digit ids sort numerically and before any other id."""
    return (0, int(item_id), "") if item_id.isdigit() else (1, 0, item_id)


# --- time rules -------------------------------------------------------------------------------------------------


def hose_posted_utc(posted_date: Any) -> datetime:
    """HOSE `postedDate` (pseudo-UTC epoch seconds) minus 7 hours, as UTC. DERIVED (research §1).

    Raises ValueError for a non-int or an epoch outside [2000, 2100) UTC (a millisecond epoch, say), so a caller drops
    one bad entry instead of the platform's OSError or OverflowError aborting the page."""
    if type(posted_date) is not int:
        raise ValueError(f"postedDate must be an int, got {type(posted_date).__name__}")
    seconds = posted_date - HOSE_PSEUDO_UTC_OFFSET_S
    if not HOSE_EPOCH_MIN_S <= seconds < HOSE_EPOCH_MAX_S:
        raise ValueError("postedDate is outside 2000-2100 (not an epoch in seconds)")
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as err:
        raise ValueError(f"postedDate is not a usable epoch: {type(err).__name__}") from err


def parse_hnx_pubdate(text: str) -> datetime:
    """RSS `pubDate` (RFC 5322 with an explicit offset) as UTC; raises ValueError without a usable zone."""
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError) as err:
        raise ValueError(f"not an RFC 5322 date: {type(err).__name__}") from err
    if parsed.tzinfo is None:  # "-0000" means "zone unknown" (RFC 5322 §3.3)
        raise ValueError("pubDate carries no zone")
    return parsed.astimezone(UTC)


def is_trading_day(day: date, holidays: frozenset[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def _require_aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a tz-aware datetime is required")
    return at


def ict_date(at: datetime) -> date:
    return _require_aware(at).astimezone(ICT).date()


def time_bucket(at: datetime, holidays: frozenset[date]) -> str:
    """The ICT posting bucket; half-open intervals, so 14:45:00 and 17:30:00 open the later bucket."""
    local = _require_aware(at).astimezone(ICT)
    if not is_trading_day(local.date(), holidays):
        return NON_TRADING
    clock = local.time()
    if clock < time(8, 0):
        return "00:00-08:00"
    if clock < time(14, 45):
        return "08:00-14:45"
    if clock < time(17, 30):
        return "14:45-17:30"
    return "17:30-24:00"


def count_trading_days(start: date, end: date, holidays: frozenset[date]) -> int:
    """Weekdays in [start, end] that are not configured holidays (only announced holidays are known: DERIVED)."""
    days = (end - start).days + 1
    return sum(1 for offset in range(max(days, 0)) if is_trading_day(start + timedelta(days=offset), holidays))


# --- portal items -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PortalItem:
    """One disclosure. `title` lives in memory and in `sample.json` (data root) only."""

    exchange: str
    source_id: str
    symbol: str | None
    event_type: str | None
    posted_at_raw: Any
    posted_at_utc: datetime
    posted_at_label: str  # DERIVED (HOSE), OBSERVED (HNX RSS), ASSUMED (HNX listing)
    source: str
    title: str
    first_seen_at: datetime | None = None
    pit_quality: str | None = None
    symbol_source: str | None = None  # "title", "listing_column" or None

    @property
    def key(self) -> tuple[str, str]:
        return (self.exchange, self.source_id)

    @property
    def from_window(self) -> bool:
        return self.source == BACKUP_SOURCE


def _item(*, exchange: str, source_id: str, title: str, posted_at_raw: Any, posted_at_utc: datetime, label: str,
          source: str, symbol: str | None = None, first_seen_at: datetime | None = None,
          pit_quality: str | None = None) -> PortalItem:
    """Symbol from a listing column when given, else from the title; type from the title rules."""
    symbol_source = "listing_column" if symbol else None
    if symbol is None:
        symbol = symbol_from_title(title)
        symbol_source = "title" if symbol else None
    return PortalItem(
        exchange=exchange, source_id=source_id, symbol=symbol, event_type=classify_title(title),
        posted_at_raw=posted_at_raw, posted_at_utc=_require_aware(posted_at_utc).astimezone(UTC),
        posted_at_label=label, source=source, title=title, first_seen_at=first_seen_at, pit_quality=pit_quality,
        symbol_source=symbol_source,
    )


def _hose_entry(entry: Any, source: str, *, first_seen_at: datetime | None = None,
                pit_quality: str | None = None) -> PortalItem | None:
    """Only `id`, `title` and `postedDate` are read; `summary` and `relatedName` never leave the raw body."""
    if not isinstance(entry, dict) or type(entry.get("id")) is not int or not isinstance(entry.get("title"), str):
        return None
    try:
        posted = hose_posted_utc(entry.get("postedDate"))
    except ValueError:
        return None
    return _item(exchange="HOSE", source_id=str(entry["id"]), title=entry["title"], posted_at_raw=entry["postedDate"],
                 posted_at_utc=posted, label=LABEL_DERIVED, source=source, first_seen_at=first_seen_at,
                 pit_quality=pit_quality)


def hose_page(body: bytes, source: str) -> tuple[list[PortalItem], int | None]:
    """Items of one HOSE listing page and its `totalPages` (None when absent). Raises ValueError on a body that is not
    a success page; an entry without an int id, a title or an in-range int postedDate is dropped."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as err:  # includes UnicodeDecodeError
        raise ValueError(f"HOSE body is not JSON: {type(err).__name__}") from err
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("data"), dict):
        raise ValueError("HOSE body is not a success page")
    data = payload["data"]
    entries = data.get("list")
    if not isinstance(entries, list):
        raise ValueError("HOSE page has no item list")  # noqa: TRY004 - bad input exits 2
    items = [item for item in (_hose_entry(entry, source) for entry in entries) if item is not None]
    paging = data.get("paging")
    pages = paging.get("totalPages") if isinstance(paging, dict) else None
    return items, pages if type(pages) is int else None


def hose_items(body: bytes, source: str) -> list[PortalItem]:
    return hose_page(body, source)[0]


def _tag_text(fragment: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}\b[^<>]*>(.*?)</{tag}\s*>", fragment, re.DOTALL | re.IGNORECASE)
    if match is None:
        return None
    text = match.group(1).strip()
    cdata = _CDATA.match(text)
    return html.unescape(cdata.group(1).strip() if cdata else text)


def _rss_item(fragment: str, source: str, *, first_seen_at: datetime | None = None,
              pit_quality: str | None = None) -> PortalItem | None:
    guid, title, stamp = _tag_text(fragment, "guid"), _tag_text(fragment, "title"), _tag_text(fragment, "pubDate")
    if not guid or title is None or stamp is None:
        return None
    try:
        posted = parse_hnx_pubdate(stamp)
    except ValueError:
        return None
    return _item(exchange="HNX", source_id=guid, title=title, posted_at_raw=stamp, posted_at_utc=posted,
                 label=LABEL_OBSERVED, source=source, first_seen_at=first_seen_at, pit_quality=pit_quality)


def hnx_rss_items(body: bytes, source: str) -> list[PortalItem]:
    """Items of HNX RSS feed 3, cut by regex (no XML parser, so no entity expansion); unparseable items are dropped.
    Raises ValueError on a body that is not UTF-8."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("HNX RSS body is not UTF-8") from err
    items = (_rss_item(fragment, source) for fragment in rss_item_fragments(text))
    return [item for item in items if item is not None]


def _cell_text(cell: str) -> str:
    return html.unescape(_TAG.sub(" ", cell)).strip()


def hnx_listing_items(body: bytes, source: str) -> list[PortalItem]:
    """Rows of the HNX POST listing [ASSUMED layout, research §2]: id from `funcViewDetailArticlesByID(<id>,...)`,
    `dd/MM/yyyy HH:MM` (ICT), the symbol from the first upper-case 3-character cell holding a letter outside the detail
    link, the title from the detail
    link."""
    text = body.decode("utf-8", errors="replace")
    items: list[PortalItem] = []
    for row in re.split(r"<tr\b", text, flags=re.IGNORECASE)[1:]:
        id_match, time_match = _HNX_POST_ID.search(row), _HNX_POST_TIME.search(row)
        if id_match is None or time_match is None:
            continue
        day, month, year, hour, minute = (int(part) for part in time_match.groups())
        try:
            posted = datetime(year, month, day, hour, minute, tzinfo=ICT)
        except ValueError:
            continue
        raw_cells = _TD.findall(row)
        cells = [_cell_text(cell) for cell in raw_cells]
        # Tickers are printed upper case: match the cell text as is, and skip the detail-link cell (its text is a word).
        symbol = next((text for raw, text in zip(raw_cells, cells)
                       if "funcViewDetailArticlesByID" not in raw and _LISTING_SYMBOL.match(text)), None)
        title_match = _HNX_POST_TITLE.search(row)
        title = _cell_text(title_match.group(1)) if title_match else max(cells, key=len, default="")
        items.append(_item(exchange="HNX", source_id=id_match.group(1), title=title, posted_at_raw=time_match.group(0),
                           posted_at_utc=posted, label=LABEL_ASSUMED, source=source, symbol=symbol))
    return items


# --- capture window (backups) -----------------------------------------------------------------------------------

BACKUP_HOSE_SOURCE = "hose_news_api"  # configs/capture.toml source names
BACKUP_HNX_SOURCE = "hnx_rss_issuers"


def _backup_member_items(name: str, data: bytes) -> list[PortalItem]:
    parts = name.split("/")
    source = parts[1] if len(parts) == 3 and parts[0] == "raw" else None
    if source not in (BACKUP_HOSE_SOURCE, BACKUP_HNX_SOURCE):
        return []
    items: list[PortalItem] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        record = CaptureRecord.from_json_line(line)  # a broken line raises: the archive already matched its manifest
        if record.record_type is not RecordType.ITEM:
            continue
        seen = {"first_seen_at": record.first_seen_at, "pit_quality": record.pit_quality.value}
        item: PortalItem | None = None
        if source == BACKUP_HOSE_SOURCE:
            item = _hose_entry(record.payload, BACKUP_SOURCE, **seen)
        elif isinstance(record.payload, str):
            item = _rss_item(record.payload, BACKUP_SOURCE, **seen)
        if item is not None:
            items.append(item)
    return items


@dataclass(frozen=True)
class BackupRead:
    items: list[PortalItem]
    days_read: list[str]
    days_missing: list[str]


def items_from_backup(mirror: Path, day_from: date, day_to: date) -> BackupRead:
    """Portal items of every backup day in [day_from, day_to]; the earliest sighting of an item wins.

    A day without a backup set is listed as missing; a set that fails `iter_backup_members` raises `BackupError`.
    """
    found: dict[tuple[str, str], PortalItem] = {}
    read: list[str] = []
    missing: list[str] = []
    for offset in range((day_to - day_from).days + 1):
        day = day_from + timedelta(days=offset)
        archive_name, _, manifest_name = backup_names(day)
        folder = mirror / "daily" / day.isoformat()
        archive, manifest = folder / archive_name, folder / manifest_name
        if not (archive.is_file() and manifest.is_file()):
            missing.append(day.isoformat())
            continue
        for name, data in iter_backup_members(archive, manifest):
            for item in _backup_member_items(name, data):
                known = found.get(item.key)
                if known is None or (item.first_seen_at and known.first_seen_at
                                     and item.first_seen_at < known.first_seen_at):
                    found[item.key] = item
        read.append(day.isoformat())
    items = sorted(found.values(), key=lambda i: (i.exchange, id_sort_key(i.source_id)))
    return BackupRead(items=items, days_read=read, days_missing=missing)


def _type_counts(items: Sequence[PortalItem]) -> dict[str, int]:
    return {"n_events": len({item.key for item in items}),
            "n_distinct_dates": len({ict_date(item.posted_at_utc) for item in items})}


def forward_counts(items: Iterable[PortalItem]) -> dict[str, Any]:
    """Forward counts for D4 and R1: typed items with pit_quality first_seen (the caller passes window items only)."""
    eligible = [i for i in items if i.pit_quality == "first_seen" and i.event_type]
    return {
        **_type_counts(eligible),
        "by_type": {kind: _type_counts([i for i in eligible if i.event_type == kind]) for kind in EVENT_TYPES},
        "pit_quality": "first_seen",
    }


# --- deterministic selection ------------------------------------------------------------------------------------


def evenly_spaced(seq: Sequence[Any], k: int) -> list[Any]:
    """k elements spread evenly over `seq` (interval midpoints); all of `seq` when k >= len(seq)."""
    n = len(seq)
    k = min(k, n)
    if k <= 0:
        return []
    return [seq[((2 * i + 1) * n) // (2 * k)] for i in range(k)]


def _take(window: list[PortalItem], history: list[PortalItem], k: int) -> list[PortalItem]:
    """Up to k items: evenly spaced window items first, then evenly spaced history items."""
    chosen = evenly_spaced(window, k)
    return chosen + evenly_spaced(history, k - len(chosen))


def select_sample(items: Iterable[PortalItem], per_type: int = PER_TYPE,
                  per_exchange: int = PER_EXCHANGE) -> tuple[list[PortalItem], list[dict[str, Any]]]:
    """Deterministic stratified sample and its strata report.

    Per type, `per_exchange` items per exchange: window (capture backup) items first, ids sorted, evenly spaced. A
    type short on one exchange is topped up from the other; the shortfall and the top-up are reported, never silent.
    """
    unique: dict[tuple[str, str], PortalItem] = {}
    for item in items:
        if item.event_type is None or item.exchange not in EXCHANGES:
            continue
        known = unique.get(item.key)
        if known is None or (item.from_window and not known.from_window):
            unique[item.key] = item
    selected: list[PortalItem] = []
    strata: list[dict[str, Any]] = []
    for kind in EVENT_TYPES:
        pools: dict[str, tuple[list[PortalItem], list[PortalItem]]] = {}
        taken: dict[str, list[PortalItem]] = {}
        for exchange in EXCHANGES:
            members = sorted((i for i in unique.values() if i.event_type == kind and i.exchange == exchange),
                             key=lambda i: id_sort_key(i.source_id))
            pools[exchange] = ([i for i in members if i.from_window], [i for i in members if not i.from_window])
            taken[exchange] = _take(*pools[exchange], per_exchange)
        topped = {exchange: 0 for exchange in EXCHANGES}
        for exchange in EXCHANGES:
            deficit = per_type - sum(len(group) for group in taken.values())
            if deficit <= 0:
                break
            chosen = {i.key for i in taken[exchange]}
            window, history = ([i for i in pool if i.key not in chosen] for pool in pools[exchange])
            extra = _take(window, history, deficit)
            taken[exchange] += extra
            topped[exchange] = len(extra)
        for exchange in EXCHANGES:
            window, history = pools[exchange]
            group = sorted(taken[exchange], key=lambda i: id_sort_key(i.source_id))
            selected += group
            strata.append({
                "event_type": kind, "exchange": exchange, "wanted": per_exchange,
                "pool": len(window) + len(history), "pool_window": len(window), "got": len(group),
                "got_window": sum(1 for i in group if i.from_window), "short_by": max(per_exchange - len(group), 0),
                "topped_up": topped[exchange], "sources": sorted({i.source for i in group}),
            })
    return selected, strata


def _iso(at: datetime | None) -> str | None:
    return at.astimezone(UTC).isoformat() if at is not None else None


def sample_row(item: PortalItem, holidays: frozenset[date]) -> dict[str, Any]:
    """One `sample.json` row (data root only: it carries the title), built from an explicit field list."""
    local = item.posted_at_utc.astimezone(ICT)
    return {
        "exchange": item.exchange, "source_id": item.source_id, "symbol": item.symbol,
        "symbol_source": item.symbol_source, "event_type": item.event_type, "posted_at_raw": item.posted_at_raw,
        "posted_at_utc": _iso(item.posted_at_utc), "posted_at_label": item.posted_at_label,
        "posted_at_ict": local.isoformat(), "posted_date_ict": local.date().isoformat(),
        "bucket": time_bucket(item.posted_at_utc, holidays), "source": item.source,
        "first_seen_at": _iso(item.first_seen_at), "pit_quality": item.pit_quality, "title": item.title,
    }


def sample_document(selected: Sequence[PortalItem], strata: Sequence[Mapping[str, Any]], *, holidays: frozenset[date],
                    meta: Mapping[str, Any], forward: Mapping[str, Any], window_open: bool) -> dict[str, Any]:
    """The `sample.json` document (data root only)."""
    return {
        "kind": "vn_reconcile_sample",
        "meta": dict(meta),
        "window_open": window_open,
        "detection_latency_skipped": None if window_open else "window_not_open",
        "selection_rule": f"{PER_TYPE} per type, {PER_EXCHANGE} per exchange, capture-window items first, ids sorted, "
                          "evenly spaced (interval midpoints); a short exchange is topped up from the other and reported",
        "strata": [dict(stratum) for stratum in strata],
        "forward": dict(forward),
        "items": [sample_row(item, holidays) for item in selected],
        "labels": {"posted_at_utc": {"HOSE": HOSE_TIME_BASIS, "HNX_RSS": HNX_RSS_TIME_BASIS,
                                     "HNX_LISTING": HNX_LISTING_TIME_BASIS},
                   "symbol": "HOSE: title prefix before ':' (3 alphanumerics); HNX RSS: none (OBSERVED 2026-10-28); "
                             "HNX listing: symbol column (ASSUMED)"},
    }


def sample_summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Counts of a sample document, safe to print: no text column of any item."""
    items = doc.get("items", [])
    return {
        "kind": "vn_reconcile_sample_summary",
        "items": len(items),
        "window_open": doc.get("window_open"),
        "detection_latency_skipped": doc.get("detection_latency_skipped"),
        "with_symbol": {x: sum(1 for i in items if i["exchange"] == x and i["symbol"]) for x in EXCHANGES},
        "without_symbol": {x: sum(1 for i in items if i["exchange"] == x and not i["symbol"]) for x in EXCHANGES},
        "buckets": _bucket_counts(items),
        "strata": [{key: stratum[key] for key in ("event_type", "exchange", "wanted", "pool", "pool_window", "got",
                                                  "short_by", "topped_up", "sources") if key in stratum}
                   for stratum in doc.get("strata", [])],
        "forward": doc.get("forward"),
    }


def print_sample_summary(summary: Mapping[str, Any]) -> None:
    """Counts only."""
    print(f"sample size: {summary['items']}; window open: {summary['window_open']}; "
          f"detection latency skipped: {summary['detection_latency_skipped']}")
    print(f"with symbol {summary['with_symbol']}; without symbol {summary['without_symbol']}")
    for s in summary["strata"]:
        print(f"  {s['event_type']:<8} {s['exchange']:<4} got {s['got']:>2}/{s['wanted']} pool {s['pool']:>3} "
              f"(window {s['pool_window']}) short_by {s['short_by']} topped_up {s['topped_up']} "
              f"sources {','.join(s['sources']) or '-'}")
    print(f"buckets: {summary['buckets']}")


# --- network shell (thin) ---------------------------------------------------------------------------------------


@dataclass
class HostGate:
    """Keeps >= MIN_HOST_GAP_S between the end of one request to a host and the start of the next."""

    sleeper: Sleeper
    monotonic: Monotonic
    gap_s: float = MIN_HOST_GAP_S
    last_end: dict[str, float] = field(default_factory=dict)

    def wait(self, host: str) -> None:
        last = self.last_end.get(host)
        if last is not None:
            remaining = self.gap_s - (self.monotonic() - last)
            if remaining > 0:
                self.sleeper(remaining)

    def done(self, host: str) -> None:
        self.last_end[host] = self.monotonic()


def _extension(content_type: str | None) -> str:
    lowered = (content_type or "").lower()
    for marker, extension in (("json", "json"), ("xml", "xml"), ("rss", "xml"), ("html", "html")):
        if marker in lowered:
            return extension
    return "bin"


@dataclass
class PoliteSession:
    """At most `max_requests` requests through a HostGate; each body is saved before anyone parses it."""

    fetcher: Fetcher
    gate: HostGate
    out_dir: Path
    max_requests: int
    count: int = 0

    def request(self, host: str, label: str, method: str, url: str, headers: Mapping[str, str],
                data: bytes | None = None) -> FetchResult | None:
        """The result, or None when the request budget is spent (nothing is sent then)."""
        if self.count >= self.max_requests:
            return None
        self.gate.wait(host)
        try:
            result = self.fetcher(method, url, headers, data)
        finally:
            self.gate.done(host)
            self.count += 1
        raw_name = f"{self.count:03d}-{label}.{_extension(result.header('Content-Type'))}"
        if result.body:
            (self.out_dir / raw_name).write_bytes(result.body)
        append_jsonl(self.out_dir / "requests.jsonl", {
            "raw": raw_name if result.body else None, "method": method, "url": url, "status": result.status,
            "error": result.error.split(":", 1)[0] if result.error else None, "bytes": len(result.body),
            "sha256": sha256_hex(result.body), "truncated": result.truncated,
            "content_type": result.header("Content-Type"), "seconds_total": round(result.seconds_total, 3),
            "local_before_utc": result.local_before_utc.isoformat(),
        })
        return result


def _hose_url(day: date, page: int) -> str:
    query = urllib.parse.urlencode({"pageIndex": page, "pageSize": HOSE_PAGE_SIZE, "startDate": day.isoformat(),
                                    "endDate": day.isoformat()})
    return f"{HOSE_STOCK_LISTING}?{query}"


class _Budget(Exception):
    """The request budget is spent."""


def _pool_sizes(found: Mapping[tuple[str, str], PortalItem], exchange: str) -> dict[str, int]:
    return {kind: sum(1 for i in found.values() if i.exchange == exchange and i.event_type == kind)
            for kind in EVENT_TYPES}


def _add(found: dict[tuple[str, str], PortalItem], new: Iterable[PortalItem]) -> int:
    added = 0
    for item in new:
        if item.key not in found:
            found[item.key] = item
            added += 1
    return added


def _parsed(result: FetchResult, parse: Callable[[bytes], Any], empty: Any) -> tuple[Any, str | None]:
    """(parsed value, None) for a 200 response that parses, else (empty, reason)."""
    if result.status != 200:
        return empty, f"status_{result.status}"
    try:
        return parse(result.body), None
    except ValueError as err:
        return empty, f"parse_error: {err}"


def collect_history(
    session: PoliteSession, found: dict[tuple[str, str], PortalItem], *, day_to: date, holidays: frozenset[date],
    max_days: int, pool_target: int, max_pages_per_day: int, hnx_post_pages: int,
) -> dict[str, Any]:
    """Fill `found` from portal history until every stratum pool holds `pool_target` items or a bound is hit.

    Returns the run facts (counts, statuses and request numbers only)."""
    facts: dict[str, Any] = {"hnx_rss": None, "hnx_post": "not_needed", "hose_days": [], "stopped": None,
                             "hose_securities_type": 1, "pool_target": pool_target}

    def get(host: str, label: str, method: str, url: str, headers: Mapping[str, str],
            data: bytes | None = None) -> FetchResult:
        result = session.request(host, label, method, url, headers, data)
        if result is None:
            raise _Budget
        return result

    def short(exchange: str) -> bool:
        return min(_pool_sizes(found, exchange).values()) < pool_target

    try:
        if short("HNX"):
            result = get(HNX_HOST, "hnx_rss3", "GET", HNX_RSS3, {})
            rss, problem = _parsed(result, lambda body: hnx_rss_items(body, "hnx_rss3"), [])
            facts["hnx_rss"] = {"status": result.status, "items": len(rss), "added": _add(found, rss),
                                "problem": problem}
        if hnx_post_pages > 0 and short("HNX"):
            pages: list[dict[str, Any]] = []
            facts["hnx_post"] = pages
            for page in range(1, hnx_post_pages + 1):
                form = urllib.parse.urlencode({"pNumPage": str(page), **HNX_POST_FORM}).encode("ascii")
                result = get(HNX_HOST, f"hnx_post-p{page}", "POST", HNX_POST_URL, HNX_POST_HEADERS, form)
                rows, problem = _parsed(result, lambda body: hnx_listing_items(body, "hnx_post_listing"), [])
                pages.append({"page": page, "status": result.status, "items": len(rows), "added": _add(found, rows),
                              "problem": problem})
                if not rows or not short("HNX"):
                    break
        for offset in range(max_days):
            if not short("HOSE"):
                facts["stopped"] = "filled"
                break
            day = day_to - timedelta(days=offset)
            if not is_trading_day(day, holidays):
                continue
            walked: dict[str, Any] = {"day": day.isoformat(), "pages": [], "total_pages": None}
            facts["hose_days"].append(walked)
            result = get(HOSE_HOST, f"hose-{day.isoformat()}-p1", "GET", _hose_url(day, 1), HOSE_HEADERS)
            (items, total_pages), problem = _parsed(result, lambda body: hose_page(body, "hose_api_history"),
                                                    ([], None))
            walked["pages"].append({"page": 1, "status": result.status, "items": len(items),
                                    "added": _add(found, items), "problem": problem})
            walked["total_pages"] = total_pages
            if total_pages and total_pages > 1 and max_pages_per_day > 1:
                for page in evenly_spaced(list(range(2, total_pages + 1)), max_pages_per_day - 1):
                    result = get(HOSE_HOST, f"hose-{day.isoformat()}-p{page}", "GET", _hose_url(day, page),
                                 HOSE_HEADERS)
                    items, problem = _parsed(result, lambda body: hose_items(body, "hose_api_history"), [])
                    walked["pages"].append({"page": page, "status": result.status, "items": len(items),
                                            "added": _add(found, items), "problem": problem})
        else:
            facts["stopped"] = "max_days" if short("HOSE") else "filled"
    except _Budget:
        facts["stopped"] = "max_requests"
    facts["requests"] = session.count
    facts["pools"] = {exchange: _pool_sizes(found, exchange) for exchange in EXCHANGES}
    return facts


def run_sample(
    run_dir: Path, *, day_from: date, day_to: date, holidays: frozenset[date], fetcher: Fetcher, sleeper: Sleeper,
    monotonic: Monotonic, backup_mirror: Path | None, max_requests: int = MAX_REQUESTS_CAP,
    max_days: int = MAX_DAYS_CAP, pool_target: int = PER_TYPE, max_pages_per_day: int = 3, hnx_post_pages: int = 2,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write `sample.json` in `run_dir` (window items when the window is open, portal history for the rest), print
    its counts and return the document."""
    window: list[PortalItem] = []
    days_read: list[str] = []
    days_missing: list[str] = []
    if backup_mirror is not None:
        read = items_from_backup(backup_mirror, day_from, day_to)
        window, days_read, days_missing = read.items, read.days_read, read.days_missing
    window_open = len(days_read) >= MIN_WINDOW_DAYS
    found: dict[tuple[str, str], PortalItem] = {item.key: item for item in window} if window_open else {}
    session = PoliteSession(fetcher=fetcher, gate=HostGate(sleeper=sleeper, monotonic=monotonic), out_dir=run_dir,
                            max_requests=max_requests)
    facts = collect_history(session, found, day_to=day_to, holidays=holidays, max_days=max_days,
                            pool_target=pool_target, max_pages_per_day=max_pages_per_day, hnx_post_pages=hnx_post_pages)
    selected, strata = select_sample(found.values())
    in_window = [i for i in window if day_from <= ict_date(i.posted_at_utc) <= day_to] if window_open else []
    meta = {"from": day_from.isoformat(), "to": day_to.isoformat(), "created_at_utc": _iso(now),
            "backup_mirror_given": backup_mirror is not None, "backup_days_read": days_read,
            "backup_days_missing": days_missing, "min_window_days": MIN_WINDOW_DAYS, "history": facts}
    doc = sample_document(selected, strata, holidays=holidays, meta=meta, forward=forward_counts(in_window),
                          window_open=window_open)
    write_json(run_dir / "sample.json", doc)
    print_sample_summary(sample_summary(doc))
    print(f"history requests: {facts['requests']}, stopped: {facts['stopped']}")
    return doc


# --- vnstock rows and matching ----------------------------------------------------------------------------------


def parse_public_date(value: Any) -> tuple[date, bool] | None:
    """(ICT date, has a time of day other than 00:00:00 ICT) of a vnstock ISO date string; None when unreadable.

    A naive timestamp is read as ICT; a `Z` suffix and up to 9 fractional digits are accepted."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text), False
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(_LONG_FRACTION.sub(r"\1", text))
    except ValueError:
        return None
    local = parsed.replace(tzinfo=ICT) if parsed.tzinfo is None else parsed.astimezone(ICT)
    return local.date(), local.time() != time(0, 0)


def vnstock_row_type(row: Mapping[str, Any]) -> str | None:
    """INSIDER for an event_code DD* or the major-shareholder category; else the title rules on the text columns."""
    code = row.get("event_code")
    if isinstance(code, str) and code.strip().upper().startswith(INSIDER_CODE_PREFIX):
        return TYPE_INSIDER
    category = row.get("category")
    if isinstance(category, str) and category.strip().upper() == INSIDER_CATEGORY:
        return TYPE_INSIDER
    for column in ("event_title_vi", "event_name_vi"):
        value = row.get(column)
        if isinstance(value, str) and (kind := classify_title(value)):
            return kind
    return None


def load_vnstock_rows(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """Rows per symbol from every `*.json` in `directory` (layout: VNSTOCK_RAW_LAYOUT); other JSON raises ValueError."""
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise ValueError(f"cannot read vnstock rows in {path.name}: {type(err).__name__}") from err
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"{path.name} is not a vnstock row list")
        stem = path.stem.upper()
        for row in rows:
            symbol = str(row.get("symbol") or stem).strip().upper()
            by_symbol.setdefault(symbol, []).append({**row, "symbol": symbol})
    return by_symbol


@dataclass(frozen=True)
class Match:
    row_id: str
    lag_days: int  # vnstock public_date - portal posting date (ICT days)
    has_time: bool


@dataclass(frozen=True)
class _Candidate:
    lag: int
    row_index: int  # position in the symbol's row list: the row's identity even when two rows share an id
    row_id: str
    has_time: bool

    def sort_key(self) -> tuple[int, tuple[int, int, str], int]:
        return (abs(self.lag), id_sort_key(self.row_id), self.row_index)


def _candidates(item: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], window_days: int) -> list[_Candidate]:
    """Rows of the item's symbol and type dated within +-window_days of its ICT posting date."""
    symbol = item.get("symbol")
    if not symbol:
        return []
    posted = date.fromisoformat(item["posted_date_ict"])
    found: list[_Candidate] = []
    for index, row in enumerate(rows):
        if str(row.get("symbol") or "").upper() != symbol or vnstock_row_type(row) != item.get("event_type"):
            continue
        parsed = parse_public_date(row.get("public_date"))
        if parsed is None:
            continue
        lag = (parsed[0] - posted).days
        if abs(lag) <= window_days:
            found.append(_Candidate(lag=lag, row_index=index, row_id=str(row.get("id")), has_time=parsed[1]))
    return found


def match_item(item: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
               window_days: int = MATCH_WINDOW_DAYS) -> Match | None:
    """The vnstock row of the same symbol and type whose date is nearest the posting date, within +-window_days,
    for ONE item on its own (`match_items` pairs a whole sample one-to-one).

    Ties: the smaller |lag|, then the smaller id. None without a symbol or without such a row."""
    found = _candidates(item, rows, window_days)
    if not found:
        return None
    best = min(found, key=_Candidate.sort_key)
    return Match(row_id=best.row_id, lag_days=best.lag, has_time=best.has_time)


def match_items(items: Sequence[Mapping[str, Any]], vnstock: Mapping[str, Sequence[Mapping[str, Any]]],
                window_days: int = MATCH_WINDOW_DAYS) -> list[Match | None]:
    """One-to-one pairing of sample items with vnstock rows (same symbol and type, +-window_days), in item order.

    Greedy over every candidate pair sorted by (|lag|, item id, exchange, row id): each item and each vnstock row is
    used at most once, so one row never counts for two disclosures."""
    pairs: list[tuple[tuple[Any, ...], int, str, _Candidate]] = []
    for position, item in enumerate(items):
        symbol = item.get("symbol")
        if not symbol:
            continue
        for candidate in _candidates(item, vnstock.get(symbol, []), window_days):
            key = (abs(candidate.lag), id_sort_key(str(item["source_id"])), str(item["exchange"]),
                   id_sort_key(candidate.row_id), candidate.row_index)
            pairs.append((key, position, symbol, candidate))
    pairs.sort(key=lambda pair: pair[0])
    matches: list[Match | None] = [None] * len(items)
    used_rows: set[tuple[str, int]] = set()
    for _, position, symbol, candidate in pairs:
        row_key = (symbol, candidate.row_index)
        if matches[position] is not None or row_key in used_rows:
            continue
        matches[position] = Match(row_id=candidate.row_id, lag_days=candidate.lag, has_time=candidate.has_time)
        used_rows.add(row_key)
    return matches


def parse_symbol_overrides(data: Any) -> dict[tuple[str, str], str]:
    """`{"HNX:<id>": "SYM", "HOSE:<id>": "SYM"}` (owner-filled, data root only), SYM 3-4 uppercase alphanumerics.
    Raises ValueError on any other shape."""
    if not isinstance(data, dict):
        raise ValueError("symbol overrides must be a JSON object")  # noqa: TRY004 - bad input exits 2
    overrides: dict[tuple[str, str], str] = {}
    for key, value in data.items():
        exchange, sep, item_id = str(key).partition(":")
        if not sep or exchange not in EXCHANGES or not item_id.strip():
            raise ValueError(f"symbol override key {key!r} is not HOSE:<id> or HNX:<id>")
        if not isinstance(value, str) or not _OVERRIDE_SYMBOL.match(value):
            raise ValueError(f"symbol override for {key!r} is not 3-4 uppercase letters or digits")
        overrides[(exchange, item_id)] = value
    return overrides


def apply_symbol_overrides(rows: Sequence[Mapping[str, Any]],
                           overrides: Mapping[tuple[str, str], str]) -> tuple[list[dict[str, Any]], int]:
    """Copies of the sample rows with override symbols set (`symbol_source: override`) and the count applied.

    An override naming an item that is not in the sample raises ValueError (a typo must not pass silently)."""
    known = {(row["exchange"], row["source_id"]) for row in rows}
    unknown = sorted(f"{exchange}:{item_id}" for exchange, item_id in overrides if (exchange, item_id) not in known)
    if unknown:
        raise ValueError(f"symbol overrides name items not in the sample: {', '.join(unknown)}")
    updated: list[dict[str, Any]] = []
    applied = 0
    for row in rows:
        symbol = overrides.get((row["exchange"], row["source_id"]))
        if symbol is None:
            updated.append(dict(row))
        else:
            updated.append({**row, "symbol": symbol, "symbol_source": "override"})
            applied += 1
    return updated, applied


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _nearest_rank(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]


def _bucket_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(row["bucket"] for row in rows)
    return {bucket: counts.get(bucket, 0) for bucket in BUCKETS}


def detection_latency(rows: Sequence[Mapping[str, Any]], window_open: bool) -> dict[str, Any]:
    """first_seen_at - posted time (s) of first_seen items: n, median, p90 (nearest rank)."""
    if not window_open:
        return {"n": 0, "median_s": None, "p90_s": None, "reason": "window_not_open"}
    values: list[float] = []
    for row in rows:
        if row.get("pit_quality") != "first_seen" or not row.get("first_seen_at") or not row.get("posted_at_utc"):
            continue
        first_seen = datetime.fromisoformat(row["first_seen_at"])
        posted = datetime.fromisoformat(row["posted_at_utc"])
        values.append((first_seen - posted).total_seconds())
    if not values:
        return {"n": 0, "median_s": None, "p90_s": None, "reason": "no_first_seen_item"}
    return {"n": len(values), "median_s": float(statistics.median(values)), "p90_s": _nearest_rank(values, 0.9),
            "reason": None}


def reconcile_summary(rows: Sequence[Mapping[str, Any]], vnstock: Mapping[str, Sequence[Mapping[str, Any]]], *,
                      overrides_applied: int, window_open: bool,
                      forward: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Aggregates only (counts, rates, ids, symbols, types): never a title, a name or a vnstock row."""
    per_item: list[dict[str, Any]] = []
    matched_times: list[bool] = []
    matched_rows: set[tuple[str, str]] = set()
    for row, match in zip(rows, match_items(rows, vnstock), strict=True):
        symbol = row.get("symbol")
        if match is not None:
            matched_times.append(match.has_time)
            matched_rows.add((symbol, match.row_id))
        reason = None if match else "no_symbol" if not symbol else (
            "no_vnstock_rows" if not vnstock.get(symbol) else
            "row_assigned_to_another_item" if match_item(row, vnstock[symbol]) else "no_row_within_window")
        per_item.append({"exchange": row["exchange"], "source_id": row["source_id"], "symbol": symbol,
                         "symbol_source": row.get("symbol_source"), "event_type": row["event_type"],
                         "bucket": row["bucket"], "matched": match is not None,
                         "lag_days": match.lag_days if match else None, "reason": reason})
    compared = [p for p in per_item if p["symbol"]]
    matched = [p for p in compared if p["matched"]]
    lags = [p["lag_days"] for p in matched]
    symbol_dates = [parse_public_date(r.get("public_date")) for s in sorted({p["symbol"] for p in compared})
                    for r in vnstock.get(s, [])]
    readable = [parsed for parsed in symbol_dates if parsed is not None]
    n = len(per_item)
    buckets = _bucket_counts(per_item)
    return {
        "kind": "vn_reconcile_summary",
        "window_open": window_open,
        "sample_items": n,
        "by_stratum": {f"{k}/{x}": sum(1 for p in per_item if p["event_type"] == k and p["exchange"] == x)
                       for k in EVENT_TYPES for x in EXCHANGES},
        "symbols": {
            "excluded_no_symbol": {x: sum(1 for p in per_item if p["exchange"] == x and not p["symbol"])
                                   for x in EXCHANGES},
            "from_override": overrides_applied,
        },
        "compared": len(compared),
        "date_match": {
            "matched": len(matched), "rate": _share(len(matched), len(compared)),
            "distinct_rows_matched": len(matched_rows),
            "by_type": {k: {"compared": sum(1 for p in compared if p["event_type"] == k),
                            "matched": sum(1 for p in matched if p["event_type"] == k)} for k in EVENT_TYPES},
            "by_exchange": {x: {"compared": sum(1 for p in compared if p["exchange"] == x),
                                "matched": sum(1 for p in matched if p["exchange"] == x)} for x in EXCHANGES},
            "unmatched_reasons": dict(Counter(p["reason"] for p in compared if not p["matched"])),
            "rule": f"same symbol, same type, vnstock public_date within +-{MATCH_WINDOW_DAYS} ICT days; one-to-one, "
                    "greedy by (|lag|, item id, row id): a vnstock row pairs with at most one item",
        },
        "lag_days": {str(lag): count for lag, count in sorted(Counter(lags).items())},
        "lag_days_median": float(statistics.median(lags)) if lags else None,
        "vnstock_share_with_intraday_time": {
            "matched_rows": _share(sum(matched_times), len(matched_times)),
            "n_matched_rows": len(matched_times),
            "all_rows_of_sample_symbols": _share(sum(1 for _, has_time in readable if has_time), len(readable)),
            "n_rows_of_sample_symbols": len(readable),
        },
        "posting_buckets": {
            "all": buckets,
            "by_exchange": {x: _bucket_counts(p for p in per_item if p["exchange"] == x) for x in EXCHANGES},
            "by_type": {k: _bucket_counts(p for p in per_item if p["event_type"] == k) for k in EVENT_TYPES},
        },
        "share_after_1445": _share(buckets["14:45-17:30"] + buckets["17:30-24:00"], n),
        "share_after_1730": _share(buckets["17:30-24:00"], n),
        "share_non_trading_day": _share(buckets[NON_TRADING], n),
        "detection_latency": detection_latency(rows, window_open),
        "forward": dict(forward) if forward is not None else forward_counts([]),
        "per_item": per_item,
        "labels": {
            "posted_at_utc": {"HOSE": HOSE_TIME_BASIS, "HNX_RSS": HNX_RSS_TIME_BASIS,
                              "HNX_LISTING": HNX_LISTING_TIME_BASIS},
            "lag_days": "vnstock public_date - portal posting date (ICT days), matched rows only",
            "vnstock_type": "ASSUMED: event_code DD* or category MAJOR_SHAREHOLDER_TRADING = INSIDER; else the "
                            "keyword rules on event_title_vi / event_name_vi",
            "symbol_overrides": "owner-filled id -> symbol map (HNX RSS items carry no symbol, OBSERVED 2026-10-28)",
            "shares": "share_after_1445 = trading-day items posted 14:45-24:00 over all sample items",
        },
    }


def print_reconcile_summary(summary: Mapping[str, Any]) -> None:
    """Counts and rates only."""
    match = summary["date_match"]
    print(f"sample: {summary['sample_items']}; excluded_no_symbol: {summary['symbols']['excluded_no_symbol']}; "
          f"from override: {summary['symbols']['from_override']}")
    print(f"date_match: {match['matched']}/{summary['compared']} (rate {match['rate']}, "
          f"{match['distinct_rows_matched']} distinct vnstock rows); lag days "
          f"{summary['lag_days']}; unmatched {match['unmatched_reasons']}")
    print(f"vnstock intraday time: {summary['vnstock_share_with_intraday_time']}")
    print(f"posting buckets: {summary['posting_buckets']['all']}")
    print(f"detection latency: {summary['detection_latency']}")


# --- go / no-go -------------------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _group_years(counts: Mapping[str, Any]) -> dict[str, list[tuple[int, int, int]]]:
    """{group: [(year, n_events, n_distinct_dates), ...]} from `by_group_year`; raises ValueError on a bad row."""
    rows = counts.get("by_group_year")
    if not isinstance(rows, list):
        raise ValueError("counts summary has no by_group_year list (see COUNTS_SUMMARY_SHAPE)")  # noqa: TRY004 - bad input exits 2
    groups: dict[str, list[tuple[int, int, int]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("group"), str):
            raise ValueError("counts row without a group")  # noqa: TRY004 - bad input exits 2
        group, year = row["group"], _int(row.get("year"))
        n, l = _int(row.get("n_events")), _int(row.get("n_distinct_dates"))
        if year is None or n is None or l is None or n < 0 or l < 0 or l > n or (n > 0 and l == 0):
            raise ValueError(f"counts row {group} {row.get('year')}: n_events={row.get('n_events')}, "
                             f"n_distinct_dates={row.get('n_distinct_dates')}")
        groups.setdefault(group, []).append((year, n, l))
    return groups


def _label_span_years(oldest: Any, as_of: date) -> float | None:
    """Years from max(oldest_date, LABEL_DEPTH_START_YEAR-01-01) to as_of; None without a readable oldest_date."""
    if not isinstance(oldest, str):
        return None
    try:
        start = date.fromisoformat(oldest[:10])
    except ValueError:
        return None
    start = max(start, date(LABEL_DEPTH_START_YEAR, 1, 1))
    return max((as_of - start).days / 365.25, 0.0)


def _clamp_l(n: int, l: int, note: str) -> tuple[int, str]:
    """L limited to N for a DERIVED row (assess refuses L > N): an estimated N below its date count still gets a row."""
    if l <= n:
        return l, note
    return n, f"{note}; L clamped from {l} to N={n} (DERIVED)"


def _check_counts_kind(counts: Mapping[str, Any]) -> None:
    if counts.get("kind") != COUNTS_KIND or _int(counts.get("schema_version")) != COUNTS_SCHEMA_VERSION:
        raise ValueError(f"counts summary must have kind {COUNTS_KIND!r} and schema_version {COUNTS_SCHEMA_VERSION} "
                         "(see COUNTS_SUMMARY_SHAPE)")


_STOP_REASON = re.compile(r"^[a-z_]{1,40}$")


def _fetched_symbols(counts: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """(symbols the counts cover, why N cannot be OBSERVED or None).

    `symbols.ok` (empty symbols included) is the denominator. An older summary without it falls back to
    `universe.size`; an early `stop` or `ok < requested` keeps the count but makes N DERIVED."""
    universe = counts.get("universe")
    size = _int(universe.get("size")) if isinstance(universe, dict) else None
    symbols = counts.get("symbols")
    ok = _int(symbols.get("ok")) if isinstance(symbols, dict) else None
    if ok is None:
        return size, "older counts summary without symbols.ok, universe.size taken as the symbols fetched"
    requested = _int(symbols.get("requested"))
    requested = size if requested is None else requested
    if ok < 0 or (requested is not None and ok > requested):
        raise ValueError(f"counts summary: symbols.ok={ok} is not within 0..requested={requested}")
    stop = counts.get("stop")
    if stop is not None:
        reason = stop.get("reason") if isinstance(stop, dict) else None
        shown = reason if isinstance(reason, str) and _STOP_REASON.match(reason) else "unknown reason"
        return ok, f"vn_counts stopped early ({shown}) after {ok} of {requested} symbols"
    if requested is not None and ok < requested:
        return ok, f"{ok} of {requested} symbols fetched (the rest failed or were skipped)"
    return ok, None


def _row(key: str, target: str, event_set: str, n: int | None, l: int | None, n_label: str, l_label: str,
         note: str, rho: float) -> dict[str, Any]:
    row: dict[str, Any] = {"key": key, "target": target, "event_set": event_set, "n_events": n,
                           "n_distinct_dates": l, "n_label": n_label, "l_label": l_label, "note": note, "rho": rho,
                           "n_eff": None, "mde": None, "tier": None, "mde_floor": None, "tier_floor": None,
                           "mde_80": None}
    if n is None or l is None:
        return row
    a = assess(n, l, rho)
    row.update(n_eff=a.n_eff, mde=a.mde, tier=a.tier.value, mde_floor=a.mde_floor, tier_floor=a.tier_floor.value,
               mde_80=a.mde_80)
    return row


def _d2_row(group: str, counts: Mapping[str, Any], groups: Mapping[str, list[tuple[int, int, int]]], *,
            as_of: date, rho: float, universe: int) -> dict[str, Any]:
    key = f"D2_{group}"
    every_year = groups.get(group, [])
    years = [entry for entry in every_year if entry[0] >= LABEL_DEPTH_START_YEAR]
    dropped = sorted({year for year, _, _ in every_year if year < LABEL_DEPTH_START_YEAR})
    depth = (f"events from {LABEL_DEPTH_START_YEAR} on only (label price bars start 2018-10-25, so 2018 is partial"
             + (f"; dropped year(s) {', '.join(map(str, dropped))})" if dropped else ")"))
    n_obs, l_obs = sum(n for _, n, _ in years), sum(l for _, _, l in years)
    if n_obs == 0:
        if group != TYPE_BCTC:
            return _row(key, "D2", group, None, None, LABEL_MISSING, LABEL_MISSING,
                        f"no {group} event in the vnstock counts; not guessed; {depth}", rho)
        span = _label_span_years(counts.get("oldest_date"), as_of)
        if span is None:
            return _row(key, "D2", group, None, None, LABEL_MISSING, LABEL_MISSING,
                        "no BCTC group and no oldest_date in the counts summary", rho)
        n = round(BCTC_EVENTS_PER_ISSUER_YEAR * universe * span)
        l, note = _clamp_l(n, round(BCTC_DATES_PER_YEAR * span),
                           f"vnstock events carry no BCTC: {BCTC_EVENTS_PER_ISSUER_YEAR}/issuer/year x U={universe} x "
                           f"{span:.2f} years from max(oldest_date, {LABEL_DEPTH_START_YEAR}-01-01), "
                           f"{BCTC_DATES_PER_YEAR} dates/year (research §4, §5.1); {depth}")
        return _row(key, "D2", group, n, l, LABEL_DERIVED, LABEL_DERIVED, note, rho)
    fetched, why = _fetched_symbols(counts)
    note = (f"{len(years)} year(s) of vnstock counts; date-only events count as after the close (research §5.2); "
            f"{depth}")
    if fetched == 0:
        raise ValueError(f"counts summary: {group} has events but 0 symbols were fetched")
    n, n_label = n_obs, (LABEL_DERIVED if why else LABEL_OBSERVED)
    if fetched is None:
        note += "; universe size missing, N not scaled"
    elif fetched != universe:
        n, n_label = round(n_obs * universe / fetched), LABEL_DERIVED
        note += f"; N scaled from {n_obs} at {fetched} symbols fetched to U={universe}; dates not scaled"
    if why:
        note += f"; N DERIVED: {why}"
    l, note = _clamp_l(n, l_obs, note)
    return _row(key, "D2", group, n, l, n_label, LABEL_OBSERVED if l == l_obs else LABEL_DERIVED, note, rho)


def _forward(reconcile: Mapping[str, Any]) -> tuple[int, int, str]:
    forward = reconcile.get("forward")
    if not isinstance(forward, dict):
        return 0, 0, "no forward counts in the reconcile summary"
    n, l = _int(forward.get("n_events")), _int(forward.get("n_distinct_dates"))
    if n is None or l is None:
        raise ValueError("reconcile forward counts must be ints")
    state = "capture window open" if reconcile.get("window_open") else "window_not_open"
    return n, l, f"forward capture, pit_quality first_seen ({state})"


def go_no_go_rows(counts: Mapping[str, Any], reconcile: Mapping[str, Any], history: Mapping[str, Any], *,
                  as_of: date, holidays: frozenset[date], rho: float = DEFAULT_RHO,
                  universe: int = UNIVERSE_TARGET) -> list[dict[str, Any]]:
    """Rows R1 (portal history), R1_forward, D2_BCTC, D2_NQ_HDQT and D4 under the R6.3 counting rules; every N and L
    carries its label (OBSERVED, DERIVED or MISSING). Raises ValueError on a counts summary of another kind or schema
    and on inconsistent counts."""
    _check_counts_kind(counts)
    groups = _group_years(counts)
    rows: list[dict[str, Any]] = []

    entries = [e for e in (history.get("years") or {}).values() if isinstance(e, dict)]
    usable = [count for e in entries if isinstance(e.get("date"), str) and e["date"] >= ECM_START.isoformat()
              and (count := _int(e.get("total_count"))) is not None and count > 0]  # a 0 or negative count is no data
    if usable:
        mean = sum(usable) / len(usable)
        trading = count_trading_days(ECM_START, as_of, holidays)
        n = round(mean * trading * STOCK_SHARE_OF_LISTING / DOCUMENT_TO_EVENT_FACTOR)
        l, note = _clamp_l(n, trading,
                           f"HOSE listing since {ECM_START} (ECM): mean {mean:.1f} documents/day (securitiesType/0) "
                           f"over {len(usable)} history-check days x {STOCK_SHARE_OF_LISTING} stock share x {trading} "
                           f"trading days / {DOCUMENT_TO_EVENT_FACTOR} documents per event (DERIVED, research §4: "
                           "x2-4); every item has a posting time; the IC rule for R1 is main's convention")
        rows.append(_row("R1", "R1", "hose_portal_history", n, l, LABEL_DERIVED, LABEL_DERIVED, note, rho))
    else:
        rows.append(_row("R1", "R1", "hose_portal_history", None, None, LABEL_MISSING, LABEL_MISSING,
                         f"no history-check day with a count on or after {ECM_START}", rho))
    n_fwd, l_fwd, fwd_note = _forward(reconcile)
    rows.append(_row("R1_forward", "R1", "forward_window", n_fwd, l_fwd, LABEL_OBSERVED, LABEL_OBSERVED, fwd_note,
                     rho))
    for group in (TYPE_BCTC, TYPE_NQ_HDQT):
        rows.append(_d2_row(group, counts, groups, as_of=as_of, rho=rho, universe=universe))
    rows.append(_row("D4", "D4", "forward_window", n_fwd, l_fwd, LABEL_OBSERVED, LABEL_OBSERVED,
                     f"{fwd_note}; expected NO_GO until the end of Phase 4 (research §5.2)", rho))
    return rows


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf"
    return f"{value:.4f}" if value < 10 else f"{value:.1f}"


def render_table(rows: Sequence[Mapping[str, Any]], *, rho: float) -> str:
    """The go/no-go markdown table: counts, labels and tiers only."""
    lines = [
        "| Target | Key | Event set | N | L | N_eff | MDE | Tier | MDE floor (tier) | MDE 80% | rho | Labels | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        derived = LABEL_DERIVED if LABEL_DERIVED in (row["n_label"], row["l_label"]) else LABEL_OBSERVED
        if row["tier"] is None:
            derived = LABEL_MISSING
        labels = f"N {row['n_label']}; L {row['l_label']}; N_eff/MDE {derived}; rho ASSUMED"
        floor = f"{_fmt(row['mde_floor'])} ({row['tier_floor']})" if row["tier_floor"] else "n/a"
        cells = [row["target"], row["key"], row["event_set"],
                 "n/a" if row["n_events"] is None else str(row["n_events"]),
                 "n/a" if row["n_distinct_dates"] is None else str(row["n_distinct_dates"]),
                 _fmt(row["n_eff"]), _fmt(row["mde"]), row["tier"] or "n/a", floor, _fmt(row["mde_80"]),
                 f"{row['rho']:.2f}", labels, row["note"]]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        (f"MDE = 2/sqrt(N_eff), N_eff = N/(1+(N/L-1)*rho); GO if MDE <= 0.05, LIMITED if <= 0.10, NO_GO above "
         f"(src/finsec_alpharadar/quant/power_rule.py). rho = {rho:.2f} ASSUMED until Phase 3. MDE floor uses "
         "N_eff = L. MDE 80% (t = 2.8) is a sensitivity line only and never changes the tier."),
        ("Labels: OBSERVED = counted from the inputs; DERIVED = scaled or estimated (research vn-calendar-sessions.md "
         "§4-5); MISSING = no input, no tier."),
    ]
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------------------------------------------


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from err


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    sample = commands.add_parser("sample", help="select 30 disclosures (window backups, then portal history)")
    sample.add_argument("--from", dest="start", type=_iso_date, required=True)
    sample.add_argument("--to", dest="end", type=_iso_date, required=True)
    sample.add_argument("--backup-mirror", type=Path, help="folder holding daily/<d>/ backup sets")
    sample.add_argument("--max-requests", type=int, default=MAX_REQUESTS_CAP)
    sample.add_argument("--max-days", type=int, default=MAX_DAYS_CAP)
    sample.add_argument("--pool-target", type=int, default=PER_TYPE, help="items per stratum pool before stopping")
    sample.add_argument("--max-pages-per-day", type=int, default=3)
    sample.add_argument("--hnx-post-pages", type=int, default=2)
    sample.add_argument("--timeout", type=float, default=90.0)
    sample.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    compare = commands.add_parser("compare", help="match sample.json with vnstock rows; aggregates only")
    compare.add_argument("--sample", type=Path, required=True)
    compare.add_argument("--vnstock-dir", type=Path, required=True)
    compare.add_argument("--symbol-overrides", type=Path, help='{"HNX:<id>": "SYM"} inside the data root')
    compare.add_argument("--out", type=Path, help="default: reconcile-summary.json next to the sample")

    go = commands.add_parser("go-no-go", help="print the go/no-go table")
    go.add_argument("--counts", type=Path, required=True)
    go.add_argument("--reconcile", type=Path, required=True)
    go.add_argument("--history", type=Path, required=True)
    go.add_argument("--as-of", type=_iso_date, help="default: today (ICT)")
    go.add_argument("--rho", type=float, default=DEFAULT_RHO)
    go.add_argument("--universe", type=int, default=UNIVERSE_TARGET, help="research universe size N is scaled to")
    go.add_argument("--out", type=Path, help="also write the table here")
    go.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    args = parser.parse_args(argv)
    if args.command == "sample":
        if args.end < args.start:
            parser.error("--to must not be before --from")
        if not 1 <= args.max_requests <= MAX_REQUESTS_CAP:
            parser.error(f"--max-requests must be 1..{MAX_REQUESTS_CAP}")
        if not 1 <= args.max_days <= MAX_DAYS_CAP:
            parser.error(f"--max-days must be 1..{MAX_DAYS_CAP}")
        if not 1 <= args.pool_target <= 50:
            parser.error("--pool-target must be 1..50")
        if not 1 <= args.max_pages_per_day <= 5 or not 0 <= args.hnx_post_pages <= 5:
            parser.error("--max-pages-per-day must be 1..5 and --hnx-post-pages 0..5")
        if args.timeout < MIN_TIMEOUT_S:
            parser.error(f"--timeout must be at least {MIN_TIMEOUT_S:.0f} s")
    if args.command == "go-no-go" and args.universe < 1:
        parser.error("--universe must be at least 1")
    return args


def _holidays(path: Path) -> frozenset[date]:
    return frozenset(holiday.day for holiday in load_config_file(path).holidays)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ValueError(f"cannot read {path.name}: {type(err).__name__}") from err


def _inside_data_root(path: Path, env: Mapping[str, str]) -> Path:
    root = resolve_data_root(env).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{path.name} must live inside the data root (FINSEC_DATA_ROOT)")
    return resolved


def _cmd_sample(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    import time as _time

    holidays = _holidays(args.config)
    now = utc_now()
    run_dir = spike_dir("vn-reconcile", env=env) / now.strftime("%H%M%SZ")
    run_dir.mkdir()

    def fetcher(method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> FetchResult:
        return fetch(url, method=method, headers=headers, data=data, timeout=args.timeout)

    run_sample(run_dir, day_from=args.start, day_to=args.end, holidays=holidays, fetcher=fetcher,
               sleeper=_time.sleep, monotonic=_time.monotonic, backup_mirror=args.backup_mirror,
               max_requests=args.max_requests, max_days=args.max_days, pool_target=args.pool_target,
               max_pages_per_day=args.max_pages_per_day, hnx_post_pages=args.hnx_post_pages, now=now)
    print(f"vn_reconcile: sample {run_dir / 'sample.json'}")
    return 0


def _cmd_compare(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    overrides: dict[tuple[str, str], str] = {}
    if args.symbol_overrides:
        overrides = parse_symbol_overrides(_read_json(_inside_data_root(args.symbol_overrides, env)))
    sample = _read_json(args.sample)
    items = sample.get("items") if isinstance(sample, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{args.sample.name} is not a vn_reconcile sample")
    forward = sample.get("forward")
    if forward is not None and not isinstance(forward, dict):
        raise ValueError(f"{args.sample.name}: forward counts must be an object")
    rows, applied = apply_symbol_overrides(items, overrides)
    summary = reconcile_summary(rows, load_vnstock_rows(args.vnstock_dir), overrides_applied=applied,
                                window_open=bool(sample.get("window_open")), forward=forward)
    out = args.out or args.sample.with_name("reconcile-summary.json")
    write_json(out, summary)
    print_reconcile_summary(summary)
    print(f"vn_reconcile: summary {out}")
    return 0


def _cmd_go_no_go(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    del env  # go-no-go reads only the three summaries and the config
    counts, reconcile, history = _read_json(args.counts), _read_json(args.reconcile), _read_json(args.history)
    if not all(isinstance(doc, dict) for doc in (counts, reconcile, history)):
        raise ValueError("counts, reconcile and history summaries must be JSON objects")
    as_of = args.as_of or utc_now().astimezone(ICT).date()
    rows = go_no_go_rows(counts, reconcile, history, as_of=as_of, holidays=_holidays(args.config), rho=args.rho,
                         universe=args.universe)
    table = render_table(rows, rho=args.rho)
    if args.out:
        args.out.write_text(table + "\n", encoding="utf-8", newline="\n")
    print(table)
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = parse_args(argv)
    handlers = {"sample": _cmd_sample, "compare": _cmd_compare, "go-no-go": _cmd_go_no_go}
    try:
        return handlers[args.command](args, os.environ if env is None else env)
    except (DataRootError, SpikeEnvError, CaptureConfigError, BackupError, ValueError, OSError) as err:
        print(f"vn_reconcile: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("vn_reconcile: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
