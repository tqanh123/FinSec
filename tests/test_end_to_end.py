#!/usr/bin/env python3
"""
End-to-End Verification Suite for the Modular FinSec AlphaRadar Package.
Tests all subpackages under `src/finsec_alpharadar/`:
  - domain, connectors, quant, forensics, rag, router
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import numpy as np

from finsec_alpharadar.connectors import (
    AlpacaStreamAdapter,
    SSIFastConnectAdapter,
    SecEdgarFilingAdapter,
    VnstockBCTCAdapter,
)
from finsec_alpharadar.domain import (
    FinancialStatementSnapshot,
    MarketRegion,
    MarketTick,
    RegulatoryFiling,
)
from finsec_alpharadar.forensics import ForensicAccountingEngine
from finsec_alpharadar.quant import (
    EventStudyAnalyzer,
    MinHashDeduplicator,
    StreamingQuantEngine,
)
from finsec_alpharadar.rag import HybridTemporalRAGEngine
from finsec_alpharadar.router import LinUCBModelRouter


def run_modular_verification() -> dict:
    now_ts = datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc).timestamp()

    # 1. Test US & VN Connectors
    us_tick_from_adapter = AlpacaStreamAdapter.parse_trade_and_quote(
        {"S": "NVDA", "p": 189.50, "s": 2500, "x": "IEX"},
        {"bp": 189.48, "ap": 189.52, "bs": 1200, "as": 800},
    )
    vn_tick_from_adapter = SSIFastConnectAdapter.parse_market_data_message({
        "Symbol": "FPT",
        "LastPrice": 142.5,
        "LastVol": 45000,
        "BidPrice1": 142.4,
        "AskPrice1": 142.6,
        "BidVol1": 90000,
        "AskVol1": 15000,
        "RefPrice": 138.0,
        "Exchange": "HOSE",
        "ForeignBuyVolTotal": 125000,
        "ForeignSellVolTotal": 15000,
        "Timestamp": now_ts,
    })
    assert us_tick_from_adapter.market == MarketRegion.US
    assert vn_tick_from_adapter.vn_ceiling_floor_proximity == (147.66, 128.34)

    # 2. Test MinHash Deduplicator
    dedup = MinHashDeduplicator()
    dedup.is_duplicate("d1", "NVDA files Form 8-K reporting record Blackwell revenue")
    is_dup, matched, _ = dedup.is_duplicate("d2", "NVDA files Form 8-K reporting record Blackwell revenue!")
    assert is_dup is True and matched == "d1"

    # 3. Test Streaming Quant Engine across 15 steps
    quant = StreamingQuantEngine(window_size=30, cusum_drift=0.0015, cusum_threshold=0.010)
    for step in range(15):
        is_spike = (step == 14)
        nvda_sig = quant.process_tick(MarketTick(
            symbol="NVDA",
            market=MarketRegion.US,
            timestamp=now_ts + step,
            price=185.0 + 0.12 * step + (3.8 if is_spike else 0.0),
            volume=12_000.0 + 1_800.0 * math.sin(step * 1.1) if not is_spike else 28_500.0,
            bid_price=188.70,
            ask_price=188.75,
            bid_size=45_000.0 if is_spike else 5_000.0,
            ask_size=6_000.0 if is_spike else 4_800.0,
        ))
        fpt_sig = quant.process_tick(MarketTick(
            symbol="FPT",
            market=MarketRegion.VN,
            timestamp=now_ts + step,
            price=138.0 + 0.15 * step + (2.9 if is_spike else 0.0),
            volume=50_000.0 + 6_500.0 * math.cos(step * 0.9) if not is_spike else 115_000.0,
            bid_price=142.8,
            ask_price=142.9,
            bid_size=180_000.0 if is_spike else 20_000.0,
            ask_size=22_000.0 if is_spike else 19_000.0,
            reference_price=138.0,
            exchange="HOSE",
            foreign_buy_volume=160_000.0 if is_spike else 15_000.0,
            foreign_sell_volume=10_000.0 if is_spike else 12_000.0,
        ))
    assert nvda_sig.is_breakout_alert is True
    assert fpt_sig.is_breakout_alert is True

    # 4. Test Event Study CAR & Forensic Accounting
    est_bench = np.linspace(-0.01, 0.012, 30)
    est_stock = 0.0005 + 1.45 * est_bench + np.sin(np.arange(30)) * 0.001
    car_res = EventStudyAnalyzer.compute_car(est_stock, est_bench, [0.028, 0.019, 0.011], [0.002, 0.001, -0.001])
    assert car_res["is_statistically_significant_95"] is True

    audit = ForensicAccountingEngine.evaluate(FinancialStatementSnapshot(
        symbol="NVDA", market=MarketRegion.US, period="2026-Q3",
        revenue=38_500.0, cost_of_goods_sold=9_200.0, selling_general_admin=3_100.0,
        depreciation=850.0, net_income=20_400.0, operating_cash_flow=22_100.0,
        accounts_receivable=5_400.0, current_assets=58_000.0, current_liabilities=14_000.0,
        property_plant_equipment=12_500.0, total_assets=92_000.0, retained_earnings=64_000.0,
        ebit=24_800.0, long_term_debt=8_500.0, total_liabilities=22_500.0,
        market_cap=3_800_000.0, shares_outstanding=24_500.0,
        prev_revenue=32_000.0, prev_cost_of_goods_sold=8_100.0, prev_selling_general_admin=2_850.0,
        prev_depreciation=780.0, prev_net_income=16_200.0, prev_accounts_receivable=4_600.0,
        prev_current_assets=49_000.0, prev_current_liabilities=13_200.0,
        prev_property_plant_equipment=10_800.0, prev_total_assets=78_000.0,
        prev_long_term_debt=9_000.0, prev_shares_outstanding=24_650.0,
    ))
    assert audit.altman_zone == "SAFE" and audit.piotroski_f_score == 9

    # 5. Test Hybrid Temporal RAG & LinUCB Router
    rag = HybridTemporalRAGEngine()
    rag.ingest_filings([
        SecEdgarFilingAdapter.normalize_filing({
            "accession_number": "SEC-NVDA-8K-2026Q3",
            "ticker": "NVDA",
            "form_type": "8-K",
            "published_at": now_ts - 300,
            "title": "Item 2.02 Q3 Results",
            "section": "Item 2.02",
            "text_content": "Blackwell Ultra GPU revenue rose 24% sequentially with 76.1% gross margin.",
            "citation_url": "https://www.sec.gov/Archives/edgar/data/1045810/nvda-8k.htm",
        }),
        VnstockBCTCAdapter.normalize_disclosure({
            "doc_id": "HOSE-FPT-BCTC-2026Q3",
            "symbol": "FPT",
            "form_type": "BCTC_QUY",
            "published_at": now_ts - 600,
            "title": "BCTC Hop nhat Quy 3/2026",
            "section": "Thuyet minh Doanh thu",
            "text_content": "Doanh thu CNTT nuoc ngoai tai Nhat Ban tang 31.4% nho hop dong AI Factory.",
            "citation_url": "https://congbothongtin.ssc.gov.vn/fpt-q3-2026",
        }),
    ])
    hits_vn = rag.search("FPT doanh thu CNTT Nhat Ban AI Factory", market_filter=MarketRegion.VN, reference_time=now_ts)
    assert len(hits_vn) == 1 and hits_vn[0].symbol == "FPT"

    router = LinUCBModelRouter(["finsec-qwen3-1.7b-qlora", "gemini-3.6-flash", "gemini-4-argon-pro"])
    ctx = router.extract_context_vector("Audit NVDA Beneish M-Score and FPT foreign net flow", True, True)
    router.update_reward("gemini-4-argon-pro", ctx, 0.98)
    chosen_model, scores = router.select_model(ctx)

    return {
        "status": "ALL_MODULAR_PACKAGES_VERIFIED",
        "us_nvda_breakout": nvda_sig.alert_reasons,
        "vn_fpt_breakout": fpt_sig.alert_reasons,
        "vn_fpt_hose_bands": vn_tick_from_adapter.vn_ceiling_floor_proximity,
        "car_pct": car_res["car_pct"],
        "forensic_f_score": audit.piotroski_f_score,
        "top_vn_rag_filing": hits_vn[0].filing_id,
        "routed_model": chosen_model,
    }


if __name__ == "__main__":
    result = run_modular_verification()
    print(json.dumps(result, indent=2, ensure_ascii=False))
