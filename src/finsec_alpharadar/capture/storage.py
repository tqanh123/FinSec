"""Capture storage (plan 261022-1147 phase 2, R2.3, R2.4): the append-only raw archive, the blob store and the SQLite state.

Layout under the data root (which must already exist; only subdirectories are created here):
- `raw/<source>/<YYYY-MM-DD>.jsonl`: UTC date of `ingested_at`, opened in binary append mode, synced once per cycle;
- `blobs/<YYYY-MM-DD>/<sha256>.bin`: temp file -> flush + fsync -> `os.replace`; an existing blob is re-verified;
- `state/capture.sqlite`: `journal_mode=WAL`, `synchronous=FULL`.

Durability before commit (red-team r1 RT12): every new blob is synced when it is written and every touched JSONL file is
synced by `RawArchive.sync()`; `durable_commit` runs that sync and only then commits the state. `reconcile_archive`
replays the archive tail written after the last commit into `seen`, so a crash between the fsync and the commit cannot
produce a second record. Only the injected syncer touches the disk's write cache, and nothing here reads a clock.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from finsec_alpharadar.capture.record import (
    CaptureRecord,
    CaptureRecordError,
    OutageKind,
    RecordType,
    detail_json,
    format_utc,
    parse_utc,
    sha256_hex,
)

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = "1"
POLL_STATUSES = frozenset({"ok", "error"})
RUN_MODES = frozenset({"run", "once"})

# Called with an open, written handle and the destination path its bytes belong to; must make them durable.
Syncer = Callable[[BinaryIO, Path], None]
Replacer = Callable[[Path, Path], None]


def fsync_file(handle: BinaryIO, path: Path) -> None:
    """Flush Python's buffer and `os.fsync` the handle (FlushFileBuffers on Windows). `path` is only for the caller."""
    handle.flush()
    os.fsync(handle.fileno())


REPLACE_RETRIES = 3  # R2.10/R2.11: a sync client can hold the target open (WinError 5); retry 3 times, 2 s apart
REPLACE_RETRY_WAIT_S = 2.0


def replace_with_retry(
    temp: Path,
    target: Path,
    *,
    replacer: Replacer = os.replace,
    sleeper: Callable[[float], None] | None = None,
    retries: int = REPLACE_RETRIES,
    wait_s: float = REPLACE_RETRY_WAIT_S,
) -> None:
    """`replacer(temp, target)`, retried `retries` times after an `OSError`, waiting `wait_s` through the injected
    `sleeper` between tries (None: no wait). The last `OSError` is raised; the caller decides what a failure means."""
    for attempt in range(retries + 1):
        try:
            replacer(temp, target)
            return
        except OSError:
            if attempt == retries:
                raise
            logger.warning("replace failed, retrying: target=%s attempt=%d", target.name, attempt + 1)
            if sleeper is not None:
                sleeper(wait_s)


def write_atomic(
    target: Path,
    data: bytes,
    *,
    replacer: Replacer = os.replace,
    sleeper: Callable[[float], None] | None = None,
    syncer: Syncer = fsync_file,
) -> None:
    """Write `data` to a temp file next to `target`, sync it, then `replace_with_retry` it over `target`. On failure the
    temp file is removed and the `OSError` raised; `target` is never left half-written."""
    temp = target.with_name(f".{target.name}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            syncer(handle, target)
        replace_with_retry(temp, target, replacer=replacer, sleeper=sleeper)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove a temp file: %s", temp.name)
        raise


class StorageError(RuntimeError):
    """The archive, the blob store or the state refused an operation (missing root, malformed detail, bad row)."""


def snapshot_kv_key(source: str) -> str:
    """`kv` key of the sha256 of a source's last archived snapshot body."""
    return f"last_snapshot_sha256:{source}"


def _check_detail(detail: str | None) -> str | None:
    """A detail is None or a JSON object string (from `record.error_detail` / `record.detail_json`), never raw text."""
    if detail is None:
        return None
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError) as err:
        raise StorageError("detail must be a JSON object built by error_detail/detail_json, not free text") from err
    if not isinstance(parsed, dict):
        raise StorageError("detail must be a JSON object built by error_detail/detail_json")
    return detail


def _existing_dir(path: Path, name: str) -> Path:
    if not path.is_dir():
        raise StorageError(f"{name} {path} does not exist or is not a directory")
    return path


class RawArchive:
    """Append-only JSONL archive. Handles opened by `append` stay open until `sync()` syncs each file once and closes it."""

    def __init__(self, root: Path, *, syncer: Syncer = fsync_file) -> None:
        self.root = _existing_dir(Path(root), "data root")
        self._syncer = syncer
        self._open: dict[Path, BinaryIO] = {}

    def path_for(self, source: str, day: date) -> Path:
        return self.root / "raw" / source / f"{day.isoformat()}.jsonl"

    def append(self, record: CaptureRecord) -> Path:
        """Write one line to the file of the record's source and UTC `ingested_at` date; return that path."""
        path = self.path_for(record.source, record.ingested_at.astimezone(timezone.utc).date())
        handle = self._open.get(path)
        if handle is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._open[path] = path.open("ab")
        handle.write(record.to_json_line().encode("utf-8"))
        return path

    def sync(self) -> list[Path]:
        """Flush + fsync every file touched since the last sync, once each, and close them. Returns the paths."""
        synced: list[Path] = []
        try:
            for path, handle in self._open.items():
                self._syncer(handle, path)
                synced.append(path)
        finally:
            for handle in self._open.values():
                handle.close()
            self._open.clear()
        return synced

    def close(self) -> None:
        self.sync()

    def source_names(self) -> list[str]:
        raw = self.root / "raw"
        return sorted(p.name for p in raw.iterdir() if p.is_dir()) if raw.is_dir() else []

    def files_for_day(self, day: date) -> list[Path]:
        return [p for p in (self.path_for(s, day) for s in self.source_names()) if p.is_file()]

    @staticmethod
    def read_records(path: Path) -> Iterator[CaptureRecord]:
        """Every record of a file, split on b"\\n" only; a malformed line raises `CaptureRecordError`."""
        for line in path.read_bytes().split(b"\n"):
            if line:
                yield CaptureRecord.from_json_line(line)

    def repair_tail(self, path: Path) -> int:
        """Cut a torn tail: a last line without "\\n" or trailing lines that do not parse. Returns the bytes cut."""
        data = path.read_bytes()
        end = len(data)
        while end > 0:
            if data[end - 1 : end] != b"\n":
                end = data.rfind(b"\n", 0, end) + 1
                continue
            start = data.rfind(b"\n", 0, end - 1) + 1
            try:
                CaptureRecord.from_json_line(data[start : end - 1])
                break
            except (CaptureRecordError, UnicodeDecodeError):
                end = start
        cut = len(data) - end
        if cut:
            with path.open("r+b") as handle:
                handle.truncate(end)
                self._syncer(handle, path)
            logger.warning("archive tail repaired: file=%s bytes_cut=%d", path.name, cut)
        return cut

    def repair_tails(self, days: Iterable[date]) -> dict[Path, int]:
        """`repair_tail` on every source's file of each day; returns only the files that were cut."""
        cuts: dict[Path, int] = {}
        for day in days:
            for path in self.files_for_day(day):
                cut = self.repair_tail(path)
                if cut:
                    cuts[path] = cut
        return cuts


@dataclass(frozen=True)
class BlobPut:
    rel_path: str  # `blobs/<YYYY-MM-DD>/<sha256>.bin`, relative to the data root
    sha256: str
    byte_count: int
    written: bool  # False when an intact copy already existed
    rewritten: bool = False  # True when an existing copy had the wrong sha256 and was replaced


class BlobStore:
    """Content-addressed snapshot bodies under `blobs/<day>/`."""

    def __init__(self, root: Path, *, syncer: Syncer = fsync_file, replacer: Replacer = os.replace) -> None:
        self.root = _existing_dir(Path(root), "data root")
        self._syncer = syncer
        self._replace = replacer

    @staticmethod
    def rel_path(sha256: str, day: date) -> str:
        return f"blobs/{day.isoformat()}/{sha256}.bin"

    def put(self, body: bytes, day: date) -> BlobPut:
        sha = sha256_hex(body)
        rel = self.rel_path(sha, day)
        target = self.root / rel
        rewritten = False
        if target.is_file():
            if sha256_hex(target.read_bytes()) == sha:
                return BlobPut(rel, sha, len(body), written=False)
            logger.warning("blob sha256 mismatch, rewriting: expected=%s file=%s", sha, target.name)
            rewritten = True
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp")
        with temp.open("wb") as handle:
            handle.write(body)
            self._syncer(handle, target)
        self._replace(temp, target)
        return BlobPut(rel, sha, len(body), written=True, rewritten=rewritten)


@dataclass(frozen=True)
class PollRow:
    """One row of `polls`. `detail` is None or JSON from `error_detail`/`detail_json`; `id` is set when read back."""

    cycle_id: str
    source: str
    started_at: datetime
    finished_at: datetime
    status: str  # "ok" | "error"
    outage_kind: OutageKind | None
    detail: str | None
    http_status: int | None
    http_date: str | None
    rtt_ms: float | None
    skew_ms: float | None
    items_total: int
    items_new: int
    items_revised: int
    pages: int
    run_mode: str  # "run" | "once"
    pid: int
    id: int | None = None

    def __post_init__(self) -> None:
        if self.status not in POLL_STATUSES:
            raise StorageError(f"poll status must be one of {sorted(POLL_STATUSES)}, got {self.status!r}")
        if self.run_mode not in RUN_MODES:
            raise StorageError(f"poll run_mode must be one of {sorted(RUN_MODES)}, got {self.run_mode!r}")
        if self.outage_kind is not None:
            object.__setattr__(self, "outage_kind", OutageKind(self.outage_kind))


@dataclass(frozen=True)
class OutageRow:
    id: int
    kind: OutageKind
    source: str | None  # None = the whole process
    start_at: datetime
    end_at: datetime | None  # None = still open
    detected_at: datetime
    detail: str | None


@dataclass(frozen=True)
class AliveRow:
    last_alive_at: datetime
    last_alive_mono: float
    pid: int


@dataclass(frozen=True)
class BackupRow:
    day: date
    manifest_sha256: str
    verified_at: datetime


@dataclass(frozen=True)
class ReconcileReport:
    records_scanned: int
    seen_added: int
    snapshots_restored: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, source_id TEXT NOT NULL, raw_sha256 TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id, raw_sha256));
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, source TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('ok', 'error')), outage_kind TEXT, detail TEXT,
    http_status INTEGER, http_date TEXT, rtt_ms REAL, skew_ms REAL, items_total INTEGER NOT NULL,
    items_new INTEGER NOT NULL, items_revised INTEGER NOT NULL, pages INTEGER NOT NULL,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('run', 'once')), pid INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS polls_source_started ON polls (source, started_at);
CREATE TABLE IF NOT EXISTS outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT, start_at TEXT NOT NULL, end_at TEXT,
    detected_at TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY, rel_path TEXT NOT NULL, first_written_at TEXT NOT NULL, byte_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS alive (
    id INTEGER PRIMARY KEY CHECK (id = 1), last_alive_at TEXT NOT NULL, last_alive_mono REAL NOT NULL, pid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeats (written_at TEXT NOT NULL, status TEXT NOT NULL, rel_path TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backups (day TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL, verified_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_POLL_COLUMNS = (
    "cycle_id", "source", "started_at", "finished_at", "status", "outage_kind", "detail", "http_status", "http_date",
    "rtt_ms", "skew_ms", "items_total", "items_new", "items_revised", "pages", "run_mode", "pid",
)


_OUTAGE_COLUMNS = "id, kind, source, start_at, end_at, detected_at, detail"


def _opt_utc(text: str | None) -> datetime | None:
    return parse_utc(text) if text is not None else None


class StateReader:
    """Read queries over a capture state connection. `CaptureState` adds the writes; `open_snapshot` reads a backup
    snapshot without writing anything (the audit, R2.12)."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = Path(path)

    @classmethod
    def open_snapshot(cls, path: Path) -> StateReader:
        """Open a state snapshot read-only with `immutable=1`: no `-wal`/`-shm`/journal file is created next to it, and a
        read-only directory works. A file whose header says WAL (bytes 18/19 = 2) is refused: pass a snapshot from
        `backup`, which is in rollback-journal mode (red-team r1 RT15)."""
        path = Path(path).resolve()
        try:
            with path.open("rb") as handle:
                header = handle.read(100)
        except OSError as err:
            raise StorageError(f"cannot read the state snapshot {path}: {type(err).__name__}") from err
        if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
            raise StorageError(f"{path} is not an SQLite database")
        if header[18] == 2 or header[19] == 2:
            raise StorageError(f"{path} is in WAL mode: pass a snapshot from `backup`, not the live state")
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        except sqlite3.Error as err:
            raise StorageError(f"cannot open the state snapshot {path}: {type(err).__name__}") from err
        return cls(connection, path)

    def __enter__(self) -> StateReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close without committing: an open transaction is rolled back."""
        self.connection.close()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def last_commit_at(self) -> datetime | None:
        return _opt_utc(self.get_meta("last_commit_at"))

    def get_kv(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def is_seen(self, source: str, source_id: str, raw_sha256: str) -> bool:
        return self.seen_first_at(source, source_id, raw_sha256) is not None

    def seen_first_at(self, source: str, source_id: str, raw_sha256: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT first_seen_at FROM seen WHERE source = ? AND source_id = ? AND raw_sha256 = ?",
            (source, source_id, raw_sha256),
        ).fetchone()
        return parse_utc(row[0]) if row else None

    def has_source_id(self, source: str, source_id: str) -> bool:
        """True when any version of `source_id` was seen: a new sha of a known id is a revision."""
        row = self.connection.execute("SELECT 1 FROM seen WHERE source = ? AND source_id = ? LIMIT 1", (source, source_id))
        return row.fetchone() is not None

    def _select_polls(
        self,
        source: str | None,
        *,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        order: str = "ASC",
        limit: int | None = None,
    ) -> list[PollRow]:
        clauses: list[str] = []
        params: list[object] = []
        for clause, value in (
            ("source = ?", source),
            ("status = ?", status),
            ("started_at >= ?", format_utc(since) if since is not None else None),
            ("started_at < ?", format_utc(until) if until is not None else None),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT id, {', '.join(_POLL_COLUMNS)} FROM polls{where} ORDER BY started_at {order}, id {order}"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows: list[PollRow] = []
        for row in self.connection.execute(query, params):
            data = dict(zip(("id", *_POLL_COLUMNS), row))
            data["started_at"], data["finished_at"] = parse_utc(data["started_at"]), parse_utc(data["finished_at"])
            rows.append(PollRow(**data))
        return rows

    def last_poll(self, source: str, *, status: str | None = None) -> PollRow | None:
        """The poll of `source` with the latest `started_at` (optionally only "ok" or "error"), or None."""
        rows = self._select_polls(source, status=status, order="DESC", limit=1)
        return rows[0] if rows else None

    def polls(
        self,
        source: str | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        status: str | None = None,
    ) -> list[PollRow]:
        """Polls (of `source`, or of every source) with `since <= started_at < until`, oldest first."""
        return self._select_polls(source, status=status, since=since, until=until)

    def outage(self, outage_id: int) -> OutageRow | None:
        row = self.connection.execute(
            f"SELECT {_OUTAGE_COLUMNS} FROM outages WHERE id = ?", (outage_id,)
        ).fetchone()
        return self._outage_row(row) if row else None

    @staticmethod
    def _outage_row(row: tuple[Any, ...]) -> OutageRow:
        oid, kind, source, start_at, end_at, detected_at, detail = row
        return OutageRow(int(oid), OutageKind(kind), source, parse_utc(start_at), _opt_utc(end_at), parse_utc(detected_at), detail)

    def _open_rows(self, where: str, params: tuple[object, ...]) -> list[OutageRow]:
        query = f"SELECT {_OUTAGE_COLUMNS} FROM outages WHERE end_at IS NULL{where} ORDER BY id"
        return [self._outage_row(row) for row in self.connection.execute(query, params)]

    def open_outages(self, source: str | None) -> list[OutageRow]:
        """Open outages of `source` (None = the process-wide ones only)."""
        return self._open_rows(" AND source IS ?", (source,))

    def all_open_outages(self) -> list[OutageRow]:
        """Every open outage, of any source or of the whole process (startup closes those left by a crash)."""
        return self._open_rows("", ())

    def outages_between(self, source: str, start: datetime, end: datetime) -> list[OutageRow]:
        """Outages of `source` or of the whole process that intersect (start, end]; an open outage extends to the future."""
        rows = self.connection.execute(
            f"SELECT {_OUTAGE_COLUMNS} FROM outages "
            "WHERE (source = ? OR source IS NULL) AND start_at <= ? AND (end_at IS NULL OR end_at > ?) ORDER BY id",
            (source, format_utc(end), format_utc(start)),
        )
        return [self._outage_row(row) for row in rows]

    def get_alive(self) -> AliveRow | None:
        row = self.connection.execute("SELECT last_alive_at, last_alive_mono, pid FROM alive WHERE id = 1").fetchone()
        return AliveRow(parse_utc(row[0]), float(row[1]), int(row[2])) if row else None

    def backup_row(self, day: date) -> BackupRow | None:
        """The verified backup of UTC `day`, or None when it was never recorded."""
        row = self.connection.execute(
            "SELECT day, manifest_sha256, verified_at FROM backups WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        return BackupRow(date.fromisoformat(row[0]), row[1], parse_utc(row[2])) if row else None


    def outages_overlapping(self, start: datetime, end: datetime) -> list[OutageRow]:
        """Every outage (any source or the whole process) that intersects (start, end]; an open one extends to the future."""
        rows = self.connection.execute(
            f"SELECT {_OUTAGE_COLUMNS} FROM outages WHERE start_at <= ? AND (end_at IS NULL OR end_at > ?) ORDER BY id",
            (format_utc(end), format_utc(start)),
        )
        return [self._outage_row(row) for row in rows]

    def all_outages(self) -> list[OutageRow]:
        return [self._outage_row(row) for row in self.connection.execute(f"SELECT {_OUTAGE_COLUMNS} FROM outages ORDER BY id")]

    def heartbeat_times(self, since: datetime, until: datetime) -> list[datetime]:
        """`written_at` of the heartbeats recorded with `since <= written_at < until`, oldest first."""
        rows = self.connection.execute(
            "SELECT written_at FROM heartbeats WHERE written_at >= ? AND written_at < ? ORDER BY written_at",
            (format_utc(since), format_utc(until)),
        )
        return [parse_utc(row[0]) for row in rows]

    def last_backup(self) -> BackupRow | None:
        """The verified backup of the latest UTC day, or None."""
        row = self.connection.execute(
            "SELECT day, manifest_sha256, verified_at FROM backups ORDER BY day DESC LIMIT 1"
        ).fetchone()
        return BackupRow(date.fromisoformat(row[0]), row[1], parse_utc(row[2])) if row else None


class CaptureState(StateReader):
    """The SQLite state of R2.4. Writes join one transaction that `commit(at)` ends; closing without commit loses it."""

    def __init__(self, path: Path, *, busy_timeout_s: float = 5.0) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=busy_timeout_s)
        except (OSError, sqlite3.Error) as err:
            raise StorageError(f"cannot open the capture state at {self.path}: {type(err).__name__}") from err
        try:
            mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise StorageError(f"PRAGMA journal_mode=WAL returned {mode!r}")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript(_SCHEMA)
            self.connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (STATE_SCHEMA_VERSION,))
            self.connection.commit()
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> CaptureState:
        return self

    def pragma(self, name: str) -> object:
        if not name.isidentifier():
            raise StorageError(f"bad pragma name {name!r}")
        return self.connection.execute(f"PRAGMA {name}").fetchone()[0]

    def commit(self, at: datetime) -> None:
        """Record `meta.last_commit_at = at` and commit. Call through `durable_commit` once a cycle wrote archive lines."""
        self.set_meta("last_commit_at", format_utc(at))
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def set_kv(self, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    def add_seen(self, source: str, source_id: str, raw_sha256: str, first_seen_at: datetime) -> bool:
        """Insert the key unless present (the earliest `first_seen_at` stays); True when it was new."""
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO seen (source, source_id, raw_sha256, first_seen_at) VALUES (?, ?, ?, ?)",
            (source, source_id, raw_sha256, format_utc(first_seen_at)),
        )
        return cursor.rowcount == 1

    def insert_poll(self, poll: PollRow) -> int:
        _check_detail(poll.detail)
        values = [getattr(poll, column) for column in _POLL_COLUMNS]
        values[2], values[3] = format_utc(poll.started_at), format_utc(poll.finished_at)
        values[5] = poll.outage_kind.value if poll.outage_kind is not None else None
        placeholders = ", ".join("?" for _ in _POLL_COLUMNS)
        cursor = self.connection.execute(f"INSERT INTO polls ({', '.join(_POLL_COLUMNS)}) VALUES ({placeholders})", values)
        return int(cursor.lastrowid or 0)

    def open_outage(
        self, kind: OutageKind, source: str | None, *, start_at: datetime, detected_at: datetime, detail: str | None
    ) -> int:
        """Open an outage, or return the id of the one already open for the same (kind, source)."""
        _check_detail(detail)
        kind = OutageKind(kind)
        row = self.connection.execute(
            "SELECT id FROM outages WHERE kind = ? AND source IS ? AND end_at IS NULL ORDER BY id LIMIT 1", (kind.value, source)
        ).fetchone()
        if row:
            return int(row[0])
        cursor = self.connection.execute(
            "INSERT INTO outages (kind, source, start_at, end_at, detected_at, detail) VALUES (?, ?, ?, NULL, ?, ?)",
            (kind.value, source, format_utc(start_at), format_utc(detected_at), detail),
        )
        return int(cursor.lastrowid or 0)

    def close_outage(self, outage_id: int, end_at: datetime, **detail_fields: object) -> None:
        """Set `end_at`; `detail_fields` (ids, flags, counts) are merged into the stored JSON detail."""
        current = self.outage(outage_id)
        if current is None:
            raise StorageError(f"no outage with id {outage_id}")
        detail = current.detail
        if detail_fields:
            detail = detail_json(**{**(json.loads(detail) if detail else {}), **detail_fields})
        self.connection.execute(
            "UPDATE outages SET end_at = ?, detail = ? WHERE id = ?", (format_utc(end_at), detail, outage_id)
        )

    def close_open_outages(self, source: str | None, end_at: datetime, **detail_fields: object) -> list[int]:
        """Close every open outage of `source` (None = the process-wide ones); returns their ids."""
        ids = [row.id for row in self.open_outages(source)]
        for outage_id in ids:
            self.close_outage(outage_id, end_at, **detail_fields)
        return ids

    def record_blob(self, sha256: str, rel_path: str, first_written_at: datetime, byte_count: int) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO blobs (sha256, rel_path, first_written_at, byte_count) VALUES (?, ?, ?, ?)",
            (sha256, rel_path, format_utc(first_written_at), byte_count),
        )
        return cursor.rowcount == 1

    def set_alive(self, at: datetime, mono: float, pid: int) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO alive (id, last_alive_at, last_alive_mono, pid) VALUES (1, ?, ?, ?)",
            (format_utc(at), float(mono), pid),
        )

    def record_heartbeat(self, written_at: datetime, status: str, rel_path: str, sha256: str) -> None:
        self.connection.execute(
            "INSERT INTO heartbeats (written_at, status, rel_path, sha256) VALUES (?, ?, ?, ?)",
            (format_utc(written_at), status, rel_path, sha256),
        )

    def record_backup(self, day: date, manifest_sha256: str, verified_at: datetime) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO backups (day, manifest_sha256, verified_at) VALUES (?, ?, ?)",
            (day.isoformat(), manifest_sha256, format_utc(verified_at)),
        )

def durable_commit(archive: RawArchive, state: CaptureState, *, at: datetime) -> list[Path]:
    """End a cycle: sync every touched archive file (blobs were synced by `put`), THEN commit the state (RT12)."""
    synced = archive.sync()
    state.commit(at)
    return synced


def reconcile_archive(archive: RawArchive, state: CaptureState, *, now: datetime) -> ReconcileReport:
    """Replay archive lines ingested after `meta.last_commit_at` into `seen` and the last snapshot sha per source into `kv`.

    Without a commit mark the two most recent UTC days (up to `now`) are scanned. Does not commit: the caller's startup
    commits once it has finished.
    """
    since = state.last_commit_at()
    last_day = now.astimezone(timezone.utc).date()
    first_day = since.date() if since is not None else last_day - timedelta(days=1)
    scanned = added = restored = 0
    latest_snapshot: dict[str, CaptureRecord] = {}
    day = first_day
    while day <= last_day:
        for path in archive.files_for_day(day):
            for record in archive.read_records(path):
                if since is not None and record.ingested_at <= since:
                    continue
                scanned += 1
                if record.record_type is RecordType.SNAPSHOT:
                    previous = latest_snapshot.get(record.source)
                    if previous is None or record.ingested_at >= previous.ingested_at:
                        latest_snapshot[record.source] = record
                elif state.add_seen(record.source, record.source_id, record.raw_sha256, record.first_seen_at):
                    added += 1
        day += timedelta(days=1)
    for source, record in latest_snapshot.items():
        if state.get_kv(snapshot_kv_key(source)) != record.raw_sha256:
            state.set_kv(snapshot_kv_key(source), record.raw_sha256)
            restored += 1
    if scanned:
        logger.info("archive reconciled: records=%d seen_added=%d snapshots_restored=%d", scanned, added, restored)
    return ReconcileReport(scanned, added, restored)
