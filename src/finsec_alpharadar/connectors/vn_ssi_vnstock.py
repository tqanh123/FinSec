from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from finsec_alpharadar.domain.models import MarketRegion, MarketTick, RegulatoryFiling


class SSIFastConnectAdapter:
    """
    Normalizes SSI FastConnect Data WebSocket (`wss://stream.ssi.com.vn`) and
    DNSE Lightspeed MQTT tick payloads for HOSE, HNX, and UPCOM equities.
    """

    WS_ENDPOINT = "wss://stream.ssi.com.vn"

    @staticmethod
    def parse_market_data_message(msg: Dict[str, Any]) -> MarketTick:
        return MarketTick(
            symbol=str(msg["Symbol"]).upper(),
            market=MarketRegion.VN,
            timestamp=float(msg.get("Timestamp", datetime.now(timezone.utc).timestamp())),
            price=float(msg["LastPrice"]),
            volume=float(msg["LastVol"]),
            bid_price=float(msg.get("BidPrice1", msg["LastPrice"])),
            ask_price=float(msg.get("AskPrice1", msg["LastPrice"])),
            bid_size=float(msg.get("BidVol1", 100.0)),
            ask_size=float(msg.get("AskVol1", 100.0)),
            reference_price=float(msg["RefPrice"]) if "RefPrice" in msg else None,
            exchange=str(msg.get("Exchange", "HOSE")).upper(),
            foreign_buy_volume=float(msg.get("ForeignBuyVolTotal", 0.0)),
            foreign_sell_volume=float(msg.get("ForeignSellVolTotal", 0.0)),
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
