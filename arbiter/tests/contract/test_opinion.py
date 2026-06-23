"""Tests for Opinion dataclass and validate_opinion — Lane 9 core.

Covers INTERFACES.md §2 and §10b.1.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from arbiter.contract.opinion import Opinion, validate_opinion, AdvisorRegistry
from arbiter.types import ConfidenceSource, HorizonBucket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _good_opinion(**overrides) -> Opinion:
    """Return a valid Opinion, with optional field overrides for testing."""
    defaults = dict(
        advisor_id="A1.insider",
        ticker="AAPL",
        stance_score=0.5,
        confidence=0.8,
        confidence_source=ConfidenceSource.EMPIRICAL,
        horizon_days=30,
        as_of=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        rationale="Strong cluster buy from multiple insiders",
        source_fingerprint="sha256:abc123def456",
        run_group_id="01ABCDEF01234567890ABCDE",
    )
    defaults.update(overrides)
    return Opinion(**defaults)


# ---------------------------------------------------------------------------
# validate_opinion: valid opinion passes
# ---------------------------------------------------------------------------

class TestValidateOpinionAcceptsGoodOpinion:
    def test_valid_opinion_no_exception(self):
        op = _good_opinion()
        validate_opinion(op)  # must not raise

    def test_boundary_stance_positive_1(self):
        op = _good_opinion(stance_score=1.0)
        validate_opinion(op)

    def test_boundary_stance_negative_1(self):
        op = _good_opinion(stance_score=-1.0)
        validate_opinion(op)

    def test_boundary_confidence_0(self):
        op = _good_opinion(confidence=0.0)
        validate_opinion(op)

    def test_boundary_confidence_1(self):
        op = _good_opinion(confidence=1.0)
        validate_opinion(op)

    def test_horizon_days_1(self):
        """horizon_days=1 is the minimum valid value."""
        op = _good_opinion(horizon_days=1)
        validate_opinion(op)

    def test_horizon_days_365(self):
        """horizon_days=365 maps to LONG bucket without raising."""
        op = _good_opinion(horizon_days=365)
        validate_opinion(op)


# ---------------------------------------------------------------------------
# validate_opinion: field violations
# ---------------------------------------------------------------------------

class TestValidateOpinionRejectsInvalidFields:
    def test_stance_above_1(self):
        op = _good_opinion(stance_score=1.01)
        with pytest.raises(ValueError, match="stance_score"):
            validate_opinion(op)

    def test_stance_below_neg_1(self):
        op = _good_opinion(stance_score=-1.01)
        with pytest.raises(ValueError, match="stance_score"):
            validate_opinion(op)

    def test_confidence_above_1(self):
        op = _good_opinion(confidence=1.001)
        with pytest.raises(ValueError, match="confidence"):
            validate_opinion(op)

    def test_confidence_below_0(self):
        op = _good_opinion(confidence=-0.001)
        with pytest.raises(ValueError, match="confidence"):
            validate_opinion(op)

    def test_horizon_days_zero(self):
        op = _good_opinion(horizon_days=0)
        with pytest.raises(ValueError):
            validate_opinion(op)

    def test_horizon_days_negative(self):
        op = _good_opinion(horizon_days=-5)
        with pytest.raises(ValueError):
            validate_opinion(op)

    def test_naive_as_of(self):
        """Non-tz-aware as_of must be rejected."""
        naive = datetime(2026, 1, 15, 12, 0, 0)  # no tzinfo
        op = _good_opinion(as_of=naive)
        with pytest.raises(ValueError, match="tz-aware"):
            validate_opinion(op)

    def test_empty_advisor_id(self):
        op = _good_opinion(advisor_id="")
        with pytest.raises(ValueError, match="advisor_id"):
            validate_opinion(op)

    def test_empty_ticker(self):
        op = _good_opinion(ticker="")
        with pytest.raises(ValueError, match="ticker"):
            validate_opinion(op)

    def test_empty_source_fingerprint(self):
        op = _good_opinion(source_fingerprint="")
        with pytest.raises(ValueError, match="source_fingerprint"):
            validate_opinion(op)

    def test_empty_run_group_id(self):
        op = _good_opinion(run_group_id="")
        with pytest.raises(ValueError, match="run_group_id"):
            validate_opinion(op)


# ---------------------------------------------------------------------------
# §10b.1: horizon_days > 365 must surface a ValueError
# ---------------------------------------------------------------------------

class TestHorizonDaysOver365:
    def test_horizon_bucket_raises_for_over_365(self):
        """horizon_bucket property raises ValueError for horizon_days > 365."""
        op = _good_opinion(horizon_days=366)
        with pytest.raises(ValueError):
            _ = op.horizon_bucket

    def test_validate_surfaces_over_365_error(self):
        """validate_opinion surfaces the bucket_for_days error for days > 365."""
        op = _good_opinion(horizon_days=400)
        with pytest.raises(ValueError):
            validate_opinion(op)

    def test_validate_message_includes_context(self):
        """The ValueError message gives the caller actionable context."""
        op = _good_opinion(horizon_days=500)
        with pytest.raises(ValueError) as exc_info:
            validate_opinion(op)
        msg = str(exc_info.value)
        # Should mention the violation somehow (Opinion contract violations or horizon)
        assert "horizon" in msg.lower() or "365" in msg or "500" in msg


# ---------------------------------------------------------------------------
# horizon_bucket property
# ---------------------------------------------------------------------------

class TestHorizonBucketProperty:
    @pytest.mark.parametrize("days,expected", [
        (0.5, HorizonBucket.INTRADAY),  # < 1 day (fractional hours)
        (1,   HorizonBucket.SHORT),
        (30,  HorizonBucket.SHORT),
        (31,  HorizonBucket.MEDIUM),
        (120, HorizonBucket.MEDIUM),
        (121, HorizonBucket.LONG),
        (365, HorizonBucket.LONG),
    ])
    def test_bucket_mapping(self, days, expected):
        """horizon_bucket maps correctly for all bucket boundaries."""
        # Use float horizon_days for INTRADAY; int for others
        op = _good_opinion(horizon_days=days if isinstance(days, int) else int(days) or 1)
        if days < 1:
            # Need fractional days — use float horizon_days via bucket_for_days directly
            from arbiter.types import bucket_for_days
            assert bucket_for_days(days) == expected
        else:
            assert op.horizon_bucket == expected

    def test_intraday_via_bucket_for_days(self):
        """bucket_for_days maps < 1 day to INTRADAY."""
        from arbiter.types import bucket_for_days
        assert bucket_for_days(0.5) == HorizonBucket.INTRADAY

    def test_short_bucket(self):
        op = _good_opinion(horizon_days=15)
        assert op.horizon_bucket == HorizonBucket.SHORT

    def test_medium_bucket(self):
        op = _good_opinion(horizon_days=60)
        assert op.horizon_bucket == HorizonBucket.MEDIUM

    def test_long_bucket(self):
        op = _good_opinion(horizon_days=200)
        assert op.horizon_bucket == HorizonBucket.LONG


# ---------------------------------------------------------------------------
# AdvisorRegistry
# ---------------------------------------------------------------------------

class TestAdvisorRegistry:
    def test_register_and_all_ids(self):
        reg = AdvisorRegistry()
        reg.register("A1.insider")
        reg.register("A1.congress")
        reg.register("A2.mirofish", hard_weight_cap=0.35)
        ids = reg.all_ids()
        assert "A1.insider" in ids
        assert "A1.congress" in ids
        assert "A2.mirofish" in ids

    def test_all_ids_sorted(self):
        reg = AdvisorRegistry()
        reg.register("C")
        reg.register("A")
        reg.register("B")
        assert reg.all_ids() == ["A", "B", "C"]

    def test_all_ids_empty(self):
        reg = AdvisorRegistry()
        assert reg.all_ids() == []

    def test_register_overwrites(self):
        """Re-registering the same ID replaces metadata."""
        reg = AdvisorRegistry()
        reg.register("A1.insider")
        reg.register("A1.insider", hard_weight_cap=0.5)
        meta = reg.get_metadata("A1.insider")
        assert meta["hard_weight_cap"] == 0.5

    def test_register_empty_advisor_id_raises(self):
        reg = AdvisorRegistry()
        with pytest.raises(ValueError):
            reg.register("")

    def test_get_metadata_missing_raises(self):
        reg = AdvisorRegistry()
        with pytest.raises(KeyError):
            reg.get_metadata("nonexistent")
