"""Forensic score guards and the issuer gate (AGENTS.md "Engineering Rules (binding)", "Forensics").

`ForensicAccountingEngine.evaluate` scores only `IssuerType.NON_FINANCIAL`: a bank, insurer or securities firm gets
no score, and any other issuer type is refused. A score whose formula reads a non-finite field, or whose ratio is
undefined, is None with a reason in `unavailable_reasons`; it adds no red flag, and the other scores are unaffected.
The base snapshot is the smoke fixture of `tests/test_end_to_end.py` (lines 106-119) as a non-financial issuer.
Pure unit tests: no network, no clock.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Tuple

import pytest

from finsec_alpharadar.domain import FinancialStatementSnapshot, IssuerType, MarketRegion
from finsec_alpharadar.forensics import ForensicAccountingEngine, ForensicAuditResult

NAN = float("nan")
INF = float("inf")

# The statement lines of the smoke fixture, tests/test_end_to_end.py:107-118 (a US quarter, no issuer_type).
SMOKE_FIXTURE: Dict[str, Any] = dict(
    symbol="NVDA", market=MarketRegion.US, period="2026-Q3",
    revenue=38_500.0, cost_of_goods_sold=9_200.0, selling_general_admin=3_100.0,
    depreciation=850.0, net_income=20_400.0, operating_cash_flow=22_100.0,
    accounts_receivable=5_400.0, current_assets=58_000.0, current_liabilities=14_000.0,
    property_plant_equipment=12_500.0, total_assets=92_000.0, retained_earnings=64_000.0,
    ebit=24_800.0, long_term_debt=8_500.0, total_liabilities=22_500.0,
    market_cap=3_800_000.0, shares_outstanding=24_500.0,
    prev_revenue=32_000.0, prev_cost_of_goods_sold=8_100.0, prev_selling_general_admin=2_850.0,
    prev_depreciation=780.0, prev_net_income=16_200.0, prev_accounts_receivable=4_600.0,
    prev_current_assets=49_000.0, prev_current_liabilities=13_200.0,
    prev_property_plant_equipment=10_800.0, prev_total_assets=78_000.0,
    prev_long_term_debt=9_000.0, prev_shares_outstanding=24_650.0,
)
BASE = FinancialStatementSnapshot(**SMOKE_FIXTURE, issuer_type=IssuerType.NON_FINANCIAL)

# Each score with the result fields it fills: the score, then its zone or flag.
SCORE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "altman": ("altman_z_score", "altman_zone"),
    "beneish": ("beneish_m_score", "is_manipulation_suspect"),
    "piotroski": ("piotroski_f_score",),
}
SCORES = tuple(SCORE_FIELDS)
FINANCIAL_ISSUERS = (IssuerType.BANK, IssuerType.INSURER, IssuerType.SECURITIES_FIRM)

# Case id -> (snapshot overrides, {score: reason} for the scores that must be None, scores that must equal the base).
# A score in neither group reads a changed field but stays defined: it must be computed, not None.
Case = Tuple[Dict[str, float], Dict[str, str], Tuple[str, ...]]
DEGENERATE_CASES: Dict[str, Case] = {
    "total_liabilities=0": (
        {"total_liabilities": 0.0}, {"altman": "total_liabilities_non_positive"}, ("beneish", "piotroski"),
    ),
    "market_cap=0": ({"market_cap": 0.0}, {"altman": "market_cap_non_positive"}, ("beneish", "piotroski")),
    "cost_of_goods_sold=revenue": (  # gross margin 0; Piotroski compares the two margins, so it changes but stays defined
        {"cost_of_goods_sold": 38_500.0}, {"beneish": "gross_margin_non_positive"}, ("altman",),
    ),
    "prev_asset_quality=0": (  # prev CA + prev PPE = prev TA; only Beneish reads prev PPE
        {"prev_property_plant_equipment": 29_000.0},
        {"beneish": "prev_asset_quality_non_positive"},
        ("altman", "piotroski"),
    ),
    "current_liabilities=0": (  # Altman (working capital) and Beneish (leverage) read it but stay defined
        {"current_liabilities": 0.0}, {"piotroski": "current_liabilities_non_positive"}, (),
    ),
    "total_assets=0": ({"total_assets": 0.0}, dict.fromkeys(SCORES, "total_assets_non_positive"), ()),
    "revenue=0": (  # Altman reads revenue (X5) but stays defined
        {"revenue": 0.0}, {"beneish": "revenue_non_positive", "piotroski": "revenue_non_positive"}, (),
    ),
    "prev_accounts_receivable=0": (
        {"prev_accounts_receivable": 0.0},
        {"beneish": "prev_accounts_receivable_non_positive"},
        ("altman", "piotroski"),
    ),
    "prev_selling_general_admin=0": (
        {"prev_selling_general_admin": 0.0},
        {"beneish": "prev_selling_general_admin_non_positive"},
        ("altman", "piotroski"),
    ),
    "depreciation=property_plant_equipment=0": (
        {"depreciation": 0.0, "property_plant_equipment": 0.0},
        {"beneish": "dep_plus_ppe_non_positive"},
        ("altman", "piotroski"),
    ),
    "retained_earnings=nan": (
        {"retained_earnings": NAN}, {"altman": "retained_earnings_not_finite"}, ("beneish", "piotroski"),
    ),
    "market_cap=nan": ({"market_cap": NAN}, {"altman": "market_cap_not_finite"}, ("beneish", "piotroski")),
    "total_liabilities=inf": (
        {"total_liabilities": INF}, {"altman": "total_liabilities_not_finite"}, ("beneish", "piotroski"),
    ),
    "net_income=nan": (
        {"net_income": NAN}, {"beneish": "net_income_not_finite", "piotroski": "net_income_not_finite"}, ("altman",),
    ),
    # One row per remaining guard, so removing any guard of R4.3 turns a row red.
    "depreciation=0": ({"depreciation": 0.0}, {"beneish": "depreciation_non_positive"}, ("altman", "piotroski")),
    "prev_revenue=0": (
        {"prev_revenue": 0.0},
        {"beneish": "prev_revenue_non_positive", "piotroski": "prev_revenue_non_positive"},
        ("altman",),
    ),
    "prev_total_assets=0": (
        {"prev_total_assets": 0.0},
        {"beneish": "prev_total_assets_non_positive", "piotroski": "prev_total_assets_non_positive"},
        ("altman",),
    ),
    "asset_quality<0": (  # CA + PPE above total assets: bad data
        {"property_plant_equipment": 40_000.0}, {"beneish": "asset_quality_negative"}, ("altman", "piotroski"),
    ),
    "prev_cost_of_goods_sold=prev_revenue": (  # previous gross margin 0; Piotroski compares margins and stays defined
        {"prev_cost_of_goods_sold": 32_000.0}, {"beneish": "prev_gross_margin_non_positive"}, ("altman",),
    ),
    "prev_depreciation=prev_property_plant_equipment=0": (
        {"prev_depreciation": 0.0, "prev_property_plant_equipment": 0.0},
        {"beneish": "prev_dep_plus_ppe_non_positive"},
        ("altman", "piotroski"),
    ),
    "prev_long_term_debt=prev_current_liabilities=0": (
        {"prev_long_term_debt": 0.0, "prev_current_liabilities": 0.0},
        {"beneish": "prev_leverage_non_positive", "piotroski": "prev_current_liabilities_non_positive"},
        ("altman",),
    ),
    "prev_current_liabilities=0": (  # Beneish leverage reads it but stays defined
        {"prev_current_liabilities": 0.0}, {"piotroski": "prev_current_liabilities_non_positive"}, ("altman",),
    ),
    # Several failing checks: the reason names the first one (finite checks in declaration order, then the guards).
    "revenue=0,prev_accounts_receivable=0": (
        {"revenue": 0.0, "prev_accounts_receivable": 0.0},
        {"beneish": "revenue_non_positive", "piotroski": "revenue_non_positive"},
        (),
    ),
    "depreciation=0,prev_selling_general_admin=0": (
        {"depreciation": 0.0, "prev_selling_general_admin": 0.0},
        {"beneish": "depreciation_non_positive"},
        ("altman", "piotroski"),
    ),
    "accounts_receivable=nan,revenue=nan": (  # revenue is declared first
        {"accounts_receivable": NAN, "revenue": NAN}, dict.fromkeys(SCORES, "revenue_not_finite"), (),
    ),
    # A quotient of two positive inputs can underflow to 0.0 (a subnormal input): its own guard catches it.
    "prev_accounts_receivable=5e-324": (
        {"prev_accounts_receivable": 5e-324}, {"beneish": "prev_receivables_ratio_non_positive"}, ("altman", "piotroski"),
    ),
    "prev_selling_general_admin=5e-324": (
        {"prev_selling_general_admin": 5e-324}, {"beneish": "prev_sga_ratio_non_positive"}, ("altman", "piotroski"),
    ),
    "depreciation=5e-324": ({"depreciation": 5e-324}, {"beneish": "depreciation_rate_non_positive"}, ("altman", "piotroski")),
    "prev_revenue=nan": (  # the finite check runs before the prev_revenue guard
        {"prev_revenue": NAN},
        {"beneish": "prev_revenue_not_finite", "piotroski": "prev_revenue_not_finite"},
        ("altman",),
    ),
}

# The snapshot fields each formula reads, written out from the prototype's formulas (xbrl_bctc_auditor.py at 7092637:
# Altman lines 27-33, Beneish 42-70, Piotroski 74-92 plus the two margins of lines 43-44), not imported from the
# module under test.
READS: Dict[str, frozenset] = {
    "altman": frozenset({
        "current_assets", "current_liabilities", "total_assets", "retained_earnings", "ebit", "market_cap",
        "total_liabilities", "revenue",
    }),
    "beneish": frozenset({
        "accounts_receivable", "revenue", "prev_accounts_receivable", "prev_revenue", "cost_of_goods_sold",
        "prev_cost_of_goods_sold", "current_assets", "property_plant_equipment", "total_assets", "prev_current_assets",
        "prev_property_plant_equipment", "prev_total_assets", "depreciation", "prev_depreciation",
        "selling_general_admin", "prev_selling_general_admin", "long_term_debt", "current_liabilities",
        "prev_long_term_debt", "prev_current_liabilities", "net_income", "operating_cash_flow",
    }),
    "piotroski": frozenset({
        "net_income", "prev_net_income", "operating_cash_flow", "total_assets", "prev_total_assets", "current_assets",
        "current_liabilities", "prev_current_assets", "prev_current_liabilities", "long_term_debt",
        "prev_long_term_debt", "shares_outstanding", "prev_shares_outstanding", "revenue", "prev_revenue",
        "cost_of_goods_sold", "prev_cost_of_goods_sold",
    }),
}
NUMBER_FIELDS = [name for name, value in SMOKE_FIXTURE.items() if isinstance(value, float)]


def _score(result: ForensicAuditResult, name: str) -> Tuple[Any, ...]:
    """One score with its zone or flag, e.g. (altman_z_score, altman_zone)."""
    return tuple(getattr(result, attr) for attr in SCORE_FIELDS[name])


def _flags_of(result: ForensicAuditResult, name: str) -> List[str]:
    """The red flags one score raised; each flag starts with the score's name ("Altman ...", "Beneish ...")."""
    return [flag for flag in result.red_flags if flag.lower().startswith(name)]


def _assert_no_scores(result: ForensicAuditResult, reason: str) -> None:
    assert [_score(result, name) for name in SCORES] == [(None, None), (None, None), (None,)]
    assert result.unavailable_reasons == dict.fromkeys(SCORES, reason)
    assert result.red_flags == []


@pytest.mark.parametrize("issuer_type", FINANCIAL_ISSUERS, ids=[issuer.name for issuer in FINANCIAL_ISSUERS])
def test_financial_issuers_get_no_scores(issuer_type: IssuerType) -> None:
    # Binding rule: Altman/Beneish/Piotroski never apply to banks, insurers or securities firms.
    result = ForensicAccountingEngine.evaluate(dataclasses.replace(BASE, issuer_type=issuer_type))
    _assert_no_scores(result, "not_applicable_financial_issuer")


def test_unknown_issuer_type_fails_closed() -> None:
    unknown = FinancialStatementSnapshot(**SMOKE_FIXTURE)  # issuer_type omitted
    assert unknown.issuer_type is IssuerType.UNKNOWN
    # Any value other than NON_FINANCIAL is refused, also a label that is not an IssuerType member.
    strays = ("bank", ["BANK"])  # a stray label, and an unhashable value that must not crash the gate
    for snapshot in (unknown, *(dataclasses.replace(unknown, issuer_type=stray) for stray in strays)):
        _assert_no_scores(ForensicAccountingEngine.evaluate(snapshot), "issuer_type_unknown")


@pytest.mark.parametrize("case", list(DEGENERATE_CASES))
def test_degenerate_input_gives_none_with_reason(case: str) -> None:
    overrides, expected_none, unchanged = DEGENERATE_CASES[case]
    base = ForensicAccountingEngine.evaluate(BASE)  # the unaffected scores are compared with this, not with constants
    result = ForensicAccountingEngine.evaluate(dataclasses.replace(BASE, **overrides))
    for name in SCORES:
        if name in expected_none:
            assert all(value is None for value in _score(result, name)), (name, _score(result, name))
            assert _flags_of(result, name) == [], name
        elif name in unchanged:
            assert _score(result, name) == _score(base, name), name
        else:
            assert _score(result, name)[0] is not None, name
    assert result.unavailable_reasons == expected_none


@pytest.mark.parametrize("field", NUMBER_FIELDS)
def test_finite_check_covers_exactly_the_fields_each_score_reads(field: str) -> None:
    # A NaN in a field nulls exactly the scores that read it (red-team r1 RT2), and no other score (RT11).
    base = ForensicAccountingEngine.evaluate(BASE)
    result = ForensicAccountingEngine.evaluate(dataclasses.replace(BASE, **{field: NAN}))
    for name in SCORES:
        if field in READS[name]:
            assert all(value is None for value in _score(result, name)), (name, _score(result, name))
            assert result.unavailable_reasons.get(name) == f"{field}_not_finite", (name, result.unavailable_reasons)
        else:
            assert _score(result, name) == _score(base, name), name
            assert name not in result.unavailable_reasons, name


def test_none_score_adds_no_red_flag() -> None:
    # A gross margin of 0 leaves the Beneish M-Score undefined; the old clamp turned it into a huge M and a red flag.
    result = ForensicAccountingEngine.evaluate(dataclasses.replace(BASE, cost_of_goods_sold=BASE.revenue))
    assert result.red_flags == []
    assert result.beneish_m_score is None and result.is_manipulation_suspect is None


def test_zero_liabilities_is_not_safe() -> None:
    # X4 = market cap / total liabilities is undefined at 0; the old clamp made Z huge and the zone "SAFE".
    result = ForensicAccountingEngine.evaluate(dataclasses.replace(BASE, total_liabilities=0.0))
    assert result.altman_zone is None
    assert result.altman_z_score is None


def test_non_financial_fixture_scores_unchanged() -> None:
    # LOCK: the values the prototype computed on this fixture (OBSERVED 2026-10-21); removing the clamps and adding
    # the guards must not change them, because no clamp or guard fires here.
    result = ForensicAccountingEngine.evaluate(BASE)
    assert result.altman_z_score == 104.189 and result.altman_zone == "SAFE"
    assert result.beneish_m_score == -2.348 and result.is_manipulation_suspect is False
    assert result.piotroski_f_score == 9
    assert result.red_flags == []
    assert result.unavailable_reasons == {}


def test_audit_result_is_frozen() -> None:
    result = ForensicAccountingEngine.evaluate(BASE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.altman_zone = "X"
    with pytest.raises(dataclasses.FrozenInstanceError):  # the snapshot is a record too (R4.1)
        BASE.revenue = 0.0  # type: ignore[misc]
