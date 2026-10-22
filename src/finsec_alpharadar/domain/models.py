from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class MarketRegion(str, Enum):
    US = "US"  # NYSE / NASDAQ / IEX (USD, T+1, SEC EDGAR)
    VN = "VN"  # HOSE / HNX / UPCOM (VND, T+2.5, +/-7%/10%/15% bands, SSC/HSX)


class VNExchangeBand(float, Enum):
    HOSE = 0.07
    HNX = 0.10
    UPCOM = 0.15


class TickValidationError(ValueError):
    """A `MarketTick` field is not a finite number, is missing, or is outside its allowed range."""


class UnknownExchangeError(TickValidationError):
    """A VN tick has no exchange, or one that `VNExchangeBand` does not list."""


class PriceOutOfBandError(TickValidationError):
    """A VN price lies outside its allowed range."""


# Number fields every tick must carry as finite numbers: json.loads (and so the API) accepts NaN and Infinity.
_FINITE_TICK_FIELDS: Tuple[str, ...] = (
    "timestamp",
    "price",
    "volume",
    "bid_price",
    "ask_price",
    "bid_size",
    "ask_size",
    "foreign_buy_volume",
    "foreign_sell_volume",
)


def _is_finite_number(value: Any) -> bool:
    """True for a finite real number; False for NaN, an infinity, an int too large for a float, None or a non-number."""
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


def _require_finite(where: str, name: str, value: Any) -> None:
    if not _is_finite_number(value):
        raise TickValidationError(f"{where}{name} must be a finite number, got {value!r}")


def _vn_exchange(exchange: Optional[str], where: str = "") -> str:
    """Return `exchange` if `VNExchangeBand` lists it; otherwise raise UnknownExchangeError. `where` prefixes the message."""
    names = ", ".join(VNExchangeBand.__members__)
    if exchange is None:
        raise UnknownExchangeError(f"{where}exchange is missing; a VN tick needs one of {names}")
    if not isinstance(exchange, str) or exchange not in VNExchangeBand.__members__:
        raise UnknownExchangeError(f"{where}exchange {exchange!r} is not a VN exchange; expected one of {names}")
    return exchange


def _vn_reference(reference_price: Optional[float], where: str = "") -> float:
    """Return `reference_price` if it is a positive finite number; otherwise raise TickValidationError."""
    if reference_price is None or not _is_finite_number(reference_price) or reference_price <= 0:
        raise TickValidationError(f"{where}reference_price must be a positive finite number, got {reference_price!r}")
    return reference_price


def _limits_pair(where: str, limits: Any) -> Tuple[Any, Any]:
    """Unpack `price_limits` into (floor, ceiling); anything other than a pair raises TickValidationError."""
    try:
        floor, ceiling = limits
    except (TypeError, ValueError) as err:
        raise TickValidationError(f"{where}price_limits must be a (floor, ceiling) pair, got {limits!r}") from err
    return floor, ceiling


def vn_tick_size(exchange: str, price: float) -> float:
    """Tick size in VND of a stock or fund certificate (not an ETF or a warrant) on `exchange` at `price`.

    HOSE: 10 below 10,000, 50 from 10,000 to 49,950, 100 from 50,000; HNX and UPCOM: 100 (VERIFIED, AGENTS.md
    "Verified Facts", VN price limits). An exchange that `VNExchangeBand` does not list raises UnknownExchangeError.
    """
    if _vn_exchange(exchange) == VNExchangeBand.HOSE.name:
        if price < 10_000:
            return 10.0
        if price < 50_000:
            return 50.0
    return 100.0


def vn_price_limits(exchange: str, reference_price: float) -> Tuple[float, float]:
    """(floor, ceiling) of the normal daily band around `reference_price`, not rounded to the tick.

    The band is `VNExchangeBand[exchange]` (HOSE 7%, HNX 10%, UPCOM 15%). The exchange itself rounds the ceiling down
    and the floor up to the tick, and moves a limit that equals the reference by one tick; `MarketTick` allows for
    that with a one-tick tolerance. Special sessions have wider bands: give `MarketTick` their `price_limits`.
    An unlisted exchange raises UnknownExchangeError; a reference that is not a positive number raises
    TickValidationError.
    """
    band = VNExchangeBand[_vn_exchange(exchange)].value
    reference = _vn_reference(reference_price)
    return reference * (1.0 - band), reference * (1.0 + band)


@dataclass(frozen=True)
class MarketTick:
    """One trade with its quote context. `__post_init__` only validates; connectors normalise (e.g. upper-case).

    VN prices are full VND at the domain boundary (decision D4, 2026-10-21). `price_limits` is (floor, ceiling) from
    the feed (SSI `Floor`/`Ceiling`) or from the caller for a special session (first trading day, resumption after 25+
    sessions, some ex-rights days, the 2026-12-28 HNX-to-HOSE transfer); `vn_ceiling_floor_proximity` returns
    (ceiling, floor). Checks, in order (every failure raises TickValidationError or a subclass):
    0. every market: each number field, plus `reference_price` and both `price_limits` values when given, is finite;
    1. `price_limits` is allowed only on a VN tick;
    2. a VN tick needs an exchange that `VNExchangeBand` lists (UnknownExchangeError) and a positive
       `reference_price`. With `price_limits` (0 < floor <= ceiling) the price must lie in [floor, ceiling]; without
       them, in the `vn_price_limits` band widened on each side by one tick (`vn_tick_size` at the ceiling, the
       larger tick). A price outside raises PriceOutOfBandError.
    Bid/ask prices and sizes are not checked: 0 means no active bid or ask. A US tick needs no exchange.
    Known limit: the check assumes full VND. A tick whose price and reference are both in thousands of VND passes
    (HOSE reference_price=5.0, price=14.0 passes, because the one-tick tolerance is wider than the band); unit
    checks are out of scope.
    """

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
    exchange: Optional[str] = None
    foreign_buy_volume: float = 0.0
    foreign_sell_volume: float = 0.0
    price_limits: Optional[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        where = f"tick {self.symbol!r}: "
        # 0. Every market, before any other check: NaN fails every comparison below and would pass silently.
        for name in _FINITE_TICK_FIELDS:
            _require_finite(where, name, getattr(self, name))
        if self.reference_price is not None:
            _require_finite(where, "reference_price", self.reference_price)
        if self.price_limits is not None:
            floor, ceiling = _limits_pair(where, self.price_limits)
            _require_finite(where, "price_limits floor", floor)
            _require_finite(where, "price_limits ceiling", ceiling)
        # 1. Limits belong to VN ticks only.
        if self.price_limits is not None and self.market != MarketRegion.VN:
            raise TickValidationError(f"{where}price_limits {self.price_limits!r} are allowed only on a VN tick")
        # 2. VN exchange, reference price and price range.
        if self.market == MarketRegion.VN:
            self._check_vn_price(where)

    def _check_vn_price(self, where: str) -> None:
        exchange = _vn_exchange(self.exchange, where)
        reference = _vn_reference(self.reference_price, where)
        if self.price_limits is not None:  # strict: the limits come from the feed or the caller
            floor, ceiling = self.price_limits
            if not 0 < floor <= ceiling:
                raise TickValidationError(
                    f"{where}price_limits {self.price_limits!r} must be (floor, ceiling) with 0 < floor <= ceiling"
                )
            if not floor <= self.price <= ceiling:
                raise PriceOutOfBandError(f"{where}price {self.price!r} is outside price_limits [{floor!r}, {ceiling!r}]")
            return
        floor, ceiling = vn_price_limits(exchange, reference)
        tick = vn_tick_size(exchange, ceiling)
        low, high = floor - tick, ceiling + tick
        if not low <= self.price <= high:
            raise PriceOutOfBandError(
                f"{where}price {self.price!r} is outside the allowed range [{low:.2f}, {high:.2f}] for {exchange} "
                f"(+/-{VNExchangeBand[exchange].value:.0%} around reference_price {reference!r}, widened by one "
                f"{tick:g} VND tick); pass price_limits=(floor, ceiling) for a special session (first trading day, "
                "resumption, ex-rights day, exchange transfer)"
            )

    @property
    def spread_bps(self) -> float:
        mid = 0.5 * (self.bid_price + self.ask_price)
        return ((self.ask_price - self.bid_price) / mid) * 10_000.0 if mid > 0 else 0.0

    @property
    def vn_ceiling_floor_proximity(self) -> Optional[Tuple[float, float]]:
        """(ceiling, floor) of a VN tick, None for any other market; note `price_limits` is (floor, ceiling).

        From `price_limits` when given; otherwise the normal band around `reference_price`, rounded to 2 dp (not to
        the tick). `__post_init__` has already validated the exchange and the reference; the helpers narrow types.
        """
        if self.market != MarketRegion.VN:
            return None
        if self.price_limits is not None:
            return self.price_limits[1], self.price_limits[0]
        floor, ceiling = vn_price_limits(_vn_exchange(self.exchange), _vn_reference(self.reference_price))
        return round(ceiling, 2), round(floor, 2)


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


class IssuerType(str, Enum):
    """Kind of issuer behind a statement; the forensic scores apply only to NON_FINANCIAL (AGENTS.md "Forensics")."""

    NON_FINANCIAL = "NON_FINANCIAL"
    BANK = "BANK"
    INSURER = "INSURER"
    SECURITIES_FIRM = "SECURITIES_FIRM"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FinancialStatementSnapshot:
    """One period's statement lines and the previous period's, as `ForensicAccountingEngine` reads them.

    `issuer_type` gates the scores: only NON_FINANCIAL is scored, and the default UNKNOWN is refused. A `market_cap`
    of 0 or less (0 also stands for a missing market cap) leaves the Altman Z-Score undefined.
    """

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
    issuer_type: IssuerType = IssuerType.UNKNOWN
