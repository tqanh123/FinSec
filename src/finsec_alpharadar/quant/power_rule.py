"""The power rule for go/no-go on a prediction target (plan 261022-1147 phase 6, R6.1).

Events cluster on dates (a reporting season puts many disclosures on one day), so N events on L distinct dates carry less
information than N independent draws. With rho the mean correlation of CAR between events on the same date and m = N / L:

    N_eff(rho) = N / (1 + (m - 1) * rho)      (research vn-calendar-sessions.md §5.1; rho = 0 gives N, rho = 1 gives L)
    MDE        = t / sqrt(N_eff), t = 2       (the registered rule t ~ IC * sqrt(N_eff), docs/project-roadmap.md §2.5)

The tier comes from MDE: GO when MDE <= 0.05, LIMITED when 0.05 < MDE <= 0.10, NO_GO when MDE > 0.10 (above 0.10 every
detectable effect would itself trigger the leakage audit, AGENTS.md "Success bar"). Both upper bounds are inclusive.

rho = 0.10 is ASSUMED until Phase 3 estimates it from VN data. `mde_80` (t = 2.8: 80% power, two-sided alpha 0.05) is a
sensitivity line only: it is reported and never changes a tier. Pure stdlib; no clock, no I/O.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

DEFAULT_RHO = 0.10  # ASSUMED until Phase 3 estimates the same-date CAR correlation.
REGISTERED_T = 2.0  # docs/project-roadmap.md §2.5.
SENSITIVITY_T_80 = 2.8  # 80% power, two-sided alpha 0.05; sensitivity line only.
GO_MAX_MDE = 0.05
LIMITED_MAX_MDE = 0.10


class PowerTier(str, Enum):
    """Go/no-go tier from the minimum detectable IC (inclusive upper bounds 0.05 and 0.10)."""

    GO = "GO"
    LIMITED = "LIMITED"
    NO_GO = "NO_GO"

    @classmethod
    def from_mde(cls, mde_value: float) -> PowerTier:
        """GO when `mde_value` <= 0.05, LIMITED when <= 0.10, NO_GO otherwise (including inf and NaN)."""
        if mde_value <= GO_MAX_MDE:
            return cls.GO
        if mde_value <= LIMITED_MAX_MDE:
            return cls.LIMITED
        return cls.NO_GO


@dataclass(frozen=True)
class PowerAssessment:
    """The power of one target: the tier comes from `mde` alone.

    `mde_floor`/`tier_floor` use N_eff = L (rho = 1, the lower bound). `mde_80` is a sensitivity line only (80% power) and
    never changes `tier`. `rho` defaults to 0.10, ASSUMED until Phase 3.
    """

    n_eff: float
    mde: float
    tier: PowerTier
    mde_floor: float
    tier_floor: PowerTier
    mde_80: float
    rho: float
    n_events: int
    n_distinct_dates: int


def _validate(n_events: int, n_distinct_dates: int, rho: float) -> None:
    if n_events < 0 or n_distinct_dates < 0:
        raise ValueError(f"counts must be non-negative: n_events={n_events}, n_distinct_dates={n_distinct_dates}")
    if n_distinct_dates > n_events:
        raise ValueError(f"n_distinct_dates ({n_distinct_dates}) cannot exceed n_events ({n_events})")
    if n_events > 0 and n_distinct_dates < 1:
        raise ValueError(f"n_events={n_events} needs at least one distinct date")
    if not 0.0 <= rho <= 1.0:  # also refuses NaN
        raise ValueError(f"rho must lie in [0, 1], got {rho!r}")


def effective_n(n_events: int, n_distinct_dates: int, rho: float) -> float:
    """N / (1 + (N / L - 1) * rho); 0.0 when there are no events.

    Raises ValueError for a negative count, L > N, L < 1 with N > 0, or rho outside [0, 1].
    """
    _validate(n_events, n_distinct_dates, rho)
    if n_events == 0:
        return 0.0
    mean_per_date = n_events / n_distinct_dates
    return n_events / (1.0 + (mean_per_date - 1.0) * rho)


def mde(n_eff: float, t: float = REGISTERED_T) -> float:
    """Minimum detectable IC, t / sqrt(n_eff); inf when n_eff is 0. Raises ValueError for a negative or non-finite n_eff."""
    if not math.isfinite(n_eff) or n_eff < 0.0:
        raise ValueError(f"n_eff must be finite and non-negative, got {n_eff!r}")
    if n_eff == 0.0:
        return math.inf
    return t / math.sqrt(n_eff)


def assess(n_events: int, n_distinct_dates: int, rho: float = DEFAULT_RHO) -> PowerAssessment:
    """Assess one target from N events on L distinct dates.

    `rho` defaults to 0.10, ASSUMED until Phase 3. The tier comes from the t = 2 MDE; `mde_80` is reported as a sensitivity
    line only. No events gives n_eff 0, an infinite MDE and NO_GO (the floor too).
    """
    n_eff = effective_n(n_events, n_distinct_dates, rho)
    main_mde = mde(n_eff)
    floor_mde = mde(float(n_distinct_dates))
    result = PowerAssessment(
        n_eff=n_eff,
        mde=main_mde,
        tier=PowerTier.from_mde(main_mde),
        mde_floor=floor_mde,
        tier_floor=PowerTier.from_mde(floor_mde),
        mde_80=mde(n_eff, t=SENSITIVITY_T_80),
        rho=rho,
        n_events=n_events,
        n_distinct_dates=n_distinct_dates,
    )
    logger.debug("power assessment n=%d l=%d rho=%.3f -> %s", n_events, n_distinct_dates, rho, result.tier.value)
    return result
