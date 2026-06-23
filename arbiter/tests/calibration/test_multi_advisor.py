"""Tests for the calibrator seam (#4, D5): transform_for + MultiAdvisorCalibrator."""
from __future__ import annotations

from arbiter.calibration.calibrator import Calibrator
from arbiter.calibration.multi_advisor import MultiAdvisorCalibrator
from arbiter.calibration.stance_base import lookup_prior
from arbiter.contract.seams import ResolvedOutcome
from arbiter.fusion.engine import PassthroughCalibrator
from arbiter.types import HorizonBucket


def _outcome(advisor_id: str, binary: int, conf: float = 0.9) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id="i",
        advisor_id=advisor_id,
        ticker="AAA",
        alpha_bps=10.0,
        binary=binary,
        advisor_confidence=conf,
        stance_score=float(binary),
        abstained=False,
        horizon_days=90,
        label_kind="normal",
    )


def test_passthrough_transform_for_defaults_to_transform():
    pc = PassthroughCalibrator()
    assert pc.transform_for("A1.insider", 0.7, 90) == pc.transform(0.7, 90) == 0.7


def test_calibrator_transform_for_defaults_to_transform():
    cal = Calibrator("A1.insider")
    # Cold start → passthrough-equivalent prior.
    assert cal.transform_for("A1.insider", 0.5, 90) == cal.transform(0.5, 90)


def test_multi_advisor_routes_per_advisor_and_cold_start_flag():
    """T7: all cold → is_cold_start True; transform_for ≈ STANCE_BASE prior."""
    ci = Calibrator("A1.insider")
    cc = Calibrator("A1.congress")
    mac = MultiAdvisorCalibrator({"A1.insider": ci, "A1.congress": cc})
    assert mac.is_cold_start is True

    # cold-start routes to each advisor's prior
    bucket = HorizonBucket.MEDIUM
    assert mac.transform_for("A1.insider", 0.6, 90) == lookup_prior("A1.insider", 0.6, bucket)
    assert mac.transform_for("A1.congress", 0.6, 90) == lookup_prior("A1.congress", 0.6, bucket)


def test_multi_advisor_cold_start_flips_after_meaningful_fit():
    """After fitting one advisor with a MEANINGFUL sample (≥ the wiring-level
    threshold), is_cold_start → False.  (P1-b / D5: a fitted model is only
    APPLIED above the meaningful-sample gate, not at the base 2-sample fit.)"""
    from arbiter.calibration.multi_advisor import MIN_APPLY_NONZERO_OUTCOMES

    ci = Calibrator("A1.insider")
    n = MIN_APPLY_NONZERO_OUTCOMES
    outcomes = [_outcome("A1.insider", 1)] * n + [_outcome("A1.insider", -1)] * n
    ci.fit(outcomes)
    cc = Calibrator("A1.congress")  # still cold
    mac = MultiAdvisorCalibrator({"A1.insider": ci, "A1.congress": cc})
    assert mac.is_cold_start is False  # at least one APPLIED


def test_multi_advisor_thin_fit_stays_passthrough():
    """P1-b / D5: an advisor with a fitted-but-thin model (≥2 but < the
    wiring-level threshold non-zero outcomes) stays PASSTHROUGH — is_cold_start
    True and transform routes through the cold-start prior, NOT the fragile fit."""
    ci = Calibrator("A1.insider")
    # 3 non-zero outcomes: base Calibrator fits (≥ _MIN_FIT_SAMPLES=2) but this
    # is below the meaningful-sample gate, so the adapter must NOT apply it.
    outcomes = [_outcome("A1.insider", 1)] * 2 + [_outcome("A1.insider", -1)] * 1
    ci.fit(outcomes)
    assert ci.is_cold_start is False  # base calibrator DID fit a model
    mac = MultiAdvisorCalibrator({"A1.insider": ci})
    # …but the wiring-level gate keeps the adapter cold / passthrough-equivalent.
    assert mac.is_cold_start is True
    bucket = HorizonBucket.MEDIUM
    assert mac.transform_for("A1.insider", 0.6, 90) == lookup_prior(
        "A1.insider", 0.6, bucket
    )


def test_thin_sample_stays_passthrough_equivalent():
    """D5: a single non-zero outcome (< _MIN_FIT_SAMPLES) does NOT fit a model —
    transform stays the cold-start prior (passthrough-equivalent)."""
    cal = Calibrator("A1.insider")
    cal.fit([_outcome("A1.insider", 1)])  # one sample only
    assert cal.is_cold_start is True
    assert cal.transform(0.5, 90) == lookup_prior("A1.insider", 0.5, HorizonBucket.MEDIUM)


def test_predict_proba_clamped_no_degenerate():
    """All routed probs stay within [0,1] (clamp guards against NaN/degenerate)."""
    from arbiter.calibration.multi_advisor import MIN_APPLY_NONZERO_OUTCOMES

    ci = Calibrator("A1.insider")
    n = MIN_APPLY_NONZERO_OUTCOMES
    outcomes = [_outcome("A1.insider", 1)] * n + [_outcome("A1.insider", -1)] * n
    ci.fit(outcomes)
    mac = MultiAdvisorCalibrator({"A1.insider": ci})
    for s in (-1.0, -0.3, 0.0, 0.5, 1.0):
        p = mac.transform_for("A1.insider", s, 90)
        assert 0.0 <= p <= 1.0
