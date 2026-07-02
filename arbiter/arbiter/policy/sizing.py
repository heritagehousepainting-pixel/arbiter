"""Position sizing — Lane 12a.

Pipeline (INTERFACES.md §9):
    1. Quarter-Kelly raw size
    2. Per-name hard cap (max_position_pct)
    3. Per-sector hard cap (max_sector_pct) — applied as headroom
    4. Gross-exposure hard cap (max_gross_pct) — applied as headroom
    5. Open-position count cap (max_open_positions)
    6. Gate size_multiplier (from TradingDecision)
    7. Calibration confidence multiplier (cold_start shrinks to 50%)
    8. ADV liquidity cap — **LAST transform**

Fail-closed: missing ADV → return 0.0.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from arbiter.config import Config
from arbiter.contract.seams import FusionOutput, TradingDecision


# ---------------------------------------------------------------------------
# Calibration confidence multiplier
# ---------------------------------------------------------------------------

_COLD_START_MULTIPLIER = 0.50
"""Sizes are halved while calibration prior dominates (cold_start=True)."""


# ---------------------------------------------------------------------------
# Quarter-Kelly
# ---------------------------------------------------------------------------

def _quarter_kelly(conviction: float, portfolio_equity: float) -> float:
    """Compute raw quarter-Kelly notional size.

    Kelly fraction = |conviction| (treating conviction in [-1,1] as the edge).
    Quarter-Kelly = 0.25 * kelly_fraction.
    Returns a notional dollar amount.

    Parameters
    ----------
    conviction:
        Signed fusion conviction in [-1, 1].
    portfolio_equity:
        Current portfolio equity (USD).
    """
    kelly_fraction = abs(conviction)
    quarter_kelly_fraction = 0.25 * kelly_fraction
    return quarter_kelly_fraction * portfolio_equity


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_size(
    fusion: FusionOutput,
    portfolio_equity: float,
    config: Config,
    gate_decision: TradingDecision,
    *,
    adv_provider: Callable[[str, datetime], float | None],
    ticker: str,
    as_of: datetime,
    current_sector_exposure: float = 0.0,
    current_gross_exposure: float = 0.0,
    current_open_positions: int = 0,
    current_name_exposure: float = 0.0,
) -> float:
    """Compute final position size (notional USD) for one ticker.

    Returns 0.0 on any fail-closed condition:
    - gate.allowed is False (HALTED)
    - gate.size_multiplier == 0.0
    - ADV data unavailable
    - Conviction rounds to zero

    Parameters
    ----------
    fusion:
        FusionOutput for the relevant HorizonBucket.
    portfolio_equity:
        Current net portfolio equity (USD).
    config:
        Loaded arbiter Config (provides cap percentages).
    gate_decision:
        Result of the safety gate check for this account.
    adv_provider:
        Callable(ticker, as_of) → 20-day ADV in USD, or None if unknown.
    ticker:
        Ticker symbol (for ADV lookup).
    as_of:
        Information timestamp for the ADV lookup.
    current_sector_exposure:
        Already-committed notional to the same sector (USD).
    current_gross_exposure:
        Already-committed gross notional across all positions (USD).
    current_open_positions:
        Number of currently open positions.
    current_name_exposure:
        Already-committed notional to THIS ticker (USD) — nonzero only for an
        ADD-ON to a held name (Tier-2 #5, 2026-07-02).  The per-name cap
        becomes a headroom cap (``name_cap − current_name_exposure``) so the
        combined position can never exceed ``max_position_pct``, and the
        open-position-count gate is skipped (an add-on does not open a NEW
        position).  Default 0.0 keeps every existing caller unchanged.
    """
    # Fail-closed: gate disallows trading
    if not gate_decision.allowed or gate_decision.size_multiplier == 0.0:
        return 0.0

    # Zero conviction → no position
    if fusion.conviction == 0.0:
        return 0.0

    # Step 1: Quarter-Kelly raw size
    size = _quarter_kelly(fusion.conviction, portfolio_equity)

    # Step 2: Per-name hard cap — applied as HEADROOM so an add-on to a held
    # name is bounded by the cap MINUS what's already committed to the ticker.
    name_cap = config.max_position_pct * portfolio_equity
    name_headroom = max(0.0, name_cap - current_name_exposure)
    size = min(size, name_headroom)

    # Step 3: Sector headroom cap
    sector_max = config.max_sector_pct * portfolio_equity
    sector_headroom = max(0.0, sector_max - current_sector_exposure)
    size = min(size, sector_headroom)

    # Step 4: Gross exposure headroom cap
    gross_max = config.max_gross_pct * portfolio_equity
    gross_headroom = max(0.0, gross_max - current_gross_exposure)
    size = min(size, gross_headroom)

    # Step 5: Open-position count cap — if at capacity, no new positions.
    # An ADD-ON (nonzero name exposure) does not open a NEW position, so the
    # count gate does not apply to it.
    if current_open_positions >= config.max_open_positions and current_name_exposure <= 0.0:
        return 0.0

    # Step 6: Gate size_multiplier
    size *= gate_decision.size_multiplier

    # Step 7: Calibration confidence multiplier
    if fusion.cold_start:
        size *= _COLD_START_MULTIPLIER

    # Step 8: ADV liquidity cap — LAST transform
    adv = adv_provider(ticker, as_of)
    if adv is None or math.isnan(adv):
        # Fail-closed: missing or NaN ADV → size 0.
        # math.isnan guard prevents min(x, nan)==x from bypassing the cap.
        return 0.0

    adv_cap = config.adv_cap_pct * adv
    size = min(size, adv_cap)

    return max(0.0, size)
