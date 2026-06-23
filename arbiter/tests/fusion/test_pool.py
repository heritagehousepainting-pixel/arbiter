"""Tests for fusion.pool — sign-space + double-count contract (W-LEARN, E2/E4).

Covers the FROZEN contract:
  - calibrators emit P(positive-alpha) ∈ [0, 1]; pool maps to signed via 2*p-1
  - a bearish-calibrated p < 0.5 contributes a NEGATIVE signal
  - an advisor with two run_groups in a bucket has its contributions SUM to the
    signal (no double-count, no dropped contribution)
"""
from __future__ import annotations

from datetime import datetime, timezone

from arbiter.contract.opinion import ConfidenceSource, Opinion
from arbiter.contract.seams import EqualWeightBundle
from arbiter.fusion.pool import pool_opinions

_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _op(
    *,
    advisor_id: str = "A1.test",
    stance_score: float = 0.6,
    run_group_id: str = "rg-001",
    horizon_days: int = 10,
) -> Opinion:
    return Opinion(
        advisor_id=advisor_id,
        ticker="AAPL",
        stance_score=stance_score,
        confidence=0.7,
        confidence_source=ConfidenceSource.SELF_REPORTED,
        horizon_days=horizon_days,
        as_of=_AS_OF,
        rationale="test",
        source_fingerprint="fp",
        run_group_id=run_group_id,
    )


class _ProbCalibrator:
    """Calibrator stub returning a FIXED probability per advisor (in [0, 1])."""

    outputs_probability = True
    is_cold_start = False

    def __init__(self, probs: dict[str, float]) -> None:
        self._probs = probs

    def transform_for(self, advisor_id: str, raw_stance: float, horizon_days: int) -> float:
        return self._probs[advisor_id]

    def transform(self, raw_stance: float, horizon_days: int) -> float:
        return 0.5


class _SignedPassthrough:
    """Calibrator with NO outputs_probability flag — signed [-1, 1] identity."""

    def transform_for(self, advisor_id: str, raw_stance: float, horizon_days: int) -> float:
        return raw_stance

    def transform(self, raw_stance: float, horizon_days: int) -> float:
        return raw_stance


# ---------------------------------------------------------------------------
# Sign-space: probability → 2*p-1
# ---------------------------------------------------------------------------

class TestSignSpace:
    def test_bearish_calibrated_prob_contributes_negative(self) -> None:
        """A bearish calibrated probability (p < 0.5) must produce NEGATIVE signal."""
        op = _op(advisor_id="A1.bear", stance_score=-0.8)
        cal = _ProbCalibrator({"A1.bear": 0.2})  # P(positive-alpha)=0.2 → 2*0.2-1=-0.6
        weights = EqualWeightBundle(["A1.bear"])

        signal, contribs, _ = pool_opinions([op], weights, cal)

        assert signal < 0.0
        assert abs(signal - (2 * 0.2 - 1)) < 1e-9
        assert contribs["A1.bear"] < 0.0

    def test_bullish_calibrated_prob_contributes_positive(self) -> None:
        op = _op(advisor_id="A1.bull", stance_score=0.8)
        cal = _ProbCalibrator({"A1.bull": 0.9})  # 2*0.9-1 = 0.8
        weights = EqualWeightBundle(["A1.bull"])

        signal, _, _ = pool_opinions([op], weights, cal)
        assert abs(signal - 0.8) < 1e-9

    def test_neutral_prob_half_is_zero_signal(self) -> None:
        op = _op(advisor_id="A1.flat", stance_score=0.0)
        cal = _ProbCalibrator({"A1.flat": 0.5})  # 2*0.5-1 = 0.0
        weights = EqualWeightBundle(["A1.flat"])

        signal, _, _ = pool_opinions([op], weights, cal)
        assert abs(signal) < 1e-12

    def test_passthrough_without_flag_is_not_remapped(self) -> None:
        """A signed passthrough (no outputs_probability) is used unchanged."""
        op = _op(advisor_id="A1.x", stance_score=0.4)
        weights = EqualWeightBundle(["A1.x"])

        signal, _, _ = pool_opinions([op], weights, _SignedPassthrough())
        # No 2*p-1 remap: signal IS the raw stance.
        assert abs(signal - 0.4) < 1e-9


# ---------------------------------------------------------------------------
# Double-count: same advisor, two run_groups in one bucket
# ---------------------------------------------------------------------------

class TestDoubleCount:
    def test_two_run_groups_contributions_sum_to_signal(self) -> None:
        """One advisor, two run_groups: contributions must SUM to signal_strength."""
        op1 = _op(advisor_id="A1.x", stance_score=0.4, run_group_id="rg-1")
        op2 = _op(advisor_id="A1.x", stance_score=0.4, run_group_id="rg-2")
        weights = EqualWeightBundle(["A1.x"])

        signal, contribs, norm = pool_opinions([op1, op2], weights, _SignedPassthrough())

        # Two distinct per-(advisor,run_group) contribution entries, no collision.
        assert len(contribs) == 2
        assert abs(sum(contribs.values()) - signal) < 1e-9
        # Single advisor → simplex weight 1.0; mean stance 0.4 → signal 0.4.
        assert abs(signal - 0.4) < 1e-9

    def test_two_run_groups_no_dropped_contribution(self) -> None:
        """Differing stances across the two run_groups both reach the signal."""
        op1 = _op(advisor_id="A1.x", stance_score=0.2, run_group_id="rg-1")
        op2 = _op(advisor_id="A1.x", stance_score=0.8, run_group_id="rg-2")
        weights = EqualWeightBundle(["A1.x"])

        signal, contribs, _ = pool_opinions([op1, op2], weights, _SignedPassthrough())

        assert len(contribs) == 2
        assert abs(sum(contribs.values()) - signal) < 1e-9
        # Mean of 0.2 and 0.8 = 0.5.
        assert abs(signal - 0.5) < 1e-9

    def test_multi_advisor_contributions_sum_to_signal(self) -> None:
        """Two advisors (one with 2 run_groups) — all contributions sum to signal."""
        ops = [
            _op(advisor_id="A1.x", stance_score=0.6, run_group_id="rg-1"),
            _op(advisor_id="A1.x", stance_score=0.2, run_group_id="rg-2"),
            _op(advisor_id="A2.y", stance_score=-0.4, run_group_id="rg-3"),
        ]
        weights = EqualWeightBundle(["A1.x", "A2.y"])

        signal, contribs, norm = pool_opinions(ops, weights, _SignedPassthrough())

        assert abs(sum(contribs.values()) - signal) < 1e-9
        # A1.x simplex weight 0.5 over mean(0.6,0.2)=0.4 → 0.2; A2.y 0.5*-0.4=-0.2.
        assert abs(signal - 0.0) < 1e-9
        assert len(norm) == 2  # weights still keyed per advisor
