"""`HybridTemporalRAGEngine.search` answers as of a decision time (AGENTS.md "No look-ahead").

A filing published after `as_of` must change nothing: it is never returned, and the BM25 corpus statistics (document
count, document frequencies, average length) come only from the filings visible at `as_of`. In the main fixture the
future filing F shares the query term "alpha" with P1, so counting F in the document frequencies flips the BM25 ranks of
P1 and P2 (OBSERVED 2026-10-21 in a spike); two more fixtures flip ranks through the document count and the average
length alone. No network, no clock.
"""
from __future__ import annotations

import dataclasses
from typing import Dict, List, Sequence, Tuple

import pytest

from finsec_alpharadar.domain import MarketRegion, RegulatoryFiling
from finsec_alpharadar.rag import HybridTemporalRAGEngine, RetrievedPassage

AS_OF = 1_792_211_400.0  # 2026-10-17T04:30:00Z, the decision time
DAY = 86_400.0
QUERY = "alpha beta"

# (filing_id, bm25_rank, dense_rank, rrf_score, colbert_maxsim_score, time_decay_factor, final_score)
Row = Tuple[str, int, int, float, float, float, float]


def _filing(filing_id: str, text: str, published_at: float) -> RegulatoryFiling:
    """A filing of the shared symbol and market; only the text and the publication time vary."""
    return RegulatoryFiling(
        filing_id=filing_id,
        symbol="TST",
        market=MarketRegion.US,
        form_type="8-K",
        published_at=published_at,
        title="",
        section="",
        text_content=text,
        citation_url=f"https://example.invalid/{filing_id}",
    )


P1 = _filing("P1", "alpha alpha alpha guidance", AS_OF - DAY)
P2 = _filing("P2", "beta guidance", AS_OF - 2 * DAY)
F = _filing("F", "alpha outlook", AS_OF + DAY)  # published one day after the decision time


def _engine(filings: Sequence[RegulatoryFiling]) -> HybridTemporalRAGEngine:
    engine = HybridTemporalRAGEngine()
    engine.ingest_filings(filings)
    return engine


def _rows(passages: Sequence[RetrievedPassage]) -> List[Row]:
    """Every rank and score of each passage, in result order."""
    return [
        (p.filing_id, p.bm25_rank, p.dense_rank, p.rrf_score, p.colbert_maxsim_score, p.time_decay_factor, p.final_score)
        for p in passages
    ]


def _bm25_order(passages: Sequence[RetrievedPassage]) -> List[str]:
    """Filing ids in BM25 rank order."""
    return [p.filing_id for p in sorted(passages, key=lambda p: p.bm25_rank)]


def test_document_after_as_of_is_never_returned() -> None:
    passages = _engine([P1, P2, F]).search(QUERY, as_of=AS_OF)
    assert sorted(p.filing_id for p in passages) == ["P1", "P2"], _rows(passages)


def test_results_invariant_to_documents_after_as_of() -> None:
    # Prefix invariance: adding a filing dated after AS_OF changes no rank and no score at AS_OF. This also catches a
    # fix that drops F from the candidates but keeps F in the BM25 statistics.
    without_future = _rows(_engine([P1, P2]).search(QUERY, as_of=AS_OF))
    with_future = _rows(_engine([P1, P2, F]).search(QUERY, as_of=AS_OF))
    assert [row[0] for row in without_future] == ["P1", "P2"]
    assert with_future == without_future


# (visible filings, future filings): each future set moves one BM25 statistic if it leaks into the scores.
STATISTIC_FIXTURES: Dict[str, Tuple[List[RegulatoryFiling], List[RegulatoryFiling]]] = {
    "document_frequency": ([P1, P2], [F]),
    "document_count": (  # four future filings share no query term: only the count N would move
        [
            _filing("A", "alpha", AS_OF - DAY),
            _filing("B3", "beta beta beta", AS_OF - 2 * DAY),
            _filing("B1", "beta", AS_OF - 3 * DAY),
        ],
        [_filing(f"G{i}", "gamma", AS_OF + DAY) for i in range(4)],
    ),
    "average_length": (  # three long future filings: only the average document length would move
        [_filing("A", "alpha", AS_OF - DAY), _filing("BX", "beta beta x x x x x x", AS_OF - 2 * DAY)],
        [_filing(f"L{i}", " ".join(["gamma"] * 40), AS_OF + DAY) for i in range(3)],
    ),
}


@pytest.mark.parametrize("future_first", [False, True], ids=["future-ingested-last", "future-ingested-first"])
@pytest.mark.parametrize("statistic", sorted(STATISTIC_FIXTURES))
def test_bm25_statistics_ignore_documents_after_as_of(statistic: str, future_first: bool) -> None:
    # Ingest order is not chronological in practice (pollers, restatements), so both orders must give the same answer.
    visible, future = STATISTIC_FIXTURES[statistic]
    without_future = _rows(_engine(visible).search(QUERY, as_of=AS_OF))
    with_future = _rows(_engine([*future, *visible] if future_first else [*visible, *future]).search(QUERY, as_of=AS_OF))
    assert len(without_future) == len(visible)
    assert with_future == without_future


def test_filters_do_not_change_bm25_statistics() -> None:
    # The statistics come from every visible filing, also one a symbol filter then leaves out; statistics over the
    # filtered candidates alone would rank P1 above P2 here.
    other = dataclasses.replace(_filing("O", "alpha outlook", AS_OF - 3 * DAY), symbol="OTH")
    engine = _engine([P1, P2, other])
    unfiltered = [fid for fid in _bm25_order(engine.search(QUERY, as_of=AS_OF)) if fid != "O"]
    filtered = _bm25_order(engine.search(QUERY, symbol_filter="TST", as_of=AS_OF))
    assert filtered == unfiltered == ["P2", "P1"]


@pytest.mark.parametrize("as_of", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_non_finite_as_of_raises(as_of: float) -> None:
    # NaN would hide every filing and +inf would show every future one (decay 0.0): the engine refuses both.
    with pytest.raises(ValueError, match="as_of"):
        _engine([P1, P2, F]).search(QUERY, as_of=as_of)


def test_as_of_is_required_and_keyword_only() -> None:
    engine = _engine([P1, P2])
    with pytest.raises(TypeError, match="as_of"):
        engine.search("alpha")  # no decision time: the engine must not fall back to the clock
    with pytest.raises(TypeError, match="positional"):
        engine.search("alpha", 5, None, None, AS_OF)  # a fifth positional argument is never the decision time


def test_as_of_zero_is_honoured() -> None:
    # Every filing is published after 1970-01-01T00:00:00Z, so nothing is visible at as_of=0.0. A truthiness test on
    # the decision time would treat 0.0 as missing.
    assert _engine([P1, P2]).search(QUERY, as_of=0.0) == []
    # And 0.0 is a real decision time: a filing published exactly then is visible, with no decay.
    passages = _engine([P1, _filing("Z", "alpha", 0.0), P2]).search(QUERY, as_of=0.0)
    assert [(p.filing_id, p.time_decay_factor) for p in passages] == [("Z", 1.0)]


def test_document_published_exactly_at_as_of_is_visible() -> None:
    # The visibility edge is published_at <= as_of: a filing published at the decision time counts, with no decay.
    passages = _engine([_filing("E", "alpha guidance", AS_OF)]).search(QUERY, as_of=AS_OF)
    assert [(p.filing_id, p.time_decay_factor) for p in passages] == [("E", 1.0)]
