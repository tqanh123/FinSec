from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Callable, Dict, List, Optional, Tuple, TypeVar

from finsec_alpharadar.domain.models import FinancialStatementSnapshot, IssuerType

_T = TypeVar("_T")

_SCORES: Tuple[str, ...] = ("altman", "beneish", "piotroski")
_FINANCIAL_ISSUERS = frozenset({IssuerType.BANK, IssuerType.INSURER, IssuerType.SECURITIES_FIRM})
_DECLARATION_INDEX: Dict[str, int] = {f.name: index for index, f in enumerate(fields(FinancialStatementSnapshot))}


def _in_declaration_order(*names: str) -> Tuple[str, ...]:
    """`names` in `FinancialStatementSnapshot` declaration order; a name it does not declare raises KeyError at import."""
    return tuple(sorted(names, key=lambda name: _DECLARATION_INDEX[name]))


# The snapshot fields each formula reads. The finite check names the first non-finite one in declaration order.
_ALTMAN_INPUTS = _in_declaration_order(
    "current_assets", "current_liabilities", "total_assets", "retained_earnings", "ebit", "market_cap",
    "total_liabilities", "revenue",
)
_BENEISH_INPUTS = _in_declaration_order(
    "accounts_receivable", "revenue", "prev_accounts_receivable", "prev_revenue",  # DSRI, SGI
    "cost_of_goods_sold", "prev_cost_of_goods_sold",  # GMI
    "current_assets", "property_plant_equipment", "total_assets",  # AQI
    "prev_current_assets", "prev_property_plant_equipment", "prev_total_assets",
    "depreciation", "prev_depreciation",  # DEPI
    "selling_general_admin", "prev_selling_general_admin",  # SGAI
    "long_term_debt", "current_liabilities", "prev_long_term_debt", "prev_current_liabilities",  # LVGI
    "net_income", "operating_cash_flow",  # TATA
)
_PIOTROSKI_INPUTS = _in_declaration_order(
    "net_income", "prev_net_income", "operating_cash_flow", "total_assets", "prev_total_assets",
    "current_assets", "current_liabilities", "prev_current_assets", "prev_current_liabilities",
    "long_term_debt", "prev_long_term_debt", "shares_outstanding", "prev_shares_outstanding",
    "revenue", "prev_revenue", "cost_of_goods_sold", "prev_cost_of_goods_sold",
)


@dataclass(frozen=True)
class ForensicAuditResult:
    """The three scores of one snapshot.

    A score that is None has its reason in `unavailable_reasons` under "altman", "beneish" or "piotroski"; its zone
    or flag is None too, and it adds no red flag. Red flags come only from computed scores.
    """

    symbol: str
    period: str
    altman_z_score: Optional[float]
    altman_zone: Optional[str]
    beneish_m_score: Optional[float]
    is_manipulation_suspect: Optional[bool]
    piotroski_f_score: Optional[int]
    red_flags: List[str] = field(default_factory=list)
    unavailable_reasons: Dict[str, str] = field(default_factory=dict)


class _ScoreUnavailable(Exception):
    """Raised inside a score function when the score is undefined; `reason` goes into `unavailable_reasons`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _require_finite(fs: FinancialStatementSnapshot, names: Tuple[str, ...]) -> None:
    """Raise "<field>_not_finite" for the first of `names` that is NaN or infinite (a guard such as x <= 0 misses NaN)."""
    for name in names:
        try:
            finite = math.isfinite(getattr(fs, name))
        except (TypeError, OverflowError):  # not a number at all, or an int too large for a float
            finite = False
        if not finite:
            raise _ScoreUnavailable(f"{name}_not_finite")


def _positive(name: str, value: float) -> float:
    """`value` if it is above 0; otherwise raise "<name>_non_positive". Written as not (x > 0) so NaN fails too."""
    if not (value > 0):
        raise _ScoreUnavailable(f"{name}_non_positive")
    return value


def _non_negative(name: str, value: float) -> float:
    """`value` if it is 0 or above; otherwise raise "<name>_negative". Written as not (x >= 0) so NaN fails too."""
    if not (value >= 0):
        raise _ScoreUnavailable(f"{name}_negative")
    return value


def _gross_margin(revenue: float, cost_of_goods_sold: float) -> float:
    """Gross margin as a share of revenue; the caller has checked that revenue is above 0."""
    return (revenue - cost_of_goods_sold) / revenue


def _altman_z(fs: FinancialStatementSnapshot) -> float:
    _require_finite(fs, _ALTMAN_INPUTS)
    total_assets = _positive("total_assets", fs.total_assets)
    total_liabilities = _positive("total_liabilities", fs.total_liabilities)
    market_cap = _positive("market_cap", fs.market_cap)  # 0 also stands for a missing market cap
    working_capital = fs.current_assets - fs.current_liabilities
    x1 = working_capital / total_assets
    x2 = fs.retained_earnings / total_assets
    x3 = fs.ebit / total_assets
    x4 = market_cap / total_liabilities
    x5 = fs.revenue / total_assets
    return 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5


def _beneish_m(fs: FinancialStatementSnapshot) -> float:
    _require_finite(fs, _BENEISH_INPUTS)
    # Guards in a fixed order, so the reason names the first one that fails; each ratio below then has a positive
    # denominator.
    revenue = _positive("revenue", fs.revenue)
    prev_revenue = _positive("prev_revenue", fs.prev_revenue)
    prev_accounts_receivable = _positive("prev_accounts_receivable", fs.prev_accounts_receivable)
    curr_gm = _positive("gross_margin", _gross_margin(revenue, fs.cost_of_goods_sold))
    prev_gm = _positive("prev_gross_margin", _gross_margin(prev_revenue, fs.prev_cost_of_goods_sold))
    total_assets = _positive("total_assets", fs.total_assets)
    prev_total_assets = _positive("prev_total_assets", fs.prev_total_assets)
    prev_aq = _positive(
        "prev_asset_quality", 1.0 - (fs.prev_current_assets + fs.prev_property_plant_equipment) / prev_total_assets
    )
    # Below 0 means current assets plus PPE exceed total assets: bad data, not a ratio edge.
    curr_aq = _non_negative("asset_quality", 1.0 - (fs.current_assets + fs.property_plant_equipment) / total_assets)
    dep_plus_ppe = _positive("dep_plus_ppe", fs.depreciation + fs.property_plant_equipment)
    prev_dep_plus_ppe = _positive("prev_dep_plus_ppe", fs.prev_depreciation + fs.prev_property_plant_equipment)
    depreciation = _positive("depreciation", fs.depreciation)
    prev_selling_general_admin = _positive("prev_selling_general_admin", fs.prev_selling_general_admin)
    prev_lev = _positive("prev_leverage", (fs.prev_long_term_debt + fs.prev_current_liabilities) / prev_total_assets)
    # A quotient of two positive inputs can still underflow to 0.0 (a subnormal input), so the derived denominators
    # get their own guard.
    prev_receivables_ratio = _positive("prev_receivables_ratio", prev_accounts_receivable / prev_revenue)
    prev_sga_ratio = _positive("prev_sga_ratio", prev_selling_general_admin / prev_revenue)
    curr_dep_rate = _positive("depreciation_rate", depreciation / dep_plus_ppe)

    dsri = (fs.accounts_receivable / revenue) / prev_receivables_ratio
    gmi = prev_gm / curr_gm
    aqi = curr_aq / prev_aq
    sgi = revenue / prev_revenue
    prev_dep_rate = fs.prev_depreciation / prev_dep_plus_ppe
    depi = prev_dep_rate / curr_dep_rate
    sgai = (fs.selling_general_admin / revenue) / prev_sga_ratio
    curr_lev = (fs.long_term_debt + fs.current_liabilities) / total_assets
    lvgi = curr_lev / prev_lev
    tata = (fs.net_income - fs.operating_cash_flow) / total_assets
    return (
        -4.84
        + 0.920 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )


def _piotroski_f(fs: FinancialStatementSnapshot) -> int:
    _require_finite(fs, _PIOTROSKI_INPUTS)
    total_assets = _positive("total_assets", fs.total_assets)
    prev_total_assets = _positive("prev_total_assets", fs.prev_total_assets)
    current_liabilities = _positive("current_liabilities", fs.current_liabilities)
    prev_current_liabilities = _positive("prev_current_liabilities", fs.prev_current_liabilities)
    revenue = _positive("revenue", fs.revenue)
    prev_revenue = _positive("prev_revenue", fs.prev_revenue)

    roa = fs.net_income / total_assets
    prev_roa = fs.prev_net_income / prev_total_assets
    cfo = fs.operating_cash_flow / total_assets
    curr_ratio = fs.current_assets / current_liabilities
    prev_curr_ratio = fs.prev_current_assets / prev_current_liabilities
    asset_turnover = revenue / total_assets
    prev_asset_turnover = prev_revenue / prev_total_assets
    curr_gm = _gross_margin(revenue, fs.cost_of_goods_sold)
    prev_gm = _gross_margin(prev_revenue, fs.prev_cost_of_goods_sold)
    return sum([
        int(roa > 0),
        int(cfo > 0),
        int(roa > prev_roa),
        int(fs.operating_cash_flow > fs.net_income),
        int((fs.long_term_debt / total_assets) <= (fs.prev_long_term_debt / prev_total_assets)),
        int(curr_ratio > prev_curr_ratio),
        int(fs.shares_outstanding <= fs.prev_shares_outstanding),
        int(curr_gm > prev_gm),
        int(asset_turnover > prev_asset_turnover),
    ])


def _try_score(
    name: str, compute: Callable[[FinancialStatementSnapshot], _T], fs: FinancialStatementSnapshot, reasons: Dict[str, str]
) -> Optional[_T]:
    """`compute(fs)`, or None with the reason stored in `reasons[name]` when the score is undefined.

    A score that comes out NaN or infinite from finite inputs (a ratio overflowing on a subnormal denominator) is
    refused as "score_not_finite": the zone test would read NaN as DISTRESS.
    """
    try:
        score = compute(fs)
    except _ScoreUnavailable as unavailable:
        reasons[name] = unavailable.reason
        return None
    if isinstance(score, float) and not math.isfinite(score):
        reasons[name] = "score_not_finite"
        return None
    return score


def _unscored(fs: FinancialStatementSnapshot, reason: str) -> ForensicAuditResult:
    """A result with no score: the same `reason` for all three scores and no red flag."""
    return ForensicAuditResult(
        symbol=fs.symbol,
        period=fs.period,
        altman_z_score=None,
        altman_zone=None,
        beneish_m_score=None,
        is_manipulation_suspect=None,
        piotroski_f_score=None,
        unavailable_reasons=dict.fromkeys(_SCORES, reason),
    )


class ForensicAccountingEngine:
    """Computes Altman Z-Score, 8-Variable Beneish M-Score, and 9-Point Piotroski F-Score.

    Only `IssuerType.NON_FINANCIAL` is scored (AGENTS.md "Forensics"): a bank, insurer or securities firm gets no
    score ("not_applicable_financial_issuer"), and any other issuer type is refused ("issuer_type_unknown"). The three
    scores are independent. A score is None, with a reason, when a snapshot field its formula reads is not finite
    ("<field>_not_finite") or when one of its ratios is undefined ("<input>_non_positive", "<input>_negative"); the
    first failing check gives the reason. The formulas are the prototype's; their corrections are roadmap Phase 2.
    """

    @staticmethod
    def evaluate(fs: FinancialStatementSnapshot) -> ForensicAuditResult:
        # The issuer gate runs before any computation and fails closed; a non-string value (even an unhashable one)
        # is not a financial issuer type and falls through to "issuer_type_unknown".
        if isinstance(fs.issuer_type, str) and fs.issuer_type in _FINANCIAL_ISSUERS:
            return _unscored(fs, "not_applicable_financial_issuer")
        if fs.issuer_type != IssuerType.NON_FINANCIAL:
            return _unscored(fs, "issuer_type_unknown")

        reasons: Dict[str, str] = {}
        red_flags: List[str] = []
        altman_z = _try_score("altman", _altman_z, fs, reasons)
        altman_zone: Optional[str] = None
        if altman_z is not None:
            if altman_z > 2.99:
                altman_zone = "SAFE"
            elif altman_z >= 1.81:
                altman_zone = "GREY_ZONE"
            else:
                altman_zone = "DISTRESS"
                red_flags.append(f"Altman Z-Score in Distress Zone ({altman_z:.2f} < 1.81)")

        beneish_m = _try_score("beneish", _beneish_m, fs, reasons)
        is_manip: Optional[bool] = None
        if beneish_m is not None:
            is_manip = beneish_m > -1.78
            if is_manip:
                red_flags.append(f"Beneish M-Score exceeds manipulation threshold ({beneish_m:.2f} > -1.78)")

        f_score = _try_score("piotroski", _piotroski_f, fs, reasons)

        return ForensicAuditResult(
            symbol=fs.symbol,
            period=fs.period,
            altman_z_score=None if altman_z is None else round(altman_z, 3),
            altman_zone=altman_zone,
            beneish_m_score=None if beneish_m is None else round(beneish_m, 3),
            is_manipulation_suspect=is_manip,
            piotroski_f_score=f_score,
            red_flags=red_flags,
            unavailable_reasons=reasons,
        )
