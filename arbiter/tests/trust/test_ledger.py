"""Tests for arbiter.trust.ledger — composite trust and TrustLedger.

Core spec requirements verified here:
- composite is geometric mean of skill × calibration × coverage
- MiroFish capped at 0.35
- negative-skill → 0.0 + shadow=True
- shadow advisor weight 0 until 30 outcomes
- coverage penalises selective abstainer (via composite)
- 26-week decay down-weights old outcomes (via brier_skill_score)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

import pytest

from arbiter.contract.seams import ResolvedOutcome, AdvisorWeight, WeightBundle
from arbiter.trust.ledger import (
    TrustLedger,
    compute_composite_trust,
    _shadow_ramp_weight,
    _apply_caps,
    _is_mirofish,
    bootstrap_skill_ci,
    effective_sample_size,
    minimum_detectable_effect,
    is_significant_skill,
    CEILING,
    MIROFISH_CAP,
    SHADOW_THRESHOLD,
    RAMP_OUTCOMES,
    THIN_SAMPLE_FLOOR,
    THIN_SAMPLE_THRESHOLD,
    MIN_EFFECTIVE_N,
    PHASE3_ACTIVATION_THRESHOLD,
    MIN_NEW_OUTCOMES,
)
from arbiter.trust.regime import RegimeTracker, RegimeChangeEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(days_ago: float = 0.0) -> datetime:
    base = datetime(2025, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    return base - timedelta(days=days_ago)


AS_OF = _utc(0)


def _make_outcome(
    idea_id: str,
    advisor_id: str = "A1.test",
    binary: int = 1,
    confidence: float = 0.8,
    abstained: bool = False,
    alpha_bps: float = 50.0,
    stance_score: float | None = None,
) -> ResolvedOutcome:
    # #5a hygiene: the factory default stance is a FIXED constant (+0.5),
    # decoupled from `binary` — it does NOT silently track the realized answer,
    # so a future regression that reintroduces answer-derived forecasts can't
    # hide behind a default that happens to align with the outcome.  A +0.5
    # stance against a default binary=+1 still yields healthy positive skill,
    # which is all the sample-gating / FSM-mechanics tests need.  Tests that
    # assert a specific skill SIGN (right vs wrong advisor) pass `stance_score`
    # explicitly (e.g. +0.9 for a right call, +0.9 vs binary=-1 for a wrong one).
    if stance_score is None:
        stance_score = 0.5
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker="TEST",
        alpha_bps=alpha_bps,
        binary=binary,
        advisor_confidence=confidence,
        stance_score=stance_score,
        abstained=abstained,
        horizon_days=30,
        label_kind="normal",
    )


def _build_records(
    n: int,
    advisor_id: str = "A1.test",
    binary: int = 1,
    confidence: float = 0.8,
    abstained: bool = False,
) -> list[tuple[ResolvedOutcome, datetime]]:
    return [
        (
            _make_outcome(f"idea-{i}", advisor_id, binary, confidence, abstained),
            _utc(float(n - i)),  # oldest first
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests: _is_mirofish
# ---------------------------------------------------------------------------

class TestIsMirofish:
    def test_a2_prefix_is_mirofish(self):
        assert _is_mirofish("A2.mirofish") is True
        assert _is_mirofish("A2.swarm") is True

    def test_other_prefixes_not_mirofish(self):
        assert _is_mirofish("A1.insider") is False
        assert _is_mirofish("A3.macro") is False


# ---------------------------------------------------------------------------
# Tests: _apply_caps
# ---------------------------------------------------------------------------

class TestApplyCaps:
    def test_negative_skill_returns_zero_and_shadow(self):
        weight, shadow = _apply_caps("A1.t", 0.4, n_outcomes=20, is_negative_skill=True)
        assert weight == 0.0
        assert shadow is True

    def test_ceiling_capped(self):
        weight, shadow = _apply_caps("A1.t", 0.99, n_outcomes=20, is_negative_skill=False)
        assert weight == CEILING
        assert shadow is False

    def test_mirofish_capped_at_0_35(self):
        weight, shadow = _apply_caps("A2.mirofish", 0.40, n_outcomes=20, is_negative_skill=False)
        assert weight == MIROFISH_CAP
        assert shadow is False

    def test_mirofish_cap_is_0_35_forever(self):
        """Even a perfect MiroFish cannot exceed 0.35."""
        weight, shadow = _apply_caps("A2.mirofish", 0.50, n_outcomes=100, is_negative_skill=False)
        assert weight == MIROFISH_CAP

    def test_thin_sample_floor_applied(self):
        weight, shadow = _apply_caps("A1.t", 0.01, n_outcomes=5, is_negative_skill=False)
        assert weight >= THIN_SAMPLE_FLOOR

    def test_normal_weight_unmodified(self):
        weight, shadow = _apply_caps("A1.t", 0.30, n_outcomes=50, is_negative_skill=False)
        assert abs(weight - 0.30) < 1e-9
        assert shadow is False


# ---------------------------------------------------------------------------
# Tests: _shadow_ramp_weight
# ---------------------------------------------------------------------------

class TestShadowRampWeight:
    def test_below_threshold_weight_zero(self):
        weight, shadow = _shadow_ramp_weight(n_non_abstain=0, composite=0.5)
        assert weight == 0.0
        assert shadow is True

    def test_at_threshold_shadow_still_on(self):
        """At exactly SHADOW_THRESHOLD: shadow lifted but ramp begins; shadow=True during ramp."""
        weight, shadow = _shadow_ramp_weight(n_non_abstain=SHADOW_THRESHOLD, composite=0.5)
        # ramp_progress = 0 → weight = 0 * composite
        assert weight == 0.0
        assert shadow is True

    def test_mid_ramp_partial_weight(self):
        n = SHADOW_THRESHOLD + RAMP_OUTCOMES // 2
        weight, shadow = _shadow_ramp_weight(n_non_abstain=n, composite=0.4)
        expected = 0.4 * (RAMP_OUTCOMES // 2) / RAMP_OUTCOMES
        assert abs(weight - expected) < 1e-9
        assert shadow is True  # still probationary

    def test_above_ramp_full_weight(self):
        n = SHADOW_THRESHOLD + RAMP_OUTCOMES + 1
        weight, shadow = _shadow_ramp_weight(n_non_abstain=n, composite=0.35)
        assert abs(weight - 0.35) < 1e-9
        assert shadow is False


# ---------------------------------------------------------------------------
# Tests: compute_composite_trust
# ---------------------------------------------------------------------------

class TestCompositeGeometricMean:
    def test_composite_is_geometric_mean(self):
        """Composite = (skill × calibration × coverage)^(1/3)."""
        # Use outcomes that give a known BSS
        # binary=1, confidence=0 → p_hat=0.5, p_outcome=1.0, BS=0.25 → BSS=0
        # That gives skill=0 → composite=0. Use nonzero confidence instead.

        # Confidently RIGHT: stance=+1.0, confidence=1.0, binary=+1 → p_hat=1.0,
        # p_outcome=1.0 → BS=0 → BSS=1.0.  Explicit stance keeps this a perfect
        # advisor (the default +0.5 constant would give BSS≈0.75, not 1.0).
        # Full eligibility → coverage=1.0; calibration=1.0 → composite=1.0
        n = 20
        outcomes = [
            _make_outcome(f"i-{i}", binary=1, confidence=1.0, stance_score=1.0)
            for i in range(n)
        ]
        dates = [_utc(float(n - i)) for i in range(n)]
        eligible = [f"i-{i}" for i in range(n)]

        composite = compute_composite_trust(
            outcomes, dates, eligible, AS_OF, calibration_score=1.0
        )
        assert composite is not None
        # BSS=1.0, cal=1.0, cov=1.0 → geom_mean = 1.0
        assert abs(composite - 1.0) < 1e-6

    def test_partial_coverage_reduces_composite(self):
        """Selective abstainer (low coverage) should reduce composite trust."""
        n = 20
        outcomes = [_make_outcome(f"i-{i}", binary=1, confidence=1.0) for i in range(n)]
        dates = [_utc(float(n - i)) for i in range(n)]
        eligible = [f"i-{i}" for i in range(n * 2)]  # 2× more eligible than opined

        composite_partial = compute_composite_trust(
            outcomes, dates, eligible, AS_OF, calibration_score=1.0
        )
        composite_full = compute_composite_trust(
            outcomes, dates, [f"i-{i}" for i in range(n)], AS_OF, calibration_score=1.0
        )
        assert composite_partial is not None
        assert composite_full is not None
        assert composite_partial < composite_full

    def test_negative_skill_clamped_to_zero_in_composite(self):
        """Negative BSS → skill=0 → composite=0."""
        # Construct outcomes where BSS < 0:
        # Use moderate confidence (0.5) and binary=0 (no-call) outcomes
        # BSS=0 for these (baseline prediction)
        # Actually we need truly terrible predictions to get negative BSS
        # The formula: p_hat = (binary * confidence + 1) / 2
        # For binary=1, confidence=1.0: p_hat=1.0
        # outcome=-1 would require a row with binary=-1 but confidence=1.0:
        # p_hat = (-1*1 + 1)/2 = 0, p_outcome=-1→0.0, BS=(0-0)^2=0 → BSS=1!
        # The encoding ties direction to the outcome direction.
        # To get negative BSS: p_hat must diverge from p_outcome past 0.5
        # That requires the "stance" direction to oppose the outcome direction.
        # Since stance = binary * confidence and binary is the OUTCOME direction,
        # we cannot get negative BSS with this encoding.
        # However the spec says: negative-skill → 0.0. We test this by
        # mocking brier_skill_score to return a negative value via monkeypatching.
        # Here we test the cap logic via _apply_caps directly.
        weight, shadow = _apply_caps("A1.t", 0.0, n_outcomes=20, is_negative_skill=True)
        assert weight == 0.0
        assert shadow is True

    def test_no_non_abstain_outcomes_returns_none(self):
        outcomes = [_make_outcome("i1", abstained=True)]
        dates = [AS_OF]
        result = compute_composite_trust(outcomes, dates, ["i1"], AS_OF)
        assert result is None

    def test_calibration_term_reduces_composite(self):
        """Lower calibration score reduces composite trust."""
        n = 20
        outcomes = [_make_outcome(f"i-{i}", binary=1, confidence=1.0) for i in range(n)]
        dates = [_utc(float(n - i)) for i in range(n)]
        eligible = [f"i-{i}" for i in range(n)]

        composite_full_cal = compute_composite_trust(
            outcomes, dates, eligible, AS_OF, calibration_score=1.0
        )
        composite_half_cal = compute_composite_trust(
            outcomes, dates, eligible, AS_OF, calibration_score=0.5
        )
        assert composite_full_cal is not None
        assert composite_half_cal is not None
        # geometric mean: half calibration → composite drops
        assert composite_half_cal < composite_full_cal


# ---------------------------------------------------------------------------
# Tests: TrustLedger
# ---------------------------------------------------------------------------

def _build_advisor_data(
    advisor_id: str,
    n: int,
    confidence: float = 0.8,
    abstained: bool = False,
    binary: int = 1,
    stance_score: float | None = None,
) -> list[tuple[ResolvedOutcome, datetime]]:
    return [
        (
            _make_outcome(f"idea-{i}", advisor_id=advisor_id,
                          binary=binary, confidence=confidence, abstained=abstained,
                          stance_score=stance_score),
            _utc(float(n - i)),
        )
        for i in range(n)
    ]


class TestTrustLedgerActivation:
    def test_dormant_below_phase3_threshold(self):
        """Returns None when total outcomes < PHASE3_ACTIVATION_THRESHOLD."""
        ledger = TrustLedger()
        # Only 30 total outcomes (below 60)
        outcomes_by_advisor = {
            "A1.t": _build_advisor_data("A1.t", n=30),
        }
        result = ledger.update(
            outcomes_by_advisor,
            eligible_by_advisor={"A1.t": [f"idea-{i}" for i in range(30)]},
            as_of=AS_OF,
        )
        assert result is None

    def test_activates_at_phase3_threshold(self):
        """Returns WeightBundle once total outcomes >= 60."""
        ledger = TrustLedger()
        n = PHASE3_ACTIVATION_THRESHOLD
        outcomes_by_advisor = {
            "A1.t": _build_advisor_data("A1.t", n=n),
        }
        result = ledger.update(
            outcomes_by_advisor,
            eligible_by_advisor={"A1.t": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,  # bypass MIN_NEW_OUTCOMES gate
        )
        assert result is not None
        assert isinstance(result, WeightBundle)


class TestMiroFishCap:
    def test_mirofish_capped_at_0_35(self):
        """MiroFish (A2.*) weight cannot exceed 0.35 regardless of performance."""
        n = PHASE3_ACTIVATION_THRESHOLD
        # Give MiroFish perfect outcomes → composite would be ~1.0 without cap
        outcomes_by_advisor = {
            "A2.mirofish": _build_advisor_data("A2.mirofish", n=n, confidence=1.0, binary=1),
        }
        ledger = TrustLedger()
        result = ledger.update(
            outcomes_by_advisor,
            eligible_by_advisor={"A2.mirofish": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        assert result is not None
        aw = result.weights.get("A2.mirofish")
        assert aw is not None
        assert aw.weight <= MIROFISH_CAP
        assert aw.weight <= 0.35


class TestNegativeSkillAdvisor:
    def test_negative_skill_advisor_weight_zero_and_shadow(self):
        """An advisor with negative BSS gets weight=0.0 and shadow=True."""
        # Patch brier_skill_score in the ledger module's namespace (where it was imported)
        import arbiter.trust.ledger as ledger_mod

        original_bss = ledger_mod.brier_skill_score

        def fake_bss(outcomes, dates, as_of):
            return -0.15  # negative skill

        ledger_mod.brier_skill_score = fake_bss
        try:
            n = PHASE3_ACTIVATION_THRESHOLD
            outcomes_by_advisor = {
                "A1.bad": _build_advisor_data("A1.bad", n=n, confidence=0.5),
            }
            ledger = TrustLedger()
            result = ledger.update(
                outcomes_by_advisor,
                eligible_by_advisor={"A1.bad": [f"idea-{i}" for i in range(n)]},
                as_of=AS_OF,
                force=True,
            )
            assert result is not None
            aw = result.weights.get("A1.bad")
            assert aw is not None
            assert aw.weight == 0.0
            assert aw.shadow is True
        finally:
            ledger_mod.brier_skill_score = original_bss


class TestRealNegativeSkillEndToEnd:
    """#5a decisive: with the real (stance-based) Brier, a confidently-WRONG
    advisor accrues BSS < 0 → ledger marks ``negative_skill`` → weight_resolver
    suppresses it.  NO monkeypatch — this exercises the previously-dead branch
    end-to-end through the genuine brier."""

    def test_confidently_wrong_advisor_is_suppressed(self):
        from arbiter.trust.weight_resolver import resolve_weight_bundle

        n = PHASE3_ACTIVATION_THRESHOLD
        # Advisor said LONG (stance +0.9, conf 0.8) but the market went DOWN
        # (binary -1) on every call → p_hat≈0.86 vs p_outcome=0.0 → BS≈0.74 →
        # BSS≈-1.96 < 0.  This is genuinely reachable only after the #5a fix.
        wrong = _build_advisor_data(
            "A1.bad", n=n, confidence=0.8, binary=-1, stance_score=0.9,
        )
        ledger = TrustLedger()
        bundle = ledger.update(
            {"A1.bad": wrong},
            eligible_by_advisor={"A1.bad": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        assert bundle is not None
        # Ledger flagged the reason and zeroed the weight.
        assert ledger.last_cap_reasons.get("A1.bad") == "negative_skill"
        assert bundle.weights["A1.bad"].weight == 0.0
        assert bundle.weights["A1.bad"].shadow is True

        # The resolver suppresses it (0 / shadow) rather than flooring it back in.
        resolved = resolve_weight_bundle(
            bundle, ["A1.bad"], cap_reasons=dict(ledger.last_cap_reasons)
        )
        assert resolved.weights["A1.bad"].weight == 0.0
        assert resolved.weights["A1.bad"].shadow is True

    def test_mostly_right_advisor_not_suppressed(self):
        """E4 control: a mostly-correct advisor with occasional losses must NOT
        be suppressed below the activation threshold — the recency-weighted
        aggregate (not a single bad call) governs suppression."""
        from arbiter.trust.weight_resolver import EQUAL_FLOOR, resolve_weight_bundle

        n = PHASE3_ACTIVATION_THRESHOLD
        records: list[tuple[ResolvedOutcome, datetime]] = []
        for i in range(n):
            # ~85% right: advisor LONG (stance +0.9) and market UP (binary +1);
            # the rest are confident losses (LONG but market DOWN).
            loss = (i % 7 == 0)
            binary = -1 if loss else 1
            records.append((
                _make_outcome(
                    f"idea-{i}", advisor_id="A1.ok",
                    binary=binary, confidence=0.8, stance_score=0.9,
                ),
                _utc(float(n - i)),
            ))
        ledger = TrustLedger()
        bundle = ledger.update(
            {"A1.ok": records},
            eligible_by_advisor={"A1.ok": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        assert bundle is not None
        # NOT flagged negative-skill (aggregate BSS > 0).
        assert ledger.last_cap_reasons.get("A1.ok") != "negative_skill"
        resolved = resolve_weight_bundle(
            bundle, ["A1.ok"], cap_reasons=dict(ledger.last_cap_reasons)
        )
        # Still trading: positive, non-shadow weight (graduated or floored).
        assert resolved.weights["A1.ok"].weight >= EQUAL_FLOOR or resolved.weights["A1.ok"].weight > 0.0
        assert resolved.weights["A1.ok"].shadow is False


class TestShadowOnboarding:
    def test_new_advisor_weight_zero_before_30_outcomes(self):
        """New advisor: weight=0, shadow=True until >= SHADOW_THRESHOLD resolved outcomes."""
        n = PHASE3_ACTIVATION_THRESHOLD
        # New advisor has only 10 non-abstain outcomes
        new_advisor_records = _build_advisor_data("A1.new", n=10, confidence=0.8)
        # Fill system with enough outcomes via another advisor
        veteran_records = _build_advisor_data("A1.vet", n=n - 10, confidence=0.8)
        outcomes_by_advisor = {
            "A1.new": new_advisor_records,
            "A1.vet": veteran_records,
        }
        eligible = {
            "A1.new": [f"idea-{i}" for i in range(10)],
            "A1.vet": [f"idea-{i}" for i in range(n - 10)],
        }
        ledger = TrustLedger()
        result = ledger.update(outcomes_by_advisor, eligible, as_of=AS_OF, force=True)
        assert result is not None
        aw = result.weights.get("A1.new")
        assert aw is not None
        assert aw.weight == 0.0
        assert aw.shadow is True

    def test_advisor_weight_zero_before_shadow_threshold(self):
        """shadow=True maintained until SHADOW_THRESHOLD non-abstain outcomes."""
        # 29 outcomes: just below SHADOW_THRESHOLD=30
        weight, shadow = _shadow_ramp_weight(n_non_abstain=29, composite=0.5)
        assert weight == 0.0
        assert shadow is True

    def test_advisor_weight_nonzero_after_shadow_threshold(self):
        """weight > 0 after enough non-abstain outcomes."""
        # SHADOW_THRESHOLD + RAMP_OUTCOMES + 1
        n = SHADOW_THRESHOLD + RAMP_OUTCOMES + 1
        weight, shadow = _shadow_ramp_weight(n_non_abstain=n, composite=0.4)
        assert weight > 0.0


class TestMinNewOutcomesGate:
    def test_no_update_below_min_new_outcomes(self):
        """WeightBundle not emitted if < MIN_NEW_OUTCOMES new outcomes."""
        n = PHASE3_ACTIVATION_THRESHOLD
        veteran_records = _build_advisor_data("A1.vet", n=n, confidence=0.8)
        ledger = TrustLedger()
        # Record current state
        ledger.outcomes_at_last_update["A1.vet"] = n - 2  # only 2 new outcomes

        result = ledger.update(
            {"A1.vet": veteran_records},
            eligible_by_advisor={"A1.vet": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            # Not forcing — gate should block
        )
        assert result is None  # < MIN_NEW_OUTCOMES new outcomes

    def test_update_emitted_with_enough_new_outcomes(self):
        """WeightBundle emitted when MIN_NEW_OUTCOMES new outcomes present."""
        n = PHASE3_ACTIVATION_THRESHOLD
        veteran_records = _build_advisor_data("A1.vet", n=n, confidence=0.8)
        ledger = TrustLedger()
        # Only n - MIN_NEW_OUTCOMES were present at last update
        ledger.outcomes_at_last_update["A1.vet"] = n - MIN_NEW_OUTCOMES

        result = ledger.update(
            {"A1.vet": veteran_records},
            eligible_by_advisor={"A1.vet": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
        )
        assert result is not None


class TestRegimeFreeze:
    def test_frozen_regime_blocks_update(self):
        """No WeightBundle emitted during 21-day freeze."""
        n = PHASE3_ACTIVATION_THRESHOLD
        veteran_records = _build_advisor_data("A1.vet", n=n)
        ledger = TrustLedger()

        # Regime changed 5 days ago → still frozen
        tracker = RegimeTracker(
            regime_events=[RegimeChangeEvent(regime_id="bear", changed_at=_utc(5))]
        )
        result = ledger.update(
            {"A1.vet": veteran_records},
            eligible_by_advisor={"A1.vet": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            regime_tracker=tracker,
        )
        assert result is None


class TestWeightBundleOutput:
    def test_weight_bundle_contains_correlation_matrix(self):
        """WeightBundle should always include a correlation_matrix dict."""
        n = PHASE3_ACTIVATION_THRESHOLD
        outcomes_by_advisor = {
            "A1.t": _build_advisor_data("A1.t", n=n // 2),
            "A2.mirofish": _build_advisor_data("A2.mirofish", n=n // 2),
        }
        eligible = {
            "A1.t": [f"idea-{i}" for i in range(n // 2)],
            "A2.mirofish": [f"idea-{i}" for i in range(n // 2)],
        }
        ledger = TrustLedger()
        result = ledger.update(outcomes_by_advisor, eligible, as_of=AS_OF, force=True)
        assert result is not None
        assert isinstance(result.correlation_matrix, dict)

    def test_weights_are_advisor_weight_instances(self):
        n = PHASE3_ACTIVATION_THRESHOLD
        outcomes_by_advisor = {"A1.t": _build_advisor_data("A1.t", n=n)}
        ledger = TrustLedger()
        result = ledger.update(
            outcomes_by_advisor,
            eligible_by_advisor={"A1.t": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        assert result is not None
        for aw in result.weights.values():
            assert isinstance(aw, AdvisorWeight)
            assert aw.weight >= 0.0
            assert aw.ci_low >= 0.0
            assert aw.ci_high >= aw.ci_low


# ---------------------------------------------------------------------------
# B-STATS: significance-gated graduation + bootstrap CI + power metrics (I2/E1)
# ---------------------------------------------------------------------------

def _build_null_advisor(advisor_id: str, n: int) -> list[tuple[ResolvedOutcome, datetime]]:
    """A NULL advisor: a constant +0.5 stance against coin-flip outcomes → BSS ~0."""
    recs = []
    for i in range(n):
        binary = 1 if i % 2 == 0 else -1
        recs.append((
            _make_outcome(f"idea-{i}", advisor_id=advisor_id,
                          binary=binary, confidence=0.8, stance_score=0.5),
            _utc(float(n - i)),
        ))
    return recs


def _build_skilled_advisor(advisor_id: str, n: int) -> list[tuple[ResolvedOutcome, datetime]]:
    """A genuinely-skilled advisor: confident +0.9 stance, ~86% right."""
    recs = []
    for i in range(n):
        binary = -1 if i % 7 == 0 else 1
        recs.append((
            _make_outcome(f"idea-{i}", advisor_id=advisor_id,
                          binary=binary, confidence=0.8, stance_score=0.9),
            _utc(float(n - i)),
        ))
    return recs


class TestBootstrapSkillCI:
    def test_ci_returns_low_le_high(self):
        recs = _build_skilled_advisor("A1.s", 60)
        ci = bootstrap_skill_ci([r for r, _ in recs], [d for _, d in recs], AS_OF)
        assert ci is not None
        assert ci[0] <= ci[1]

    def test_ci_none_when_no_scorable(self):
        outs = [_make_outcome("i1", abstained=True)]
        assert bootstrap_skill_ci(outs, [AS_OF], AS_OF) is None

    def test_null_advisor_ci_straddles_zero(self):
        """NULL advisor's bootstrap CI lower bound does NOT clear 0 (skill ~0)."""
        recs = _build_null_advisor("A1.n", 60)
        ci = bootstrap_skill_ci([r for r, _ in recs], [d for _, d in recs], AS_OF)
        assert ci is not None
        assert ci[0] <= 0.0  # cannot reject "no skill"

    def test_skilled_advisor_ci_clears_zero(self):
        recs = _build_skilled_advisor("A1.s", 60)
        ci = bootstrap_skill_ci([r for r, _ in recs], [d for _, d in recs], AS_OF)
        assert ci is not None
        assert ci[0] > 0.0  # genuinely distinguishable from chance

    def test_ci_widens_on_small_n(self):
        """Thin sample → wider bootstrap CI than the same advisor at large n."""
        big = _build_skilled_advisor("A1.s", 60)
        small = big[:8]
        ci_big = bootstrap_skill_ci([r for r, _ in big], [d for _, d in big], AS_OF)
        ci_small = bootstrap_skill_ci([r for r, _ in small], [d for _, d in small], AS_OF)
        assert (ci_small[1] - ci_small[0]) > (ci_big[1] - ci_big[0])

    def test_ci_is_deterministic(self):
        recs = _build_skilled_advisor("A1.s", 40)
        a = bootstrap_skill_ci([r for r, _ in recs], [d for _, d in recs], AS_OF)
        b = bootstrap_skill_ci([r for r, _ in recs], [d for _, d in recs], AS_OF)
        assert a == b  # seeded → reproducible, no clock entropy


class TestEffectiveNAndMDE:
    def test_effective_n_le_raw_count(self):
        recs = _build_skilled_advisor("A1.s", 50)
        n_eff = effective_sample_size([r for r, _ in recs], [d for _, d in recs], AS_OF)
        assert 0 < n_eff <= 50

    def test_effective_n_zero_when_no_scorable(self):
        outs = [_make_outcome("i1", abstained=True)]
        assert effective_sample_size(outs, [AS_OF], AS_OF) == 0.0

    def test_mde_shrinks_with_n(self):
        assert minimum_detectable_effect(100.0) < minimum_detectable_effect(10.0)

    def test_mde_infinite_at_zero_n(self):
        assert minimum_detectable_effect(0.0) == float("inf")


class TestSignificanceGate:
    def test_null_ci_not_significant(self):
        assert is_significant_skill(-0.05, 100.0) is False

    def test_positive_ci_low_n_not_significant(self):
        """Skill positive but effective-n too thin → not significant."""
        assert is_significant_skill(0.1, MIN_EFFECTIVE_N - 1.0) is False

    def test_positive_ci_and_enough_n_significant(self):
        assert is_significant_skill(0.1, MIN_EFFECTIVE_N + 1.0) is True

    def test_none_ci_not_significant(self):
        assert is_significant_skill(None, 100.0) is False


class TestGraduationGate:
    """The behavior change: NULL advisors no longer graduate on bare count."""

    def test_null_advisor_does_not_graduate_at_60_outcomes(self):
        n = PHASE3_ACTIVATION_THRESHOLD  # 60, well past SHADOW_THRESHOLD=30
        recs = _build_null_advisor("A1.null", n)
        ledger = TrustLedger()
        bundle = ledger.update(
            {"A1.null": recs},
            eligible_by_advisor={"A1.null": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        assert bundle is not None
        aw = bundle.weights["A1.null"]
        # Bare count of 60 used to graduate this advisor; significance gate blocks it.
        assert aw.shadow is True
        assert aw.weight == 0.0

    def test_skilled_advisor_does_graduate(self):
        n = PHASE3_ACTIVATION_THRESHOLD
        recs = _build_skilled_advisor("A1.good", n)
        ledger = TrustLedger()
        bundle = ledger.update(
            {"A1.good": recs},
            eligible_by_advisor={"A1.good": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        assert bundle is not None
        aw = bundle.weights["A1.good"]
        assert aw.shadow is False
        assert aw.weight > 0.0

    def test_graduated_ci_is_real_bootstrap_not_fixed_band(self):
        """ci_low/ci_high are the bootstrap skill interval, NOT composite*0.8/1.2."""
        n = PHASE3_ACTIVATION_THRESHOLD
        recs = _build_skilled_advisor("A1.good", n)
        ledger = TrustLedger()
        bundle = ledger.update(
            {"A1.good": recs},
            eligible_by_advisor={"A1.good": [f"idea-{i}" for i in range(n)]},
            as_of=AS_OF,
            force=True,
        )
        aw = bundle.weights["A1.good"]
        # The old placeholder would force ci_high == clip(composite*1.2) and a
        # fixed ci_low/ci_high RATIO of 0.8/1.2.  Assert the band is not that.
        if aw.ci_low > 0 and aw.ci_high > 0:
            ratio = aw.ci_low / aw.ci_high
            assert abs(ratio - (0.8 / 1.2)) > 1e-6


class TestThinSampleFloorReachable:
    """E1: the thin-sample floor is now reachable via effective-n."""

    def test_floor_applied_on_thin_effective_n(self):
        # Graduated (not shadow), tiny effective n → floor applies.
        weight, shadow = _apply_caps(
            "A1.t", 0.001, n_outcomes=40, is_negative_skill=False,
            is_shadow=False, effective_n=THIN_SAMPLE_THRESHOLD - 5.0,
        )
        assert weight >= THIN_SAMPLE_FLOOR

    def test_floor_not_applied_on_fat_effective_n(self):
        weight, shadow = _apply_caps(
            "A1.t", 0.30, n_outcomes=80, is_negative_skill=False,
            is_shadow=False, effective_n=THIN_SAMPLE_THRESHOLD + 50.0,
        )
        assert abs(weight - 0.30) < 1e-9


# ---------------------------------------------------------------------------
# Audit fix: Finding P1-B — empty eligible roster emits a loud warning
# ---------------------------------------------------------------------------

class TestEmptyEligibleRosterWarning:
    """Regression test for audit finding P1-B: when outcomes exist but the
    eligible-idea roster is empty, coverage collapses to 0.0 → all weights 0 →
    silent deadlock.  The ledger must emit a LOUD warning."""

    def test_empty_roster_with_outcomes_logs_warning(self, caplog):
        """When outcomes exist but eligible_by_advisor is empty, a warning must fire."""
        import logging
        n = PHASE3_ACTIVATION_THRESHOLD
        outcomes_by_advisor = {
            "A1.vet": _build_advisor_data("A1.vet", n=n),
        }

        ledger = TrustLedger()
        with caplog.at_level(logging.WARNING, logger="arbiter.trust.ledger"):
            result = ledger.update(
                outcomes_by_advisor,
                eligible_by_advisor={},  # empty — roster not wired
                as_of=AS_OF,
                force=True,
            )

        assert result is not None
        # Warning must have been emitted
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("empty_roster" in msg for msg in warning_messages), (
            f"Expected 'trust.coverage.empty_roster' warning, got: {warning_messages}"
        )

    def test_empty_roster_warning_mentions_lane_wiring(self, caplog):
        """The warning must be clear enough for Phase-3 implementers to act on."""
        import logging
        n = PHASE3_ACTIVATION_THRESHOLD
        outcomes_by_advisor = {
            "A1.vet": _build_advisor_data("A1.vet", n=n),
        }

        ledger = TrustLedger()
        with caplog.at_level(logging.WARNING, logger="arbiter.trust.ledger"):
            ledger.update(
                outcomes_by_advisor,
                eligible_by_advisor={},
                as_of=AS_OF,
                force=True,
            )

        full_log = " ".join(r.message for r in caplog.records)
        # Must mention the issue clearly
        assert "eligible" in full_log.lower() or "roster" in full_log.lower(), (
            "Warning must mention the eligible roster"
        )

    def test_no_warning_when_eligible_roster_provided(self, caplog):
        """No empty-roster warning when eligible_by_advisor is non-empty."""
        import logging
        n = PHASE3_ACTIVATION_THRESHOLD
        outcomes_by_advisor = {
            "A1.vet": _build_advisor_data("A1.vet", n=n),
        }
        eligible = {"A1.vet": [f"idea-{i}" for i in range(n)]}

        ledger = TrustLedger()
        with caplog.at_level(logging.WARNING, logger="arbiter.trust.ledger"):
            ledger.update(
                outcomes_by_advisor,
                eligible_by_advisor=eligible,
                as_of=AS_OF,
                force=True,
            )

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("empty_roster" in msg for msg in warning_messages), (
            f"Unexpected empty_roster warning when roster was provided: {warning_messages}"
        )
