"""VN tick validation (decision D4, AGENTS.md "Decisions" 2026-10-21) and the finite-number check for every tick.

Pure unit tests of `finsec_alpharadar.domain`: no network, no clock. Prices are full VND. No test asserts behaviour
exactly at a widened edge: a real price lies at least one tick inside it, and float products such as 10_000 * 1.07
are not exact.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pytest

from finsec_alpharadar.domain import (
    MarketRegion,
    MarketTick,
    PriceOutOfBandError,
    TickValidationError,
    UnknownExchangeError,
    vn_price_limits,
    vn_tick_size,
)

TS = 1_792_211_400.0  # 2026-10-17T04:30:00Z, a fixed time instead of a clock read
NAN = float("nan")
INF = float("inf")


def _vn_tick(**overrides: Any) -> MarketTick:
    """A valid HOSE tick (reference 10,000 VND, price 10,000); each test overrides only the fields it is about."""
    fields: Dict[str, Any] = {
        "symbol": "TSTV",
        "market": MarketRegion.VN,
        "timestamp": TS,
        "price": 10_000.0,
        "volume": 1_000.0,
        "bid_price": 9_950.0,
        "ask_price": 10_050.0,
        "bid_size": 500.0,
        "ask_size": 700.0,
        "reference_price": 10_000.0,
        "exchange": "HOSE",
    }
    fields.update(overrides)
    return MarketTick(**fields)


def _us_tick(**overrides: Any) -> MarketTick:
    """A valid US tick with no exchange and no reference price."""
    fields: Dict[str, Any] = {
        "symbol": "TSTU",
        "market": MarketRegion.US,
        "timestamp": TS,
        "price": 189.5,
        "volume": 2_500.0,
        "bid_price": 189.48,
        "ask_price": 189.52,
        "bid_size": 1_200.0,
        "ask_size": 800.0,
    }
    fields.update(overrides)
    return MarketTick(**fields)


def test_unknown_vn_exchange_raises() -> None:
    with pytest.raises(UnknownExchangeError, match="XYZ"):
        _vn_tick(exchange="XYZ")


def test_missing_vn_exchange_raises() -> None:
    with pytest.raises(UnknownExchangeError, match="exchange is missing"):
        _vn_tick(exchange=None)


@pytest.mark.parametrize("reference_price", [None, 0.0, -1.0])
def test_vn_reference_price_required(reference_price: Optional[float]) -> None:
    with pytest.raises(TickValidationError, match=r"\breference_price\b") as excinfo:
        _vn_tick(reference_price=reference_price)
    assert type(excinfo.value) is TickValidationError


@pytest.mark.parametrize(
    ("exchange", "price"),
    [("HOSE", 10_700.0), ("HNX", 11_000.0), ("UPCOM", 11_500.0)],
)
def test_price_inside_normal_band_passes(exchange: str, price: float) -> None:
    tick = _vn_tick(exchange=exchange, price=price)
    assert tick.price == price


@pytest.mark.parametrize(
    ("exchange", "price"),
    [
        ("HOSE", 10_800.0),  # ceiling 10,700 plus one 50 VND tick = 10,750
        ("HOSE", 9_200.0),  # floor 9,300 minus one 50 VND tick = 9,250
        ("HNX", 11_200.0),  # 11,000 + 100 = 11,100
        ("UPCOM", 11_700.0),  # 11,500 + 100 = 11,600
    ],
)
def test_price_outside_normal_band_raises(exchange: str, price: float) -> None:
    with pytest.raises(PriceOutOfBandError) as excinfo:
        _vn_tick(exchange=exchange, price=price)
    message = str(excinfo.value)
    assert repr(price) in message
    assert "price_limits" in message, "the message must point to price_limits for special sessions"


@pytest.mark.parametrize(
    ("exchange", "reference_price", "price"),
    [
        # Real limits after rounding to the tick and the one-tick bump: UPCOM 500 -> 400 / 600, HOSE 100 -> 90 / 110.
        ("UPCOM", 500.0, 600.0),
        ("UPCOM", 500.0, 400.0),
        ("HOSE", 100.0, 110.0),
        ("HOSE", 100.0, 90.0),
    ],
)
def test_low_price_one_tick_bump_passes(exchange: str, reference_price: float, price: float) -> None:
    tick = _vn_tick(exchange=exchange, reference_price=reference_price, price=price)
    assert tick.price == price


def test_low_price_beyond_tolerance_raises() -> None:
    with pytest.raises(PriceOutOfBandError):  # UPCOM 500: 575 + one 100 tick = 675 < 700
        _vn_tick(exchange="UPCOM", reference_price=500.0, price=700.0)


@pytest.mark.parametrize(
    ("reference_price", "price", "passes"),
    [
        # The tolerance is the tick AT THE CEILING (D4), not at the reference, the floor or the price.
        (9_500.0, 10_200.0, True),  # ceiling 10,165 sits in the 50 VND tier: 10,215 is the limit (10 VND would give 10,175)
        (9_500.0, 10_250.0, False),  # beyond 10,165 + 50
        (10_500.0, 9_720.0, True),  # floor 9,765 - 50 = 9,715 (the 10 VND tick of a 9,720 price would give 9,755)
    ],
)
def test_tolerance_uses_ceiling_tick(reference_price: float, price: float, passes: bool) -> None:
    if passes:
        assert _vn_tick(reference_price=reference_price, price=price).price == price
    else:
        with pytest.raises(PriceOutOfBandError):
            _vn_tick(reference_price=reference_price, price=price)


def test_price_limits_allow_special_session() -> None:
    tick = _vn_tick(price=12_000.0, price_limits=(8_000.0, 12_000.0))
    assert tick.price_limits == (8_000.0, 12_000.0)
    with pytest.raises(PriceOutOfBandError):  # the same price without the override is outside the normal band
        _vn_tick(price=12_000.0)


def test_price_outside_given_limits_raises() -> None:
    with pytest.raises(PriceOutOfBandError, match="12050"):  # strict: no tick tolerance around given limits
        _vn_tick(price=12_050.0, price_limits=(8_000.0, 12_000.0))


@pytest.mark.parametrize("price_limits", [(12_000.0, 8_000.0), (0.0, 12_000.0), (-1.0, 5.0)])
def test_invalid_price_limits_raise(price_limits: Tuple[float, float]) -> None:
    with pytest.raises(TickValidationError, match=r"\bprice_limits\b") as excinfo:
        _vn_tick(price_limits=price_limits)
    assert type(excinfo.value) is TickValidationError, "invalid limits are not a price-out-of-band case"


def test_us_tick_without_exchange_is_valid() -> None:
    tick = _us_tick()
    assert tick.market == MarketRegion.US
    assert tick.vn_ceiling_floor_proximity is None


def test_us_tick_with_price_limits_raises() -> None:
    with pytest.raises(TickValidationError, match=r"\bprice_limits\b"):
        _us_tick(price_limits=(180.0, 200.0))


def test_bid_ask_zero_is_not_validated() -> None:
    tick = _vn_tick(bid_price=0.0, ask_size=0.0)  # 0 means no active bid; a one-sided book is valid
    assert tick.bid_price == 0.0
    assert tick.ask_size == 0.0


def test_ceiling_floor_proximity_order() -> None:
    # price_limits is (floor, ceiling); the property returns (ceiling, floor).
    assert _vn_tick(price_limits=(8_000.0, 12_000.0)).vn_ceiling_floor_proximity == (12_000.0, 8_000.0)
    assert _vn_tick().vn_ceiling_floor_proximity == (10_700.0, 9_300.0)


@pytest.mark.parametrize(
    ("exchange", "price", "expected"),
    [
        ("HOSE", 9_990.0, 10.0),
        ("HOSE", 10_000.0, 50.0),
        ("HOSE", 49_950.0, 50.0),
        ("HOSE", 50_000.0, 100.0),
        ("HNX", 5_000.0, 100.0),
        ("UPCOM", 5_000.0, 100.0),
        ("XYZ", 5_000.0, None),  # None: the exchange is unknown and the call raises
    ],
)
def test_vn_tick_size_table(exchange: str, price: float, expected: Optional[float]) -> None:
    if expected is None:
        with pytest.raises(UnknownExchangeError):
            vn_tick_size(exchange, price)
    else:
        assert vn_tick_size(exchange, price) == expected


def test_vn_price_limits_return_unrounded_floor_then_ceiling() -> None:
    floor, ceiling = vn_price_limits("HNX", 10_000.0)
    assert (floor, ceiling) == (pytest.approx(9_000.0), pytest.approx(11_000.0))
    floor, ceiling = vn_price_limits("HOSE", 25_350.0)  # the exchange's own limits are 23,600 / 27,100 after rounding
    assert (floor, ceiling) == (pytest.approx(23_575.5), pytest.approx(27_124.5))
    with pytest.raises(UnknownExchangeError):
        vn_price_limits("XYZ", 10_000.0)
    with pytest.raises(TickValidationError, match=r"\breference_price\b"):
        vn_price_limits("HOSE", 0.0)


@pytest.mark.parametrize(
    ("market", "overrides", "field"),
    [
        ("US", {"price": NAN}, "price"),
        ("US", {"price": INF}, "price"),
        ("US", {"bid_size": NAN}, "bid_size"),
        ("US", {"timestamp": NAN}, "timestamp"),
        ("VN", {"price": INF, "reference_price": INF}, "price"),
        ("VN", {"price_limits": (1.0, INF)}, "price_limits"),
    ],
    ids=["us-price-nan", "us-price-inf", "us-bid_size-nan", "us-timestamp-nan", "hose-price-ref-inf", "hose-limits-inf"],
)
def test_non_finite_numbers_raise(market: str, overrides: Dict[str, Any], field: str) -> None:
    build = _us_tick if market == "US" else _vn_tick
    with pytest.raises(TickValidationError, match=rf"\b{field}\b"):
        build(**overrides)


FINITE_FIELDS = [
    "timestamp",
    "price",
    "volume",
    "bid_price",
    "ask_price",
    "bid_size",
    "ask_size",
    "foreign_buy_volume",
    "foreign_sell_volume",
]


@pytest.mark.parametrize("value", [NAN, INF, -INF], ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize("field", FINITE_FIELDS)
def test_every_number_field_must_be_finite(field: str, value: float) -> None:
    # A NaN foreign volume would poison the shared engine's foreign-flow window for 50 ticks (red-team r1 RT1).
    with pytest.raises(TickValidationError, match=rf"\b{field}\b"):
        _us_tick(**{field: value})


def test_non_finite_reference_and_limits_raise() -> None:
    with pytest.raises(TickValidationError, match=r"\breference_price\b"):
        _us_tick(reference_price=NAN)  # checked on every market, not only VN
    with pytest.raises(TickValidationError, match=r"\bprice_limits floor\b"):
        _vn_tick(price_limits=(NAN, 12_000.0))


@pytest.mark.parametrize(("field", "value"), [("price", "189.5"), ("volume", None), ("price", 10**400)])
def test_non_numeric_numbers_raise(field: str, value: Any) -> None:
    with pytest.raises(TickValidationError, match=rf"\b{field}\b"):
        _us_tick(**{field: value})
