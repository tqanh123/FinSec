from __future__ import annotations

import hashlib
import re
from typing import Dict, Optional, Tuple
import numpy as np


class MinHashDeduplicator:
    """O(1) near-duplicate detector using k-word shingles and universal 64-bit hashing."""

    def __init__(self, num_perm: int = 64, jaccard_threshold: float = 0.80, shingle_size: int = 3) -> None:
        self.num_perm = num_perm
        self.threshold = jaccard_threshold
        self.shingle_size = shingle_size
        self._prime = (1 << 61) - 1
        rng = np.random.default_rng(seed=42)
        self._a = rng.integers(1, self._prime - 1, size=num_perm, dtype=np.uint64)
        self._b = rng.integers(0, self._prime - 1, size=num_perm, dtype=np.uint64)
        self._signatures: Dict[str, np.ndarray] = {}

    def _compute_signature(self, text: str) -> np.ndarray:
        tokens = re.findall(r"\w+", text.lower())
        if len(tokens) < self.shingle_size:
            tokens = tokens + ["_pad_"] * (self.shingle_size - len(tokens))
        shingle_hashes = np.array([
            int(hashlib.blake2b(" ".join(tokens[i:i + self.shingle_size]).encode("utf-8"), digest_size=8).hexdigest(), 16)
            & ((1 << 61) - 1)
            for i in range(len(tokens) - self.shingle_size + 1)
        ], dtype=np.uint64)
        hashed = (shingle_hashes[:, None] * self._a[None, :] + self._b[None, :]) % np.uint64(self._prime)
        return hashed.min(axis=0)

    def is_duplicate(self, doc_id: str, text: str) -> Tuple[bool, Optional[str], float]:
        sig = self._compute_signature(text)
        best_match: Optional[str] = None
        best_sim = 0.0
        for existing_id, existing_sig in self._signatures.items():
            sim = float(np.mean(sig == existing_sig))
            if sim > best_sim:
                best_sim = sim
                best_match = existing_id
        if best_sim >= self.threshold:
            return True, best_match, best_sim
        self._signatures[doc_id] = sig
        return False, None, best_sim
