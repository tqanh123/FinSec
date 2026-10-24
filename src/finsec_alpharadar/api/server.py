from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from fastapi import FastAPI, HTTPException, Query

from finsec_alpharadar.domain.models import MarketRegion, MarketTick
from finsec_alpharadar.quant.kalman_microstructure import StreamingQuantEngine
from finsec_alpharadar.rag.hybrid_temporal_rag import HybridTemporalRAGEngine, InvalidAsOfError
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

# Carried by every research response (AGENTS.md "Outputs"): Vietnamese first, then English; NFC text.
DISCLAIMER = "Chỉ dùng cho nghiên cứu, không phải khuyến nghị đầu tư. For research only; not investment advice."


def _number(name: str, value: Any) -> float:
    """One tick field as a float; null, a bool, a non-number or an int too large for a float raises ValueError."""
    try:
        if isinstance(value, bool):  # float(True) == 1.0: JSON true is not a number
            raise TypeError("a bool is not a number")
        return float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise ValueError(f"tick field {name!r} must be a number, got {value!r}") from err


def _tick_from_payload(payload: Dict[str, Any]) -> MarketTick:
    """A `MarketTick` from a request body: a missing required key raises KeyError, a bad value ValueError."""
    symbol = payload["symbol"]
    if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
        raise ValueError(f"tick field 'symbol' must be a non-empty string without surrounding spaces, got {symbol!r}")
    market = payload["market"]
    try:
        region = MarketRegion(market)
    except ValueError as err:
        allowed = ", ".join(member.value for member in MarketRegion)
        raise ValueError(f"tick field 'market' must be one of {allowed}, got {market!r}") from err
    exchange = payload.get("exchange")  # JSON null counts as absent: None, never the string "NONE"
    if exchange is not None and (not isinstance(exchange, str) or not exchange.strip() or exchange != exchange.strip()):
        raise ValueError(f"tick field 'exchange' must be a non-empty string without surrounding spaces, got {exchange!r}")
    reference_price = payload.get("reference_price")
    raw_limits = payload.get("price_limits")
    price_limits: Optional[Tuple[float, float]] = None
    if raw_limits is not None:
        if not isinstance(raw_limits, (list, tuple)) or len(raw_limits) != 2:
            raise ValueError(f"tick field 'price_limits' must be [floor, ceiling], got {raw_limits!r}")
        price_limits = (_number("price_limits floor", raw_limits[0]), _number("price_limits ceiling", raw_limits[1]))
    return MarketTick(
        symbol=symbol.upper(),
        market=region,
        timestamp=_number("timestamp", payload["timestamp"]),
        price=_number("price", payload["price"]),
        volume=_number("volume", payload["volume"]),
        bid_price=_number("bid_price", payload["bid_price"]),
        ask_price=_number("ask_price", payload["ask_price"]),
        bid_size=_number("bid_size", payload["bid_size"]),
        ask_size=_number("ask_size", payload["ask_size"]),
        reference_price=None if reference_price is None else _number("reference_price", reference_price),
        exchange=None if exchange is None else exchange.upper(),
        foreign_buy_volume=_number("foreign_buy_volume", payload.get("foreign_buy_volume", 0.0)),
        foreign_sell_volume=_number("foreign_sell_volume", payload.get("foreign_sell_volume", 0.0)),
        price_limits=price_limits,
    )


@app.get("/health")
def health_check() -> Dict[str, str]:
    return {"status": "ok", "markets": "US,VN"}


@app.post("/v1/ticks/ingest")
def ingest_tick(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one tick and feed it to the streaming quant engine.

    Required: symbol, market, timestamp, price, volume, bid_price, ask_price, bid_size, ask_size. Optional: exchange
    (upper-cased; JSON null counts as absent), reference_price, price_limits sent as [floor, ceiling], and
    foreign_buy_volume / foreign_sell_volume (default 0.0). A missing or invalid field, including a VN price outside
    its range, returns 422. The response's vn_ceiling_floor is [ceiling, floor], the reverse of price_limits.
    """
    try:
        tick = _tick_from_payload(payload)
    except KeyError as err:
        raise HTTPException(status_code=422, detail=f"missing required tick field: {err.args[0]}") from err
    except (TypeError, ValueError) as err:  # includes TickValidationError and its subclasses
        raise HTTPException(status_code=422, detail=str(err)) from err
    report = quant_engine.process_tick(tick)
    return {
        "signal": report.__dict__,
        "vn_ceiling_floor": tick.vn_ceiling_floor_proximity,
        "disclaimer": DISCLAIMER,
    }


@app.get("/v1/rag/search")
def search_disclosures(
    q: str = Query(..., description="Search query over SEC 8-K/10-Q and Vietnam BCTC"),
    market: Optional[MarketRegion] = Query(None),
    symbol: Optional[str] = Query(None),
    top_k: int = Query(5, ge=1, le=20),
    as_of: Optional[float] = Query(None, description="Decision time, epoch seconds UTC; default = request time"),
) -> Dict[str, Any]:
    """Search the filings published at or before the decision time and echo that time as "as_of".

    The decision time is `as_of`, or the time the request arrives when it is absent. This is the only clock read on
    the search path: the RAG engine never reads the clock. A non-finite as_of (nan, inf) returns 422.
    """
    decision_time = as_of if as_of is not None else datetime.now(timezone.utc).timestamp()
    ctx = bandit_router.extract_context_vector(q, requires_xbrl_forensics=True, is_realtime_spike=False)
    model_arm, ucb_scores = bandit_router.select_model(ctx)
    try:
        passages = rag_engine.search(q, top_k=top_k, market_filter=market, symbol_filter=symbol, as_of=decision_time)
    except InvalidAsOfError as err:  # a non-finite as_of (nan, inf); any other engine error is not the caller's
        raise HTTPException(status_code=422, detail=str(err)) from err
    return {
        "routed_model": model_arm,
        "router_ucb_scores": ucb_scores,
        "results": [p.__dict__ for p in passages],
        "as_of": decision_time,
        "disclaimer": DISCLAIMER,
    }
