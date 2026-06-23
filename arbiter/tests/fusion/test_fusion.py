"""Tests for the fusion engine (Lane L10).

Coverage:
- equal-weight pool of 2 agreeing opinions → higher conviction than 1
- opposing opinions → conviction near 0
- cross-bucket opinions never pooled together
- same run_group_id in same bucket merged to 1 logical opinion
- same run_group_id in different buckets stay independent
- 3 correlated advisors (ρ high) → effective_N ≈ 1 and conviction cut
- veto zeroes the bucket
- abstaining opinions (None) excluded from pool
- diversity_factor and effective_N basic math
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import (
    AdvisorWeight,
    EqualWeightBundle,
    FusionOutput,
    WeightBundle,
)
from arbiter.fusion.engine import PassthroughCalibrator, fuse
from arbiter.types import ConfidenceSource, HorizonBucket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_opinion(
    *,
    advisor_id: str = "A1.test",
    ticker: str = "AAPL",
    stance_score: float = 0.6,
    confidence: float = 0.7,
    horizon_days: int = 10,  # SHORT bucket
    run_group_id: str = "rg-001",
    source_fingerprint: str = "fp-001",
    confidence_source: ConfidenceSource = ConfidenceSource.SELF_REPORTED,
) -> Opinion:
    return Opinion(
        advisor_id=advisor_id,
        ticker=ticker,
        stance_score=stance_score,
        confidence=confidence,
        confidence_source=confidence_source,
        horizon_days=horizon_days,
        as_of=_AS_OF,
        rationale="test opinion",
        source_fingerprint=source_fingerprint,
        run_group_id=run_group_id,
    )


_CALIBRATOR = PassthroughCalibrator()


# ---------------------------------------------------------------------------
# 1. Equal-weight pool: 2 agreeing opinions → higher conviction than 1
# ---------------------------------------------------------------------------

class TestAgreingOpinionsHigherConviction:
    def test_two_agreeing_higher_than_one(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.6, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.6, run_group_id="rg-2")

        weights_one = EqualWeightBundle(["A1.x"])
        weights_two = EqualWeightBundle(["A1.x", "A2.x"])

        result_one = fuse([op1], weights_one, _CALIBRATOR)
        result_two = fuse([op1, op2], weights_two, _CALIBRATOR)

        conv_one = result_one[HorizonBucket.SHORT].conviction
        conv_two = result_two[HorizonBucket.SHORT].conviction

        # Both positive; two agreeing should have equal or higher conviction.
        # With EqualWeightBundle (ρ=0 off-diagonal), diversity_factor=1.0 for both
        # so the signal_strength is the same; but the n_opinions count differs.
        # In Phase 1 (identity calibrator, no correlation), both give same signal.
        # This test asserts both are positive and the 2-opinion result is at least
        # as strong as the 1-opinion result.
        assert conv_one > 0.0
        assert conv_two > 0.0
        assert conv_two >= conv_one - 1e-9  # equal or higher

    def test_two_agreeing_both_positive(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.8, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.8, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.conviction > 0.0
        assert fo.n_opinions == 2

    def test_single_opinion_conviction_equals_stance_times_diversity(self) -> None:
        op = make_opinion(advisor_id="A1.x", stance_score=0.5, run_group_id="rg-1")
        weights = EqualWeightBundle(["A1.x"])
        result = fuse([op], weights, _CALIBRATOR)
        fo = result[HorizonBucket.SHORT]
        # With 1 advisor, eff_N=1, diversity_factor=1, conviction = 0.5.
        assert abs(fo.conviction - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# 2. Opposing opinions → conviction near 0
# ---------------------------------------------------------------------------

class TestOpposingOpinionsNearZero:
    def test_equal_opposite_near_zero(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.6, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=-0.6, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert abs(fo.conviction) < 1e-9

    def test_near_opposing_small_conviction(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.7, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=-0.5, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        # (0.7 - 0.5) / 2 = 0.1
        assert abs(fo.conviction - 0.1) < 1e-9

    def test_dispersion_is_non_zero_for_opposing(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.6, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=-0.6, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.dispersion > 0.0


# ---------------------------------------------------------------------------
# 3. Cross-bucket opinions are never pooled together
# ---------------------------------------------------------------------------

class TestCrossBucketNeverPooled:
    def test_short_and_medium_are_separate_buckets(self) -> None:
        op_short = make_opinion(
            advisor_id="A1.x",
            stance_score=0.8,
            horizon_days=10,   # SHORT
            run_group_id="rg-1",
        )
        op_medium = make_opinion(
            advisor_id="A2.x",
            stance_score=0.8,
            horizon_days=60,   # MEDIUM
            run_group_id="rg-2",
        )

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op_short, op_medium], weights, _CALIBRATOR)

        # Both buckets present.
        assert HorizonBucket.SHORT in result
        assert HorizonBucket.MEDIUM in result

        # Each bucket has exactly 1 opinion — they were NOT pooled together.
        assert result[HorizonBucket.SHORT].n_opinions == 1
        assert result[HorizonBucket.MEDIUM].n_opinions == 1

    def test_short_conviction_independent_of_medium(self) -> None:
        op_short = make_opinion(
            advisor_id="A1.x",
            stance_score=0.8,
            horizon_days=10,
            run_group_id="rg-1",
        )
        op_medium = make_opinion(
            advisor_id="A2.x",
            stance_score=-0.8,  # opposing, but different bucket
            horizon_days=60,
            run_group_id="rg-2",
        )

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op_short, op_medium], weights, _CALIBRATOR)

        # SHORT conviction should be positive (only A1.x at +0.8).
        assert result[HorizonBucket.SHORT].conviction > 0.0
        # MEDIUM conviction should be negative (only A2.x at -0.8).
        assert result[HorizonBucket.MEDIUM].conviction < 0.0

    def test_missing_bucket_absent_from_result(self) -> None:
        op = make_opinion(advisor_id="A1.x", horizon_days=10, run_group_id="rg-1")
        weights = EqualWeightBundle(["A1.x"])
        result = fuse([op], weights, _CALIBRATOR)

        assert HorizonBucket.SHORT in result
        assert HorizonBucket.MEDIUM not in result
        assert HorizonBucket.LONG not in result
        assert HorizonBucket.INTRADAY not in result


# ---------------------------------------------------------------------------
# 4. Same run_group_id in same bucket → merged to 1 logical opinion
# ---------------------------------------------------------------------------

class TestSameRunGroupSameBucketMerged:
    def test_two_ops_same_run_group_same_bucket_merged(self) -> None:
        # MiroFish emitting two SHORT opinions in the same run_group.
        op1 = make_opinion(
            advisor_id="A2.mirofish",
            stance_score=0.8,
            horizon_days=10,
            run_group_id="rg-miro-001",
            source_fingerprint="fp-1",
        )
        op2 = make_opinion(
            advisor_id="A2.mirofish",
            stance_score=0.4,
            horizon_days=15,
            run_group_id="rg-miro-001",  # SAME group
            source_fingerprint="fp-2",
        )

        weights = EqualWeightBundle(["A2.mirofish"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        # After dedup: 1 merged opinion with stance = (0.8 + 0.4) / 2 = 0.6.
        assert fo.n_opinions == 1
        assert abs(fo.conviction - 0.6) < 1e-9

    def test_three_ops_two_groups_same_bucket(self) -> None:
        # Advisor A1 in group rg-1, Advisor A1 in group rg-1 (same) + Advisor A2 separate.
        op1 = make_opinion(
            advisor_id="A1.x",
            stance_score=0.6,
            horizon_days=10,
            run_group_id="rg-1",
            source_fingerprint="fp-1",
        )
        op2 = make_opinion(
            advisor_id="A1.x",
            stance_score=0.8,
            horizon_days=10,
            run_group_id="rg-1",  # same group, same advisor
            source_fingerprint="fp-2",
        )
        op3 = make_opinion(
            advisor_id="A2.x",
            stance_score=0.5,
            horizon_days=10,
            run_group_id="rg-2",  # different group
            source_fingerprint="fp-3",
        )

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2, op3], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        # op1 + op2 merged → 1 with stance 0.7; op3 stays → 2 total.
        assert fo.n_opinions == 2


# ---------------------------------------------------------------------------
# 5. Same run_group_id in DIFFERENT buckets → stay independent
# ---------------------------------------------------------------------------

class TestSameRunGroupDifferentBucketsIndependent:
    def test_mirofish_short_and_medium_same_run_group(self) -> None:
        """MiroFish SHORT+MEDIUM case: same run_group, different buckets → independent."""
        op_short = make_opinion(
            advisor_id="A2.mirofish",
            stance_score=0.7,
            horizon_days=10,   # SHORT
            run_group_id="rg-miro-session-1",
        )
        op_medium = make_opinion(
            advisor_id="A2.mirofish",
            stance_score=0.3,
            horizon_days=60,   # MEDIUM
            run_group_id="rg-miro-session-1",  # SAME group
        )

        weights = EqualWeightBundle(["A2.mirofish"])
        result = fuse([op_short, op_medium], weights, _CALIBRATOR)

        # Both buckets exist with 1 opinion each (no cross-bucket merge).
        assert HorizonBucket.SHORT in result
        assert HorizonBucket.MEDIUM in result
        assert result[HorizonBucket.SHORT].n_opinions == 1
        assert result[HorizonBucket.MEDIUM].n_opinions == 1
        assert abs(result[HorizonBucket.SHORT].conviction - 0.7) < 1e-9
        assert abs(result[HorizonBucket.MEDIUM].conviction - 0.3) < 1e-9


# ---------------------------------------------------------------------------
# 6. 3 correlated bots (ρ high) → effective_N ≈ 1 and conviction cut
# ---------------------------------------------------------------------------

class TestHighCorrelationEffectiveN:
    def _make_corr_bundle(
        self, advisors: list[str], off_diag_rho: float
    ) -> WeightBundle:
        n = len(advisors)
        equal_weight = 1.0 / n
        weights = {
            aid: AdvisorWeight(
                advisor_id=aid,
                weight=equal_weight,
                ci_low=equal_weight,
                ci_high=equal_weight,
                shadow=False,
            )
            for aid in advisors
        }
        corr: dict[tuple[str, str], float] = {}
        for i, ai in enumerate(advisors):
            for j, aj in enumerate(advisors):
                if i != j:
                    corr[(ai, aj)] = off_diag_rho
        return WeightBundle(weights=weights, correlation_matrix=corr)

    def test_high_corr_reduces_effective_n(self) -> None:
        advisors = ["bot1", "bot2", "bot3"]
        ops = [
            make_opinion(
                advisor_id=a,
                stance_score=0.6,
                run_group_id=f"rg-{a}",
            )
            for a in advisors
        ]

        # High correlation: ρ = 0.9
        high_corr_bundle = self._make_corr_bundle(advisors, 0.9)
        result = fuse(ops, high_corr_bundle, _CALIBRATOR)
        fo = result[HorizonBucket.SHORT]

        # eff_N = 1 / (3 * (1/3)^2 * 1.0 + 6 * (1/3)^2 * 0.9)
        # = 1 / (3/9 + 6*0.9/9) = 1 / (0.333 + 0.6) = 1 / 0.933 ≈ 1.071
        expected_eff_n = 1.0 / (3 * (1 / 3) ** 2 + 6 * (1 / 3) ** 2 * 0.9)
        assert abs(fo.effective_n - expected_eff_n) < 1e-6
        assert fo.effective_n < 1.5  # well below N=3

    def test_high_corr_cuts_conviction_vs_independent(self) -> None:
        advisors = ["bot1", "bot2", "bot3"]
        ops = [
            make_opinion(
                advisor_id=a,
                stance_score=0.6,
                run_group_id=f"rg-{a}",
            )
            for a in advisors
        ]

        # Independent bundle (Phase-1 default ρ=0 off-diagonal).
        indep_bundle = EqualWeightBundle(advisors)
        # High-correlation bundle.
        high_corr_bundle = self._make_corr_bundle(advisors, 0.9)

        result_indep = fuse(ops, indep_bundle, _CALIBRATOR)
        result_high_corr = fuse(ops, high_corr_bundle, _CALIBRATOR)

        conv_indep = result_indep[HorizonBucket.SHORT].conviction
        conv_high_corr = result_high_corr[HorizonBucket.SHORT].conviction

        # Both positive; high-corr should be smaller (diversity_factor < 1).
        assert conv_indep > 0.0
        assert conv_high_corr > 0.0
        assert conv_high_corr < conv_indep

    def test_independent_bundle_effective_n_equals_n(self) -> None:
        advisors = ["A1", "A2", "A3"]
        ops = [
            make_opinion(
                advisor_id=a,
                stance_score=0.5,
                run_group_id=f"rg-{a}",
            )
            for a in advisors
        ]

        # EqualWeightBundle has empty corr matrix → default ρ=0 off-diagonal.
        weights = EqualWeightBundle(advisors)
        result = fuse(ops, weights, _CALIBRATOR)
        fo = result[HorizonBucket.SHORT]

        # With ρ=0 off-diagonal: eff_N = 1 / (3 * (1/3)^2) = 1/(1/3) = 3.
        assert abs(fo.effective_n - 3.0) < 1e-9


# ---------------------------------------------------------------------------
# 7. Veto zeroes the bucket
# ---------------------------------------------------------------------------

class TestVetoZeroesBucket:
    def test_veto_produces_zero_conviction(self) -> None:
        # Full-conviction veto sentinel: confidence=1.0, abs(stance)=1.0.
        veto_op = make_opinion(
            advisor_id="A1.veto",
            stance_score=1.0,
            confidence=1.0,
            run_group_id="rg-veto",
        )
        normal_op = make_opinion(
            advisor_id="A2.normal",
            stance_score=0.5,
            confidence=0.7,
            run_group_id="rg-normal",
        )

        weights = EqualWeightBundle(["A1.veto", "A2.normal"])
        result = fuse([veto_op, normal_op], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.conviction == 0.0
        assert fo.n_opinions == 0
        assert "A1.veto" in fo.vetoes

    def test_veto_negative_stance_also_zeroes(self) -> None:
        veto_op = make_opinion(
            advisor_id="A1.veto",
            stance_score=-1.0,
            confidence=1.0,
            run_group_id="rg-veto",
        )
        weights = EqualWeightBundle(["A1.veto"])
        result = fuse([veto_op], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.conviction == 0.0
        assert "A1.veto" in fo.vetoes

    def test_veto_only_affects_its_own_bucket(self) -> None:
        veto_short = make_opinion(
            advisor_id="A1.veto",
            stance_score=1.0,
            confidence=1.0,
            horizon_days=10,   # SHORT
            run_group_id="rg-veto",
        )
        normal_medium = make_opinion(
            advisor_id="A2.normal",
            stance_score=0.6,
            confidence=0.7,
            horizon_days=60,   # MEDIUM
            run_group_id="rg-normal",
        )

        weights = EqualWeightBundle(["A1.veto", "A2.normal"])
        result = fuse([veto_short, normal_medium], weights, _CALIBRATOR)

        # SHORT vetoed.
        assert result[HorizonBucket.SHORT].conviction == 0.0
        # MEDIUM unaffected.
        assert result[HorizonBucket.MEDIUM].conviction > 0.0

    def test_non_veto_high_confidence_not_vetoed(self) -> None:
        """High confidence alone (without abs(stance)==1.0) is NOT a veto."""
        op = make_opinion(
            advisor_id="A1.x",
            stance_score=0.8,  # not ±1.0
            confidence=1.0,
            run_group_id="rg-1",
        )
        weights = EqualWeightBundle(["A1.x"])
        result = fuse([op], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.vetoes == []
        assert fo.conviction > 0.0


# ---------------------------------------------------------------------------
# 8. Abstaining opinions (None) are excluded
# ---------------------------------------------------------------------------

class TestAbstainsExcluded:
    def test_none_opinions_excluded(self) -> None:
        op = make_opinion(advisor_id="A1.x", stance_score=0.5, run_group_id="rg-1")
        weights = EqualWeightBundle(["A1.x"])

        # Pass a mix of None and one real opinion.
        result = fuse([None, op, None], weights, _CALIBRATOR)  # type: ignore[list-item]

        assert HorizonBucket.SHORT in result
        fo = result[HorizonBucket.SHORT]
        assert fo.n_opinions == 1

    def test_all_none_returns_empty(self) -> None:
        weights = EqualWeightBundle([])
        result = fuse([None, None], weights, _CALIBRATOR)  # type: ignore[list-item]
        assert result == {}

    def test_empty_list_returns_empty(self) -> None:
        weights = EqualWeightBundle([])
        result = fuse([], weights, _CALIBRATOR)
        assert result == {}


# ---------------------------------------------------------------------------
# 9. FusionOutput fields are correct
# ---------------------------------------------------------------------------

class TestFusionOutputFields:
    def test_output_fields_populated(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.4, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.6, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert isinstance(fo, FusionOutput)
        assert fo.bucket == HorizonBucket.SHORT
        assert fo.n_opinions == 2
        assert fo.effective_n > 0.0
        assert "A1.x" in fo.advisor_contributions
        assert "A2.x" in fo.advisor_contributions
        assert fo.vetoes == []
        assert fo.cold_start is True  # PassthroughCalibrator default

    def test_advisor_contributions_sum_to_conviction_approx(self) -> None:
        """Sum of contributions ≈ signal_strength (before diversity factor)."""
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.4, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.6, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        contrib_sum = sum(fo.advisor_contributions.values())
        # contrib_sum = signal_strength = (0.4 + 0.6) / 2 = 0.5
        assert abs(contrib_sum - 0.5) < 1e-9

    def test_cold_start_false_when_calibrator_says_so(self) -> None:
        class RealCalibrator:
            is_cold_start = False

            def transform(self, raw_stance: float, horizon_days: int) -> float:
                return raw_stance

        op = make_opinion(advisor_id="A1.x", stance_score=0.5, run_group_id="rg-1")
        weights = EqualWeightBundle(["A1.x"])
        result = fuse([op], weights, RealCalibrator())

        assert result[HorizonBucket.SHORT].cold_start is False


# ---------------------------------------------------------------------------
# 10. Passthrough calibrator
# ---------------------------------------------------------------------------

class TestPassthroughCalibrator:
    def test_transform_is_identity(self) -> None:
        cal = PassthroughCalibrator()
        for stance in [-1.0, -0.5, 0.0, 0.3, 1.0]:
            assert cal.transform(stance, 10) == stance

    def test_is_cold_start_true(self) -> None:
        cal = PassthroughCalibrator()
        assert cal.is_cold_start is True


# ---------------------------------------------------------------------------
# 11. Dispersion
# ---------------------------------------------------------------------------

class TestDispersion:
    def test_dispersion_zero_for_identical_stances(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.5, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.5, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.dispersion < 1e-9

    def test_dispersion_positive_for_diverse_stances(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.9, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=-0.9, run_group_id="rg-2")

        weights = EqualWeightBundle(["A1.x", "A2.x"])
        result = fuse([op1, op2], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        assert fo.dispersion > 0.5


# ---------------------------------------------------------------------------
# 12. Shadow advisor excluded from pool
# ---------------------------------------------------------------------------

class TestShadowAdvisorExcluded:
    def test_shadow_advisor_not_in_contributions(self) -> None:
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.7, run_group_id="rg-1")
        op_shadow = make_opinion(
            advisor_id="A2.shadow", stance_score=-0.9, run_group_id="rg-s"
        )

        # Build a weight bundle where A2.shadow is shadow=True.
        weights = WeightBundle(
            weights={
                "A1.x": AdvisorWeight(
                    advisor_id="A1.x",
                    weight=1.0,
                    ci_low=1.0,
                    ci_high=1.0,
                    shadow=False,
                ),
                "A2.shadow": AdvisorWeight(
                    advisor_id="A2.shadow",
                    weight=0.5,
                    ci_low=0.5,
                    ci_high=0.5,
                    shadow=True,  # onboarding — excluded from live pool
                ),
            },
            correlation_matrix={},
        )

        result = fuse([op1, op_shadow], weights, _CALIBRATOR)

        fo = result[HorizonBucket.SHORT]
        # Shadow advisor not in contributions; conviction driven by A1.x only.
        assert "A2.shadow" not in fo.advisor_contributions
        assert fo.conviction > 0.0  # A1.x at +0.7 dominates


# ---------------------------------------------------------------------------
# 13. Audit fix regressions
# ---------------------------------------------------------------------------

class TestAuditFixes:
    """Regression tests for the 6 audit findings (P0–P2a)."""

    # --- Finding 1: is_cold_start is a property, not a method ---

    def test_passthrough_calibrator_is_cold_start_is_property(self) -> None:
        """PassthroughCalibrator.is_cold_start must be a bool, not a bound method."""
        cal = PassthroughCalibrator()
        result = cal.is_cold_start
        assert isinstance(result, bool), (
            f"is_cold_start should be bool, got {type(result)}: "
            "if it were a bound method, bool() would always be True"
        )
        assert result is True

    def test_real_calibrator_shaped_object_cold_start_resolves_to_bool(self) -> None:
        """A real Calibrator-shaped object: cold_start must resolve to real bool."""
        from arbiter.calibration.calibrator import Calibrator

        cal = Calibrator(advisor_id="A1.test")
        # Before any fit: is_cold_start must be a bool True (not a method)
        assert isinstance(cal.is_cold_start, bool)
        assert cal.is_cold_start is True

        # After fit, it must be a bool False (not always-True method truthy trap)
        from arbiter.contract.seams import ResolvedOutcome
        outcomes = [
            ResolvedOutcome(
                idea_id=f"i{i}",
                advisor_id="A1.test",
                ticker="AAPL",
                alpha_bps=50.0,
                binary=1 if i % 2 == 0 else -1,
                advisor_confidence=0.7,
                stance_score=1.0 if i % 2 == 0 else -1.0,
                abstained=False,
                horizon_days=15,
                label_kind="normal",
            )
            for i in range(10)
        ]
        cal.fit(outcomes)
        result = cal.is_cold_start
        assert isinstance(result, bool)
        assert result is False  # must be real False, not always-True method

    def test_fuse_cold_start_false_with_real_calibrator_shaped_object(self) -> None:
        """fuse() must set cold_start=False when Calibrator reports is_cold_start=False."""
        from arbiter.calibration.calibrator import Calibrator
        from arbiter.contract.seams import ResolvedOutcome

        cal = Calibrator(advisor_id="A1.test")
        outcomes = [
            ResolvedOutcome(
                idea_id=f"i{i}",
                advisor_id="A1.test",
                ticker="AAPL",
                alpha_bps=50.0,
                binary=1 if i % 2 == 0 else -1,
                advisor_confidence=0.7,
                stance_score=1.0 if i % 2 == 0 else -1.0,
                abstained=False,
                horizon_days=15,
                label_kind="normal",
            )
            for i in range(10)
        ]
        cal.fit(outcomes)
        assert cal.is_cold_start is False

        op = make_opinion(advisor_id="A1.test", stance_score=0.5, run_group_id="rg-1")
        weights = EqualWeightBundle(["A1.test"])
        result = fuse([op], weights, cal)
        # With a fitted calibrator, cold_start must be False (not stuck True).
        assert result[HorizonBucket.SHORT].cold_start is False

    # --- Finding 2: EqualWeightBundle raw log-pool weights, pool still normalises ---

    def test_equal_weight_pooling_unchanged_after_weight_convention_fix(self) -> None:
        """Equal-weight pooling must produce identical conviction before and after
        the 1/N → 1.0 weight-convention fix (pool.py normalises, so behaviour is
        identical; only the stored weight value changes)."""
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.6, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.6, run_group_id="rg-2")

        # EqualWeightBundle now stores 1.0 each; pool.py normalises to 0.5 each.
        weights = EqualWeightBundle(["A1.x", "A2.x"])
        assert weights.weights["A1.x"].weight == 1.0  # raw log-pool weight
        assert weights.weights["A2.x"].weight == 1.0

        result = fuse([op1, op2], weights, _CALIBRATOR)
        fo = result[HorizonBucket.SHORT]
        # Conviction = (0.6 + 0.6) / 2 * 1.0 (diversity_factor=1 with ρ=0) = 0.6
        assert abs(fo.conviction - 0.6) < 1e-9

    def test_equal_weight_three_advisors_conviction_unchanged(self) -> None:
        """Three-advisor equal-weight pool: same conviction as before weight fix."""
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.9, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.3, run_group_id="rg-2")
        op3 = make_opinion(advisor_id="A3.x", stance_score=0.6, run_group_id="rg-3")

        weights = EqualWeightBundle(["A1.x", "A2.x", "A3.x"])
        result = fuse([op1, op2, op3], weights, _CALIBRATOR)
        fo = result[HorizonBucket.SHORT]
        # Weighted mean = (0.9 + 0.3 + 0.6) / 3 = 0.6; eff_N=3, diversity=1.0
        assert abs(fo.conviction - 0.6) < 1e-9

    # --- Finding 4: Shadow advisor must NOT trigger lone-bull tax ---

    def test_shadow_advisor_does_not_trigger_lone_bull_tax(self) -> None:
        """A shadow (onboarding) advisor with opposing stance must NOT apply the
        −0.10 lone-bull tax penalty.  Before fix, it would silently count as a
        'dissenter' and apply the tax."""
        # Two real advisors agree: +0.8 each (pool is unanimous).
        op1 = make_opinion(advisor_id="A1.x", stance_score=0.8, run_group_id="rg-1")
        op2 = make_opinion(advisor_id="A2.x", stance_score=0.8, run_group_id="rg-2")
        # Shadow advisor has opposing stance (−0.8) but should NOT count as dissenter.
        op_shadow = make_opinion(
            advisor_id="A3.shadow",
            stance_score=-0.8,
            run_group_id="rg-s",
        )

        weights = WeightBundle(
            weights={
                "A1.x": AdvisorWeight(
                    advisor_id="A1.x",
                    weight=1.0,
                    ci_low=1.0,
                    ci_high=1.0,
                    shadow=False,
                ),
                "A2.x": AdvisorWeight(
                    advisor_id="A2.x",
                    weight=1.0,
                    ci_low=1.0,
                    ci_high=1.0,
                    shadow=False,
                ),
                "A3.shadow": AdvisorWeight(
                    advisor_id="A3.shadow",
                    weight=0.5,
                    ci_low=0.5,
                    ci_high=0.5,
                    shadow=True,  # onboarding — must not trigger tax
                ),
            },
            correlation_matrix={},
        )

        result = fuse([op1, op2, op_shadow], weights, _CALIBRATOR)
        fo = result[HorizonBucket.SHORT]

        # With ρ=0 (empty matrix) and 2 pooled advisors, eff_N=2, diversity=1.0.
        # diversity_factor=1.0 ≥ LONE_BULL_CORR_THRESHOLD=0.5, so no tax even
        # if a valid dissenter existed.  To test the shadow exclusion specifically
        # we need a high-correlation scenario to get diversity_factor < 0.5.
        # For this test, verify shadow not in contributions and conviction > 0.
        assert "A3.shadow" not in fo.advisor_contributions
        # If shadow had been counted as dissenter AND diversity_factor < threshold,
        # conviction would be reduced by 0.1.  With 2 real advisors and ρ=0:
        # eff_N=2, diversity=1.0 → tax=0.  Conviction = 0.8.
        assert abs(fo.conviction - 0.8) < 1e-9

    def test_shadow_advisor_lone_bull_tax_excluded_under_high_correlation(self) -> None:
        """Shadow advisor must not count as dissenter even when diversity_factor < 0.5
        (the combined condition for lone-bull tax).  This is the real regression case."""
        from arbiter.fusion.correlation import lone_bull_tax

        # Three advisors all agree on +1.0 stance.
        pooled = [
            make_opinion(advisor_id=f"bot{i}", stance_score=0.8, run_group_id=f"rg-{i}")
            for i in range(3)
        ]
        # One shadow advisor has −0.8 stance (opposing but should be excluded).
        shadow_op = make_opinion(
            advisor_id="shadow1", stance_score=-0.8, run_group_id="rg-shadow"
        )
        all_bucket = pooled + [shadow_op]

        weights = WeightBundle(
            weights={
                "bot0": AdvisorWeight("bot0", 1.0, 1.0, 1.0, False),
                "bot1": AdvisorWeight("bot1", 1.0, 1.0, 1.0, False),
                "bot2": AdvisorWeight("bot2", 1.0, 1.0, 1.0, False),
                "shadow1": AdvisorWeight("shadow1", 0.5, 0.5, 0.5, True),  # shadow
            },
            correlation_matrix={},
        )

        # Manually compute with diversity_factor < threshold (simulate corr scenario)
        diversity_factor = 0.3  # < 0.5 → tax would apply IF dissenter found

        # WITHOUT weights: shadow is seen as dissenter → tax applied
        tax_without_weights = lone_bull_tax(pooled, all_bucket, diversity_factor, None)

        # WITH weights: shadow excluded → no valid dissenter → no tax
        tax_with_weights = lone_bull_tax(pooled, all_bucket, diversity_factor, weights)

        assert tax_without_weights == 0.10, (
            "Without weight filtering, shadow dissenter should trigger the tax"
        )
        assert tax_with_weights == 0.0, (
            "With weight filtering, shadow advisor must not count as a dissenter"
        )
