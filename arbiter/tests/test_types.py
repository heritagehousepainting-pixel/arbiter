"""Tests for arbiter/types.py — enums and bucket_for_days."""
from __future__ import annotations

import pytest

from arbiter.types import (
    ConfidenceSource,
    DegradationLevel,
    HorizonBucket,
    IdeaState,
    OrderSide,
    bucket_for_days,
)


# ---------------------------------------------------------------------------
# bucket_for_days mapping tests (per task spec and INTERFACES.md §1)
# ---------------------------------------------------------------------------

class TestBucketForDays:
    def test_intraday(self) -> None:
        assert bucket_for_days(0.5) == HorizonBucket.INTRADAY

    def test_intraday_boundary_below_one(self) -> None:
        assert bucket_for_days(0.9) == HorizonBucket.INTRADAY

    def test_short_at_one(self) -> None:
        assert bucket_for_days(1) == HorizonBucket.SHORT

    def test_short_at_ten(self) -> None:
        assert bucket_for_days(10) == HorizonBucket.SHORT

    def test_short_at_30(self) -> None:
        assert bucket_for_days(30) == HorizonBucket.SHORT

    def test_medium_at_31(self) -> None:
        assert bucket_for_days(31) == HorizonBucket.MEDIUM

    def test_medium_at_60(self) -> None:
        assert bucket_for_days(60) == HorizonBucket.MEDIUM

    def test_medium_at_120(self) -> None:
        assert bucket_for_days(120) == HorizonBucket.MEDIUM

    def test_long_at_121(self) -> None:
        assert bucket_for_days(121) == HorizonBucket.LONG

    def test_long_at_200(self) -> None:
        assert bucket_for_days(200) == HorizonBucket.LONG

    def test_long_at_365(self) -> None:
        assert bucket_for_days(365) == HorizonBucket.LONG

    def test_invalid_zero(self) -> None:
        with pytest.raises(ValueError):
            bucket_for_days(0)

    def test_invalid_negative(self) -> None:
        with pytest.raises(ValueError):
            bucket_for_days(-1)

    def test_invalid_over_365(self) -> None:
        with pytest.raises(ValueError):
            bucket_for_days(366)


# ---------------------------------------------------------------------------
# Enum value tests — INTERFACES.md §1 specifies exact string/int values
# ---------------------------------------------------------------------------

class TestHorizonBucket:
    def test_values(self) -> None:
        assert HorizonBucket.INTRADAY.value == "INTRADAY"
        assert HorizonBucket.SHORT.value == "SHORT"
        assert HorizonBucket.MEDIUM.value == "MEDIUM"
        assert HorizonBucket.LONG.value == "LONG"

    def test_is_str_enum(self) -> None:
        assert isinstance(HorizonBucket.INTRADAY, str)


class TestConfidenceSource:
    def test_values(self) -> None:
        assert ConfidenceSource.EMPIRICAL.value == "empirical"
        assert ConfidenceSource.MODELED.value == "modeled"
        assert ConfidenceSource.SELF_REPORTED.value == "self_reported"
        assert ConfidenceSource.NONE.value == "none"


class TestOrderSide:
    def test_values(self) -> None:
        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"


class TestIdeaState:
    def test_all_states_present(self) -> None:
        states = {s.value for s in IdeaState}
        assert states == {
            "NASCENT",
            "GATHERING",
            "PROVISIONAL_DECIDED",
            "FINAL_DECIDED",
            "EXECUTED",
            "MONITORED",
            "OUTCOME_READY",
            "CLOSED",
            "ABANDONED",
        }


class TestDegradationLevel:
    def test_values(self) -> None:
        assert DegradationLevel.NORMAL.value == 0
        assert DegradationLevel.CAUTION.value == 1
        assert DegradationLevel.DEGRADED.value == 2
        assert DegradationLevel.RESTRICTED.value == 3
        assert DegradationLevel.HALTED.value == 4

    def test_is_int_enum(self) -> None:
        assert isinstance(DegradationLevel.NORMAL, int)

    def test_ordering(self) -> None:
        assert DegradationLevel.NORMAL < DegradationLevel.HALTED
