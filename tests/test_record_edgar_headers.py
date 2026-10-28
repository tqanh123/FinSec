"""Pure checks of `scripts/spike/record_edgar_headers.py` (plan 261022-1147, phase 4, R4.1-R4.2).

Synthetic headers only: made-up people ("DOE JANE"), streets, phones and CIKs; the dialects follow the shapes recorded
by edgartools from sec.gov (research edgar-header.md §1.2). The fetcher and the sleeper are fakes: no network, no sleep,
no clock. The script is loaded by path because `scripts/` is not a package.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPIKE_DIR = REPO_ROOT / "scripts" / "spike"


def _load_recorder() -> ModuleType:
    if str(SPIKE_DIR) not in sys.path:
        sys.path.insert(0, str(SPIKE_DIR))  # the script imports its sibling `_spikelib`
    spec = importlib.util.spec_from_file_location("record_edgar_headers", SPIKE_DIR / "record_edgar_headers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()
FetchResult = recorder.FetchResult  # the script's own _spikelib types
SpikeEnvError = recorder.SpikeEnvError

IDENTITY = "Example Research jane.doe@example.org"  # synthetic
FIXED = datetime(2026, 10, 21, 20, 30, 28, tzinfo=timezone.utc)
FORM4 = "0001140361-26-040127"
APPLE = ("0000320193-26-000018", "0000320193-26-000020", "0000320193-26-000005")
ACCEPTANCE = {
    "0000320193-26-000018": "20260730163028",
    FORM4: "20261019183255",
    "0000320193-26-000020": "20261030060102",
    "0000320193-26-000005": "20260129163033",
}
OWNER_CIK = "0009999991"
ISSUER_CIK = "0009999992"


# --- synthetic headers -----------------------------------------------------------------------------------------


def _tag_header(accession: str, acceptance: str | None, *, owner: bool, lt: str = "<", gt: str = ">") -> str:
    lines = [f"{lt}SEC-HEADER{gt}{accession}.hdr.sgml : 20261021"]
    if acceptance is not None:
        lines.append(f"{lt}ACCEPTANCE-DATETIME{gt}{acceptance}")
    lines += [f"{lt}ACCESSION-NUMBER{gt}{accession}", f"{lt}TYPE{gt}{'4' if owner else '8-K'}"]
    if owner:
        lines += [
            f"{lt}REPORTING-OWNER{gt}",
            f"{lt}OWNER-DATA{gt}",
            f"{lt}CONFORMED-NAME{gt}DOE JANE",
            f"{lt}CIK{gt}{OWNER_CIK}",
            f"{lt}/OWNER-DATA{gt}",
            f"{lt}MAIL-ADDRESS{gt}",
            f"{lt}STREET1{gt}1 FAKE STREET",
            f"{lt}STREET2{gt}",
            f"{lt}CITY{gt}NOWHERESVILLE",
            f"{lt}STATE{gt}ZZ",
            f"{lt}ZIP{gt}00000",
            f"{lt}PHONE{gt}555-0100",
            f"{lt}/MAIL-ADDRESS{gt}",
            f"{lt}/REPORTING-OWNER{gt}",
        ]
    lines += [
        f"{lt}ISSUER{gt}" if owner else f"{lt}FILER{gt}",
        f"{lt}COMPANY-DATA{gt}",
        f"{lt}CONFORMED-NAME{gt}EXAMPLE WIDGETS INC",
        f"{lt}CIK{gt}{ISSUER_CIK}",
        f"{lt}/COMPANY-DATA{gt}",
        f"{lt}BUSINESS-ADDRESS{gt}",
        f"{lt}STREET1{gt}9 MARKET PLAZA",
        f"{lt}CITY{gt}SAMPLETOWN",
        f"{lt}STATE{gt}YY",
        f"{lt}PHONE{gt}555-0199",
        f"{lt}/BUSINESS-ADDRESS{gt}",
        f"{lt}/ISSUER{gt}" if owner else f"{lt}/FILER{gt}",
        f"{lt}/SEC-HEADER{gt}",
    ]
    return "\n".join(lines) + "\n"


def _space_block(accession: str, *, owner: bool) -> str:
    lines = [f"ACCESSION NUMBER:\t\t{accession}", f"CONFORMED SUBMISSION TYPE:\t{'4' if owner else '8-K'}", ""]
    if owner:
        lines += [
            "REPORTING-OWNER:\t",
            "",
            "\tOWNER DATA:\t",
            "\t\tCOMPANY CONFORMED NAME:\t\t\tDOE JANE",
            f"\t\tCENTRAL INDEX KEY:\t\t\t{OWNER_CIK}",
            "",
            "\tMAIL ADDRESS:\t",
            "\t\tSTREET 1:\t\t1 FAKE STREET",
            "\t\tSTREET 2:\t\t",
            "\t\tCITY:\t\t\tNOWHERESVILLE",
            "\t\tSTATE:\t\t\tZZ",
            "\t\tZIP:\t\t\t00000",
            "\t\tBUSINESS PHONE:\t\t555-0100",
            "",
        ]
    lines += [
        "ISSUER:\t" if owner else "FILER:",
        "",
        "\tCOMPANY DATA:\t",
        "\t\tCOMPANY CONFORMED NAME:\t\t\tEXAMPLE WIDGETS INC",
        f"\t\tCENTRAL INDEX KEY:\t\t\t{ISSUER_CIK}",
        "\t\tSTATE OF INCORPORATION:\t\t\tDE",
        "",
        "\tBUSINESS ADDRESS:\t",
        "\t\tSTREET 1:\t\t9 MARKET PLAZA",
        "\t\tCITY:\t\t\tSAMPLETOWN",
        "\t\tSTATE:\t\t\tYY",
        "\t\tBUSINESS PHONE:\t\t555-0199",
        "&lt;/SEC-HEADER&gt;",
    ]
    return "\n".join(lines) + "\n"


def _hdr(accession: str, acceptance: str | None = "", *, owner: bool = False) -> str:
    return _tag_header(accession, ACCEPTANCE[accession] if acceptance == "" else acceptance, owner=owner)


def _html(accession: str, acceptance: str = "", *, owner: bool = False, escaped_acceptance: str | None = "") -> str:
    value = ACCEPTANCE[accession] if acceptance == "" else acceptance
    pre_value = value if escaped_acceptance == "" else escaped_acceptance
    pre = f"<PRE>&lt;SEC-DOCUMENT&gt;{accession}-index.html : 20261021\n&lt;SEC-HEADER&gt;{accession}.hdr.sgml : 20261021\n"
    if pre_value is not None:
        pre += f"&lt;ACCEPTANCE-DATETIME&gt;{pre_value}\n"
    return (
        f"<HTML><HEAD><TITLE>SEC EDGAR Submission {accession}</TITLE>\n<!--\n"
        + _tag_header(accession, value, owner=owner)
        + "-->\n</HEAD><BODY>\n"
        + pre
        + _space_block(accession, owner=owner)
        + "</PRE></BODY></HTML>\n"
    )


# --- fake network ----------------------------------------------------------------------------------------------


def _result(url: str, status: int, body: bytes, encoding: str | None = None) -> FetchResult:
    headers = (("Content-Encoding", encoding),) if encoding else ()
    return FetchResult("GET", url, url, status, headers, body, False, FIXED, FIXED, 0.1, 0.2, None)


class FakeSec:
    """Serves synthetic headers by URL; the Form 4 lives under ISSUER_CIK only unless `agent_serves` is set."""

    def __init__(self, *, agent_serves: bool = False, overrides: Mapping[str, tuple[int, bytes]] | None = None) -> None:
        self.agent_serves = agent_serves
        self.overrides = dict(overrides or {})
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.sleeps: list[float] = []

    def files(self, cik: str) -> dict[str, bytes]:
        served: dict[str, bytes] = {}
        for accession in APPLE:
            if cik == "320193":
                served[f"{accession}.hdr.sgml"] = _hdr(accession).encode("ascii")
                served[f"{accession}-index-headers.html"] = _html(accession).encode("ascii")
        if cik == str(int(ISSUER_CIK)) or (cik == "1140361" and self.agent_serves):
            served[f"{FORM4}.hdr.sgml"] = _hdr(FORM4, owner=True).replace("\n", "\r\n").encode("ascii")  # CRLF on purpose
            served[f"{FORM4}-index-headers.html"] = _html(FORM4, owner=True).encode("ascii")
        return served

    def fetch(self, url: str, headers: Mapping[str, str]) -> FetchResult:
        self.calls.append((url, dict(headers)))
        if url in self.overrides:
            status, body = self.overrides[url]
            return _result(url, status, body)
        cik, _folder, name = url.removeprefix("https://www.sec.gov/Archives/edgar/data/").split("/")
        body = self.files(cik).get(name)
        if body is None:
            return _result(url, 404, b"<html>Not Found</html>")
        return _result(url, 200, gzip.compress(body, mtime=0), "gzip")


def _run(tmp_path: Path, sec: FakeSec, issuer_cik: str | None = None) -> tuple[Path, Path, dict[str, object]]:
    fixtures, raw = tmp_path / "tests" / "fixtures" / "edgar_headers", tmp_path / "raw"
    raw.mkdir()
    manifest = recorder.record(
        IDENTITY,
        fixture_dir=fixtures,
        raw_dir=raw,
        fetcher=sec.fetch,
        sleeper=sec.sleeps.append,
        issuer_cik=issuer_cik,
        log=lambda line: None,
    )
    return fixtures, raw, manifest


def _url(cik: str, accession: str, suffix: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}{suffix}"


# --- environment (R4.1) ------------------------------------------------------------------------------------------


def test_identity_with_a_contact_email_passes() -> None:
    assert recorder.check_identity({"EDGAR_IDENTITY": f"  {IDENTITY} "}) == IDENTITY


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("", "is not set"),
        ("Example Research", "no '@'"),
        ("FinSecAlphaRadar research@yourdomain.com", "placeholder"),
        ("Example research@YourDomain.COM", "placeholder"),
        ("Example Résearch jane@example.org", "printable ASCII"),
        ("Example\tResearch jane@example.org", "printable ASCII"),
    ],
)
def test_bad_identity_is_refused_and_the_error_names_only_the_variable(value: str, rule: str) -> None:
    with pytest.raises(SpikeEnvError) as excinfo:
        recorder.check_identity({"EDGAR_IDENTITY": value})
    message = str(excinfo.value)
    assert "EDGAR_IDENTITY" in message and rule in message
    assert not value.strip() or value.strip() not in message


def test_check_env_prints_exactly_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f'EDGAR_IDENTITY="{IDENTITY}"\n', encoding="utf-8")
    monkeypatch.setattr(recorder, "ENV_FILE", env_file)
    monkeypatch.delenv("FINSEC_DATA_ROOT", raising=False)  # --check-env needs no data root
    assert recorder.main(["--check-env"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "ok\n" and captured.err == ""


def test_check_env_refuses_the_placeholder_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('EDGAR_IDENTITY="FinSecAlphaRadar research@yourdomain.com"\n', encoding="utf-8")
    monkeypatch.setattr(recorder, "ENV_FILE", env_file)
    assert recorder.main(["--check-env"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "EDGAR_IDENTITY" in captured.err
    assert "research@" not in captured.err and "FinSecAlphaRadar" not in captured.err


# --- decoding ----------------------------------------------------------------------------------------------------


def test_bodies_are_decompressed_by_hand() -> None:
    data = b"<ACCEPTANCE-DATETIME>20261021163028\n"
    assert recorder.decode_body(gzip.compress(data), "gzip") == data
    assert recorder.decode_body(zlib.compress(data), "deflate") == data
    raw_deflate = zlib.compressobj(wbits=-15)
    assert recorder.decode_body(raw_deflate.compress(data) + raw_deflate.flush(), "deflate") == data
    assert recorder.decode_body(data, None) == data


def test_bad_or_oversized_bodies_are_refused() -> None:
    with pytest.raises(recorder.RecordError, match="unsupported"):
        recorder.decode_body(b"x", "br")
    with pytest.raises(recorder.RecordError, match="beyond"):
        recorder.decode_body(gzip.compress(b"0" * 1000), "gzip", max_bytes=100)
    with pytest.raises(recorder.RecordError, match="truncated"):
        recorder.decode_body(gzip.compress(b"0123456789" * 50)[:-12], "gzip")
    with pytest.raises(recorder.RecordError, match="does not decompress"):
        recorder.decode_body(b"not gzip", "gzip")


# --- header checks (fail-closed) -----------------------------------------------------------------------------------


def test_index_headers_html_needs_both_copies_and_they_must_agree() -> None:
    accession = APPLE[0]
    assert recorder.check_header("html", accession, _html(accession)) == "20260730163028"
    with pytest.raises(recorder.RecordError, match="0 escaped"):
        recorder.check_header("html", accession, _html(accession, escaped_acceptance=None))
    with pytest.raises(recorder.RecordError, match="disagree"):
        recorder.check_header("html", accession, _html(accession, escaped_acceptance="20260730163029"))


def test_tagless_hdr_sgml_is_recorded_not_refused() -> None:
    assert recorder.check_header("hdr", APPLE[0], _hdr(APPLE[0], None)) is None


@pytest.mark.parametrize("bad", ["2026073016302", "2026073016302X", "  "])  # "  ": a tag with an empty value
def test_acceptance_must_be_fourteen_digits(bad: str) -> None:
    with pytest.raises(recorder.RecordError, match="not 14 digits"):
        recorder.check_header("hdr", APPLE[0], _hdr(APPLE[0], bad))


def test_wrong_accession_and_the_403_page_are_refused() -> None:
    with pytest.raises(recorder.RecordError, match="expected only"):
        recorder.check_header("hdr", APPLE[1], _hdr(APPLE[0]))
    page = "<title>SEC.gov | Your Request Originates from an Undeclared Automated Tool</title>"
    with pytest.raises(recorder.RecordError, match="Undeclared Automated Tool"):
        recorder.check_header("html", APPLE[0], page)


# --- Form 4 redaction (D8) -----------------------------------------------------------------------------------------


def test_form4_redaction_covers_all_three_dialects_and_keeps_cik_and_acceptance() -> None:
    original = _html(FORM4, owner=True).replace("\n", "\r\n")  # CRLF must survive untouched
    redacted, counts = recorder.redact_reporting_owner(original)
    for personal in ("DOE JANE", "1 FAKE STREET", "NOWHERESVILLE", "00000", "555-0100", "ZZ"):
        assert personal not in redacted, personal
    # Raw tag block + space block = 2 copies of each field; the escaped copy is covered by the next test.
    assert dict(counts) == {"CITY": 2, "CONFORMED-NAME": 2, "PHONE": 2, "STATE": 2, "STREET1": 2, "ZIP": 2}
    assert redacted.count(OWNER_CIK) == original.count(OWNER_CIK) == 2
    assert "<ACCEPTANCE-DATETIME>20261019183255\r\n" in redacted
    assert "&lt;ACCEPTANCE-DATETIME&gt;20261019183255\r\n" in redacted
    for issuer in ("EXAMPLE WIDGETS INC", "9 MARKET PLAZA", "SAMPLETOWN", "555-0199", "STATE OF INCORPORATION:\t\t\tDE"):
        assert redacted.count(issuer) == original.count(issuer) > 0, issuer
    assert redacted.count("\r\n") == original.count("\r\n") and "\n" not in redacted.replace("\r\n", "")
    assert "\t\tSTREET 2:\t\t\r\n" in redacted  # an empty value stays empty and is not counted


def test_escaped_tag_block_is_redacted_and_redaction_is_idempotent() -> None:
    original = _tag_header(FORM4, "20261019183255", owner=True, lt="&lt;", gt="&gt;")
    redacted, counts = recorder.redact_reporting_owner(original)
    assert "DOE JANE" not in redacted and "&lt;CONFORMED-NAME&gt;REDACTED" in redacted
    assert sum(counts.values()) == 6
    again, more = recorder.redact_reporting_owner(redacted)
    assert again == redacted and not more


def test_unclosed_or_nameless_owner_blocks_are_refused() -> None:
    unclosed = _hdr(FORM4, owner=True).replace("</REPORTING-OWNER>", "")
    with pytest.raises(recorder.RecordError, match="openers"):
        recorder.redact_reporting_owner(unclosed)
    nameless = _hdr(FORM4, owner=True).replace("<CONFORMED-NAME>DOE JANE", "<ORGANIZATION-NAME>")
    with pytest.raises(recorder.RecordError, match="no owner name"):
        recorder.redact_reporting_owner(nameless)


def test_a_name_left_outside_the_block_is_refused() -> None:
    leaky = _hdr(FORM4, owner=True).replace("<TYPE>4", "<TYPE>4\n<REMARK>DOE JANE")
    with pytest.raises(recorder.RecordError, match="still appear"):
        recorder.redact_reporting_owner(leaky)


# --- the recording run -----------------------------------------------------------------------------------------------


def test_agent_folder_404_stops_after_one_request_and_asks_for_cik(tmp_path: Path) -> None:
    sec = FakeSec()
    with pytest.raises(recorder.IssuerCikNeeded, match="--cik"):
        _run(tmp_path, sec)
    assert [url for url, _ in sec.calls] == [_url("1140361", FORM4, ".hdr.sgml")]
    assert not (tmp_path / "tests").exists()
    assert (tmp_path / "raw" / "requests.jsonl").is_file()  # the 404 is kept as evidence


def test_full_run_writes_eight_headers_and_a_manifest_without_the_identity(tmp_path: Path) -> None:
    sec = FakeSec()
    fixtures, raw, manifest = _run(tmp_path, sec, issuer_cik=str(int(ISSUER_CIK)))
    names = sorted(path.name for path in fixtures.iterdir())
    assert len(names) == 9 and "manifest.json" in names
    assert len(sec.calls) == 9  # 8 files + the agent-folder 404
    assert sec.sleeps == [0.5] * 8
    assert all(headers == {"User-Agent": IDENTITY, "Accept-Encoding": "gzip, deflate"} for _, headers in sec.calls)

    manifest_text = (fixtures / "manifest.json").read_text(encoding="ascii")
    assert json.loads(manifest_text) == manifest
    every_written = "".join(path.read_bytes().decode("latin-1") for path in [*fixtures.iterdir(), *raw.iterdir()])
    assert "jane.doe@example.org" not in every_written and "Example Research" not in every_written
    for forbidden in ("utc_expected", "information_time", "user_agent\""):
        assert forbidden not in manifest_text

    entries = {entry["file"]: entry for entry in manifest["files"]}
    assert len(entries) == 8
    for name, entry in entries.items():
        data = (fixtures / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256_committed"] and len(data) == entry["bytes"]
        assert entry["user_agent_declared"] is True and entry["http_status"] == 200
        assert entry["fetched_at_utc"] == "2026-10-21T20:30:28Z"
        assert entry["acceptance_raw"] == ACCEPTANCE[entry["accession"]] and entry["hdr_has_acceptance"] is True
    apple = entries[f"{APPLE[0]}.hdr.sgml"]
    assert apple["sha256_original"] == apple["sha256_committed"] and apple["redactions"] == {}
    assert apple["cik_folder_used"] == "320193" and "agent_cik_folder_status" not in apple
    assert (fixtures / f"{APPLE[0]}.hdr.sgml").read_bytes() == _hdr(APPLE[0]).encode("ascii")  # byte-exact
    form4 = entries[f"{FORM4}.hdr.sgml"]
    assert form4["cik_folder_used"] == str(int(ISSUER_CIK)) and form4["agent_cik_folder_status"] == 404
    assert form4["sha256_original"] != form4["sha256_committed"] and form4["redactions"]["CONFORMED-NAME"] == 1
    form4_bytes = (fixtures / f"{FORM4}.hdr.sgml").read_bytes()
    assert b"DOE JANE" not in form4_bytes and OWNER_CIK.encode() in form4_bytes and b"\r\n" in form4_bytes
    assert entries[f"{FORM4}-index-headers.html"]["redactions"]["CONFORMED-NAME"] == 2


def test_agent_folder_that_serves_the_form4_is_used(tmp_path: Path) -> None:
    _, _, manifest = _run(tmp_path, FakeSec(agent_serves=True))
    form4 = next(entry for entry in manifest["files"] if entry["accession"] == FORM4)
    assert form4["cik_folder_used"] == "1140361" and form4["agent_cik_folder_status"] == 200


def test_tagless_hdr_sgml_lands_in_the_manifest(tmp_path: Path) -> None:
    accession = APPLE[1]
    sec = FakeSec(overrides={_url("320193", accession, ".hdr.sgml"): (200, _hdr(accession, None).encode("ascii"))})
    _, _, manifest = _run(tmp_path, sec, issuer_cik=str(int(ISSUER_CIK)))
    entry = next(e for e in manifest["files"] if e["file"] == f"{accession}.hdr.sgml")
    assert entry["hdr_has_acceptance"] is False and entry["acceptance_raw"] is None


def test_one_bad_response_writes_nothing_under_tests(tmp_path: Path) -> None:
    page = b"<title>SEC.gov | Your Request Originates from an Undeclared Automated Tool</title>"
    sec = FakeSec(overrides={_url("320193", APPLE[2], "-index-headers.html"): (403, page)})
    with pytest.raises(recorder.RecordError, match="HTTP 403"):
        _run(tmp_path, sec, issuer_cik=str(int(ISSUER_CIK)))
    assert not (tmp_path / "tests").exists()  # neither fixtures nor a staging directory


def test_a_200_whose_body_read_failed_is_refused(tmp_path: Path) -> None:
    url = _url("320193", APPLE[0], ".hdr.sgml")

    class BrokenRead(FakeSec):
        def fetch(self, url_: str, headers: Mapping[str, str]) -> FetchResult:
            result = super().fetch(url_, headers)
            if url_ != url:
                return result
            body = _hdr(APPLE[0]).encode("ascii")  # a header that would pass every content check
            return FetchResult("GET", url_, url_, 200, (), body, False, FIXED, FIXED, 0.1, 0.2, "ConnectionResetError: reset")

    with pytest.raises(recorder.RecordError, match="ConnectionResetError"):
        _run(tmp_path, BrokenRead(), issuer_cik=str(int(ISSUER_CIK)))
    assert not (tmp_path / "tests").exists()


def test_existing_fixtures_are_never_overwritten(tmp_path: Path) -> None:
    fixtures = tmp_path / "tests" / "fixtures" / "edgar_headers"
    fixtures.mkdir(parents=True)
    (fixtures / "manifest.json").write_text("{}\n", encoding="ascii")
    sec = FakeSec()
    with pytest.raises(recorder.RecordError, match="never overwritten"):
        _run(tmp_path, sec, issuer_cik=str(int(ISSUER_CIK)))
    assert sec.calls == []  # refused before the first request
    assert (fixtures / "manifest.json").read_text(encoding="ascii") == "{}\n"
    assert not hasattr(recorder.parse_args([]), "force")


def test_cik_argument_is_digits_only() -> None:
    assert recorder.parse_args(["--cik", "0000320193"]).cik == "320193"
    with pytest.raises(SystemExit):
        recorder.parse_args(["--cik", "../320193"])
