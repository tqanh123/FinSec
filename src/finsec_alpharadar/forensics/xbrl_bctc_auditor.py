from __future__ import annotations

from dataclasses import dataclass
from typing import List

from finsec_alpharadar.domain.models import FinancialStatementSnapshot


@dataclass
class ForensicAuditResult:
    symbol: str
    period: str
    altman_z_score: float
    altman_zone: str
    beneish_m_score: float
    is_manipulation_suspect: bool
    piotroski_f_score: int
    red_flags: List[str]


class ForensicAccountingEngine:
    """Computes Altman Z-Score, 8-Variable Beneish M-Score, and 9-Point Piotroski F-Score."""

    @staticmethod
    def evaluate(fs: FinancialStatementSnapshot) -> ForensicAuditResult:
        red_flags: List[str] = []
        working_capital = fs.current_assets - fs.current_liabilities
        x1 = working_capital / fs.total_assets
        x2 = fs.retained_earnings / fs.total_assets
        x3 = fs.ebit / fs.total_assets
        x4 = fs.market_cap / max(1.0, fs.total_liabilities)
        x5 = fs.revenue / fs.total_assets
        altman_z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
        if altman_z > 2.99:
            altman_zone = "SAFE"
        elif altman_z >= 1.81:
            altman_zone = "GREY_ZONE"
        else:
            altman_zone = "DISTRESS"
            red_flags.append(f"Altman Z-Score in Distress Zone ({altman_z:.2f} < 1.81)")

        dsri = (fs.accounts_receivable / fs.revenue) / (fs.prev_accounts_receivable / fs.prev_revenue)
        prev_gm = (fs.prev_revenue - fs.prev_cost_of_goods_sold) / fs.prev_revenue
        curr_gm = (fs.revenue - fs.cost_of_goods_sold) / fs.revenue
        gmi = prev_gm / max(1e-4, curr_gm)
        curr_aq = 1.0 - (fs.current_assets + fs.property_plant_equipment) / fs.total_assets
        prev_aq = 1.0 - (fs.prev_current_assets + fs.prev_property_plant_equipment) / fs.prev_total_assets
        aqi = curr_aq / max(1e-4, prev_aq)
        sgi = fs.revenue / fs.prev_revenue
        prev_dep_rate = fs.prev_depreciation / (fs.prev_depreciation + fs.prev_property_plant_equipment)
        curr_dep_rate = fs.depreciation / (fs.depreciation + fs.property_plant_equipment)
        depi = prev_dep_rate / max(1e-4, curr_dep_rate)
        sgai = (fs.selling_general_admin / fs.revenue) / (fs.prev_selling_general_admin / fs.prev_revenue)
        curr_lev = (fs.long_term_debt + fs.current_liabilities) / fs.total_assets
        prev_lev = (fs.prev_long_term_debt + fs.prev_current_liabilities) / fs.prev_total_assets
        lvgi = curr_lev / max(1e-4, prev_lev)
        tata = (fs.net_income - fs.operating_cash_flow) / fs.total_assets

        beneish_m = (
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
        is_manip = beneish_m > -1.78
        if is_manip:
            red_flags.append(f"Beneish M-Score exceeds manipulation threshold ({beneish_m:.2f} > -1.78)")

        roa = fs.net_income / fs.total_assets
        prev_roa = fs.prev_net_income / fs.prev_total_assets
        cfo = fs.operating_cash_flow / fs.total_assets
        curr_ratio = fs.current_assets / max(1.0, fs.current_liabilities)
        prev_curr_ratio = fs.prev_current_assets / max(1.0, fs.prev_current_liabilities)
        asset_turnover = fs.revenue / fs.total_assets
        prev_asset_turnover = fs.prev_revenue / fs.prev_total_assets

        f_score = sum([
            int(roa > 0),
            int(cfo > 0),
            int(roa > prev_roa),
            int(fs.operating_cash_flow > fs.net_income),
            int((fs.long_term_debt / fs.total_assets) <= (fs.prev_long_term_debt / fs.prev_total_assets)),
            int(curr_ratio > prev_curr_ratio),
            int(fs.shares_outstanding <= fs.prev_shares_outstanding),
            int(curr_gm > prev_gm),
            int(asset_turnover > prev_asset_turnover),
        ])

        return ForensicAuditResult(
            symbol=fs.symbol,
            period=fs.period,
            altman_z_score=round(altman_z, 3),
            altman_zone=altman_zone,
            beneish_m_score=round(beneish_m, 3),
            is_manipulation_suspect=is_manip,
            piotroski_f_score=f_score,
            red_flags=red_flags,
        )
