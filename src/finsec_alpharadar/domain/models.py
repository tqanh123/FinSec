from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class MarketRegion(str, Enum):
    US = "US"  # NYSE / NASDAQ / IEX (USD, T+1, SEC EDGAR)
    VN = "VN"  # HOSE / HNX / UPCOM (VND, T+2.5, +/-7%/10%/15% bands, SSC/HSX)


class VNExchangeBand(float, Enum):
    HOSE = 0.07
    HNX = 0.10
    UPCOM = 0.15


@dataclass(frozen=True)
class MarketTick:
    symbol: str
    market: MarketRegion
    timestamp: float
    price: float
    volume: float
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    reference_price: Optional[float] = None
    exchange: str = "IEX"
    foreign_buy_volume: float = 0.0
    foreign_sell_volume: float = 0.0

    @property
    def spread_bps(self) -> float:
        mid = 0.5 * (self.bid_price + self.ask_price)
        return ((self.ask_price - self.bid_price) / mid) * 10_000.0 if mid > 0 else 0.0

    @property
    def vn_ceiling_floor_proximity(self) -> Optional[Tuple[float, float]]:
        if self.market != MarketRegion.VN or not self.reference_price:
            return None
        band = VNExchangeBand[self.exchange].value if self.exchange in VNExchangeBand.__members__ else 0.07
        return round(self.reference_price * (1.0 + band), 2), round(self.reference_price * (1.0 - band), 2)


@dataclass(frozen=True)
class RegulatoryFiling:
    filing_id: str
    symbol: str
    market: MarketRegion
    form_type: str
    published_at: float
    title: str
    section: str
    text_content: str
    citation_url: str
    xbrl_metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class FinancialStatementSnapshot:
    symbol: str
    market: MarketRegion
    period: str
    revenue: float
    cost_of_goods_sold: float
    selling_general_admin: float
    depreciation: float
    net_income: float
    operating_cash_flow: float
    accounts_receivable: float
    current_assets: float
    current_liabilities: float
    property_plant_equipment: float
    total_assets: float
    retained_earnings: float
    ebit: float
    long_term_debt: float
    total_liabilities: float
    market_cap: float
    shares_outstanding: float
    prev_revenue: float
    prev_cost_of_goods_sold: float
    prev_selling_general_admin: float
    prev_depreciation: float
    prev_net_income: float
    prev_accounts_receivable: float
    prev_current_assets: float
    prev_current_liabilities: float
    prev_property_plant_equipment: float
    prev_total_assets: float
    prev_long_term_debt: float
    prev_shares_outstanding: float
