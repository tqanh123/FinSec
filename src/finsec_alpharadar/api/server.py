from __future__ import annotations

from typing import Any, Dict, Optional
from fastapi import FastAPI, Query

from finsec_alpharadar.domain.models import MarketRegion, MarketTick
from finsec_alpharadar.quant.kalman_microstructure import StreamingQuantEngine
from finsec_alpharadar.rag.hybrid_temporal_rag import HybridTemporalRAGEngine
from finsec_alpharadar.router.linucb_bandit import LinUCBModelRouter

app = FastAPI(
    title="FinSec AlphaRadar Dual-Market API (US SEC/Alpaca & Vietnam HOSE/HNX)",
    version="0.1.0",
)

quant_engine = StreamingQuantEngine(window_size=50)
rag_engine = HybridTemporalRAGEngine()
bandit_router = LinUCBModelRouter(
    arm_names=["finsec-qwen3-1.7b-qlora", "gemini-3.6-flash", "gemini-4-argon-pro"]
)


@app.get("/health")
def health_check() -> Dict[str, str]:
    return {"status": "ok", "markets": "US,VN"}


@app.post("/v1/ticks/ingest")
def ingest_tick(payload: Dict[str, Any]) -> Dict[str, Any]:
    tick = MarketTick(
        symbol=str(payload["symbol"]).upper(),
        market=MarketRegion(payload.get("market", "US")),
        timestamp=float(payload["timestamp"]),
        price=float(payload["price"]),
        volume=float(payload["volume"]),
        bid_price=float(payload.get("bid_price", payload["price"])),
        ask_price=float(payload.get("ask_price", payload["price"])),
        bid_size=float(payload.get("bid_size", 100.0)),
        ask_size=float(payload.get("ask_size", 100.0)),
        reference_price=float(payload["reference_price"]) if "reference_price" in payload else None,
        exchange=str(payload.get("exchange", "IEX")),
        foreign_buy_volume=float(payload.get("foreign_buy_volume", 0.0)),
        foreign_sell_volume=float(payload.get("foreign_sell_volume", 0.0)),
    )
    report = quant_engine.process_tick(tick)
    return {
        "signal": report.__dict__,
        "vn_ceiling_floor": tick.vn_ceiling_floor_proximity,
    }


@app.get("/v1/rag/search")
def search_disclosures(
    q: str = Query(..., description="Search query over SEC 8-K/10-Q and Vietnam BCTC"),
    market: Optional[MarketRegion] = Query(None),
    symbol: Optional[str] = Query(None),
    top_k: int = Query(5, ge=1, le=20),
) -> Dict[str, Any]:
    ctx = bandit_router.extract_context_vector(q, requires_xbrl_forensics=True, is_realtime_spike=False)
    model_arm, ucb_scores = bandit_router.select_model(ctx)
    passages = rag_engine.search(q, top_k=top_k, market_filter=market, symbol_filter=symbol)
    return {
        "routed_model": model_arm,
        "router_ucb_scores": ucb_scores,
        "results": [p.__dict__ for p in passages],
    }
