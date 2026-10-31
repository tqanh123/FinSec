"""Pure checks of `scripts/spike/vn_counts.py` (plan 261022-1147, phase 6, R6.2).

Synthetic rows only: made-up tickers (AAA, BBB, CCC), made-up ids and titles. The vendor call, the status runner, the
sleeper and the clocks are fakes: no network, no sleep, no real clock, and vnstock is never imported. The script is
loaded by path WITHOUT adding `scripts/spike/` to `sys.path`: it must be self-contained (it runs alone under `python -I`
from the probe kit).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import unicodedata
import warnings
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "spike" / "vn_counts.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vn_counts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


vc = _load()

FIXED_NOW = datetime(2026, 10, 28, 15, 0, 0, tzinfo=UTC)  # 22:00 ICT, synthetic
SECRET_TITLE = "Synthetic title ZZQX do not leak"
SECRET_NAME = "Nguyen Van Synthetic"


def _row(row_id: str, symbol: str, code: str, category: str, public_date: str | None, **extra: Any) -> dict[str, Any]:
    row = {
        "id": row_id,
        "symbol": symbol,
        "event_code": code,
        "category": category,
        "public_date": public_date,
        "record_date": None,
        "event_title_vi": f"{SECRET_TITLE} {row_id}",
        "event_name_vi": SECRET_NAME,
    }
    row.update(extra)
    return row


def _status_entry(command: str, stdout: str) -> dict[str, Any]:
    return {"command": f"vnstock {command}", "exit_code": 0, "seconds": 0.0, "stdout": stdout, "stderr": ""}


def _doctor(used_24h: str = "0%", used_30d: str = "0%") -> str:
    return f"✔ Khoá API: hợp lệ\n✔ Hạn mức: 24 giờ qua đã dùng {used_24h}, 30 ngày qua {used_30d}\nKhông thấy lỗi nào.\n"


class FakeStatus:
    """Doctor text per call, in order; the last one repeats. Records every subcommand asked."""

    def __init__(self, doctors: list[str] | None = None, token: str = "") -> None:
        self.doctors = doctors or [_doctor()]
        self.calls: list[str] = []
        self.token = token
        self._doctor_index = 0

    def __call__(self, subcommand: str) -> dict[str, Any]:
        self.calls.append(subcommand)
        if subcommand == "doctor":
            text = self.doctors[min(self._doctor_index, len(self.doctors) - 1)]
            self._doctor_index += 1
            return _status_entry(subcommand, text)
        return _status_entry(subcommand, f"Tài khoản: cấp Cộng đồng\nkey {self.token}\n")


class FakeEvents:
    """`call_events(symbol) -> (rows, attrs)`; a symbol mapped to an exception raises it, to a callable runs it."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self.asked.append(symbol)
        answer = self.answers[symbol]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer()
        return answer, {"source": "vci"}


class VendorError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class FakeClock:
    def __init__(self, step: float = 0.0) -> None:
        self.value = 1000.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def _run(
    tmp_path: Path,
    symbols: list[str],
    call_events: Callable[[str], Any],
    *,
    status: FakeStatus | None = None,
    rate: float = 60.0,
    clock: FakeClock | None = None,
    status_every: int = 0,
) -> tuple[int, Path, list[float], FakeStatus]:
    sleeps: list[float] = []
    status = status or FakeStatus()
    out = tmp_path / "counts"
    code = vc.run_events(
        symbols,
        out,
        rate_per_min=rate,
        call_events=call_events,
        status_runner=status,
        sleeper=sleeps.append,
        monotonic=clock or FakeClock(),
        now=lambda: FIXED_NOW,
        status_every=status_every,
    )
    return code, out, sleeps, status


def _summary(out: Path) -> dict[str, Any]:
    return json.loads((out / "counts-summary.json").read_text(encoding="utf-8"))


# --- import hygiene ---------------------------------------------------------------------------------------------


def test_importing_the_script_loads_neither_vnstock_nor_finsec_alpharadar() -> None:
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('vn_counts', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['vn_counts'] = module\n"
        "spec.loader.exec_module(module)\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('vnstock', 'pandas', 'finsec_alpharadar'))\n"
        "print('BAD=' + ','.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert "BAD=\n" in result.stdout or result.stdout.strip() == "BAD=", result.stdout


def test_script_help_runs_under_isolated_mode() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "events" in result.stdout and "universe" in result.stdout


def test_main_refuses_without_isolated_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    symbols = tmp_path / "u.txt"
    symbols.write_text("AAA\n", encoding="utf-8")
    code = vc.main(["events", "--symbols-file", str(symbols), "--rate", "30", "--out", str(tmp_path / "o")])
    assert code == 2
    assert "python -I" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


# --- event groups -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "category", "group"),
    [
        ("DDINS", "MAJOR_SHAREHOLDER_TRADING", "INSIDER"),
        ("DDIND", "MAJOR_SHAREHOLDER_TRADING", "INSIDER"),
        ("DDRP", "MAJOR_SHAREHOLDER_TRADING", "INSIDER"),
        ("DDXYZ", "SOMETHING_NEW", "INSIDER"),  # the DD* prefix rule
        ("DIV", "DIVIDEND", "DIVIDEND"),
        ("ISS", "DIVIDEND", "DIVIDEND"),
        ("AGME", "SHAREHOLDER_MEETING", "DHDCD"),
        ("AGMR", "SHAREHOLDER_MEETING", "DHDCD"),
        ("EGME", "SHAREHOLDER_MEETING", "DHDCD"),
        ("AIS", "OTHER", "OTHER"),
        ("OTHE", "OTHER", "OTHER"),
        ("NLIS", "OTHER", "OTHER"),
        ("NEWCODE", "SHAREHOLDER_MEETING", "DHDCD"),  # category fallback
        ("NEWCODE", "DIVIDEND", "DIVIDEND"),
        ("NEWCODE", "MAJOR_SHAREHOLDER_TRADING", "INSIDER"),
        ("NEWCODE", "UNHEARD_OF", "OTHER"),
        (None, None, "OTHER"),
    ],
)
def test_event_group_mapping(code: str | None, category: str | None, group: str) -> None:
    assert vc.group_of(code, category) == group


def _titled(code: str | None, category: str | None, title: str | None, name: str | None = None) -> dict[str, Any]:
    return {"event_code": code, "category": category, "event_title_vi": title, "event_name_vi": name}


@pytest.mark.parametrize(
    ("row", "group"),
    [
        # OTHER by code -> the title decides (logic F1: vci has no BCTC / NQ HDQT code).
        (_titled("OTHE", "OTHER", "Nghị quyết HĐQT về việc A"), "NQ_HDQT"),
        (_titled("OTHE", "OTHER", "NGHỊ QUYẾT HỘI ĐỒNG QUẢN TRỊ ngày 1"), "NQ_HDQT"),
        (_titled("AIS", "OTHER", "Báo cáo tài chính quý 3/2026"), "BCTC"),
        (_titled("NEWCODE", "UNHEARD_OF", "Giải trình BCTC  hợp nhất"), "BCTC"),
        (_titled(None, None, None, "Nghị quyết HĐQT"), "NQ_HDQT"),  # event_name_vi is read after event_title_vi
        (_titled("OTHE", "OTHER", "Thông báo khác", "Báo cáo tài chính"), "BCTC"),
        # Precedence INSIDER > NQ_HDQT > BCTC, as vn_reconcile.classify_title.
        (_titled("OTHE", "OTHER", "Nghị quyết HĐQT thông qua báo cáo tài chính"), "NQ_HDQT"),
        (_titled("OTHE", "OTHER", "Người nội bộ đăng ký giao dịch, nghị quyết HĐQT"), "INSIDER"),
        # Decomposed (NFD) Vietnamese still matches after NFC.
        (_titled("OTHE", "OTHER", unicodedata.normalize("NFD", "Nghị quyết HĐQT")), "NQ_HDQT"),
        # DIVIDEND and DHDCD also fall back (parity with vn_reconcile.vnstock_row_type); no keyword keeps the code group.
        (_titled("DIV", "DIVIDEND", "Nghị quyết HĐQT về việc chi trả cổ tức"), "NQ_HDQT"),
        (_titled("DIV", "DIVIDEND", "Chi trả cổ tức đợt 1"), "DIVIDEND"),
        (_titled("AGME", "SHAREHOLDER_MEETING", "Nghị quyết ĐHĐCĐ thường niên"), "DHDCD"),
        (_titled("OTHE", "OTHER", "Thông báo khác"), "OTHER"),
        # INSIDER by code or by category wins over any title.
        (_titled("DDINS", "MAJOR_SHAREHOLDER_TRADING", "Báo cáo tài chính"), "INSIDER"),
        (_titled("DIV", "MAJOR_SHAREHOLDER_TRADING", "Nghị quyết HĐQT"), "INSIDER"),
        (_titled("OTHE", "OTHER", 12345), "OTHER"),  # a non-string title is ignored
    ],
)
def test_row_group_falls_back_to_the_title_keywords(row: dict[str, Any], group: str) -> None:
    assert vc.group_of_row(row) == group


def test_both_scripts_type_the_same_rows_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """vn_counts (run under -I, cannot import its sibling) carries a copy of vn_reconcile's title rules: same result."""
    spike_dir = SCRIPT.parent
    monkeypatch.syspath_prepend(str(spike_dir))  # vn_reconcile imports its sibling `_spikelib`; this test only
    spec = importlib.util.spec_from_file_location("vn_reconcile_parity", spike_dir / "vn_reconcile.py")
    assert spec is not None and spec.loader is not None
    vr = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, vr)
    spec.loader.exec_module(vr)

    assert vc.TITLE_KEYWORDS == vr.TITLE_KEYWORDS
    assert vc.TITLE_PRIORITY == vr.TYPE_PRIORITY
    titles = [
        None, "", "Thông báo khác", "Báo cáo tài chính quý 3", "bctc hợp nhất", "BCTC", "Nghị quyết HĐQT về việc A",
        "nghị quyết hội đồng quản trị", "Nghị quyết  HĐQT", unicodedata.normalize("NFD", "Nghị quyết HĐQT"),
        "Đăng ký giao dịch cổ phiếu", "Người nội bộ", "Nghị quyết HĐQT thông qua BCTC",
        "Người nội bộ đăng ký giao dịch theo nghị quyết HĐQT", "Nghị quyết ĐHĐCĐ", "Chi trả cổ tức",
    ]
    codes = [("OTHE", "OTHER"), ("AIS", "OTHER"), ("DIV", "DIVIDEND"), ("AGME", "SHAREHOLDER_MEETING"),
             ("DDINS", "MAJOR_SHAREHOLDER_TRADING"), ("DDXYZ", "SOMETHING_NEW"), ("DIV", "MAJOR_SHAREHOLDER_TRADING"),
             ("NEWCODE", "UNHEARD_OF"), (None, None)]
    checked = 0
    for code, category in codes:
        for title in titles:
            for name in (None, "Báo cáo tài chính", "Thông báo"):
                row = _titled(code, category, title, name)
                reconcile_type = vr.vnstock_row_type(row)
                counts_group = vc.group_of_row(row)
                if reconcile_type is None:
                    assert counts_group == vc.group_of(code, category), row
                    assert counts_group not in ("BCTC", "NQ_HDQT", "INSIDER"), row
                else:
                    assert counts_group == reconcile_type, row
                checked += 1
    assert checked == len(codes) * len(titles) * 3


def test_aggregate_counts_title_typed_bctc_and_nq_hdqt_and_reports_the_retyped_codes() -> None:
    rows = {
        "AAA": [
            {**_titled("OTHE", "OTHER", "Nghị quyết HĐQT về việc A"), "public_date": "2026-09-15T17:00:00.000Z"},
            {**_titled("OTHE", "OTHER", "Báo cáo tài chính quý 2"), "public_date": "2026-07-29T17:00:00.000Z"},
            {**_titled("OTHE", "OTHER", "Thông báo khác"), "public_date": "2026-07-29T17:00:00.000Z"},
            {**_titled("DIV", "DIVIDEND", "Nghị quyết HĐQT về cổ tức"), "public_date": "2026-05-01T17:00:00.000Z"},
        ],
    }
    result = vc.aggregate(rows)
    groups = {entry["group"]: entry for entry in result["by_group"]}
    assert groups["NQ_HDQT"]["n_events"] == 2 and groups["BCTC"]["n_events"] == 1
    assert groups["OTHER"]["n_events"] == 1 and groups["DIVIDEND"]["n_events"] == 0
    assert {(e["group"], e["year"]) for e in result["by_group_year"]} >= {("NQ_HDQT", 2026), ("BCTC", 2026)}
    assert result["pools"]["BCTC+NQ_HDQT"]["n_events"] == 3
    # codes_seen keeps the code/category map group; the title retyping is reported per (code, category, group).
    codes = {(entry["event_code"], entry["category"]): entry for entry in result["codes_seen"]}
    assert codes[("OTHE", "OTHER")]["group"] == "OTHER" and codes[("OTHE", "OTHER")]["n_docs"] == 3
    assert result["codes_retyped_by_title"] == [
        {"event_code": "DIV", "category": "DIVIDEND", "code_group": "DIVIDEND", "group": "NQ_HDQT", "n_docs": 1},
        {"event_code": "OTHE", "category": "OTHER", "code_group": "OTHER", "group": "BCTC", "n_docs": 1},
        {"event_code": "OTHE", "category": "OTHER", "code_group": "OTHER", "group": "NQ_HDQT", "n_docs": 1},
    ]
    text = json.dumps(result, ensure_ascii=False)
    assert "Nghị quyết" not in text and "Báo cáo" not in text and "Thông báo" not in text  # counts only


def test_group_table_names_every_group_and_says_bctc_and_nq_hdqt_have_no_code() -> None:
    table = vc.group_map_table()
    assert set(vc.GROUPS) == {"BCTC", "NQ_HDQT", "DHDCD", "DIVIDEND", "INSIDER", "OTHER"}
    assert table["default"] == "OTHER"
    assert "BCTC" in table["note"] and "NQ_HDQT" in table["note"]
    assert not any(group in ("BCTC", "NQ_HDQT") for group in table["by_code"].values())


def test_group_table_states_the_title_fallback_rules() -> None:
    table = vc.group_map_table()
    fallback = table["title_fallback"]
    assert fallback["priority"] == ["INSIDER", "NQ_HDQT", "BCTC"]
    assert fallback["keywords"] == {kind: list(words) for kind, words in vc.TITLE_KEYWORDS.items()}
    assert fallback["columns"] == ["event_title_vi", "event_name_vi"]
    assert set(fallback["applies_to_code_groups"]) == {"OTHER", "DIVIDEND", "DHDCD"}
    assert "vn_reconcile" in fallback["note"]


# --- time basis -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-15T17:00:00.000Z", (date(2026, 9, 16), False)),  # ICT midnight as pandas to_json writes it
        ("2026-09-16T02:30:00.000Z", (date(2026, 9, 16), True)),  # 09:30 ICT
        ("2026-09-16T00:00:00+07:00", (date(2026, 9, 16), False)),
        ("2026-09-16T16:45:00+07:00", (date(2026, 9, 16), True)),
        ("2026-09-16", (date(2026, 9, 16), False)),
        ("2026-09-16 10:15:00", (date(2026, 9, 16), True)),  # naive -> read as ICT
        ("2025-12-31T17:00:00.000Z", (date(2026, 1, 1), False)),  # lands in year 2026
        ("not a date", None),
        ("", None),
        (None, None),
        (1726506000000, None),
    ],
)
def test_public_date_is_read_in_ict(value: Any, expected: tuple[date, bool] | None) -> None:
    assert vc.parse_public_date(value) == expected


def test_raw_dates_get_an_explicit_ict_offset() -> None:
    row = {"public_date": "2026-09-15T17:00:00.000Z", "record_date": None, "exercise_ratio": 0.1, "id": "x1",
           "event_title_vi": "2026-09-15T17:00:00.000Z is a title"}
    converted = vc.ict_row(row)
    assert converted["public_date"] == "2026-09-16T00:00:00+07:00"
    assert converted["record_date"] is None and converted["exercise_ratio"] == 0.1 and converted["id"] == "x1"
    assert converted["event_title_vi"] == row["event_title_vi"]  # only whole-value UTC timestamps are converted
    assert vc.ict_row({"d": "2026-09-16T02:30:00Z"})["d"] == "2026-09-16T09:30:00+07:00"


# --- aggregation ------------------------------------------------------------------------------------------------

GROUP_YEAR_COLUMNS = {
    "group",
    "year",
    "n_docs",
    "n_events",
    "n_issuers",
    "n_distinct_dates",
    "max_events_per_date",
    "share_with_intraday_time",
    "n_events_with_intraday_time",
    "n_distinct_dates_with_intraday_time",
}


def _sample_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "AAA": [
            # One event, two docs (same symbol, code and ICT date; one carries a time of day).
            _row("a1", "AAA", "DIV", "DIVIDEND", "2026-03-01T17:00:00.000Z"),
            _row("a2", "AAA", "DIV", "DIVIDEND", "2026-03-02T03:00:00.000Z"),
            # Same date, other code: a second event that date.
            _row("a3", "AAA", "ISS", "DIVIDEND", "2026-03-01T17:00:00.000Z"),
            _row("a4", "AAA", "AGME", "SHAREHOLDER_MEETING", "2025-04-10T17:00:00.000Z"),
            _row("a5", "AAA", "DDINS", "MAJOR_SHAREHOLDER_TRADING", None),
        ],
        "BBB": [
            _row("b1", "BBB", "DIV", "DIVIDEND", "2026-03-01T17:00:00.000Z"),
            _row("b2", "BBB", "DIV", "DIVIDEND", "2025-12-31T17:00:00.000Z"),  # ICT 2026-01-01
            _row("b3", "BBB", "DIV", "DIVIDEND", "garbage"),
        ],
    }


def test_aggregation_keys_and_columns() -> None:
    result = vc.aggregate(_sample_rows())
    rows = {(entry["group"], entry["year"]): entry for entry in result["by_group_year"]}
    assert set(rows) == {("DIVIDEND", 2026), ("DHDCD", 2025)}
    for entry in result["by_group_year"]:
        assert set(entry) == GROUP_YEAR_COLUMNS
    dividend = rows[("DIVIDEND", 2026)]
    # docs: a1 a2 a3 b1 b2 (b3 unparsed, excluded from year rows)
    assert dividend["n_docs"] == 5
    # events: (AAA,DIV,03-02) (AAA,ISS,03-02) (BBB,DIV,03-02) (BBB,DIV,01-01)
    assert dividend["n_events"] == 4
    assert dividend["n_issuers"] == 2
    assert dividend["n_distinct_dates"] == 2
    assert dividend["max_events_per_date"] == 3
    # only (AAA,DIV,2026-03-02) has a doc with a time of day
    assert dividend["n_events_with_intraday_time"] == 1
    assert dividend["share_with_intraday_time"] == pytest.approx(0.25)
    assert dividend["n_distinct_dates_with_intraday_time"] == 1
    meeting = rows[("DHDCD", 2025)]
    assert (meeting["n_docs"], meeting["n_events"], meeting["n_issuers"], meeting["n_distinct_dates"]) == (1, 1, 1, 1)
    assert meeting["share_with_intraday_time"] == 0.0


def test_group_totals_pools_and_dates() -> None:
    result = vc.aggregate(_sample_rows())
    groups = {entry["group"]: entry for entry in result["by_group"]}
    assert set(groups) == set(vc.GROUPS)
    assert groups["DIVIDEND"]["n_events"] == 4
    assert groups["DIVIDEND"]["n_docs_without_public_date"] == 1  # b3 unparsed
    assert groups["INSIDER"]["n_docs"] == 0 and groups["INSIDER"]["n_docs_without_public_date"] == 1
    assert groups["DIVIDEND"]["oldest_date"] == "2026-01-01" and groups["DIVIDEND"]["newest_date"] == "2026-03-02"
    assert groups["BCTC"]["n_events"] == 0 and groups["BCTC"]["oldest_date"] is None
    pools = result["pools"]
    assert pools["ALL"]["n_events"] == 5
    assert pools["ALL"]["n_distinct_dates"] == 3  # union over groups: 2025-04-11, 2026-01-01, 2026-03-02
    assert pools["BCTC+NQ_HDQT"]["n_events"] == 0
    assert result["oldest_date"] == "2025-04-11" and result["newest_date"] == "2026-03-02"
    codes = {(entry["event_code"], entry["category"]): entry for entry in result["codes_seen"]}
    assert codes[("DIV", "DIVIDEND")] == {"event_code": "DIV", "category": "DIVIDEND", "group": "DIVIDEND", "n_docs": 5}
    assert codes[("DDINS", "MAJOR_SHAREHOLDER_TRADING")]["group"] == "INSIDER"


# --- quota ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "remaining"),
    [
        (_doctor("0%", "0%"), 100.0),
        (_doctor("<1%", "<1%"), 99.0),
        (_doctor("75%", "10%"), 25.0),
        (_doctor("12%", "71%"), 29.0),
        (_doctor("12,5%", "3%"), 87.5),
        ("Tài khoản: cấp Cộng đồng\n", None),
        ("", None),
    ],
)
def test_quota_remaining_is_read_from_the_doctor_line(text: str, remaining: float | None) -> None:
    assert vc.quota_remaining_pct(text) == remaining


# --- run_events -------------------------------------------------------------------------------------------------


def test_events_writes_raw_lists_status_files_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = _sample_rows()
    fake = FakeEvents({"AAA": rows["AAA"], "BBB": rows["BBB"]})
    token = "A" * 30
    code, out, _, status = _run(tmp_path, ["AAA", "BBB"], fake, status=FakeStatus(token=token))
    assert code == 0
    assert fake.asked == ["AAA", "BBB"]
    raw = json.loads((out / "raw" / "AAA.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 5 and all(isinstance(entry, dict) for entry in raw)
    assert raw[0]["public_date"] == "2026-03-02T00:00:00+07:00"
    before = (out / "vnstock-status-before.txt").read_text(encoding="utf-8")
    after = (out / "vnstock-status-after.txt").read_text(encoding="utf-8")
    assert token not in before and token not in after and "<redacted-token>" in before
    assert "Hạn mức" in before and "Hạn mức" in after
    assert status.calls[:3] == ["status", "env", "doctor"] and status.calls[-3:] == ["status", "env", "doctor"]
    summary = _summary(out)
    assert summary["kind"] == "vn_counts_summary"  # the shape vn_reconcile.py go-no-go reads
    assert "ASSUMED" in summary["quota"]["source"]  # the doctor quota-line wording was never stored
    assert summary["stop"] is None
    assert summary["symbols"]["requested"] == 2 and summary["symbols"]["ok"] == 2
    assert summary["symbols"]["failed"] == 0 and summary["symbols"]["skipped"] == 0
    assert summary["universe"]["size"] == 2
    assert "point-in-time" in summary["universe"]["note"]
    assert summary["quota"]["before_remaining_pct"] == 100.0 and summary["quota"]["after_remaining_pct"] == 100.0
    assert summary["calls_made"] == 2
    assert "Asia/Ho_Chi_Minh" in summary["time_basis"]
    assert summary["median_lag_vs_portal_min"] is None
    assert {entry["group"] for entry in summary["by_group"]} == set(vc.GROUPS)
    assert summary["group_map"]["default"] == "OTHER"
    out_text = capsys.readouterr().out
    assert out_text.isascii()


def test_counts_summary_and_stdout_carry_no_row_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = _sample_rows()
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB"], FakeEvents({"AAA": rows["AAA"], "BBB": rows["BBB"]}))
    assert code == 0
    text = (out / "counts-summary.json").read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for secret in (SECRET_TITLE, SECRET_NAME, "Synthetic"):
        assert secret not in text and secret not in captured.out and secret not in captured.err
    for row_value in ("a1", "b2", "garbage", "2026-03-02T00:00:00+07:00"):  # row ids and raw values stay in raw/
        assert f'"{row_value}"' not in text


def test_rate_limit_goes_through_the_injected_sleeper(tmp_path: Path) -> None:
    empty: dict[str, Any] = {"AAA": [], "BBB": [], "CCC": []}
    code, _, sleeps, _ = _run(tmp_path / "frozen", ["AAA", "BBB", "CCC"], FakeEvents(empty), rate=30.0)
    assert code == 0
    assert sleeps == [pytest.approx(2.0), pytest.approx(2.0)]
    code, _, sleeps, _ = _run(tmp_path / "moving", ["AAA", "BBB", "CCC"], FakeEvents(empty), rate=30.0,
                              clock=FakeClock(step=0.5))
    assert code == 0
    assert sleeps == [pytest.approx(1.5), pytest.approx(1.5)]


@pytest.mark.parametrize("rate", [0.0, -1.0, 61.0, float("nan"), float("inf")])
def test_rate_outside_the_safe_range_is_refused(tmp_path: Path, rate: float) -> None:
    with pytest.raises(ValueError, match="rate"):
        _run(tmp_path, ["AAA"], FakeEvents({"AAA": []}), rate=rate)
    assert not (tmp_path / "counts").exists()


def test_quota_below_threshold_before_the_run_makes_no_call(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": [], "BBB": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB"], fake, status=FakeStatus([_doctor("71%", "0%")]))
    assert code == 1
    assert fake.asked == []
    summary = _summary(out)
    assert summary["stop"]["reason"] == "quota_below_threshold"
    assert summary["stop"]["remaining_pct"] == 29.0
    assert summary["symbols"]["skipped"] == 2 and summary["calls_made"] == 0
    assert (out / "vnstock-status-after.txt").is_file()


def test_periodic_quota_check_stops_the_run(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": [], "BBB": [], "CCC": []})
    status = FakeStatus([_doctor("0%"), _doctor("80%"), _doctor("80%")])
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake, status=status, status_every=2)
    assert code == 1
    assert fake.asked == ["AAA", "BBB"]
    summary = _summary(out)
    assert summary["stop"]["reason"] == "quota_below_threshold"
    assert summary["symbols"]["ok"] == 2 and summary["symbols"]["skipped"] == 1
    assert summary["quota"]["checks"] == [{"after_calls": 2, "remaining_pct": 20.0}]


def test_unreadable_quota_is_noted_and_the_run_goes_on(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": []})
    code, out, _, _ = _run(tmp_path, ["AAA"], fake, status=FakeStatus(["no quota line here\n"]))
    assert code == 0
    summary = _summary(out)
    assert summary["quota"]["before_remaining_pct"] is None
    assert [note.split(":")[0] for note in summary["quota"]["notes"]] == ["quota_unreadable"]


def test_quota_warning_stops_and_keeps_the_rows(tmp_path: Path) -> None:
    def warned() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        warnings.warn("Kết quả bị cắt do vượt hạn mức", UserWarning, stacklevel=1)
        return [_row("b1", "BBB", "DIV", "DIVIDEND", "2026-03-01T17:00:00.000Z")], {}

    fake = FakeEvents({"AAA": [], "BBB": warned, "CCC": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 1
    assert fake.asked == ["AAA", "BBB"]
    assert (out / "raw" / "BBB.json").is_file()
    summary = _summary(out)
    assert summary["stop"]["reason"] == "quota_warning" and summary["stop"]["symbol"] == "BBB"
    assert summary["stop"]["type"] == "UserWarning"
    assert summary["symbols"]["skipped"] == 1
    assert {entry["group"]: entry["n_events"] for entry in summary["by_group"]}["DIVIDEND"] == 1
    assert "cắt" not in json.dumps(summary, ensure_ascii=False)


def test_quota_note_in_attrs_stops(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": lambda: ([], {"note": "result truncated by quota"}), "BBB": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB"], fake)
    assert code == 1
    assert _summary(out)["stop"]["reason"] == "quota_warning"
    assert _summary(out)["stop"]["type"] == "attrs"


def test_update_notice_warning_does_not_stop(tmp_path: Path) -> None:
    def notice() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        warnings.warn("Có bản mới 5.2.0, vui lòng cập nhật phiên bản", UserWarning, stacklevel=1)
        return [], {}

    code, out, _, _ = _run(tmp_path, ["AAA", "BBB"], FakeEvents({"AAA": notice, "BBB": []}))
    assert code == 0
    assert _summary(out)["warnings_seen"] == {"UserWarning": 1}


def test_vendor_error_stops_and_writes_why(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": [], "BBB": VendorError("release expired", "VNSTOCK_RELEASE_EXPIRED"), "CCC": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 1
    assert fake.asked == ["AAA", "BBB"]
    summary = _summary(out)
    assert summary["stop"] == {
        "reason": "vendor_error",
        "symbol": "BBB",
        "type": "VendorError",
        "code": "VNSTOCK_RELEASE_EXPIRED",
        "remaining_pct": None,
        # The message itself is never kept: only its fingerprint and length (data-safety F1).
        "message_sha256": hashlib.sha256(b"release expired").hexdigest(),
        "message_length": len("release expired"),
    }
    assert summary["symbols"]["ok"] == 1 and summary["symbols"]["skipped"] == 1
    assert (out / "vnstock-status-after.txt").is_file()


class Flaky:
    """Raise each error in order, then return `rows` (a callable answer for FakeEvents)."""

    def __init__(self, errors: list[Exception], rows: list[dict[str, Any]] | None = None) -> None:
        self.errors = list(errors)
        self.rows = rows if rows is not None else []

    def __call__(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.errors:
            raise self.errors.pop(0)
        return self.rows, {"source": "vci"}


def _unavailable() -> VendorError:
    return VendorError("source down", "VNSTOCK_SOURCE_UNAVAILABLE")


def test_transient_source_error_is_retried_then_the_run_goes_on(tmp_path: Path) -> None:
    # OBSERVED 2026-10-28: one NetworkError VNSTOCK_SOURCE_UNAVAILABLE stopped a 143-symbol run after 30 symbols.
    fake = FakeEvents({"AAA": [], "BBB": Flaky([_unavailable()]), "CCC": []})
    code, out, sleeps, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 0
    assert fake.asked == ["AAA", "BBB", "BBB", "CCC"]
    assert vc.TRANSIENT_BACKOFF_S[0] in sleeps
    summary = _summary(out)
    assert summary["stop"] is None and summary["symbols"]["ok"] == 3
    assert summary["calls_made"] == 4
    assert summary["retries"] == [
        {"symbol": "BBB", "attempt": 1, "type": "VendorError", "code": "VNSTOCK_SOURCE_UNAVAILABLE"}
    ]


def test_transient_source_error_that_persists_stops_after_the_last_retry(tmp_path: Path) -> None:
    attempts = 1 + len(vc.TRANSIENT_BACKOFF_S)
    fake = FakeEvents({"AAA": [], "BBB": Flaky([_unavailable() for _ in range(attempts)]), "CCC": []})
    code, out, sleeps, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 1
    assert fake.asked == ["AAA"] + ["BBB"] * attempts
    assert [s for s in sleeps if s in vc.TRANSIENT_BACKOFF_S] == list(vc.TRANSIENT_BACKOFF_S)
    summary = _summary(out)
    assert summary["stop"]["reason"] == "vendor_error" and summary["stop"]["code"] == "VNSTOCK_SOURCE_UNAVAILABLE"
    assert [entry["attempt"] for entry in summary["retries"]] == list(range(1, attempts))


def test_skipped_counts_symbols_never_asked_not_retry_calls(tmp_path: Path) -> None:
    # A retried symbol makes 3 calls but is asked once: 4 symbols, AAA ok, BBB stops after its retries, 2 never asked.
    attempts = 1 + len(vc.TRANSIENT_BACKOFF_S)
    fake = FakeEvents({"AAA": [], "BBB": Flaky([_unavailable() for _ in range(attempts)]), "CCC": [], "DDD": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC", "DDD"], fake)
    assert code == 1
    summary = _summary(out)
    assert summary["calls_made"] == 1 + attempts
    assert summary["symbols"]["skipped"] == 2


def test_a_non_transient_vendor_error_is_not_retried(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": VendorError("expired", "VNSTOCK_RELEASE_EXPIRED"), "BBB": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB"], fake)
    assert code == 1 and fake.asked == ["AAA"]
    assert _summary(out)["retries"] == []


ERR_TITLE = "ERRSENT Nghị quyết sentinel title QWXZ"
ERR_NAME = "Pham Errsentinel Name"


def _all_written_text(out: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(out.rglob("*")) if path.is_file())


@pytest.mark.parametrize(
    "error",
    [
        VendorError(f"bad row: '{ERR_TITLE}; {ERR_NAME}'", "VNSTOCK_KBS_COMPANY_SHAPE"),
        ValueError(f"could not convert string to float: '{ERR_TITLE} {ERR_NAME}'"),
    ],
)
def test_vendor_error_text_never_reaches_the_console_or_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    fake = FakeEvents({"AAA": [], "BBB": error, "CCC": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 1
    captured = capsys.readouterr()
    written = _all_written_text(out)
    for secret in (ERR_TITLE, ERR_NAME, "ERRSENT", "Errsentinel", "bad row", "could not convert"):
        assert secret not in captured.err, captured.err
        assert secret not in captured.out
        assert secret not in written
    fingerprint = hashlib.sha256(str(error).encode("utf-8")).hexdigest()
    assert fingerprint in captured.err and type(error).__name__ in captured.err
    stop = _summary(out)["stop"]
    assert stop["message_sha256"] == fingerprint and stop["message_length"] == len(str(error))


def _bctc_rows(symbol: str, count: int) -> list[dict[str, Any]]:
    months = ("01-29", "04-28", "07-29", "10-28")
    return [
        {"id": f"{symbol}{i}", "event_code": "OTHE", "category": "OTHER", "event_title_vi": "Báo cáo tài chính quý",
         "public_date": f"2026-{months[i % 4]}T10:00:00+07:00"}
        for i in range(count)
    ]


def test_summary_states_the_symbols_fetched_and_the_stop_and_sanity_uses_symbols_ok(tmp_path: Path) -> None:
    # AAA is fetched (4 BCTC in 2026), BBB stops the run, CCC is never asked: 1 symbol fetched out of 3 (spec-math F1).
    fake = FakeEvents({"AAA": _bctc_rows("AAA", 4), "BBB": VendorError("expired", "VNSTOCK_RELEASE_EXPIRED"),
                       "CCC": _bctc_rows("CCC", 4)})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 1
    summary = _summary(out)
    assert summary["universe"]["size"] == 3 and summary["symbols"]["ok"] == 1  # existing keys kept
    assert summary["coverage"] == {
        "symbols_requested": 3,
        "symbols_fetched": 1,
        "complete": False,
        "stop_reason": "vendor_error",
        "scale_by": "symbols.ok",
        "note": summary["coverage"]["note"],
    }
    assert "symbols.ok" in summary["coverage"]["note"] and "universe.size" in summary["coverage"]["note"]
    assert summary["sanity"]["bctc_events_per_issuer_year"] == pytest.approx(4.0)  # 4 events / 1 fetched / 1 year
    assert summary["sanity"]["denominator"] == "symbols.ok"


def test_summary_coverage_of_a_complete_run(tmp_path: Path) -> None:
    fake = FakeEvents({"AAA": _bctc_rows("AAA", 4), "BBB": []})
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB"], fake)
    assert code == 0
    coverage = _summary(out)["coverage"]
    assert (coverage["symbols_fetched"], coverage["complete"], coverage["stop_reason"]) == (2, True, None)
    assert _summary(out)["sanity"]["bctc_events_per_issuer_year"] == pytest.approx(2.0)  # empty BBB counts as 0 rows


def test_sanity_is_null_when_nothing_was_fetched(tmp_path: Path) -> None:
    code, out, _, _ = _run(tmp_path, ["AAA"], FakeEvents({"AAA": []}), status=FakeStatus([_doctor("71%")]))
    assert code == 1
    summary = _summary(out)
    assert summary["coverage"]["symbols_fetched"] == 0 and summary["coverage"]["stop_reason"] == "quota_below_threshold"
    assert summary["sanity"]["bctc_events_per_issuer_year"] is None


def test_import_failure_stops_as_vendor_unavailable(tmp_path: Path) -> None:
    code, out, _, _ = _run(tmp_path, ["AAA"], FakeEvents({"AAA": ImportError("No module named 'vnstock'")}))
    assert code == 1
    assert _summary(out)["stop"]["reason"] == "vendor_unavailable"


def test_unknown_symbol_is_counted_not_fatal(tmp_path: Path) -> None:
    fake = FakeEvents(
        {"AAA": VendorError("unknown", "VNSTOCK_INPUT_SYMBOL_UNKNOWN"), "BBB": [], "CCC": _sample_rows()["BBB"]}
    )
    code, out, _, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake)
    assert code == 0
    assert fake.asked == ["AAA", "BBB", "CCC"]
    symbols = _summary(out)["symbols"]
    assert symbols["failed"] == 1 and symbols["empty"] == 1 and symbols["ok"] == 2
    assert symbols["failed_list"] == [{"symbol": "AAA", "type": "VendorError", "code": "VNSTOCK_INPUT_SYMBOL_UNKNOWN"}]
    assert not (out / "raw" / "AAA.json").exists() and (out / "raw" / "BBB.json").is_file()


class FakeClient:
    """Stands in for `VnstockClient` in the CLI wiring tests (vnstock is never imported)."""

    def __init__(self) -> None:
        self.version = "0.0-synthetic"

    def events(self, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return [_row(f"{symbol.lower()}1", symbol, "DIV", "DIVIDEND", "2026-03-01T17:00:00.000Z")], {}

    def index_members(self, group: str) -> list[str]:
        return ["AAA"] if group == "VN100" else ["BBB"]


def test_cli_handlers_wire_the_injected_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every clock and the sleeper are injected fakes (brief rule 8): no real clock, no real sleep (data-safety F3).
    sleeps: list[float] = []
    monkeypatch.setattr(vc, "VnstockClient", FakeClient)
    monkeypatch.setattr(vc, "run_vnstock_cli", lambda subcommand: _status_entry(subcommand, _doctor()))
    extra = tmp_path / "extra.txt"
    extra.write_text("CCC\n", encoding="utf-8")
    universe = tmp_path / "u.txt"
    universe_args = vc.parse_args(["universe", "--out", str(universe), "--extra-symbols-file", str(extra)])
    assert vc.cmd_universe(universe_args, now=lambda: FIXED_NOW) == 0
    assert universe.read_text(encoding="utf-8").splitlines() == ["AAA", "BBB", "CCC"]
    meta = json.loads(universe.with_name(universe.name + ".json").read_text(encoding="utf-8"))
    assert meta["created_at_utc"] == FIXED_NOW.isoformat()
    out = tmp_path / "counts"
    args = vc.parse_args(["events", "--symbols-file", str(universe), "--rate", "60", "--out", str(out),
                          "--status-every", "0"])
    assert vc.cmd_events(args, sleeper=sleeps.append, monotonic=FakeClock(), now=lambda: FIXED_NOW) == 0
    summary = _summary(out)
    assert summary["library_version"] == "0.0-synthetic"
    assert summary["universe"]["size"] == 3 and len(summary["universe"]["symbols_file_sha256"]) == 64
    assert summary["symbols"]["ok"] == 3 and summary["calls_made"] == 3
    assert sorted(path.name for path in (out / "raw").iterdir()) == ["AAA.json", "BBB.json", "CCC.json"]
    assert sleeps == [1.0, 1.0]  # frozen fake clock at --rate 60: the full 1 s interval, deterministically
    assert summary["started_at_utc"] == FIXED_NOW.isoformat() and summary["finished_at_utc"] == FIXED_NOW.isoformat()


def test_cli_handlers_default_to_the_real_clock_and_sleeper() -> None:
    import inspect
    import time as real_time

    events = inspect.signature(vc.cmd_events).parameters
    assert events["sleeper"].default is real_time.sleep
    assert events["monotonic"].default is real_time.monotonic
    assert events["now"].default is vc.utc_now
    assert inspect.signature(vc.cmd_universe).parameters["now"].default is vc.utc_now


# --- work-tree guard (data-safety F2) ---------------------------------------------------------------------------


def _clone(tmp_path: Path, git_entry: str) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    if git_entry == "dir":
        (clone / ".git").mkdir()
    else:  # a worktree or submodule checkout has a `.git` FILE pointing at the real git dir
        (clone / ".git").write_text("gitdir: ../elsewhere/.git/worktrees/x\n", encoding="utf-8")
    return clone


@pytest.mark.parametrize("git_entry", ["dir", "file"])
def test_events_refuses_an_out_inside_a_git_work_tree(tmp_path: Path, git_entry: str) -> None:
    clone = _clone(tmp_path, git_entry)
    out = clone / "nested" / "counts"
    fake = FakeEvents({"AAA": []})
    status = FakeStatus()
    with pytest.raises(vc.WorkTreeRefused):
        vc.run_events(["AAA"], out, rate_per_min=60.0, call_events=fake, status_runner=status,
                      sleeper=lambda s: None, monotonic=FakeClock(), now=lambda: FIXED_NOW, status_every=0)
    assert fake.asked == [] and status.calls == []
    assert not (clone / "nested").exists()


@pytest.mark.parametrize("git_entry", ["dir", "file"])
def test_universe_refuses_an_out_inside_a_git_work_tree(tmp_path: Path, git_entry: str) -> None:
    clone = _clone(tmp_path, git_entry)
    out = clone / "u.txt"
    asked: list[str] = []
    with pytest.raises(vc.WorkTreeRefused):
        vc.run_universe(out, groups=("VN100",), members=lambda g: asked.append(g) or ["AAA"], extra_symbols=[],
                        now=lambda: FIXED_NOW)
    assert asked == []
    assert not out.exists() and not out.with_name("u.txt.json").exists()


@pytest.mark.parametrize("command", ["events", "universe"])
def test_main_exits_4_for_an_out_inside_a_git_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    monkeypatch.setattr(vc, "is_isolated", lambda: True)
    monkeypatch.setattr(vc, "VnstockClient", FakeClient)  # never the real vendor, even if the guard were missing
    monkeypatch.setattr(vc, "run_vnstock_cli", lambda subcommand: _status_entry(subcommand, _doctor()))
    monkeypatch.setattr(vc.time, "sleep", lambda seconds: None)
    clone = _clone(tmp_path, "dir")
    symbols = tmp_path / "symbols.txt"
    symbols.write_text("AAA\n", encoding="utf-8")
    if command == "events":
        argv = ["events", "--symbols-file", str(symbols), "--rate", "60", "--out", str(clone / "counts")]
    else:
        argv = ["universe", "--out", str(clone / "u.txt")]
    assert vc.main(argv) == 4
    assert "git work tree" in capsys.readouterr().err
    assert sorted(path.name for path in clone.iterdir()) == [".git"]


def test_out_outside_a_work_tree_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "clone" / ".git").mkdir(parents=True)
    vc.refuse_inside_work_tree(tmp_path / "sibling" / "counts")  # a sibling of the clone is fine
    with pytest.raises(vc.WorkTreeRefused):
        vc.refuse_inside_work_tree(tmp_path / "clone")  # the clone root itself is refused


def test_existing_evidence_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "counts" / "raw").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        _run(tmp_path, ["AAA"], FakeEvents({"AAA": []}))


# --- symbols files and universe ---------------------------------------------------------------------------------


def test_symbols_file_is_validated(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text("﻿# universe\naaa\nBBB\n\nAAA\n  ccc1  \n", encoding="utf-8")
    assert vc.read_symbols_file(good) == ["AAA", "BBB", "CCC1"]
    bad = tmp_path / "bad.txt"
    bad.write_text("AAA\nnot a symbol\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        vc.read_symbols_file(bad)
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no symbol"):
        vc.read_symbols_file(empty)


def test_universe_writes_members_plus_extras_and_says_what_failed(tmp_path: Path) -> None:
    def members(group: str) -> list[str]:
        if group == "HNX30":
            raise VendorError("no such group", "VNSTOCK_INPUT_GROUP_UNKNOWN")
        return ["AAA", "bbb", "AAA", "bad symbol!"]

    out = tmp_path / "universe.txt"
    code = vc.run_universe(out, groups=("VN100", "HNX30"), members=members, extra_symbols=["CCC", "BBB"],
                           now=lambda: FIXED_NOW)
    assert code == 1  # a group failed; what was obtained is still written
    assert out.read_text(encoding="utf-8").splitlines() == ["AAA", "BBB", "CCC"]
    meta = json.loads(out.with_name(out.name + ".json").read_text(encoding="utf-8"))
    assert meta["size"] == 3
    assert meta["groups"]["VN100"] == {"status": "ok", "count": 2, "dropped_invalid": 1}
    assert meta["groups"]["HNX30"]["status"] == "error" and meta["groups"]["HNX30"]["code"] == "VNSTOCK_INPUT_GROUP_UNKNOWN"
    assert meta["extra_added"] == 1
    assert "point-in-time" in meta["note"]
    assert vc.read_symbols_file(out) == ["AAA", "BBB", "CCC"]


def test_universe_all_groups_ok(tmp_path: Path) -> None:
    out = tmp_path / "u.txt"
    code = vc.run_universe(out, groups=("VN100", "HNX30"), members=lambda g: ["AAA"] if g == "VN100" else ["BBB"],
                           extra_symbols=[], now=lambda: FIXED_NOW)
    assert code == 0
    assert out.read_text(encoding="utf-8").splitlines() == ["AAA", "BBB"]


def test_universe_with_nothing_obtained_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "u.txt"

    def members(group: str) -> list[str]:
        raise VendorError("down", "X")

    code = vc.run_universe(out, groups=("VN100",), members=members, extra_symbols=[], now=lambda: FIXED_NOW)
    assert code == 1
    assert not out.exists()


# --- merge -------------------------------------------------------------------------------------------------------


def _merge_answers() -> dict[str, Any]:
    return {
        "AAA": _bctc_rows("AAA", 4) + [_row("a1", "AAA", "DDINS", "MAJOR_SHAREHOLDER_TRADING", "2025-03-04T09:15:00+07:00")],
        "BBB": [_row("b1", "BBB", "AGME", "SHAREHOLDER_MEETING", "2024-04-20T00:00:00+07:00"),
                _row("b2", "BBB", "DIV", "DIVIDEND", "2024-06-01T00:00:00+07:00")],
        "CCC": [_row("c1", "CCC", "DDIND", "MAJOR_SHAREHOLDER_TRADING", "2025-03-04T14:00:00+07:00"),
                _row("c2", "CCC", "AIS", "OTHER", "2023-01-05T10:00:00+07:00")],
    }


def _universe(tmp_path: Path, symbols: list[str]) -> Path:
    path = tmp_path / "universe.txt"
    path.write_text("".join(f"{symbol}\n" for symbol in symbols), encoding="utf-8")
    return path


def _two_parts(tmp_path: Path) -> tuple[Path, Path]:
    answers = _merge_answers()
    _, first, _, _ = _run(tmp_path / "p1", ["AAA", "BBB"], FakeEvents(answers))
    _, second, _, _ = _run(tmp_path / "p2", ["CCC"], FakeEvents(answers))
    return first, second


def _edit_summary(out: Path, **changes: Any) -> None:
    summary = _summary(out)
    summary.update(changes)
    (out / "counts-summary.json").write_text(json.dumps(summary), encoding="utf-8")


COUNT_KEYS = ("by_group", "by_group_year", "pools", "codes_seen", "codes_retyped_by_title", "oldest_date", "newest_date",
              "sanity")


def test_merge_of_disjoint_runs_equals_one_run_over_all_symbols(tmp_path: Path) -> None:
    _, whole, _, _ = _run(tmp_path / "whole", ["AAA", "BBB", "CCC"], FakeEvents(_merge_answers()))
    first, second = _two_parts(tmp_path)
    merged_dir = tmp_path / "merged"
    code = vc.run_merge([first, second], _universe(tmp_path, ["AAA", "BBB", "CCC"]), merged_dir)
    assert code == 0
    merged, one = _summary(merged_dir), _summary(whole)
    for key in COUNT_KEYS:
        assert merged[key] == one[key], key
    assert merged["kind"] == "vn_counts_summary" and merged["schema_version"] == 1
    assert merged["symbols"]["ok"] == 3 and merged["symbols"]["skipped"] == 0
    assert merged["coverage"]["complete"] is True and merged["coverage"]["symbols_fetched"] == 3
    assert merged["stop"] is None
    assert merged["calls_made"] == 3
    assert merged["isolated_mode"] is False  # every input ran under pytest, not `python -I`: the AND of the inputs
    assert sorted(path.name for path in (merged_dir / "raw").iterdir()) == ["AAA.json", "BBB.json", "CCC.json"]
    assert (merged_dir / "raw" / "AAA.json").read_bytes() == (first / "raw" / "AAA.json").read_bytes()
    assert [entry["symbols_ok"] for entry in merged["merged_from"]] == [2, 1]
    assert all(entry["counts_summary_sha256"] for entry in merged["merged_from"])


def test_merge_keeps_the_stops_and_retries_of_its_inputs(tmp_path: Path) -> None:
    answers = _merge_answers()
    answers["BBB"] = Flaky([_unavailable() for _ in range(1 + len(vc.TRANSIENT_BACKOFF_S))], answers["BBB"])
    _, first, _, _ = _run(tmp_path / "p1", ["AAA", "BBB", "CCC"], FakeEvents(answers))  # stops at BBB
    _, second, _, _ = _run(tmp_path / "p2", ["BBB", "CCC"], FakeEvents(_merge_answers()))
    merged_dir = tmp_path / "merged"
    assert vc.run_merge([first, second], _universe(tmp_path, ["AAA", "BBB", "CCC"]), merged_dir) == 0
    merged = _summary(merged_dir)
    assert merged["stop"] is None and merged["coverage"]["complete"] is True
    assert merged["merged_from"][0]["stop"]["code"] == "VNSTOCK_SOURCE_UNAVAILABLE"
    assert len(merged["retries"]) == len(vc.TRANSIENT_BACKOFF_S)


def test_merge_with_a_universe_symbol_never_fetched_is_incomplete(tmp_path: Path) -> None:
    first, second = _two_parts(tmp_path)
    merged_dir = tmp_path / "merged"
    code = vc.run_merge([first, second], _universe(tmp_path, ["AAA", "BBB", "CCC", "DDD"]), merged_dir)
    assert code == 1
    merged = _summary(merged_dir)
    assert merged["coverage"]["complete"] is False and merged["coverage"]["symbols_requested"] == 4
    assert merged["symbols"]["skipped"] == 1
    assert merged["stop"]["reason"] == "incomplete_merge"


@pytest.mark.parametrize(
    "case",
    ["overlap", "outside_universe", "library_version", "missing_raw", "wrong_kind"],
)
def test_merge_refuses_inputs_it_cannot_combine(tmp_path: Path, case: str) -> None:
    first, second = _two_parts(tmp_path)
    universe = ["AAA", "BBB", "CCC"]
    inputs = [first, second]
    if case == "overlap":
        _, third, _, _ = _run(tmp_path / "p3", ["CCC"], FakeEvents(_merge_answers()))
        inputs.append(third)
    elif case == "outside_universe":
        universe = ["AAA", "BBB"]
    elif case == "library_version":
        _edit_summary(first, library_version="5.1.0")
        _edit_summary(second, library_version="5.2.1")
    elif case == "missing_raw":
        (first / "raw" / "BBB.json").unlink()
    elif case == "wrong_kind":
        _edit_summary(second, kind="vn_counts_universe")
    merged_dir = tmp_path / "merged"
    with pytest.raises(ValueError):
        vc.run_merge(inputs, _universe(tmp_path, universe), merged_dir)
    assert not merged_dir.exists()


def test_merge_refuses_an_existing_or_in_tree_output(tmp_path: Path) -> None:
    first, second = _two_parts(tmp_path)
    universe = _universe(tmp_path, ["AAA", "BBB", "CCC"])
    with pytest.raises(FileExistsError):
        vc.run_merge([first, second], universe, first)
    (tmp_path / "tree" / ".git").mkdir(parents=True)
    with pytest.raises(vc.WorkTreeRefused):
        vc.run_merge([first, second], universe, tmp_path / "tree" / "merged")


def test_merge_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _two_parts(tmp_path)
    universe = _universe(tmp_path, ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(vc, "is_isolated", lambda: True)
    monkeypatch.setattr(vc, "VnstockClient", None)  # merge never needs the vendor
    code = vc.main(["merge", "--inputs", str(first), str(second), "--universe-file", str(universe),
                    "--out", str(tmp_path / "merged")])
    assert code == 0
    assert _summary(tmp_path / "merged")["universe"]["symbols_file_sha256"] == vc.sha256_file(universe)


# --- review fixes (2026-10-30) -------------------------------------------------------------------------------------


def test_rate_limit_counts_from_the_last_attempt_and_bounds_the_retry_wait(tmp_path: Path) -> None:
    # rate 1/min = 60 s apart. The clock never moves, so every wait is the full interval. A retry waits
    # max(backoff, interval), and the next symbol waits a full interval after the LAST attempt of the previous one.
    fake = FakeEvents({"AAA": [], "BBB": Flaky([_unavailable()]), "CCC": []})
    code, _, sleeps, _ = _run(tmp_path, ["AAA", "BBB", "CCC"], fake, rate=1.0)
    assert code == 0
    assert sleeps == [60.0, max(vc.TRANSIENT_BACKOFF_S[0], 60.0), 60.0]


def test_quota_check_is_not_skipped_when_retries_cross_its_turn(tmp_path: Path) -> None:
    attempts = 1 + len(vc.TRANSIENT_BACKOFF_S)
    rows_after = Flaky([_unavailable() for _ in range(attempts - 1)])  # succeeds on the last attempt
    fake = FakeEvents({"S0": [], "S1": rows_after, "S2": [], "S3": []})
    code, out, _, _ = _run(tmp_path, ["S0", "S1", "S2", "S3"], fake, status_every=3)
    assert code == 0
    # S0 = call 1, S1 = calls 2-4: the check due after 3 calls runs before S2 (after 4), not after 6.
    assert [entry["after_calls"] for entry in _summary(out)["quota"]["checks"]] == [1 + attempts]


def test_merge_counts_a_symbol_that_failed_twice_once(tmp_path: Path) -> None:
    unknown = lambda: VendorError("unknown", "VNSTOCK_INPUT_SYMBOL_UNKNOWN")  # noqa: E731
    answers = _merge_answers()
    _, first, _, _ = _run(tmp_path / "p1", ["AAA", "BBB", "CCC"],
                          FakeEvents({**answers, "BBB": unknown(), "CCC": VendorError("x", "VNSTOCK_RELEASE_EXPIRED")}))
    _, second, _, _ = _run(tmp_path / "p2", ["BBB", "CCC"], FakeEvents({**answers, "BBB": unknown()}))
    merged_dir = tmp_path / "merged"
    assert vc.run_merge([first, second], _universe(tmp_path, ["AAA", "BBB", "CCC"]), merged_dir) == 0
    symbols = _summary(merged_dir)["symbols"]
    assert symbols["ok"] == 2 and symbols["failed"] == 1 and symbols["skipped"] == 0
    assert [entry["symbol"] for entry in symbols["failed_list"]] == ["BBB"]


def test_merge_refuses_a_failed_symbol_outside_the_universe(tmp_path: Path) -> None:
    first, second = _two_parts(tmp_path)
    failed_list = [{"symbol": "ZZZ", "type": "VendorError", "code": "VNSTOCK_INPUT_SYMBOL_UNKNOWN"}]
    _edit_summary(second, symbols={**_summary(second)["symbols"], "failed": 1, "failed_list": failed_list})
    with pytest.raises(ValueError, match="ZZZ"):
        vc.run_merge([first, second], _universe(tmp_path, ["AAA", "BBB", "CCC"]), tmp_path / "merged")


def test_merge_keeps_a_quota_warning_stop_of_an_input(tmp_path: Path) -> None:
    # A quota_warning run keeps the flagged rows in raw/: a rerun of the skipped symbols cannot clear that flag.
    answers = _merge_answers()

    def warned() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        warnings.warn("result truncated", UserWarning, stacklevel=1)
        return answers["BBB"], {}

    _, first, _, _ = _run(tmp_path / "p1", ["AAA", "BBB", "CCC"], FakeEvents({**answers, "BBB": warned}))
    _, second, _, _ = _run(tmp_path / "p2", ["CCC"], FakeEvents(answers))
    merged_dir = tmp_path / "merged"
    assert vc.run_merge([first, second], _universe(tmp_path, ["AAA", "BBB", "CCC"]), merged_dir) == 1
    merged = _summary(merged_dir)
    assert merged["stop"]["reason"] == "quota_warning" and merged["stop"]["symbol"] == "BBB"
    assert merged["stop"]["input"] == first.name
    assert merged["coverage"]["complete"] is False and merged["symbols"]["ok"] == 3
