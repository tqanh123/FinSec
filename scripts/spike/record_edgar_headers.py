"""Record the EDGAR filing headers of the golden test once, from www.sec.gov (plan 261022-1147, phase 4, R4.2).

Run from the repo root with the dev venv, after `.env` holds a real EDGAR_IDENTITY ("Name contact@domain"):
    .venv/Scripts/python scripts/spike/record_edgar_headers.py --check-env
    .venv/Scripts/python scripts/spike/record_edgar_headers.py
    .venv/Scripts/python scripts/spike/record_edgar_headers.py --cik <issuer CIK>
`--check-env` prints `ok` and sends nothing. A recording run needs FINSEC_DATA_ROOT (the environment, else `.env`).
Reaches the network; never collected by pytest.

Two files per accession: `<acc>.hdr.sgml` and `<acc>-index-headers.html`, 8 requests plus one per Form 4 folder tried.
Fair access (VERIFIED https://www.sec.gov/os/accessing-edgar-data): the declared User-Agent, `Accept-Encoding: gzip,
deflate` (decompressed here by hand) and 0.5 s between requests (2 req/s; the roadmap caps EDGAR at 5). The identity is
never printed, logged or written: the manifest says only `user_agent_declared: true`.

Fail-closed: every response is checked (HTTP 200, no "Undeclared Automated Tool" page, the requested accession, a
14-digit `<ACCEPTANCE-DATETIME>`; the raw and escaped copies of -index-headers.html agree) BEFORE anything is written
under `tests/`. The body of every response is first saved under `$FINSEC_DATA_ROOT/spike/edgar/<UTC-date>/<run-id>/`.
A `.hdr.sgml` without the tag is kept and recorded as `hdr_has_acceptance: false`. Fixtures are written all at once
into a new `tests/fixtures/edgar_headers/`; the script refuses to run when that directory holds any file and has no
force flag. Form 4 (decision D8): the reporting owner's name, street, city, state, ZIP and phone become `REDACTED` in
all three header dialects; its CIK and `<ACCEPTANCE-DATETIME>` stay untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from _spikelib import (
    MAX_BODY_BYTES,
    FetchResult,
    SpikeEnvError,
    append_jsonl,
    fetch,
    load_env_file,
    require_env,
    sha256_hex,
    spike_dir,
    utc_now,
)
from finsec_alpharadar.data_root import DATA_ROOT_ENV, DataRootError

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "edgar_headers"
MANIFEST_NAME = "manifest.json"

IDENTITY_ENV = "EDGAR_IDENTITY"
PLACEHOLDER = "yourdomain.com"  # .env.example:5
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
REQUEST_GAP_S = 0.5
UNDECLARED_MARKER = "Undeclared Automated Tool"  # the 403 page title (OBSERVED 2026-10-22, research edgar-header.md §4)
AGENT_CIK = "1140361"  # accession prefix of the Form 4: the filer agent, whose folder may not serve the filing
REDACTED = "REDACTED"

HDR = "hdr"
HTML = "html"


@dataclass(frozen=True)
class Filing:
    accession: str
    form_type: str
    cik: str | None  # None: try the agent folder, then --cik

    @property
    def redact(self) -> bool:
        return self.form_type == "4"


# Roadmap §2.1 (docs/project-roadmap.md:48-53). Apple's CIK 320193 is [PRIOR] plus the accession prefix.
FILINGS: tuple[Filing, ...] = (
    Filing("0000320193-26-000018", "8-K", "320193"),
    Filing("0001140361-26-040127", "4", None),
    Filing("0000320193-26-000020", "10-Q", "320193"),
    Filing("0000320193-26-000005", "8-K", "320193"),
)

Fetcher = Callable[[str, Mapping[str, str]], FetchResult]
Sleeper = Callable[[float], None]


class RecordError(RuntimeError):
    """A response or a fixture fails a check; nothing is written under `tests/`."""


class IssuerCikNeeded(RecordError):
    """The Form 4 is not served from the agent folder; rerun with `--cik <issuer CIK>`."""


# --- environment -----------------------------------------------------------------------------------------------


def check_identity(values: Mapping[str, str]) -> str:
    """The EDGAR_IDENTITY value, refused when empty, without `@`, still the placeholder, or not printable ASCII.

    Every error names the variable only, never its value.
    """
    identity = require_env(IDENTITY_ENV, values)
    if PLACEHOLDER in identity.lower():
        raise SpikeEnvError(f"{IDENTITY_ENV} still holds the .env.example placeholder domain")
    if "@" not in identity:
        raise SpikeEnvError(f"{IDENTITY_ENV} has no contact email (no '@'); SEC asks for 'Name contact@domain'")
    if not (identity.isascii() and identity.isprintable()):
        raise SpikeEnvError(f"{IDENTITY_ENV} must be printable ASCII to go into an HTTP header")
    return identity


def _data_root_env(values: Mapping[str, str]) -> dict[str, str]:
    """The process environment, with FINSEC_DATA_ROOT taken from `.env` only when the environment lacks it."""
    env = dict(os.environ)
    if not env.get(DATA_ROOT_ENV) and values.get(DATA_ROOT_ENV):
        env[DATA_ROOT_ENV] = values[DATA_ROOT_ENV]
    return env


# --- pure checks -----------------------------------------------------------------------------------------------


def file_name(accession: str, kind: str) -> str:
    return f"{accession}.hdr.sgml" if kind == HDR else f"{accession}-index-headers.html"


def file_url(cik: str, accession: str, kind: str) -> str:
    return f"{ARCHIVES}/{cik}/{accession.replace('-', '')}/{file_name(accession, kind)}"


def decode_body(body: bytes, content_encoding: str | None, *, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """The body after the `Content-Encoding` (gzip, deflate or none), bounded to `max_bytes` (fetched data is untrusted)."""
    encoding = (content_encoding or "identity").strip().lower()
    if encoding == "identity":
        return body
    if encoding == "gzip":
        wbits_options = (31,)
    elif encoding == "deflate":
        wbits_options = (15, -15)  # zlib-wrapped as RFC 9110 says, or the raw deflate some servers send
    else:
        raise RecordError(f"unsupported Content-Encoding {content_encoding!r}")
    last: zlib.error | None = None
    for wbits in wbits_options:
        inflater = zlib.decompressobj(wbits)
        try:
            data = inflater.decompress(body, max_bytes + 1)
        except zlib.error as err:
            last = err
            continue
        if len(data) > max_bytes or inflater.unconsumed_tail:
            raise RecordError(f"{encoding} body inflates beyond {max_bytes} bytes")
        if not inflater.eof:
            raise RecordError(f"{encoding} body is truncated")
        return data
    raise RecordError(f"{encoding} body does not decompress: {last}")


# Written apart from finsec_alpharadar.connectors.edgar_acceptance on purpose: the manifest value the golden test
# compares with must not come from the code under test.
_RAW_ACCEPTANCE = re.compile(r"<ACCEPTANCE-DATETIME>([^\r\n]*)")
_ESCAPED_ACCEPTANCE = re.compile(r"&lt;ACCEPTANCE-DATETIME&gt;([^\r\n]*)")
_ACCESSION = re.compile(
    r"(?:<|&lt;)ACCESSION-NUMBER(?:>|&gt;)[ \t]*([^\r\n]*)|^ACCESSION NUMBER:[ \t]*([^\r\n]*)", re.MULTILINE
)
_FOURTEEN_DIGITS = re.compile(r"[0-9]{14}")


def check_header(kind: str, accession: str, text: str) -> str | None:
    """The 14-digit acceptance value of one decoded header file, or None for a `.hdr.sgml` without the tag.

    Raises RecordError on the 403 page, a missing or different accession, a value that is not 14 digits, copies that
    disagree, or an -index-headers.html without both its raw and its escaped copy.
    """
    if UNDECLARED_MARKER in text:
        raise RecordError(f"{file_name(accession, kind)}: SEC answered with the '{UNDECLARED_MARKER}' page")
    found = {(raw or spaced).strip() for raw, spaced in _ACCESSION.findall(text)}
    if found != {accession}:
        raise RecordError(f"{file_name(accession, kind)}: accession numbers {sorted(found)!r}, expected only {accession}")
    raw_values = [value.strip() for value in _RAW_ACCEPTANCE.findall(text)]
    escaped_values = [value.strip() for value in _ESCAPED_ACCEPTANCE.findall(text)]
    if kind == HTML and not (raw_values and escaped_values):
        raise RecordError(
            f"{file_name(accession, kind)}: expected <ACCEPTANCE-DATETIME> in the HTML comment and escaped in <PRE>, "
            f"found {len(raw_values)} raw and {len(escaped_values)} escaped"
        )
    values = set(raw_values + escaped_values)
    if not values:
        return None
    if len(values) > 1:
        raise RecordError(f"{file_name(accession, kind)}: <ACCEPTANCE-DATETIME> copies disagree: {sorted(values)!r}")
    (value,) = values
    if not _FOURTEEN_DIGITS.fullmatch(value):
        raise RecordError(f"{file_name(accession, kind)}: <ACCEPTANCE-DATETIME> {value!r} is not 14 digits")
    return value


# --- Form 4 redaction (decision D8) -----------------------------------------------------------------------------

_TAG_FIELDS = "CONFORMED-NAME|FORMER-CONFORMED-NAME|STREET1|STREET2|CITY|STATE|ZIP|PHONE"
# The tag dialect, raw (`<X>`, the .hdr.sgml and the HTML comment) and escaped (`&lt;X&gt;`, inside <PRE>).
_TAG_BLOCKS = tuple(
    (
        re.compile(rf"{lt}REPORTING-OWNER{gt}.*?{lt}/REPORTING-OWNER{gt}", re.DOTALL),
        re.compile(rf"({lt}({_TAG_FIELDS}){gt}[ \t]*)([^\r\n]*)"),
    )
    for lt, gt in (("<", ">"), ("&lt;", "&gt;"))
)
# The "space" dialect inside <PRE>: from a column-0 `REPORTING-OWNER:` over every blank or indented line after it.
_SPACE_BLOCK = re.compile(r"^REPORTING-OWNER:[^\r\n]*(?:\r?\n(?:[ \t][^\r\n]*)?)*", re.MULTILINE)
_SPACE_LABELS = {
    "COMPANY CONFORMED NAME": "CONFORMED-NAME",
    "FORMER CONFORMED NAME": "FORMER-CONFORMED-NAME",
    "STREET 1": "STREET1",
    "STREET 2": "STREET2",
    "CITY": "CITY",
    "STATE": "STATE",
    "ZIP": "ZIP",
    "BUSINESS PHONE": "PHONE",
    "PHONE": "PHONE",
}
_SPACE_FIELD = re.compile(rf"^([ \t]+({'|'.join(_SPACE_LABELS)}):[ \t]*)([^\r\n]*)", re.MULTILINE)
_OWNER_OPENERS = re.compile(r"(?:<|&lt;)REPORTING-OWNER(?:>|&gt;)|^REPORTING-OWNER:", re.MULTILINE)
_NAME_FIELDS = frozenset({"CONFORMED-NAME", "FORMER-CONFORMED-NAME"})


def redact_reporting_owner(text: str) -> tuple[str, Counter[str]]:
    """`text` with the reporting owner's name, street, city, state, ZIP and phone replaced by REDACTED, and the counts.

    Deterministic and idempotent; only the REPORTING-OWNER blocks change (the issuer keeps its business address), line
    endings and every other byte stay as they were. Raises RecordError when a block is not closed or holds no name,
    or when a redacted name still appears elsewhere in the text.
    """
    counts: Counter[str] = Counter()
    names: set[str] = set()
    blocks = 0

    def redact_fields(block: str, field_pattern: re.Pattern[str], canonical: Callable[[str], str]) -> str:
        nonlocal blocks
        blocks += 1
        block_names = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal block_names
            field, value = canonical(match.group(2)), match.group(3).strip()
            if field in _NAME_FIELDS and value:
                block_names += 1
            if not value or value == REDACTED:
                return match.group(0)
            counts[field] += 1
            if field in _NAME_FIELDS:
                names.add(value)
            return match.group(1) + REDACTED

        result = field_pattern.sub(replace, block)
        if block_names == 0:
            raise RecordError("a REPORTING-OWNER block holds no owner name; the header layout is unknown, refusing")
        return result

    for block_pattern, field_pattern in _TAG_BLOCKS:
        text = block_pattern.sub(lambda match, fields=field_pattern: redact_fields(match.group(0), fields, str), text)
    text = _SPACE_BLOCK.sub(lambda match: redact_fields(match.group(0), _SPACE_FIELD, _SPACE_LABELS.__getitem__), text)

    openers = len(_OWNER_OPENERS.findall(text))
    if openers != blocks:
        raise RecordError(f"{openers} REPORTING-OWNER openers but {blocks} complete blocks; refusing a partial redaction")
    leftover = sorted(name for name in names if name in text)
    if leftover:
        raise RecordError(f"{len(leftover)} redacted owner name(s) still appear outside the REPORTING-OWNER blocks")
    return text, counts


# --- recording -------------------------------------------------------------------------------------------------


@dataclass
class _Requests:
    """Sends the requests 0.5 s apart and saves every response body under the raw directory before any check."""

    fetcher: Fetcher
    sleeper: Sleeper
    raw_dir: Path
    headers: Mapping[str, str]
    count: int = 0

    def get(self, url: str, name: str) -> tuple[FetchResult, bytes | None, str | None]:
        if self.count:
            self.sleeper(REQUEST_GAP_S)
        self.count += 1
        result = self.fetcher(url, self.headers)
        decoded: bytes | None = None
        decode_error: str | None = None
        try:
            decoded = decode_body(result.body, result.header("Content-Encoding"))
        except RecordError as err:
            decode_error = str(err)
        saved = f"{self.count:02d}-{name}" + ("" if decoded is not None else ".undecoded")
        (self.raw_dir / saved).write_bytes(decoded if decoded is not None else result.body)
        append_jsonl(
            self.raw_dir / "requests.jsonl",
            {
                "seq": self.count,
                "url": url,
                "status": result.status,
                "error": result.error or decode_error,
                "content_encoding": result.header("Content-Encoding"),
                "bytes_received": len(result.body),
                "truncated": result.truncated,
                "saved_as": saved,
                "fetched_at_utc": result.local_after_utc.isoformat(),
            },
        )
        return result, decoded, decode_error


def _checked_body(result: FetchResult, decoded: bytes | None, decode_error: str | None, name: str) -> bytes:
    if result.error:  # also a 200 whose body read failed: the partial body is never trusted
        raise RecordError(f"{name}: HTTP {result.status}, {result.error}")
    if result.status != 200:
        raise RecordError(f"{name}: HTTP {result.status}, expected 200")
    if result.truncated:
        raise RecordError(f"{name}: body truncated at {len(result.body)} bytes")
    if decoded is None:
        raise RecordError(f"{name}: {decode_error}")
    return decoded


def _fixture_targets(fixture_dir: Path) -> list[Path]:
    return [fixture_dir / file_name(f.accession, kind) for f in FILINGS for kind in (HDR, HTML)] + [
        fixture_dir / MANIFEST_NAME
    ]


def record(
    identity: str,
    *,
    fixture_dir: Path,
    raw_dir: Path,
    fetcher: Fetcher,
    sleeper: Sleeper,
    issuer_cik: str | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Fetch, check and redact all eight headers, then write them and the manifest into a new `fixture_dir`.

    Returns the manifest. Nothing is written to `fixture_dir` unless every file passes; an existing non-empty
    `fixture_dir` is refused before the first request.
    """
    if fixture_dir.exists() and any(fixture_dir.iterdir()):
        raise RecordError(f"{fixture_dir} already holds files; recorded fixtures are never overwritten")
    requests = _Requests(fetcher, sleeper, raw_dir, {"User-Agent": identity, "Accept-Encoding": "gzip, deflate"})
    outputs: dict[str, bytes] = {}
    entries: dict[str, dict[str, Any]] = {}

    # Form 4 first: when its agent folder answers 404 the run stops after one request.
    for filing in sorted(FILINGS, key=lambda f: f.cik is not None):
        folders = [filing.cik] if filing.cik else [AGENT_CIK] + ([issuer_cik] if issuer_cik else [])
        agent_status: int | None = None
        served_cik = folders[0]
        for kind in (HDR, HTML):
            name = file_name(filing.accession, kind)
            # The .hdr.sgml picks the folder; the -index-headers.html comes from the same one.
            for cik in folders if kind == HDR else [served_cik]:
                url = file_url(cik, filing.accession, kind)
                result, decoded, decode_error = requests.get(url, name)
                if filing.cik is None and cik == AGENT_CIK:
                    agent_status = result.status
                if result.status != 404:
                    break
                log(f"{name}: HTTP 404 under CIK folder {cik}")
            else:
                if kind == HDR and filing.cik is None and not issuer_cik:
                    raise IssuerCikNeeded(
                        f"{name}: HTTP 404 under the agent folder {AGENT_CIK}. Find the issuer CIK (EDGAR full-text "
                        f'search for "{filing.accession}", or ask the owner) and rerun with --cik <issuer CIK>'
                    )
            original = _checked_body(result, decoded, decode_error, name)  # a 404 left over is refused here
            served_cik = cik
            text = original.decode("latin-1")  # one byte per character: the redaction round-trips every other byte
            acceptance = check_header(kind, filing.accession, text)
            committed, redactions = original, Counter[str]()
            if filing.redact:
                redacted_text, redactions = redact_reporting_owner(text)
                if not redactions:
                    raise RecordError(f"{name}: a Form 4 header with nothing to redact; refusing")
                committed = redacted_text.encode("latin-1")
            entry: dict[str, Any] = {
                "accession": filing.accession,
                "form_type": filing.form_type,
                "file": name,
                "url": url,
                "cik_folder_used": cik,
                "fetched_at_utc": result.local_after_utc.isoformat().replace("+00:00", "Z"),
                "http_status": result.status,
                "bytes": len(committed),
                "bytes_original": len(original),
                "sha256_original": sha256_hex(original),
                "sha256_committed": sha256_hex(committed),
                "redactions": dict(sorted(redactions.items())),
                "user_agent_declared": True,
                "hdr_has_acceptance": acceptance is not None,
                "acceptance_raw": acceptance,
            }
            if filing.cik is None:
                entry["agent_cik_folder_status"] = agent_status
            outputs[name] = committed
            entries[name] = entry
            log(
                f"{name}: HTTP 200 from CIK folder {cik}, {len(committed)} bytes, acceptance "
                f"{acceptance or 'absent (tagless .hdr.sgml)'}, {sum(redactions.values())} redactions"
            )

    manifest: dict[str, Any] = {
        "plan": "261022-1147-phase0a-capture",
        "phase": 4,
        "recorded_by": "scripts/spike/record_edgar_headers.py",
        "files": [entries[target.name] for target in _fixture_targets(fixture_dir)[:-1]],
    }
    outputs[MANIFEST_NAME] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("ascii")
    _write_all(fixture_dir, outputs)
    return manifest


def _write_all(fixture_dir: Path, outputs: Mapping[str, bytes]) -> None:
    """Write every file into a staging directory, then move it into place: the fixtures land all at once or not at all."""
    staging = fixture_dir.with_name(f".{fixture_dir.name}.staging-{os.getpid()}")
    staging.mkdir(parents=True)
    try:
        for name, data in outputs.items():
            with (staging / name).open("xb") as handle:
                handle.write(data)
        if fixture_dir.exists():
            fixture_dir.rmdir()  # empty (checked before the first request); rmdir refuses a directory that filled since
        staging.rename(fixture_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


# --- command line ----------------------------------------------------------------------------------------------


def _cik(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,10}", value):
        raise argparse.ArgumentTypeError("a CIK is 1 to 10 digits")
    return str(int(value))  # EDGAR folders drop the leading zeros


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-env", action="store_true", help="check EDGAR_IDENTITY in .env, print ok, send nothing")
    parser.add_argument("--cik", type=_cik, help="issuer CIK folder for the Form 4 when the agent folder answers 404")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        values = load_env_file(ENV_FILE)
        identity = check_identity(values)
    except SpikeEnvError as err:
        print(f"record_edgar_headers: {err}", file=sys.stderr)
        return 2
    if args.check_env:
        print("ok")
        return 0
    try:
        raw_dir = spike_dir("edgar", env=_data_root_env(values)) / utc_now().strftime("%H%M%SZ")
        raw_dir.mkdir()
    except (DataRootError, SpikeEnvError, FileExistsError) as err:
        print(f"record_edgar_headers: {err}", file=sys.stderr)
        return 2
    try:
        record(
            identity,
            fixture_dir=FIXTURE_DIR,
            raw_dir=raw_dir,
            fetcher=lambda url, headers: fetch(url, headers=headers),
            sleeper=time.sleep,
            issuer_cik=args.cik,
        )
    except IssuerCikNeeded as err:
        print(f"record_edgar_headers: {err}; raw responses in {raw_dir}", file=sys.stderr)
        return 3
    except RecordError as err:
        print(f"record_edgar_headers: {err}; nothing written under tests/; raw responses in {raw_dir}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"record_edgar_headers: interrupted; nothing written under tests/; raw responses in {raw_dir}", file=sys.stderr)
        return 130
    print(f"record_edgar_headers: wrote 8 headers and {MANIFEST_NAME} to {FIXTURE_DIR}; raw responses in {raw_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
