---
name: sec-vn-rag-eval
description: "Evaluates Hybrid Temporal RAG (BM25 + Dense + RRF + ColBERT MaxSim + Time-Decay) and QoQ Semantic Disclosure Diffs on SEC and Vietnam filings."
---
# SEC & Vietnam BCTC Hybrid RAG Evaluation (`/sec-vn-rag-eval`)
Run `PYTHONPATH=src python3 tests/test_end_to_end.py` and inspect the RRF, ColBERT MaxSim, and Time-Decay retrieval rankings.
