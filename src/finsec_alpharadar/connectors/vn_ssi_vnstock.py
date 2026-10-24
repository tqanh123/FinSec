from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from finsec_alpharadar.connectors.errors import PayloadFieldError, _required_number, _required_text
from finsec_alpharadar.domain.models import MarketRegion, MarketTick, RegulatoryFiling

_SOURCE = "ssi"


def _price_limits(msg: Dict[str, Any], symbol: str) -> Optional[Tuple[float, float]]:
    """(Floor, Ceiling) as `price_limits` when both are present, None when neither is; only one of them raises."""
    has_ceiling, has_floor = "Ceiling" in msg, "Floor" in msg
    if has_ceiling != has_floor:
        missing = "Floor" if has_ceiling else "Ceiling"
        raise PayloadFieldError(_SOURCE, symbol, missing, "is missing: Ceiling and Floor come together")
    if not has_ceiling:
        return None
    return _required_number(_SOURCE, symbol, "Floor", msg), _required_number(_SOURCE, symbol, "Ceiling", msg)


class SSIFastConnectAdapter:
    """
    Parses one SSI FastConnect Data tick message for a HOSE, HNX or UPCOM stock into a `MarketTick`.

    Payload parser only, no network client: FC Data only, and no SSI client before Phase 6 (owner decision 9.5).
    Field names are [ASSUMED] until the Phase 6 R-channel spec, except `Ceiling`, `Floor` and `RefPrice`, which
    X-TRADE messages carry (VERIFIED https://guide.ssi.com.vn/ssi-products/fastconnect-data/streaming-data.md).
    `Timestamp` is this stub's epoch float [ASSUMED]. Prices are full VND, as in the SSI doc samples; nothing is
    converted. Required: `Symbol`, `LastPrice`, `LastVol`, `BidPrice1`, `AskPrice1`, `BidVol1`, `AskVol1`,
    `RefPrice`, `Exchange`, `Timestamp`; a missing, null or malformed one raises `PayloadFieldError`. `Ceiling` and
    `Floor` together become `price_limits` (floor, ceiling); only one of them raises. Domain errors such as
    `UnknownExchangeError` propagate unwrapped. Absent foreign volumes still read as 0.0 (fixed in Phase 6).
    """

    STREAM_URL = "https://fc-datahub.ssi.com.vn/v2.0"  # VERIFIED, AGENTS.md "Verified Facts"
    REST_URL = "https://fc-data.ssi.com.vn/v2.0/Market"  # VERIFIED, AGENTS.md "Verified Facts"

    @staticmethod
    def parse_market_data_message(msg: Dict[str, Any]) -> MarketTick:
        symbol = _required_text(_SOURCE, None, "Symbol", msg).upper()
        price = _required_number(_SOURCE, symbol, "LastPrice", msg)
        volume = _required_number(_SOURCE, symbol, "LastVol", msg)
        bid_price = _required_number(_SOURCE, symbol, "BidPrice1", msg)
        ask_price = _required_number(_SOURCE, symbol, "AskPrice1", msg)
        bid_size = _required_number(_SOURCE, symbol, "BidVol1", msg)
        ask_size = _required_number(_SOURCE, symbol, "AskVol1", msg)
        reference_price = _required_number(_SOURCE, symbol, "RefPrice", msg)
        exchange = _required_text(_SOURCE, symbol, "Exchange", msg).upper()
        timestamp = _required_number(_SOURCE, symbol, "Timestamp", msg)
        price_limits = _price_limits(msg, symbol)
        foreign_buy_volume = float(msg.get("ForeignBuyVolTotal", 0.0))
        foreign_sell_volume = float(msg.get("ForeignSellVolTotal", 0.0))
        return MarketTick(
            symbol=symbol,
            market=MarketRegion.VN,
            timestamp=timestamp,
            price=price,
            volume=volume,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            reference_price=reference_price,
            exchange=exchange,
            foreign_buy_volume=foreign_buy_volume,
            foreign_sell_volume=foreign_sell_volume,
            price_limits=price_limits,
        )


class VnstockBCTCAdapter:
    """
    Adapter for Vietnam listed company financial disclosures (BCTC Hop Nhat Quy/Nam,
    Nghi quyet HDQT, Cong bo thong tin bat thuong) via `vnstock` (VCI/TCBS sources).
    """

    @staticmethod
    def normalize_disclosure(raw: Dict[str, Any]) -> RegulatoryFiling:
        return RegulatoryFiling(
            filing_id=str(raw["doc_id"]),
            symbol=str(raw["symbol"]).upper(),
            market=MarketRegion.VN,
            form_type=str(raw.get("form_type", "BCTC_QUY")),
            published_at=float(raw["published_at"]),
            title=str(raw["title"]),
            section=str(raw.get("section", "Thuyet minh BCTC")),
            text_content=str(raw["text_content"]),
            citation_url=str(raw["citation_url"]),
            xbrl_metrics=dict(raw.get("financial_ratios", {})),
        )
