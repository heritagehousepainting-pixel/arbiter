"""Tests for Clock and BacktestClock — Lane 3 core.

Covers INTERFACES.md §3.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from arbiter.data.clock import BacktestClock, Clock


class TestClock:
    def test_now_returns_tz_aware(self):
        """Live Clock.now() must return a tz-aware datetime."""
        clock = Clock()
        result = clock.now()
        assert isinstance(result, datetime)
        assert result.tzinfo is not None, "Clock.now() must return tz-aware datetime"

    def test_now_is_utc(self):
        """Live Clock.now() must be UTC."""
        clock = Clock()
        result = clock.now()
        # UTC offset should be zero
        assert result.utcoffset() == timedelta(0), "Clock.now() must be UTC"

    def test_now_is_recent(self):
        """Live Clock.now() should return a recent timestamp."""
        clock = Clock()
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        result = clock.now()
        after = datetime.now(timezone.utc) + timedelta(seconds=5)
        assert before <= result <= after


class TestBacktestClock:
    def test_returns_fixed_as_of(self):
        """BacktestClock.now() returns exactly the as_of passed at construction."""
        as_of = datetime(2026, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
        clock = BacktestClock(as_of)
        assert clock.now() == as_of

    def test_now_is_tz_aware(self):
        as_of = datetime(2026, 1, 15, tzinfo=timezone.utc)
        clock = BacktestClock(as_of)
        result = clock.now()
        assert result.tzinfo is not None

    def test_naive_as_of_raises(self):
        """BacktestClock must reject naive datetimes at construction."""
        naive = datetime(2026, 1, 15, 9, 30, 0)  # no tzinfo
        with pytest.raises(ValueError, match="tz-aware"):
            BacktestClock(naive)

    def test_advance_by_timedelta(self):
        """advance() moves the clock forward by the given timedelta."""
        as_of = datetime(2026, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
        clock = BacktestClock(as_of)
        clock.advance(timedelta(days=1))
        expected = datetime(2026, 1, 16, 9, 30, 0, tzinfo=timezone.utc)
        assert clock.now() == expected

    def test_advance_multiple_times(self):
        """advance() can be called repeatedly."""
        as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(as_of)
        clock.advance(timedelta(days=7))
        clock.advance(timedelta(hours=12))
        expected = datetime(2026, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
        assert clock.now() == expected

    def test_set_as_of(self):
        """set_as_of() jumps the clock to an explicit datetime."""
        clock = BacktestClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        new_as_of = datetime(2026, 6, 15, tzinfo=timezone.utc)
        clock.set_as_of(new_as_of)
        assert clock.now() == new_as_of

    def test_set_as_of_naive_raises(self):
        """set_as_of() rejects naive datetimes."""
        clock = BacktestClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="tz-aware"):
            clock.set_as_of(datetime(2026, 6, 15))  # naive

    def test_multiple_calls_return_same_value(self):
        """BacktestClock.now() is stable — same value on repeated calls without advance."""
        as_of = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc)
        clock = BacktestClock(as_of)
        results = [clock.now() for _ in range(10)]
        assert all(r == as_of for r in results)

    def test_is_subclass_of_clock(self):
        """BacktestClock IS-A Clock."""
        as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = BacktestClock(as_of)
        assert isinstance(clock, Clock)
