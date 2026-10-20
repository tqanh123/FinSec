from __future__ import annotations

import math
from typing import Dict, Sequence
import numpy as np


class EventStudyAnalyzer:
    """Computes OLS Market Model Abnormal Returns (AR) and Cumulative Abnormal Return (CAR)."""

    @staticmethod
    def compute_car(
        stock_returns: Sequence[float],
        benchmark_returns: Sequence[float],
        event_window_stock: Sequence[float],
        event_window_benchmark: Sequence[float],
    ) -> Dict[str, float]:
        x = np.asarray(benchmark_returns, dtype=np.float64)
        y = np.asarray(stock_returns, dtype=np.float64)
        A = np.vstack([np.ones_like(x), x]).T
        alpha, beta = np.linalg.lstsq(A, y, rcond=None)[0]
        residuals = y - (alpha + beta * x)
        sigma_eps = float(np.std(residuals, ddof=2)) if len(residuals) > 2 else 1e-4

        ev_x = np.asarray(event_window_benchmark, dtype=np.float64)
        ev_y = np.asarray(event_window_stock, dtype=np.float64)
        abnormal_returns = ev_y - (alpha + beta * ev_x)
        car = float(np.sum(abnormal_returns))
        car_std = sigma_eps * math.sqrt(len(ev_y))
        t_stat = car / car_std if car_std > 0 else 0.0

        return {
            "alpha": round(float(alpha), 6),
            "beta": round(float(beta), 4),
            "car": round(car, 5),
            "car_pct": round(car * 100.0, 3),
            "t_stat": round(float(t_stat), 3),
            "is_statistically_significant_95": bool(abs(t_stat) >= 1.96),
        }
