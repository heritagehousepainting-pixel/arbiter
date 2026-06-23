"""Tests for arbiter.orchestrator.revision — post-execution revision rules (Lane 13).

Covers all three revision rule branches (§3.6):
    EXIT   — sign flip OR |conviction| < 0.25
    REDUCE — |conviction| < 0.50 (same sign, >= 0.25)
    HOLD   — |conviction| >= 0.50 (same sign)

Also covers:
    - ValueError when original_conviction is 0.0
    - Exact boundary values at 0.25 and 0.50 thresholds
    - RevisionResult attributes populated correctly
"""
from __future__ import annotations

import pytest

from arbiter.orchestrator.revision import (
    EXIT_THRESHOLD,
    REDUCE_THRESHOLD,
    RevisionAction,
    RevisionResult,
    evaluate_revision,
)


# ---------------------------------------------------------------------------
# EXIT cases
# ---------------------------------------------------------------------------

class TestExitRule:
    def test_sign_flip_long_to_short(self):
        """Original long, current short → EXIT."""
        result = evaluate_revision(current_conviction=-0.6, original_conviction=+0.7)
        assert result.action is RevisionAction.EXIT
        assert "flip" in result.reason.lower()

    def test_sign_flip_short_to_long(self):
        """Original short, current long → EXIT."""
        result = evaluate_revision(current_conviction=+0.5, original_conviction=-0.8)
        assert result.action is RevisionAction.EXIT

    def test_sign_flip_to_zero(self):
        """Original long, current zero → treat as flip → EXIT."""
        result = evaluate_revision(current_conviction=0.0, original_conviction=+0.5)
        assert result.action is RevisionAction.EXIT

    def test_sign_flip_short_to_zero(self):
        """Original short, current zero → EXIT."""
        result = evaluate_revision(current_conviction=0.0, original_conviction=-0.5)
        assert result.action is RevisionAction.EXIT

    def test_below_exit_threshold_same_sign(self):
        """|conviction| < 0.25, same sign → EXIT."""
        result = evaluate_revision(current_conviction=+0.20, original_conviction=+0.8)
        assert result.action is RevisionAction.EXIT
        assert "exit threshold" in result.reason.lower()

    def test_exactly_at_zero_conviction(self):
        """conviction = 0 is treated as sign flip for longs."""
        result = evaluate_revision(current_conviction=0.0, original_conviction=+0.9)
        assert result.action is RevisionAction.EXIT

    def test_very_small_positive_original_long_below_threshold(self):
        result = evaluate_revision(current_conviction=+0.05, original_conviction=+0.7)
        assert result.action is RevisionAction.EXIT

    def test_exit_threshold_constant_is_0_25(self):
        assert EXIT_THRESHOLD == 0.25

    def test_reduce_threshold_constant_is_0_50(self):
        assert REDUCE_THRESHOLD == 0.50

    def test_strong_sign_flip_with_high_magnitude(self):
        """Even high |conviction| exits if sign flipped."""
        result = evaluate_revision(current_conviction=-0.95, original_conviction=+0.9)
        assert result.action is RevisionAction.EXIT


# ---------------------------------------------------------------------------
# REDUCE cases
# ---------------------------------------------------------------------------

class TestReduceRule:
    def test_reduce_same_sign_below_0_50(self):
        """|conviction| < 0.50, same sign, >= 0.25 → REDUCE."""
        result = evaluate_revision(current_conviction=+0.30, original_conviction=+0.8)
        assert result.action is RevisionAction.REDUCE
        assert "reduce" in result.reason.lower()

    def test_reduce_for_short_position(self):
        result = evaluate_revision(current_conviction=-0.35, original_conviction=-0.8)
        assert result.action is RevisionAction.REDUCE

    def test_reduce_at_exit_threshold_boundary(self):
        """Exactly at EXIT_THRESHOLD (0.25) — this is REDUCE, not EXIT (exclusive lower bound)."""
        result = evaluate_revision(current_conviction=+0.25, original_conviction=+0.9)
        assert result.action is RevisionAction.REDUCE

    def test_reduce_just_below_0_50(self):
        result = evaluate_revision(current_conviction=+0.49, original_conviction=+0.7)
        assert result.action is RevisionAction.REDUCE

    def test_reduce_short_at_lower_boundary(self):
        result = evaluate_revision(current_conviction=-0.25, original_conviction=-0.6)
        assert result.action is RevisionAction.REDUCE


# ---------------------------------------------------------------------------
# HOLD cases
# ---------------------------------------------------------------------------

class TestHoldRule:
    def test_hold_at_reduce_threshold(self):
        """Exactly at REDUCE_THRESHOLD (0.50) — HOLD."""
        result = evaluate_revision(current_conviction=+0.50, original_conviction=+0.8)
        assert result.action is RevisionAction.HOLD

    def test_hold_above_reduce_threshold(self):
        result = evaluate_revision(current_conviction=+0.75, original_conviction=+0.8)
        assert result.action is RevisionAction.HOLD
        assert "hold" in result.reason.lower()

    def test_hold_high_conviction_long(self):
        result = evaluate_revision(current_conviction=+0.95, original_conviction=+0.9)
        assert result.action is RevisionAction.HOLD

    def test_hold_high_conviction_short(self):
        result = evaluate_revision(current_conviction=-0.80, original_conviction=-0.7)
        assert result.action is RevisionAction.HOLD

    def test_hold_conviction_unchanged(self):
        """Conviction exactly matches original → HOLD."""
        result = evaluate_revision(current_conviction=+0.80, original_conviction=+0.80)
        assert result.action is RevisionAction.HOLD


# ---------------------------------------------------------------------------
# RevisionResult attributes
# ---------------------------------------------------------------------------

class TestRevisionResultAttributes:
    def test_result_carries_current_conviction(self):
        result = evaluate_revision(current_conviction=+0.60, original_conviction=+0.7)
        assert result.conviction == +0.60

    def test_result_carries_original_side_long(self):
        result = evaluate_revision(current_conviction=+0.60, original_conviction=+0.7)
        assert result.original_side == "long"

    def test_result_carries_original_side_short(self):
        result = evaluate_revision(current_conviction=-0.35, original_conviction=-0.8)
        assert result.original_side == "short"

    def test_result_has_non_empty_reason(self):
        for action_case in [
            (0.80, 0.70),  # HOLD
            (0.35, 0.70),  # REDUCE
            (-0.50, 0.70), # EXIT (flip)
            (0.10, 0.70),  # EXIT (below threshold)
        ]:
            cur, orig = action_case
            result = evaluate_revision(current_conviction=cur, original_conviction=orig)
            assert isinstance(result.reason, str) and len(result.reason) > 0

    def test_result_is_frozen(self):
        result = evaluate_revision(current_conviction=+0.60, original_conviction=+0.7)
        assert isinstance(result, RevisionResult)
        with pytest.raises((AttributeError, TypeError)):
            result.action = RevisionAction.EXIT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    def test_raises_for_zero_original_conviction(self):
        with pytest.raises(ValueError, match="original_conviction"):
            evaluate_revision(current_conviction=0.5, original_conviction=0.0)
