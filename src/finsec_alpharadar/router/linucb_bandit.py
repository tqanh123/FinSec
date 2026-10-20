from __future__ import annotations

import math
from typing import Dict, Sequence, Tuple
import numpy as np


class LinUCBModelRouter:
    """Contextual Bandit (Disjoint LinUCB) for Mixture-of-Models routing."""

    def __init__(self, arm_names: Sequence[str], context_dim: int = 5, alpha: float = 0.6) -> None:
        self.arm_names = list(arm_names)
        self.d = context_dim
        self.alpha = alpha
        self.A = {arm: np.eye(context_dim, dtype=np.float64) for arm in self.arm_names}
        self.b = {arm: np.zeros((context_dim, 1), dtype=np.float64) for arm in self.arm_names}

    @staticmethod
    def extract_context_vector(query: str, requires_xbrl_forensics: bool, is_realtime_spike: bool) -> np.ndarray:
        complexity = min(1.0, len(query.split()) / 40.0)
        multi_market = 1.0 if ("vn" in query.lower() or "hose" in query.lower() or "sec" in query.lower()) else 0.3
        vec = np.array([
            1.0,
            complexity,
            1.0 if requires_xbrl_forensics else 0.0,
            1.0 if is_realtime_spike else 0.0,
            multi_market,
        ], dtype=np.float64)
        return vec / np.linalg.norm(vec)

    def select_model(self, context_vec: np.ndarray) -> Tuple[str, Dict[str, float]]:
        x = context_vec.reshape(-1, 1)
        ucb_scores: Dict[str, float] = {}
        for arm in self.arm_names:
            A_inv = np.linalg.inv(self.A[arm])
            theta_hat = A_inv @ self.b[arm]
            mean_reward = float((theta_hat.T @ x)[0, 0])
            uncertainty = float(self.alpha * math.sqrt(max(0.0, float((x.T @ A_inv @ x)[0, 0]))))
            ucb_scores[arm] = round(mean_reward + uncertainty, 4)
        chosen = max(ucb_scores, key=ucb_scores.get)
        return chosen, ucb_scores

    def update_reward(self, arm: str, context_vec: np.ndarray, reward: float) -> None:
        x = context_vec.reshape(-1, 1)
        self.A[arm] += x @ x.T
        self.b[arm] += reward * x
