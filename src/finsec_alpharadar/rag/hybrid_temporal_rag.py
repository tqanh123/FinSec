from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
import numpy as np

from finsec_alpharadar.domain.models import MarketRegion, RegulatoryFiling


@dataclass
class RetrievedPassage:
    filing_id: str
    symbol: str
    market: MarketRegion
    form_type: str
    section: str
    text_content: str
    citation_url: str
    bm25_rank: int
    dense_rank: int
    rrf_score: float
    colbert_maxsim_score: float
    time_decay_factor: float
    final_score: float


class HybridTemporalRAGEngine:
    """BM25 + Reciprocal Rank Fusion + exponential time decay over in-memory filings, answered as of a decision time.

    The "dense" leg and the ColBERT-style MaxSim leg use per-token hash vectors (`_token_vec`): placeholders with no
    semantics, not embeddings. `search` sees only the filings published at or before its `as_of`.
    """

    def __init__(self, embedding_dim: int = 128, rrf_k: int = 60, decay_half_life_days: float = 30.0) -> None:
        self.embedding_dim = embedding_dim
        self.rrf_k = rrf_k
        self.decay_lambda = math.log(2.0) / max(1.0, decay_half_life_days)
        self._docs: List[RegulatoryFiling] = []
        self._doc_tokens: List[List[str]] = []
        self._dense_matrix: Optional[np.ndarray] = None

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return re.findall(r"[a-z0-9_]+", text.lower())

    def _token_vec(self, token: str) -> np.ndarray:
        seed = int(hashlib.blake2b(token.encode("utf-8"), digest_size=4).hexdigest(), 16)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(self.embedding_dim, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        return v / norm if norm > 0 else v

    def _embed_text(self, tokens: Sequence[str]) -> np.ndarray:
        if not tokens:
            return np.zeros(self.embedding_dim, dtype=np.float32)
        vecs = np.stack([self._token_vec(t) for t in tokens], axis=0)
        mean_vec = np.mean(vecs, axis=0)
        norm = float(np.linalg.norm(mean_vec))
        return mean_vec / norm if norm > 0 else mean_vec

    def ingest_filings(self, filings: Iterable[RegulatoryFiling]) -> int:
        added = 0
        for f in filings:
            tokens = self._tokenize(f"{f.symbol} {f.form_type} {f.title} {f.section} {f.text_content}")
            self._docs.append(f)
            self._doc_tokens.append(tokens)
            added += 1
        if self._docs:
            self._dense_matrix = np.stack([self._embed_text(t) for t in self._doc_tokens], axis=0)
        return added

    def _bm25_scores(
        self, query_tokens: Sequence[str], visible: Sequence[int], k1: float = 1.5, b: float = 0.75
    ) -> np.ndarray:
        """BM25 score per filing index, for the filings in `visible` (non-empty) only.

        The corpus statistics (document count, document frequencies, average length) come from `visible` alone, so a
        filing outside it cannot change any score. Entries outside `visible` stay 0.0 and are never read.
        """
        n_docs = len(visible)
        doc_freqs: Counter[str] = Counter()
        for idx in visible:
            doc_freqs.update(set(self._doc_tokens[idx]))
        avg_dl = sum(len(self._doc_tokens[idx]) for idx in visible) / n_docs
        scores = np.zeros(len(self._docs), dtype=np.float64)
        for idx in visible:
            doc_toks = self._doc_tokens[idx]
            dl = len(doc_toks)
            tf_counter = Counter(doc_toks)
            score = 0.0
            for qt in query_tokens:
                df = doc_freqs.get(qt, 0)
                if df == 0:
                    continue
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                tf = tf_counter.get(qt, 0)
                denom = tf + k1 * (1.0 - b + b * (dl / max(1.0, avg_dl)))
                score += idf * ((tf * (k1 + 1.0)) / max(1e-9, denom))
            scores[idx] = score
        return scores

    def _colbert_maxsim(self, query_tokens: Sequence[str], doc_tokens: Sequence[str]) -> float:
        if not query_tokens or not doc_tokens:
            return 0.0
        q_mat = np.stack([self._token_vec(t) for t in query_tokens[:32]], axis=0)
        d_mat = np.stack([self._token_vec(t) for t in set(doc_tokens)], axis=0)
        sim_matrix = q_mat @ d_mat.T
        return float(np.mean(np.max(sim_matrix, axis=1)))

    def search(
        self,
        query: str,
        top_k: int = 5,
        market_filter: Optional[MarketRegion] = None,
        symbol_filter: Optional[str] = None,
        *,
        as_of: float,
    ) -> List[RetrievedPassage]:
        """The best `top_k` matches for `query` among the filings published at or before `as_of`.

        `as_of` is the decision time in epoch seconds UTC, the unit of `RegulatoryFiling.published_at`. It is required
        and keyword-only, and the engine never reads the clock. Filings published after `as_of` are dropped before
        anything is scored, and the BM25 statistics come from the visible filings only, so a later filing changes no
        score and no rank. The market and symbol filters then narrow the candidates without changing those statistics.
        A non-finite `as_of` raises ValueError: NaN would hide every filing and +inf would show every future one.
        """
        if not math.isfinite(as_of):
            raise ValueError(f"as_of must be a finite epoch time in seconds, got {as_of!r}")
        visible = [i for i, doc in enumerate(self._docs) if doc.published_at <= as_of]
        candidates = [
            i for i in visible
            if (market_filter is None or self._docs[i].market == market_filter)
            and (symbol_filter is None or self._docs[i].symbol.upper() == symbol_filter.upper())
        ]
        if not candidates or self._dense_matrix is None:
            return []

        q_tokens = self._tokenize(query)
        bm25_raw = self._bm25_scores(q_tokens, visible)
        q_vec = self._embed_text(q_tokens)
        dense_raw = (self._dense_matrix @ q_vec).astype(np.float64)

        bm25_sorted = sorted(candidates, key=lambda i: bm25_raw[i], reverse=True)
        dense_sorted = sorted(candidates, key=lambda i: dense_raw[i], reverse=True)
        bm25_rank_map = {doc_idx: rank + 1 for rank, doc_idx in enumerate(bm25_sorted)}
        dense_rank_map = {doc_idx: rank + 1 for rank, doc_idx in enumerate(dense_sorted)}

        results: List[RetrievedPassage] = []
        for idx in candidates:
            doc = self._docs[idx]
            r_bm25 = bm25_rank_map[idx]
            r_dense = dense_rank_map[idx]
            rrf_score = (1.0 / (self.rrf_k + r_bm25)) + (1.0 / (self.rrf_k + r_dense))
            maxsim = self._colbert_maxsim(q_tokens, self._doc_tokens[idx])
            age_days = (as_of - doc.published_at) / 86_400.0  # never negative: every candidate is visible
            decay = math.exp(-self.decay_lambda * age_days)
            combined = (0.45 * (rrf_score * 30.0) + 0.55 * maxsim) * (0.7 + 0.3 * decay)
            results.append(RetrievedPassage(
                filing_id=doc.filing_id,
                symbol=doc.symbol,
                market=doc.market,
                form_type=doc.form_type,
                section=doc.section,
                text_content=doc.text_content,
                citation_url=doc.citation_url,
                bm25_rank=r_bm25,
                dense_rank=r_dense,
                rrf_score=round(rrf_score, 5),
                colbert_maxsim_score=round(maxsim, 4),
                time_decay_factor=round(decay, 4),
                final_score=round(combined, 4),
            ))
        results.sort(key=lambda r: r.final_score, reverse=True)
        return results[:top_k]

    @staticmethod
    def compute_semantic_disclosure_diff(old_section_text: str, new_section_text: str) -> Dict[str, Any]:
        old_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", old_section_text) if s.strip()]
        new_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", new_section_text) if s.strip()]
        old_set = {s.lower() for s in old_sentences}
        added_clauses = [s for s in new_sentences if s.lower() not in old_set]
        removed_clauses = [s for s in old_sentences if s.lower() not in {n.lower() for n in new_sentences}]
        return {
            "added_clause_count": len(added_clauses),
            "removed_clause_count": len(removed_clauses),
            "newly_introduced_risks": added_clauses,
            "removed_statements": removed_clauses,
        }
