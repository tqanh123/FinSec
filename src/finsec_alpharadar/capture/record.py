"""Raw capture record schema v1: the contract that the Phase 1 `store/` and the Phase 3 parser read (plan 261022-1147, R2.2).

One JSON object per line, UTF-8, keys sorted, compact separators, non-ASCII text kept as received (no Unicode
normalization). Every instant is a UTC string `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Readers split archive files on b"\\n" only:
`ensure_ascii=False` leaves U+2028/U+2029 raw inside strings, and `str.splitlines` would cut there.

Read rule of v1 (red-team r1 RT12): for one key `(source, source_id, raw_sha256)` the record with the EARLIEST
`first_seen_at` wins; any later duplicate is ignored.

Capture only extracts ids: `information_time` is provisionally `first_seen_at` (`information_time_basis = "first_seen"`);
the Phase 1/3 parser computes the real one.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from finsec_alpharadar import __version__ as PACKAGE_VERSION

SCHEMA_VERSION = 1
INFORMATION_TIME_BASIS = "first_seen"
UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
CYCLE_ID_FORMAT = "%Y%m%dT%H%M%S%fZ"

_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_DATETIME_FIELDS = ("information_time", "first_seen_at", "ingested_at")


class RecordType(str, Enum):
    ITEM = "item"  # one disclosure / row
    SNAPSHOT = "snapshot"  # one whole response body, stored as a blob


class PitQuality(str, Enum):
    FIRST_SEEN = "first_seen"  # the source was watched without a gap up to this poll
    LATE = "late"  # first poll ever, a missed schedule, or an outage since the last good poll (R2.6)


class PayloadFormat(str, Enum):
    JSON = "json"  # HOSE and vnstock items
    TEXT = "text"  # an RSS `<item>` fragment, verbatim
    BLOB = "blob"  # a snapshot; the body lives under `blob_path`


class OutageKind(str, Enum):
    """Outage classes of R2.7. A failing source opens ONE outage, closed by its next successful poll."""

    PROCESS_DOWN = "process_down"
    HOST_STALLED = "host_stalled"
    CLOCK_STEP = "clock_step"
    SOURCE_UNREACHABLE = "source_unreachable"
    SOURCE_REJECTED = "source_rejected"
    SOURCE_INVALID_PAYLOAD = "source_invalid_payload"
    COVERAGE_OVERFLOW = "coverage_overflow"
    VENDOR_AUTH_UNREACHABLE = "vendor_auth_unreachable"
    VENDOR_RELEASE_EXPIRED = "vendor_release_expired"
    VENDOR_QUOTA_EXCEEDED = "vendor_quota_exceeded"
    VENDOR_UNAVAILABLE = "vendor_unavailable"


class CaptureRecordError(ValueError):
    """A capture record, a UTC string or a JSONL line breaks schema v1."""


def _require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CaptureRecordError(f"{name} must be a tz-aware UTC datetime, got {value!r}")
    if value.utcoffset() != timedelta(0):
        raise CaptureRecordError(f"{name} must have a zero UTC offset, got {value.isoformat()}")
    return value.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    """`YYYY-MM-DDTHH:MM:SS.ffffffZ` of a UTC datetime; a naive or non-zero-offset datetime raises `CaptureRecordError`."""
    return _require_utc(value, "datetime").strftime(UTC_FORMAT)


def parse_utc(text: str) -> datetime:
    """The UTC datetime of a `format_utc` string; any other shape raises `CaptureRecordError`."""
    if not isinstance(text, str) or not _UTC_TEXT.match(text):
        raise CaptureRecordError(f"expected a UTC string YYYY-MM-DDTHH:MM:SS.ffffffZ, got {text!r}")
    return datetime.strptime(text, UTC_FORMAT).replace(tzinfo=timezone.utc)


def make_cycle_id(started_at: datetime, seq: int) -> str:
    """`<started_at %Y%m%dT%H%M%S%fZ>-<seq>`: the id of one poll cycle."""
    return f"{_require_utc(started_at, 'started_at').strftime(CYCLE_ID_FORMAT)}-{seq}"


def canonical_item_bytes(payload: Any) -> bytes:
    """The bytes `raw_sha256` and `byte_count` of an item are computed from.

    A str (an RSS `<item>` fragment) is its UTF-8 encoding; any other value is strict compact JSON with sorted keys and
    non-ASCII kept raw. NaN, infinity or a lone surrogate (no UTF-8 form) raises `CaptureRecordError`. Nothing is
    Unicode-normalized.
    """
    try:
        if isinstance(payload, str):
            return payload.encode("utf-8")
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return text.encode("utf-8")
    except (TypeError, ValueError) as err:  # UnicodeEncodeError is a ValueError
        raise CaptureRecordError(f"item payload is not strict JSON: {type(err).__name__}") from err


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def message_fingerprint(text: str) -> dict[str, Any]:
    """`{"message_sha256", "message_len"}` of a vendor or error message: its text is never stored (red-team r1 LT8)."""
    return {"message_sha256": sha256_hex(text.encode("utf-8")), "message_len": len(text)}


def detail_json(**fields_: Any) -> str:
    """A `polls.detail` / `outages.detail` string: compact JSON with sorted keys. Pass ids, codes, counts, never text."""
    return json.dumps(fields_, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def error_detail(error: BaseException, **extra: Any) -> str:
    """The detail of an error: class name, `code` attribute (str or int, else null), message sha256 and length, plus `extra`."""
    code = getattr(error, "code", None)
    if isinstance(code, bool) or not isinstance(code, (str, int)):
        code = None
    return detail_json(**{"class": type(error).__name__, "code": code, **message_fingerprint(str(error)), **extra})


def capture_code_digest(directory: Path | None = None) -> str:
    """sha256 over the `*.py` files of `directory` (default: this package), sorted by name, with CRLF read as LF.

    The working tree on the dev machine is CRLF (core.autocrlf) while the laptop clone may be LF: the same code must give
    the same digest.
    """
    root = directory if directory is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py"), key=lambda p: p.name):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def capture_version(digest: str | None = None) -> str:
    """`<package __version__>+<first 12 hex of capture_code_digest>`."""
    return f"{PACKAGE_VERSION}+{(digest if digest is not None else capture_code_digest())[:12]}"


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureRecordError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX.match(value):
        raise CaptureRecordError(f"{name} must be 64 lower-case hex digits, got {value!r}")
    return value


def _require_enum(enum_type: type[Enum], value: object, name: str) -> Any:
    try:
        return enum_type(value)
    except ValueError as err:
        raise CaptureRecordError(f"{name} must be one of {[m.value for m in enum_type]}, got {value!r}") from err


@dataclass(frozen=True)
class CaptureRecord:
    """One line of the raw archive (schema v1). Construction validates every rule of R2.2 and raises `CaptureRecordError`.

    Items: `payload` is the verbatim value (dict for json, str for text), `raw_sha256`/`byte_count` describe
    `canonical_item_bytes(payload)`, `blob_path` is None. Snapshots: `payload` is None, `blob_path` is
    `blobs/<YYYY-MM-DD>/<sha256>.bin` relative to the data root, `source_id` is `sha256:<raw_sha256>`.
    """

    record_type: RecordType
    source: str
    source_id: str
    information_time: datetime
    first_seen_at: datetime
    ingested_at: datetime
    pit_quality: PitQuality
    raw_sha256: str
    payload_format: PayloadFormat
    payload: Any
    blob_path: str | None
    byte_count: int
    snapshot_sha256: str | None
    request: Mapping[str, Any]
    response: Mapping[str, Any]
    cycle_id: str
    capture_version: str
    information_time_basis: str = INFORMATION_TIME_BASIS
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise CaptureRecordError(f"schema_version must be {SCHEMA_VERSION}, got {self.schema_version!r}")
        set_ = object.__setattr__  # frozen: normalize enums and datetimes once, here
        set_(self, "record_type", _require_enum(RecordType, self.record_type, "record_type"))
        set_(self, "pit_quality", _require_enum(PitQuality, self.pit_quality, "pit_quality"))
        set_(self, "payload_format", _require_enum(PayloadFormat, self.payload_format, "payload_format"))
        for name in _DATETIME_FIELDS:
            set_(self, name, _require_utc(getattr(self, name), name))
        if self.information_time_basis != INFORMATION_TIME_BASIS:
            raise CaptureRecordError(f"information_time_basis must be {INFORMATION_TIME_BASIS!r} in v1")
        if self.information_time != self.first_seen_at:
            raise CaptureRecordError("information_time must equal first_seen_at in v1")
        if self.ingested_at < self.first_seen_at:
            raise CaptureRecordError("ingested_at must not be earlier than first_seen_at")
        for name in ("source", "source_id", "cycle_id", "capture_version"):
            _require_text(getattr(self, name), name)
        _require_sha(self.raw_sha256, "raw_sha256")
        if self.snapshot_sha256 is not None:
            _require_sha(self.snapshot_sha256, "snapshot_sha256")
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int) or self.byte_count < 0:
            raise CaptureRecordError(f"byte_count must be a non-negative int, got {self.byte_count!r}")
        if not isinstance(self.request, Mapping) or not isinstance(self.response, Mapping):
            raise CaptureRecordError("request and response must be mappings")
        if self.record_type is RecordType.ITEM:
            self._check_item()
        else:
            self._check_snapshot()

    def _check_item(self) -> None:
        if self.blob_path is not None:
            raise CaptureRecordError("an item must not have a blob_path")
        if self.payload is None:
            raise CaptureRecordError("an item must have a payload")
        expected = PayloadFormat.TEXT if isinstance(self.payload, str) else PayloadFormat.JSON
        if self.payload_format is not expected:
            raise CaptureRecordError(f"payload_format of this item must be {expected.value!r}, got {self.payload_format.value!r}")
        canonical = canonical_item_bytes(self.payload)
        if self.raw_sha256 != sha256_hex(canonical):
            raise CaptureRecordError("raw_sha256 is not the sha256 of canonical_item_bytes(payload)")
        if self.byte_count != len(canonical):
            raise CaptureRecordError("byte_count is not the length of canonical_item_bytes(payload)")

    def _check_snapshot(self) -> None:
        if self.payload is not None:
            raise CaptureRecordError("a snapshot must not carry a payload")
        if self.payload_format is not PayloadFormat.BLOB:
            raise CaptureRecordError("payload_format of a snapshot must be 'blob'")
        if not isinstance(self.blob_path, str) or not self.blob_path.strip():
            raise CaptureRecordError("a snapshot must have a blob_path")
        if self.source_id != f"sha256:{self.raw_sha256}":
            raise CaptureRecordError("source_id of a snapshot must be 'sha256:<raw_sha256>'")

    def to_dict(self) -> dict[str, Any]:
        """The JSON-ready mapping of the line: enums as values, datetimes as UTC strings."""
        data = asdict(self)
        for name in ("record_type", "pit_quality", "payload_format"):
            data[name] = getattr(self, name).value
        for name in _DATETIME_FIELDS:
            data[name] = format_utc(getattr(self, name))
        return data

    def to_json_line(self) -> str:
        """One JSONL line, ending in exactly one "\\n"."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"

    @classmethod
    def from_json_line(cls, line: str | bytes) -> CaptureRecord:
        """The exact inverse of `to_json_line`; any key set other than v1's, or any broken value, raises `CaptureRecordError`."""
        try:
            data = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        except ValueError as err:
            raise CaptureRecordError(f"not a JSON line: {type(err).__name__}") from err
        if not isinstance(data, dict):
            raise CaptureRecordError("a record line must be a JSON object")
        expected = {field.name for field in fields(cls)}
        if set(data) != expected:
            missing, extra = sorted(expected - set(data)), sorted(set(data) - expected)
            raise CaptureRecordError(f"record keys differ from schema v1: missing {missing}, unexpected {extra}")
        for name in _DATETIME_FIELDS:
            data[name] = parse_utc(data[name])
        return cls(**data)
