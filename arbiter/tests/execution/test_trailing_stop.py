"""Tier-3 #10 — LONG horizon 240→150 + deterministic trailing stop."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.execution.exit_monitor import (
    _TRAIL_LOCK,
    _trail_extreme,
    apply_trailing_stop,
    evaluate_triggers,
    recompute_stop,
)
from arbiter.policy.exits import compute_exits
from arbiter.types import HorizonBucket, OrderSide

_AS_OF = datetime(2025, 3, 17, 14, 0, 0, tzinfo=timezone.utc)


class TestApplyTrailingStop:
    def test_long_below_trigger_keeps_base(self):
        base = recompute_stop(100.0, HorizonBucket.LONG)  # 92.0
        assert apply_trailing_stop(base, 100.0, 105.0) == base  # +5% < trigger

    def test_long_above_trigger_ratchets_up(self):
        base = recompute_stop(100.0, HorizonBucket.LONG)  # 92.0
        tightened = apply_trailing_stop(base, 100.0, 115.0)  # +15% ≥ trigger
        assert tightened == 115.0 * (1.0 - _TRAIL_LOCK)  # 108.1 — locks a gain
        assert tightened > 100.0  # above entry: profit locked

    def test_never_loosens(self):
        # Extreme barely at trigger: hw×(1−lock) could fall below base → keep base.
        base = 99.0
        assert apply_trailing_stop(base, 100.0, 101.0) == base

    def test_short_mirror(self):
        base = recompute_stop(100.0, HorizonBucket.LONG, is_short=True)  # 108.0
        tightened = apply_trailing_stop(base, 100.0, 85.0, is_short=True)
        assert tightened == 85.0 * (1.0 + _TRAIL_LOCK)  # 90.1 — locks the gain
        assert tightened < 100.0


class TestTrailExtreme:
    def _pit(self, ticker, closes):
        """Closes land on Wed/Thu/Fri (Mar 12–14) — the walk skips weekends."""
        fx = FixtureSource()
        pit = PITGateway()
        pit.register_source("price_close", fx)
        for i, c in enumerate(closes):
            fx.add(
                "price_close", ticker,
                _AS_OF - timedelta(days=len(closes) + 2 - i), c,
            )
        return pit

    def test_highwater_of_recent_closes(self):
        pit = self._pit("AAPL", [100.0, 118.0, 110.0])
        entry = (_AS_OF - timedelta(days=10)).date()
        assert _trail_extreme(pit, "AAPL", entry, _AS_OF) == 118.0

    def test_lowwater_for_short(self):
        pit = self._pit("MS", [100.0, 84.0, 90.0])
        entry = (_AS_OF - timedelta(days=10)).date()
        assert _trail_extreme(pit, "MS", entry, _AS_OF, is_short=True) == 84.0

    def test_no_data_returns_none(self):
        pit = PITGateway()
        pit.register_source("price_close", FixtureSource())
        entry = (_AS_OF - timedelta(days=10)).date()
        assert _trail_extreme(pit, "GRMN", entry, _AS_OF) is None


class TestTrailingTriggerIntegration:
    def test_giveback_after_run_fires_trailing_stop(self):
        """+15% run then a pullback to +7%: base stop silent, trail fires."""
        common = dict(
            avg_price=100.0,
            bucket=HorizonBucket.LONG,
            horizon_expiry=_AS_OF.date() + timedelta(days=100),
            current_price=107.0,  # above base stop (92), below hw lock (108.1)
            current_stance=None,
            now=_AS_OF,
        )
        assert evaluate_triggers(**common) is None  # no trail → holds
        d = evaluate_triggers(**common, trail_extreme=115.0)
        assert d is not None and d.reason == "stop_loss"


def test_long_horizon_is_150_days():
    exits = compute_exits(
        bucket=HorizonBucket.LONG, side=OrderSide.BUY,
        entry_price=100.0, entry_date=date(2026, 7, 2),
    )
    assert exits["horizon_expiry"] == date(2026, 7, 2) + timedelta(days=150)
