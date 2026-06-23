"""Tests for arbiter.policy.exits — Lane 12a.

Covered cases:
- Stop-loss set correctly for BUY (below entry) and SELL (above entry)
- Horizon expiry date set from bucket defaults
- Horizon expiry overrideable by horizon_days
- Conviction reversal defaults to 0.0
- Exits dict has all three required keys
- Each bucket gets appropriate stop fraction
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from arbiter.policy.exits import (
    compute_exits,
    _STOP_LOSS_BY_BUCKET,
    _HORIZON_DAYS_BY_BUCKET,
    _CONVICTION_REVERSAL_THRESHOLD,
)
from arbiter.types import HorizonBucket, OrderSide


class TestExitsStructure:
    """exits dict always has the three required keys."""

    def test_exits_keys_present(self):
        exits = compute_exits(
            bucket=HorizonBucket.SHORT,
            side=OrderSide.BUY,
            entry_price=100.0,
            entry_date=date(2026, 6, 19),
        )
        assert "stop_loss" in exits
        assert "horizon_expiry" in exits
        assert "conviction_reversal" in exits

    def test_exits_types(self):
        exits = compute_exits(
            bucket=HorizonBucket.SHORT,
            side=OrderSide.BUY,
            entry_price=100.0,
            entry_date=date(2026, 6, 19),
        )
        assert isinstance(exits["stop_loss"], float)
        assert isinstance(exits["horizon_expiry"], date)
        assert isinstance(exits["conviction_reversal"], float)


class TestStopLoss:
    """Stop-loss direction and magnitude."""

    def test_buy_stop_below_entry(self):
        """BUY stop is below entry price."""
        exits = compute_exits(
            bucket=HorizonBucket.SHORT,
            side=OrderSide.BUY,
            entry_price=100.0,
            entry_date=date(2026, 6, 19),
        )
        assert exits["stop_loss"] < 100.0

    def test_sell_stop_above_entry(self):
        """SELL stop is above entry price."""
        exits = compute_exits(
            bucket=HorizonBucket.SHORT,
            side=OrderSide.SELL,
            entry_price=100.0,
            entry_date=date(2026, 6, 19),
        )
        assert exits["stop_loss"] > 100.0

    def test_buy_stop_correct_fraction(self):
        """BUY stop = entry * (1 - fraction)."""
        bucket = HorizonBucket.SHORT
        frac = _STOP_LOSS_BY_BUCKET[bucket]
        entry = 200.0
        exits = compute_exits(
            bucket=bucket, side=OrderSide.BUY, entry_price=entry, entry_date=date(2026, 6, 19)
        )
        assert exits["stop_loss"] == pytest.approx(entry * (1.0 - frac))

    def test_sell_stop_correct_fraction(self):
        """SELL stop = entry * (1 + fraction)."""
        bucket = HorizonBucket.SHORT
        frac = _STOP_LOSS_BY_BUCKET[bucket]
        entry = 200.0
        exits = compute_exits(
            bucket=bucket, side=OrderSide.SELL, entry_price=entry, entry_date=date(2026, 6, 19)
        )
        assert exits["stop_loss"] == pytest.approx(entry * (1.0 + frac))

    def test_stop_fraction_override(self):
        """Custom stop_fraction is respected."""
        custom_frac = 0.10
        entry = 100.0
        exits = compute_exits(
            bucket=HorizonBucket.MEDIUM,
            side=OrderSide.BUY,
            entry_price=entry,
            entry_date=date(2026, 6, 19),
            stop_fraction=custom_frac,
        )
        assert exits["stop_loss"] == pytest.approx(entry * (1.0 - custom_frac))

    @pytest.mark.parametrize("bucket", list(HorizonBucket))
    def test_each_bucket_has_stop(self, bucket):
        """Every bucket has a defined stop-loss fraction."""
        exits = compute_exits(
            bucket=bucket, side=OrderSide.BUY, entry_price=50.0, entry_date=date(2026, 6, 19)
        )
        assert exits["stop_loss"] < 50.0  # always below entry for BUY

    def test_longer_bucket_has_wider_stop(self):
        """LONG bucket stop fraction > SHORT bucket stop fraction."""
        assert _STOP_LOSS_BY_BUCKET[HorizonBucket.LONG] > _STOP_LOSS_BY_BUCKET[HorizonBucket.SHORT]


class TestHorizonExpiry:
    """horizon_expiry is entry_date + horizon_days."""

    def test_default_horizon_expiry(self):
        """Default expiry uses bucket's horizon days."""
        bucket = HorizonBucket.SHORT
        entry = date(2026, 6, 19)
        exits = compute_exits(bucket=bucket, side=OrderSide.BUY, entry_price=100.0, entry_date=entry)
        expected = entry + timedelta(days=_HORIZON_DAYS_BY_BUCKET[bucket])
        assert exits["horizon_expiry"] == expected

    def test_horizon_override(self):
        """Custom horizon_days overrides bucket default."""
        entry = date(2026, 6, 19)
        custom_days = 45
        exits = compute_exits(
            bucket=HorizonBucket.MEDIUM,
            side=OrderSide.BUY,
            entry_price=100.0,
            entry_date=entry,
            horizon_days=custom_days,
        )
        assert exits["horizon_expiry"] == entry + timedelta(days=custom_days)

    @pytest.mark.parametrize("bucket", list(HorizonBucket))
    def test_each_bucket_has_expiry(self, bucket):
        """Every bucket produces a valid horizon_expiry."""
        entry = date(2026, 1, 1)
        exits = compute_exits(bucket=bucket, side=OrderSide.BUY, entry_price=100.0, entry_date=entry)
        assert exits["horizon_expiry"] > entry


class TestConvictionReversal:
    """conviction_reversal threshold is set at entry."""

    def test_default_reversal_threshold(self):
        """Default conviction reversal = 0.0 (any opposite sign exits)."""
        exits = compute_exits(
            bucket=HorizonBucket.SHORT,
            side=OrderSide.BUY,
            entry_price=100.0,
            entry_date=date(2026, 6, 19),
        )
        assert exits["conviction_reversal"] == pytest.approx(_CONVICTION_REVERSAL_THRESHOLD)

    def test_reversal_override(self):
        """Custom conviction_reversal_threshold is respected."""
        custom = 0.3
        exits = compute_exits(
            bucket=HorizonBucket.MEDIUM,
            side=OrderSide.SELL,
            entry_price=100.0,
            entry_date=date(2026, 6, 19),
            conviction_reversal_threshold=custom,
        )
        assert exits["conviction_reversal"] == pytest.approx(custom)
