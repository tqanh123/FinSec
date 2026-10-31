"""Pure checks of `scripts/spike/vn_reconcile.py` (plan 261022-1147, phase 6, R6.3).

Synthetic data only: made-up tickers (AAA, BBB, CCC), made-up ids, titles and names. The fetcher, the sleeper, the
monotonic clock and the wall clock are fakes: no network, no sleep, no real clock, and vnstock is never imported. The
script is loaded by path because `scripts/` is not a package; it imports its sibling `_spikelib`.
"""
# Naive datetimes here are ICT wall-clock inputs (HOSE pseudo-UTC encoding, sample rows), never instants.
# ruff: noqa: DTZ001
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import random
import sys
import tarfile
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from finsec_alpharadar.capture.record import (
    CaptureRecord,
    PayloadFormat,
    PitQuality,
    RecordType,
    canonical_item_bytes,
    make_cycle_id,
)
from finsec_alpharadar.quant.power_rule import PowerTier, assess

REPO_ROOT = Path(__file__).resolve().parents[1]
SPIKE_DIR = REPO_ROOT / "scripts" / "spike"


def _load() -> ModuleType:
    if str(SPIKE_DIR) not in sys.path:
        sys.path.insert(0, str(SPIKE_DIR))  # the script imports its sibling `_spikelib`
    spec = importlib.util.spec_from_file_location("vn_reconcile", SPIKE_DIR / "vn_reconcile.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


vr = _load()
FetchResult = vr.FetchResult  # the script's own _spikelib type

SECRET_TITLE = "ZZQX sentinel title do not leak"
SECRET_NAME = "Tran Thi Synthetic"
HOLIDAY = date(2026, 11, 24)
HOLIDAYS = frozenset({HOLIDAY})
ICT = timedelta(hours=7)


def _pseudo_utc(local: datetime) -> int:
    """HOSE `postedDate`: the ICT wall time encoded as if it were UTC (research vn-disclosure-portals.md §1)."""
    return int(local.replace(tzinfo=UTC).timestamp())


def _ict(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """An instant given by its ICT wall time, as UTC."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC) - ICT


# --- title classification ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AAA: Báo cáo tài chính quý 3/2026", "BCTC"),
        ("AAA: BCTC hợp nhất bán niên 2026", "BCTC"),
        ("BÁO CÁO TÀI CHÍNH NĂM 2025", "BCTC"),
        ("AAA: Nghị quyết HĐQT số 12/2026", "NQ_HDQT"),
        ("BBB: Nghị quyết Hội đồng quản trị về việc vay vốn", "NQ_HDQT"),
        ("BBB: NGHỊ QUYẾT HĐQT", "NQ_HDQT"),
        ("CCC: Đăng ký giao dịch cổ phiếu", "INSIDER"),
        ("CCC: Thông báo giao dịch của người nội bộ", "INSIDER"),
        # Precedence: INSIDER > NQ_HDQT > BCTC.
        ("AAA: Nghị quyết HĐQT thông qua BCTC quý 3", "NQ_HDQT"),
        ("AAA: Nghị quyết HĐQT về giao dịch của người nội bộ", "INSIDER"),
        ("AAA: Nghị quyết ĐHĐCĐ thường niên", None),
        ("AAA: Giải trình kết quả kinh doanh", None),
        ("", None),
    ],
)
def test_classify_title(title: str, expected: str | None) -> None:
    assert vr.classify_title(title) == expected


def test_classify_title_normalizes_nfd_input() -> None:
    decomposed = unicodedata.normalize("NFD", "AAA: Nghị quyết Hội đồng quản trị")
    assert decomposed != unicodedata.normalize("NFC", decomposed)
    assert vr.classify_title(decomposed) == "NQ_HDQT"
    assert vr.classify_title(unicodedata.normalize("NFD", "Báo cáo tài chính")) == "BCTC"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AAA: Nghị quyết", "AAA"),
        ("  bbb : Báo cáo", "BBB"),
        ("A1B: x", "A1B"),
        ("FUEAAA01: Thông báo", None),  # a fund code is not a 3-character stock symbol
        ("AB: x", None),
        ("Báo cáo: x", None),
        ("AAA Nghị quyết", None),
        ("", None),
    ],
)
def test_symbol_from_title(title: str, expected: str | None) -> None:
    assert vr.symbol_from_title(title) == expected


# --- times --------------------------------------------------------------------------------------------------------


def test_hose_posted_date_is_pseudo_utc() -> None:
    posted = _pseudo_utc(datetime(2026, 10, 21, 10, 33, 16))
    assert vr.hose_posted_utc(posted) == datetime(2026, 10, 21, 3, 33, 16, tzinfo=UTC)
    assert vr.HOSE_PSEUDO_UTC_OFFSET_S == 25200


@pytest.mark.parametrize("bad", [True, "1792665196", 1792665196.0, None])
def test_hose_posted_date_rejects_non_integers(bad: Any) -> None:
    with pytest.raises(ValueError):
        vr.hose_posted_utc(bad)


@pytest.mark.parametrize("bad", [1761579000000, 10**20, -(10**12), -1, 0])
def test_hose_posted_date_out_of_range_raises_value_error(bad: int) -> None:
    # logic F3: fromtimestamp raises OSError (Windows) or OverflowError; both must surface as ValueError.
    with pytest.raises(ValueError):
        vr.hose_posted_utc(bad)


def test_hose_page_drops_an_entry_with_an_out_of_range_posted_date() -> None:
    good = _hose_entry(301, "AAA: Báo cáo tài chính quý 3", datetime(2026, 10, 21, 9))
    body = _hose_body([
        {**_hose_entry(302, "BBB: Báo cáo tài chính", datetime(2026, 10, 21, 9)), "postedDate": 1761579000000},
        {**_hose_entry(303, "BBB: Báo cáo tài chính", datetime(2026, 10, 21, 9)), "postedDate": 10**20},
        good,
    ])
    items, _ = vr.hose_page(body, "hose_listing")
    assert [item.source_id for item in items] == ["301"]


def test_hnx_pubdate_is_read_with_its_offset() -> None:
    assert vr.parse_hnx_pubdate("Wed, 21 Oct 2026 19:07:49 +0700") == datetime(2026, 10, 21, 12, 7, 49, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["Wed, 21 Oct 2026 19:07:49 -0000", "garbage", ""])
def test_hnx_pubdate_without_a_zone_or_unparseable_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        vr.parse_hnx_pubdate(bad)


@pytest.mark.parametrize(
    ("local", "bucket"),
    [
        ((2026, 10, 21, 8, 0, 0), "08:00-14:45"),
        ((2026, 10, 21, 14, 44, 59), "08:00-14:45"),
        ((2026, 10, 21, 14, 45, 0), "14:45-17:30"),
        ((2026, 10, 21, 17, 29, 59), "14:45-17:30"),
        ((2026, 10, 21, 17, 30, 0), "17:30-24:00"),
        ((2026, 10, 21, 23, 59, 59), "17:30-24:00"),
        ((2026, 10, 21, 0, 0, 0), "00:00-08:00"),
        ((2026, 10, 21, 7, 59, 59), "00:00-08:00"),
        ((2026, 10, 24, 10, 0, 0), "non_trading_day"),  # Saturday
        ((2026, 10, 25, 18, 0, 0), "non_trading_day"),  # Sunday evening
        ((2026, 11, 24, 10, 0, 0), "non_trading_day"),  # configured holiday (a Tuesday)
    ],
)
def test_time_bucket_boundaries(local: tuple[int, ...], bucket: str) -> None:
    assert vr.time_bucket(_ict(*local), HOLIDAYS) == bucket


def test_time_bucket_without_the_holiday_is_a_trading_day() -> None:
    assert vr.time_bucket(_ict(2026, 11, 24, 10), frozenset()) == "08:00-14:45"
    assert vr.is_trading_day(date(2026, 11, 24), frozenset()) is True
    assert vr.is_trading_day(date(2026, 11, 24), HOLIDAYS) is False
    assert vr.is_trading_day(date(2026, 10, 24), frozenset()) is False


def test_time_bucket_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError):
        vr.time_bucket(datetime(2026, 10, 21, 9, 0), HOLIDAYS)


def test_buckets_are_the_five_registered_ones() -> None:
    assert vr.BUCKETS == ("08:00-14:45", "14:45-17:30", "17:30-24:00", "00:00-08:00", "non_trading_day")


# --- portal parsing ---------------------------------------------------------------------------------------------


def _hose_body(entries: list[dict[str, Any]], total: int | None = None) -> bytes:
    payload = {
        "data": {"list": entries, "object": None,
                 "paging": {"pageIndex": 1, "pageSize": 20, "totalCount": total or len(entries), "totalPages": 1}},
        "success": True,
        "message": None,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _hose_entry(entry_id: int, title: str, local: datetime) -> dict[str, Any]:
    return {
        "id": entry_id, "catId": 21, "code": None, "title": title, "postedDate": _pseudo_utc(local),
        "publishFrom": _pseudo_utc(local.replace(hour=0, minute=0, second=0)), "publishTo": None,
        "relatedName": SECRET_NAME, "summary": f"<p>{SECRET_TITLE}</p>",
    }


def _rss_body(items: list[tuple[str, str, str]]) -> bytes:
    parts = ['<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>feed</title>']
    for guid, title, pub in items:
        parts.append(
            f'<item><guid isPermaLink="false">{guid}</guid><link>http://portal.example.invalid:7978/{guid}</link>'
            f"<title>{title}</title><description>&lt;p&gt;{SECRET_NAME}&lt;/p&gt;</description>"
            f"<pubDate>{pub}</pubDate></item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


def test_hose_items_parse_type_symbol_and_derived_time() -> None:
    body = _hose_body([
        _hose_entry(501, "AAA: Nghị quyết HĐQT số 1", datetime(2026, 10, 21, 10, 33, 16)),
        _hose_entry(502, "Thông báo quỹ mở", datetime(2026, 10, 21, 9, 0, 0)),
    ])
    items = vr.hose_items(body, "hose_api_history")
    assert [item.source_id for item in items] == ["501", "502"]
    first = items[0]
    assert (first.exchange, first.symbol, first.event_type, first.source) == ("HOSE", "AAA", "NQ_HDQT", "hose_api_history")
    assert first.posted_at_utc == datetime(2026, 10, 21, 3, 33, 16, tzinfo=UTC)
    assert first.posted_at_label == "DERIVED"
    assert first.posted_at_raw == _pseudo_utc(datetime(2026, 10, 21, 10, 33, 16))
    assert items[1].event_type is None and items[1].symbol is None


@pytest.mark.parametrize("body", [b"not json", b'{"success": false}', b'{"success": true, "data": {"list": "x"}}'])
def test_hose_items_refuse_an_unexpected_body(body: bytes) -> None:
    with pytest.raises(ValueError):
        vr.hose_items(body, "hose_api_history")


def test_hnx_rss_items_have_no_symbol_and_observed_time() -> None:
    body = _rss_body([
        ("700002", "Báo cáo tài chính quý 3/2026 &amp; thuyết minh", "Wed, 21 Oct 2026 19:07:49 +0700"),
        ("700001", "<![CDATA[Nghị quyết Hội đồng quản trị]]>", "Wed, 21 Oct 2026 08:00:00 +0700"),
    ])
    items = vr.hnx_rss_items(body, "hnx_rss3")
    assert [(item.source_id, item.event_type, item.symbol) for item in items] == [
        ("700002", "BCTC", None), ("700001", "NQ_HDQT", None),
    ]
    assert items[0].posted_at_utc == datetime(2026, 10, 21, 12, 7, 49, tzinfo=UTC)
    assert items[0].posted_at_label == "OBSERVED"
    assert items[0].title == "Báo cáo tài chính quý 3/2026 & thuyết minh"


def test_hnx_listing_items_take_the_symbol_column() -> None:
    row = (
        f"<tr><td>21/10/2026 11:12</td><td>BBB</td><td>{SECRET_NAME}</td>"
        "<td><a href=\"javascript:funcViewDetailArticlesByID(638921,1)\">Đăng ký giao dịch cổ phiếu</a></td><td></td></tr>"
    )
    body = f"<html><table><tbody>{row}</tbody></table></html>".encode()
    items = vr.hnx_listing_items(body, "hnx_post_listing")
    assert len(items) == 1
    item = items[0]
    assert (item.source_id, item.symbol, item.event_type, item.exchange) == ("638921", "BBB", "INSIDER", "HNX")
    assert item.posted_at_utc == datetime(2026, 10, 21, 4, 12, tzinfo=UTC)
    assert vr.hnx_listing_items(b"<html>no rows</html>", "hnx_post_listing") == []


def test_hnx_listing_symbol_cell_needs_a_letter() -> None:
    # logic F4: a 3-digit row number (STT) before the symbol column is not a ticker.
    row = ("<tr><td>101</td><td><a onclick=\"funcViewDetailArticlesByID(555,1)\">Báo cáo tài chính</a></td>"
           "<td>27/10/2026 15:30</td><td>BBB</td></tr>")
    items = vr.hnx_listing_items(f"<table>{row}</table>".encode(), "hnx_post_listing")
    assert [(item.source_id, item.symbol) for item in items] == [("555", "BBB")]
    only_digits = row.replace("<td>BBB</td>", "<td>202</td>")
    items = vr.hnx_listing_items(f"<table>{only_digits}</table>".encode(), "hnx_post_listing")
    assert [(item.source_id, item.symbol) for item in items] == [("555", None)]


# --- deterministic selection ------------------------------------------------------------------------------------


def test_id_sort_key_orders_digit_ids_numerically() -> None:
    assert sorted(["10", "9", "100", "x2"], key=vr.id_sort_key) == ["9", "10", "100", "x2"]


def test_evenly_spaced() -> None:
    assert vr.evenly_spaced(list(range(10)), 5) == [1, 3, 5, 7, 9]
    assert vr.evenly_spaced(list(range(3)), 5) == [0, 1, 2]
    assert vr.evenly_spaced([], 5) == []
    assert vr.evenly_spaced(list(range(4)), 0) == []


def _item(exchange: str, source_id: int, event_type: str | None, *, symbol: str | None = "AAA",
          local: datetime = datetime(2026, 10, 21, 10, 0), source: str = "synthetic") -> Any:
    return vr.PortalItem(
        exchange=exchange, source_id=str(source_id), symbol=symbol, event_type=event_type,
        posted_at_raw=str(source_id), posted_at_utc=local.replace(tzinfo=UTC) - ICT,
        posted_at_label="OBSERVED", source=source, title=f"{SECRET_TITLE} {source_id}",
    )


def _pool(sizes: Mapping[tuple[str, str], int]) -> list[Any]:
    items: list[Any] = []
    base = 1000
    for (event_type, exchange), size in sizes.items():
        items += [_item(exchange, base + n, event_type) for n in range(size)]
        base += 1000
    items += [_item("HOSE", 99, None), _item("HNX", 98, None)]  # unclassified: never selected
    return items


FULL = {(t, x): 12 for t in ("BCTC", "NQ_HDQT", "INSIDER") for x in ("HOSE", "HNX")}


def test_selection_is_ten_per_type_five_per_exchange_and_deterministic() -> None:
    items = _pool(FULL)
    selected, strata = vr.select_sample(items)
    assert len(selected) == 30
    for event_type in ("BCTC", "NQ_HDQT", "INSIDER"):
        for exchange in ("HOSE", "HNX"):
            assert sum(1 for i in selected if (i.event_type, i.exchange) == (event_type, exchange)) == 5
    assert all(i.event_type is not None for i in selected)
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)
    again, strata_again = vr.select_sample(shuffled)
    assert [(i.exchange, i.source_id) for i in again] == [(i.exchange, i.source_id) for i in selected]
    assert strata == strata_again
    entry = next(s for s in strata if (s["event_type"], s["exchange"]) == ("BCTC", "HOSE"))
    assert (entry["wanted"], entry["pool"], entry["got"], entry["short_by"], entry["topped_up"]) == (5, 12, 5, 0, 0)
    # evenly spaced over ids 1000..1011: positions 1, 3, 6, 8, 10
    assert [i.source_id for i in selected if (i.event_type, i.exchange) == ("BCTC", "HOSE")] == [
        "1001", "1003", "1006", "1008", "1010",
    ]


def test_short_stratum_is_reported_and_topped_up_from_the_other_exchange() -> None:
    sizes = dict(FULL)
    sizes[("INSIDER", "HNX")] = 2
    selected, strata = vr.select_sample(_pool(sizes))
    assert len(selected) == 30
    by_key = {(s["event_type"], s["exchange"]): s for s in strata}
    assert by_key[("INSIDER", "HNX")]["got"] == 2 and by_key[("INSIDER", "HNX")]["short_by"] == 3
    assert by_key[("INSIDER", "HOSE")]["got"] == 8 and by_key[("INSIDER", "HOSE")]["topped_up"] == 3


def test_both_exchanges_short_gives_fewer_than_thirty() -> None:
    sizes = dict(FULL)
    sizes[("BCTC", "HOSE")] = 3
    sizes[("BCTC", "HNX")] = 4
    selected, strata = vr.select_sample(_pool(sizes))
    assert len(selected) == 27
    by_key = {(s["event_type"], s["exchange"]): s for s in strata}
    assert by_key[("BCTC", "HOSE")]["got"] == 3 and by_key[("BCTC", "HNX")]["got"] == 4


def test_items_without_symbol_stay_in_the_sample() -> None:
    items = [_item("HNX", 10 + n, "BCTC", symbol=None) for n in range(5)]
    selected, _ = vr.select_sample(items)
    assert len(selected) == 5 and all(i.symbol is None for i in selected)


# --- sample document and data hygiene -----------------------------------------------------------------------------


def test_sample_document_rows_and_summary_hold_no_title_outside_rows() -> None:
    items = _pool(FULL)
    selected, strata = vr.select_sample(items)
    doc = vr.sample_document(selected, strata, holidays=HOLIDAYS, meta={"from": "2026-10-01", "to": "2026-10-21"},
                             forward=vr.forward_counts([]), window_open=False)
    assert doc["kind"] == "vn_reconcile_sample" and len(doc["items"]) == 30
    row = doc["items"][0]
    assert set(row) >= {"exchange", "source_id", "symbol", "symbol_source", "event_type", "posted_at_raw", "posted_at_utc",
                        "posted_at_label", "posted_date_ict", "bucket", "source", "first_seen_at", "pit_quality", "title"}
    assert row["bucket"] == "08:00-14:45" and row["first_seen_at"] is None
    summary = vr.sample_summary(doc)
    text = json.dumps(summary, ensure_ascii=False)
    assert SECRET_TITLE not in text and "title" not in text
    assert summary["items"] == 30


def test_forward_counts_use_first_seen_items_only() -> None:
    a = vr.PortalItem("HOSE", "1", "AAA", "BCTC", 1, _ict(2026, 10, 21, 9), "DERIVED", "capture_backup", "t",
                      first_seen_at=_ict(2026, 10, 21, 9, 1), pit_quality="first_seen")
    b = vr.PortalItem("HNX", "2", None, "NQ_HDQT", "x", _ict(2026, 10, 21, 18), "OBSERVED", "capture_backup", "t",
                      first_seen_at=_ict(2026, 10, 21, 18, 2), pit_quality="first_seen")
    c = vr.PortalItem("HNX", "3", None, "BCTC", "x", _ict(2026, 10, 22, 9), "OBSERVED", "capture_backup", "t",
                      first_seen_at=_ict(2026, 10, 22, 9, 5), pit_quality="late")
    d = vr.PortalItem("HNX", "4", None, None, "x", _ict(2026, 10, 22, 9), "OBSERVED", "capture_backup", "t",
                      first_seen_at=_ict(2026, 10, 22, 9, 5), pit_quality="first_seen")
    counts = vr.forward_counts([a, b, c, d])
    assert counts["n_events"] == 2 and counts["n_distinct_dates"] == 1
    assert counts["by_type"]["BCTC"] == {"n_events": 1, "n_distinct_dates": 1}
    assert vr.forward_counts([])["n_events"] == 0


# --- backup path ----------------------------------------------------------------------------------------------------


def _record(source: str, source_id: str, payload: Any, at: datetime, pit: PitQuality) -> CaptureRecord:
    canonical = canonical_item_bytes(payload)
    return CaptureRecord(
        record_type=RecordType.ITEM, source=source, source_id=source_id, information_time=at, first_seen_at=at,
        ingested_at=at, pit_quality=pit, raw_sha256=hashlib.sha256(canonical).hexdigest(),
        payload_format=PayloadFormat.TEXT if isinstance(payload, str) else PayloadFormat.JSON, payload=payload,
        blob_path=None, byte_count=len(canonical), snapshot_sha256=None,
        request={"method": "GET", "url": "https://portal.example.invalid/x", "params": {}, "header_names": []},
        response={"status": 200, "http_date": None, "rtt_ms": 5, "content_type": "application/json"},
        cycle_id=make_cycle_id(at, 1), capture_version="0.1.0+0123456789ab",
    )


def _write_backup(mirror: Path, day: date, members: dict[str, bytes]) -> None:
    folder = mirror / "daily" / day.isoformat()
    folder.mkdir(parents=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    archive = folder / f"capture-{day.isoformat()}.tar.gz"
    archive.write_bytes(data)
    (folder / f"manifest-{day.isoformat()}.json").write_text(json.dumps({
        "schema": "capture-backup/1", "day": day.isoformat(),
        "files": [{"name": archive.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
        "inputs": [{"path": name, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "lines": 1}
                   for name, blob in members.items()],
    }), encoding="utf-8")


def test_backup_items_carry_first_seen_and_pit_quality(tmp_path: Path) -> None:
    day = date(2026, 10, 21)
    seen = _ict(2026, 10, 21, 10, 34)
    hose = _record("hose_news_api", "501", _hose_entry(501, "AAA: Nghị quyết HĐQT", datetime(2026, 10, 21, 10, 33, 16)),
                   seen, PitQuality.FIRST_SEEN)
    fragment = ('<item><guid isPermaLink="false">700001</guid><title>Báo cáo tài chính quý 3</title>'
                "<pubDate>Wed, 21 Oct 2026 10:30:00 +0700</pubDate></item>")
    hnx = _record("hnx_rss_issuers", "700001", fragment, _ict(2026, 10, 21, 10, 31), PitQuality.LATE)
    lines = {
        f"raw/hose_news_api/{day.isoformat()}.jsonl": hose.to_json_line().encode("utf-8"),
        f"raw/hnx_rss_issuers/{day.isoformat()}.jsonl": hnx.to_json_line().encode("utf-8"),
    }
    _write_backup(tmp_path, day, lines)
    read = vr.items_from_backup(tmp_path, day, date(2026, 10, 22))
    assert read.days_read == ["2026-10-21"] and read.days_missing == ["2026-10-22"]
    by_id = {item.source_id: item for item in read.items}
    assert by_id["501"].first_seen_at == seen and by_id["501"].pit_quality == "first_seen"
    assert by_id["501"].source == "capture_backup" and by_id["501"].event_type == "NQ_HDQT"
    assert by_id["700001"].exchange == "HNX" and by_id["700001"].pit_quality == "late"
    assert by_id["700001"].posted_at_utc == _ict(2026, 10, 21, 10, 30)


def test_backup_with_a_bad_sha_is_refused(tmp_path: Path) -> None:
    day = date(2026, 10, 21)
    _write_backup(tmp_path, day, {f"raw/hose_news_api/{day.isoformat()}.jsonl": b""})
    archive = tmp_path / "daily" / day.isoformat() / f"capture-{day.isoformat()}.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"!")
    with pytest.raises(vr.BackupError):
        vr.items_from_backup(tmp_path, day, day)


# --- symbol overrides ---------------------------------------------------------------------------------------------


def test_symbol_overrides_are_validated() -> None:
    assert vr.parse_symbol_overrides({"HNX:700001": "BBB", "HOSE:501": "AAAA"}) == {
        ("HNX", "700001"): "BBB", ("HOSE", "501"): "AAAA",
    }
    for bad in ({"UPCOM:1": "AAA"}, {"700001": "AAA"}, {"HNX:": "AAA"}, {"HNX:1": "aaa"}, {"HNX:1": "AB"},
                {"HNX:1": "ABCDE"}, {"HNX:1": 123}, ["HNX:1"]):
        with pytest.raises(ValueError):
            vr.parse_symbol_overrides(bad)


def _sample_row(exchange: str, source_id: str, symbol: str | None, event_type: str, local: datetime,
                *, first_seen: datetime | None = None) -> dict[str, Any]:
    posted = local.replace(tzinfo=UTC) - ICT
    return {
        "exchange": exchange, "source_id": source_id, "symbol": symbol,
        "symbol_source": "title" if symbol else None, "event_type": event_type,
        "posted_at_raw": source_id, "posted_at_utc": posted.isoformat(), "posted_at_label": "OBSERVED",
        "posted_date_ict": local.date().isoformat(), "bucket": vr.time_bucket(posted, HOLIDAYS), "source": "synthetic",
        "first_seen_at": first_seen.isoformat() if first_seen else None,
        "pit_quality": "first_seen" if first_seen else None, "title": f"{SECRET_TITLE} {source_id}",
    }


def test_apply_overrides_counts_and_refuses_unknown_items() -> None:
    rows = [
        _sample_row("HNX", "700001", None, "BCTC", datetime(2026, 10, 21, 9)),
        _sample_row("HOSE", "501", "AAA", "NQ_HDQT", datetime(2026, 10, 21, 9)),
    ]
    updated, applied = vr.apply_symbol_overrides(rows, {("HNX", "700001"): "BBB"})
    assert applied == 1 and updated[0]["symbol"] == "BBB" and updated[0]["symbol_source"] == "override"
    assert rows[0]["symbol"] is None  # the input is not mutated
    with pytest.raises(ValueError, match="HNX:999"):
        vr.apply_symbol_overrides(rows, {("HNX", "999"): "BBB"})


# --- vnstock rows and matching ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-16T00:00:00+07:00", (date(2026, 9, 16), False)),
        ("2026-09-16T16:45:00+07:00", (date(2026, 9, 16), True)),
        ("2026-09-15T17:00:00.000Z", (date(2026, 9, 16), False)),
        ("2026-09-16", (date(2026, 9, 16), False)),
        ("2026-09-16 10:15:00", (date(2026, 9, 16), True)),
        ("garbage", None),
        (None, None),
        (1726506000000, None),
    ],
)
def test_parse_public_date(value: Any, expected: tuple[date, bool] | None) -> None:
    assert vr.parse_public_date(value) == expected


def _vrow(row_id: str, symbol: str, code: str, public_date: str | None, *, category: str = "OTHER",
          title: str = SECRET_TITLE) -> dict[str, Any]:
    return {"id": row_id, "symbol": symbol, "event_code": code, "category": category, "public_date": public_date,
            "event_title_vi": title, "event_name_vi": SECRET_NAME}


def test_vnstock_row_type() -> None:
    assert vr.vnstock_row_type(_vrow("1", "AAA", "DDINS", None)) == "INSIDER"
    assert vr.vnstock_row_type(_vrow("1", "AAA", "NEW", None, category="MAJOR_SHAREHOLDER_TRADING")) == "INSIDER"
    assert vr.vnstock_row_type(_vrow("1", "AAA", "OTHE", None, title="Nghị quyết HĐQT về cổ tức")) == "NQ_HDQT"
    assert vr.vnstock_row_type(_vrow("1", "AAA", "OTHE", None, title="Báo cáo tài chính quý 3")) == "BCTC"
    assert vr.vnstock_row_type(_vrow("1", "AAA", "DIV", None, category="DIVIDEND", title="Trả cổ tức")) is None


@pytest.mark.parametrize(("offset_days", "matched"), [(-4, False), (-3, True), (0, True), (3, True), (4, False)])
def test_match_window_is_three_days_each_way(offset_days: int, matched: bool) -> None:
    item = _sample_row("HOSE", "501", "AAA", "INSIDER", datetime(2026, 10, 14, 9))
    row_day = date(2026, 10, 14) + timedelta(days=offset_days)
    rows = [_vrow("v1", "AAA", "DDINS", f"{row_day.isoformat()}T00:00:00+07:00")]
    match = vr.match_item(item, rows)
    if matched:
        assert match is not None and match.row_id == "v1" and match.lag_days == offset_days and match.has_time is False
    else:
        assert match is None


def test_match_requires_same_symbol_and_type_and_prefers_the_nearest_then_lowest_id() -> None:
    item = _sample_row("HOSE", "501", "AAA", "INSIDER", datetime(2026, 10, 14, 9))
    rows = [
        _vrow("v9", "BBB", "DDINS", "2026-10-14T00:00:00+07:00"),  # other symbol
        _vrow("v8", "AAA", "DIV", "2026-10-14T00:00:00+07:00", category="DIVIDEND", title="x"),  # other type
        _vrow("v3", "AAA", "DDINS", "2026-10-16T00:00:00+07:00"),
        _vrow("v2", "AAA", "DDINS", "2026-10-13T00:00:00+07:00"),
        _vrow("v1", "AAA", "DDINS", "2026-10-15T09:30:00+07:00"),
    ]
    match = vr.match_item(item, rows)
    assert match is not None and match.row_id == "v1" and match.lag_days == 1 and match.has_time is True
    assert vr.match_item({**item, "symbol": None}, rows) is None


def test_matching_uses_each_vnstock_row_at_most_once() -> None:
    # logic F2: two NQ items of AAA on adjacent days and one NQ row; two INSIDER items and one DD row.
    rows = [
        _sample_row("HOSE", "77", "AAA", "NQ_HDQT", datetime(2026, 9, 17, 9)),
        _sample_row("HOSE", "78", "AAA", "NQ_HDQT", datetime(2026, 9, 16, 9)),
        _sample_row("HOSE", "79", "AAA", "INSIDER", datetime(2026, 9, 16, 10)),
        _sample_row("HOSE", "80", "AAA", "INSIDER", datetime(2026, 9, 16, 11)),
    ]
    vnstock = {"AAA": [_vrow("1", "AAA", "OTHE", "2026-09-16T00:00:00+07:00", title="Nghị quyết HĐQT"),
                       _vrow("3", "AAA", "DDINS", "2026-09-16T00:00:00+07:00")]}
    matches = vr.match_items(rows, vnstock)
    assert [(m.row_id, m.lag_days) if m else None for m in matches] == [None, ("1", 0), ("3", 0), None]
    summary = vr.reconcile_summary(rows, vnstock, overrides_applied=0, window_open=False)
    match = summary["date_match"]
    assert match["matched"] == 2 and match["rate"] == pytest.approx(0.5)
    assert match["distinct_rows_matched"] == 2
    assert match["unmatched_reasons"] == {"row_assigned_to_another_item": 2}
    assert [(p["source_id"], p["matched"]) for p in summary["per_item"]] == [
        ("77", False), ("78", True), ("79", True), ("80", False)]


def test_matching_gives_a_shared_row_to_the_nearer_item_and_lets_the_other_take_its_next_row() -> None:
    rows = [
        _sample_row("HOSE", "10", "AAA", "INSIDER", datetime(2026, 9, 15, 9)),  # lag +1 to v1, lag +3 to v2
        _sample_row("HOSE", "11", "AAA", "INSIDER", datetime(2026, 9, 16, 9)),  # lag 0 to v1
    ]
    vnstock = {"AAA": [_vrow("v1", "AAA", "DDINS", "2026-09-16T00:00:00+07:00"),
                       _vrow("v2", "AAA", "DDINS", "2026-09-18T00:00:00+07:00")]}
    matches = vr.match_items(rows, vnstock)
    assert [(m.row_id, m.lag_days) for m in matches if m] == [("v2", 3), ("v1", 0)]


def test_load_vnstock_rows_reads_one_list_per_symbol(tmp_path: Path) -> None:
    (tmp_path / "AAA.json").write_text(json.dumps([_vrow("v1", "AAA", "DDINS", "2026-10-14")]), encoding="utf-8")
    (tmp_path / "BBB.json").write_text(json.dumps([{"id": "v2", "event_code": "DIV", "public_date": None}]),
                                       encoding="utf-8")
    rows = vr.load_vnstock_rows(tmp_path)
    assert set(rows) == {"AAA", "BBB"}
    assert rows["BBB"][0]["symbol"] == "BBB"  # the file name gives a missing symbol
    (tmp_path / "CCC.json").write_text('{"rows": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="CCC.json"):
        vr.load_vnstock_rows(tmp_path)


# --- reconcile summary --------------------------------------------------------------------------------------------


def _reconcile_inputs() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = [
        _sample_row("HOSE", "501", "AAA", "INSIDER", datetime(2026, 10, 14, 9, 0), first_seen=None),
        _sample_row("HOSE", "502", "AAA", "NQ_HDQT", datetime(2026, 10, 14, 15, 0)),
        _sample_row("HOSE", "503", "BBB", "BCTC", datetime(2026, 10, 14, 18, 0)),
        _sample_row("HNX", "700001", None, "BCTC", datetime(2026, 10, 17, 10, 0)),  # Saturday, no symbol
        _sample_row("HNX", "700002", "CCC", "INSIDER", datetime(2026, 10, 15, 6, 0)),
    ]
    vnstock = {
        "AAA": [_vrow("v1", "AAA", "DDINS", "2026-10-15T00:00:00+07:00"),
                _vrow("v2", "AAA", "OTHE", "2026-10-14T00:00:00+07:00", title="Nghị quyết HĐQT")],
        "BBB": [_vrow("v3", "BBB", "DIV", "2026-10-14T00:00:00+07:00", category="DIVIDEND", title="x")],
        "CCC": [_vrow("v4", "CCC", "DDINS", "2026-10-15T11:00:00+07:00")],
    }
    return rows, vnstock


def test_reconcile_summary_counts_rates_and_buckets() -> None:
    rows, vnstock = _reconcile_inputs()
    summary = vr.reconcile_summary(rows, vnstock, overrides_applied=1, window_open=False)
    assert summary["kind"] == "vn_reconcile_summary"
    assert summary["sample_items"] == 5
    assert summary["symbols"]["excluded_no_symbol"] == {"HOSE": 0, "HNX": 1}
    assert summary["symbols"]["from_override"] == 1
    assert summary["compared"] == 4
    assert summary["date_match"]["matched"] == 3 and summary["date_match"]["rate"] == pytest.approx(0.75)
    assert summary["date_match"]["by_type"]["BCTC"] == {"compared": 1, "matched": 0}
    assert summary["lag_days"] == {"0": 2, "1": 1}
    assert summary["vnstock_share_with_intraday_time"]["matched_rows"] == pytest.approx(1 / 3)
    assert summary["vnstock_share_with_intraday_time"]["all_rows_of_sample_symbols"] == pytest.approx(1 / 4)
    buckets = summary["posting_buckets"]["all"]
    assert buckets == {"08:00-14:45": 1, "14:45-17:30": 1, "17:30-24:00": 1, "00:00-08:00": 1, "non_trading_day": 1}
    assert summary["share_after_1445"] == pytest.approx(2 / 5)
    assert summary["share_after_1730"] == pytest.approx(1 / 5)
    assert summary["share_non_trading_day"] == pytest.approx(1 / 5)
    assert summary["detection_latency"] == {"n": 0, "median_s": None, "p90_s": None, "reason": "window_not_open"}
    assert summary["labels"]["posted_at_utc"]["HOSE"].startswith("DERIVED")


def test_detection_latency_median_and_p90() -> None:
    rows = [
        _sample_row("HOSE", str(500 + n), "AAA", "BCTC", datetime(2026, 10, 21, 9),
                    first_seen=_ict(2026, 10, 21, 9) + timedelta(seconds=10 * (n + 1)))
        for n in range(10)
    ]
    summary = vr.reconcile_summary(rows, {}, overrides_applied=0, window_open=True)
    latency = summary["detection_latency"]
    assert latency["n"] == 10 and latency["median_s"] == pytest.approx(55.0) and latency["p90_s"] == pytest.approx(90.0)
    assert latency["reason"] is None


def test_no_title_or_name_reaches_any_summary_or_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows, vnstock = _reconcile_inputs()
    sample = {"kind": "vn_reconcile_sample", "window_open": False, "items": rows, "strata": [], "forward": vr.forward_counts([])}
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    raw = tmp_path / "raw"
    raw.mkdir()
    for symbol, symbol_rows in vnstock.items():
        (raw / f"{symbol}.json").write_text(json.dumps(symbol_rows, ensure_ascii=False), encoding="utf-8")
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"HNX:700001": "DDD"}), encoding="utf-8")
    env = {"FINSEC_DATA_ROOT": str(tmp_path)}
    code = vr.main(["compare", "--sample", str(sample_path), "--vnstock-dir", str(raw),
                    "--symbol-overrides", str(overrides)], env=env)
    assert code == 0
    summary_text = (tmp_path / "reconcile-summary.json").read_text(encoding="utf-8")
    out = capsys.readouterr()
    for text in (summary_text, out.out, out.err):
        assert SECRET_TITLE not in text and SECRET_NAME not in text and "ZZQX" not in text
    assert json.loads(summary_text)["symbols"]["from_override"] == 1


def test_compare_refuses_an_overrides_file_outside_the_data_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sample_path = root / "sample.json"
    sample_path.write_text(json.dumps({"kind": "vn_reconcile_sample", "window_open": False, "items": [], "strata": [],
                                       "forward": vr.forward_counts([])}), encoding="utf-8")
    outside = tmp_path / "overrides.json"
    outside.write_text("{}", encoding="utf-8")
    code = vr.main(["compare", "--sample", str(sample_path), "--vnstock-dir", str(root),
                    "--symbol-overrides", str(outside)], env={"FINSEC_DATA_ROOT": str(root)})
    assert code == 2


# --- go / no-go ---------------------------------------------------------------------------------------------------


def _counts(by_group_year: list[dict[str, Any]], universe: int = 130, *, ok: int | None = None,
            stop: dict[str, Any] | None = None) -> dict[str, Any]:
    """The counts-summary shape vn_counts.py writes (schema 1): every requested symbol fetched unless `ok` says less."""
    fetched = universe if ok is None else ok
    skipped = universe - fetched if stop is not None else 0
    return {"kind": "vn_counts_summary", "schema_version": 1,
            "universe": {"size": universe, "note": "current members, not point-in-time"},
            "symbols": {"requested": universe, "ok": fetched, "empty": 0, "failed": universe - fetched - skipped,
                        "skipped": skipped, "failed_list": []},
            "stop": stop, "oldest_date": "2018-10-25", "by_group_year": by_group_year}


def _legacy_counts(by_group_year: list[dict[str, Any]], universe: int) -> dict[str, Any]:
    """An older counts summary: no `symbols` block and no `stop`."""
    counts = _counts(by_group_year, universe)
    del counts["symbols"], counts["stop"]
    return counts


def _gy(group: str, year: int, n_events: int, n_dates: int, intraday: int = 0, intraday_dates: int = 0) -> dict[str, Any]:
    return {"group": group, "year": year, "n_docs": n_events, "n_events": n_events, "n_issuers": 10,
            "n_distinct_dates": n_dates, "max_events_per_date": 3,
            "share_with_intraday_time": intraday / n_events if n_events else 0.0,
            "n_events_with_intraday_time": intraday, "n_distinct_dates_with_intraday_time": intraday_dates}


HISTORY = {"kind": "portal_history_check", "years": {
    "2023": {"date": "2023-10-11", "status": 200, "total_count": 111},
    "2024": {"date": "2024-10-09", "status": 200, "total_count": 95},
    "2025": {"date": "2025-10-08", "status": 200, "total_count": 124},
    "2026": {"date": "2026-10-14", "status": 200, "total_count": 129},
}}
RECONCILE = {"kind": "vn_reconcile_summary", "window_open": False,
             "forward": {"n_events": 0, "n_distinct_dates": 0, "by_type": {}},
             "vnstock_share_with_intraday_time": {"matched_rows": 0.0, "all_rows_of_sample_symbols": 0.0}}
AS_OF = date(2026, 10, 28)


def _rows(counts: dict[str, Any], reconcile: dict[str, Any] = RECONCILE) -> dict[str, dict[str, Any]]:
    rows = vr.go_no_go_rows(counts, reconcile, HISTORY, rho=0.10, universe=200, as_of=AS_OF, holidays=HOLIDAYS)
    return {row["key"]: row for row in rows}


def test_go_no_go_has_r1_d2_and_d4_rows_with_labels() -> None:
    rows = _rows(_counts([_gy("DIVIDEND", 2025, 300, 100), _gy("NQ_HDQT", 2025, 400, 150)]))
    assert {"R1", "D2_BCTC", "D2_NQ_HDQT", "D4"} <= set(rows)
    for row in rows.values():
        assert row["target"] in ("R1", "D2", "D4")
        assert row["n_label"] in ("OBSERVED", "DERIVED", "MISSING")
        assert row["l_label"] in ("OBSERVED", "DERIVED", "MISSING")


def test_d2_bctc_is_derived_when_vnstock_has_no_bctc_group() -> None:
    rows = _rows(_counts([_gy("DIVIDEND", 2025, 300, 100)]))
    bctc = rows["D2_BCTC"]
    assert bctc["n_label"] == "DERIVED" and bctc["l_label"] == "DERIVED"
    # oldest_date is 2018-10-25, but labels need price bars and those start 2018-10-25: the span starts 2019-01-01.
    years = (AS_OF - date(2019, 1, 1)).days / 365.25
    assert bctc["n_events"] == round(4 * 200 * years)
    assert bctc["n_distinct_dates"] == round(72 * years)
    expected = assess(bctc["n_events"], bctc["n_distinct_dates"], 0.10)
    assert bctc["tier"] == expected.tier.value and bctc["mde"] == pytest.approx(expected.mde)
    assert bctc["mde_80"] == pytest.approx(expected.mde_80) and bctc["tier_floor"] == expected.tier_floor.value


def test_d2_from_counts_is_observed_at_the_counted_universe_and_scaled_is_derived() -> None:
    rows = _rows(_counts([_gy("NQ_HDQT", 2024, 400, 150), _gy("NQ_HDQT", 2025, 600, 200)], universe=200))
    nq = rows["D2_NQ_HDQT"]
    assert (nq["n_events"], nq["n_distinct_dates"], nq["n_label"], nq["l_label"]) == (1000, 350, "OBSERVED", "OBSERVED")
    scaled = _rows(_counts([_gy("NQ_HDQT", 2024, 400, 150), _gy("NQ_HDQT", 2025, 600, 200)], universe=100))["D2_NQ_HDQT"]
    assert scaled["n_events"] == 2000 and scaled["n_label"] == "DERIVED"
    assert scaled["n_distinct_dates"] == 350  # dates do not scale with the universe


def test_d2_nq_hdqt_without_counts_is_missing_not_guessed() -> None:
    nq = _rows(_counts([_gy("DIVIDEND", 2025, 300, 100)]))["D2_NQ_HDQT"]
    assert nq["n_label"] == "MISSING" and nq["tier"] is None and nq["n_events"] is None


def test_r1_counts_portal_history_since_the_ecm_start() -> None:
    r1 = _rows(_counts([]))["R1"]
    assert r1["n_label"] == "DERIVED" and r1["l_label"] == "DERIVED"
    trading_days = vr.count_trading_days(vr.ECM_START, AS_OF, HOLIDAYS)
    mean = (95 + 124 + 129) / 3  # history days on or after 2024-08-15 only
    assert r1["n_distinct_dates"] == trading_days
    # the listing counts documents; research §4 puts them at x2-4 per event, so N is divided by a DERIVED 2.5
    assert vr.DOCUMENT_TO_EVENT_FACTOR == 2.5
    assert r1["n_events"] == round(mean * trading_days * vr.STOCK_SHARE_OF_LISTING / 2.5)
    assert "2.5" in r1["note"] and "document" in r1["note"]
    assert r1["tier"] == assess(r1["n_events"], r1["n_distinct_dates"], 0.10).tier.value


def test_d4_is_forward_only_and_no_go_while_the_window_is_closed() -> None:
    d4 = _rows(_counts([]))["D4"]
    assert (d4["n_events"], d4["n_distinct_dates"], d4["n_label"]) == (0, 0, "OBSERVED")
    assert d4["tier"] == PowerTier.NO_GO.value and d4["mde"] == float("inf")
    forward = {**RECONCILE, "window_open": True, "forward": {"n_events": 50, "n_distinct_dates": 5, "by_type": {}}}
    d4_open = _rows(_counts([]), forward)["D4"]
    assert (d4_open["n_events"], d4_open["n_distinct_dates"]) == (50, 5)


def test_go_no_go_refuses_a_counts_row_with_more_dates_than_events() -> None:
    with pytest.raises(ValueError, match="NQ_HDQT"):
        _rows(_counts([_gy("NQ_HDQT", 2025, 10, 11)]))


def test_d2_after_an_early_stop_scales_by_the_symbols_fetched_and_is_derived() -> None:
    # spec-math F1: the quota stops the run after 50 of 200 symbols; N must not be read as the whole universe.
    stop = {"reason": "quota_below_threshold", "symbol": None, "type": None, "code": None, "remaining_pct": 20.0}
    nq = _rows(_counts([_gy("NQ_HDQT", 2025, 500, 200)], universe=200, ok=50, stop=stop))["D2_NQ_HDQT"]
    assert nq["n_events"] == 2000 and nq["n_label"] == "DERIVED"
    assert nq["n_distinct_dates"] == 200
    assert "quota_below_threshold" in nq["note"] and "50" in nq["note"]


def test_d2_with_failed_symbols_scales_by_symbols_ok_and_is_derived() -> None:
    nq = _rows(_counts([_gy("NQ_HDQT", 2025, 380, 150)], universe=200, ok=190))["D2_NQ_HDQT"]
    assert nq["n_events"] == 400 and nq["n_label"] == "DERIVED"
    assert "190" in nq["note"]


def test_d2_from_an_older_counts_shape_uses_the_universe_size_and_is_derived() -> None:
    nq = _rows(_legacy_counts([_gy("NQ_HDQT", 2025, 1000, 350)], universe=200))["D2_NQ_HDQT"]
    assert (nq["n_events"], nq["n_distinct_dates"]) == (1000, 350)
    assert nq["n_label"] == "DERIVED" and "symbols.ok" in nq["note"]


def test_d2_drops_years_before_the_label_depth_start() -> None:
    # vnstock ohlcv reaches back to 2018-10-25 only, so 2018 is partial and dropped (main's decision).
    assert vr.LABEL_DEPTH_START_YEAR == 2019
    rows = _rows(_counts([_gy("NQ_HDQT", 2018, 100, 50), _gy("NQ_HDQT", 2025, 400, 150)], universe=200))
    nq = rows["D2_NQ_HDQT"]
    assert (nq["n_events"], nq["n_distinct_dates"], nq["n_label"]) == (400, 150, "OBSERVED")
    assert "2018" in nq["note"] and "2019" in nq["note"]
    only_2018 = _rows(_counts([_gy("NQ_HDQT", 2018, 100, 50)], universe=200))["D2_NQ_HDQT"]
    assert only_2018["n_label"] == "MISSING" and only_2018["n_events"] is None


def test_derived_rows_clamp_l_to_n_instead_of_failing() -> None:
    # spec-math F5: a history-check day with total_count 1 gives N < L; go-no-go must still print a row.
    history = {"kind": "portal_history_check", "years": {"2026": {"date": "2026-10-14", "total_count": 1}}}
    rows = vr.go_no_go_rows(_counts([]), RECONCILE, history, rho=0.10, universe=5, as_of=AS_OF, holidays=HOLIDAYS)
    by_key = {row["key"]: row for row in rows}
    r1 = by_key["R1"]
    assert r1["n_distinct_dates"] == r1["n_events"] and r1["l_label"] == "DERIVED" and "clamped" in r1["note"]
    bctc = by_key["D2_BCTC"]
    assert bctc["n_distinct_dates"] <= bctc["n_events"] and "clamped" in bctc["note"]
    assert r1["tier"] is not None and bctc["tier"] is not None


@pytest.mark.parametrize(
    "change",
    [{"kind": "vn_counts"}, {"schema_version": 2}, {"schema_version": None}, {"kind": None}],
)
def test_go_no_go_refuses_a_counts_summary_of_another_kind_or_schema(change: dict[str, Any], tmp_path: Path) -> None:
    counts = {**_counts([_gy("NQ_HDQT", 2025, 400, 150)]), **change}
    counts = {key: value for key, value in counts.items() if value is not None}
    with pytest.raises(ValueError, match="counts summary"):
        _rows(counts)
    paths = {}
    for name, doc in (("counts", counts), ("reconcile", RECONCILE), ("history", HISTORY)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(doc), encoding="utf-8")
    code = vr.main(["go-no-go", "--counts", str(paths["counts"]), "--reconcile", str(paths["reconcile"]),
                    "--history", str(paths["history"]), "--as-of", AS_OF.isoformat()],
                   env={"FINSEC_DATA_ROOT": str(tmp_path)})
    assert code == 2


def test_render_table_has_one_line_per_row_and_no_titles() -> None:
    rows = vr.go_no_go_rows(_counts([_gy("NQ_HDQT", 2025, 400, 150)]), RECONCILE, HISTORY, rho=0.10, universe=200,
                            as_of=AS_OF, holidays=HOLIDAYS)
    table = vr.render_table(rows, rho=0.10)
    lines = [line for line in table.splitlines() if line.startswith("| ")]
    assert len(lines) == 2 + len(rows)  # header, separator, rows
    assert "DERIVED" in table and "OBSERVED" in table and "MISSING" in table
    assert "R1" in table and "D2" in table and "D4" in table


def test_go_no_go_command_prints_the_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    counts = tmp_path / "counts-summary.json"
    counts.write_text(json.dumps(_counts([_gy("NQ_HDQT", 2025, 400, 150)])), encoding="utf-8")
    reconcile = tmp_path / "reconcile-summary.json"
    reconcile.write_text(json.dumps(RECONCILE), encoding="utf-8")
    history = tmp_path / "history-summary.json"
    history.write_text(json.dumps(HISTORY), encoding="utf-8")
    out_md = tmp_path / "go-no-go.md"
    code = vr.main(["go-no-go", "--counts", str(counts), "--reconcile", str(reconcile), "--history", str(history),
                    "--as-of", AS_OF.isoformat(), "--out", str(out_md)], env={"FINSEC_DATA_ROOT": str(tmp_path)})
    assert code == 0
    printed = capsys.readouterr().out
    assert "| R1" in printed and "| D4" in printed
    assert out_md.read_text(encoding="utf-8").strip() in printed


# --- network shell (fake fetcher) ---------------------------------------------------------------------------------


class FakeNet:
    """Answers by URL substring; records every request. A monotonic clock that only the fake sleeper advances."""

    def __init__(self, answers: list[tuple[str, bytes]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []
        self.now = 1000.0
        self.slept: list[float] = []

    def fetch(self, method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> Any:
        self.calls.append((method, url))
        body = next((answer for marker, answer in self.answers if marker in url), b"")
        at = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)
        return FetchResult(method=method, url=url, final_url=url, status=200 if body else 404,
                           headers=(("Content-Type", "application/json"),), body=body, truncated=False,
                           local_before_utc=at, local_after_utc=at, seconds_to_headers=0.1, seconds_total=0.2,
                           error=None)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _hose_day_body(day: date, start_id: int) -> bytes:
    local = datetime(day.year, day.month, day.day, 9, 0)
    titles = ["AAA: Báo cáo tài chính quý 3", "BBB: Nghị quyết HĐQT", "CCC: Đăng ký giao dịch cổ phiếu"]
    return _hose_body([_hose_entry(start_id + n, titles[n % 3], local + timedelta(minutes=n)) for n in range(6)])


def test_sample_walks_hose_days_politely_saves_bodies_and_writes_sample(tmp_path: Path,
                                                                          capsys: pytest.CaptureFixture[str]) -> None:
    rss = _rss_body([(str(800000 + n), ["Báo cáo tài chính quý 3", "Nghị quyết HĐQT", "Đăng ký giao dịch cổ phiếu"][n % 3],
                      "Tue, 20 Oct 2026 10:00:00 +0700") for n in range(30)])
    net = FakeNet([
        ("startDate=2026-10-21", _hose_day_body(date(2026, 10, 21), 100)),
        ("startDate=2026-10-20", _hose_day_body(date(2026, 10, 20), 200)),
        ("startDate=2026-10-19", _hose_day_body(date(2026, 10, 19), 300)),
        (".rss", rss),
    ])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    doc = vr.run_sample(
        run_dir, day_from=date(2026, 10, 17), day_to=date(2026, 10, 21), holidays=HOLIDAYS, fetcher=net.fetch,
        sleeper=net.sleep, monotonic=net.monotonic, max_requests=10, max_days=30, pool_target=5,
        max_pages_per_day=1, backup_mirror=None,
    )
    hose_calls = [url for _, url in net.calls if "api.hsx.vn" in url]
    assert all("securitiesType/1" in url and "pageSize=20" in url for url in hose_calls)
    assert not any("2026-10-18" in url or "2026-10-17" in url for url in hose_calls)  # weekend skipped
    assert len(net.calls) <= 10
    assert all(gap >= 0 for gap in net.slept)
    saved = sorted(path.name for path in run_dir.iterdir())
    assert any(name.endswith(".json") and "hose" in name for name in saved)
    assert "requests.jsonl" in saved and "sample.json" in saved
    assert len(doc["items"]) == 30
    assert doc["meta"]["history"]["stopped"] == "filled"
    assert doc["window_open"] is False and doc["detection_latency_skipped"] == "window_not_open"
    printed = capsys.readouterr().out
    assert SECRET_TITLE not in printed and "Báo cáo" not in printed


def test_host_gate_keeps_five_seconds_between_requests_to_one_host() -> None:
    net = FakeNet([])
    gate = vr.HostGate(sleeper=net.sleep, monotonic=net.monotonic)
    gate.wait("a")
    gate.done("a")
    net.now += 1.0
    gate.wait("a")
    assert net.slept == [pytest.approx(4.0)]
    gate.done("a")
    gate.wait("b")
    assert len(net.slept) == 1


def test_request_budget_stops_the_walk(tmp_path: Path) -> None:
    net = FakeNet([("api.hsx.vn", _hose_day_body(date(2026, 10, 21), 100))])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    vr.run_sample(run_dir, day_from=date(2026, 9, 1), day_to=date(2026, 10, 21), holidays=HOLIDAYS, fetcher=net.fetch,
                  sleeper=net.sleep, monotonic=net.monotonic, max_requests=3, max_days=30, pool_target=1000,
                  max_pages_per_day=1, backup_mirror=None)
    assert len(net.calls) == 3


def test_cli_bounds_are_enforced() -> None:
    with pytest.raises(SystemExit):
        vr.parse_args(["sample", "--from", "2026-10-01", "--to", "2026-10-21", "--max-requests", "61"])
    with pytest.raises(SystemExit):
        vr.parse_args(["sample", "--from", "2026-10-01", "--to", "2026-10-21", "--max-days", "31"])
    with pytest.raises(SystemExit):
        vr.parse_args(["sample", "--from", "2026-10-22", "--to", "2026-10-21"])


def test_shape_constants_document_the_shared_files() -> None:
    assert "by_group_year" in vr.COUNTS_SUMMARY_SHAPE and "n_distinct_dates" in vr.COUNTS_SUMMARY_SHAPE
    # logic F8: the kind vn_counts.py writes, and the fields go-no-go reads to scale D2
    assert '"kind": "vn_counts_summary"' in vr.COUNTS_SUMMARY_SHAPE
    assert '"kind": "vn_counts",' not in vr.COUNTS_SUMMARY_SHAPE
    for key in ('"schema_version": 1', '"symbols"', '"ok"', '"stop"'):
        assert key in vr.COUNTS_SUMMARY_SHAPE
    assert "public_date" in vr.VNSTOCK_RAW_LAYOUT and "<SYMBOL>.json" in vr.VNSTOCK_RAW_LAYOUT


def test_backups_are_read_only_through_iter_backup_members() -> None:
    # RT9: the script never opens or extracts an archive itself.
    source = (SPIKE_DIR / "vn_reconcile.py").read_text(encoding="utf-8")
    assert "extractall" not in source and "import tarfile" not in source
    assert "iter_backup_members" in source


def test_hose_extra_pages_are_evenly_spaced_and_hnx_listing_tops_up_a_short_rss(tmp_path: Path) -> None:
    rss = _rss_body([("700001", "Báo cáo tài chính quý 3", "Tue, 20 Oct 2026 10:00:00 +0700")])
    row = ("<tr><td>20/10/2026 09:15</td><td>BBB</td><td><a href=\"javascript:funcViewDetailArticlesByID(638921,1)\">"
           "Nghị quyết HĐQT</a></td></tr>")
    listing = f"<table>{row}</table>".encode()
    page = json.loads(_hose_day_body(date(2026, 10, 21), 100))
    page["data"]["paging"]["totalPages"] = 5
    net = FakeNet([(".rss", rss), ("NextPageTinCPNY", listing),
                   ("pageIndex=1&", json.dumps(page).encode("utf-8")),
                   ("api.hsx.vn", _hose_day_body(date(2026, 10, 21), 200))])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    doc = vr.run_sample(run_dir, day_from=date(2026, 10, 21), day_to=date(2026, 10, 21), holidays=HOLIDAYS,
                        fetcher=net.fetch, sleeper=net.sleep, monotonic=net.monotonic, max_requests=10, max_days=1,
                        pool_target=50, max_pages_per_day=3, hnx_post_pages=1, backup_mirror=None)
    hose_pages = [url.split("pageIndex=")[1].split("&")[0] for _, url in net.calls if "api.hsx.vn" in url]
    assert hose_pages == ["1", "3", "5"]
    assert [method for method, url in net.calls if "NextPageTinCPNY" in url] == ["POST"]
    listed = [row for row in doc["items"] if row["source"] == "hnx_post_listing"]
    assert [(row["source_id"], row["symbol"], row["symbol_source"], row["posted_at_label"]) for row in listed] == [
        ("638921", "BBB", "listing_column", "ASSUMED"),
    ]
    assert doc["meta"]["history"]["stopped"] == "max_days"
    assert net.slept and all(gap <= vr.MIN_HOST_GAP_S for gap in net.slept)


def test_open_window_prefers_backup_items_and_counts_forward(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    for offset, day in enumerate((date(2026, 10, 21), date(2026, 10, 22))):
        posted = datetime(day.year, day.month, day.day, 10, 0)
        record = _record("hose_news_api", str(900 + offset),
                         _hose_entry(900 + offset, "AAA: Nghị quyết HĐQT", posted),
                         _ict(day.year, day.month, day.day, 10, 1), PitQuality.FIRST_SEEN)
        _write_backup(mirror, day, {f"raw/hose_news_api/{day.isoformat()}.jsonl": record.to_json_line().encode()})
    net = FakeNet([])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    doc = vr.run_sample(run_dir, day_from=date(2026, 10, 21), day_to=date(2026, 10, 22), holidays=HOLIDAYS,
                        fetcher=net.fetch, sleeper=net.sleep, monotonic=net.monotonic, max_requests=2, max_days=2,
                        pool_target=1, max_pages_per_day=1, backup_mirror=mirror)
    assert doc["window_open"] is True and doc["detection_latency_skipped"] is None
    assert doc["meta"]["backup_days_read"] == ["2026-10-21", "2026-10-22"]
    assert doc["forward"]["n_events"] == 2 and doc["forward"]["n_distinct_dates"] == 2
    assert doc["forward"]["by_type"]["NQ_HDQT"] == {"n_events": 2, "n_distinct_dates": 2}
    window_rows = [row for row in doc["items"] if row["source"] == "capture_backup"]
    assert [row["source_id"] for row in window_rows] == ["900", "901"]
    assert all(row["first_seen_at"] and row["pit_quality"] == "first_seen" for row in window_rows)
    summary = vr.reconcile_summary(doc["items"], {}, overrides_applied=0, window_open=True, forward=doc["forward"])
    assert summary["detection_latency"]["n"] == 2 and summary["detection_latency"]["median_s"] == pytest.approx(60.0)
    assert summary["forward"]["n_events"] == 2


def test_one_backup_day_keeps_the_window_closed(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    day = date(2026, 10, 21)
    record = _record("hose_news_api", "900", _hose_entry(900, "AAA: Nghị quyết HĐQT", datetime(2026, 10, 21, 10)),
                     _ict(2026, 10, 21, 10, 1), PitQuality.FIRST_SEEN)
    _write_backup(mirror, day, {f"raw/hose_news_api/{day.isoformat()}.jsonl": record.to_json_line().encode()})
    net = FakeNet([])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    doc = vr.run_sample(run_dir, day_from=day, day_to=date(2026, 10, 22), holidays=HOLIDAYS, fetcher=net.fetch,
                        sleeper=net.sleep, monotonic=net.monotonic, max_requests=1, max_days=1, pool_target=1,
                        max_pages_per_day=1, backup_mirror=mirror)
    assert doc["window_open"] is False and doc["detection_latency_skipped"] == "window_not_open"
    assert doc["forward"]["n_events"] == 0 and doc["items"] == []


def test_go_no_go_rows_carry_the_power_rule_numbers_and_an_r1_forward_row() -> None:
    rows = vr.go_no_go_rows(_counts([_gy("NQ_HDQT", 2025, 400, 150)], universe=200), RECONCILE, HISTORY, rho=0.10,
                            universe=200, as_of=AS_OF, holidays=HOLIDAYS)
    assert [row["key"] for row in rows] == ["R1", "R1_forward", "D2_BCTC", "D2_NQ_HDQT", "D4"]
    nq = next(row for row in rows if row["key"] == "D2_NQ_HDQT")
    expected = assess(400, 150, 0.10)
    assert nq["n_eff"] == pytest.approx(expected.n_eff) and nq["mde_floor"] == pytest.approx(expected.mde_floor)
    assert next(row for row in rows if row["key"] == "R1_forward")["tier"] == PowerTier.NO_GO.value


def test_stopped_reports_the_hose_walk_even_when_an_hnx_stratum_stays_short(tmp_path: Path) -> None:
    rss = _rss_body([("700001", "Báo cáo tài chính quý 3", "Tue, 20 Oct 2026 10:00:00 +0700")])
    net = FakeNet([(".rss", rss), ("api.hsx.vn", _hose_day_body(date(2026, 10, 21), 100))])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    doc = vr.run_sample(run_dir, day_from=date(2026, 10, 21), day_to=date(2026, 10, 21), holidays=HOLIDAYS,
                        fetcher=net.fetch, sleeper=net.sleep, monotonic=net.monotonic, max_requests=10, max_days=5,
                        pool_target=2, max_pages_per_day=1, hnx_post_pages=1, backup_mirror=None)
    history = doc["meta"]["history"]
    assert history["stopped"] == "filled"
    assert history["pools"]["HNX"] == {"BCTC": 1, "NQ_HDQT": 0, "INSIDER": 0}
    assert history["pools"]["HOSE"] == {"BCTC": 2, "NQ_HDQT": 2, "INSIDER": 2}


def test_r1_ignores_a_negative_or_zero_history_count() -> None:
    # Re-verify finding: a negative totalCount made N negative and go-no-go exit 2; it is dropped like a missing count.
    history = {"kind": "portal_history_check", "years": {
        "2025": {"date": "2025-10-08", "status": 200, "total_count": 124},
        "2026": {"date": "2026-10-14", "status": 200, "total_count": -5},
        "2024": {"date": "2024-10-09", "status": 200, "total_count": 0},
    }}
    rows = vr.go_no_go_rows(_counts([]), RECONCILE, history, rho=0.10, universe=200, as_of=AS_OF, holidays=HOLIDAYS)
    r1 = {row["key"]: row for row in rows}["R1"]
    trading_days = vr.count_trading_days(vr.ECM_START, AS_OF, HOLIDAYS)
    assert r1["n_events"] == round(124 * trading_days * vr.STOCK_SHARE_OF_LISTING / vr.DOCUMENT_TO_EVENT_FACTOR)


def test_hnx_listing_symbol_skips_the_detail_link_and_lower_case_words() -> None:
    # Re-verify finding: the link text "Tin" or a "Xem" cell before the symbol column was upper-cased into a ticker.
    row = ("<tr><td><a onclick=\"funcViewDetailArticlesByID(555,1)\">Tin</a></td><td>Xem</td>"
           "<td>27/10/2026 15:30</td><td>BBB</td></tr>")
    items = vr.hnx_listing_items(f"<table>{row}</table>".encode(), "hnx_post_listing")
    assert [(item.source_id, item.symbol) for item in items] == [("555", "BBB")]
