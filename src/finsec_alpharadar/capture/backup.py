"""Nightly capture backup (plan 261022-1147 phase 2, R2.11, D3; red-team r1 RT9, RT11, RT14, RT15).

`run_backup` copies one CLOSED UTC day (`day < now`'s UTC date, so a manual run at 03:00 ICT cannot take a day that is
still being written) into `<backup_dir>/daily/<day>/`:
- `capture-<day>.tar.gz`: `raw/<source>/<day>.jsonl` and `blobs/<day>/<sha256>.bin`, with no owner names in the headers;
- `state-<day>.sqlite`: an `sqlite3` backup of the COMMITTED state, read through its own connection (the live writer
  connection may hold an open transaction, and a backup through it waits forever, OBSERVED 2026-10-27), converted to
  `journal_mode=DELETE` in the data root before it is copied, so the snapshot is not in WAL mode and reading it never
  creates `-wal`/`-shm` files in the sync folder (RT15);
- `manifest-<day>.json`: name, bytes and sha256 of both files, and sha256 + line count of every input.

Every file goes through temp + `os.replace` with retries (a sync client can lock the target, RT11); the files are read
back and checked against the manifest before the `backups` row is recorded. A verified set is skipped (idempotent); a
set that does not verify is kept and a new `-retry-<n>` set is written beside it. Nothing is ever deleted or
overwritten, sources included. A write failure is swallowed into `kv[write_status:backup]` (heartbeat DEGRADED).

`iter_backup_members` reads an archive back safely (RT9): sha256 against the manifest BEFORE opening, then only regular
files with a whitelisted name listed in the manifest; never `extract`/`extractall`.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import re
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from finsec_alpharadar.capture.heartbeat import write_status_kv_key
from finsec_alpharadar.capture.record import detail_json, error_detail, format_utc, sha256_hex
from finsec_alpharadar.capture.sources import Sleeper
from finsec_alpharadar.capture.storage import CaptureState, Replacer, Syncer, fsync_file, write_atomic

logger = logging.getLogger(__name__)

BACKUP_SCHEMA = "capture-backup/1"
DAILY_DIR = "daily"
SOURCE_NAME = r"[a-z][a-z0-9_]*"
_SQLITE_ROLLBACK_MODE = bytes([1, 1])  # header bytes 18/19: 1/1 = rollback journal, 2/2 = WAL

class BackupError(RuntimeError):
    """A backup set or archive failed a check (sha256, member type or name, manifest shape)."""


class BackupRefused(BackupError, ValueError):
    """The requested day is not a closed UTC day."""


@dataclass(frozen=True)
class BackupResult:
    day: date
    verified: bool
    skipped: bool  # True when a verified set already existed
    suffix: str  # "" or "-retry-<n>"
    manifest_sha256: str | None
    retry_written: bool  # a set that did not verify was kept and a "-retry-<n>" set written
    error: str | None = None  # the class of a swallowed write failure, or "verify_failed"


def backup_names(day: date, suffix: str = "") -> tuple[str, str, str]:
    """(archive, state snapshot, manifest) file names of one backup set."""
    d = day.isoformat()
    return f"capture-{d}{suffix}.tar.gz", f"state-{d}{suffix}.sqlite", f"manifest-{d}{suffix}.json"


def _member_pattern(day: str) -> re.Pattern[str]:
    d = re.escape(day)
    return re.compile(rf"^(?:raw/{SOURCE_NAME}/{d}\.jsonl|blobs/{d}/[0-9a-f]{{64}}\.bin)$")


def _inputs(root: Path, day: date) -> list[tuple[str, bytes]]:
    """(archive name, bytes) of the day's raw files (per source) and blobs, sorted; only whitelisted names."""
    pattern = _member_pattern(day.isoformat())
    paths = sorted((root / "raw").glob(f"*/{day.isoformat()}.jsonl")) + sorted((root / "blobs" / day.isoformat()).glob("*.bin"))
    found: list[tuple[str, bytes]] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if path.is_file() and pattern.match(rel):
            found.append((rel, path.read_bytes()))
    return found


def _archive(inputs: list[tuple[str, bytes]], now: datetime) -> bytes:
    """A tar.gz of `inputs`; headers carry no owner name or id, and gzip no file name."""
    mtime = int(now.timestamp())
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=mtime) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, data in inputs:
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), mtime, 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def snapshot_state(state_path: Path, work_path: Path, *, busy_timeout_s: float = 30.0) -> bytes:
    """The bytes of a rollback-journal copy of the committed state at `state_path`, built at `work_path` (removed after)."""
    companions = [work_path.with_name(work_path.name + extra) for extra in ("", "-journal", "-wal", "-shm")]
    try:
        reader = sqlite3.connect(state_path, timeout=busy_timeout_s)
        try:
            copy = sqlite3.connect(work_path)
            try:
                reader.backup(copy)
                mode = copy.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            finally:
                copy.close()
        finally:
            reader.close()
        if str(mode).lower() != "delete":
            raise BackupError(f"the state snapshot stayed in journal mode {mode!r}")
        data = work_path.read_bytes()
    finally:
        for path in companions:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove a backup work file: %s", path.name)
    if data[18:20] != _SQLITE_ROLLBACK_MODE:
        raise BackupError("the state snapshot is still in WAL mode")
    return data


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError) as err:
        raise BackupError(f"cannot read the manifest {path.name}: {type(err).__name__}") from err
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("day"), str):
        raise BackupError(f"{path.name} is not a capture backup manifest")
    return manifest


def verify_backup_set(folder: Path, day: date, suffix: str = "") -> str | None:
    """sha256 of the set's manifest when the manifest lists exactly its archive and snapshot and both match; else None."""
    archive, snapshot, manifest_name = backup_names(day, suffix)
    manifest_path = folder / manifest_name
    try:
        manifest = _load_manifest(manifest_path)
        entries = {entry.get("name"): entry for entry in manifest["files"] if isinstance(entry, dict)}
        if set(entries) != {archive, snapshot} or manifest["day"] != day.isoformat():
            return None
        for name, entry in entries.items():
            data = (folder / name).read_bytes()
            if entry.get("sha256") != sha256_hex(data) or entry.get("bytes") != len(data):
                return None
        return sha256_hex(manifest_path.read_bytes())
    except (OSError, BackupError):
        return None


def run_backup(
    root: Path,
    backup_dir: Path,
    state: CaptureState,
    day: date,
    *,
    now: datetime,
    sleeper: Sleeper | None = None,
    replacer: Replacer | None = None,
    syncer: Syncer = fsync_file,
) -> BackupResult:
    """Back up the closed UTC `day` (see the module docstring). Raises `BackupRefused` for a day that is not closed at
    `now`; any other failure is returned (`verified=False`) and recorded, never raised. The caller commits."""
    utc_today = now.astimezone(timezone.utc).date()
    if day >= utc_today:
        raise BackupRefused(
            f"{day.isoformat()} is not a closed UTC day at {format_utc(now)}; back up {utc_today.isoformat()} later"
        )
    folder = backup_dir / DAILY_DIR / day.isoformat()
    attempt = 0
    while True:  # find a verified set (skip), or the first free set name
        suffix = "" if attempt == 0 else f"-retry-{attempt}"
        names = backup_names(day, suffix)
        if not any((folder / name).exists() for name in names):
            break
        verified = verify_backup_set(folder, day, suffix)
        if verified is not None:
            existing = state.backup_row(day)
            if existing is None or existing.manifest_sha256 != verified:
                state.record_backup(day, verified, now)
            return BackupResult(day, True, True, suffix, verified, False)
        logger.warning("backup set does not verify, keeping it: day=%s suffix=%s", day.isoformat(), suffix or "-")
        attempt += 1
    retry_written = attempt > 0
    key = write_status_kv_key("backup")
    write: dict[str, Any] = {"sleeper": sleeper, "syncer": syncer}
    if replacer is not None:
        write["replacer"] = replacer
    try:
        inputs = _inputs(root, day)
        archive = _archive(inputs, now)
        snapshot = snapshot_state(state.path, root / "state" / f".backup-{day.isoformat()}.sqlite.tmp")
        manifest = {
            "schema": BACKUP_SCHEMA,
            "day": day.isoformat(),
            "created_at": format_utc(now),
            "files": [
                {"name": name, "bytes": len(data), "sha256": sha256_hex(data)} for name, data in zip(names, (archive, snapshot))
            ],
            "inputs": [
                {
                    "path": rel, "bytes": len(data), "sha256": sha256_hex(data),
                    "lines": data.count(b"\n") if rel.endswith(".jsonl") else None,
                }
                for rel, data in inputs
            ],
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in zip(names, (archive, snapshot, manifest_bytes)):
            write_atomic(folder / name, data, **write)
    except (OSError, sqlite3.Error, BackupError) as err:  # a locked sync folder must not stop capture (RT11)
        logger.error("backup failed: day=%s class=%s", day.isoformat(), type(err).__name__)
        state.set_kv(key, error_detail(err, ok=False, at=format_utc(now), retry_written=retry_written))
        return BackupResult(day, False, False, suffix, None, retry_written, type(err).__name__)
    verified = verify_backup_set(folder, day, suffix)
    if verified != sha256_hex(manifest_bytes):
        logger.error("backup did not verify after writing: day=%s", day.isoformat())
        state.set_kv(key, detail_json(ok=False, at=format_utc(now), reason="verify_failed", retry_written=retry_written))
        return BackupResult(day, False, False, suffix, None, retry_written, "verify_failed")
    state.record_backup(day, verified, now)
    state.set_kv(key, detail_json(ok=True, at=format_utc(now), retry_written=retry_written))
    logger.info("backup verified: day=%s inputs=%d suffix=%s", day.isoformat(), len(inputs), suffix or "-")
    return BackupResult(day, True, False, suffix, verified, retry_written)


def iter_backup_members(archive_path: Path, manifest_path: Path) -> Iterator[tuple[str, bytes]]:
    """(name, bytes) of every member of a backup archive, checked first against its manifest (RT9).

    Raises `BackupError` when the archive is not listed in the manifest or its sha256 differs (before the archive is
    opened), or on a member that is not a regular file, whose name is not `raw/<source>/<day>.jsonl` or
    `blobs/<day>/<64 hex>.bin`, or that the manifest does not list. Nothing is extracted to disk.
    """
    manifest = _load_manifest(manifest_path)
    entry = next((f for f in manifest["files"] if isinstance(f, dict) and f.get("name") == archive_path.name), None)
    if entry is None:
        raise BackupError(f"{archive_path.name} is not listed in {manifest_path.name}")
    data = archive_path.read_bytes()
    if entry.get("sha256") != sha256_hex(data):
        raise BackupError(f"{archive_path.name}: sha256 does not match the manifest")
    pattern = _member_pattern(manifest["day"])
    listed = {item.get("path") for item in manifest.get("inputs", []) if isinstance(item, dict)}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if not member.isreg():
                raise BackupError(f"member {member.name!r} is not a regular file")
            if not pattern.match(member.name):
                raise BackupError(f"member {member.name!r} is not a raw or blob file of {manifest['day']}")
            if member.name not in listed:
                raise BackupError(f"member {member.name!r} is not listed in the manifest")
            handle = tar.extractfile(member)
            if handle is None:
                raise BackupError(f"member {member.name!r} has no data")
            yield member.name, handle.read()
