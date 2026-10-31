"""Count VN corporate events from vnstock for the go/no-go power rule (plan 261022-1147, phase 6, R6.2).

Self-contained: stdlib only at import time; vnstock is imported lazily inside `VnstockClient`, after the telemetry
variables are set exactly as `vnstock_probe.py` sets them, and the script never imports `finsec_alpharadar` (it is
copied alone into the probe kit, outside the clone). Always run it with `python -I` under the probe user (D6):

  python -I vn_counts.py universe --out <universe.txt> [--groups VN100,HNX30] [--extra-symbols-file <txt>]
  python -I vn_counts.py events --symbols-file <universe.txt> --rate <calls/min> --out <dir> [--status-every 50]
  python -I vn_counts.py merge --inputs <dir> [<dir> ...] --universe-file <universe.txt> --out <dir>

`universe` writes today's VN100 + HNX30 members (`Reference().index.members(group)`, VERIFIED vendor docs; the group
names are [ASSUMED]) plus the extra symbols, one per line, and `<universe.txt>.json` with what was obtained and what
failed. Membership is today's, not point-in-time: accepted for a counts estimate and stated in both outputs.

`events` calls `Reference().company(symbol).events(source="vci")` once per symbol at most `--rate` calls per minute and
writes:
- `raw/<SYMBOL>.json`: one JSON list of row dicts per symbol (licensed vendor data: stays under --out, outside the
  clone, never committed). Rows come from pandas `to_json(orient="records", date_format="iso")`, which writes UTC
  strings (`2026-09-15T17:00:00.000Z` is ICT midnight on 2026-09-16); every whole-value UTC timestamp is rewritten to
  the same instant with an explicit ICT offset (`2026-09-16T00:00:00+07:00`), so its first 10 characters are the ICT
  date. Other values are unchanged.
- `vnstock-status-before.txt` / `vnstock-status-after.txt`: `vnstock status|env|doctor`, runs of 24+ token characters
  redacted (R1.5, as in `vnstock_probe.py`).
- `counts-summary.json`: aggregates only (counts, shares, dates, vendor type codes, symbols); no row text.

Stops early, still writing the summary and the status after, and exits 1 when: the quota left (from the quota line
of `vnstock doctor`; the 2026-10-26 probe saw used % of a 24-hour and a 30-day quota, but the line's exact wording was
not stored, so the parser is [ASSUMED] and an unreadable line is noted, not guessed) is below 30%, before the run or at
a check every `--status-every` calls; a warning or the frame's attrs carry a quota or
truncation word (the rows are kept); or any vendor error other than an unknown symbol (counted, not fatal). A source
outage (VNSTOCK_SOURCE_UNAVAILABLE) is first retried after 30 s and 90 s; each retry is listed in the summary.

`merge` makes no vendor call: it combines disjoint `events` outputs (a rerun of the symbols an early stop skipped)
over one universe file into one `counts-summary.json`, re-aggregated from the raw rows, plus a copy of `raw/`, and
lists its inputs in `merged_from`. It refuses a symbol fetched twice, a symbol outside the universe, inputs of
different vnstock versions, a raw/ count that does not match symbols.ok, and a fetched or failed symbol outside the
universe; a symbol that failed in several runs counts once. It exits 1 when the union leaves a universe symbol
unasked, or when an input stopped on a quota warning (its rows stay, possibly truncated, so the merge keeps that stop).
A retry waits at least the --rate interval, which counts from the last attempt.
A vendor error message is never printed or written (it can quote row text): only its type, vendor code, sha256 and
length. Exit 0 = every symbol asked; 2 = usage or environment error; 4 = an output path inside a git work tree (a
`.git` directory or file in it or any parent; nothing is written). Printed output, this docstring included (it is the --help text), is ASCII only:
`-I` ignores PYTHONUTF8 and PYTHONIOENCODING, so a redirected Windows console encodes as cp1252.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import unicodedata
import warnings
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any

# Set before `import vnstock` (research vnstock-gen5.md §4, §7); copied from vnstock_probe.py.
TELEMETRY_ENV = {
    "DO_NOT_TRACK": "1",
    "VNSTOCK_TELEMETRY": "0",
    "VNSTOCK_DISABLE_AGENT_SETUP": "1",
    "VNSTOCK_DISABLE_GLOBAL_AGENT": "1",
}
# R1.5 step 2 (copied from vnstock_probe.py): any run of 24+ token characters is replaced before CLI output is kept.
TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")
REDACTED = "<redacted-token>"
CLI_TIMEOUT_S = 120
STATUS_COMMANDS = ("status", "env", "doctor")

VENDOR_CALL = "Reference().company(symbol).events(source='vci')"
VENDOR_SOURCE = "vci"  # "kbs" events are broken (VNSTOCK_KBS_COMPANY_SHAPE, spike report §1)
# Vietnam has no DST: a fixed UTC+7 offset is Asia/Ho_Chi_Minh and needs no tzdata.
ICT = timezone(timedelta(hours=7), "ICT")
SYMBOL = re.compile(r"^[A-Z0-9]{2,10}$")

# Spike report §3 row 1: 60 calls/min is the safe rate (50% of the fastest clean cadence, 10-call burst only).
MAX_RATE_PER_MIN = 60.0
QUOTA_STOP_REMAINING_PCT = 30.0
DEFAULT_STATUS_EVERY = 50
# Per-symbol errors: counted, not fatal. [ASSUMED] for events(): the vendor documents this code for ohlcv() (as
# configs/capture.toml `symbol_error_codes`).
SYMBOL_ERROR_CODES = frozenset({"VNSTOCK_INPUT_SYMBOL_UNKNOWN"})
# A source outage is retried after each wait in turn, then stops the run (OBSERVED 2026-10-28: one NetworkError
# VNSTOCK_SOURCE_UNAVAILABLE stopped a 143-symbol run after 30 symbols; that it is transient is [ASSUMED]).
TRANSIENT_ERROR_CODES = frozenset({"VNSTOCK_SOURCE_UNAVAILABLE"})
TRANSIENT_BACKOFF_S = (30.0, 90.0)
# Case-insensitive words of a quota/truncation warning or attrs note (configs/capture.toml `quota_warning_patterns`;
# the attrs of the 2026-10-26 probe rows match none of them, OBSERVED 2026-10-28).
QUOTA_WARNING_WORDS = ("truncat", "quota", "giới hạn", "hạn mức", "cắt")
QUOTA_LINE_WORDS = ("hạn mức", "quota")
QUOTA_USED_WORDS = ("dùng", "used")
QUOTA_LEFT_WORDS = ("còn", "remaining", "left")
PERCENT = re.compile(r"(<\s*)?(\d+(?:[.,]\d+)?)\s*%")

UNIVERSE_GROUPS = ("VN100", "HNX30")  # [ASSUMED] group names of Reference().index.members()
UNIVERSE_NOTE = (
    "today's index membership, not point-in-time: survivorship is not handled; accepted for a counts estimate only"
)

# --------------------------------------------------------------------------------------------------- event groups
#
# | group    | vci event_code (OBSERVED 2026-10-26, 5 symbols)  | vci category              |
# |----------|--------------------------------------------------|---------------------------|
# | INSIDER  | DDINS, DDIND, DDRP; any DD* (vendor docs: DD*)   | MAJOR_SHAREHOLDER_TRADING |
# | DIVIDEND | DIV, ISS                                         | DIVIDEND                  |
# | DHDCD    | AGME, AGMR, EGME                                 | SHAREHOLDER_MEETING       |
# | OTHER    | AIS, OTHE, NLIS; every unknown code and category | OTHER                     |
# | BCTC     | none seen (title keywords only)                  | none seen                 |
# | NQ_HDQT  | none seen (title keywords only)                  | none seen                 |
#
# The code wins over the category; an unknown code falls back to its category, then to OTHER. `codes_seen` in the
# summary lists every (code, category) met with this code/category group, so an unmapped code is visible.
#
# Title fallback (`group_of_row`, logic review F1): a row is INSIDER when its code is DD* or its category is
# MAJOR_SHAREHOLDER_TRADING; otherwise the title keyword rules below (a COPY of vn_reconcile.py `TITLE_KEYWORDS`,
# `normalize_text` and `TYPE_PRIORITY`: this script runs alone under `python -I` and cannot import it) are read on
# event_title_vi, then event_name_vi, and the first match decides; with no match the code/category group stands. The
# fallback applies to OTHER and ALSO to DIVIDEND and DHDCD: vn_reconcile.vnstock_row_type reads the titles of every
# non-insider row, so a dividend board resolution is NQ_HDQT in both scripts and the go-no-go counts agree with what
# `compare` matched. `codes_retyped_by_title` in the summary counts the rows the titles moved, per (code, category).
GROUPS = ("BCTC", "NQ_HDQT", "DHDCD", "DIVIDEND", "INSIDER", "OTHER")
GROUP_BY_CODE = {
    "DDINS": "INSIDER",
    "DDIND": "INSIDER",
    "DDRP": "INSIDER",
    "DIV": "DIVIDEND",
    "ISS": "DIVIDEND",
    "AGME": "DHDCD",
    "AGMR": "DHDCD",
    "EGME": "DHDCD",
    "AIS": "OTHER",
    "OTHE": "OTHER",
    "NLIS": "OTHER",
}
GROUP_BY_CODE_PREFIX = {"DD": "INSIDER"}
GROUP_BY_CATEGORY = {
    "MAJOR_SHAREHOLDER_TRADING": "INSIDER",
    "DIVIDEND": "DIVIDEND",
    "SHAREHOLDER_MEETING": "DHDCD",
}
DEFAULT_GROUP = "OTHER"
GROUP_NOTE = (
    "BCTC and NQ_HDQT have no vci events code or category: none was seen in the 2026-10-26 probe (11 codes over "
    "FPT, VNM, HPG, PVS, ACV), so they are counted only through the title fallback (title_fallback); when no title "
    "matches, their counts are 0 and N(BCTC) must be DERIVED (research vn-calendar-sessions.md §4); an unseen code "
    "goes to its category, else OTHER, and is listed in codes_seen"
)
POOLS = {"BCTC+NQ_HDQT": ("BCTC", "NQ_HDQT"), "ALL": GROUPS}

# Copied from vn_reconcile.py (phase file R6.3 keyword rules, matched on NFC + casefold text); tests/test_vn_counts.py
# checks that the two copies stay equal and type the same rows the same way.
TITLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BCTC": ("báo cáo tài chính", "bctc"),
    "NQ_HDQT": ("nghị quyết hđqt", "nghị quyết hội đồng quản trị"),
    "INSIDER": ("đăng ký giao dịch", "người nội bộ"),
}
TITLE_PRIORITY = ("INSIDER", "NQ_HDQT", "BCTC")  # a title matching several types takes the first here
TITLE_COLUMNS = ("event_title_vi", "event_name_vi")
INSIDER_CODE_PREFIX = "DD"
INSIDER_CATEGORY = "MAJOR_SHAREHOLDER_TRADING"
TITLE_FALLBACK_GROUPS = ("OTHER", "DIVIDEND", "DHDCD")  # every code/category group but INSIDER
TITLE_FALLBACK_NOTE = (
    "INSIDER when event_code is DD* or category is MAJOR_SHAREHOLDER_TRADING; else the first title keyword match "
    "(priority INSIDER, NQ_HDQT, BCTC) on event_title_vi then event_name_vi, NFC + casefold; else the code/category "
    "group. Copied from vn_reconcile.py vnstock_row_type/classify_title so both scripts type a row the same way; "
    "DIVIDEND and DHDCD rows fall back too, for that parity"
)
_WS = re.compile(r"\s+")

_UTC_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]00:?00)$")


def group_of(event_code: Any, category: Any) -> str:
    """The event group of a vci events row (table above)."""
    code = event_code.strip().upper() if isinstance(event_code, str) else ""
    if code in GROUP_BY_CODE:
        return GROUP_BY_CODE[code]
    for prefix, group in GROUP_BY_CODE_PREFIX.items():
        if code.startswith(prefix):
            return group
    category_key = category.strip().upper() if isinstance(category, str) else ""
    return GROUP_BY_CATEGORY.get(category_key, DEFAULT_GROUP)


def normalize_text(text: str) -> str:
    """NFC, casefold, NFC again, whitespace runs collapsed to one space (copied from vn_reconcile.py)."""
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    return _WS.sub(" ", folded).strip()


_NORMALIZED_KEYWORDS = {kind: tuple(normalize_text(word) for word in words) for kind, words in TITLE_KEYWORDS.items()}


def classify_title(title: str) -> str | None:
    """The event type of a title by the keyword rules, INSIDER before NQ_HDQT before BCTC; None when none matches."""
    text = normalize_text(title or "")
    for kind in TITLE_PRIORITY:
        if any(keyword in text for keyword in _NORMALIZED_KEYWORDS[kind]):
            return kind
    return None


def group_of_row(row: Mapping[str, Any]) -> str:
    """The group of a vci events row: INSIDER by code or category, else the title fallback, else `group_of`."""
    code = row.get("event_code")
    category = row.get("category")
    code_key = code.strip().upper() if isinstance(code, str) else ""
    category_key = category.strip().upper() if isinstance(category, str) else ""
    if code_key.startswith(INSIDER_CODE_PREFIX) or category_key == INSIDER_CATEGORY:
        return "INSIDER"
    code_group = group_of(code, category)
    if code_group in TITLE_FALLBACK_GROUPS:
        for column in TITLE_COLUMNS:
            value = row.get(column)
            if isinstance(value, str) and (kind := classify_title(value)):
                return kind
    return code_group


def group_map_table() -> dict[str, Any]:
    return {
        "by_code": dict(GROUP_BY_CODE),
        "by_code_prefix": dict(GROUP_BY_CODE_PREFIX),
        "by_category": dict(GROUP_BY_CATEGORY),
        "default": DEFAULT_GROUP,
        "groups": list(GROUPS),
        "note": GROUP_NOTE,
        "title_fallback": {
            "priority": list(TITLE_PRIORITY),
            "keywords": {kind: list(words) for kind, words in TITLE_KEYWORDS.items()},
            "columns": list(TITLE_COLUMNS),
            "applies_to_code_groups": list(TITLE_FALLBACK_GROUPS),
            "note": TITLE_FALLBACK_NOTE,
        },
    }


# --------------------------------------------------------------------------------------------------- time basis


def _parse_moment(text: str) -> datetime | None:
    """An ISO timestamp (a `Z` suffix and up to 9 fractional digits accepted); naive values are read as ICT."""
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", normalized)  # Python 3.11 reads at most 6 fractional digits
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return moment.replace(tzinfo=ICT) if moment.tzinfo is None else moment


def parse_public_date(value: Any) -> tuple[date, bool] | None:
    """(ICT calendar date, has a time of day other than 00:00:00 ICT), or None when the value is not an ISO string."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10:
        try:
            return date.fromisoformat(text), False
        except ValueError:
            return None
    moment = _parse_moment(text)
    if moment is None:
        return None
    local = moment.astimezone(ICT)
    return local.date(), local.time() != clock_time(0)


def ict_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of a row with every whole-value UTC timestamp rewritten to the same instant at ICT (+07:00)."""
    converted: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str) and _UTC_STAMP.match(value.strip()):
            moment = _parse_moment(value)
            converted[key] = moment.astimezone(ICT).isoformat() if moment is not None else value
        else:
            converted[key] = value
    return converted


# --------------------------------------------------------------------------------------------------- aggregation

_Doc = tuple[str, str, date, bool]  # (symbol, event_code, ICT date, has time of day)


def _stats(docs: list[_Doc]) -> dict[str, Any]:
    events: dict[tuple[str, str, date], bool] = {}
    for symbol, code, day, intraday in docs:
        key = (symbol, code, day)
        events[key] = events.get(key, False) or intraday
    per_date = Counter(day for _, _, day in events)
    intraday_events = [key for key, intraday in events.items() if intraday]
    return {
        "n_docs": len(docs),
        "n_events": len(events),
        "n_issuers": len({symbol for symbol, _, _, _ in docs}),
        "n_distinct_dates": len(per_date),
        "max_events_per_date": max(per_date.values()) if per_date else 0,
        "share_with_intraday_time": (len(intraday_events) / len(events)) if events else None,
        "n_events_with_intraday_time": len(intraday_events),
        "n_distinct_dates_with_intraday_time": len({day for _, _, day in intraday_events}),
    }


def _date_range(docs: list[_Doc]) -> dict[str, Any]:
    days = [day for _, _, day, _ in docs]
    return {
        "oldest_date": min(days).isoformat() if days else None,
        "newest_date": max(days).isoformat() if days else None,
    }


def aggregate(rows_by_symbol: Mapping[str, Iterable[Mapping[str, Any]]]) -> dict[str, Any]:
    """Counts per (group, ICT year), per group, per pool, and per vendor (code, category); no row text.

    An event is `(symbol, event_code, ICT date of public_date)`; a doc is one vendor row. A row whose `public_date` is
    missing or unreadable is counted in `codes_seen` and `n_docs_without_public_date` only.
    """
    docs_by_group: dict[str, list[_Doc]] = defaultdict(list)
    undated: Counter[str] = Counter()
    codes: Counter[tuple[str | None, str | None]] = Counter()
    retyped: Counter[tuple[str | None, str | None, str, str]] = Counter()
    for symbol, rows in rows_by_symbol.items():
        for row in rows:
            code = row.get("event_code")
            category = row.get("category")
            code_text = str(code) if code is not None else None
            category_text = str(category) if category is not None else None
            code_group = group_of(code_text, category_text)
            group = group_of_row({**row, "event_code": code_text, "category": category_text})
            codes[(code_text, category_text)] += 1
            if group != code_group:
                retyped[(code_text, category_text, code_group, group)] += 1
            parsed = parse_public_date(row.get("public_date"))
            if parsed is None:
                undated[group] += 1
                continue
            docs_by_group[group].append((symbol, code_text or "", parsed[0], parsed[1]))

    by_group_year: list[dict[str, Any]] = []
    by_group: list[dict[str, Any]] = []
    for group in GROUPS:
        docs = docs_by_group.get(group, [])
        years = sorted({day.year for _, _, day, _ in docs})
        for year in years:
            year_docs = [doc for doc in docs if doc[2].year == year]
            by_group_year.append({"group": group, "year": year, **_stats(year_docs)})
        by_group.append(
            {
                "group": group,
                **_stats(docs),
                **_date_range(docs),
                "years": years,
                "n_docs_without_public_date": undated.get(group, 0),
            }
        )
    pools = {}
    for name, members in POOLS.items():
        docs = [doc for group in members for doc in docs_by_group.get(group, [])]
        pools[name] = {"groups": list(members), **_stats(docs), **_date_range(docs)}
    codes_seen = [
        {"event_code": code, "category": category, "group": group_of(code, category), "n_docs": count}
        for (code, category), count in sorted(codes.items(), key=lambda item: (-item[1], str(item[0][0]), str(item[0][1])))
    ]
    # Rows moved out of their code/category group by the title fallback (or, for a non-DD code with the insider
    # category, by the INSIDER-category override): counts and vendor codes only, never a title.
    codes_retyped_by_title = [
        {"event_code": code, "category": category, "code_group": code_group, "group": group, "n_docs": count}
        for (code, category, code_group, group), count in sorted(
            retyped.items(), key=lambda item: (-item[1], str(item[0][0]), str(item[0][1]), item[0][3])
        )
    ]
    every_doc = [doc for docs in docs_by_group.values() for doc in docs]
    return {
        "by_group_year": by_group_year,
        "by_group": by_group,
        "pools": pools,
        "codes_seen": codes_seen,
        "codes_retyped_by_title": codes_retyped_by_title,
        **_date_range(every_doc),
    }


# --------------------------------------------------------------------------------------------------- vnstock CLI


def redact(text: str) -> str:
    return TOKEN.sub(REDACTED, text)


def utc_now() -> datetime:
    return datetime.now(UTC)


def say(text: str, *, error: bool = False) -> None:
    """Print ASCII only: under `python -I` the console encoding cannot be forced to UTF-8."""
    print(text.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr if error else sys.stdout, flush=True)


def vnstock_executable() -> Path:
    """The `vnstock` console script installed next to this interpreter (the probe venv's Scripts directory)."""
    return Path(sys.executable).parent / ("vnstock.exe" if os.name == "nt" else "vnstock")


def run_vnstock_cli(subcommand: str) -> dict[str, Any]:
    """`vnstock <subcommand>` with telemetry off; stdout and stderr are redacted (copied from vnstock_probe.py)."""
    executable = vnstock_executable()
    entry: dict[str, Any] = {"command": f"vnstock {subcommand}"}
    if not executable.is_file():
        entry["error"] = f"{executable.name} not found next to {Path(sys.executable).name}"
        return entry
    env = {**os.environ, **TELEMETRY_ENV, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [str(executable), subcommand],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        entry["error"] = f"timed out after {CLI_TIMEOUT_S} s"
        return entry
    except OSError as err:
        entry["error"] = f"could not run: {type(err).__name__}"
        return entry
    entry.update(
        exit_code=result.returncode,
        seconds=round(time.perf_counter() - started, 3),
        stdout=redact(result.stdout),
        stderr=redact(result.stderr),
    )
    return entry


def cli_report(entries: list[dict[str, Any]]) -> str:
    blocks = []
    for entry in entries:
        header = f"$ {entry['command']}"
        if "error" in entry:
            blocks.append(f"{header}\n[error] {entry['error']}\n")
            continue
        timing = f"(exit {entry.get('exit_code')}, {entry.get('seconds')} s)"
        blocks.append(f"{header}   {timing}\n{entry.get('stdout') or ''}\n[stderr]\n{entry.get('stderr') or ''}\n")
    return redact("\n".join(blocks))


def quota_remaining_pct(text: str) -> float | None:
    """Quota left in %, from the "Hạn mức" (quota) line: 100 - the largest used %, or the smallest remaining %.

    "<1%" reads as 1. None when no quota line with a percentage is found (e.g. an English locale: wording unknown).
    """
    remaining: list[float] = []
    for line in unicodedata.normalize("NFC", text).splitlines():
        lowered = line.casefold()
        if not any(word in lowered for word in QUOTA_LINE_WORDS):
            continue
        values = [float(number.replace(",", ".")) for _, number in PERCENT.findall(line)]
        if not values:
            continue
        if any(word in lowered for word in QUOTA_USED_WORDS):
            remaining.append(100.0 - max(values))
        elif any(word in lowered for word in QUOTA_LEFT_WORDS):
            remaining.append(min(values))
    return min(remaining) if remaining else None


def _status_text(entries: list[dict[str, Any]]) -> str:
    return "\n".join(str(entry.get("stdout") or "") for entry in entries)


def _quota_words(text: str) -> bool:
    lowered = unicodedata.normalize("NFC", text).casefold()
    return any(word in lowered for word in QUOTA_WARNING_WORDS)


# --------------------------------------------------------------------------------------------------- vendor


class VnstockClient:
    """The real vendor calls; vnstock is imported on first use, after the telemetry variables are set."""

    def __init__(self) -> None:
        self._reference: Any = None
        self.version: str | None = None

    def _ref(self) -> Any:
        if self._reference is None:
            os.environ.update(TELEMETRY_ENV)
            module = importlib.import_module("vnstock")
            version = getattr(module, "__version__", None)
            self.version = None if version is None else str(version)
            # `from vnstock import Reference` is VERIFIED vendor docs (tham-chieu-reference, research §6).
            self._reference = module.Reference()
        return self._reference

    def events(self, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        frame = self._ref().company(symbol).events(source=VENDOR_SOURCE)
        rows = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
        attrs = json.loads(json.dumps(dict(getattr(frame, "attrs", {}) or {}), ensure_ascii=False, default=str))
        return rows, attrs

    def index_members(self, group: str) -> list[str]:
        frame = self._ref().index.members(group)
        column = "symbol" if "symbol" in frame.columns else frame.columns[0]
        # Only strings: a missing cell is a float NaN, and str(NaN) = "nan" would pass SYMBOL after upper-casing.
        return [value for value in frame[column].tolist() if isinstance(value, str)]


# --------------------------------------------------------------------------------------------------- files


def write_new_json(path: Path, payload: Any) -> None:
    """Write JSON to a file that must not exist yet (evidence is never overwritten)."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_new_text(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def normalize_symbol(value: str) -> str | None:
    symbol = value.strip().upper()
    return symbol if SYMBOL.match(symbol) else None


def read_symbols_file(path: Path) -> list[str]:
    """One symbol per line, upper-cased, first occurrence kept; `#` comments and blank lines skipped; strict."""
    symbols: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        symbol = normalize_symbol(stripped)
        if symbol is None:
            raise ValueError(f"{path}: line {number} is not a symbol ({SYMBOL.pattern})")
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError(f"{path}: no symbol")
    return symbols


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------------- events


def _check_rate(rate_per_min: float) -> float:
    if not (math.isfinite(rate_per_min) and 0 < rate_per_min <= MAX_RATE_PER_MIN):
        raise ValueError(f"--rate must be > 0 and <= {MAX_RATE_PER_MIN:g} calls/min (spike report §3 row 1)")
    return 60.0 / rate_per_min


def _stop(reason: str, *, symbol: str | None = None, kind: str | None = None, code: str | None = None,
          remaining_pct: float | None = None) -> dict[str, Any]:
    return {"reason": reason, "symbol": symbol, "type": kind, "code": code, "remaining_pct": remaining_pct}


def message_fingerprint(err: BaseException) -> tuple[str, int]:
    """(sha256 hex, length) of an exception message: a vendor or pandas message can quote row text (a title, a
    person name), which may never be printed or written outside raw/ (brief resolution 5), so only this is kept."""
    text = str(err)
    return hashlib.sha256(text.encode("utf-8", "backslashreplace")).hexdigest(), len(text)


class WorkTreeRefused(Exception):
    """An output path inside a git work tree (licensed vendor rows must never land in a clone); main exits 4."""


def refuse_inside_work_tree(path: Path) -> None:
    """Raise WorkTreeRefused when `path` (resolved, it need not exist) or any ancestor holds a `.git` entry.

    A `.git` directory (a clone) and a `.git` file (a worktree or submodule) both count. Stdlib only: this script
    cannot import finsec_alpharadar.data_root, so there is no `git check-ignore` escape here; choose a path outside.
    """
    resolved = Path(path).resolve()
    for folder in (resolved, *resolved.parents):
        if (folder / ".git").exists():
            raise WorkTreeRefused(f"{resolved} is inside the git work tree at {folder}; write outside the clone")


COVERAGE_NOTE = (
    "symbols_fetched = symbols.ok, the symbols whose events call returned (an empty result counts as zero rows); scale "
    "the counts by symbols.ok, never by universe.size, which is the number requested and overstates coverage after an "
    "early stop"
)
QUOTA_SOURCE = (
    "the quota line of `vnstock doctor`: used % over 24 h and 30 days (OBSERVED 2026-10-26, spike report §3 row 1); the "
    "line wording the parser expects is [ASSUMED] (not stored)"
)


def build_summary(
    rows_by_symbol: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    started_at: str,
    finished_at: str,
    library_version: str | None,
    python: str | None,
    isolated_mode: bool,
    rate_per_min: float | None,
    status_every: int | None,
    calls_made: int,
    requested: int,
    symbols_file_sha256: str | None,
    ok: int,
    empty: int,
    failed: list[dict[str, Any]],
    skipped: int,
    stop: dict[str, Any] | None,
    retries: list[dict[str, Any]],
    quota_before: float | None,
    quota_after: float | None,
    quota_checks: list[dict[str, Any]],
    quota_notes: list[str],
    warnings_seen: Mapping[str, int],
) -> dict[str, Any]:
    """counts-summary.json of one `events` run or of a `merge`: the same keys, so go-no-go reads either."""
    counts = aggregate(rows_by_symbol)
    bctc = next(entry for entry in counts["by_group"] if entry["group"] == "BCTC")
    return {
        "kind": "vn_counts_summary",
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "vendor_call": VENDOR_CALL,
        "library_version": library_version,
        "python": python,
        "isolated_mode": isolated_mode,
        "telemetry_env": TELEMETRY_ENV,
        "rate_per_min": rate_per_min,
        "status_every": status_every,
        "calls_made": calls_made,
        "universe": {"size": requested, "symbols_file_sha256": symbols_file_sha256, "note": UNIVERSE_NOTE},
        "symbols": {
            "requested": requested,
            "ok": ok,
            "empty": empty,
            "failed": len(failed),
            "skipped": skipped,
            "failed_list": failed,
        },
        "stop": stop,
        "retries": retries,
        "coverage": {
            "symbols_requested": requested,
            "symbols_fetched": ok,
            "complete": stop is None,
            "stop_reason": None if stop is None else stop["reason"],
            "scale_by": "symbols.ok",
            "note": COVERAGE_NOTE,
        },
        "quota": {
            "threshold_remaining_pct": QUOTA_STOP_REMAINING_PCT,
            "source": QUOTA_SOURCE,
            "before_remaining_pct": quota_before,
            "after_remaining_pct": quota_after,
            "checks": quota_checks,
            "notes": quota_notes,
        },
        "warnings_seen": dict(warnings_seen),
        "time_basis": (
            "public_date read at Asia/Ho_Chi_Minh (fixed UTC+7, no DST): year and date are ICT; a doc has an intraday "
            "time when its ICT time of day is not 00:00:00; raw rows carry +07:00 offsets"
        ),
        "event_key": "(symbol, event_code, ICT date of public_date)",
        "columns_note": (
            "n_docs = vendor rows with a readable public_date; n_events = distinct event keys; "
            "share_with_intraday_time = events with at least one intraday doc / n_events (null when n_events = 0)"
        ),
        "group_map": group_map_table(),
        **counts,
        "median_lag_vs_portal_min": None,
        "median_lag_note": "not computable from vnstock alone; vn_reconcile.py compare measures the lag against the portals",
        "sanity": {
            "bctc_events_per_issuer_year": (
                bctc["n_events"] / ok / len(bctc["years"]) if bctc["n_events"] and ok else None
            ),
            "denominator": "symbols.ok",
            "expected": "about 4 quarterly BCTC per issuer and year (research vn-calendar-sessions.md §4)",
            "note": None if bctc["n_events"] else "no BCTC event in vci events: N(BCTC) is DERIVED in go-no-go",
        },
    }


def run_events(
    symbols: list[str],
    out: Path,
    *,
    rate_per_min: float,
    call_events: Callable[[str], tuple[list[dict[str, Any]], dict[str, Any]]],
    status_runner: Callable[[str], dict[str, Any]],
    sleeper: Callable[[float], None],
    monotonic: Callable[[], float],
    now: Callable[[], datetime],
    status_every: int = DEFAULT_STATUS_EVERY,
    library_version: Callable[[], str | None] | None = None,
    symbols_file_sha256: str | None = None,
) -> int:
    """Fetch, store and count; returns 0 when every symbol was asked, 1 on an early stop (the summary says why)."""
    interval_s = _check_rate(rate_per_min)
    if status_every < 0:
        raise ValueError("--status-every must be >= 0")
    out = Path(out)
    refuse_inside_work_tree(out)
    raw_dir = out / "raw"
    summary_path = out / "counts-summary.json"
    if summary_path.exists() or raw_dir.exists():
        raise FileExistsError(f"{out} already holds counts evidence; choose another --out")
    raw_dir.mkdir(parents=True)
    started_at = now()

    before = [status_runner(command) for command in STATUS_COMMANDS]
    write_new_text(out / "vnstock-status-before.txt", f"# vnstock CLI before, {started_at.isoformat()}\n\n{cli_report(before)}")
    remaining_before = quota_remaining_pct(_status_text(before))
    quota_notes: list[str] = []
    if remaining_before is None:
        quota_notes.append("quota_unreadable: no quota line in vnstock doctor; only warnings and attrs can stop the run")
    checks: list[dict[str, Any]] = []

    stop: dict[str, Any] | None = None
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    failed: list[dict[str, Any]] = []
    retries: list[dict[str, Any]] = []
    warnings_seen: Counter[str] = Counter()
    ok = empty = calls_made = asked = 0
    last_start: float | None = None
    if remaining_before is not None and remaining_before < QUOTA_STOP_REMAINING_PCT:
        stop = _stop("quota_below_threshold", remaining_pct=remaining_before)

    checked_at_calls = 0
    for index, symbol in enumerate(symbols if stop is None else []):
        # Due once status_every calls have passed since the last check: retries can step over an exact multiple.
        if status_every and calls_made - checked_at_calls >= status_every:
            checked_at_calls = calls_made
            remaining = quota_remaining_pct(_status_text([status_runner("doctor")]))
            checks.append({"after_calls": calls_made, "remaining_pct": remaining})
            if remaining is not None and remaining < QUOTA_STOP_REMAINING_PCT:
                stop = _stop("quota_below_threshold", remaining_pct=remaining)
                break
        if last_start is not None:
            wait_s = interval_s - (monotonic() - last_start)
            if wait_s > 0:
                sleeper(wait_s)
        asked += 1
        attempt = 0
        while True:
            last_start = monotonic()  # the rate counts from the last attempt, retries included
            calls_made += 1
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    rows, attrs = call_events(symbol)
                err = None
            except ImportError as import_err:
                stop = _stop("vendor_unavailable", symbol=symbol, kind=type(import_err).__name__)
                break
            except Exception as vendor_err:  # noqa: BLE001 - counted per symbol, retried, or stops the run
                err = vendor_err
            code = None if err is None else getattr(err, "code", None)
            code_text = None if code is None else str(code)
            if err is None or code_text not in TRANSIENT_ERROR_CODES or attempt >= len(TRANSIENT_BACKOFF_S):
                break
            attempt += 1
            retries.append({"symbol": symbol, "attempt": attempt, "type": type(err).__name__, "code": code_text})
            wait_s = max(TRANSIENT_BACKOFF_S[attempt - 1], interval_s - (monotonic() - last_start))
            say(f"{symbol}: {type(err).__name__} {code_text}, retry {attempt}/{len(TRANSIENT_BACKOFF_S)} in {wait_s:g} s")
            sleeper(wait_s)
        if stop is not None:
            break
        if err is not None:
            if code_text in SYMBOL_ERROR_CODES:
                failed.append({"symbol": symbol, "type": type(err).__name__, "code": code_text})
                say(f"{index + 1}/{len(symbols)} {symbol}: {type(err).__name__} {code_text} (counted, not fatal)")
                continue
            fingerprint, length = message_fingerprint(err)
            stop = _stop("vendor_error", symbol=symbol, kind=type(err).__name__, code=code_text)
            stop.update(message_sha256=fingerprint, message_length=length)
            say(f"{symbol}: vendor error {type(err).__name__} {code_text} (message withheld: sha256 {fingerprint}, "
                f"{length} chars)", error=True)
            break
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            stop = _stop("invalid_payload", symbol=symbol, kind=type(rows).__name__)
            break
        converted = [ict_row(row) for row in rows]
        write_new_json(raw_dir / f"{symbol}.json", converted)
        rows_by_symbol[symbol] = converted
        ok += 1
        empty += 0 if converted else 1
        quota_hit: str | None = None
        for warning in caught:
            if _quota_words(str(warning.message)):
                quota_hit = quota_hit or warning.category.__name__
            else:
                warnings_seen[warning.category.__name__] += 1
        if quota_hit is None and _quota_words(json.dumps(attrs or {}, ensure_ascii=False, default=str)):
            quota_hit = "attrs"
        say(f"{index + 1}/{len(symbols)} {symbol}: {len(converted)} rows")
        if quota_hit is not None:
            stop = _stop("quota_warning", symbol=symbol, kind=quota_hit)
            break

    after = [status_runner(command) for command in STATUS_COMMANDS]
    finished_at = now()
    write_new_text(out / "vnstock-status-after.txt", f"# vnstock CLI after, {finished_at.isoformat()}\n\n{cli_report(after)}")
    summary = build_summary(
        rows_by_symbol,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        library_version=library_version() if library_version is not None else None,
        python=platform.python_version(),
        isolated_mode=bool(sys.flags.isolated),
        rate_per_min=rate_per_min,
        status_every=status_every,
        calls_made=calls_made,
        requested=len(symbols),
        symbols_file_sha256=symbols_file_sha256,
        ok=ok,
        empty=empty,
        failed=failed,
        skipped=len(symbols) - asked,
        stop=stop,
        retries=retries,
        quota_before=remaining_before,
        quota_after=quota_remaining_pct(_status_text(after)),
        quota_checks=checks,
        quota_notes=quota_notes,
        warnings_seen=dict(warnings_seen),
    )
    write_new_json(summary_path, summary)
    say(f"wrote {summary_path}")
    if stop is not None:
        say(f"stopped early: {stop['reason']} (symbol {stop['symbol']}, {stop['type']}, {stop['code']})", error=True)
        return 1
    return 0


# --------------------------------------------------------------------------------------------------- merge


def _read_counts_dir(folder: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The summary and raw rows of one `events` output; raises ValueError when they disagree."""
    summary = json.loads((folder / "counts-summary.json").read_text(encoding="utf-8"))
    if summary.get("kind") != "vn_counts_summary" or summary.get("schema_version") != 1:
        raise ValueError(f"{folder}: not a vn_counts_summary v1")
    rows = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted((folder / "raw").glob("*.json"))}
    if len(rows) != summary["symbols"]["ok"]:
        raise ValueError(f"{folder}: {len(rows)} raw files but symbols.ok = {summary['symbols']['ok']}")
    return summary, rows


def run_merge(inputs: list[Path], universe_file: Path, out: Path) -> int:
    """Combine disjoint `events` outputs over one universe into `out` (summary + copied raw/); 1 when incomplete.

    Refused (ValueError, nothing written): a symbol fetched twice, a fetched symbol outside the universe, inputs of
    different vnstock versions, a summary whose raw/ count does not match symbols.ok. The counts are re-aggregated from
    the raw rows, so they equal one run over the union (vendor rows never leave the data folders).
    """
    out = Path(out)
    refuse_inside_work_tree(out)
    if out.exists():
        raise FileExistsError(f"{out} exists; choose another --out")
    universe = read_symbols_file(Path(universe_file))
    parts = [(Path(folder), *_read_counts_dir(Path(folder))) for folder in inputs]
    versions = sorted({str(summary.get("library_version")) for _, summary, _ in parts})
    if len(versions) > 1:
        raise ValueError(f"inputs ran on different vnstock versions {versions}: merge only one version")
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    source_of: dict[str, Path] = {}
    failed: list[dict[str, Any]] = []
    for folder, summary, rows in parts:
        for symbol, symbol_rows in rows.items():
            if symbol in rows_by_symbol:
                raise ValueError(f"{symbol} is fetched in both {source_of[symbol]} and {folder}")
            rows_by_symbol[symbol] = symbol_rows
            source_of[symbol] = folder
        failed.extend(summary["symbols"]["failed_list"])
    outside = sorted((set(rows_by_symbol) | {entry["symbol"] for entry in failed}) - set(universe))
    if outside:
        raise ValueError(f"fetched or failed symbols outside the universe file: {outside}")
    # A symbol fetched in one run is no failure; one that failed in several runs counts once (its first entry).
    failed = list({entry["symbol"]: entry for entry in reversed(failed) if entry["symbol"] not in rows_by_symbol}.values())
    failed.reverse()
    answered = set(rows_by_symbol) | {entry["symbol"] for entry in failed}
    skipped = [symbol for symbol in universe if symbol not in answered]
    # A quota_warning stop keeps its flagged rows in raw/, so rerunning the skipped symbols cannot clear it: it stays.
    flagged = [dict(summary["stop"], input=folder.name) for folder, summary, _ in parts
               if (summary.get("stop") or {}).get("reason") == "quota_warning"]
    if flagged:
        stop = flagged[0]
    else:
        stop = None if not skipped else _stop("incomplete_merge", symbol=skipped[0])
    summaries = [summary for _, summary, _ in parts]
    rates = {summary.get("rate_per_min") for summary in summaries}
    pythons = {summary.get("python") for summary in summaries}
    warnings_seen: Counter[str] = Counter()
    for summary in summaries:
        warnings_seen.update(summary.get("warnings_seen") or {})
    merged = build_summary(
        rows_by_symbol,
        started_at=min(summary["started_at_utc"] for summary in summaries),
        finished_at=max(summary["finished_at_utc"] for summary in summaries),
        library_version=summaries[0].get("library_version"),
        python=pythons.pop() if len(pythons) == 1 else None,
        isolated_mode=all(summary.get("isolated_mode") is True for summary in summaries),
        rate_per_min=rates.pop() if len(rates) == 1 else None,
        status_every=None,
        calls_made=sum(summary["calls_made"] for summary in summaries),
        requested=len(universe),
        symbols_file_sha256=sha256_file(Path(universe_file)),
        ok=len(rows_by_symbol),
        empty=sum(1 for symbol_rows in rows_by_symbol.values() if not symbol_rows),
        failed=failed,
        skipped=len(skipped),
        stop=stop,
        retries=[entry for summary in summaries for entry in summary.get("retries", [])],
        quota_before=summaries[0]["quota"]["before_remaining_pct"],
        quota_after=summaries[-1]["quota"]["after_remaining_pct"],
        quota_checks=[entry for summary in summaries for entry in summary["quota"]["checks"]],
        quota_notes=[note for summary in summaries for note in summary["quota"]["notes"]],
        warnings_seen=warnings_seen,
    )
    merged["merged_from"] = [
        {
            "dir": folder.name,
            "counts_summary_sha256": sha256_file(folder / "counts-summary.json"),
            "started_at_utc": summary["started_at_utc"],
            "finished_at_utc": summary["finished_at_utc"],
            "symbols_file_sha256": summary["universe"]["symbols_file_sha256"],
            "calls_made": summary["calls_made"],
            "symbols_ok": summary["symbols"]["ok"],
            "stop": summary["stop"],
        }
        for folder, summary, _ in parts
    ]
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True)
    for symbol, folder in sorted(source_of.items()):
        (raw_dir / f"{symbol}.json").write_bytes((folder / "raw" / f"{symbol}.json").read_bytes())
    write_new_json(out / "counts-summary.json", merged)
    say(f"wrote {out / 'counts-summary.json'}: {len(rows_by_symbol)}/{len(universe)} symbols from {len(parts)} runs")
    if stop is not None:
        if flagged:
            say(f"incomplete: input {stop['input']} stopped on a quota warning at {stop['symbol']} (rows kept, "
                "possibly truncated)", error=True)
        if skipped:
            say(f"incomplete: {len(skipped)} universe symbol(s) never fetched (first {skipped[0]})", error=True)
        return 1
    return 0


# --------------------------------------------------------------------------------------------------- universe


def run_universe(
    out: Path,
    *,
    groups: Iterable[str],
    members: Callable[[str], list[str]],
    extra_symbols: Iterable[str],
    now: Callable[[], datetime],
) -> int:
    """Index members plus extra symbols to `out` (one per line) and `<out>.json`; 1 when a group failed or nothing."""
    out = Path(out)
    refuse_inside_work_tree(out)
    meta_path = out.with_name(out.name + ".json")
    if out.exists() or meta_path.exists():
        raise FileExistsError(f"{out} or {meta_path.name} exists; choose another --out")
    symbols: list[str] = []
    report: dict[str, Any] = {}
    for group in groups:
        try:
            listed = members(group)
        except Exception as err:  # noqa: BLE001 - a failed group is recorded and the others still count
            code = getattr(err, "code", None)
            report[group] = {"status": "error", "type": type(err).__name__, "code": None if code is None else str(code)}
            say(f"index.members({group}) failed: {type(err).__name__} {code}", error=True)
            continue
        valid: list[str] = []
        dropped = 0
        for value in listed:
            symbol = normalize_symbol(str(value))
            if symbol is None:
                dropped += 1
            elif symbol not in valid:
                valid.append(symbol)
        report[group] = {"status": "ok", "count": len(valid), "dropped_invalid": dropped}
        symbols.extend(symbol for symbol in valid if symbol not in symbols)
    extras = [symbol for symbol in (normalize_symbol(str(value)) for value in extra_symbols) if symbol is not None]
    added = [symbol for symbol in dict.fromkeys(extras) if symbol not in symbols]
    symbols.extend(added)
    failed = [group for group, entry in report.items() if entry["status"] != "ok"]
    if not symbols:
        say("no symbol obtained; nothing written", error=True)
        return 1
    write_new_text(out, "".join(f"{symbol}\n" for symbol in symbols))
    write_new_json(
        meta_path,
        {
            "kind": "vn_counts_universe",
            "created_at_utc": now().isoformat(),
            "source_call": "Reference().index.members(group)",
            "group_names": "[ASSUMED] vendor group names",
            "groups": report,
            "extra_requested": len(extras),
            "extra_added": len(added),
            "size": len(symbols),
            "complete": not failed,
            "note": UNIVERSE_NOTE,
        },
    )
    say(f"wrote {out}: {len(symbols)} symbols" + (f"; groups FAILED: {', '.join(failed)} (partial universe)" if failed else ""))
    return 1 if failed else 0


# --------------------------------------------------------------------------------------------------- CLI


def cmd_events(
    args: argparse.Namespace,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = utc_now,
) -> int:
    """`events`; the sleeper and the clocks default to the real ones and are injectable (tests pass fakes)."""
    symbols_file = Path(args.symbols_file)
    symbols = read_symbols_file(symbols_file)
    client = VnstockClient()
    return run_events(
        symbols,
        Path(args.out),
        rate_per_min=args.rate,
        call_events=client.events,
        status_runner=run_vnstock_cli,
        sleeper=sleeper,
        monotonic=monotonic,
        now=now,
        status_every=args.status_every,
        library_version=lambda: client.version,
        symbols_file_sha256=sha256_file(symbols_file),
    )


def cmd_universe(args: argparse.Namespace, *, now: Callable[[], datetime] = utc_now) -> int:
    groups = [group.strip() for group in args.groups.split(",") if group.strip()]
    if not groups:
        raise ValueError("--groups is empty")
    extras = read_symbols_file(Path(args.extra_symbols_file)) if args.extra_symbols_file else []
    client = VnstockClient()
    return run_universe(Path(args.out), groups=groups, members=client.index_members, extra_symbols=extras, now=now)


def cmd_merge(args: argparse.Namespace) -> int:
    return run_merge([Path(folder) for folder in args.inputs], Path(args.universe_file), Path(args.out))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    events = commands.add_parser("events", help="fetch vci events per symbol and write counts-summary.json")
    events.add_argument("--symbols-file", required=True, help="universe file, one symbol per line")
    events.add_argument("--rate", required=True, type=float, help=f"calls per minute (0 < rate <= {MAX_RATE_PER_MIN:g})")
    events.add_argument("--out", required=True, help="new output directory (outside the clone)")
    events.add_argument(
        "--status-every", type=int, default=DEFAULT_STATUS_EVERY,
        help=f"re-read the quota every N calls (0 = only before and after; default {DEFAULT_STATUS_EVERY})",
    )
    events.set_defaults(handler=cmd_events)

    universe = commands.add_parser("universe", help="write today's VN100 + HNX30 members plus extra symbols")
    universe.add_argument("--out", required=True, help="universe text file to create")
    universe.add_argument("--groups", default=",".join(UNIVERSE_GROUPS), help="index groups ([ASSUMED] names)")
    universe.add_argument("--extra-symbols-file", help="extra symbols (e.g. the reconcile sample), one per line")
    universe.set_defaults(handler=cmd_universe)

    merge = commands.add_parser("merge", help="combine disjoint events outputs over one universe (no vendor call)")
    merge.add_argument("--inputs", required=True, nargs="+", help="events output directories, in run order")
    merge.add_argument("--universe-file", required=True, help="the universe the runs together cover")
    merge.add_argument("--out", required=True, help="new output directory (outside the clone)")
    merge.set_defaults(handler=cmd_merge)
    return parser.parse_args(argv)


def is_isolated() -> bool:
    return bool(sys.flags.isolated)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not is_isolated():
        say("vn_counts: run with `python -I` (isolated mode)", error=True)
        return 2
    try:
        return args.handler(args)
    except WorkTreeRefused as err:
        say(f"vn_counts {args.command}: refused, --out is in a git work tree: {err}", error=True)
        return 4
    except (ValueError, OSError) as err:
        say(f"vn_counts {args.command}: {err}", error=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
