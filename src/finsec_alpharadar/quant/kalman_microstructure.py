from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Deque, Dict, List, Optional, Tuple
import numpy as np

from finsec_alpharadar.domain.models import MarketRegion, MarketTick


class KalmanPriceFilter:
    """2-State Constant-Velocity Kalman Filter for real-time tick streams."""

    def __init__(self, initial_price: float, process_noise: float = 1e-3, measurement_noise: float = 5e-2) -> None:
        self.x = np.array([initial_price, 0.0], dtype=np.float64)
        self.P = np.eye(2, dtype=np.float64)
        self.q = process_noise
        self.R = np.array([[measurement_noise]], dtype=np.float64)
        self.H = np.array([[1.0, 0.0]], dtype=np.float64)
        self.last_ts: Optional[float] = None

    def update(self, price: float, timestamp: float) -> Tuple[float, float]:
        dt = max(1e-3, timestamp - self.last_ts) if self.last_ts is not None else 1.0
        self.last_ts = timestamp
        F = np.array([[1.0, dt], [0.0, 1.0]], dtype=np.float64)
        Q = self.q * np.array([[dt**3 / 3.0, dt**2 / 2.0], [dt**2 / 2.0, dt]], dtype=np.float64)
        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q
        y = np.array([price]) - (self.H @ x_pred)
        S = self.H @ P_pred @ self.H.T + self.R
        K = (P_pred @ self.H.T) / S[0, 0]
        self.x = x_pred + (K[:, 0] * y[0])
        self.P = (np.eye(2) - K @ self.H) @ P_pred
        return float(self.x[0]), float(self.x[1])


@dataclass
class QuantSignalReport:
    symbol: str
    market: MarketRegion
    latest_price: float
    kalman_latent_price: float
    kalman_velocity: float
    rolling_vwap: float
    vwap_deviation_pct: float
    order_book_imbalance: float
    volume_z_score: float
    cusum_pos: float
    cusum_neg: float
    vn_foreign_net_volume: float
    is_breakout_alert: bool
    alert_reasons: List[str]


class StreamingQuantEngine:
    def __init__(self, window_size: int = 50, cusum_drift: float = 0.002, cusum_threshold: float = 0.015) -> None:
        self.window_size = window_size
        self.cusum_drift = cusum_drift
        self.cusum_threshold = cusum_threshold
        self._ticks: Dict[str, Deque[MarketTick]] = {}
        self._kalman: Dict[str, KalmanPriceFilter] = {}
        self._cusum_state: Dict[str, Tuple[float, float]] = {}

    def process_tick(self, tick: MarketTick) -> QuantSignalReport:
        sym = tick.symbol
        if sym not in self._ticks:
            self._ticks[sym] = deque(maxlen=self.window_size)
            self._kalman[sym] = KalmanPriceFilter(initial_price=tick.price)
            self._cusum_state[sym] = (0.0, 0.0)

        history = self._ticks[sym]
        prev_price = history[-1].price if history else tick.price
        history.append(tick)

        latent_price, velocity = self._kalman[sym].update(tick.price, tick.timestamp)
        prices = np.array([t.price for t in history], dtype=np.float64)
        volumes = np.array([t.volume for t in history], dtype=np.float64)
        total_vol = float(np.sum(volumes))
        vwap = float(np.sum(prices * volumes) / total_vol) if total_vol > 0 else tick.price
        vwap_dev_pct = ((tick.price - vwap) / vwap) * 100.0 if vwap > 0 else 0.0

        if len(volumes) >= 5:
            mean_vol = float(np.mean(volumes[:-1]))
            std_vol = max(1.0, float(np.std(volumes[:-1])))
            vol_z = (tick.volume - mean_vol) / std_vol
        else:
            vol_z = 0.0

        total_depth = tick.bid_size + tick.ask_size
        obi = (tick.bid_size - tick.ask_size) / total_depth if total_depth > 0 else 0.0

        log_ret = math.log(tick.price / prev_price) if prev_price > 0 and tick.price > 0 else 0.0
        s_pos, s_neg = self._cusum_state[sym]
        s_pos = max(0.0, s_pos + log_ret - self.cusum_drift)
        s_neg = max(0.0, s_neg - log_ret - self.cusum_drift)
        self._cusum_state[sym] = (s_pos, s_neg)

        vn_foreign_net = float(sum(t.foreign_buy_volume - t.foreign_sell_volume for t in history))

        reasons: List[str] = []
        if vol_z >= 2.5:
            reasons.append(f"Volume Z-Score Spike ({vol_z:.2f} sigma)")
        if s_pos >= self.cusum_threshold:
            reasons.append(f"Positive CUSUM Regime Shift ({s_pos:.4f})")
            self._cusum_state[sym] = (0.0, s_neg)
        if s_neg >= self.cusum_threshold:
            reasons.append(f"Negative CUSUM Breakdown ({s_neg:.4f})")
            self._cusum_state[sym] = (s_pos, 0.0)
        if abs(obi) >= 0.65:
            reasons.append(f"Extreme Order Book Imbalance (OBI={obi:+.2f})")
        if tick.market == MarketRegion.VN and abs(vn_foreign_net) >= 100_000:
            reasons.append(f"VN Foreign Institutional Net Flow ({vn_foreign_net:+,.0f} shares)")

        return QuantSignalReport(
            symbol=sym,
            market=tick.market,
            latest_price=round(tick.price, 4),
            kalman_latent_price=round(latent_price, 4),
            kalman_velocity=round(velocity, 6),
            rolling_vwap=round(vwap, 4),
            vwap_deviation_pct=round(vwap_dev_pct, 3),
            order_book_imbalance=round(obi, 4),
            volume_z_score=round(vol_z, 3),
            cusum_pos=round(s_pos, 5),
            cusum_neg=round(s_neg, 5),
            vn_foreign_net_volume=vn_foreign_net,
            is_breakout_alert=len(reasons) > 0,
            alert_reasons=reasons,
        )
