"""Tests for arbiter.trust.coverage — coverage term."""
from __future__ import annotations

import pytest

from arbiter.contract.seams import ResolvedOutcome
from arbiter.trust.coverage import coverage_score


def _make_outcome(idea_id: str, advisor_id: str = "A1.test", abstained: bool = False) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker="AAPL",
        alpha_bps=50.0,
        binary=1,
        advisor_confidence=0.8,
        stance_score=1.0,
        abstained=abstained,
        horizon_days=30,
        label_kind="normal",
    )


class TestCoverageScore:
    def test_full_coverage(self):
        """Advisor opined on all eligible ideas → coverage = 1.0."""
        eligible = ["idea-1", "idea-2", "idea-3"]
        outcomes = [_make_outcome(iid) for iid in eligible]
        score = coverage_score(outcomes, eligible)
        assert abs(score - 1.0) < 1e-9

    def test_zero_eligible_returns_zero(self):
        """No eligible ideas → coverage = 0.0 (no division by zero)."""
        score = coverage_score([], [])
        assert score == 0.0

    def test_half_coverage(self):
        eligible = ["idea-1", "idea-2", "idea-3", "idea-4"]
        outcomes = [_make_outcome("idea-1"), _make_outcome("idea-2")]
        score = coverage_score(outcomes, eligible)
        assert abs(score - 0.5) < 1e-9

    def test_coverage_penalizes_selective_abstainer(self):
        """An advisor who only opines on easy ideas (low coverage) is penalised."""
        eligible = [f"idea-{i}" for i in range(10)]
        # Only opined on 2 out of 10
        outcomes = [_make_outcome("idea-0"), _make_outcome("idea-1")]
        score = coverage_score(outcomes, eligible)
        assert score == 0.2

    def test_abstained_rows_count_as_covered(self):
        """Abstained outcomes still count — advisor was assigned and made a choice."""
        eligible = ["idea-1", "idea-2"]
        outcomes = [
            _make_outcome("idea-1", abstained=False),
            _make_outcome("idea-2", abstained=True),  # abstained but still assigned
        ]
        score = coverage_score(outcomes, eligible)
        assert abs(score - 1.0) < 1e-9

    def test_outcomes_outside_eligible_set_ignored(self):
        """Outcomes for non-eligible ideas do not inflate coverage."""
        eligible = ["idea-1", "idea-2"]
        outcomes = [
            _make_outcome("idea-1"),
            _make_outcome("idea-999"),  # not in eligible set
        ]
        score = coverage_score(outcomes, eligible)
        assert abs(score - 0.5) < 1e-9

    def test_duplicate_outcomes_for_same_idea_not_double_counted(self):
        """Multiple outcome rows for the same idea count as one opined idea."""
        eligible = ["idea-1", "idea-2", "idea-3"]
        outcomes = [
            _make_outcome("idea-1"),
            _make_outcome("idea-1"),  # duplicate
            _make_outcome("idea-1"),  # another duplicate
        ]
        score = coverage_score(outcomes, eligible)
        # Only 1 out of 3 eligible ideas covered
        assert abs(score - 1.0 / 3.0) < 1e-9

    def test_coverage_capped_at_one(self):
        """Coverage cannot exceed 1.0."""
        eligible = ["idea-1"]
        outcomes = [_make_outcome("idea-1"), _make_outcome("idea-2")]
        score = coverage_score(outcomes, eligible)
        assert score <= 1.0
        assert abs(score - 1.0) < 1e-9
