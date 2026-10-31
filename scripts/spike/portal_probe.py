"""Probe the official VN disclosure portals (HOSE, HNX, VSDC, SSC) for the Phase 2 source choice (plan 261022-1147, R1.6).

Run from the repo root with the dev venv, during trading hours:
    .venv/Scripts/python scripts/spike/portal_probe.py --rounds 12 --interval 300
    .venv/Scripts/python scripts/spike/portal_probe.py --history-check
`--rounds 1` is one smoke round. Reaches the network; never collected by pytest.

Politeness: at least 5 s between two requests to the same host, a neutral User-Agent without personal data, one request
per candidate per round. Raw bodies (titles name real people: PDPL, research vn-disclosure-portals.md section 5) go only to
`$FINSEC_DATA_ROOT/spike/portal/<UTC-date>/<run-id>/`. The summary JSON holds counts, latencies and flags only: no
title, no name. Candidates and their facts: research `vn-disclosure-portals.md` sections 1-4 (OBSERVED 2026-10-22).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from _spikelib import (
    USER_AGENT,
    FetchResult,
    SpikeEnvError,
    append_jsonl,
    fetch,
    sha256_hex,
    skew_from_date_header,
    spike_dir,
    utc_now,
    write_json,
)
from finsec_alpharadar.data_root import DataRootError

MIN_HOST_GAP_S = 5.0  # R1.6: at least 5 s between two requests to the same host
MIN_ROUND_INTERVAL_S = 60.0
MIN_TIMEOUT_S = 30.0  # HNX connects in ~20 s through WARP (research §2)
ROBOTS_HEAD_BYTES = 2048
ICT_OFFSET = timedelta(hours=7)

HOSE_LISTING = "https://api.hsx.vn/n/api/v1/1/news/securitiesType/0"
HOSE_HEADERS = {"Origin": "https://www.hsx.vn", "Referer": "https://www.hsx.vn/", "Accept": "application/json"}
HNX_LISTING_PAGE = "https://hnx.vn/vi-vn/thong-tin-cong-bo-ny-hnx.html"
HNX_POST_URL = "https://hnx.vn/ModuleArticles/ArticlesCPEtfs/NextPageTinCPNY_CBTCPH"
# Field names OBSERVED (research §2); only pNumRecord=50 was recorded, the other values are [ASSUMED] (empty = no filter).
HNX_POST_FORM = {
    "pNumPage": "1",
    "pAction": "",
    "pNhomTin": "",
    "pTieuDeTin": "",
    "pMaChungKhoan": "",
    "pFromDate": "",
    "pToDate": "",
    "pOrderBy": "",
    "pNumRecord": "50",
}
ROBOTS_HOSTS = ("www.hsx.vn", "api.hsx.vn", "hnx.vn", "vsdc.vn", "congbothongtin.ssc.gov.vn")
SSC_FRONT_PAGE = "https://congbothongtin.ssc.gov.vn/"
HISTORY_YEARS = range(2018, 2027)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
WAF_MARKERS = (
    "captcha",
    "request rejected",
    "the requested url was rejected",
    "access denied",
    "attention required",
    "cf-chl",
    "support id",
)
# Digit lookarounds, not \b: an ISO stamp's "T10:39:02" has no word boundary between "T" and "1".
HH_MM_SS = re.compile(r"(?<!\d)\d{1,2}:\d{2}:\d{2}(?!\d)")
HH_MM = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")


@dataclass(frozen=True)
class Listing:
    """Derived facts of one listing body: ids, timestamp precision, item count. Never titles."""

    ids: tuple[str, ...]
    items: int
    time_precision: str  # "seconds", "minutes", "date_only", "none_found" or "unparsed"
    total_count: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class Candidate:
    name: str
    host: str
    build: Callable[[date], tuple[str, str, dict[str, str], bytes | None]]  # ICT day -> (method, url, headers, data)
    parse: Callable[[bytes], Listing]
    expect: str  # "json", "xml" or "html": the content type a non-WAF answer carries


@dataclass
class HostGate:
    """Sleeps so that two requests to the same host are at least MIN_HOST_GAP_S apart (end of one to start of next)."""

    last_end: dict[str, float] = field(default_factory=dict)

    def wait(self, host: str) -> None:
        last = self.last_end.get(host)
        if last is not None:
            remaining = MIN_HOST_GAP_S - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)

    def done(self, host: str) -> None:
        self.last_end[host] = time.monotonic()


def _precision(stamps: list[str]) -> str:
    if not stamps:
        return "none_found"
    if any(HH_MM_SS.search(stamp) for stamp in stamps):
        return "seconds"
    if any(HH_MM.search(stamp) for stamp in stamps):
        return "minutes"
    return "date_only"


def parse_hose_json(body: bytes) -> Listing:
    """HOSE API: `data.list[*].id`, `postedDate` (pseudo-UTC epoch seconds, research §1), `data.paging.totalCount`."""
    try:
        payload = json.loads(body.decode("utf-8"))
        data = payload["data"]
        items = data["list"] or []
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as err:
        return Listing(ids=(), items=0, time_precision="unparsed", note=f"not the expected JSON: {type(err).__name__}")
    ids = tuple(str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id") is not None)
    posted = [item.get("postedDate") for item in items if isinstance(item, dict)]
    epochs = [value for value in posted if isinstance(value, int) and not isinstance(value, bool)]
    if not epochs:
        precision = "none_found"
    elif any(value % 60 for value in epochs):
        precision = "seconds"
    else:
        precision = "minutes"
    paging = data.get("paging") if isinstance(data, dict) else None
    total = paging.get("totalCount") if isinstance(paging, dict) else None
    return Listing(ids=ids, items=len(items), time_precision=precision, total_count=total if isinstance(total, int) else None)


def parse_rss(body: bytes) -> Listing:
    """RSS 2.0 items: `guid` as id, `pubDate` or Atom `updated` as the timestamp."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as err:
        return Listing(ids=(), items=0, time_precision="unparsed", note=f"not XML: {err.code}")
    items = root.findall("./channel/item")
    ids = tuple((item.findtext("guid") or "").strip() for item in items if (item.findtext("guid") or "").strip())
    stamps = [(item.findtext("pubDate") or item.findtext(f"{ATOM_NS}updated") or "").strip() for item in items]
    return Listing(ids=ids, items=len(items), time_precision=_precision([stamp for stamp in stamps if stamp]))


def parse_hnx_post(body: bytes) -> Listing:
    """HNX listing HTML: ids from `funcViewDetailArticlesByID(<id>,...)`, times `dd/MM/yyyy HH:MM` (research §2)."""
    text = body.decode("utf-8", errors="replace")
    ids = tuple(dict.fromkeys(re.findall(r"funcViewDetailArticlesByID\((\d+)", text)))
    stamps = re.findall(r"\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", text)
    return Listing(ids=ids, items=len(ids), time_precision=_precision(stamps))


def parse_vsdc(body: bytes) -> Listing:
    """VSDC list HTML: ids from `/vi/ad/<id>`, times `dd/MM/yyyy - HH:MM:SS` after "Cập nhật ngày" (research §4)."""
    text = body.decode("utf-8", errors="replace")
    ids = tuple(dict.fromkeys(re.findall(r"/vi/ad/(\d+)", text)))
    stamps = re.findall(r"\d{2}/\d{2}/\d{4}(?:\s*-\s*\d{1,2}:\d{2}(?::\d{2})?)?", text)
    return Listing(ids=ids, items=len(ids), time_precision=_precision(stamps))


def _hose_request(start: date, end: date, page_size: int) -> tuple[str, str, dict[str, str], bytes | None]:
    query = urllib.parse.urlencode(
        {"pageIndex": 1, "pageSize": page_size, "startDate": start.isoformat(), "endDate": end.isoformat()}
    )
    return "GET", f"{HOSE_LISTING}?{query}", HOSE_HEADERS, None


def build_candidates(hose_page_size: int) -> list[Candidate]:
    """Round order alternates hosts, so the per-host gap rarely has to sleep."""
    hnx_post_headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",  # the page loads this tab by AJAX [ASSUMED required]
        "Referer": HNX_LISTING_PAGE,
    }
    hnx_post_body = urllib.parse.urlencode(HNX_POST_FORM).encode("ascii")
    return [
        # Phase 2 sends both HOSE forms (red-team r1 RT13): today only, and yesterday..today on the first poll of an ICT day.
        Candidate("hose_api_today", "api.hsx.vn", lambda day: _hose_request(day, day, hose_page_size), parse_hose_json, "json"),
        Candidate(
            "hnx_rss3",
            "hnx.vn",
            lambda day: ("GET", "https://hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss", {}, None),
            parse_rss,
            "xml",
        ),
        Candidate(
            "vsdc_list", "vsdc.vn", lambda day: ("GET", "https://vsdc.vn/vi/tin-thi-truong-co-so", {}, None), parse_vsdc, "html"
        ),
        Candidate(
            "hose_api_two_day",
            "api.hsx.vn",
            lambda day: _hose_request(day - timedelta(days=1), day, hose_page_size),
            parse_hose_json,
            "json",
        ),
        Candidate(
            "hnx_post", "hnx.vn", lambda day: ("POST", HNX_POST_URL, hnx_post_headers, hnx_post_body), parse_hnx_post, "html"
        ),
        Candidate(
            "hose_rss",
            "api.hsx.vn",
            lambda day: ("GET", "https://api.hsx.vn/n/api/v1/News/NewsByCateFeed/21", {}, None),
            parse_rss,
            "xml",
        ),
    ]


def ict_today() -> date:
    return (utc_now() + ICT_OFFSET).date()


def _extension(content_type: str | None) -> str:
    lowered = (content_type or "").lower()
    for marker, extension in (("json", "json"), ("xml", "xml"), ("rss", "xml"), ("html", "html")):
        if marker in lowered:
            return extension
    return "bin"


def _cookie_names(result: FetchResult) -> list[str]:
    """Names only: a cookie value is never recorded."""
    return sorted({value.split("=", 1)[0].strip() for value in result.header_values("Set-Cookie") if "=" in value})


def _waf_markers(body: bytes) -> list[str]:
    head = body[:65536].decode("utf-8", errors="replace").lower()
    return [marker for marker in WAF_MARKERS if marker in head]


def _metrics(result: FetchResult) -> dict[str, Any]:
    """Per-request facts for requests.jsonl and the summary: status, timings, size, hash, clock skew, WAF signs."""
    return {
        "method": result.method,
        "url": result.url,
        "final_url": result.final_url,
        "status": result.status,
        "error": result.error,
        "seconds_to_headers": result.seconds_to_headers,
        "seconds_total": result.seconds_total,
        "bytes": len(result.body),
        "truncated": result.truncated,
        "sha256": sha256_hex(result.body),
        "content_type": result.header("Content-Type"),
        "date_header": result.header("Date"),
        "skew_s": skew_from_date_header(result.header("Date"), result.local_before_utc, result.local_after_utc),
        "local_before_utc": result.local_before_utc.isoformat(),
        "local_after_utc": result.local_after_utc.isoformat(),
        "cookie_names": _cookie_names(result),
        "waf_markers": _waf_markers(result.body),
    }


@dataclass
class Run:
    """One invocation: its output directory, request log and polite fetcher."""

    directory: Path
    run_id: str
    timeout: float
    gate: HostGate = field(default_factory=HostGate)

    @property
    def log(self) -> Path:
        return self.directory / "requests.jsonl"

    def request(
        self, host: str, method: str, url: str, headers: dict[str, str], data: bytes | None, raw_name: str
    ) -> FetchResult:
        self.gate.wait(host)
        try:
            result = fetch(url, method=method, headers=headers, data=data, timeout=self.timeout)
        finally:
            self.gate.done(host)
        extension = _extension(result.header("Content-Type"))
        if result.body:
            (self.directory / f"{raw_name}.{extension}").write_bytes(result.body)
        append_jsonl(self.log, {"run_id": self.run_id, "raw": raw_name, **_metrics(result)})
        return result


def check_robots(run: Run, candidate_urls: dict[str, list[str]]) -> dict[str, Any]:
    """Status and the first 2 KB of each robots.txt (raw file), and whether it allows our candidate URLs on that host."""
    report: dict[str, Any] = {}
    for host in ROBOTS_HOSTS:
        result = run.request(host, "GET", f"https://{host}/robots.txt", {}, None, f"robots-{host}")
        head = result.body[:ROBOTS_HEAD_BYTES]
        entry: dict[str, Any] = {
            "status": result.status,
            "error": result.error.split(":", 1)[0] if result.error else None,
            "bytes": len(result.body),
            "seconds_total": round(result.seconds_total, 3),
        }
        urls = candidate_urls.get(host, [])
        if result.status == 200:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(head.decode("utf-8", errors="replace").splitlines())
            entry["allows_candidates"] = {url: parser.can_fetch(USER_AGENT, url) for url in urls}
        elif result.status is not None and 400 <= result.status < 500 and result.status not in (401, 403):
            entry["allows_candidates"] = {url: True for url in urls}  # no robots.txt: everything allowed (RFC 9309 §2.3.1.3)
        else:
            entry["allows_candidates"] = {url: None for url in urls}  # unknown: 401/403, 5xx or unreachable
        # The robots file is not personal data; keep only its first 2 KB next to the full raw body.
        (run.directory / f"robots-{host}.head.txt").write_bytes(head)
        report[host] = entry
    return report


def _spread(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"p50": round(statistics.median(values), 3), "max": round(max(values), 3), "min": round(min(values), 3)}


def summarize(candidate_log: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Counts and latencies per candidate: no title, no name, no body text."""
    summary: dict[str, Any] = {}
    for name, entries in candidate_log.items():
        statuses: dict[str, int] = {}
        errors: dict[str, int] = {}
        for entry in entries:
            statuses[str(entry["status"])] = statuses.get(str(entry["status"]), 0) + 1
            if entry["error"]:
                key = entry["error"].split(":", 1)[0]
                errors[key] = errors.get(key, 0) + 1
        summary[name] = {
            "requests": len(entries),
            "status_counts": statuses,
            "error_types": errors,
            "seconds_to_headers": _spread([e["seconds_to_headers"] for e in entries if e["seconds_to_headers"] is not None]),
            "seconds_total": _spread([e["seconds_total"] for e in entries if e["status"] is not None]),
            "bytes": _spread([float(e["bytes"]) for e in entries if e["status"] is not None]),
            "skew_s": _spread([e["skew_s"] for e in entries if e["skew_s"] is not None]),
            "content_types": sorted({e["content_type"] or "" for e in entries}),
            "unexpected_content_type": sum(1 for e in entries if e["unexpected_content_type"]),
            "cookie_names": sorted({cookie for e in entries for cookie in e["cookie_names"]}),
            "waf_markers": sorted({marker for e in entries for marker in e["waf_markers"]}),
            "time_precision": sorted({e["time_precision"] for e in entries}),
            "has_ids": any(e["ids"] for e in entries),
            "items_per_page": _spread([float(e["items"]) for e in entries if e["status"] == 200]),
            "total_count": [e["total_count"] for e in entries if e["total_count"] is not None],
            "overlap_with_previous_round": [e["overlap_with_previous"] for e in entries],
            "parse_notes": sorted({e["parse_note"] for e in entries if e["parse_note"]}),
        }
    return summary


def run_rounds(run: Run, rounds: int, interval: float, hose_page_size: int, summary_path: Path) -> dict[str, Any]:
    candidates = build_candidates(hose_page_size)
    day = ict_today()
    candidate_urls: dict[str, list[str]] = {}
    for candidate in candidates:
        _, url, _, _ = candidate.build(day)
        candidate_urls.setdefault(candidate.host, []).append(url)
    started = utc_now()
    summary: dict[str, Any] = {
        "kind": "portal_rounds",
        "run_id": run.run_id,
        "user_agent": USER_AGENT,
        "rounds_requested": rounds,
        "interval_s": interval,
        "min_host_gap_s": MIN_HOST_GAP_S,
        "timeout_s": run.timeout,
        "hose_page_size": hose_page_size,
        "started_at_utc": started.isoformat(),
        "robots": check_robots(run, candidate_urls),
    }
    ssc = run.request("congbothongtin.ssc.gov.vn", "GET", SSC_FRONT_PAGE, {}, None, "ssc-front")
    summary["ssc_front_page"] = {key: value for key, value in _metrics(ssc).items() if key not in ("url", "final_url")}
    write_json(summary_path, summary)

    previous_ids: dict[str, set[str]] = {}
    candidate_log: dict[str, list[dict[str, Any]]] = {candidate.name: [] for candidate in candidates}
    schedule_start = time.monotonic()
    late_starts: list[float] = []
    for round_index in range(rounds):
        due = schedule_start + round_index * interval
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif round_index:
            late_starts.append(round(-delay, 1))
        day = ict_today()
        for candidate in candidates:
            method, url, headers, data = candidate.build(day)
            result = run.request(candidate.host, method, url, headers, data, f"r{round_index + 1:02d}-{candidate.name}")
            metrics = _metrics(result)
            listing = candidate.parse(result.body) if result.status == 200 else Listing(ids=(), items=0, time_precision="not_200")
            current = set(listing.ids)
            previous = previous_ids.get(candidate.name)
            content_type = (metrics["content_type"] or "").lower()
            candidate_log[candidate.name].append(
                {
                    **metrics,
                    "unexpected_content_type": result.status == 200 and candidate.expect not in content_type
                    and not (candidate.expect == "xml" and "rss" in content_type),
                    "ids": len(current),
                    "items": listing.items,
                    "time_precision": listing.time_precision,
                    "total_count": listing.total_count,
                    "parse_note": listing.note,
                    "overlap_with_previous": None if previous is None else len(previous & current),
                }
            )
            if current:
                previous_ids[candidate.name] = current
        summary["rounds_completed"] = round_index + 1
        summary["late_round_starts_s"] = late_starts
        summary["candidates"] = summarize(candidate_log)
        summary["finished_at_utc"] = utc_now().isoformat()
        write_json(summary_path, summary)
        print(f"round {round_index + 1}/{rounds} done", flush=True)
    return summary


def history_dates() -> list[date]:
    """One trading day per year: the second Wednesday of October (no VN public holiday in October). Fixed, not clock-derived."""
    days = []
    for year in HISTORY_YEARS:
        wednesdays = [day for day in (date(year, 10, d) for d in range(1, 15)) if day.weekday() == 2]
        days.append(wednesdays[1])
    return days


def run_history_check(run: Run, summary_path: Path) -> dict[str, Any]:
    """HOSE API `pageSize=1` for one day per year 2018..2026: `totalCount` shows how deep the posting-time history goes."""
    summary: dict[str, Any] = {"kind": "portal_history_check", "run_id": run.run_id, "user_agent": USER_AGENT, "years": {}}
    summary["started_at_utc"] = utc_now().isoformat()
    for day in history_dates():
        method, url, headers, data = _hose_request(day, day, 1)
        result = run.request("api.hsx.vn", method, url, headers, data, f"history-{day.isoformat()}")
        listing = parse_hose_json(result.body) if result.status == 200 else None
        summary["years"][str(day.year)] = {
            "date": day.isoformat(),
            "status": result.status,
            "error": result.error.split(":", 1)[0] if result.error else None,
            "seconds_total": round(result.seconds_total, 3),
            "total_count": listing.total_count if listing else None,
            "parse_note": listing.note if listing else None,
        }
        write_json(summary_path, summary)
    summary["finished_at_utc"] = utc_now().isoformat()
    write_json(summary_path, summary)
    return summary


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--rounds", type=int, default=12, help="rounds of one request per candidate (default 12; 1 = smoke round)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=300.0,
        help=f"seconds between round starts (default 300, minimum {MIN_ROUND_INTERVAL_S:.0f})",
    )
    parser.add_argument(
        "--hose-page-size", type=int, default=20, help="HOSE API pageSize, the value Phase 2 will configure (default 20)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help=f"connect and read timeout in seconds (default 90, minimum {MIN_TIMEOUT_S:.0f})",
    )
    parser.add_argument("--history-check", action="store_true", help="only query HOSE totalCount for one day per year 2018..2026")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    if args.rounds > 1 and args.interval < MIN_ROUND_INTERVAL_S:
        parser.error(f"--interval must be at least {MIN_ROUND_INTERVAL_S:.0f} s when --rounds > 1")
    if not 1 <= args.hose_page_size <= 50:
        parser.error("--hose-page-size must be between 1 and 50 (research: keep it at 20 or less)")
    if args.timeout < MIN_TIMEOUT_S:
        parser.error(f"--timeout must be at least {MIN_TIMEOUT_S:.0f} s")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        base = spike_dir("portal")
    except (DataRootError, SpikeEnvError) as err:
        print(f"portal_probe: {err}", file=sys.stderr)
        return 2
    run_id = utc_now().strftime("%H%M%SZ") + ("-history" if args.history_check else "-rounds")
    directory = base / run_id
    directory.mkdir()
    run = Run(directory=directory, run_id=run_id, timeout=args.timeout)
    summary_path = directory / ("history-summary.json" if args.history_check else "rounds-summary.json")
    try:
        if args.history_check:
            run_history_check(run, summary_path)
        else:
            run_rounds(run, args.rounds, args.interval, args.hose_page_size, summary_path)
    except KeyboardInterrupt:
        print(f"portal_probe: interrupted; partial summary in {summary_path}", file=sys.stderr)
        return 130
    print(f"portal_probe: summary {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
