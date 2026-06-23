"""Post-execution position revision — Lane 13.

After execution, each cycle re-evaluates open positions using the latest
conviction from fusion.  Three outcomes are possible:

    EXIT    — conviction sign has flipped relative to the original direction,
              OR absolute conviction has fallen below 0.25
    REDUCE  — conviction has fallen below 0.50 (but >= 0.25) and is still in
              the same direction — cut position by 50%
    HOLD    — conviction is >= 0.50 in the same direction — no action needed

The original direction is inferred from the idea's thesis / order side (the
order records which side was taken).  Conviction is signed (positive = long,
negative = short).

Rules from spec §3.6:
    - EXIT if conviction flips sign OR absolute(conviction) < 0.25
    - REDUCE 50% if absolute(conviction) < 0.50 (and same sign, >= 0.25)
    - HOLD otherwise (conviction >= 0.50, same sign)
    - Never size UP — this function can only hold, reduce, or exit.

Thresholds:
    EXIT_THRESHOLD   = 0.25  (exclusive lower bound for same-side conviction)
    REDUCE_THRESHOLD = 0.50  (exclusive lower bound for HOLD)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Thresholds (§3.6)
# ---------------------------------------------------------------------------

EXIT_THRESHOLD: float = 0.25    # |conviction| < this → EXIT
REDUCE_THRESHOLD: float = 0.50  # |conviction| < this (and >= EXIT_THRESHOLD) → REDUCE


# ---------------------------------------------------------------------------
# Action enum
# ---------------------------------------------------------------------------

class RevisionAction(str, Enum):
    """Outcome of a post-execution revision check."""
    HOLD   = "HOLD"    # conviction still strong — no change
    REDUCE = "REDUCE"  # reduce position by 50%
    EXIT   = "EXIT"    # close position entirely


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RevisionResult:
    """Result of evaluating the revision rules for one position.

    Attributes
    ----------
    action:
        What to do with the position (HOLD / REDUCE / EXIT).
    conviction:
        The current (updated) conviction value that drove the decision.
    original_side:
        ``"long"`` if the original position was long (positive conviction),
        ``"short"`` if it was short (negative conviction).
    reason:
        Human-readable explanation for logging.
    """
    action: RevisionAction
    conviction: float
    original_side: str
    reason: str


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate_revision(
    current_conviction: float,
    *,
    original_conviction: float,
) -> RevisionResult:
    """Apply post-execution revision rules to determine what to do with a position.

    Parameters
    ----------
    current_conviction:
        Latest signed conviction from fusion (updated this cycle).
        Positive = bullish (long), negative = bearish (short).
    original_conviction:
        The conviction at the time of execution.  Used to determine the
        original side of the trade (sign of this value).  Must be non-zero.

    Returns
    -------
    RevisionResult
        The revision decision.

    Raises
    ------
    ValueError
        If ``original_conviction`` is zero (cannot determine original side).

    Notes
    -----
    "Sign flip" means the original was long (positive) and current is
    negative or zero, OR original was short (negative) and current is
    positive or zero.  We treat zero as a sign flip to be conservative
    (fail-safe: exit on ambiguity).
    """
    if original_conviction == 0.0:
        raise ValueError(
            "original_conviction must be non-zero to determine the original "
            "trade direction; received 0.0"
        )

    original_side = "long" if original_conviction > 0 else "short"
    abs_current = abs(current_conviction)

    # Check sign flip: original long and current is not-positive, or
    # original short and current is not-negative.
    sign_flipped = (
        (original_conviction > 0 and current_conviction <= 0)
        or (original_conviction < 0 and current_conviction >= 0)
    )

    # Rule 1: EXIT — sign flip OR below exit threshold
    if sign_flipped:
        return RevisionResult(
            action=RevisionAction.EXIT,
            conviction=current_conviction,
            original_side=original_side,
            reason=(
                f"Conviction sign flipped "
                f"(original {original_conviction:+.3f} → current {current_conviction:+.3f}) "
                f"— EXIT position"
            ),
        )

    if abs_current < EXIT_THRESHOLD:
        return RevisionResult(
            action=RevisionAction.EXIT,
            conviction=current_conviction,
            original_side=original_side,
            reason=(
                f"|conviction| {abs_current:.3f} < exit threshold {EXIT_THRESHOLD} "
                f"— EXIT position"
            ),
        )

    # Rule 2: REDUCE — below reduce threshold but same sign and above exit threshold
    if abs_current < REDUCE_THRESHOLD:
        return RevisionResult(
            action=RevisionAction.REDUCE,
            conviction=current_conviction,
            original_side=original_side,
            reason=(
                f"|conviction| {abs_current:.3f} in "
                f"[{EXIT_THRESHOLD}, {REDUCE_THRESHOLD}) "
                f"— REDUCE position by 50%"
            ),
        )

    # Rule 3: HOLD — conviction still strong and same sign
    return RevisionResult(
        action=RevisionAction.HOLD,
        conviction=current_conviction,
        original_side=original_side,
        reason=(
            f"|conviction| {abs_current:.3f} >= {REDUCE_THRESHOLD} and same sign "
            f"— HOLD position"
        ),
    )
