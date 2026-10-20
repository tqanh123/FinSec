---
name: dual-market-stream-check
description: "Runs real-time tick ingestion, Kalman filtering, OBI, CUSUM breakout detection, and Event Study CAR verification across US (NVDA/SPY) and Vietnam (FPT/VNINDEX) streams."
---
# Dual-Market Stream & Quant Verification (`/dual-market-stream-check`)
Run `PYTHONPATH=src python3 tests/test_end_to_end.py` and verify all US and Vietnam quantitative assertions pass.
