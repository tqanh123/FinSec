"""Strict payload parsing in the Alpaca and SSI adapters: required fields, no invented values, verified FC Data URLs.

Every test starts from a complete, valid payload and changes or drops exactly one field, so a test about one field
never fails because another one is missing. No network, no clock.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from finsec_alpharadar.connectors import AlpacaStreamAdapter, PayloadFieldError, SSIFastConnectAdapter
from finsec_alpharadar.domain import MarketRegion, MarketTick, TickValidationError, UnknownExchangeError

TRADE_T = "2026-10-17T04:30:00.123456789Z"  # nanosecond precision, as Alpaca sends it
QUOTE_T = "2026-10-17T04:29:59.950000Z"  # before the trade


def _trade() -> Dict[str, Any]:
    return {"S": "NVDA", "p": 189.50, "s": 2500, "x": "V", "t": TRADE_T}


def _quote() -> Dict[str, Any]:
    return {"S": "NVDA", "t": QUOTE_T, "bp": 189.48, "ap": 189.52, "bs": 1200, "as": 800}


def _ssi() -> Dict[str, Any]:
    return {
        "Symbol": "FPT",
        "LastPrice": 142_500.0,
        "LastVol": 45_000,
        "BidPrice1": 142_400.0,
        "AskPrice1": 142_600.0,
        "BidVol1": 90_000,
        "AskVol1": 15_000,
        "RefPrice": 138_000.0,
        "Exchange": "HOSE",
        "Timestamp": 1_792_211_400.0,
    }


def _without(msg: Dict[str, Any], key: str) -> Dict[str, Any]:
    return {name: value for name, value in msg.items() if name != key}


def _parse_with(source: str, key: str, value: Any) -> MarketTick:
    """Parse the valid payload of `source` ("alpaca" trade or "ssi") with one field replaced."""
    if source == "alpaca":
        return AlpacaStreamAdapter.parse_trade_and_quote({**_trade(), key: value}, _quote())
    return SSIFastConnectAdapter.parse_market_data_message({**_ssi(), key: value})


ALPACA_REQUIRED: List[Tuple[str, str]] = [
    ("trade", "S"),
    ("trade", "p"),
    ("trade", "s"),
    ("trade", "t"),
    ("trade", "x"),
    ("quote", "S"),
    ("quote", "t"),
    ("quote", "bp"),
    ("quote", "ap"),
    ("quote", "bs"),
    ("quote", "as"),
]
SSI_REQUIRED: List[str] = [
    "Symbol",
    "LastPrice",
    "LastVol",
    "BidPrice1",
    "AskPrice1",
    "BidVol1",
    "AskVol1",
    "RefPrice",
    "Exchange",
    "Timestamp",
]


@pytest.mark.parametrize(("message", "key"), ALPACA_REQUIRED, ids=[f"{m}.{k}" for m, k in ALPACA_REQUIRED])
def test_alpaca_missing_required_field_raises(message: str, key: str) -> None:
    trade = _without(_trade(), key) if message == "trade" else _trade()
    quote = _without(_quote(), key) if message == "quote" else _quote()
    with pytest.raises(PayloadFieldError) as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote(trade, quote)
    assert excinfo.value.field == f"{message}.{key}"
    assert f"{message}.{key}" in str(excinfo.value)


def test_alpaca_quote_symbol_mismatch_raises() -> None:
    with pytest.raises(PayloadFieldError, match="AMD") as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote(_trade(), {**_quote(), "S": "AMD"})
    assert excinfo.value.field == "quote.S"


def test_alpaca_quote_after_trade_raises() -> None:
    late_quote = {**_quote(), "t": "2026-10-17T04:30:00.124456789Z"}  # 1 ms after the trade: look-ahead
    with pytest.raises(PayloadFieldError) as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote(_trade(), late_quote)
    assert excinfo.value.field == "quote.t"


def test_alpaca_quote_at_trade_time_is_accepted() -> None:
    tick = AlpacaStreamAdapter.parse_trade_and_quote(_trade(), {**_quote(), "t": TRADE_T})
    assert tick.timestamp == pytest.approx(1_792_211_400.123456, abs=1e-6)


def test_alpaca_whole_second_quote_is_accepted() -> None:
    # As strings "...04:30:00Z" sorts after "...04:30:00.123456789Z"; as instants the quote is earlier.
    tick = AlpacaStreamAdapter.parse_trade_and_quote(_trade(), {**_quote(), "t": "2026-10-17T04:30:00Z"})
    assert tick.timestamp == pytest.approx(1_792_211_400.123456, abs=1e-6)


def test_alpaca_offset_timestamps_compare_as_instants() -> None:
    earlier = {**_quote(), "t": "2026-10-17T11:29:59.950000+07:00"}  # 04:29:59.95 UTC
    assert AlpacaStreamAdapter.parse_trade_and_quote(_trade(), earlier).symbol == "NVDA"
    later = {**_quote(), "t": "2026-10-17T11:30:00.124456+07:00"}  # 04:30:00.124456 UTC, after the trade
    with pytest.raises(PayloadFieldError) as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote(_trade(), later)
    assert excinfo.value.field == "quote.t"


@pytest.mark.parametrize(("source", "key", "field"), [("alpaca", "p", "trade.p"), ("ssi", "LastVol", "LastVol")])
def test_huge_integer_number_raises(source: str, key: str, field: str) -> None:
    # JSON keeps a 400-digit integer literal as an exact int, and float() raises OverflowError on it.
    with pytest.raises(PayloadFieldError) as excinfo:
        _parse_with(source, key, 10**400)
    assert excinfo.value.field == field


def test_alpaca_nan_price_raises() -> None:
    # The domain error propagates as is; pytest.raises would not accept a PayloadFieldError here.
    with pytest.raises(TickValidationError, match=r"\bprice\b"):
        AlpacaStreamAdapter.parse_trade_and_quote({**_trade(), "p": float("nan")}, _quote())


def test_alpaca_nanosecond_z_timestamp() -> None:
    tick = AlpacaStreamAdapter.parse_trade_and_quote(_trade(), _quote())
    assert tick.market == MarketRegion.US
    assert tick.timestamp == pytest.approx(1_792_211_400.123456, abs=1e-6)


def test_alpaca_naive_timestamp_raises() -> None:
    with pytest.raises(PayloadFieldError, match="offset") as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote({**_trade(), "t": "2026-10-17T04:30:00.123456"}, _quote())
    assert excinfo.value.field == "trade.t"


def test_alpaca_non_string_timestamp_raises() -> None:
    with pytest.raises(PayloadFieldError) as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote({**_trade(), "t": 1_792_211_400}, _quote())
    assert excinfo.value.field == "trade.t"


def test_alpaca_malformed_price_raises() -> None:
    with pytest.raises(PayloadFieldError, match="n/a") as excinfo:
        AlpacaStreamAdapter.parse_trade_and_quote({**_trade(), "p": "n/a"}, _quote())
    assert excinfo.value.field == "trade.p"


def test_alpaca_keeps_raw_exchange_code() -> None:
    tick = AlpacaStreamAdapter.parse_trade_and_quote(_trade(), _quote())
    assert tick.exchange == "V"  # Alpaca's code for IEX, kept raw


def test_alpaca_one_sided_quote_is_accepted() -> None:
    tick = AlpacaStreamAdapter.parse_trade_and_quote(_trade(), {**_quote(), "bp": 0})  # 0 = no active bid
    assert tick.bid_price == 0.0
    assert tick.ask_price == pytest.approx(189.52)


@pytest.mark.parametrize("key", SSI_REQUIRED)
def test_ssi_missing_required_field_raises(key: str) -> None:
    with pytest.raises(PayloadFieldError) as excinfo:
        SSIFastConnectAdapter.parse_market_data_message(_without(_ssi(), key))
    assert excinfo.value.field == key
    assert key in str(excinfo.value)


def test_ssi_ceiling_floor_become_price_limits() -> None:
    tick = SSIFastConnectAdapter.parse_market_data_message({**_ssi(), "Ceiling": 147_600.0, "Floor": 128_400.0})
    assert tick.price_limits == (128_400.0, 147_600.0)  # (floor, ceiling)


@pytest.mark.parametrize(("present", "value", "absent"), [("Ceiling", 147_600.0, "Floor"), ("Floor", 128_400.0, "Ceiling")])
def test_ssi_single_limit_raises(present: str, value: float, absent: str) -> None:
    with pytest.raises(PayloadFieldError) as excinfo:
        SSIFastConnectAdapter.parse_market_data_message({**_ssi(), present: value})
    assert excinfo.value.field == absent


def test_ssi_unknown_exchange_propagates() -> None:
    # UnknownExchangeError is not a PayloadFieldError, so a wrapped error would fail this check.
    with pytest.raises(UnknownExchangeError, match="XYZ"):
        SSIFastConnectAdapter.parse_market_data_message({**_ssi(), "Exchange": "XYZ"})


def test_ssi_endpoints_are_verified() -> None:
    # VERIFIED in AGENTS.md "Verified Facts" (SSI FastConnect Data).
    assert SSIFastConnectAdapter.STREAM_URL == "https://fc-datahub.ssi.com.vn/v2.0"
    assert SSIFastConnectAdapter.REST_URL == "https://fc-data.ssi.com.vn/v2.0/Market"
    stale = sorted(
        name
        for name, value in vars(SSIFastConnectAdapter).items()
        if isinstance(value, str) and "stream.ssi.com.vn" in value
    )
    assert not stale, f"attributes still name the old, wrong SSI host: {stale}"


@pytest.mark.parametrize(
    ("source", "key", "field"),
    [("alpaca", "S", "trade.S"), ("alpaca", "x", "trade.x"), ("ssi", "Symbol", "Symbol"), ("ssi", "Exchange", "Exchange")],
)
def test_null_string_fields_raise(source: str, key: str, field: str) -> None:
    with pytest.raises(PayloadFieldError) as excinfo:
        _parse_with(source, key, None)
    assert excinfo.value.field == field
