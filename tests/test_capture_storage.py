"""Capture storage (`finsec_alpharadar.capture.storage`, plan 261022-1147 phase 2, R2.3, R2.4, red-team r1 RT12 / LT8).

Everything lives under `tmp_path`; fsync goes through an injected syncer that records which file it was asked to sync.
Synthetic payloads only (made-up tickers AAA, BBB). No network, no clock, no sleep.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from finsec_alpharadar.capture.record import (
    CaptureRecord,
    OutageKind,
    PayloadFormat,
    PitQuality,
    RecordType,
    canonical_item_bytes,
    error_detail,
    make_cycle_id,
)
from finsec_alpharadar.capture.storage import (
    BlobStore,
    CaptureState,
    PollRow,
    RawArchive,
    StorageError,
    durable_commit,
    reconcile_archive,
    snapshot_kv_key,
)

T0 = datetime(2026, 10, 27, 3, 0, 0, tzinfo=timezone.utc)


class RecordingSyncer:
    """Stands in for flush + os.fsync; records the destination path of every sync, in order."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events: list[str] = events if events is not None else []

    def __call__(self, handle: BinaryIO, path: Path) -> None:
        handle.flush()
        self.events.append(f"fsync:{path.name}")


def _item(source: str, source_id: str, *, title: str = "AAA: tin", at: datetime = T0, ingested: datetime | None = None) -> CaptureRecord:
    payload: dict[str, Any] = {"id": int(source_id) if source_id.isdigit() else source_id, "title": title}
    canonical = canonical_item_bytes(payload)
    return CaptureRecord(
        record_type=RecordType.ITEM,
        source=source,
        source_id=source_id,
        information_time=at,
        first_seen_at=at,
        ingested_at=ingested or at,
        pit_quality=PitQuality.FIRST_SEEN,
        raw_sha256=hashlib.sha256(canonical).hexdigest(),
        payload_format=PayloadFormat.JSON,
        payload=payload,
        blob_path=None,
        byte_count=len(canonical),
        snapshot_sha256=None,
        request={"method": "GET", "url": "https://portal.example.invalid/x", "params": {}, "header_names": []},
        response={"status": 200, "http_date": None, "rtt_ms": 5, "content_type": "application/json"},
        cycle_id=make_cycle_id(at, 1),
        capture_version="0.1.0+0123456789ab",
    )


def _snapshot(source: str, body: bytes, *, at: datetime = T0) -> CaptureRecord:
    sha = hashlib.sha256(body).hexdigest()
    return CaptureRecord(
        record_type=RecordType.SNAPSHOT,
        source=source,
        source_id=f"sha256:{sha}",
        information_time=at,
        first_seen_at=at,
        ingested_at=at,
        pit_quality=PitQuality.FIRST_SEEN,
        raw_sha256=sha,
        payload_format=PayloadFormat.BLOB,
        payload=None,
        blob_path=f"blobs/{at.date().isoformat()}/{sha}.bin",
        byte_count=len(body),
        snapshot_sha256=None,
        request={"method": "GET", "url": "https://portal.example.invalid/x", "params": {}, "header_names": []},
        response={"status": 200, "http_date": None, "rtt_ms": 5, "content_type": "application/json"},
        cycle_id=make_cycle_id(at, 1),
        capture_version="0.1.0+0123456789ab",
    )


def _poll(**changes: Any) -> PollRow:
    fields: dict[str, Any] = {
        "cycle_id": make_cycle_id(T0, 1),
        "source": "hose_news_api",
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=4),
        "status": "ok",
        "outage_kind": None,
        "detail": None,
        "http_status": 200,
        "http_date": "Tue, 27 Oct 2026 03:00:03 GMT",
        "rtt_ms": 3900.0,
        "skew_ms": -400.0,
        "items_total": 20,
        "items_new": 2,
        "items_revised": 0,
        "pages": 1,
        "run_mode": "run",
        "pid": 4242,
    }
    fields.update(changes)
    return PollRow(**fields)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    data_root = tmp_path / "capture-root"
    data_root.mkdir()
    return data_root


def test_jsonl_rotates_by_utc_date_of_ingested_at(root: Path) -> None:
    archive = RawArchive(root, syncer=RecordingSyncer())
    late_evening = datetime(2026, 10, 27, 23, 59, 59, 999999, tzinfo=timezone.utc)
    first = _item("hose_news_api", "1", at=late_evening)
    # Seen before midnight UTC, written after it: the file follows ingested_at.
    second = _item("hose_news_api", "2", at=late_evening, ingested=late_evening + timedelta(microseconds=1))
    assert archive.append(first) == root / "raw" / "hose_news_api" / "2026-10-27.jsonl"
    assert archive.append(second) == root / "raw" / "hose_news_api" / "2026-10-28.jsonl"
    archive.sync()
    assert [r.source_id for r in archive.read_records(root / "raw" / "hose_news_api" / "2026-10-27.jsonl")] == ["1"]
    assert [r.source_id for r in archive.read_records(root / "raw" / "hose_news_api" / "2026-10-28.jsonl")] == ["2"]
    assert (root / "raw" / "hose_news_api" / "2026-10-27.jsonl").read_bytes() == first.to_json_line().encode("utf-8")


def test_every_touched_file_is_fsynced_once_before_commit(root: Path) -> None:
    events: list[str] = []
    syncer = RecordingSyncer(events)
    archive, blobs = RawArchive(root, syncer=syncer), BlobStore(root, syncer=syncer)
    state = CaptureState(root / "state" / "capture.sqlite")
    original_commit = state.commit

    def logged_commit(at: datetime) -> None:
        events.append("commit")
        original_commit(at)

    state.commit = logged_commit  # type: ignore[method-assign]
    body = b'{"success":true}'
    put = blobs.put(body, date(2026, 10, 27))
    archive.append(_item("hose_news_api", "1"))
    archive.append(_item("hose_news_api", "2"))
    archive.append(_item("hnx_rss_issuers", "3"))
    durable_commit(archive, state, at=T0)
    assert events == [f"fsync:{put.sha256}.bin", "fsync:2026-10-27.jsonl", "fsync:2026-10-27.jsonl", "commit"]
    assert sorted(e for e in events if e.endswith(".jsonl")) == ["fsync:2026-10-27.jsonl"] * 2  # one per source file
    events.clear()
    durable_commit(archive, state, at=T0 + timedelta(seconds=1))
    assert events == ["commit"], "a file not touched since the last sync is not synced again"
    assert state.last_commit_at() == T0 + timedelta(seconds=1)
    state.close()


def test_torn_tail_is_truncated_and_reported(root: Path) -> None:
    archive = RawArchive(root, syncer=RecordingSyncer())
    good = [_item("hose_news_api", str(n)) for n in (1, 2)]
    for record in good:
        archive.append(record)
    archive.sync()
    path = root / "raw" / "hose_news_api" / "2026-10-27.jsonl"
    intact = path.read_bytes()
    torn = _item("hose_news_api", "3").to_json_line().encode("utf-8")[:-25]
    path.write_bytes(intact + torn)
    assert archive.repair_tails([date(2026, 10, 27)]) == {path: len(torn)}
    assert path.read_bytes() == intact
    garbage = b'{"not":"a record"}\n'
    path.write_bytes(intact + garbage + b"{\"half")
    assert archive.repair_tails([date(2026, 10, 27), date(2026, 10, 26)]) == {path: len(garbage) + 6}
    assert path.read_bytes() == intact
    assert archive.repair_tails([date(2026, 10, 27)]) == {}, "an intact file is left alone"
    assert [r.source_id for r in archive.read_records(path)] == ["1", "2"]


def test_blob_put_is_idempotent_and_leaves_no_temp_file(root: Path) -> None:
    events: list[str] = []
    blobs = BlobStore(root, syncer=RecordingSyncer(events))
    body = "Đại hội cổ đông AAA".encode("utf-8")
    first = blobs.put(body, date(2026, 10, 27))
    second = blobs.put(body, date(2026, 10, 27))
    sha = hashlib.sha256(body).hexdigest()
    assert first.rel_path == second.rel_path == f"blobs/2026-10-27/{sha}.bin"
    assert (first.written, second.written) == (True, False)
    assert (first.sha256, first.byte_count) == (sha, len(body))
    assert [p.name for p in (root / "blobs" / "2026-10-27").iterdir()] == [f"{sha}.bin"]
    assert (root / first.rel_path).read_bytes() == body
    assert events == [f"fsync:{sha}.bin"], "only the first put writes and syncs"


def test_existing_blob_with_a_wrong_sha_is_rewritten(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    blobs = BlobStore(root, syncer=RecordingSyncer())
    body = b"<rss><item><guid>1</guid></item></rss>"
    sha = hashlib.sha256(body).hexdigest()
    target = root / "blobs" / "2026-10-27" / f"{sha}.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(body[:10])  # torn by a power cut
    with caplog.at_level("WARNING"):
        put = blobs.put(body, date(2026, 10, 27))
    assert put.written and put.rewritten
    assert target.read_bytes() == body
    assert [p.name for p in target.parent.iterdir()] == [f"{sha}.bin"]
    assert sha in caplog.text


def test_startup_reconciles_archived_records_into_seen(root: Path) -> None:
    archive = RawArchive(root, syncer=RecordingSyncer())
    state_path = root / "state" / "capture.sqlite"
    state = CaptureState(state_path)
    old = _item("hose_news_api", "1", at=T0 - timedelta(seconds=30))
    archive.append(old)
    state.add_seen("hose_news_api", "1", old.raw_sha256, old.first_seen_at)
    durable_commit(archive, state, at=T0)
    # The next cycle reaches the disk, then the process dies before the state commit.
    new_item = _item("hose_news_api", "2", at=T0 + timedelta(seconds=10))
    body = b'{"success":true,"data":{"list":[2]}}'
    snapshot = _snapshot("hose_news_api", body, at=T0 + timedelta(seconds=10))
    archive.append(snapshot)
    archive.append(new_item)
    state.add_seen("hose_news_api", "2", new_item.raw_sha256, new_item.first_seen_at)
    state.set_kv(snapshot_kv_key("hose_news_api"), snapshot.raw_sha256)
    archive.sync()
    state.close()  # no commit: the open transaction is lost

    state = CaptureState(state_path)
    assert not state.is_seen("hose_news_api", "2", new_item.raw_sha256)
    report = reconcile_archive(archive, state, now=T0 + timedelta(minutes=5))
    assert (report.records_scanned, report.seen_added, report.snapshots_restored) == (2, 1, 1)
    assert state.is_seen("hose_news_api", "2", new_item.raw_sha256)
    assert state.get_kv(snapshot_kv_key("hose_news_api")) == snapshot.raw_sha256
    again = reconcile_archive(archive, state, now=T0 + timedelta(minutes=5))
    assert again.seen_added == 0, "reconciling twice adds nothing"
    state.close()

    # Without a last commit mark, the two most recent UTC days are scanned.
    fresh = CaptureState(root / "state" / "other.sqlite")
    report = reconcile_archive(archive, fresh, now=T0 + timedelta(days=1))
    assert report.records_scanned == 3 and report.seen_added == 2
    fresh.close()


def test_state_uses_wal_and_full_sync(root: Path) -> None:
    state = CaptureState(root / "state" / "capture.sqlite")
    assert state.pragma("journal_mode") == "wal"
    assert state.pragma("synchronous") == 2
    assert state.get_meta("schema_version") == "1"
    tables = {row[0] for row in state.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "seen", "polls", "outages", "blobs", "alive", "heartbeats", "backups", "kv"} <= tables
    state.close()
    with pytest.raises(StorageError):
        CaptureState(root / "missing-dir" / "deeper" / "capture.sqlite")


def test_detail_stores_class_code_hash_and_length_only(root: Path) -> None:
    class VendorError(Exception):
        code = "VENDOR_CODE"

    vendor_text = "HTTP 403 for AAA with placeholder-value-3"
    detail = error_detail(VendorError(vendor_text))
    state = CaptureState(root / "state" / "capture.sqlite")
    state.open_outage(OutageKind.SOURCE_REJECTED, "hose_news_api", start_at=T0, detected_at=T0, detail=detail)
    state.insert_poll(_poll(status="error", outage_kind=OutageKind.SOURCE_REJECTED, detail=detail, http_status=403))
    state.commit(T0)
    stored = [row[0] for row in state.connection.execute("SELECT detail FROM outages UNION ALL SELECT detail FROM polls")]
    assert len(stored) == 2
    for text in stored:
        assert vendor_text not in text and "placeholder" not in text
        assert set(json.loads(text)) == {"class", "code", "message_sha256", "message_len"}
    for raw in (vendor_text, "[1, 2]"):
        with pytest.raises(StorageError, match="detail"):
            state.open_outage(OutageKind.SOURCE_REJECTED, "hnx_rss_issuers", start_at=T0, detected_at=T0, detail=raw)
        with pytest.raises(StorageError, match="detail"):
            state.insert_poll(_poll(status="error", detail=raw))
    state.close()


def test_changed_payload_of_a_known_id_is_a_new_seen_key(root: Path) -> None:
    state = CaptureState(root / "state" / "capture.sqlite")
    first = _item("hose_news_api", "7", title="AAA: tin gốc")
    revised = _item("hose_news_api", "7", title="AAA: tin đã sửa", at=T0 + timedelta(minutes=3))
    assert first.raw_sha256 != revised.raw_sha256
    assert not state.has_source_id("hose_news_api", "7")
    assert state.add_seen("hose_news_api", "7", first.raw_sha256, first.first_seen_at)
    assert not state.add_seen("hose_news_api", "7", first.raw_sha256, revised.first_seen_at)
    assert state.has_source_id("hose_news_api", "7")
    assert state.add_seen("hose_news_api", "7", revised.raw_sha256, revised.first_seen_at)
    assert state.is_seen("hose_news_api", "7", first.raw_sha256) and state.is_seen("hose_news_api", "7", revised.raw_sha256)
    assert not state.has_source_id("hnx_rss_issuers", "7"), "keys are per source"
    assert state.seen_first_at("hose_news_api", "7", first.raw_sha256) == T0, "the earliest first_seen_at is kept"
    state.close()


def test_outage_open_close_and_reopen_state(root: Path) -> None:
    state = CaptureState(root / "state" / "capture.sqlite")
    kind = OutageKind.SOURCE_UNREACHABLE
    first = state.open_outage(kind, "hnx_rss_issuers", start_at=T0, detected_at=T0, detail=None)
    again = state.open_outage(kind, "hnx_rss_issuers", start_at=T0 + timedelta(minutes=3), detected_at=T0, detail=None)
    assert first == again, "a retry or a later failing cycle does not open a second outage"
    assert [o.id for o in state.open_outages("hnx_rss_issuers")] == [first]
    assert state.open_outages("hose_news_api") == []
    end = T0 + timedelta(minutes=9)
    assert state.close_open_outages("hnx_rss_issuers", end) == [first]
    assert state.open_outages("hnx_rss_issuers") == []
    closed = state.outage(first)
    assert closed is not None and closed.end_at == end and closed.start_at == T0 and closed.kind is kind
    reopened = state.open_outage(kind, "hnx_rss_issuers", start_at=end + timedelta(minutes=30), detected_at=end, detail=None)
    assert reopened != first

    process = state.open_outage(OutageKind.PROCESS_DOWN, None, start_at=T0 - timedelta(hours=1), detected_at=T0, detail=None)
    state.close_outage(process, T0 - timedelta(minutes=50), closed_at_restart=True)
    record = state.outage(process)
    assert record is not None and json.loads(record.detail or "{}") == {"closed_at_restart": True}
    # Overlap with (start, end]: the source's own outages and process-wide ones; an open outage covers up to now.
    window = state.outages_between("hnx_rss_issuers", T0 - timedelta(hours=2), T0 + timedelta(hours=2))
    assert {o.id for o in window} == {first, reopened, process}
    assert {o.id for o in state.outages_between("hose_news_api", T0 + timedelta(minutes=1), T0 + timedelta(hours=2))} == set()
    assert {o.id for o in state.outages_between("hnx_rss_issuers", end, end + timedelta(minutes=10))} == set()
    state.commit(T0)
    state.close()


def test_poll_alive_kv_and_blob_rows_round_trip(root: Path) -> None:
    state = CaptureState(root / "state" / "capture.sqlite")
    ok = _poll()
    failed = _poll(started_at=T0 + timedelta(minutes=3), finished_at=T0 + timedelta(minutes=4), status="error",
                   outage_kind=OutageKind.SOURCE_UNREACHABLE, http_status=None, items_new=0)
    ok_id = state.insert_poll(ok)
    state.insert_poll(failed)
    last_ok = state.last_poll("hose_news_api", status="ok")
    assert last_ok is not None and last_ok.id == ok_id and last_ok.finished_at == ok.finished_at
    last_any = state.last_poll("hose_news_api")
    assert last_any is not None and last_any.outage_kind is OutageKind.SOURCE_UNREACHABLE
    assert state.last_poll("hnx_rss_issuers") is None
    with pytest.raises(StorageError):
        _poll(status="partial")
    with pytest.raises(StorageError):
        _poll(run_mode="cron")
    assert state.get_alive() is None
    state.set_alive(T0, 123.5, 4242)
    state.set_alive(T0 + timedelta(seconds=30), 153.5, 4242)
    alive = state.get_alive()
    assert alive is not None and (alive.last_alive_at, alive.last_alive_mono, alive.pid) == (T0 + timedelta(seconds=30), 153.5, 4242)
    state.set_kv("vnstock_cursor:vnstock_events", "4")
    assert state.get_kv("vnstock_cursor:vnstock_events") == "4" and state.get_kv("missing") is None
    assert state.record_blob("a" * 64, "blobs/2026-10-27/" + "a" * 64 + ".bin", T0, 10)
    assert not state.record_blob("a" * 64, "blobs/2026-10-27/" + "a" * 64 + ".bin", T0, 10)
    state.commit(T0)
    state.close()
    with sqlite3.connect(root / "state" / "capture.sqlite") as check:
        assert check.execute("SELECT COUNT(*) FROM polls").fetchone() == (2,)
    check.close()
