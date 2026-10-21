"""`POST /v1/ticks/ingest` and `GET /v1/rag/search` handlers, called directly: no HTTP client, no network, no clock.

Needs fastapi: without it (the global Python on the dev machine) pytest skips this whole module. The handlers share
the module-level engines of `api/server.py`, so every test sends its own symbol. A direct call bypasses FastAPI's
query handling: always pass every search parameter (an omitted `Query(...)` parameter receives the `Query` object).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List

import pytest

fastapi = pytest.importorskip("fastapi")

# Both imports need fastapi, so they follow the skip check: without fastapi the module skips instead of failing.
from fastapi import HTTPException

from finsec_alpharadar.api import server

TS = 1_792_211_400.0  # 2026-10-17T04:30:00Z
REQUIRED: List[str] = ["symbol", "market", "timestamp", "price", "volume", "bid_price", "ask_price", "bid_size", "ask_size"]


def _us_payload(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market": "US",
        "timestamp": TS,
        "price": 189.5,
        "volume": 2_500.0,
        "bid_price": 189.48,
        "ask_price": 189.52,
        "bid_size": 1_200.0,
        "ask_size": 800.0,
    }


def _vn_payload(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market": "VN",
        "timestamp": TS,
        "price": 142_500.0,
        "volume": 45_000.0,
        "bid_price": 142_400.0,
        "ask_price": 142_600.0,
        "bid_size": 90_000.0,
        "ask_size": 15_000.0,
        "reference_price": 138_000.0,
        "exchange": "HOSE",
    }


def _rejected(payload: Dict[str, Any]) -> str:
    """Call the ingest handler, require HTTP 422 and return the error detail."""
    with pytest.raises(HTTPException) as excinfo:
        server.ingest_tick(payload)
    assert excinfo.value.status_code == 422
    return str(excinfo.value.detail)


def test_vn_tick_without_exchange_returns_422() -> None:
    payload = _vn_payload("TSTE")
    del payload["exchange"]
    assert "exchange" in _rejected(payload)


@pytest.mark.parametrize("field", REQUIRED)
def test_missing_required_field_returns_422(field: str) -> None:
    payload = _us_payload(f"TSTM{REQUIRED.index(field)}")
    del payload[field]
    detail = _rejected(payload)
    assert re.search(rf"\b{field}\b", detail), detail


def test_ingest_response_carries_disclaimer() -> None:
    response = server.ingest_tick(_us_payload("TSTA"))
    assert response["disclaimer"] == server.DISCLAIMER


def test_search_response_carries_disclaimer() -> None:
    response = server.search_disclosures(q="alpha", market=None, symbol=None, top_k=5)
    assert response["disclaimer"] == server.DISCLAIMER
    assert {"routed_model", "router_ucb_scores", "results", "disclaimer"} <= set(response)


def test_disclaimer_text_invariants() -> None:
    text = server.DISCLAIMER
    assert unicodedata.is_normalized("NFC", text)
    lowered = text.lower()
    assert "không phải khuyến nghị đầu tư" in lowered
    assert "not investment advice" in lowered
    assert lowered.index("không phải khuyến nghị đầu tư") < lowered.index("not investment advice"), "Vietnamese first"
    assert not re.search(r"\b(buy|sell|mua|bán)\b", text, re.IGNORECASE)


def test_non_finite_price_returns_422() -> None:
    detail = _rejected({**_us_payload("TSTN"), "price": float("nan")})
    assert re.search(r"\bprice\b", detail), detail
    assert "TSTN" not in server.quant_engine._ticks, "a rejected tick must not reach the shared engine"


def test_non_finite_foreign_volume_returns_422() -> None:
    detail = _rejected({**_us_payload("TSTF"), "foreign_buy_volume": float("nan")})
    assert re.search(r"\bforeign_buy_volume\b", detail), detail
    assert "TSTF" not in server.quant_engine._ticks


def test_huge_integer_price_returns_422() -> None:
    # JSON keeps a 400-digit integer literal as an exact int; float() raises OverflowError on it, which must not be a 500.
    detail = _rejected({**_us_payload("TSTO"), "price": 10**400})
    assert re.search(r"\bprice\b", detail), detail


def test_vn_null_exchange_reports_missing() -> None:
    detail = _rejected({**_vn_payload("TSTX"), "exchange": None})  # JSON null counts as absent
    assert "exchange" in detail and "missing" in detail, detail
    assert "NONE" not in detail


def test_null_symbol_returns_422() -> None:
    detail = _rejected({**_us_payload("TSTS"), "symbol": None})
    assert re.search(r"\bsymbol\b", detail), detail


def test_price_limits_request_and_response_order() -> None:
    # The request sends price_limits as [floor, ceiling]; the response's vn_ceiling_floor is (ceiling, floor).
    response = server.ingest_tick({**_vn_payload("TSTL"), "price_limits": [128_400.0, 147_600.0]})
    assert response["vn_ceiling_floor"] == (147_600.0, 128_400.0)
