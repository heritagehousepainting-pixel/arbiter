"""Tests for arbiter.safety.quorum — quorum gate (Lane L4).

Covers INTERFACES.md §8 quorum rules:
    - 0 live advisors  → HALTED,   multiplier=0.0
    - 1 live advisor   → DEGRADED, multiplier=0.25
    - 2+ live advisors → NORMAL,   multiplier=1.0
"""
from __future__ import annotations

import pytest

from arbiter.safety.quorum import QuorumResult, assess_quorum
from arbiter.types import DegradationLevel


class TestAssessQuorumZeroAdvisors:
    """0 live advisors → HALTED, size_multiplier=0.0."""

    def test_level_is_halted(self) -> None:
        result = assess_quorum(0)
        assert result.level == DegradationLevel.HALTED

    def test_multiplier_is_zero(self) -> None:
        result = assess_quorum(0)
        assert result.size_multiplier == pytest.approx(0.0)

    def test_reasons_non_empty(self) -> None:
        result = assess_quorum(0)
        assert len(result.reasons) >= 1
        # Must mention something about halted / 0 advisors
        combined = " ".join(result.reasons).lower()
        assert "halt" in combined or "0" in combined


class TestAssessQuorumOneAdvisor:
    """1 live advisor → DEGRADED, size_multiplier=0.25."""

    def test_level_is_degraded(self) -> None:
        result = assess_quorum(1)
        assert result.level == DegradationLevel.DEGRADED

    def test_multiplier_is_quarter(self) -> None:
        result = assess_quorum(1)
        assert result.size_multiplier == pytest.approx(0.25)

    def test_reasons_mention_degraded(self) -> None:
        result = assess_quorum(1)
        assert len(result.reasons) >= 1
        combined = " ".join(result.reasons).lower()
        assert "degraded" in combined or "1" in combined


class TestAssessQuorumTwoAdvisors:
    """2 live advisors → NORMAL, size_multiplier=1.0."""

    def test_level_is_normal(self) -> None:
        result = assess_quorum(2)
        assert result.level == DegradationLevel.NORMAL

    def test_multiplier_is_one(self) -> None:
        result = assess_quorum(2)
        assert result.size_multiplier == pytest.approx(1.0)

    def test_reasons_empty(self) -> None:
        """Full quorum should produce no warning reasons."""
        result = assess_quorum(2)
        assert result.reasons == []


class TestAssessQuorumManyAdvisors:
    """3+ live advisors → NORMAL, multiplier=1.0."""

    @pytest.mark.parametrize("count", [3, 5, 10, 100])
    def test_level_normal_for_many(self, count: int) -> None:
        result = assess_quorum(count)
        assert result.level == DegradationLevel.NORMAL
        assert result.size_multiplier == pytest.approx(1.0)


class TestAssessQuorumReturn:
    """QuorumResult is a frozen dataclass."""

    def test_result_is_quorum_result(self) -> None:
        result = assess_quorum(2)
        assert isinstance(result, QuorumResult)

    def test_result_frozen(self) -> None:
        result = assess_quorum(1)
        with pytest.raises((AttributeError, TypeError)):
            result.level = DegradationLevel.NORMAL  # type: ignore[misc]


class TestAssessQuorumValidation:
    """Negative counts are rejected."""

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            assess_quorum(-1)

    def test_large_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            assess_quorum(-99)
