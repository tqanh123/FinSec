---
trigger: glob
globs: "*.py, **/*.py"
description: "Enforce deterministic quant calculations, US/VN market microstructure rules, and look-ahead bias prevention."
---
# Quantitative & Dual-Market Microstructure Rules
1. **No Look-Ahead Bias**: In all sliding windows, Kalman updates, and Event Study CAR calculations, strictly use timestamps `<= t_event`.
2. **Vietnam Stock Market Specifics**:
   - Validate price against `HOSE (+/-7%)`, `HNX (+/-10%)`, and `UPCOM (+/-15%)` bands around `reference_price`.
   - Always include Foreign Institutional Net Flow (`foreign_buy_volume - foreign_sell_volume`) when scoring Vietnam large-cap equities (VN30).
3. **Hybrid RAG Standards**: Always combine sparse lexical matching (BM25) with dense embeddings via Reciprocal Rank Fusion (`RRF`), rerank with late-interaction MaxSim, and apply exponential time-decay.
