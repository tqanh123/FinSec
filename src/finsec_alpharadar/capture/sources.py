"""Capture sources (plan 261022-1147 phase 2, R2.8): the HTTP fetcher and the three adapters.

- `UrllibFetcher`: the only HTTP client in `src/`. A total deadline per request (the socket timeout bounds ONE read
  only, red-team r1 RT2), a body size cap that also bounds gzip decompression (LT9), and failures mapped to `SourceError`.
- `HoseNewsApiSource` (HOSE JSON listing), `RssFeedSource` (HNX RSS feed 3, no XML parser), `VnstockEventsSource`
  (vnstock `events()`, imported lazily inside a worker thread with a timeout).

Capture only extracts ids. Each adapter makes ONE attempt per `poll()` (paging included) and always returns a
`PollResult`: the items it got, the 200-response bodies (`pages`, for snapshots), and an `outage_kind` + JSON `detail`
when the poll failed. Items and an outage can coexist (coverage overflow, vendor quota): the items are real data and
must be archived. Retries, `pit_quality`, records and state belong to the poller. Untrusted payloads are never
evaluated; stored request descriptors carry header NAMES only; details carry class, code, sha256 and length, never text.
"""
from __future__ import annotations

import http.client
import importlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import warnings
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, MutableMapping, Protocol
from urllib.parse import urlencode

from finsec_alpharadar.capture.config import SourceConfig, SourceKind
from finsec_alpharadar.capture.record import (
    CaptureRecordError,
    OutageKind,
    PayloadFormat,
    canonical_item_bytes,
    detail_json,
    error_detail,
    message_fingerprint,
    sha256_hex,
)
from finsec_alpharadar.capture.schedule import local_zone

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 64 * 1024
# Set (setdefault: an explicit owner value wins) before vnstock is imported (research vnstock-gen5.md; P1 probe).
TELEMETRY_ENV: Mapping[str, str] = {
    "DO_NOT_TRACK": "1",
    "VNSTOCK_TELEMETRY": "0",
    "VNSTOCK_DISABLE_AGENT_SETUP": "1",
    "VNSTOCK_DISABLE_GLOBAL_AGENT": "1",
}
VNSTOCK_EVENTS_CALL = "Reference().company(symbol).events"
# Opening and closing tags searched separately from a moving position: the lazy `<item>.*?</item>` was quadratic on a body
# of unclosed tags and held the GIL, so the watchdog thread could not fire (code review F6).
_RSS_ITEM_OPEN = re.compile(r"<item\b[^<>]*>", re.IGNORECASE)
_RSS_ITEM_CLOSE = re.compile(r"</item\s*>", re.IGNORECASE)
_RSS_GUID_OPEN = re.compile(r"<guid\b[^<>]*>", re.IGNORECASE)
_RSS_GUID_CLOSE = re.compile(r"</guid\s*>", re.IGNORECASE)
_CDATA = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.DOTALL)


class Clock(Protocol):
    """Injected time: tz-aware UTC wall clock and a monotonic counter in seconds (`capture/host.py` has the real one)."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


Sleeper = Callable[[float], None]


class RateLimiter(Protocol):
    """Non-blocking token source (the poller's `TokenBucket`): True when one vendor call may run now."""

    def try_acquire(self) -> bool: ...


class Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> Any: ...


class SourceError(Exception):
    """A classified failure of one request or vendor call. `detail` is JSON from `error_detail` / `detail_json`."""

    def __init__(self, kind: OutageKind, detail: str, *, http_status: int | None = None) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail
        self.http_status = http_status


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def full_url(self) -> str:
        return f"{self.url}?{urlencode(self.params)}" if self.params else self.url

    def descriptor(self) -> dict[str, Any]:
        """The stored `request` object: method, url, params and header NAMES (never values: cookies, origins)."""
        return {"method": self.method, "url": self.url, "params": dict(self.params), "header_names": sorted(self.headers)}


@dataclass(frozen=True)
class HttpResponse:
    """`headers` keys are lower case; `received_at` is the injected clock when the headers arrived; `rtt_ms` is the
    monotonic time from sending to the headers."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    received_at: datetime
    rtt_ms: float

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def descriptor(self) -> dict[str, Any]:
        """The stored `response` object."""
        return {
            "status": self.status,
            "http_date": self.header("date"),
            "rtt_ms": self.rtt_ms,
            "content_type": self.header("content-type"),
        }


Fetcher = Callable[[HttpRequest], HttpResponse]


class UrllibFetcher:
    """`Fetcher` over urllib. An HTTP error status is a normal `HttpResponse`; a transport error, a timeout or the total
    `deadline_s` raises `SourceError(source_unreachable)`; a body over `max_body_bytes` (raw or after gzip) or a broken
    gzip stream raises `SourceError(source_invalid_payload)`.
    """

    def __init__(
        self,
        clock: Clock,
        user_agent: str,
        timeout_s: float,
        deadline_s: float,
        *,
        max_body_bytes: int,
        opener: Opener | None = None,
    ) -> None:
        self._clock = clock
        self._user_agent = user_agent
        self._timeout_s = timeout_s
        self._deadline_s = deadline_s
        self._max_body_bytes = max_body_bytes
        self._opener: Opener = opener if opener is not None else urllib.request.build_opener()

    @classmethod
    def for_source(cls, config: SourceConfig, clock: Clock, user_agent: str, *, opener: Opener | None = None) -> UrllibFetcher:
        return cls(clock, user_agent, config.timeout_s, config.deadline_s, max_body_bytes=config.max_body_bytes, opener=opener)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        headers = {**request.headers, "User-Agent": self._user_agent, "Accept-Encoding": "gzip"}
        sent = urllib.request.Request(request.full_url, headers=headers, method=request.method)
        start = self._clock.monotonic()
        try:
            try:
                response = self._opener.open(sent, timeout=self._timeout_s)
                status = int(response.status)
            except urllib.error.HTTPError as err:  # a status page is an answer, not a transport failure
                response, status = err, int(err.code)
            try:
                received_at = self._clock.now()
                rtt_ms = (self._clock.monotonic() - start) * 1000.0
                header_items = response.headers.items() if response.headers is not None else []
                lowered = {str(name).lower(): str(value) for name, value in header_items}
                raw = self._read(response, start)
            finally:
                response.close()
        except SourceError:
            raise
        except (OSError, http.client.HTTPException) as err:  # URLError, TimeoutError, resets, bad status lines
            raise SourceError(OutageKind.SOURCE_UNREACHABLE, error_detail(err)) from err
        body = self._decode(raw, lowered.get("content-encoding"))
        return HttpResponse(status=status, headers=lowered, body=body, received_at=received_at, rtt_ms=rtt_ms)

    def _check_deadline(self, start: float) -> None:
        elapsed = self._clock.monotonic() - start
        if elapsed > self._deadline_s:
            raise SourceError(OutageKind.SOURCE_UNREACHABLE, detail_json(reason="deadline", deadline_s=self._deadline_s))

    def _too_large(self) -> SourceError:
        return SourceError(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason="body_too_large", max_body_bytes=self._max_body_bytes))

    def _read(self, response: Any, start: float) -> bytes:
        """At most `max_body_bytes + 1` raw bytes, in chunks, checking the total deadline after every read."""
        chunks: list[bytes] = []
        total = 0
        limit = self._max_body_bytes + 1
        while True:
            self._check_deadline(start)
            chunk = response.read(min(READ_CHUNK_BYTES, limit - total))
            self._check_deadline(start)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > self._max_body_bytes:
                raise self._too_large()

    def _decode(self, raw: bytes, encoding: str | None) -> bytes:
        if encoding is None or encoding.strip().lower() in ("", "identity"):
            return raw
        if encoding.strip().lower() != "gzip":
            raise SourceError(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason="unsupported_encoding"))
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            body = inflater.decompress(raw, self._max_body_bytes + 1)
        except zlib.error as err:
            raise SourceError(OutageKind.SOURCE_INVALID_PAYLOAD, error_detail(err, reason="bad_gzip")) from err
        if len(body) > self._max_body_bytes or inflater.unconsumed_tail:
            raise self._too_large()
        if not inflater.eof or inflater.unused_data:
            raise SourceError(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason="bad_gzip"))
        return body


def classify_status(status: int) -> OutageKind | None:
    """200 -> None; 401/403/429 (WAF, rate limit) -> rejected; 5xx -> unreachable; anything else -> invalid payload."""
    if status == 200:
        return None
    if status in (401, 403, 429):
        return OutageKind.SOURCE_REJECTED
    if status >= 500:
        return OutageKind.SOURCE_UNREACHABLE
    return OutageKind.SOURCE_INVALID_PAYLOAD


@dataclass(frozen=True)
class PollContext:
    """What a source may know about its past: no database access. `is_known(source_id)` is True when ANY version of
    that id was seen (overlap test); `cursor` is the vnstock round-robin position (from `kv`)."""

    now: datetime
    last_ok_at: datetime | None
    is_known: Callable[[str], bool]
    cursor: int = 0


@dataclass(frozen=True)
class CapturedPage:
    """A 200-response body, for the snapshot record (written by the poller when its sha differs from the last one)."""

    body: bytes
    sha256: str
    request: Mapping[str, Any]
    response: Mapping[str, Any]
    received_at: datetime


@dataclass(frozen=True)
class CapturedItem:
    """One item, ready for a `CaptureRecord` (the poller adds pit_quality, cycle_id, ingested_at, capture_version)."""

    source_id: str
    payload: Any
    payload_format: PayloadFormat
    raw_sha256: str
    byte_count: int
    first_seen_at: datetime
    request: Mapping[str, Any]
    response: Mapping[str, Any]
    snapshot_sha256: str | None


@dataclass(frozen=True)
class PollResult:
    """Outcome of one attempt. `outage_kind is None` = ok; `detail` may also note per-symbol errors of an ok vnstock
    poll. `http_*` / `received_at` describe the last HTTP response (None for vnstock or a transport failure)."""

    items: tuple[CapturedItem, ...] = ()
    pages: tuple[CapturedPage, ...] = ()
    outage_kind: OutageKind | None = None
    detail: str | None = None
    next_cursor: int | None = None
    http_status: int | None = None
    http_date: str | None = None
    rtt_ms: float | None = None
    received_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.outage_kind is None


class CaptureSource(Protocol):
    config: SourceConfig

    def poll(self, ctx: PollContext) -> PollResult: ...


class _InvalidPayload(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Collector:
    """Accumulates one poll's pages and items (deduplicated on (source_id, raw_sha256)) and the last response facts."""

    def __init__(self) -> None:
        self.items: list[CapturedItem] = []
        self.pages: list[CapturedPage] = []
        self._keys: set[tuple[str, str]] = set()
        self._last: HttpResponse | None = None

    def response(self, response: HttpResponse) -> None:
        self._last = response

    def page(self, request: HttpRequest, response: HttpResponse) -> CapturedPage:
        page = CapturedPage(response.body, sha256_hex(response.body), request.descriptor(), response.descriptor(), response.received_at)
        self.pages.append(page)
        return page

    def item(
        self,
        source_id: str,
        payload: Any,
        *,
        first_seen_at: datetime,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        snapshot_sha256: str | None,
    ) -> None:
        canonical = canonical_item_bytes(payload)
        sha = sha256_hex(canonical)
        if (source_id, sha) in self._keys:
            return
        self._keys.add((source_id, sha))
        payload_format = PayloadFormat.TEXT if isinstance(payload, str) else PayloadFormat.JSON
        self.items.append(
            CapturedItem(source_id, payload, payload_format, sha, len(canonical), first_seen_at, request, response, snapshot_sha256)
        )

    def try_item(self, source_id: str, payload: Any, **fields: Any) -> bool:
        """`item`, but False (and a warning with the id only) when the payload is not strict JSON (code review F4)."""
        try:
            self.item(source_id, payload, **fields)
        except CaptureRecordError as err:
            logger.warning("item skipped: source_id=%s reason=not_strict_json class=%s", source_id, type(err.__cause__).__name__)
            return False
        return True

    def result(self, kind: OutageKind | None = None, detail: str | None = None, *, next_cursor: int | None = None) -> PollResult:
        last = self._last
        return PollResult(
            items=tuple(self.items),
            pages=tuple(self.pages),
            outage_kind=kind,
            detail=detail,
            next_cursor=next_cursor,
            http_status=last.status if last else None,
            http_date=last.header("date") if last else None,
            rtt_ms=last.rtt_ms if last else None,
            received_at=last.received_at if last else None,
        )


def _noted(
    collector: _Collector, kind: OutageKind | None, skipped: list[dict[str, str]], *, next_cursor: int | None = None,
    **detail: Any,
) -> PollResult:
    """The result, with the skipped items (ids and reasons only) added to its detail."""
    if skipped:
        detail["skipped_items"] = skipped
    return collector.result(kind, detail_json(**detail) if detail else None, next_cursor=next_cursor)


def _fetch(fetcher: Fetcher, request: HttpRequest, collector: _Collector) -> HttpResponse | PollResult:
    """One request: the response when it is a 200, else the failed `PollResult` (keeping what was collected)."""
    try:
        response = fetcher(request)
    except SourceError as err:
        return collector.result(err.kind, err.detail)
    collector.response(response)
    kind = classify_status(response.status)
    if kind is not None:
        return collector.result(kind, detail_json(reason="http_status", http_status=response.status))
    return response


def _hose_entries(body: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as err:  # includes UnicodeDecodeError
        raise _InvalidPayload("not_json") from err
    if not isinstance(data, dict) or data.get("success") is not True:
        raise _InvalidPayload("unsuccessful")
    inner = data.get("data")
    entries = inner.get("list") if isinstance(inner, dict) else None
    if not isinstance(entries, list):
        raise _InvalidPayload("no_list")
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("id")) is not int:
            raise _InvalidPayload("bad_id")
    return entries


# HOSE shows an item 23-52 s after its posted time (OBSERVED 2026-10-27): a poll this soon after ICT midnight can miss an
# item of the day before, so the next poll still asks for that day.
HOSE_VISIBILITY_MARGIN = timedelta(minutes=5)
HOSE_MAX_LOOKBACK_DAYS = 7  # a longer gap is noted in polls.detail, not a request for weeks of listing


class HoseNewsApiSource:
    """HOSE disclosure listing (`securitiesType/0`): JSON pages of `page_size`, ICT dates, paging while a page is all new.

    `startDate` is the ICT day of the last ok poll minus `HOSE_VISIBILITY_MARGIN` (yesterday without one), at most
    `HOSE_MAX_LOOKBACK_DAYS` back, so no day between two ok polls goes unasked after a gap (red-team r1 RT13; code
    review F1). An entry that is not strict JSON is skipped and noted; the page snapshot keeps its bytes.
    """

    def __init__(self, config: SourceConfig, fetcher: Fetcher, *, sleeper: Sleeper) -> None:
        if config.kind is not SourceKind.HOSE_NEWS_API or config.url is None or config.page_size is None:
            raise ValueError(f"source {config.name} is not a hose_news_api source")
        self.config = config
        self._fetcher = fetcher
        self._sleeper = sleeper

    def _dates(self, ctx: PollContext) -> tuple[date, date, bool]:
        """(startDate, endDate, capped): capped when the lookback limit cut the window short."""
        zone = local_zone()
        today = ctx.now.astimezone(zone).date()
        if ctx.last_ok_at is None:
            return today - timedelta(days=1), today, False
        wanted = min((ctx.last_ok_at - HOSE_VISIBILITY_MARGIN).astimezone(zone).date(), today)
        earliest = today - timedelta(days=HOSE_MAX_LOOKBACK_DAYS)
        return max(wanted, earliest), today, wanted < earliest

    def poll(self, ctx: PollContext) -> PollResult:
        cfg = self.config
        assert cfg.url is not None and cfg.page_size is not None  # checked in __init__
        start, end, capped = self._dates(ctx)
        collector = _Collector()
        skipped: list[dict[str, str]] = []
        for page_index in range(1, cfg.max_pages + 1):
            if page_index > 1:
                self._sleeper(cfg.min_request_spacing_s)
            params = (
                ("pageIndex", str(page_index)),
                ("pageSize", str(cfg.page_size)),
                ("startDate", start.isoformat()),
                ("endDate", end.isoformat()),
            )
            request = HttpRequest("GET", cfg.url, params=params, headers=cfg.headers)
            answer = _fetch(self._fetcher, request, collector)
            if isinstance(answer, PollResult):
                return answer
            page = collector.page(request, answer)
            try:
                entries = _hose_entries(answer.body)
            except _InvalidPayload as err:
                return collector.result(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason=err.reason, page=page_index))
            for entry in entries:
                source_id = str(entry["id"])
                if not collector.try_item(
                    source_id, entry, first_seen_at=answer.received_at, request=page.request,
                    response=page.response, snapshot_sha256=page.sha256,
                ):
                    skipped.append({"id": source_id, "reason": "not_strict_json"})
            all_new = bool(entries) and not any(ctx.is_known(str(entry["id"])) for entry in entries)
            if not all_new or len(entries) < cfg.page_size:
                break
        else:
            if ctx.last_ok_at is not None:  # nothing to overlap with on a first poll: its items are late anyway
                return _noted(collector, OutageKind.COVERAGE_OVERFLOW, skipped, reason="page_cap", pages=cfg.max_pages)
        if capped:  # a note on an ok poll, not an outage: an outage keeps last_ok stale, so every later poll would cap too.
            # The gap itself is already an outage (process_down, ...) and its items are late by the gap deadline.
            return _noted(collector, None, skipped, lookback_cap_days=HOSE_MAX_LOOKBACK_DAYS, unasked_before=start.isoformat())
        return _noted(collector, None, skipped)


def rss_item_fragments(text: str) -> list[str]:
    """Every `<item ...>...</item>` fragment, in linear time; an item without a closing tag ends the list."""
    fragments: list[str] = []
    position = 0
    while (opening := _RSS_ITEM_OPEN.search(text, position)) is not None:
        closing = _RSS_ITEM_CLOSE.search(text, opening.end())
        if closing is None:
            break
        fragments.append(text[opening.start() : closing.end()])
        position = closing.end()
    return fragments


def _guid(fragment: str) -> str | None:
    """The text of the first `<guid>`, or None when it has no closing tag."""
    opening = _RSS_GUID_OPEN.search(fragment)
    closing = _RSS_GUID_CLOSE.search(fragment, opening.end()) if opening is not None else None
    if opening is None or closing is None:
        return None
    text = fragment[opening.end() : closing.start()].strip()
    cdata = _CDATA.match(text)
    if cdata is not None:
        text = cdata.group(1).strip()
    return text or None


class RssFeedSource:
    """An RSS feed read as text: `<item>` fragments and their `<guid>` cut out by regex (no XML parser, so no entity
    expansion). The item payload is the verbatim fragment; internal `<link>` targets are never followed."""

    def __init__(self, config: SourceConfig, fetcher: Fetcher) -> None:
        if config.kind is not SourceKind.RSS_FEED or config.url is None:
            raise ValueError(f"source {config.name} is not an rss_feed source")
        self.config = config
        self._fetcher = fetcher

    def poll(self, ctx: PollContext) -> PollResult:
        assert self.config.url is not None  # checked in __init__
        collector = _Collector()
        request = HttpRequest("GET", self.config.url, headers=self.config.headers)
        answer = _fetch(self._fetcher, request, collector)
        if isinstance(answer, PollResult):
            return answer
        page = collector.page(request, answer)
        try:
            text = answer.body.decode("utf-8")
        except UnicodeDecodeError:
            return collector.result(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason="not_utf8"))
        fragments = rss_item_fragments(text)
        if not fragments:
            return collector.result(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason="no_items"))
        guids: list[str] = []
        missing = 0
        for fragment in fragments:
            guid = _guid(fragment)
            if guid is None:
                missing += 1
                continue
            guids.append(guid)
            collector.item(
                guid, fragment, first_seen_at=answer.received_at, request=page.request, response=page.response,
                snapshot_sha256=page.sha256,
            )
        if missing:
            return collector.result(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason="item_without_guid", items=missing))
        # A hole in the guid sequence is not a miss (research vn-disclosure-portals.md §2); no overlap at all is.
        if ctx.last_ok_at is not None and not any(ctx.is_known(guid) for guid in guids):
            return collector.result(OutageKind.COVERAGE_OVERFLOW, detail_json(reason="no_overlap", items=len(guids)))
        return collector.result()


class VendorWorkerBusy(TimeoutError):
    """The previous vendor call timed out and its thread is still running."""


class VendorAborted(RuntimeError):
    """A vendor call raised a `BaseException` that is not an `Exception` (SystemExit, KeyboardInterrupt, ...)."""


class VendorWorker:
    """Runs one vendor call at a time on a daemon thread and waits at most `timeout_s` for it.

    A thread cannot be killed: after a timeout the worker stays `busy` until the hung call returns, and refuses new calls
    meanwhile (no second worker). Daemon, so a hung call never blocks the interpreter's exit.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_with_timeout(self, fn: Callable[[], Any], timeout_s: float) -> Any:
        if self.busy:
            raise VendorWorkerBusy("the previous vendor call is still running")
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = fn()
            except Exception as err:  # handed to the caller's thread below, never swallowed
                box["error"] = err
            except BaseException as err:  # sys.exit() in a vendor gate must not end capture (code review F7)
                box["error"] = VendorAborted(f"vendor call raised {type(err).__name__}")

        thread = threading.Thread(target=target, name="capture-vendor-call", daemon=True)
        self._thread = thread
        thread.start()
        thread.join(timeout_s)
        if thread.is_alive():
            raise TimeoutError(f"vendor call still running after {timeout_s} s")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def join_current(self) -> None:
        """Wait for the current call to end (shutdown and tests)."""
        if self._thread is not None:
            self._thread.join()


class VendorCaller(Protocol):
    busy: bool

    def run_with_timeout(self, fn: Callable[[], Any], timeout_s: float) -> Any: ...


CallEvents = Callable[[str, str], tuple[list[dict[str, Any]], dict[str, Any]]]


@dataclass(frozen=True)
class _VendorAnswer:
    rows: list[Any]
    attrs: dict[str, Any]
    caught: list[warnings.WarningMessage]


class VnstockEventsSource:
    """vnstock `events()` over a round-robin of `symbols` (secondary source, D2).

    Each poll asks up to `symbols_per_poll` symbols from `ctx.cursor`, one limiter token per call, each call on the
    worker with `vendor_call_timeout_s`. `source_id` is `<vnstock_source>:<row id>`. A quota/truncation warning or attrs
    note keeps the rows, ends the poll with `vendor_quota_exceeded` and leaves the cursor on that symbol. A code in
    `symbol_error_codes` is noted in `detail` and skipped; a code in `error_map` maps to its outage; an import failure is
    `vendor_unavailable`; anything else is `source_unreachable` with the class name. vnstock is imported only by the
    default `call_events`, inside the worker, after `TELEMETRY_ENV` is set.

    With `max_poll_duration_s` (the poller's per-poll budget) the sweep stops before a call that could end after the
    budget (elapsed + `vendor_call_timeout_s` > budget; the first symbol is always asked). That stop is not an outage:
    the poll stays ok, `detail` says `reason: "poll_budget"` with `symbols_asked`, and the cursor waits on the first
    symbol not asked.
    """

    def __init__(
        self,
        config: SourceConfig,
        call_events: CallEvents | None = None,
        *,
        limiter: RateLimiter,
        clock: Clock,
        worker: VendorCaller | None = None,
        environ: MutableMapping[str, str] | None = None,
        importer: Callable[[str], Any] | None = None,
        max_poll_duration_s: float | None = None,
    ) -> None:
        if config.kind is not SourceKind.VNSTOCK_EVENTS or not config.symbols or config.vnstock_source is None:
            raise ValueError(f"source {config.name} is not a vnstock_events source")
        self.config = config
        self._call_events: CallEvents = call_events if call_events is not None else self._vendor_call_events
        self._limiter = limiter
        self._clock = clock
        self._worker: VendorCaller = worker if worker is not None else VendorWorker()
        self._environ: MutableMapping[str, str] = environ if environ is not None else os.environ
        self._importer: Callable[[str], Any] = importer if importer is not None else importlib.import_module
        self._reference: Any = None
        self.library_version: str | None = None
        self._budget_s = max_poll_duration_s

    def _vendor_call_events(self, symbol: str, vendor_source: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The real call: `Reference().company(symbol).events(source=...)`, rows via `to_json` (JSON-safe values)."""
        if self._reference is None:
            for key, value in TELEMETRY_ENV.items():
                self._environ.setdefault(key, value)
            module = self._importer("vnstock")
            version = getattr(module, "__version__", None)
            self.library_version = str(version) if version is not None else None
            self._reference = module.Reference()
        frame = self._reference.company(symbol).events(source=vendor_source)
        rows = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
        attrs = json.loads(json.dumps(dict(getattr(frame, "attrs", {}) or {}), ensure_ascii=False, default=str))
        return rows, attrs

    def _call_with_warnings(self, symbol: str) -> _VendorAnswer:
        """Runs inside the worker: one call with its warnings recorded (one worker, so the global filter is not shared)."""
        assert self.config.vnstock_source is not None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rows, attrs = self._call_events(symbol, self.config.vnstock_source)
        return _VendorAnswer(list(rows) if isinstance(rows, list) else rows, dict(attrs or {}), list(caught))

    def _quota_hit(self, answer: _VendorAnswer) -> list[str]:
        """Categories of the quota/truncation signals in the warnings or the attrs; logs the other warnings (no text)."""
        patterns = [p.lower() for p in self.config.quota_warning_patterns]
        ignored = [p.lower() for p in self.config.ignore_warning_patterns]
        hits: list[str] = []
        for caught in answer.caught:
            text = str(caught.message)
            category = caught.category.__name__
            lowered = text.lower()
            fingerprint = message_fingerprint(text)
            if any(p in lowered for p in patterns):
                hits.append(category)
                continue
            known = "ignored" if any(p in lowered for p in ignored) else "unclassified"
            logger.info(
                "vendor warning %s: source=%s category=%s message_sha256=%s message_len=%d",
                known, self.config.name, category, fingerprint["message_sha256"], fingerprint["message_len"],
            )
        attrs_text = json.dumps(answer.attrs, ensure_ascii=False, default=str).lower()
        if any(p in attrs_text for p in patterns):
            hits.append("attrs")
        return hits

    def poll(self, ctx: PollContext) -> PollResult:
        cfg = self.config
        collector = _Collector()
        symbols = cfg.symbols
        cursor = ctx.cursor % len(symbols)
        if self._worker.busy:
            return collector.result(OutageKind.SOURCE_UNREACHABLE, detail_json(reason="vendor_worker_busy"), next_cursor=cursor)
        symbol_errors: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        notes: dict[str, Any] = {}
        start = self._clock.monotonic()
        done = 0
        while done < min(cfg.symbols_per_poll, len(symbols)):
            position = (cursor + done) % len(symbols)
            symbol = symbols[position]
            if done and self._budget_s is not None and (
                self._clock.monotonic() - start + cfg.vendor_call_timeout_s > self._budget_s
            ):
                notes = {"reason": "poll_budget", "symbols_asked": done}
                break
            if not self._limiter.try_acquire():
                break
            try:
                answer: _VendorAnswer = self._worker.run_with_timeout(
                    lambda s=symbol: self._call_with_warnings(s), cfg.vendor_call_timeout_s
                )
            except VendorWorkerBusy:
                return collector.result(OutageKind.SOURCE_UNREACHABLE, detail_json(reason="vendor_worker_busy"), next_cursor=position)
            except TimeoutError:
                detail = detail_json(reason="vendor_call_timeout", symbol=symbol, timeout_s=cfg.vendor_call_timeout_s)
                return collector.result(OutageKind.SOURCE_UNREACHABLE, detail, next_cursor=position)
            except ImportError as err:
                return collector.result(OutageKind.VENDOR_UNAVAILABLE, error_detail(err, symbol=symbol), next_cursor=position)
            except Exception as err:  # any vendor error becomes a classified outage or a per-symbol note, never silence
                code = getattr(err, "code", None)
                if isinstance(code, str) and code in cfg.symbol_error_codes:
                    symbol_errors.append({"symbol": symbol, "code": code, "class": type(err).__name__})
                    done += 1
                    continue
                kind = cfg.error_map.get(code, OutageKind.SOURCE_UNREACHABLE) if isinstance(code, str) else OutageKind.SOURCE_UNREACHABLE
                return collector.result(kind, error_detail(err, symbol=symbol), next_cursor=position)
            first_seen_at = self._clock.now()
            invalid = self._collect_rows(collector, symbol, answer, first_seen_at, skipped)
            if invalid is not None:
                return collector.result(OutageKind.SOURCE_INVALID_PAYLOAD, detail_json(reason=invalid, symbol=symbol), next_cursor=position)
            hits = self._quota_hit(answer)
            if hits:
                detail = detail_json(reason="vendor_quota", symbol=symbol, signals=hits, rows=len(answer.rows))
                return collector.result(OutageKind.VENDOR_QUOTA_EXCEEDED, detail, next_cursor=position)
            done += 1
        if symbol_errors:
            notes["symbol_errors"] = symbol_errors
        return _noted(collector, None, skipped, next_cursor=(cursor + done) % len(symbols), **notes)

    def _collect_rows(
        self, collector: _Collector, symbol: str, answer: _VendorAnswer, first_seen_at: datetime, skipped: list[dict[str, str]]
    ) -> str | None:
        """Add the rows as items; returns a reason when the frame is not a list of rows with an id. A row that is not
        strict JSON is skipped and noted in `skipped`, so one bad row cannot hold the cursor (code review F4)."""
        if not isinstance(answer.rows, list):
            return "rows_not_a_list"
        request = {
            "call": VNSTOCK_EVENTS_CALL,
            "args": {"symbol": symbol, "source": self.config.vnstock_source},
            "library_version": self.library_version,
        }
        response = {"rows": len(answer.rows), "attrs": answer.attrs}
        for row in answer.rows:
            row_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(row_id, bool) or not isinstance(row_id, (str, int)) or not str(row_id).strip():
                return "row_without_id"
            source_id = f"{self.config.vnstock_source}:{row_id}"
            if not collector.try_item(
                source_id, row, first_seen_at=first_seen_at, request=request, response=response, snapshot_sha256=None,
            ):
                skipped.append({"id": source_id, "reason": "not_strict_json"})
        return None
