"""Canonical enum definitions for the arbiter package.

This is the ONLY file that defines these enums. No other module may redefine them.
All other modules import from here: ``from arbiter.types import HorizonBucket``.

See INTERFACES.md §1 for the frozen contract.
"""
from __future__ import annotations

from enum import Enum


class HorizonBucket(str, Enum):
    """Trading horizon buckets. Fusion pools only *within* a bucket."""

    INTRADAY = "INTRADAY"  # < 1 day
    SHORT = "SHORT"        # 1–30 days
    MEDIUM = "MEDIUM"      # 31–120 days
    LONG = "LONG"          # 121–365 days


class ConfidenceSource(str, Enum):
    """How the confidence figure was derived."""

    EMPIRICAL = "empirical"
    MODELED = "modeled"
    SELF_REPORTED = "self_reported"
    NONE = "none"


class OrderSide(str, Enum):
    """Direction of an order."""

    BUY = "BUY"
    SELL = "SELL"


class IdeaState(str, Enum):
    """FSM states for an Idea object (INTERFACES.md §7)."""

    NASCENT = "NASCENT"
    GATHERING = "GATHERING"
    PROVISIONAL_DECIDED = "PROVISIONAL_DECIDED"
    FINAL_DECIDED = "FINAL_DECIDED"
    EXECUTED = "EXECUTED"
    MONITORED = "MONITORED"
    OUTCOME_READY = "OUTCOME_READY"
    CLOSED = "CLOSED"
    ABANDONED = "ABANDONED"


class DegradationLevel(int, Enum):
    """Safety degradation ladder (INTERFACES.md §8)."""

    NORMAL = 0
    CAUTION = 1
    DEGRADED = 2
    RESTRICTED = 3
    HALTED = 4


def bucket_for_days(days: float) -> HorizonBucket:
    """Map a horizon in days to a HorizonBucket.

    Boundaries per INTERFACES.md §1 and build-plan §2 item 8:
    - INTRADAY : days < 1
    - SHORT    : 1 <= days <= 30
    - MEDIUM   : 31 <= days <= 120
    - LONG     : 121 <= days <= 365

    Raises ValueError for days <= 0 or days > 365 (out of range).
    """
    if days <= 0:
        raise ValueError(f"horizon_days must be positive, got {days}")
    if days > 365:
        raise ValueError(f"horizon_days {days} exceeds LONG bucket maximum (365)")
    if days < 1:
        return HorizonBucket.INTRADAY
    if days <= 30:
        return HorizonBucket.SHORT
    if days <= 120:
        return HorizonBucket.MEDIUM
    return HorizonBucket.LONG
