"""Tests for arbiter.trust.regime — regime freeze + 2× post-regime weights."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from arbiter.contract.seams import ResolvedOutcome
from arbiter.trust.regime import (
    RegimeChangeEvent,
    RegimeTracker,
    apply_regime_weights,
    FREEZE_DAYS,
    POST_REGIME_MULTIPLIER,
)


def _utc(days_ago: float = 0.0) -> datetime:
    base = datetime(2025, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    return base - timedelta(days=days_ago)


AS_OF = _utc(0)


def _make_outcome(idea_id: str = "i1") -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id="A1.test",
        ticker="X",
        alpha_bps=30.0,
        binary=1,
        advisor_confidence=0.7,
        stance_score=1.0,
        abstained=False,
        horizon_days=30,
        label_kind="normal",
    )


class TestRegimeTracker:
    def test_no_events_not_frozen(self):
        tracker = RegimeTracker()
        assert not tracker.is_frozen(AS_OF)

    def test_recent_event_is_frozen(self):
        """Changed 5 days ago → still frozen (< 21 days)."""
        event = RegimeChangeEvent(regime_id="bear", changed_at=_utc(5))
        tracker = RegimeTracker(regime_events=[event])
        assert tracker.is_frozen(AS_OF)

    def test_old_event_is_not_frozen(self):
        """Changed 22 days ago → freeze window expired."""
        event = RegimeChangeEvent(regime_id="bear", changed_at=_utc(22))
        tracker = RegimeTracker(regime_events=[event])
        assert not tracker.is_frozen(AS_OF)

    def test_exactly_at_freeze_boundary(self):
        """Changed exactly FREEZE_DAYS ago → NOT frozen (boundary exclusive)."""
        event = RegimeChangeEvent(regime_id="bear", changed_at=_utc(FREEZE_DAYS))
        tracker = RegimeTracker(regime_events=[event])
        # timedelta comparison: (as_of - changed_at) < timedelta(21) is False at exactly 21d
        assert not tracker.is_frozen(AS_OF)

    def test_freeze_uses_most_recent_event(self):
        """Only the latest event matters for freeze."""
        old_event = RegimeChangeEvent(regime_id="bull", changed_at=_utc(30))
        new_event = RegimeChangeEvent(regime_id="bear", changed_at=_utc(3))
        tracker = RegimeTracker(regime_events=[old_event, new_event])
        assert tracker.is_frozen(AS_OF)

    def test_regime_at_returns_correct_regime(self):
        e1 = RegimeChangeEvent(regime_id="bull", changed_at=_utc(60))
        e2 = RegimeChangeEvent(regime_id="bear", changed_at=_utc(10))
        tracker = RegimeTracker(regime_events=[e1, e2])
        assert tracker.regime_at(AS_OF) == "bear"

    def test_regime_at_before_any_event_returns_none(self):
        e = RegimeChangeEvent(regime_id="bull", changed_at=_utc(0))  # changed "now"
        tracker = RegimeTracker(regime_events=[e])
        # as_of before the event
        assert tracker.regime_at(_utc(5)) is None

    def test_no_events_last_regime_change_is_none(self):
        tracker = RegimeTracker()
        assert tracker.last_regime_change() is None


class TestApplyRegimeWeights:
    def test_no_regime_events_weights_unchanged(self):
        tracker = RegimeTracker()
        outcomes = [_make_outcome()]
        dates = [AS_OF]
        base_weights = [1.0]
        adjusted = apply_regime_weights(outcomes, dates, base_weights, tracker)
        assert adjusted == [1.0]

    def test_post_regime_outcome_doubled(self):
        """Outcome after regime change gets 2× weight."""
        changed_at = _utc(10)  # regime changed 10 days ago
        tracker = RegimeTracker(
            regime_events=[RegimeChangeEvent(regime_id="bear", changed_at=changed_at)]
        )
        post_date = _utc(5)   # 5 days ago → after regime change
        pre_date = _utc(20)   # 20 days ago → before regime change

        outcomes = [_make_outcome("i1"), _make_outcome("i2")]
        dates = [post_date, pre_date]
        base_weights = [1.0, 1.0]

        adjusted = apply_regime_weights(outcomes, dates, base_weights, tracker)
        assert adjusted[0] == 1.0 * POST_REGIME_MULTIPLIER  # post-regime: 2×
        assert adjusted[1] == 1.0                           # pre-regime: unchanged

    def test_length_mismatch_raises(self):
        tracker = RegimeTracker()
        with pytest.raises(ValueError):
            apply_regime_weights([_make_outcome()], [AS_OF], [], tracker)

    def test_all_pre_regime_unchanged(self):
        """All outcomes before regime change: no multiplier applied."""
        changed_at = _utc(5)
        tracker = RegimeTracker(
            regime_events=[RegimeChangeEvent(regime_id="bull", changed_at=changed_at)]
        )
        outcomes = [_make_outcome("i1"), _make_outcome("i2")]
        dates = [_utc(10), _utc(20)]  # both before regime change
        base_weights = [0.5, 0.25]

        adjusted = apply_regime_weights(outcomes, dates, base_weights, tracker)
        assert adjusted == [0.5, 0.25]
