"""Tests for the STANCE_BASE cold-start prior table (Lane 9)."""
from __future__ import annotations

import pytest

from arbiter.calibration.stance_base import lookup_prior, _stance_bin, STANCE_BASE
from arbiter.types import HorizonBucket


class TestStanceBin:
    """Verify that _stance_bin correctly buckets raw stances."""

    def test_strong_short(self) -> None:
        assert _stance_bin(-1.0) == -2
        assert _stance_bin(-0.7) == -2

    def test_short(self) -> None:
        assert _stance_bin(-0.6) == -1   # boundary: < -0.2 → -1
        assert _stance_bin(-0.4) == -1

    def test_neutral(self) -> None:
        assert _stance_bin(0.0) == 0
        assert _stance_bin(-0.19) == 0
        assert _stance_bin(0.19) == 0

    def test_long(self) -> None:
        assert _stance_bin(0.2) == 1
        assert _stance_bin(0.4) == 1

    def test_strong_long(self) -> None:
        assert _stance_bin(0.6) == 2
        assert _stance_bin(1.0) == 2


class TestLookupPrior:
    """Tests for the lookup_prior function."""

    def test_neutral_stance_returns_half(self) -> None:
        """Neutral stance (0.0) must return 0.50 across all advisor types and horizons."""
        for advisor_id in ("A1.insider", "A1.congress", "A2.mirofish", "A3.quant"):
            for bucket in HorizonBucket:
                prob = lookup_prior(advisor_id, 0.0, bucket)
                assert prob == pytest.approx(0.50), (
                    f"{advisor_id}, {bucket}: expected 0.50 for neutral stance, got {prob}"
                )

    def test_positive_stance_gives_prob_above_half(self) -> None:
        """Positive stance should give P > 0.5 for all advisors."""
        for advisor_id in ("A1.insider", "A2.mirofish", "A3.quant"):
            prob = lookup_prior(advisor_id, 0.8, HorizonBucket.SHORT)
            assert prob > 0.5, f"{advisor_id}: prob={prob} not > 0.5 for strong long"

    def test_negative_stance_gives_prob_below_half(self) -> None:
        """Negative stance should give P < 0.5 for all advisors."""
        for advisor_id in ("A1.insider", "A2.mirofish", "A3.quant"):
            prob = lookup_prior(advisor_id, -0.8, HorizonBucket.SHORT)
            assert prob < 0.5, f"{advisor_id}: prob={prob} not < 0.5 for strong short"

    def test_monotone_stance_to_prob(self) -> None:
        """Probability must be non-decreasing as stance increases."""
        stances = [-1.0, -0.5, 0.0, 0.5, 1.0]
        probs = [lookup_prior("A1.insider", s, HorizonBucket.SHORT) for s in stances]
        for i in range(len(probs) - 1):
            assert probs[i] <= probs[i + 1], (
                f"Non-monotone: prob[{stances[i]}]={probs[i]} > prob[{stances[i+1]}]={probs[i+1]}"
            )

    def test_probabilities_in_unit_interval(self) -> None:
        """All priors must be strictly in (0, 1)."""
        for advisor_id in ("A1.insider", "A2.mirofish", "A3.quant", "UNKNOWN.x"):
            for bucket in HorizonBucket:
                for stance in (-1.0, -0.5, 0.0, 0.5, 1.0):
                    prob = lookup_prior(advisor_id, stance, bucket)
                    assert 0.0 < prob < 1.0, (
                        f"{advisor_id}, {bucket}, stance={stance}: prob={prob} not in (0,1)"
                    )

    def test_unknown_advisor_uses_default(self) -> None:
        """Unknown advisor type should fall back to the '*' default row."""
        # UNKNOWN.x is not in STANCE_BASE; must not raise.
        prob = lookup_prior("UNKNOWN.x", 0.8, HorizonBucket.SHORT)
        assert prob > 0.5

    def test_advisor_without_dot_uses_full_id_or_default(self) -> None:
        """Advisor IDs without a dot are looked up as-is, fall back to '*'."""
        prob = lookup_prior("A1", 0.0, HorizonBucket.SHORT)
        # A1 IS a known type — should be 0.50 for neutral.
        assert prob == pytest.approx(0.50)

    def test_all_horizons_covered_for_known_advisors(self) -> None:
        """All HorizonBuckets must be covered for known advisor types."""
        for advisor_type_prefix in ("A1", "A2", "A3"):
            for bucket in HorizonBucket:
                assert bucket in STANCE_BASE[advisor_type_prefix], (
                    f"STANCE_BASE[{advisor_type_prefix!r}] missing {bucket!r}"
                )
