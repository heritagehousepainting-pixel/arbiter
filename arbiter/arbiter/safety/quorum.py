"""Quorum gate for the safety layer (Lane L4).

Implements the quorum rule from INTERFACES.md §8:

    - 2+ live advisors → size_multiplier=1.0, level=NORMAL
    - 1 live advisor   → size_multiplier=0.25, level=DEGRADED
    - 0 live advisors  → size_multiplier=0.0, level=HALTED

This module is pure (no I/O, no clock).  The gate module wires it into the
full ``is_trading_allowed`` decision and audits the outcome.

Public API
----------
QuorumResult  — frozen dataclass; outcome of a quorum assessment.
assess_quorum(live_advisor_count) → QuorumResult
"""
from __future__ import annotations

from dataclasses import dataclass

from arbiter.types import DegradationLevel


@dataclass(frozen=True)
class QuorumResult:
    """Outcome of the quorum assessment.

    Attributes
    ----------
    level:
        DegradationLevel implied by the quorum state.
    size_multiplier:
        Scalar applied to all position sizes: 1.0, 0.25, or 0.0.
    reasons:
        Human-readable explanation strings.
    """

    level: DegradationLevel
    size_multiplier: float
    reasons: list[str]


def assess_quorum(live_advisor_count: int) -> QuorumResult:
    """Return a QuorumResult for the given number of live advisors.

    Parameters
    ----------
    live_advisor_count:
        Non-negative integer count of advisors currently producing signals.

    Returns
    -------
    QuorumResult
        - count >= 2  → NORMAL,   multiplier=1.0
        - count == 1  → DEGRADED, multiplier=0.25
        - count == 0  → HALTED,   multiplier=0.0

    Raises
    ------
    ValueError
        If ``live_advisor_count`` is negative.
    """
    if live_advisor_count < 0:
        raise ValueError(
            f"live_advisor_count must be non-negative, got {live_advisor_count}"
        )

    if live_advisor_count >= 2:
        return QuorumResult(
            level=DegradationLevel.NORMAL,
            size_multiplier=1.0,
            reasons=[],
        )

    if live_advisor_count == 1:
        return QuorumResult(
            level=DegradationLevel.DEGRADED,
            size_multiplier=0.25,
            reasons=[
                "quorum: only 1 live advisor — trading at 25% size (DEGRADED)"
            ],
        )

    # live_advisor_count == 0
    return QuorumResult(
        level=DegradationLevel.HALTED,
        size_multiplier=0.0,
        reasons=[
            "quorum: 0 live advisors — trading HALTED"
        ],
    )
