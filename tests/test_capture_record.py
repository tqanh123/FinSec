"""Raw capture record schema v1 (`finsec_alpharadar.capture.record`, plan 261022-1147 phase 2, R2.2).

Synthetic values only: made-up tickers (AAA, BBB) and titles, fixed aware timestamps. No network, no clock, no sleep.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from finsec_alpharadar.capture.record import (
    SCHEMA_VERSION,
    CaptureRecord,
    CaptureRecordError,
    OutageKind,
    PayloadFormat,
    PitQuality,
    RecordType,
    canonical_item_bytes,
    capture_code_digest,
    capture_version,
    error_detail,
    format_utc,
    make_cycle_id,
    parse_utc,
)

T0 = datetime(2026, 10, 27, 3, 15, 0, 123456, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=2)
HOSE_ITEM: dict[str, Any] = {"id": 9000001, "title": "AAA: Nghị quyết HĐQT số 01", "postedDate": 1793000000, "code": None}


def _item(**changes: Any) -> CaptureRecord:
    payload = changes.pop("payload", HOSE_ITEM)
    canonical = canonical_item_bytes(payload)
    fields: dict[str, Any] = {
        "record_type": RecordType.ITEM,
        "source": "hose_news_api",
        "source_id": "9000001",
        "information_time": T0,
        "first_seen_at": T0,
        "ingested_at": T1,
        "pit_quality": PitQuality.FIRST_SEEN,
        "raw_sha256": hashlib.sha256(canonical).hexdigest(),
        "payload_format": PayloadFormat.JSON if not isinstance(payload, str) else PayloadFormat.TEXT,
        "payload": payload,
        "blob_path": None,
        "byte_count": len(canonical),
        "snapshot_sha256": "a" * 64,
        "request": {"method": "GET", "url": "https://example.invalid/list", "params": {"pageIndex": 1}, "header_names": ["Origin"]},
        "response": {"status": 200, "http_date": "Tue, 27 Oct 2026 03:15:00 GMT", "rtt_ms": 812, "content_type": "application/json"},
        "cycle_id": make_cycle_id(T0, 1),
        "capture_version": "0.1.0+0123456789ab",
    }
    fields.update(changes)
    return CaptureRecord(**fields)


def _snapshot(**changes: Any) -> CaptureRecord:
    body = b'{"success":true,"data":{"list":[]}}'
    sha = hashlib.sha256(body).hexdigest()
    fields: dict[str, Any] = {
        "record_type": RecordType.SNAPSHOT,
        "source": "hose_news_api",
        "source_id": f"sha256:{sha}",
        "information_time": T0,
        "first_seen_at": T0,
        "ingested_at": T1,
        "pit_quality": PitQuality.LATE,
        "raw_sha256": sha,
        "payload_format": PayloadFormat.BLOB,
        "payload": None,
        "blob_path": f"blobs/2026-10-27/{sha}.bin",
        "byte_count": len(body),
        "snapshot_sha256": None,
        "request": {"method": "GET", "url": "https://example.invalid/list", "params": {}, "header_names": []},
        "response": {"status": 200, "http_date": None, "rtt_ms": 10, "content_type": "application/json"},
        "cycle_id": make_cycle_id(T0, 1),
        "capture_version": "0.1.0+0123456789ab",
    }
    fields.update(changes)
    return CaptureRecord(**fields)


def test_record_round_trips_through_one_json_line() -> None:
    for record in (_item(), _snapshot(), _item(payload="<item><guid>638933</guid><title>BBB: tin</title></item>")):
        line = record.to_json_line()
        assert line.endswith("\n") and line.count("\n") == 1
        decoded = json.loads(line)
        assert list(decoded) == sorted(decoded), "keys must be sorted"
        assert line == json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        assert decoded["schema_version"] == SCHEMA_VERSION == 1
        assert decoded["information_time_basis"] == "first_seen"
        assert decoded["first_seen_at"] == "2026-10-27T03:15:00.123456Z"
        assert CaptureRecord.from_json_line(line) == record
        assert CaptureRecord.from_json_line(line.encode("utf-8")) == record


def test_vietnamese_payload_bytes_are_preserved() -> None:
    title = "Nghị quyết Đại hội đồng cổ đông thường niên"
    for form in ("NFC", "NFD"):
        text = unicodedata.normalize(form, title)
        payload = {"id": 1, "title": text}
        record = _item(payload=payload, source_id="1")
        line = record.to_json_line()
        assert text.encode("utf-8") in line.encode("utf-8"), f"{form} bytes must pass through unchanged"
        restored = CaptureRecord.from_json_line(line)
        assert restored.payload["title"].encode("utf-8") == text.encode("utf-8")
    nfc = canonical_item_bytes({"t": unicodedata.normalize("NFC", title)})
    nfd = canonical_item_bytes({"t": unicodedata.normalize("NFD", title)})
    assert nfc != nfd, "the canonical form must not normalize Unicode"


def test_naive_or_non_utc_datetimes_are_refused() -> None:
    naive = T0.replace(tzinfo=None)
    ict = T0.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    for field in ("first_seen_at", "ingested_at"):
        for bad in (naive, ict):
            changes: dict[str, Any] = {field: bad}
            if field == "first_seen_at":
                changes["information_time"] = bad
            with pytest.raises(CaptureRecordError):
                _item(**changes)
    # A zero offset from another tzinfo object is UTC.
    zoned = T0.astimezone(ZoneInfo("UTC"))
    assert _item(information_time=zoned, first_seen_at=zoned).first_seen_at == T0
    for bad_text in (naive, ict):
        with pytest.raises(CaptureRecordError):
            format_utc(bad_text)
    for text in ("2026-10-27T03:15:00Z", "2026-10-27T03:15:00.123456+00:00", "2026-10-27 03:15:00.123456Z", ""):
        with pytest.raises(CaptureRecordError):
            parse_utc(text)
    assert parse_utc(format_utc(T0)) == T0


def test_information_time_must_equal_first_seen_at_in_v1() -> None:
    with pytest.raises(CaptureRecordError, match="information_time"):
        _item(information_time=T0 - timedelta(seconds=1))
    with pytest.raises(CaptureRecordError, match="basis"):
        _item(information_time_basis="portal_posted")


def test_ingested_before_first_seen_is_refused() -> None:
    with pytest.raises(CaptureRecordError, match="ingested_at"):
        _item(ingested_at=T0 - timedelta(microseconds=1))
    assert _item(ingested_at=T0).ingested_at == T0


def test_bad_sha_or_empty_ids_are_refused() -> None:
    for bad in ("A" * 64, "a" * 63, "g" * 64, ""):
        with pytest.raises(CaptureRecordError):
            _snapshot(raw_sha256=bad)
        with pytest.raises(CaptureRecordError):
            _item(snapshot_sha256=bad)
    with pytest.raises(CaptureRecordError, match="raw_sha256"):
        _item(raw_sha256="b" * 64)  # well formed, but not the sha of the canonical payload
    for field in ("source", "source_id", "cycle_id", "capture_version"):
        for empty in ("", "   "):
            with pytest.raises(CaptureRecordError, match=field):
                _item(**{field: empty})
    with pytest.raises(CaptureRecordError, match="source_id"):
        _snapshot(source_id="sha256:" + "0" * 64)  # a snapshot's id names its own body


def test_item_and_snapshot_payload_shapes_are_exclusive() -> None:
    with pytest.raises(CaptureRecordError, match="blob_path"):
        _item(blob_path="blobs/2026-10-27/x.bin")
    with pytest.raises(CaptureRecordError, match="payload"):
        _item(payload=None)
    with pytest.raises(CaptureRecordError, match="payload"):
        _snapshot(payload={"id": 1})
    with pytest.raises(CaptureRecordError, match="blob_path"):
        _snapshot(blob_path=None)
    with pytest.raises(CaptureRecordError, match="payload_format"):
        _snapshot(payload_format=PayloadFormat.JSON)
    with pytest.raises(CaptureRecordError, match="payload_format"):
        _item(payload_format=PayloadFormat.BLOB)
    with pytest.raises(CaptureRecordError, match="payload_format"):
        _item(payload_format=PayloadFormat.TEXT)  # a dict payload is json, not text
    with pytest.raises(CaptureRecordError, match="byte_count"):
        _item(byte_count=-1)


def test_schema_version_is_one() -> None:
    with pytest.raises(CaptureRecordError, match="schema_version"):
        _item(schema_version=2)
    line = json.loads(_item().to_json_line())
    for bad in ({**line, "schema_version": 2}, {**line, "extra": 1}, {k: v for k, v in line.items() if k != "cycle_id"}):
        with pytest.raises(CaptureRecordError):
            CaptureRecord.from_json_line(json.dumps(bad))
    for bad_line in ("", "{", "[]", '{"schema_version": 1}'):
        with pytest.raises(CaptureRecordError):
            CaptureRecord.from_json_line(bad_line)


def test_canonical_item_bytes_ignore_key_order() -> None:
    first = canonical_item_bytes({"id": 1, "title": "AAA", "nested": {"b": 2, "a": 1}})
    second = canonical_item_bytes({"nested": {"a": 1, "b": 2}, "title": "AAA", "id": 1})
    assert first == second == b'{"id":1,"nested":{"a":1,"b":2},"title":"AAA"}'
    assert canonical_item_bytes("<item>Đ</item>") == "<item>Đ</item>".encode("utf-8")
    with pytest.raises(CaptureRecordError):
        canonical_item_bytes({"x": float("nan")})  # strict JSON only


def test_capture_code_digest_ignores_crlf(tmp_path: Path) -> None:
    lf, crlf, changed = tmp_path / "lf", tmp_path / "crlf", tmp_path / "changed"
    for directory in (lf, crlf, changed):
        directory.mkdir()
    (lf / "a.py").write_bytes(b"x = 1\ny = 2\n")
    (crlf / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    (changed / "a.py").write_bytes(b"x = 1\ny = 3\n")
    for directory in (lf, crlf, changed):
        (directory / "notes.txt").write_bytes(b"not code")
    assert capture_code_digest(lf) == capture_code_digest(crlf)
    assert capture_code_digest(lf) != capture_code_digest(changed)
    assert len(capture_code_digest()) == 64
    version = capture_version()
    package, _, digest = version.partition("+")
    assert package == "0.1.0" and digest == capture_code_digest()[:12]


def test_cycle_id_and_outage_kinds_and_error_detail() -> None:
    assert make_cycle_id(T0, 7) == "20261027T031500123456Z-7"
    assert {kind.value for kind in OutageKind} == {
        "process_down", "host_stalled", "clock_step", "source_unreachable", "source_rejected", "source_invalid_payload",
        "coverage_overflow", "vendor_auth_unreachable", "vendor_release_expired", "vendor_quota_exceeded", "vendor_unavailable",
    }

    class VendorError(Exception):
        code = "VENDOR_CODE"

    vendor_text = "placeholder-value-1 for AAA"
    detail = json.loads(error_detail(VendorError(vendor_text), stage="events"))
    assert detail == {
        "class": "VendorError",
        "code": "VENDOR_CODE",
        "message_sha256": hashlib.sha256(vendor_text.encode("utf-8")).hexdigest(),
        "message_len": len(vendor_text),
        "stage": "events",
    }
    assert "placeholder-value-1" not in error_detail(VendorError(vendor_text))
    assert json.loads(error_detail(ValueError("x")))["code"] is None
    assert dataclasses.is_dataclass(CaptureRecord)
