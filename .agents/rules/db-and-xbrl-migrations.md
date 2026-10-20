---
trigger: model_decision
description: "Apply when designing or modifying TimescaleDB/PostgreSQL schemas for XBRL financials, BCTC Vietnam, or tick hypertables."
---
# TimescaleDB & XBRL Storage Rules
1. Store tick data in a TimescaleDB hypertable partitioned by `timestamp` and indexed on `(symbol, market, timestamp DESC)`.
2. Store XBRL / BCTC line items as exact numeric types (`DOUBLE PRECISION` or `NUMERIC(20, 4)`) with period start/end dates and filing publication timestamps (`published_at`) to prevent restatement look-ahead bias.
