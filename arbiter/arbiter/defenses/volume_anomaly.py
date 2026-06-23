"""Volume-anomaly gate for anti-manipulation defenses — Lane 8.

INTERFACES.md §8 (safety breakers) lists ``a3_volume_anomaly`` as a latching
circuit-breaker: when A3 detects abnormal volume on a held name, safety halts
trading on that position.  This module provides the detection logic.

Design
------
``VolumeAnomalyGate.is_anomalous(ticker, as_of, pit)`` computes a z-score of
today's volume relative to a 20-day rolling baseline and returns True when
the z-score exceeds the configured threshold (default: 3.0σ).

Algorithm:
  1. Fetch daily volumes for the 20 trading days BEFORE ``as_of`` via
     ``pit.get("price_close", ticker, day_ts)`` — the same field used by
     adv.py (returns a Bar or scalar).
  2. Compute mean (μ) and population std-dev (σ) of the 20-day window.
  3. Fetch today's volume at ``as_of`` (the observation day).
  4. z = (today_vol - μ) / σ  (if σ == 0, returns False — no anomaly detectable)
  5. Return True iff z >= threshold.

Fail-closed rules (INTERFACES.md §11 convention 4):
  - Fewer than 20 baseline days → ``is_anomalous`` returns ``False`` (not
    enough data to call an anomaly; we don't block trading on uncertainty).
  - ``pit.get`` returns None for today → ``False`` (no live data to check).
  - σ == 0 (all baseline days identical) → ``False`` (degenerate baseline).

Wave-C wiring points
--------------------
Lane 4 (``arbiter.safety.breakers.CircuitBreaker.check_a3_volume_anomaly``):

    from arbiter.defenses import VolumeAnomalyGate
    gate = VolumeAnomalyGate()
    if gate.is_anomalous(ticker, as_of, pit):
        breaker.check_a3_volume_anomaly(ticker, conn, clock)

Lane 13 (orchestrator sweep):

    from arbiter.defenses import VolumeAnomalyGate
    gate = VolumeAnomalyGate()
    if gate.is_anomalous(held_ticker, as_of, pit):
        # suppress new idea creation on this name; existing position protected
        # by the Lane 4 breaker
        continue

INTERFACES.md §11 — no ``datetime.now()``.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from arbiter.data.adv import _get_pit_bar
from arbiter.data.pit import Bar, PITGateway

_logger = logging.getLogger(__name__)

# Default z-score threshold above which volume is considered anomalous.
_DEFAULT_Z_THRESHOLD: float = 3.0

# Number of baseline trading days required before the gate will fire.
_BASELINE_DAYS: int = 20

# Calendar look-back to capture _BASELINE_DAYS trading days (weekends + holidays).
_LOOKBACK_CALENDAR_DAYS: int = 35


def _extract_volume(value: object) -> float | None:
    """Extract volume from a PIT value.

    Handles:
    - ``Bar`` instance (production): returns ``bar.volume``.
    - Numeric scalar (test fixture): returns ``float(value)`` (treated as
      pre-computed volume, mirroring ``adv.py`` convention).

    Returns None on conversion failure.
    """
    if isinstance(value, Bar):
        if value.volume >= 0:
            return float(value.volume)
        return None
    try:
        v = float(value)  # type: ignore[arg-type]
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


class VolumeAnomalyGate:
    """Detects abnormal trading volume on a ticker via z-score.

    Used by Lane 4 (safety breakers) and Lane 13 (orchestrator sweep) as an
    active safety dependency.  The tips layer itself is shadow/dormant in
    Phase-6, but this gate is LIVE regardless.

    Parameters
    ----------
    z_threshold:
        Z-score above which volume is flagged as anomalous.
        Default is 3.0σ (one-tailed).
    baseline_days:
        Number of trading days for the rolling baseline window.
        Default is 20 (matching the ADV calculation in Lane 3).
    """

    def __init__(
        self,
        z_threshold: float = _DEFAULT_Z_THRESHOLD,
        baseline_days: int = _BASELINE_DAYS,
    ) -> None:
        if z_threshold <= 0:
            raise ValueError(f"z_threshold must be positive, got {z_threshold}")
        if baseline_days < 1:
            raise ValueError(f"baseline_days must be >= 1, got {baseline_days}")
        self._z_threshold = z_threshold
        self._baseline_days = baseline_days

    def is_anomalous(
        self,
        ticker: str,
        as_of: datetime,
        pit: PITGateway,
    ) -> bool:
        """Return True if today's volume is anomalously high vs the baseline.

        Parameters
        ----------
        ticker:
            Exchange ticker symbol to check.
        as_of:
            Information timestamp (tz-aware UTC).  The observation day is
            ``as_of``; the baseline window is the ``baseline_days`` trading
            days strictly before ``as_of`` (no look-ahead).
        pit:
            PITGateway — all reads go through here.  Must have ``price_close``
            registered (returns ``Bar`` or numeric scalar).

        Returns
        -------
        bool
            True iff volume z-score >= z_threshold.  False on insufficient
            data, missing today-volume, or degenerate baseline (all same).
        """
        baseline_vols = self._fetch_baseline_volumes(ticker, as_of, pit)

        if len(baseline_vols) < self._baseline_days:
            _logger.debug(
                "VolumeAnomalyGate: insufficient baseline (%d/%d days) for %s as_of %s — not anomalous",
                len(baseline_vols),
                self._baseline_days,
                ticker,
                as_of,
            )
            return False

        today_vol = self._fetch_today_volume(ticker, as_of, pit)
        if today_vol is None:
            _logger.debug(
                "VolumeAnomalyGate: no today-volume for %s as_of %s — not anomalous",
                ticker,
                as_of,
            )
            return False

        mu, sigma = _mean_stddev(baseline_vols)

        if sigma == 0.0:
            _logger.debug(
                "VolumeAnomalyGate: zero std-dev in baseline for %s — not anomalous",
                ticker,
            )
            return False

        z = (today_vol - mu) / sigma
        anomalous = z >= self._z_threshold

        _logger.debug(
            "VolumeAnomalyGate: %s as_of %s vol=%.0f μ=%.0f σ=%.0f z=%.2f threshold=%.1f anomalous=%s",
            ticker,
            as_of,
            today_vol,
            mu,
            sigma,
            z,
            self._z_threshold,
            anomalous,
        )
        return anomalous

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_baseline_volumes(
        self,
        ticker: str,
        as_of: datetime,
        pit: PITGateway,
    ) -> list[float]:
        """Collect daily volumes strictly before ``as_of`` (no look-ahead).

        Walks [as_of - LOOKBACK_CALENDAR_DAYS, as_of) day-by-day and collects
        usable volume values.  Returns at most the ``baseline_days`` most recent.
        """
        end_exclusive = as_of
        start = as_of - timedelta(days=_LOOKBACK_CALENDAR_DAYS)

        volumes: list[float] = []
        cursor = start
        one_day = timedelta(days=1)

        while cursor < end_exclusive:
            value = _get_pit_bar(pit, ticker, cursor)
            if value is not None:
                vol = _extract_volume(value)
                if vol is not None:
                    volumes.append(vol)
            cursor = cursor + one_day

        # Return the most recent ``baseline_days`` values.
        return volumes[-self._baseline_days :]

    def _fetch_today_volume(
        self,
        ticker: str,
        as_of: datetime,
        pit: PITGateway,
    ) -> float | None:
        """Fetch the observation-day volume (the value at or nearest to ``as_of``).

        Probes ``pit.get("price_close", ticker, as_of)`` and extracts volume.
        Returns None if no data is available.
        """
        value = _get_pit_bar(pit, ticker, as_of)
        if value is None:
            return None
        return _extract_volume(value)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _mean_stddev(values: list[float]) -> tuple[float, float]:
    """Return (mean, population std-dev) of *values*.

    Uses population std-dev (divides by N) — appropriate for a fixed
    historical window rather than a sample.

    Returns (0.0, 0.0) for empty or single-element lists.
    """
    n = len(values)
    if n == 0:
        return 0.0, 0.0

    mu = sum(values) / n
    variance = sum((v - mu) ** 2 for v in values) / n
    return mu, math.sqrt(variance)
