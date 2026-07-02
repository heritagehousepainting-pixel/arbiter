"""Exit rule computation — Lane 12a.

Exits are defined AT ENTRY and stored on PaperOrder.exits.  They are never
revised upward after entry (INTERFACES.md §9).

Exit triggers:
    stop_loss            : price level = entry_price * (1 - stop_fraction)
                           for BUY; entry_price * (1 + stop_fraction) for SELL.
    horizon_expiry       : calendar date = entry_date + horizon_days
    conviction_reversal  : conviction magnitude threshold that triggers exit
                           (0.0 → opposite sign conviction triggers exit)
"""
from __future__ import annotations

from datetime import date, timedelta

from arbiter.types import HorizonBucket, OrderSide


# ---------------------------------------------------------------------------
# Horizon-bucket stop-loss fractions
# ---------------------------------------------------------------------------

_STOP_LOSS_BY_BUCKET: dict[HorizonBucket, float] = {
    HorizonBucket.INTRADAY: 0.005,   # 0.5% intraday stop
    HorizonBucket.SHORT:    0.03,    # 3% for 1–30 day ideas
    HorizonBucket.MEDIUM:   0.05,    # 5% for 31–120 day ideas
    HorizonBucket.LONG:     0.08,    # 8% for 121–365 day ideas
}

# When conviction flips sign to at least this magnitude, exit the position.
# Threshold = 0.0 means any opposite-sign conviction triggers exit.
_CONVICTION_REVERSAL_THRESHOLD = 0.0


# ---------------------------------------------------------------------------
# Horizon-bucket calendar days (midpoint of range, used for expiry)
# ---------------------------------------------------------------------------

_HORIZON_DAYS_BY_BUCKET: dict[HorizonBucket, int] = {
    HorizonBucket.INTRADAY: 1,    # exit by end of day
    HorizonBucket.SHORT:    15,   # midpoint of 1–30
    HorizonBucket.MEDIUM:   75,   # midpoint of 31–120
    # Tier-3 #10 (2026-07-02): LONG was 240 (midpoint of 121–365).  Shortened
    # to 150 — still inside the bucket range — to speed outcome generation
    # ~1.6x (the learning loop was starved by 8-month holds; 6 positions
    # wouldn't have closed until Feb-2027).  Applies retroactively to held
    # positions: the exit monitor recomputes horizons from this constant each
    # cycle (the stored horizon_expiry is as phantom as the stored stop).
    HorizonBucket.LONG:     150,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_exits(
    bucket: HorizonBucket,
    side: OrderSide,
    entry_price: float,
    entry_date: date,
    *,
    horizon_days: int | None = None,
    stop_fraction: float | None = None,
    conviction_reversal_threshold: float | None = None,
) -> dict:
    """Compute exit parameters set at entry time.

    Returns the ``exits`` dict expected by PaperOrder:

    .. code-block:: python

        {
            "stop_loss": float,          # absolute price level
            "horizon_expiry": date,      # calendar date
            "conviction_reversal": float # threshold that triggers reversal exit
        }

    Parameters
    ----------
    bucket:
        HorizonBucket for this position.
    side:
        BUY or SELL (determines stop-loss direction).
    entry_price:
        Price at which the position is entered.
    entry_date:
        Calendar date of entry.
    horizon_days:
        Override the bucket's default horizon in days.
    stop_fraction:
        Override the bucket's default stop-loss fraction.
    conviction_reversal_threshold:
        Override the default conviction reversal threshold.
    """
    # Stop-loss fraction
    stop_frac = stop_fraction if stop_fraction is not None else _STOP_LOSS_BY_BUCKET[bucket]

    # Stop price: below entry for BUY, above entry for SELL
    if side == OrderSide.BUY:
        stop_price = entry_price * (1.0 - stop_frac)
    else:
        stop_price = entry_price * (1.0 + stop_frac)

    # Horizon expiry
    days = horizon_days if horizon_days is not None else _HORIZON_DAYS_BY_BUCKET[bucket]
    expiry = entry_date + timedelta(days=days)

    # Conviction reversal threshold
    reversal = (
        conviction_reversal_threshold
        if conviction_reversal_threshold is not None
        else _CONVICTION_REVERSAL_THRESHOLD
    )

    return {
        "stop_loss": stop_price,
        "horizon_expiry": expiry,
        "conviction_reversal": reversal,
    }
