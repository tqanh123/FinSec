"""Shared helpers for the Phase 0 spike scripts under `scripts/spike/` (plan 261022-1147, R1.7).

Stdlib plus `finsec_alpharadar.data_root`. Spike scripts reach the network and are never collected by pytest; their raw
output goes only under `$FINSEC_DATA_ROOT/spike/<kind>/<UTC-date>/`, outside the clone. Nothing here prints or logs an
environment value: errors name the variable or the file, never the value.

`capture/` (Phase 2) has its own fetcher on purpose: capture must not depend on `scripts/`, and a spike must not depend
on capture.
"""
from __future__ import annotations

import email.utils
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from finsec_alpharadar.data_root import DATA_ROOT_ENV, resolve_data_root

# Neutral, no personal data (R1.6).
USER_AGENT = "FinSecAlphaRadar-research/0.1 (personal, non-commercial)"
# Upper bound on one response body: fetched content is untrusted input (docs/code-standards.md "Untrusted input").
MAX_BODY_BYTES = 20 * 1024 * 1024

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KIND = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class SpikeEnvError(ValueError):
    """A spike setting is missing, malformed or still a placeholder. The message names the variable or file only."""


def load_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` pairs of a dotenv file: blank lines and `#` comments skipped, one pair of quotes removed.

    An unquoted value loses a trailing ` # comment`. A malformed line raises with its line number only, never its text.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as err:
        raise SpikeEnvError(f"{path} does not exist") from err
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY.match(key):
            raise SpikeEnvError(f"{path}: line {number} is not KEY=VALUE")
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end == -1:
                raise SpikeEnvError(f"{path}: line {number}: {key} has an unclosed quote")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def require_env(name: str, values: Mapping[str, str], *, placeholders: Iterable[str] = ()) -> str:
    """`values[name]` without surrounding whitespace; raises when it is missing, empty or contains a placeholder."""
    value = values.get(name, "").strip()
    if not value:
        raise SpikeEnvError(f"{name} is not set")
    if any(placeholder and placeholder in value for placeholder in placeholders):
        raise SpikeEnvError(f"{name} still holds the .env.example placeholder")
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def spike_dir(kind: str, *, env: Mapping[str, str] | None = None, today: date | None = None) -> Path:
    """`<FINSEC_DATA_ROOT>/spike/<kind>/<UTC-date>/`, created below an existing data root.

    The data root itself must already exist: a mistyped root fails here instead of being created silently.
    """
    if not _KIND.match(kind):
        raise SpikeEnvError(f"spike kind {kind!r} must match {_KIND.pattern}")
    root = resolve_data_root(os.environ if env is None else env)
    if not root.is_dir():
        raise SpikeEnvError(f"{DATA_ROOT_ENV} points to {root}, which is not an existing directory; create it first")
    day = (today or utc_now().date()).isoformat()
    path = root / "spike" / kind / day
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class FetchResult:
    """One HTTP exchange. `status` is None when no response arrived; `error` then says why (type name and message)."""

    method: str
    url: str
    final_url: str | None
    status: int | None
    headers: tuple[tuple[str, str], ...]
    body: bytes
    truncated: bool
    local_before_utc: datetime
    local_after_utc: datetime
    seconds_to_headers: float | None  # urlopen returns once the status line and headers are in: about TTFB
    seconds_total: float
    error: str | None

    def header(self, name: str) -> str | None:
        """The first header named `name` (case-insensitive), or None."""
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)

    def header_values(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
    max_bytes: int = MAX_BODY_BYTES,
) -> FetchResult:
    """Send one request with the neutral User-Agent and record timings; an HTTP error status is a result, not an exception.

    A connection failure or timeout also returns a result (status None, `error` set): for a probe, the failure is the
    measurement. `timeout` bounds the connect and every socket read (urllib has one timeout for both).
    """
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    before = utc_now()
    start = time.perf_counter()
    seconds_to_headers: float | None = None
    status: int | None = None
    final_url: str | None = None
    response_headers: tuple[tuple[str, str], ...] = ()
    body = b""
    truncated = False
    error: str | None = None
    response: Any = None  # http.client.HTTPResponse or urllib.error.HTTPError; both are file-like responses
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        response = err
    except (OSError, http.client.HTTPException) as err:  # URLError, timeouts and resets are OSError subclasses
        error = f"{type(err).__name__}: {err}"
    if response is not None:
        seconds_to_headers = time.perf_counter() - start
        with response:
            status = response.status if getattr(response, "status", None) is not None else response.code
            final_url = response.geturl()
            response_headers = tuple(response.headers.items())
            try:
                body = response.read(max_bytes + 1)
            except (OSError, http.client.HTTPException) as err:
                error = f"{type(err).__name__} while reading the body: {err}"
        if len(body) > max_bytes:
            body, truncated = body[:max_bytes], True
    seconds_total = time.perf_counter() - start
    return FetchResult(
        method=method,
        url=url,
        final_url=final_url,
        status=status,
        headers=response_headers,
        body=body,
        truncated=truncated,
        local_before_utc=before,
        local_after_utc=utc_now(),
        seconds_to_headers=seconds_to_headers,
        seconds_total=seconds_total,
        error=error,
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def skew_from_date_header(date_header: str | None, local_before: datetime, local_after: datetime) -> float | None:
    """Seconds by which the local clock leads the server: midpoint(local before, local after) - `Date` (1 s resolution).

    None when the header is missing, unparseable or carries no zone (RFC 5322 "-0000").
    """
    if not date_header:
        return None
    try:
        server = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if server.tzinfo is None:
        return None
    midpoint = local_before + (local_after - local_before) / 2
    return (midpoint - server).total_seconds()


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """Append one JSON object as a UTF-8 line (LF, keys sorted, non-ASCII kept)."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def write_json(path: Path, payload: Any) -> None:
    """Write `payload` as indented UTF-8 JSON, replacing the file atomically (a crash never leaves half a summary)."""
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
