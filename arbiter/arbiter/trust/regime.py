"""Regime change handling — Lane 11 / trust sub-module.

Regime tracking rules (spec §3.4):
- Regime changes trigger a 21-day freeze (no trust-weight updates for 21 days
  after a regime transition is detected).
- Post-regime outcomes are weighted 2× (double their recency decay weight) to
  fast-track re-calibration after a regime change.

``RegimeTracker`` is stateless beyond a ``regime_id`` string and the
``changed_at`` timestamp.  It does NOT call datetime.now() — callers
supply all timestamps.

Wave-C wiring: who detects regime changes and feeds this module is TBD;
the caller passes regime events as parameters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from arbiter.contract.seams import ResolvedOutcome

FREEZE_DAYS: int = 21        # days after regime change where updates are frozen
POST_REGIME_MULTIPLIER: float = 2.0   # weight multiplier for post-regime outcomes


@dataclass
class RegimeChangeEvent:
    """A discrete regime change event.

    Parameters
    ----------
    regime_id:
        Identifier for the new regime (e.g. "bull_2024", "bear_q3_2025").
    changed_at:
        Timestamp when the regime change was detected/declared (tz-aware UTC).
    """

    regime_id: str
    changed_at: datetime


@dataclass
class RegimeTracker:
    """Tracks regime-change events and applies freeze + weight-multiplier logic.

    Parameters
    ----------
    regime_events:
        History of regime change events in chronological order.
    """

    regime_events: list[RegimeChangeEvent] = field(default_factory=list)

    def is_frozen(self, as_of: datetime) -> bool:
        """Return True if trust-weight updates are frozen at as_of.

        Frozen = the most recent regime change occurred within the last 21 days.
        """
        if not self.regime_events:
            return False
        latest = max(self.regime_events, key=lambda e: e.changed_at)
        return (as_of - latest.changed_at) < timedelta(days=FREEZE_DAYS)

    def last_regime_change(self) -> RegimeChangeEvent | None:
        """Return the most recent regime change event, or None."""
        if not self.regime_events:
            return None
        return max(self.regime_events, key=lambda e: e.changed_at)

    def regime_at(self, as_of: datetime) -> str | None:
        """Return the active regime ID at a given timestamp, or None."""
        past_events = [e for e in self.regime_events if e.changed_at <= as_of]
        if not past_events:
            return None
        return max(past_events, key=lambda e: e.changed_at).regime_id


def apply_regime_weights(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    base_weights: Sequence[float],
    regime_tracker: RegimeTracker,
) -> list[float]:
    """Apply 2× post-regime multiplier to outcomes that occurred after a regime change.

    Post-regime = outcome_date > most recent regime change event.

    Parameters
    ----------
    outcomes:
        Sequence of ResolvedOutcome objects.
    outcome_dates:
        Parallel sequence of datetime objects for when each outcome resolved.
    base_weights:
        Base recency weights (from exponential decay) for each outcome.
    regime_tracker:
        RegimeTracker holding the history of regime change events.

    Returns
    -------
    list[float]
        Adjusted weights, same length as base_weights.  Post-regime weights
        are multiplied by POST_REGIME_MULTIPLIER (2×).
    """
    if not (len(outcomes) == len(outcome_dates) == len(base_weights)):
        raise ValueError(
            "outcomes, outcome_dates, and base_weights must all have equal length"
        )

    last_event = regime_tracker.last_regime_change()
    if last_event is None:
        # No regime changes recorded; weights are unaffected.
        return list(base_weights)

    adjusted: list[float] = []
    for date, w in zip(outcome_dates, base_weights):
        if date > last_event.changed_at:
            adjusted.append(w * POST_REGIME_MULTIPLIER)
        else:
            adjusted.append(w)
    return adjusted
