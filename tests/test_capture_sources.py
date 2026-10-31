"""Capture sources (`finsec_alpharadar.capture.sources`, plan 261022-1147 phase 2, R2.8; red-team r1 RT2, RT7, RT13, LT9).

Every payload is synthetic and written inline: made-up tickers (AAA, BBB, CCC), made-up titles and ids, no real portal
or vnstock data, no fixture files. Fetchers, openers, clocks, sleepers, limiters, workers and the vnstock importer are
fakes: no network, no sleep, no real clock, and vnstock is never imported.
"""
from __future__ import annotations

import gzip
import io
import json
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from types import SimpleNamespace
from typing import Any, Callable
from urllib.error import HTTPError, URLError

import pytest

from finsec_alpharadar.capture.config import SourceConfig, SourceKind
from finsec_alpharadar.capture.record import OutageKind, PayloadFormat, canonical_item_bytes, sha256_hex
from finsec_alpharadar.capture.sources import (
    TELEMETRY_ENV,
    HoseNewsApiSource,
    HttpRequest,
    HttpResponse,
    PollContext,
    RssFeedSource,
    SourceError,
    UrllibFetcher,
    VendorWorker,
    VnstockEventsSource,
)

HOSE_URL = "https://portal-a.example.invalid/n/api/v1/1/news/securitiesType/0"
RSS_URL = "https://portal-b.example.invalid/3/vi_vn/feed.rss"
T0 = datetime(2026, 10, 27, 3, 0, 0, tzinfo=timezone.utc)  # 10:00 ICT, a Tuesday


class FakeClock:
    def __init__(self, now: datetime = T0, mono: float = 1000.0) -> None:
        self._now, self._mono = now, mono

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


class FakeFetcher:
    """Answers each request with the next queued response (or raises a queued `SourceError`)."""

    def __init__(self, *answers: HttpResponse | SourceError) -> None:
        self.answers = list(answers)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, SourceError):
            raise answer
        return answer


@dataclass
class FakeSleeper:
    calls: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _response(body: bytes, status: int = 200, *, at: datetime = T0, content_type: str = "application/json") -> HttpResponse:
    headers = {"date": "Tue, 27 Oct 2026 03:00:01 GMT", "content-type": content_type}
    return HttpResponse(status=status, headers=headers, body=body, received_at=at, rtt_ms=850.0)


def _hose_body(ids: list[int], *, success: Any = True) -> bytes:
    items = [{"id": n, "title": f"AAA: Thông báo số {n}", "postedDate": 1793000000 + n, "code": None} for n in ids]
    return json.dumps({"data": {"list": items, "paging": {"pageIndex": 1}}, "success": success, "message": None},
                      ensure_ascii=False).encode("utf-8")


def _hose_config(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "hose_news_api", "kind": SourceKind.HOSE_NEWS_API, "enabled": True, "url": HOSE_URL,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 30.0, "deadline_s": 60.0, "retries": 1,
        "max_body_bytes": 2_000_000, "page_size": 3, "max_pages": 2, "min_request_spacing_s": 1.0,
        "headers": {"Origin": "https://portal-a.example.invalid", "Referer": "https://portal-a.example.invalid/x"},
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _rss_config(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "hnx_rss_issuers", "kind": SourceKind.RSS_FEED, "enabled": True, "url": RSS_URL,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 90.0, "deadline_s": 150.0, "retries": 1,
        "max_body_bytes": 2_000_000,
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _vnstock_config(**changes: Any) -> SourceConfig:
    fields: dict[str, Any] = {
        "name": "vnstock_events", "kind": SourceKind.VNSTOCK_EVENTS, "enabled": True, "url": None,
        "active_interval_s": 180.0, "idle_interval_s": 3600.0, "timeout_s": 60.0, "deadline_s": 60.0, "retries": 0,
        "max_body_bytes": 2_000_000, "vnstock_source": "vci", "symbols": ("AAA", "BBB", "CCC", "DDD"),
        "symbols_per_poll": 2, "calls_per_min": 60.0, "vendor_call_timeout_s": 60.0,
        "release_expires_on": date(2027, 10, 17),
        "error_map": {"VENDOR_FEATURE_GONE": "vendor_unavailable", "VENDOR_SHAPE": "source_invalid_payload"},
        "symbol_error_codes": ("VENDOR_SYMBOL_UNKNOWN",),
        "quota_warning_patterns": ("truncat", "quota", "giới hạn"),
        "ignore_warning_patterns": ("new version",),
    }
    fields.update(changes)
    return SourceConfig(**fields)


def _ctx(known: set[str] = frozenset(), *, now: datetime = T0, last_ok_at: datetime | None = T0 - timedelta(minutes=3),
         cursor: int = 0) -> PollContext:
    return PollContext(now=now, last_ok_at=last_ok_at, is_known=lambda source_id: source_id in known, cursor=cursor)


def _hose(*answers: HttpResponse | SourceError, **config: Any) -> tuple[HoseNewsApiSource, FakeFetcher, FakeSleeper]:
    fetcher, sleeper = FakeFetcher(*answers), FakeSleeper()
    return HoseNewsApiSource(_hose_config(**config), fetcher, sleeper=sleeper), fetcher, sleeper


# ---------------------------------------------------------------- HOSE


def test_hose_ids_are_extracted_from_a_synthetic_page() -> None:
    body = _hose_body([9000003, 9000001])
    source, fetcher, _ = _hose(_response(body))
    result = source.poll(_ctx({"9000001"}))
    assert result.outage_kind is None and result.detail is None
    assert [item.source_id for item in result.items] == ["9000003", "9000001"]
    first = result.items[0]
    assert first.payload_format is PayloadFormat.JSON
    assert first.payload == json.loads(body)["data"]["list"][0], "the item payload is the verbatim dict"
    assert first.raw_sha256 == sha256_hex(canonical_item_bytes(first.payload))
    assert first.byte_count == len(canonical_item_bytes(first.payload))
    assert first.first_seen_at == T0 and first.snapshot_sha256 == sha256_hex(body)
    assert len(result.pages) == 1 and result.pages[0].body == body and result.pages[0].sha256 == sha256_hex(body)
    assert (result.http_status, result.http_date, result.rtt_ms) == (200, "Tue, 27 Oct 2026 03:00:01 GMT", 850.0)
    assert len(fetcher.requests) == 1, "a page shorter than page_size ends the listing"
    request = fetcher.requests[0]
    assert request.url == HOSE_URL and dict(request.params)["pageIndex"] == "1" and dict(request.params)["pageSize"] == "3"
    assert request.headers["Origin"] == "https://portal-a.example.invalid"


def test_hose_request_dates_are_ict_calendar_days() -> None:
    late = datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc)  # 06:30 ICT on 2026-10-28
    source, fetcher, _ = _hose(_response(_hose_body([]), at=late))
    source.poll(_ctx(now=late, last_ok_at=late - timedelta(minutes=30)))
    params = dict(fetcher.requests[0].params)
    assert (params["startDate"], params["endDate"]) == ("2026-10-28", "2026-10-28")


def test_hose_asks_for_yesterday_only_on_the_first_poll_of_an_ict_day() -> None:
    after_midnight = datetime(2026, 10, 27, 17, 10, tzinfo=timezone.utc)  # 00:10 ICT on 2026-10-28
    source, fetcher, _ = _hose(*(_response(_hose_body([]), at=after_midnight) for _ in range(3)))
    source.poll(_ctx(now=after_midnight, last_ok_at=datetime(2026, 10, 27, 16, 50, tzinfo=timezone.utc)))  # 23:50 ICT
    source.poll(_ctx(now=after_midnight, last_ok_at=None))
    source.poll(_ctx(now=after_midnight + timedelta(minutes=3), last_ok_at=after_midnight))
    dates = [(dict(r.params)["startDate"], dict(r.params)["endDate"]) for r in fetcher.requests]
    assert dates == [("2026-10-27", "2026-10-28"), ("2026-10-27", "2026-10-28"), ("2026-10-28", "2026-10-28")]


def test_hose_pages_until_a_seen_id() -> None:
    page1, page2 = _hose_body([16, 15, 14]), _hose_body([13, 12, 11])
    source, fetcher, sleeper = _hose(_response(page1), _response(page2), max_pages=3)
    result = source.poll(_ctx({"12", "11"}))
    assert result.outage_kind is None
    assert [dict(r.params)["pageIndex"] for r in fetcher.requests] == ["1", "2"]
    assert sleeper.calls == [1.0], "pages are spaced by min_request_spacing_s through the injected sleeper"
    assert [item.source_id for item in result.items] == ["16", "15", "14", "13", "12", "11"]
    assert [item.snapshot_sha256 for item in result.items] == [sha256_hex(page1)] * 3 + [sha256_hex(page2)] * 3
    # An id that moved from page 1 to page 2 between the two requests is captured once.
    shifted, fetcher2, _ = _hose(_response(_hose_body([16, 15, 14])), _response(_hose_body([14, 13, 12])), max_pages=3)
    assert [item.source_id for item in shifted.poll(_ctx({"12"})).items] == ["16", "15", "14", "13", "12"]


def test_hose_page_cap_reports_coverage_overflow() -> None:
    source, fetcher, _ = _hose(_response(_hose_body([6, 5, 4])), _response(_hose_body([3, 2, 1])))
    result = source.poll(_ctx({"99"}))
    assert result.outage_kind is OutageKind.COVERAGE_OVERFLOW
    assert len(result.items) == 6, "the items fetched are still returned (real data)"
    assert json.loads(result.detail or "{}")["pages"] == 2
    # The first poll ever has nothing to overlap with: no overflow (its items are late anyway).
    first, _, _ = _hose(_response(_hose_body([6, 5, 4])), _response(_hose_body([3, 2, 1])))
    assert first.poll(_ctx(last_ok_at=None)).outage_kind is None


def test_hose_unsuccessful_body_is_invalid_payload() -> None:
    bodies = [
        _hose_body([1], success=False),
        _hose_body([1], success="true"),
        b"<html>maintenance</html>",
        json.dumps({"success": True, "data": {"list": [{"id": "1"}]}}).encode(),  # id must be an int
        json.dumps({"success": True, "data": {"list": [{"title": "AAA"}]}}).encode(),
        json.dumps({"success": True, "data": {"list": [{"id": True}]}}).encode(),
        json.dumps({"success": True, "data": None}).encode(),
        b"\xff\xfe not utf-8",
    ]
    for body in bodies:
        source, _, _ = _hose(_response(body))
        result = source.poll(_ctx())
        assert result.outage_kind is OutageKind.SOURCE_INVALID_PAYLOAD, body
        assert result.items == ()
        assert len(result.pages) == 1, "the body is still kept as a snapshot for diagnosis"


def test_http_403_and_429_are_source_rejected() -> None:
    for status in (401, 403, 429):
        source, _, _ = _hose(_response(b"denied", status=status))
        result = source.poll(_ctx())
        assert result.outage_kind is OutageKind.SOURCE_REJECTED
        assert result.http_status == status and result.items == ()
    rss = RssFeedSource(_rss_config(), FakeFetcher(_response(b"denied", status=403)))
    assert rss.poll(_ctx()).outage_kind is OutageKind.SOURCE_REJECTED


def test_http_5xx_and_timeouts_are_source_unreachable() -> None:
    for status in (500, 502, 503, 504):
        source, _, _ = _hose(_response(b"", status=status))
        assert source.poll(_ctx()).outage_kind is OutageKind.SOURCE_UNREACHABLE
    transport = SourceError(OutageKind.SOURCE_UNREACHABLE, '{"class":"TimeoutError"}')
    source, _, _ = _hose(transport)
    result = source.poll(_ctx())
    assert result.outage_kind is OutageKind.SOURCE_UNREACHABLE and result.detail == '{"class":"TimeoutError"}'
    # The real fetcher maps a transport failure to the same class.
    clock = FakeClock()
    for error in (TimeoutError("timed out"), URLError("no route"), ConnectionResetError("reset")):
        fetcher = UrllibFetcher(clock, "test-agent/0", 30.0, 60.0, max_body_bytes=1000, opener=FakeOpener(error))
        with pytest.raises(SourceError) as caught:
            fetcher(HttpRequest("GET", HOSE_URL))
        assert caught.value.kind is OutageKind.SOURCE_UNREACHABLE
        assert json.loads(caught.value.detail)["class"] == type(error).__name__
    # A page-2 failure keeps the page-1 items.
    paged, _, _ = _hose(_response(_hose_body([6, 5, 4])), _response(b"", status=503))
    partial = paged.poll(_ctx())
    assert partial.outage_kind is OutageKind.SOURCE_UNREACHABLE and len(partial.items) == 3


def test_request_descriptor_holds_no_header_values() -> None:
    body = _hose_body([1])
    source, fetcher, _ = _hose(_response(body), headers={"Origin": "https://o.example.invalid", "Cookie": "TS01=placeholder-value"})
    result = source.poll(_ctx())
    descriptor = result.items[0].request
    assert descriptor == {
        "method": "GET",
        "url": HOSE_URL,
        "params": {"pageIndex": "1", "pageSize": "3", "startDate": "2026-10-27", "endDate": "2026-10-27"},
        "header_names": ["Cookie", "Origin"],
    }
    assert "placeholder-value" not in json.dumps(descriptor) and "o.example" not in json.dumps(descriptor)
    assert result.items[0].response == {
        "status": 200, "http_date": "Tue, 27 Oct 2026 03:00:01 GMT", "rtt_ms": 850.0, "content_type": "application/json",
    }
    assert fetcher.requests[0].headers["Cookie"] == "TS01=placeholder-value", "values are sent, only names are stored"


# ---------------------------------------------------------------- RSS


def _rss_body(guids: list[str], *, prolog: str = "") -> bytes:
    items = "".join(
        f"<item><title>BBB: Công bố thông tin {g}</title><link>https://portal-b.example.invalid/{g}</link>"
        f"<guid isPermaLink=\"false\">{g}</guid><pubDate>Tue, 27 Oct 2026 10:00:00 +0700</pubDate></item>"
        for g in guids
    )
    return f'<?xml version="1.0" encoding="utf-8"?>{prolog}<rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


def test_rss_guids_are_extracted_without_an_xml_parser() -> None:
    prolog = '<!DOCTYPE rss [<!ENTITY boom "BOOM"><!ENTITY big "&boom;&boom;&boom;">]>'
    body = _rss_body(["700003", "700002"], prolog=prolog).replace(b"BBB: C", b"&big; BBB: C", 1)
    result = RssFeedSource(_rss_config(), FakeFetcher(_response(body, content_type="application/rss+xml"))).poll(_ctx({"700002"}))
    assert result.outage_kind is None
    assert [item.source_id for item in result.items] == ["700003", "700002"]
    first = result.items[0]
    assert first.payload_format is PayloadFormat.TEXT
    assert first.payload.startswith("<item>") and first.payload.endswith("</item>")
    assert "&big; BBB: Công bố" in first.payload and "BOOM" not in first.payload, "entities stay text"
    assert first.raw_sha256 == sha256_hex(first.payload.encode("utf-8"))
    assert first.snapshot_sha256 == sha256_hex(body)
    cdata = _rss_body(["x"]).replace(b'<guid isPermaLink="false">x</guid>', b"<guid><![CDATA[ 700004 ]]></guid>")
    assert [i.source_id for i in RssFeedSource(_rss_config(), FakeFetcher(_response(cdata))).poll(_ctx()).items] == ["700004"]


def test_rss_item_without_guid_is_invalid() -> None:
    body = _rss_body(["700003", "700002"]).replace(b'<guid isPermaLink="false">700002</guid>', b"")
    result = RssFeedSource(_rss_config(), FakeFetcher(_response(body))).poll(_ctx({"700003"}))
    assert result.outage_kind is OutageKind.SOURCE_INVALID_PAYLOAD
    assert [item.source_id for item in result.items] == ["700003"], "items that have a guid are kept"
    for broken in (_rss_body([]), b"\xff\xfe", _rss_body(["  "])):
        assert RssFeedSource(_rss_config(), FakeFetcher(_response(broken))).poll(_ctx()).outage_kind is (
            OutageKind.SOURCE_INVALID_PAYLOAD
        )


def test_rss_without_overlap_reports_coverage_overflow() -> None:
    body = _rss_body(["700010", "700009"])
    result = RssFeedSource(_rss_config(), FakeFetcher(_response(body))).poll(_ctx({"700001"}))
    assert result.outage_kind is OutageKind.COVERAGE_OVERFLOW and len(result.items) == 2
    # A hole in the guid sequence is not a miss: overlap is all that matters.
    holes = RssFeedSource(_rss_config(), FakeFetcher(_response(_rss_body(["700010", "700001"])))).poll(_ctx({"700001"}))
    assert holes.outage_kind is None
    first = RssFeedSource(_rss_config(), FakeFetcher(_response(body))).poll(_ctx(last_ok_at=None))
    assert first.outage_kind is None


# ---------------------------------------------------------------- UrllibFetcher


class FakeHttpResponse:
    """The slice of `http.client.HTTPResponse` the fetcher uses: `status`, `headers`, `read(n)`, `close()`."""

    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None,
                 chunk: int | None = None, on_read: Callable[[], None] | None = None) -> None:
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self._body, self._chunk, self._on_read = io.BytesIO(body), chunk, on_read
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self._on_read is not None:
            self._on_read()
        return self._body.read(min(size, self._chunk) if self._chunk else size)

    def close(self) -> None:
        pass


class FakeOpener:
    def __init__(self, answer: FakeHttpResponse | BaseException) -> None:
        self.answer = answer
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float) -> FakeHttpResponse:
        self.requests.append((request, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def test_urllib_fetcher_maps_http_errors_without_network() -> None:
    clock = FakeClock()
    headers = Message()
    headers["Date"] = "Tue, 27 Oct 2026 03:00:01 GMT"
    error = HTTPError(HOSE_URL, 403, "Forbidden", headers, io.BytesIO(b"blocked"))
    opener = FakeOpener(error)
    fetcher = UrllibFetcher(clock, "test-agent/0", 30.0, 60.0, max_body_bytes=1000, opener=opener)
    response = fetcher(HttpRequest("GET", HOSE_URL, params=(("pageIndex", "1"),), headers={"Origin": "https://o.invalid"}))
    assert (response.status, response.body, response.header("date")) == (403, b"blocked", "Tue, 27 Oct 2026 03:00:01 GMT")
    sent, timeout = opener.requests[0]
    assert sent.full_url == HOSE_URL + "?pageIndex=1" and timeout == 30.0
    assert sent.get_header("User-agent") == "test-agent/0" and sent.get_header("Origin") == "https://o.invalid"
    assert sent.get_header("Accept-encoding") == "gzip"
    ok = UrllibFetcher(clock, "test-agent/0", 30.0, 60.0, max_body_bytes=1000,
                       opener=FakeOpener(FakeHttpResponse(b"hello", headers={"Content-Type": "text/plain"})))
    answer = ok(HttpRequest("GET", HOSE_URL))
    assert (answer.status, answer.body, answer.received_at, answer.header("content-type")) == (200, b"hello", T0, "text/plain")


def test_oversized_body_is_invalid() -> None:
    clock = FakeClock()
    exact = UrllibFetcher(clock, "ua", 30.0, 60.0, max_body_bytes=100, opener=FakeOpener(FakeHttpResponse(b"x" * 100)))
    assert len(exact(HttpRequest("GET", RSS_URL)).body) == 100
    big = FakeHttpResponse(b"x" * 10_000)
    fetcher = UrllibFetcher(clock, "ua", 30.0, 60.0, max_body_bytes=100, opener=FakeOpener(big))
    with pytest.raises(SourceError) as caught:
        fetcher(HttpRequest("GET", RSS_URL))
    assert caught.value.kind is OutageKind.SOURCE_INVALID_PAYLOAD
    assert json.loads(caught.value.detail)["reason"] == "body_too_large"


def test_gzip_bomb_is_cut_at_max_body_bytes() -> None:
    clock = FakeClock()
    bomb = gzip.compress(b"\0" * 5_000_000)
    assert len(bomb) < 10_000
    response = FakeHttpResponse(bomb, headers={"Content-Encoding": "gzip"})
    fetcher = UrllibFetcher(clock, "ua", 30.0, 60.0, max_body_bytes=10_000, opener=FakeOpener(response))
    with pytest.raises(SourceError) as caught:
        fetcher(HttpRequest("GET", RSS_URL))
    assert caught.value.kind is OutageKind.SOURCE_INVALID_PAYLOAD
    assert json.loads(caught.value.detail)["reason"] == "body_too_large"
    small = gzip.compress("Đ tin".encode("utf-8"))
    ok = UrllibFetcher(clock, "ua", 30.0, 60.0, max_body_bytes=10_000,
                       opener=FakeOpener(FakeHttpResponse(small, headers={"Content-Encoding": "gzip"})))
    assert ok(HttpRequest("GET", RSS_URL)).body == "Đ tin".encode("utf-8")
    corrupt = UrllibFetcher(clock, "ua", 30.0, 60.0, max_body_bytes=10_000,
                            opener=FakeOpener(FakeHttpResponse(b"not gzip", headers={"Content-Encoding": "gzip"})))
    with pytest.raises(SourceError) as broken:
        corrupt(HttpRequest("GET", RSS_URL))
    assert broken.value.kind is OutageKind.SOURCE_INVALID_PAYLOAD


def test_trickling_body_hits_the_total_deadline() -> None:
    clock = FakeClock()
    trickle = FakeHttpResponse(b"y" * 1000, chunk=1, on_read=lambda: clock.advance(5.0))
    fetcher = UrllibFetcher(clock, "ua", 30.0, 60.0, max_body_bytes=10_000, opener=FakeOpener(trickle))
    with pytest.raises(SourceError) as caught:
        fetcher(HttpRequest("GET", RSS_URL))
    assert caught.value.kind is OutageKind.SOURCE_UNREACHABLE
    assert json.loads(caught.value.detail)["reason"] == "deadline"
    assert trickle.reads <= 13, "reading stops as soon as the 60 s deadline is passed"


# ---------------------------------------------------------------- vnstock


class FakeLimiter:
    def __init__(self, tokens: int = 100) -> None:
        self.tokens = tokens

    def try_acquire(self) -> bool:
        if self.tokens <= 0:
            return False
        self.tokens -= 1
        return True


class InlineWorker:
    """Runs the vendor call inline (no thread); `busy` stays False."""

    busy = False

    def __init__(self) -> None:
        self.calls = 0

    def run_with_timeout(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        self.calls += 1
        return fn()


class VendorError(Exception):
    def __init__(self, code: str, message: str = "vendor said no for AAA") -> None:
        super().__init__(message)
        self.code = code


def _rows(symbol: str, ids: list[str]) -> list[dict[str, Any]]:
    return [{"id": i, "symbol": symbol, "event_code": "DIV", "public_date": "2026-10-27T00:00:00.000+07:00"} for i in ids]


def _vnstock(call_events: Callable[[str, str], tuple[list[dict[str, Any]], dict[str, Any]]], *, limiter: FakeLimiter | None = None,
             worker: Any = None, clock: FakeClock | None = None, **config: Any) -> VnstockEventsSource:
    return VnstockEventsSource(
        _vnstock_config(**config), call_events, limiter=limiter or FakeLimiter(), clock=clock or FakeClock(),
        worker=worker or InlineWorker(),
    )


def test_vnstock_rows_become_items_with_prefixed_ids() -> None:
    calls: list[tuple[str, str]] = []

    def call_events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        calls.append((symbol, vendor_source))
        return _rows(symbol, [f"{symbol.lower()}-1", f"{symbol.lower()}-2"]), {}

    clock = FakeClock()
    result = _vnstock(call_events, clock=clock).poll(_ctx(cursor=0))
    assert result.outage_kind is None and result.pages == ()
    assert calls == [("AAA", "vci"), ("BBB", "vci")]
    assert [item.source_id for item in result.items] == ["vci:aaa-1", "vci:aaa-2", "vci:bbb-1", "vci:bbb-2"]
    item = result.items[0]
    assert item.payload == _rows("AAA", ["aaa-1"])[0] and item.payload_format is PayloadFormat.JSON
    assert item.snapshot_sha256 is None and item.first_seen_at == T0
    assert item.request == {"call": "Reference().company(symbol).events", "args": {"symbol": "AAA", "source": "vci"},
                            "library_version": None}
    assert item.response == {"rows": 2, "attrs": {}}
    assert result.next_cursor == 2
    missing_id = _vnstock(lambda s, v: ([{"symbol": s}], {})).poll(_ctx())
    assert missing_id.outage_kind is OutageKind.SOURCE_INVALID_PAYLOAD


def test_vnstock_round_robin_resumes_from_the_cursor() -> None:
    seen: list[str] = []

    def call_events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        seen.append(symbol)
        return [], {}

    source = _vnstock(call_events)
    assert source.poll(_ctx(cursor=3)).next_cursor == 1  # DDD, then wraps to AAA
    assert seen == ["DDD", "AAA"]
    seen.clear()
    limited = _vnstock(call_events, limiter=FakeLimiter(tokens=1))
    result = limited.poll(_ctx(cursor=1))
    assert seen == ["BBB"] and result.next_cursor == 2, "no token: the sweep stops and resumes at the next symbol"
    assert result.outage_kind is None


def test_vnstock_quota_warning_opens_an_outage_and_keeps_the_cursor(caplog: pytest.LogCaptureFixture) -> None:
    def warned(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if symbol == "BBB":
            warnings.warn("Kết quả bị cắt theo giới hạn của gói Community", UserWarning, stacklevel=1)
        return _rows(symbol, [f"{symbol}-1"]), {}

    result = _vnstock(warned).poll(_ctx(cursor=0))
    assert result.outage_kind is OutageKind.VENDOR_QUOTA_EXCEEDED
    assert [item.source_id for item in result.items] == ["vci:AAA-1", "vci:BBB-1"], "truncated rows are still real data"
    assert result.next_cursor == 1, "the cursor stays on BBB so it is asked again"
    detail = json.loads(result.detail or "{}")
    assert detail["symbol"] == "BBB" and "giới hạn" not in (result.detail or "")

    def noted(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return _rows(symbol, [f"{symbol}-1"]), {"note": "rows truncated by the daily quota", "dropped": 12}

    by_attrs = _vnstock(noted).poll(_ctx(cursor=2))
    assert by_attrs.outage_kind is OutageKind.VENDOR_QUOTA_EXCEEDED and by_attrs.next_cursor == 2
    assert by_attrs.items[0].response == {"rows": 1, "attrs": {"note": "rows truncated by the daily quota", "dropped": 12}}

    def update_nag(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        warnings.warn("A new version 9.9.9 is available for AAA", UserWarning, stacklevel=1)
        return [], {}

    with caplog.at_level("INFO"):
        nagged = _vnstock(update_nag).poll(_ctx(cursor=0))
    assert nagged.outage_kind is None and nagged.next_cursor == 2, "the update-available warning is logged and ignored"
    assert "9.9.9" not in caplog.text and "UserWarning" in caplog.text


def test_vnstock_call_timeout_is_an_outage_and_blocks_a_second_worker() -> None:
    class HangingWorker:
        def __init__(self) -> None:
            self.busy = False
            self.calls = 0

        def run_with_timeout(self, fn: Callable[[], Any], timeout_s: float) -> Any:
            self.calls += 1
            assert timeout_s == 60.0
            self.busy = True  # the thread cannot be killed: it is still running
            raise TimeoutError("vendor call exceeded 60.0 s")

    worker = HangingWorker()
    source = _vnstock(lambda s, v: ([], {}), worker=worker)
    first = source.poll(_ctx(cursor=1))
    assert first.outage_kind is OutageKind.SOURCE_UNREACHABLE
    assert json.loads(first.detail or "{}")["reason"] == "vendor_call_timeout" and first.next_cursor == 1
    second = source.poll(_ctx(cursor=1))
    assert second.outage_kind is OutageKind.SOURCE_UNREACHABLE
    assert json.loads(second.detail or "{}")["reason"] == "vendor_worker_busy"
    assert worker.calls == 1, "no second call while the hung one is alive"
    worker.busy = False
    source.poll(_ctx(cursor=1))
    assert worker.calls == 2


def test_vendor_worker_runs_on_a_daemon_thread_and_stays_busy_after_a_timeout() -> None:
    import threading

    worker = VendorWorker()
    assert worker.run_with_timeout(lambda: threading.current_thread().daemon, 5.0) is True
    with pytest.raises(VendorError):
        worker.run_with_timeout(lambda: (_ for _ in ()).throw(VendorError("X")), 5.0)
    release = threading.Event()
    with pytest.raises(TimeoutError):
        worker.run_with_timeout(release.wait, 0.0)  # join(0) returns at once: the call is still running
    assert worker.busy
    with pytest.raises(TimeoutError):
        worker.run_with_timeout(lambda: 1, 5.0)  # refused while busy
    release.set()
    worker.join_current()
    assert not worker.busy and worker.run_with_timeout(lambda: 2, 5.0) == 2


def test_vnstock_errors_map_to_outage_kinds() -> None:
    def failing(code: str | None, error_type: type[BaseException] = VendorError) -> OutageKind | None:
        def call_events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            if error_type is VendorError:
                raise VendorError(code or "")
            raise error_type("no vendor module")

        result = _vnstock(call_events).poll(_ctx())
        assert result.next_cursor == 0 and result.items == ()
        return result.outage_kind

    assert failing("VENDOR_FEATURE_GONE") is OutageKind.VENDOR_UNAVAILABLE
    assert failing("VENDOR_SHAPE") is OutageKind.SOURCE_INVALID_PAYLOAD
    assert failing("SOMETHING_NEW") is OutageKind.SOURCE_UNREACHABLE
    assert failing(None, ModuleNotFoundError) is OutageKind.VENDOR_UNAVAILABLE
    assert failing(None, RuntimeError) is OutageKind.SOURCE_UNREACHABLE

    def leaky(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raise VendorError("SOMETHING_NEW", "placeholder-value-2 rejected")

    result = _vnstock(leaky).poll(_ctx())
    detail = json.loads(result.detail or "{}")
    assert detail["class"] == "VendorError" and detail["code"] == "SOMETHING_NEW" and "placeholder-value-2" not in (result.detail or "")


def test_unknown_symbol_is_not_an_outage() -> None:
    def call_events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if symbol == "AAA":
            raise VendorError("VENDOR_SYMBOL_UNKNOWN", "symbol AAA not found")
        return _rows(symbol, ["1"]), {}

    result = _vnstock(call_events).poll(_ctx(cursor=0))
    assert result.outage_kind is None
    assert [item.source_id for item in result.items] == ["vci:1"]
    assert result.next_cursor == 2, "the unknown symbol is skipped, not retried forever"
    detail = json.loads(result.detail or "{}")
    assert detail["symbol_errors"] == [{"symbol": "AAA", "code": "VENDOR_SYMBOL_UNKNOWN", "class": "VendorError"}]


def test_telemetry_env_is_set_before_import() -> None:
    environ: dict[str, str] = {"DO_NOT_TRACK": "0"}  # an explicit owner value is kept (setdefault)
    imported: list[str] = []

    class FakeFrame:
        attrs = {"source": "vci"}

        def to_json(self, *, orient: str, date_format: str, force_ascii: bool) -> str:
            assert (orient, date_format, force_ascii) == ("records", "iso", False)
            return json.dumps(_rows("AAA", ["e1"]), ensure_ascii=False)

    def importer(name: str) -> Any:
        assert name == "vnstock"
        for key, value in TELEMETRY_ENV.items():
            assert key in environ, f"{key} must be set before the import"
        imported.append(name)
        company = SimpleNamespace(events=lambda source: FakeFrame())
        return SimpleNamespace(__version__="5.1.0", Reference=lambda: SimpleNamespace(company=lambda symbol: company))

    assert set(TELEMETRY_ENV) == {"DO_NOT_TRACK", "VNSTOCK_TELEMETRY", "VNSTOCK_DISABLE_AGENT_SETUP", "VNSTOCK_DISABLE_GLOBAL_AGENT"}
    source = VnstockEventsSource(_vnstock_config(symbols_per_poll=1), None, limiter=FakeLimiter(), clock=FakeClock(),
                                 worker=InlineWorker(), environ=environ, importer=importer)
    assert imported == [], "nothing is imported before the first poll"
    result = source.poll(_ctx())
    assert imported == ["vnstock"] and environ["DO_NOT_TRACK"] == "0" and environ["VNSTOCK_TELEMETRY"] == "0"
    assert [item.source_id for item in result.items] == ["vci:e1"]
    assert result.items[0].request["library_version"] == "5.1.0"
    assert result.items[0].response == {"rows": 1, "attrs": {"source": "vci"}}


def test_vnstock_sweep_stops_at_the_poll_budget() -> None:
    """A slow vendor (still under its call timeout) must not stretch one poll past `max_poll_duration_s`: the sweep stops
    before a call that could end after the budget, the poll stays ok, and the cursor waits on the first symbol not asked."""
    clock = FakeClock()
    asked: list[str] = []

    def slow(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        asked.append(symbol)
        clock.advance(100.0)  # each call takes 100 s, under the 140 s vendor call timeout
        return _rows(symbol, [f"{symbol}-1"]), {}

    source = VnstockEventsSource(
        _vnstock_config(symbols_per_poll=4, vendor_call_timeout_s=140.0), slow, limiter=FakeLimiter(), clock=clock,
        worker=InlineWorker(), max_poll_duration_s=330.0,
    )
    result = source.poll(_ctx(cursor=1))
    # 0 s: BBB (ends 100); 100 s: 100 + 140 <= 330, CCC (ends 200); 200 s: 200 + 140 > 330, stop before DDD.
    assert asked == ["BBB", "CCC"]
    assert result.outage_kind is None, "stopping on the budget is not an outage"
    assert result.next_cursor == 3, "the cursor waits on DDD, the first symbol not asked"
    assert json.loads(result.detail or "{}") == {"reason": "poll_budget", "symbols_asked": 2}
    assert [item.source_id for item in result.items] == ["vci:BBB-1", "vci:CCC-1"]

    starved = VnstockEventsSource(
        _vnstock_config(symbols_per_poll=2, vendor_call_timeout_s=140.0), slow, limiter=FakeLimiter(), clock=clock,
        worker=InlineWorker(), max_poll_duration_s=100.0,
    )
    asked.clear()
    assert starved.poll(_ctx(cursor=0)).next_cursor == 1 and asked == ["AAA"], "at least one symbol is always asked"

    unbounded = _vnstock(slow, clock=clock, symbols_per_poll=4, vendor_call_timeout_s=140.0)
    asked.clear()
    assert unbounded.poll(_ctx(cursor=0)).detail is None and len(asked) == 4, "no budget given: the whole sweep runs"


# ---------------------------------------------------------------- code review fixes (2026-10-28)


def test_hose_window_reaches_back_to_the_last_ok_day_after_a_multi_day_gap() -> None:
    monday = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)  # 09:00 ICT
    friday_close = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)  # 16:00 ICT
    source, fetcher, _ = _hose(_response(_hose_body([5, 4]), at=monday))
    result = source.poll(_ctx(now=monday, last_ok_at=friday_close))
    params = dict(fetcher.requests[0].params)
    assert (params["startDate"], params["endDate"]) == ("2026-10-23", "2026-10-26"), "Friday evening and Saturday too"
    assert result.outage_kind is None


def test_hose_first_poll_just_after_midnight_still_covers_yesterday() -> None:
    # HOSE shows an item 23-52 s after its posted time (OBSERVED 2026-10-27): the poll at 00:00:30 ICT can miss an item
    # posted at 23:59:50, so the next poll still asks for yesterday.
    last_ok = datetime(2026, 10, 27, 17, 0, 30, tzinfo=timezone.utc)  # 00:00:30 ICT on 2026-10-28
    now = last_ok + timedelta(minutes=60)
    source, fetcher, _ = _hose(_response(_hose_body([]), at=now))
    source.poll(_ctx(now=now, last_ok_at=last_ok))
    params = dict(fetcher.requests[0].params)
    assert (params["startDate"], params["endDate"]) == ("2026-10-27", "2026-10-28")


def test_hose_lookback_is_capped_and_noted_on_an_ok_poll() -> None:
    long_ago = T0 - timedelta(days=30)
    source, fetcher, _ = _hose(_response(_hose_body([7])))
    result = source.poll(_ctx(last_ok_at=long_ago))
    params = dict(fetcher.requests[0].params)
    assert params["startDate"] == "2026-10-20", "seven ICT days back at most"
    assert [item.source_id for item in result.items] == ["7"], "the items found are still captured"
    assert result.outage_kind is None, "an outage would keep last_ok stale and cap every later poll too"
    assert json.loads(result.detail or "{}") == {"lookback_cap_days": 7, "unasked_before": "2026-10-20"}


def test_hose_entry_that_is_not_strict_json_is_skipped_and_noted() -> None:
    # A title cut inside a UTF-16 surrogate pair parses as a lone surrogate, which has no UTF-8 encoding.
    body = (b'{"success": true, "data": {"list": [{"id": 3, "title": "AAA \\ud83d"}, {"id": 2, "title": "BBB"},'
            b' {"id": 1, "title": "CCC"}]}}')
    source, _, _ = _hose(_response(body))
    result = source.poll(_ctx({"1"}))
    assert result.outage_kind is None, "one bad entry must not black out the source"
    assert [item.source_id for item in result.items] == ["2", "1"]
    assert len(result.pages) == 1 and result.pages[0].body == body, "the page snapshot keeps the skipped entry's bytes"
    assert json.loads(result.detail or "{}") == {"skipped_items": [{"id": "3", "reason": "not_strict_json"}]}


def test_vnstock_row_that_is_not_strict_json_is_skipped_and_the_cursor_moves_on() -> None:
    def call_events(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = _rows(symbol, [f"{symbol.lower()}-1", f"{symbol.lower()}-2"])
        if symbol == "AAA":
            rows[0]["title"] = "\ud83d"
        return rows, {}

    result = _vnstock(call_events).poll(_ctx(cursor=0))
    assert result.outage_kind is None
    assert [item.source_id for item in result.items] == ["vci:aaa-2", "vci:bbb-1", "vci:bbb-2"]
    assert result.next_cursor == 2, "the sweep goes past the symbol with the bad row"
    detail = json.loads(result.detail or "{}")
    assert detail["skipped_items"] == [{"id": "vci:aaa-1", "reason": "not_strict_json"}]


def test_rss_items_are_cut_in_linear_time() -> None:
    # A body of unclosed `<item>` tags made the old lazy regex quadratic while holding the GIL, so the watchdog thread
    # could not fire (code review F6). Run in a child process so a regression fails on the timeout instead of hanging.
    import subprocess
    import sys

    script = (
        "from finsec_alpharadar.capture.sources import rss_item_fragments\n"
        "body = '<rss><channel>' + '<item><title>x</title>' * 200_000 + '</channel></rss>'\n"
        "assert rss_item_fragments(body) == []\n"
        "assert rss_item_fragments('<ITEM a=\"1\"><guid>9</guid></Item><item><guid>8</guid></item>') == "
        "['<ITEM a=\"1\"><guid>9</guid></Item>', '<item><guid>8</guid></item>']\n"
    )
    import os
    from pathlib import Path

    import finsec_alpharadar

    src = str(Path(finsec_alpharadar.__file__).resolve().parent.parent)  # the child does not get pytest's `pythonpath`
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (src, os.environ.get("PYTHONPATH"))))}
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script], capture_output=True, text=True, timeout=60, env=env
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_rss_guid_of_an_unclosed_tag_is_none_and_the_item_is_invalid() -> None:
    body = "<rss><channel><item><title>AAA</title><guid>1</guid></item><item><guid>2<guid>3</item></channel></rss>"
    source = RssFeedSource(_rss_config(), FakeFetcher(_response(body.encode("utf-8"), content_type="application/rss+xml")))
    result = source.poll(_ctx(set()))
    assert [item.source_id for item in result.items] == ["1"]
    assert result.outage_kind is OutageKind.SOURCE_INVALID_PAYLOAD
    assert json.loads(result.detail or "{}") == {"reason": "item_without_guid", "items": 1}


def test_vendor_base_exception_becomes_an_outage_not_an_exit() -> None:
    def exits(symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        raise SystemExit(1)  # a vendor licence or expiry gate that calls sys.exit()

    worker = VendorWorker()
    result = _vnstock(exits, worker=worker).poll(_ctx(cursor=0))
    worker.join_current()
    assert result.outage_kind is OutageKind.SOURCE_UNREACHABLE and result.next_cursor == 0
    assert json.loads(result.detail or "{}")["class"] == "VendorAborted"
