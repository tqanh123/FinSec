"""The VN power rule (plan 261022-1147 phase 6, R6.1).

N_eff(rho) = N / (1 + (m - 1) * rho) with m = N / L (research vn-calendar-sessions.md §5.1); MDE = t / sqrt(N_eff) with the
registered t = 2 (roadmap §2.5); tiers GO (MDE <= 0.05), LIMITED (0.05 < MDE <= 0.10), NO_GO (MDE > 0.10). mde_80 = 2.8 / sqrt(N_eff)
is a sensitivity line only and never changes the tier. Expected values are hand-written literals with the arithmetic in a comment.
Pure unit tests: no network, no clock.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

import finsec_alpharadar.quant as quant
from finsec_alpharadar.quant.power_rule import PowerAssessment, PowerTier, assess, effective_n, mde


def test_effective_n_is_n_without_correlation_and_l_with_full_correlation() -> None:
    # N = 6400, L = 576, m = 11.111...; rho = 0 -> N / 1 = 6400; rho = 1 -> N / m = L = 576.
    assert effective_n(6400, 576, 0.0) == pytest.approx(6400.0, abs=1e-9)
    assert effective_n(6400, 576, 1.0) == pytest.approx(576.0, abs=1e-9)
    # Events spread one per date (m = 1): N_eff = N for any rho.
    assert effective_n(250, 250, 0.7) == pytest.approx(250.0, abs=1e-9)


def test_effective_n_matches_the_research_example() -> None:
    # Research §5.1: N = 6400, L = 576, m = 6400 / 576 = 11.1111; 1 + 10.1111 * 0.10 = 2.01111; 6400 / 2.01111 = 3182.32 (the research
    # rounds it to about 3,180); MDE = 2 / sqrt(3182.32) = 2 / 56.412 = 0.03545 (research: about 0.035).
    n_eff = effective_n(6400, 576, 0.10)
    assert n_eff == pytest.approx(3182.3, abs=0.1)
    assert mde(n_eff) == pytest.approx(0.0355, abs=1e-4)
    result = assess(6400, 576)
    assert result.rho == 0.10  # the default rho is the ASSUMED 0.10
    assert result.n_eff == pytest.approx(3182.3, abs=0.1)
    assert result.tier is PowerTier.GO
    # Floor (rho = 1, N_eff = L = 576): 2 / sqrt(576) = 2 / 24 = 0.083333 -> LIMITED.
    assert result.mde_floor == pytest.approx(0.083333, abs=1e-6)
    assert result.tier_floor is PowerTier.LIMITED


def test_mde_matches_the_roadmap_power_rule() -> None:
    # Research §5.1 table, N = (t / IC)^2 at t = 2: 4444.4 -> 2 / 66.666 = 0.0300002; 1600 -> 2 / 40 = 0.05; 400 -> 2 / 20 = 0.10.
    assert mde(4444.4) == pytest.approx(0.03, abs=1e-6)
    assert mde(1600.0) == pytest.approx(0.05, abs=1e-12)
    assert mde(400.0) == pytest.approx(0.10, abs=1e-12)
    # The 80% power column, t = 2.80: 784 -> 2.8 / 28 = 0.10.
    assert mde(784.0, t=2.8) == pytest.approx(0.10, abs=1e-12)
    # No events: infinite MDE rather than an error.
    assert mde(0.0) == math.inf


def test_tier_upper_bounds_are_inclusive() -> None:
    assert PowerTier.from_mde(0.03) is PowerTier.GO
    assert PowerTier.from_mde(0.05) is PowerTier.GO
    assert PowerTier.from_mde(0.0500001) is PowerTier.LIMITED
    assert PowerTier.from_mde(0.10) is PowerTier.LIMITED
    assert PowerTier.from_mde(0.1000001) is PowerTier.NO_GO
    assert PowerTier.from_mde(math.inf) is PowerTier.NO_GO
    # The rule's own boundary inputs: N_eff 1600 -> MDE 0.05 -> GO; N_eff 400 -> MDE 0.10 -> LIMITED.
    assert assess(1600, 1600).tier is PowerTier.GO
    assert assess(400, 400).tier is PowerTier.LIMITED
    # N_eff 399 -> 2 / 19.975 = 0.100125 -> NO_GO.
    assert assess(399, 399).tier is PowerTier.NO_GO
    assert PowerTier.GO.value == "GO" and PowerTier.LIMITED.value == "LIMITED" and PowerTier.NO_GO.value == "NO_GO"


def test_zero_events_is_no_go_with_infinite_mde() -> None:
    result = assess(0, 0)
    assert result.n_events == 0 and result.n_distinct_dates == 0
    assert result.n_eff == 0.0
    assert result.mde == math.inf
    assert result.tier is PowerTier.NO_GO
    assert result.mde_floor == math.inf
    assert result.tier_floor is PowerTier.NO_GO
    assert result.mde_80 == math.inf
    assert effective_n(0, 0, 0.5) == 0.0


@pytest.mark.parametrize(
    ("n_events", "n_distinct_dates", "rho"),
    [
        (10, 11, 0.10),  # L > N
        (0, 1, 0.10),  # L > N with no events
        (10, 0, 0.10),  # L = 0 with N > 0
        (10, 5, -0.01),  # rho < 0
        (10, 5, 1.01),  # rho > 1
        (10, 5, math.nan),  # rho not a number
        (-1, 0, 0.10),  # negative N
        (10, -1, 0.10),  # negative L
    ],
)
def test_invalid_inputs_raise(n_events: int, n_distinct_dates: int, rho: float) -> None:
    with pytest.raises(ValueError):
        effective_n(n_events, n_distinct_dates, rho)
    with pytest.raises(ValueError):
        assess(n_events, n_distinct_dates, rho=rho)


@pytest.mark.parametrize("bad_n_eff", [-1.0, math.nan, math.inf])
def test_mde_rejects_a_negative_or_non_finite_n_eff(bad_n_eff: float) -> None:
    with pytest.raises(ValueError):
        mde(bad_n_eff)


def test_eighty_percent_power_is_sensitivity_only() -> None:
    # N = L = 1975 (m = 1) -> N_eff = 1975 for any rho; sqrt(1975) = 44.441; mde = 2 / 44.441 = 0.045004 -> GO;
    # mde_80 = 2.8 / 44.441 = 0.063005, reported only; the ratio mde_80 / mde is always 1.4.
    result = assess(1975, 1975)
    assert result.n_eff == pytest.approx(1975.0, abs=1e-9)
    assert result.mde == pytest.approx(0.045004, abs=1e-6)
    assert result.mde_80 == pytest.approx(0.063005, abs=1e-6)
    assert result.mde_80 == 2.8 / math.sqrt(result.n_eff)
    assert result.tier is PowerTier.GO
    assert result.tier is PowerTier.from_mde(result.mde)
    assert result.mde_80 / result.mde == pytest.approx(1.4, abs=1e-12)


def test_assessment_is_frozen() -> None:
    result = assess(6400, 576)
    assert isinstance(result, PowerAssessment)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.tier = PowerTier.NO_GO  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.mde = 0.0  # type: ignore[misc]


def test_quant_package_re_exports_the_power_rule() -> None:
    for name in ("assess", "PowerAssessment", "PowerTier"):
        assert name in quant.__all__
    assert quant.assess is assess
    assert quant.PowerAssessment is PowerAssessment
    assert quant.PowerTier is PowerTier
