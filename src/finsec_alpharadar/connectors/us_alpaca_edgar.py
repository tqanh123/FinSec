from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from finsec_alpharadar.connectors.errors import PayloadFieldError, _required_number, _required_text
from finsec_alpharadar.domain.models import MarketRegion, MarketTick, RegulatoryFiling

_SOURCE = "alpaca"


def _event_time(msg: Dict[str, Any], kind: str, symbol: str) -> datetime:
    """Parse the `t` of a trade or quote message into an aware datetime; a value without a UTC offset raises."""
    field = f"{kind}.t"
    raw = _required_text(_SOURCE, symbol, field, msg, "t")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as err:
        raise PayloadFieldError(_SOURCE, symbol, field, f"is not an RFC-3339 timestamp: {raw!r}") from err
    if parsed.tzinfo is None:
        raise PayloadFieldError(_SOURCE, symbol, field, f"timestamp has no UTC offset: {raw!r}")
    return parsed


class AlpacaStreamAdapter:
    """
    Parses one Alpaca Market Data v2 stream trade message and its quote message into a `MarketTick`.

    Payload parser only, no network client. Required: trade `S, p, s, t, x`; quote `S, t, bp, ap, bs, as`. The
    stream docs list no required fields, so that Alpaca always sends these is [ASSUMED] (research 2026-10-21).
    A missing, null or malformed field raises `PayloadFieldError`; domain errors (`TickValidationError`) propagate.
    - `t` is RFC-3339 with 0-9 fractional digits and "Z"
      (VERIFIED https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data). `datetime.fromisoformat` parses
      it on Python >= 3.11 and keeps microseconds; a value without a UTC offset raises.
    - The quote must carry the trade's `S` and a `t` at or before the trade's `t` (no look-ahead), compared at
      microsecond resolution.
    - `x` is kept raw: `V` is IEX (VERIFIED https://docs.alpaca.markets/us/reference/stockmetaexchanges-1).
    - `bp`/`ap` = 0 means no active bid/ask and is accepted. `bs`/`as` are shares since 2025-11-03 for SIP quotes
      (VERIFIED https://docs.alpaca.markets/us/changelog/marketdata-bid-and-ask-size-display-change); the unit of
      the IEX feed is [ASSUMED].
    """

    WS_ENDPOINT = "wss://stream.data.alpaca.markets/v2/iex"

    @staticmethod
    def parse_trade_and_quote(trade_msg: Dict[str, Any], quote_msg: Dict[str, Any]) -> MarketTick:
        trade_symbol = _required_text(_SOURCE, None, "trade.S", trade_msg, "S")
        symbol = trade_symbol.upper()
        price = _required_number(_SOURCE, symbol, "trade.p", trade_msg, "p")
        volume = _required_number(_SOURCE, symbol, "trade.s", trade_msg, "s")
        trade_time = _event_time(trade_msg, "trade", symbol)
        exchange = _required_text(_SOURCE, symbol, "trade.x", trade_msg, "x")
        quote_symbol = _required_text(_SOURCE, symbol, "quote.S", quote_msg, "S")
        if quote_symbol != trade_symbol:
            raise PayloadFieldError(
                _SOURCE, symbol, "quote.S", f"is {quote_symbol!r}, not the trade's symbol {trade_symbol!r}"
            )
        quote_time = _event_time(quote_msg, "quote", symbol)
        if quote_time > trade_time:
            raise PayloadFieldError(
                _SOURCE,
                symbol,
                "quote.t",
                f"is {quote_time.isoformat()}, after the trade at {trade_time.isoformat()} (look-ahead)",
            )
        bid_price = _required_number(_SOURCE, symbol, "quote.bp", quote_msg, "bp")
        ask_price = _required_number(_SOURCE, symbol, "quote.ap", quote_msg, "ap")
        bid_size = _required_number(_SOURCE, symbol, "quote.bs", quote_msg, "bs")
        ask_size = _required_number(_SOURCE, symbol, "quote.as", quote_msg, "as")
        return MarketTick(
            symbol=symbol,
            market=MarketRegion.US,
            timestamp=trade_time.timestamp(),
            price=price,
            volume=volume,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            exchange=exchange,
        )


class SecEdgarFilingAdapter:
    """
    Adapter for SEC EDGAR real-time filings (`8-K`, `10-Q`, `10-K`, `Form 4`)
    compatible with `edgartools` and `sec-edgar-mcp`.
    """

    @staticmethod
    def normalize_filing(raw: Dict[str, Any]) -> RegulatoryFiling:
        return RegulatoryFiling(
            filing_id=str(raw["accession_number"]),
            symbol=str(raw["ticker"]).upper(),
            market=MarketRegion.US,
            form_type=str(raw["form_type"]),
            published_at=float(raw["published_at"]),
            title=str(raw.get("title", "")),
            section=str(raw.get("section", "Full Filing")),
            text_content=str(raw["text_content"]),
            citation_url=str(raw["citation_url"]),
            xbrl_metrics=dict(raw.get("xbrl_metrics", {})),
        )
