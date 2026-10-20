# FinSec AlphaRadar — Dual-Market (US & Vietnam) Agentic Engineering Contract

## System Overview
FinSec AlphaRadar is a real-time event-driven quantitative and hybrid RAG intelligence platform covering:
1. **US Equities (NYSE/NASDAQ/IEX)**: Real-time ticks/quotes via Alpaca WebSocket & official SEC EDGAR filings (8-K, 10-Q, 10-K, Form 4).
2. **Vietnam Equities (HOSE/HNX/UPCOM)**: Real-time intraday data via SSI FastConnect (`wss://stream.ssi.com.vn`) & `vnstock`, plus Consolidated Financial Statements (BCTC Hợp nhất) and Board Resolutions (Nghị quyết HĐQT).

## Package Layout (`src/finsec_alpharadar/`)
- `domain/models.py`: Multi-market data schemas (`MarketRegion`, `MarketTick`, `RegulatoryFiling`, `FinancialStatementSnapshot`).
- `connectors/`: Async stream & disclosure adapters (`us_alpaca_edgar.py`, `vn_ssi_vnstock.py`).
- `quant/`: Real-time `MinHashDeduplicator`, `KalmanPriceFilter`, `StreamingQuantEngine` (VWAP, OBI, CUSUM, Volume Z-Score, VN Foreign Net Flow), and `EventStudyAnalyzer` (CAR).
- `forensics/xbrl_bctc_auditor.py`: Deterministic Altman Z-Score, 8-variable Beneish M-Score, and 9-point Piotroski F-Score.
- `rag/hybrid_temporal_rag.py`: Sparse BM25 + Dense Vector + Reciprocal Rank Fusion (RRF) + ColBERT MaxSim Reranker + Exponential Time-Decay + Semantic Diff.
- `router/linucb_bandit.py`: Disjoint LinUCB Contextual Bandit routing between local fine-tuned SLM (`Qwen3-1.7B`) and frontier models.
- `api/server.py`: FastAPI REST & WebSocket streaming endpoints.

## Build & Verification Commands
- Run full end-to-end verification & unit tests: `PYTHONPATH=src python3 tests/test_end_to_end.py`
- Start FastAPI real-time server: `PYTHONPATH=src uvicorn finsec_alpharadar.api.server:app --reload --port 8000`
