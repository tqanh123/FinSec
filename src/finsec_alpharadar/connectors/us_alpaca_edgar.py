from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from finsec_alpharadar.domain.models import MarketRegion, MarketTick, RegulatoryFiling


class AlpacaStreamAdapter:
    """
    Normalizes Alpaca Market Data v2 WebSocket payloads (`wss://stream.data.alpaca.markets/v2/iex`)
    into unified `MarketTick` objects.
    """

    WS_ENDPOINT = "wss://stream.data.alpaca.markets/v2/iex"

    @staticmethod
    def parse_trade_and_quote(trade_msg: Dict[str, Any], quote_msg: Dict[str, Any]) -> MarketTick:
        ts_str = trade_msg.get("t")
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() if ts_str else datetime.now(timezone.utc).timestamp()
        return MarketTick(
            symbol=str(trade_msg["S"]).upper(),
            market=MarketRegion.US,
            timestamp=ts,
            price=float(trade_msg["p"]),
            volume=float(trade_msg["s"]),
            bid_price=float(quote_msg.get("bp", trade_msg["p"])),
            ask_price=float(quote_msg.get("ap", trade_msg["p"])),
            bid_size=float(quote_msg.get("bs", 100.0)),
            ask_size=float(quote_msg.get("as", 100.0)),
            exchange=str(trade_msg.get("x", "IEX")),
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
